from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


PROCESSED_DIRS = (
    "cleaning",
    "analysis",
    "intelligence",
    "investigations",
    "visualizations",
    "exports",
)
BACKUP_DIRNAME = ".reprocess-backup"


class ReprocessSafetyError(RuntimeError):
    """Raised before reprocessing when preserving the existing run cannot be guaranteed."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_dir(folder: Path) -> Path:
    return folder / BACKUP_DIRNAME


def backup_exists(folder: Path) -> bool:
    return backup_dir(folder).is_dir()


def _read_json(path: Path, default):
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _manual_review_reasons(folder: Path) -> list[str]:
    reasons: list[str] = []

    cleaned = _read_json(folder / "cleaning" / "cleaned.json", [])
    if isinstance(cleaned, list):
        if any(
            isinstance(row, dict)
            and isinstance(row.get("cleaning"), dict)
            and (
                row["cleaning"].get("human_override")
                or row["cleaning"].get("human_review_history")
            )
            for row in cleaned
        ):
            reasons.append("Step 3 cleaning overrides/history")

    analyzed = _read_json(folder / "analysis" / "analyzed.json", [])
    if isinstance(analyzed, list):
        if any(
            isinstance(row, dict)
            and isinstance(row.get("ai_analysis"), dict)
            and (
                row["ai_analysis"].get("human_override")
                or row["ai_analysis"].get("human_review_history")
                or row["ai_analysis"].get("human_review")
            )
            for row in analyzed
        ):
            reasons.append("Step 4 AI review overrides/history")

    human_review = _read_json(folder / "analysis" / "human-review.json", {})
    if isinstance(human_review, dict) and (human_review.get("decisions") or {}):
        reasons.append("Review Queue decisions")

    return reasons


def assert_safe_to_reprocess(folder: Path) -> None:
    normalized = folder / "normalized-all.json"
    if not normalized.is_file():
        raise ReprocessSafetyError(
            "Full reprocess needs the saved normalized collection (normalized-all.json). "
            "No collection will be started automatically."
        )

    if not (folder / "exports").is_dir():
        raise ReprocessSafetyError(
            "Full reprocess is only available for a run that already has report exports."
        )

    manual = _manual_review_reasons(folder)
    if manual:
        raise ReprocessSafetyError(
            "Full reprocess stopped before changing anything because this run contains "
            "manual review work that the current cleaning/AI rebuild would overwrite: "
            + ", ".join(manual)
            + ". Preserve/migrate those decisions explicitly before reprocessing."
        )


def read_backup_status(folder: Path) -> dict | None:
    payload = _read_json(backup_dir(folder) / "status.json", None)
    return payload if isinstance(payload, dict) else None


def normalized_hash_from_backup(folder: Path) -> str | None:
    meta = _read_json(backup_dir(folder) / "meta.json", {})
    return (str(meta.get("normalized_sha256") or "") or None) if isinstance(meta, dict) else None


def create_backup(folder: Path) -> dict:
    """Snapshot only mutable downstream state; raw/normalized evidence is never changed."""
    target = backup_dir(folder)
    temp = folder / f"{BACKUP_DIRNAME}.tmp"
    if target.exists():
        raise ReprocessSafetyError(
            "A previous full-reprocess safety backup still exists. "
            "The run must be recovered before another reprocess starts."
        )

    assert_safe_to_reprocess(folder)
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=False)

    try:
        for name in PROCESSED_DIRS:
            src = folder / name
            if src.is_dir():
                shutil.copytree(src, temp / name)

        for name in ("status.json", "control.json"):
            src = folder / name
            if src.is_file():
                shutil.copy2(src, temp / name)

        meta = {
            "created_at": _utcnow(),
            "normalized_sha256": _sha256(folder / "normalized-all.json"),
            "processed_dirs": list(PROCESSED_DIRS),
        }
        (temp / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(target)
        return meta
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def assert_normalized_unchanged(folder: Path) -> None:
    expected = normalized_hash_from_backup(folder)
    actual = _sha256(folder / "normalized-all.json")
    if expected != actual:
        raise ReprocessSafetyError(
            "Safety invariant failed: normalized-all.json changed during full reprocess. "
            "The old processed state will be restored."
        )


def restore_backup(folder: Path) -> dict:
    """Restore the exact downstream state captured before reprocessing."""
    source = backup_dir(folder)
    if not source.is_dir():
        raise ReprocessSafetyError("Full-reprocess backup is missing; automatic rollback is not possible.")

    previous_status = read_backup_status(folder) or {}

    for name in PROCESSED_DIRS:
        dst = folder / name
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        src = source / name
        if src.is_dir():
            shutil.copytree(src, dst)

    return previous_status


def discard_backup(folder: Path) -> None:
    shutil.rmtree(backup_dir(folder), ignore_errors=True)


def recover_stale_backup(folder: Path, current_status: dict) -> dict:
    """Heal a backup left by an interrupted/stale process before a new request starts.

    The caller (RunManager.enqueue_reprocess) has already returned early for a live,
    non-stale worker. Therefore a remaining queued/running backup here is an orphan
    and the safest action is to restore the exact pre-reprocess downstream state.
    """
    if not backup_exists(folder):
        return current_status

    rp = current_status.get("reprocess") if isinstance(current_status, dict) else {}
    rp_status = str((rp or {}).get("status") or "")

    if rp_status in {"succeeded", "failed", "cancelled"}:
        discard_backup(folder)
        return current_status

    previous = restore_backup(folder)
    discard_backup(folder)
    return previous or current_status
