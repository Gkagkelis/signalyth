from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


class CloudPersistenceError(RuntimeError):
    pass


class CloudPersistence:
    """Persist SIGNALYTH run/config state in Vercel Blob.

    The application continues to use its existing filesystem-oriented pipeline in a
    writable scratch directory. On Vercel that scratch directory is /tmp; durable
    metadata and run snapshots are mirrored to a private Blob store.
    """

    RUN_PREFIX = "signalyth/runs"
    CONFIG_PREFIX = "signalyth/config"

    @property
    def enabled(self) -> bool:
        # New Vercel Blob project connections use OIDC by default. In that mode
        # Vercel injects BLOB_STORE_ID and the SDK obtains short-lived credentials
        # automatically at runtime. Keep supporting BLOB_READ_WRITE_TOKEN for local
        # development and legacy/static-token connections.
        has_static_token = bool(os.getenv("BLOB_READ_WRITE_TOKEN"))
        has_oidc_store = bool(os.getenv("VERCEL") and os.getenv("BLOB_STORE_ID"))
        return bool(settings.signalyth_cloud_storage and (has_static_token or has_oidc_store))

    @property
    def requested(self) -> bool:
        return bool(settings.signalyth_cloud_storage)

    def require(self) -> None:
        if self.requested and not self.enabled:
            raise CloudPersistenceError(
                "Cloud storage is enabled but no Vercel Blob connection is available. "
                "Connect a private Blob store to this project (OIDC/BLOB_STORE_ID) "
                "or provide BLOB_READ_WRITE_TOKEN."
            )

    @staticmethod
    def _client():
        try:
            from vercel.blob import BlobClient
        except ImportError as exc:  # pragma: no cover - only exercised in cloud deployment
            raise CloudPersistenceError(
                "The Vercel Python SDK is not installed. Install the 'vercel' package."
            ) from exc
        return BlobClient()

    @classmethod
    def _run_path(cls, run_id: str, name: str) -> str:
        return f"{cls.RUN_PREFIX}/{run_id}/{name}"

    @classmethod
    def _config_path(cls, name: str) -> str:
        return f"{cls.CONFIG_PREFIX}/{name}"

    def put_json(self, run_id: str, name: str, payload: Any) -> None:
        if not self.enabled:
            return
        body = (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
        with self._client() as client:
            client.put(
                self._run_path(run_id, name),
                body,
                access="private",
                content_type="application/json; charset=utf-8",
                overwrite=True,
            )

    def get_json(self, run_id: str, name: str) -> Any | None:
        if not self.enabled:
            return None
        try:
            with self._client() as client:
                result = client.get(
                    self._run_path(run_id, name),
                    access="private",
                    use_cache=False,
                )
            return json.loads(result.content.decode("utf-8"))
        except Exception:
            return None


    def delete_run(self, run_id: str) -> None:
        """Best-effort removal of every stored object for one run."""
        if not self.enabled:
            return
        try:
            with self._client() as client:
                paths = []
                for item in client.iter_objects(prefix=f"{self.RUN_PREFIX}/{run_id}/", limit=1000):
                    path = str(getattr(item, "pathname", "") or getattr(item, "url", "") or "")
                    if path:
                        paths.append(path)
                for path in paths:
                    try:
                        client.delete(path)
                    except Exception:
                        pass
        except Exception:
            # Deletion is user-facing cleanup; a transient Blob error must not 500 the API.
            pass

    def list_run_ids(self, limit: int = 100) -> list[str]:
        if not self.enabled:
            return []
        found: set[str] = set()
        try:
            with self._client() as client:
                for item in client.iter_objects(prefix=f"{self.RUN_PREFIX}/", limit=max(limit * 4, 100)):
                    path = str(getattr(item, "pathname", "") or "")
                    if not path.endswith("/status.json"):
                        continue
                    parts = path.split("/")
                    if len(parts) >= 4:
                        found.add(parts[-2])
                    if len(found) >= limit:
                        break
        except Exception:
            return []
        return sorted(found, reverse=True)[:limit]

    def persist_run_archive(self, run_id: str, folder: Path) -> str | None:
        if not self.enabled or not folder.is_dir():
            return None
        stamp = datetime.now(timezone.utc).isoformat()
        tmp_dir = Path(tempfile.mkdtemp(prefix="signalyth-archive-", dir="/tmp" if Path("/tmp").exists() else None))
        try:
            archive = tmp_dir / f"{run_id}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        zf.write(path, arcname=path.relative_to(folder).as_posix())
            with self._client() as client:
                client.upload_file(
                    archive,
                    self._run_path(run_id, "archive.zip"),
                    access="private",
                    content_type="application/zip",
                    overwrite=True,
                    multipart=archive.stat().st_size >= 8 * 1024 * 1024,
                )
            try:
                self.put_json(run_id, "archive-meta.json", {"checkpointed_at": stamp})
            except Exception:
                pass
            try:
                (folder / ".signalyth-archive-stamp").write_text(stamp, encoding="utf-8")
            except Exception:
                pass
            return stamp
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _safe_extract(zf: zipfile.ZipFile, destination: Path) -> None:
        destination = destination.resolve()
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise CloudPersistenceError("Unsafe path in stored run archive.")
        zf.extractall(destination)

    def get_archive_meta(self, run_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            meta = self.get_json(run_id, "archive-meta.json")
            return meta if isinstance(meta, dict) else None
        except Exception:
            return None

    def restore_run_archive(self, run_id: str, folder: Path) -> bool:
        if not self.enabled:
            return False
        try:
            with self._client() as client:
                result = client.get(
                    self._run_path(run_id, "archive.zip"),
                    access="private",
                    use_cache=False,
                )
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder.parent / f".{run_id}.restore.zip"
            archive.write_bytes(result.content)
            try:
                with zipfile.ZipFile(archive, "r") as zf:
                    self._safe_extract(zf, folder)
            finally:
                archive.unlink(missing_ok=True)
            meta = self.get_archive_meta(run_id)
            if meta and meta.get("checkpointed_at"):
                try:
                    (folder / ".signalyth-archive-stamp").write_text(str(meta["checkpointed_at"]), encoding="utf-8")
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def restore_run_metadata(self, run_id: str, folder: Path) -> bool:
        if not self.enabled:
            return False
        restored = False
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("plan.json", "status.json", "control.json"):
            payload = self.get_json(run_id, name)
            if payload is None:
                continue
            (folder / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            restored = True
        if not restored:
            try:
                folder.rmdir()
            except OSError:
                pass
        return restored

    def put_config_file(self, name: str, path: Path) -> None:
        if not self.enabled or not path.is_file():
            return
        with self._client() as client:
            client.upload_file(
                path,
                self._config_path(name),
                access="private",
                content_type="application/json; charset=utf-8",
                overwrite=True,
            )

    def restore_config_file(self, name: str, path: Path) -> bool:
        if not self.enabled:
            return False
        try:
            with self._client() as client:
                result = client.get(
                    self._config_path(name),
                    access="private",
                    use_cache=False,
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result.content)
            return True
        except Exception:
            return False


cloud_persistence = CloudPersistence()
