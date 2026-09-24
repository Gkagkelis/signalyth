"""The handoff: a worker dies mid-collection and another finishes the run.

This is the failure that kept reaching production. Every other test drives the
pipeline inside ONE process, which is exactly the condition under which the bug
cannot appear. Here the first worker is cut off in the middle of the comment
layer — the way Vercel cuts one off — and a second worker picks the run up from
the durable state, as the queue makes it.

What must be true afterwards:
  * the second worker does NOT skip to the AI because a cleaning file exists,
  * no already-collected source is paid for twice,
  * the finished run holds the comments, not four of them.

Reproduced from run 20260922T160743Z-b01eced8, which reported a finished
analysis over 4 records while three collecting steps were still marked running.
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
from app.services.run_manager import RunManager, resume_at_analysis
from app.services.storage import RunStore

from tests.test_whole_pipeline_v19 import FakeApify, FakeModel, _wait_terminal

THREE = ["facebook", "instagram", "tiktok"]

#: Where to cut worker A off. A full three-source run makes 16 Actor calls and
#: the comment layer starts at call 7, so cutting after 8 leaves the run exactly
#: where the live one died: Facebook comments half-collected, Instagram and
#: TikTok comments not started, a cleaning file already on disk.
CUT_AFTER_CALL = 8


def cut_worker_after_call(manager, limit: int):
    """Kill the worker's clock once `limit` Actor calls have been made.

    Wall-clock deadlines make this test a coin toss — a short one stops the
    worker before it has collected anything, which passes while proving nothing.
    Counting paid calls cuts the run at the same point every time.
    """
    original = (manager._deadline_reached, manager._seconds_left,
                manager._requeue_continuation)

    def out_of_time() -> bool:
        return len(FakeApify.calls) >= limit

    manager._deadline_reached = lambda margin_seconds=0.0: out_of_time()
    manager._seconds_left = lambda margin_seconds=0.0: (-1.0 if out_of_time() else float("inf"))
    # The cut worker would auto-queue a continuation into the executor, which
    # then runs IN PARALLEL with the test's own explicit "worker B". Two
    # writers on one run made these tests fail two times in four. In these
    # tests the handoff is driven by hand, so the automatic one is silenced.
    manager._requeue_continuation = lambda run_id: None

    def revive_a_fresh_worker() -> None:
        """Worker B is a NEW invocation: it starts with its own full clock."""
        (manager._deadline_reached, manager._seconds_left,
         manager._requeue_continuation) = original
        manager.set_invocation_deadline(None)

    return revive_a_fresh_worker


class SlowTikTok(FakeApify):
    """Every TikTok comment call times out, silently, the way the live one did.

    Nine minutes, zero rows, $0.00 charged. The other two sources behave.
    """

    tiktok_comment_calls = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        is_comment_call = bool(run_input.get("postUrls") or run_input.get("replyTweetIds")
                               or run_input.get("mode") == "replies")
        if "tiktok" in actor_id and is_comment_call:
            type(self).tiktok_comment_calls += 1
            raise RuntimeError("Actor run did not finish — timed out")
        return super().run(actor_id, run_input, max_items=max_items, max_charge_usd=max_charge_usd)


class WorkerHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.manager = RunManager(store=self.store, max_workers=1)
        FakeApify.calls = []
        SlowTikTok.tiktok_comment_calls = 0
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
            keywords=["κλήρωση", "τζακ ποτ"], sources=THREE,
            sample_mode="perSource",
            per_source={s: 12 for s in THREE},
            per_source_comments={"facebook": 40, "instagram": 20, "tiktok": 20},
            source_pages={s: [f"{s}.com/brandpage"] for s in THREE},
            owned_share_pct=60, comments=True, max_budget_usd=5.0,
            smart_search=True, report_language="Ελληνικά",
        )
        plan = build_collection_plan(draft).model_dump(mode="json")
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") in set(THREE):
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
        return plan

    # ------------------------------------------------------------------
    def test_a_second_worker_does_not_finish_a_run_that_is_still_collecting(self):
        """The 4-record report, reproduced at its decision point.

        The run is put into exactly the state the live one was in — a cleaning
        file on disk, collecting steps still marked running — and a fresh worker
        must refuse to treat that as a collected run.
        """
        run_id = self.store.create(self._plan())[0]
        folder = self.store.folder_for(run_id)
        (folder / "cleaning").mkdir(parents=True, exist_ok=True)
        (folder / "cleaning" / "semantic-candidates.json").write_text(
            '[{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]', encoding="utf-8")

        live = {
            "status": "running",
            "cleaning": {"status": "running"},
            "adaptive_collection": {"status": "running"},
            "comment_deepening": {"tiktok": "running"},
        }
        self.assertFalse(
            resume_at_analysis(live, folder),
            "a fresh worker would have analysed 4 records and called the run finished",
        )

    def test_the_run_completes_across_a_real_handoff(self):
        """First worker cut off mid-comment-layer; second finishes the job."""
        run_id = self.store.create(self._plan())[0]

        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):

            # --- Worker A: cut off inside the comment layer.
            new_worker = cut_worker_after_call(self.manager, CUT_AFTER_CALL)
            self.manager._worker(run_id)
            mid = self.store.read_status(run_id)
            calls_a = len(FakeApify.calls)

            # The cut has to land where it is supposed to, or this test proves
            # nothing: worker A must have REACHED the comment layer.
            self.assertGreaterEqual(calls_a, 7,
                                    f"worker A stopped before the comment layer ({calls_a} calls)")
            self.assertTrue(
                (self.store.folder_for(run_id) / "cleaning" / "semantic-candidates.json").exists(),
                "the cleaning file that used to mislead the next worker is not even there",
            )
            # And it must not have declared success on partial evidence.
            self.assertNotIn(mid.get("status"), {"succeeded", "completed_shortfall"},
                             f"worker A finished a run it had not collected: {mid.get('phase')}")

            # --- Worker B: a fresh invocation with a full budget.
            new_worker()
            self.manager._worker(run_id)
            done = _wait_terminal(self.store, run_id, timeout=150.0)
            self.assertGreater(len(FakeApify.calls), calls_a,
                               "worker B did no work at all; the handoff was not exercised")

        folder = self.store.folder_for(run_id)
        self.assertNotEqual(done.get("status"), "failed",
                            msg=f"fatal_error={done.get('fatal_error')}")

        comments = []
        for source in THREE:
            comments += self.store.read(folder / f"normalized-comments-{source}.json", []) or []
        self.assertTrue(comments, f"the handoff lost the comment layer: {done.get('comment_deepening')}")

        analysed = self.store.read(folder / "analysis" / "analyzed.json", []) or []
        self.assertGreater(
            len(analysed), 4,
            f"the run finished on {len(analysed)} records — this is the live failure",
        )

        summary = self.store.read(folder / "intelligence" / "summary.json", None)
        self.assertIsInstance(summary, dict,
                              f"Step 5 produced nothing; fatal={done.get('fatal_error')}")

    def test_a_source_already_collected_is_never_paid_for_twice(self):
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            new_worker = cut_worker_after_call(self.manager, CUT_AFTER_CALL)
            self.manager._worker(run_id)
            after_a = [c for c in FakeApify.calls]
            self.assertGreaterEqual(len(after_a), 7, "worker A never got far enough to matter")

            new_worker()
            self.manager._worker(run_id)
            _wait_terminal(self.store, run_id, timeout=150.0)

        # A search call is the expensive one. The same source must not be
        # searched again by the second worker on evidence already on disk.
        def _search_sources(calls):
            out = []
            for actor_id, inp in calls:
                if not (inp.get("postUrls") or inp.get("replyTweetIds")
                        or inp.get("mode") == "replies"
                        or inp.get("directUrls") or inp.get("profiles")
                        or inp.get("twitterHandles")):
                    out.append(actor_id)
            return out

        repeated = _search_sources(FakeApify.calls[len(after_a):])
        already = set(_search_sources(after_a))
        self.assertFalse(
            [a for a in repeated if a in already],
            f"the second worker re-ran a search already paid for: {repeated}",
        )

    def test_a_source_whose_actor_hangs_does_not_take_the_run_down(self):
        """TikTok returning nothing must cost the run TikTok, not the report."""
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", SlowTikTok), \
             patch("app.services.relevance_expansion.ApifyRunner", SlowTikTok), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id, timeout=180.0)

        folder = self.store.folder_for(run_id)
        self.assertNotEqual(done.get("status"), "failed",
                            msg=f"fatal_error={done.get('fatal_error')}")

        others = []
        for source in ("facebook", "instagram"):
            others += self.store.read(folder / f"normalized-comments-{source}.json", []) or []
        self.assertTrue(others, "the working sources lost their comments too")

        summary = self.store.read(folder / "intelligence" / "summary.json", None)
        self.assertIsInstance(summary, dict,
                              f"one dead Actor killed the report; fatal={done.get('fatal_error')}")


if __name__ == "__main__":
    unittest.main()
