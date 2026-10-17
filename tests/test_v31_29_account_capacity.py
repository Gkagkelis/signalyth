"""v31.29 — a full account is not a broken Actor.

The third Apify account is on a free plan, which allows 5 concurrent Actor
runs. Four parallel sources plus a comment call cross that line routinely, and
Apify answers:

    "By launching this job you will exceed your limit of 5 concurrent Actor
     runs. Please consider an upgrade to a paid plan at ..."

Three things went wrong with that one sentence:

1. It matched none of the transient markers, so it classified as "unknown" —
   which the collector reads as a PERMANENT Actor failure.
2. That opened the source circuit breaker, and run 20261017T172315Z lost all
   13 of its X routes to a single message: 1 failed, 12 skipped_circuit_open,
   0 records.
3. The attempt was charged at the cap although the job never launched, so a
   refusal that cost nothing still ate the source's budget.

The job was never even queued, and the queue drains on its own as our own runs
finish. So it is now its own classification: wait, retry, charge nothing, and
never hold it against the Actor.
"""
from __future__ import annotations

import pytest

from app.services import resilience
from app.services.resilience import classify_exception, run_actor_resilient


CONCURRENCY = (
    "By launching this job you will exceed your limit of 5 concurrent Actor runs. "
    "Please consider an upgrade to a paid plan at "
    "https://console.apify.com/billing/subscription to increase it."
)


# ---------- 1. classification ----------

def test_a_concurrency_refusal_is_capacity_not_permanent():
    assert classify_exception(Exception(CONCURRENCY)) == "capacity"


def test_capacity_is_decided_before_the_permanent_markers():
    """The provider's own wording contains "paid plan", which reads as a
    permission problem to the permanent markers. Capacity must win."""
    assert "plan" in CONCURRENCY
    assert classify_exception(Exception(CONCURRENCY)) == "capacity"
    # And the ordinary classifications are untouched.
    assert classify_exception(Exception("401 Unauthorized")) == "permanent"
    assert classify_exception(Exception("429 rate limit")) == "transient"
    assert classify_exception(Exception("something nobody has seen")) == "unknown"


@pytest.mark.parametrize("message", [
    "By launching this job you will exceed your limit of 5 concurrent Actor runs.",
    "Actor run exceeds the memory limit of this plan",
    "You have exceeded the maximum number of concurrent runs",
])
def test_every_account_capacity_wording_classifies_the_same(message):
    assert classify_exception(Exception(message)) == "capacity"


# ---------- 2. it waits and retries, and costs nothing ----------

def _run_with_failures(failures: int, *, max_calls: int = 8):
    """An Actor that refuses for capacity `failures` times, then succeeds."""
    calls = {"n": 0}
    slept: list[float] = []

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise RuntimeError(CONCURRENCY)
            return (
                {"usageTotalUsd": 0.002, "defaultDatasetId": "d"},
                [{"id": "1", "text": "καλό", "date": "2026-07-01", "url": "https://e/1"}],
            )

    result = run_actor_resilient(
        Runner(), "xquik/x-tweet-scraper", {"searchTerms": ["ΚΠΙΣΝ"]},
        max_items=10, max_charge_usd=0.05, max_calls=max_calls,
        sleep_fn=slept.append,
    )
    return result, calls["n"], slept


def test_a_capacity_refusal_waits_and_then_succeeds():
    result, calls, slept = _run_with_failures(2)
    assert calls == 3, "it must retry rather than give the route up"
    assert result.items, "the evidence is collected once a slot frees up"
    assert slept and slept[0] >= 4.0, (
        f"it must actually wait for a slot, not hammer the limit: {slept}")
    assert slept == sorted(slept), f"the wait must back off, not shrink: {slept}"
    assert "waited_for_account_capacity" in result.adaptive_actions


def test_a_refused_job_is_never_charged():
    """The job did not launch. Charging the cap for it starves later routes."""
    result, _calls, _slept = _run_with_failures(2)
    assert result.accounted_cost_usd == pytest.approx(0.002, abs=1e-6), (
        f"only the successful call may be accounted: {result.accounted_cost_usd}")


def test_capacity_alone_never_reads_as_a_permanent_route_failure():
    result, _calls, _slept = _run_with_failures(1)
    assert "capacity" in result.failure_kinds
    assert "permanent" not in result.failure_kinds
    assert "unknown" not in result.failure_kinds
    assert result.status in {"succeeded", "partial"}


def test_the_retry_budget_is_bounded():
    """A genuinely saturated account must not spin forever."""
    result, calls, slept = _run_with_failures(99, max_calls=4)
    assert calls <= 4, f"max_calls must still bound the attempts: {calls}"
    assert not result.items
    assert result.accounted_cost_usd == 0.0, "nothing launched, nothing charged"


# ---------- 3. the circuit breaker ignores it ----------

def test_capacity_does_not_open_the_source_circuit_breaker(tmp_path, monkeypatch):
    """The defect verbatim: one refusal cost 12 healthy routes."""
    from datetime import date
    import app.services.collector as collector
    from app.models import AnalysisDraft
    from app.services.query_planner import build_collection_plan
    from app.services.storage import RunStore

    draft = AnalysisDraft(
        client="ΚΠΙΣΝ", topic="ΚΠΙΣΝ", market="Greece",
        date_from=date(2026, 4, 17), date_to=date(2026, 10, 17),
        keywords=["SNFCC", "Σταύρος Νιάρχος"],
        keyword_roles={"SNFCC": "alias", "Σταύρος Νιάρχος": "alias"},
        sources=["x"], sample_mode="perSource", per_source={"x": 40},
        comments=False, max_budget_usd=2.0, smart_search=True,
        report_language="Ελληνικά", place_subject=True,
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "status.json", {"run_id": "T"})

    state = {"n": 0}

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            state["n"] += 1
            # The first two logical batches are refused for capacity, exactly
            # as the live run was; everything after that is healthy.
            if state["n"] <= 2:
                raise RuntimeError(CONCURRENCY)
            return (
                {"usageTotalUsd": 0.0005, "defaultDatasetId": "d"},
                [{"id": f"{state['n']}", "text": "ωραίο μέρος",
                  "date": "2026-07-01", "url": f"https://e/{state['n']}"}],
            )

    monkeypatch.setattr(collector, "ApifyRunner", Runner)
    monkeypatch.setattr(collector.settings, "signalyth_dry_run", False, raising=False)
    monkeypatch.setattr(resilience, "CAPACITY_BACKOFF_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(resilience, "CAPACITY_BACKOFF_CAP_SECONDS", 0.0, raising=False)

    status = collector.execute_plan(plan, "T", tmp_path)

    row = status["sources"]["x"]
    skipped = [sr for sr in row["subruns"] if sr.get("status") == "skipped_circuit_open"]
    assert not skipped, (
        f"a full account must not skip healthy routes: {len(skipped)} skipped")
    assert not (row.get("circuit_breaker") or {}).get("open"), row.get("circuit_breaker")
    assert row["collected"] > 0, "the routes that ran must still deliver evidence"


# ---------- 4. an exhausted balance is zero-cost and final ----------

CREDIT = (
    "Your remaining usage of $0.003762 this billing cycle isn't enough for this run. "
    "Increase your limit by upgrading to a paid plan at https://console.apify.com/billing"
)


def test_an_exhausted_balance_is_its_own_classification():
    assert classify_exception(Exception(CREDIT)) == "credit_exhausted"
    # Both messages offer a paid plan; only one of them clears by waiting.
    assert classify_exception(Exception(CONCURRENCY)) == "capacity"


def test_an_exhausted_balance_is_never_charged_and_never_retried():
    """Twelve refused comment buckets booked $4.44 of phantom spend against a
    $6 envelope in run 20261017T174324Z, because a job that never launched was
    accounted at its cap."""
    calls = {"n": 0}
    slept: list[float] = []

    class Runner:
        def run(self, actor_id, run_input, *, max_items, max_charge_usd):
            calls["n"] += 1
            raise RuntimeError(CREDIT)

    result = run_actor_resilient(
        Runner(), "apify/instagram-comment-scraper", {"postUrls": ["https://e/1"]},
        max_items=120, max_charge_usd=0.60, max_calls=8, sleep_fn=slept.append,
    )

    assert calls["n"] == 1, "waiting cannot refill a balance; one attempt is enough"
    assert not slept, "there is nothing to wait for"
    assert result.accounted_cost_usd == 0.0, (
        f"a job that never launched must cost nothing: {result.accounted_cost_usd}")
    assert "credit_exhausted" in result.failure_kinds
    assert result.status == "failed"
