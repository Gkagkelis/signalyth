"""The operator must be able to see and clear what the app is storing.

A full scratch disk is not a cosmetic problem: the durable run archive is built
on that same disk, so when it fills, a finished analysis can be lost after it
was paid for. These tests pin the three things that prevent it — an honest
inventory, a way to delete, and a checkpoint that refuses to fail quietly.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.storage import RunStore


def _run(store: RunStore, client: str, topic: str, kb: int = 8) -> str:
    run_id = f"20260922T090000Z-{uuid4().hex[:8]}"
    folder = store.root / "runs" / run_id
    (folder / "analysis").mkdir(parents=True, exist_ok=True)
    store.write(folder / "plan.json", {"client": client, "topic": topic, "market": "Greece"})
    store.write(folder / "status.json", {
        "run_id": run_id, "status": "succeeded", "phase": "completed",
        "created_at": "2026-09-22T09:00:00+00:00", "updated_at": "2026-09-22T09:00:00+00:00",
    })
    (folder / "analysis" / "blob.bin").write_bytes(b"x" * (kb * 1024))
    return run_id


class TestInventory:
    def test_every_stored_run_is_listed_with_its_size(self):
        store = RunStore()
        a = _run(store, "ΟΠΑΠ", "Eurojackpot", kb=64)
        b = _run(store, "ΕΛΑΣ", "Αστυνομική βία", kb=8)
        out = store.storage_overview()
        rows = {r["run_id"]: r for r in out["runs"]}
        assert a in rows and b in rows
        assert rows[a]["client"] == "ΟΠΑΠ"
        assert rows[a]["local_mb"] > rows[b]["local_mb"], "sizes must be real, not placeholders"
        assert out["run_count"] >= 2
        store.delete_run(a)
        store.delete_run(b)

    def test_the_biggest_offender_comes_first(self):
        """The person is looking for what to delete, so lead with the largest."""
        store = RunStore()
        small = _run(store, "A", "small", kb=4)
        big = _run(store, "B", "big", kb=256)
        rows = [r["run_id"] for r in store.storage_overview()["runs"]]
        assert rows.index(big) < rows.index(small)
        store.delete_run(small)
        store.delete_run(big)

    def test_leftover_folders_are_shown_too(self):
        """Folders with no status still eat the disk; hiding them hides MB."""
        store = RunStore()
        orphan = f"20260922T090000Z-{uuid4().hex[:8]}"
        folder = store.root / "runs" / orphan
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "junk.bin").write_bytes(b"y" * 4096)
        rows = {r["run_id"]: r for r in store.storage_overview()["runs"]}
        assert orphan in rows
        assert rows[orphan]["status"] == "orphan"
        store.delete_run(orphan)


class TestDeleteEverything:
    def test_it_removes_every_run(self):
        store = RunStore()
        ids = [_run(store, f"C{i}", f"T{i}") for i in range(3)]
        out = store.delete_all_runs()
        assert out["deleted_count"] >= 3
        remaining = {r["run_id"] for r in store.storage_overview()["runs"]}
        assert not (set(ids) & remaining)

    def test_a_run_can_be_protected_from_the_wipe(self):
        """A run still collecting must survive; deleting it mid-flight corrupts it."""
        store = RunStore()
        keep = _run(store, "Live", "running now")
        gone = _run(store, "Old", "finished")
        out = store.delete_all_runs(keep={keep})
        assert keep not in out["deleted"]
        assert gone in out["deleted"]
        remaining = {r["run_id"] for r in store.storage_overview()["runs"]}
        assert keep in remaining
        store.delete_run(keep)


class TestDurabilityIsHonest:
    """A checkpoint that fails silently is how a paid analysis disappears."""

    def test_a_failed_checkpoint_is_recorded_and_raised(self, monkeypatch):
        store = RunStore()
        run_id = _run(store, "ΟΠΑΠ", "Eurojackpot")

        monkeypatch.setattr(type(store.cloud), "enabled", property(lambda self: True))
        monkeypatch.setattr(store.cloud, "persist_run_archive",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("No space left on device")))

        with pytest.raises(OSError):
            store.checkpoint_run(run_id)

        ok, reason = store.evidence_is_durable(run_id)
        assert ok is False, "a run whose archive failed must not claim to be saved"
        assert "space" in reason.lower()
        store.delete_run(run_id)

    def test_a_successful_checkpoint_is_recorded(self, monkeypatch):
        store = RunStore()
        run_id = _run(store, "ΟΠΑΠ", "Eurojackpot")
        monkeypatch.setattr(type(store.cloud), "enabled", property(lambda self: True))
        monkeypatch.setattr(store.cloud, "persist_run_archive", lambda *a, **k: "2026-09-22T09:00:00+00:00")

        store.checkpoint_run(run_id)

        ok, _ = store.evidence_is_durable(run_id)
        assert ok is True
        status = store.read_status(run_id)
        assert status["durability"]["saved"] is True
        store.delete_run(run_id)


class TestTheErrorTellsTheTruth:
    def test_a_lost_analysis_is_not_reported_as_missing_evidence(self):
        """The message the operator actually saw said the wrong thing."""
        from app.services.intelligence import build_intelligence

        store = RunStore()
        run_id = _run(store, "ΟΠΑΠ", "Eurojackpot")
        folder = store.root / "runs" / run_id
        status = store.read_status(run_id)
        status["analysis"] = {"status": "succeeded", "summary": {"analysis_ready_records": 38}}
        store.write_status_folder(folder, status)

        with pytest.raises(RuntimeError) as err:
            build_intelligence(folder, plan={"client": "ΟΠΑΠ", "topic": "Eurojackpot"})

        message = str(err.value)
        assert "38" in message, message
        assert "gone" in message or "not stored durably" in message, message
        store.delete_run(run_id)
