from datetime import date

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan, build_terms, base_queries


def draft(**changes):
    data = dict(
        client="Agency Example",
        topic="Vodafone Internet",
        market="Greece",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        keywords=["Vodafone Internet"],
        sources=["x", "facebook", "tiktok", "youtube", "news"],
        sample_mode="automatic",
        sample_target=1000,
        comments=True,
        max_budget_usd=5.0,
        smart_search=True,
        report_language="Ελληνικά",
        additional_context=[],
        exclusions=[],
    )
    data.update(changes)
    return AnalysisDraft(**data)


def test_client_name_is_never_used_as_discovery_context():
    d = draft(client="Completely Unrelated Agency")
    plan = build_collection_plan(d)
    blob = "\n".join(q for sp in plan.sources for q in sp.queries)
    x_blob = "\n".join(t for sp in plan.sources if sp.source == "x" for sr in sp.subruns for t in sr.input.get("searchTerms", []))
    assert "Completely Unrelated Agency" not in blob
    assert "Completely Unrelated Agency" not in x_blob
    assert plan.preflight_forecast["query_safety"]["client_field_used_for_discovery"] is False


def test_greece_aliases_are_never_emitted_as_naked_queries():
    d = draft()
    core, context, glish = build_terms(d)
    queries = base_queries(core, context, glish)
    folded = {q.casefold().strip() for q in queries}
    assert "greece" not in folded
    assert "ελλάδα" not in folded
    assert "ellada" not in folded
    assert all("vodafone" in q.casefold() for q in queries)


def test_vodafone_multisource_plan_has_no_market_only_or_client_only_queries():
    plan = build_collection_plan(draft())
    forbidden = {"greece", "ελλάδα", "ellada", "agency example"}
    for sp in plan.sources:
        for q in sp.queries:
            assert q.casefold().strip() not in forbidden
        # Every non-X query is anchored by the brand/topic.
        if sp.source != "x":
            assert all("vodafone" in q.casefold() for q in sp.queries)


def test_market_suffix_is_canonicalized_for_non_x_discovery():
    d = draft(topic="Allwyn Greece", keywords=["Allwyn Greece"], sources=["facebook", "news"])
    plan = build_collection_plan(d)
    assert plan.core_terms == ["Allwyn"]
    for sp in plan.sources:
        assert "Allwyn Greece Greece" not in sp.queries
        assert any(q.casefold() == "allwyn greece" for q in sp.queries)


def test_parent_brand_keyword_does_not_waste_a_redundant_query_bucket():
    d = draft(topic="Vodafone Internet", keywords=["Vodafone Internet", "Vodafone"])
    plan = build_collection_plan(d)
    assert "Vodafone" not in plan.context_terms
    for sp in plan.sources:
        assert all(q.casefold() != "vodafone internet vodafone" for q in sp.queries)


def test_secondary_keyword_never_becomes_unanchored_query():
    d = draft(topic="Allwyn", keywords=["Allwyn", "OPAP"], sources=["facebook", "tiktok", "youtube", "news"])
    plan = build_collection_plan(d)
    for sp in plan.sources:
        folded = {q.casefold().strip() for q in sp.queries}
        assert "opap" not in folded
        assert any("allwyn opap" in q.casefold() for q in sp.queries)


def test_allwyn_context_remains_explicit_and_x_is_batched_safely():
    d = draft(
        client="Agency Example",
        topic="Allwyn",
        keywords=["Allwyn"],
        sources=["x"],
        additional_context=["OPAP", "ΟΠΑΠ"],
        sample_target=1000,
    )
    plan = build_collection_plan(d)
    sp = plan.sources[0]
    terms = [q for sr in sp.subruns for q in sr.input.get("searchTerms", [])]
    assert any("OPAP" in q or "ΟΠΑΠ" in q for q in terms)
    assert all("Agency Example" not in q for q in terms)
    assert all(len(sr.input.get("searchTerms", [])) <= 2 for sr in sp.subruns)
    assert sum(sr.target_items for sr in sp.subruns) == 1000


def test_query_safety_holds_for_all_63_source_combinations():
    import itertools
    sources = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]
    count = 0
    for r in range(1, 7):
        for combo in itertools.combinations(sources, r):
            plan = build_collection_plan(draft(sources=list(combo), sample_target=500))
            count += 1
            assert plan.preflight_forecast["query_safety"]["market_only_queries_allowed"] is False
            for sp in plan.sources:
                assert all(q.casefold().strip() not in {"greece", "ελλάδα", "ellada"} for q in sp.queries)
    assert count == 63


def test_instagram_never_uses_unanchored_secondary_hashtags():
    d = draft(
        topic="Vodafone Internet",
        keywords=["Vodafone Internet", "fiber", "outage"],
        additional_context=["support"],
        sources=["instagram"],
        sample_target=200,
    )
    plan = build_collection_plan(d)
    urls = [u for sr in plan.sources[0].subruns for u in sr.input.get("directUrls", [])]
    assert urls
    assert all("vodafoneinternet" in u.casefold() for u in urls)
    assert all("/tags/fiber/" not in u.casefold() for u in urls)
    assert all("/tags/outage/" not in u.casefold() for u in urls)
    assert all("/tags/support/" not in u.casefold() for u in urls)


def test_x_intents_are_entity_agnostic_for_person_case():
    d = draft(
        client="Internal",
        topic="Αλέξης Τσίπρας",
        keywords=["Αλέξης Τσίπρας", "Alexis Tsipras"],
        sources=["x"],
        sample_target=500,
    )
    plan = build_collection_plan(d)
    terms = [q for sr in plan.sources[0].subruns for q in sr.input.get("searchTerms", [])]
    blob = "\n".join(terms).casefold()
    # Generic planning must not assume that every subject is a corporation or a consumer brand.
    assert "εταιρεία or όμιλος" not in blob
    assert "company or group" not in blob
    assert "λογότυπο" not in blob
    assert "stock" not in blob
    # It should still cover reactions, public activity/reporting and supplied aliases.
    assert "αντίδραση" in blob or "reaction" in blob
    assert "δήλωση" in blob or "statement" in blob
    assert "media" in blob or "είδηση" in blob
    assert "alexis tsipras" in blob
