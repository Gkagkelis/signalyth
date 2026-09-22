"""Regression tests for the review-round-three orchestration edge cases.

These tests target bugs that can stay hidden while unit tests built from
hand-written plan dictionaries still pass. They should be run after applying
01-round2-full.patch and 02-round3-safety.patch.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import COMMENT_BUCKETS, comment_source_is_complete
from app.services.run_manager import RunManager, resume_at_analysis


def _cleaning_file(folder: Path) -> None:
    (folder / "cleaning").mkdir(parents=True, exist_ok=True)
    (folder / "cleaning" / "semantic-candidates.json").write_text("[]", encoding="utf-8")


def _real_plan(*, comments: bool = True, owned_share_pct: int = 60) -> dict:
    draft = AnalysisDraft(
        client="Client",
        topic="Topic",
        market="Greece",
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 22),
        sources=["facebook", "instagram", "tiktok"],
        sample_mode="perSource",
        per_source={"facebook": 2, "instagram": 2, "tiktok": 2},
        per_source_comments={"facebook": 4, "instagram": 4, "tiktok": 4},
        comments=comments,
        owned_share_pct=owned_share_pct,
        max_budget_usd=5.0,
    )
    return build_collection_plan(draft).model_dump(mode="json")


def _complete_comment_row() -> dict:
    return {"status": "collected", "buckets_done": list(COMMENT_BUCKETS)}


def test_real_collection_plan_uses_comments_requested_for_resume_barrier(tmp_path: Path):
    """Do not let a hand-written `comments=True` test mask the production field."""
    plan = _real_plan(comments=True)
    assert plan.get("comments_requested") is True
    assert "comments" not in plan
    _cleaning_file(tmp_path)

    status = {
        "status": "running",
        "cleaning": {"status": "succeeded"},
        "adaptive_collection": {"status": "target_met"},
        # Only one of three expected sources has reported completion.
        "comment_deepening": {"facebook": _complete_comment_row()},
    }
    assert resume_at_analysis(status, tmp_path, plan=plan) is False


def test_all_expected_sources_must_be_explicitly_complete(tmp_path: Path):
    plan = _real_plan(comments=True)
    _cleaning_file(tmp_path)
    status = {
        "status": "running",
        "cleaning": {"status": "succeeded"},
        "adaptive_collection": {"status": "target_met"},
        "comment_deepening": {
            "facebook": _complete_comment_row(),
            "instagram": _complete_comment_row(),
            "tiktok": _complete_comment_row(),
        },
    }
    assert resume_at_analysis(status, tmp_path, plan=plan) is True


def test_comment_source_with_missing_zero_work_bucket_is_not_complete():
    row = {"status": "deferred", "buckets_done": ["open", "backfill"]}
    assert comment_source_is_complete(row) is False


def test_failed_lease_claim_never_becomes_anonymous_ownership(tmp_path: Path, monkeypatch):
    manager = RunManager()
    run_id = "R"
    folder = tmp_path / "runs" / run_id
    folder.mkdir(parents=True)

    monkeypatch.setattr(manager.store, "folder_for", lambda _run_id: folder)
    monkeypatch.setattr(manager, "_claim_lease", lambda _run_id: "")

    paid_or_pipeline_work = {"read_plan": 0}
    def should_not_read_plan(_run_id):
        paid_or_pipeline_work["read_plan"] += 1
        raise AssertionError("worker continued after an unrecorded lease claim")
    monkeypatch.setattr(manager.store, "read_plan", should_not_read_plan)

    checkpoints = []
    monkeypatch.setattr(manager.store, "checkpoint_run", lambda rid: checkpoints.append(rid))

    manager._worker(run_id)
    assert paid_or_pipeline_work["read_plan"] == 0
    assert checkpoints == [], "an unleased worker must not checkpoint from finally either"


def test_milestone_refuses_to_publish_after_lease_loss(monkeypatch):
    manager = RunManager()
    calls = {"lease": 0, "checkpoint": 0, "bump": 0}

    def lease_ok(_run_id):
        calls["lease"] += 1
        # Own it before checkpoint, lose it while checkpointing.
        return calls["lease"] == 1

    monkeypatch.setattr(manager, "_lease_ok", lease_ok)
    monkeypatch.setattr(manager.store, "folder_for", lambda _run_id: Path("/tmp/R"))
    monkeypatch.setattr(manager.store, "bump_workspace_generation", lambda _folder: calls.__setitem__("bump", calls["bump"] + 1) or 1)
    monkeypatch.setattr(manager.store, "checkpoint_run", lambda _run_id: calls.__setitem__("checkpoint", calls["checkpoint"] + 1))

    assert manager._checkpoint_milestone("R") is False
    assert calls["bump"] == 1
    assert calls["checkpoint"] == 1


def test_generation_failure_is_not_silently_ignored(monkeypatch):
    manager = RunManager()
    monkeypatch.setattr(manager, "_lease_ok", lambda _run_id: True)
    monkeypatch.setattr(manager.store, "folder_for", lambda _run_id: Path("/tmp/R"))
    monkeypatch.setattr(manager.store, "bump_workspace_generation", lambda _folder: 0)
    with pytest.raises(Exception, match="workspace generation"):
        manager._checkpoint_milestone("R")
