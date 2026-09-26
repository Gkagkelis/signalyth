from __future__ import annotations

import json
import shutil
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from app.config import settings
from app.services.cloud_persistence import cloud_persistence
from app.services.scratch import ensure_free_space, is_no_space_error

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
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Cold start on a warm instance whose /tmp is already full: clean
            # first, then create. Without this the module fails at import and
            # every endpoint answers 500.
            if not is_no_space_error(exc):
                raise
            ensure_free_space(self.root, aggressive=True)
            self.root.mkdir(parents=True, exist_ok=True)
        self.cloud = cloud_persistence

    _refresh_checked: dict[str, float] = {}
    _refresh_lock = threading.Lock()

    def folder_for(self, run_id: str, allow_restore: bool = True) -> Path:
        """Local workspace of a run, restored from the mirror when missing.

        `allow_restore=False` is for read-only listing paths. Restoring pulls the
        COMPLETE archive — exports, raw datasets, everything — so a screen that
        touches every run would silently re-download every archive and refill the
        scratch disk seconds after the operator cleared it.
        """
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        folder = self.root / "runs" / run_id
        if not folder.is_dir() and self.cloud.enabled and allow_restore:
            # Prefer the complete snapshot; a newly planned run may only have metadata.
            if not self.cloud.restore_run_archive(run_id, folder):
                self.cloud.restore_run_metadata(run_id, folder)
                # Metadata is plan + status: NO evidence. For a run that has
                # already collected, continuing from here means analysing an
                # empty workspace and publishing a report built on nothing.
                # Leave a mark so the pipeline can refuse instead.
                try:
                    if folder.is_dir():
                        (folder / ".signalyth-metadata-only").write_text(
                            _utcnow(), encoding="utf-8")
                except Exception:
                    pass
        elif folder.is_dir() and self.cloud.enabled:
            # A warm serverless instance may hold a mid-run copy restored earlier.
            # When the durable archive has since moved forward, replace the local
            # copy so status, evidence pack and exports never disagree.
            self._refresh_if_outdated(run_id, folder)
        if not folder.is_dir():
            raise RunNotFound(run_id)
        return folder

    def evidence_intact(self, run_id: str) -> tuple[bool, str]:
        """Does the workspace still hold the evidence the run says it collected?

        A serverless worker can be replaced at any moment. When the replacement
        cannot restore the run archive it falls back to plan + status only — and
        the pipeline used to carry on, analyse the handful of rows that happened
        to be there and call it a finished report. 135 collected records became
        4 analysed ones, with nothing anywhere saying so.

        Returns (ok, reason). A failing check must stop the run, not shrink it.
        """
        try:
            # Go through the normal restore path: this is exactly what the
            # replacement worker would get, including the metadata-only fallback.
            folder = self.folder_for(run_id)
        except Exception:
            return False, "the run workspace could not be restored on this machine"
        try:
            status = self.read(folder / "status.json", {}) or {}
        except Exception:
            status = {}
        claimed = int(status.get("normalized_total") or 0)
        # normalized_total is only written when collection finalizes. A worker
        # replaced MID-collection leaves it at 0 while per-source rows already
        # say money was spent — and a metadata-only restore of that state used
        # to look like "nothing collected yet", so the replacement silently
        # re-collected (and re-paid) every source. Count both signals.
        collected_by_sources = 0
        for row in (status.get("sources") or {}).values():
            if isinstance(row, dict):
                try:
                    collected_by_sources += max(0, int(row.get("collected") or 0))
                except Exception:
                    continue
        if claimed <= 0 and collected_by_sources <= 0:
            return True, "nothing collected yet"
        present = len(self.read(folder / "normalized-all.json", []) or [])
        if (folder / ".signalyth-metadata-only").exists() and present <= 0:
            shown = claimed or collected_by_sources
            return False, (f"the run collected {shown} records but only its settings could be "
                           "restored here — the evidence itself is not reachable")
        if claimed <= 0:
            # Mid-collection archive restored intact: per-source evidence files
            # are authoritative and the finalize step rebuilds normalized-all.
            return True, "mid-collection evidence restored"
        if present < claimed:
            return False, (f"the run collected {claimed} records but only {present} are in this "
                           "workspace; the rest did not survive the worker being replaced")
        return True, "ok"

    def clear_metadata_only_marker(self, folder: Path) -> None:
        """Make a validated workspace authoritative again.

        Called ONLY after evidence_intact() confirmed nothing paid is missing
        (e.g. a planned run that had not collected yet). From that point the
        local workspace is the real state and checkpoints must be allowed.
        """
        try:
            (folder / ".signalyth-metadata-only").unlink(missing_ok=True)
        except Exception:
            pass

    def _refresh_if_outdated(self, run_id: str, folder: Path) -> None:
        import time as _time
        with self._refresh_lock:
            last = self._refresh_checked.get(run_id, 0.0)
            if _time.monotonic() - last < 20.0:
                return
            self._refresh_checked[run_id] = _time.monotonic()
        try:
            meta = self.cloud.get_archive_meta(run_id)
            remote = str((meta or {}).get("checkpointed_at") or "")
            stamp_file = folder / ".signalyth-archive-stamp"
            local = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.exists() else ""
            needs_refresh = bool(remote) and remote != local
            if not remote and not local:
                # Legacy run archived before version stamps existed. Heal a possibly
                # mid-run local copy ONCE per instance — but never clobber a copy that
                # an active worker in this process is still writing to.
                actively_written = False
                try:
                    raw = json.loads((folder / "status.json").read_text(encoding="utf-8"))
                    if str(raw.get("status")) in {"running", "cancelling", "queued"}:
                        stamp = raw.get("updated_at")
                        if stamp:
                            updated = datetime.fromisoformat(str(stamp))
                            if updated.tzinfo is None:
                                updated = updated.replace(tzinfo=timezone.utc)
                            actively_written = (datetime.now(timezone.utc) - updated).total_seconds() < 120
                except Exception:
                    pass
                if not actively_written:
                    needs_refresh = True
            if needs_refresh:
                refreshed = folder.parent / f".{run_id}.refresh"
                shutil.rmtree(refreshed, ignore_errors=True)
                ok = self.cloud.restore_run_archive(run_id, refreshed)
                if ok and refreshed.is_dir():
                    stamp_file2 = refreshed / ".signalyth-archive-stamp"
                    if not stamp_file2.exists():
                        stamp_file2.write_text("legacy-refreshed", encoding="utf-8")
                    shutil.rmtree(folder, ignore_errors=True)
                    refreshed.rename(folder)
                else:
                    shutil.rmtree(refreshed, ignore_errors=True)
        except Exception:
            # Freshness sync is best-effort; the existing local copy stays usable.
            pass

    def prune_local_scratch(self, keep_run_id: str | None = None, aggressive: bool = False) -> dict:
        """Free serverless scratch space before writing.

        Local (non-cloud) installs are never pruned — there the filesystem IS
        the store. In cloud mode every run folder is a disposable cache of the
        Blob mirror, so they can be reclaimed whenever the disk runs low.
        """
        if not self.cloud.enabled:
            return {"stage": "local_install_not_pruned", "removed": []}
        return ensure_free_space(self.root, keep_run_id=keep_run_id, aggressive=aggressive)

    def create(self, plan: dict) -> tuple[str, Path]:
        self.cloud.require()
        self.prune_local_scratch()
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
        def _attempt() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2, default=str)
                tmp.flush()
                temp_path = Path(tmp.name)
            temp_path.replace(path)

        lock = _path_lock(path)
        with lock:
            try:
                _attempt()
            except OSError as exc:
                # Last line of defence: any write anywhere in the pipeline that
                # hits a full scratch disk reclaims space and retries once,
                # instead of failing the whole run.
                if not is_no_space_error(exc):
                    raise
                ensure_free_space(settings.signalyth_data_dir, aggressive=True)
                _attempt()

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

    def delete_run(self, run_id: str) -> None:
        """Permanently remove a run's local workspace and its cloud mirror."""
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        folder = self.root / "runs" / run_id
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        self.cloud.delete_run(run_id)

    @staticmethod
    def _tree_bytes(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except Exception:
                return 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except Exception:
                continue
        return total

    def run_footprint(self, run_id: str) -> dict:
        """Local disk used by a run, so the operator can decide what to clear."""
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        folder = self.root / "runs" / run_id
        exports = folder / "exports"
        total = self._tree_bytes(folder)
        exports_bytes = self._tree_bytes(exports)
        return {
            "run_id": run_id,
            "present_locally": folder.is_dir(),
            "total_mb": round(total / 1e6, 2),
            "exports_mb": round(exports_bytes / 1e6, 2),
            "evidence_mb": round(max(0, total - exports_bytes) / 1e6, 2),
        }

    def drop_run_exports(self, run_id: str) -> dict:
        """Delete only the generated export files; evidence and status stay."""
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        folder = self.root / "runs" / run_id
        exports = folder / "exports"
        freed = self._tree_bytes(exports)
        if exports.is_dir():
            shutil.rmtree(exports, ignore_errors=True)
        return {"run_id": run_id, "exports_deleted": True, "freed_mb": round(freed / 1e6, 2)}

    def drop_local_run_files(self, run_id: str) -> dict:
        """Remove the local scratch copy of a run, keeping the durable mirror.

        In cloud mode the mirror is the source of truth and the run is restored
        from it on demand, so this frees disk without losing the analysis. On a
        local install the filesystem IS the store, so nothing is removed.
        """
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise RunNotFound(run_id)
        if not self.cloud.enabled:
            return {"run_id": run_id, "freed_mb": 0.0, "kept_in_cloud": False,
                    "note": "local install: the filesystem is the only copy"}
        folder = self.root / "runs" / run_id
        freed = self._tree_bytes(folder)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return {"run_id": run_id, "freed_mb": round(freed / 1e6, 2), "kept_in_cloud": True}

    def storage_overview(self) -> dict:
        """Every run the operator could delete, with what it costs on disk.

        The operator needs one place that answers "what is filling the disk and
        what can I remove", without reading logs or asking anyone.
        """
        rows = []
        try:
            listed = self.list_runs() or []
        except Exception:
            listed = []
        seen = set()
        for run in listed:
            run_id = str(run.get("run_id") or "").strip()
            if not run_id or run_id in seen:
                continue
            seen.add(run_id)
            folder = self.root / "runs" / run_id
            rows.append({
                "run_id": run_id,
                "client": run.get("client"),
                "topic": run.get("topic"),
                "status": run.get("status"),
                "created_at": run.get("created_at") or run.get("updated_at"),
                "present_locally": folder.is_dir(),
                "local_mb": round(self._tree_bytes(folder) / 1e6, 2),
            })
        # Folders left on disk whose status is gone are pure waste; they must be
        # listed too, or the numbers never add up for the person looking.
        runs_root = self.root / "runs"
        if runs_root.is_dir():
            for folder in runs_root.iterdir():
                if not folder.is_dir() or folder.name in seen:
                    continue
                seen.add(folder.name)
                rows.append({
                    "run_id": folder.name, "client": None, "topic": None,
                    "status": "orphan", "created_at": None,
                    "present_locally": True,
                    "local_mb": round(self._tree_bytes(folder) / 1e6, 2),
                })
        rows.sort(key=lambda r: (-float(r["local_mb"]), str(r["run_id"])), reverse=False)
        try:
            probe = self.root if Path(self.root).is_dir() else Path("/tmp")
            usage = shutil.disk_usage(str(probe))
            disk = {
                "total_mb": round(usage.total / 1e6, 1),
                "free_mb": round(usage.free / 1e6, 1),
                "used_mb": round((usage.total - usage.free) / 1e6, 1),
            }
        except Exception:
            disk = {}
        return {
            "disk": disk,
            "runs": rows,
            "run_count": len(rows),
            "runs_mb": round(sum(float(r["local_mb"]) for r in rows), 2),
            "cloud_enabled": bool(self.cloud.enabled),
        }

    def delete_all_runs(self, keep: set[str] | None = None) -> dict:
        """Remove every run, on disk and in the mirror. There is no undo."""
        keep = {str(x) for x in (keep or set())}
        deleted, failed = [], []
        targets = {str(r.get("run_id") or "") for r in (self.storage_overview().get("runs") or [])}
        for run_id in sorted(x for x in targets if x and x not in keep):
            try:
                self.delete_run(run_id)
                deleted.append(run_id)
            except Exception as exc:
                failed.append({"run_id": run_id, "error": str(exc)})
        return {"deleted": deleted, "deleted_count": len(deleted),
                "failed": failed, "kept": sorted(keep)}

    def bump_workspace_generation(self, folder: Path) -> int:
        """Mark that this run's workspace has moved on to a new milestone.

        `status.json` is written constantly; `archive.zip` only at checkpoints.
        Counting the milestones on one side and recording, on the other, which
        milestone the archive actually contains is what lets a fresh worker tell
        "my saved copy is current" from "my saved copy is a stage behind".
        """
        try:
            status = self.read(folder / "status.json", {}) or {}
            if not isinstance(status, dict):
                return 0
            nxt = int(status.get("workspace_generation") or 0) + 1
            status["workspace_generation"] = nxt
            self.write_status_folder(folder, status)
            return nxt
        except Exception:
            return 0

    def durable_state_consistent(self, folder: Path) -> tuple[bool, str]:
        """Does the saved copy actually contain the stage the status describes?

        A checkpoint that fails is swallowed at most call sites so a mirror
        hiccup cannot kill a healthy pipeline. The cost of that silence is a
        later worker restoring a stage-old archive, reading a status that
        describes a newer stage, and failing at aggregation looking for files
        that were never in the archive it holds.
        """
        try:
            status = self.read(folder / "status.json", {}) or {}
        except Exception as exc:
            return False, f"status is unreadable ({exc})"
        if not isinstance(status, dict):
            return False, "status is not a valid object"

        durability = status.get("durability")
        if not isinstance(durability, dict) or not durability:
            # Nothing has been checkpointed yet: a first invocation is not a
            # divergence, and there is nothing to be behind.
            return True, "no checkpoint has been taken yet"

        if not durability.get("saved", True):
            error = str(durability.get("error") or "the last checkpoint failed")
            return False, f"the last save of this run's workspace did not succeed ({error})"

        workspace = int(status.get("workspace_generation") or 0)
        saved = int(durability.get("generation") or 0)
        if workspace and saved < workspace:
            return False, (
                f"the saved copy is at generation {saved} while the run has "
                f"progressed to generation {workspace}, so files this status "
                "describes are not in the archive"
            )
        return True, "the saved copy matches the run's progress"

    def reconcile_normalized_total(self, folder: Path) -> int | None:
        """Make the status sample counter match the evidence actually on disk.

        A pre-v31.6 semantic refill could rewrite normalized-all smaller while
        the counter kept the larger number; ``evidence_intact`` then read an
        honest workspace as data loss and failed the run at the finish line.
        This is called ONLY where the operator explicitly rebuilds from the
        evidence that exists (full reprocess) — never before the evidence
        guard on an ordinary resume, where a shrunken workspace may be REAL
        loss that must keep stopping the run.
        """
        rows = self.read(folder / "normalized-all.json", None)
        if not isinstance(rows, list):
            return None
        try:
            status = self.read(folder / "status.json", {}) or {}
            if not isinstance(status, dict):
                return len(rows)
            if int(status.get("normalized_total") or 0) != len(rows):
                status["normalized_total"] = len(rows)
                self.write_status_folder(folder, status)
        except Exception:
            pass
        return len(rows)

    def _record_durability(self, run_id: str, **fields) -> None:
        """Write down whether this run's evidence is actually saved.

        Most checkpoint call sites are wrapped in ``try/except: pass`` so that a
        transient mirror hiccup cannot kill a healthy pipeline. That silence is
        what let a run lose every file while still reporting a successful
        analysis. Recording the outcome here means the truth survives the
        swallow: later steps and the operator can both see it.
        """
        try:
            folder = self.root / "runs" / run_id
            status = self.read(folder / "status.json", {}) or {}
            if not isinstance(status, dict):
                return
            status["durability"] = {**fields, "at": _utcnow()}
            self.write_status_folder(folder, status)
        except Exception:
            pass

    def checkpoint_run(self, run_id: str) -> None:
        """Persist the complete run workspace after an expensive pipeline boundary.

        Raises on failure ON PURPOSE. A checkpoint that quietly does nothing
        leaves the run alive with no durable evidence: the instance recycles and
        everything the operator paid for becomes unreachable, while the status
        still says the step succeeded.
        """
        if not self.cloud.enabled:
            return
        folder = self.folder_for(run_id)
        # A workspace restored as metadata-only holds SETTINGS, not evidence.
        # Archiving it would overwrite whatever the durable store has (or will
        # accept) with a copy that is empty by construction — a worker that
        # failed to restore must never become the author of the archive.
        if (folder / ".signalyth-metadata-only").exists():
            self._record_durability(
                run_id, saved=False, generation=0,
                error="refused: this worker only restored the run's settings, not its evidence")
            return
        # Which milestone this archive is about to contain. Recorded with the
        # outcome either way, so a later worker can compare it against how far
        # the status says the run has progressed.
        try:
            generation = int((self.read(folder / "status.json", {}) or {}).get("workspace_generation") or 0)
        except Exception:
            generation = 0
        try:
            stamp = self.cloud.persist_run_archive(run_id, folder)
        except Exception as exc:
            self._record_durability(run_id, saved=False, generation=generation, error=str(exc)[:400])
            raise
        self._record_durability(run_id, saved=True, generation=generation, checkpointed_at=stamp)

    def evidence_is_durable(self, run_id: str) -> tuple[bool, str]:
        """Is this run's evidence really saved? Returns (ok, reason)."""
        if not self.cloud.enabled:
            return True, "local install: the filesystem is the store"
        try:
            status = self.read_status(run_id)
        except Exception:
            status = {}
        record = status.get("durability") if isinstance(status, dict) else None
        if isinstance(record, dict) and record.get("saved") is False:
            return False, str(record.get("error") or "the run archive could not be stored")
        return True, "ok"

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
            # The run screen shows the comment layer for the whole life of a
            # run — including "off" and "waiting" — so the operator never has
            # to guess whether comments are coming.
            "comments_requested": bool(plan.get("comments_requested")),
            "comment_sources": [
                sp.get("source") for sp in (plan.get("sources") or [])
                if sp.get("source") in {"x", "tiktok", "instagram", "facebook"}
            ],
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
