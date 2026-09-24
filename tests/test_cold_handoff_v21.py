"""The two tests the review asked for before shipping.

Round one's handoff test reused the same RunManager, the same store and the
same temp workspace, so it exercised the resume DECISION and nothing else. The
two situations that actually cost money in production were untested:

  1. A cold handoff — new manager, wiped workspace, restore from the durable
     archive, fresh lease. This is what Vercel does between invocations.
  2. A cut in the middle of ONE source's comment layer — the operator's own
     pages harvested, open search never run. The next worker has to finish that
     source without re-paying for the pages.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import comment_source_is_complete
from app.services.run_manager import RunManager
from app.services.storage import RunStore

from tests.test_whole_pipeline_v19 import FakeApify, FakeModel, _wait_terminal
from tests.test_worker_handoff_v20 import cut_worker_after_call

THREE = ["facebook", "instagram", "tiktok"]


def _plan_for(sources: list[str]) -> dict:
    draft = AnalysisDraft(
        client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
        date_from=date(2026, 8, 23), date_to=date(2026, 9, 21),
        keywords=["κλήρωση", "τζακ ποτ"], sources=sources,
        sample_mode="perSource",
        per_source={s: 12 for s in sources},
        per_source_comments={s: 40 for s in sources},
        source_pages={s: [f"{s}.com/brandpage"] for s in sources},
        owned_share_pct=60, comments=True, max_budget_usd=5.0,
        smart_search=True, report_language="Ελληνικά",
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    for row in (plan.get("preflight_forecast") or {}).get("sources", []):
        if row.get("source") in set(sources):
            row.setdefault("comments", {})["status"] = "verified_available"
            row["comments"]["live_verified"] = True
    return plan


class _ArchivingCloud:
    """A stand-in mirror that behaves like Blob: status.json is written often
    and separately, the workspace archive only at checkpoints."""

    enabled = True

    def __init__(self, root: Path):
        self._lock = threading.RLock()
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.json_blobs: dict[str, dict] = {}
        self.archives: dict[str, Path] = {}

    # --- the JSON side (frequent, small) ---------------------------------
    def put_json(self, run_id, name, payload):
        self.json_blobs[f"{run_id}/{name}"] = payload

    def get_json(self, run_id, name):
        return self.json_blobs.get(f"{run_id}/{name}")

    def require(self, *args, **kwargs):
        return True

    # --- the archive side (rare, whole workspace) ------------------------
    def persist_run_archive(self, run_id, folder):
        # Written into a staging copy and swapped in, so a checkpoint is never
        # half-visible. Doing it in place made this double flaky, which is no
        # better than broken: a test that fails one run in three teaches nothing.
        with self._lock:
            target = self.root / f"{run_id}"
            staging = self.root / f".{run_id}.staging"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(folder, staging)
            shutil.rmtree(target, ignore_errors=True)
            staging.replace(target)
            self.archives[run_id] = target
        return "2026-09-22T17:00:00+00:00"

    def archive_exists(self, run_id):
        return run_id in self.archives

    def restore_run_archive(self, run_id, folder):
        src = self.archives.get(run_id)
        if not src or not src.exists():
            return False
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, folder, dirs_exist_ok=True)
        return True

    def list_runs(self):
        return list(self.archives)

    def delete_run(self, run_id):
        self.archives.pop(run_id, None)


class ColdHandoffTest(unittest.TestCase):
    """A genuinely new invocation: nothing in memory, nothing on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud_dir = Path(self.tmp.name) / "mirror"
        self.local_a = Path(self.tmp.name) / "machine-a"
        self.local_b = Path(self.tmp.name) / "machine-b"
        self.cloud = _ArchivingCloud(self.cloud_dir)
        # Every manager this test builds, so tearDown can stop them even when an
        # assertion fails half way. A leaked worker thread keeps writing into a
        # temp directory that has already been removed, and the damage lands on
        # whatever test runs next — which is how this suite got an intermittent
        # failure in a file that passes 7 times out of 7 on its own.
        self.managers: list[RunManager] = []
        FakeApify.calls = []
        self._ai, self._key = settings.signalyth_ai_enabled, settings.openai_api_key
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "test-key"

    def tearDown(self):
        settings.signalyth_ai_enabled, settings.openai_api_key = self._ai, self._key
        for manager in self.managers:
            try:
                manager.shutdown(wait=True)
            except Exception:
                pass
        self.tmp.cleanup()

    def _machine(self, root: Path) -> tuple[RunStore, RunManager]:
        """A fresh serverless machine sharing only the mirror."""
        store = RunStore()
        store.root = root
        store.cloud = self.cloud
        manager = RunManager(store=store, max_workers=1)
        self.managers.append(manager)
        return store, manager

    def test_a_new_machine_finishes_the_run_from_the_archive_alone(self):
        store_a, manager_a = self._machine(self.local_a)
        run_id = store_a.create(_plan_for(THREE))[0]

        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):

            new_worker = cut_worker_after_call(manager_a, 21)
            manager_a._worker(run_id)
            calls_a = len(FakeApify.calls)
            self.assertGreaterEqual(calls_a, 20, "machine A never reached the comment layer")
            self.assertTrue(self.cloud.archive_exists(run_id),
                            "machine A never saved a durable copy")
            new_worker()
            manager_a.shutdown(wait=True)

            # The machine is gone. Its disk goes with it.
            shutil.rmtree(self.local_a, ignore_errors=True)

            store_b, manager_b = self._machine(self.local_b)
            self.assertFalse((self.local_b / "runs" / run_id).exists(),
                             "machine B started with a workspace it should not have")

            manager_b._worker(run_id)
            done = _wait_terminal(store_b, run_id, timeout=180.0)
            manager_b.shutdown(wait=True)

        self.assertNotEqual(done.get("status"), "failed",
                            msg=f"fatal_error={done.get('fatal_error')}")
        self.assertGreater(len(FakeApify.calls), calls_a,
                           "machine B did no work; the cold handoff was not exercised")

        folder = store_b.folder_for(run_id)
        comments = sum(len(store_b.read(folder / f"normalized-comments-{s}.json", []) or [])
                       for s in THREE)
        self.assertTrue(comments, f"the cold handoff lost the comments: {done.get('comment_deepening')}")

        summary = store_b.read(folder / "intelligence" / "summary.json", None)
        self.assertIsInstance(summary, dict,
                              f"no report after a cold handoff; fatal={done.get('fatal_error')}")

    def test_the_generation_is_fed_by_the_pipeline_not_by_the_test(self):
        """The review's sharpest point: the detector was never connected.

        Nothing here sets a number by hand. If the pipeline does not advance the
        counter on its own, this fails.
        """
        store, manager = self._machine(self.local_a)
        run_id = store.create(_plan_for(["facebook"]))[0]

        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            manager.enqueue(run_id)
            _wait_terminal(store, run_id, timeout=180.0)
            manager.shutdown(wait=True)

        status = store.read_status(run_id)
        generation = int(status.get("workspace_generation") or 0)
        self.assertGreater(generation, 1,
                           "the pipeline never advanced the workspace generation")

        durability = status.get("durability") or {}
        self.assertEqual(int(durability.get("generation") or -1), generation,
                         f"the archive records a different stage than the run reached: {durability}")

        ok, why = store.durable_state_consistent(store.folder_for(run_id))
        self.assertTrue(ok, why)


class MidSourceCutTest(unittest.TestCase):
    """A cut inside ONE source: pages harvested, open search never run."""

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

    def test_the_second_worker_finishes_the_source_without_re_paying_the_pages(self):
        run_id = self.store.create(_plan_for(["facebook"]))[0]

        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):

            # One source under v30: six diversified searches, two semantic
            # probes, page discovery at call 9, FIRST comment call at 10.
            # Cutting right after it leaves the owned pass done and open unrun.
            new_worker = cut_worker_after_call(self.manager, 10)
            self.manager._worker(run_id)
            calls_a = list(FakeApify.calls)

            folder = self.store.folder_for(run_id)
            row = ((self.store.read_status(run_id).get("comment_deepening") or {})
                   .get("facebook") or {})
            owned_now = len(self.store.read(folder / "normalized-comments-facebook.json", []) or [])

            self.assertFalse(
                comment_source_is_complete(row),
                f"a half-harvested source was written down as finished: {row}",
            )

            new_worker()
            self.manager._worker(run_id)
            _wait_terminal(self.store, run_id, timeout=180.0)

        row_after = ((self.store.read_status(run_id).get("comment_deepening") or {})
                     .get("facebook") or {})
        self.assertTrue(comment_source_is_complete(row_after),
                        f"the source never finished: {row_after}")

        buckets = {str(b) for b in (row_after.get("buckets_done") or [])}
        self.assertEqual(buckets, {"owned", "open", "backfill"}, row_after)

        final = len(self.store.read(folder / "normalized-comments-facebook.json", []) or [])
        self.assertGreaterEqual(final, owned_now,
                                "the resumed source lost comments already paid for")

        # And the pages must not have been bought a second time.
        def _page_calls(calls):
            return [a for a, i in calls
                    if (i.get("directUrls") or i.get("profiles") or i.get("twitterHandles")
                        or (i.get("startUrls") and not (i.get("postUrls") or i.get("replyTweetIds"))))]

        self.assertLessEqual(
            len(_page_calls(FakeApify.calls[len(calls_a):])), len(_page_calls(calls_a)),
            "the second worker re-ran page discovery it had already paid for",
        )


if __name__ == "__main__":
    unittest.main()
