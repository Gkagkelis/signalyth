"""v31.36 — an Actor that REFUSES the input never ran, so it must cost nothing.

Run 20261019T170033Z sent bare strings to a `requestListSources` field. Apify
answered every call with "Input is not valid: Field input.startUrls.0 must be
object" — a refusal issued BEFORE any run started, so nothing was scraped and
nothing was really charged. That wording matches none of the permanent markers,
so it classified as "unknown" and the attempt was charged at its cap: nineteen
refusals × $0.15 = $2.69 of a $3 envelope, after which the X and Instagram
comment routes were turned away with `budget_exhausted` while the money they
needed sat accounted against calls that had collected zero rows.
"""
from __future__ import annotations

import pytest

from app.services.resilience import classify_exception, run_actor_resilient

APIFY_REFUSAL = "Input is not valid: Field input.startUrls.0 must be object"


class _RefusingRunner:
    """Every call is refused by the Actor's own input-schema validation."""

    def __init__(self, message: str = APIFY_REFUSAL):
        self.message = message
        self.calls = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls += 1
        raise RuntimeError(self.message)


# ---------- classification ----------

@pytest.mark.parametrize("message", [
    APIFY_REFUSAL,
    "Input is not valid: Field input.resultsLimit must be integer",
    "ApifyApiError: field input.startUrls is required",
    "Actor input did not match the schema",
])
def test_a_schema_refusal_is_its_own_kind(message):
    assert classify_exception(RuntimeError(message)) == "invalid_input"


def test_capacity_and_credit_still_win_over_it():
    assert classify_exception(
        RuntimeError("You exceed your limit of 5 concurrent actor runs")
    ) == "capacity"
    assert classify_exception(
        RuntimeError("Your remaining usage isn't enough for this run")
    ) == "credit_exhausted"


def test_an_ordinary_permission_error_is_still_permanent():
    assert classify_exception(RuntimeError("403 Forbidden")) == "permanent"


def test_an_unrecognised_error_is_still_unknown():
    assert classify_exception(RuntimeError("something went sideways")) == "unknown"


# ---------- the money ----------

def test_a_refused_call_is_charged_nothing():
    runner = _RefusingRunner()
    result = run_actor_resilient(
        runner, "dami_studio/facebook-comments-scraper",
        {"startUrls": ["https://www.facebook.com/x/posts/1"]},
        max_items=300, max_charge_usd=0.15, rate_per_1000=0.25,
        sleep_fn=lambda _s: None,
    )
    assert result.accounted_cost_usd == 0.0


def test_a_refused_call_still_fails_the_route():
    runner = _RefusingRunner()
    result = run_actor_resilient(
        runner, "dami_studio/facebook-comments-scraper",
        {"startUrls": ["https://www.facebook.com/x/posts/1"]},
        max_items=300, max_charge_usd=0.15, rate_per_1000=0.25,
        sleep_fn=lambda _s: None,
    )
    assert result.status == "failed"
    assert "invalid_input" in result.failure_kinds


def test_a_refused_call_is_not_retried():
    runner = _RefusingRunner()
    run_actor_resilient(
        runner, "dami_studio/facebook-comments-scraper",
        {"startUrls": ["https://www.facebook.com/x/posts/1"]},
        max_items=300, max_charge_usd=0.15, rate_per_1000=0.25,
        max_calls=4, sleep_fn=lambda _s: None,
    )
    assert runner.calls == 1


def test_nineteen_refusals_leave_the_envelope_whole():
    """The regression as it actually happened, in miniature."""
    spent = 0.0
    for _ in range(19):
        result = run_actor_resilient(
            _RefusingRunner(), "dami_studio/facebook-comments-scraper",
            {"startUrls": ["https://www.facebook.com/x/posts/1"]},
            max_items=300, max_charge_usd=0.15, rate_per_1000=0.25,
            sleep_fn=lambda _s: None,
        )
        spent += result.accounted_cost_usd
    assert spent == 0.0


def test_an_unknown_exception_is_still_charged_conservatively():
    """The conservative default must survive: only a REFUSAL is free."""
    result = run_actor_resilient(
        _RefusingRunner("something went sideways"), "some/actor", {"urls": ["u"]},
        max_items=300, max_charge_usd=0.15, rate_per_1000=0.25,
        sleep_fn=lambda _s: None,
    )
    assert result.accounted_cost_usd > 0.0
