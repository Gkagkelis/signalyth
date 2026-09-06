from __future__ import annotations

import copy
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from app.services.apify_service import ApifyRunner, CollectionNotConfigured
from app.services.normalizer import normalize_dataset, parse_date
from app.services.storage import RunStore
from app.registry import output_mapping_for, load_registry
from app.config import settings
from app.services.resilience import (
    ActorHealthStore,
    actual_cost_from_meta as _resilience_actual_cost_from_meta,
    run_actor_resilient,
    split_diagnostic_rows,
    target_count,
)

SOURCE_TERMINAL = {"succeeded", "succeeded_empty", "partial", "failed", "cancelled_partial", "skipped_cancelled", "skipped_budget_safety"}




def _assert_live_sources_verified(plan: dict) -> None:
    """Allow shipped default Actors without forcing a separate paid smoke test.

    Built-in locked Actor routes run under the normal budget and resilience guards.
    Replacement/custom Actors still require verification first.
    """
    if settings.signalyth_dry_run:
        return
    registry = load_registry()
    blocked = []
    for sp in plan.get("sources", []) or []:
        source = str(sp.get("source") or "")
        actor_id = str(sp.get("actor_id") or "")
        cfg = registry.get(source) or {}
        configured_actor = str(cfg.get("actor_id") or "")
        default_actor = str(cfg.get("default_actor_id") or "")
        is_shipped_default = bool(
            cfg.get("locked", False)
            and actor_id
            and configured_actor == actor_id
            and default_actor == actor_id
            and cfg.get("adapter_mode", "legacy") == "legacy"
        )
        is_verified = cfg.get("actor_status") == "verified" and configured_actor == actor_id
        if not (is_shipped_default or is_verified):
            blocked.append(f"{source}:{actor_id or 'missing-actor'}")
    if blocked:
        raise CollectionNotConfigured(
            "Live collection blocked: replacement/custom source Actors require verification before paid collection: "
            + ", ".join(blocked)
        )

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class BudgetGuard:
    """Global run budget with conservative reservation and actual-cost settlement."""

    def __init__(self, max_budget: float):
        self.max_budget = float(max_budget)
        self.spent = 0.0
        self.reserved = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_budget - self.spent - self.reserved)

    def reserve(self, amount: float) -> float:
        amount = max(0.0, float(amount))
        if self.spent + self.reserved + amount > self.max_budget + 1e-9:
            raise RuntimeError("Budget guard blocked a sub-run that could exceed the analysis budget.")
        self.reserved += amount
        return amount

    def settle(self, reservation: float, actual_cost: float | None) -> float:
        reservation = max(0.0, float(reservation))
        self.reserved = max(0.0, self.reserved - reservation)
        charged = reservation if actual_cost is None else max(0.0, float(actual_cost))
        # The provider receives the same per-call cap. If reported usage exceeds it,
        # fail closed instead of silently consuming budget reserved for later sources.
        if charged > reservation + 1e-6:
            raise RuntimeError("Actor reported cost above the reserved per-call hard cap.")
        if self.spent + charged > self.max_budget + 1e-6:
            raise RuntimeError("Actual collection cost exceeded the SIGNALYTH analysis budget guard.")
        self.spent += charged
        return charged


def actual_cost_from_meta(meta: dict) -> float | None:
    # Backward-compatible export used by tests and adaptive expansion.
    return _resilience_actual_cost_from_meta(meta)


def in_range(item: dict, date_from: date, date_to: date) -> bool:
    dt = parse_date(item.get("date"))
    if not dt:
        return False
    d = dt.date()
    return date_from <= d <= date_to


def _scaled_targets(original: list[int], desired_total: int) -> list[int]:
    """Scale integer sub-run targets while preserving exact desired total."""
    desired_total = max(0, int(desired_total))
    if not original:
        return []
    base_total = sum(max(0, int(v)) for v in original)
    if base_total <= 0:
        q, r = divmod(desired_total, len(original))
        return [q + (1 if i < r else 0) for i in range(len(original))]
    raw = [desired_total * max(0, int(v)) / base_total for v in original]
    floors = [int(math.floor(v)) for v in raw]
    remainder = desired_total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i]), reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def _resize_input(source: str, original_input: dict, old_target: int, new_target: int) -> dict:
    inp = copy.deepcopy(original_input)
    new_target = max(1, int(new_target))
    old_target = max(1, int(old_target))
    ratio = new_target / old_target

    # Replacement Actors use their verified generic input mapping. Rebalancing
    # must resize the mapped Actor field rather than silently writing legacy keys.
    cfg = (load_registry().get(source) or {})
    if cfg.get("adapter_mode") == "generic":
        field = (cfg.get("input_mapping") or {}).get("max_items")
        if field:
            field_type = ((cfg.get("input_schema_fields") or {}).get(field) or {}).get("type")
            if field_type in {"integer", "number", None, "unknown"}:
                inp[field] = new_target
        return inp

    if source in ("x", "tiktok", "youtube"):
        inp["maxItems"] = new_target
    elif source == "instagram":
        inp["resultsLimit"] = new_target
    elif source == "facebook":
        # Public schema does not impose a 200-item ceiling. Keep the resized
        # request aligned with the logical target; provider-side caps are handled
        # by the Actor and SIGNALYTH still enforces its own max_items/cost envelope.
        inp["resultsCount"] = new_target
    elif source == "news":
        old_per_query = int(inp.get("maxArticles") or 1)
        inp["maxArticles"] = min(500, max(1, int(math.ceil(old_per_query * ratio))))
    return inp


def _elastic_subruns(source_plan: dict, adjusted_target: int, source_cap: float) -> list[dict]:
    originals = source_plan.get("subruns", [])
    if not originals:
        return []
    old_targets = [max(0, int(sr.get("target_items", 0))) for sr in originals]
    new_targets = _scaled_targets(old_targets, adjusted_target)
    total_new = sum(new_targets) or 1
    out = []
    for sr, old_target, new_target in zip(originals, old_targets, new_targets):
        if new_target <= 0:
            continue
        row = copy.deepcopy(sr)
        row["target_items"] = new_target
        row["input"] = _resize_input(source_plan["source"], row.get("input", {}), old_target or 1, new_target)
        row["max_charge_usd"] = max(0.0, source_cap * new_target / total_new)
        row["purpose"] = row.get("purpose") or "discovery"
        out.append(row)
    return out


def _source_base_cap(source_plan: dict) -> float:
    return sum(float(sr.get("max_charge_usd", 0) or 0) for sr in source_plan.get("subruns", []))


def _execution_order(sources: list[dict], automatic: bool) -> list[dict]:
    if not automatic:
        return list(sources)

    def key(sp: dict):
        rate = sp.get("price_per_1000_hint")
        return (0 if rate is None else 1, -(float(rate) if rate is not None else 0.0))

    return sorted(list(sources), key=key)


def _planned_future_floor(execution_sources: list[dict], current_index: int) -> float:
    return sum(_source_base_cap(sp) for sp in execution_sources[current_index + 1:])


def _adjusted_target(base_target: int, carry_shortfall: int, remaining_sources: int) -> int:
    if carry_shortfall <= 0 or remaining_sources <= 0:
        return int(base_target)
    return int(base_target) + int(math.ceil(carry_shortfall / remaining_sources))


def _normalize_partial(source: str, source_raw: list[dict], desired_target: int, date_from: date, date_to: date):
    data_rows, diagnostic_rows = split_diagnostic_rows(source_raw)
    normalized_before = normalize_dataset(source, data_rows, mapping=output_mapping_for(source))
    missing_date_items = sum(1 for r in normalized_before if not parse_date(r.get("date")))
    in_range_rows = [r for r in normalized_before if in_range(r, date_from, date_to)]
    normalized = in_range_rows[:desired_target]
    metrics = {
        "raw_items": len(source_raw),
        "data_items": len(data_rows),
        "diagnostic_items": len(diagnostic_rows),
        "normalized_before_date_filter": len(normalized_before),
        "missing_date_items": missing_date_items,
        "date_filtered_out": len(normalized_before) - len(in_range_rows),
        "capped_out": max(0, len(in_range_rows) - len(normalized)),
        "collected": len(normalized),
    }
    return normalized, metrics


def execute_plan(
    plan: dict,
    run_id: str,
    folder: Path,
    cancel_check: Callable[[], bool] | None = None,
    continue_pipeline: bool = False,
):
    """Execute a collection plan and persist observable lifecycle state.

    Cancellation is cooperative: SIGNALYTH never starts another Actor call after a
    cancellation request. A currently blocking Actor call cannot be killed safely
    by this adapter; when it returns, partial data is saved and the run stops.

    When continue_pipeline=True, successful collection hands the run to Step 3
    without exposing a false terminal state between collection and cleaning.
    """
    store = RunStore()
    guard = BudgetGuard(float(plan["max_budget_usd"]))
    date_from = date.fromisoformat(plan["date_from"])
    date_to = date.fromisoformat(plan["date_to"])
    automatic = bool(plan.get("rebalancing_enabled", plan.get("sample_mode") == "automatic"))
    execution_sources = _execution_order(plan.get("sources", []), automatic)
    cancel_check = cancel_check or (lambda: False)

    try:
        status = store.read(folder / "status.json") or {}
    except Exception:
        status = {}

    status.update({
        "run_id": run_id,
        "status": "running",
        "phase": "collecting",
        "started_at": status.get("started_at") or _utcnow(),
        "completed_at": None,
        "sample_mode": plan.get("sample_mode", "automatic"),
        "sample_target": int(plan.get("target_total", 0) or 0),
        "cancel_requested": False,
        "fatal_error": None,
        "rebalancing": {
            "enabled": automatic,
            "strategy": "elastic_remaining_sources" if automatic else "fixed_per_source",
            "note": "Rebalancing uses collected in-range availability. Relevance-aware expansion is added after Step 3 cleaning.",
        },
        "budget": {"max_usd": guard.max_budget, "spent_usd": 0.0, "remaining_usd": guard.max_budget},
    })
    status.setdefault("sources", {})

    total_sources = len(execution_sources)
    total_subruns = sum(len(sp.get("subruns", []) or []) for sp in execution_sources)
    status["progress"] = {
        "percent": 0,
        "completed_sources": 0,
        "total_sources": total_sources,
        "completed_subruns": 0,
        "total_subruns": total_subruns,
    }
    status["current"] = {"source": None, "code": "starting_collection", "message": "Starting collection"}

    for sp in execution_sources:
        source = sp["source"]
        existing = status["sources"].get(source, {})
        existing.update({
            "status": "pending",
            "base_target": int(sp.get("target_items", 0) or 0),
            "adjusted_target": int(sp.get("target_items", 0) or 0),
            "carry_in": 0,
            "collected": 0,
            "base_shortfall": 0,
            "carry_out": 0,
            "elastic_extra_requested": 0,
            "cost_usd": 0.0,
            "raw_items": 0,
            "data_items": 0,
            "diagnostic_items": 0,
            "normalized_before_date_filter": 0,
            "missing_date_items": 0,
            "date_filtered_out": 0,
            "capped_out": 0,
            "subruns_total": len(sp.get("subruns", []) or []),
            "subruns_completed": 0,
            "subruns_failed": 0,
            "subruns_partial": 0,
            "retry_attempts": 0,
            "subruns": [],
            "started_at": None,
            "completed_at": None,
            "error": None,
        })
        status["sources"][source] = existing

    def sync(message: str, source: str | None = None, phase: str | None = None, terminal: bool = False, code: str | None = None, purpose: str | None = None):
        if phase:
            status["phase"] = phase
        completed_sources = sum(1 for v in status.get("sources", {}).values() if v.get("status") in SOURCE_TERMINAL)
        completed_subruns = sum(int(v.get("subruns_completed", 0) or 0) for v in status.get("sources", {}).values())
        pct = 100 if terminal else (round(100 * completed_sources / total_sources) if total_sources else 100)
        status["progress"] = {
            "percent": pct,
            "completed_sources": completed_sources,
            "total_sources": total_sources,
            "completed_subruns": completed_subruns,
            "total_subruns": total_subruns,
        }
        status["current"] = {"source": source, "code": code, "purpose": purpose, "message": message}
        budget_row = {
            "max_usd": round(guard.max_budget, 6),
            "spent_usd": round(guard.spent, 6),
            "remaining_usd": round(guard.remaining, 6),
        }
        if (status.get("budget") or {}).get("violation_detected"):
            budget_row["violation_detected"] = True
        status["budget"] = budget_row
        store.write_status_folder(folder, status)

    def mark_remaining_cancelled(start_index: int):
        for rest in execution_sources[start_index:]:
            row = status["sources"][rest["source"]]
            if row.get("status") == "pending":
                row.update({
                    "status": "skipped_cancelled",
                    "completed_at": _utcnow(),
                    "error": None,
                })

    def finish_cancelled(message: str):
        target_total = int(plan.get("target_total", 0) or 0)
        normalized_all = list({r["id"]: r for r in all_normalized}.values())
        store.write(folder / "normalized-all.json", normalized_all)
        store.write(folder / "rebalancing.json", rebalance_audit)
        final_total = len(normalized_all)
        status.update({
            "status": "cancelled",
            "phase": "completed",
            "cancel_requested": True,
            "completed_at": _utcnow(),
            "normalized_total": final_total,
            "sample_shortfall": max(0, target_total - final_total),
            "sample_status": "cancelled",
        })
        sync(message, None, "completed", terminal=True, code="cancel_completed")
        return status

    sync("Starting collection", None, "collecting", code="starting_collection")
    _assert_live_sources_verified(plan)
    runner = ApifyRunner()
    health_store = ActorHealthStore()

    all_normalized: list[dict] = []
    processed_base_total = 0
    collected_total_so_far = 0
    rebalance_audit = []
    fatal_budget_violation = False

    for idx, sp in enumerate(execution_sources):
        if cancel_check():
            status["cancel_requested"] = True
            mark_remaining_cancelled(idx)
            return finish_cancelled("Cancelled before the next source started")

        source = sp["source"]
        base_target = int(sp.get("target_items", 0) or 0)
        carry_in = max(0, processed_base_total - collected_total_so_far) if automatic else 0
        remaining_count = len(execution_sources) - idx
        desired_target = _adjusted_target(base_target, carry_in, remaining_count) if automatic else base_target

        base_cap = _source_base_cap(sp)
        future_floor = _planned_future_floor(execution_sources, idx)
        desired_ratio = (desired_target / base_target) if base_target > 0 else 1.0
        proposed_cap = base_cap * max(1.0, desired_ratio)
        extra_headroom = max(0.0, guard.remaining - future_floor - base_cap)
        source_cap = min(proposed_cap, base_cap + extra_headroom)
        source_cap = min(source_cap, max(0.0, guard.remaining - future_floor))

        source_raw: list[dict] = []
        source_meta: list[dict] = []
        source_status = status["sources"][source]
        source_status.update({
            "status": "running",
            "started_at": _utcnow(),
            "base_target": base_target,
            "adjusted_target": desired_target,
            "carry_in": carry_in,
            "elastic_extra_requested": max(0, desired_target - base_target),
            "error": None,
        })

        subruns = _elastic_subruns(sp, desired_target, source_cap)
        source_status["subruns_total"] = len(subruns)
        source_status["subruns_completed"] = 0
        source_status["subruns"] = [
            {
                "index": i + 1,
                "purpose": sr.get("purpose", "discovery"),
                "actor_id": sr.get("actor_id"),
                "target_items": int(sr.get("target_items", 0) or 0),
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "attempts": [],
                "diagnostics": [],
                "adaptive_actions": [],
                "failure_kinds": [],
                "accounted_cost_usd": 0.0,
            }
            for i, sr in enumerate(subruns)
        ]
        sync(f"Collecting {source}", source, code="collecting_source")

        cancelled_mid_source = False
        source_level_error: str | None = None
        consecutive_transient_failures = 0
        circuit_open = False
        if source_cap <= 0 or (base_cap > 0 and source_cap + 1e-9 < min(base_cap, guard.remaining)):
            source_level_error = "Budget guard left no safe acquisition budget for this source."
        else:
            for sr_idx, sr in enumerate(subruns):
                if cancel_check():
                    status["cancel_requested"] = True
                    cancelled_mid_source = True
                    break

                sr_status = source_status["subruns"][sr_idx]
                sr_status.update({"status": "running", "started_at": _utcnow()})
                sync(f"{source}: collecting {sr.get('purpose', 'discovery')}", source, code="collecting_purpose", purpose=sr.get("purpose", "discovery"))

                reservation = guard.reserve(float(sr["max_charge_usd"]))
                try:
                    resilient = run_actor_resilient(
                        runner, sr["actor_id"], sr["input"],
                        max_items=int(sr["target_items"]),
                        max_charge_usd=float(sr["max_charge_usd"]),
                        rate_per_1000=sp.get("price_per_1000_hint"),
                        max_calls=int(sr.get("max_attempt_calls", 6) or 6),
                    )
                    charged = guard.settle(reservation, resilient.accounted_cost_usd)
                except Exception as exc:
                    charged = guard.settle(reservation, None)
                    source_status["cost_usd"] = round(float(source_status["cost_usd"]) + charged, 6)
                    sr_status.update({
                        "status": "failed", "completed_at": _utcnow(), "error": str(exc),
                        "accounted_cost_usd": round(charged, 6),
                        "failure_kinds": ["orchestrator_error"],
                    })
                    source_status["subruns_failed"] = int(source_status.get("subruns_failed", 0) or 0) + 1
                    source_status["subruns_completed"] += 1
                    sync(f"{source}: logical Actor batch failed safely; continuing", source, code="actor_batch_failed")
                    continue

                source_status["cost_usd"] = round(float(source_status["cost_usd"]) + charged, 6)
                source_status["retry_attempts"] = int(source_status.get("retry_attempts", 0) or 0) + max(0, len(resilient.attempts) - 1)
                source_raw.extend(resilient.items)
                source_raw.extend(resilient.diagnostics)

                for meta in resilient.metas:
                    meta_row = dict(meta or {})
                    meta_row["signalyth"] = {
                        "purpose": sr.get("purpose", "discovery"),
                        "target_items": int(sr["target_items"]),
                        "logical_max_charge_usd": float(sr["max_charge_usd"]),
                        "logical_accounted_cost_usd": charged,
                        "resilience_status": resilient.status,
                    }
                    source_meta.append(meta_row)

                mapped_status = {
                    "succeeded": "succeeded",
                    "empty": "succeeded_empty",
                    "partial": "partial",
                    "failed": "failed",
                }.get(resilient.status, "failed")
                if mapped_status == "failed":
                    source_status["subruns_failed"] = int(source_status.get("subruns_failed", 0) or 0) + 1
                elif mapped_status == "partial":
                    source_status["subruns_partial"] = int(source_status.get("subruns_partial", 0) or 0) + 1

                errors = [a.get("error") for a in resilient.attempts if a.get("error")]
                sr_status.update({
                    "status": mapped_status,
                    "completed_at": _utcnow(),
                    "error": errors[-1] if mapped_status == "failed" and errors else None,
                    "attempts": resilient.attempts,
                    "diagnostics": resilient.diagnostics,
                    "adaptive_actions": resilient.adaptive_actions,
                    "failure_kinds": resilient.failure_kinds,
                    "accounted_cost_usd": round(charged, 6),
                    "provider_reported_cost_usd": resilient.provider_reported_cost_usd,
                    "cost_cap_violation": resilient.cost_cap_violation,
                    "returned_items": len(resilient.items),
                })
                if resilient.cost_cap_violation:
                    fatal_budget_violation = True
                    source_status["budget_safety_violation"] = {
                        "detected": True,
                        "provider_reported_cost_usd": resilient.provider_reported_cost_usd,
                        "logical_max_charge_usd": float(sr.get("max_charge_usd", 0) or 0),
                        "message": "Provider reported a charge above the hard per-call cap. Further paid collection was stopped.",
                    }
                    status["fatal_error"] = "Provider-reported run cost exceeded the allocated SIGNALYTH envelope; paid collection stopped safely."
                source_status["subruns_completed"] += 1

                # Source-level circuit breaker: a permanently invalid Actor/credential should not
                # burn every remaining logical batch. Repeated transient infrastructure failures
                # also stop this source after the resilient call has already exhausted its own
                # split/retry policy. Other sources continue normally.
                permanent_failure = mapped_status == "failed" and any(k in {"permanent", "unknown", "cost_cap_violation"} for k in resilient.failure_kinds)
                transient_failure = mapped_status == "failed" and "transient" in resilient.failure_kinds
                consecutive_transient_failures = consecutive_transient_failures + 1 if transient_failure else 0
                if permanent_failure or consecutive_transient_failures >= 2:
                    circuit_open = True
                    reason = "permanent_actor_failure" if permanent_failure else "repeated_transient_actor_failure"
                    source_status["circuit_breaker"] = {
                        "open": True,
                        "reason": reason,
                        "opened_after_subrun": sr_idx + 1,
                    }
                    for later in source_status.get("subruns", [])[sr_idx + 1:]:
                        if later.get("status") == "pending":
                            later["status"] = "skipped_circuit_open"
                            later["completed_at"] = _utcnow()
                            later["error"] = "Skipped to prevent repeated spend against an unhealthy Actor route."
                            source_status["subruns_completed"] += 1

                # Checkpoint after every logical batch. A later source/Actor failure cannot erase
                # already acquired evidence. Diagnostic rows stay in raw/audit but never enter analysis.
                checkpoint_norm, checkpoint_metrics = _normalize_partial(source, source_raw, desired_target, date_from, date_to)
                source_status.update(checkpoint_metrics)
                store.write(folder / f"raw-{source}.json", source_raw)
                store.write(folder / f"normalized-{source}.json", checkpoint_norm)
                store.write(folder / f"apify-{source}.json", source_meta)
                store.write(folder / f"diagnostics-{source}.json", [r for r in source_raw if split_diagnostic_rows([r])[1]])

                try:
                    health_store.record(source, sr["actor_id"], resilient, target_count(sr.get("input") or {}))
                except Exception:
                    # Health learning must never break the evidence run.
                    pass

                sync(
                    f"{source}: batch {mapped_status}", source,
                    code="actor_batch_completed" if mapped_status in {"succeeded", "succeeded_empty"} else "actor_batch_degraded",
                )

                if fatal_budget_violation:
                    sync(f"{source}: provider-reported run cost exceeded the allocated envelope; stopping all paid collection", source, code="budget_safety_stop")
                    break
                if circuit_open:
                    sync(f"{source}: circuit opened; moving to the next source", source, code="actor_circuit_open")
                    break

                if cancel_check():
                    status["cancel_requested"] = True
                    cancelled_mid_source = True
                    break

        normalized, metrics = _normalize_partial(source, source_raw, desired_target, date_from, date_to)
        source_status.update(metrics)
        store.write(folder / f"raw-{source}.json", source_raw)
        store.write(folder / f"normalized-{source}.json", normalized)
        store.write(folder / f"apify-{source}.json", source_meta)
        store.write(folder / f"diagnostics-{source}.json", split_diagnostic_rows(source_raw)[1])
        all_normalized.extend(normalized)

        failed_subruns = int(source_status.get("subruns_failed", 0) or 0)
        partial_subruns = int(source_status.get("subruns_partial", 0) or 0)
        data_contract_failure = (
            int(source_status.get("data_items", 0) or 0) > 0
            and (
                int(source_status.get("normalized_before_date_filter", 0) or 0) == 0
                or (
                    int(source_status.get("normalized_before_date_filter", 0) or 0) > 0
                    and int(source_status.get("missing_date_items", 0) or 0) >= int(source_status.get("normalized_before_date_filter", 0) or 0)
                )
            )
        )
        if cancelled_mid_source:
            for later in source_status.get("subruns", []):
                if later.get("status") == "pending":
                    later["status"] = "skipped_cancelled"
            source_status.update({"status": "cancelled_partial", "completed_at": _utcnow()})
        elif fatal_budget_violation:
            source_status.update({"status": "failed", "error": "Provider-reported run cost exceeded the allocated SIGNALYTH envelope; source stopped safely.", "completed_at": _utcnow()})
        elif source_level_error:
            source_status.update({"status": "failed", "error": source_level_error, "completed_at": _utcnow()})
        elif data_contract_failure:
            source_status["data_contract_failure"] = True
            source_status.update({
                "status": "partial" if int(source_status.get("collected", 0) or 0) > 0 else "failed",
                "error": "Actor returned data, but required normalized/date fields were unusable. Possible schema or mapping drift.",
                "completed_at": _utcnow(),
            })
        elif failed_subruns or partial_subruns:
            final_source_status = "partial" if int(source_status.get("collected", 0) or 0) > 0 else "failed"
            source_status.update({
                "status": final_source_status,
                "error": f"{failed_subruns} failed and {partial_subruns} partial logical batch(es); successful evidence preserved",
                "completed_at": _utcnow(),
            })
        elif int(source_status.get("collected", 0) or 0) == 0:
            source_status.update({"status": "succeeded_empty", "completed_at": _utcnow(), "error": None})
        else:
            source_status.update({"status": "succeeded", "completed_at": _utcnow(), "error": None})

        processed_base_total += base_target
        collected_total_so_far += int(source_status.get("collected", 0) or 0)
        base_shortfall = max(0, base_target - int(source_status.get("collected", 0) or 0))
        carry_out = max(0, processed_base_total - collected_total_so_far) if automatic else base_shortfall
        source_status["base_shortfall"] = base_shortfall
        source_status["carry_out"] = carry_out
        rebalance_audit.append({
            "source": source,
            "base_target": base_target,
            "carry_in": carry_in,
            "adjusted_target": desired_target,
            "collected": int(source_status.get("collected", 0) or 0),
            "carry_out": carry_out,
            "cost_usd": source_status.get("cost_usd", 0.0),
        })
        sync(f"{source} complete" if source_status["status"] in {"succeeded", "succeeded_empty"} else f"{source}: {source_status['status']}", source, code="source_complete" if source_status["status"] in {"succeeded", "succeeded_empty"} else "source_status")

        if fatal_budget_violation:
            for rest in execution_sources[idx + 1:]:
                row = status["sources"][rest["source"]]
                if row.get("status") == "pending":
                    row.update({
                        "status": "skipped_budget_safety",
                        "completed_at": _utcnow(),
                        "error": "Skipped because an upstream provider reported cost above a hard per-call cap.",
                    })
            normalized_all = list({r["id"]: r for r in all_normalized}.values())
            store.write(folder / "normalized-all.json", normalized_all)
            store.write(folder / "rebalancing.json", rebalance_audit)
            status.update({
                "collection_status": "failed",
                "status": "failed",
                "phase": "completed",
                "completed_at": _utcnow(),
                "normalized_total": len(normalized_all),
                "sample_shortfall": max(0, int(plan.get("target_total", 0) or 0) - len(normalized_all)),
                "sample_status": "failed_budget_safety",
            })
            status["budget"] = {
                "max_usd": round(guard.max_budget, 6),
                "spent_usd": round(guard.spent, 6),
                "remaining_usd": round(guard.remaining, 6),
                "violation_detected": True,
            }
            sync("Collection stopped: provider-reported run cost exceeded the allocated SIGNALYTH envelope; preserved evidence is not treated as a completed sample", None, "completed", terminal=True, code="budget_safety_failed")
            return status

        if cancelled_mid_source:
            mark_remaining_cancelled(idx + 1)
            return finish_cancelled("Cancellation completed; partial data was preserved")

    status["phase"] = "finalizing"
    sync("Finalizing the collected sample", None, "finalizing", code="finalizing_collection")

    dedup = {r["id"]: r for r in all_normalized}
    normalized_all = list(dedup.values())
    store.write(folder / "normalized-all.json", normalized_all)
    store.write(folder / "rebalancing.json", rebalance_audit)

    target_total = int(plan.get("target_total", 0) or 0)
    final_total = len(normalized_all)
    shortfall = max(0, target_total - final_total)
    any_failed = any(v.get("status") in {"failed", "partial"} for v in status["sources"].values())
    all_sources_failed = bool(status.get("sources")) and all(v.get("status") == "failed" for v in status["sources"].values())
    if all_sources_failed and final_total == 0:
        terminal_status = "failed"
    else:
        terminal_status = "completed_with_errors" if any_failed else ("completed_shortfall" if shortfall > 0 else "succeeded")
    status.update({
        "collection_status": terminal_status,
        "normalized_total": final_total,
        "sample_shortfall": shortfall,
        "sample_status": "target_met" if shortfall == 0 else "shortfall",
    })
    if continue_pipeline and terminal_status != "failed":
        status.update({
            "status": "running",
            "phase": "cleaning",
            "completed_at": None,
        })
        sync("Collection complete; cleaning and relevance checks are starting", None, "cleaning", terminal=False, code="starting_cleaning")
        status.setdefault("progress", {})["percent"] = 85
        store.write_status_folder(folder, status)
        return status

    status.update({
        "status": terminal_status,
        "phase": "completed",
        "completed_at": _utcnow(),
    })
    message = {
        "succeeded": "Collection completed",
        "completed_shortfall": "Collection completed with a sample shortfall",
        "completed_with_errors": "Collection completed; one or more sources failed",
        "failed": "Collection failed safely; all selected source routes failed",
    }[terminal_status]
    sync(message, None, "completed", terminal=True, code={"succeeded":"collection_completed","completed_shortfall":"collection_shortfall","completed_with_errors":"collection_errors","failed":"collection_failed"}[terminal_status])
    return status
