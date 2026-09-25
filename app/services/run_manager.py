from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import uuid
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.services.apify_service import CollectionNotConfigured
from app.services.collector import CollectionTimeBudgetExceeded, execute_plan
from app.services.cleaning import CleaningCancelled, clean_run
from app.services.relevance_expansion import (
    COMMENT_CAPABLE_SOURCES,
    adaptive_expand_after_cleaning,
    comment_source_is_complete,
)
from app.services.ai_analysis import AIAnalysisCancelled, AIAnalysisProviderOutage, AIAnalysisTimeBudgetExceeded, analyze_run
from app.services.intelligence import build_intelligence
from app.services.investigations import InvestigationCancelled, build_investigations
from app.services.visualizations import VisualizationCancelled, build_visualizations
from app.services.semantic_refill import semantic_refill
from app.services.presentation import PresentationCancelled, build_exports
from app.services.storage import RunNotFound, RunStore


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStateError(RuntimeError):
    pass


#: Step states that mean "there is still work coming", so a fresh worker must
#: not treat the run as collected. "deferred" is included on purpose: it is what
#: the comment layer writes when it stops at the worker deadline and expects to
#: be resumed, not skipped.
UNFINISHED_STEP_STATES = {"running", "queued", "deferred", "pending", "retrying"}


def resume_at_analysis(prior_status: dict, folder: Path, plan: dict | None = None) -> bool:
    """May a fresh worker skip collection and go straight to the AI analysis?

    Only when every collecting step has REPORTED that it finished. The presence
    of a cleaning output file is not evidence of that: `clean_run()` runs again
    after every comment Actor call, so `cleaning/semantic-candidates.json`
    exists from the first call onwards, while the comment layer is still adding
    evidence. Reading that file as a completion marker is what made a second
    worker analyse 4 records and report a finished run.

    The file check stays, but only as a second condition: the status says the
    work is done AND the output it produced is actually on this machine.
    """
    status = prior_status or {}

    if str((status.get("cleaning") or {}).get("status") or "") != "succeeded":
        return False

    adaptive = str((status.get("adaptive_collection") or {}).get("status") or "")
    if adaptive in UNFINISHED_STEP_STATES:
        return False

    deepening = status.get("comment_deepening") or {}
    if not isinstance(deepening, dict):
        deepening = {}

    # Which sources OWE a comment layer is a property of the plan, not of what
    # happens to be written in the status. Checking only the rows that exist
    # means a source that died before writing its first row objects to nothing,
    # which is how two of three sources could vanish from a "finished" run.
    # CollectionPlan serializes this field as `comments_requested`. Using the
    # AnalysisDraft name (`comments`) here silently disables the expected-source
    # barrier for real production plans while hand-written tests can still pass.
    if plan is not None and plan.get("comments_requested"):
        expected = {str(sp.get("source") or "") for sp in (plan.get("sources") or [])}
        expected &= COMMENT_CAPABLE_SOURCES
        for source in sorted(expected):
            if not comment_source_is_complete(deepening.get(source)):
                return False
    else:
        for value in deepening.values():
            row = value if isinstance(value, dict) else {"status": value}
            if not comment_source_is_complete(row):
                return False

    return ((folder / "cleaning" / "semantic-candidates.json").exists()
            or (folder / "cleaning" / "trusted.json").exists())


#: How many times a fresh worker may resume the adaptive/comment step at the
#: SAME durable state before the run stops waiting for it. Run
#: 20260925T002306Z-65236947 resumed the X comment pass every ~25 minutes for
#: eight hours: the pass never finished inside one worker, never recorded any
#: progress, and nothing bounded the loop.
MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS = 3


def adaptive_progress_fingerprint(store: RunStore, folder: Path, status: dict) -> str:
    """A stable digest of everything the adaptive step has durably achieved.

    Two consecutive continuations with the same fingerprint did no work.
    """
    import hashlib
    import json as _json
    parts = []
    deepening = status.get("comment_deepening") if isinstance(status.get("comment_deepening"), dict) else {}
    for source in sorted(COMMENT_CAPABLE_SOURCES):
        rows = store.read(folder / f"normalized-comments-{source}.json", []) or []
        row = deepening.get(source) if isinstance(deepening.get(source), dict) else {}
        parts.append([source, len(rows), sorted(str(b) for b in (row.get("buckets_done") or [])),
                      str(row.get("status") or ""), int(row.get("collected") or 0),
                      len(row.get("attempted_refs") or [])])
    parts.append(len(store.read(folder / "analysis" / "analysis-ready.json", []) or []))
    parts.append(sorted((store.read(folder / "semantic-refill-state.json", {}) or {}).get("done_routes") or []))
    parts.append(store.read(folder / "conversation-parent-discovery-state.json", {}) or {})
    blob = _json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def update_adaptive_continuation_guard(status: dict, fingerprint: str) -> dict:
    """Count consecutive continuations that left the adaptive step where it was.

    Returns the guard block (also written into ``status``). ``tripped`` becomes
    true once the limit is reached; the caller then closes the step honestly
    instead of resuming it again.
    """
    guard = dict(status.get("adaptive_continuation_guard") or {})
    count = int(guard.get("count") or 0) + 1 if guard.get("fingerprint") == fingerprint else 1
    guard.update({
        "fingerprint": fingerprint,
        "count": count,
        "updated_at": _utcnow(),
        "limit": MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS,
    })
    if count >= MAX_ADAPTIVE_CONTINUATIONS_WITHOUT_PROGRESS:
        guard["tripped"] = True
        guard["reason"] = "no_progress_after_repeated_continuations"
    status["adaptive_continuation_guard"] = guard
    return guard


def close_adaptive_step_without_progress(store: RunStore, run_id: str, guard: dict) -> None:
    """Write an honest terminal state for every comment pass still 'owed'.

    Every source still marked unfinished becomes a shortfall with an explicit
    reason, so the operator sees WHY, and ``resume_at_analysis`` lets the next
    worker analyse the evidence that exists instead of resuming forever.
    """
    status = store.read_status(run_id)
    deepening = dict(status.get("comment_deepening") or {})
    for source, row in list(deepening.items()):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") in UNFINISHED_STEP_STATES or not comment_source_is_complete(row):
            collected = int(row.get("collected") or 0)
            requested = int(row.get("requested") or row.get("target") or 0)
            row.update({
                "status": "shortfall",
                "reason": "no_progress_after_repeated_continuations",
                "collected": collected,
                "shortfall": max(0, requested - collected) if requested else 0,
                "buckets_done": ["owned", "open", "backfill"],
                "updated_at": _utcnow(),
            })
            deepening[source] = row
    status["comment_deepening"] = deepening
    status["adaptive_collection"] = {
        **(status.get("adaptive_collection") or {}),
        "status": "completed_no_progress",
        "completed_at": _utcnow(),
        "guard": guard,
    }
    status["current"] = {
        "source": None,
        "code": "adaptive_closed_no_progress",
        "message": (f"The comment/adaptive pass made no progress across "
                    f"{guard.get('count')} worker continuations; analysing the evidence collected so far"),
    }
    store.write_status(run_id, status)


class RunManager:
    """Background lifecycle manager for collection runs.

    The internal foundation deliberately uses a small in-process executor: it is simple and low-cost
    for the two-person internal app, while exposing a clean API that can later be
    backed by a durable queue without changing the UI contract.
    """

    def __init__(self, store: RunStore | None = None, max_workers: int | None = None):
        self.store = store or RunStore()
        workers = int(max_workers or settings.signalyth_max_parallel_runs)
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="signalyth-run")
        self._lock = threading.RLock()
        self._jobs: dict[str, Future] = {}
        self._lease_tokens: dict[str, str] = {}
        # Monotonic timestamp after which the current invocation must stop doing new
        # paid work, checkpoint and requeue a continuation. None disables the guard.
        self._deadline_monotonic: float | None = None

    def set_invocation_deadline(self, seconds: float | None) -> None:
        if seconds and seconds > 0:
            self._deadline_monotonic = time.monotonic() + float(seconds)
        else:
            self._deadline_monotonic = None

    def _seconds_left(self, margin_seconds: float = 0.0) -> float:
        """Seconds of this invocation still usable, after keeping a margin.

        A yes/no deadline cannot stop a worker from starting a three-minute
        Actor call with one minute to live. This can.
        """
        if self._deadline_monotonic is None:
            return float("inf")
        return (self._deadline_monotonic - max(0.0, margin_seconds)) - time.monotonic()

    def _deadline_reached(self, margin_seconds: float = 0.0) -> bool:
        if self._deadline_monotonic is None:
            return False
        return time.monotonic() >= self._deadline_monotonic - max(0.0, margin_seconds)

    def _analysis_deadline_check(self) -> bool:
        """Deadline check used to gate NEW analysis batch submissions.

        In-flight OpenAI requests can run up to the client timeout (60s) after
        the last submission, and the final checkpoint/requeue also needs time,
        so submissions must stop with a safety margin before the soft deadline.
        """
        return self._deadline_reached(margin_seconds=75.0)

    @staticmethod
    def _status_age_seconds(status: dict) -> float | None:
        stamp = status.get("updated_at")
        if not stamp:
            return None
        try:
            updated = datetime.fromisoformat(str(stamp))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        except Exception:
            return None

    def _checkpoint_milestone(self, run_id: str) -> bool:
        """Commit a semantic stage boundary and prove this worker still owns it.

        Generation is part of the handoff safety contract, not optional
        bookkeeping. A worker that cannot advance the generation, persist the
        workspace, or confirm ownership afterwards must not publish the next
        stage status.
        """
        if not self._lease_ok(run_id):
            return False

        generation = self.store.bump_workspace_generation(self.store.folder_for(run_id))
        if generation <= 0:
            raise RunStateError("Could not advance workspace generation before checkpoint")

        self.store.checkpoint_run(run_id)

        # Ownership may have changed while the archive was being built/uploaded.
        # Do not let the displaced worker publish a newer phase afterwards.
        return self._lease_ok(run_id)

    def _lease_ok(self, run_id: str) -> bool:
        return self._owns_lease(run_id, self._lease_tokens.get(run_id, ""))

    def _requeue_continuation(self, run_id: str) -> None:
        """Schedule a fresh invocation that resumes the run from durable state."""
        if settings.signalyth_execution_backend.strip().lower() == "celery":
            from app.worker.tasks import run_signalyth
            result = run_signalyth.apply_async(args=[run_id], task_id=f"{run_id}-c{uuid4().hex[:8]}")
            status = self.store.read_status(run_id)
            status["queue_message_id"] = str(result.id)
            self.store.write_status(run_id, status)
        else:
            with self._lock:
                future = self.executor.submit(self._worker, run_id)
                self._jobs[run_id] = future

    def _set_status(self, run_id: str, **changes) -> dict:
        status = self.store.read_status(run_id)
        status.update(changes)
        return self.store.write_status(run_id, status)

    def enqueue(self, run_id: str) -> dict:
        with self._lock:
            status = self.store.read_status(run_id)
            current = status.get("status")
            existing = self._jobs.get(run_id)
            if existing is not None and not existing.done():
                return status
            phase = str(status.get("phase") or "")
            recoverable_stage_failure = current == "failed" and phase in {"ai_analysis_failed", "exports_failed"}
            if current in self.store.TERMINAL_STATUSES and not recoverable_stage_failure:
                raise RunStateError(f"Run is already terminal: {current}")
            if current not in {"planned", "queued"} and not recoverable_stage_failure:
                # A serverless worker can be hard-killed mid-run, leaving the status
                # permanently "running". If nothing has written status.json for the
                # stale window, treat the worker as dead and allow a durable resume.
                age = self._status_age_seconds(status)
                stale_after = max(60, int(settings.signalyth_stale_running_after_seconds))
                if current in {"running", "cancelling"} and age is not None and age >= stale_after:
                    pass  # recoverable orphan: fall through and requeue a resume
                else:
                    raise RunStateError(f"Run cannot be started from state: {current}")

            self.store.reset_control(run_id)
            status.update({
                "status": "queued",
                "phase": "queued",
                "cancel_requested": False,
                "fatal_error": None,
                "current": {"source": None, "code": "waiting_worker", "message": "Waiting for an available collection worker"},
            })
            self.store.write_status(run_id, status)

            if settings.signalyth_execution_backend.strip().lower() == "celery":
                try:
                    # Lazy import keeps the local/offline build independent of Celery.
                    from app.worker.tasks import run_signalyth
                    result = run_signalyth.apply_async(args=[run_id], task_id=f"{run_id}-{uuid4().hex[:8]}")
                    status = self.store.read_status(run_id)
                    status["queue_message_id"] = str(result.id)
                    self.store.write_status(run_id, status)
                    return self.store.read_status(run_id)
                except Exception as exc:
                    status = self.store.read_status(run_id)
                    status.update({
                        "status": "planned",
                        "phase": "planned",
                        "fatal_error": None,
                        "current": {"source": None, "code": "queue_failed", "message": "Could not enqueue the cloud worker; no collection was started"},
                    })
                    self.store.write_status(run_id, status)
                    raise RunStateError(f"Could not enqueue cloud worker: {exc}") from exc

            future = self.executor.submit(self._worker, run_id)
            self._jobs[run_id] = future
            return self.store.read_status(run_id)


    def enqueue_reprocess(self, run_id: str) -> dict:
        """Rebuild a completed run from saved normalized evidence without any new collection."""
        from app.services.reprocess import (
            ReprocessSafetyError,
            backup_exists,
            create_backup,
            recover_stale_backup,
        )

        with self._lock:
            folder = self.store.folder_for(run_id)
            status = self.store.read_status(run_id)
            existing = self._jobs.get(run_id)
            rp = status.get("reprocess") or {}
            age = self._status_age_seconds(status)
            stale_after = max(60, int(settings.signalyth_stale_running_after_seconds))

            if (
                str(rp.get("status") or "") in {"queued", "running"}
                and str(status.get("status") or "") in {"queued", "running", "cancelling"}
                and (
                    (existing is not None and not existing.done())
                    or age is None
                    or age < stale_after
                )
            ):
                return status

            if backup_exists(folder):
                try:
                    status = recover_stale_backup(folder, status)
                    self.store.write_status(run_id, status)
                    self.store.reset_control(run_id)
                    try:
                        self.store.checkpoint_run(run_id)
                    except Exception:
                        pass
                except ReprocessSafetyError as exc:
                    raise RunStateError(str(exc)) from exc

            current = str(status.get("status") or "")
            if current not in self.store.TERMINAL_STATUSES:
                raise RunStateError(
                    f"Full reprocess requires a completed/terminal run; current state is {current or 'unknown'}."
                )
            if not settings.signalyth_ai_enabled:
                raise RunStateError("Full reprocess requires AI Analysis to be enabled.")
            if not settings.openai_api_key:
                raise RunStateError("Full reprocess requires OPENAI_API_KEY. No Apify collection will be started.")

            try:
                meta = create_backup(folder)
            except ReprocessSafetyError as exc:
                raise RunStateError(str(exc)) from exc

            previous_status = current
            previous_phase = str(status.get("phase") or "")
            self.store.reset_control(run_id)
            status = dict(status)
            status.update({
                "status": "queued",
                "phase": "reprocess_cleaning",
                "completed_at": None,
                "cancel_requested": False,
                "fatal_error": None,
                "current": {
                    "source": None,
                    "code": "reprocess_waiting_worker",
                    "message": "Full reprocess queued from saved evidence; collection/Apify will not run",
                },
                "reprocess": {
                    "status": "queued",
                    "stage": "cleaning",
                    "requested_at": _utcnow(),
                    "started_at": None,
                    "completed_at": None,
                    "previous_status": previous_status,
                    "previous_phase": previous_phase,
                    "normalized_sha256": meta.get("normalized_sha256"),
                    "no_collection": True,
                    "error": None,
                },
            })
            status.setdefault("progress", {})["percent"] = 0
            self.store.write_status(run_id, status)

            # The backup must be durable before another serverless/Celery worker can claim the run.
            try:
                self.store.checkpoint_run(run_id)
            except Exception as exc:
                self._rollback_reprocess(run_id, "failed", f"Could not persist reprocess backup: {exc}")
                raise RunStateError("Could not persist the safety backup; full reprocess was not started.") from exc

            if settings.signalyth_execution_backend.strip().lower() == "celery":
                try:
                    from app.worker.tasks import run_signalyth
                    result = run_signalyth.apply_async(args=[run_id], task_id=f"{run_id}-r{uuid4().hex[:8]}")
                    status = self.store.read_status(run_id)
                    status["queue_message_id"] = str(result.id)
                    self.store.write_status(run_id, status)
                    return self.store.read_status(run_id)
                except Exception as exc:
                    self._rollback_reprocess(run_id, "failed", f"Could not enqueue full reprocess: {exc}")
                    raise RunStateError(f"Could not enqueue full reprocess: {exc}") from exc

            future = self.executor.submit(self._worker, run_id)
            self._jobs[run_id] = future
            return self.store.read_status(run_id)

    def run_now(self, run_id: str) -> None:
        """Execute one run in the current process (used by the Vercel Celery subscriber)."""
        self.set_invocation_deadline(settings.signalyth_worker_soft_deadline_seconds)
        self._worker(run_id)

    def _claim_lease(self, run_id: str) -> str:
        """Stamp this invocation as the owner of the run.

        Serverless invocations are separate processes, so the in-process job map
        cannot stop two workers from driving the same run after a stale-worker
        resume. The newest claimant wins; older ones stand down at the next
        phase boundary instead of rewinding the run's progress.
        """
        token = uuid.uuid4().hex
        try:
            status = self.store.read_status(run_id)
            previous = dict(status.get("worker_lease") or {})
            status["worker_lease"] = {
                "token": token,
                "claimed_at": _utcnow(),
                # Who this invocation took the run from, and how many times the
                # run has changed hands. Without this there is no way to tell,
                # after the fact, that two workers were driving the same run.
                "displaced_token": str(previous.get("token") or "") or None,
                "displaced_at": previous.get("claimed_at"),
                "generation": int(previous.get("generation") or 0) + 1,
            }
            self.store.write_status(run_id, status)
        except Exception:
            # The claim was never recorded, so this invocation does not own the
            # run. Returning a token anyway let a worker act on an ownership it
            # had never actually taken.
            return ""
        return token

    def _owns_lease(self, run_id: str, token: str) -> bool:
        """Does this invocation still own the run?

        Fail CLOSED. This is a lock, and a worker that cannot confirm it still
        holds the lock must stop rather than keep writing: the cost of a false
        stand-down is a requeue, the cost of a false ownership is two workers
        writing the same evidence and a report built on half of it.
        """
        if not token:
            return True
        try:
            lease = (self.store.read_status(run_id) or {}).get("worker_lease") or {}
        except Exception:
            return False
        current = str(lease.get("token") or "")
        if not current:
            # This worker holds a token but the run records none: its claim was
            # lost or wiped. That is not ownership.
            return False
        return current == token

    def _worker(self, run_id: str):
        try:
            # The worker's serverless scratch disk fills up across warm
            # invocations just like the API's; make room before heavy writes.
            self.store.prune_local_scratch(keep_run_id=run_id)
            folder = self.store.folder_for(run_id)
            lease_token = self._claim_lease(run_id)
            self._lease_tokens[run_id] = lease_token
            if not lease_token:
                # The ownership write did not succeed. Acting anyway turns a
                # transient Blob failure into an unleased paid worker.
                return

            # Before ANY resume path — collection as much as analysis — refuse to
            # build on a workspace whose saved copy is behind its own status.
            consistent, why = self.store.durable_state_consistent(folder)
            if not consistent:
                self._mark_failed(
                    run_id,
                    "Stopped before resuming a run whose saved copy is behind its "
                    f"progress: {why}. Nothing was re-collected and nothing was paid twice.",
                )
                return

            if self.store.cancel_requested_folder(folder):
                self._mark_cancelled_before_start(run_id)
                return
            plan = self.store.read_plan(run_id)

            current_status = self.store.read_status(run_id)
            if str((current_status.get("reprocess") or {}).get("status") or "") in {"queued", "running"}:
                self._run_reprocess(run_id, folder, plan)
                return

            # Durable resume: if an earlier invocation already finished collection and
            # cleaning, do NOT re-run paid collection. Jump straight to the analysis
            # chain, which itself resumes cheaply via the per-batch OpenAI cache.
            prior_status = self.store.read_status(run_id)
            # "The file exists" is NOT "the step finished" — see resume_at_analysis.
            cleaning_done = resume_at_analysis(prior_status, folder, plan=plan)
            resumable = prior_status.get("status") not in self.store.TERMINAL_STATUSES
            if cleaning_done and resumable:
                if not self._evidence_guard(run_id):
                    return
                terminal = prior_status.get("collection_status") or "succeeded"
                if settings.signalyth_ai_enabled and settings.openai_api_key:
                    self._run_ai_analysis(run_id, folder, plan, terminal)
                else:
                    # AI disabled: re-running cheap deterministic cleaning safely
                    # re-establishes the terminal state without any paid collection.
                    self._run_cleaning(run_id, folder, plan)
                return

            try:
                collection_status = execute_plan(
                    plan,
                    run_id,
                    folder,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                    continue_pipeline=True,
                    deadline_check=lambda: self._deadline_reached(
                        margin_seconds=float(settings.signalyth_collection_deadline_margin_seconds)
                    ),
                )
            except CollectionTimeBudgetExceeded:
                # Not a failure: completed sources are terminal on disk/Blob and are
                # never re-paid. Requeue a continuation that resumes the rest.
                status = self.store.read_status(run_id)
                status.update({
                    "status": "queued",
                    "phase": "collecting",
                    "fatal_error": None,
                    "current": {
                        "source": None,
                        "code": "collection_continuation",
                        "message": "Worker time budget reached; collection continues automatically from the completed sources",
                    },
                })
                self.store.write_status(run_id, status)
                try:
                    self.store.checkpoint_run(run_id)
                except Exception:
                    pass
                try:
                    self._requeue_continuation(run_id)
                except Exception as requeue_exc:
                    status = self.store.read_status(run_id)
                    status["current"] = {
                        "source": None,
                        "code": "continuation_requeue_failed",
                        "message": f"Automatic continuation could not be enqueued ({requeue_exc}); press Start to resume safely",
                    }
                    self.store.write_status(run_id, status)
                return
            if not self._checkpoint_milestone(run_id):  # collection finished
                return
            if collection_status.get("status") in {"cancelled", "failed"}:
                return
            self._run_cleaning(run_id, folder, plan)
        except RunNotFound:
            return
        except CollectionNotConfigured as exc:
            self._mark_failed(run_id, str(exc))
        except Exception as exc:
            self._mark_failed(run_id, f"Unexpected collection failure: {exc}")
        finally:
            # Finalization is also a write boundary. A failed claimant or a worker
            # displaced during a slow external call must not overwrite the current
            # owner's archive merely because Python is unwinding this invocation.
            try:
                token = self._lease_tokens.get(run_id, "")
                if token and self._owns_lease(run_id, token):
                    self.store.checkpoint_run(run_id)
            except Exception:
                # Preserve the original pipeline outcome; durability checks will
                # surface a failed save on the next safe resume.
                pass
            with self._lock:
                self._jobs.pop(run_id, None)


    def _set_reprocess_stage(self, run_id: str, stage: str, message: str, percent: int) -> dict:
        status = self.store.read_status(run_id)
        rp = dict(status.get("reprocess") or {})
        rp.update({
            "status": "running",
            "stage": stage,
            "started_at": rp.get("started_at") or _utcnow(),
            "error": None,
        })
        status.update({
            "status": "running",
            "phase": f"reprocess_{stage}",
            "completed_at": None,
            "cancel_requested": False,
            "reprocess": rp,
            "current": {"source": None, "code": f"reprocess_{stage}", "message": message},
        })
        status.setdefault("progress", {})["percent"] = max(0, min(99, int(percent)))
        return self.store.write_status(run_id, status)

    def _complete_reprocess_stage(self, run_id: str, status_key: str, report: dict, next_stage: str) -> None:
        status = self.store.read_status(run_id)
        status[status_key] = {
            **(status.get(status_key) or {}),
            "status": "succeeded",
            "completed_at": report.get("generated_at") or _utcnow(),
            "error": None,
            "summary": report,
        }
        rp = dict(status.get("reprocess") or {})
        rp["stage"] = next_stage
        status["reprocess"] = rp
        self.store.write_status(run_id, status)
        try:
            self.store.checkpoint_run(run_id)
        except Exception:
            pass

    def _queue_reprocess_continuation(self, run_id: str, stage: str, message: str) -> None:
        status = self.store.read_status(run_id)
        rp = dict(status.get("reprocess") or {})
        rp.update({"status": "queued", "stage": stage, "error": None})
        status.update({
            "status": "queued",
            "phase": f"reprocess_{stage}",
            "reprocess": rp,
            "current": {"source": None, "code": "reprocess_continuation", "message": message},
        })
        self.store.write_status(run_id, status)
        try:
            self.store.checkpoint_run(run_id)
        except Exception:
            pass
        try:
            self._requeue_continuation(run_id)
        except Exception as exc:
            status = self.store.read_status(run_id)
            status["current"] = {
                "source": None,
                "code": "reprocess_continuation_failed",
                "message": f"Full reprocess is safely checkpointed but continuation could not be enqueued ({exc}); retry Rebuild report to resume.",
            }
            self.store.write_status(run_id, status)

    def _rollback_reprocess(self, run_id: str, outcome: str, error: str | None) -> None:
        from app.services.reprocess import (
            assert_normalized_unchanged,
            discard_backup,
            restore_backup,
        )

        try:
            folder = self.store.folder_for(run_id)
            active_status = self.store.read_status(run_id)
            active_rp = dict(active_status.get("reprocess") or {})
            normalized_error = None
            try:
                assert_normalized_unchanged(folder)
            except Exception as exc:
                normalized_error = str(exc)

            previous = restore_backup(folder)
            restored = dict(previous or {})
            restored["reprocess"] = {
                **active_rp,
                "status": outcome,
                "stage": "rolled_back",
                "completed_at": _utcnow(),
                "rolled_back": True,
                "no_collection": True,
                "error": error or normalized_error,
            }
            restored["cancel_requested"] = False
            restored["current"] = {
                "source": None,
                "code": "reprocess_cancelled_rolled_back" if outcome == "cancelled" else "reprocess_failed_rolled_back",
                "message": (
                    "Full reprocess cancelled; previous report and processed state were restored"
                    if outcome == "cancelled"
                    else "Full reprocess failed safely; previous report and processed state were restored"
                ),
            }
            self.store.write_status(run_id, restored)
            self.store.reset_control(run_id)
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                pass
            discard_backup(folder)
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                pass
        except Exception as rollback_exc:
            try:
                status = self.store.read_status(run_id)
                status.update({
                    "status": "failed",
                    "phase": "reprocess_rollback_failed",
                    "completed_at": _utcnow(),
                    "fatal_error": f"Full reprocess rollback needs attention: {rollback_exc}",
                    "current": {
                        "source": None,
                        "code": "reprocess_rollback_failed",
                        "message": "Safety backup was kept because automatic rollback could not be completed",
                    },
                })
                status["reprocess"] = {
                    **(status.get("reprocess") or {}),
                    "status": "rollback_failed",
                    "error": str(rollback_exc),
                }
                self.store.write_status(run_id, status)
            except Exception:
                pass

    def _reprocess_deadline_checkpoint(self, run_id: str, next_stage: str) -> bool:
        if not self._deadline_reached(margin_seconds=60.0):
            return False
        self._queue_reprocess_continuation(
            run_id,
            next_stage,
            "Worker time budget reached; full reprocess will continue from the saved downstream stage",
        )
        return True

    def _run_reprocess(self, run_id: str, folder: Path, plan: dict) -> None:
        from app.services.reprocess import assert_normalized_unchanged, discard_backup

        if not self._lease_ok(run_id):
            return

        try:
            status = self.store.read_status(run_id)
            stage = str((status.get("reprocess") or {}).get("stage") or "cleaning")

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "cleaning":
                self._set_reprocess_stage(
                    run_id, "cleaning",
                    "Reprocessing saved evidence: Cleaning & Relevance (no collection/Apify)",
                    10,
                )
                report = clean_run(
                    folder,
                    plan=plan,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                )
                self._complete_reprocess_stage(run_id, "cleaning", report, "ai_analysis")
                stage = "ai_analysis"
                if self._reprocess_deadline_checkpoint(run_id, stage):
                    return

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "ai_analysis":
                self._set_reprocess_stage(
                    run_id, "ai_analysis",
                    "Reprocessing saved evidence: AI semantic analysis (OpenAI; no Apify)",
                    30,
                )
                try:
                    report = analyze_run(
                        folder,
                        plan=plan,
                        cancel_check=lambda: self.store.cancel_requested_folder(folder),
                        force=True,
                        deadline_check=self._analysis_deadline_check,
                    )
                except AIAnalysisTimeBudgetExceeded:
                    self._queue_reprocess_continuation(
                        run_id,
                        "ai_analysis",
                        "AI reprocess checkpointed; continuing automatically from the saved OpenAI cache",
                    )
                    return
                self._complete_reprocess_stage(run_id, "analysis", report, "intelligence")
                stage = "intelligence"
                if self._reprocess_deadline_checkpoint(run_id, stage):
                    return

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "intelligence":
                self._set_reprocess_stage(
                    run_id, "intelligence",
                    "Reprocessing saved evidence: Intelligence Engine",
                    72,
                )
                report = build_intelligence(folder, plan=plan, force=True)
                self._complete_reprocess_stage(run_id, "intelligence", report, "investigations")
                stage = "investigations"
                if self._reprocess_deadline_checkpoint(run_id, stage):
                    return

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "investigations":
                self._set_reprocess_stage(
                    run_id, "investigations",
                    "Reprocessing saved evidence: Automatic Investigations",
                    82,
                )
                report = build_investigations(
                    folder,
                    plan=plan,
                    force=True,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                )
                self._complete_reprocess_stage(run_id, "investigations", report, "visualizations")
                stage = "visualizations"
                if self._reprocess_deadline_checkpoint(run_id, stage):
                    return

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "visualizations":
                self._set_reprocess_stage(
                    run_id, "visualizations",
                    "Reprocessing saved evidence: Charts & Dashboard",
                    90,
                )
                report = build_visualizations(
                    folder,
                    plan=plan,
                    force=True,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                )
                self._complete_reprocess_stage(run_id, "visualizations", report, "exports")
                stage = "exports"
                if self._reprocess_deadline_checkpoint(run_id, stage):
                    return

            if self.store.cancel_requested_folder(folder):
                self._rollback_reprocess(run_id, "cancelled", None)
                return

            if stage == "exports":
                self._set_reprocess_stage(
                    run_id, "exports",
                    "Reprocessing saved evidence: rebuilding presentation & exports",
                    96,
                )
                report = build_exports(
                    folder,
                    plan=plan,
                    force=True,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                )
                self._complete_reprocess_stage(run_id, "exports", report, "completed")

            assert_normalized_unchanged(folder)

            status = self.store.read_status(run_id)
            rp = dict(status.get("reprocess") or {})
            previous_terminal = str(rp.get("previous_status") or "")
            terminal = (
                previous_terminal
                if previous_terminal in {"succeeded", "completed_shortfall", "completed_with_errors"}
                else "succeeded"
            )
            rp.update({
                "status": "succeeded",
                "stage": "completed",
                "completed_at": _utcnow(),
                "rolled_back": False,
                "no_collection": True,
                "error": None,
            })
            status.update({
                "status": terminal,
                "phase": "exports_ready",
                "completed_at": _utcnow(),
                "cancel_requested": False,
                "fatal_error": None,
                "reprocess": rp,
                "current": {
                    "source": None,
                    "code": "reprocess_completed",
                    "message": "Full reprocess completed from saved evidence; no collection/Apify was run",
                },
            })
            status.setdefault("progress", {})["percent"] = 100
            self.store.write_status(run_id, status)
            self.store.reset_control(run_id)

            # First checkpoint commits the new outputs while the rollback backup still exists.
            # Only after that succeeds do we drop the backup and checkpoint once more.
            self.store.checkpoint_run(run_id)
            discard_backup(folder)
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                # A remote archive may temporarily retain the backup, but status marks
                # the new state successful; the next reprocess preflight cleans it up.
                pass

        except (CleaningCancelled, AIAnalysisCancelled, InvestigationCancelled, VisualizationCancelled, PresentationCancelled):
            self._rollback_reprocess(run_id, "cancelled", None)
        except AIAnalysisProviderOutage as exc:
            self._rollback_reprocess(run_id, "failed", f"OpenAI analysis failed for every batch: {exc}")
        except Exception as exc:
            self._rollback_reprocess(run_id, "failed", str(exc))

    def _evidence_guard(self, run_id: str) -> bool:
        """Stop the run rather than let it quietly shrink. True = safe to go on.

        A replacement worker that cannot restore the archive gets plan + status
        and nothing else. Continuing there produces a report built on whatever
        happened to survive, with no sign that anything is missing — which is
        far worse than stopping.
        """
        ok, reason = self.store.evidence_intact(run_id)
        if ok:
            return True
        status = self.store.read_status(run_id)
        status.update({
            "status": "failed",
            "phase": "evidence_unavailable",
            "completed_at": _utcnow(),
            "fatal_error": f"Stopped to avoid a report built on missing evidence: {reason}.",
            "current": {"source": None, "code": "evidence_unavailable",
                        "message": ("Τα συλλεγμένα στοιχεία δεν είναι διαθέσιμα σε αυτόν τον "
                                    "server. Το run σταμάτησε αντί να βγάλει αναφορά από "
                                    "λιγότερα δεδομένα.")},
        })
        status.setdefault("progress", {})["percent"] = 100
        self.store.write_status(run_id, status)
        return False

    def _run_cleaning(self, run_id: str, folder: Path, plan: dict):
        if not self._lease_ok(run_id):
            return  # a newer invocation owns this run
        if self.store.cancel_requested_folder(folder):
            self._mark_cancelled_after_collection(run_id)
            return
        if not self._evidence_guard(run_id):
            return
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "cleaning",
            "cleaning": {"status": "running", "ruleset_version": "0.8.0", "started_at": _utcnow(), "completed_at": None, "error": None},
            "current": {"source": None, "code": "cleaning_data", "message": "Cleaning, relevance and authenticity checks"},
        })
        status.setdefault("progress", {})["percent"] = 88
        self.store.write_status(run_id, status)
        try:
            report = clean_run(
                folder,
                plan=plan,
                cancel_check=lambda: self.store.cancel_requested_folder(folder),
            )
            # Smart Collection v2 closes the gap between a raw collection target
            # and the requested *trusted/analyzable* target.  It may diversify a
            # dominant context and, when Comments is ON, deepen into direct replies.
            # Every adaptive Actor call remains inside the existing run budget.
            # The adaptive phase must also run when Comments is ON with no sample
            # shortfall: comment deepening is requested audience evidence, not a
            # shortfall repair, so meeting the primary target must not silently
            # skip the comment layer the operator paid attention to enable.
            needs_adaptive = (
                int(report.get("trusted_sample_shortfall", 0) or 0) > 0
                or bool(plan.get("comments_requested"))
            )
            entry_status = self.store.read_status(run_id)
            if str((entry_status.get("adaptive_collection") or {}).get("status") or "") in UNFINISHED_STEP_STATES:
                # A previous worker died INSIDE this step (hard kill, no graceful
                # deadline hand-off) and the stale-run resume brought us here.
                # That is a continuation too, and it must count against the same
                # limit — otherwise a call that never returns loops forever.
                guard_before = update_adaptive_continuation_guard(
                    entry_status, adaptive_progress_fingerprint(self.store, folder, entry_status))
                self.store.write_status(run_id, entry_status)
            else:
                guard_before = dict(entry_status.get("adaptive_continuation_guard") or {})
            if guard_before.get("tripped"):
                # A previous continuation already decided this step cannot
                # progress. Close it honestly (idempotent) and move on.
                close_adaptive_step_without_progress(self.store, run_id, guard_before)
                needs_adaptive = False
            if needs_adaptive and plan.get("search_strategy_version") in {"smart-collection-v2", "master30-search-v1"}:
                adaptive_status = self.store.read_status(run_id)
                adaptive_status.update({
                    "status": "running",
                    "phase": "adaptive_collection",
                    "adaptive_collection": {"status": "running", "strategy_version": "smart-collection-v2", "started_at": _utcnow(), "completed_at": None},
                    "current": {"source": "x", "code": "adaptive_collection", "message": "Diversifying relevant evidence and deepening useful conversations"},
                })
                adaptive_status.setdefault("progress", {})["percent"] = 89
                self.store.write_status(run_id, adaptive_status)
                def _adaptive_heartbeat(source: str) -> None:
                    # A status write every call keeps the run visibly alive, so the
                    # stale-worker recovery never resumes it in parallel.
                    try:
                        beat = self.store.read_status(run_id)
                        beat["current"] = {
                            "source": source,
                            "code": "adaptive_collection",
                            "message": "Diversifying relevant evidence and deepening useful conversations",
                        }
                        beat.setdefault("progress", {})["percent"] = 89
                        self.store.write_status(run_id, beat)
                    except Exception:
                        pass

                expanded = adaptive_expand_after_cleaning(
                    folder,
                    plan=plan,
                    initial_report=report,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
                    deadline_check=lambda: self._deadline_reached(
                        margin_seconds=float(settings.signalyth_collection_deadline_margin_seconds)
                    ),
                    time_left=lambda: self._seconds_left(
                        margin_seconds=float(settings.signalyth_collection_deadline_margin_seconds)
                    ),
                    heartbeat=_adaptive_heartbeat,
                )
                report = expanded.get("report") or report
                adaptive_status = self.store.read_status(run_id)
                adaptive_status["adaptive_collection"] = {
                    "status": expanded.get("audit", {}).get("status", "completed"),
                    "strategy_version": "smart-collection-v2",
                    "started_at": (adaptive_status.get("adaptive_collection") or {}).get("started_at"),
                    "completed_at": _utcnow(),
                    "summary": expanded.get("audit"),
                }
                self.store.write_status(run_id, adaptive_status)
                if (expanded.get("audit") or {}).get("deadline_reached"):
                    # Collected evidence is already durable; hand the rest of the
                    # pipeline to a fresh invocation instead of dying at the wall —
                    # but only a bounded number of times at the SAME state.
                    cont = self.store.read_status(run_id)
                    guard = update_adaptive_continuation_guard(
                        cont, adaptive_progress_fingerprint(self.store, folder, cont))
                    self.store.write_status(run_id, cont)
                    if guard.get("tripped"):
                        close_adaptive_step_without_progress(self.store, run_id, guard)
                        cont = self.store.read_status(run_id)
                        cont.update({
                            "status": "queued",
                            "phase": "cleaning",
                            "fatal_error": None,
                        })
                        self.store.write_status(run_id, cont)
                        try:
                            self.store.checkpoint_run(run_id)
                        except Exception:
                            pass
                        try:
                            self._requeue_continuation(run_id)
                        except Exception:
                            pass
                        return
                    cont.update({
                        "status": "queued",
                        "phase": "cleaning",
                        "fatal_error": None,
                        "current": {
                            "source": None,
                            "code": "adaptive_continuation",
                            "message": "Worker time budget reached; the analysis continues automatically from the saved evidence",
                        },
                    })
                    self.store.write_status(run_id, cont)
                    try:
                        self.store.checkpoint_run(run_id)
                    except Exception:
                        pass
                    try:
                        self._requeue_continuation(run_id)
                    except Exception:
                        pass
                    return
        except CleaningCancelled:
            self._mark_cancelled_after_collection(run_id)
            return

        if not self._checkpoint_milestone(run_id):      # cleaning + comment layer finished
            return
        status = self.store.read_status(run_id)
        terminal = status.get("collection_status") or "succeeded"
        status.update({
            "status": "running" if settings.signalyth_ai_enabled and settings.openai_api_key else terminal,
            "phase": "ai_analysis" if settings.signalyth_ai_enabled and settings.openai_api_key else "cleaned",
            "completed_at": None if settings.signalyth_ai_enabled and settings.openai_api_key else _utcnow(),
            "cleaning": {
                "status": "succeeded",
                "ruleset_version": report.get("ruleset_version"),
                "started_at": status.get("cleaning", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": report,
            },
            "current": {
                "source": None,
                "code": "starting_ai_analysis" if settings.signalyth_ai_enabled and settings.openai_api_key else "cleaning_completed",
                "message": "Starting AI semantic analysis" if settings.signalyth_ai_enabled and settings.openai_api_key else "Cleaning and relevance checks completed",
            },
        })
        status.setdefault("progress", {})["percent"] = 90 if settings.signalyth_ai_enabled and settings.openai_api_key else 100
        self.store.write_status(run_id, status)
        if settings.signalyth_ai_enabled and settings.openai_api_key:
            self._run_ai_analysis(run_id, folder, plan, terminal)

    def _run_ai_analysis(self, run_id: str, folder: Path, plan: dict, terminal_status: str):
        if not self._lease_ok(run_id):
            return  # superseded by a newer worker invocation
        if self.store.cancel_requested_folder(folder):
            self._mark_cancelled_after_collection(run_id)
            return
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "ai_analysis",
            "analysis": {
                "status": "running",
                "ruleset_version": "0.9.0",
                "prompt_version": "signalyth-semantic-v0.9",
                "started_at": _utcnow(),
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "current": {"source": None, "code": "ai_analysis", "message": "Semantic relevance, sentiment, emotion, topic, narrative and sarcasm analysis"},
        })
        status.setdefault("progress", {})["percent"] = 92
        self.store.write_status(run_id, status)
        try:
            report = analyze_run(
                folder,
                plan=plan,
                cancel_check=lambda: self.store.cancel_requested_folder(folder),
                deadline_check=self._analysis_deadline_check,
            )
        except AIAnalysisCancelled:
            self._mark_cancelled_after_collection(run_id)
            return
        except AIAnalysisProviderOutage as exc:
            # Every OpenAI call failed — say the TRUE cause on the run card and stop
            # cleanly. All collected/cleaned evidence stays saved; a Resume retries
            # the analysis for free once the OpenAI account issue is fixed.
            status = self.store.read_status(run_id)
            status.update({
                "status": "failed", "phase": "ai_analysis_failed", "completed_at": _utcnow(),
                "fatal_error": f"OpenAI analysis failed for every batch: {exc}",
                "analysis": {**(status.get("analysis") or {}), "status": "failed", "error": str(exc)},
                "current": {"source": None, "code": "ai_provider_outage",
                             "message": f"Η ανάλυση OpenAI απέτυχε για ΟΛΑ τα πακέτα — αιτία: {str(exc)[:220]}. Συνήθης λόγος: εξαντλημένη πίστωση ή άκυρο OPENAI_API_KEY. Διόρθωσε το OpenAI account και πάτα Συνέχιση."},
            })
            self.store.write_status(run_id, status)
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                pass
            return
        except AIAnalysisTimeBudgetExceeded as exc:
            # Not a failure: every completed OpenAI batch is already durably cached.
            # Hand the run to a fresh invocation that resumes exactly where we stopped.
            status = self.store.read_status(run_id)
            status.update({
                "status": "queued",
                "phase": "ai_analysis",
                "fatal_error": None,
                "current": {
                    "source": None,
                    "code": "ai_analysis_continuation",
                    "message": "Worker time budget reached; analysis will continue automatically from the last saved batch",
                },
                "analysis": {
                    **(status.get("analysis") or {}),
                    "status": "running",
                    "error": None,
                },
            })
            self.store.write_status(run_id, status)
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                pass
            try:
                self._requeue_continuation(run_id)
            except Exception as requeue_exc:
                status = self.store.read_status(run_id)
                status.update({
                    "status": "queued",
                    "current": {
                        "source": None,
                        "code": "continuation_requeue_failed",
                        "message": f"Automatic continuation could not be enqueued ({requeue_exc}); press Start to resume safely",
                    },
                })
                self.store.write_status(run_id, status)
            return
        except Exception as exc:
            status = self.store.read_status(run_id)
            status.update({
                "status": "failed",
                "phase": "ai_analysis_failed",
                "completed_at": _utcnow(),
                "fatal_error": f"AI analysis failed safely: {exc}",
                "analysis": {
                    **(status.get("analysis") or {}),
                    "status": "failed",
                    "completed_at": _utcnow(),
                    "error": str(exc),
                },
                "current": {"source": None, "code": "ai_analysis_failed", "message": "AI analysis failed safely; collected and cleaned evidence was preserved"},
            })
            status.setdefault("progress", {})["percent"] = 100
            self.store.write_status(run_id, status)
            return

        # Master30: if semantic relevance leaves a per-source analyzable shortfall,
        # do one bounded source-specific refill pass, then re-clean/re-analyze. OpenAI
        # cache prevents re-paying unchanged records.
        refill_already_attempted = (folder / "semantic-refill.json").exists()
        if plan.get("master_spec_version") == "SIGNALYTH-master30-v1" and not refill_already_attempted:
            try:
                refill = semantic_refill(folder, plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
                if int(refill.get("added_normalized", 0) or 0) > 0:
                    clean_run(folder, plan=plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
                    report = analyze_run(
                        folder,
                        plan=plan,
                        cancel_check=lambda: self.store.cancel_requested_folder(folder),
                        force=True,
                        deadline_check=self._analysis_deadline_check,
                    )
                status_refill = self.store.read_status(run_id)
                status_refill["semantic_refill"] = {"status": refill.get("status"), "summary": refill, "completed_at": _utcnow()}
                self.store.write_status(run_id, status_refill)
            except AIAnalysisTimeBudgetExceeded:
                # The refill re-analysis ran out of invocation time. All paid batches
                # are cached durably; requeue and let the continuation finish it.
                status_refill = self.store.read_status(run_id)
                status_refill.update({
                    "status": "queued",
                    "phase": "ai_analysis",
                    "current": {"source": None, "code": "ai_analysis_continuation", "message": "Refill analysis will continue automatically from the last saved batch"},
                })
                self.store.write_status(run_id, status_refill)
                try:
                    self.store.checkpoint_run(run_id)
                except Exception:
                    pass
                try:
                    self._requeue_continuation(run_id)
                except Exception:
                    pass
                return
            except Exception as exc:
                # Refill is quality-improving and bounded; a provider failure must not
                # erase a valid first-pass analysis.
                status_refill = self.store.read_status(run_id)
                status_refill["semantic_refill"] = {"status":"failed_safe","error":str(exc),"completed_at":_utcnow()}
                self.store.write_status(run_id, status_refill)

        if not self._checkpoint_milestone(run_id):      # AI analysis finished
            return
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "intelligence",
            "completed_at": None,
            "analysis": {
                "status": "succeeded",
                "ruleset_version": report.get("ruleset_version"),
                "prompt_version": report.get("prompt_version"),
                "started_at": status.get("analysis", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": report,
            },
            "intelligence": {
                "status": "running",
                "ruleset_version": "1.0.0",
                "methodology_version": "signalyth-intelligence-v1",
                "started_at": _utcnow(),
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "current": {"source": None, "code": "intelligence_engine", "message": "Computing deterministic reputation, impact, drivers and trends"},
        })
        if not self._lease_ok(run_id):
            return  # superseded by a newer worker invocation
        status.setdefault("progress", {})["percent"] = 96
        self.store.write_status(run_id, status)
        if not (folder / "analysis" / "analysis-ready.json").exists():
            # The analysis finished on a worker that was replaced before its
            # files reached the archive (run 20260925T094032Z-0e94d435). The
            # OpenAI batch cache is durable, so re-running the analysis is
            # cheap; failing the run here threw away a complete collection.
            rebuilds = int(status.get("analysis_rebuild_attempts") or 0)
            if rebuilds < 1:
                status = self.store.read_status(run_id)
                status.update({
                    "status": "queued",
                    "phase": "cleaning",
                    "fatal_error": None,
                    "analysis_rebuild_attempts": rebuilds + 1,
                    "current": {"source": None, "code": "analysis_rebuild",
                                "message": "Analysis files were lost in a worker hand-off; rebuilding the analysis from the saved evidence"},
                })
                self.store.write_status(run_id, status)
                try:
                    self.store.checkpoint_run(run_id)
                except Exception:
                    pass
                try:
                    self._requeue_continuation(run_id)
                except Exception:
                    pass
                return
        try:
            intelligence = build_intelligence(folder, plan=plan)
        except Exception as exc:
            status = self.store.read_status(run_id)
            status.update({
                "status": "failed",
                "phase": "intelligence_failed",
                "completed_at": _utcnow(),
                "fatal_error": f"Intelligence aggregation failed safely: {exc}",
                "intelligence": {
                    **(status.get("intelligence") or {}),
                    "status": "failed",
                    "completed_at": _utcnow(),
                    "error": str(exc),
                },
                "current": {"source": None, "code": "intelligence_failed", "message": "Deterministic intelligence aggregation failed safely; evidence was preserved"},
            })
            status.setdefault("progress", {})["percent"] = 100
            self.store.write_status(run_id, status)
            return

        if not self._checkpoint_milestone(run_id):      # intelligence finished
            return
        if self.store.cancel_requested_folder(folder):
            self._mark_cancelled_after_collection(run_id)
            return

        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "investigations",
            "completed_at": None,
            "intelligence": {
                "status": "succeeded",
                "ruleset_version": intelligence.get("ruleset_version"),
                "methodology_version": intelligence.get("methodology_version"),
                "started_at": status.get("intelligence", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": intelligence,
            },
            "investigations": {
                "status": "running",
                "ruleset_version": "1.1.0",
                "methodology_version": "signalyth-investigations-v1",
                "evidence_contract_version": "signalyth-evidence-pack-v1.1",
                "started_at": _utcnow(),
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "current": {"source": None, "code": "automatic_investigations", "message": "Investigating meaningful changes, drivers, divergences, coordination and quality signals"},
        })
        if not self._lease_ok(run_id):
            return  # superseded by a newer worker invocation
        status.setdefault("progress", {})["percent"] = 98
        self.store.write_status(run_id, status)
        try:
            investigations = build_investigations(
                folder,
                plan=plan,
                cancel_check=lambda: self.store.cancel_requested_folder(folder),
            )
        except InvestigationCancelled:
            self._mark_cancelled_after_collection(run_id)
            return
        except Exception as exc:
            status = self.store.read_status(run_id)
            status.update({
                "status": "failed",
                "phase": "investigations_failed",
                "completed_at": _utcnow(),
                "fatal_error": f"Automatic investigations failed safely: {exc}",
                "investigations": {
                    **(status.get("investigations") or {}),
                    "status": "failed",
                    "completed_at": _utcnow(),
                    "error": str(exc),
                },
                "current": {"source": None, "code": "investigations_failed", "message": "Automatic investigations failed safely; all prior evidence was preserved"},
            })
            status.setdefault("progress", {})["percent"] = 100
            self.store.write_status(run_id, status)
            return

        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "visualizations",
            "completed_at": None,
            "investigations": {
                "status": "succeeded",
                "ruleset_version": investigations.get("ruleset_version"),
                "methodology_version": investigations.get("methodology_version"),
                "evidence_contract_version": investigations.get("evidence_contract_version"),
                "started_at": status.get("investigations", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": investigations,
            },
            "visualizations": {
                "status": "running",
                "ruleset_version": "1.2.0",
                "methodology_version": "signalyth-visual-intelligence-v1",
                "visual_contract_version": "signalyth-visual-pack-v1.2",
                "started_at": _utcnow(),
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "current": {"source": None, "code": "visualization_engine", "message": "Building evidence-linked charts, dashboard and presentation visual pack"},
        })
        status.setdefault("progress", {})["percent"] = 99
        self.store.write_status(run_id, status)
        try:
            visualizations = build_visualizations(
                folder,
                plan=plan,
                cancel_check=lambda: self.store.cancel_requested_folder(folder),
            )
        except VisualizationCancelled:
            self._mark_cancelled_after_collection(run_id)
            return
        except Exception as exc:
            status = self.store.read_status(run_id)
            status.update({
                "status": "failed",
                "phase": "visualizations_failed",
                "completed_at": _utcnow(),
                "fatal_error": f"Charts & Dashboard generation failed safely: {exc}",
                "visualizations": {
                    **(status.get("visualizations") or {}),
                    "status": "failed",
                    "completed_at": _utcnow(),
                    "error": str(exc),
                },
                "current": {"source": None, "code": "visualizations_failed", "message": "Charts & Dashboard generation failed safely; all prior evidence was preserved"},
            })
            status.setdefault("progress", {})["percent"] = 100
            self.store.write_status(run_id, status)
            return

        self.store.checkpoint_run(run_id)
        status = self.store.read_status(run_id)
        status.update({
            "status": "running",
            "phase": "exports",
            "completed_at": None,
            "visualizations": {
                "status": "succeeded",
                "ruleset_version": visualizations.get("ruleset_version"),
                "methodology_version": visualizations.get("methodology_version"),
                "visual_contract_version": visualizations.get("visual_contract_version"),
                "started_at": status.get("visualizations", {}).get("started_at"),
                "completed_at": _utcnow(),
                "error": None,
                "summary": visualizations,
            },
            "exports": {"status":"running","started_at":_utcnow(),"completed_at":None,"error":None,"summary":None},
            "current": {"source": None, "code": "building_professional_report", "message": "Building evidence-grounded professional report and editable exports"},
        })
        status.setdefault("progress", {})["percent"] = 99
        self.store.write_status(run_id, status)
        prerequisites = [
            folder / "visualizations" / "presentation-visual-pack.json",
            folder / "investigations" / "evidence-pack.json",
        ]
        exports = None
        if all(p.exists() for p in prerequisites):
            try:
                exports = build_exports(folder, plan=plan, cancel_check=lambda: self.store.cancel_requested_folder(folder))
            except PresentationCancelled:
                self._mark_cancelled_after_collection(run_id); return
            except Exception as exc:
                status=self.store.read_status(run_id); status.update({"status":"failed","phase":"exports_failed","completed_at":_utcnow(),
                    "fatal_error":f"Professional report/export generation failed safely: {exc}",
                    "exports":{**(status.get("exports") or {}),"status":"failed","completed_at":_utcnow(),"error":str(exc)},
                    "current":{"source":None,"code":"exports_failed","message":"Report/export QA failed; all evidence and charts were preserved"}})
                status.setdefault("progress",{})["percent"]=100; self.store.write_status(run_id,status); return
        self.store.checkpoint_run(run_id)
        status=self.store.read_status(run_id)
        export_state = ({"status":"succeeded","completed_at":_utcnow(),"error":None,"summary":exports}
                        if exports is not None else {"status":"skipped_not_ready","completed_at":_utcnow(),"error":None,"summary":None})
        status.update({"status":terminal_status,"phase":"visualizations_ready","completed_at":_utcnow(),
            "exports":{**(status.get("exports") or {}),**export_state},
            "current":{"source":None,"code":"visualizations_completed","message":"Charts are ready" + ("; professional report exports are ready" if exports is not None else "")}})
        status.setdefault("progress",{})["percent"]=100; self.store.write_status(run_id,status)

    def _mark_cancelled_after_collection(self, run_id: str):
        status = self.store.read_status(run_id)
        status.update({
            "status": "cancelled",
            "phase": "completed",
            "cancel_requested": True,
            "completed_at": _utcnow(),
            "current": {"source": None, "code": "cancelled_during_cleaning", "message": "Cancelled after collection; raw and normalized evidence were preserved"},
        })
        if isinstance(status.get("cleaning"), dict) and status["cleaning"].get("status") == "running":
            status["cleaning"].update({"status": "cancelled", "completed_at": _utcnow()})
        if isinstance(status.get("analysis"), dict) and status["analysis"].get("status") == "running":
            status["analysis"].update({"status": "cancelled", "completed_at": _utcnow(), "error": None})
        if isinstance(status.get("intelligence"), dict) and status["intelligence"].get("status") == "running":
            status["intelligence"].update({"status": "cancelled", "completed_at": _utcnow(), "error": None})
        if isinstance(status.get("investigations"), dict) and status["investigations"].get("status") == "running":
            status["investigations"].update({"status": "cancelled", "completed_at": _utcnow(), "error": None})
        if isinstance(status.get("visualizations"), dict) and status["visualizations"].get("status") == "running":
            status["visualizations"].update({"status": "cancelled", "completed_at": _utcnow(), "error": None})
        status.setdefault("progress", {})["percent"] = 100
        self.store.write_status(run_id, status)

    def _mark_failed(self, run_id: str, message: str):
        try:
            status = self.store.read_status(run_id)
        except RunNotFound:
            return
        if status.get("status") in self.store.TERMINAL_STATUSES:
            return
        status.update({
            "status": "failed",
            "phase": "completed",
            "completed_at": _utcnow(),
            "fatal_error": message,
            "current": {"source": None, "code": "run_failed", "message": "Run failed safely"},
        })
        if isinstance(status.get("cleaning"), dict) and status["cleaning"].get("status") == "running":
            status["cleaning"].update({"status": "failed", "completed_at": _utcnow(), "error": message})
        if isinstance(status.get("analysis"), dict) and status["analysis"].get("status") == "running":
            status["analysis"].update({"status": "failed", "completed_at": _utcnow(), "error": message})
        if isinstance(status.get("intelligence"), dict) and status["intelligence"].get("status") == "running":
            status["intelligence"].update({"status": "failed", "completed_at": _utcnow(), "error": message})
        if isinstance(status.get("investigations"), dict) and status["investigations"].get("status") == "running":
            status["investigations"].update({"status": "failed", "completed_at": _utcnow(), "error": message})
        status.setdefault("progress", {})["percent"] = 100
        self.store.write_status(run_id, status)

    def _mark_cancelled_before_start(self, run_id: str):
        status = self.store.read_status(run_id)
        if str((status.get("reprocess") or {}).get("status") or "") in {"queued", "running"}:
            self._rollback_reprocess(run_id, "cancelled", None)
            return
        status.update({
            "status": "cancelled",
            "phase": "completed",
            "cancel_requested": True,
            "completed_at": _utcnow(),
            "current": {"source": None, "code": "cancelled_before_start", "message": "Cancelled before collection started"},
        })
        for source_status in status.get("sources", {}).values():
            if source_status.get("status") in {"pending", "running"}:
                source_status["status"] = "skipped_cancelled"
        progress = status.setdefault("progress", {})
        progress["percent"] = 100
        progress["completed_sources"] = progress.get("total_sources", len(status.get("sources", {})))
        self.store.write_status(run_id, status)

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            status = self.store.read_status(run_id)
            current = status.get("status")
            if current in self.store.TERMINAL_STATUSES:
                return status

            self.store.request_cancel(run_id)
            future = self._jobs.get(run_id)
            if current == "planned" or (current == "queued" and future is None):
                self._mark_cancelled_before_start(run_id)
                return self.store.read_status(run_id)

            if future is not None and future.cancel():
                self._mark_cancelled_before_start(run_id)
                return self.store.read_status(run_id)

            status = self.store.read_status(run_id)
            status.update({
                "status": "cancelling",
                "cancel_requested": True,
                "current": {
                    "source": status.get("current", {}).get("source"),
                    "code": "cancel_requested",
                    "message": "Cancellation requested; SIGNALYTH will stop after the active Actor call returns",
                },
            })
            return self.store.write_status(run_id, status)

    def is_active(self, run_id: str) -> bool:
        if settings.signalyth_execution_backend.strip().lower() == "celery":
            try:
                return self.store.read_status(run_id).get("status") in {"queued", "running", "cancelling"}
            except RunNotFound:
                return False
        with self._lock:
            future = self._jobs.get(run_id)
            return bool(future and not future.done())

    def shutdown(self, wait: bool = True):
        self.executor.shutdown(wait=wait, cancel_futures=True)
