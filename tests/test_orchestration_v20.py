"""The execution layer, not the pipeline.

Every one of the 668 existing tests drives the pipeline inside ONE continuous
process. That proves the business logic. It cannot prove the thing that has
actually been failing in production, which is what happens when the run is
carried by several short-lived workers that hand off to each other.

Four defects were found in that layer. Each class below reproduces one of them
against the real code. They are written to FAIL on the code as it stands, so
that passing means something.

Live evidence they come from — run 20260922T160743Z-b01eced8:

    cleaning:                  running
    comment_deepening.tiktok:  running   (3 parents, 60 requested)
    adaptive_collection:       running
    analysis.input_records:    4         <-- analysis ran anyway

"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.resilience import run_actor_resilient


# ---------------------------------------------------------------------------
# Defect 1 — an intermediate file was read as "the step finished"
# ---------------------------------------------------------------------------
class TestAPartialFileIsNotAFinishedStep:
    """`clean_run()` is called again after EVERY comment Actor call, so
    `cleaning/semantic-candidates.json` exists long before the comment layer
    is done. The resume path used its existence to decide it could skip
    straight to the AI. That is how analysis ran on 4 records."""

    def _folder_with_partial_cleaning(self, tmp_path: Path) -> Path:
        (tmp_path / "cleaning").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cleaning" / "semantic-candidates.json").write_text("[]", encoding="utf-8")
        return tmp_path

    def test_the_exact_live_status_must_not_resume_into_analysis(self, tmp_path):
        from app.services.run_manager import resume_at_analysis

        folder = self._folder_with_partial_cleaning(tmp_path)
        live_status = {
            "status": "running",
            "cleaning": {"status": "running"},
            "adaptive_collection": {"status": "running"},
            "comment_deepening": {"tiktok": "running"},
        }
        assert resume_at_analysis(live_status, folder) is False, (
            "a second worker must not jump to analysis while collection is in flight"
        )

    def test_cleaning_still_running_is_not_enough(self, tmp_path):
        from app.services.run_manager import resume_at_analysis

        folder = self._folder_with_partial_cleaning(tmp_path)
        assert resume_at_analysis(
            {"status": "running", "cleaning": {"status": "running"}}, folder) is False

    def test_the_comment_layer_still_running_blocks_it_too(self, tmp_path):
        """Cleaning can report success between two comment Actor calls."""
        from app.services.run_manager import resume_at_analysis

        folder = self._folder_with_partial_cleaning(tmp_path)
        assert resume_at_analysis({
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "running"},
        }, folder) is False

    def test_a_deferred_comment_source_blocks_it(self, tmp_path):
        """Deferred means "resumes automatically" — there is more to collect."""
        from app.services.run_manager import resume_at_analysis

        folder = self._folder_with_partial_cleaning(tmp_path)
        assert resume_at_analysis({
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            "comment_deepening": {"tiktok": "deferred"},
        }, folder) is False

    def test_a_genuinely_finished_run_still_resumes_without_re_paying(self, tmp_path):
        """The guard must not push the run back into paid collection."""
        from app.services.run_manager import resume_at_analysis

        folder = self._folder_with_partial_cleaning(tmp_path)
        assert resume_at_analysis({
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
            # Every pass of every source reported done — the only shape that
            # counts as finished since bucket tracking was added.
            "comment_deepening": {
                "tiktok": {"status": "collected", "collected": 20,
                           "buckets_done": ["owned", "open", "backfill"]},
                "facebook": {"status": "collected", "collected": 40,
                             "buckets_done": ["owned", "open", "backfill"]},
            },
        }, folder) is True

    def test_no_cleaning_output_at_all_means_start_from_collection(self, tmp_path):
        from app.services.run_manager import resume_at_analysis

        assert resume_at_analysis({
            "status": "running",
            "cleaning": {"status": "succeeded"},
            "adaptive_collection": {"status": "succeeded"},
        }, tmp_path) is False


# ---------------------------------------------------------------------------
# Defect 2 — the soft deadline is invisible inside one logical Actor call
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds: float):
        self.now += seconds


class SlowFlakyRunner:
    """Each call burns the Actor's full 3-minute timeout and fails retryably.

    This is the TikTok comment Actor as observed live: minutes of silence,
    zero rows, $0.00 charged.
    """

    def __init__(self, clock: FakeClock, seconds_per_call: float = 180.0):
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.calls = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls += 1
        self.clock.advance(self.seconds_per_call)
        raise RuntimeError("Actor run timed out — temporarily unavailable")


class TestOneActorCallCannotOutliveTheWorker:
    """`max_calls=4` on the comment path at a 3-minute Actor timeout is a
    12-minute logical operation. `run_actor_resilient` never consulted the
    deadline, so entering it with 2 minutes of budget left overran the wall
    by ten minutes — which is what let the stale-worker recovery fire."""

    def test_it_stops_at_the_deadline_instead_of_burning_every_retry(self):
        clock = FakeClock()
        runner = SlowFlakyRunner(clock)
        budget_seconds = 200.0

        result = run_actor_resilient(
            runner, "actor/comments", {"postURLs": ["a"]},
            max_items=60, max_charge_usd=1.0, rate_per_1000=2.0, max_calls=4,
            # 200 seconds left, and one Actor call takes 180. The first fits;
            # after it only 20 seconds remain, so a second must not be started.
            time_left=lambda: budget_seconds - clock.now,
            sleep_fn=lambda _s: None,
        )

        assert runner.calls == 1, (
            f"only one 180s call fits in {budget_seconds:.0f}s; "
            f"{runner.calls} calls were made"
        )
        assert clock.now <= budget_seconds, (
            f"overran the worker wall by {clock.now - budget_seconds:.0f}s"
        )
        assert "worker_deadline_reached" in result.failure_kinds, result.failure_kinds

    def test_a_call_that_cannot_finish_is_never_started(self):
        """The live setting was a 110-second margin against a 180-second Actor."""
        clock = FakeClock()
        runner = SlowFlakyRunner(clock)

        run_actor_resilient(
            runner, "actor/comments", {"postURLs": ["a"]},
            max_items=60, max_charge_usd=1.0, rate_per_1000=2.0, max_calls=4,
            time_left=lambda: 110.0 - clock.now,
            sleep_fn=lambda _s: None,
        )
        assert runner.calls == 0, "a 180s call was started with 110s left"

    def test_partial_rows_already_paid_for_are_kept(self):
        """Stopping early must never throw away evidence already charged."""
        clock = FakeClock()

        class PartialThenSlow:
            def __init__(self):
                self.calls = 0

            def run(self, actor_id, run_input, *, max_items, max_charge_usd):
                self.calls += 1
                clock.advance(180.0)
                if self.calls == 1:
                    return ({"usageTotalUsd": 0.01},
                            [{"id": f"c{i}", "text": f"σχόλιο {i}"} for i in range(5)])
                raise RuntimeError("temporarily unavailable")

        runner = PartialThenSlow()
        result = run_actor_resilient(
            runner, "actor/comments", {"postURLs": ["a"]},
            max_items=60, max_charge_usd=1.0, rate_per_1000=2.0, max_calls=4,
            time_left=lambda: 200.0 - clock.now,
            sleep_fn=lambda _s: None,
        )
        assert len(result.items) == 5, "rows already paid for were discarded"

    def test_without_a_deadline_nothing_changes_for_existing_callers(self):
        clock = FakeClock()
        runner = SlowFlakyRunner(clock)
        run_actor_resilient(
            runner, "actor/x", {"q": "a"},
            max_items=10, max_charge_usd=1.0, rate_per_1000=2.0, max_calls=3,
            sleep_fn=lambda _s: None,
        )
        assert runner.calls > 1, "with no deadline the retry behaviour must be untouched"


class TestTheRunKeepsSayingItIsAlive:
    """Staleness is judged from `status.updated_at`. A worker inside a long
    Actor call writes nothing, so after ~11 minutes of TikTok silence the UI
    declared it dead and started a second one. A live worker must keep a
    pulse from INSIDE the call, not only between sources."""

    def test_every_attempt_reports_a_pulse(self):
        clock = FakeClock()
        runner = SlowFlakyRunner(clock)
        beats: list[str] = []

        run_actor_resilient(
            runner, "actor/comments", {"postURLs": ["a"]},
            max_items=60, max_charge_usd=1.0, rate_per_1000=2.0, max_calls=3,
            heartbeat=lambda note: beats.append(note),
            sleep_fn=lambda _s: None,
        )

        assert len(beats) >= runner.calls, (
            f"{runner.calls} Actor calls produced only {len(beats)} signs of life"
        )


# ---------------------------------------------------------------------------
# Defect 3 — the lease is a note, not a lock
# ---------------------------------------------------------------------------
class TestADisplacedWorkerStandsDown:
    """`_claim_lease` is read-then-write with no compare-and-swap, and
    `_owns_lease` returns True when the status cannot be read. Fail-open on
    a lock means two workers writing the same run."""

    def _manager(self, tmp_path, monkeypatch):
        from app.services.run_manager import RunManager
        manager = RunManager()
        return manager

    def test_a_different_token_means_stand_down(self, tmp_path, monkeypatch):
        from app.services.run_manager import RunManager

        manager = RunManager()
        monkeypatch.setattr(manager.store, "read_status",
                            lambda run_id: {"worker_lease": {"token": "worker-B"}})
        assert manager._owns_lease("R", "worker-A") is False

    def test_an_unreadable_lease_is_not_treated_as_ownership(self, tmp_path, monkeypatch):
        """Fail-open here is what allows a second writer. A worker that
        cannot confirm it still owns the run must not keep writing."""
        from app.services.run_manager import RunManager

        def _boom(run_id):
            raise OSError("blob unreachable")

        manager = RunManager()
        monkeypatch.setattr(manager.store, "read_status", _boom)
        assert manager._owns_lease("R", "worker-A") is False

    def test_claiming_records_the_previous_holder(self, tmp_path, monkeypatch):
        """Without this there is no way to tell, afterwards, that two workers
        were on the same run."""
        from app.services.run_manager import RunManager

        written = {}
        manager = RunManager()
        monkeypatch.setattr(manager.store, "read_status",
                            lambda run_id: {"worker_lease": {"token": "worker-A",
                                                             "generation": 1,
                                                             "claimed_at": "2026-09-22T16:11:00+00:00"}})
        monkeypatch.setattr(manager.store, "write_status",
                            lambda run_id, status: written.update(status))

        manager._claim_lease("R")
        lease = written.get("worker_lease") or {}
        assert lease.get("displaced_token") == "worker-A", lease
        assert lease.get("generation") == 2, lease


# ---------------------------------------------------------------------------
# Defect 4 — the status and the archive are two separate saves
# ---------------------------------------------------------------------------
class TestTheStatusNeverDescribesFilesTheArchiveDoesNotHave:
    """`status.json` is written on every step. `archive.zip` is written only at
    checkpoints, and several checkpoint calls are wrapped in `except: pass`.
    So a fresh worker can restore an archive from stage N-1 while reading a
    status that describes stage N — and then fail at aggregation looking for
    files that were never in the archive it has."""

    def test_a_status_ahead_of_the_archive_is_refused_not_guessed(self, tmp_path):
        from app.services.storage import RunStore

        store = RunStore()
        (tmp_path / "cleaning").mkdir(parents=True, exist_ok=True)
        store.write(tmp_path / "status.json", {
            "run_id": "R",
            "status": "running",
            "normalized_total": 300,
            "durability": {"saved": True, "generation": 7},
            "workspace_generation": 9,
        })
        ok, reason = store.durable_state_consistent(tmp_path)
        assert ok is False
        assert "generation" in reason.lower() or "archive" in reason.lower(), reason

    def test_matching_generations_are_accepted(self, tmp_path):
        from app.services.storage import RunStore

        store = RunStore()
        store.write(tmp_path / "status.json", {
            "run_id": "R", "status": "running",
            "durability": {"saved": True, "generation": 7},
            "workspace_generation": 7,
        })
        ok, _ = store.durable_state_consistent(tmp_path)
        assert ok is True

    def test_a_run_that_never_checkpointed_is_not_falsely_accused(self, tmp_path):
        """A first-invocation run has no archive yet and that is fine."""
        from app.services.storage import RunStore

        store = RunStore()
        store.write(tmp_path / "status.json", {"run_id": "R", "status": "running"})
        ok, _ = store.durable_state_consistent(tmp_path)
        assert ok is True

    def test_a_failed_checkpoint_is_recorded_rather_than_swallowed(self, tmp_path):
        """`except: pass` around a checkpoint is what makes the divergence
        silent. The failure has to leave a mark the next worker can read."""
        from app.services.storage import RunStore

        store = RunStore()
        store.write(tmp_path / "status.json", {
            "run_id": "R", "status": "running",
            "workspace_generation": 4,
            "durability": {"saved": False, "generation": 2,
                           "error": "blob upload failed"},
        })
        ok, reason = store.durable_state_consistent(tmp_path)
        assert ok is False, "a run whose last checkpoint failed must not resume blindly"
        assert "blob upload failed" in reason or "generation" in reason.lower(), reason
