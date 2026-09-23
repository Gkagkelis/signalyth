"""The bare global name is the one route with no market signal.

A run for Greece was spending ~43% of its paid sample on Polish and Spanish
Eurojackpot posts — correctly thrown away afterwards, but paid for first. The
diagnostic endpoint showed it directly:

    outside_target_market: 11 of 30   and   9 of 16

The fix removes ONLY the route that is nothing but the subject's name. Every
other route keeps its market signal, and for Greek content that signal is the
Greek language itself, not the literal word "Greece": people do not write their
own country's name when talking about their own country.
"""
from __future__ import annotations

from datetime import date

from app.models import AnalysisDraft
from app.services.cleaning import _fold
from app.services.query_planner import build_collection_plan, drop_bare_topic_route


def _queries(source: str = "facebook", market: str = "Greece") -> list[str]:
    draft = AnalysisDraft(
        client="ΟΠΑΠ", topic="Eurojackpot", market=market,
        date_from=date(2026, 9, 15), date_to=date(2026, 9, 22),
        keywords=["κλήρωση", "τζακ ποτ"], topic_aliases=["ευρωτζάκποτ"],
        sources=[source], sample_mode="perSource", per_source={source: 15},
        per_source_comments={source: 40}, comments=True, max_budget_usd=10.0,
        smart_search=True, report_language="Ελληνικά",
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    return [str(q) for q in (plan["sources"][0].get("queries") or [])]


class TestTheBareNameIsGone:
    def test_the_global_route_is_not_bought(self):
        assert "Eurojackpot" not in _queries()

    def test_x_loses_it_too_despite_the_operator_suffix(self):
        assert "Eurojackpot -filter:nativeretweets" not in _queries("x")


class TestEverythingWithAMarketSignalSurvives:
    """The failure mode to avoid is trading foreign noise for no data at all."""

    def test_the_greek_language_routes_are_kept(self):
        queries = _queries()
        assert "Eurojackpot κλήρωση" in queries
        assert "Eurojackpot τζακ ποτ" in queries

    def test_the_greeklish_routes_are_kept(self):
        assert "Eurojackpot klirosi" in _queries()

    def test_the_explicit_market_routes_are_kept(self):
        queries = _queries()
        assert "Eurojackpot Greece" in queries
        assert "Eurojackpot Ελλάδα" in queries

    def test_the_language_filter_is_not_weakened_on_x(self):
        """`lang:el` IS the market filter. Demanding the word "Greece" on top
        of it would exclude every Greek speaker who writes only in Greek."""
        assert any("lang:el" in q and "Greece OR" not in q for q in _queries("x"))

    def test_a_useful_number_of_routes_remains(self):
        assert len(_queries()) >= 5, "narrowing must not starve the run"


class TestItIsHorizontal:
    def test_nothing_is_dropped_when_no_market_is_set(self):
        assert drop_bare_topic_route(["Nike", "Nike shoes"], "Nike", "") == ["Nike", "Nike shoes"]

    def test_it_works_for_any_brand_and_any_market(self):
        assert drop_bare_topic_route(
            ["Nike", "Nike Deutschland"], "Nike", "Germany") == ["Nike Deutschland"]

    def test_the_last_route_is_never_taken_away(self):
        """Leaving a source with zero queries would be worse than the noise."""
        assert drop_bare_topic_route(["Nike"], "Nike", "Greece") == ["Nike"]

    def test_case_and_spacing_do_not_let_it_slip_through(self):
        assert drop_bare_topic_route(["  nike  ", "nike Greece"], "Nike", "Greece") == ["nike Greece"]


class TestStylisedTextIsReadable:
    """Marketing posts routinely use bold Unicode. Casefolding before NFKD
    dropped the leading capital, so the subject was never found."""

    def test_bold_unicode_resolves_to_the_plain_name(self):
        assert "eurojackpot" in _fold("𝗘𝘂𝗿𝗼𝗷𝗮𝗰𝗸𝗽𝗼𝘁 τζακ ποτ")

    def test_an_ordinary_name_is_unaffected(self):
        assert "eurojackpot" in _fold("Eurojackpot τζακ ποτ")

    def test_it_holds_for_any_brand(self):
        assert "nike" in _fold("𝗡𝗶𝗸𝗲 Ελλάδα")
