import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.services.intelligence import compute_intelligence
from app.services.investigations import (
    INDICATOR_REGISTRY,
    INVESTIGATION_RULESET_VERSION,
    PRESENTATION_EVIDENCE_CONTRACT_VERSION,
    InvestigationCancelled,
    build_investigations,
    compute_investigations,
    load_evidence_pack,
    load_investigation_summary,
)
from app.services.storage import RunStore


def plan():
    return {
        "client": "OPAP",
        "topic": "Eurojackpot",
        "market": "Greece",
        "date_from": "2026-08-01",
        "date_to": "2026-08-14",
        "target_total": 100,
        "sources": [
            {"source": "x", "target_items": 40},
            {"source": "news", "target_items": 30},
            {"source": "tiktok", "target_items": 30},
        ],
    }


def quality(score=92, coverage=1.0, achievement=1.0):
    return {
        "data_quality_score": score,
        "data_quality_label": "high" if score >= 80 else "medium",
        "source_coverage_ratio": coverage,
        "sample_achievement_ratio": achievement,
        "trusted_records": 100,
        "review_records": 0,
        "excluded_records": 0,
        "total_records": 100,
        "organic_opinion_records": 70,
        "high_greece_market_relevance": 92,
    }


def row(i, day=1, sentiment=0.0, label="neutral", emotion="neutral", narrative="General", topic="Lottery",
        platform="x", origin="earned_person", opinion=True, stance="neutral", impact=100,
        author=None, independent=1.0, authenticity=95, coordination=None, story=None, confidence=0.95):
    account = "person_or_creator"
    content = "organic"
    if origin == "earned_media":
        account = "media"; content = "news"
    elif origin == "owned":
        account = "brand_owned"; content = "owned"
    return {
        "id": str(i),
        "platform": platform,
        "text": f"evidence text {i} {narrative}",
        "date": f"2026-08-{day:02d}T10:00:00+00:00",
        "author": author or f"author-{i}",
        "followers": impact,
        "views": impact * 10,
        "likes": max(1, impact // 10),
        "comments": max(0, impact // 50),
        "shares": max(0, impact // 100),
        "url": f"https://example.com/{i}",
        "content_type": "post",
        "parent_post": None,
        "raw_data": {"views": impact * 10},
        "cleaning": {
            "decision": "trusted",
            "confidence": confidence,
            "authenticity_score": authenticity,
            "market_score": 0.9,
            "origin_class": origin,
            "content_class": content,
            "account_type": account,
            "organic_eligible": opinion,
            "independent_voice_weight": independent,
            "story_cluster_id": story,
            "coordination_cluster_id": coordination,
        },
        "ai_analysis": {
            "decision": "ready",
            "semantic_relevance": "relevant",
            "relevance_confidence": confidence,
            "sentiment_label": label,
            "sentiment_score": sentiment,
            "sentiment_confidence": confidence,
            "primary_emotion": emotion,
            "emotion_intensity": 0.8 if emotion != "neutral" else 0.2,
            "emotion_confidence": confidence,
            "target_stance": stance,
            "topic": topic,
            "narrative": narrative,
            "overall_confidence": confidence,
            "opinion_eligible": opinion,
        },
    }


def make_intelligence(rows, q=None, p=None):
    p = p or plan()
    q = q or quality()
    result = compute_intelligence(rows, p, q)
    return result["records"], result["summary"], result["time_series"]


def stable_rows():
    rows = []
    i = 1
    for day in range(1, 15):
        rows.append(row(i, day=day, sentiment=0.05, label="neutral", narrative="Routine discussion", emotion="neutral")); i += 1
        rows.append(row(i, day=day, sentiment=0.05, label="neutral", narrative="Routine discussion", emotion="neutral", platform="news", origin="earned_media", opinion=False, stance="not_applicable")); i += 1
    return rows


def anomaly_rows():
    rows = []
    i = 1
    for day in range(1, 8):
        n = 3 if day < 7 else 25
        for j in range(n):
            if day == 7:
                rows.append(row(i, day=day, sentiment=-0.85, label="negative", emotion="anger", narrative="Draw integrity concern", topic="Trust", platform="x" if j % 2 == 0 else "tiktok", stance="critical", impact=500 + j * 10))
            else:
                rows.append(row(i, day=day, sentiment=0.15, label="positive", emotion="joy", narrative="Winning dream", topic="Aspirational", platform="x" if j % 2 == 0 else "news", origin="earned_person" if j % 2 == 0 else "earned_media", opinion=j % 2 == 0, stance="supportive" if j % 2 == 0 else "not_applicable", impact=100 + j))
            i += 1
    return rows


class InvestigationTests(unittest.TestCase):
    def test_indicator_contract_has_unique_ids(self):
        ids = [x[0] for x in INDICATOR_REGISTRY]
        self.assertEqual(len(ids), 25)
        self.assertEqual(len(ids), len(set(ids)))

    def test_presentation_critical_volume_market_and_top_mentions_are_explicit(self):
        records, summary, time = make_intelligence(stable_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        by_id = {x["indicator_id"]: x for x in result["indicator_inventory"]}
        self.assertTrue(by_id["sample_volume"]["examined"])
        self.assertTrue(by_id["market_relevance"]["examined"])
        self.assertTrue(by_id["top_mentions"]["examined"])
        self.assertEqual(by_id["sample_volume"]["value"]["analysis_ready_records"], len(records))
        self.assertEqual(by_id["market_relevance"]["value"]["market"], "Greece")
        self.assertGreater(by_id["market_relevance"]["value"]["high_relevance_share"], 0.9)
        self.assertTrue(by_id["top_mentions"]["value"])
        self.assertTrue(all(x.get("record_id") for x in by_id["top_mentions"]["value"]))
        headline = result["evidence_pack"]["headline_metrics"]
        self.assertIn("sample_volume", headline)
        self.assertIn("market_relevance", headline)
        self.assertIn("top_mentions", headline)

    def test_every_indicator_is_examined_even_when_not_interesting(self):
        records, summary, time = make_intelligence(stable_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        inventory = result["indicator_inventory"]
        self.assertEqual(len(inventory), len(INDICATOR_REGISTRY))
        self.assertTrue(all(x["examined"] is True for x in inventory))
        self.assertTrue(result["summary"]["indicator_contract"]["complete"])
        self.assertEqual(result["summary"]["indicator_contract"]["omitted"], 0)

    def test_evidence_pack_requires_all_indicator_review_for_step8(self):
        records, summary, time = make_intelligence(stable_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        guard = result["evidence_pack"]["presentation_guardrails"]
        self.assertTrue(guard["must_review_all_indicators"])
        self.assertTrue(guard["may_omit_uninteresting_slide"])
        self.assertTrue(guard["omitting_slide_does_not_mean_omitting_indicator_review"])

    def test_no_proven_causality_is_emitted(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(result["investigations"])
        self.assertTrue(all(x["causal_status"] == "not_proven" for x in result["investigations"]))
        joined = json.dumps(result["investigations"], ensure_ascii=False).lower()
        self.assertNotIn("caused by", joined)
        self.assertNotIn("proven cause", joined)

    def test_changed_plan_context_is_rejected_instead_of_mixing_research_frames(self):
        records, summary, time = make_intelligence(stable_rows())
        changed = plan()
        changed["date_from"] = "2026-08-02"
        with self.assertRaises(RuntimeError):
            compute_investigations(records, summary, time, changed, quality())

    def test_input_evidence_is_never_mutated(self):
        records, summary, time = make_intelligence(anomaly_rows())
        original = copy.deepcopy(records)
        compute_investigations(records, summary, time, plan(), quality())
        self.assertEqual(records, original)

    def test_duplicate_ids_stop_investigation(self):
        records, summary, time = make_intelligence(stable_rows())
        dup = copy.deepcopy(records)
        dup.append(copy.deepcopy(dup[0]))
        with self.assertRaises(RuntimeError):
            compute_investigations(dup, summary, time, plan(), quality())

    def test_missing_id_stops_investigation(self):
        records, summary, time = make_intelligence(stable_rows())
        records[0]["id"] = ""
        with self.assertRaises(RuntimeError):
            compute_investigations(records, summary, time, plan(), quality())

    def test_stale_step5_is_rejected(self):
        records, summary, time = make_intelligence(stable_rows())
        summary = {**summary, "stale": True}
        with self.assertRaises(RuntimeError):
            compute_investigations(records, summary, time, plan(), quality())

    def test_numeric_anomaly_triggers_investigation(self):
        records, summary, time = make_intelligence(anomaly_rows())
        self.assertTrue(time["anomalies"])
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = [x for x in result["investigations"] if x["type"] == "numeric_anomaly"]
        self.assertTrue(inv)
        self.assertIn("negative_share_spike", inv[0]["trigger"]["flags"])
        self.assertGreater(len(inv[0]["evidence_record_ids"]), 0)

    def test_anomaly_investigation_surfaces_narrative_shift(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "numeric_anomaly")
        names = [x["name"] for x in inv["drivers"] if x["dimension"] == "narrative"]
        self.assertIn("Draw integrity concern", names)

    def test_daily_reputation_change_is_detected(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(any(x["type"] == "reputation_daily_change" for x in result["investigations"]))

    def test_period_shift_is_detected(self):
        rows = []
        for i in range(1, 15):
            if i <= 7:
                rows.append(row(i, day=i, sentiment=0.6, label="positive", emotion="joy", narrative="Winning dream", stance="supportive"))
            else:
                rows.append(row(i, day=i, sentiment=-0.7, label="negative", emotion="anger", narrative="Price concern", stance="critical"))
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "period_shift")
        self.assertLess(inv["metrics"]["deltas"]["brand_reputation_points"], 0)

    def test_negative_driver_uses_exact_step5_contribution(self):
        rows = [
            row(1, 2, -0.8, "negative", "anger", "Rigging concern", "Trust", stance="critical"),
            row(2, 3, -0.7, "negative", "anger", "Rigging concern", "Trust", stance="critical"),
            row(3, 4, 0.2, "positive", "joy", "Winning dream", "Aspirational", stance="supportive"),
        ]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "negative_narrative_driver")
        expected = summary["top_negative_narrative_drivers"][0]["reputation_point_contribution"]
        self.assertEqual(inv["metrics"]["driver"]["reputation_point_contribution"], expected)
        self.assertEqual(inv["conclusion_type"], "deterministic_contribution")

    def test_positive_driver_is_available(self):
        rows = [row(i, day=i, sentiment=0.8, label="positive", emotion="joy", narrative="Winning dream", stance="supportive") for i in range(1, 6)]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(any(x["type"] == "positive_narrative_driver" for x in result["investigations"]))

    def test_emotion_profile_investigates_high_anger(self):
        rows = [row(i, day=(i % 10) + 1, sentiment=-0.5, label="negative", emotion="anger", narrative="Price concern", stance="critical") for i in range(1, 12)]
        rows += [row(100+i, day=(i % 10) + 1, sentiment=0.1, label="neutral", emotion="joy", narrative="Routine", stance="neutral") for i in range(3)]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "emotion_profile")
        self.assertEqual(inv["trigger"]["emotion"], "anger")
        self.assertGreater(inv["trigger"]["weighted_percent"], 25)

    def test_source_divergence_requires_material_gap(self):
        rows = []
        for i in range(1, 5):
            rows.append(row(i, day=i, sentiment=-0.8, label="negative", platform="x", narrative="Trust", stance="critical"))
            rows.append(row(100+i, day=i, sentiment=0.8, label="positive", platform="tiktok", narrative="Dream", stance="supportive"))
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "source_divergence")
        self.assertGreaterEqual(inv["trigger"]["gap_points"], 20)
        self.assertEqual(inv["conclusion_type"], "descriptive_difference")

    def test_media_people_divergence_is_detected(self):
        rows = []
        for i in range(1, 5):
            rows.append(row(i, day=i, sentiment=-0.7, label="negative", origin="earned_media", opinion=True, narrative="Media concern", stance="critical"))
            rows.append(row(100+i, day=i, sentiment=0.7, label="positive", origin="earned_person", opinion=True, narrative="Public optimism", stance="supportive"))
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(any(x["type"] == "media_people_divergence" for x in result["investigations"]))

    def test_coordination_signal_does_not_prove_bot_or_malicious_intent(self):
        rows = [row(i, day=5, sentiment=-0.7, label="negative", emotion="anger", narrative="Same message", stance="critical", coordination="coord-1", authenticity=45, author=f"u{i}") for i in range(1, 8)]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "coordination_signal")
        self.assertEqual(inv["conclusion_type"], "risk_signal")
        self.assertIn("not proven", inv["finding"]["en"].lower())
        self.assertNotIn("is a bot", inv["finding"]["en"].lower())

    def test_story_syndication_is_examined_without_becoming_coordination(self):
        rows = [row(i, day=5, sentiment=0.0, label="neutral", origin="earned_media", opinion=False, stance="not_applicable", story="story-1", author=f"media{i}") for i in range(1, 6)]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        story_item = next(x for x in result["indicator_inventory"] if x["indicator_id"] == "story_syndication")
        coord_item = next(x for x in result["indicator_inventory"] if x["indicator_id"] == "coordination")
        self.assertTrue(story_item["available"])
        self.assertFalse(coord_item["available"])
        self.assertFalse(any(x["type"] == "coordination_signal" for x in result["investigations"]))

    def test_quality_guardrail_triggers_on_low_coverage(self):
        records, summary, time = make_intelligence(stable_rows(), q=quality(80, 0.5, 0.9))
        result = compute_investigations(records, summary, time, plan(), quality(80, 0.5, 0.9))
        inv = next(x for x in result["investigations"] if x["type"] == "data_quality_guardrail")
        self.assertEqual(inv["conclusion_type"], "quality_guardrail")
        self.assertIn("source coverage", inv["finding"]["en"].lower())

    def test_emerging_narrative_detected_by_share_acceleration(self):
        rows = []
        i = 1
        for day in range(1, 8):
            for _ in range(4):
                rows.append(row(i, day=day, sentiment=0.1, label="neutral", narrative="Routine", stance="neutral")); i += 1
        for day in range(8, 15):
            for j in range(4):
                narrative = "Price backlash" if j < 3 else "Routine"
                sent = -0.6 if narrative == "Price backlash" else 0.1
                label = "negative" if sent < 0 else "neutral"
                rows.append(row(i, day=day, sentiment=sent, label=label, emotion="anger" if sent < 0 else "neutral", narrative=narrative, stance="critical" if sent < 0 else "neutral")); i += 1
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        inv = next(x for x in result["investigations"] if x["type"] == "emerging_narrative")
        self.assertEqual(inv["trigger"]["narrative"], "Price backlash")
        self.assertGreater(inv["trigger"]["share_delta"], 0.12)

    def test_stable_neutral_data_can_have_zero_investigations_but_complete_inventory(self):
        rows = [row(i, day=((i-1) % 14)+1, sentiment=0.0, label="neutral", emotion="neutral", narrative="Routine", stance="neutral") for i in range(1, 57)]
        records, summary, time = make_intelligence(rows)
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(result["summary"]["indicator_contract"]["complete"])
        # Driver contribution is zero and there are no material changes/anomalies.
        self.assertEqual(result["summary"]["high_priority_count"], 0)

    def test_priority_and_confidence_are_bounded(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        for inv in result["investigations"]:
            self.assertGreaterEqual(inv["priority_score"], 0)
            self.assertLessEqual(inv["priority_score"], 100)
            self.assertGreaterEqual(inv["confidence"]["score"], 0)
            self.assertLessEqual(inv["confidence"]["score"], 100)

    def test_evidence_records_are_traceable(self):
        records, summary, time = make_intelligence(anomaly_rows())
        valid = {r["id"] for r in records}
        result = compute_investigations(records, summary, time, plan(), quality())
        for inv in result["investigations"]:
            self.assertTrue(set(inv["evidence_record_ids"]).issubset(valid))
            for ev in inv["evidence"]:
                self.assertIn(ev["record_id"], valid)
                self.assertTrue(ev["url"].startswith("https://"))

    def test_presentation_candidates_require_nonlimited_evidence(self):
        rows = [row(1, day=1, sentiment=-1, label="negative", emotion="anger", narrative="Single complaint", stance="critical", confidence=0.2)]
        records, summary, time = make_intelligence(rows, q=quality(30, 0.2, 0.2))
        result = compute_investigations(records, summary, time, plan(), quality(30, 0.2, 0.2))
        for inv in result["investigations"]:
            if inv["evidence_status"] == "limited_evidence":
                self.assertFalse(inv["presentation"]["candidate"])

    def test_recommended_visual_exists_for_every_investigation(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertTrue(all((x.get("presentation") or {}).get("recommended_visual") for x in result["investigations"]))

    def test_headline_metrics_preserve_all_core_step5_outputs(self):
        records, summary, time = make_intelligence(anomaly_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        h = result["evidence_pack"]["headline_metrics"]
        for key in ["brand_reputation", "evidence_confidence", "sentiment", "emotions", "stance", "origin_breakdown", "source_breakdown", "positive_narrative_drivers", "negative_narrative_drivers", "topic_drivers", "media_influence", "people_influence", "time_series"]:
            self.assertIn(key, h)

    def test_output_is_deterministic_except_timestamps(self):
        rows = anomaly_rows()
        records, summary, time = make_intelligence(rows)
        a = compute_investigations(records, summary, time, plan(), quality())
        b = compute_investigations(list(reversed(records)), summary, time, plan(), quality())
        def signature(result):
            return [(x["type"], x["trigger"], x["priority_score"], x["evidence_record_ids"]) for x in result["investigations"]]
        self.assertEqual(signature(a), signature(b))

    def test_methodology_versions_are_explicit(self):
        records, summary, time = make_intelligence(stable_rows())
        result = compute_investigations(records, summary, time, plan(), quality())
        self.assertEqual(result["summary"]["ruleset_version"], INVESTIGATION_RULESET_VERSION)
        self.assertEqual(result["summary"]["evidence_contract_version"], PRESENTATION_EVIDENCE_CONTRACT_VERSION)
        self.assertIn("causality", result["methodology"])


class InvestigationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = None
        from app.config import settings
        self.settings = settings
        self.old_root = settings.signalyth_data_dir
        settings.signalyth_data_dir = self.tmp.name
        self.store = RunStore()
        self.folder = Path(self.tmp.name) / "runs" / "manual-run"
        self.folder.mkdir(parents=True)
        self.store.write(self.folder / "plan.json", plan())
        self.store.write(self.folder / "status.json", {"run_id":"manual-run"})
        self.store.write(self.folder / "control.json", {"cancel_requested":False})

    def tearDown(self):
        self.settings.signalyth_data_dir = self.old_root
        self.tmp.cleanup()

    def seed(self, rows=None, q=None):
        rows = rows or anomaly_rows()
        q = q or quality()
        result = compute_intelligence(rows, plan(), q)
        self.store.write(self.folder / "intelligence" / "records.json", result["records"])
        self.store.write(self.folder / "intelligence" / "summary.json", result["summary"])
        self.store.write(self.folder / "intelligence" / "time-series.json", result["time_series"])
        self.store.write(self.folder / "cleaning" / "report.json", q)
        return result

    def test_build_persists_all_step6_files(self):
        self.seed()
        summary = build_investigations(self.folder, plan())
        self.assertEqual(summary["ruleset_version"], "1.1.0")
        for name in ["summary.json", "indicator-inventory.json", "investigations.json", "evidence-pack.json", "audit.json", "methodology.json"]:
            self.assertTrue((self.folder / "investigations" / name).exists(), name)

    def test_build_is_idempotent_when_input_unchanged(self):
        self.seed()
        a = build_investigations(self.folder, plan())
        b = build_investigations(self.folder, plan())
        self.assertEqual(a, b)

    def test_load_marks_stale_when_step5_changes(self):
        self.seed()
        build_investigations(self.folder, plan())
        summary = self.store.read(self.folder / "intelligence" / "summary.json")
        summary["input_hash"] = "changed"
        self.store.write(self.folder / "intelligence" / "summary.json", summary)
        loaded = load_investigation_summary(self.folder)
        self.assertTrue(loaded["stale"])

    def test_evidence_pack_loads(self):
        self.seed()
        build_investigations(self.folder, plan())
        pack = load_evidence_pack(self.folder)
        self.assertEqual(pack["contract_version"], "signalyth-evidence-pack-v1.1")
        self.assertEqual(len(pack["indicator_inventory"]), 25)

    def test_missing_step5_files_fail_safely(self):
        with self.assertRaises(RuntimeError):
            build_investigations(self.folder, plan())

    def test_cancel_before_processing_does_not_persist_outputs(self):
        self.seed()
        with self.assertRaises(InvestigationCancelled):
            build_investigations(self.folder, plan(), cancel_check=lambda: True)
        self.assertFalse((self.folder / "investigations" / "summary.json").exists())

    def test_load_marks_stale_when_evidence_text_changes(self):
        self.seed()
        build_investigations(self.folder, plan())
        records = self.store.read(self.folder / "intelligence" / "records.json")
        records[0]["text"] = records[0].get("text", "") + " changed evidence"
        self.store.write(self.folder / "intelligence" / "records.json", records)
        loaded = load_investigation_summary(self.folder)
        self.assertTrue(loaded["stale"])

    def test_load_marks_stale_when_market_score_changes(self):
        self.seed()
        build_investigations(self.folder, plan())
        records = self.store.read(self.folder / "intelligence" / "records.json")
        records[0].setdefault("cleaning", {})["market_score"] = 0.01
        self.store.write(self.folder / "intelligence" / "records.json", records)
        loaded = load_investigation_summary(self.folder)
        self.assertTrue(loaded["stale"])

    def test_load_marks_stale_when_research_context_changes(self):
        self.seed()
        build_investigations(self.folder, plan())
        summary = self.store.read(self.folder / "intelligence" / "summary.json")
        summary.setdefault("research_context", {})["market"] = "Italy"
        self.store.write(self.folder / "intelligence" / "summary.json", summary)
        loaded = load_investigation_summary(self.folder)
        self.assertTrue(loaded["stale"])

    def test_force_rebuild_keeps_same_input_hash(self):
        self.seed()
        a = build_investigations(self.folder, plan())
        b = build_investigations(self.folder, plan(), force=True)
        self.assertEqual(a["input_hash"], b["input_hash"])


if __name__ == "__main__":
    unittest.main()
