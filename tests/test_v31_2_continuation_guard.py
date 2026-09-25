"""A step that never progresses must not be resumed forever.

Run 20260925T002306Z-65236947 (production, 2026-09-25) sat in "running" for
eight hours: the X comment pass hit the worker deadline, the run was requeued,
a fresh worker resumed at exactly the same durable state, hit the deadline
again, and so on every ~25 minutes. Nothing was spent, nothing finished, and
nothing in the code bounded the loop.

Contract: after MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS consecutive
continuations whose durable fingerprint did not change, the adaptive step is
closed honestly (each unfinished comment pass becomes a shortfall with an
explicit reason) and the run proceeds to analysis on the evidence it has.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.run_manager import (
    MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS,
    RunManager,
    resume_at_analysis,
    update_adaptive_continuation_guard,
)
from app.services.storage import RunStore

from tests.test_whole_pipeline_v19 import FakeApify, FakeModel, _wait_terminal


class TestTheGuardItself(unittest.TestCase):
    def test_identical_states_count_up_and_trip_at_the_limit(self):
        status: dict = {}
        for i in range(1, MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS + 1):
            guard = update_adaptive_continuation_guard(status, "same")
            self.assertEqual(guard["count"], i)
        self.assertTrue(guard.get("tripped"))
        self.assertEqual(guard["reason"], "no_progress_after_repeated_continuations")

    def test_any_progress_resets_the_count(self):
        status: dict = {}
        update_adaptive_continuation_guard(status, "a")
        update_adaptive_continuation_guard(status, "a")
        guard = update_adaptive_continuation_guard(status, "b")   # something changed
        self.assertEqual(guard["count"], 1)
        self.assertFalse(guard.get("tripped"))


class StuckAdaptiveStepTest(unittest.TestCase):
    """The whole loop, driven by the real RunManager and a step that never finishes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.manager = RunManager(store=self.store, max_workers=1)
        FakeApify.calls = []
        self._ai, self._key = settings.signalyth_ai_enabled, settings.openai_api_key
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "test-key"

    def tearDown(self):
        settings.signalyth_ai_enabled, settings.openai_api_key = self._ai, self._key
        self.manager.shutdown(wait=True)
        self.tmp.cleanup()

    def _plan(self):
        draft = AnalysisDraft(
            client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
            date_from=date(2026, 8, 23), date_to=date(2026, 9, 21),
            keywords=["κλήρωση"], sources=["facebook"], sample_mode="perSource",
            per_source={"facebook": 12}, per_source_comments={"facebook": 40},
            comments=True, max_budget_usd=5.0, smart_search=True, report_language="Ελληνικά",
        )
        plan = build_collection_plan(draft).model_dump(mode="json")
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") == "facebook":
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
        return plan

    def test_a_step_that_never_progresses_is_closed_after_the_limit(self):
        run_id = self.store.create(self._plan())[0]
        calls = []

        def stuck_adaptive(folder, *, plan, initial_report, **kwargs):
            # Behaves like the live X pass: writes "running", never finishes,
            # reports the worker deadline every single time.
            calls.append(1)
            status = self.store.read_status(run_id)
            status.setdefault("comment_deepening", {})["facebook"] = {
                "status": "running", "bucket": "open", "buckets_done": ["owned"],
                "requested": 40, "collected": 0, "parents": 2,
            }
            self.store.write_status(run_id, status)
            return {"report": initial_report, "audit": {"deadline_reached": True, "warnings": []}}

        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel), \
             patch("app.services.run_manager.adaptive_expand_after_cleaning", stuck_adaptive):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id, timeout=240.0)

        self.assertNotIn(done.get("status"), {"running", "queued"},
                         f"the run is still looping: {done.get('current')}")
        self.assertNotEqual(done.get("status"), "failed", msg=f"fatal_error={done.get('fatal_error')}")
        self.assertEqual(len(calls), MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS,
                         f"the stuck step was resumed {len(calls)} times")

        guard = done.get("adaptive_continuation_guard") or {}
        self.assertTrue(guard.get("tripped"), guard)
        self.assertEqual((done.get("adaptive_collection") or {}).get("status"), "completed_no_progress")

        row = (done.get("comment_deepening") or {}).get("facebook") or {}
        self.assertEqual(row.get("status"), "shortfall", row)
        self.assertEqual(row.get("reason"), "no_progress_after_repeated_continuations")
        self.assertEqual(row.get("shortfall"), 40)

        # And a fresh worker would now go straight to analysis instead of resuming.
        self.assertTrue(resume_at_analysis(done, self.store.folder_for(run_id), self.store.read_plan(run_id)))

        # The report was still produced from the evidence that existed.
        folder = self.store.folder_for(run_id)
        self.assertIsInstance(self.store.read(folder / "intelligence" / "summary.json", None), dict)


if __name__ == "__main__":
    unittest.main()
