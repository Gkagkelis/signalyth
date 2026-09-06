from __future__ import annotations

import json
import math
import re
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.config import settings


TRANSIENT_MARKERS = (
    "504", "503", "502", "429", "timeout", "timed out", "rate limit", "rate-limit",
    "temporarily unavailable", "temporary failure", "upstream", "retryable", "retry-exhausted",
    "fetch failed", "partial_failure", "partial failure", "connection reset", "connection aborted",
    "service unavailable", "gateway timeout", "network error",
)
PERMANENT_MARKERS = (
    "401", "403", "unauthorized", "forbidden", "invalid input", "invalid-input", "validation",
    "not found", "private actor", "permission", "authentication", "bad request", "unsupported",
)
DIAGNOSTIC_STATUSES = {
    "no-input", "invalid-input", "replies-incomplete", "zero-output", "aborted", "unexpected-error",
}
MULTI_TARGET_FIELDS = (
    "searchTerms", "search", "queries", "keywords", "directUrls", "startUrls", "replyTweetIds",
    "conversationIds", "tweetIds", "urls",
)
DEFAULT_SAFE_BATCH_SIZE = {
    "x": 2,
    "tiktok": 2,
    "instagram": 1,
    "facebook": 1,
    "youtube": 2,
    "news": 2,
}


def _fold(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def actual_cost_from_meta(meta: dict | None) -> float | None:
    meta = meta or {}
    for key in ("usageTotalUsd", "usage_total_usd", "usageUsd", "chargedUsd", "costUsd"):
        value = meta.get(key)
        if value not in (None, ""):
            try:
                return max(0.0, float(value))
            except Exception:
                pass
    usage = meta.get("usage")
    if isinstance(usage, dict):
        for key in ("totalUsd", "total_usd", "USD"):
            value = usage.get(key)
            if value not in (None, ""):
                try:
                    return max(0.0, float(value))
                except Exception:
                    pass
    return None


def classify_exception(exc: Exception) -> str:
    text = _fold(f"{type(exc).__name__}: {exc}")
    if any(marker in text for marker in PERMANENT_MARKERS):
        return "permanent"
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def is_diagnostic_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    rid = str(row.get("id") or "")
    result_type = _fold(row.get("resultType") or row.get("result_type") or row.get("type"))
    status = _fold(row.get("status"))
    return rid.startswith("diag:") or result_type == "diagnostic" or status in DIAGNOSTIC_STATUSES


def split_diagnostic_rows(items: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    data, diagnostics = [], []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        (diagnostics if is_diagnostic_row(row) else data).append(row)
    return data, diagnostics


def _diagnostic_text(meta: dict | None, diagnostics: list[dict]) -> str:
    parts: list[str] = []
    meta = meta or {}
    for key in ("statusMessage", "status_message", "status", "errorMessage", "message"):
        value = meta.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    for row in diagnostics:
        for key in ("status", "message", "error", "reason"):
            value = row.get(key)
            if value not in (None, ""):
                parts.append(str(value))
    return _fold(" | ".join(parts))


def classify_actor_outcome(meta: dict | None, data_items: list[dict], diagnostics: list[dict]) -> str:
    """Classify an Actor call without mistaking infrastructure failure for valid zero yield.

    Provider UIs can report a technically completed container while the acquisition
    itself failed. Explicit FAILED/ABORTED/TIMED-OUT states therefore outrank a clean
    zero-output interpretation.
    """
    meta = meta or {}
    text = _diagnostic_text(meta, diagnostics)
    status = _fold(meta.get("status"))
    explicit_failed = status in {"failed", "aborted", "timed-out", "timed_out", "timeout"}
    explicit_transient = status in {"timed-out", "timed_out", "timeout"}
    permanent = any(marker in text for marker in PERMANENT_MARKERS)
    transient = any(marker in text for marker in TRANSIENT_MARKERS) or explicit_transient
    unexpected = any(_fold(d.get("status")) in {"aborted", "unexpected-error"} for d in diagnostics)
    invalid = any(_fold(d.get("status")) in {"invalid-input", "no-input"} for d in diagnostics)

    if data_items:
        if permanent or invalid:
            return "partial_permanent"
        if transient or unexpected or explicit_failed:
            return "partial_transient"
        return "success"
    if permanent or invalid:
        return "permanent_failure"
    if transient or unexpected:
        return "transient_failure"
    if explicit_failed:
        return "permanent_failure"
    # A genuinely clean zero-output success means scarcity, not infrastructure failure.
    return "empty"


def _resize_known_limit(inp: dict, target: int) -> dict:
    out = deepcopy(inp)
    target = max(1, int(target))
    for key in ("maxItems", "maxResults", "resultsLimit", "resultsCount", "maxArticles"):
        if key in out and out.get(key) not in (None, ""):
            try:
                current = int(out[key])
                out[key] = min(current, target) if current > 0 else target
            except Exception:
                out[key] = target
    if "maxItems" in out:
        out["maxItems"] = target
    return out


def _multi_target_field(inp: dict) -> tuple[str | None, list]:
    for field in MULTI_TARGET_FIELDS:
        value = inp.get(field)
        if isinstance(value, list) and len(value) > 1:
            return field, value
    return None, []


def split_input_for_retry(inp: dict, target: int) -> list[tuple[dict, int]]:
    """Split a multi-target call after a transient failure.

    This is intentionally generic: it understands common Actor array fields and leaves
    unknown inputs intact. It never increases requested items.
    """
    field, values = _multi_target_field(inp)
    if not field:
        return []
    mid = max(1, len(values) // 2)
    chunks = [values[:mid], values[mid:]]
    chunks = [c for c in chunks if c]
    shares = []
    remaining = max(1, int(target))
    for idx, chunk in enumerate(chunks):
        if idx == len(chunks) - 1:
            share = remaining
        else:
            share = max(1, round(target * len(chunk) / len(values)))
            share = min(remaining - (len(chunks) - idx - 1), share)
        shares.append(max(1, share))
        remaining -= shares[-1]
    out: list[tuple[dict, int]] = []
    for chunk, share in zip(chunks, shares):
        child = deepcopy(inp)
        child[field] = chunk
        child = _resize_known_limit(child, share)
        if "maxItemsPerTarget" in child:
            child["maxItemsPerTarget"] = max(1, int(math.ceil(share / len(chunk))))
        out.append((child, share))
    return out


def target_count(inp: dict) -> int:
    for field in MULTI_TARGET_FIELDS:
        value = inp.get(field)
        if isinstance(value, list):
            return max(1, len(value))
    return 1


def _attempt_cap(remaining_envelope: float, rate_per_1000: float | None, target: int, attempts_left: int) -> float:
    remaining_envelope = max(0.0, float(remaining_envelope))
    if remaining_envelope <= 0:
        return 0.0
    attempts_left = max(1, int(attempts_left))
    if rate_per_1000 not in (None, 0):
        expected = max(0.001, float(rate_per_1000) * max(1, int(target)) / 1000)
        # Enough headroom for ordinary pay-per-result calls, while retaining retry budget.
        return min(remaining_envelope, max(0.001, expected * 1.6))
    return min(remaining_envelope, max(0.001, remaining_envelope / attempts_left))


def _dedupe_raw(items: Iterable[dict]) -> list[dict]:
    out, seen = [], set()
    for row in items:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("id") or row.get("tweetId") or row.get("postId") or row.get("videoId") or ""),
            str(row.get("url") or row.get("postUrl") or row.get("webVideoUrl") or row.get("link") or ""),
            str(row.get("text") or row.get("caption") or row.get("title") or row.get("message") or "")[:500],
        )
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


@dataclass
class ResilientCallResult:
    status: str
    items: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    metas: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    accounted_cost_usd: float = 0.0
    provider_reported_cost_usd: float = 0.0
    cost_cap_violation: bool = False
    failure_kinds: list[str] = field(default_factory=list)
    adaptive_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def run_actor_resilient(
    runner,
    actor_id: str,
    run_input: dict,
    *,
    max_items: int,
    max_charge_usd: float,
    rate_per_1000: float | None = None,
    max_calls: int = 6,
    sleep_fn: Callable[[float], None] | None = None,
) -> ResilientCallResult:
    """Run one logical Actor acquisition inside a hard cost envelope.

    Transient failures are isolated. Multi-target inputs are split before retrying.
    Partial successful rows are preserved and deduplicated. No retry can spend above
    max_charge_usd. Unknown-cost exceptions are charged conservatively at the attempt cap.
    """
    max_items = max(1, int(max_items))
    envelope = max(0.0, float(max_charge_usd))
    sleep_fn = sleep_fn or (lambda seconds: None if settings.signalyth_dry_run else time.sleep(seconds))

    result = ResilientCallResult(status="failed")
    queue: list[dict] = [{"input": deepcopy(run_input), "target": max_items, "depth": 0, "retry_no": 0}]
    calls = 0

    while queue and calls < max(1, int(max_calls)) and result.accounted_cost_usd < envelope - 1e-9:
        task = queue.pop(0)
        target = max(1, int(task["target"]))
        calls += 1
        remaining = max(0.0, envelope - result.accounted_cost_usd)
        attempt_cap = _attempt_cap(remaining, rate_per_1000, target, max_calls - calls + 1)
        if attempt_cap <= 0:
            result.failure_kinds.append("budget_envelope_exhausted")
            break

        attempt = {
            "call": calls,
            "target_items": target,
            "target_count": target_count(task["input"]),
            "max_charge_usd": round(attempt_cap, 6),
            "depth": int(task.get("depth", 0)),
            "retry_no": int(task.get("retry_no", 0)),
            "status": "running",
        }
        result.attempts.append(attempt)
        try:
            meta, raw_items = runner.run(
                actor_id,
                task["input"],
                max_items=target,
                max_charge_usd=attempt_cap,
            )
            meta = dict(meta or {})
            data_items, diagnostics = split_diagnostic_rows(raw_items or [])
            outcome = classify_actor_outcome(meta, data_items, diagnostics)
            actual = actual_cost_from_meta(meta)
            if actual is not None:
                result.provider_reported_cost_usd += max(0.0, float(actual))
            # Never silently clamp a provider-reported charge above the hard per-call cap.
            # That would make the UI look budget-safe even though the upstream provider says otherwise.
            if actual is not None and float(actual) > attempt_cap + 1e-6:
                result.cost_cap_violation = True
                result.failure_kinds.append("cost_cap_violation")
                result.accounted_cost_usd = envelope
                result.metas.append(meta)
                result.diagnostics.extend(diagnostics)
                result.items.extend(data_items)
                attempt.update({
                    "status": "cost_cap_violation",
                    "returned_items": len(data_items),
                    "diagnostic_rows": len(diagnostics),
                    "provider_reported_cost_usd": round(float(actual), 6),
                    "accounted_cost_usd": round(envelope, 6),
                    "error": "Provider reported cost above the SIGNALYTH hard per-call cap.",
                })
                break
            charged = attempt_cap if actual is None else min(remaining, max(0.0, float(actual)))
            result.accounted_cost_usd += charged
            result.metas.append(meta)
            result.diagnostics.extend(diagnostics)
            result.items.extend(data_items)
            attempt.update({
                "status": outcome,
                "returned_items": len(data_items),
                "diagnostic_rows": len(diagnostics),
                "provider_reported_cost_usd": round(float(actual), 6) if actual is not None else None,
                "accounted_cost_usd": round(charged, 6),
            })

            if outcome in {"success", "empty", "partial_permanent"}:
                if outcome == "partial_permanent":
                    result.failure_kinds.append("permanent")
                continue

            if outcome in {"transient_failure", "partial_transient"}:
                result.failure_kinds.append("transient")
                remaining_target = max(1, target - len(data_items))
                children = split_input_for_retry(task["input"], remaining_target)
                if children and calls + len(children) <= max_calls:
                    result.adaptive_actions.append("split_multi_target_after_transient_failure")
                    for child_input, child_target in children:
                        queue.append({
                            "input": child_input,
                            "target": child_target,
                            "depth": int(task.get("depth", 0)) + 1,
                            "retry_no": 0,
                        })
                elif int(task.get("retry_no", 0)) < 1 and calls < max_calls:
                    result.adaptive_actions.append("retry_single_target_after_transient_failure")
                    sleep_fn(min(2.0, 0.5 * (2 ** int(task.get("retry_no", 0)))))
                    retry_input = _resize_known_limit(task["input"], remaining_target)
                    queue.append({
                        "input": retry_input,
                        "target": remaining_target,
                        "depth": int(task.get("depth", 0)),
                        "retry_no": int(task.get("retry_no", 0)) + 1,
                    })
                continue

            result.failure_kinds.append("permanent")
        except Exception as exc:
            kind = classify_exception(exc)
            # Unknown actual cost is conservatively accounted at the cap.
            result.accounted_cost_usd += attempt_cap
            attempt.update({
                "status": "exception",
                "failure_kind": kind,
                "error": str(exc),
                "accounted_cost_usd": round(attempt_cap, 6),
            })
            result.failure_kinds.append(kind)
            if kind == "transient":
                children = split_input_for_retry(task["input"], target)
                if children and calls + len(children) <= max_calls:
                    result.adaptive_actions.append("split_multi_target_after_exception")
                    for child_input, child_target in children:
                        queue.append({"input": child_input, "target": child_target, "depth": int(task.get("depth", 0)) + 1, "retry_no": 0})
                elif int(task.get("retry_no", 0)) < 1 and calls < max_calls:
                    result.adaptive_actions.append("retry_single_target_after_exception")
                    sleep_fn(min(2.0, 0.5 * (2 ** int(task.get("retry_no", 0)))))
                    queue.append({"input": deepcopy(task["input"]), "target": target, "depth": int(task.get("depth", 0)), "retry_no": int(task.get("retry_no", 0)) + 1})
            # Permanent/unknown exceptions are isolated; caller continues with later subruns/sources.

    result.items = _dedupe_raw(result.items)
    result.diagnostics = _dedupe_raw(result.diagnostics)
    result.accounted_cost_usd = round(min(envelope, result.accounted_cost_usd), 6)

    result.provider_reported_cost_usd = round(result.provider_reported_cost_usd, 6)
    has_transient = "transient" in result.failure_kinds
    has_permanent = any(k in {"permanent", "unknown", "cost_cap_violation"} for k in result.failure_kinds)
    if result.items:
        result.status = "partial" if (has_transient or has_permanent) else "succeeded"
    elif has_transient or has_permanent:
        result.status = "failed"
    else:
        result.status = "empty"
    return result


class ActorHealthStore:
    """Small non-secret health profile used to make future runs more conservative.

    It never contains API tokens or raw evidence. Persistence is disabled in dry-run/test mode.
    """
    def __init__(self, path: Path | None = None):
        self.path = path or (Path(settings.signalyth_data_dir) / "actor-health.json")

    def read(self) -> dict:
        try:
            if self.path.exists():
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
        return {}

    def safe_batch_size(self, source: str, actor_id: str) -> int:
        default = int(DEFAULT_SAFE_BATCH_SIZE.get(source, 1))
        row = self.read().get(f"{source}:{actor_id}", {})
        try:
            learned = int(row.get("safe_batch_size", default))
            return max(1, min(default, learned))
        except Exception:
            return default

    def record(self, source: str, actor_id: str, call_result: ResilientCallResult, observed_batch_size: int):
        if settings.signalyth_dry_run:
            return
        payload = self.read()
        key = f"{source}:{actor_id}"
        row = dict(payload.get(key) or {})
        row["calls"] = int(row.get("calls", 0) or 0) + len(call_result.attempts)
        row["logical_runs"] = int(row.get("logical_runs", 0) or 0) + 1
        row["successes"] = int(row.get("successes", 0) or 0) + (1 if call_result.status in {"succeeded", "partial", "empty"} else 0)
        row["failures"] = int(row.get("failures", 0) or 0) + (1 if call_result.status == "failed" else 0)
        row["transient_failures"] = int(row.get("transient_failures", 0) or 0) + sum(1 for k in call_result.failure_kinds if k == "transient")
        row["last_status"] = call_result.status
        if "transient" in call_result.failure_kinds and observed_batch_size > 1:
            row["safe_batch_size"] = max(1, observed_batch_size // 2)
        else:
            row.setdefault("safe_batch_size", int(DEFAULT_SAFE_BATCH_SIZE.get(source, 1)))
        payload[key] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def get_safe_batch_size(source: str, actor_id: str) -> int:
    return ActorHealthStore().safe_batch_size(source, actor_id)
