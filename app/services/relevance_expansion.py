from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

from app.registry import output_mapping_for, load_registry
from app.services.apify_service import ApifyRunner
from app.services.cleaning import clean_run
from app.services.collector import in_range
from app.services.resilience import run_actor_resilient, split_diagnostic_rows
from app.services.normalizer import normalize_dataset
from app.services.smart_collection import (
    canonical_topic,
    detect_dominant_entity_collisions,
    refine_x_input_from_source_plan,
    x_reply_deepening_input,
)
from app.services.storage import RunStore
from app.services.source_capabilities import build_comment_deepening_input, source_capabilities


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
    # small headroom while preserving the user's global hard cap
    return min(remaining, max(0.001, expected * 1.25))


def _append_source_items(
    folder: Path, source: str, items: list[dict], date_from: date, date_to: date, *, mapping: dict | None = None
) -> dict:
    store = RunStore()
    raw_path = folder / f"raw-{source}.json"
    normalized_source_path = folder / f"normalized-{source}.json"
    normalized_all_path = folder / "normalized-all.json"

    raw_existing = store.read(raw_path, []) or []
    raw_all = [*raw_existing, *[x for x in items if isinstance(x, dict)]]
    store.write(raw_path, raw_all)

    data_items, diagnostics = split_diagnostic_rows(items)
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
        "in_range_normalized_added": max(0, len(all_dedup) - len(before_ids)),
        "normalized_total": len(all_dedup),
    }


def _comment_seed_refs(source: str, cleaned: list[dict], max_seeds: int = 40) -> tuple[list[str], list[dict]]:
    rows = [r for r in cleaned if str(r.get("platform") or "") == source]
    rows.sort(key=lambda r: int(r.get("comments", 0) or 0), reverse=True)
    refs, meta, seen = [], [], set()
    for row in rows:
        # Prefer seeds that actually advertise conversation. A zero-comment seed is
        # not useful for deepening and can waste a paid call.
        if int(row.get("comments", 0) or 0) <= 0:
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
        meta.append({"ref": ref, "comments": int(row.get("comments", 0) or 0), "url": row.get("url")})
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


def adaptive_expand_after_cleaning(
    folder: Path,
    plan: dict,
    initial_report: dict,
    cancel_check: Callable[[], bool] | None = None,
    runner=None,
) -> dict:
    """Audited post-cleaning expansion across any selected source combination.

    X gets an additional entity-collision refinement when its verified legacy route
    supports it. All sources may deepen into comments/replies only after that exact
    route has separately passed a clean paid smoke test. Partial failures preserve
    existing evidence and every extra call remains inside the remaining run budget.
    """
    cancel_check = cancel_check or (lambda: False)
    store = RunStore()
    audit = {
        "strategy_version": "smart-collection-v2.1-multisource",
        "status": "not_needed",
        "initial_trusted_shortfall": int(initial_report.get("trusted_sample_shortfall", 0) or 0),
        "steps": [], "warnings": [],
    }
    if plan.get("search_strategy_version") not in {"smart-collection-v2", "master30-search-v1"}:
        audit["reason"] = "strategy_not_supported"
        store.write(folder / "smart-collection-expansion.json", audit)
        return {"report": initial_report, "audit": audit}

    shortfall = int(initial_report.get("trusted_sample_shortfall", 0) or 0)
    if shortfall <= 0:
        audit["reason"] = "trusted_target_met"
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

    def do_call(source: str, actor_id: str, kind: str, actor_input: dict, wanted: int,
                rate: float | None = None, mapping: dict | None = None):
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
        actor_input["maxItems"] = min(int(actor_input.get("maxItems") or allowed), allowed)
        cap = _charge_cap(remaining, rate, actor_input["maxItems"])
        if cap <= 0:
            audit["warnings"].append(f"{source}:{kind}:no_safe_charge_cap")
            return False
        try:
            resilient = run_actor_resilient(
                runner, actor_id, actor_input, max_items=actor_input["maxItems"],
                max_charge_usd=cap, rate_per_1000=rate, max_calls=6,
            )
            charged = resilient.accounted_cost_usd
            _update_budget(folder, charged)
            combined = [*resilient.items, *resilient.diagnostics]
            append = _append_source_items(folder, source, combined, date_from, date_to, mapping=mapping)
            report = clean_run(folder, plan=plan, cancel_check=cancel_check)
            audit["steps"].append({
                "source": source, "kind": kind, "actor_id": actor_id,
                "requested_items": actor_input["maxItems"], "max_charge_usd": round(cap, 6),
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

    if bool(plan.get("comments_requested")) and shortfall > 0:
        forecast_rows = {r.get("source"): r for r in ((plan.get("preflight_forecast") or {}).get("sources") or [])}
        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []
        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]
        for sp in selected:
            if shortfall <= 0 or cancel_check():
                break
            source = str(sp.get("source"))
            if source == "news":
                continue
            comment_info = (forecast_rows.get(source) or {}).get("comments") or {}
            if comment_info.get("status") != "verified_available":
                audit["warnings"].append(f"{source}:comment_deepening_blocked_until_live_route_verification")
                if source == "x":
                    audit["warnings"].append("reply_deepening_blocked_until_live_route_verification")
                continue
            cfg = registry.get(source) or {}
            cap = source_capabilities(source).get("comment_deepening") or {}
            actor_id = str(cfg.get("comment_actor_id") or comment_info.get("candidate_actor_id") or cap.get("candidate_actor_id") or "")
            if not actor_id:
                audit["warnings"].append(f"{source}:comment_deepening_missing_actor")
                continue
            refs, seed_meta = _comment_seed_refs(source, cleaned, max_seeds=40)
            if not refs:
                audit["warnings"].append(f"{source}:no_comment_seed_refs")
                continue
            wanted = max(1, shortfall)
            try:
                inp = build_comment_deepening_input(source, refs, wanted)
            except ValueError as exc:
                audit["warnings"].append(f"{source}:comment_input_unavailable:{exc}")
                continue
            audit.setdefault("comment_seeds", {})[source] = seed_meta
            mapping = cfg.get("comment_output_mapping") or None
            rate = sp.get("price_per_1000_hint") if cap.get("mode") == "same_actor" else None
            do_call(source, actor_id, "comment_deepening", inp, wanted, rate, mapping=mapping)
            shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)

    final_shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)
    audit["final_trusted_shortfall"] = final_shortfall
    audit["status"] = "target_met" if final_shortfall <= 0 else "exhausted_or_shortfall"
    audit["stop_rule"] = "No junk fill: stop at target, budget, source exhaustion, relevance floor, or unverified paid route."
    store.write(folder / "smart-collection-expansion.json", audit)
    return {"report": report, "audit": audit}
