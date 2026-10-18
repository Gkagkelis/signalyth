"""Adaptive time slicing: ask a narrower window when a route came back full.

Discovery Actors cap how much one call returns. Measured on run
20261017T174324Z, asking each route for a whole six-month window:

    facebook   asked 31  ->  5, 5, 5, 5, 5, 5, 5, 5     (a hard ceiling)
    news       asked  8  ->  2, 2, 2, 2, 2, 2, 2        (a hard ceiling)
    youtube    asked  8  ->  8, 8, 8, 8, 8, 8           (we asked too little)
    x          asked 28  ->  20, 14, 15, 27, 8, 15, 10  (no ceiling: that is all there is)
    instagram  asked 250 ->  24, 23, 18                 (no ceiling)

Where the ceiling binds, the window is the bottleneck, not the subject: the
same query over January alone returns January's ceiling, and over February
February's. So a route that comes back FULL is re-asked over halves of its
window, recursively, until the halves come back under the ceiling.

Where no ceiling binds — x and Instagram above — slicing buys nothing and is
not attempted: those routes already returned everything the search has.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, timedelta

#: Native from/to field pairs used by the discovery Actors we map. A pair is
#: only sliceable when BOTH ends are expressible: Instagram's
#: `onlyPostsNewerThan` has no upper bound, so a narrower window there would
#: silently widen the result instead of narrowing it.
DATE_FIELD_PAIRS = (
    ("since", "until"),
    ("startDate", "endDate"),
    ("fromDate", "toDate"),
    ("publishedAfter", "publishedBefore"),
    ("dateFrom", "dateTo"),
)

_X_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{2}:\d{2}:\d{2}_[A-Z]+$")
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}")

#: A window is not split below this many days: a one-day slice is the floor,
#: and below ~2 days the per-call minimum charges cost more than the evidence.
MIN_SLICE_DAYS = 1
#: Halving depth. 3 turns a six-month window into roughly three-week slices,
#: 4 into ten-day ones. Each level doubles the calls, so this is a cost dial.
MAX_SLICE_DEPTH = 3


def _parse(value) -> tuple[date, str] | None:
    """Read a date out of an Actor input value, remembering how it was written."""
    text = str(value or "").strip()
    for pattern, style in ((_X_STAMP, "x_stamp"), (_ISO_DATE, "iso"), (_ISO_DATETIME, "iso_datetime")):
        match = pattern.match(text)
        if match:
            try:
                return date.fromisoformat(match.group(1)), style
            except ValueError:
                return None
    return None


def _render(value: date, style: str) -> str:
    if style == "x_stamp":
        return f"{value.isoformat()}_00:00:00_UTC"
    if style == "iso_datetime":
        return f"{value.isoformat()}T00:00:00"
    return value.isoformat()


def native_date_window(inp: dict, plan_to: date | None = None) -> dict | None:
    """Describe the date window an Actor input already carries, or None.

    `plan_to` is the run's own last day. When the input's upper bound sits one
    day past it, that bound is exclusive (X is built that way) and every slice
    must be written the same way, or the last day of each slice is lost.
    """
    if not isinstance(inp, dict):
        return None
    for from_field, to_field in DATE_FIELD_PAIRS:
        if from_field not in inp or to_field not in inp:
            continue
        start = _parse(inp.get(from_field))
        end = _parse(inp.get(to_field))
        if not start or not end:
            continue
        start_date, start_style = start
        end_date, end_style = end
        if end_date < start_date:
            continue
        exclusive = bool(plan_to) and end_date == plan_to + timedelta(days=1)
        return {
            "from_field": from_field,
            "to_field": to_field,
            "from_style": start_style,
            "to_style": end_style,
            "to_exclusive": exclusive,
            "from": start_date.isoformat(),
            "to": (end_date - timedelta(days=1) if exclusive else end_date).isoformat(),
        }
    return None


def halve(window: dict) -> list[tuple[date, date]]:
    """Split an inclusive window into two, or [] when it is too short."""
    start = date.fromisoformat(window["from"])
    end = date.fromisoformat(window["to"])
    span = (end - start).days + 1
    if span < 2 * MIN_SLICE_DAYS:
        return []
    midpoint = start + timedelta(days=span // 2 - 1)
    return [(start, midpoint), (midpoint + timedelta(days=1), end)]


def apply_window(inp: dict, window: dict, start: date, end: date) -> dict:
    """A copy of the input asking for [start, end] instead of its own window."""
    out = deepcopy(inp)
    out[window["from_field"]] = _render(start, window["from_style"])
    upper = end + timedelta(days=1) if window.get("to_exclusive") else end
    out[window["to_field"]] = _render(upper, window["to_style"])
    return out


#: How many routes must agree before an identical return count is read as the
#: Actor's own per-call ceiling rather than a coincidence.
CEILING_AGREEMENT = 3


def detect_call_ceiling(returns: list[int]) -> int | None:
    """The Actor's per-call ceiling, inferred from routes that all filled up.

    Facebook answers eight DIFFERENT queries with exactly 5 results each, and
    Google News answers seven with exactly 2. Neither ever reaches the number
    we asked for, so "returned what we asked" misses both — yet a constant,
    non-zero count across unrelated queries is not a coincidence about the
    world, it is a property of the Actor. Three agreeing routes are required
    so that a quiet subject returning 1, 1, 1 by chance is not mistaken for a
    ceiling before the evidence is there.
    """
    counts = [int(n) for n in returns if isinstance(n, (int, float)) and int(n) > 0]
    if len(counts) < CEILING_AGREEMENT:
        return None
    recent = counts[-CEILING_AGREEMENT:]
    return recent[0] if len(set(recent)) == 1 else None


def returned_at_ceiling(requested: int, returned: int, call_ceiling: int | None = None) -> bool:
    """Did the Actor stop because the call filled up, rather than run out?

    Two ways a call fills up. It returns everything we asked for — YouTube
    answering 8 of 8 — or it returns the Actor's own ceiling, below our ask,
    which `detect_call_ceiling` infers from routes that all came back with the
    same count. Anything short of both ran out, and re-asking it would pay
    twice for the same emptiness.
    """
    try:
        requested = int(requested)
        returned = int(returned)
    except (TypeError, ValueError):
        return False
    if returned <= 0:
        return False
    if requested > 0 and returned >= requested:
        return True
    return bool(call_ceiling) and returned >= int(call_ceiling)
