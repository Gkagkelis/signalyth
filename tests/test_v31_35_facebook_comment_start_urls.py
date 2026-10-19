"""v31.35 — dami_studio facebook comments: startUrls is a request list, not strings.

The Actor's `startUrls` field is declared with `editor: requestListSources`
(live schema read from its default build, 19 Oct 2026), so every entry must be
an object carrying a `url` key. Run 20261019T170033Z sent bare strings under
128 parents and collected ZERO comments — the same silent, paid-for nothing the
wrong field name produced two runs earlier.

The smoke input must be built against the same Actor for the same reason: a
smoke that validates a shape no production run sends validates nothing.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.source_capabilities import (
    build_comment_deepening_input,
    build_comment_smoke_input,
)

DAMI = "dami_studio/facebook-comments-scraper"
POSTS = [
    "https://www.facebook.com/snfcc/posts/pfbid02abcdefghijklmnopqrstuvwxyz0123456789",
    "https://www.facebook.com/snfcc/posts/pfbid02zyxwvutsrqponmlkjihgfedcba9876543210",
]


def test_start_urls_entries_are_objects_with_a_url_key():
    out = build_comment_deepening_input("facebook", POSTS, 300, actor_id=DAMI)
    assert out["startUrls"] == [{"url": POSTS[0]}, {"url": POSTS[1]}]


def test_no_bare_string_survives_in_start_urls():
    out = build_comment_deepening_input("facebook", POSTS, 300, actor_id=DAMI)
    assert all(isinstance(entry, dict) for entry in out["startUrls"])


def test_view_option_is_a_documented_enum_value():
    out = build_comment_deepening_input("facebook", POSTS, 300, actor_id=DAMI)
    assert out["viewOption"] in {"RANKED_UNFILTERED", "RANKED_THREADED", "RECENT_ACTIVITY"}


def test_no_comments_sort_type_on_the_dami_studio_route():
    out = build_comment_deepening_input("facebook", POSTS, 300, actor_id=DAMI)
    assert "commentsSortType" not in out
    assert "postUrls" not in out


def test_legacy_actor_still_gets_the_flat_post_urls_shape():
    out = build_comment_deepening_input(
        "facebook", POSTS, 300, actor_id="scraper_one/facebook-comments-scraper"
    )
    assert out["postUrls"] == POSTS
    assert "startUrls" not in out


def test_native_lower_bound_is_still_sent():
    out = build_comment_deepening_input(
        "facebook", POSTS, 300, actor_id=DAMI, date_from=date(2026, 4, 19)
    )
    assert out["onlyCommentsNewerThan"] == "2026-04-19"


def test_replies_fields_unchanged():
    out = build_comment_deepening_input("facebook", POSTS, 300, actor_id=DAMI)
    assert out["includeNestedComments"] is True
    assert 1 <= out["maxRepliesPerComment"] <= 1000


def test_smoke_input_builds_the_same_shape_as_the_production_route():
    smoke = build_comment_smoke_input("facebook", POSTS, 10, actor_id=DAMI)
    assert smoke["startUrls"] == [{"url": POSTS[0]}, {"url": POSTS[1]}]
    assert "postUrls" not in smoke


def test_smoke_input_without_an_actor_keeps_the_legacy_shape():
    smoke = build_comment_smoke_input("facebook", POSTS, 10)
    assert "postUrls" in smoke


def test_smoke_input_stays_capped():
    smoke = build_comment_smoke_input("facebook", POSTS, 500, actor_id=DAMI)
    assert smoke["resultsLimit"] <= 10


@pytest.mark.parametrize("source", ["instagram", "tiktok", "x"])
def test_other_sources_are_untouched_by_the_actor_hint(source):
    refs = {
        "instagram": ["https://www.instagram.com/p/Cabcdefghij/"],
        "tiktok": ["https://www.tiktok.com/@snfcc/video/7300000000000000000"],
        "x": ["https://x.com/snfcc/status/1800000000000000000"],
    }[source]
    without = build_comment_deepening_input(source, refs, 50)
    with_hint = build_comment_deepening_input(source, refs, 50, actor_id=DAMI)
    assert without == with_hint
