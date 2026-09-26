"""v31.14 — paid collection evidence survives a hard-killed worker.

NBG run 20260926T131930Z-7e260342 collected all six sources (89 records,
$0.38 paid) and lost every one of them. Three defects lined up:

1. The invocation soft deadline was ONE shared field on the RunManager
   singleton. A serverless subscriber that handles several queue messages
   concurrently reset it for every in-flight run each time a new message
   arrived, so the run sailed past 700s without ever checkpointing or
   requeuing, and the platform's hard kill took the scratch disk with it.
2. Collection wrote evidence to the local scratch disk only: the first
   durable archive was made at the soft-deadline requeue or the collection
   milestone. Hard-killed in between = nothing durable.
3. The replacement worker restored settings-only (metadata-only marker),
   correctly refused to analyse — and then ARCHIVED its empty workspace,
   and evidence_intact treated "normalized_total == 0" as "nothing collected
   yet" even while per-source rows said money had been spent.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.services.run_manager import RunManager
from app.services.storage import RunStore


# ---------- 1. per-run deadlines ----------

def test_new_queue_message_cannot_extend_another_runs_deadline():
    manager = RunManager.__new__(RunManager)  # no executor/store needed here
    manager._run_deadlines = {}
    import threading
    manager._deadline_lock = threading.Lock()
    manager._deadline_monotonic = None

    manager.set_invocation_deadline(0.01, run_id="run-A")
    time.sleep(0.03)
    assert manager._deadline_reached(run_id="run-A") is True

    # The bug: a second message arriving in the same process pushed the shared
    # deadline forward and run-A suddenly had "time" again.
    manager.set_invocation_deadline(700, run_id="run-B")
    assert manager._deadline_reached(run_id="run-A") is True, (
        "run-B's fresh deadline must never extend run-A's")
    assert manager._deadline_reached(run_id="run-B") is False
    assert manager._seconds_left(run_id="run-A") < 0
    assert manager._seconds_left(run_id="run-B") > 600

    # Clearing one run leaves the other untouched.
    manager.set_invocation_deadline(None, run_id="run-B")
    assert manager._deadline_reached(run_id="run-B") is False  # falls back to None
    assert manager._deadline_reached(run_id="run-A") is True


# ---------- 2. per-source durable checkpoints ----------

def test_every_completed_source_is_checkpointed(tmp_path, monkeypatch):
    from datetime import date
    from app.models import AnalysisDraft
    from app.services.query_planner import build_collection_plan
    import app.services.collector as collector

    draft = AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x", "news"], sample_mode="perSource",
        per_source={"x": 2, "news": 2}, comments=False, max_budget_usd=2.0,
        smart_search=True, report_language="Ελληνικά",
        additional_context=[], exclusions=[],
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "status.json", {"run_id": "T"})

    class FakeRunner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return {"usageTotalUsd": 0.0001, "defaultDatasetId": "d"}, []

    monkeypatch.setattr(collector, "ApifyRunner", FakeRunner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)

    checkpoints: list[str] = []
    collector.execute_plan(
        plan, "T", tmp_path,
        checkpoint=lambda step: checkpoints.append(step),
    )

    terminal_marks = [c for c in checkpoints if c.endswith(":terminal")]
    sources_marked = {c.split(":", 1)[0] for c in terminal_marks}
    assert sources_marked >= {"x", "news"}, (
        f"each completed source must produce a durable checkpoint: {checkpoints}")


# ---------- 3. metadata-only workspaces ----------

def _store_with_fake_cloud(tmp_path, monkeypatch):
    store = RunStore()
    calls = {"persist": 0}

    class FakeCloud:
        enabled = True
        def persist_run_archive(self, run_id, folder):
            calls["persist"] += 1
            return "2026-09-26T00:00:00+00:00"

    monkeypatch.setattr(store, "cloud", FakeCloud(), raising=False)
    monkeypatch.setattr(store, "folder_for", lambda run_id, allow_restore=True: tmp_path)
    recorded = {}
    monkeypatch.setattr(store, "_record_durability",
                        lambda run_id, **kw: recorded.update(kw), raising=False)
    return store, calls, recorded


def test_checkpoint_refuses_to_archive_a_metadata_only_workspace(tmp_path, monkeypatch):
    store, calls, recorded = _store_with_fake_cloud(tmp_path, monkeypatch)
    (tmp_path / ".signalyth-metadata-only").write_text("t", encoding="utf-8")
    store.write(tmp_path / "status.json", {"run_id": "T"})

    store.checkpoint_run("T")
    assert calls["persist"] == 0, "a settings-only workspace must never become the archive"
    assert recorded.get("saved") is False

    # Once the workspace is validated and the marker cleared, checkpoints work.
    store.clear_metadata_only_marker(tmp_path)
    store.checkpoint_run("T")
    assert calls["persist"] == 1


def test_evidence_guard_counts_per_source_spend_before_finalize(tmp_path, monkeypatch):
    """Mid-collection loss: normalized_total is still 0 but sources say 47 paid."""
    store = RunStore()
    monkeypatch.setattr(store, "folder_for", lambda run_id, allow_restore=True: tmp_path)
    (tmp_path / ".signalyth-metadata-only").write_text("t", encoding="utf-8")
    store.write(tmp_path / "status.json", {
        "run_id": "T", "normalized_total": 0,
        "sources": {"x": {"status": "succeeded", "collected": 25, "cost_usd": 0.09},
                    "news": {"status": "succeeded", "collected": 22, "cost_usd": 0.05}},
    })
    ok, reason = store.evidence_intact("T")
    assert ok is False, reason
    assert "47" in reason

    # A genuinely fresh run (nothing collected anywhere) still passes.
    store.write(tmp_path / "status.json", {"run_id": "T", "normalized_total": 0, "sources": {}})
    ok, reason = store.evidence_intact("T")
    assert ok is True, reason
