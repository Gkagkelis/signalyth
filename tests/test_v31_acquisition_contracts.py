from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan, semantic_broad_probe_target
from app.services.relevance_expansion import (
    _comment_parent_candidate_tier,
    _comment_per_parent_limit,
    _comment_seed_refs,
)
from app.services.resilience import DEFAULT_SAFE_BATCH_SIZE, MULTI_TARGET_FIELDS
from app.services.source_capabilities import (
    build_comment_deepening_input,
    comment_actor_contract,
    discovery_actor_contract,
)


COMMENT_SOURCES = {"x", "tiktok", "instagram", "facebook"}
ALL_SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]


def _topic_only_draft(source: str) -> AnalysisDraft:
    return AnalysisDraft(
        client="Test",
        topic="Nike",
        market="Greece",
        date_from=date(2026, 9, 14),
        date_to=date(2026, 9, 23),
        keywords=[],
        keyword_roles={},
        sources=[source],
        sample_mode="perSource",
        per_source={source: 20},
        per_source_comments={source: 50} if source in COMMENT_SOURCES else {},
        comments=source in COMMENT_SOURCES,
        max_budget_usd=5.0,
        smart_search=True,
        search_strategy="balanced_smart",
        report_language="Ελληνικά",
    )


@pytest.mark.parametrize("source", ALL_SOURCES)
def test_topic_only_greece_brief_always_has_a_bounded_acquisition_route(monkeypatch, source):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    plan = build_collection_plan(_topic_only_draft(source)).model_dump(mode="json")
    sp = plan["sources"][0]
    assert sp["subruns"] or sp["semantic_topup_subruns"], (
        f"{source} planned no acquisition route with empty keywords"
    )


def test_instagram_topic_only_uses_bare_subject_only_as_semantic_probe(monkeypatch):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    sp = build_collection_plan(_topic_only_draft("instagram")).model_dump(mode="json")["sources"][0]
    primary_urls = [
        u
        for sr in sp["subruns"]
        for u in (sr.get("input", {}).get("directUrls") or [])
    ]
    semantic = [
        sr for sr in sp["semantic_topup_subruns"]
        if sr.get("purpose") == "semantic_broad_probe"
    ]
    assert not any("/explore/tags/nike/" in u.casefold() for u in primary_urls)
    assert semantic
    assert "/explore/tags/nike/" in semantic[0]["input"]["directUrls"][0].casefold()


def test_broad_probe_can_measure_low_market_yield_without_becoming_unbounded():
    # 1.5x was not "bounded": at target 20 it bought 30 global rows, i.e. the
    # probe outweighed the anchored sample. Minority route, hard cap 20.
    assert semantic_broad_probe_target(20) == 10
    assert semantic_broad_probe_target(40) == 20
    assert semantic_broad_probe_target(500) == 20


def test_native_market_post_actors_keep_their_market_controls(monkeypatch):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    tiktok = build_collection_plan(_topic_only_draft("tiktok")).model_dump(mode="json")["sources"][0]
    youtube = build_collection_plan(_topic_only_draft("youtube")).model_dump(mode="json")["sources"][0]
    news = build_collection_plan(_topic_only_draft("news")).model_dump(mode="json")["sources"][0]

    assert all(sr["input"].get("location") == "GR" for sr in tiktok["subruns"])
    assert all(sr["input"].get("gl") == "gr" and sr["input"].get("hl") == "el" for sr in youtube["subruns"])
    assert all(sr["input"].get("country") == "GR" and sr["input"].get("language") == "el" for sr in news["subruns"])


def test_global_limit_search_actors_do_not_batch_unrelated_targets_together():
    assert DEFAULT_SAFE_BATCH_SIZE["tiktok"] == 1
    assert DEFAULT_SAFE_BATCH_SIZE["youtube"] == 1


def test_x_primary_search_allocates_capacity_per_target(monkeypatch):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    sp = build_collection_plan(_topic_only_draft("x")).model_dump(mode="json")["sources"][0]
    for sr in sp["subruns"]:
        inp = sr["input"]
        assert inp["maxItemsPerTarget"] >= 1
        assert inp["maxItems"] >= inp["maxItemsPerTarget"]


@pytest.mark.parametrize("field", ["postUrls", "replyTweetIds", "threadTweetIds", "startUrls", "directUrls"])
def test_resilience_knows_comment_and_parent_multi_target_fields(field):
    assert field in MULTI_TARGET_FIELDS


def test_comment_contracts_are_explicit_for_every_production_social():
    assert comment_actor_contract("x")["reply_depth"] == "direct"
    assert comment_actor_contract("tiktok")["parent_batch_limit"] == 1
    assert comment_actor_contract("instagram")["sort"] == "recent_activity"
    assert comment_actor_contract("facebook")["sort"] == "newest"
    assert comment_actor_contract("facebook")["reply_depth"] == "top_level"


def test_x_comment_contract_is_threaded_per_target_and_exact_dated():
    inp = build_comment_deepening_input(
        "x",
        ["https://x.com/example/status/123"],
        11,
        max_per_parent=11,
        date_from=date(2026, 9, 14),
        date_to=date(2026, 9, 23),
    )
    assert inp["mode"] == "replies"
    assert inp["replyTweetIds"] == ["123"]
    assert inp["maxItemsPerTarget"] == 11
    assert inp["since"] == "2026-09-14_00:00:00_UTC"
    assert inp["until"] == "2026-09-24_00:00:00_UTC"


def _provenance_parent(*, market_score=0.72, provenance=True, comments=12) -> dict:
    return {
        "id": "p1",
        "platform": "facebook",
        "evidence_layer": "primary",
        "url": "https://www.facebook.com/example/posts/123",
        "text": "Μεγάλη κλήρωση απόψε — ποιος έπαιξε;",
        "comments": comments,
        "likes": 20,
        "subject_search_provenance": provenance,
        "collection_route": "semantic_broad_probe",
        "collection_query": "Eurojackpot",
        "metric_availability": {"comments_known": True},
        "raw_data": {"postUrl": "https://www.facebook.com/example/posts/123"},
        "cleaning": {
            "decision": "excluded",
            "relevance_score": 0.18,
            "market_score": market_score,
            "spam_score": 0.0,
            "authenticity_status": "low_risk",
            "content_class": "unknown",
            "origin_class": "unknown",
            "organic_eligible": True,
            "flags": ["no_subject_signal", "core_term_missing"],
            "reasons": ["subject_not_mentioned", "greek_script"],
        },
    }


def test_subject_search_provenance_can_open_a_strong_greek_conversation_parent():
    row = _provenance_parent()
    assert _comment_parent_candidate_tier("facebook", row) == "provenance"
    refs, meta, _ = _comment_seed_refs("facebook", [row], max_seeds=5)
    assert refs == ["https://www.facebook.com/example/posts/123"]
    assert meta[0]["qualification_tier"] == "provenance"
    assert meta[0]["market_score"] == pytest.approx(0.72)


def test_provenance_keeps_real_greeklish_market_signal():
    assert _comment_parent_candidate_tier(
        "facebook", _provenance_parent(market_score=0.30)
    ) == "provenance"


def test_provenance_never_overrides_market_or_random_post_guards():
    assert _comment_parent_candidate_tier("facebook", _provenance_parent(market_score=0.27)) is None
    assert _comment_parent_candidate_tier("facebook", _provenance_parent(market_score=0.1)) is None
    assert _comment_parent_candidate_tier("facebook", _provenance_parent(provenance=False)) is None


def test_open_comment_harvest_advances_through_all_untried_cached_parents():
    source = Path("app/services/relevance_expansion.py").read_text(encoding="utf-8")
    assert 'extra_bucket = f"open_extra_{extra_wave_index}"' in source
    assert 'if str(durable_bucket).startswith("open")' in source
    assert 'while (\n                source_comment_target > 0' in source
    assert 'exclude_refs=used_open_refs' in source


def test_per_parent_comment_limits_follow_batch_shortfall_without_affecting_global_actors():
    assert _comment_per_parent_limit("facebook", 20, 5, 40) == 4
    assert _comment_per_parent_limit("instagram", 17, 5, 40) == 4
    assert _comment_per_parent_limit("tiktok", 20, 5, 40) == 40
    assert _comment_per_parent_limit("x", 20, 2, 40) == 40


def test_facebook_comment_input_preserves_hotfix_contract_and_post_filters_dates():
    ref = "https://www.facebook.com/example/posts/123"
    inp = build_comment_deepening_input(
        "facebook", [ref], 20, max_per_parent=20,
        include_replies=True,
        date_from=date(2026, 9, 14),
        date_to=date(2026, 9, 23),
    )
    assert inp["postUrls"] == [ref]
    assert inp["resultsLimit"] == 20
    assert inp["commentsSortType"] == "newest"
    assert "startUrls" not in inp
    assert "onlyCommentsNewerThan" not in inp


def test_discovery_contract_matrix_matches_production_actors_and_limit_scopes():
    expected = {
        "x": ("xquik/x-tweet-scraper", "global"),
        "tiktok": ("epctex/tiktok-search-scraper", "global"),
        "instagram": ("apify/instagram-scraper", "per_source"),
        "facebook": ("scraper_one/facebook-posts-search", "per_query"),
        "youtube": ("apidojo/youtube-scraper", "global"),
        "news": ("logiover/google-news-scraper", "per_query_feed"),
    }
    for source, (actor_id, scope) in expected.items():
        contract = discovery_actor_contract(source)
        assert contract["actor_id"] == actor_id
        assert contract["limit_scope"] == scope


def test_facebook_discovery_contract_never_claims_pinned_location_is_country_filter():
    contract = discovery_actor_contract("facebook")
    assert contract["safe_target_batch"] == 1
    assert contract["query_max_length"] == 100
    assert "not country-wide Greece" in contract["market_support"]
