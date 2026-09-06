from __future__ import annotations

import itertools
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import AnalysisDraft
from app.services.apify_service import CollectionNotConfigured
from app.services.collector import execute_plan
from app.services.query_planner import build_collection_plan
from app.services.resilience import run_actor_resilient

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]


def _draft(sources, target=None, budget=2.0):
    return AnalysisDraft(
        client="Administrative Client Name",
        topic="Allwyn",
        market="Greece",
        date_from=date(2026, 8, 15),
        date_to=date(2026, 8, 31),
        keywords=["OPAP"],
        sources=list(sources),
        sample_mode="automatic",
        sample_target=target or max(1, len(sources)),
        comments=False,
        max_budget_usd=budget,
        smart_search=True,
        report_language="Ελληνικά",
    )


def _tiny_plan(sources):
    plan = build_collection_plan(_draft(sources, target=max(1, len(sources)), budget=2.0)).model_dump(mode="json")
    for sp in plan["sources"]:
        sp["target_items"] = 1
        sp["subruns"] = sp["subruns"][:1]
        sr = sp["subruns"][0]
        sr["target_items"] = 1
        sr["max_charge_usd"] = min(0.02, float(sr.get("max_charge_usd") or 0.02))
    plan["target_total"] = len(sources)
    plan["max_budget_usd"] = 2.0
    return plan


class SuccessRunner:
    calls = 0
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls += 1
        return {"status": "SUCCEEDED", "usageTotalUsd": min(0.0001, max_charge_usd)}, [{
            "text": f"evidence {actor_id}",
            "timestamp": "2026-08-20T12:00:00Z",
            "url": f"https://evidence.invalid/{actor_id}/{self.__class__.calls}",
        }]


def test_live_full_collection_is_blocked_until_selected_actors_are_verified():
    plan = _tiny_plan(["x", "news"])
    with tempfile.TemporaryDirectory() as td, \
         patch("app.services.collector.settings.signalyth_dry_run", False), \
         patch("app.services.collector.load_registry", return_value={
             "x": {"actor_id": plan["sources"][0]["actor_id"], "actor_status": "unverified"},
             "news": {"actor_id": plan["sources"][1]["actor_id"], "actor_status": "verified"},
         }), \
         patch("app.services.collector.ApifyRunner") as runner_cls:
        with pytest.raises(CollectionNotConfigured, match="tiny paid smoke test"):
            execute_plan(plan, "live-block", Path(td))
        runner_cls.assert_not_called()


def test_live_collection_can_start_after_exact_actor_ids_are_verified():
    plan = _tiny_plan(["x", "news"])
    verified = {sp["source"]: {"actor_id": sp["actor_id"], "actor_status": "verified"} for sp in plan["sources"]}
    SuccessRunner.calls = 0
    with tempfile.TemporaryDirectory() as td, \
         patch("app.services.collector.settings.signalyth_dry_run", False), \
         patch("app.services.collector.load_registry", return_value=verified), \
         patch("app.services.collector.ApifyRunner", SuccessRunner), \
         patch("app.services.resilience.settings.signalyth_dry_run", True):
        status = execute_plan(plan, "live-verified", Path(td))
    assert status["status"] == "succeeded"
    assert status["normalized_total"] == 2


def test_provider_reported_cost_above_attempt_cap_is_never_silently_clamped():
    class OverCap:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return {"status": "SUCCEEDED", "usageTotalUsd": max_charge_usd + 0.5}, [{"id": "1", "text": "evidence"}]
    result = run_actor_resilient(
        OverCap(), "actor/test", {"query": "Allwyn", "maxItems": 1},
        max_items=1, max_charge_usd=0.01, rate_per_1000=None, max_calls=2,
        sleep_fn=lambda _: None,
    )
    assert result.cost_cap_violation is True
    assert "cost_cap_violation" in result.failure_kinds
    assert result.status == "partial"
    assert result.provider_reported_cost_usd > 0.01
    assert result.accounted_cost_usd <= 0.01


def test_provider_cost_cap_violation_stops_all_later_paid_sources_and_preserves_evidence():
    plan = _tiny_plan(["x", "news"])
    class OverCapThenSuccess:
        calls = []
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            self.__class__.calls.append(actor_id)
            if len(self.__class__.calls) == 1:
                return {"status": "SUCCEEDED", "usageTotalUsd": max_charge_usd + 0.25}, [{
                    "text": "preserved evidence", "timestamp": "2026-08-20T12:00:00Z", "url": "https://evidence.invalid/overcap"
                }]
            return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [{
                "text": "should never be purchased", "timestamp": "2026-08-20T12:00:00Z", "url": "https://evidence.invalid/later"
            }]
    OverCapThenSuccess.calls = []
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", OverCapThenSuccess):
        folder = Path(td)
        status = execute_plan(plan, "overcap-stop", folder)
        assert status["status"] == "failed"
        assert status["budget"]["violation_detected"] is True
        assert status["normalized_total"] == 1
        assert "preserved evidence" in (folder / "normalized-all.json").read_text(encoding="utf-8")
    assert len(OverCapThenSuccess.calls) == 1
    failed_sources = [s for s, row in status["sources"].items() if row["status"] == "failed"]
    skipped_sources = [s for s, row in status["sources"].items() if row["status"] == "skipped_budget_safety"]
    assert len(failed_sources) == 1
    assert len(skipped_sources) == 1


def test_raw_rows_with_unusable_schema_are_failure_not_false_scarcity():
    class Drifted:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [{"newPayload": "schema changed", "unknownDate": "yesterday"}]
    plan = _tiny_plan(["news"])
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", Drifted):
        status = execute_plan(plan, "schema-drift", Path(td))
    assert status["status"] == "failed"
    src = status["sources"]["news"]
    assert src["data_contract_failure"] is True
    assert src["status"] == "failed"
    assert "schema or mapping drift" in src["error"]


def test_all_rows_missing_dates_are_failure_not_clean_zero_yield():
    class MissingDates:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [{
                "text": "real text but date missing", "url": "https://evidence.invalid/no-date"
            }]
    plan = _tiny_plan(["x"])
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", MissingDates):
        status = execute_plan(plan, "missing-dates", Path(td))
    assert status["status"] == "failed"
    assert status["sources"]["x"]["missing_date_items"] >= 1
    assert status["sources"]["x"]["data_contract_failure"] is True


def test_exhaustive_63_combinations_isolate_every_possible_single_source_failure():
    combos = []
    for r in range(1, len(SOURCES) + 1):
        combos.extend(itertools.combinations(SOURCES, r))
    exercised_positions = 0
    for failure_message in ("HTTP 504 gateway timeout", "HTTP 403 forbidden"):
        for combo in combos:
            plan = _tiny_plan(combo)
            actors = {sp["source"]: sp["actor_id"] for sp in plan["sources"]}
            for failing_source in combo:
                exercised_positions += 1
                failing_actor = actors[failing_source]
                class OneSourceFailure:
                    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
                        if actor_id == failing_actor:
                            raise RuntimeError(failure_message)
                        return {"status": "SUCCEEDED", "usageTotalUsd": 0.0}, [{
                            "text": f"healthy {actor_id}", "timestamp": "2026-08-20T12:00:00Z", "url": f"https://healthy.invalid/{actor_id}"
                        }]
                with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", OneSourceFailure):
                    status = execute_plan(plan, f"matrix-{exercised_positions}", Path(td))
                assert status["sources"][failing_source]["status"] == "failed"
                if len(combo) == 1:
                    assert status["status"] == "failed"
                else:
                    assert status["status"] == "completed_with_errors"
                    for source in combo:
                        if source != failing_source:
                            assert status["sources"][source]["collected"] == 1
    # 192 source positions across the 63 combinations, exercised once as transient and once as permanent.
    assert exercised_positions == 384

