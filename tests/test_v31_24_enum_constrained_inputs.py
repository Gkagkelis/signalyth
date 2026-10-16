"""v31.24 — a fixed-value Actor field is matched, never overwritten blindly.

The Εθνική Τράπεζα run asked for 80 news items over a whole month and got
three. The cause was not Greek media silence: Google News' ``language`` field
only accepts composite members such as ``"GR:el"``, the planner wrote the
draft's bare ``"el"`` over the committed template, and the Actor quietly fell
back to its ``US:en`` default. Every other source that was never probed shared
the same class of defect.

The fix is one rule applied in both places that build Actor input: when a field
declares a fixed value list, pick the member that expresses our value, and when
none does, leave the committed template's own valid value alone.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import AnalysisDraft
from app.services.integrations import enum_input_value, sanitize_actor_input_types

GOOGLE_NEWS_LANGUAGES = ["US:en", "GB:en", "DE:de", "GR:el", "FR:fr", "IT:it"]


def test_bare_language_resolves_to_the_composite_greek_member():
    assert enum_input_value("el", GOOGLE_NEWS_LANGUAGES, ["GR"]) == "GR:el"


def test_exact_member_is_returned_untouched():
    assert enum_input_value("GR:el", GOOGLE_NEWS_LANGUAGES) == "GR:el"


def test_case_differences_do_not_lose_a_valid_member():
    assert enum_input_value("gr:EL", GOOGLE_NEWS_LANGUAGES) == "GR:el"


def test_plain_enum_without_composites_still_matches():
    assert enum_input_value("GR", ["GR", "US", "DE"]) == "GR"


def test_a_value_the_list_cannot_express_is_refused():
    # facebook-search-scraper's searchType is the original of this bug class:
    # the search query must never be written into it.
    assert enum_input_value("Εθνική Τράπεζα", ["pages", "posts", "reels"]) is None


def test_companion_disambiguates_between_two_members_sharing_a_part():
    options = ["GR:el", "CY:el"]
    assert enum_input_value("el", options, ["CY"]) == "CY:el"
    assert enum_input_value("el", options, ["GR"]) == "GR:el"


def test_sanitizer_repairs_the_language_and_keeps_the_rest():
    schema = {"properties": {
        "query": {"type": "string"},
        "language": {"type": "string", "enum": GOOGLE_NEWS_LANGUAGES},
        "country": {"type": "string"},
    }}
    cleaned, mismatches = sanitize_actor_input_types(
        schema, {"query": "Εθνική Τράπεζα", "language": "el", "country": "GR"})
    assert cleaned["language"] == "GR:el"
    assert cleaned["query"] == "Εθνική Τράπεζα"
    assert "language" not in mismatches


def test_sanitizer_drops_an_unexpressable_value_instead_of_sending_it():
    schema = {"properties": {"searchType": {"type": "string", "enum": ["pages", "posts"]}}}
    cleaned, mismatches = sanitize_actor_input_types(schema, {"searchType": "Εθνική Τράπεζα"})
    assert "searchType" not in cleaned
    assert "searchType" in mismatches


# ---------------------------------------------------------------------------
# The run-time planner, which is where the live damage happened
# ---------------------------------------------------------------------------

def _news_registry():
    return {"news": {
        "actor_id": "lhotanok/google-news-scraper",
        "adapter_mode": "generic",
        "actor_status": "verified",
        "input_mapping": {"query": "query", "max_items": "maxItems",
                          "date_from": "dateFrom", "date_to": "dateTo",
                          "language": "language"},
        "input_schema_fields": {
            "query": {"type": "string", "required": True},
            "maxItems": {"type": "integer", "required": False},
            "dateFrom": {"type": "string", "required": False},
            "dateTo": {"type": "string", "required": False},
            "language": {"type": "string", "required": False, "enum": GOOGLE_NEWS_LANGUAGES},
        },
        "input_template": {"query": "", "maxItems": 10, "language": "GR:el"},
        "output_mapping": {"text": "title", "date": "publishedAt", "url": "link", "author": "source"},
        "price_per_1000_hint": 1.0,
    }}


def _draft():
    return AnalysisDraft(
        client="Εθνική Τράπεζα (NBG)", topic="Εθνική Τράπεζα", market="Greece",
        date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
        keywords=["Εθνική Τράπεζα"], sources=["news"], sample_mode="perSource",
        per_source={"news": 80}, comments=False, max_budget_usd=2.0,
        smart_search=True, report_language="Ελληνικά",
        additional_context=["τράπεζα"], exclusions=["εθνική ομάδα"],
    )


def test_live_news_plan_sends_greek_not_the_us_default():
    from app.services.query_planner import make_source_plan
    plan = make_source_plan("news", 80, _draft(), ["Εθνική Τράπεζα"], _news_registry(), 1.0)
    assert plan.subruns, "no subruns were planned"
    for sub in plan.subruns:
        assert sub.input["language"] == "GR:el", (
            f"the planner sent {sub.input.get('language')!r}; the Actor would "
            "fall back to its US:en default and return English articles")


def test_a_non_greek_market_still_gets_a_valid_member():
    from app.services.query_planner import make_source_plan
    draft = _draft().model_copy(update={"market": "United Kingdom"})
    plan = make_source_plan("news", 80, draft, ["National Bank"], _news_registry(), 1.0)
    for sub in plan.subruns:
        assert sub.input["language"] in GOOGLE_NEWS_LANGUAGES
