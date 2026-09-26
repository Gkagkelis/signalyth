"""v31.11 — the per-source comment layers run in parallel, safely.

The Apify calls dominate the comment stage's wall time, and the sources are
independent, yet they ran strictly one after another: four sources cost the
SUM of their durations instead of the slowest one. v31.11 gives each source
its own worker thread. The dangerous part is not the threads — it is the
shared money and the shared files:

1. Budget: every paid call must RESERVE its cap before starting (ledger), or
   two sources spend the same remaining dollar.
2. status.json / harvest state / normalized-all: read-modify-writes must not
   lose another source's update.
3. One cleaning pass for the whole stage (v31.9's economy, kept).
4. signalyth_comment_parallel_sources=1 restores sequential behavior.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.config import settings
from app.services.cleaning import clean_run
from app.services.normalizer import normalize_dataset
from app.services.query_planner import build_collection_plan
import app.services.relevance_expansion as rx
from app.services.storage import RunStore

from tests.test_v31_5_per_call_checkpoint import _post


def _draft2():
    from datetime import date
    from app.models import AnalysisDraft
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x", "tiktok"], sample_mode="perSource",
        per_source={"x": 4, "tiktok": 4},
        per_source_comments={"x": 20, "tiktok": 20},
        comments=True, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", additional_context=["OPAP"], exclusions=[],
    )


def _tiktok_post(i):
    url = f"https://www.tiktok.com/@ttuser{i}/video/70000000{i}"
    return {
        "id": f"tt{i}", "platform": "tiktok",
        "text": f"Allwyn OPAP Greece tiktok take {i} for the market",
        "date": "2026-08-17T12:00:00+00:00", "author": f"ttuser{i}",
        "url": url, "likes": 5, "comments": 3, "shares": 0, "views": 200,
        "metric_availability": {"comments_known": True},
        "raw_data": {"webVideoUrl": url},
    }


def _prepare2(tmp_path: Path, budget_remaining: float = 4.95):
    plan = build_collection_plan(_draft2()).model_dump(mode="json")
    for row in (plan.get("preflight_forecast") or {}).get("sources", []):
        if row.get("source") in ("x", "tiktok"):
            row.setdefault("comments", {})["status"] = "verified_available"
            row["comments"]["live_verified"] = True
    x_raw = [
        _post(400, "Allwyn OPAP Greece customer experience review", "person1"),
        _post(401, "Allwyn Greece sponsorship discussion tonight", "person2"),
        _post(402, "Allwyn OPAP Greece payout complaint thread", "person3"),
        _post(403, "Allwyn Greece lottery app opinions today", "person4"),
    ]
    x_rows = normalize_dataset("x", x_raw)
    tt_rows = [_tiktok_post(i) for i in range(1, 5)]
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "raw-x.json", x_raw)
    store.write(tmp_path / "normalized-x.json", x_rows)
    store.write(tmp_path / "raw-tiktok.json", tt_rows)
    store.write(tmp_path / "normalized-tiktok.json", tt_rows)
    store.write(tmp_path / "normalized-all.json", [*x_rows, *tt_rows])
    store.write(tmp_path / "status.json", {
        "run_id": "T",
        "budget": {"max_usd": 5.0, "spent_usd": round(5.0 - budget_remaining, 6),
                   "remaining_usd": budget_remaining},
    })
    report = clean_run(tmp_path, plan=plan)
    return plan, report, store


class TimelineRunner:
    """Answers comment calls after a fixed delay and records call intervals."""

    def __init__(self, delay=0.4, charge_full_cap=False):
        self.delay = delay
        self.charge_full_cap = charge_full_cap
        self.calls = []  # (kind, start, end)
        self.n = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.n += 1
        start = time.monotonic()
        time.sleep(self.delay)
        usage = max_charge_usd if self.charge_full_cap else 0.0001
        meta = {"usageTotalUsd": usage, "defaultDatasetId": "d"}
        if "replyTweetIds" in run_input:
            parent = str(run_input["replyTweetIds"][0])
            items = [{
                "id": f"xr{self.n}{i}", "text": f"Allwyn Greece reply {self.n}{i}",
                "createdAt": "2026-08-18T10:00:00Z", "authorUsername": f"fan{self.n}{i}",
                "inReplyToId": parent, "url": f"https://x.com/f/status/9{self.n}{i}",
                "type": "reply",
            } for i in range(3)]
            kind = "x-comments"
        elif "postURLs" in run_input:
            items = [{
                "cid": f"ttc{self.n}{i}", "text": f"Allwyn Greece σχόλιο {self.n}{i}",
                "createTime": "2026-08-18T11:00:00Z",
                "user": {"uniqueId": f"ttfan{self.n}{i}"},
            } for i in range(3)]
            kind = "tiktok-comments"
        else:
            items = []
            kind = "probe"
        self.calls.append((kind, start, time.monotonic()))
        return meta, items

    def intervals(self, kind):
        return [(a, b) for k, a, b in self.calls if k == kind]


def _overlaps(a, b):
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


def test_sources_run_in_parallel_and_no_state_is_lost(tmp_path, monkeypatch):
    plan, report, store = _prepare2(tmp_path)
    cleans = []
    real_clean = rx.clean_run
    monkeypatch.setattr(rx, "clean_run",
                        lambda *a, **k: (cleans.append(1) or real_clean(*a, **k)))
    runner = TimelineRunner(delay=0.4)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

    x_iv = runner.intervals("x-comments")
    tt_iv = runner.intervals("tiktok-comments")
    assert x_iv and tt_iv, f"both sources must harvest: {[c[0] for c in runner.calls]}"
    assert _overlaps(x_iv, tt_iv) or _overlaps(x_iv, runner.intervals("probe")), (
        "the two sources never ran concurrently")

    # No source lost the other's status update (the classic parallel RMW bug).
    deep = (store.read(tmp_path / "status.json", {}) or {}).get("comment_deepening") or {}
    assert set(deep) >= {"x", "tiktok"}, deep
    x_comments = store.read(tmp_path / "normalized-comments-x.json", []) or []
    tt_comments = store.read(tmp_path / "normalized-comments-tiktok.json", []) or []
    assert len(x_comments) >= 3 and len(tt_comments) >= 3
    assert deep["x"].get("collected") == len(x_comments)
    assert deep["tiktok"].get("collected") == len(tt_comments)

    # v31.9's economy survives parallelism: ONE clean for the whole stage.
    assert len(cleans) == 1, f"cleaning ran {len(cleans)} times"


def test_parallel_paid_calls_cannot_jointly_overspend_the_budget(tmp_path):
    plan, report, store = _prepare2(tmp_path, budget_remaining=0.004)
    runner = TimelineRunner(delay=0.15, charge_full_cap=True)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    budget = (store.read(tmp_path / "status.json", {}) or {}).get("budget") or {}
    max_usd = float(budget.get("max_usd") or 0)
    spent = float(budget.get("spent_usd") or 0)
    # Without cap reservations two threads each read "remaining 0.004" and
    # jointly spend double it. The ledger makes joint spend <= the envelope.
    assert spent <= max_usd + 1e-6, budget


def test_one_worker_restores_strictly_sequential_sources(tmp_path, monkeypatch):
    plan, report, store = _prepare2(tmp_path)
    monkeypatch.setattr(settings, "signalyth_comment_parallel_sources", 1)
    runner = TimelineRunner(delay=0.2)
    rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    x_all = [(a, b) for k, a, b in runner.calls if k in ("x-comments",)]
    tt_all = [(a, b) for k, a, b in runner.calls if k in ("tiktok-comments",)]
    assert x_all and tt_all
    assert not _overlaps(x_all, tt_all), "sequential mode must not interleave sources"
