from __future__ import annotations

from datetime import date

import pytest

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan, semantic_broad_probe_target
from app.services.relevance_expansion import (
    _comment_parent_candidate_tier,
    _comment_seed_refs,
)
from app.services.resilience import DEFAULT_SAFE_BATCH_SIZE, MULTI_TARGET_FIELDS
from app.services.source_capabilities import (
    build_comment_deepening_input,
    comment_actor_contract,
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
def test_topic_only_greece_brief_always_has_a_real_primary_route(monkeypatch, source):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    plan = build_collection_plan(_topic_only_draft(source)).model_dump(mode="json")
    sp = plan["sources"][0]
    assert sp["subruns"], f"{source} planned zero primary routes with empty keywords"


def test_instagram_topic_only_falls_back_to_real_subject_hashtag(monkeypatch):
    monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _draft: [])
    sp = build_collection_plan(_topic_only_draft("instagram")).model_dump(mode="json")["sources"][0]
    urls = [
        u
        for sr in sp["subruns"]
        for u in (sr.get("input", {}).get("directUrls") or [])
    ]
    assert any("/explore/tags/nike/" in u.casefold() for u in urls)


def test_broad_probe_can_measure_low_market_yield_without_becoming_unbounded():
    assert semantic_broad_probe_target(20) == 30
    assert semantic_broad_probe_target(40) == 60
    assert semantic_broad_probe_target(500) == 60


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


@pytest.mark.parametrize("field", ["postUrls", "threadTweetIds", "startUrls", "directUrls"])
def test_resilience_knows_comment_and_parent_multi_target_fields(field):
    assert field in MULTI_TARGET_FIELDS


def test_comment_contracts_are_explicit_for_every_production_social():
    assert comment_actor_contract("x")["reply_depth"] == "thread"
    assert comment_actor_contract("tiktok")["parent_batch_limit"] == 1
    assert comment_actor_contract("instagram")["sort"] == "recent"
    assert comment_actor_contract("facebook")["sort"] == "newest"


def test_x_comment_contract_is_threaded_per_target_and_exact_dated():
    inp = build_comment_deepening_input(
        "x",
        ["https://x.com/example/status/123"],
        11,
        max_per_parent=11,
        date_from=date(2026, 9, 14),
        date_to=date(2026, 9, 23),
    )
    assert inp["mode"] == "thread"
    assert inp["threadTweetIds"] == ["123"]
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
