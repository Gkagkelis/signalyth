from __future__ import annotations

from app.services.relevance_expansion import (
    _comment_seed_refs,
    comment_fulfillment_status,
    comment_source_is_complete,
)


def _fb_row(i: int, comments: int, known: bool = True) -> dict:
    return {
        "id": f"post-{i}",
        "platform": "facebook",
        "evidence_layer": "primary",
        "url": f"https://www.facebook.com/example/posts/{1000 + i}",
        "comments": comments,
        "metric_availability": {"comments_known": known},
        "likes": 20 - i,
        "text": f"Eurojackpot post {i}",
        "raw_data": {"postUrl": f"https://www.facebook.com/example/posts/{1000 + i}"},
        "cleaning": {
            "decision": "trusted",
            "relevance_score": 0.8,
            "market_score": 0.7,
            "spam_score": 0.0,
            "organic_eligible": True,
            "authenticity_status": "low_risk",
            "flags": [],
            # v30.1: the open-parent gate demands what every REAL trusted row
            # has — a direct subject hit. A trusted row without core_term
            # reasons cannot exist in production.
            "reasons": ["core_term:eurojackpot"],
        },
    }


def test_reported_parents_are_supplemented_instead_of_ending_selection():
    rows = [
        _fb_row(0, 5, True),
        _fb_row(1, 2, True),
        *[_fb_row(i, 0, True) for i in range(2, 12)],
    ]
    refs, meta, mode = _comment_seed_refs("facebook", rows, max_seeds=12)
    assert mode == "reported_comments_plus_probe"
    assert len(refs) == 7
    assert len(meta) == 7


def test_retry_selection_can_exclude_first_wave_and_get_new_parents():
    rows = [_fb_row(i, 0, True) for i in range(12)]
    first_refs, _, _ = _comment_seed_refs("facebook", rows, max_seeds=5)
    second_refs, _, _ = _comment_seed_refs(
        "facebook", rows, max_seeds=5, exclude_refs=set(first_refs)
    )
    assert len(first_refs) == 5
    assert len(second_refs) == 5
    assert set(first_refs).isdisjoint(second_refs)


def test_two_of_eighty_is_shortfall_not_collected():
    status, reason, shortfall = comment_fulfillment_status(2, 80, True)
    assert status == "shortfall"
    assert reason == "source_exhausted_before_comment_target"
    assert shortfall == 78


def test_target_met_is_collected():
    status, reason, shortfall = comment_fulfillment_status(80, 80, True)
    assert status == "collected"
    assert reason is None
    assert shortfall == 0


def test_unfinished_pass_is_deferred_even_with_some_rows():
    status, reason, shortfall = comment_fulfillment_status(20, 80, False)
    assert status == "deferred"
    assert shortfall == 60


def test_shortfall_is_terminal_and_not_requeued_forever():
    row = {
        "status": "shortfall",
        "buckets_done": ["owned", "open", "backfill"],
        "collected": 2,
        "target": 80,
        "shortfall": 78,
    }
    assert comment_source_is_complete(row) is True
