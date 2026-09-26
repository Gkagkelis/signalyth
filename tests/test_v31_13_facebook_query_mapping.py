"""v31.13 — the input-mapping suggester must not map the search query onto an enum.

NBG run #3 (20260926T122900Z-0d4ae0d6) lost its entire facebook layer because
suggest_input_mapping had picked ``searchType`` as the query field for
apify/facebook-search-scraper: "search" is a substring of "searchType", while
the actor's real free-text field, ``categories``, matched no alias at all.
``searchType`` is an enum ("pages-google"|"pages"|"profiles"|"posts"|"reels"),
so every generated discovery input failed Apify validation:

    Input is not valid: Field input.searchType must be equal to one of the
    allowed values: "pages-google", "pages", "profiles", "posts", "reels"

The probe never caught it because probes send the input_template verbatim; the
mapping is only exercised by a real run's query planner.

Two rules pinned here:
1. A field whose schema carries a fixed enum can never be the query target.
2. Exact alias matches rank by alias order, so a conventional name ("query")
   still beats the last-resort "categories" alias when both exist.
"""
from __future__ import annotations

from app.services.integrations import suggest_input_mapping

FACEBOOK_SEARCH_SCRAPER_SCHEMA = {
    "properties": {
        "categories": {"type": "array", "items": {"type": "string"}},
        "searchType": {
            "type": "string",
            "enum": ["pages-google", "pages", "profiles", "posts", "reels"],
        },
        "resultsLimit": {"type": "integer"},
        "locations": {"type": "array"},
        "helloWorld": {"type": "integer"},
    },
    "required": ["categories"],
}


def test_query_never_maps_to_an_enum_field():
    mapping = suggest_input_mapping(FACEBOOK_SEARCH_SCRAPER_SCHEMA)
    assert mapping.get("query") == "categories", mapping
    assert mapping.get("max_items") == "resultsLimit"
    assert mapping.get("country") == "locations"


def test_conventional_query_field_still_beats_categories():
    schema = {
        "properties": {
            "categories": {"type": "array"},
            "searchQueries": {"type": "array"},
            "maxItems": {"type": "integer"},
        }
    }
    mapping = suggest_input_mapping(schema)
    assert mapping.get("query") == "searchQueries", mapping


def test_enum_query_with_no_free_text_alternative_stays_unmapped():
    schema = {
        "properties": {
            "searchType": {"type": "string", "enum": ["top", "latest"]},
            "maxItems": {"type": "integer"},
        }
    }
    mapping = suggest_input_mapping(schema)
    assert "query" not in mapping, mapping
