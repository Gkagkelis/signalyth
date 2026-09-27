"""v31.21 — YouTube finally has a comment route.

The Christodoulakis run showed why it mattered: news and youtube reported
"— n=0" in the sentiment table because a video title is coverage, not an
opinion, and YouTube comments — where political audiences actually argue —
were never collected. The capability map had carried a youtube candidate
Actor for months while every gate hardcoded the other four sources, so the
source could only ever read neutral.

Actor choice was settled by paid smoke tests on 2026-09-27, not by store
popularity. The field the pipeline cannot do without is an ABSOLUTE comment
date: streamers (24k users, and the only one with a native date INPUT
filter) returns only publishedTimeText — "2 weeks ago" — so every row would
fail the window and the source would collect nothing. parseforge returns
publishedAt, carries replies, ran 4,612 times in 30 days with zero
failures, and costs $1.30/1k with no minimum run charge.
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
LIVE_SCHEMA_FIELDS = {"startUrls", "maxItems", "includeReplies",
                      "maxRepliesPerComment", "aiEnhancement"}


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
    assert inp["maxItems"] == 60              # documented PER RUN, not per video
    assert inp["includeReplies"] is True
    assert inp["maxRepliesPerComment"] == 5


def test_the_actors_own_ai_scoring_is_never_paid_for():
    """aiEnhancement nearly doubles the per-comment price to add sentiment
    SIGNALYTH computes itself, under its own audited rules."""
    inp = build_comment_deepening_input("youtube", [VIDEO], 10)
    assert inp["aiEnhancement"] is False


def test_no_date_field_is_invented_for_an_actor_that_has_none():
    """parseforge has no date input; the window is enforced after
    normalization, exactly as it is for facebook comments."""
    inp = build_comment_deepening_input(
        "youtube", [VIDEO], 10, date_from=date(2026, 9, 1), date_to=date(2026, 9, 26))
    assert not {"oldestCommentDate", "newestCommentDate", "dateFrom", "dateTo"} & set(inp)


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
        # The exact shape the paid smoke returned from parseforge.
        {"commentId": "c1", "text": "Πολύ σωστά τα είπε ο βουλευτής",
         "authorName": "@user1", "publishedAt": "2026-09-12T10:00:00Z",
         "publishedTimeText": "2 weeks ago", "likeCount": 4, "replyCount": 1,
         "videoId": "xObhZ0Ga7EQ", "videoUrl": VIDEO},
        {"commentId": "c2", "text": "Διαφωνώ κάθετα",
         "authorName": "@user2", "publishedAt": "2026-09-13T11:00:00Z",
         "publishedTimeText": "2 weeks ago", "likeCount": 0,
         "videoId": "AbCdEfGhIjK", "videoUrl": VIDEO2},
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
    """publishedTimeText alone is not a date. Anchoring it to "now" would
    invent evidence timestamps, so the row simply has no date and falls out
    of the window."""
    rows = [{"commentId": "c9", "text": "κάτι", "authorName": "@u",
             "publishedTimeText": "2 weeks ago", "videoId": "xObhZ0Ga7EQ"}]
    out = normalize_comment_dataset("youtube", rows, seed_refs=[VIDEO])
    assert len(out) == 1 and out[0]["date"] is None


def test_the_route_is_offered_once_the_registry_configures_it():
    cap = source_capabilities("youtube")["comment_deepening"]
    assert cap["candidate_actor_id"] == "parseforge/youtube-comments-scraper"
    cfg = {"comment_deepening_status": "configured",
           "comment_actor_id": "parseforge/youtube-comments-scraper",
           "comment_enabled": True}
    assert comments_forecast("youtube", True, cfg)["status"] == "configured_available"
