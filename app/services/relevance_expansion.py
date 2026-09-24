from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import copy
import math
from pathlib import Path
from typing import Callable

from app.registry import output_mapping_for, load_registry
from app.services.apify_service import ApifyRunner
from app.services.cleaning import clean_run, clean_records, RULESET_CONFIG
from app.services.collector import in_range
from app.services.comment_deepening import normalize_comment_dataset
from app.services.resilience import run_actor_resilient, split_diagnostic_rows
from app.services.normalizer import normalize_dataset
from app.services.smart_collection import (
    canonical_topic,
    detect_dominant_entity_collisions,
    refine_x_input_from_source_plan,
)
from app.services.storage import RunStore
from app.services.source_capabilities import (
    build_comment_deepening_input,
    comment_parent_batch_limit,
    is_comment_parent_ref,
    build_page_discovery_input,
    comments_forecast,
    normalise_page_ref,
)


def _x_source_plan(plan: dict) -> dict | None:
    for sp in plan.get("sources", []) or []:
        if sp.get("source") == "x":
            return sp
    return None


def _remaining_budget(status: dict, plan: dict) -> float:
    budget = status.get("budget") if isinstance(status.get("budget"), dict) else {}
    if budget.get("remaining_usd") not in (None, ""):
        try:
            return max(0.0, float(budget.get("remaining_usd")))
        except Exception:
            pass
    max_budget = float(plan.get("max_budget_usd", 0) or 0)
    spent = float(budget.get("spent_usd", 0) or 0)
    return max(0.0, max_budget - spent)


def _max_affordable_items(remaining: float, rate_per_1000: float | None, wanted: int) -> int:
    wanted = max(0, int(wanted))
    if wanted <= 0 or remaining <= 0:
        return 0
    if rate_per_1000 in (None, 0):
        return wanted
    try:
        affordable = int((remaining * 1000) // float(rate_per_1000))
    except Exception:
        return wanted
    return max(0, min(wanted, affordable))


def _charge_cap(
    remaining: float,
    rate_per_1000: float | None,
    items: int,
    *,
    minimum_usd: float = 0.0,
) -> float:
    if remaining <= 0 or items <= 0:
        return 0.0
    minimum_usd = max(0.0, float(minimum_usd or 0.0))
    if rate_per_1000 in (None, 0):
        calculated = 0.25
    else:
        expected = float(rate_per_1000) * int(items) / 1000
        calculated = max(0.001, expected * 1.25)
    return min(remaining, max(calculated, minimum_usd))


# epctex/tiktok-comment-scraper uses event pricing in addition to dataset items.
# Public pricing verified 2026-09-23: $0.003/video comment query, $0.003/reply
# query, plus $0.0003 per dataset item beyond the included rows. The generic
# per-1,000 cap only covered dataset items, which produced a $0.015 cap for a
# 40-comment / 12-video call even though the 12 video queries alone cost $0.036.
TIKTOK_COMMENT_QUERY_USD = 0.003
TIKTOK_REPLY_QUERY_USD = 0.003


def _comment_minimum_attempt_charge_usd(
    source: str,
    actor_input: dict,
    wanted: int,
    rate_per_1000: float | None,
) -> float:
    """Minimum safe Apify event cap for one comment Actor attempt.

    Only TikTok currently needs a non-result event floor. The reply allowance is
    deliberately conservative: at most one reply-query event per requested
    output item. This is a CAP, not a charge; Apify still bills only used events.
    """
    if source != "tiktok":
        return 0.0
    refs = actor_input.get("startUrls") if isinstance(actor_input, dict) else None
    parent_count = len(refs) if isinstance(refs, list) else 0
    if parent_count <= 0:
        return 0.0
    wanted = max(1, int(wanted or 0))
    item_cost = 0.0
    if rate_per_1000 not in (None, 0):
        item_cost = float(rate_per_1000) * wanted / 1000.0
    query_cost = TIKTOK_COMMENT_QUERY_USD * parent_count
    reply_cost = TIKTOK_REPLY_QUERY_USD * wanted if actor_input.get("includeReplies") else 0.0
    # Small headroom prevents floating-point/event-rounding edge cases.
    return round((query_cost + reply_cost + item_cost) * 1.10, 6)


def _instagram_shortcode_parent_ref(row: dict) -> str:
    """Recover a concrete Instagram media URL when discovery exposes only a shortcode."""
    if not isinstance(row, dict):
        return ""
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else row
    shortcode = str(raw.get("shortCode") or raw.get("shortcode") or "").strip()
    if not shortcode or not all(ch.isalnum() or ch in "_-" for ch in shortcode):
        return ""
    # /p/<shortcode>/ is a stable concrete media URL and the configured comment
    # Actor accepts post/reel media URLs. This avoids treating a hashtag/profile
    # inputUrl as the parent merely because a direct `url` field was absent.
    return f"https://www.instagram.com/p/{shortcode}/"


def _effective_open_comment_quota(target: int, configured_open: int, collected: int) -> int:
    """Open search absorbs any comment target the owned/page pass did not fill."""
    target = max(0, int(target or 0))
    configured_open = max(0, int(configured_open or 0))
    collected = max(0, int(collected or 0))
    if target <= 0:
        return configured_open
    return max(configured_open, max(0, target - collected))


def _append_source_items(
    folder: Path,
    source: str,
    items: list[dict],
    date_from: date,
    date_to: date,
    *,
    mapping: dict | None = None,
    evidence_layer: str = "primary",
    origin: str | None = None,
    seed_refs: list[str] | None = None,
    seed_context: dict[str, str | dict] | None = None,
) -> dict:
    store = RunStore()
    normalized_source_path = folder / f"normalized-{source}.json"
    normalized_all_path = folder / "normalized-all.json"
    data_items, diagnostics = split_diagnostic_rows(items)

    if evidence_layer == "comment":
        raw_path = folder / f"raw-comments-{source}.json"
        comment_norm_path = folder / f"normalized-comments-{source}.json"
        raw_existing = store.read(raw_path, []) or []
        raw_all = [*raw_existing, *[x for x in items if isinstance(x, dict)]]
        store.write(raw_path, raw_all)
        new_norm = normalize_comment_dataset(source, data_items, seed_refs=seed_refs or [], seed_context=seed_context or {}, mapping=mapping)
        # Comments outside the requested research window are not analysis evidence.
        new_norm = [r for r in new_norm if in_range(r, date_from, date_to)]
        if origin:
            # Where this evidence came from decides what the report may claim:
            # a comment under the brand's own post is not the same public as a
            # comment found by open search, and the split must be stateable.
            for row in new_norm:
                row["evidence_origin"] = origin
        existing_comments = store.read(comment_norm_path, []) or []
        comment_dedup = {str(r.get("id")): r for r in [*existing_comments, *new_norm] if r.get("id")}
        store.write(comment_norm_path, list(comment_dedup.values()))
    else:
        raw_path = folder / f"raw-{source}.json"
        raw_existing = store.read(raw_path, []) or []
        raw_all = [*raw_existing, *[x for x in items if isinstance(x, dict)]]
        store.write(raw_path, raw_all)
        use_mapping = mapping if mapping is not None else output_mapping_for(source)
        new_norm = normalize_dataset(source, data_items, mapping=use_mapping)
        new_norm = [r for r in new_norm if in_range(r, date_from, date_to)]

    existing_source = store.read(normalized_source_path, []) or []
    existing_all = store.read(normalized_all_path, []) or []
    source_dedup = {str(r.get("id")): r for r in [*existing_source, *new_norm] if r.get("id")}
    all_dedup = {str(r.get("id")): r for r in [*existing_all, *new_norm] if r.get("id")}
    store.write(normalized_source_path, list(source_dedup.values()))
    store.write(normalized_all_path, list(all_dedup.values()))
    before_ids = {str(r.get("id")) for r in existing_all if r.get("id")}
    return {
        "raw_added": len(items),
        "diagnostic_added": len(diagnostics),
        "in_range_normalized_added": sum(1 for r in new_norm if str(r.get("id")) not in before_ids),
        "normalized_total": len(all_dedup),
        "evidence_layer": evidence_layer,
    }


#: Parents attempted when every discovery row reports zero comments. Search
#: endpoints routinely return commentsCount: 0 for hits that do have comments,
#: so a small bounded probe is worth far more than skipping the layer entirely.
UNRELIABLE_COUNT_PROBE_PARENTS = 5

#: Which platform an operator-supplied parent URL belongs to.
_SEED_URL_HOSTS = {
    "facebook": ("facebook.com", "fb.com", "fb.watch"),
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "x": ("x.com", "twitter.com"),
    "youtube": ("youtube.com", "youtu.be"),
}


#: Parent posts are searched further back than the comment window, because a
#: post from three weeks ago still collects comments inside it. Filtering the
#: PARENTS by the comment window silently throws those comments away.
PARENT_LOOKBACK_DAYS = 30


#: A source's comments are harvested in three passes. All three have to have
#: run before the source can be called finished.
COMMENT_BUCKETS = ("owned", "open", "backfill")

#: Sources with a comment Actor. Nothing waits on the others.
COMMENT_CAPABLE_SOURCES = {"x", "tiktok", "instagram", "facebook"}

#: States that mean the source is still owed work.
_UNFINISHED_COMMENT_STATES = {"running", "queued", "pending", "deferred", "retrying"}


def comment_source_is_complete(row: dict | None) -> bool:
    """Has this source's comment layer really finished?

    A non-empty ``normalized-comments-{source}.json`` used to answer this, and
    that is the same mistake as reading a cleaning file as a finished cleaning:
    the file appears after the FIRST of three passes. A worker cut off after the
    operator's own pages left a file behind, and the next worker wrote the
    source down as collected and never ran open search or the backfill.

    Completion is therefore recorded per pass, by the worker that finished it.
    """
    if not isinstance(row, dict) or not row:
        return False
    status = str(row.get("status") or "")
    if status == "skipped":
        # No Actor, no seeds, not operational: there is nothing further to do.
        return True
    if status in _UNFINISHED_COMMENT_STATES:
        return False
    return set(COMMENT_BUCKETS) <= {str(b) for b in (row.get("buckets_done") or [])}


def comment_fulfillment_status(
    collected: int,
    target: int,
    every_pass_ran: bool,
    attempt_outcomes: list[dict] | None = None,
) -> tuple[str, str | None, int]:
    """Return a truthful terminal status for one source's comment layer.

    Zero comments is not automatically an operational failure. If at least one
    Actor call completed normally but returned no comments, that is a terminal
    source shortfall. Failed is reserved for runs where no comment attempt
    completed normally and every attempted route was blocked or failed.
    """
    collected = max(0, int(collected or 0))
    target = max(0, int(target or 0))
    shortfall = max(0, target - collected) if target > 0 else 0
    outcomes = [dict(x or {}) for x in (attempt_outcomes or []) if isinstance(x, dict)]

    if not every_pass_ran:
        return "deferred", "worker_deadline_or_unfinished_pass", shortfall

    normal = [x for x in outcomes if str(x.get("status") or "") in {"success", "empty"}]
    failed = [x for x in outcomes if str(x.get("status") or "") in {"actor_failed", "budget_blocked"}]

    if collected <= 0 and failed and not normal:
        if any(str(x.get("status") or "") == "budget_blocked" for x in failed):
            return "failed", "comment_budget_blocked_before_successful_attempt", shortfall
        return "failed", "comment_actor_failed_before_successful_attempt", shortfall

    if target > 0 and collected < target:
        reason = (
            "no_comments_found_after_bounded_parent_discovery"
            if collected <= 0
            else "source_exhausted_before_comment_target"
        )
        return "shortfall", reason, shortfall

    if collected <= 0:
        return "shortfall", "no_comments_found_after_bounded_parent_discovery", shortfall

    return "collected", None, 0

def operator_page_refs(plan: dict, source: str) -> list[str]:
    """Pages/accounts the operator named for this source, ready for its Actor."""
    raw = (plan.get("source_pages") or {}).get(source) or []
    out: list[str] = []
    for value in raw:
        ref = normalise_page_ref(source, value)
        if ref and ref not in out:
            out.append(ref)
    return out


def parent_search_from(date_from: date) -> str:
    """Earliest date a parent post may have, for the comment layer."""
    return (date_from - timedelta(days=PARENT_LOOKBACK_DAYS)).isoformat()


def owned_comment_quota(target: int, share_pct: int) -> int:
    """How many of a source's comments are reserved for the operator's pages.

    The rest belongs to open search. This is a RESERVATION, not a ceiling: if
    open search comes back with less than its half, the shortfall is taken here
    rather than left as an empty bucket.
    """
    target = max(0, int(target or 0))
    share = min(100, max(0, int(share_pct if share_pct is not None else 60)))
    if target <= 0 or share <= 0:
        return 0
    return max(1, round(target * share / 100.0))


def operator_parent_context(plan: dict) -> str:
    """Parent text to attach to comments harvested under an operator-pasted URL.

    A ranked seed carries the post's own text, and that is what lets the cleaner
    keep a reply like "πάλι τίποτα" — the subject is in the parent, so
    `contextual_parent_match` fires. An operator URL has no post text (we never
    scraped that post), so without this every such comment fails the subject gate
    and is thrown away as `subject_not_mentioned` — silently deleting exactly the
    threads the operator hand-picked.

    Pasting the URL IS the assertion that the post is about the subject, so the
    subject is recorded as the parent context. It buys the same treatment a
    ranked seed gets — review, not trusted — never more.
    """
    terms = [str(x).strip() for x in (plan.get("core_terms") or []) if str(x).strip()]
    for key in ("topic", "client"):
        value = str(plan.get(key) or "").strip()
        if value and value not in terms:
            terms.append(value)
    return " · ".join(dict.fromkeys(terms))


def operator_seed_urls(plan: dict, source: str) -> list[str]:
    """Parent posts the operator supplied for this source, in the order given.

    These are deliberate choices by someone who knows the market, so they are
    collected before anything discovery ranked.
    """
    hosts = _SEED_URL_HOSTS.get(source, ())
    if not hosts:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in plan.get("comment_seed_urls") or []:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        folded = url.casefold()
        if any(h in folded for h in hosts) and is_comment_parent_ref(source, url):
            # Someone pasting their page here means "look at my page", which is
            # what the page field is for. Sending it to a comment Actor buys
            # nothing.
            seen.add(url)
            out.append(url)
    return out


def parent_heat_score(row: dict) -> float:
    """How much real conversation a parent post is likely to carry.

    Ranking parents purely by engagement buys the comments of whatever is
    loudest — which, for a brand that sponsors a league, is match coverage.
    This blends three signals the pipeline already computes:

      * conversation volume (comments, then likes as a weaker proxy)
      * how much the post is about the SUBJECT rather than merely naming it
        (cleaning relevance, and a penalty for announcement/promotional posts)
      * whether it is organic audience speech rather than brand or media output
    """
    cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
    comments = float(row.get("comments", 0) or 0)
    likes = float(row.get("likes", 0) or 0)
    # Diminishing returns: 500 comments is not 10x more useful than 50.
    volume = math.log1p(comments) * 1.0 + math.log1p(likes) * 0.25

    relevance = float(cleaning.get("relevance_score", 0) or 0)
    content_class = str(cleaning.get("content_class") or "")
    origin_class = str(cleaning.get("origin_class") or "")
    decision = str(cleaning.get("decision") or "")

    score = volume + relevance * 4.0
    if decision == "trusted":
        score += 1.5
    if cleaning.get("organic_eligible"):
        score += 1.0
    # Score bulletins and promos carry the brand name but no opinion about it.
    if content_class in {"announcement", "promotional", "news", "owned", "repost"}:
        score -= 3.0
    if origin_class in {"earned_media", "owned_media"}:
        score -= 1.0
    return score


def _seed_ref(source: str, row: dict) -> str:
    raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
    if source == "x":
        candidates = (raw.get("id"), raw.get("tweetId"), raw.get("tweet_id"),
                      row.get("url"), raw.get("url"))
    else:
        candidates = (
            raw.get("postUrl"), raw.get("permalink"), raw.get("permalink_url"),
            raw.get("reelUrl"), raw.get("webVideoUrl"), raw.get("link"),
            row.get("url"), raw.get("url"), raw.get("sourceUrl"),
            raw.get("inputUrl"), raw.get("postLink"), raw.get("videoUrl"),
        )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and is_comment_parent_ref(source, value):
            return value
    if source == "instagram":
        return _instagram_shortcode_parent_ref(row)
    return ""


def _comment_parent_candidate_allowed(source: str, row: dict) -> bool:
    """Hard gate for OPEN conversation parents.

    Open-search parents are paid doorways into audience conversation, so they
    must satisfy both a direct subject/alias anchor and a positive target-market
    signal before another paid scrape is allowed.
    """
    if str(row.get("platform") or "") != source:
        return False
    if str(row.get("evidence_layer") or "primary") != "primary":
        return False
    if not _seed_ref(source, row):
        return False

    cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
    flags = {str(x) for x in (cleaning.get("flags") or [])}
    reasons = {str(x) for x in (cleaning.get("reasons") or [])}

    if "no_subject_signal" in flags or "subject_not_mentioned" in reasons:
        return False

    if not any(reason.startswith("core_term:") for reason in reasons):
        return False

    if flags & {
        "exact_duplicate",
        "near_duplicate_same_author",
        "syndicated_duplicate_content",
        "explicit_exclusion_context",
        "likely_automated",
    }:
        return False

    if reasons & {
        "duplicate_not_independent_evidence",
        "explicit_exclusion_context",
        "high_spam_risk",
        "high_automation_or_manipulation_risk",
        "outside_target_market",
    }:
        return False

    try:
        market_score = float(cleaning.get("market_score", 0) or 0)
        if market_score < float(RULESET_CONFIG["market_review_below"]):
            return False
        if float(cleaning.get("spam_score", 0) or 0) >= float(RULESET_CONFIG["spam_exclude_at"]):
            return False
    except Exception:
        return False

    if str(cleaning.get("authenticity_status") or "") == "likely_automated":
        return False

    return True


def _comment_parent_candidate_tier(source: str, row: dict) -> str | None:
    """Return direct or provenance for a safe conversation parent."""
    if _comment_parent_candidate_allowed(source, row):
        return "direct"
    if str(row.get("platform") or "") != source or str(row.get("evidence_layer") or "primary") != "primary":
        return None
    if not _seed_ref(source, row) or not bool(row.get("subject_search_provenance")):
        return None
    cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
    flags = {str(x) for x in (cleaning.get("flags") or [])}
    reasons = {str(x) for x in (cleaning.get("reasons") or [])}
    if flags & {"exact_duplicate","near_duplicate_same_author","syndicated_duplicate_content","explicit_exclusion_context","likely_automated"}:
        return None
    if reasons & {"duplicate_not_independent_evidence","explicit_exclusion_context","high_spam_risk","high_automation_or_manipulation_risk","outside_target_market"}:
        return None
    try:
        if float(cleaning.get("market_score", 0) or 0) < max(0.45, float(RULESET_CONFIG["market_review_below"])):
            return None
        if float(cleaning.get("spam_score", 0) or 0) >= float(RULESET_CONFIG["spam_exclude_at"]):
            return None
    except Exception:
        return None
    if str(cleaning.get("authenticity_status") or "") == "likely_automated":
        return None
    return "provenance"


def _collect_seeds(source: str, rows: list[dict], max_seeds: int, skip_reported_zero: bool) -> tuple[list[str], list[dict]]:
    refs: list[str] = []
    meta: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        comments_n = int(row.get("comments", 0) or 0)
        availability = row.get("metric_availability") if isinstance(row.get("metric_availability"), dict) else {}
        comments_known = bool(availability.get("comments_known"))
        if skip_reported_zero and comments_known and comments_n <= 0:
            continue
        ref = _seed_ref(source, row)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        cleaning = row.get("cleaning") if isinstance(row.get("cleaning"), dict) else {}
        tier = _comment_parent_candidate_tier(source, row) or "direct"
        meta.append({
            "ref": ref, "comments": comments_n, "url": row.get("url"),
            "text": str(row.get("text") or "")[:220],
            "heat": round(parent_heat_score(row), 3),
            "origin": "ranked",
            "market_score": float(cleaning.get("market_score", 0) or 0),
            "subject_qualified": True,
            "qualification_tier": tier,
            "collection_route": row.get("collection_route"),
            "collection_query": row.get("collection_query"),
        })
        if len(refs) >= max_seeds:
            break
    return refs, meta


def _comment_seed_refs(
    source: str,
    cleaned: list[dict],
    max_seeds: int = 40,
    *,
    exclude_refs: set[str] | None = None,
) -> tuple[list[str], list[dict], str]:
    """Pick useful parent posts without trusting engagement counts too much.

    Search Actors often under-report commentsCount. Therefore finding one or two
    parents with a reported non-zero count is NOT enough reason to stop parent
    discovery. We keep those strong parents first, then add a bounded number of
    eligible zero/unknown-count parents. A later retry wave can ask for the next
    batch by passing ``exclude_refs``.
    """
    excluded = {str(x) for x in (exclude_refs or set()) if str(x or "").strip()}

    rows: list[dict] = []
    for row in cleaned:
        tier = _comment_parent_candidate_tier(source, row)
        if not tier:
            continue
        ref = _seed_ref(source, row)
        if not ref or ref in excluded:
            continue
        rows.append(row)

    rows.sort(
        key=lambda row: (
            1 if _comment_parent_candidate_tier(source, row) == "direct" else 0,
            parent_heat_score(row),
        ),
        reverse=True,
    )
    if not rows:
        return [], [], "no_relevant_parent_rows"

    refs, meta = _collect_seeds(source, rows, max_seeds, skip_reported_zero=True)
    if refs:
        probe_cap = min(max_seeds, len(refs) + UNRELIABLE_COUNT_PROBE_PARENTS)
        all_refs, all_meta = _collect_seeds(
            source, rows, probe_cap, skip_reported_zero=False,
        )
        seen = set(refs)
        added = 0
        for ref, item in zip(all_refs, all_meta):
            if ref in seen:
                continue
            refs.append(ref)
            meta.append(item)
            seen.add(ref)
            added += 1
            if len(refs) >= probe_cap:
                break
        return refs, meta, "reported_comments_plus_probe" if added else "reported_comments"

    refs, meta = _collect_seeds(
        source, rows, min(max_seeds, UNRELIABLE_COUNT_PROBE_PARENTS),
        skip_reported_zero=False,
    )
    if refs:
        return refs, meta, "probe_unreliable_counts"
    return [], [], "no_addressable_parent_url"


def _post_ref_from_row(source: str, row: dict) -> tuple[str, str]:
    """(reference, text) for one post returned by a page-discovery Actor.

    Each Actor names these fields differently, and the comment Actor needs the
    reference in the shape ITS schema expects — a tweet id for X, a URL for the
    rest.
    """
    if not isinstance(row, dict):
        return "", ""
    text = str(row.get("text") or row.get("caption") or row.get("message")
               or row.get("title") or row.get("content") or "")[:220]
    if source == "x":
        ref = row.get("id") or row.get("tweetId") or row.get("tweet_id") or ""
        return str(ref or "").strip(), text

    # A page-discovery Actor may put the PAGE in `url` and the post permalink in
    # another field. Taking `url` first is how a page reference reached a comment
    # Actor. Every candidate is tried and the first real post wins.
    candidates = [row.get("postUrl"), row.get("permalink"), row.get("permalink_url"),
                  row.get("webVideoUrl"), row.get("link"), row.get("url"),
                  row.get("postLink"), row.get("videoUrl")]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and is_comment_parent_ref(source, value):
            return value, text
    if source == "instagram":
        derived = _instagram_shortcode_parent_ref(row)
        if derived:
            return derived, text
    return "", text


def _probe_items(folder, plan, source, actor_id, actor_input, wanted, rate,
                 audit, store, runner, cancel_check,
                 deadline_check=None, time_left=None, heartbeat=None,
                 charge_cap_usd=None) -> list[dict]:
    """Run an Actor for references only, without filing its rows as evidence.

    Page discovery exists to find WHERE the conversation is. Its posts are not
    the sample — the comments under them are — so they are never written into
    the normalized set, only mined for parent references.
    """
    if cancel_check():
        return []
    status = store.read(folder / "status.json", {}) or {}
    remaining = _remaining_budget(status, plan)
    allowed = _max_affordable_items(remaining, rate, wanted)
    if allowed <= 0:
        audit["warnings"].append(f"{source}:page_discovery:budget_exhausted")
        return []
    cap = _charge_cap(remaining, rate, allowed)
    if charge_cap_usd not in (None, ""):
        try:
            cap = min(cap, max(0.0, float(charge_cap_usd)))
        except Exception:
            pass
    if cap <= 0:
        audit["warnings"].append(f"{source}:page_discovery:no_safe_charge_cap")
        return []
    try:
        resilient = run_actor_resilient(
            runner, actor_id, actor_input, max_items=allowed,
            max_charge_usd=cap, rate_per_1000=rate, max_calls=2,
            deadline_check=deadline_check, time_left=time_left, heartbeat=heartbeat,
        )
    except Exception as exc:
        audit["warnings"].append(f"{source}:page_discovery:orchestrator_failed:{exc}")
        return []
    _update_budget(folder, resilient.accounted_cost_usd)
    data_items, _ = split_diagnostic_rows(resilient.items)
    deadline_hit = "worker_deadline_reached" in (resilient.failure_kinds or [])
    if deadline_hit:
        audit["deadline_reached"] = True
        audit["warnings"].append(f"{source}:page_discovery_deferred_worker_deadline")

    page_outcome = (
        "deadline_with_rows" if deadline_hit and data_items
        else "deadline" if deadline_hit
        else "complete" if (data_items or resilient.status in {"succeeded", "empty"})
        else "failed"
    )
    audit.setdefault("page_discovery_outcome", {})[source] = page_outcome
    audit["steps"].append({
        "source": source, "kind": "page_discovery", "actor_id": actor_id,
        "requested_items": allowed, "max_charge_usd": round(cap, 6),
        "accounted_cost_usd": round(resilient.accounted_cost_usd, 6),
        "returned_items": len(resilient.items), "resilience_status": resilient.status,
        "page_discovery_outcome": page_outcome,
    })
    return [r for r in data_items if isinstance(r, dict)]


def _owned_parent_refs(source, plan, cfg, max_parents, *, date_from, date_to,
                       run_page_actor, audit, cache_path=None) -> tuple[list[str], list[dict]]:
    """Parent posts the operator vouched for: their pages, plus any direct links.

    The pages are turned into posts here — no comment Actor accepts a page — and
    those posts are searched further back than the comment window, so a post
    from three weeks ago that is still collecting comments is not lost.
    """
    refs: list[str] = []
    meta: list[dict] = []

    for url in operator_seed_urls(plan, source):
        if url not in refs:
            refs.append(url)
            meta.append({"ref": url, "comments": 0, "url": url,
                         "text": operator_parent_context(plan), "origin": "operator_link",
                         "market_score": 0.0, "subject_qualified": True,
                         "qualification_tier": "operator"})

    pages = operator_page_refs(plan, source)
    if pages and cfg.get("page_enabled", True):
        actor_id = str(cfg.get("page_actor_id") or "")
        if not actor_id:
            audit["warnings"].append(f"{source}:page_discovery_actor_missing")
            return refs[:max_parents], meta[:max_parents]
        want_posts = max(1, min(200, int(cfg.get("page_max_posts") or 60)))

        # Turning a page into its posts is a PAID call, and its answer does not
        # change between two invocations of the same run. A worker replaced
        # mid-comment-layer used to buy the same posts all over again.
        cached = RunStore().read(cache_path, None) if cache_path else None
        if (isinstance(cached, dict) and cached.get("pages") == pages
                and cached.get("complete") is True and "parents" in cached):
            for row in cached["parents"]:
                ref = str(row.get("ref") or "")
                if ref and ref not in refs:
                    refs.append(ref)
                    meta.append(row)
            audit.setdefault("page_discovery", {})[source] = {
                "pages": pages, "posts_found": len(cached["parents"]), "reused": True,
            }
            return refs[:max_parents], meta[:max_parents]

        try:
            inp = build_page_discovery_input(
                source, pages, want_posts,
                date_from=parent_search_from(date_from),
                date_to=date_to.isoformat(),
            )
        except ValueError as exc:
            audit["warnings"].append(f"{source}:page_discovery_input_unavailable:{exc}")
            return refs[:max_parents], meta[:max_parents]
        rows = run_page_actor(actor_id, inp, want_posts, cfg.get("page_price_per_1000_hint"))
        page_outcome = str((audit.get("page_discovery_outcome") or {}).get(source) or "failed")
        audit.setdefault("page_discovery", {})[source] = {
            "pages": pages, "posts_found": len(rows), "outcome": page_outcome,
        }
        discovered: list[dict] = []
        for row in rows:
            ref, text = _post_ref_from_row(source, row)
            if not ref or ref in refs:
                continue
            found = {"ref": ref, "comments": int(row.get("commentsCount")
                                                 or row.get("comments") or 0),
                     "url": row.get("url"), "text": text or operator_parent_context(plan),
                     "origin": "owned_page",
                     "market_score": 0.0, "subject_qualified": True,
                     "qualification_tier": "operator"}
            refs.append(ref)
            meta.append(found)
            discovered.append(found)
        # Successful zero-yield is also a result and must not be re-paid on
        # every continuation. If the clock stopped after useful rows arrived,
        # accept that bounded parent set and let the next worker harvest comments.
        cache_complete = page_outcome == "complete" or (
            page_outcome == "deadline_with_rows" and bool(discovered)
        )
        if cache_path and cache_complete:
            RunStore().write(cache_path, {
                "pages": pages,
                "parents": discovered,
                "complete": True,
                "outcome": page_outcome,
            })

    return refs[:max_parents], meta[:max_parents]


def _update_budget(folder: Path, charged: float) -> dict:
    store = RunStore()
    status = store.read(folder / "status.json", {}) or {}
    budget = dict(status.get("budget") or {})
    max_usd = float(budget.get("max_usd", 0) or 0)
    spent = float(budget.get("spent_usd", 0) or 0) + max(0.0, float(charged or 0))
    spent = min(max_usd, spent) if max_usd > 0 else spent
    budget.update({"spent_usd": round(spent, 6), "remaining_usd": round(max(0.0, max_usd - spent), 6)})
    status["budget"] = budget
    store.write_status_folder(folder, status)
    return status


def _comment_status_update(folder: Path, source: str, *, current_message: str | None = None, **fields) -> None:
    """Live, durable visibility for the comment layer.

    The run screen previously showed only the generic adaptive-collection message
    while comment Actors were running, so the operator could not tell whether
    comments were being collected, how many, or why a source was skipped. This
    writes a per-source ``comment_deepening`` block into status.json on every
    transition, and optionally updates the ``current`` step line, so the process
    is observable while it happens — not only in the final audit file.
    """
    try:
        store = RunStore()
        status = store.read(folder / "status.json", {}) or {}
        block = dict(status.get("comment_deepening") or {})
        row = dict(block.get(source) or {})
        row.update({k: v for k, v in fields.items() if v is not None})
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        block[source] = row
        status["comment_deepening"] = block
        if current_message:
            status["current"] = {"source": source, "code": "comment_deepening", "message": current_message}
        store.write_status_folder(folder, status)
    except Exception:
        # Observability must never break collection: a failed status write is
        # dropped, the paid pipeline continues.
        pass


def _limit_adaptive_input(source: str, actor_input: dict, wanted: int) -> dict:
    """Clamp a planned Actor input to the bounded adaptive request."""
    wanted = max(1, int(wanted or 1))
    inp = copy.deepcopy(actor_input or {})
    field = {
        "x": "maxItems",
        "tiktok": "maxItems",
        "youtube": "maxItems",
        "instagram": "resultsLimit",
        "facebook": "resultsCount",
    }.get(source)
    if field and field in inp:
        try:
            inp[field] = min(max(1, int(inp.get(field) or wanted)), wanted)
        except Exception:
            inp[field] = wanted
    if source == "news" and "maxArticles" in inp:
        try:
            inp["maxArticles"] = min(max(1, int(inp.get("maxArticles") or wanted)), wanted)
        except Exception:
            inp["maxArticles"] = wanted
    return inp


def _source_semantic_shortfall(report: dict, source: str, target: int) -> int:
    target = max(0, int(target or 0))
    row = ((report.get("source_breakdown") or {}).get(source) or {})
    analyzable = int(row.get("trusted", 0) or 0) + int(row.get("review", 0) or 0)
    return max(0, target - analyzable)


def _conversation_probe_target(comment_target: int, max_parents: int) -> int:
    """Bounded parent-candidate pool sized for multiple comment harvest waves.

    Discovery itself runs once and is durable. We deliberately collect enough
    qualified parent candidates up front to support more than one harvest wave
    without re-running the same paid search on a continuation worker.
    """
    comment_target = max(0, int(comment_target or 0))
    max_parents = max(1, int(max_parents or 1))
    if comment_target <= 0:
        return 0
    return min(
        60,
        max(
            24,
            max_parents * 3,
            math.ceil(comment_target * 0.9),
        ),
    )

def _conversation_parent_is_strong(source: str, row: dict) -> bool:
    return _comment_parent_candidate_tier(source, row) is not None


def _conversation_probe_input(
    source: str,
    actor_input: dict,
    wanted: int,
    date_from: date,
    date_to: date,
) -> dict:
    inp = _limit_adaptive_input(source, actor_input, wanted)
    parent_from = date_from - timedelta(days=PARENT_LOOKBACK_DAYS)
    until_exclusive = date_to + timedelta(days=1)
    if source == "facebook":
        inp["resultsCount"] = wanted
        inp["startDate"] = parent_from.isoformat()
        inp["endDate"] = date_to.isoformat()
    elif source == "instagram":
        inp["resultsLimit"] = wanted
        inp["onlyPostsNewerThan"] = parent_from.isoformat()
    elif source == "x":
        inp["maxItems"] = wanted
        inp["since"] = f"{parent_from.isoformat()}_00:00:00_UTC"
        inp["until"] = f"{until_exclusive.isoformat()}_00:00:00_UTC"
    elif source == "tiktok":
        inp["maxItems"] = wanted
    return inp


def _discover_conversation_parent_candidates(
    folder: Path,
    plan: dict,
    source_plan: dict,
    source: str,
    *,
    comment_target: int,
    max_parents: int,
    date_from: date,
    date_to: date,
    audit: dict,
    store: RunStore,
    runner,
    cancel_check,
    deadline_check,
    time_left,
    heartbeat,
) -> list[dict]:
    """Discover extra parent posts without adding them to analysis evidence.

    The paid discovery pass is durable and runs once per route. Later comment
    retries consume untried parents from this cached pool instead of paying for
    the same search again after a worker handoff.
    """
    cache_path = folder / f"conversation-parent-candidates-{source}.json"
    cached = store.read(cache_path, []) or []
    cached_by_id = {
        str(row.get("id")): row
        for row in cached
        if isinstance(row, dict) and row.get("id")
    }
    probe_target = _conversation_probe_target(comment_target, max_parents)
    if probe_target <= 0 or len(cached_by_id) >= probe_target:
        return list(cached_by_id.values())

    routes = [
        dict(sr)
        for sr in [
            *(source_plan.get("semantic_topup_subruns") or []),
            *(source_plan.get("topup_subruns") or []),
            *(source_plan.get("subruns") or []),
        ]
        if isinstance(sr, dict)
    ]
    dedup_routes = []
    seen_route_keys = set()
    for sr in routes:
        key = (
            str(sr.get("actor_id") or source_plan.get("actor_id") or ""),
            str(sr.get("purpose") or ""),
            repr(sr.get("input") or {}),
        )
        if key in seen_route_keys:
            continue
        seen_route_keys.add(key)
        dedup_routes.append(sr)
    routes = sorted(
        dedup_routes,
        key=lambda sr: 0 if str(sr.get("purpose") or "") == "semantic_broad_probe" else 1,
    )[:3]
    if not routes:
        return list(cached_by_id.values())

    state_path = folder / "conversation-parent-discovery-state.json"
    state = store.read(state_path, {}) or {}
    done = {str(x) for x in (state.get(source) or [])}
    route_count = len(routes)

    for idx, sr in enumerate(routes):
        if cancel_check() or deadline_check():
            break
        purpose = str(sr.get("purpose") or f"route_{idx+1}")
        route_key = f"{idx}:{purpose}"
        if route_key in done:
            continue
        missing_candidates = max(1, probe_target - len(cached_by_id))
        remaining_routes = max(1, route_count - idx)
        wanted = min(30, max(4, math.ceil(missing_candidates / remaining_routes)))
        inp = _conversation_probe_input(
            source, dict(sr.get("input") or {}), wanted, date_from, date_to
        )
        raw_rows = _probe_items(
            folder,
            plan,
            source,
            str(sr.get("actor_id") or source_plan.get("actor_id") or ""),
            inp,
            wanted,
            source_plan.get("price_per_1000_hint"),
            audit,
            store,
            runner,
            cancel_check,
            deadline_check=deadline_check,
            time_left=time_left,
            heartbeat=lambda _note, _s=source: heartbeat(_s),
            charge_cap_usd=sr.get("max_charge_usd"),
        )
        if not audit.get("deadline_reached"):
            done.add(route_key)
            state[source] = sorted(done)
            store.write(state_path, state)

        if raw_rows:
            normalized = normalize_dataset(
                source, raw_rows, mapping=output_mapping_for(source)
            )
            route_query = None
            for query_field in ("query", "searchTerms", "search", "queries", "keywords", "directUrls", "startUrls"):
                value = inp.get(query_field)
                if value not in (None, "", []):
                    route_query = copy.deepcopy(value)
                    break
            for row in normalized:
                row["subject_search_provenance"] = True
                row["collection_route"] = purpose
                row["collection_query"] = route_query
            parent_from = date_from - timedelta(days=PARENT_LOOKBACK_DAYS)
            normalized = [row for row in normalized if in_range(row, parent_from, date_to)]
            if normalized:
                cleaned_batch = clean_records(normalized, plan)["cleaned"]
                for row in cleaned_batch:
                    if not _conversation_parent_is_strong(source, row):
                        continue
                    rid = str(row.get("id") or "")
                    if rid:
                        cached_by_id[rid] = row
                store.write(cache_path, list(cached_by_id.values()))

        audit.setdefault("conversation_parent_discovery", {}).setdefault(source, []).append({
            "route": purpose,
            "requested": wanted,
            "qualified_candidates_total": len(cached_by_id),
            "target_candidates": probe_target,
        })
        if len(cached_by_id) >= probe_target:
            break

    return list(cached_by_id.values())


def adaptive_expand_after_cleaning(
    folder: Path,
    plan: dict,
    initial_report: dict,
    cancel_check: Callable[[], bool] | None = None,
    runner=None,
    deadline_check: Callable[[], bool] | None = None,
    time_left: Callable[[], float] | None = None,
    heartbeat: Callable[[str], None] | None = None,
) -> dict:
    """Bounded adaptive discovery plus configured comment/reply deepening.

    Parent seeds come from the already-cleaned relevant evidence. Comments are then
    normalized as a separate evidence layer and cleaning is rerun, so a comment is
    not considered relevant merely because it sits under a relevant post.
    """
    # Comment deepening makes extra paid Actor calls and can run for many
    # minutes. Without a deadline it overruns the serverless wall; without a
    # heartbeat the run looks dead and gets resumed in parallel.
    deadline_check = deadline_check or (lambda: False)
    time_left = time_left or (lambda: float("inf"))
    heartbeat = heartbeat or (lambda _msg: None)
    cancel_check = cancel_check or (lambda: False)
    store = RunStore()
    shortfall = int(initial_report.get("trusted_sample_shortfall", 0) or 0)
    comments_requested = bool(plan.get("comments_requested"))
    audit = {
        "strategy_version": "smart-collection-v2.2-comment-layer",
        "status": "not_needed",
        "initial_trusted_shortfall": shortfall,
        "comments_requested": comments_requested,
        "steps": [], "warnings": [],
    }
    if plan.get("search_strategy_version") not in {"smart-collection-v2", "master30-search-v1"}:
        audit["reason"] = "strategy_not_supported"
        store.write(folder / "smart-collection-expansion.json", audit)
        return {"report": initial_report, "audit": audit}
    if shortfall <= 0 and not comments_requested:
        audit["reason"] = "trusted_target_met_and_comments_not_requested"
        store.write(folder / "smart-collection-expansion.json", audit)
        return {"report": initial_report, "audit": audit}

    date_from = date.fromisoformat(str(plan["date_from"]))
    date_to = date.fromisoformat(str(plan["date_to"]))
    if runner is None:
        try:
            runner = ApifyRunner()
        except Exception as exc:
            audit["reason"] = "adaptive_runner_unavailable"
            audit["warnings"].append(str(exc))
            store.write(folder / "smart-collection-expansion.json", audit)
            return {"report": initial_report, "audit": audit}

    report = initial_report
    audit["status"] = "attempted"
    registry = load_registry()

    #: Set when the last Actor acquisition stopped because the worker ran out of
    #: time rather than because it finished. Without this, a call that returned
    #: some rows before stopping looked like an ordinary partial success, and
    #: the caller wrote the source down as collected.
    last_call_hit_deadline = False
    last_call_outcome: dict = {"status": "not_run"}

    def do_call(
        source: str,
        actor_id: str,
        kind: str,
        actor_input: dict,
        wanted: int,
        rate: float | None = None,
        mapping: dict | None = None,
        *,
        evidence_layer: str = "primary",
        origin: str | None = None,
        seed_refs: list[str] | None = None,
        seed_context: dict[str, str] | None = None,
        minimum_attempt_charge_usd: float = 0.0,
        logical_max_charge_usd: float | None = None,
    ):
        nonlocal report, last_call_hit_deadline, last_call_outcome
        last_call_hit_deadline = False
        last_call_outcome = {"status": "not_started", "kind": kind, "source": source}

        if cancel_check():
            audit["warnings"].append(f"{source}:{kind}:cancelled_before_call")
            last_call_outcome = {"status": "cancelled", "kind": kind, "source": source}
            return False

        status = store.read(folder / "status.json", {}) or {}
        remaining = _remaining_budget(status, plan)
        minimum_attempt_charge_usd = max(0.0, float(minimum_attempt_charge_usd or 0.0))
        if minimum_attempt_charge_usd > remaining + 1e-9:
            audit["warnings"].append(
                f"{source}:{kind}:minimum_actor_charge_exceeds_remaining_budget:"
                f"{minimum_attempt_charge_usd:.6f}>{remaining:.6f}"
            )
            last_call_outcome = {
                "status": "budget_blocked", "kind": kind, "source": source,
                "reason": "minimum_actor_charge_exceeds_remaining_budget",
            }
            return False

        allowed = _max_affordable_items(remaining, rate, wanted)
        if allowed <= 0:
            audit["warnings"].append(f"{source}:{kind}:budget_exhausted")
            last_call_outcome = {
                "status": "budget_blocked", "kind": kind, "source": source,
                "reason": "budget_exhausted",
            }
            return False

        actor_input = _limit_adaptive_input(source, dict(actor_input), allowed)
        cap = _charge_cap(
            remaining, rate, allowed, minimum_usd=minimum_attempt_charge_usd,
        )
        if logical_max_charge_usd not in (None, ""):
            try:
                cap = min(cap, max(0.0, float(logical_max_charge_usd)))
            except Exception:
                pass
        if cap <= 0:
            audit["warnings"].append(f"{source}:{kind}:no_safe_charge_cap")
            last_call_outcome = {
                "status": "budget_blocked", "kind": kind, "source": source,
                "reason": "no_safe_charge_cap",
            }
            return False

        try:
            resilient = run_actor_resilient(
                runner, actor_id, actor_input, max_items=allowed,
                max_charge_usd=cap, rate_per_1000=rate, max_calls=4,
                minimum_attempt_charge_usd=minimum_attempt_charge_usd,
                deadline_check=deadline_check, time_left=time_left,
                heartbeat=lambda _note, _s=source: heartbeat(_s),
            )
            charged = resilient.accounted_cost_usd
            _update_budget(folder, charged)
            last_call_hit_deadline = "worker_deadline_reached" in (resilient.failure_kinds or [])
            combined = [*resilient.items, *resilient.diagnostics]
            append = _append_source_items(
                folder, source, combined, date_from, date_to, mapping=mapping,
                evidence_layer=evidence_layer, origin=origin,
                seed_refs=seed_refs, seed_context=seed_context,
            )
            report = clean_run(folder, plan=plan, cancel_check=cancel_check)

            if last_call_hit_deadline:
                outcome_status = "deadline"
            elif resilient.status == "failed":
                outcome_status = "actor_failed"
            elif len(resilient.items) <= 0:
                outcome_status = "empty"
            else:
                outcome_status = "success"

            last_call_outcome = {
                "status": outcome_status,
                "kind": kind,
                "source": source,
                "requested_items": allowed,
                "returned_items": len(resilient.items),
                "normalized_added": int((append or {}).get("in_range_normalized_added", 0) or 0),
                "resilience_status": resilient.status,
                "failure_kinds": list(resilient.failure_kinds or []),
                "accounted_cost_usd": round(charged, 6),
            }

            audit["steps"].append({
                "source": source, "kind": kind, "actor_id": actor_id,
                "requested_items": allowed, "max_charge_usd": round(cap, 6),
                "accounted_cost_usd": round(charged, 6), "returned_items": len(resilient.items),
                "diagnostic_rows": len(resilient.diagnostics), "resilience_status": resilient.status,
                "attempts": resilient.attempts, "adaptive_actions": resilient.adaptive_actions,
                "failure_kinds": resilient.failure_kinds, "append": append,
                "trusted_after": int(report.get("trusted_records", 0) or 0),
                "trusted_shortfall_after": int(report.get("trusted_sample_shortfall", 0) or 0),
                "actor_meta": [dict(m or {}) for m in resilient.metas],
                "call_outcome": outcome_status,
            })

            if resilient.status == "failed":
                audit["warnings"].append(f"{source}:{kind}:resilient_failure")
                return False
            return True

        except Exception as exc:
            audit["warnings"].append(f"{source}:{kind}:orchestrator_failed:{exc}")
            last_call_outcome = {
                "status": "actor_failed", "kind": kind, "source": source,
                "reason": f"orchestrator_failed:{exc}",
            }
            return False

    # Normal adaptive discovery is still driven only by a genuine sample shortfall.
    xplan = _x_source_plan(plan)
    if xplan and shortfall > 0:
        actor_id = str(xplan.get("actor_id") or "")
        if actor_id.casefold() == "xquik/x-tweet-scraper":
            normalized = store.read(folder / "normalized-all.json", []) or []
            core = canonical_topic(str(plan.get("topic") or ""), str(plan.get("market") or ""))
            collisions = detect_dominant_entity_collisions(normalized, core)
            audit["detected_collisions"] = collisions
            if collisions:
                exclusions = [x.get("exclude_term") for x in collisions if x.get("exclude_term")]
                wanted = min(max(shortfall, 1) * 2, int(plan.get("target_total", shortfall) or shortfall))
                inp, buckets = refine_x_input_from_source_plan(xplan, exclusions, wanted)
                audit["refinement_buckets"] = buckets
                if inp:
                    do_call("x", actor_id, "adaptive_refinement", inp, wanted, xplan.get("price_per_1000_hint"))
                    shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)
        else:
            audit["warnings"].append("x:adaptive_refinement_blocked_for_unverified_actor_capability")

    # ---- v30: real post-cleaning semantic refill -------------------------------
    semantic_state_path = folder / "semantic-refill-state.json"
    semantic_state = store.read(semantic_state_path, {}) or {}
    semantic_done = {str(x) for x in (semantic_state.get("done_routes") or [])}

    for semantic_sp in (plan.get("sources") or []):
        source = str(semantic_sp.get("source") or "")
        routes = [dict(sr) for sr in (semantic_sp.get("semantic_topup_subruns") or []) if isinstance(sr, dict)]
        if not source or not routes:
            continue
        source_target = int(semantic_sp.get("target_items", 0) or 0)
        missing = _source_semantic_shortfall(report, source, source_target)
        if missing <= 0:
            continue
        routes = sorted(
            routes,
            key=lambda sr: 0 if str(sr.get("purpose") or "") == "semantic_broad_probe" else 1,
        )
        for idx, sr in enumerate(routes[:3]):
            if missing <= 0 or cancel_check():
                break
            if deadline_check():
                audit["deadline_reached"] = True
                audit["warnings"].append(f"{source}:semantic_refill_deferred_worker_deadline")
                break
            purpose = str(sr.get("purpose") or "semantic_refill")
            route_key = f"{source}:{idx}:{purpose}"
            if route_key in semantic_done:
                continue
            hard_cap = max(1, int(sr.get("target_items", 1) or 1))
            wanted = min(hard_cap, max(1, missing * 2))
            ok = do_call(
                source,
                str(sr.get("actor_id") or semantic_sp.get("actor_id") or ""),
                purpose,
                dict(sr.get("input") or {}),
                wanted,
                semantic_sp.get("price_per_1000_hint"),
                logical_max_charge_usd=sr.get("max_charge_usd"),
            )
            if not last_call_hit_deadline:
                semantic_done.add(route_key)
                semantic_state["done_routes"] = sorted(semantic_done)
                store.write(semantic_state_path, semantic_state)
            missing = _source_semantic_shortfall(report, source, source_target)
            audit.setdefault("semantic_refill", {}).setdefault(source, []).append({
                "route": purpose,
                "requested": wanted,
                "completed": bool(ok),
                "remaining_analyzable_shortfall": missing,
            })
            if last_call_hit_deadline:
                audit["deadline_reached"] = True
                break

    shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)

    # Comment deepening is independent from shortfall: it is requested audience evidence.
    if comments_requested:
        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []
        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]

        def _never_attempted(sp: dict) -> int:
            """Sources nobody has reached yet go first on a continuation.

            The loop stops at the worker's time limit. Keeping the original
            order meant the source that ran out of time was retried first every
            time, and the ones behind it were never reached at all — in a real
            run two of three sources collected nothing for exactly this reason.
            """
            source = str(sp.get("source") or "")
            already = store.read(folder / f"normalized-comments-{source}.json", []) or []
            if already:
                return 2          # finished: cheap to skip, keep last
            attempted = ((store.read(folder / "status.json", {}) or {})
                         .get("comment_deepening") or {}).get(source)
            return 1 if attempted else 0

        selected.sort(key=_never_attempted)
        forecast_rows = {r.get("source"): r for r in ((plan.get("preflight_forecast") or {}).get("sources") or [])}
        for sp in selected:
            if cancel_check():
                break
            source = str(sp.get("source"))
            if source not in {"x", "tiktok", "instagram", "facebook"}:
                continue
            cfg = registry.get(source) or {}
            comment_info = comments_forecast(source, True, cfg)
            plan_comment_info = (forecast_rows.get(source) or {}).get("comments") or {}
            if plan_comment_info.get("status") in {"verified_available", "configured_available"}:
                comment_info = {**comment_info, "status": plan_comment_info.get("status"), "enabled": True}
            if comment_info.get("status") in {"verified_disabled", "configured_disabled"}:
                audit["warnings"].append(f"{source}:comment_actor_disabled_in_settings")
                _comment_status_update(folder, source, status="skipped", reason="comment_actor_disabled_in_settings")
                continue
            if comment_info.get("status") not in {"verified_available", "configured_available"}:
                audit["warnings"].append(f"{source}:comment_deepening_not_operational")
                _comment_status_update(folder, source, status="skipped", reason="comment_deepening_not_operational")
                continue
            # Durable idempotence, per PASS rather than per source. A file on
            # disk only proves the first pass ran; skipping on that alone is how
            # a resumed run silently dropped open search and the backfill.
            prior_row = (((store.read(folder / "status.json", {}) or {})
                          .get("comment_deepening") or {}).get(source) or {})
            done_buckets = {str(b) for b in (prior_row.get("buckets_done") or [])}
            existing_comments = store.read(folder / f"normalized-comments-{source}.json", []) or []
            if comment_source_is_complete(prior_row):
                planned_target = int((plan.get("per_source_comments") or {}).get(source, 0) or 0)
                terminal_status, terminal_reason, terminal_shortfall = comment_fulfillment_status(
                    len(existing_comments), planned_target, True,
                )
                audit["warnings"].append(
                    f"{source}:comment_deepening_already_terminal:{terminal_status}"
                )
                _comment_status_update(
                    folder, source,
                    status=terminal_status,
                    collected=len(existing_comments),
                    target=planned_target,
                    shortfall=terminal_shortfall,
                    reason=terminal_reason or "already_collected_in_previous_invocation",
                )
                continue
            if done_buckets:
                audit["warnings"].append(
                    f"{source}:comment_deepening_resuming_after:{','.join(sorted(done_buckets))}")

            def bucket_finished(bucket: str) -> None:
                """Record a pass as done, durably, the moment it completes."""
                done_buckets.add("backfill" if bucket == "owned_backfill" else bucket)
                _comment_status_update(folder, source, buckets_done=sorted(done_buckets))
            actor_id = str(cfg.get("comment_actor_id") or comment_info.get("candidate_actor_id") or "")
            if not actor_id:
                audit["warnings"].append(f"{source}:comment_deepening_missing_actor")
                _comment_status_update(folder, source, status="skipped", reason="no_comment_actor_configured")
                continue
            # The operator's per-source comment target governs the layer. The
            # registry values are a safety ceiling for a single Actor call, not
            # the size of the evidence: a run asking for 300 comments from a
            # source must be allowed to buy 300, spread over as many parents as
            # that needs.
            source_comment_target = int((plan.get("per_source_comments") or {}).get(source, 0) or 0)
            registry_per_parent = max(1, min(1000, int(cfg.get("comment_max_per_parent") or 40)))
            registry_parents = max(1, min(100, int(cfg.get("comment_max_parents") or 12)))
            if source_comment_target > 0:
                max_per_parent = registry_per_parent
                # Enough parents to actually reach the requested number.
                max_parents = max(
                    registry_parents,
                    min(100, math.ceil(source_comment_target / max(1, max_per_parent))),
                )
            else:
                max_parents = registry_parents
                max_per_parent = registry_per_parent

            mapping = cfg.get("comment_output_mapping") or None
            rate = cfg.get("comment_price_per_1000_hint")
            comment_path = folder / f"normalized-comments-{source}.json"

            def collected_count() -> int:
                return len(store.read(comment_path, []) or [])

            comment_attempt_outcomes: list[dict] = []

            def harvest(refs: list[str], seed_meta: list[dict], wanted: int, bucket: str) -> int:
                """Buy comments under refs in provider-safe batches.

                Facebook's published Actor schema accepts at most five postUrls per
                call. The old path sent every selected parent in one request (13 in
                the failing run), which is outside that contract. We also persist
                completed parent refs after every batch so a worker handoff never
                re-pays the same comment request.
                """
                if not refs or wanted <= 0:
                    return 0

                harvest_state_path = folder / "comment-harvest-state.json"
                harvest_state = store.read(harvest_state_path, {}) or {}
                source_state = dict(harvest_state.get(source) or {})
                attempted_by_bucket = dict(source_state.get("attempted_refs_by_bucket") or {})
                attempted_refs = {str(x) for x in (attempted_by_bucket.get(bucket) or [])}
                pairs = [
                    (r, m) for r, m in zip(refs, seed_meta)
                    if str(r) not in attempted_refs
                ]

                # If all parents for this pass were already completed by an earlier
                # worker, the pass is durably complete and can be closed for free.
                if not pairs:
                    bucket_finished(bucket)
                    return 0

                batch_limit = comment_parent_batch_limit(source)
                before_total = collected_count()
                all_batches_finished = True

                for batch_index in range(0, len(pairs), batch_limit):
                    if deadline_check():
                        all_batches_finished = False
                        audit["deadline_reached"] = True
                        audit["warnings"].append(
                            f"{source}:{bucket}:comment_batch_deferred_worker_deadline"
                        )
                        break

                    batch_pairs = pairs[batch_index:batch_index + batch_limit]
                    batch_refs = [r for r, _ in batch_pairs]
                    batch_meta = [m for _, m in batch_pairs]
                    already_arrived = max(0, collected_count() - before_total)
                    remaining_wanted = max(0, wanted - already_arrived)
                    if remaining_wanted <= 0:
                        break
                    batch_wanted = min(
                        remaining_wanted,
                        max(1, len(batch_refs) * max_per_parent),
                    )

                    try:
                        inp = build_comment_deepening_input(
                            source, batch_refs, batch_wanted,
                            max_per_parent=max_per_parent,
                            include_replies=bool(cfg.get("comment_include_replies", True)),
                        )
                    except ValueError as exc:
                        audit["warnings"].append(
                            f"{source}:{bucket}:comment_input_unavailable:{exc}"
                        )
                        comment_attempt_outcomes.append({
                            "bucket": bucket,
                            "status": "actor_failed",
                            "reason": f"comment_input_unavailable:{exc}",
                            "parents": len(batch_refs),
                            "requested": batch_wanted,
                            "batch_index": batch_index // batch_limit,
                        })
                        # Invalid refs are terminal for this batch; record them so a
                        # continuation does not buy the same impossible request.
                        attempted_refs.update(str(r) for r in batch_refs)
                        continue

                    minimum_attempt_charge_usd = _comment_minimum_attempt_charge_usd(
                        source, inp, batch_wanted, rate,
                    )
                    before_batch = collected_count()
                    heartbeat(source)
                    _comment_status_update(
                        folder, source, status="running", actor_id=actor_id,
                        parents=len(batch_refs), requested=batch_wanted, bucket=bucket,
                        current_message=(
                            f"Collecting comments — {source} ({bucket}): "
                            f"up to {batch_wanted} under {len(batch_refs)} posts"
                        ),
                    )
                    ok = do_call(
                        source, actor_id, f"comment_deepening_{bucket}",
                        inp, batch_wanted, rate,
                        mapping=mapping, evidence_layer="comment", origin=bucket,
                        seed_refs=batch_refs,
                        seed_context={
                            str(m.get("ref")): str(m.get("text") or "")
                            for m in batch_meta if m.get("ref")
                        },
                        minimum_attempt_charge_usd=minimum_attempt_charge_usd,
                    )
                    arrived = max(0, collected_count() - before_batch)
                    outcome = dict(last_call_outcome or {})
                    outcome.update({
                        "bucket": bucket,
                        "parents": len(batch_refs),
                        "requested": batch_wanted,
                        "collected": arrived,
                        "batch_index": batch_index // batch_limit,
                        "parent_refs": list(batch_refs),
                    })
                    if ok and arrived <= 0 and outcome.get("status") == "success":
                        outcome["status"] = "empty"
                    comment_attempt_outcomes.append(outcome)

                    if not ok:
                        audit["warnings"].append(
                            f"{source}:{bucket}:actor_call_failed_or_budget_exhausted:"
                            f"{outcome.get('status') or 'unknown'}"
                        )

                    if last_call_hit_deadline:
                        all_batches_finished = False
                        break

                    # The Actor call ended normally (success, empty or a bounded
                    # failure). Persist these refs before the next paid call.
                    attempted_refs.update(str(r) for r in batch_refs)
                    attempted_by_bucket[bucket] = sorted(attempted_refs)
                    source_state["attempted_refs_by_bucket"] = attempted_by_bucket
                    harvest_state[source] = source_state
                    store.write(harvest_state_path, harvest_state)

                    if source_comment_target > 0 and collected_count() >= source_comment_target:
                        break

                if all_batches_finished and not last_call_hit_deadline:
                    bucket_finished(bucket)
                return max(0, collected_count() - before_total)

            # ---- A. The operator's own pages and links come first ----------------
            owned_refs, owned_meta = _owned_parent_refs(
                source, plan, cfg, max_parents,
                date_from=date_from, date_to=date_to,
                run_page_actor=lambda actor, inp, want, price: _probe_items(
                    folder, plan, source, actor, inp, want, price, audit, store, runner, cancel_check,
                    # Page discovery runs BEFORE the first harvest heartbeat, so
                    # without these it was the longest silent stretch of the run.
                    deadline_check=deadline_check, time_left=time_left,
                    heartbeat=lambda _note, _s=source: heartbeat(_s),
                ),
                audit=audit,
                cache_path=folder / f"page-parents-{source}.json",
            )
            if audit.get("deadline_reached"):
                # `time_left` may refuse page discovery while the coarser boolean
                # deadline is still false. Do not translate that into "no pages".
                _comment_status_update(
                    folder, source, status="deferred",
                    reason="page_discovery_worker_deadline_resumes_automatically",
                )
                break

            owned_quota = owned_comment_quota(source_comment_target, plan.get("owned_share_pct"))
            open_quota = max(0, source_comment_target - owned_quota)

            if deadline_check():
                audit["warnings"].append(f"{source}:comment_deepening_deferred_worker_deadline")
                audit["deadline_reached"] = True
                _comment_status_update(folder, source, status="deferred", reason="worker_deadline_reached_resumes_automatically")
                break

            if not owned_refs:
                # Nothing was ever pasted for this source: both passes that read
                # the operator's pages are done by definition, not owed.
                bucket_finished("owned")
                bucket_finished("backfill")
            owned_wanted = min(owned_quota, len(owned_refs) * max_per_parent)
            if "owned" in done_buckets:
                got_owned = 0
            elif owned_wanted <= 0:
                # A 0% owned share is a completed zero-work pass, not a debt that
                # should requeue forever.
                bucket_finished("owned")
                got_owned = 0
            else:
                got_owned = harvest(owned_refs, owned_meta, owned_wanted, "owned")

            # ---- B. Then open conversation found by search -----------------------
            # Evidence target and conversation-parent capacity are separate.
            comment_cleaned = list(cleaned)
            cached_parent_candidates = (
                store.read(folder / f"conversation-parent-candidates-{source}.json", []) or []
            )
            if cached_parent_candidates:
                merged_rows = {
                    str(row.get("id")): row
                    for row in [*comment_cleaned, *cached_parent_candidates]
                    if isinstance(row, dict) and row.get("id")
                }
                comment_cleaned = list(merged_rows.values())

            capacity_refs, capacity_meta, _capacity_mode = _comment_seed_refs(
                source, comment_cleaned, max_seeds=max_parents
            )
            reported_comment_capacity = sum(
                max(0, int(m.get("comments", 0) or 0)) for m in capacity_meta
            )
            if (
                source_comment_target > 0
                and reported_comment_capacity < source_comment_target
                and not deadline_check()
            ):
                extra_parent_rows = _discover_conversation_parent_candidates(
                    folder,
                    plan,
                    sp,
                    source,
                    comment_target=source_comment_target,
                    max_parents=max_parents,
                    date_from=date_from,
                    date_to=date_to,
                    audit=audit,
                    store=store,
                    runner=runner,
                    cancel_check=cancel_check,
                    deadline_check=deadline_check,
                    time_left=time_left,
                    heartbeat=heartbeat,
                )
                merged_rows = {
                    str(row.get("id")): row
                    for row in [*cleaned, *extra_parent_rows]
                    if isinstance(row, dict) and row.get("id")
                }
                comment_cleaned = list(merged_rows.values())

            ranked_refs, ranked_meta, selection_mode = _comment_seed_refs(
                source, comment_cleaned, max_seeds=max_parents
            )
            audit.setdefault("comment_parent_capacity", {})[source] = {
                "evidence_pool_reported_comments": reported_comment_capacity,
                "extra_parent_candidates": len(
                    store.read(folder / f"conversation-parent-candidates-{source}.json", []) or []
                ),
                "selected_parents": len(ranked_refs),
                "comment_target": source_comment_target,
            }
            owned_set = set(owned_refs)
            pairs = [(r, m) for r, m in zip(ranked_refs, ranked_meta) if r not in owned_set]
            ranked_refs = [r for r, _ in pairs]
            ranked_meta = [m for _, m in pairs]
            if not owned_refs and not ranked_refs:
                audit["warnings"].append(f"{source}:{selection_mode}")
                _comment_status_update(folder, source, status="skipped", reason=selection_mode)
                continue
            audit.setdefault("comment_selection", {})[source] = selection_mode
            audit.setdefault("comment_seeds", {})[source] = [*owned_meta, *ranked_meta]

            if source_comment_target <= 0:
                # Older plans with no explicit number: top up the sample shortfall.
                open_quota = max(20, min(shortfall, len(ranked_refs) * max_per_parent))
            else:
                # The configured owned/open split is a reservation, not a reason
                # to leave the requested layer short. If owned-page collection
                # returned fewer comments (including zero), open search absorbs
                # the remaining target before the owned backfill pass.
                open_quota = _effective_open_comment_quota(
                    source_comment_target, open_quota, collected_count(),
                )
            open_wanted = min(open_quota, len(ranked_refs) * max_per_parent)
            if not ranked_refs or open_wanted <= 0:
                # Includes the valid 100% owned-share configuration.
                bucket_finished("open")
            got_open = 0 if "open" in done_buckets else harvest(
                ranked_refs, ranked_meta, open_wanted, "open")

            # If the explicit target is still short, try ONE more bounded wave
            # using previously untried eligible parents. This is specifically for
            # Actors whose engagement counts under-report which posts have comments.
            got_open_retry = 0
            retry_refs: list[str] = []
            retry_meta: list[dict] = []
            retry_mode = ""
            missing_after_open = max(0, source_comment_target - collected_count())
            if (
                source_comment_target > 0
                and missing_after_open > 0
                and not last_call_hit_deadline
                and not deadline_check()
            ):
                used_refs = set(owned_refs) | set(ranked_refs)
                retry_refs, retry_meta, retry_mode = _comment_seed_refs(
                    source,
                    comment_cleaned,
                    max_seeds=max_parents,
                    exclude_refs=used_refs,
                )
                if retry_refs:
                    retry_wanted = min(
                        missing_after_open,
                        len(retry_refs) * max_per_parent,
                    )
                    got_open_retry = harvest(
                        retry_refs,
                        retry_meta,
                        retry_wanted,
                        "open_retry",
                    )
                    audit.setdefault("comment_retry", {})[source] = {
                        "selection": retry_mode,
                        "parents": len(retry_refs),
                        "requested": retry_wanted,
                        "collected": got_open_retry,
                    }

            # One additional bounded harvest wave from the ALREADY-DISCOVERED
            # parent cache. No new search Actor call is made here; this preserves
            # the worker-handoff invariant that a paid discovery route is never
            # bought twice on a continuation.
            got_open_wave2 = 0
            wave2_refs: list[str] = []
            wave2_meta: list[dict] = []
            missing_after_retry = max(0, source_comment_target - collected_count())
            if (
                source_comment_target > 0
                and missing_after_retry > 0
                and not last_call_hit_deadline
                and not deadline_check()
            ):
                used_refs = set(owned_refs) | set(ranked_refs) | set(retry_refs)
                wave2_refs, wave2_meta, wave2_mode = _comment_seed_refs(
                    source,
                    comment_cleaned,
                    max_seeds=max_parents,
                    exclude_refs=used_refs,
                )
                if wave2_refs:
                    wave2_wanted = min(
                        missing_after_retry,
                        len(wave2_refs) * max_per_parent,
                    )
                    got_open_wave2 = harvest(
                        wave2_refs,
                        wave2_meta,
                        wave2_wanted,
                        "open_wave2",
                    )
                    audit.setdefault("comment_wave2", {})[source] = {
                        "selection": wave2_mode,
                        "parents": len(wave2_refs),
                        "requested": wave2_wanted,
                        "collected": got_open_wave2,
                        "discovery_reused_cached_pool": True,
                    }

            # ---- C. Whatever open search could not deliver comes back here ------
            # An empty bucket helps nobody: if the wider web returned little, the
            # remainder is taken from the pages the operator trusts.
            # Use evidence durable across ALL invocations. Completed buckets
            # intentionally return 0 on resume; local counters would therefore
            # buy backfill for comments a previous worker already collected.
            missing = max(0, source_comment_target - collected_count())
            got_backfill = 0
            if "backfill" in done_buckets:
                pass
            elif missing <= 0:
                # Nothing is missing, so there is nothing to backfill: done.
                bucket_finished("backfill")
            elif missing > 0 and owned_refs and not deadline_check():
                got_backfill = harvest(owned_refs, owned_meta,
                                       min(missing, len(owned_refs) * max_per_parent), "owned_backfill")

            collected_now = collected_count()
            every_pass_ran = (
                set(COMMENT_BUCKETS) <= done_buckets
                and not last_call_hit_deadline
                and not deadline_check()
            )
            terminal_status, terminal_reason, comment_shortfall = comment_fulfillment_status(
                collected_now,
                source_comment_target,
                every_pass_ran,
                attempt_outcomes=comment_attempt_outcomes,
            )
            audit.setdefault("comment_buckets", {})[source] = {
                "target": source_comment_target,
                "shortfall": comment_shortfall,
                "owned_quota": owned_quota,
                "open_quota": open_quota,
                "owned": got_owned,
                "open": got_open,
                "open_retry": got_open_retry,
                "open_wave2": got_open_wave2,
                "backfill": got_backfill,
                "owned_parents": len(owned_refs),
                "open_parents": len(ranked_refs),
                "retry_parents": len(retry_refs),
                "wave2_parents": len(wave2_refs),
                "attempt_outcomes": comment_attempt_outcomes,
            }
            if not every_pass_ran:
                audit["deadline_reached"] = True

            total_parents = len(
                set(owned_refs) | set(ranked_refs) | set(retry_refs) | set(wave2_refs)
            )
            _comment_status_update(
                folder, source,
                status=terminal_status,
                buckets_done=sorted(done_buckets),
                collected=collected_now,
                target=source_comment_target,
                requested=source_comment_target,
                shortfall=comment_shortfall,
                parents=total_parents,
                owned=got_owned,
                open_web=got_open,
                open_retry=got_open_retry,
                open_wave2=got_open_wave2,
                backfill=got_backfill,
                attempt_outcomes=comment_attempt_outcomes,
                selection=selection_mode,
                reason=terminal_reason,
                current_message=(
                    f"Comments — {source}: {collected_now}/{source_comment_target}"
                    if source_comment_target > 0
                    else f"Comments — {source}: {collected_now}"
                ),
            )
            shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)

    final_shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)
    audit["final_trusted_shortfall"] = final_shortfall
    audit["comment_evidence_total"] = sum(
        len(store.read(folder / f"normalized-comments-{s}.json", []) or [])
        for s in ("x", "tiktok", "instagram", "facebook")
    )
    audit["status"] = "target_met" if final_shortfall <= 0 else "exhausted_or_shortfall"
    audit["stop_rule"] = "Stop at target/budget/source exhaustion; configured comment routes must be enabled and comments are re-cleaned for direct or parent-context relevance."
    store.write(folder / "smart-collection-expansion.json", audit)
    return {"report": report, "audit": audit}
