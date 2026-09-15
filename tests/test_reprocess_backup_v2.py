import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from app.services.cloud_persistence import cloud_persistence
from app.services.reprocess import (
    ReprocessSafetyError,
    backup_archive,
    backup_exists,
    create_backup,
    discard_backup,
    restore_backup,
)


class SeparateReprocessBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "runs" / "20260915T130000Z-test"
        self.folder.mkdir(parents=True)
        (self.folder / "normalized-all.json").write_text(
            json.dumps([{"id": "1", "text": "saved evidence"}]), encoding="utf-8"
        )
        for name in ("cleaning", "analysis", "intelligence", "investigations", "visualizations", "exports"):
            (self.folder / name).mkdir()
        (self.folder / "cleaning" / "sentinel.txt").write_text("old cleaning", encoding="utf-8")
        (self.folder / "exports" / "old.txt").write_text("old export", encoding="utf-8")
        (self.folder / "analysis" / "analyzed.json").write_text("[]", encoding="utf-8")
        (self.folder / "status.json").write_text(
            json.dumps({"run_id": self.folder.name, "status": "succeeded", "phase": "exports_ready"}),
            encoding="utf-8",
        )
        (self.folder / "control.json").write_text(
            json.dumps({"cancel_requested": False, "requested_at": None}), encoding="utf-8"
        )

    def tearDown(self):
        # Local-mode cleanup never touches a real cloud service.
        with patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=False):
            try:
                discard_backup(self.folder)
            except Exception:
                pass
        self.tmp.cleanup()

    def test_backup_archive_is_sibling_not_inside_run(self):
        with patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=False):
            create_backup(self.folder)
        archive = backup_archive(self.folder)
        self.assertTrue(archive.is_file())
        self.assertEqual(archive.parent, self.folder.parent)
        self.assertNotIn(self.folder, archive.parents)
        self.assertFalse((self.folder / ".reprocess-backup.zip").exists())

    def test_cloud_backup_is_persisted_separately_before_reprocess(self):
        with (
            patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=True),
            patch.object(cloud_persistence, "reprocess_backup_exists", return_value=False),
            patch.object(cloud_persistence, "persist_reprocess_backup") as persist,
        ):
            create_backup(self.folder)
        persist.assert_called_once_with(self.folder.name, backup_archive(self.folder))
        self.assertTrue(backup_archive(self.folder).is_file())

    def test_cloud_persist_failure_aborts_and_removes_local_backup(self):
        with (
            patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=True),
            patch.object(cloud_persistence, "reprocess_backup_exists", return_value=False),
            patch.object(cloud_persistence, "persist_reprocess_backup", side_effect=RuntimeError("blob unavailable")),
        ):
            with self.assertRaises(ReprocessSafetyError):
                create_backup(self.folder)
        self.assertFalse(backup_archive(self.folder).exists())

    def test_fresh_worker_can_restore_backup_from_cloud_object(self):
        with patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=False):
            create_backup(self.folder)
        archive_bytes = backup_archive(self.folder).read_bytes()
        backup_archive(self.folder).unlink()

        (self.folder / "cleaning" / "sentinel.txt").write_text("partial reprocess", encoding="utf-8")
        (self.folder / "exports" / "old.txt").write_text("new export", encoding="utf-8")

        def restore_remote(run_id, destination):
            self.assertEqual(run_id, self.folder.name)
            destination.write_bytes(archive_bytes)
            return True

        with (
            patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=True),
            patch.object(cloud_persistence, "reprocess_backup_exists", return_value=True),
            patch.object(cloud_persistence, "restore_reprocess_backup", side_effect=restore_remote),
        ):
            self.assertTrue(backup_exists(self.folder))
            previous = restore_backup(self.folder)

        self.assertEqual(previous["status"], "succeeded")
        self.assertEqual((self.folder / "cleaning" / "sentinel.txt").read_text(), "old cleaning")
        self.assertEqual((self.folder / "exports" / "old.txt").read_text(), "old export")

    def test_cloud_lookup_error_fails_closed(self):
        with (
            patch.object(type(cloud_persistence), "enabled", new_callable=PropertyMock, return_value=True),
            patch.object(cloud_persistence, "reprocess_backup_exists", side_effect=RuntimeError("network error")),
        ):
            with self.assertRaises(RuntimeError):
                backup_exists(self.folder)


if __name__ == "__main__":
    unittest.main()
