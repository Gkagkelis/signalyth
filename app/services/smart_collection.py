from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Iterable


# Smart Collection v2 is intentionally deterministic at planning time.  It does
# not ask an LLM to invent search syntax.  AI may later suggest aliases/context,
# but the final Actor plan is built and audited here.

MARKET_ALIASES = {
    "greece": ["Greece", "Ελλάδα", "Ellada"],
}

# Universal discovery vocabularies. These are deliberately entity-agnostic: the
# same planner must work for a company, service, product, person, campaign or topic
# without injecting brand-specific assumptions into the search. Specialised
# context comes from user keywords and later evidence-led adaptive expansion.
REACTION_TERMS = [
    "γνώμη", "κριτική", "αντίδραση", "σχόλιο", "συζήτηση", "θετικό", "αρνητικό",
    "εμπιστοσύνη", "παράπονο", "αμφισβήτηση",
    "opinion", "review", "reaction", "comment", "discussion", "positive", "negative",
    "trust", "complaint", "criticism", "controversy",
]
EXPERIENCE_ISSUE_TERMS = [
    "εμπειρία", "πρόβλημα", "θέμα", "παράπονο", "ανησυχία", "διαφωνία",
    "experience", "problem", "issue", "complaint", "concern", "dispute",
]
PUBLIC_ACTIVITY_TERMS = [
    "ανακοίνωση", "δήλωση", "συνέντευξη", "συνεργασία", "συμφωνία", "χορηγία",
    "καμπάνια", "εκδήλωση", "λανσάρισμα", "αποτελέσματα",
    "announcement", "statement", "interview", "partnership", "deal", "sponsorship",
    "campaign", "event", "launch", "results",
]
MEDIA_CONTEXT_TERMS = [
    "είδηση", "δημοσίευμα", "ρεπορτάζ", "μέσα", "media", "news", "article", "report",
]

STOP_AFTER_CORE = {
    "greece", "ellada", "ελλαδα", "ελλάδα", "greek", "ελληνικη", "ελληνική",
    "και", "the", "of", "for", "by", "στο", "στη", "στην", "με", "x",
}


@dataclass(frozen=True)
class IntentBucket:
    bucket_id: str
    label: str
    query: str
    purpose: str
    weight: float

    def to_dict(self) -> dict:
        return asdict(self)


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _uniq(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        key = _fold(value)
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _safe_phrase(value: str) -> str:
    value = str(value or "").strip().replace('"', " ")
    value = re.sub(r"\s+", " ", value)
    return f'"{value}"' if " " in value else value


def _or_group(values: Iterable[str], max_terms: int = 12) -> str:
    vals = [_safe_phrase(v) for v in _uniq(values)[:max_terms]]
    return "(" + " OR ".join(vals) + ")" if vals else ""


def _negative_terms(values: Iterable[str]) -> str:
    parts = []
    for value in _uniq(values):
        value = value.strip()
        if not value:
            continue
        # X advanced search accepts -term and -"multi word".
        parts.append("-" + _safe_phrase(value))
    return " ".join(parts)


def canonical_topic(topic: str, market: str) -> str:
    """Remove a trailing market alias from a topic such as 'Allwyn Greece'."""
    raw = re.sub(r"\s+", " ", str(topic or "").strip())
    aliases = MARKET_ALIASES.get(_fold(market), [market])
    folded = _fold(raw)
    for alias in sorted(aliases, key=len, reverse=True):
        af = _fold(alias)
        if af and folded.endswith(" " + af):
            cut = raw[: len(raw) - len(alias)].strip(" ,-–—")
            if cut:
                return cut
    return raw


def build_x_intent_buckets(draft, collision_exclusions: Iterable[str] | None = None) -> list[IntentBucket]:
    """Build balanced X search intents for brand/topic research.

    A broad language query is deliberately only one bucket, so it cannot consume
    the whole sample when the brand is also a venue, team property, product name,
    event title, etc.  The Actor's maxItemsPerTarget enforces that balance.
    """
    brand = canonical_topic(draft.topic, draft.market)
    brand_q = _safe_phrase(brand)
    market_terms = MARKET_ALIASES.get(_fold(draft.market), [draft.market])
    context = _uniq([*getattr(draft, "additional_context", [])])
    context = [x for x in context if _fold(x) not in {_fold(brand), _fold(draft.market)}]
    brand_fold = _fold(brand)
    keywords = [
        x for x in getattr(draft, "keywords", [])
        if _fold(x) not in {brand_fold, _fold(draft.topic)} and _fold(x) not in brand_fold
    ]

    user_exclusions = _uniq(getattr(draft, "exclusions", []))
    collision_exclusions = _uniq(collision_exclusions or [])
    exclusions = _uniq([*user_exclusions, *collision_exclusions])
    neg = _negative_terms(exclusions)
    suffix = "-filter:nativeretweets"
    if neg:
        suffix = f"{neg} {suffix}"
    property_suffix = "-filter:nativeretweets"
    user_neg = _negative_terms(user_exclusions)
    if user_neg:
        property_suffix = f"{user_neg} {property_suffix}"

    buckets: list[IntentBucket] = []

    market_group = _or_group(market_terms)
    if market_group:
        buckets.append(IntentBucket(
            "market_context", "Market context",
            f"{brand_q} {market_group} {suffix}",
            "Find market-explicit brand mentions", 1.0,
        ))

    if context:
        buckets.append(IntentBucket(
            "known_context", "Known context",
            f"{brand_q} {_or_group(context)} {suffix}",
            "Find mentions tied to known client/context entities", 1.0,
        ))

    if keywords:
        buckets.append(IntentBucket(
            "user_focus", "User-supplied focus",
            f"{brand_q} {_or_group(keywords)} {suffix}",
            "Find discussion tied to the user-supplied aliases, subtopics or focus terms", 1.0,
        ))

    buckets.append(IntentBucket(
        "public_reaction", "Public reaction",
        f"{brand_q} {_or_group(REACTION_TERMS)} {suffix}",
        "Find evaluative, supportive, critical and trust-related discussion without assuming the subject type", 1.2,
    ))
    buckets.append(IntentBucket(
        "experience_issues", "Experience / issues",
        f"{brand_q} {_or_group(EXPERIENCE_ISSUE_TERMS)} {suffix}",
        "Find experience, problem, quality, service and friction signals where they exist", 1.0,
    ))
    buckets.append(IntentBucket(
        "public_activity", "Public activity / developments",
        f"{brand_q} {_or_group(PUBLIC_ACTIVITY_TERMS)} {suffix}",
        "Find announcements, statements, partnerships, events and results without presuming causality", 0.9,
    ))
    buckets.append(IntentBucket(
        "media_context", "Media / reporting context",
        f"{brand_q} {_or_group(MEDIA_CONTEXT_TERMS)} {suffix}",
        "Find reporting and media-context evidence separately from direct public reaction", 0.7,
    ))

    # Recall probe: useful, but intentionally one bounded target only.
    if _fold(draft.market) == "greece":
        buckets.append(IntentBucket(
            "greek_language_probe", "Greek-language recall probe",
            f"{brand_q} lang:el {suffix}",
            "Catch Greek/Greeklish-adjacent mentions missed by explicit contexts", 0.6,
        ))
    else:
        buckets.append(IntentBucket(
            "broad_probe", "Broad recall probe",
            f"{brand_q} {suffix}",
            "Bounded broad recall; never allowed to dominate the sample", 0.5,
        ))

    # Keep each detected property/context as a separate, bounded bucket.  It is
    # excluded from the other intents so it cannot dominate, but it remains in
    # the evidence universe for exposure/association analysis.
    for idx, term in enumerate(collision_exclusions[:3]):
        buckets.append(IntentBucket(
            f"context_collision_{idx+1}", f"Separated context: {term}",
            f"{brand_q} {_safe_phrase(term)} {property_suffix}",
            "Measure the dominant property/context separately instead of deleting it", 0.35,
        ))

    # Deduplicate exact query strings while preserving intent order.
    out: list[IntentBucket] = []
    seen: set[str] = set()
    for bucket in buckets:
        key = _fold(bucket.query)
        if key not in seen:
            seen.add(key)
            out.append(bucket)
    return out[:9]


def x_search_input(draft, target: int, collision_exclusions: Iterable[str] | None = None) -> tuple[dict, list[dict]]:
    buckets = build_x_intent_buckets(draft, collision_exclusions=collision_exclusions)
    count = max(1, len(buckets))
    # One query cannot occupy more than roughly one bucket's fair share.  A small
    # floor keeps tiny smoke tests usable.
    per_target = max(3, int(math.ceil(max(1, int(target)) / count)))
    # For very small tests, never let per-target caps exceed the total target.
    per_target = min(max(1, int(target)), per_target)
    inp = {
        "mode": "search",
        "searchTerms": [b.query for b in buckets],
        "maxItems": max(1, int(target)),
        "maxItemsPerTarget": per_target,
        "includeSearchTerms": True,
        "queryType": "Latest",
    }
    return inp, [b.to_dict() for b in buckets]


def _tokens(text: object) -> list[str]:
    folded = _fold(text)
    return re.findall(r"[a-z0-9α-ω]+", folded, flags=re.IGNORECASE)


def detect_dominant_entity_collisions(rows: list[dict], core_term: str, min_count: int = 3, min_share: float = 0.28) -> list[dict]:
    """Detect dominant compounds such as 'Allwyn Arena' in discovery results.

    This is a *context collision* signal, not proof of irrelevance.  The planner
    uses it to split/cap the property in subsequent discovery rather than deleting
    it from the evidence base.
    """
    core_tokens = _tokens(core_term)
    if not core_tokens:
        return []
    first = core_tokens[0]
    counts: Counter[str] = Counter()
    authors: dict[str, set[str]] = {}
    matched_rows = 0

    for row in rows:
        toks = _tokens(row.get("text"))
        if first not in toks:
            continue
        matched_rows += 1
        for idx, tok in enumerate(toks):
            if tok != first or idx + 1 >= len(toks):
                continue
            nxt = toks[idx + 1]
            if len(nxt) < 3 or nxt in STOP_AFTER_CORE:
                continue
            compound = f"{core_term.strip()} {nxt}"
            counts[compound] += 1
            authors.setdefault(compound, set()).add(_fold(row.get("author") or row.get("authorUsername")))
            break

    if matched_rows <= 0:
        return []
    out = []
    for compound, count in counts.most_common():
        share = count / matched_rows
        distinct_authors = len({x for x in authors.get(compound, set()) if x})
        if count >= min_count and share >= min_share and distinct_authors >= 2:
            out.append({
                "compound": compound,
                "exclude_term": compound.split(" ", 1)[1],
                "count": count,
                "share_of_core_mentions": round(share, 4),
                "distinct_authors": distinct_authors,
                "classification": "dominant_context_collision",
                "action": "split_and_cap_not_delete",
            })
    return out


def author_concentration(rows: list[dict]) -> dict:
    authors = [str(r.get("author") or r.get("authorUsername") or "").strip() for r in rows]
    authors = [a for a in authors if a]
    if not authors:
        return {"top_author": None, "top_author_count": 0, "top_author_share": 0.0, "distinct_authors": 0}
    counts = Counter(authors)
    top_author, top_n = counts.most_common(1)[0]
    return {
        "top_author": top_author,
        "top_author_count": top_n,
        "top_author_share": round(top_n / len(rows), 4) if rows else 0.0,
        "distinct_authors": len(counts),
    }


def _raw_tweet_id(row: dict) -> str | None:
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else row
    for key in ("id", "tweetId", "tweet_id", "restId"):
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, ""):
            return str(value)
    # CSV simulation rows may already expose id directly.
    value = row.get("id")
    return str(value) if value not in (None, "") else None


def _reply_count(row: dict) -> int:
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else row
    for key in ("replyCount", "reply_count", "comments", "commentCount"):
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, ""):
            try:
                return max(0, int(float(value)))
            except Exception:
                pass
    try:
        return max(0, int(float(row.get("comments") or 0)))
    except Exception:
        return 0


def select_x_reply_seeds(rows: list[dict], max_seeds: int = 40) -> list[dict]:
    """Pick seed tweets for direct-reply deepening.

    Trusted/review evidence is preferred when cleaning labels exist.  Media posts
    remain eligible because their replies can contain consumer reaction, but seed
    ranking is driven primarily by available reply volume and relevance.
    """
    candidates = []
    for row in rows:
        cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
        if cleaning.get("decision") == "excluded":
            continue
        rid = _raw_tweet_id(row)
        replies = _reply_count(row)
        if not rid or replies <= 0:
            continue
        relevance = float(cleaning.get("relevance_score", 0.6) or 0.6)
        market = float(cleaning.get("market_score", 0.5) or 0.5)
        # sqrt prevents one viral post from swallowing all seeds.
        score = (math.sqrt(replies) * 2.0) + relevance + 0.5 * market
        candidates.append((score, replies, rid, row))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out = []
    seen = set()
    for score, replies, rid, row in candidates:
        if rid in seen:
            continue
        seen.add(rid)
        out.append({
            "tweet_id": rid,
            "reply_count": replies,
            "score": round(score, 4),
            "author": row.get("author") or row.get("authorUsername"),
            "url": row.get("url"),
        })
        if len(out) >= max(1, int(max_seeds)):
            break
    return out


def x_reply_deepening_input(rows: list[dict], requested_items: int, max_seeds: int = 40) -> tuple[dict | None, list[dict]]:
    seeds = select_x_reply_seeds(rows, max_seeds=max_seeds)
    if not seeds or requested_items <= 0:
        return None, seeds
    seed_ids = [s["tweet_id"] for s in seeds]
    per_target = max(3, int(math.ceil(max(1, int(requested_items)) / len(seed_ids))))
    inp = {
        "mode": "replies",
        "replyTweetIds": seed_ids,
        "maxItems": max(1, int(requested_items)),
        "maxItemsPerTarget": per_target,
    }
    return inp, seeds


def smart_collection_summary(rows: list[dict], core_term: str) -> dict:
    return {
        "records": len(rows),
        "author_concentration": author_concentration(rows),
        "dominant_entity_collisions": detect_dominant_entity_collisions(rows, core_term),
    }


def refine_x_input_from_source_plan(source_plan: dict, collision_exclusions: Iterable[str], requested_items: int) -> tuple[dict | None, list[dict]]:
    """Create a second X discovery wave from the already-audited first-wave intents.

    This avoids needing the original UI draft after the run has started.  Dominant
    contexts are excluded from the normal intents and reintroduced as separately
    capped buckets.
    """
    buckets = source_plan.get("intent_buckets") or []
    original_queries = [str(b.get("query") or "").strip() for b in buckets if str(b.get("query") or "").strip()]
    collisions = _uniq(collision_exclusions)
    if not original_queries or not collisions or requested_items <= 0:
        return None, []

    neg = _negative_terms(collisions)
    refined: list[dict] = []
    seen: set[str] = set()
    for idx, query in enumerate(original_queries):
        # Avoid stacking duplicate collision filters if a prior adaptive round exists.
        q = query
        for term in collisions:
            pattern = re.compile(rf"(?:^|\s)-(?:\"?){re.escape(term)}(?:\"?)(?=\s|$)", re.IGNORECASE)
            if pattern.search(q):
                continue
        if "-filter:nativeretweets" in q:
            q = q.replace("-filter:nativeretweets", f"{neg} -filter:nativeretweets", 1)
        else:
            q = f"{q} {neg}".strip()
        key = _fold(q)
        if key not in seen:
            seen.add(key)
            refined.append({
                "bucket_id": str((buckets[idx] if idx < len(buckets) else {}).get("bucket_id") or f"refined_{idx+1}"),
                "label": str((buckets[idx] if idx < len(buckets) else {}).get("label") or "Refined intent"),
                "query": q,
                "purpose": "Adaptive discovery with dominant context separated",
            })

    # Preserve each collision as a bounded context bucket instead of deleting it.
    first_query = original_queries[0]
    # First token/phrase before the first space is enough for the query prefix here;
    # quoted multiword brands remain intact because they start with a quote.
    m = re.match(r'^("[^"]+"|\S+)', first_query)
    brand_q = m.group(1) if m else first_query.split()[0]
    for i, term in enumerate(collisions[:3]):
        q = f"{brand_q} {_safe_phrase(term)} -filter:nativeretweets"
        key = _fold(q)
        if key not in seen:
            seen.add(key)
            refined.append({
                "bucket_id": f"context_collision_{i+1}",
                "label": f"Separated context: {term}",
                "query": q,
                "purpose": "Measure dominant property/context separately",
            })

    if not refined:
        return None, []
    per_target = max(3, int(math.ceil(max(1, int(requested_items)) / len(refined))))
    per_target = min(max(1, int(requested_items)), per_target)
    original_input = ((source_plan.get("subruns") or [{}])[0].get("input") or {})
    inp = {
        "mode": "search",
        "searchTerms": [x["query"] for x in refined],
        "maxItems": max(1, int(requested_items)),
        "maxItemsPerTarget": per_target,
        "includeSearchTerms": True,
        "queryType": "Latest",
    }
    for key in ("since", "until"):
        if original_input.get(key):
            inp[key] = original_input[key]
    return inp, refined
