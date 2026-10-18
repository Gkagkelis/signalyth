"""v31.31 — a route that came back FULL is re-asked over narrower windows.

Measured on run 20261017T174324Z, every route asking for the whole six-month
window at once:

    facebook   asked 31  ->  5, 5, 5, 5, 5, 5, 5, 5     a hard per-call ceiling
    news       asked  8  ->  2, 2, 2, 2, 2, 2, 2        a hard per-call ceiling
    youtube    asked  8  ->  8, 8, 8, 8, 8, 8           cut off at what we asked
    x          asked 28  ->  20, 14, 15, 27, 8, 15, 10  no ceiling
    instagram  asked 250 ->  24, 23, 18                 no ceiling

Where the ceiling binds, the WINDOW is the bottleneck and not the subject: the
same query over April alone returns April's ceiling. So a full route is split
and re-asked. Where it does not bind the search already returned everything it
holds, and a split would pay twice for the same emptiness — so nothing is
split there, which is what keeps this from doubling the bill for nothing.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.time_slicing import (
    apply_window,
    detect_call_ceiling,
    halve,
    native_date_window,
    returned_at_ceiling,
)


X_INPUT = {
    "mode": "search", "searchTerms": ["ΚΠΙΣΝ"],
    "since": "2026-04-17_00:00:00_UTC", "until": "2026-10-18_00:00:00_UTC",
}
NEWS_INPUT = {"query": ["ΚΠΙΣΝ"], "fromDate": "2026-04-17", "toDate": "2026-10-17"}
INSTAGRAM_INPUT = {
    "directUrls": ["https://www.instagram.com/explore/tags/snfcc/"],
    "onlyPostsNewerThan": "2026-04-17",
}

PLAN_TO = date(2026, 10, 17)


# ---------- 1. is the call full, or is that all there is ----------

@pytest.mark.parametrize("asked,got,ceiling,full", [
    (8, 8, None, True),      # youtube: cut off at exactly what we asked
    (28, 20, None, False),   # x: ran out well short of the ask
    (250, 24, None, False),  # instagram: ran out
    (8, 9, None, True),      # more than asked is still full
    (0, 0, None, False),     # nothing asked, nothing to conclude
    (31, 5, 5, True),        # facebook: below our ask, but AT the Actor's ceiling
    (8, 2, 2, True),         # news: the same shape
    (31, 4, 5, False),       # below the ceiling too: it really did run out
    (31, 5, None, False),    # no ceiling established yet — do not guess
])
def test_only_a_filled_call_counts_as_cut_off(asked, got, ceiling, full):
    assert returned_at_ceiling(asked, got, ceiling) is full


# ---------- 1b. inferring the Actor's own ceiling ----------

def test_a_constant_return_across_queries_reveals_a_ceiling():
    """Eight different Facebook queries each returning exactly 5 is a property
    of the Actor, not a fact about the world."""
    assert detect_call_ceiling([5, 5, 5, 5, 5, 5, 5, 5]) == 5
    assert detect_call_ceiling([2, 2, 2, 2, 2, 2, 2]) == 2


def test_varying_returns_reveal_no_ceiling():
    """X returned 20, 14, 15, 27, 8 — that is a subject running out, and
    slicing it would pay twice for the same emptiness."""
    assert detect_call_ceiling([20, 14, 15, 27, 8, 15, 10]) is None


def test_too_few_routes_to_conclude_anything():
    assert detect_call_ceiling([5, 5]) is None, "two agreeing routes may be coincidence"
    assert detect_call_ceiling([]) is None


def test_zeroes_are_not_a_ceiling():
    assert detect_call_ceiling([0, 0, 0, 0]) is None


# ---------- 2. reading the window out of an Actor input ----------

def test_an_exclusive_upper_bound_is_recognised():
    """X is built with `until` one day past the last day. A slice written any
    other way would silently drop a day at every boundary."""
    window = native_date_window(X_INPUT, PLAN_TO)
    assert window["from"] == "2026-04-17"
    assert window["to"] == "2026-10-17", "the inclusive last day, not the exclusive bound"
    assert window["to_exclusive"] is True
    assert window["from_style"] == "x_stamp"


def test_a_plain_iso_window_is_recognised():
    window = native_date_window(NEWS_INPUT, PLAN_TO)
    assert (window["from"], window["to"]) == ("2026-04-17", "2026-10-17")
    assert window["to_exclusive"] is False


def test_an_input_with_no_upper_bound_is_not_sliceable():
    """Instagram's `onlyPostsNewerThan` has no counterpart. Narrowing the lower
    bound alone would WIDEN the result, not narrow it."""
    assert native_date_window(INSTAGRAM_INPUT, PLAN_TO) is None


# ---------- 3. the halves tile the window exactly ----------

def test_the_two_halves_leave_no_gap_and_no_overlap():
    window = native_date_window(X_INPUT, PLAN_TO)
    (a_from, a_to), (b_from, b_to) = halve(window)
    assert a_from == date(2026, 4, 17)
    assert b_to == date(2026, 10, 17)
    assert (b_from - a_to).days == 1, "the second half starts the day after the first ends"


def test_each_half_is_written_back_in_the_actors_own_format():
    window = native_date_window(X_INPUT, PLAN_TO)
    first, second = halve(window)

    child_a = apply_window(X_INPUT, window, *first)
    child_b = apply_window(X_INPUT, window, *second)

    # The exclusive upper bound is preserved, so the halves still tile exactly.
    assert child_a["since"] == "2026-04-17_00:00:00_UTC"
    assert child_a["until"] == "2026-07-18_00:00:00_UTC"
    assert child_b["since"] == "2026-07-18_00:00:00_UTC"
    assert child_b["until"] == "2026-10-18_00:00:00_UTC"
    # Everything that is not a date is carried through untouched.
    assert child_a["searchTerms"] == ["ΚΠΙΣΝ"]
    assert X_INPUT["since"] == "2026-04-17_00:00:00_UTC", "the original must not move"


def test_an_inclusive_window_keeps_its_inclusive_bounds():
    window = native_date_window(NEWS_INPUT, PLAN_TO)
    first, second = halve(window)
    assert apply_window(NEWS_INPUT, window, *first)["toDate"] == "2026-07-17"
    assert apply_window(NEWS_INPUT, window, *second)["fromDate"] == "2026-07-18"


def test_a_window_too_short_to_split_is_left_alone():
    one_day = {"fromDate": "2026-04-17", "toDate": "2026-04-17"}
    assert halve(native_date_window(one_day, PLAN_TO)) == []


# ---------- 4. end to end in a real collection ----------

def _run(monkeypatch, tmp_path, *, fills_up: bool):
    """A source whose Actor either fills every call, or comes back short."""
    import app.services.collector as collector
    from app.models import AnalysisDraft
    from app.services.query_planner import build_collection_plan
    from app.services.storage import RunStore

    draft = AnalysisDraft(
        client="ΚΠΙΣΝ", topic="ΚΠΙΣΝ", market="Greece",
        date_from=date(2026, 4, 17), date_to=date(2026, 10, 17),
        keywords=["SNFCC"], keyword_roles={"SNFCC": "alias"},
        sources=["news"], sample_mode="perSource", per_source={"news": 400},
        comments=False, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", place_subject=True,
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "status.json", {"run_id": "T"})

    seen_windows: list[tuple[str, str]] = []
    CEILING = 5

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            seen_windows.append((run_input.get("fromDate"), run_input.get("toDate")))
            # Filling up is the Facebook case: the same small count every
            # time, whatever we ask. Coming back short is the X case: the
            # search ran out, and the counts vary.
            n = CEILING if fills_up else max(1, 7 - len(seen_windows))
            return (
                {"usageTotalUsd": 0.0001, "defaultDatasetId": "d"},
                [{"id": f"{len(seen_windows)}-{i}", "text": "ωραίο μέρος",
                  "date": "2026-07-01", "url": f"https://e/{len(seen_windows)}/{i}"}
                 for i in range(n)],
            )

    monkeypatch.setattr(collector, "ApifyRunner", Runner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)
    status = collector.execute_plan(plan, "T", tmp_path)
    return status, seen_windows


def test_a_full_route_is_re_asked_over_narrower_windows(tmp_path, monkeypatch):
    # Every call fills up, so every route is cut off and must be split.
    status, windows = _run(monkeypatch, tmp_path, fills_up=True)

    narrowed = [w for w in windows if w != ("2026-04-17", "2026-10-17")]
    assert narrowed, f"a filled route must be re-asked over halves: {windows}"

    row = status["sources"]["news"]
    slices = [sr for sr in row["subruns"] if sr.get("slice_of")]
    assert slices, "the split routes must be visible in the run's own record"
    assert all(sr["slice_window"]["from"] >= "2026-04-17" for sr in slices)
    assert row["subruns_total"] == len(row["subruns"])


def test_a_route_that_ran_out_is_never_re_asked(tmp_path, monkeypatch):
    """The cost guard: splitting a route that already returned everything pays
    twice for the same emptiness."""
    status, windows = _run(monkeypatch, tmp_path, fills_up=False)

    assert all(w == ("2026-04-17", "2026-10-17") for w in windows), (
        f"nothing may be re-asked when the calls came back short: {windows}")
    assert not [sr for sr in status["sources"]["news"]["subruns"] if sr.get("slice_of")]


def test_the_number_of_extra_routes_is_bounded(tmp_path, monkeypatch):
    """Every slice is a paid call, so recursion cannot run away."""
    from app.services.collector import MAX_SLICE_ROUTES_PER_SOURCE

    status, _windows = _run(monkeypatch, tmp_path, fills_up=True)
    row = status["sources"]["news"]
    added = len([sr for sr in row["subruns"] if sr.get("slice_of")])
    assert added <= MAX_SLICE_ROUTES_PER_SOURCE, added
