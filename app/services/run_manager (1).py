from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.services.apify_service import CollectionNotConfigured
from app.services.collector import CollectionTimeBudgetExceeded, execute_plan
from app.services.cleaning import CleaningCancelled, clean_run
from app.services.relevance_expansion import adaptive_expand_after_cleaning
from app.services.ai_analysis import AIAnalysisCancelled, AIAnalysisTimeBudgetExceeded, analyze_run
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
        # Monotonic timestamp after which the current invocation must stop doing new
        # paid work, checkpoint and requeue a continuation. None disables the guard.
        self._deadline_monotonic: float | None = None

    def set_invocation_deadline(self, seconds: float | None) -> None:
        if seconds and seconds > 0:
            self._deadline_monotonic = time.monotonic() + float(seconds)
        else:
            self._deadline_monotonic = None

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
            if current in self.store.TERMINAL_STATUSES:
                raise RunStateError(f"Run is already terminal: {current}")
            if current not in {"planned", "queued"}:
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

    def run_now(self, run_id: str) -> None:
        """Execute one run in the current process (used by the Vercel Celery subscriber)."""
        self.set_invocation_deadline(settings.signalyth_worker_soft_deadline_seconds)
        self._worker(run_id)

    def _worker(self, run_id: str):
        try:
            folder = self.store.folder_for(run_id)
            if self.store.cancel_requested_folder(folder):
                self._mark_cancelled_before_start(run_id)
                return
            plan = self.store.read_plan(run_id)

            # Durable resume: if an earlier invocation already finished collection and
            # cleaning, do NOT re-run paid collection. Jump straight to the analysis
            # chain, which itself resumes cheaply via the per-batch OpenAI cache.
            cleaning_done = (folder / "cleaning" / "semantic-candidates.json").exists() or (
                folder / "cleaning" / "trusted.json"
            ).exists()
            prior_status = self.store.read_status(run_id)
            resumable = prior_status.get("status") not in self.store.TERMINAL_STATUSES
            if cleaning_done and resumable:
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
            self.store.checkpoint_run(run_id)
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
            try:
                self.store.checkpoint_run(run_id)
            except Exception:
                # Status/metadata writes are persisted independently; do not hide the
                # original pipeline outcome if only the final archive snapshot fails.
                pass
            with self._lock:
                self._jobs.pop(run_id, None)

    def _run_cleaning(self, run_id: str, folder: Path, plan: dict):
        if self.store.cancel_requested_folder(folder):
            self._mark_cancelled_after_collection(run_id)
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
            if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") in {"smart-collection-v2", "master30-search-v1"}:
                adaptive_status = self.store.read_status(run_id)
                adaptive_status.update({
                    "status": "running",
                    "phase": "adaptive_collection",
                    "adaptive_collection": {"status": "running", "strategy_version": "smart-collection-v2", "started_at": _utcnow(), "completed_at": None},
                    "current": {"source": "x", "code": "adaptive_collection", "message": "Diversifying relevant evidence and deepening useful conversations"},
                })
                adaptive_status.setdefault("progress", {})["percent"] = 89
                self.store.write_status(run_id, adaptive_status)
                expanded = adaptive_expand_after_cleaning(
                    folder,
                    plan=plan,
                    initial_report=report,
                    cancel_check=lambda: self.store.cancel_requested_folder(folder),
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
        except CleaningCancelled:
            self._mark_cancelled_after_collection(run_id)
            return

        self.store.checkpoint_run(run_id)
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

        self.store.checkpoint_run(run_id)
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
        status.setdefault("progress", {})["percent"] = 96
        self.store.write_status(run_id, status)
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

        self.store.checkpoint_run(run_id)
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
