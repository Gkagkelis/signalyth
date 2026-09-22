"""Pending records are evidence the operator paid for and did not get.

A record sitting in the review queue does not count towards Brand Reputation
(`_reputation_eligible` requires decision == "ready"). A queue of 293 is 293
excluded pieces of evidence — and the queue lived in a separate screen, so it
was easy to publish a report without ever seeing it.

These tests pin the triage that makes the queue actionable: rank by how much
each record could actually move the published score, so a handful are worth a
human's attention and the rest are visibly volume.
"""
from __future__ import annotations

import pytest

from app.services.review_triage import (
    MATERIAL_POINT_THRESHOLD,
    estimated_point_movement,
    potential_weight,
    triage_review_queue,
)


def _rec(rid, *, independent=1.0, authenticity=100, confidence=0.8,
         impact=0.5, sentiment=0.0):
    return {
        "id": rid,
        "text": f"record {rid}",
        "cleaning": {
            "independent_voice_weight": independent,
            "authenticity_score": authenticity,
            "confidence": confidence,
        },
        "ai_analysis": {
            "overall_confidence": confidence,
            "sentiment_confidence": confidence,
            "relevance_confidence": confidence,
            "impact_score": impact,
            "sentiment_score": sentiment,
        },
    }


class TestPotentialWeight:
    def test_reach_and_authenticity_raise_the_weight(self):
        loud = _rec("loud", impact=0.95, authenticity=100)
        quiet = _rec("quiet", impact=0.05, authenticity=100)
        assert potential_weight(loud) > potential_weight(quiet)

    def test_a_suspicious_account_weighs_almost_nothing(self):
        genuine = _rec("genuine", authenticity=100)
        bot = _rec("bot", authenticity=5)
        assert potential_weight(bot) < potential_weight(genuine) * 0.2

    def test_a_record_in_a_duplicate_cluster_is_discounted(self):
        single = _rec("single", independent=1.0)
        one_of_ten = _rec("clustered", independent=0.1)
        assert potential_weight(one_of_ten) == pytest.approx(potential_weight(single) * 0.1)


class TestMovement:
    def test_a_neutral_record_cannot_move_the_score(self):
        neutral = _rec("neutral", impact=0.99, sentiment=0.0)
        assert estimated_point_movement(neutral, 0.0, 50) == 0.0

    def test_a_strong_opinion_moves_more_than_a_mild_one(self):
        furious = _rec("furious", sentiment=-0.95, impact=0.9)
        mild = _rec("mild", sentiment=-0.15, impact=0.9)
        assert (estimated_point_movement(furious, 0.0, 50)
                > estimated_point_movement(mild, 0.0, 50))

    def test_movement_shrinks_as_the_analysed_sample_grows(self):
        row = _rec("r", sentiment=-0.8, impact=0.8)
        small = estimated_point_movement(row, 0.0, 10)
        large = estimated_point_movement(row, 0.0, 2000)
        assert small > large


class TestTriage:
    def test_a_large_queue_collapses_to_a_short_worklist(self):
        """The real case: 293 pending, only a few can change the answer."""
        pending = [
            _rec("heavy-1", impact=0.95, sentiment=-0.9, confidence=0.9),
            _rec("heavy-2", impact=0.88, sentiment=0.8, confidence=0.85),
        ] + [
            _rec(f"noise-{i}", impact=0.05, sentiment=0.02,
                 authenticity=40, confidence=0.2, independent=0.1)
            for i in range(291)
        ]
        ready = [_rec(f"ready-{i}") for i in range(40)]

        out = triage_review_queue(pending, ready)

        assert out["total_pending"] == 293
        assert out["material_count"] <= 5, out["material_count"]
        assert out["volume_count"] == 293 - out["material_count"]
        assert out["priority"][0]["record"]["id"] == "heavy-1"
        assert out["priority"][1]["record"]["id"] == "heavy-2"

    def test_priority_is_ordered_by_movement(self):
        pending = [
            _rec("a", impact=0.2, sentiment=-0.3),
            _rec("b", impact=0.9, sentiment=-0.9),
            _rec("c", impact=0.5, sentiment=-0.6),
        ]
        out = triage_review_queue(pending, [])
        ids = [i["record"]["id"] for i in out["priority"]]
        assert ids == ["b", "c", "a"], ids
        movements = [i["movement"] for i in out["priority"]]
        assert movements == sorted(movements, reverse=True)

    def test_an_empty_queue_is_reported_as_empty(self):
        out = triage_review_queue([], [])
        assert out["total_pending"] == 0
        assert out["material_count"] == 0
        assert out["priority"] == []
        assert out["max_movement"] == 0.0

    def test_priority_size_is_respected(self):
        pending = [_rec(f"r{i}", impact=0.9, sentiment=-0.9) for i in range(50)]
        out = triage_review_queue(pending, [], priority_size=7)
        assert out["priority_count"] == 7

    def test_when_nothing_is_material_the_top_records_are_still_offered(self):
        """Never show an empty worklist: the best available still gets ranked."""
        pending = [_rec(f"r{i}", impact=0.01, sentiment=0.01, authenticity=30) for i in range(30)]
        out = triage_review_queue(pending, [_rec(f"x{i}") for i in range(500)])
        assert out["material_count"] == 0
        assert out["priority"], "an all-volume queue must still propose something"
        assert out["threshold_points"] == MATERIAL_POINT_THRESHOLD


class TestAutoAdjudication:
    """Second-pass AI adjudication of the pending queue.

    The queue exists because the first pass was not confident. Re-reading the
    same records with the reasoning tier settles most of them; what is still
    uncertain must stay for a human rather than be guessed.
    """

    FULL_AI = dict(
        sentiment_label="neutral", sentiment_score=0.0, sentiment_confidence=0.7,
        primary_emotion="neutral", emotion_confidence=0.5, language="greek",
        target_stance="neutral", topic="t", narrative="n", sarcasm=False,
        impact_score=0.3, relevance_confidence=0.7, semantic_relevance="relevant",
    )

    def _folder(self, tmp_path):
        from uuid import uuid4

        from app.services.storage import RunStore

        store = RunStore()
        # Settings are read at import time, so the store root is fixed for the
        # session: give every test its own run id instead of a shared folder.
        folder = store.root / "runs" / f"20260922T100000Z-{uuid4().hex[:8]}"
        (folder / "analysis").mkdir(parents=True)
        (folder / "cleaning").mkdir(parents=True)
        store.write(folder / "plan.json", {
            "client": "Stoiximan", "topic": "Stoiximan", "market": "Greece",
            "core_terms": ["Stoiximan"],
        })
        store.write(folder / "cleaning" / "semantic-candidates.json", [])
        rows = [
            {"id": rid, "text": f"σχόλιο {rid}", "platform": "facebook",
             "cleaning": {"confidence": 0.7},
             "ai_analysis": {**self.FULL_AI, "decision": dec, "overall_confidence": 0.5}}
            for rid, dec in (("1", "review"), ("2", "review"), ("3", "review"), ("4", "ready"))
        ]
        store.write(folder / "analysis" / "analyzed.json", rows)
        return store, folder

    class _Provider:
        """Answers the way a real model can: partial, and sometimes off-vocabulary."""
        VERDICTS = {
            "1": {"semantic_relevance": "relevant", "overall_confidence": 0.93,
                  "sentiment_label": "negative", "sentiment_score": -0.7,
                  "primary_emotion": "ΟΡΓΗ"},
            "2": {"semantic_relevance": "irrelevant", "overall_confidence": 0.91},
            "3": {"semantic_relevance": "uncertain", "overall_confidence": 0.40},
        }

        def analyze_batch(self, records, context, tier):
            import types
            anns = [{"record_id": r["record_id"], **self.VERDICTS[r["record_id"]]} for r in records]
            return types.SimpleNamespace(annotations=anns, cost_usd=0.02)

    def test_confident_verdicts_are_applied_and_uncertain_ones_are_not(self, tmp_path):
        from app.services.ai_analysis import auto_adjudicate_review_queue
        store, folder = self._folder(tmp_path)

        out = auto_adjudicate_review_queue(folder, provider=self._Provider())

        assert out["pending_before"] == 3
        assert out["kept"] == 1
        assert out["excluded"] == 1
        assert out["still_pending"] == 1

        rows = {r["id"]: r["ai_analysis"] for r in store.read(folder / "analysis" / "analyzed.json", [])}
        assert rows["1"]["decision"] == "ready"
        assert rows["2"]["decision"] == "excluded"
        assert rows["3"]["decision"] == "review", "an uncertain verdict must not be applied"
        assert rows["4"]["decision"] == "ready", "a settled record must not be touched"

    def test_off_vocabulary_values_are_snapped_to_safe_ones(self, tmp_path):
        """A label outside the closed vocabulary would crash report rebuilding."""
        from app.services.ai_analysis import auto_adjudicate_review_queue, EMOTIONS
        store, folder = self._folder(tmp_path)

        auto_adjudicate_review_queue(folder, provider=self._Provider())

        rows = {r["id"]: r["ai_analysis"] for r in store.read(folder / "analysis" / "analyzed.json", [])}
        assert rows["1"]["primary_emotion"] in EMOTIONS
        assert rows["1"]["sentiment_label"] == "negative", "a valid verdict must survive"

    def test_an_empty_queue_costs_nothing(self, tmp_path):
        from app.services.ai_analysis import auto_adjudicate_review_queue
        store, folder = self._folder(tmp_path)
        rows = store.read(folder / "analysis" / "analyzed.json", [])
        for row in rows:
            row["ai_analysis"]["decision"] = "ready"
        store.write(folder / "analysis" / "analyzed.json", rows)

        class _Fail:
            def analyze_batch(self, *a, **k):
                raise AssertionError("must not call a paid model with an empty queue")

        out = auto_adjudicate_review_queue(folder, provider=_Fail())
        assert out["pending_before"] == 0
        assert out["cost_usd"] == 0.0

    def test_a_provider_failure_leaves_records_pending(self, tmp_path):
        """Never guess a verdict: a failed call must keep the record in the queue."""
        from app.services.ai_analysis import auto_adjudicate_review_queue
        store, folder = self._folder(tmp_path)

        class _Broken:
            def analyze_batch(self, *a, **k):
                raise RuntimeError("provider down")

        out = auto_adjudicate_review_queue(folder, provider=_Broken())
        assert out["resolved"] == 0
        assert out["still_pending"] == 3
        rows = {r["id"]: r["ai_analysis"]["decision"]
                for r in store.read(folder / "analysis" / "analyzed.json", [])}
        assert rows["1"] == rows["2"] == rows["3"] == "review"
