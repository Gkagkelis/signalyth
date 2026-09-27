"""v31.19 — the batches of ONE source run in parallel, and stay as safe.

v31.11 put the four comment SOURCES on parallel threads, which stopped
helping the moment one source held many parents: x batches two parents per
Actor call, so forty parents meant twenty calls, one after another, and the
comment layer is the run's wall clock. An 800-record run would have spent
hours there.

Now each source dispatches its batches in waves. The dangerous parts are the
same three as in v31.11, plus one the sequential loop got for free:

1. Money: concurrent batches share the reservation ledger, so they cannot
   jointly overspend.
2. Files: the attempted-ref set, the harvest state file and the audit lists
   are mutated under the adaptive state lock.
3. The two feedback rules of the sequential loop survive — never request more
   than `wanted` in total, stop as soon as the source target is met — because
   waves re-read both and allocate each wave out of what is still wanted.
4. v31.5's promise holds THROUGH a kill: a worker hard-killed inside a wave
   still leaves every batch that completed recorded as attempted, so the
   continuation never re-pays it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services.cleaning import clean_run
from app.services.normalizer import normalize_dataset
from app.services.query_planner import build_collection_plan
from app.services.run_manager import coalescing_checkpointer
import app.services.relevance_expansion as rx
from app.services.storage import RunStore

from tests.test_v31_5_per_call_checkpoint import WorkerKilled, _post, _reply


def _draft_many_parents():
    from datetime import date
    from app.models import AnalysisDraft
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x"], sample_mode="perSource",
        per_source={"x": 8}, per_source_comments={"x": 120},
        comments=True, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", additional_context=["OPAP"], exclusions=[],
    )


def _prepare_many(tmp_path: Path, budget_remaining: float = 4.95):
    """Eight relevant x posts → x batches two parents at a time → 4+ batches."""
    plan = build_collection_plan(_draft_many_parents()).model_dump(mode="json")
    for row in (plan.get("preflight_forecast") or {}).get("sources", []):
        if row.get("source") == "x":
            row.setdefault("comments", {})["status"] = "verified_available"
            row["comments"]["live_verified"] = True
    posts = [
        _post(500, "Allwyn OPAP Greece customer experience review", "person1"),
        _post(501, "Allwyn Greece sponsorship discussion tonight", "person2"),
        _post(502, "Allwyn OPAP Greece payout complaint thread", "person3"),
        _post(503, "Allwyn Greece lottery app opinions today", "person4"),
        _post(504, "Allwyn OPAP Greece store service feedback", "person5"),
        _post(505, "Allwyn Greece jackpot discussion in Athens", "person6"),
        _post(506, "Allwyn OPAP Greece customer support reply", "person7"),
        _post(507, "Allwyn Greece betting shop experience today", "person8"),
    ]
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "raw-x.json", posts)
    rows = normalize_dataset("x", posts)
    store.write(tmp_path / "normalized-x.json", rows)
    store.write(tmp_path / "normalized-all.json", rows)
    store.write(tmp_path / "status.json", {
        "run_id": "T",
        "budget": {"max_usd": 5.0, "spent_usd": round(5.0 - budget_remaining, 6),
                   "remaining_usd": budget_remaining},
    })
    report = clean_run(tmp_path, plan=plan)
    return plan, report, store


class WaveRunner:
    """Answers reply calls after a delay, recording each call's interval."""

    def __init__(self, delay=0.3, charge_full_cap=False, kill_on_reply_call=None):
        self.delay = delay
        self.charge_full_cap = charge_full_cap
        self.kill_on = kill_on_reply_call
        self.intervals: list[tuple[float, float]] = []
        self.reply_refs: list[set[str]] = []
        self._lock = threading.Lock()
        self._n = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        if run_input.get("mode") != "replies":
            return {"usageTotalUsd": 0.0001, "defaultDatasetId": "d"}, []
        with self._lock:
            self._n += 1
            n = self._n
            refs = {str(r) for r in (run_input.get("replyTweetIds") or [])}
            self.reply_refs.append(refs)
        start = time.monotonic()
        time.sleep(self.delay)
        if self.kill_on is not None and n == self.kill_on:
            raise WorkerKilled("platform reclaimed the worker mid comment call")
        with self._lock:
            self.intervals.append((start, time.monotonic()))
        parents = sorted(refs) or ["0"]
        items = [
            _reply(f"9{n}{i}", parents[i % len(parents)],
                   f"Allwyn Greece reply 9{n}{i} opinion about the market")
            for i in range(3)
        ]
        usage = max_charge_usd if self.charge_full_cap else 0.002
        return {"usageTotalUsd": usage, "defaultDatasetId": "d"}, items


def _any_overlap(intervals: list[tuple[float, float]]) -> bool:
    for i, (s1, e1) in enumerate(intervals):
        for s2, e2 in intervals[i + 1:]:
            if s1 < e2 and s2 < e1:
                return True
    return False


def test_batches_of_one_source_run_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "signalyth_comment_parallel_batches", 3)
    plan, report, store = _prepare_many(tmp_path)
    runner = WaveRunner(delay=0.3)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

    assert len(runner.intervals) >= 2, (
        f"expected several comment batches, got {len(runner.intervals)}")
    assert _any_overlap(runner.intervals), (
        "batches of the same source never ran at the same time")
    comments = store.read(tmp_path / "normalized-comments-x.json", []) or []
    assert comments, "the parallel wave collected nothing"


def test_sequential_knob_restores_strict_batch_order(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "signalyth_comment_parallel_batches", 1)
    plan, report, store = _prepare_many(tmp_path)
    runner = WaveRunner(delay=0.15)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    assert len(runner.intervals) >= 2
    assert not _any_overlap(runner.intervals), (
        "signalyth_comment_parallel_batches=1 must keep batches strictly ordered")


def test_parallel_batches_cannot_jointly_overspend_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "signalyth_comment_parallel_batches", 4)
    plan, report, store = _prepare_many(tmp_path, budget_remaining=0.004)
    runner = WaveRunner(delay=0.1, charge_full_cap=True)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    budget = (store.read(tmp_path / "status.json", {}) or {}).get("budget") or {}
    assert float(budget.get("spent_usd") or 0) <= float(budget.get("max_usd") or 0) + 1e-6, budget


def test_a_kill_inside_a_wave_still_records_the_batches_that_finished(tmp_path, monkeypatch):
    """v31.5's no-double-payment promise, now across concurrent siblings."""
    monkeypatch.setattr(settings, "signalyth_comment_parallel_batches", 3)
    plan, report, store = _prepare_many(tmp_path)
    runner = WaveRunner(delay=0.2, kill_on_reply_call=2)

    with pytest.raises(WorkerKilled):
        rx.adaptive_expand_after_cleaning(
            tmp_path, plan, report, runner=runner, checkpoint=lambda step: None,
        )

    state = store.read(tmp_path / "comment-harvest-state.json", {}) or {}
    attempted = set()
    for refs in ((state.get("x") or {}).get("attempted_refs_by_bucket") or {}).values():
        attempted.update(str(r) for r in refs)

    # Every sibling that RETURNED before the kill must be recorded, or the
    # continuation buys its parents a second time.
    survived = set()
    for refs in runner.reply_refs:
        survived.update(refs)
    finished = {r for r in survived if r in attempted}
    assert finished, (
        f"no completed batch was recorded as attempted: refs={runner.reply_refs} "
        f"attempted={attempted}")
    # …and the comments those calls paid for are on disk.
    assert store.read(tmp_path / "normalized-comments-x.json", []), (
        "paid comments from the finished siblings were lost")


# ---------------------------------------------------------------------------
# The archive boundary that parallelism made hot
# ---------------------------------------------------------------------------

def test_coalescing_checkpointer_never_queues_callers_and_still_covers_them():
    builds: list[tuple[float, float]] = []
    build_lock = threading.Lock()

    def build() -> None:
        start = time.monotonic()
        time.sleep(0.15)
        with build_lock:
            builds.append((start, time.monotonic()))

    request = coalescing_checkpointer(build)
    barrier = threading.Barrier(6)
    last_request = [0.0]
    request_lock = threading.Lock()

    def caller() -> None:
        barrier.wait()
        with request_lock:
            last_request[0] = max(last_request[0], time.monotonic())
        request()

    threads = [threading.Thread(target=caller) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert builds, "no archive build ran at all"
    # Coalesced: six simultaneous requests must not cost six serialized builds.
    assert len(builds) <= 3, f"callers queued behind each other: {len(builds)} builds"
    # …and the promise still holds: a build STARTED after the last request.
    assert max(s for s, _ in builds) > last_request[0], (
        "the last request was not covered by a build that started after it")
    # Builds never overlap: the archive is still written one at a time.
    assert not _any_overlap(builds), "two archive builds ran concurrently"
