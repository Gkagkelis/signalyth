from __future__ import annotations

from datetime import date, datetime, timezone
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
from app.services.source_capabilities import build_comment_deepening_input, comments_forecast


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


def _comment_seed_refs(source: str, cleaned: list[dict], max_seeds: int = 40) -> tuple[list[str], list[dict]]:
    rows = [r for r in cleaned
            if str(r.get("platform") or "") == source
            and str(r.get("evidence_layer") or "primary") == "primary"
            # Location chain: never deepen a parent the cleaning excluded — an
            # outside-market or spam post must not buy its comments either.
            and str(((r.get("cleaning") or {}).get("decision")) or "") != "excluded"]
    rows.sort(key=lambda r: (int(r.get("comments", 0) or 0), int(r.get("likes", 0) or 0)), reverse=True)
    refs, meta, seen = [], [], set()
    for row in rows:
        comments_n = int(row.get("comments", 0) or 0)
        availability = row.get("metric_availability") if isinstance(row.get("metric_availability"), dict) else {}
        comments_known = bool(availability.get("comments_known"))
        # Do not pay for a parent the discovery Actor explicitly says has zero comments.
        # If the count is unavailable, a bounded attempt is still allowed.
        if comments_known and comments_n <= 0:
            continue
        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
        if source == "x":
            ref = raw.get("id") or raw.get("tweetId") or raw.get("tweet_id")
        else:
            ref = row.get("url") or raw.get("url") or raw.get("postUrl") or raw.get("webVideoUrl") or raw.get("link")
        ref = str(ref or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        meta.append({"ref": ref, "comments": int(row.get("comments", 0) or 0), "url": row.get("url"), "text": str(row.get("text") or "")})
        if len(refs) >= max_seeds:
            break
    return refs, meta


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
                evidence_layer=evidence_layer, seed_refs=seed_refs, seed_context=seed_context,
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
            max_parents = max(1, min(40, int(cfg.get("comment_max_parents") or 12)))
            max_per_parent = max(1, min(1000, int(cfg.get("comment_max_per_parent") or 40)))
            refs, seed_meta = _comment_seed_refs(source, cleaned, max_seeds=max_parents)
            if not refs:
                audit["warnings"].append(f"{source}:no_relevant_parent_with_comments")
                _comment_status_update(folder, source, status="skipped", reason="no_relevant_parent_with_comments")
                continue
            max_possible = len(refs) * max_per_parent
            # Second layer should add depth without allowing one viral thread to dominate.
            baseline = max(20, int(math.ceil(int(sp.get("target_items", 0) or 0) * 0.35)))
            wanted = min(max_possible, max(baseline, min(shortfall, max_possible)))
            try:
                inp = build_comment_deepening_input(
                    source, refs, wanted,
                    max_per_parent=max_per_parent,
                    include_replies=bool(cfg.get("comment_include_replies", True)),
                )
            except ValueError as exc:
                audit["warnings"].append(f"{source}:comment_input_unavailable:{exc}")
                _comment_status_update(folder, source, status="skipped", reason=f"comment_input_unavailable:{exc}")
                continue
            audit.setdefault("comment_seeds", {})[source] = seed_meta
            seed_context = {str(m.get("ref")): str(m.get("text") or "") for m in seed_meta if m.get("ref")}
            mapping = cfg.get("comment_output_mapping") or None
            rate = cfg.get("comment_price_per_1000_hint")
            if deadline_check():
                audit["warnings"].append(f"{source}:comment_deepening_deferred_worker_deadline")
                audit["deadline_reached"] = True
                _comment_status_update(folder, source, status="deferred", reason="worker_deadline_reached_resumes_automatically")
                break
            heartbeat(source)
            _comment_status_update(
                folder, source, status="running", actor_id=actor_id,
                parents=len(refs), requested=wanted, collected=0,
                current_message=f"Collecting comments — {source}: up to {wanted} comments under {len(refs)} relevant posts",
            )
            ok = do_call(
                source, actor_id, "comment_deepening", inp, wanted, rate, mapping=mapping,
                evidence_layer="comment", seed_refs=refs, seed_context=seed_context,
            )
            collected_now = len(store.read(folder / f"normalized-comments-{source}.json", []) or [])
            last_step = next(
                (st for st in reversed(audit.get("steps") or [])
                 if st.get("source") == source and st.get("kind") == "comment_deepening"),
                {},
            )
            _comment_status_update(
                folder, source,
                status="collected" if ok else "failed",
                collected=collected_now,
                returned_items=int(last_step.get("returned_items") or 0) or None,
                cost_usd=float(last_step.get("accounted_cost_usd") or 0) or None,
                reason=None if ok else "actor_call_failed_or_budget_exhausted",
                current_message=f"Comments — {source}: {collected_now} collected",
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
