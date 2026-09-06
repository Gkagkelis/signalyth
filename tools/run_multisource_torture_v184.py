from __future__ import annotations

import itertools
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.resilience import run_actor_resilient, split_diagnostic_rows

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]
ARRAY_FIELDS = ("searchTerms", "search", "queries", "keywords", "directUrls", "startUrls", "replyTweetIds", "urls")
SAFE = {"x": 2, "tiktok": 2, "instagram": 1, "facebook": 1, "youtube": 2, "news": 2}
SEED = 18220260905


def all_combos():
    for r in range(1, 7):
        yield from itertools.combinations(SOURCES, r)


def make_draft(combo, target, budget, comments=True):
    return AnalysisDraft(
        client="Stress Client", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn", "OPAP"], sources=list(combo), sample_mode="automatic",
        sample_target=target, comments=comments, max_budget_usd=budget,
        smart_search=True, report_language="Ελληνικά",
    )


class ScenarioRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "504":
            raise RuntimeError("HTTP 504 gateway timeout")
        if outcome == "429":
            raise RuntimeError("HTTP 429 rate limit")
        if outcome == "403":
            raise RuntimeError("HTTP 403 forbidden")
        if outcome == "diagnostic504":
            return (
                {"status": "SUCCEEDED", "statusMessage": "partial_failure HTTP 504", "usageTotalUsd": min(0.0001, max_charge_usd)},
                [{"id": "diag:z", "resultType": "diagnostic", "status": "zero-output", "message": "Fetch failed HTTP 504"}],
            )
        if outcome == "empty":
            return (
                {"status": "SUCCEEDED", "statusMessage": "No tweets matched", "usageTotalUsd": min(0.0001, max_charge_usd)},
                [{"id": "diag:e", "resultType": "diagnostic", "status": "zero-output", "message": "No tweets matched"}],
            )
        if outcome == "partial":
            return (
                {"status": "SUCCEEDED", "statusMessage": "partial_failure HTTP 504", "usageTotalUsd": min(0.0001, max_charge_usd)},
                [
                    {"id": f"real-{self.calls}", "text": "real evidence", "url": f"https://e/{self.calls}"},
                    {"id": f"diag:p-{self.calls}", "resultType": "diagnostic", "status": "unexpected-error", "message": "HTTP 504"},
                ],
            )
        if outcome == "overcap":
            return (
                {"status": "SUCCEEDED", "usageTotalUsd": max_charge_usd + 0.25},
                [{"id": f"real-{self.calls}", "text": "evidence despite cost anomaly", "url": f"https://e/{self.calls}"}],
            )
        return (
            {"status": "SUCCEEDED", "usageTotalUsd": min(0.0001, max_charge_usd)},
            [{"id": f"real-{self.calls}", "text": "real evidence", "url": f"https://e/{self.calls}"}],
        )


def main():
    rnd = random.Random(SEED)
    checks = 0
    scenario_failures = []

    # Exhaustive 63-combination planner matrix across multiple targets and budgets.
    combo_count = 0
    for combo in all_combos():
        combo_count += 1
        for target in (1, 100, 500, 1000, 3000):
            for budget in (0.01, 0.25, 1.0, 5.0, 20.0):
                plan = build_collection_plan(make_draft(combo, target, budget, comments=True))
                caps = sum(sr.max_charge_usd for sp in plan.sources for sr in sp.subruns)
                assert caps <= budget + 1e-9, (combo, target, budget, caps)
                assert sum(sp.target_items for sp in plan.sources) == target
                assert plan.target_total == target
                assert set(plan.preflight_forecast["selected_sources"]).issubset(set(combo))
                assert set(plan.preflight_forecast["selected_sources"]) == {sp.source for sp in plan.sources}
                assert plan.preflight_forecast["comments_coverage"]["fully_live_verified"] is False
                assert plan.preflight_forecast["query_safety"]["client_field_used_for_discovery"] is False
                assert plan.preflight_forecast["query_safety"]["market_only_queries_allowed"] is False
                for sp in plan.sources:
                    forbidden = {"greece", "ελλάδα", "ellada", "stress client"}
                    assert all(q.casefold().strip() not in forbidden for q in sp.queries)
                    assert all("stress client" not in q.casefold() for q in sp.queries)
                    if sp.source != "x":
                        assert all("allwyn" in q.casefold() for q in sp.queries)
                    for sr in sp.subruns:
                        for field in ARRAY_FIELDS:
                            value = sr.input.get(field)
                            if isinstance(value, list):
                                assert len(value) <= SAFE[sp.source], (sp.source, field, len(value))
                checks += 12

    # Randomized resilience scenarios: 50,000 logical Actor acquisitions.
    outcome_space = ["success", "success", "empty", "504", "429", "403", "diagnostic504", "partial", "overcap"]
    for i in range(50_000):
        target_count = rnd.randint(1, 8)
        max_items = rnd.randint(1, 1000)
        budget = round(rnd.uniform(0.001, 0.25), 6)
        run_input = {"searchTerms": [f"q{n}" for n in range(target_count)], "maxItems": max_items}
        outcomes = [rnd.choice(outcome_space) for _ in range(8)]
        runner = ScenarioRunner(outcomes)
        try:
            result = run_actor_resilient(
                runner, "stress/actor", run_input,
                max_items=max_items, max_charge_usd=budget,
                rate_per_1000=rnd.choice([None, 0.15, 0.5, 2.7]),
                max_calls=6, sleep_fn=lambda _: None,
            )
            assert result.accounted_cost_usd <= budget + 1e-9
            assert runner.calls <= 6
            data, diagnostics = split_diagnostic_rows(result.items)
            assert len(diagnostics) == 0  # result.items can never contain diagnostic rows
            assert len(data) == len(result.items)
            assert result.status in {"succeeded", "empty", "partial", "failed"}
            if result.cost_cap_violation:
                assert "cost_cap_violation" in result.failure_kinds
                assert result.provider_reported_cost_usd > 0
            checks += 7
        except Exception as exc:
            scenario_failures.append({"scenario": i, "error": repr(exc), "outcomes": outcomes})
            if len(scenario_failures) >= 20:
                break

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "source_combinations": combo_count,
        "planner_matrix_variants": combo_count * 5 * 5,
        "random_resilience_scenarios": 50_000 if not scenario_failures else scenario_failures[0]["scenario"] + 1,
        "invariant_checks": checks,
        "failures": scenario_failures,
        "status": "PASS" if not scenario_failures else "FAIL",
        "invariants": [
            "all 63 non-empty source combinations plan successfully",
            "planned logical-batch caps never exceed user max budget",
            "source targets sum exactly to requested sample",
            "multi-target fan-out stays within conservative source batch limits",
            "comments coverage is never falsely presented as live-verified",
            "resilient retries never exceed logical cost envelope",
            "resilient retries are bounded",
            "diagnostic rows never become analysis rows",
            "client/agency names never leak into search discovery",
            "market-only queries are forbidden",
            "non-X smart-search queries stay topic-anchored",
        ],
    }
    out = Path("evals/multisource_resilience_torture_v183.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if scenario_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
