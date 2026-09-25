"""v31.5 — a hard-killed comment-layer worker must not lose paid evidence.

Run 20260925T130636Z-c55323ad: the worker died abruptly ~5 minutes into the
comment layer, inside relevance_expansion, without writing status. Evidence
files were only on the worker's local scratch disk — the durable archive is
written at stage milestones — so 47 posts survived and every paid comment was
lost. Two contracts close that hole:

1. After EVERY paid comment / parent-discovery call, a durable checkpoint runs
   with the normalized comments, the attempted parent refs and the status
   already on disk, so the next worker resumes from the NEXT call and never
   re-pays a finished one.
2. While a blocking Apify call is in flight, a heartbeat thread refreshes
   status.json every ≤20 seconds, so a dead worker becomes visible to the API
   within the stale window instead of hiding behind a multi-minute Actor wait.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from app.models import AnalysisDraft
from app.services.cleaning import clean_run
from app.services.normalizer import normalize_dataset
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import adaptive_expand_after_cleaning
from app.services.resilience import HEARTBEAT_INTERVAL_SECONDS, run_actor_resilient
from app.services.storage import RunStore


class WorkerKilled(BaseException):
    """Emulates the platform hard-killing the worker process mid-call.

    BaseException on purpose: nothing in the pipeline may catch it, exactly as
    nothing in the pipeline gets to run when the real process dies.
    """


# ---------------------------------------------------------------------------
# 2. The in-flight heartbeat
# ---------------------------------------------------------------------------

class SlowRunner:
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        time.sleep(0.35)
        return {"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, [{"id": "1"}]


def test_the_heartbeat_interval_is_within_the_required_bound():
    assert HEARTBEAT_INTERVAL_SECONDS <= 20.0


def test_heartbeat_fires_repeatedly_while_an_actor_call_blocks():
    beats: list[str] = []
    result = run_actor_resilient(
        SlowRunner(), "a/b", {"q": "x"}, max_items=5, max_charge_usd=0.1,
        heartbeat=beats.append, heartbeat_interval_seconds=0.05,
    )
    assert result.status == "succeeded"
    in_flight = [b for b in beats if b.endswith(":in_flight")]
    assert len(in_flight) >= 3, f"heartbeat did not fire during the blocking call: {beats}"
    # The thread stops with the call: no beats keep arriving afterwards.
    settled = len(beats)
    time.sleep(0.2)
    assert len(beats) == settled


def test_heartbeat_thread_stops_even_when_the_call_dies():
    beats: list[str] = []

    class DyingRunner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            time.sleep(0.15)
            raise WorkerKilled("hard kill inside the Actor wait")

    with pytest.raises(WorkerKilled):
        run_actor_resilient(
            DyingRunner(), "a/b", {"q": "x"}, max_items=5, max_charge_usd=0.1,
            heartbeat=beats.append, heartbeat_interval_seconds=0.05,
        )
    settled = len(beats)
    time.sleep(0.2)
    assert len(beats) == settled


# ---------------------------------------------------------------------------
# 1 + 4. Per-call durable checkpoint and the no-double-payment continuation
# ---------------------------------------------------------------------------

def _draft():
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x"], sample_mode="perSource",
        per_source={"x": 4}, per_source_comments={"x": 40},
        comments=True, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", additional_context=["OPAP"], exclusions=[],
    )


def _post(tweet_id, text, author, replies=3, day=16):
    return {
        "id": str(tweet_id), "text": text,
        "createdAt": f"Sun Aug {day:02d} 12:03:14 +0000 2026",
        "authorUsername": author, "replyCount": replies,
        "viewCount": 100, "likeCount": 2, "retweetCount": 0,
        "url": f"https://x.com/{author}/status/{tweet_id}", "type": "tweet",
    }


def _reply(reply_id, parent_id, text):
    return {
        **_post(reply_id, text, f"fan{reply_id}", replies=0, day=18),
        "type": "reply", "inReplyToId": str(parent_id),
    }


def _prepare(tmp_path: Path):
    plan = build_collection_plan(_draft()).model_dump(mode="json")
    for row in (plan.get("preflight_forecast") or {}).get("sources", []):
        if row.get("source") == "x":
            row.setdefault("comments", {})["status"] = "verified_available"
            row["comments"]["live_verified"] = True
    posts = [
        _post(400, "Allwyn OPAP Greece customer experience review", "person1"),
        _post(401, "Allwyn Greece sponsorship discussion tonight", "person2"),
        _post(402, "Allwyn OPAP Greece payout complaint thread", "person3"),
        _post(403, "Allwyn Greece lottery app opinions today", "person4"),
    ]
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "raw-x.json", posts)
    rows = normalize_dataset("x", posts)
    store.write(tmp_path / "normalized-x.json", rows)
    store.write(tmp_path / "normalized-all.json", rows)
    store.write(tmp_path / "status.json", {
        "run_id": "T", "budget": {"max_usd": 5.0, "spent_usd": 0.05, "remaining_usd": 4.95},
    })
    report = clean_run(tmp_path, plan=plan)
    return plan, report, store


class HarvestRunner:
    """Answers reply calls with fresh comments; optionally dies on the Nth one."""

    def __init__(self, id_prefix: str, kill_on_reply_call: int | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.reply_calls = 0
        self.id_prefix = id_prefix
        self.kill_on = kill_on_reply_call

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls.append((actor_id, dict(run_input)))
        if run_input.get("mode") != "replies":
            return {"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, []
        self.reply_calls += 1
        if self.kill_on is not None and self.reply_calls >= self.kill_on:
            raise WorkerKilled("platform reclaimed the worker mid comment call")
        parent = str((run_input.get("replyTweetIds") or ["0"])[0])
        items = [
            _reply(f"{self.id_prefix}{self.reply_calls}{i}", parent,
                   f"Allwyn Greece reply {self.id_prefix}{self.reply_calls}{i} opinion")
            for i in range(3)
        ]
        return {"usageTotalUsd": 0.002, "defaultDatasetId": "d"}, items

    def reply_ref_sets(self) -> list[set[str]]:
        return [set(map(str, inp.get("replyTweetIds") or []))
                for _, inp in self.calls if inp.get("mode") == "replies"]


def test_worker_killed_after_one_paid_call_hands_everything_to_the_next_worker(tmp_path):
    plan, report, store = _prepare(tmp_path)
    checkpoints: list[dict] = []

    def checkpoint(step: str) -> None:
        # What the durable archive would contain at this exact moment.
        state = store.read(tmp_path / "comment-harvest-state.json", {}) or {}
        checkpoints.append({
            "step": step,
            "comments": len(store.read(tmp_path / "normalized-comments-x.json", []) or []),
            "attempted": dict((state.get("x") or {}).get("attempted_refs_by_bucket") or {}),
        })

    # --- Worker 1: one successful comment batch, then a hard kill -------------
    killed = HarvestRunner("6", kill_on_reply_call=2)
    with pytest.raises(WorkerKilled):
        adaptive_expand_after_cleaning(
            tmp_path, plan, report, runner=killed, checkpoint=checkpoint,
        )
    first_batch = killed.reply_ref_sets()[0]
    assert first_batch, "the first comment batch never ran"

    batch_checkpoints = [c for c in checkpoints if c["step"].endswith(":comment_batch")]
    assert batch_checkpoints, f"no per-call checkpoint after the paid comment call: {checkpoints}"
    durable = batch_checkpoints[-1]
    # The checkpoint fired AFTER the comments and the attempted refs hit disk.
    assert durable["comments"] == 3
    attempted_open = {ref for refs in durable["attempted"].values() for ref in refs}
    assert first_batch <= attempted_open

    # Paid parent discovery is checkpointed per route as well.
    assert any(":parent_discovery:" in c["step"] for c in checkpoints), checkpoints

    comments_after_kill = len(store.read(tmp_path / "normalized-comments-x.json", []) or [])
    assert comments_after_kill == 3

    # --- Worker 2: resumes from durable state ---------------------------------
    fresh = HarvestRunner("7")
    adaptive_expand_after_cleaning(tmp_path, plan, report, runner=fresh, checkpoint=checkpoint)

    # The continuation NEVER re-pays the finished call: no reply call repeats a
    # parent the killed worker already completed, and no discovery route re-runs.
    for refs in fresh.reply_ref_sets():
        assert not (refs & first_batch), (
            f"the continuation re-paid parents {refs & first_batch} already harvested"
        )
    assert all(inp.get("mode") == "replies" for _, inp in fresh.calls), (
        f"the continuation re-paid a discovery search: {[i for _, i in fresh.calls]}"
    )

    # The comments ACCUMULATE across the two workers.
    final_comments = store.read(tmp_path / "normalized-comments-x.json", []) or []
    assert len(final_comments) == comments_after_kill + 3
    ids = [str(r.get("id")) for r in final_comments]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 3. Worker observability in status.json + RunManager wiring
# ---------------------------------------------------------------------------

def test_status_records_worker_start_last_step_and_elapsed(tmp_path):
    from app.services.run_manager import RunManager

    store = RunStore()
    store.root = tmp_path
    manager = RunManager(store=store, max_workers=1)
    try:
        plan = build_collection_plan(_draft()).model_dump(mode="json")
        run_id, folder = store.create(plan)
        token = manager._claim_lease(run_id)
        assert token

        worker = (store.read_status(run_id) or {}).get("worker") or {}
        assert worker.get("worker_started_at"), worker
        assert worker.get("last_step") == "worker_started"

        manager._note_worker_step(folder, "apify_call:x")
        status = store.read_status(run_id)
        worker = status.get("worker") or {}
        assert worker.get("last_step") == "apify_call:x"
        assert float(worker.get("elapsed_seconds")) >= 0.0
        # Every step note refreshes updated_at, which is what stale-worker
        # detection reads: a dead worker becomes visible within the window.
        assert status.get("updated_at")
    finally:
        manager.shutdown(wait=True)


def test_run_manager_wires_the_per_call_checkpoint_into_the_comment_layer(tmp_path):
    """The adaptive layer receives a checkpoint that really persists the run."""
    from unittest.mock import patch

    from app.config import settings
    from app.services.run_manager import RunManager
    from tests.test_whole_pipeline_v19 import FakeApify, FakeModel, _wait_terminal

    store = RunStore()
    store.root = tmp_path
    manager = RunManager(store=store, max_workers=1)
    FakeApify.calls = []
    prior = settings.signalyth_ai_enabled, settings.openai_api_key
    settings.signalyth_ai_enabled, settings.openai_api_key = True, "test-key"

    checkpoint_steps: list[str | None] = []
    real_checkpoint = store.checkpoint_run

    def recording_checkpoint(run_id: str) -> None:
        worker = (store.read(store.root / "runs" / run_id / "status.json", {}) or {}).get("worker") or {}
        checkpoint_steps.append(worker.get("last_step"))
        real_checkpoint(run_id)

    store.checkpoint_run = recording_checkpoint

    def adaptive_probe(folder, *, plan, initial_report, **kwargs):
        checkpoint = kwargs.get("checkpoint")
        assert callable(checkpoint), "RunManager no longer passes a per-call checkpoint"
        checkpoint("x:open:comment_batch")
        return {"report": initial_report, "audit": {"deadline_reached": False, "warnings": []}}

    try:
        plan = build_collection_plan(_draft()).model_dump(mode="json")
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") == "x":
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
        run_id = store.create(plan)[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel), \
             patch("app.services.run_manager.adaptive_expand_after_cleaning", adaptive_probe):
            manager.enqueue(run_id)
            done = _wait_terminal(store, run_id, timeout=240.0)

        assert done.get("status") not in {"failed", "running", "queued"}, done.get("fatal_error")
        # The probe's checkpoint reached the durable store, and the step it
        # persisted was recorded in the worker block first.
        assert "checkpoint:x:open:comment_batch" in checkpoint_steps, checkpoint_steps
        assert (done.get("worker") or {}).get("worker_started_at")
    finally:
        settings.signalyth_ai_enabled, settings.openai_api_key = prior
        manager.shutdown(wait=True)
