from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Callable

from app.services.normalizer import parse_date
from app.services.storage import RunStore

CLEANING_RULESET_VERSION = "0.8.0"
RULESET_CONFIG = {
    "relevance_exclude_below": 0.28,
    "relevance_review_below": 0.58,
    "market_review_below": 0.25,
    "spam_exclude_at": 0.82,
    "bot_suspicious_at": 0.45,
    "bot_likely_automated_at": 0.80,
    "coordination_window_hours": 6,
    "coordination_similarity": 0.82,
    "story_similarity": 0.72,
    "same_author_near_duplicate_similarity": 0.85,
    "principle": "Conservative evidence scoring; no public-data rule proves a human is fake. Uncertain cases go to human review.",
}

TOKEN_RE = re.compile(r"[A-Za-zΑ-Ωα-ωΆ-ώ0-9]+", re.UNICODE)
HASHTAG_RE = re.compile(r"#[\wΑ-Ωα-ωΆ-ώ]+", re.UNICODE)
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\s().-]*){8,}")

GREEK_COMMON = {
    "και", "στο", "στη", "στην", "στης", "στην", "απο", "από", "για", "με", "σε", "την", "τον", "το", "της",
    "ελλαδα", "ελλάδα", "ελλην", "αθηνα", "αθήνα", "θεσσαλονικη", "θεσσαλονίκη", "κυπρο", "κύπρο", "κερδ",
}
GREEKLISH_COMMON = {
    "kai", "sto", "sti", "stin", "apo", "gia", "me", "tin", "ton", "ellada", "ellhn", "athina", "thessaloniki",
    "kerdisa", "kerdise", "kerd", "xthes", "simera", "stoixima", "tzoker", "opap",
}
GREECE_MARKET_TERMS = {
    "greece", "greek", "hellas", "ellada", "ellhn", "ελλαδα", "ελλάδα", "ελλην", "athens", "athina", "αθηνα", "αθήνα",
    "thessaloniki", "θεσσαλονικη", "θεσσαλονίκη", "opap", "οπαπ",
}
MEDIA_HINTS = {
    "news", "media", "tv", "radio", "press", "times", "daily", "journal", "newspaper", "ειδησεις", "ειδήσεις", "nea", "νέα",
}
PROMO_TERMS = {
    "buy", "order", "sale", "discount", "offer", "register", "registration", "book now", "shop", "price", "promo", "sponsored",
    "αγορα", "αγορά", "παραγγειλε", "παράγγειλε", "προσφορα", "προσφορά", "εκπτωση", "έκπτωση", "εγγραφη", "εγγραφή",
    "κρατηση", "κράτηση", "τιμη", "τιμή", "διαγωνισμος", "διαγωνισμός",
}
SPAM_TERMS = {
    "whatsapp", "telegram", "dm me", "click link", "guaranteed win", "100% guaranteed", "free money", "crypto signal",
    "στείλε dm", "στειλε dm", "σίγουρο κέρδος", "σιγουρο κερδος",
}
REPOST_PREFIXES = ("rt @", "repost", "via @", "shared from", "αναδημοσίευση", "αναδημοσιευση")

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "you", "your", "are", "was", "were", "have", "has", "had", "into",
    "και", "για", "στο", "στη", "στην", "της", "τον", "την", "από", "απο", "που", "με", "σε", "ένα", "μια", "το", "τα",
}


class CleaningCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TextView:
    folded: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fold(value: str | None) -> str:
    value = str(value or "").strip().casefold()
    decomposed = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    value = URL_RE.sub(" ", value)
    value = re.sub(r"[^a-z0-9α-ω\s#@]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _text_view(value: str | None) -> TextView:
    folded = _fold(value)
    tokens = tuple(t for t in TOKEN_RE.findall(folded) if t and t not in STOPWORDS)
    return TextView(folded=folded, tokens=tokens, token_set=frozenset(tokens))


def _term_present(folded_text: str, term: str) -> bool:
    term_folded = _fold(term)
    if not term_folded:
        return False
    if " " in term_folded:
        return term_folded in folded_text
    return re.search(rf"(?<!\w){re.escape(term_folded)}(?!\w)", folded_text, re.UNICODE) is not None


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _raw_first(raw: dict, keys: tuple[str, ...], default=None):
    for key in keys:
        cur = raw
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur not in (None, ""):
            return cur
    return default


def _as_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _metric_impact(row: dict) -> int:
    return int(row.get("views", 0) or 0) + 20 * int(row.get("likes", 0) or 0) + 30 * int(row.get("comments", 0) or 0) + 50 * int(row.get("shares", 0) or 0)


def _account_type(row: dict, plan: dict, view: TextView) -> tuple[str, float, list[str]]:
    source = row.get("platform") or ""
    author = _fold(row.get("author"))
    reasons: list[str] = []
    client = _fold(plan.get("client"))
    topic = _fold(plan.get("topic"))

    if source == "news":
        return "media", 0.98, ["news_source"]
    if author and ((client and client in author) or (topic and len(topic) >= 5 and topic in author)):
        return "brand_owned", 0.86, ["author_matches_client_or_topic"]
    if any(h in author for h in MEDIA_HINTS):
        return "media", 0.72, ["media_name_hint"]

    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    verified = bool(_raw_first(raw, ("verified", "isVerified", "authorMeta.verified", "owner.is_verified"), False))
    category = _fold(_raw_first(raw, ("category", "pageCategory", "businessCategory", "authorMeta.signature"), ""))
    if any(h in category for h in ("company", "organization", "business", "brand", "εταιρ", "οργαν")):
        return "organization", 0.78, ["organization_category"]
    if verified and any(h in author for h in MEDIA_HINTS):
        return "media", 0.86, ["verified_media_hint"]
    if source in {"x", "tiktok", "instagram", "youtube", "facebook"} and author:
        return "person_or_creator", 0.58, ["social_author_present"]
    return "unknown", 0.35, reasons


def _content_class(row: dict, account_type: str, view: TextView) -> tuple[str, float, list[str]]:
    text = view.folded
    reasons: list[str] = []
    if account_type == "brand_owned":
        return "owned", 0.92, ["brand_owned_account"]
    if account_type == "media" or row.get("platform") == "news":
        return "news", 0.93, ["media_or_news_source"]
    if any(text.startswith(prefix) for prefix in REPOST_PREFIXES) or _raw_first(row.get("raw_data") or {}, ("retweetedTweet", "quotedTweet", "parentPost", "parentData")):
        return "repost", 0.82, ["repost_or_parent_reference"]
    promo_hits = [term for term in PROMO_TERMS if _term_present(text, term)]
    hashtags = HASHTAG_RE.findall(str(row.get("text") or ""))
    if promo_hits or (len(hashtags) >= 6 and len(hashtags) > max(2, len(view.tokens) * 0.28)):
        reasons.extend([f"promo_term:{x}" for x in sorted(promo_hits)[:4]])
        if len(hashtags) >= 6:
            reasons.append("heavy_hashtag_usage")
        return "promotional", min(0.95, 0.65 + 0.07 * len(reasons)), reasons
    if account_type == "person_or_creator":
        return "organic", 0.62, ["person_or_creator_without_promo_signal"]
    return "unknown", 0.4, reasons


def _market_score(row: dict, plan: dict, view: TextView) -> tuple[float, list[str]]:
    if str(plan.get("market", "")).casefold() != "greece":
        market = _fold(plan.get("market"))
        return (0.9, ["market_term_match"]) if market and market in view.folded else (0.55, ["non_greece_market_no_native_rule"])

    text = str(row.get("text") or "")
    greek_chars = sum(1 for ch in text if "\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff")
    alphabetic = sum(1 for ch in text if ch.isalpha()) or 1
    greek_ratio = greek_chars / alphabetic
    score = 0.0
    reasons: list[str] = []
    if greek_ratio >= 0.08:
        score += 0.62
        reasons.append("greek_script")
    elif greek_ratio > 0:
        score += 0.35
        reasons.append("some_greek_script")

    ambiguous_own_terms = {_fold(plan.get("topic")), _fold(plan.get("client"))}
    market_hits = [term for term in GREECE_MARKET_TERMS if _fold(term) not in ambiguous_own_terms and _term_present(view.folded, term)]
    if market_hits:
        score += min(0.45, 0.18 + 0.09 * len(market_hits))
        reasons.extend([f"greece_term:{x}" for x in sorted(market_hits)[:3]])

    greeklish_hits = [term for term in GREEKLISH_COMMON if _fold(term) not in ambiguous_own_terms and _term_present(view.folded, term)]
    greek_common_hits = [term for term in GREEK_COMMON if _term_present(view.folded, term)]
    if len(greeklish_hits) >= 2:
        score += 0.35
        reasons.append("greeklish_pattern")
    if len(greek_common_hits) >= 2:
        score += 0.22
        reasons.append("greek_language_pattern")

    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    location = _fold(_raw_first(raw, ("locationName", "location", "authorMeta.location", "pageLocation", "country"), ""))
    if any(term in location for term in ("greece", "athens", "attica", "thessaloniki", "ελλαδα", "αθηνα")):
        score += 0.35
        reasons.append("greek_location_metadata")

    return min(1.0, score), reasons


def _relevance_score(row: dict, plan: dict, view: TextView, market_score: float) -> tuple[float, list[str], list[str]]:
    core_terms = [x for x in plan.get("core_terms", []) if str(x).strip()]
    context_terms = [x for x in plan.get("context_terms", []) if str(x).strip()]
    greeklish_terms = [x for x in plan.get("greeklish_variants", []) if str(x).strip()]
    exclusions = [x for x in plan.get("exclusions", []) if str(x).strip()]

    core_hits = [x for x in core_terms if _term_present(view.folded, x)]
    core_folded = {_fold(x) for x in core_terms if _fold(x)}
    context_candidates = [x for x in [*context_terms, *greeklish_terms] if _fold(x) not in core_folded]
    context_hits = [x for x in context_candidates if _term_present(view.folded, x)]
    exclusion_hits = [x for x in exclusions if _term_present(view.folded, x)]

    score = 0.0
    reasons: list[str] = []
    flags: list[str] = []
    if core_hits:
        score += min(0.72, 0.58 + 0.07 * (len(core_hits) - 1))
        reasons.extend([f"core_term:{x}" for x in core_hits[:3]])
    if context_hits:
        score += min(0.22, 0.08 + 0.05 * len(context_hits))
        reasons.extend([f"context_term:{x}" for x in context_hits[:3]])
    score += 0.16 * market_score

    if exclusion_hits:
        score -= 0.55
        reasons.extend([f"exclusion_term:{x}" for x in exclusion_hits[:3]])
        flags.append("explicit_exclusion_context")

    # Short/acronym topics are ambiguous by nature. Require market/context evidence.
    topic = _fold(plan.get("topic"))
    if core_hits and len(topic.replace(" ", "")) <= 5 and market_score < 0.35 and not context_hits:
        score -= 0.32
        flags.append("ambiguous_short_entity")
        reasons.append("short_entity_without_market_context")

    if not core_hits:
        score -= 0.18
        flags.append("core_term_missing")
        reasons.append("core_term_missing")

    return max(0.0, min(1.0, score)), reasons, flags


def _spam_score(row: dict, view: TextView, content_class: str) -> tuple[float, list[str]]:
    text_raw = str(row.get("text") or "")
    score = 0.0
    reasons: list[str] = []
    spam_hits = [term for term in SPAM_TERMS if _term_present(view.folded, term)]
    if spam_hits:
        score += min(0.7, 0.38 + 0.14 * len(spam_hits))
        reasons.extend([f"spam_term:{x}" for x in spam_hits[:4]])
    hashtags = HASHTAG_RE.findall(text_raw)
    if len(hashtags) >= 10:
        score += 0.22
        reasons.append("excessive_hashtags")
    if PHONE_RE.search(text_raw) and content_class == "promotional":
        score += 0.28
        reasons.append("promo_contact_number")
    url_count = len(URL_RE.findall(text_raw))
    if url_count >= 3:
        score += 0.2
        reasons.append("many_links")
    if len(view.tokens) <= 3 and len(hashtags) >= 5:
        score += 0.18
        reasons.append("thin_hashtag_only_content")
    return min(1.0, score), reasons


def _base_bot_risk(row: dict, view: TextView, spam_score: float) -> tuple[float, list[str]]:
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    risk = 0.0
    reasons: list[str] = []
    followers = _as_int(_raw_first(raw, ("followers", "followerCount", "authorFollowers", "followersCount", "authorMeta.followers")))
    following = _as_int(_raw_first(raw, ("following", "followingCount", "authorFollowing", "authorMeta.following")))
    posts = _as_int(_raw_first(raw, ("postsCount", "statusesCount", "authorMeta.video", "mediaCount")))

    # Never treat low follower count alone as a bot signal.
    if followers is not None and following is not None and followers <= 5 and following >= 1500:
        risk += 0.18
        reasons.append("extreme_following_ratio")
    if posts is not None and posts >= 50000:
        risk += 0.12
        reasons.append("extreme_post_volume_metadata")
    if spam_score >= 0.6:
        risk += 0.24
        reasons.append("strong_spam_signal")
    if len(view.tokens) <= 2 and spam_score > 0.25:
        risk += 0.1
        reasons.append("thin_spam_like_content")
    return min(1.0, risk), reasons


def _author_key(row: dict) -> str:
    author = _fold(row.get("author"))
    return f"{row.get('platform','')}::{author}" if author else ""


def _url_key(row: dict) -> str:
    return _fold(row.get("url"))


def _duplicate_key(row: dict, view: TextView) -> str:
    author = _author_key(row)
    url = _url_key(row)
    if url:
        return "url:" + url
    if author and view.folded:
        return "author_text:" + sha1(f"{author}|{view.folded}".encode("utf-8")).hexdigest()
    return ""


def _candidate_pairs(views: list[TextView]) -> set[tuple[int, int]]:
    index: dict[str, list[int]] = defaultdict(list)
    pairs: set[tuple[int, int]] = set()
    for idx, view in enumerate(views):
        useful = sorted({t for t in view.token_set if len(t) >= 4})[:40]
        candidates: set[int] = set()
        for token in useful:
            candidates.update(index[token][-80:])
        for other in candidates:
            pairs.add((other, idx))
        for token in useful:
            index[token].append(idx)
    return pairs


def _cluster_records(rows: list[dict], views: list[TextView], account_types: list[str]) -> tuple[list[str | None], list[str | None], dict[int, float], dict[int, list[str]]]:
    n = len(rows)
    story_parent = list(range(n))
    coord_parent = list(range(n))
    coord_risk_add: dict[int, float] = defaultdict(float)
    coord_reasons: dict[int, list[str]] = defaultdict(list)

    def find(parent, x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(parent, a, b):
        ra, rb = find(parent, a), find(parent, b)
        if ra != rb:
            parent[rb] = ra

    # Exact same text across different authors is a coordination/story signal, not an exact duplicate deletion.
    exact_text_groups: dict[str, list[int]] = defaultdict(list)
    for i, view in enumerate(views):
        if len(view.folded) >= 24:
            exact_text_groups[view.folded].append(i)
    for indices in exact_text_groups.values():
        authors = {_author_key(rows[i]) for i in indices if _author_key(rows[i])}
        if len(indices) < 2 or len(authors) < 2:
            continue
        all_media = all(account_types[i] == "media" for i in indices)
        parent = story_parent if all_media else coord_parent
        for i in indices[1:]:
            union(parent, indices[0], i)

    for a, b in _candidate_pairs(views):
        if a == b:
            continue
        sim = _jaccard(views[a].token_set, views[b].token_set)
        if sim < 0.68:
            continue
        author_a, author_b = _author_key(rows[a]), _author_key(rows[b])
        if author_a and author_a == author_b:
            continue
        media_pair = account_types[a] == account_types[b] == "media"
        if media_pair and sim >= RULESET_CONFIG["story_similarity"]:
            union(story_parent, a, b)
            continue
        if sim >= RULESET_CONFIG["coordination_similarity"]:
            da, db = parse_date(rows[a].get("date")), parse_date(rows[b].get("date"))
            close_in_time = bool(da and db and abs((da - db).total_seconds()) <= RULESET_CONFIG["coordination_window_hours"] * 3600)
            if close_in_time:
                union(coord_parent, a, b)

    def group_ids(parent, prefix: str, min_size: int = 2) -> list[str | None]:
        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            groups[find(parent, i)].append(i)
        result: list[str | None] = [None] * n
        counter = 0
        for members in sorted(groups.values(), key=lambda x: min(x)):
            if len(members) < min_size:
                continue
            counter += 1
            gid = f"{prefix}-{counter:04d}"
            for i in members:
                result[i] = gid
        return result

    story_ids = group_ids(story_parent, "story")
    coord_ids = group_ids(coord_parent, "coord")

    coord_members: dict[str, list[int]] = defaultdict(list)
    for i, cid in enumerate(coord_ids):
        if cid:
            coord_members[cid].append(i)
    for cid, members in coord_members.items():
        distinct_authors = len({_author_key(rows[i]) for i in members if _author_key(rows[i])})
        if distinct_authors < 2:
            continue
        strength = min(0.64, 0.32 + 0.08 * min(5, len(members) - 2))
        for i in members:
            coord_risk_add[i] += strength
            coord_reasons[i].append(f"coordinated_near_duplicate_cluster:{cid}")

    return story_ids, coord_ids, coord_risk_add, coord_reasons


def _author_behavior(rows: list[dict], views: list[TextView]) -> tuple[dict[int, float], dict[int, list[str]]]:
    by_author: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        key = _author_key(row)
        if key:
            by_author[key].append(i)

    risk_add: dict[int, float] = defaultdict(float)
    reasons: dict[int, list[str]] = defaultdict(list)
    for _, indices in by_author.items():
        if len(indices) < 4:
            continue
        dates = sorted(parse_date(rows[i].get("date")) for i in indices if parse_date(rows[i].get("date")))
        if len(dates) >= 5:
            gaps = [(b - a).total_seconds() for a, b in zip(dates, dates[1:])]
            rapid = sum(1 for gap in gaps if 0 <= gap <= 180)
            if rapid >= 4:
                for i in indices:
                    risk_add[i] += 0.32
                    reasons[i].append("rapid_repetitive_posting")

        urls = [_url_key(rows[i]) for i in indices if _url_key(rows[i])]
        url_counts = Counter(urls)
        repeated_url = any(c >= 3 for c in url_counts.values())
        if repeated_url:
            for i in indices:
                risk_add[i] += 0.18
                reasons[i].append("repeated_same_link_by_author")

        if len(indices) >= 5:
            sims = []
            base = views[indices[0]].token_set
            for j in indices[1:]:
                sims.append(_jaccard(base, views[j].token_set))
            if sims and sum(1 for s in sims if s >= 0.86) >= 3:
                for i in indices:
                    risk_add[i] += 0.24
                    reasons[i].append("repeated_near_identical_author_content")
    return risk_add, reasons


def _origin_class(account_type: str, content_class: str) -> str:
    if account_type == "brand_owned" or content_class == "owned":
        return "owned"
    if account_type == "media" or content_class == "news":
        return "earned_media"
    if account_type == "person_or_creator":
        return "earned_person"
    if account_type == "organization":
        return "earned_organization"
    return "unknown"


def _decision(relevance: float, market: float, spam: float, bot_risk: float, bot_reason_count: int, flags: list[str], content_class: str, impact: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if "exact_duplicate" in flags or "near_duplicate_same_author" in flags:
        return "excluded", ["duplicate_not_independent_evidence"]
    if relevance < RULESET_CONFIG["relevance_exclude_below"]:
        return "excluded", ["low_relevance"]
    if spam >= RULESET_CONFIG["spam_exclude_at"]:
        return "excluded", ["high_spam_risk"]
    if bot_risk >= RULESET_CONFIG["bot_likely_automated_at"] and bot_reason_count >= 2:
        return "excluded", ["high_automation_or_manipulation_risk"]
    if RULESET_CONFIG["relevance_exclude_below"] <= relevance < RULESET_CONFIG["relevance_review_below"]:
        reasons.append("relevance_uncertain")
    if str(content_class) == "unknown" and relevance < 0.68:
        reasons.append("content_context_uncertain")
    if RULESET_CONFIG["bot_suspicious_at"] <= bot_risk < RULESET_CONFIG["bot_likely_automated_at"]:
        reasons.append("authenticity_uncertain")
    if market < RULESET_CONFIG["market_review_below"]:
        reasons.append("market_relevance_uncertain")
    if impact >= 10000 and bot_risk >= 0.35:
        reasons.append("high_impact_suspicious_activity")
    if "ambiguous_short_entity" in flags:
        reasons.append("entity_disambiguation_needed")
    return ("review", reasons) if reasons else ("trusted", [])


def _quality_report(cleaned: list[dict], plan: dict) -> dict:
    total = len(cleaned)
    trusted = [r for r in cleaned if r["cleaning"]["decision"] == "trusted"]
    review = [r for r in cleaned if r["cleaning"]["decision"] == "review"]
    excluded = [r for r in cleaned if r["cleaning"]["decision"] == "excluded"]
    organic = [r for r in trusted if r["cleaning"].get("organic_eligible")]
    independent_units = sum(float(r["cleaning"].get("independent_voice_weight", 1.0) or 0.0) for r in trusted)

    def count_flag(flag: str) -> int:
        return sum(1 for r in cleaned if flag in r["cleaning"].get("flags", []))

    with_text = sum(1 for r in cleaned if str(r.get("text") or "").strip())
    with_date = sum(1 for r in cleaned if r.get("date"))
    with_evidence = sum(1 for r in cleaned if r.get("url") or str(r.get("text") or "").strip())
    completeness = (with_text + with_date + with_evidence) / max(1, total * 3)
    avg_conf = sum(float(r["cleaning"].get("confidence", 0)) for r in cleaned) / max(1, total)
    target = int(plan.get("target_total", 0) or 0)
    achievement = min(1.0, len(trusted) / max(1, target)) if target else (1.0 if trusted else 0.0)
    selected_sources = {sp.get("source") for sp in plan.get("sources", []) if sp.get("source")}
    observed_sources = {r.get("platform") for r in cleaned if r.get("platform")}
    source_coverage = len(selected_sources & observed_sources) / max(1, len(selected_sources))
    contamination = len(excluded) / max(1, total)

    score = 100 * (0.28 * completeness + 0.24 * avg_conf + 0.28 * achievement + 0.20 * source_coverage) - 12 * contamination
    score = int(round(max(0.0, min(100.0, score))))
    label = "high" if score >= 80 else ("medium" if score >= 60 else "low")

    story_ids = {r["cleaning"].get("story_cluster_id") for r in cleaned if r["cleaning"].get("story_cluster_id")}
    coord_ids = {r["cleaning"].get("coordination_cluster_id") for r in cleaned if r["cleaning"].get("coordination_cluster_id")}
    likely_auto = sum(1 for r in cleaned if r["cleaning"].get("authenticity_status") == "likely_automated")
    suspicious = sum(1 for r in cleaned if r["cleaning"].get("authenticity_status") == "suspicious")
    promo = sum(1 for r in cleaned if r["cleaning"].get("content_class") == "promotional")
    high_market = sum(1 for r in cleaned if float(r["cleaning"].get("market_score", 0)) >= 0.65)
    source_targets = {sp.get("source"): int(sp.get("target_items", 0) or 0) for sp in plan.get("sources", []) if sp.get("source")}
    source_breakdown = {}
    for source in sorted(selected_sources):
        source_rows = [r for r in cleaned if r.get("platform") == source]
        source_trusted = [r for r in source_rows if r["cleaning"]["decision"] == "trusted"]
        source_review = [r for r in source_rows if r["cleaning"]["decision"] == "review"]
        target_n = source_targets.get(source, 0)
        observed_n = len(source_rows)
        target_ratio = min(1.0, observed_n / max(1, target_n)) if target_n else (1.0 if observed_n else 0.0)
        source_breakdown[source] = {
            "target": target_n, "observed": observed_n, "trusted": len(source_trusted), "review": len(source_review),
            "excluded": observed_n - len(source_trusted) - len(source_review), "collection_target_ratio": round(target_ratio, 4),
            "collection_target_status": "target_met" if target_n and observed_n >= target_n else ("partial" if observed_n else "no_data"),
            "platform_completeness": "not_claimed",
        }

    return {
        "ruleset_version": CLEANING_RULESET_VERSION,
        "generated_at": _utcnow(),
        "total_records": total,
        "trusted_records": len(trusted),
        "review_records": len(review),
        "excluded_records": len(excluded),
        "organic_opinion_records": len(organic),
        "independent_evidence_units": round(independent_units, 4),
        "sample_target": target,
        "trusted_sample_shortfall": max(0, target - len(trusted)),
        "sample_achievement_ratio": round(achievement, 4),
        "data_quality_score": score,
        "data_quality_label": label,
        "evidence_completeness": round(completeness, 4),
        "average_classification_confidence": round(avg_conf, 4),
        "source_coverage_ratio": round(source_coverage, 4),
        "contamination_ratio": round(contamination, 4),
        "exact_duplicates": count_flag("exact_duplicate"),
        "near_duplicates": count_flag("near_duplicate_same_author"),
        "story_clusters": len(story_ids),
        "coordination_clusters": len(coord_ids),
        "promotional_records": promo,
        "suspicious_authenticity": suspicious,
        "likely_automated": likely_auto,
        "high_greece_market_relevance": high_market,
        "source_breakdown": source_breakdown,
        "note": "Quality score describes this collected/cleaned dataset and is not a claim of statistical representativeness of the whole market.",
    }


def clean_records(records: list[dict], plan: dict, cancel_check: Callable[[], bool] | None = None) -> dict:
    """Clean normalized records without mutating or deleting the supplied evidence.

    The deterministic v0.8 layer is intentionally conservative. It labels uncertainty
    for human review rather than pretending that public metadata can prove a person is fake.
    """
    cancel_check = cancel_check or (lambda: False)
    original_snapshot = copy.deepcopy(records)
    rows = copy.deepcopy(records)
    views = [_text_view(r.get("text")) for r in rows]

    account_types: list[str] = []
    account_confidences: list[float] = []
    account_reasons: list[list[str]] = []
    content_classes: list[str] = []
    content_confidences: list[float] = []
    content_reasons: list[list[str]] = []
    market_scores: list[float] = []
    market_reasons: list[list[str]] = []
    relevance_scores: list[float] = []
    relevance_reasons: list[list[str]] = []
    relevance_flags: list[list[str]] = []
    spam_scores: list[float] = []
    spam_reasons: list[list[str]] = []
    base_bot_scores: list[float] = []
    bot_reasons: list[list[str]] = []

    for i, (row, view) in enumerate(zip(rows, views)):
        if i % 200 == 0 and cancel_check():
            raise CleaningCancelled("Cleaning cancelled safely before completion.")
        at, ac, ar = _account_type(row, plan, view)
        cc, cconf, cr = _content_class(row, at, view)
        ms, mr = _market_score(row, plan, view)
        rs, rr, rf = _relevance_score(row, plan, view, ms)
        ss, sr = _spam_score(row, view, cc)
        br, brr = _base_bot_risk(row, view, ss)
        account_types.append(at); account_confidences.append(ac); account_reasons.append(ar)
        content_classes.append(cc); content_confidences.append(cconf); content_reasons.append(cr)
        market_scores.append(ms); market_reasons.append(mr)
        relevance_scores.append(rs); relevance_reasons.append(rr); relevance_flags.append(rf)
        spam_scores.append(ss); spam_reasons.append(sr)
        base_bot_scores.append(br); bot_reasons.append(brr)

    # Exact duplicates only when URL is the same or the same author repeats the exact same text.
    duplicate_of: dict[int, str] = {}
    duplicate_seen: dict[str, int] = {}
    for i, (row, view) in enumerate(zip(rows, views)):
        key = _duplicate_key(row, view)
        if not key:
            continue
        if key in duplicate_seen:
            primary = duplicate_seen[key]
            duplicate_of[i] = rows[primary].get("id") or str(primary)
            relevance_flags[i].append("exact_duplicate")
        else:
            duplicate_seen[key] = i

    # Near duplicate only within the same author. Cross-author similarity is handled as story/coordination evidence.
    by_author: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        if _author_key(row):
            by_author[_author_key(row)].append(i)
    for indices in by_author.values():
        for pos, i in enumerate(indices):
            if i in duplicate_of or len(views[i].token_set) < 5:
                continue
            for j in indices[:pos]:
                if j in duplicate_of:
                    continue
                if _jaccard(views[i].token_set, views[j].token_set) >= RULESET_CONFIG["same_author_near_duplicate_similarity"]:
                    duplicate_of[i] = rows[j].get("id") or str(j)
                    relevance_flags[i].append("near_duplicate_same_author")
                    break

    story_ids, coord_ids, coord_add, coord_reasons = _cluster_records(rows, views, account_types)
    author_add, author_reasons = _author_behavior(rows, views)
    story_sizes = Counter(x for x in story_ids if x)
    coord_sizes = Counter(x for x in coord_ids if x)

    cleaned: list[dict] = []
    audit: list[dict] = []
    for i, row in enumerate(rows):
        if i % 200 == 0 and cancel_check():
            raise CleaningCancelled("Cleaning cancelled safely before completion.")
        bot_score = min(1.0, base_bot_scores[i] + coord_add.get(i, 0.0) + author_add.get(i, 0.0))
        bot_rs = [*bot_reasons[i], *coord_reasons.get(i, []), *author_reasons.get(i, [])]
        auth_status = "likely_automated" if bot_score >= RULESET_CONFIG["bot_likely_automated_at"] and len(set(bot_rs)) >= 2 else ("suspicious" if bot_score >= RULESET_CONFIG["bot_suspicious_at"] else "low_risk")
        impact = _metric_impact(row)
        flags = list(dict.fromkeys(relevance_flags[i]))
        if story_ids[i]: flags.append("story_cluster_member")
        if coord_ids[i]: flags.append("coordination_cluster_member")
        if content_classes[i] == "promotional": flags.append("promotional_content")
        if auth_status == "suspicious": flags.append("suspicious_authenticity")
        if auth_status == "likely_automated": flags.append("likely_automated")
        decision, decision_reasons = _decision(
            relevance_scores[i], market_scores[i], spam_scores[i], bot_score, len(set(bot_rs)), flags, content_classes[i], impact
        )
        organic_eligible = bool(
            decision == "trusted"
            and content_classes[i] == "organic"
            and account_types[i] == "person_or_creator"
            and auth_status == "low_risk"
            and not coord_ids[i]
        )
        confidence = min(0.99, max(0.05, (
            0.34 * max(relevance_scores[i], 1 - relevance_scores[i])
            + 0.22 * max(market_scores[i], 1 - market_scores[i])
            + 0.18 * account_confidences[i]
            + 0.14 * content_confidences[i]
            + 0.12 * max(bot_score, 1 - bot_score)
        )))
        reasons = list(dict.fromkeys([
            *relevance_reasons[i], *market_reasons[i], *account_reasons[i], *content_reasons[i], *spam_reasons[i], *bot_rs, *decision_reasons
        ]))
        origin_class = _origin_class(account_types[i], content_classes[i])
        if decision == "excluded":
            independent_weight = 0.0
        elif story_ids[i]:
            independent_weight = 1.0 / max(1, story_sizes[story_ids[i]])
        elif coord_ids[i]:
            independent_weight = 1.0 / max(1, coord_sizes[coord_ids[i]])
        else:
            independent_weight = 1.0
        cleaning = {
            "ruleset_version": CLEANING_RULESET_VERSION,
            "decision": decision,
            "relevance_score": round(relevance_scores[i], 4),
            "market_score": round(market_scores[i], 4),
            "spam_score": round(spam_scores[i], 4),
            "bot_risk_score": round(bot_score, 4),
            "authenticity_score": int(round((1.0 - bot_score) * 100)),
            "authenticity_status": auth_status,
            "confidence": round(confidence, 4),
            "account_type": account_types[i],
            "content_class": content_classes[i],
            "origin_class": origin_class,
            "organic_eligible": organic_eligible,
            "independent_voice_weight": round(independent_weight, 6),
            "duplicate_of": duplicate_of.get(i),
            "story_cluster_id": story_ids[i],
            "coordination_cluster_id": coord_ids[i],
            "flags": list(dict.fromkeys(flags)),
            "reasons": reasons,
            "human_override": None,
            "human_review_history": [],
        }
        enriched = copy.deepcopy(row)
        enriched["cleaning"] = cleaning
        cleaned.append(enriched)
        audit.append({
            "record_id": row.get("id"),
            "ruleset_version": CLEANING_RULESET_VERSION,
            "decision": decision,
            "flags": cleaning["flags"],
            "reasons": reasons,
            "scores": {
                "relevance": cleaning["relevance_score"],
                "market": cleaning["market_score"],
                "spam": cleaning["spam_score"],
                "bot_risk": cleaning["bot_risk_score"],
                "confidence": cleaning["confidence"],
            },
        })

    if records != original_snapshot:
        raise RuntimeError("Cleaning mutated the source evidence, which is forbidden.")

    report = _quality_report(cleaned, plan)
    return {
        "cleaned": cleaned,
        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"]["organic_eligible"]],
        "review_queue": [r for r in cleaned if r["cleaning"]["decision"] == "review"],
        "excluded": [r for r in cleaned if r["cleaning"]["decision"] == "excluded"],
        "audit": audit,
        "report": report,
    }


def persist_cleaning(folder: Path, result: dict) -> dict:
    store = RunStore()
    base = folder / "cleaning"
    store.write(base / "cleaned.json", result["cleaned"])
    store.write(base / "trusted.json", result["trusted"])
    store.write(base / "organic.json", result["organic"])
    store.write(base / "review-queue.json", result["review_queue"])
    store.write(base / "excluded.json", result["excluded"])
    store.write(base / "audit.json", result["audit"])
    store.write(base / "report.json", result["report"])
    store.write(base / "ruleset.json", {"version": CLEANING_RULESET_VERSION, "config": RULESET_CONFIG})
    return result["report"]


def clean_run(folder: Path, plan: dict | None = None, cancel_check: Callable[[], bool] | None = None) -> dict:
    store = RunStore()
    plan = plan or store.read(folder / "plan.json") or {}
    records = store.read(folder / "normalized-all.json", []) or []
    if not isinstance(records, list):
        raise RuntimeError("normalized-all.json is not a valid list")
    result = clean_records(records, plan, cancel_check=cancel_check)
    persist_cleaning(folder, result)
    return result["report"]


def _rebuild_outputs(folder: Path, cleaned: list[dict], plan: dict) -> dict:
    result = {
        "cleaned": cleaned,
        "trusted": [r for r in cleaned if r["cleaning"]["decision"] == "trusted"],
        "organic": [r for r in cleaned if r["cleaning"]["decision"] == "trusted" and r["cleaning"].get("organic_eligible")],
        "review_queue": [r for r in cleaned if r["cleaning"]["decision"] == "review"],
        "excluded": [r for r in cleaned if r["cleaning"]["decision"] == "excluded"],
        "audit": [
            {
                "record_id": r.get("id"),
                "ruleset_version": r["cleaning"].get("ruleset_version", CLEANING_RULESET_VERSION),
                "decision": r["cleaning"]["decision"],
                "flags": r["cleaning"].get("flags", []),
                "reasons": r["cleaning"].get("reasons", []),
                "human_override": r["cleaning"].get("human_override"),
                "human_review_history": r["cleaning"].get("human_review_history", []),
            }
            for r in cleaned
        ],
        "report": _quality_report(cleaned, plan),
    }
    persist_cleaning(folder, result)
    return result["report"]


def apply_review_decision(folder: Path, record_id: str, action: str, note: str = "", account_type: str | None = None, content_class: str | None = None) -> dict:
    store = RunStore()
    cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []
    plan = store.read(folder / "plan.json", {}) or {}
    found = None
    for row in cleaned:
        if str(row.get("id")) != str(record_id):
            continue
        found = row
        cleaning = row.setdefault("cleaning", {})
        prior = cleaning.get("decision")
        if action == "keep":
            cleaning["decision"] = "trusted"
        elif action == "exclude":
            cleaning["decision"] = "excluded"
            cleaning["organic_eligible"] = False
        else:
            raise ValueError("action must be keep or exclude")
        if account_type:
            cleaning["account_type"] = account_type
        if content_class:
            cleaning["content_class"] = content_class
        review_event = {
            "action": action,
            "note": str(note or "")[:1000],
            "previous_decision": prior,
            "reviewed_at": _utcnow(),
        }
        cleaning.setdefault("human_review_history", []).append(review_event)
        cleaning["human_override"] = review_event
        cleaning["origin_class"] = _origin_class(cleaning.get("account_type", "unknown"), cleaning.get("content_class", "unknown"))
        if action == "keep":
            cleaning["organic_eligible"] = bool(
                cleaning.get("content_class") == "organic"
                and cleaning.get("account_type") == "person_or_creator"
                and cleaning.get("authenticity_status") == "low_risk"
                and not cleaning.get("coordination_cluster_id")
            )
        break
    if found is None:
        raise KeyError(record_id)
    report = _rebuild_outputs(folder, cleaned, plan)
    return {"record": found, "report": report}


def load_cleaning_summary(folder: Path) -> dict | None:
    store = RunStore()
    report = store.read(folder / "cleaning" / "report.json")
    return report if isinstance(report, dict) else None


def load_review_queue(folder: Path) -> list[dict]:
    store = RunStore()
    payload = store.read(folder / "cleaning" / "review-queue.json", []) or []
    return payload if isinstance(payload, list) else []
