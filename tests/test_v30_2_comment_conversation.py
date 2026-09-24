from __future__ import annotations

from pathlib import Path

from app.services.relevance_expansion import (
    _conversation_probe_target,
    comment_fulfillment_status,
    comment_source_is_complete,
)


def test_zero_comments_after_normal_empty_actor_run_is_shortfall_not_failure():
    status, reason, shortfall = comment_fulfillment_status(
        0,
        50,
        True,
        attempt_outcomes=[{"status": "empty", "bucket": "open"}],
    )
    assert status == "shortfall"
    assert reason == "no_comments_found_after_bounded_parent_discovery"
    assert shortfall == 50


def test_zero_comments_when_all_actor_attempts_failed_is_operational_failure():
    status, reason, shortfall = comment_fulfillment_status(
        0,
        50,
        True,
        attempt_outcomes=[{"status": "actor_failed", "bucket": "open"}],
    )
    assert status == "failed"
    assert reason == "comment_actor_failed_before_successful_attempt"
    assert shortfall == 50


def test_zero_comments_when_budget_blocks_every_attempt_is_budget_failure():
    status, reason, shortfall = comment_fulfillment_status(
        0,
        50,
        True,
        attempt_outcomes=[{"status": "budget_blocked", "bucket": "open"}],
    )
    assert status == "failed"
    assert reason == "comment_budget_blocked_before_successful_attempt"
    assert shortfall == 50


def test_one_normal_empty_attempt_prevents_false_operational_failure():
    status, reason, shortfall = comment_fulfillment_status(
        0,
        50,
        True,
        attempt_outcomes=[
            {"status": "actor_failed", "bucket": "open"},
            {"status": "empty", "bucket": "open_wave2"},
        ],
    )
    assert status == "shortfall"
    assert reason == "no_comments_found_after_bounded_parent_discovery"
    assert shortfall == 50


def test_partial_comments_remain_truthful_shortfall_even_with_failed_attempt():
    status, reason, shortfall = comment_fulfillment_status(
        7,
        50,
        True,
        attempt_outcomes=[
            {"status": "success", "bucket": "open", "collected": 7},
            {"status": "actor_failed", "bucket": "open_wave2"},
        ],
    )
    assert status == "shortfall"
    assert reason == "source_exhausted_before_comment_target"
    assert shortfall == 43


def test_deadline_state_is_never_terminal():
    status, reason, shortfall = comment_fulfillment_status(
        0,
        50,
        False,
        attempt_outcomes=[{"status": "deadline", "bucket": "open"}],
    )
    assert status == "deferred"
    assert reason == "worker_deadline_or_unfinished_pass"
    assert shortfall == 50
    assert comment_source_is_complete(
        {"status": "deferred", "buckets_done": ["owned", "open", "backfill"]}
    ) is False


def test_parent_discovery_pool_is_deep_enough_for_multiple_waves_but_bounded():
    assert _conversation_probe_target(50, 12) == 45
    assert _conversation_probe_target(100, 12) == 60
    assert _conversation_probe_target(500, 12) == 60
    assert _conversation_probe_target(0, 12) == 0


def test_frontend_uses_source_collection_count_not_total_normalized_candidates():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "const sourceCollected=Object.values(x.sources||{})" in html
    assert "const detailSourceCollected=Object.values(d.sources||{})" in html


def test_frontend_does_not_mark_deferred_comments_done():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "const commentPending=commentStates.some" in html
    assert "['queued','pending','deferred','retrying']" in html
    assert "else if(st==='deferred'){cls='queued'" in html


def test_backend_wave2_reuses_cached_parent_pool_without_new_search():
    source = Path("app/services/relevance_expansion.py").read_text(encoding="utf-8")
    assert "wave=2," not in source
    assert '"open_wave2"' in source
    assert '"wave2_parents"' in source
    assert '"discovery_reused_cached_pool": True' in source
