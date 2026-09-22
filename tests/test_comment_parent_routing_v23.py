from __future__ import annotations

import pytest

from app.services.relevance_expansion import (
    _comment_seed_refs,
    _post_ref_from_row,
    operator_seed_urls,
)
from app.services.source_capabilities import (
    build_comment_deepening_input,
    is_comment_parent_ref,
)


def _row(source: str, url: str, *, comments=0, relevance=0.0, reasons=None, flags=None):
    return {
        "id": f"{source}-1",
        "platform": source,
        "evidence_layer": "primary",
        "url": url,
        "text": "candidate conversation",
        "comments": comments,
        "likes": 10,
        "metric_availability": {"comments_known": True},
        "cleaning": {
            "decision": "excluded",
            "relevance_score": relevance,
            "market_score": 0.7,
            "spam_score": 0.0,
            "authenticity_status": "low_risk",
            "content_class": "unknown",
            "origin_class": "unknown",
            "organic_eligible": False,
            "reasons": reasons or [],
            "flags": flags or [],
        },
        "raw_data": {},
    }


def test_facebook_page_is_not_a_comment_parent():
    page = "https://facebook.com/allwyngr.official.account"
    post = "https://www.facebook.com/allwyngr.official.account/posts/123456789"
    assert not is_comment_parent_ref("facebook", page)
    assert is_comment_parent_ref("facebook", post)


def test_comment_input_drops_page_and_keeps_post():
    page = "https://facebook.com/allwyngr.official.account"
    post = "https://www.facebook.com/allwyngr.official.account/posts/123456789"
    payload = build_comment_deepening_input("facebook", [page, post], 40, max_per_parent=40)
    assert payload["postUrls"] == [post]


def test_comment_input_refuses_page_only():
    with pytest.raises(ValueError):
        build_comment_deepening_input(
            "facebook",
            ["https://facebook.com/allwyngr.official.account"],
            40,
            max_per_parent=40,
        )


def test_operator_seed_urls_rejects_profile_but_keeps_post():
    plan = {
        "comment_seed_urls": [
            "https://facebook.com/allwyngr.official.account",
            "https://facebook.com/allwyngr.official.account/posts/123456789",
        ]
    }
    assert operator_seed_urls(plan, "facebook") == [
        "https://facebook.com/allwyngr.official.account/posts/123456789"
    ]


def test_page_discovery_prefers_concrete_post_field_over_page_url():
    ref, _ = _post_ref_from_row(
        "facebook",
        {
            "url": "https://facebook.com/allwyngr.official.account",
            "postUrl": "https://facebook.com/allwyngr.official.account/posts/123456789",
            "text": "Eurojackpot post",
        },
    )
    assert ref.endswith("/posts/123456789")


def test_subject_not_mentioned_can_still_seed_conversation():
    row = _row(
        "facebook",
        "https://facebook.com/example/posts/123456789",
        comments=12,
        relevance=0.0,
        reasons=["subject_not_mentioned"],
    )
    refs, _, mode = _comment_seed_refs("facebook", [row], max_seeds=5)
    assert refs == ["https://facebook.com/example/posts/123456789"]
    assert mode == "reported_comments"


def test_hard_quality_exclusions_still_block_paid_comment_scrape():
    row = _row(
        "facebook",
        "https://facebook.com/example/posts/123456789",
        comments=50,
        relevance=0.9,
        reasons=["outside_target_market"],
    )
    refs, _, mode = _comment_seed_refs("facebook", [row], max_seeds=5)
    assert refs == []
    assert mode == "no_relevant_parent_rows"


@pytest.mark.parametrize(
    ("source", "profile", "content"),
    [
        (
            "instagram",
            "https://www.instagram.com/example/",
            "https://www.instagram.com/reel/ABC123/",
        ),
        (
            "tiktok",
            "https://www.tiktok.com/@example",
            "https://www.tiktok.com/@example/video/7123456789012345678",
        ),
        (
            "x",
            "https://x.com/example",
            "https://x.com/example/status/1234567890123456789",
        ),
    ],
)
def test_profile_urls_never_reach_comment_routes(source, profile, content):
    assert not is_comment_parent_ref(source, profile)
    assert is_comment_parent_ref(source, content)
