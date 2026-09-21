from __future__ import annotations

"""Serverless scratch-disk hygiene.

On Vercel the function filesystem is read-only except for /tmp, which is a
small (~512 MB) disk that SURVIVES across warm invocations. Every run writes
raw datasets, normalized evidence and exports there, so the disk fills up and
then *every* write fails with OSError(errno 28) — including tiny ones like
creating the config directory at import time, which surfaced to operators as a
bare "Internal Server Error".

Durable copies of every run live in the Blob mirror and are re-hydrated on
demand, so local run folders are disposable caches. This module frees space
before the application needs it, escalating only as far as necessary:

    1. old run folders (oldest first)
    2. the rest of the data directory
    3. unrelated scratch left in /tmp by tooling

Credentials and the source registry are never touched.
"""

import errno
import os
import shutil
import time
from pathlib import Path

TMP_ROOT = Path("/tmp")

#: Small, essential state: API credentials and the Actor registry. Never pruned.
PROTECTED_NAMES = {"signalyth-config", ".signalyth"}

DEFAULT_MIN_FREE_MB = 250.0


def free_mb(path: Path | str = TMP_ROOT) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1e6
    except Exception:
        return float("inf")


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _e: None):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except Exception:
                    continue
    except Exception:
        pass
    return total


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _sorted_by_age(paths) -> list[Path]:
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0
    return sorted(paths, key=mtime)


def ensure_free_space(
    data_dir: Path | str,
    min_free_mb: float = DEFAULT_MIN_FREE_MB,
    keep_run_id: str | None = None,
    aggressive: bool = False,
) -> dict:
    """Free scratch space, escalating until `min_free_mb` is available.

    Returns a small report of what was removed. Never raises: a failure to
    clean must not be worse than the condition it repairs.
    """
    report: dict = {
        "checked_at": time.time(),
        "free_mb_before": round(free_mb(), 1),
        "removed": [],
        "stage": "none",
    }
    try:
        data_root = Path(data_dir)
        target = float(min_free_mb)
        if not aggressive and free_mb() >= target:
            report["stage"] = "not_needed"
            report["free_mb_after"] = report["free_mb_before"]
            return report

        # Stage 1 — old run folders, oldest first.
        report["stage"] = "runs"
        runs_root = data_root / "runs"
        if runs_root.is_dir():
            for folder in _sorted_by_age(p for p in runs_root.iterdir() if p.is_dir()):
                if keep_run_id and folder.name == keep_run_id:
                    continue
                _remove(folder)
                report["removed"].append(f"runs/{folder.name}")
                if free_mb() >= target:
                    report["free_mb_after"] = round(free_mb(), 1)
                    return report

        # Stage 2 — anything else inside the data directory.
        report["stage"] = "data_dir"
        if data_root.is_dir():
            for entry in _sorted_by_age(data_root.iterdir()):
                if entry.name == "runs":
                    continue
                _remove(entry)
                report["removed"].append(entry.name)
                if free_mb() >= target:
                    report["free_mb_after"] = round(free_mb(), 1)
                    return report

        # Stage 3 — unrelated scratch in /tmp (build/tooling leftovers).
        report["stage"] = "tmp"
        now = time.time()
        for entry in _sorted_by_age(TMP_ROOT.iterdir()):
            if entry.name in PROTECTED_NAMES:
                continue
            try:
                if entry.resolve() == data_root.resolve():
                    continue
            except Exception:
                pass
            try:
                # Leave anything currently being written by this invocation.
                if now - entry.stat().st_mtime < 30:
                    continue
            except Exception:
                continue
            _remove(entry)
            report["removed"].append(f"/tmp/{entry.name}")
            if free_mb() >= target:
                break
    except Exception as exc:  # cleaning must never break the request
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["free_mb_after"] = round(free_mb(), 1)
    return report


def is_no_space_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno == errno.ENOSPC


def disk_report(data_dir: Path | str, limit: int = 15) -> dict:
    """Readable picture of what is occupying the scratch disk."""
    entries = []
    try:
        for entry in TMP_ROOT.iterdir():
            entries.append({"path": f"/tmp/{entry.name}", "mb": round(_dir_size_bytes(entry) / 1e6, 2)})
    except Exception:
        pass
    entries.sort(key=lambda row: row["mb"], reverse=True)

    runs = []
    try:
        runs_root = Path(data_dir) / "runs"
        if runs_root.is_dir():
            for folder in runs_root.iterdir():
                runs.append({"run_id": folder.name, "mb": round(_dir_size_bytes(folder) / 1e6, 2)})
    except Exception:
        pass
    runs.sort(key=lambda row: row["mb"], reverse=True)

    try:
        usage = shutil.disk_usage(str(TMP_ROOT))
        totals = {
            "total_mb": round(usage.total / 1e6, 1),
            "used_mb": round(usage.used / 1e6, 1),
            "free_mb": round(usage.free / 1e6, 1),
        }
    except Exception:
        totals = {}

    return {"disk": totals, "largest_in_tmp": entries[:limit], "runs": runs[:limit]}
