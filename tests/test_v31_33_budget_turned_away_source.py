"""v31.33 — a source the budget guard turns away must not crash the worker.

Run 20261019T155108Z: Instagram spent $0.68 of a $2 envelope, and when a
resumed worker re-entered Facebook there was no safe share left for it. That
path sets `source_level_error` and skips the collecting branch entirely — but
the terminal block below reads `interrupted_time_budget`, which was only ever
assigned INSIDE that branch:

    UnboundLocalError: cannot access local variable 'interrupted_time_budget'

Facebook's 154 already-collected posts survived, because v31.27 marks a
crashed source worker terminal with its error instead of leaving it silent;
without that the run would have reported a plain shortfall and nobody would
have known a source died. The flag is now initialised before the branch.
"""
from __future__ import annotations

from datetime import date

import app.services.collector as collector
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.storage import RunStore


def test_a_source_with_no_budget_share_fails_cleanly(tmp_path, monkeypatch):
    draft = AnalysisDraft(
        client="ΚΠΙΣΝ", topic="ΚΠΙΣΝ", market="Greece",
        date_from=date(2026, 4, 19), date_to=date(2026, 10, 19),
        keywords=["SNFCC"], keyword_roles={"SNFCC": "alias"},
        sources=["news", "x"], sample_mode="perSource",
        per_source={"news": 100, "x": 100},
        comments=False, max_budget_usd=0.05,
        smart_search=True, report_language="Ελληνικά", place_subject=True,
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "status.json", {"run_id": "T"})

    called = {"n": 0}

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            called["n"] += 1
            return ({"usageTotalUsd": 0.0, "defaultDatasetId": "d"}, [])

    monkeypatch.setattr(collector, "ApifyRunner", Runner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)
    # The live shape: an earlier source has already spent the envelope, so this
    # one's planned floor is far above the share the guard can still allow.
    monkeypatch.setattr(collector, "_source_base_cap", lambda sp: 10.0)

    status = collector.execute_plan(plan, "T", tmp_path)

    row = status["sources"]["news"]
    assert row["status"] in collector.SOURCE_TERMINAL
    assert "UnboundLocalError" not in str(row.get("error") or ""), row.get("error")
    assert "worker stopped unexpectedly" not in str(row.get("error") or ""), (
        f"the guard must decline the source, not crash on it: {row.get('error')}")
    assert "budget" in str(row.get("error") or "").casefold(), row.get("error")
    assert float(row.get("cost_usd") or 0) == 0.0, (
        "a source the guard turned away must not be paid for at all")
    assert called["n"] >= 0
