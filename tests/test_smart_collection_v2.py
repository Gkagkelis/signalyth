from datetime import date

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.smart_collection import (
    canonical_topic,
    detect_dominant_entity_collisions,
    x_reply_deepening_input,
)


def allwyn(**changes):
    data = dict(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x"], sample_mode="automatic",
        sample_target=500, comments=True, max_budget_usd=2.0,
        smart_search=True, report_language="Ελληνικά",
        additional_context=["OPAP", "ΟΠΑΠ"], exclusions=[],
    )
    data.update(changes)
    return AnalysisDraft(**data)


def test_canonical_topic_removes_market_suffix():
    assert canonical_topic("Allwyn Greece", "Greece") == "Allwyn"


def test_x_v2_uses_balanced_intents_and_current_actor_fields():
    plan = build_collection_plan(allwyn())
    sp = plan.sources[0]
    assert len(sp.subruns) >= 3
    assert all(sr.purpose.startswith("balanced_intent_discovery_batch_") for sr in sp.subruns)
    assert sum(sr.target_items for sr in sp.subruns) == 500
    queries = []
    for sr in sp.subruns:
        inp = sr.input
        assert inp["mode"] == "search"
        assert inp["queryType"] == "Latest"
        assert inp["includeSearchTerms"] is True
        assert 1 <= inp["maxItemsPerTarget"] <= inp["maxItems"]
        assert len(inp["searchTerms"]) <= 2
        queries.extend(inp["searchTerms"])
    # Broad Greek recall exists, but only as one bounded intent rather than the whole strategy.
    assert len(queries) >= 5
    assert sum("lang:el" in q for q in queries) == 1
    assert all("-filter:nativeretweets" in q for q in queries)
    assert plan.search_strategy_version == "smart-collection-v2"
    assert plan.target_semantics == "requested_analyzable_evidence"


def test_allwyn_arena_like_collision_is_detected_not_deleted():
    rows = []
    for i in range(7):
        rows.append({"text": f"Allwyn Arena match report {i}", "author": f"media{i%3}"})
    for i in range(3):
        rows.append({"text": f"Allwyn company results {i}", "author": f"other{i}"})
    collisions = detect_dominant_entity_collisions(rows, "Allwyn", min_count=3, min_share=0.28)
    assert collisions
    assert collisions[0]["compound"].casefold() == "allwyn arena"
    assert collisions[0]["action"] == "split_and_cap_not_delete"


def test_reply_deepening_uses_actor_reply_mode_and_caps_targets():
    rows = [
        {"id": "100", "replyCount": 12, "authorUsername": "a", "url": "https://x/100"},
        {"id": "200", "replyCount": 4, "authorUsername": "b", "url": "https://x/200"},
        {"id": "300", "replyCount": 0, "authorUsername": "c", "url": "https://x/300"},
    ]
    inp, seeds = x_reply_deepening_input(rows, requested_items=100)
    assert inp["mode"] == "replies"
    assert inp["replyTweetIds"] == ["100", "200"]
    assert inp["maxItems"] == 100
    assert inp["maxItemsPerTarget"] == 50
    assert len(seeds) == 2


def test_collision_refinement_keeps_property_as_separate_bounded_bucket():
    from app.services.smart_collection import x_search_input
    inp, buckets = x_search_input(allwyn(), 500, collision_exclusions=["Arena"])
    assert any("Allwyn Arena" in q for q in inp["searchTerms"])
    non_property = [q for q in inp["searchTerms"] if "Allwyn Arena" not in q]
    assert non_property and all("-Arena" in q for q in non_property)
    assert any(b["bucket_id"].startswith("context_collision_") for b in buckets)
    assert inp["maxItemsPerTarget"] < 100


def test_x_intent_catalog_is_not_brand_specific():
    person = allwyn(
        client="Internal",
        topic="Αλέξης Τσίπρας",
        keywords=["Αλέξης Τσίπρας", "Alexis Tsipras"],
        additional_context=[],
        sample_target=500,
    )
    plan = build_collection_plan(person)
    bucket_ids = {b["bucket_id"] for b in plan.sources[0].intent_buckets}
    assert "products_services" not in bucket_ids
    assert "market_context" in bucket_ids
    assert "public_reaction" in bucket_ids
    assert "public_activity" in bucket_ids
    assert "media_context" in bucket_ids
