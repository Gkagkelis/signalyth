"""A Greek writing in Latin letters is still the Greek market.

Greeklish is how a very large share of Greek social conversation is typed,
especially in comments. It carries no Greek letters, no country name and no
language tag, so the market gate scored it 0.00 and threw it away.

The waste was double: the planner already BUYS Greeklish search routes
("Eurojackpot klirosi", "Eurojackpot tzak pot"), so the run paid for results
that the next step was guaranteed to discard.

The hard part is not recognising Greeklish. It is not mistaking Polish, Spanish,
German or Italian for it, which is why every negative below is a real sentence
in another language and why two distinct markers are required: "den" on its own
is Dutch, "kai" is Finnish, "me" is English.
"""
from __future__ import annotations

import pytest

from app.services.cleaning import (
    RULESET_CONFIG,
    _market_score,
    _text_view,
    greeklish_prose_score,
)

GREECE = {"topic": "Nike", "market": "Greece", "core_terms": ["Nike"]}
FLOOR = RULESET_CONFIG["market_exclude_below"]


def _keeps(text: str) -> bool:
    score, _ = _market_score({"text": text, "platform": "facebook", "raw_data": {}},
                             GREECE, _text_view(text))
    return score >= FLOOR


class TestAGreekWritingInLatinLettersIsKept:
    @pytest.mark.parametrize("text", [
        "Ta kainourgia Nike den aksizoun ta lefta",
        "Pali tipota me to Eurojackpot, kala kanw kai den paizw",
        "Einai poly kalo auto pou ekanan, mpravo tous",
        "Den mporw na katalabw giati prepei na plirwsw tosa lefta",
    ])
    def test_it_survives_the_market_gate(self, text):
        assert _keeps(text), text


class TestOtherLanguagesAreStillRejected:
    """The whole risk of this change is letting the world back in."""

    @pytest.mark.parametrize("text", [
        "Nike customer service is terrible and I want a refund",
        "W sobotnim losowaniu Eurojackpot padla tylko jedna wygrana",
        "Granada reparte 700000 euros del Eurojackpot que sono para todos",
        "Die Nike Schuhe sind nicht gut und der Service ist ein Problem",
        "Le Nike non sono della qualita che pensavo, che peccato",
    ])
    def test_it_does_not_reach_the_greek_sample(self, text):
        assert not _keeps(text), text


class TestOneWordProvesNothing:
    """Every marker is an ordinary word somewhere else."""

    @pytest.mark.parametrize("text", [
        "den",                       # Dutch / Danish / German
        "kai",                       # Finnish / Hawaiian
        "Nike shoes for me",         # English containing "me"
        "Auto parts and service",    # English containing "auto"
    ])
    def test_a_single_marker_scores_nothing(self, text):
        score, reasons = greeklish_prose_score(text)
        assert score == 0.0, (text, reasons)

    def test_two_distinct_markers_are_needed(self):
        assert greeklish_prose_score("den")[0] == 0.0
        assert greeklish_prose_score("den kai")[0] > 0.0


class TestGreekScriptIsLeftToItsOwnRule:
    def test_real_greek_is_not_double_counted_here(self):
        assert greeklish_prose_score("Πήρα τα καινούργια Nike")[0] == 0.0

    def test_but_it_still_passes_the_market_gate(self):
        assert _keeps("Πήρα τα καινούργια Nike και μου άνοιξαν σε μία εβδομάδα")


class TestItIsHorizontal:
    def test_a_non_greece_market_is_untouched(self):
        plan = {"topic": "Nike", "market": "Germany", "core_terms": ["Nike"]}
        text = "Ta kainourgia Nike den aksizoun ta lefta"
        score, _ = _market_score({"text": text, "platform": "facebook", "raw_data": {}},
                                 plan, _text_view(text))
        assert score == 0.55, "only the Greece rule should have changed"
