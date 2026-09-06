from __future__ import annotations

import itertools
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import AnalysisDraft
from app.services.collector import BudgetGuard, execute_plan
from app.services.query_planner import build_collection_plan
from app.services.resilience import ActorHealthStore, classify_actor_outcome, run_actor_resilient

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]


def mkdraft(sources, *, target=None, budget=5.0, comments=True):
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn", "OPAP"], sources=list(sources),
        sample_mode="automatic", sample_target=target or max(1, len(sources)),
        comments=comments, max_budget_usd=budget, smart_search=True,
        report_language="Ελληνικά",
    )


def combos():
    for r in range(1, len(SOURCES) + 1):
        yield from itertools.combinations(SOURCES, r)


class HealthyRunner:
    calls = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append((actor_id, dict(run_input), max_items, max_charge_usd))
        row = {
            "text": f"evidence from {actor_id}",
            "timestamp": "2026-08-20T12:00:00Z",
            "url": f"https://evidence.invalid/{actor_id}/{len(self.__class__.calls)}",
            "authorUsername": "tester",
        }
        return {"status": "SUCCEEDED", "usageTotalUsd": min(max_charge_usd, 0.000001)}, [row][:max_items]


def test_every_one_of_63_source_combinations_executes_and_preserves_each_source():
    count = 0
    for combo in combos():
        count += 1
        HealthyRunner.calls = []
        plan = build_collection_plan(mkdraft(combo, target=len(combo), comments=False)).model_dump(mode="json")
        # Tiny target semantics: exactly one logical target per selected source, never hidden over-requesting.
        assert all(sum(sr["target_items"] for sr in sp["subruns"]) == sp["target_items"] for sp in plan["sources"])
        with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", HealthyRunner):
            folder = Path(td)
            status = execute_plan(plan, f"combo-{count}", folder)
            assert status["status"] == "succeeded"
            assert status["normalized_total"] == len(combo)
            for source in combo:
                assert status["sources"][source]["collected"] == 1
                assert (folder / f"raw-{source}.json").exists()
                assert (folder / f"normalized-{source}.json").exists()
    assert count == 63


@pytest.mark.parametrize("source", SOURCES)
def test_tiny_target_never_multiplies_paid_subruns(source):
    plan = build_collection_plan(mkdraft([source], target=1, budget=0.01)).model_dump(mode="json")
    sp = plan["sources"][0]
    assert sp["target_items"] == 1
    assert len(sp["subruns"]) == 1
    assert sp["subruns"][0]["target_items"] == 1
    assert sp["subruns"][0]["max_charge_usd"] > 0


def test_explicit_failed_provider_status_is_never_treated_as_clean_scarcity():
    assert classify_actor_outcome({"status": "FAILED"}, [], []) == "permanent_failure"
    assert classify_actor_outcome({"status": "TIMED-OUT"}, [], []) == "transient_failure"
    assert classify_actor_outcome({"status": "ABORTED"}, [], []) == "permanent_failure"


class AlwaysForbidden:
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        raise RuntimeError("HTTP 403 forbidden")


def test_when_all_selected_sources_fail_run_is_failed_not_green_completed():
    plan = build_collection_plan(mkdraft(["x", "facebook"], target=4, budget=1.0, comments=False)).model_dump(mode="json")
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", AlwaysForbidden):
        status = execute_plan(plan, "all-fail", Path(td), continue_pipeline=True)
    assert status["status"] == "failed"
    assert status["collection_status"] == "failed"
    assert status["normalized_total"] == 0
    assert all(v["status"] == "failed" for v in status["sources"].values())


class AlwaysTimeout:
    calls = 0
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls += 1
        raise RuntimeError("HTTP 504 gateway timeout")


def test_repeated_transient_failures_open_source_circuit_and_skip_remaining_batches():
    # X target is large enough to produce multiple safe logical batches.
    plan = build_collection_plan(mkdraft(["x"], target=1000, budget=1.0, comments=False)).model_dump(mode="json")
    assert len(plan["sources"][0]["subruns"]) >= 3
    # Keep each logical resilient call short for a deterministic torture test.
    for sr in plan["sources"][0]["subruns"]:
        sr["max_attempt_calls"] = 2
    AlwaysTimeout.calls = 0
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", AlwaysTimeout):
        status = execute_plan(plan, "circuit", Path(td))
    src = status["sources"]["x"]
    assert src["status"] == "failed"
    assert src["circuit_breaker"]["open"] is True
    assert src["circuit_breaker"]["reason"] == "repeated_transient_actor_failure"
    assert any(sr["status"] == "skipped_circuit_open" for sr in src["subruns"])
    # Crucially, not every planned batch was sent to the unhealthy upstream route.
    assert src["subruns_completed"] == src["subruns_total"]


class MixedRunner:
    call_count = {}
    actors = {}

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        n = self.__class__.call_count.get(actor_id, 0) + 1
        self.__class__.call_count[actor_id] = n
        source = self.__class__.actors.get(actor_id)
        if source == "facebook":
            raise RuntimeError("HTTP 403 forbidden")
        if source == "x" and n == 1:
            raise RuntimeError("HTTP 504 gateway timeout")
        if source == "instagram":
            return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, []
        return {
            "status": "SUCCEEDED", "usageTotalUsd": min(max_charge_usd, 0.000001)
        }, [{
            "text": f"valid {source}", "timestamp": "2026-08-20T12:00:00Z",
            "url": f"https://evidence.invalid/{source}/{n}", "authorUsername": source,
        }]


def test_mixed_six_source_disaster_preserves_good_evidence_and_exposes_degradation():
    plan_obj = build_collection_plan(mkdraft(SOURCES, target=12, budget=2.0, comments=False))
    plan = plan_obj.model_dump(mode="json")
    MixedRunner.call_count = {}
    MixedRunner.actors = {sp.actor_id: sp.source for sp in plan_obj.sources}
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", MixedRunner):
        folder = Path(td)
        status = execute_plan(plan, "mixed-six", folder)
        assert status["status"] == "completed_with_errors"
        assert status["sources"]["facebook"]["status"] == "failed"
        assert status["sources"]["x"]["collected"] > 0
        assert status["sources"]["instagram"]["status"] == "succeeded_empty"
        assert status["sources"]["news"]["collected"] > 0
        assert status["budget"]["spent_usd"] <= status["budget"]["max_usd"]
        assert (folder / "normalized-all.json").exists()


class MalformedRunner:
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [
            {}, {"foo": "bar"},
            {"text": "valid", "timestamp": "2026-08-20T12:00:00Z", "url": "https://valid.invalid/1"},
            {"text": "missing date", "url": "https://valid.invalid/2"},
        ]


def test_malformed_and_missing_date_rows_cannot_poison_analysis_dataset():
    plan = build_collection_plan(mkdraft(["news"], target=4, budget=1.0, comments=False)).model_dump(mode="json")
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", MalformedRunner):
        folder = Path(td)
        status = execute_plan(plan, "malformed", folder)
        assert status["sources"]["news"]["normalized_before_date_filter"] == 2
        assert status["sources"]["news"]["date_filtered_out"] == 1
        assert status["sources"]["news"]["collected"] == 1
        assert status["status"] == "completed_shortfall"


def test_budget_guard_accounts_provider_runtime_overhead_against_global_cap():
    guard = BudgetGuard(1.0)
    reservation = guard.reserve(0.10)
    charged = guard.settle(reservation, 0.11)
    assert charged == 0.11
    assert guard.spent == 0.11
    assert guard.remaining == pytest.approx(0.89)


def test_actor_health_store_learns_smaller_batch_after_transient_failure(tmp_path):
    class OneTimeout:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            raise RuntimeError("HTTP 504 timeout")
    result = run_actor_resilient(
        OneTimeout(), "actor/x", {"searchTerms": ["a", "b", "c", "d"], "maxItems": 4},
        max_items=4, max_charge_usd=0.01, rate_per_1000=0.15, max_calls=1,
        sleep_fn=lambda _: None,
    )
    path = tmp_path / "actor-health.json"
    store = ActorHealthStore(path)
    with patch("app.services.resilience.settings.signalyth_dry_run", False):
        store.record("x", "actor/x", result, observed_batch_size=4)
    assert store.safe_batch_size("x", "actor/x") == 2
    payload = path.read_text(encoding="utf-8")
    assert "token" not in payload.casefold()


def test_same_external_url_from_two_sources_remains_two_source_evidence_units():
    class SameUrl:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [{
                "text": "same syndicated headline", "timestamp": "2026-08-20T12:00:00Z",
                "url": "https://same.invalid/story", "authorUsername": "publisher",
            }]
    plan = build_collection_plan(mkdraft(["x", "news"], target=2, budget=1.0, comments=False)).model_dump(mode="json")
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", SameUrl):
        status = execute_plan(plan, "cross-source", Path(td))
    assert status["normalized_total"] == 2
    assert status["status"] == "succeeded"
