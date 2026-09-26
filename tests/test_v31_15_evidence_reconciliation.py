"""v31.15 — partial evidence loss is reconciled with an audit, not a dead run.

NBG run 20260926T142313Z-d4659ef0 finished its comment layer, and a worker
died between bumping normalized_total to 200 (status.json syncs to cloud on
every write) and archiving the batch behind it. The replacement restored an
archive holding 196 real records and the guard killed the whole run over the
4-record unarchived tail. The guard now reconciles down to the archive's
contents and records the shrink BY NAME in status.evidence_reconciliations —
v31.5 forbids a run quietly shrinking, not an audited recovery. A workspace
with NO evidence (metadata-only restore) still fails hard.
"""
from __future__ import annotations

from app.services.run_manager import RunManager
from app.services.storage import RunStore


def _manager_with_folder(tmp_path, monkeypatch):
    manager = RunManager.__new__(RunManager)
    manager.store = RunStore()
    monkeypatch.setattr(manager.store, "folder_for",
                        lambda run_id, allow_restore=True: tmp_path)
    return manager


def test_partial_loss_is_reconciled_with_audit_instead_of_failing(tmp_path, monkeypatch):
    manager = _manager_with_folder(tmp_path, monkeypatch)
    store = manager.store
    rows = [{"id": f"r{i}"} for i in range(196)]
    store.write(tmp_path / "normalized-all.json", rows)
    store.write(tmp_path / "status.json", {
        "run_id": "T", "status": "running", "normalized_total": 200,
        "sample_target": 110,
        "sources": {"x": {"status": "succeeded", "collected": 24}},
    })

    assert manager._evidence_guard("T") is True

    status = store.read(tmp_path / "status.json", {})
    assert status["normalized_total"] == 196
    audits = status.get("evidence_reconciliations") or []
    assert audits and audits[-1]["claimed"] == 200
    assert audits[-1]["recovered"] == 196
    assert audits[-1]["lost_records"] == 4
    # The run was NOT marked failed.
    assert status.get("status") == "running"


def test_zero_evidence_still_fails_hard(tmp_path, monkeypatch):
    manager = _manager_with_folder(tmp_path, monkeypatch)
    store = manager.store
    (tmp_path / ".signalyth-metadata-only").write_text("t", encoding="utf-8")
    store.write(tmp_path / "status.json", {
        "run_id": "T", "status": "running", "normalized_total": 200,
        "sources": {"x": {"status": "succeeded", "collected": 24}},
    })

    assert manager._evidence_guard("T") is False
    status = store.read(tmp_path / "status.json", {})
    assert status.get("status") == "failed"
    assert status.get("phase") == "evidence_unavailable"
