from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from app.config import settings
from app.services.cloud_persistence import cloud_persistence

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9T:-]+$")
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class RunNotFound(KeyError):
    pass


class RunStore:
    """Small durable run store for the internal v1 app.

    Files are atomically replaced so UI polling never reads half-written JSON.
    Control signals (for example cancellation) live in control.json so collector
    status updates cannot overwrite them accidentally.
    """

    TERMINAL_STATUSES = {
        "cancelled",
        "succeeded",
        "completed_shortfall",
        "completed_with_errors",
        "failed",
        "interrupted",
    }

    def __init__(self):
        self.root = Path(settings.signalyth_data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cloud = cloud_persistence

    def folder_for(self, run_id: str) -> Path:
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        folder = self.root / "runs" / run_id
        if not folder.is_dir() and self.cloud.enabled:
            # Prefer the complete snapshot; a newly planned run may only have metadata.
            if not self.cloud.restore_run_archive(run_id, folder):
                self.cloud.restore_run_metadata(run_id, folder)
        if not folder.is_dir():
            raise RunNotFound(run_id)
        return folder

    def create(self, plan: dict) -> tuple[str, Path]:
        self.cloud.require()
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        folder = self.root / "runs" / run_id
        folder.mkdir(parents=True, exist_ok=False)
        self.write(folder / "plan.json", plan)

        source_rows = {}
        total_subruns = 0
        for sp in plan.get("sources", []):
            source = sp.get("source")
            if not source:
                continue
            subruns = sp.get("subruns", []) or []
            total_subruns += len(subruns)
            source_rows[source] = {
                "status": "pending",
                "base_target": int(sp.get("target_items", 0) or 0),
                "adjusted_target": int(sp.get("target_items", 0) or 0),
                "collected": 0,
                "cost_usd": 0.0,
                "subruns_total": len(subruns),
                "subruns_completed": 0,
                "error": None,
            }

        now = _utcnow()
        status = {
            "run_id": run_id,
            "status": "planned",
            "phase": "planned",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "sample_mode": plan.get("sample_mode", "automatic"),
            "sample_target": int(plan.get("target_total", 0) or 0),
            "normalized_total": 0,
            "sample_shortfall": int(plan.get("target_total", 0) or 0),
            "sample_status": "pending",
            "progress": {
                "percent": 0,
                "completed_sources": 0,
                "total_sources": len(source_rows),
                "completed_subruns": 0,
                "total_subruns": total_subruns,
            },
            "current": {
                "source": None,
                "code": "ready_to_start",
                "message": "Ready to start",
            },
            "budget": {
                "max_usd": float(plan.get("max_budget_usd", 0) or 0),
                "spent_usd": 0.0,
                "remaining_usd": float(plan.get("max_budget_usd", 0) or 0),
            },
            "sources": source_rows,
            "cleaning": {
                "status": "pending",
                "ruleset_version": "0.8.0",
                "started_at": None,
                "completed_at": None,
                "error": None,
            },
            "analysis": {
                "status": "pending",
                "ruleset_version": "0.9.0",
                "prompt_version": "signalyth-semantic-v0.9",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "intelligence": {
                "status": "pending",
                "ruleset_version": "1.0.0",
                "methodology_version": "signalyth-intelligence-v1",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "investigations": {
                "status": "pending",
                "ruleset_version": "1.1.0",
                "methodology_version": "signalyth-investigations-v1",
                "evidence_contract_version": "signalyth-evidence-pack-v1.1",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "visualizations": {
                "status": "pending",
                "ruleset_version": "1.2.0",
                "methodology_version": "signalyth-visual-intelligence-v1",
                "visual_contract_version": "signalyth-visual-pack-v1.2",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "exports": {
                "status": "pending",
                "ruleset_version": "1.3.0",
                "methodology_version": "signalyth-presentation-intelligence-v1",
                "presentation_contract_version": "signalyth-presentation-pack-v1.3",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "summary": None,
            },
            "fatal_error": None,
            "cancel_requested": False,
        }
        self.write(folder / "status.json", status)
        control = {"cancel_requested": False, "requested_at": None}
        self.write(folder / "control.json", control)
        if self.cloud.enabled:
            self.cloud.put_json(run_id, "plan.json", plan)
            self.cloud.put_json(run_id, "status.json", status)
            self.cloud.put_json(run_id, "control.json", control)
        return run_id, folder

    @staticmethod
    def write(path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = _path_lock(path)
        with lock:
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2, default=str)
                tmp.flush()
                temp_path = Path(tmp.name)
            temp_path.replace(path)

    @staticmethod
    def read(path: Path, default=None):
        lock = _path_lock(path)
        with lock:
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8"))

    def read_plan(self, run_id: str) -> dict:
        folder = self.folder_for(run_id)
        payload = self.cloud.get_json(run_id, "plan.json") if self.cloud.enabled else None
        if isinstance(payload, dict):
            self.write(folder / "plan.json", payload)
        else:
            payload = self.read(folder / "plan.json")
        if not isinstance(payload, dict):
            raise RunNotFound(run_id)
        return payload

    def read_status(self, run_id: str) -> dict:
        folder = self.folder_for(run_id)
        payload = self.cloud.get_json(run_id, "status.json") if self.cloud.enabled else None
        if isinstance(payload, dict):
            self.write(folder / "status.json", payload)
        else:
            payload = self.read(folder / "status.json")
        if not isinstance(payload, dict):
            raise RunNotFound(run_id)
        return payload

    def write_status(self, run_id: str, payload: dict) -> dict:
        folder = self.folder_for(run_id)
        payload = dict(payload)
        payload["run_id"] = run_id
        payload["updated_at"] = _utcnow()
        self.write(folder / "status.json", payload)
        self.cloud.put_json(run_id, "status.json", payload)
        return payload

    def write_status_folder(self, folder: Path, payload: dict) -> dict:
        payload = dict(payload)
        payload["updated_at"] = _utcnow()
        self.write(folder / "status.json", payload)
        run_id = str(payload.get("run_id") or folder.name)
        if _RUN_ID_RE.match(run_id):
            self.cloud.put_json(run_id, "status.json", payload)
        return payload

    def read_control(self, run_id: str) -> dict:
        folder = self.folder_for(run_id)
        payload = self.cloud.get_json(run_id, "control.json") if self.cloud.enabled else None
        if isinstance(payload, dict):
            self.write(folder / "control.json", payload)
            return payload
        return self.read(folder / "control.json", {"cancel_requested": False, "requested_at": None}) or {}

    def read_control_folder(self, folder: Path) -> dict:
        run_id = folder.name
        payload = self.cloud.get_json(run_id, "control.json") if self.cloud.enabled else None
        if isinstance(payload, dict):
            self.write(folder / "control.json", payload)
            return payload
        return self.read(folder / "control.json", {"cancel_requested": False, "requested_at": None}) or {}

    def reset_control(self, run_id: str):
        folder = self.folder_for(run_id)
        payload = {"cancel_requested": False, "requested_at": None}
        self.write(folder / "control.json", payload)
        self.cloud.put_json(run_id, "control.json", payload)

    def request_cancel(self, run_id: str) -> dict:
        folder = self.folder_for(run_id)
        payload = {"cancel_requested": True, "requested_at": _utcnow()}
        self.write(folder / "control.json", payload)
        self.cloud.put_json(run_id, "control.json", payload)
        return payload

    def cancel_requested_folder(self, folder: Path) -> bool:
        return bool(self.read_control_folder(folder).get("cancel_requested"))

    def checkpoint_run(self, run_id: str) -> None:
        """Persist the complete run workspace after an expensive pipeline boundary."""
        if not self.cloud.enabled:
            return
        folder = self.folder_for(run_id)
        self.cloud.persist_run_archive(run_id, folder)

    def get_run(self, run_id: str) -> dict:
        status = self.read_status(run_id)
        plan = self.read_plan(run_id)
        return {
            **status,
            "client": plan.get("client"),
            "topic": plan.get("topic"),
            "market": plan.get("market"),
            "date_from": plan.get("date_from"),
            "date_to": plan.get("date_to"),
            "target_total": plan.get("target_total"),
            "max_budget_usd": plan.get("max_budget_usd"),
        }

    def list_runs(self):
        if self.cloud.enabled:
            rows = []
            for run_id in self.cloud.list_run_ids(limit=100):
                try:
                    status = self.cloud.get_json(run_id, "status.json")
                    plan = self.cloud.get_json(run_id, "plan.json")
                    if not isinstance(status, dict) or not isinstance(plan, dict):
                        continue
                    rows.append({
                        **status,
                        "client": plan.get("client"),
                        "topic": plan.get("topic"),
                        "market": plan.get("market"),
                        "date_from": plan.get("date_from"),
                        "date_to": plan.get("date_to"),
                        "target_total": plan.get("target_total"),
                        "max_budget_usd": plan.get("max_budget_usd"),
                    })
                except Exception:
                    continue
            if rows:
                return rows[:100]

        base = self.root / "runs"
        if not base.exists():
            return []
        rows = []
        for folder in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
            try:
                status = self.read(folder / "status.json")
                plan = self.read(folder / "plan.json")
                if not isinstance(status, dict) or not isinstance(plan, dict):
                    continue
                rows.append({
                    **status,
                    "client": plan.get("client"),
                    "topic": plan.get("topic"),
                    "market": plan.get("market"),
                    "date_from": plan.get("date_from"),
                    "date_to": plan.get("date_to"),
                    "target_total": plan.get("target_total"),
                    "max_budget_usd": plan.get("max_budget_usd"),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return rows[:100]

    def recover_interrupted_runs(self) -> list[str]:
        """Mark jobs left active by a previous process as interrupted.

        The current internal executor is intentionally single-process. After an app restart,
        a queued/running job cannot safely be assumed alive, so it is surfaced
        explicitly instead of displaying a permanent spinner.
        """
        if settings.signalyth_execution_backend == "celery":
            return []
        recovered = []
        for row in self.list_runs():
            if row.get("status") not in {"queued", "running", "cancelling"}:
                continue
            run_id = row["run_id"]
            status = self.read_status(run_id)
            status.update({
                "status": "interrupted",
                "phase": "completed",
                "completed_at": _utcnow(),
                "fatal_error": "The application restarted while this run was active. The run was stopped safely; start a new analysis before spending again.",
                "cancel_requested": False,
                "current": {"source": None, "code": "interrupted_restart", "message": "Interrupted by application restart"},
            })
            for source_status in status.get("sources", {}).values():
                if source_status.get("status") == "running":
                    source_status["status"] = "interrupted"
                    source_status["error"] = "Application restart interrupted this source."
                elif source_status.get("status") == "pending":
                    source_status["status"] = "skipped_interrupted"
            if isinstance(status.get("cleaning"), dict) and status["cleaning"].get("status") == "running":
                status["cleaning"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted cleaning."})
            if isinstance(status.get("analysis"), dict) and status["analysis"].get("status") == "running":
                status["analysis"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted AI analysis."})
            if isinstance(status.get("intelligence"), dict) and status["intelligence"].get("status") == "running":
                status["intelligence"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted deterministic intelligence aggregation."})
            if isinstance(status.get("investigations"), dict) and status["investigations"].get("status") == "running":
                status["investigations"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted automatic investigations."})
            if isinstance(status.get("visualizations"), dict) and status["visualizations"].get("status") == "running":
                status["visualizations"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted Charts & Dashboard generation."})
            if isinstance(status.get("exports"), dict) and status["exports"].get("status") == "running":
                status["exports"].update({"status": "interrupted", "completed_at": _utcnow(), "error": "Application restart interrupted presentation/export generation."})
            progress = status.setdefault("progress", {})
            progress["percent"] = 100
            progress["completed_sources"] = progress.get("total_sources", len(status.get("sources", {})))
            self.write_status(run_id, status)
            recovered.append(run_id)
        return recovered
