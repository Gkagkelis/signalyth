from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Callable

from app.registry import output_mapping_for, load_registry
from app.services.apify_service import ApifyRunner
from app.services.cleaning import clean_run
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


def _charge_cap(remaining: float, rate_per_1000: float | None, items: int) -> float:
    if remaining <= 0 or items <= 0:
        return 0.0
    if rate_per_1000 in (None, 0):
        return min(remaining, 0.25)
    expected = float(rate_per_1000) * int(items) / 1000
    return min(remaining, max(0.001, expected * 1.25))


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
    seed_context: dict[str, str] | None = None,
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
        if any(h in folded for h in hosts):
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
        ref = raw.get("id") or raw.get("tweetId") or raw.get("tweet_id")
    else:
        ref = row.get("url") or raw.get("url") or raw.get("postUrl") or raw.get("webVideoUrl") or raw.get("link")
    return str(ref or "").strip()


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
        meta.append({
            "ref": ref, "comments": comments_n, "url": row.get("url"),
            "text": str(row.get("text") or "")[:220],
            "heat": round(parent_heat_score(row), 3),
            "origin": "ranked",
        })
        if len(refs) >= max_seeds:
            break
    return refs, meta


def _comment_seed_refs(source: str, cleaned: list[dict], max_seeds: int = 40) -> tuple[list[str], list[dict], str]:
    """Pick the relevant parent posts whose comments are worth buying.

    Returns (refs, meta, selection_mode). Preference order:

    1. Parents the discovery Actor reports as having comments (or whose count
       it does not report at all).
    2. If that yields nothing because every row reports exactly zero — the
       common case for search endpoints, which frequently omit real engagement
       counts — a small bounded probe of the most engaged parents, so the
       comment layer is attempted instead of silently skipped.
    """
    rows = [r for r in cleaned
            if str(r.get("platform") or "") == source
            and str(r.get("evidence_layer") or "primary") == "primary"
            # Location chain: never deepen a parent the cleaning excluded — an
            # outside-market or spam post must not buy its comments either.
            and str(((r.get("cleaning") or {}).get("decision")) or "") != "excluded"]
    rows.sort(key=parent_heat_score, reverse=True)
    if not rows:
        return [], [], "no_relevant_parent_rows"

    refs, meta = _collect_seeds(source, rows, max_seeds, skip_reported_zero=True)
    if refs:
        return refs, meta, "reported_comments"

    refs, meta = _collect_seeds(
        source, rows, min(max_seeds, UNRELIABLE_COUNT_PROBE_PARENTS), skip_reported_zero=False,
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
    else:
        ref = (row.get("url") or row.get("postUrl") or row.get("webVideoUrl")
               or row.get("link") or row.get("permalink") or "")
    return str(ref or "").strip(), text


def _probe_items(folder, plan, source, actor_id, actor_input, wanted, rate,
                 audit, store, runner, cancel_check) -> list[dict]:
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
    if cap <= 0:
        audit["warnings"].append(f"{source}:page_discovery:no_safe_charge_cap")
        return []
    try:
        resilient = run_actor_resilient(
            runner, actor_id, actor_input, max_items=allowed,
            max_charge_usd=cap, rate_per_1000=rate, max_calls=2,
        )
    except Exception as exc:
        audit["warnings"].append(f"{source}:page_discovery:orchestrator_failed:{exc}")
        return []
    _update_budget(folder, resilient.accounted_cost_usd)
    audit["steps"].append({
        "source": source, "kind": "page_discovery", "actor_id": actor_id,
        "requested_items": allowed, "max_charge_usd": round(cap, 6),
        "accounted_cost_usd": round(resilient.accounted_cost_usd, 6),
        "returned_items": len(resilient.items), "resilience_status": resilient.status,
    })
    data_items, _ = split_diagnostic_rows(resilient.items)
    return [r for r in data_items if isinstance(r, dict)]


def _owned_parent_refs(source, plan, cfg, max_parents, *, date_from, date_to,
                       run_page_actor, audit) -> tuple[list[str], list[dict]]:
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
                         "text": operator_parent_context(plan), "origin": "operator_link"})

    pages = operator_page_refs(plan, source)
    if pages and cfg.get("page_enabled", True):
        actor_id = str(cfg.get("page_actor_id") or "")
        if not actor_id:
            audit["warnings"].append(f"{source}:page_discovery_actor_missing")
            return refs[:max_parents], meta[:max_parents]
        want_posts = max(1, min(200, int(cfg.get("page_max_posts") or 60)))
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
        audit.setdefault("page_discovery", {})[source] = {
            "pages": pages, "posts_found": len(rows),
        }
        for row in rows:
            ref, text = _post_ref_from_row(source, row)
            if not ref or ref in refs:
                continue
            refs.append(ref)
            meta.append({"ref": ref, "comments": int(row.get("commentsCount")
                                                     or row.get("comments") or 0),
                         "url": row.get("url"), "text": text or operator_parent_context(plan),
                         "origin": "owned_page"})

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


def adaptive_expand_after_cleaning(
    folder: Path,
    plan: dict,
    initial_report: dict,
    cancel_check: Callable[[], bool] | None = None,
    runner=None,
    deadline_check: Callable[[], bool] | None = None,
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
    ):
        nonlocal report
        if cancel_check():
            audit["warnings"].append(f"{source}:{kind}:cancelled_before_call")
            return False
        status = store.read(folder / "status.json", {}) or {}
        remaining = _remaining_budget(status, plan)
        allowed = _max_affordable_items(remaining, rate, wanted)
        if allowed <= 0:
            audit["warnings"].append(f"{source}:{kind}:budget_exhausted")
            return False
        actor_input = dict(actor_input)
        # Only Actors whose public schema actually contains maxItems receive it.
        if "maxItems" in actor_input:
            actor_input["maxItems"] = min(int(actor_input.get("maxItems") or allowed), allowed)
        cap = _charge_cap(remaining, rate, allowed)
        if cap <= 0:
            audit["warnings"].append(f"{source}:{kind}:no_safe_charge_cap")
            return False
        try:
            resilient = run_actor_resilient(
                runner, actor_id, actor_input, max_items=allowed,
                max_charge_usd=cap, rate_per_1000=rate, max_calls=4,
            )
            charged = resilient.accounted_cost_usd
            _update_budget(folder, charged)
            combined = [*resilient.items, *resilient.diagnostics]
            append = _append_source_items(
                folder, source, combined, date_from, date_to, mapping=mapping,
                evidence_layer=evidence_layer, origin=origin,
                seed_refs=seed_refs, seed_context=seed_context,
            )
            report = clean_run(folder, plan=plan, cancel_check=cancel_check)
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
            })
            if resilient.status == "failed":
                audit["warnings"].append(f"{source}:{kind}:resilient_failure")
                return False
            return True
        except Exception as exc:
            audit["warnings"].append(f"{source}:{kind}:orchestrator_failed:{exc}")
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

    # Comment deepening is independent from shortfall: it is requested audience evidence.
    if comments_requested:
        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []
        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]
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
            # Durable idempotence: a resumed run must not pay for the same layer twice.
            existing_comments = store.read(folder / f"normalized-comments-{source}.json", []) or []
            if existing_comments:
                audit["warnings"].append(f"{source}:comment_deepening_already_collected")
                _comment_status_update(folder, source, status="collected", collected=len(existing_comments),
                                       reason="already_collected_in_previous_invocation")
                continue
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

            def harvest(refs: list[str], seed_meta: list[dict], wanted: int, bucket: str) -> int:
                """Buy `wanted` comments under `refs`. Returns how many arrived."""
                if not refs or wanted <= 0:
                    return 0
                try:
                    inp = build_comment_deepening_input(
                        source, refs, wanted,
                        max_per_parent=max_per_parent,
                        include_replies=bool(cfg.get("comment_include_replies", True)),
                    )
                except ValueError as exc:
                    audit["warnings"].append(f"{source}:{bucket}:comment_input_unavailable:{exc}")
                    return 0
                before = collected_count()
                heartbeat(source)
                _comment_status_update(
                    folder, source, status="running", actor_id=actor_id,
                    parents=len(refs), requested=wanted, bucket=bucket,
                    current_message=(f"Collecting comments — {source} ({bucket}): "
                                     f"up to {wanted} under {len(refs)} posts"),
                )
                ok = do_call(
                    source, actor_id, f"comment_deepening_{bucket}", inp, wanted, rate,
                    mapping=mapping, evidence_layer="comment", origin=bucket,
                    seed_refs=refs,
                    seed_context={str(m.get("ref")): str(m.get("text") or "")
                                  for m in seed_meta if m.get("ref")},
                )
                if not ok:
                    audit["warnings"].append(f"{source}:{bucket}:actor_call_failed_or_budget_exhausted")
                return max(0, collected_count() - before)

            # ---- A. The operator's own pages and links come first ----------------
            owned_refs, owned_meta = _owned_parent_refs(
                source, plan, cfg, max_parents,
                date_from=date_from, date_to=date_to,
                run_page_actor=lambda actor, inp, want, price: _probe_items(
                    folder, plan, source, actor, inp, want, price, audit, store, runner, cancel_check,
                ),
                audit=audit,
            )
            owned_quota = owned_comment_quota(source_comment_target, plan.get("owned_share_pct"))
            open_quota = max(0, source_comment_target - owned_quota)

            if deadline_check():
                audit["warnings"].append(f"{source}:comment_deepening_deferred_worker_deadline")
                audit["deadline_reached"] = True
                _comment_status_update(folder, source, status="deferred", reason="worker_deadline_reached_resumes_automatically")
                break

            got_owned = harvest(owned_refs, owned_meta, min(owned_quota, len(owned_refs) * max_per_parent), "owned")

            # ---- B. Then open conversation found by search -----------------------
            ranked_refs, ranked_meta, selection_mode = _comment_seed_refs(source, cleaned, max_seeds=max_parents)
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
            got_open = harvest(ranked_refs, ranked_meta,
                               min(open_quota, len(ranked_refs) * max_per_parent), "open")

            # ---- C. Whatever open search could not deliver comes back here ------
            # An empty bucket helps nobody: if the wider web returned little, the
            # remainder is taken from the pages the operator trusts.
            missing = max(0, source_comment_target - (got_owned + got_open))
            got_backfill = 0
            if missing > 0 and owned_refs and not deadline_check():
                got_backfill = harvest(owned_refs, owned_meta,
                                       min(missing, len(owned_refs) * max_per_parent), "owned_backfill")

            collected_now = collected_count()
            audit.setdefault("comment_buckets", {})[source] = {
                "target": source_comment_target,
                "owned_quota": owned_quota, "open_quota": open_quota,
                "owned": got_owned, "open": got_open, "backfill": got_backfill,
                "owned_parents": len(owned_refs), "open_parents": len(ranked_refs),
            }
            _comment_status_update(
                folder, source,
                status="collected" if collected_now else "failed",
                collected=collected_now,
                owned=got_owned, open_web=got_open, backfill=got_backfill,
                selection=selection_mode,
                reason=None if collected_now else "no_comments_returned",
                current_message=(f"Comments — {source}: {collected_now} "
                                 f"({got_owned + got_backfill} own pages, {got_open} open search)"),
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
