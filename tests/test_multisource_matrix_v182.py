from __future__ import annotations

import itertools
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.collector import execute_plan

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]
ARRAY_FIELDS = ("searchTerms", "search", "queries", "keywords", "directUrls", "startUrls", "replyTweetIds", "urls")
SAFE = {"x": 2, "tiktok": 2, "instagram": 1, "facebook": 1, "youtube": 2, "news": 2}


def draft(sources, target=1000, budget=10.0, comments=True):
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn", "OPAP"], sources=list(sources), sample_target=target,
        sample_mode="automatic", comments=comments, max_budget_usd=budget,
        smart_search=True, report_language="Ελληνικά",
    )


def combos():
    for r in range(1, len(SOURCES) + 1):
        yield from itertools.combinations(SOURCES, r)


def test_all_63_source_combinations_have_budget_safe_isolated_plans():
    seen = 0
    for combo in combos():
        plan = build_collection_plan(draft(combo))
        seen += 1
        assert plan.target_total == 1000
        assert {sp.source for sp in plan.sources} == set(combo)
        assert sum(sr.max_charge_usd for sp in plan.sources for sr in sp.subruns) <= plan.max_budget_usd + 1e-9
        assert all(sr.resilience_policy == "adaptive-isolation-v1" for sp in plan.sources for sr in sp.subruns)
        for sp in plan.sources:
            assert sp.subruns
            for sr in sp.subruns:
                for field in ARRAY_FIELDS:
                    value = sr.input.get(field)
                    if isinstance(value, list):
                        assert len(value) <= SAFE[sp.source]
        forecast = plan.preflight_forecast
        assert forecast["failure_isolation"] == "per logical Actor batch and per source"
        if plan.comments_requested:
            assert set(forecast["comments_coverage"]["verification_blockers"]).issubset(set(combo))
    assert seen == 63


def test_all_63_combinations_remain_budget_safe_at_tiny_and_large_targets():
    for combo in combos():
        for target, budget in ((1, 0.01), (5, 0.05), (100, 0.25), (3000, 20.0)):
            plan = build_collection_plan(draft(combo, target=target, budget=budget))
            caps = sum(sr.max_charge_usd for sp in plan.sources for sr in sp.subruns)
            assert caps <= budget + 1e-9
            assert sum(sp.target_items for sp in plan.sources) == target


def test_comments_forecast_never_claims_unverified_full_coverage():
    plan = build_collection_plan(draft(SOURCES, comments=True))
    cc = plan.preflight_forecast["comments_coverage"]
    assert cc["fully_live_verified"] is False
    assert "facebook" in cc["verification_blockers"]
    assert "tiktok" in cc["verification_blockers"]
    assert "youtube" in cc["verification_blockers"]
    rows = {r["source"]: r for r in plan.preflight_forecast["sources"]}
    assert rows["news"]["comments"]["status"] == "not_applicable"
    assert rows["x"]["comments"]["status"] == "available_but_unverified"


class MatrixRunner:
    failing_actor = None
    transient_actor = None
    calls = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append((actor_id, max_items, max_charge_usd))
        if actor_id == self.__class__.failing_actor:
            raise RuntimeError("HTTP 403 forbidden")
        if actor_id == self.__class__.transient_actor:
            raise RuntimeError("HTTP 504 gateway timeout")
        n = min(max_items, 2)
        items = [
            {"text": f"evidence {actor_id} {i}", "timestamp": "2026-08-20T12:00:00Z", "url": f"https://evidence/{actor_id}/{len(self.__class__.calls)}/{i}"}
            for i in range(n)
        ]
        return {"status": "SUCCEEDED", "usageTotalUsd": min(max_charge_usd, 0.0002)}, items


def minimal_plan(combo):
    # use real planner, then shrink each source to one tiny logical batch so execution matrix stays fast
    p = build_collection_plan(draft(combo, target=max(6, len(combo) * 2), budget=2.0, comments=False)).model_dump(mode="json")
    for sp in p["sources"]:
        sp["target_items"] = 2
        sp["subruns"] = sp["subruns"][:1]
        sp["subruns"][0]["target_items"] = 2
        sp["subruns"][0]["max_charge_usd"] = min(0.05, sp["subruns"][0]["max_charge_usd"] or 0.05)
    p["target_total"] = 2 * len(combo)
    return p


def test_each_source_can_fail_without_erasing_other_source_evidence():
    # 6 representative multi-source failure-isolation scenarios; planning matrix above covers all 63 combinations.
    all_combo = tuple(SOURCES)
    actor_by_source = {sp.source: sp.actor_id for sp in build_collection_plan(draft(all_combo, target=12, comments=False)).sources}
    for failing_source in SOURCES:
        MatrixRunner.calls = []
        MatrixRunner.failing_actor = actor_by_source[failing_source]
        MatrixRunner.transient_actor = None
        with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", MatrixRunner):
            folder = Path(td)
            status = execute_plan(minimal_plan(all_combo), f"fail-{failing_source}", folder)
            assert status["sources"][failing_source]["status"] == "failed"
            assert status["status"] == "completed_with_errors"
            for source in SOURCES:
                if source == failing_source:
                    continue
                assert status["sources"][source]["collected"] > 0
                assert (folder / f"raw-{source}.json").exists()
        MatrixRunner.failing_actor = None


def test_two_simultaneous_source_failures_are_isolated():
    class TwoFail(MatrixRunner):
        failed = set()
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            if actor_id in self.__class__.failed:
                raise RuntimeError("HTTP 504 timeout")
            return super().run(actor_id, run_input, max_items=max_items, max_charge_usd=max_charge_usd)

    all_combo = tuple(SOURCES)
    actor_by_source = {sp.source: sp.actor_id for sp in build_collection_plan(draft(all_combo, target=12, comments=False)).sources}
    TwoFail.failed = {actor_by_source["x"], actor_by_source["facebook"]}
    MatrixRunner.failing_actor = None
    MatrixRunner.transient_actor = None
    with tempfile.TemporaryDirectory() as td, patch("app.services.collector.ApifyRunner", TwoFail):
        status = execute_plan(minimal_plan(all_combo), "two-fail", Path(td))
    assert status["sources"]["x"]["status"] == "failed"
    assert status["sources"]["facebook"]["status"] == "failed"
    assert status["sources"]["news"]["collected"] > 0
    assert status["budget"]["spent_usd"] <= status["budget"]["max_usd"]
