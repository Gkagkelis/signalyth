import itertools
import unittest
from datetime import date
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan

SOURCES=["x","tiktok","instagram","facebook","youtube","news"]

class StressTests(unittest.TestCase):
    def make(self, sources, total):
        return AnalysisDraft(
            client="OPAP", topic="Eurojackpot", market="Greece",
            date_from=date(2026,8,1), date_to=date(2026,8,31),
            keywords=["Eurojackpot"], sources=list(sources), sample_mode="automatic",
            sample_target=total, max_budget_usd=1000, smart_search=True,
            additional_context=["ΟΠΑΠ"], exclusions=["KNVB"],
        )

    def test_252_automatic_source_and_sample_combinations_preserve_exact_total(self):
        checked=0
        for n in range(1,len(SOURCES)+1):
            for combo in itertools.combinations(SOURCES,n):
                for total in (1,500,1000,3000):
                    p=build_collection_plan(self.make(combo,total))
                    self.assertEqual(p.target_total,total)
                    self.assertTrue(p.rebalancing_enabled)
                    self.assertLessEqual(sum(sr.max_charge_usd for sp in p.sources for sr in sp.subruns), p.max_budget_usd+1e-9)
                    checked+=1
        self.assertEqual(checked,252)

    def test_per_source_many_integer_distributions_stay_exact(self):
        for a,b,c in ((1,1,1),(300,150,550),(0,1,999),(777,222,1),(10000,20000,30000)):
            d=AnalysisDraft(
                client="C",topic="T",market="Greece",date_from=date(2026,8,1),date_to=date(2026,8,31),
                keywords=["T"],sources=["x","news","youtube"],sample_mode="perSource",sample_target=1000,
                per_source={"x":a,"news":b,"youtube":c},max_budget_usd=1000,
            )
            p=build_collection_plan(d)
            self.assertEqual(p.target_total,a+b+c)
            self.assertFalse(p.rebalancing_enabled)

    def test_duplicate_sources_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make(["x","x"],100)

if __name__ == "__main__":
    unittest.main()
