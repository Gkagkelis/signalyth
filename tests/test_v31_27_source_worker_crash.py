"""v31.27 — a source that never ran can no longer hide inside a finished run.

Two live runs on 16 Oct 2026 finished as plain "completed_shortfall" with one
of their two sources stuck at status "running", 0 subruns, $0 spent:

    20261016T102618Z-59f71adc   instagram running 0/3   news succeeded 8
    20261016T092051Z-3ae3f65f   news      running 0/8   youtube succeeded_empty

Three defects lined up:

1. ROOT CAUSE. Every paid route reloads the actor registry, and on Vercel that
   re-downloads source_registry.json and rewrote it IN PLACE. Collection runs
   up to four source threads at once, so one thread could read the file while
   another was halfway through writing it and die on a JSONDecodeError.
2. CONTAINMENT. The parallel pool swallowed that exception into a status line
   that did not even name the source, leaving its row non-terminal.
3. REPORTING. Finalization counts only "failed"/"partial" rows as errors, so a
   row left at "running" made the run look like an ordinary shortfall.
"""
from __future__ import annotations

import inspect
import json
import os
from datetime import date
from pathlib import Path

import pytest

import app.registry as registry
import app.services.collector as collector
from app.models import AnalysisDraft
from app.services.cloud_persistence import CloudPersistence
from app.services.query_planner import build_collection_plan
from app.services.storage import RunStore


# ---------- 1. root cause: restoring a config file is atomic ----------

def test_restore_config_file_never_rewrites_in_place(tmp_path, monkeypatch):
    target = tmp_path / "source_registry.json"
    target.write_text(json.dumps({"news": {"actor_id": "old"}}), encoding="utf-8")

    class FakeResult:
        content = json.dumps({"news": {"actor_id": "new"}}).encode("utf-8")

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            return FakeResult()

    monkeypatch.setattr(CloudPersistence, "enabled", property(lambda self: True))
    cloud = CloudPersistence()
    monkeypatch.setattr(cloud, "_client", lambda: FakeClient(), raising=False)
    monkeypatch.setattr(cloud, "_config_path", lambda name: f"config/{name}", raising=False)

    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src, dst, *a, **kw):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr("app.services.cloud_persistence.os.replace", spy_replace)

    # A truncating write on the destination is exactly what let another thread
    # read half a file; the destination may only appear via an atomic rename.
    written: list[str] = []
    real_write_bytes = Path.write_bytes

    def spy_write_bytes(self, data):
        written.append(str(self))
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy_write_bytes)

    assert cloud.restore_config_file("source_registry.json", target) is True

    assert str(target) not in written, (
        f"the destination must never be written in place: {written}")
    assert replaced and replaced[-1][1] == str(target), (
        f"the restore must finish with an atomic replace onto the target: {replaced}")
    assert json.loads(target.read_text(encoding="utf-8")) == {"news": {"actor_id": "new"}}
    assert not list(tmp_path.glob(".*.tmp")), "no temporary file may be left behind"


def test_registry_read_retries_once_then_fails_loudly(tmp_path, monkeypatch):
    """A torn file is re-fetched, never silently swapped for the shipped baseline.

    The registry decides which Actor a paid route calls, so quietly falling back
    to a superseded mapping would spend real money on the wrong Actor.
    """
    path = tmp_path / "source_registry.json"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"news": {"actor_id": "baseline"}}), encoding="utf-8")
    monkeypatch.setattr(registry, "REGISTRY_PATH", path)
    monkeypatch.setattr(registry, "BASE_REGISTRY_PATH", baseline)

    restores = {"n": 0}

    def fake_ensure(p: Path, base: Path) -> None:
        restores["n"] += 1
        # First restore lands a truncated file; the retry lands a whole one.
        p.write_text(
            '{"news": {"actor_' if restores["n"] == 1
            else json.dumps({"news": {"actor_id": "live"}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(registry, "_ensure_runtime_file", fake_ensure)
    assert registry._read_registry_file() == {"news": {"actor_id": "live"}}
    assert restores["n"] == 2, "a torn read must trigger exactly one fresh restore"

    monkeypatch.setattr(
        registry, "_ensure_runtime_file",
        lambda p, base: p.write_text('{"news": {"actor_', encoding="utf-8"),
    )
    with pytest.raises(RuntimeError) as err:
        registry._read_registry_file()
    assert "No Actor was called" in str(err.value)
    assert "baseline" not in str(err.value)


# ---------- 2 + 3. a crashed source thread is terminal, named and reported ----------

def test_a_crashed_source_is_marked_failed_and_the_run_says_so(tmp_path, monkeypatch):
    draft = AnalysisDraft(
        client="ΚΠΙΣΝ", topic="ΚΠΙΣΝ", market="Greece",
        date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
        keywords=["ΚΠΙΣΝ"], sources=["news", "youtube"], sample_mode="perSource",
        per_source={"news": 2, "youtube": 2}, comments=False, max_budget_usd=2.0,
        smart_search=True, report_language="Ελληνικά",
        additional_context=[], exclusions=[],
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "status.json", {"run_id": "T"})

    class FakeRunner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            return (
                {"usageTotalUsd": 0.0001, "defaultDatasetId": "d"},
                [{"id": "1", "text": "καλό", "date": "2026-09-10", "url": "https://e/1"}],
            )

    monkeypatch.setattr(collector, "ApifyRunner", FakeRunner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)
    monkeypatch.setattr(
        collector.settings, "signalyth_collection_parallel_sources", 2, raising=False)

    real_load = collector.load_registry

    def torn_read_for_news():
        # The production crash, reproduced: the per-route registry read raises
        # on the news worker thread only.
        for frame in inspect.stack():
            if frame.frame.f_locals.get("source") == "news":
                raise json.JSONDecodeError("Expecting value", "{", 1)
        return real_load()

    monkeypatch.setattr(collector, "load_registry", torn_read_for_news)

    status = collector.execute_plan(plan, "T", tmp_path)

    news = status["sources"]["news"]
    youtube = status["sources"]["youtube"]

    assert news["status"] == "failed", (
        f"a source whose worker crashed must be terminal, not {news['status']!r}")
    assert "worker stopped unexpectedly" in (news.get("error") or ""), news.get("error")
    assert "JSONDecodeError" in (news.get("worker_error") or ""), (
        "the crash must be recorded so the next one is diagnosable without logs")

    # The healthy source is untouched and keeps its evidence.
    assert youtube["status"] in {"succeeded", "succeeded_empty", "partial"}

    assert status["collection_status"] == "completed_with_errors", (
        "a run that lost a whole source must not report a plain shortfall")

    # No row may survive finalization outside a terminal state, whatever the cause.
    assert all(row["status"] in collector.SOURCE_TERMINAL
               for row in status["sources"].values()), status["sources"]
    assert (status["progress"]["completed_sources"]
            == status["progress"]["total_sources"])
