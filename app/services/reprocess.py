from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.services.cloud_persistence import cloud_persistence


PROCESSED_DIRS = (
    "cleaning",
    "analysis",
    "intelligence",
    "investigations",
    "visualizations",
    "exports",
)
LEGACY_BACKUP_DIRNAME = ".reprocess-backup"
BACKUP_ARCHIVE_SUFFIX = ".reprocess-backup.zip"
BACKUP_TEMP_SUFFIX = ".reprocess-backup.tmp.zip"


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
    """Legacy v1 backup directory, kept only for safe recovery of an old attempt."""
    return folder / LEGACY_BACKUP_DIRNAME


def backup_archive(folder: Path) -> Path:
    # IMPORTANT: sibling of the run folder, never inside it. Therefore the normal
    # archive.zip cannot recursively include/duplicate the rollback payload.
    return folder.parent / f".{folder.name}{BACKUP_ARCHIVE_SUFFIX}"


def _backup_temp(folder: Path) -> Path:
    return folder.parent / f".{folder.name}{BACKUP_TEMP_SUFFIX}"


def _ensure_backup_archive(folder: Path) -> Path | None:
    target = backup_archive(folder)
    if target.is_file():
        return target
    if not cloud_persistence.enabled:
        return None
    # First ask Blob whether the object exists. Connectivity errors deliberately
    # propagate and stop preflight rather than risking a false "no backup" result.
    if not cloud_persistence.reprocess_backup_exists(folder.name):
        return None
    if not cloud_persistence.restore_reprocess_backup(folder.name, target):
        raise ReprocessSafetyError("The durable reprocess backup exists but could not be restored safely.")
    return target


def backup_exists(folder: Path) -> bool:
    if backup_dir(folder).is_dir():
        return True
    return _ensure_backup_archive(folder) is not None


def _read_json(path: Path, default):
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_archive_json(folder: Path, name: str, default):
    legacy = backup_dir(folder) / name
    if legacy.is_file():
        return _read_json(legacy, default)
    archive = _ensure_backup_archive(folder)
    if archive is None:
        return default
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            return json.loads(zf.read(name).decode("utf-8"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile):
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
    payload = _read_archive_json(folder, "status.json", None)
    return payload if isinstance(payload, dict) else None


def normalized_hash_from_backup(folder: Path) -> str | None:
    meta = _read_archive_json(folder, "meta.json", {})
    return (str(meta.get("normalized_sha256") or "") or None) if isinstance(meta, dict) else None


def create_backup(folder: Path) -> dict:
    """Create a compact rollback ZIP and persist it separately from archive.zip."""
    target = backup_archive(folder)
    temp = _backup_temp(folder)
    if backup_exists(folder):
        raise ReprocessSafetyError(
            "A previous full-reprocess safety backup still exists. "
            "The run must be recovered before another reprocess starts."
        )

    assert_safe_to_reprocess(folder)
    target.unlink(missing_ok=True)
    temp.unlink(missing_ok=True)

    meta = {
        "created_at": _utcnow(),
        "normalized_sha256": _sha256(folder / "normalized-all.json"),
        "processed_dirs": list(PROCESSED_DIRS),
        "storage": "separate-zip-v2",
    }

    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for name in PROCESSED_DIRS:
                src = folder / name
                if not src.is_dir():
                    continue
                for path in sorted(src.rglob("*")):
                    if path.is_file():
                        zf.write(path, arcname=path.relative_to(folder).as_posix())
            for name in ("status.json", "control.json"):
                src = folder / name
                if src.is_file():
                    zf.write(src, arcname=name)
            zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        temp.replace(target)

        # Cloud workers run in separate scratch filesystems. Upload the rollback
        # payload BEFORE the run is queued, but never put it inside archive.zip.
        if cloud_persistence.enabled:
            try:
                cloud_persistence.persist_reprocess_backup(folder.name, target)
            except Exception as exc:
                target.unlink(missing_ok=True)
                raise ReprocessSafetyError(
                    f"Could not persist the separate safety backup; full reprocess was not started: {exc}"
                ) from exc
        return meta
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def assert_normalized_unchanged(folder: Path) -> None:
    expected = normalized_hash_from_backup(folder)
    actual = _sha256(folder / "normalized-all.json")
    if expected != actual:
        raise ReprocessSafetyError(
            "Safety invariant failed: normalized-all.json changed during full reprocess. "
            "The old processed state will be restored."
        )


def _safe_member_target(folder: Path, member_name: str) -> Path:
    target = (folder / member_name).resolve()
    root = folder.resolve()
    if target != root and root not in target.parents:
        raise ReprocessSafetyError("Unsafe path in full-reprocess backup archive.")
    return target


def restore_backup(folder: Path) -> dict:
    """Restore the exact downstream state captured before reprocessing."""
    legacy = backup_dir(folder)
    if legacy.is_dir():
        previous_status = _read_json(legacy / "status.json", {}) or {}
        for name in PROCESSED_DIRS:
            dst = folder / name
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            src = legacy / name
            if src.is_dir():
                shutil.copytree(src, dst)
        return previous_status

    archive = _ensure_backup_archive(folder)
    if archive is None:
        raise ReprocessSafetyError("Full-reprocess backup is missing; automatic rollback is not possible.")

    previous_status = read_backup_status(folder) or {}
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            members = zf.infolist()
            for name in PROCESSED_DIRS:
                dst = folder / name
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                prefix = f"{name}/"
                for member in members:
                    if member.is_dir() or not member.filename.startswith(prefix):
                        continue
                    target = _safe_member_target(folder, member.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
    except zipfile.BadZipFile as exc:
        raise ReprocessSafetyError("Full-reprocess backup archive is corrupt; rollback stopped safely.") from exc
    return previous_status


def discard_backup(folder: Path) -> None:
    # Delete durable copy first. If cloud cleanup fails, keep the local copy and
    # propagate the error so we never silently lose rollback capability.
    if cloud_persistence.enabled:
        cloud_persistence.delete_reprocess_backup(folder.name)
    backup_archive(folder).unlink(missing_ok=True)
    _backup_temp(folder).unlink(missing_ok=True)
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
