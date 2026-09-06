import copy
import math
import tempfile
import unittest
from pathlib import Path

from app.services.intelligence import (
    INTELLIGENCE_RULESET_VERSION,
    METHODOLOGY_VERSION,
    _group_contributions,
    build_intelligence,
    compute_intelligence,
    load_intelligence_summary,
)
from app.services.storage import RunStore


def plan(target=100):
    return {
        "client": "OPAP", "topic": "Eurojackpot", "market": "Greece",
        "date_from": "2026-08-01", "date_to": "2026-08-31", "target_total": target,
        "sources": [{"source": "x", "target_items": target}],
    }


def quality(score=90, coverage=1.0, achievement=1.0):
    return {"data_quality_score": score, "source_coverage_ratio": coverage, "sample_achievement_ratio": achievement}


def row(i, sentiment=0.0, label="neutral", *, platform="x", origin="earned_person", content="organic",
        opinion=True, stance="neutral", narrative="General discussion", topic="Lottery discussion",
        emotion="neutral", independent=1.0, authenticity=100, confidence=0.95,
        followers=100, views=100, likes=10, comments=2, shares=1, day=15):
    account = "person_or_creator"
    if origin == "earned_media": account = "media"
    elif origin == "owned": account = "brand_owned"
    elif origin == "earned_organization": account = "organization"
    return {
        "id": str(i), "platform": platform, "text": f"text {i}",
        "date": f"2026-08-{day:02d}T10:00:00+00:00", "author": f"author-{i}",
        "followers": followers, "views": views, "likes": likes, "comments": comments, "shares": shares,
        "url": f"https://example/{i}", "content_type": "post", "parent_post": None, "raw_data": {},
        "cleaning": {
            "decision": "trusted", "confidence": confidence, "authenticity_score": authenticity,
            "origin_class": origin, "content_class": content, "account_type": account,
            "organic_eligible": opinion, "independent_voice_weight": independent,
            "story_cluster_id": None, "coordination_cluster_id": None,
        },
        "ai_analysis": {
            "decision": "ready", "semantic_relevance": "relevant", "relevance_confidence": confidence,
            "sentiment_label": label, "sentiment_score": sentiment, "sentiment_confidence": confidence,
            "primary_emotion": emotion, "emotion_intensity": 0.7, "emotion_confidence": confidence,
            "target_stance": stance, "topic": topic, "narrative": narrative,
            "overall_confidence": confidence, "opinion_eligible": opinion,
        },
    }


class IntelligenceTests(unittest.TestCase):
    def test_neutral_evidence_maps_to_50(self):
        result = compute_intelligence([row(1, 0.0, "neutral")], plan(1), quality())
        self.assertEqual(result["summary"]["brand_reputation"]["index"], 50.0)

    def test_fully_positive_maps_to_100(self):
        result = compute_intelligence([row(1, 1.0, "positive", stance="supportive")], plan(1), quality())
        self.assertEqual(result["summary"]["brand_reputation"]["index"], 100.0)

    def test_fully_negative_maps_to_zero(self):
        result = compute_intelligence([row(1, -1.0, "negative", stance="critical")], plan(1), quality())
        self.assertEqual(result["summary"]["brand_reputation"]["index"], 0.0)

    def test_owned_content_does_not_change_reputation(self):
        rows = [
            row(1, -0.6, "negative", stance="critical"),
            row(2, 1.0, "positive", origin="owned", content="owned", opinion=False, stance="supportive", views=1000000),
        ]
        result = compute_intelligence(rows, plan(2), quality())
        self.assertLess(result["summary"]["brand_reputation"]["index"], 50)
        owned = next(r for r in result["records"] if r["id"] == "2")
        self.assertFalse(owned["intelligence"]["reputation_eligible"])
        self.assertEqual(owned["intelligence"]["reputation_exclusion_reason"], "owned_or_promotional")

    def test_promotional_content_is_excluded_from_reputation(self):
        promo = row(1, 1.0, "positive", content="promotional", opinion=False, stance="supportive")
        result = compute_intelligence([promo], plan(1), quality())
        self.assertIsNone(result["summary"]["brand_reputation"]["index"])

    def test_factual_news_without_stance_is_dissemination_not_reputation(self):
        news = row(1, -0.8, "negative", origin="earned_media", content="news", opinion=False, stance="not_applicable")
        result = compute_intelligence([news], plan(1), quality())
        self.assertIsNone(result["summary"]["brand_reputation"]["index"])
        self.assertEqual(result["summary"]["origin_breakdown"]["media"]["records"], 1)

    def test_story_cluster_independent_weight_reduces_replication(self):
        rows = [
            row(1, -1, "negative", stance="critical", independent=0.1),
            row(2, -1, "negative", stance="critical", independent=0.1),
            row(3, 1, "positive", stance="supportive", independent=1.0),
        ]
        result = compute_intelligence(rows, plan(3), quality())
        self.assertGreater(result["summary"]["brand_reputation"]["index"], 50)

    def test_missing_public_metrics_are_unknown_not_zero_impact(self):
        r = row(1, 0.2, "positive", views=0, followers=0, likes=0, comments=0, shares=0)
        result = compute_intelligence([r], plan(1), quality())
        intel = result["records"][0]["intelligence"]
        self.assertEqual(intel["impact_score"], 0.5)
        self.assertEqual(intel["impact_confidence"], 0.0)

    def test_available_impact_signals_are_reweighted_not_penalized(self):
        r = row(1, 0.2, "positive", views=1000, followers=0, likes=0, comments=0, shares=0)
        result = compute_intelligence([r], plan(1), quality())
        intel = result["records"][0]["intelligence"]
        self.assertGreater(intel["impact_score"], 0)
        self.assertEqual(intel["available_signals"], ["visibility"])
        self.assertAlmostEqual(intel["impact_confidence"], 0.5, places=6)

    def test_high_impact_negative_has_more_weight_than_low_impact_positive(self):
        rows = []
        for i in range(1, 6):
            rows.append(row(i, 0.5, "positive", stance="supportive", views=10, likes=1, comments=0, shares=0, followers=10))
        rows.append(row(99, -0.9, "negative", stance="critical", views=1000000, likes=50000, comments=5000, shares=20000, followers=100000))
        result = compute_intelligence(rows, plan(6), quality())
        # Bounded impact can amplify the viral negative without letting it become arbitrarily dominant.
        negative = next(r for r in result["records"] if r["id"] == "99")["intelligence"]["reputation_weight"]
        positive = next(r for r in result["records"] if r["id"] == "1")["intelligence"]["reputation_weight"]
        self.assertGreater(negative, positive)

    def test_impact_is_bounded(self):
        rows = [row(1, 0.1, "positive", views=10**15, likes=10**15, comments=10**15, shares=10**15, followers=10**15)]
        result = compute_intelligence(rows, plan(1), quality())
        self.assertLessEqual(result["records"][0]["intelligence"]["impact_score"], 1.0)

    def test_emotions_use_organic_opinion_layer(self):
        rows = [
            row(1, -0.6, "negative", emotion="anger", opinion=True, stance="critical"),
            row(2, -0.6, "negative", emotion="anger", opinion=False, origin="earned_media", content="news", stance="critical"),
        ]
        result = compute_intelligence(rows, plan(2), quality())
        emo = result["summary"]["emotions_organic_people"]
        self.assertEqual(emo["records"], 1)
        self.assertEqual(emo["counts"]["anger"], 1)

    def test_narrative_contributions_sum_to_reputation_deviation(self):
        rows = [
            row(1, 0.8, "positive", stance="supportive", narrative="Winning dream"),
            row(2, -0.4, "negative", stance="critical", narrative="Price concern"),
            row(3, -0.2, "negative", stance="critical", narrative="Price concern"),
        ]
        result = compute_intelligence(rows, plan(3), quality())
        groups = _group_contributions(result["records"], "narrative", 100)
        contribution = sum(x["reputation_point_contribution"] for x in groups)
        index = result["summary"]["brand_reputation"]["index"]
        self.assertAlmostEqual(contribution, index - 50.0, places=2)

    def test_source_breakdown_is_independent(self):
        rows = [
            row(1, 0.8, "positive", platform="x", stance="supportive"),
            row(2, -0.8, "negative", platform="news", origin="earned_media", content="news", opinion=False, stance="critical"),
        ]
        result = compute_intelligence(rows, plan(2), quality())
        self.assertIn("x", result["summary"]["source_breakdown"])
        self.assertIn("news", result["summary"]["source_breakdown"])

    def test_media_and_people_influence_are_separate(self):
        rows = [
            row(1, 0.2, "positive", origin="earned_person"),
            row(2, 0.0, "neutral", origin="earned_media", content="news", opinion=False, stance="not_applicable"),
        ]
        # Override authors after helper generation.
        rows[0]["author"] = "Person A"
        rows[1]["author"] = "Media A"
        result = compute_intelligence(rows, plan(2), quality())
        self.assertEqual(result["summary"]["people_influence"][0]["author"], "Person A")
        self.assertEqual(result["summary"]["media_influence"][0]["author"], "Media A")

    def test_daily_series_keeps_numeric_metrics_only(self):
        rows = [row(i, 0.1, "positive", day=1 + i, stance="supportive") for i in range(1, 8)]
        result = compute_intelligence(rows, plan(7), quality())
        self.assertEqual(len(result["time_series"]["daily"]), 7)
        self.assertTrue(all("cause" not in x for x in result["time_series"]["anomalies"]))

    def test_volume_spike_is_flagged_with_robust_baseline(self):
        rows = []
        idx = 1
        for day in range(1, 8):
            n = 20 if day == 7 else 1
            for _ in range(n):
                rows.append(row(idx, 0.0, "neutral", day=day))
                idx += 1
        result = compute_intelligence(rows, plan(len(rows)), quality())
        flags = [f for a in result["time_series"]["anomalies"] for f in a["flags"]]
        self.assertIn("volume_spike", flags)

    def test_confidence_is_separate_from_reputation_point_estimate(self):
        rows = [row(1, 0.8, "positive", stance="supportive")]
        high = compute_intelligence(rows, plan(1), quality(95, 1.0, 1.0))["summary"]
        low = compute_intelligence(rows, plan(1), quality(20, 0.2, 0.2))["summary"]
        self.assertEqual(high["brand_reputation"]["index"], low["brand_reputation"]["index"])
        self.assertGreater(high["confidence"]["score"], low["confidence"]["score"])

    def test_low_effective_sample_creates_warning(self):
        result = compute_intelligence([row(1, 0.4, "positive", stance="supportive")], plan(100), quality())
        self.assertTrue(any("effective independent sample" in w for w in result["summary"]["warnings"]))

    def test_no_reputation_evidence_returns_none_not_fake_50(self):
        result = compute_intelligence([row(1, 0.0, "neutral", stance="not_applicable")], plan(1), quality())
        self.assertIsNone(result["summary"]["brand_reputation"]["index"])
        self.assertEqual(result["summary"]["brand_reputation"]["interpretation"], "insufficient_evidence")

    def test_input_is_never_mutated(self):
        rows = [row(1, 0.2, "positive", stance="supportive")]
        before = copy.deepcopy(rows)
        compute_intelligence(rows, plan(1), quality())
        self.assertEqual(rows, before)

    def test_duplicate_ids_fail_before_aggregation(self):
        rows = [row(1), row(1)]
        with self.assertRaises(RuntimeError):
            compute_intelligence(rows, plan(2), quality())

    def test_all_numbers_are_finite(self):
        rows = [row(i, (-1) ** i * 0.5, "positive" if i % 2 == 0 else "negative", stance="supportive" if i % 2 == 0 else "critical") for i in range(1, 50)]
        result = compute_intelligence(rows, plan(len(rows)), quality())
        def walk(v):
            if isinstance(v, dict):
                for x in v.values(): walk(x)
            elif isinstance(v, list):
                for x in v: walk(x)
            elif isinstance(v, float):
                self.assertTrue(math.isfinite(v))
        walk(result)

    def test_ruleset_and_methodology_are_versioned(self):
        result = compute_intelligence([row(1)], plan(1), quality())
        self.assertEqual(result["summary"]["ruleset_version"], INTELLIGENCE_RULESET_VERSION)
        self.assertEqual(result["summary"]["methodology_version"], METHODOLOGY_VERSION)
        self.assertIn("formula", result["methodology"]["brand_reputation"])

    def test_persistence_is_idempotent_and_stale_detection_works(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            store = RunStore()
            store.write(folder / "analysis" / "analysis-ready.json", [row(1, 0.3, "positive", stance="supportive")])
            store.write(folder / "cleaning" / "report.json", quality())
            first = build_intelligence(folder, plan(1))
            second = build_intelligence(folder, plan(1))
            self.assertEqual(first["input_hash"], second["input_hash"])
            report = load_intelligence_summary(folder)
            self.assertFalse(report["stale"])
            changed = row(1, -0.3, "negative", stance="critical")
            store.write(folder / "analysis" / "analysis-ready.json", [changed])
            self.assertTrue(load_intelligence_summary(folder)["stale"])

    def test_persists_separate_audit_methodology_and_timeseries(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            store = RunStore()
            store.write(folder / "analysis" / "analysis-ready.json", [row(1, 0.3, "positive", stance="supportive")])
            store.write(folder / "cleaning" / "report.json", quality())
            build_intelligence(folder, plan(1))
            for name in ("records.json", "summary.json", "time-series.json", "top-mentions.json", "audit.json", "methodology.json"):
                self.assertTrue((folder / "intelligence" / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
