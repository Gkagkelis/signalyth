from __future__ import annotations

import random
from datetime import date, timedelta

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.resilience import DEFAULT_SAFE_BATCH_SIZE

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]
ARRAY_FIELDS = ("searchTerms", "search", "queries", "keywords", "directUrls", "startUrls", "replyTweetIds", "urls")


def test_1000_deterministic_random_plans_preserve_hard_invariants():
    rng = random.Random(1802)
    for i in range(1000):
        chosen = rng.sample(SOURCES, rng.randint(1, 6))
        target = rng.choice([1, 2, 3, 5, 10, 25, 50, 100, 500, 1000, 3000])
        start = date(2026, 1, 1) + timedelta(days=rng.randint(0, 180))
        end = start + timedelta(days=rng.randint(0, 90))
        budget = rng.choice([0.01, 0.05, 0.25, 1.0, 5.0, 20.0])
        topic = rng.choice(["Allwyn", "Vodafone Internet", "Brand X"])
        client = f"Agency-{i}"
        draft = AnalysisDraft(
            client=client, topic=topic, market="Greece",
            date_from=start, date_to=end, keywords=["primary", "δευτερεύον", "context"],
            sources=chosen, sample_mode="automatic", sample_target=target,
            comments=bool(i % 2), max_budget_usd=budget, smart_search=True,
            report_language="Ελληνικά" if i % 3 else "English",
        )
        plan = build_collection_plan(draft)
        assert plan.target_total == target
        assert sum(sp.target_items for sp in plan.sources) == target
        assert sum(sr.max_charge_usd for sp in plan.sources for sr in sp.subruns) <= budget + 1e-8
        for sp in plan.sources:
            assert sp.target_items > 0
            assert sp.subruns
            assert sum(sr.target_items for sr in sp.subruns) == sp.target_items
            assert all(sr.target_items > 0 for sr in sp.subruns)
            assert all(sr.max_charge_usd >= 0 for sr in sp.subruns)
            safe = DEFAULT_SAFE_BATCH_SIZE[sp.source]
            for sr in sp.subruns:
                for field in ARRAY_FIELDS:
                    value = sr.input.get(field)
                    if isinstance(value, list):
                        assert 1 <= len(value) <= safe
        assert plan.preflight_forecast["sample_rule"].startswith("never fill")
        assert plan.preflight_forecast["query_safety"]["client_field_used_for_discovery"] is False
        forbidden = {"greece", "ελλάδα", "ellada", client.casefold()}
        for sp in plan.sources:
            assert all(q.casefold().strip() not in forbidden for q in sp.queries)
            assert all(client.casefold() not in q.casefold() for q in sp.queries)
            if sp.source == "x":
                for sr in sp.subruns:
                    assert all(client.casefold() not in q.casefold() for q in sr.input.get("searchTerms", []))
