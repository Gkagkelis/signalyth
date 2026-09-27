"""v31.21 — YouTube finally has a comment route.

The Christodoulakis run showed why it mattered: news and youtube reported
"— n=0" in the sentiment table because a video title is coverage, not an
opinion, and YouTube comments — where political audiences actually argue —
were never collected. The capability map had carried a youtube candidate
Actor for months while every gate hardcoded the other four sources, so the
source could only ever read neutral.

The chosen Actor is streamers/youtube-comments-scraper: 24k users, 98.6% of
88k runs succeeded in 30 days, and the only leading YouTube comment Actor
with a NATIVE date filter (oldestCommentDate). apidojo's has no date field,
which makes a bounded research window guesswork.
"""
from __future__ import annotations

from datetime import date

from app.services.comment_deepening import normalize_comment_dataset
from app.services.source_capabilities import (
    COMMENT_CAPABLE_SOURCES,
    build_comment_deepening_input,
    comment_parent_batch_limit,
    comments_forecast,
    is_comment_parent_ref,
    source_capabilities,
)

VIDEO = "https://www.youtube.com/watch?v=xObhZ0Ga7EQ"
VIDEO2 = "https://www.youtube.com/watch?v=AbCdEfGhIjK"

#: Live public input schema, read from the Actor's latest build on 2026-09-27.
LIVE_SCHEMA_FIELDS = {"startUrls", "maxComments", "sortCommentsBy", "oldestCommentDate"}
SORT_ENUM = {"TOP_COMMENTS", "NEWEST_FIRST"}


def test_youtube_is_a_comment_capable_source_everywhere():
    assert "youtube" in COMMENT_CAPABLE_SOURCES
    # The gates that used to hardcode four sources now read the same set.
    from app.services.relevance_expansion import COMMENT_CAPABLE_SOURCES as rx_set
    from app.services.storage import COMMENT_CAPABLE_SOURCES as store_set
    assert rx_set is COMMENT_CAPABLE_SOURCES is store_set


def test_the_comment_input_matches_the_actors_live_schema():
    inp = build_comment_deepening_input(
        "youtube", [VIDEO, VIDEO2], 60,
        max_per_parent=30, date_from=date(2026, 9, 1), date_to=date(2026, 9, 26),
    )
    assert set(inp) <= LIVE_SCHEMA_FIELDS, f"field outside the published schema: {set(inp)}"
    # startUrls is a requestListSources field: objects, never bare strings.
    assert inp["startUrls"] == [{"url": VIDEO}, {"url": VIDEO2}]
    assert inp["maxComments"] == 30           # documented PER VIDEO
    assert inp["sortCommentsBy"] in SORT_ENUM
    assert inp["oldestCommentDate"] == "2026-09-01"


def test_the_window_start_is_pushed_into_the_actor_not_just_filtered_after():
    """The whole reason this Actor won: it can be told the date."""
    inp = build_comment_deepening_input("youtube", [VIDEO], 10, date_from=date(2026, 9, 1))
    assert inp.get("oldestCommentDate") == "2026-09-01"
    # No native upper bound exists, so date_to must NOT be invented as a field.
    assert "newestCommentDate" not in inp and "dateTo" not in inp


def test_only_concrete_videos_are_accepted_as_parents():
    assert is_comment_parent_ref("youtube", VIDEO)
    assert is_comment_parent_ref("youtube", "https://youtu.be/xObhZ0Ga7EQ")
    assert is_comment_parent_ref("youtube", "https://www.youtube.com/shorts/xObhZ0Ga7EQ")
    assert not is_comment_parent_ref("youtube", "https://www.youtube.com/@SomeChannel")
    assert not is_comment_parent_ref("youtube", "https://www.youtube.com/")


def test_parents_batch_so_one_call_covers_several_videos():
    assert comment_parent_batch_limit("youtube") == 5


def test_comments_are_normalized_and_attributed_to_the_right_video():
    rows = [
        {"commentId": "c1", "comment": "Πολύ σωστά τα είπε ο βουλευτής",
         "author": "@user1", "publishedAt": "2026-09-12T10:00:00Z",
         "voteCount": 4, "replyCount": 1, "videoId": "xObhZ0Ga7EQ"},
        {"commentId": "c2", "comment": "Διαφωνώ κάθετα",
         "author": "@user2", "publishedAt": "2026-09-13T11:00:00Z",
         "voteCount": 0, "videoId": "AbCdEfGhIjK"},
    ]
    out = normalize_comment_dataset("youtube", rows, seed_refs=[VIDEO, VIDEO2])
    assert len(out) == 2
    first, second = out
    assert first["platform"] == "youtube"
    assert first["text"].startswith("Πολύ σωστά")
    assert first["date"].startswith("2026-09-12")
    assert first["author"] == "@user1"
    assert first["likes"] == 4
    assert first["evidence_layer"] == "comment"
    # Each comment keeps ITS OWN video, not the first one in the batch.
    assert first["parent_post"] == VIDEO
    assert second["parent_post"] == VIDEO2


def test_a_relative_timestamp_never_becomes_an_invented_date():
    rows = [{"commentId": "c9", "comment": "κάτι", "author": "@u",
             "publishedTimeText": "2 weeks ago", "videoId": "xObhZ0Ga7EQ"}]
    out = normalize_comment_dataset("youtube", rows, seed_refs=[VIDEO])
    assert len(out) == 1 and out[0]["date"] is None


def test_the_route_is_offered_once_the_registry_configures_it():
    cap = source_capabilities("youtube")["comment_deepening"]
    assert cap["candidate_actor_id"] == "streamers/youtube-comments-scraper"
    cfg = {"comment_deepening_status": "configured",
           "comment_actor_id": "streamers/youtube-comments-scraper",
           "comment_enabled": True}
    assert comments_forecast("youtube", True, cfg)["status"] == "configured_available"
