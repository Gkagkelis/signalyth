from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.apify_service import ApifyRunner
from app.services.relevance_expansion import (
    _comment_minimum_attempt_charge_usd,
    _effective_open_comment_quota,
    _seed_ref,
)
from app.services.resilience import (
    ACTOR_RUN_TIMEOUT_SECONDS,
    FACEBOOK_COMMENT_RUN_TIMEOUT_SECONDS,
    _attempt_cap,
    actor_run_timeout_seconds,
)
from app.services.source_capabilities import is_comment_parent_ref


def _tiktok_urls(n=12):
    return [f"https://www.tiktok.com/@example{i}/video/{7600000000000000000+i}" for i in range(n)]


def test_tiktok_event_pricing_floor_covers_query_costs_and_reply_allowance():
    inp = {"startUrls": _tiktok_urls(12), "includeReplies": True, "maxItems": 40}
    floor = _comment_minimum_attempt_charge_usd("tiktok", inp, 40, 0.3)
    # Base query cost is already $0.036. The old code capped this call at $0.015.
    assert floor > 0.036
    assert floor == pytest.approx(0.1848, abs=1e-6)


def test_resilience_attempt_cap_honors_actor_event_floor():
    cap = _attempt_cap(
        1.0, 0.3, 40, 4,
        minimum_attempt_charge_usd=0.1848,
    )
    assert cap == pytest.approx(0.1848, abs=1e-6)


def test_resilience_attempt_cap_never_exceeds_remaining_envelope():
    cap = _attempt_cap(
        0.10, 0.3, 40, 4,
        minimum_attempt_charge_usd=0.1848,
    )
    assert cap == pytest.approx(0.10, abs=1e-6)


def test_facebook_comment_actor_gets_longer_timeout_only_for_that_actor():
    assert actor_run_timeout_seconds("scraper_one/facebook-comments-scraper") == FACEBOOK_COMMENT_RUN_TIMEOUT_SECONDS
    assert FACEBOOK_COMMENT_RUN_TIMEOUT_SECONDS == 420.0
    assert actor_run_timeout_seconds("epctex/tiktok-comment-scraper") == ACTOR_RUN_TIMEOUT_SECONDS
    assert actor_run_timeout_seconds("xquik/x-tweet-scraper") == ACTOR_RUN_TIMEOUT_SECONDS


def test_apify_runner_uses_actor_specific_timeout(monkeypatch):
    captured = {}

    class FakeRun(dict):
        pass

    class FakeActor:
        def call(self, **kwargs):
            captured.update(kwargs)
            return FakeRun(id="r1", defaultDatasetId="d1", status="SUCCEEDED")

    class FakeDataset:
        class Result:
            items = []
        def list_items(self):
            return self.Result()

    class FakeClient:
        def actor(self, actor_id):
            captured["actor_id"] = actor_id
            return FakeActor()
        def dataset(self, dataset_id):
            return FakeDataset()

    runner = object.__new__(ApifyRunner)
    runner.client = FakeClient()
    runner.run(
        "scraper_one/facebook-comments-scraper",
        {"postUrls": ["https://facebook.com/example/posts/123"]},
        max_items=40,
        max_charge_usd=1.0,
    )
    assert captured["run_timeout"] == timedelta(seconds=420)


def test_instagram_shortcode_can_become_concrete_parent():
    row = {
        "platform": "instagram",
        "url": "https://www.instagram.com/explore/tags/example/",
        "raw_data": {
            "shortCode": "DX7lzTOJ1p6",
            "inputUrl": "https://www.instagram.com/explore/tags/example/",
        },
    }
    assert _seed_ref("instagram", row) == "https://www.instagram.com/p/DX7lzTOJ1p6/"


def test_instagram_profile_is_still_not_a_comment_parent():
    assert not is_comment_parent_ref("instagram", "https://www.instagram.com/example/")
    assert is_comment_parent_ref("instagram", "https://www.instagram.com/p/DX7lzTOJ1p6/")


def test_open_search_absorbs_owned_shortfall_without_changing_configured_split():
    # Target 40, configured open share 16. Owned pass returned 0 -> open may try all 40.
    assert _effective_open_comment_quota(40, 16, 0) == 40
    # Owned pass returned 18 -> open gets the remaining 22 rather than staying capped at 16.
    assert _effective_open_comment_quota(40, 16, 18) == 22
    # If owned did better than its reservation, configured open quota remains available.
    assert _effective_open_comment_quota(40, 16, 30) == 16
