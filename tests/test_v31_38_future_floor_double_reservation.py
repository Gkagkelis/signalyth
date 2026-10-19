"""v31.38 — the budget floor reserved for sources that had already paid.

Run 20261019T181713Z turned Facebook away at 129 of 300 posts with
"Budget guard left no safe acquisition budget for this source" while $1.55 of
its $2.50 envelope was still unspent. Facebook's planned cap is large because
its price-per-1000 hint is high; the posts themselves cost $0.0002. Four
sources collect at once on the deployed platform, and a resumed worker
re-enters the unfinished one after the others have finished — so the floor was
still holding Instagram's and news's full planned caps long after both had
spent their money, and the guard counted that spend as well.
"""
from __future__ import annotations

from app.services.collector import _planned_future_floor

SOURCES = [
    {"source": "x", "source_budget_usd": 0.04},
    {"source": "facebook", "source_budget_usd": 0.84},
    {"source": "instagram", "source_budget_usd": 0.70},
    {"source": "news", "source_budget_usd": 0.09},
]


def test_nothing_is_reserved_for_the_last_source():
    assert _planned_future_floor(SOURCES, 3, {}) == 0.0


def test_unstarted_sources_are_still_reserved_for():
    rows = {s["source"]: {"status": "pending"} for s in SOURCES}
    assert _planned_future_floor(SOURCES, 1, rows) == 0.70 + 0.09


def test_a_finished_source_owes_nothing():
    rows = {
        "instagram": {"status": "succeeded"},
        "news": {"status": "pending"},
    }
    assert _planned_future_floor(SOURCES, 1, rows) == 0.09


def test_the_regression_itself_leaves_facebook_its_cap():
    """Facebook re-entered after Instagram and news have both settled."""
    rows = {
        "x": {"status": "succeeded"},
        "facebook": {"status": "running"},
        "instagram": {"status": "succeeded"},
        "news": {"status": "succeeded"},
    }
    floor = _planned_future_floor(SOURCES, 1, rows)
    remaining = 2.50 - 0.946          # what the run actually had left
    assert floor == 0.0
    assert remaining - floor >= 0.84  # ... which is Facebook's own base cap


def test_an_empty_success_also_owes_nothing():
    rows = {"instagram": {"status": "succeeded_empty"}, "news": {"status": "succeeded_empty"}}
    assert _planned_future_floor(SOURCES, 1, rows) == 0.0


def test_a_failed_source_owes_nothing():
    rows = {"instagram": {"status": "failed"}, "news": {"status": "failed"}}
    assert _planned_future_floor(SOURCES, 1, rows) == 0.0


def test_a_partial_source_owes_nothing():
    rows = {"instagram": {"status": "partial"}, "news": {"status": "partial"}}
    assert _planned_future_floor(SOURCES, 1, rows) == 0.0


def test_a_source_the_next_worker_will_re_enter_keeps_its_reservation():
    """`interrupted_time_budget` is not settled: it will ask for money again."""
    rows = {
        "instagram": {"status": "interrupted_time_budget"},
        "news": {"status": "succeeded"},
    }
    assert _planned_future_floor(SOURCES, 1, rows) == 0.70


def test_a_running_source_keeps_its_reservation():
    rows = {"instagram": {"status": "running"}, "news": {"status": "succeeded"}}
    assert _planned_future_floor(SOURCES, 1, rows) == 0.70


def test_missing_rows_are_treated_as_unstarted():
    assert _planned_future_floor(SOURCES, 0, None) == 0.84 + 0.70 + 0.09
