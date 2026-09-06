import unittest
from datetime import date
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan, allocate_equal

class PlannerTests(unittest.TestCase):
    def base(self, **changes):
        data=dict(client="OPAP",topic="Eurojackpot",market="Greece",date_from=date(2026,8,1),date_to=date(2026,8,31),
                  keywords=["Eurojackpot"],sources=["x","tiktok","instagram","facebook","youtube","news"],sample_target=1000,
                  sample_mode="automatic",comments=False,max_budget_usd=10,smart_search=True,report_language="Ελληνικά",
                  additional_context=[],exclusions=["KNVB"])
        data.update(changes); return AnalysisDraft(**data)

    def test_equal_allocation_sums_exactly(self):
        a=allocate_equal(1000,["x","tiktok","instagram","facebook","youtube","news"])
        self.assertEqual(sum(a.values()),1000)
        self.assertLessEqual(max(a.values())-min(a.values()),1)

    def test_greece_context_and_context_greeklish_are_topic_anchored(self):
        p=build_collection_plan(self.base(additional_context=["ΟΠΑΠ"]))
        self.assertIn("Ελλάδα",p.context_terms)
        self.assertIn("OPAP",[v.upper() for v in p.context_terms])
        self.assertNotIn("OPAP",[v.upper() for v in p.greeklish_variants])
        self.assertTrue(all(q.casefold() != "opap" for sp in p.sources for q in sp.queries))

    def test_native_and_post_filter_date_strategies(self):
        p=build_collection_plan(self.base())
        by={x.source:x for x in p.sources}
        self.assertEqual(by["x"].date_strategy,"native_exact")
        self.assertTrue(by["tiktok"].subruns[0].exact_post_filter)
        self.assertTrue(by["youtube"].subruns[0].exact_post_filter)
        self.assertTrue(all(x.exact_post_filter for x in by["instagram"].subruns))

    def test_facebook_has_exact_start_end(self):
        p=build_collection_plan(self.base(sources=["facebook"],sample_target=50))
        for sr in p.sources[0].subruns:
            self.assertEqual(sr.input["startDate"],"2026-08-01")
            self.assertEqual(sr.input["endDate"],"2026-08-31")

    def test_news_greece_greek(self):
        p=build_collection_plan(self.base(sources=["news"],sample_target=100))
        inp=p.sources[0].subruns[0].input
        self.assertEqual(inp["country"],"GR"); self.assertEqual(inp["language"],"el")
        self.assertEqual(inp["fromDate"],"2026-08-01"); self.assertEqual(inp["toDate"],"2026-08-31")

    def test_per_source_exact_total(self):
        p=build_collection_plan(self.base(sources=["x","news"],sample_mode="perSource",per_source={"x":300,"news":700}))
        self.assertEqual(p.target_total,1000)
        self.assertEqual({x.source:x.target_items for x in p.sources},{"x":300,"news":700})

    def test_subrun_caps_never_exceed_analysis_budget(self):
        p=build_collection_plan(self.base(max_budget_usd=5))
        total=sum(sr.max_charge_usd for sp in p.sources for sr in sp.subruns)
        self.assertLessEqual(total,5.0001)

    def test_budget_overage_is_flagged(self):
        p=build_collection_plan(self.base(sample_target=100000,max_budget_usd=0.05))
        self.assertEqual(p.budget_check,"estimate_over_budget")

    def test_automatic_plan_enables_rebalancing_and_carries_price_hints(self):
        p=build_collection_plan(self.base(sources=["x","instagram"], sample_target=100))
        self.assertEqual(p.sample_mode,"automatic")
        self.assertTrue(p.rebalancing_enabled)
        by={x.source:x for x in p.sources}
        self.assertEqual(by["x"].price_per_1000_hint,0.15)
        self.assertEqual(by["instagram"].price_per_1000_hint,2.7)

    def test_per_source_plan_disables_cross_source_rebalancing(self):
        p=build_collection_plan(self.base(sources=["x","news"],sample_mode="perSource",per_source={"x":30,"news":70}))
        self.assertEqual(p.sample_mode,"perSource")
        self.assertFalse(p.rebalancing_enabled)

    def test_invalid_dates_break(self):
        with self.assertRaises(ValueError):
            self.base(date_from=date(2026,9,1),date_to=date(2026,8,31))

if __name__ == '__main__': unittest.main()
