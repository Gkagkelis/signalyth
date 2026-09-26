"""v31.7 — paid open comment parents must be worth paying for.

Run 20260925T130636Z: an aggregator page reposting global live-score feeds
(market_score 0.27, zero comments) took FOUR of the seven paid facebook
comment-parent slots, because the parent gate reused the cleaning REVIEW
threshold (0.25) and had no per-author bound. A paid comment call is a
stronger commitment than keeping a record around for review:

1. An open parent needs a clear target-market signal of its own
   (market_score >= OPEN_PARENT_MARKET_FLOOR).
2. One author/page holds at most MAX_OPEN_PARENTS_PER_AUTHOR paid slots.

The operator's own pages are untouched: their posts come through the owned
pass, not through this open-search selection.
"""
from __future__ import annotations

from app.services.relevance_expansion import (
    MAX_OPEN_PARENTS_PER_AUTHOR,
    OPEN_PARENT_MARKET_FLOOR,
    _comment_parent_candidate_allowed,
    _comment_seed_refs,
)


def _row(rid, author, market, comments=5, decision="trusted", relevance=0.8):
    return {
        "id": str(rid), "platform": "facebook", "evidence_layer": "primary",
        "author": author, "comments": comments, "likes": 10,
        "url": f"https://www.facebook.com/{author}/posts/{rid}",
        "metric_availability": {"comments_known": True},
        "cleaning": {
            "reasons": ["core_term:eurojackpot"], "flags": [],
            "market_score": market, "spam_score": 0.1,
            "relevance_score": relevance, "decision": decision,
        },
    }


def test_weak_market_low_relevance_posts_are_not_paid_parents():
    # The production live-score spam profile: market 0.27, review decision,
    # subject buried in an off-topic feed (low relevance).
    spam = _row(1, "livescores", 0.27, decision="review", relevance=0.35)
    assert not _comment_parent_candidate_allowed("facebook", spam)
    # A genuine short Greek-market post with the same weak market score stays
    # eligible, because it is clearly ABOUT the subject.
    legit = _row(2, "person2", 0.27, decision="trusted", relevance=0.75)
    assert _comment_parent_candidate_allowed("facebook", legit)
    # High relevance alone also clears the weak-market bar.
    review_but_on_topic = _row(3, "person3", 0.3, decision="review", relevance=0.7)
    assert _comment_parent_candidate_allowed("facebook", review_but_on_topic)
    # Strong market needs no extra proof.
    assert _comment_parent_candidate_allowed(
        "facebook", _row(4, "cnn.greece", 0.84, decision="review", relevance=0.4))
    assert OPEN_PARENT_MARKET_FLOOR >= 0.5


def test_records_cleaning_excluded_are_never_paid_parents():
    row = _row(5, "aggregator", 0.9, decision="excluded", relevance=0.9)
    assert not _comment_parent_candidate_allowed("facebook", row)


def test_one_page_cannot_take_more_than_its_share_of_paid_slots():
    spam = [_row(i, "spampage", 0.9, comments=50) for i in range(1, 5)]
    real = [_row(i, f"person{i}", 0.9, comments=3) for i in range(10, 14)]
    refs, meta, _mode = _comment_seed_refs("facebook", [*spam, *real], max_seeds=10)
    per_author: dict[str, int] = {}
    for m in meta:
        author = str(m.get("url") or "").split("facebook.com/")[1].split("/")[0]
        per_author[author] = per_author.get(author, 0) + 1
    assert per_author.get("spampage", 0) <= MAX_OPEN_PARENTS_PER_AUTHOR, per_author
    # The freed slots go to the other authors instead of being lost.
    assert sum(per_author.values()) >= 6


def test_two_strong_posts_from_the_same_newsroom_are_still_allowed():
    rows = [_row(i, "tanea", 0.9, comments=8) for i in range(1, 4)]
    refs, meta, _mode = _comment_seed_refs("facebook", rows, max_seeds=10)
    assert len(refs) == MAX_OPEN_PARENTS_PER_AUTHOR
