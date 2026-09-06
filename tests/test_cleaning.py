import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from app.services.cleaning import (
    CLEANING_RULESET_VERSION,
    CleaningCancelled,
    apply_review_decision,
    clean_records,
    clean_run,
)


def plan(topic="Eurojackpot", client="OPAP", exclusions=None, target=20, sources=None):
    return {
        "client": client,
        "topic": topic,
        "market": "Greece",
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
        "target_total": target,
        "core_terms": [topic],
        "context_terms": [client, "Greece", "Ελλάδα", "Ellada"],
        "greeklish_variants": ["opap", "ellada"],
        "exclusions": list(exclusions or []),
        "sources": sources or [{"source": "x"}, {"source": "news"}],
    }


def row(i, text, *, author="user", platform="x", date="2026-08-15T10:00:00+00:00", url=None, followers=10,
        views=0, likes=0, comments=0, shares=0, raw_data=None):
    return {
        "id": str(i), "platform": platform, "text": text, "date": date, "author": author,
        "followers": followers, "views": views, "likes": likes, "comments": comments, "shares": shares,
        "url": url or f"https://example.test/{platform}/{i}", "content_type": "post", "parent_post": None,
        "raw_data": raw_data or {},
    }


class CleaningTests(unittest.TestCase):
    def test_ruleset_is_versioned(self):
        result = clean_records([row(1, "Eurojackpot Ελλάδα ΟΠΑΠ")], plan(target=1))
        self.assertEqual(result["cleaned"][0]["cleaning"]["ruleset_version"], CLEANING_RULESET_VERSION)
        self.assertEqual(result["report"]["ruleset_version"], CLEANING_RULESET_VERSION)

    def test_raw_input_is_never_mutated(self):
        records = [row(1, "Eurojackpot Ελλάδα ΟΠΑΠ")]
        before = copy.deepcopy(records)
        clean_records(records, plan(target=1))
        self.assertEqual(records, before)

    def test_greek_relevant_post_is_trusted(self):
        result = clean_records([row(1, "Κέρδισα στο Eurojackpot χθες μέσω ΟΠΑΠ!")], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["decision"], "trusted")
        self.assertGreaterEqual(c["market_score"], 0.6)
        self.assertGreaterEqual(c["relevance_score"], 0.6)

    def test_greeklish_relevant_post_is_recognized(self):
        result = clean_records([row(1, "kerdisa sto Eurojackpot xthes apo opap kai den to pistevw")], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["decision"], "trusted")
        self.assertIn("greeklish_pattern", c["reasons"])

    def test_english_post_about_greece_is_not_rejected(self):
        result = clean_records([row(1, "Eurojackpot winner in Greece claims the jackpot")], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertNotEqual(c["decision"], "excluded")
        self.assertGreater(c["market_score"], 0.2)

    def test_known_irrelevant_context_is_excluded(self):
        result = clean_records([row(1, "Eurojackpot KNVB Beker football final")], plan(exclusions=["KNVB"], target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["decision"], "excluded")
        self.assertIn("explicit_exclusion_context", c["flags"])

    def test_missing_core_term_is_not_forced_relevant(self):
        result = clean_records([row(1, "OPAP coffee shop Greece special offer")], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertIn("core_term_missing", c["flags"])
        self.assertNotEqual(c["decision"], "trusted")

    def test_ambiguous_short_entity_requires_market_context(self):
        p = plan(topic="OPAP", client="OPAP", target=1)
        p["core_terms"] = ["OPAP"]
        p["context_terms"] = ["Greece", "Ελλάδα"]
        result = clean_records([row(1, "OPAP annual conference on orofacial pain")], p)
        c = result["cleaned"][0]["cleaning"]
        self.assertIn("ambiguous_short_entity", c["flags"])
        self.assertNotEqual(c["decision"], "trusted")

    def test_exact_same_url_becomes_duplicate(self):
        records = [
            row(1, "Eurojackpot Ελλάδα ΟΠΑΠ", url="https://same/1"),
            row(2, "Eurojackpot Ελλάδα ΟΠΑΠ", url="https://same/1"),
        ]
        result = clean_records(records, plan(target=2))
        self.assertEqual(result["cleaned"][0]["cleaning"]["decision"], "trusted")
        self.assertEqual(result["cleaned"][1]["cleaning"]["decision"], "excluded")
        self.assertEqual(result["report"]["exact_duplicates"], 1)

    def test_same_author_near_duplicate_is_not_double_counted(self):
        records = [
            row(1, "Eurojackpot Ελλάδα ΟΠΑΠ μεγάλο jackpot σήμερα", author="same"),
            row(2, "Eurojackpot Ελλάδα ΟΠΑΠ μεγάλο jackpot σήμερα τώρα", author="same"),
        ]
        result = clean_records(records, plan(target=2))
        flags = result["cleaned"][1]["cleaning"]["flags"]
        self.assertIn("near_duplicate_same_author", flags)
        self.assertEqual(result["cleaned"][1]["cleaning"]["decision"], "excluded")

    def test_cross_author_same_message_is_coordination_not_exact_duplicate(self):
        records = [row(i, "Eurojackpot OPAP Greece same campaign message now", author=f"u{i}", date=f"2026-08-15T10:0{i}:00+00:00") for i in range(4)]
        result = clean_records(records, plan(target=4))
        self.assertTrue(all(r["cleaning"]["coordination_cluster_id"] for r in result["cleaned"]))
        self.assertTrue(all("exact_duplicate" not in r["cleaning"]["flags"] for r in result["cleaned"]))
        self.assertTrue(all(r["cleaning"]["decision"] == "review" for r in result["cleaned"]))

    def test_syndicated_news_forms_story_cluster_not_bot_cluster(self):
        records = [
            row(1, "Eurojackpot Greece jackpot winner receives record prize", author="Alpha News", platform="news"),
            row(2, "Eurojackpot Greece jackpot winner receives record prize today", author="Beta News", platform="news"),
            row(3, "Eurojackpot Greece jackpot winner receives record prize in Athens", author="Gamma News", platform="news"),
        ]
        result = clean_records(records, plan(target=3))
        self.assertTrue(any(r["cleaning"]["story_cluster_id"] for r in result["cleaned"]))
        self.assertTrue(all(r["cleaning"]["coordination_cluster_id"] is None for r in result["cleaned"]))
        self.assertTrue(all(r["cleaning"]["authenticity_status"] == "low_risk" for r in result["cleaned"]))

    def test_promo_is_retained_but_not_organic_opinion(self):
        record = row(1, "Eurojackpot Greece register now special offer price 5 euro")
        result = clean_records([record], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["content_class"], "promotional")
        self.assertFalse(c["organic_eligible"])
        self.assertNotEqual(c["decision"], "excluded")

    def test_news_is_trusted_but_not_organic_opinion(self):
        record = row(1, "Eurojackpot winner in Greece announced today", platform="news", author="News Desk")
        result = clean_records([record], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["account_type"], "media")
        self.assertEqual(c["content_class"], "news")
        self.assertFalse(c["organic_eligible"])

    def test_brand_owned_is_separated_from_person_opinion(self):
        record = row(1, "Eurojackpot Ελλάδα νέα κλήρωση", author="OPAP")
        result = clean_records([record], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["account_type"], "brand_owned")
        self.assertEqual(c["content_class"], "owned")
        self.assertFalse(c["organic_eligible"])

    def test_low_follower_user_is_not_called_bot(self):
        record = row(1, "Eurojackpot Ελλάδα ΟΠΑΠ μου αρέσει πολύ", followers=0, raw_data={"followingCount": 3})
        result = clean_records([record], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertEqual(c["authenticity_status"], "low_risk")
        self.assertEqual(c["bot_risk_score"], 0.0)

    def test_extreme_following_ratio_alone_does_not_exclude_user(self):
        record = row(1, "Eurojackpot Ελλάδα ΟΠΑΠ γνώμη χρήστη", followers=1, raw_data={"followingCount": 3000, "followerCount": 1})
        result = clean_records([record], plan(target=1))
        c = result["cleaned"][0]["cleaning"]
        self.assertNotEqual(c["decision"], "excluded")
        self.assertLess(c["bot_risk_score"], 0.45)

    def test_spam_and_coordination_can_trigger_likely_automated(self):
        text = "Eurojackpot OPAP Greece guaranteed win whatsapp +30 210 1234567 click link #a #b #c #d #e #f #g #h #i #j"
        records = [row(i, text, author=f"spam{i}", date=f"2026-08-15T10:0{i}:00+00:00") for i in range(6)]
        result = clean_records(records, plan(target=6))
        self.assertTrue(any(r["cleaning"]["authenticity_status"] == "likely_automated" for r in result["cleaned"]))
        self.assertTrue(any(r["cleaning"]["decision"] == "excluded" for r in result["cleaned"]))

    def test_high_impact_suspicious_activity_goes_to_review(self):
        text = "Eurojackpot OPAP Greece same claim share it now"
        records = [row(i, text, author=f"u{i}", date=f"2026-08-15T10:0{i}:00+00:00", views=100000 if i == 0 else 0) for i in range(4)]
        result = clean_records(records, plan(target=4))
        first = result["cleaned"][0]["cleaning"]
        self.assertEqual(first["decision"], "review")
        self.assertIn("high_impact_suspicious_activity", first["reasons"])

    def test_review_queue_contains_only_review_records(self):
        records = [
            row(1, "Eurojackpot Ελλάδα ΟΠΑΠ"),
            row(2, "Eurojackpot generic mention"),
            row(3, "Eurojackpot KNVB final"),
        ]
        result = clean_records(records, plan(exclusions=["KNVB"], target=3))
        self.assertTrue(result["review_queue"])
        self.assertTrue(all(r["cleaning"]["decision"] == "review" for r in result["review_queue"]))

    def test_decision_partitions_are_exact(self):
        records = [row(i, f"Eurojackpot Ελλάδα ΟΠΑΠ mention {i}") for i in range(7)]
        result = clean_records(records, plan(target=7))
        self.assertEqual(len(result["cleaned"]), len(result["trusted"]) + len(result["review_queue"]) + len(result["excluded"]))

    def test_quality_report_has_non_representativeness_note(self):
        result = clean_records([row(1, "Eurojackpot Ελλάδα ΟΠΑΠ")], plan(target=1))
        self.assertIn("not a claim of statistical representativeness", result["report"]["note"])

    def test_quality_report_counts_coordination_clusters(self):
        records = [row(i, "Eurojackpot OPAP Greece same message for everyone", author=f"u{i}", date=f"2026-08-15T10:0{i}:00+00:00") for i in range(4)]
        result = clean_records(records, plan(target=4))
        self.assertGreaterEqual(result["report"]["coordination_clusters"], 1)

    def test_clean_run_persists_separate_evidence_layers(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=2)
            source = [row(1, "Eurojackpot Ελλάδα ΟΠΑΠ"), row(2, "Eurojackpot KNVB", author="u2")]
            (folder / "plan.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            report = clean_run(folder, plan=p)
            self.assertEqual(report["total_records"], 2)
            for name in ("cleaned.json", "trusted.json", "organic.json", "review-queue.json", "excluded.json", "audit.json", "report.json"):
                self.assertTrue((folder / "cleaning" / name).exists(), name)
            original = json.loads((folder / "normalized-all.json").read_text(encoding="utf-8"))
            self.assertEqual(original, source)

    def test_human_review_keep_is_persisted_and_recomputes_report(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot generic mention")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            out = apply_review_decision(folder, "1", "keep", "Human verified Greek-market context")
            self.assertEqual(out["record"]["cleaning"]["decision"], "trusted")
            self.assertEqual(out["record"]["cleaning"]["human_override"]["action"], "keep")
            trusted = json.loads((folder / "cleaning" / "trusted.json").read_text(encoding="utf-8"))
            self.assertEqual(len(trusted), 1)

    def test_human_review_exclude_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot generic mention")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            out = apply_review_decision(folder, "1", "exclude", "Human confirmed irrelevant")
            self.assertEqual(out["record"]["cleaning"]["decision"], "excluded")
            excluded = json.loads((folder / "cleaning" / "excluded.json").read_text(encoding="utf-8"))
            self.assertEqual(len(excluded), 1)

    def test_human_review_can_change_content_classification(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot generic mention")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            out = apply_review_decision(folder, "1", "keep", account_type="media", content_class="news")
            self.assertEqual(out["record"]["cleaning"]["account_type"], "media")
            self.assertEqual(out["record"]["cleaning"]["content_class"], "news")
            self.assertFalse(out["record"]["cleaning"]["organic_eligible"])

    def test_review_unknown_record_fails_without_damage(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot generic mention")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            before = (folder / "cleaning" / "cleaned.json").read_text(encoding="utf-8")
            with self.assertRaises(KeyError):
                apply_review_decision(folder, "missing", "exclude")
            self.assertEqual((folder / "cleaning" / "cleaned.json").read_text(encoding="utf-8"), before)

    def test_story_cluster_weights_sum_to_one_independent_evidence_unit(self):
        records = [
            row(1, "Eurojackpot Greece jackpot winner receives record prize", author="Alpha News", platform="news"),
            row(2, "Eurojackpot Greece jackpot winner receives record prize today", author="Beta News", platform="news"),
            row(3, "Eurojackpot Greece jackpot winner receives record prize in Athens", author="Gamma News", platform="news"),
        ]
        result = clean_records(records, plan(target=3))
        clustered = [r for r in result["cleaned"] if r["cleaning"]["story_cluster_id"]]
        self.assertGreaterEqual(len(clustered), 2)
        by_cluster = {}
        for r in clustered:
            by_cluster.setdefault(r["cleaning"]["story_cluster_id"], 0.0)
            by_cluster[r["cleaning"]["story_cluster_id"]] += r["cleaning"]["independent_voice_weight"]
        for total in by_cluster.values():
            self.assertAlmostEqual(total, 1.0, places=5)

    def test_origin_class_explicitly_separates_owned_media_and_person(self):
        records = [
            row(1, "Eurojackpot Ελλάδα νέα κλήρωση", author="OPAP"),
            row(2, "Eurojackpot winner in Greece", author="Alpha News", platform="news"),
            row(3, "Eurojackpot Ελλάδα ΟΠΑΠ γνώμη μου", author="maria"),
        ]
        result = clean_records(records, plan(target=3))
        origins = {r['id']: r['cleaning']['origin_class'] for r in result['cleaned']}
        self.assertEqual(origins['1'], 'owned')
        self.assertEqual(origins['2'], 'earned_media')
        self.assertEqual(origins['3'], 'earned_person')

    def test_source_breakdown_never_claims_platform_completeness(self):
        p = plan(target=2, sources=[{"source":"x","target_items":1},{"source":"news","target_items":1}])
        result = clean_records([row(1, "Eurojackpot Ελλάδα ΟΠΑΠ", platform="x")], p)
        self.assertEqual(result['report']['source_breakdown']['x']['platform_completeness'], 'not_claimed')
        self.assertEqual(result['report']['source_breakdown']['news']['collection_target_status'], 'no_data')

    def test_ruleset_snapshot_is_persisted_for_reproducibility(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot Ελλάδα ΟΠΑΠ")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            ruleset = json.loads((folder / "cleaning" / "ruleset.json").read_text(encoding="utf-8"))
            self.assertEqual(ruleset['version'], CLEANING_RULESET_VERSION)
            self.assertIn('bot_likely_automated_at', ruleset['config'])
            self.assertIn('Conservative evidence scoring', ruleset['config']['principle'])

    def test_multiple_human_reviews_preserve_history(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            p = plan(target=1)
            records = [row(1, "Eurojackpot generic mention")]
            (folder / "plan.json").write_text(json.dumps(p), encoding="utf-8")
            (folder / "normalized-all.json").write_text(json.dumps(records), encoding="utf-8")
            clean_run(folder, plan=p)
            apply_review_decision(folder, "1", "keep", "first")
            out = apply_review_decision(folder, "1", "exclude", "second")
            history = out['record']['cleaning']['human_review_history']
            self.assertEqual([x['action'] for x in history], ['keep', 'exclude'])
            self.assertEqual(history[-1]['note'], 'second')

    def test_cleaning_can_cancel_without_partial_clean_files(self):
        records = [row(i, f"Eurojackpot Ελλάδα ΟΠΑΠ {i}") for i in range(500)]
        calls = {"n": 0}
        def cancel():
            calls["n"] += 1
            return calls["n"] >= 2
        with self.assertRaises(CleaningCancelled):
            clean_records(records, plan(target=500), cancel_check=cancel)

    def test_deterministic_same_input_same_decisions_and_clusters(self):
        records = [row(i, "Eurojackpot OPAP Greece coordinated line", author=f"u{i}", date=f"2026-08-15T10:0{i}:00+00:00") for i in range(5)]
        a = clean_records(records, plan(target=5))
        b = clean_records(records, plan(target=5))
        def sig(result):
            return [(r["id"], r["cleaning"]["decision"], r["cleaning"]["story_cluster_id"], r["cleaning"]["coordination_cluster_id"]) for r in result["cleaned"]]
        self.assertEqual(sig(a), sig(b))

    def test_randomized_mixed_noise_invariants(self):
        rng = random.Random(8042026)
        vocab = ["Eurojackpot", "ΟΠΑΠ", "Greece", "Ελλάδα", "KNVB", "football", "kerdisa", "sto", "xthes", "news", "sale", "whatsapp", "hello", "κόσμος"]
        for batch in range(25):
            records = []
            for i in range(rng.randint(5, 45)):
                words = rng.sample(vocab, rng.randint(1, min(7, len(vocab))))
                records.append(row(f"{batch}-{i}", " ".join(words), author=f"u{rng.randint(1,12)}", platform=rng.choice(["x","facebook","instagram","news"]), views=rng.randint(0,50000)))
            before = copy.deepcopy(records)
            result = clean_records(records, plan(exclusions=["KNVB"], target=len(records), sources=[{"source":"x"},{"source":"facebook"},{"source":"instagram"},{"source":"news"}]))
            self.assertEqual(records, before)
            self.assertEqual(len(result['cleaned']), len(records))
            self.assertEqual(len(result['cleaned']), len(result['trusted']) + len(result['review_queue']) + len(result['excluded']))
            self.assertTrue(all(r['cleaning']['decision'] in {'trusted','review','excluded'} for r in result['cleaned']))
            self.assertTrue(0 <= result['report']['data_quality_score'] <= 100)

    def test_large_3000_record_batch_completes_and_preserves_count(self):
        records = [row(i, f"Eurojackpot Ελλάδα ΟΠΑΠ unique mention number {i} analysis data") for i in range(3000)]
        result = clean_records(records, plan(target=3000, sources=[{"source": "x"}]))
        self.assertEqual(result["report"]["total_records"], 3000)
        self.assertEqual(len(result["cleaned"]), 3000)


if __name__ == "__main__":
    unittest.main()
