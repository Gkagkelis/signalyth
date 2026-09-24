from app.models import SourceConfigUpdate
from app.services.comment_deepening import normalize_comment_dataset
from app.services.cleaning import clean_records
from app.services.source_capabilities import (
    build_comment_deepening_input,
    comment_parent_batch_limit,
    comments_forecast,
)


def test_comment_input_shapes_are_actor_specific():
    x = build_comment_deepening_input(
        "x", ["https://x.com/u/status/123"], 7, max_per_parent=7,
    )
    assert x["mode"] == "thread"
    assert x["threadTweetIds"] == ["123"]
    assert x["maxItemsPerTarget"] == 7
    assert build_comment_deepening_input("tiktok", ["https://www.tiktok.com/@u/video/123"], 9)["includeReplies"] is True
    ig = build_comment_deepening_input("instagram", ["https://www.instagram.com/p/ABC/"], 12, max_per_parent=5)
    assert ig == {"postUrls": ["https://www.instagram.com/p/ABC/"], "maxCommentsPerPost": 5, "sortOrder": "recent"}
    fb = build_comment_deepening_input("facebook", ["https://www.facebook.com/x/posts/1"], 12, max_per_parent=6)
    assert fb["resultsLimit"] == 6 and fb["commentsSortType"] == "newest"


def test_instagram_comments_and_nested_replies_become_separate_evidence():
    rows = normalize_comment_dataset("instagram", [{
        "postUrl": "https://www.instagram.com/p/ABC/", "commentId": "c1", "commentUrl": "https://ig/c1",
        "text": "top", "timestamp": 1700000000, "likesCount": 2, "username": "a",
        "replies": [{"commentId": "r1", "text": "reply", "timestamp": 1700000001, "username": "b"}],
    }], seed_refs=["https://www.instagram.com/p/ABC/"])
    assert len(rows) == 2
    assert rows[0]["evidence_layer"] == "comment"
    assert rows[1]["evidence_layer"] == "reply"
    assert rows[1]["parent_comment_id"] == "c1"
    assert all(r["parent_post"] == "https://www.instagram.com/p/ABC/" for r in rows)


def test_tiktok_comment_normalization_keeps_parent_video():
    rows = normalize_comment_dataset("tiktok", [{
        "cid": "55", "text": "service issue", "create_time": 1700000000,
        "digg_count": 3, "reply_comment_total": 1, "aweme_id": "123",
        "user": {"unique_id": "person"},
    }], seed_refs=["https://www.tiktok.com/@u/video/123"])
    assert rows[0]["parent_post"].endswith("/video/123")
    assert rows[0]["text"] == "service issue"
    assert rows[0]["author"] == "person"


def test_facebook_comment_normalization_matches_actor_shape():
    rows = normalize_comment_dataset("facebook", [{
        "commentText": "hello", "id": "fb1", "timestamp": 1742045152000,
        "url": "https://facebook.com/post?comment_id=1", "postUrl": "https://facebook.com/post",
        "author": {"name": "Zack"}, "reactionsCount": "5",
    }], seed_refs=["https://facebook.com/post"])
    assert rows[0]["parent_post"] == "https://facebook.com/post"
    assert rows[0]["author"] == "Zack"
    assert rows[0]["likes"] == 5


def test_x_reply_normalization_is_reply_layer():
    rows = normalize_comment_dataset("x", [{
        "id": "r1", "text": "reply", "createdAt": "2026-01-01T00:00:00Z",
        "authorUsername": "u", "inReplyToId": "123", "url": "https://x.com/u/status/r1",
    }], seed_refs=["123"])
    assert rows[0]["evidence_layer"] == "reply"
    assert rows[0]["parent_post"] == "123"


def test_forecast_allows_curated_configured_actor_when_enabled():
    cfg = {"comment_actor_id": "scraper_one/facebook-comments-scraper", "comment_deepening_status": "configured", "comment_enabled": False}
    assert comments_forecast("facebook", True, cfg)["status"] == "configured_disabled"
    cfg["comment_enabled"] = True
    assert comments_forecast("facebook", True, cfg)["status"] == "configured_available"
    cfg["comment_deepening_status"] = "verified"
    assert comments_forecast("facebook", True, cfg)["status"] == "verified_available"


def test_source_update_model_exposes_comment_controls():
    row = SourceConfigUpdate(comment_enabled=True, comment_max_per_parent=55, comment_max_parents=9)
    assert row.comment_enabled is True
    assert row.comment_max_per_parent == 55
    assert row.comment_max_parents == 9


def test_comment_can_be_contextually_relevant_via_parent_without_repeating_topic():
    rows = [{
        "id": "comment:x:1", "platform": "x", "text": "Εμένα από το πρωί δεν δουλεύει τίποτα",
        "date": "2026-09-13T09:00:00+00:00", "author": "person", "url": "https://x.com/u/status/2",
        "evidence_layer": "reply", "parent_post": "1",
        "parent_context": "Vodafone: μεγάλη διακοπή στο δίκτυο σήμερα στην Ελλάδα",
        "likes": 1, "comments": 0, "shares": 0, "views": 0, "raw_data": {},
    }]
    plan = {
        "client": "Vodafone", "topic": "Vodafone", "market": "Greece",
        "core_terms": ["Vodafone"], "context_terms": ["διακοπή", "δίκτυο"],
        "greeklish_variants": [], "exclusions": [], "target_total": 1,
        "sources": [{"source": "x", "target_items": 1}],
    }
    result = clean_records(rows, plan)
    cleaned = result["cleaned"][0]
    assert "contextual_parent_match" in cleaned["cleaning"]["flags"]
    assert cleaned["cleaning"]["decision"] != "excluded"



def test_comment_actor_parent_batch_contracts_protect_coverage():
    assert comment_parent_batch_limit("x") == 2
    assert comment_parent_batch_limit("tiktok") == 1
    assert comment_parent_batch_limit("instagram") == 5
    assert comment_parent_batch_limit("facebook") == 5


def test_x_thread_normalization_keeps_nested_replies_but_drops_root():
    rows = normalize_comment_dataset("x", [
        {
            "id": "123", "text": "root", "createdAt": "2026-09-18T09:00:00Z",
            "authorUsername": "root", "conversationId": "123",
            "url": "https://x.com/root/status/123",
        },
        {
            "id": "200", "text": "direct", "createdAt": "2026-09-18T10:00:00Z",
            "authorUsername": "a", "conversationId": "123", "sourceTweetId": "123",
            "inReplyToId": "123", "url": "https://x.com/a/status/200",
        },
        {
            "id": "201", "text": "nested", "createdAt": "2026-09-18T10:01:00Z",
            "authorUsername": "b", "conversationId": "123", "sourceTweetId": "123",
            "inReplyToId": "200", "url": "https://x.com/b/status/201",
        },
    ], seed_refs=["123"])
    assert [r["comment_id"] for r in rows] == ["200", "201"]
    assert all(r["parent_post"] == "123" for r in rows)
    assert rows[0]["parent_comment_id"] is None
    assert rows[1]["parent_comment_id"] == "200"


def test_comment_inherits_structured_parent_subject_and_market_without_literal_words():
    rows = normalize_comment_dataset(
        "facebook",
        [{
            "commentText": "πάλι τίποτα", "id": "fb-ctx",
            "timestamp": 1789725600000,
            "postUrl": "https://www.facebook.com/example/posts/123",
            "author": {"name": "Maria"},
        }],
        seed_refs=["https://www.facebook.com/example/posts/123"],
        seed_context={
            "https://www.facebook.com/example/posts/123": {
                "text": "Μεγάλη κλήρωση απόψε",
                "market_score": 0.72,
                "subject_qualified": True,
                "qualification_tier": "provenance",
            }
        },
    )
    plan = {
        "client": "ΟΠΑΠ", "topic": "Eurojackpot", "market": "Greece",
        "core_terms": ["Eurojackpot"], "context_terms": [],
        "greeklish_variants": [], "exclusions": [], "target_total": 1,
        "sources": [{"source": "facebook", "target_items": 1}],
    }
    cleaned = clean_records(rows, plan)["cleaned"][0]
    assert cleaned["cleaning"]["decision"] == "review"
    assert "contextual_parent_match" in cleaned["cleaning"]["flags"]
    assert "parent_market_context" in cleaned["cleaning"]["flags"]
    assert "provenance_parent_context" in cleaned["cleaning"]["flags"]
