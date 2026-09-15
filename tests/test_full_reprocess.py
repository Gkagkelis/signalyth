import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.reprocess import (
    ReprocessSafetyError,
    assert_normalized_unchanged,
    backup_exists,
    create_backup,
    restore_backup,
)
from app.services.run_manager import RunManager
from app.services.storage import RunStore


class FullReprocessSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "runs" / "20260915T100000Z-test"
        self.folder.mkdir(parents=True)
        (self.folder / "normalized-all.json").write_text(
            json.dumps([{"id": "1", "text": "saved evidence"}]),
            encoding="utf-8",
        )
        (self.folder / "exports").mkdir()
        (self.folder / "exports" / "old.txt").write_text("old export", encoding="utf-8")
        (self.folder / "cleaning").mkdir()
        (self.folder / "cleaning" / "sentinel.txt").write_text("old cleaning", encoding="utf-8")
        (self.folder / "analysis").mkdir()
        (self.folder / "analysis" / "analyzed.json").write_text("[]", encoding="utf-8")
        (self.folder / "plan.json").write_text(json.dumps({"topic": "Test", "market": "Greece"}), encoding="utf-8")
        status = {
            "run_id": self.folder.name,
            "status": "succeeded",
            "phase": "exports_ready",
            "progress": {"percent": 100},
            "current": {"source": None, "code": "done", "message": "done"},
        }
        (self.folder / "status.json").write_text(json.dumps(status), encoding="utf-8")
        (self.folder / "control.json").write_text(
            json.dumps({"cancel_requested": False, "requested_at": None}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_restores_processed_state_and_never_changes_normalized(self):
        original_normalized = (self.folder / "normalized-all.json").read_bytes()
        create_backup(self.folder)
        self.assertTrue(backup_exists(self.folder))

        (self.folder / "cleaning" / "sentinel.txt").write_text("new cleaning", encoding="utf-8")
        (self.folder / "exports" / "old.txt").write_text("new export", encoding="utf-8")

        previous = restore_backup(self.folder)
        self.assertEqual(previous["status"], "succeeded")
        self.assertEqual((self.folder / "cleaning" / "sentinel.txt").read_text(), "old cleaning")
        self.assertEqual((self.folder / "exports" / "old.txt").read_text(), "old export")
        self.assertEqual((self.folder / "normalized-all.json").read_bytes(), original_normalized)
        assert_normalized_unchanged(self.folder)

    def test_manual_review_state_blocks_before_backup(self):
        analyzed = [{
            "id": "1",
            "ai_analysis": {
                "decision": "ready",
                "human_override": {"action": "keep"},
                "human_review_history": [{"action": "keep"}],
            },
        }]
        (self.folder / "analysis" / "analyzed.json").write_text(json.dumps(analyzed), encoding="utf-8")
        with self.assertRaises(ReprocessSafetyError):
            create_backup(self.folder)
        self.assertFalse(backup_exists(self.folder))


class FullReprocessManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.run_id = "20260915T110000Z-test"
        self.folder = self.store.root / "runs" / self.run_id
        self.folder.mkdir(parents=True)
        self.store.write(self.folder / "normalized-all.json", [{"id": "1", "text": "evidence"}])
        self.store.write(self.folder / "plan.json", {"topic": "Test", "market": "Greece"})
        self.store.write(
            self.folder / "status.json",
            {
                "run_id": self.run_id,
                "status": "succeeded",
                "phase": "exports_ready",
                "progress": {"percent": 100},
                "current": {"source": None, "code": "done", "message": "done"},
            },
        )
        self.store.write(
            self.folder / "control.json",
            {"cancel_requested": False, "requested_at": None},
        )
        for name in ("cleaning", "analysis", "intelligence", "investigations", "visualizations", "exports"):
            (self.folder / name).mkdir()
        (self.folder / "cleaning" / "sentinel.txt").write_text("old", encoding="utf-8")
        self.store.write(self.folder / "analysis" / "analyzed.json", [])
        create_backup(self.folder)
        status = self.store.read(self.folder / "status.json")
        status["status"] = "queued"
        status["phase"] = "reprocess_cleaning"
        status["reprocess"] = {
            "status": "queued",
            "stage": "cleaning",
            "previous_status": "succeeded",
            "previous_phase": "exports_ready",
            "no_collection": True,
        }
        self.store.write(self.folder / "status.json", status)
        self.manager = RunManager(store=self.store, max_workers=1)

    def tearDown(self):
        self.manager.shutdown(wait=False)
        self.tmp.cleanup()

    @staticmethod
    def _report(version="test"):
        return {"generated_at": "2026-09-15T11:00:00+00:00", "ruleset_version": version}

    def test_reprocess_chain_never_calls_collection(self):
        calls = []

        def mark(name):
            def _fn(*args, **kwargs):
                calls.append(name)
                return self._report(name)
            return _fn

        paid_path_error = AssertionError("full reprocess must never start collection/refill")
        with (
            patch("app.services.run_manager.execute_plan", side_effect=paid_path_error) as collect,
            patch("app.services.run_manager.adaptive_expand_after_cleaning", side_effect=paid_path_error) as adaptive,
            patch("app.services.run_manager.semantic_refill", side_effect=paid_path_error) as refill,
            patch("app.services.run_manager.clean_run", side_effect=mark("cleaning")),
            patch("app.services.run_manager.analyze_run", side_effect=mark("analysis")),
            patch("app.services.run_manager.build_intelligence", side_effect=mark("intelligence")),
            patch("app.services.run_manager.build_investigations", side_effect=mark("investigations")),
            patch("app.services.run_manager.build_visualizations", side_effect=mark("visualizations")),
            patch("app.services.run_manager.build_exports", side_effect=mark("exports")),
        ):
            self.manager._run_reprocess(self.run_id, self.folder, self.store.read(self.folder / "plan.json"))

        collect.assert_not_called()
        adaptive.assert_not_called()
        refill.assert_not_called()
        self.assertEqual(
            calls,
            ["cleaning", "analysis", "intelligence", "investigations", "visualizations", "exports"],
        )
        status = self.store.read(self.folder / "status.json")
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["reprocess"]["status"], "succeeded")
        self.assertTrue(status["reprocess"]["no_collection"])
        self.assertFalse(backup_exists(self.folder))

    def test_failure_rolls_back_previous_processed_state(self):
        def mutate_cleaning(*args, **kwargs):
            (self.folder / "cleaning" / "sentinel.txt").write_text("new", encoding="utf-8")
            return self._report("cleaning")

        with (
            patch("app.services.run_manager.clean_run", side_effect=mutate_cleaning),
            patch("app.services.run_manager.analyze_run", side_effect=RuntimeError("boom")),
            patch("app.services.run_manager.execute_plan", side_effect=AssertionError("collection must not run")),
        ):
            self.manager._run_reprocess(self.run_id, self.folder, self.store.read(self.folder / "plan.json"))

        self.assertEqual((self.folder / "cleaning" / "sentinel.txt").read_text(), "old")
        status = self.store.read(self.folder / "status.json")
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["reprocess"]["status"], "failed")
        self.assertTrue(status["reprocess"]["rolled_back"])
        self.assertFalse(backup_exists(self.folder))


if __name__ == "__main__":
    unittest.main()
