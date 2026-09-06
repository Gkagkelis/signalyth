from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.services.resilience import (
    classify_actor_outcome,
    run_actor_resilient,
    split_diagnostic_rows,
    split_input_for_retry,
)
from app.services.collector import execute_plan


class QueueRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls.append((actor_id, dict(run_input), max_items, max_charge_usd))
        if not self.responses:
            raise AssertionError("unexpected extra actor call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def diagnostic(status, message):
    return {"id": f"diag:{status}", "resultType": "diagnostic", "status": status, "message": message}


def test_diagnostic_rows_are_never_data_rows():
    rows = [
        {"id": "1", "text": "real", "timestamp": "2026-08-15T00:00:00Z"},
        diagnostic("zero-output", "No tweets matched the query"),
    ]
    data, diag = split_diagnostic_rows(rows)
    assert len(data) == 1
    assert len(diag) == 1


def test_clean_zero_output_is_scarcity_not_infrastructure_failure():
    diag = [diagnostic("zero-output", "No tweets matched. Check the search or retry.")]
    assert classify_actor_outcome({"status": "SUCCEEDED"}, [], diag) == "empty"


def test_green_succeeded_with_partial_failure_504_is_failure():
    diag = [diagnostic("zero-output", "Partial result. 0 tweets fetched. Fetch failed.")]
    meta = {"status": "SUCCEEDED", "statusMessage": "partial_failure HTTP 504 retry-exhausted"}
    assert classify_actor_outcome(meta, [], diag) == "transient_failure"


def test_multi_target_transient_exception_splits_and_preserves_successes():
    runner = QueueRunner([
        RuntimeError("HTTP 504 Gateway Timeout"),
        ({"usageTotalUsd": 0.001}, [{"id": "a", "text": "a"}]),
        ({"usageTotalUsd": 0.001}, [{"id": "b", "text": "b"}]),
    ])
    result = run_actor_resilient(
        runner,
        "actor/test",
        {"searchTerms": ["q1", "q2", "q3", "q4"], "maxItems": 4, "maxItemsPerTarget": 1},
        max_items=4,
        max_charge_usd=0.02,
        rate_per_1000=0.15,
        max_calls=6,
        sleep_fn=lambda _: None,
    )
    assert result.status == "partial"  # transient happened, but evidence survived
    assert {r["id"] for r in result.items} == {"a", "b"}
    assert "split_multi_target_after_exception" in result.adaptive_actions
    assert result.accounted_cost_usd <= 0.02


def test_single_target_429_retries_once_then_succeeds():
    runner = QueueRunner([
        RuntimeError("HTTP 429 rate limit"),
        ({"usageTotalUsd": 0.001}, [{"id": "ok", "text": "ok"}]),
    ])
    result = run_actor_resilient(
        runner, "actor/test", {"query": "x", "maxItems": 5},
        max_items=5, max_charge_usd=0.02, rate_per_1000=0.15, max_calls=4,
        sleep_fn=lambda _: None,
    )
    assert result.status == "partial"
    assert [x["id"] for x in result.items] == ["ok"]
    assert len(runner.calls) == 2


def test_permanent_403_does_not_retry():
    runner = QueueRunner([RuntimeError("HTTP 403 forbidden")])
    result = run_actor_resilient(
        runner, "actor/test", {"searchTerms": ["a", "b"]},
        max_items=10, max_charge_usd=0.02, rate_per_1000=0.15, max_calls=6,
        sleep_fn=lambda _: None,
    )
    assert result.status == "failed"
    assert len(runner.calls) == 1
    assert "permanent" in result.failure_kinds


def test_budget_envelope_is_never_exceeded_under_retries():
    runner = QueueRunner([RuntimeError("HTTP 504 timeout")] * 20)
    result = run_actor_resilient(
        runner, "actor/test", {"searchTerms": ["a", "b", "c", "d"], "maxItems": 1000},
        max_items=1000, max_charge_usd=0.007, rate_per_1000=None, max_calls=10,
        sleep_fn=lambda _: None,
    )
    assert result.accounted_cost_usd <= 0.007 + 1e-9
    assert len(runner.calls) <= 10


def test_split_never_increases_target():
    parts = split_input_for_retry({"queries": ["a", "b", "c", "d"], "maxItems": 7}, 7)
    assert len(parts) == 2
    assert sum(target for _, target in parts) == 7
    assert all(inp["maxItems"] <= target for inp, target in parts)


def test_collector_does_not_normalize_504_diagnostic_and_continues_next_batch():
    class Fake:
        calls = 0
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            self.__class__.calls += 1
            if run_input.get("query") == "bad":
                return (
                    {"status": "SUCCEEDED", "statusMessage": "partial_failure HTTP 504", "usageTotalUsd": 0.001},
                    [diagnostic("zero-output", "Fetch failed HTTP 504")],
                )
            return (
                {"status": "SUCCEEDED", "usageTotalUsd": 0.001},
                [{"text": "good evidence", "timestamp": "2026-08-15T10:00:00Z", "url": "https://x/good"}],
            )

    plan = {
        "max_budget_usd": 1.0,
        "date_from": "2026-08-01", "date_to": "2026-08-31",
        "sample_mode": "perSource", "target_total": 2, "rebalancing_enabled": False,
        "sources": [{
            "source": "x", "target_items": 2, "price_per_1000_hint": 0.15,
            "subruns": [
                {"actor_id": "xquik/x-tweet-scraper", "input": {"query": "bad", "maxItems": 1}, "target_items": 1, "max_charge_usd": 0.02, "max_attempt_calls": 2},
                {"actor_id": "xquik/x-tweet-scraper", "input": {"query": "good", "maxItems": 1}, "target_items": 1, "max_charge_usd": 0.02, "max_attempt_calls": 2},
            ],
        }],
    }
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", Fake):
        folder = Path(td)
        status = execute_plan(plan, "resilience-green-failure", folder)
        raw = (folder / "raw-x.json").read_text(encoding="utf-8")
        normalized = (folder / "normalized-x.json").read_text(encoding="utf-8")
        diagnostics = (folder / "diagnostics-x.json").read_text(encoding="utf-8")
    assert "good evidence" in normalized
    assert "diag:zero-output" not in normalized
    assert "diag:zero-output" in raw
    assert "diag:zero-output" in diagnostics
    assert status["sources"]["x"]["status"] == "partial"
    assert status["status"] == "completed_with_errors"
