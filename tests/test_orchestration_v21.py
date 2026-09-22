"""Round two: the holes an external review found in round one's fixes.

Round one fixed "a cleaning file exists, therefore cleaning finished". The
review found the SAME mistake one level down, plus a fix that was never wired
to anything. Both are the kind that pass a test suite and fail in production,
so each one here is pinned by a test that would have caught it.

  1. `normalized-comments-{source}.json` exists ⇒ that source is "collected",
     even when only the first of its three buckets ever ran.
  2. `bump_workspace_generation()` had no call sites at all, so the archive
     divergence detector could never fire on a real run.
  3. `resume_at_analysis` only inspected sources PRESENT in comment_deepening,
     so a source that never started blocked nothing.
  4. A call stopped by the worker deadline came back as "partial" and the
     caller wrote the source down as collected.
  5. The lease treats "no token recorded" as ownership.
  6. The archive-divergence guard ran only on the jump-to-analysis path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.run_manager import RunManager, resume_at_analysis
from app.services.storage import RunStore


def _real_plan(sources: list[str], *, comments: bool = True) -> dict:
    """A plan straight from the planner, with the field names production uses."""
    from datetime import date

    from app.models import AnalysisDraft
    from app.services.query_planner import build_collection_plan

    draft = AnalysisDraft(
        client="Client", topic="Topic", market="Greece",
        date_from=date(2026, 9, 1), date_to=date(2026, 9, 22),
        sources=sources, sample_mode="perSource",
        per_source={s: 2 for s in sources},
        per_source_comments={s: 4 for s in sources},
        comments=comments, max_budget_usd=5.0,
    )
    return build_collection_plan(draft).model_dump(mode="json")


def _folder_with_cleaning(tmp_path: Path) -> Path:
    (tmp_path / "cleaning").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cleaning" / "semantic-candidates.json").write_text("[]", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# 1 + 4 — a source is not finished because a file for it exists
# ---------------------------------------------------------------------------
class TestASourceIsFinishedOnlyWhenEveryBucketIs:
    """Each source is harvested in three passes: the operator's own pages, then
    open search, then a backfill from the pages for whatever open search could
    not deliver. A worker cut off after the first pass leaves a file on disk —
    and the next worker used to read that file as "this source is done"."""

    def test_the_buckets_a_worker_finished_are_recorded(self):
        from app.services.relevance_expansion import comment_source_is_complete

        half_done = {"status": "collected", "collected": 24, "buckets_done": ["owned"]}
        assert comment_source_is_complete(half_done) is False, (
            "only the operator's pages were harvested; open search never ran"
        )

    def test_all_three_buckets_means_done(self):
        from app.services.relevance_expansion import comment_source_is_complete

        assert comment_source_is_complete(
            {"status": "collected", "collected": 61,
             "buckets_done": ["owned", "open", "backfill"]}) is True

    def test_an_old_row_without_the_field_is_not_assumed_finished(self):
        """Runs started before this change must not be read as complete."""
        from app.services.relevance_expansion import comment_source_is_complete

        assert comment_source_is_complete({"status": "collected", "collected": 24}) is False

    def test_a_source_that_legitimately_has_nothing_to_do_is_complete(self):
        from app.services.relevance_expansion import comment_source_is_complete

        assert comment_source_is_complete(
            {"status": "skipped", "reason": "no_comment_actor_configured"}) is True

    def test_a_deadline_stop_is_never_complete(self):
        from app.services.relevance_expansion import comment_source_is_complete

        assert comment_source_is_complete(
            {"status": "deferred", "collected": 24,
             "reason": "worker_deadline_reached_resumes_automatically",
             "buckets_done": ["owned"]}) is False


# ---------------------------------------------------------------------------
# 3 — a source that never started must block the analysis
# ---------------------------------------------------------------------------
class TestEverySelectedSourceMustReportBack:
    """The predicate used to iterate only over what the dict CONTAINED. A run
    that died before Instagram and TikTok ever wrote a row had nothing to
    iterate over, so nothing objected."""

    #: Built by the real planner, never hand-written: a literal dict let these
    #: tests pass against a field name (`comments`) that production does not use,
    #: which is exactly how the barrier came to be dead code.
    PLAN = _real_plan(["facebook", "instagram", "tiktok"])

    def test_two_sources_that_never_started_block_it(self, tmp_path):
        folder = _folder_with_cleaning(tmp_path)
        status = {
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            "comment_deepening": {
                "facebook": {"status": "collected", "collected": 24,
                             "buckets_done": ["owned", "open", "backfill"]},
            },
        }
        assert resume_at_analysis(status, folder, plan=self.PLAN) is False, (
            "instagram and tiktok never reported at all and were silently ignored"
        )

    def test_all_three_reporting_terminal_allows_it(self, tmp_path):
        folder = _folder_with_cleaning(tmp_path)
        done = {"status": "collected", "collected": 20,
                "buckets_done": ["owned", "open", "backfill"]}
        status = {
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            "comment_deepening": {"facebook": done, "instagram": done,
                                  "tiktok": {"status": "skipped",
                                             "reason": "no_comment_actor_configured"}},
        }
        assert resume_at_analysis(status, folder, plan=self.PLAN) is True

    def test_a_run_that_asked_for_no_comments_is_not_held_up(self, tmp_path):
        folder = _folder_with_cleaning(tmp_path)
        status = {"status": "running",
                  "cleaning": {"status": "succeeded"},
                  "adaptive_collection": {"status": "succeeded"}}
        plan = _real_plan(["facebook"], comments=False)
        assert resume_at_analysis(status, folder, plan=plan) is True

    def test_a_source_that_cannot_do_comments_is_not_waited_for(self, tmp_path):
        """YouTube has no comment Actor; waiting for it would hang every run."""
        folder = _folder_with_cleaning(tmp_path)
        status = {
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            "comment_deepening": {"facebook": {"status": "collected", "collected": 5,
                                               "buckets_done": ["owned", "open", "backfill"]}},
        }
        plan = _real_plan(["facebook", "youtube"])
        assert resume_at_analysis(status, folder, plan=plan) is True

    def test_without_a_plan_it_stays_conservative(self, tmp_path):
        """Callers that cannot supply the plan must not get a laxer answer."""
        folder = _folder_with_cleaning(tmp_path)
        status = {
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            "comment_deepening": {"facebook": {"status": "collected", "collected": 24,
                                               "buckets_done": ["owned"]}},
        }
        assert resume_at_analysis(status, folder) is False


# ---------------------------------------------------------------------------
# 2 — the generation counter has to be fed by the pipeline, not by a test
# ---------------------------------------------------------------------------
class TestTheDivergenceDetectorIsActuallyConnected:
    """The round-one tests set `workspace_generation` and `durability.generation`
    by hand. That proved the comparison worked. It did not prove that anything
    in the running system ever writes those numbers — and nothing did."""

    def test_the_counter_has_call_sites_in_the_pipeline(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "bump_workspace_generation", "app/"],
            capture_output=True, text=True).stdout
        callers = [line for line in out.splitlines()
                   if "def bump_workspace_generation" not in line]
        assert callers, (
            "nothing in app/ calls bump_workspace_generation, so workspace_generation "
            "stays 0 for ever and the detector can never fire"
        )

    def test_a_milestone_moves_the_counter(self, tmp_path):
        store = RunStore()
        store.write(tmp_path / "status.json", {"run_id": "R", "status": "running"})
        first = store.bump_workspace_generation(tmp_path)
        second = store.bump_workspace_generation(tmp_path)
        assert (first, second) == (1, 2)

    def test_a_checkpoint_that_never_ran_leaves_the_status_ahead(self, tmp_path):
        """The real shape of the bug: milestones advanced, the archive did not."""
        store = RunStore()
        store.write(tmp_path / "status.json", {"run_id": "R", "status": "running"})
        store.bump_workspace_generation(tmp_path)
        store.bump_workspace_generation(tmp_path)
        # A checkpoint recorded at generation 1, two milestones ago.
        status = store.read(tmp_path / "status.json", {})
        status["durability"] = {"saved": True, "generation": 1}
        store.write(tmp_path / "status.json", status)

        ok, reason = store.durable_state_consistent(tmp_path)
        assert ok is False, reason


# ---------------------------------------------------------------------------
# 5 — "no lease recorded" is not ownership
# ---------------------------------------------------------------------------
class TestTheLeaseDoesNotHandOutOwnershipByDefault:
    def test_a_missing_lease_is_not_ownership_for_a_worker_that_claimed_one(
            self, monkeypatch):
        """If this worker holds a token, an EMPTY lease means its claim was
        lost or overwritten — not that it may carry on writing."""
        manager = RunManager()
        monkeypatch.setattr(manager.store, "read_status", lambda run_id: {})
        assert manager._owns_lease("R", "worker-A") is False

    def test_a_worker_that_never_claimed_is_unaffected(self, monkeypatch):
        manager = RunManager()
        monkeypatch.setattr(manager.store, "read_status", lambda run_id: {})
        assert manager._owns_lease("R", "") is True

    def test_a_displaced_worker_does_not_overwrite_the_archive(self, monkeypatch):
        """Fencing, since there is no compare-and-swap to be had.

        A worker can lose the run while it is inside an Actor call and come back
        minutes later holding an older workspace. It must not save that over the
        copy the current worker is building.
        """
        manager = RunManager()
        saved: list[str] = []
        monkeypatch.setattr(manager.store, "checkpoint_run", lambda run_id: saved.append(run_id))
        monkeypatch.setattr(manager.store, "bump_workspace_generation", lambda folder: 1)
        monkeypatch.setattr(manager.store, "folder_for", lambda run_id: Path("/tmp"))

        manager._lease_tokens["R"] = "worker-A"
        monkeypatch.setattr(manager.store, "read_status",
                            lambda run_id: {"worker_lease": {"token": "worker-B"}})
        manager._checkpoint_milestone("R")
        assert saved == [], "a displaced worker wrote its stale workspace to the archive"

        monkeypatch.setattr(manager.store, "read_status",
                            lambda run_id: {"worker_lease": {"token": "worker-A"}})
        manager._checkpoint_milestone("R")
        assert saved == ["R"], "the worker that still owns the run could not save"

    def test_a_failed_claim_does_not_hand_back_a_usable_token(self, monkeypatch):
        """`_claim_lease` swallowed the write failure and returned a token
        anyway, so the worker believed it owned a run it had never claimed."""
        manager = RunManager()

        def _boom(run_id, status):
            raise OSError("blob unreachable")

        monkeypatch.setattr(manager.store, "read_status", lambda run_id: {})
        monkeypatch.setattr(manager.store, "write_status", _boom)
        assert manager._claim_lease("R") == ""


# ---------------------------------------------------------------------------
# 6 — the divergence guard belongs on every resume, not just one branch
# ---------------------------------------------------------------------------
class TestTheGuardCoversACollectionResumeToo:
    def test_a_worker_resuming_into_collection_is_checked(self, tmp_path, monkeypatch):
        """A stale archive is just as dangerous when the run resumes into
        collection: the worker re-collects onto evidence it cannot see."""
        from app.services import run_manager as rm

        manager = RunManager()
        checked: list[Path] = []
        monkeypatch.setattr(manager.store, "durable_state_consistent",
                            lambda folder: (checked.append(folder), (True, "ok"))[1])
        monkeypatch.setattr(manager.store, "folder_for", lambda run_id: tmp_path)
        monkeypatch.setattr(manager.store, "prune_local_scratch", lambda **kw: None)
        monkeypatch.setattr(manager.store, "cancel_requested_folder", lambda folder: True)
        monkeypatch.setattr(manager.store, "read_status", lambda run_id: {"status": "running"})
        monkeypatch.setattr(manager, "_claim_lease", lambda run_id: "t")
        monkeypatch.setattr(manager, "_mark_cancelled_before_start", lambda run_id: None)

        manager._worker("R")
        assert checked, "the consistency guard never ran on this resume path"
