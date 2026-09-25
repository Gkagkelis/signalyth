"""An Actor run that never starts must not hold our worker forever.

Run 20260925T094032Z-0e94d435 stopped writing status at 09:45:35 UTC inside
an X comment call and was still silent 25 minutes later. `run_timeout` only
bounds an Actor that is RUNNING; a run parked in READY (no free account
memory while other Actors of ours run) never reaches it, and `call()` waits
indefinitely unless a client-side wait is given.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.apify_service import ApifyRunner
from app.services.resilience import actor_run_timeout_seconds


def _runner(run_status: str, captured: dict, aborted: list):
    class FakeRun(dict):
        pass

    class FakeActor:
        def call(self, **kwargs):
            captured.update(kwargs)
            return FakeRun(id="r1", defaultDatasetId="d1", status=run_status)

    class FakeDataset:
        class Result:
            items = [{"id": "c1"}]

        def list_items(self):
            return self.Result()

    class FakeRunClient:
        def abort(self):
            aborted.append("r1")

    class FakeClient:
        def actor(self, actor_id):
            return FakeActor()

        def dataset(self, dataset_id):
            return FakeDataset()

        def run(self, run_id):
            return FakeRunClient()

    runner = object.__new__(ApifyRunner)
    runner.client = FakeClient()
    return runner


def test_we_stop_waiting_shortly_after_the_actor_timeout():
    captured, aborted = {}, []
    runner = _runner("SUCCEEDED", captured, aborted)
    runner.run("xquik/x-tweet-scraper", {"mode": "replies", "replyTweetIds": ["1"]},
               max_items=10, max_charge_usd=0.1)
    expected = actor_run_timeout_seconds("xquik/x-tweet-scraper") + ApifyRunner.WAIT_MARGIN_SECONDS
    assert captured["wait_duration"] == timedelta(seconds=expected)
    assert captured["run_timeout"] == timedelta(seconds=actor_run_timeout_seconds("xquik/x-tweet-scraper"))
    assert not aborted


@pytest.mark.parametrize("state", ["READY", "RUNNING"])
def test_a_run_still_not_finished_after_the_wait_is_aborted_and_reported(state):
    captured, aborted = {}, []
    runner = _runner(state, captured, aborted)
    with pytest.raises(RuntimeError) as exc:
        runner.run("xquik/x-tweet-scraper", {"mode": "replies", "replyTweetIds": ["1"]},
                   max_items=10, max_charge_usd=0.1)
    assert "timed out" in str(exc.value)
    assert aborted == ["r1"], "the parked run must be aborted so it cannot bill later"


def test_a_finished_run_is_read_normally():
    captured, aborted = {}, []
    runner = _runner("TIMED-OUT", captured, aborted)
    meta, items = runner.run("xquik/x-tweet-scraper", {"mode": "replies", "replyTweetIds": ["1"]},
                             max_items=10, max_charge_usd=0.1)
    assert items == [{"id": "c1"}] and not aborted
