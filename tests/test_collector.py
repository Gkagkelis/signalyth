import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.services.normalizer import normalize_item
from app.services.collector import (
    execute_plan,
    BudgetGuard,
    _scaled_targets,
    _resize_input,
)


class BasicFakeRunner:
    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        if actor_id == "bad/actor":
            raise RuntimeError("deliberate source failure")
        return {"id": "fake", "defaultDatasetId": "fake", "usageTotalUsd": min(0.02, max_charge_usd)}, [
            {"text": "μέσα στο range", "timestamp": "2026-08-15T10:00:00Z", "url": f"https://example/{actor_id}/1", "likeCount": 10, "viewCount": 100},
            {"text": "εκτός range", "timestamp": "2026-09-01T10:00:00Z", "url": f"https://example/{actor_id}/2"},
        ]


class ElasticFakeRunner:
    """Instagram is scarce; X can supply exactly the requested elastic target."""
    calls = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append((actor_id, max_items, max_charge_usd, dict(run_input)))
        if actor_id == "scarce/instagram":
            n = min(10, max_items)
        elif actor_id == "empty/source":
            n = 0
        elif actor_id == "bad/actor":
            raise RuntimeError("deliberate source failure")
        else:
            n = max_items
        items = [
            {"caption": f"item {i}", "timestamp": "2026-08-15T10:00:00Z", "url": f"https://{actor_id}/{i}"}
            for i in range(n)
        ]
        cost = min(max_charge_usd, max(0.001, n * 0.0001))
        return {"id": f"run-{actor_id}", "defaultDatasetId": "fake", "usageTotalUsd": cost}, items


class CollectorTests(unittest.TestCase):
    def setUp(self):
        ElasticFakeRunner.calls = []

    def test_normalizer_handles_millisecond_timestamp_and_metrics(self):
        row = normalize_item("facebook", {"postText": "γειά", "timestamp": 1786773600000, "url": "u", "likesCount": 4, "commentsCount": 2, "shares": 1})
        self.assertEqual(row["platform"], "facebook")
        self.assertEqual(row["text"], "γειά")
        self.assertEqual(row["likes"], 4)
        self.assertEqual(row["comments"], 2)
        self.assertEqual(row["shares"], 1)
        self.assertIsNotNone(row["date"])

    def test_budget_guard_breaks_deliberately(self):
        g = BudgetGuard(1.0)
        reservation = g.reserve(0.6)
        g.settle(reservation, 0.2)
        self.assertAlmostEqual(g.spent, 0.2)
        self.assertAlmostEqual(g.remaining, 0.8)
        with self.assertRaises(RuntimeError):
            g.reserve(0.81)

    def test_unknown_actual_cost_is_conservative(self):
        g = BudgetGuard(1.0)
        reservation = g.reserve(0.4)
        charged = g.settle(reservation, None)
        self.assertAlmostEqual(charged, 0.4)
        self.assertAlmostEqual(g.remaining, 0.6)

    def test_scaled_targets_preserve_exact_total(self):
        self.assertEqual(sum(_scaled_targets([25, 25, 50], 137)), 137)
        self.assertEqual(_scaled_targets([1, 1], 5), [3, 2])

    def test_source_input_resize_updates_actor_limit(self):
        self.assertEqual(_resize_input("instagram", {"resultsLimit": 50}, 50, 90)["resultsLimit"], 90)
        self.assertEqual(_resize_input("x", {"maxItems": 50}, 50, 90)["maxItems"], 90)
        self.assertEqual(_resize_input("facebook", {"resultsCount": 50}, 50, 999)["resultsCount"], 999)

    def test_source_failure_is_isolated_and_exact_date_filter_applies(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            plan = {"max_budget_usd": 2.0, "date_from": "2026-08-01", "date_to": "2026-08-31", "sample_mode": "perSource", "target_total": 4, "rebalancing_enabled": False, "sources": [
                {"source": "x", "target_items": 2, "price_per_1000_hint": 0.15, "subruns": [{"actor_id": "good/actor", "input": {"maxItems": 2}, "target_items": 2, "max_charge_usd": 0.5}]},
                {"source": "facebook", "target_items": 2, "price_per_1000_hint": 2.49, "subruns": [{"actor_id": "bad/actor", "input": {"resultsCount": 2}, "target_items": 2, "max_charge_usd": 0.5}]},
            ]}
            with patch("app.services.collector.ApifyRunner", BasicFakeRunner):
                status = execute_plan(plan, "test-run", folder)
            self.assertEqual(status["sources"]["x"]["status"], "succeeded")
            self.assertEqual(status["sources"]["facebook"]["status"], "failed")
            self.assertEqual(status["status"], "completed_with_errors")
            rows = json.loads((folder / "normalized-x.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 1)
            self.assertIn("μέσα", rows[0]["text"])

    def elastic_plan(self, automatic=True):
        return {
            "max_budget_usd": 2.0,
            "date_from": "2026-08-01",
            "date_to": "2026-08-31",
            "sample_mode": "automatic" if automatic else "perSource",
            "target_total": 100,
            "rebalancing_enabled": automatic,
            "sources": [
                {"source": "x", "target_items": 50, "price_per_1000_hint": 0.15, "subruns": [{"actor_id": "elastic/x", "input": {"maxItems": 50}, "target_items": 50, "max_charge_usd": 0.4}]},
                {"source": "instagram", "target_items": 50, "price_per_1000_hint": 2.7, "subruns": [{"actor_id": "scarce/instagram", "input": {"resultsLimit": 50}, "target_items": 50, "max_charge_usd": 0.6}]},
            ],
        }

    def test_automatic_mode_rebalances_shortfall_to_remaining_source(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                status = execute_plan(self.elastic_plan(True), "elastic", Path(td))
        # Expensive Instagram runs first and supplies 10/50. X absorbs the 40-item carry: 50 -> 90.
        self.assertEqual(status["sources"]["instagram"]["collected"], 10)
        self.assertEqual(status["sources"]["x"]["adjusted_target"], 90)
        self.assertEqual(status["sources"]["x"]["collected"], 90)
        self.assertEqual(status["normalized_total"], 100)
        self.assertEqual(status["sample_status"], "target_met")
        self.assertEqual(status["sample_shortfall"], 0)

    def test_per_source_mode_never_redistributes_user_targets(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                status = execute_plan(self.elastic_plan(False), "fixed", Path(td))
        self.assertEqual(status["sources"]["x"]["adjusted_target"], 50)
        self.assertEqual(status["normalized_total"], 60)
        self.assertEqual(status["sample_shortfall"], 40)
        self.assertEqual(status["sample_status"], "shortfall")

    def test_failed_source_shortfall_can_be_absorbed_automatically(self):
        plan = self.elastic_plan(True)
        plan["sources"][1]["subruns"][0]["actor_id"] = "bad/actor"
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                status = execute_plan(plan, "failure-carry", Path(td))
        self.assertEqual(status["sources"]["instagram"]["status"], "failed")
        self.assertEqual(status["sources"]["x"]["adjusted_target"], 100)
        self.assertEqual(status["normalized_total"], 100)
        self.assertEqual(status["sample_shortfall"], 0)
        self.assertEqual(status["status"], "completed_with_errors")

    def test_unfillable_target_reports_shortfall_instead_of_looping_or_inventing_data(self):
        plan = self.elastic_plan(True)
        plan["sources"][0]["subruns"][0]["actor_id"] = "empty/source"
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                status = execute_plan(plan, "shortfall", Path(td))
        self.assertEqual(status["sample_status"], "shortfall")
        self.assertGreater(status["sample_shortfall"], 0)
        self.assertLessEqual(status["budget"]["spent_usd"], status["budget"]["max_usd"])

    def test_actor_overdelivery_is_preserved_raw_but_analysis_sample_is_capped(self):
        class OverRunner:
            def run(self, actor_id, run_input, *, max_items, max_charge_usd):
                items=[{"text":f"over {i}","timestamp":"2026-08-15T00:00:00Z","url":f"https://over/{i}"} for i in range(max_items+25)]
                return {"id":"over","defaultDatasetId":"over","usageTotalUsd":0.001},items
        plan={"max_budget_usd":1,"date_from":"2026-08-01","date_to":"2026-08-31","sample_mode":"perSource",
              "target_total":50,"rebalancing_enabled":False,"sources":[
              {"source":"x","target_items":50,"price_per_1000_hint":0.15,"subruns":[
              {"actor_id":"over/actor","input":{"maxItems":50},"target_items":50,"max_charge_usd":0.2}]}]}
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            with patch("app.services.collector.ApifyRunner", OverRunner):
                status=execute_plan(plan,"over",folder)
            raw=json.loads((folder/"raw-x.json").read_text(encoding="utf-8"))
            normalized=json.loads((folder/"normalized-x.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw),75)
        self.assertEqual(len(normalized),50)
        self.assertEqual(status["normalized_total"],50)
        self.assertEqual(status["sources"]["x"]["capped_out"],25)
        self.assertEqual(status["sample_status"],"target_met")

    def test_undated_items_are_preserved_raw_but_excluded_from_exact_date_sample(self):
        class UndatedRunner:
            def run(self, actor_id, run_input, *, max_items, max_charge_usd):
                return {"id":"u","defaultDatasetId":"u","usageTotalUsd":0.001}, [
                    {"text":"dated","timestamp":"2026-08-15T00:00:00Z","url":"https://u/dated"},
                    {"text":"undated","url":"https://u/undated"},
                ]
        plan={"max_budget_usd":1,"date_from":"2026-08-01","date_to":"2026-08-31","sample_mode":"perSource",
              "target_total":2,"rebalancing_enabled":False,"sources":[
              {"source":"x","target_items":2,"price_per_1000_hint":0.15,"subruns":[
              {"actor_id":"undated/actor","input":{"maxItems":2},"target_items":2,"max_charge_usd":0.2}]}]}
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            with patch("app.services.collector.ApifyRunner", UndatedRunner):
                status=execute_plan(plan,"undated",folder)
            raw=json.loads((folder/"raw-x.json").read_text(encoding="utf-8"))
            normalized=json.loads((folder/"normalized-x.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw),2)
        self.assertEqual(len(normalized),1)
        self.assertEqual(status["sample_shortfall"],1)
        self.assertEqual(status["status"],"completed_shortfall")

    def test_failed_run_cost_is_accounted_conservatively(self):
        plan=self.elastic_plan(True)
        plan["sources"][1]["subruns"][0]["actor_id"]="bad/actor"
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                status=execute_plan(plan,"failed-cost",Path(td))
        self.assertGreater(status["sources"]["instagram"]["cost_usd"],0)
        self.assertGreater(status["budget"]["spent_usd"],0)
        self.assertLessEqual(status["budget"]["spent_usd"],status["budget"]["max_usd"])

    def test_automatic_rebalancing_does_not_repeat_successful_subruns(self):
        with tempfile.TemporaryDirectory() as td:
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                execute_plan(self.elastic_plan(True),"single-pass",Path(td))
        actors=[row[0] for row in ElasticFakeRunner.calls]
        self.assertEqual(actors.count("scarce/instagram"),1)
        self.assertEqual(actors.count("elastic/x"),1)

    def test_rebalancing_audit_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with patch("app.services.collector.ApifyRunner", ElasticFakeRunner):
                execute_plan(self.elastic_plan(True), "audit", folder)
            audit = json.loads((folder / "rebalancing.json").read_text(encoding="utf-8"))
            self.assertEqual([row["source"] for row in audit], ["instagram", "x"])
            self.assertEqual(audit[1]["carry_in"], 40)
            self.assertEqual(audit[1]["adjusted_target"], 90)


if __name__ == "__main__":
    unittest.main()


def test_xquik_real_style_rfc_date_and_flat_author_normalize():
    from app.services.normalizer import normalize_item
    item = {
        "id": "2088959769847501210",
        "text": "Allwyn test",
        "createdAt": "Sun Aug 16 12:03:14 +0000 2026",
        "author": {"username": "nested_should_not_stringify", "followers": 10},
        "authorUsername": "CKonstantonis",
        "authorFollowers": 123,
        "replyCount": 2,
        "viewCount": 441,
        "likeCount": 8,
        "retweetCount": 1,
        "url": "https://x.com/example/status/2088959769847501210",
        "type": "tweet",
    }
    row = normalize_item("x", item)
    assert row["author"] == "CKonstantonis"
    assert row["date"] == "2026-08-16T12:03:14+00:00"
    assert row["comments"] == 2 and row["views"] == 441 and row["likes"] == 8 and row["shares"] == 1
