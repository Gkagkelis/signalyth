"""Regression contract for v30 conversation-oriented acquisition."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.models import AnalysisDraft
from app.services.collector import _route_request_target
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import (
    _conversation_parent_is_strong,
    _conversation_probe_target,
    _source_semantic_shortfall,
)


def _draft(source: str) -> AnalysisDraft:
    return AnalysisDraft(
        client="Test client",
        topic="Nike",
        market="Greece",
        date_from=date(2026, 9, 17),
        date_to=date(2026, 9, 23),
        keywords=[],
        keyword_roles={},
        sources=[source],
        sample_mode="perSource",
        per_source={source: 20},
        per_source_comments={source: 50} if source in {"x", "tiktok", "instagram", "facebook"} else {},
        comments=source in {"x", "tiktok", "instagram", "facebook"},
        max_budget_usd=5.0,
        smart_search=True,
        search_strategy="balanced_smart",
        report_language="Ελληνικά",
    )


def test_primary_route_keeps_planned_share():
    assert _route_request_target(7, 20, primary=True) == 7
    assert _route_request_target(7, 5, primary=True) == 5


def test_topup_route_can_take_whole_remaining_shortfall():
    assert _route_request_target(7, 20, primary=False) == 20
    assert _route_request_target(7, 5, primary=False) == 5


def test_facebook_empty_keywords_is_not_global_first(monkeypatch):
    monkeypatch.setattr(
        "app.services.query_planner.suggest_public_names",
        lambda _draft: [],
    )
    plan = build_collection_plan(_draft("facebook")).model_dump(mode="json")
    sp = plan["sources"][0]

    primary_queries = [
        str(sr.get("input", {}).get("query") or "")
        for sr in sp.get("subruns") or []
    ]
    assert primary_queries
    assert all(q.casefold() != "nike" for q in primary_queries)
    assert any(
        any(anchor.casefold() in q.casefold() for anchor in ("Greece", "Ελλάδα", "Ellada"))
        for q in primary_queries
    )

    probes = [
        sr for sr in sp.get("semantic_topup_subruns") or []
        if sr.get("purpose") == "semantic_broad_probe"
    ]
    assert len(probes) == 1
    assert probes[0]["input"]["query"] == "Nike"


def test_instagram_empty_keywords_does_not_invent_market_hashtag(monkeypatch):
    monkeypatch.setattr(
        "app.services.query_planner.suggest_public_names",
        lambda _draft: [],
    )
    plan = build_collection_plan(_draft("instagram")).model_dump(mode="json")
    sp = plan["sources"][0]

    primary_urls = [
        url
        for sr in sp.get("subruns") or []
        for url in (sr.get("input", {}).get("directUrls") or [])
    ]
    assert not any("nikegreece" in url.casefold() for url in primary_urls)
    assert not any("nikeελλάδα" in url.casefold() for url in primary_urls)

    probes = [
        sr for sr in sp.get("semantic_topup_subruns") or []
        if sr.get("purpose") == "semantic_broad_probe"
    ]
    assert len(probes) == 1
    assert "nike" in str(probes[0]["input"]).casefold()


def test_semantic_shortfall_counts_trusted_plus_review():
    report = {
        "source_breakdown": {
            "facebook": {"target": 20, "trusted": 15, "review": 3, "excluded": 4}
        }
    }
    assert _source_semantic_shortfall(report, "facebook", 20) == 2


def test_conversation_probe_is_bounded():
    assert _conversation_probe_target(50, 12) == 25
    assert _conversation_probe_target(100, 12) == 50
    assert _conversation_probe_target(500, 12) == 60
    assert _conversation_probe_target(0, 12) == 0


def _parent_row(*, market_score=0.62, reasons=None, flags=None):
    return {
        "platform": "facebook",
        "evidence_layer": "primary",
        "url": "https://www.facebook.com/example/posts/123",
        "raw_data": {"postUrl": "https://www.facebook.com/example/posts/123"},
        "cleaning": {
            "decision": "trusted",
            "market_score": market_score,
            "relevance_score": 0.8,
            "spam_score": 0.0,
            "authenticity_status": "low_risk",
            "flags": list(flags or []),
            "reasons": list(reasons or ["core_term:Nike", "greek_script"]),
        },
    }


def test_parent_only_discovery_requires_direct_subject_and_market():
    assert _conversation_parent_is_strong("facebook", _parent_row()) is True

    no_subject = _parent_row(
        reasons=["subject_not_mentioned", "greek_script"],
        flags=["no_subject_signal"],
    )
    assert _conversation_parent_is_strong("facebook", no_subject) is False

    outside_market = _parent_row(
        market_score=0.0,
        reasons=["core_term:Nike", "outside_target_market"],
    )
    assert _conversation_parent_is_strong("facebook", outside_market) is False


def test_ui_prefers_original_comment_target():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "c.target||c.requested" in html
    assert "const shownTarget=Number(c.target||c.requested||0)" in html
