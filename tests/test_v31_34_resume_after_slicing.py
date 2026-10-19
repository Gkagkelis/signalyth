"""v31.34 — a sliced source must still resume where it stopped.

Adaptive time slicing (v31.31) appends routes WHILE a source is running. The
next worker rebuilds the plan from the original subruns, so the prior status
row holds MORE routes than the fresh plan does. The mid-source resume guard
demanded an exact match, so after slicing it never matched, and every worker
cycle re-collected the source from zero.

Seen live in run 20261019T155108Z: the news source reached 10 records, the
worker cycled, and it reappeared at 0 — again and again, paying each time and
never finishing, while the completed routes sat in the archive unused.

The prefix is what matters. Those routes are the same ones, in the same order,
and they are already paid for.
"""
from __future__ import annotations

from datetime import date

import app.services.collector as collector
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.storage import RunStore


def _plan_and_store(tmp_path):
    draft = AnalysisDraft(
        client="ΚΠΙΣΝ", topic="ΚΠΙΣΝ", market="Greece",
        date_from=date(2026, 4, 19), date_to=date(2026, 10, 19),
        keywords=["SNFCC"], keyword_roles={"SNFCC": "alias"},
        sources=["news"], sample_mode="perSource", per_source={"news": 400},
        comments=False, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", place_subject=True,
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    return plan, store


def test_a_source_whose_rows_outnumber_its_plan_still_resumes(tmp_path, monkeypatch):
    plan, store = _plan_and_store(tmp_path)
    planned = len((plan["sources"][0].get("subruns") or []))
    assert planned >= 2

    # The shape slicing leaves behind: every planned route finished, and extra
    # sliced routes were appended beyond them.
    prior_rows = [
        {"index": i + 1, "purpose": f"primary_route_{i + 1}", "status": "succeeded",
         "accounted_cost_usd": 0.03, "attempts": [], "diagnostics": [],
         "adaptive_actions": [], "failure_kinds": [], "target_items": 25}
        for i in range(planned)
    ] + [
        {"index": planned + 1, "purpose": "primary_route_1_slice_2026-04-19",
         "status": "succeeded", "accounted_cost_usd": 0.03, "attempts": [],
         "diagnostics": [], "adaptive_actions": [], "failure_kinds": [],
         "target_items": 25, "slice_of": "primary_route_1"},
    ]
    store.write(tmp_path / "status.json", {
        "run_id": "T",
        "sources": {"news": {"status": "running", "subruns": prior_rows,
                             "cost_usd": 0.03 * len(prior_rows), "collected": 10}},
    })
    store.write(tmp_path / "raw-news.json", [
        {"id": f"p{i}", "text": "ωραίο μέρος", "date": "2026-07-01",
         "url": f"https://e/{i}"} for i in range(10)
    ])

    calls = {"n": 0}

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            calls["n"] += 1
            return ({"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, [])

    monkeypatch.setattr(collector, "ApifyRunner", Runner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)

    collector.execute_plan(plan, "T", tmp_path)

    assert calls["n"] == 0, (
        f"every planned route was already paid for; {calls['n']} were bought again")


def test_an_unfinished_prefix_resumes_at_the_first_pending_route(tmp_path, monkeypatch):
    """Resume is still a prefix: routes after the first unfinished one re-run."""
    plan, store = _plan_and_store(tmp_path)
    planned = len((plan["sources"][0].get("subruns") or []))

    prior_rows = [
        {"index": 1, "purpose": "primary_route_1", "status": "succeeded",
         "accounted_cost_usd": 0.03, "attempts": [], "diagnostics": [],
         "adaptive_actions": [], "failure_kinds": [], "target_items": 25},
    ] + [
        {"index": i + 2, "purpose": f"primary_route_{i + 2}", "status": "pending",
         "accounted_cost_usd": 0.0, "attempts": [], "diagnostics": [],
         "adaptive_actions": [], "failure_kinds": [], "target_items": 25}
        for i in range(planned)  # one more row than the plan has
    ]
    store.write(tmp_path / "status.json", {
        "run_id": "T",
        "sources": {"news": {"status": "running", "subruns": prior_rows,
                             "cost_usd": 0.03, "collected": 3}},
    })
    store.write(tmp_path / "raw-news.json", [
        {"id": "p1", "text": "ωραίο", "date": "2026-07-01", "url": "https://e/1"}
    ])

    calls = {"n": 0}

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            calls["n"] += 1
            return ({"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, [])

    monkeypatch.setattr(collector, "ApifyRunner", Runner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)

    collector.execute_plan(plan, "T", tmp_path)

    assert calls["n"] == planned - 1, (
        f"only the routes after the finished first one may be bought: {calls['n']}")
