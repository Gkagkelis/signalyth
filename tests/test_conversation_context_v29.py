"""The conversation-context architecture, as shipped — slice by slice.

The model (two independent anchors; source-specific acquisition through each
platform's native market controls; a bounded post-cleaning broad probe) came
out of this week's production failures and an external review. The review's
own implementation regressed 31 existing behaviours, so it was rebuilt here
in small slices on top of v28, and THESE tests pin each slice's contract.

The heart of it, in two lines:
  * a comment inherits the SUBJECT from any parent that names it,
  * but it inherits the MARKET only from a parent that is itself Greek.
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from app.models import AnalysisDraft
from app.services.cleaning import _parent_market_signal, clean_run
from app.services.query_planner import (
    NATIVE_MARKET_FILTER_SOURCES,
    build_collection_plan,
    greece_x_route_anchor,
    instagram_discovery_tags,
    semantic_broad_probe_target,
)
from app.services.storage import RunStore


def _draft(sources, market="Greece", **kw):
    base = dict(
        client="ΟΠΑΠ", topic="Eurojackpot", market=market,
        date_from=date(2026, 9, 15), date_to=date(2026, 9, 22),
        keywords=["κλήρωση", "ευρωτζάκποτ"], keyword_roles={"ευρωτζάκποτ": "alias"},
        sources=sources, sample_mode="perSource",
        per_source={s: 20 for s in sources},
        per_source_comments={s: 40 for s in sources if s not in ("news", "youtube")},
        comments=True, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά",
    )
    base.update(kw)
    return AnalysisDraft(**base)


def _plan(sources, **kw):
    return build_collection_plan(_draft(sources, **kw)).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Slice A — market inheritance flows through the PARENT, not around it
# ---------------------------------------------------------------------------
class TestTheTwoAnchorsAreIndependent:
    PLAN = {"topic": "Eurojackpot", "client": "ΟΠΑΠ", "market": "Greece",
            "core_terms": ["Eurojackpot", "ευρωτζάκποτ"], "context_terms": ["κλήρωση"],
            "greeklish_variants": ["ευρωτζάκποτ"], "exclusions": [],
            "owned_accounts": [], "comments_requested": True}
    GR = "Eurojackpot: αποτελέσματα κλήρωσης — δείτε τους αριθμούς"
    PL = "Eurojackpot wyniki losowania w Polsce"

    def _verdicts(self, rows):
        tmp = Path(tempfile.mkdtemp())
        store = RunStore()
        store.write(tmp / "normalized-all.json", rows)
        store.write(tmp / "status.json", {"run_id": "T"})
        clean_run(tmp, plan=self.PLAN)
        out = {}
        for bucket in ("cleaned.json", "excluded.json"):
            for r in store.read(tmp / "cleaning" / bucket, []) or []:
                c = r.get("cleaning") or {}
                out[r["id"]] = c.get("decision") in ("trusted", "review", "semantic_candidate")
        return out

    def _comment(self, cid, text, parent):
        return {"id": cid, "platform": "facebook", "evidence_layer": "comment",
                "text": text, "parent_context": parent, "evidence_origin": "open",
                "author": "u" + cid, "url": f"https://www.facebook.com/p/posts/{cid}",
                "created_at": "2026-09-18T10:00:00Z", "raw_data": {}}

    def test_the_full_matrix(self):
        """Every promise of the architecture, in one table."""
        alive = self._verdicts([
            self._comment("c1", "Κέρδισα 50 ευρώ χθες!", self.GR),
            self._comment("c2", "Κέρδισα 50 ευρώ χθες!", ""),
            self._comment("c3", "Wygralem 50 euro wczoraj!", self.PL),
            self._comment("c4", "Kerdisa 50 evro xthes, kala kanw!", self.GR),
            self._comment("c5", "!!!! 🎉🎉", self.GR),
            self._comment("c6", "Won fifty euros yesterday, so happy!", self.GR),
        ])
        assert alive["c1"], "Greek under Greek parent must live"
        assert not alive["c2"], "an orphan with no subject anchor must die"
        assert not alive["c3"], ("subject inheritance must NOT smuggle in the market: "
                                 "Polish under a Polish Eurojackpot parent")
        assert alive["c4"], "Greeklish under a Greek parent must live"
        assert alive["c5"], "an emoji reply inherits BOTH anchors from a Greek parent"
        assert alive["c6"], "a Greek may comment in English under a Greek parent"

    def test_the_parent_signal_itself(self):
        assert _parent_market_signal(self.GR) > 0
        assert _parent_market_signal(self.PL) == 0.0
        assert _parent_market_signal("Eurojackpot draw results for Greece") > 0
        assert _parent_market_signal("Pame gia to tzakpot, kali tyxi se olous") > 0
        assert _parent_market_signal("") == 0.0


# ---------------------------------------------------------------------------
# Slice B — X: exactly one market anchor per route
# ---------------------------------------------------------------------------
class TestXSingleAnchor:
    def test_greek_script_routes_gain_the_language_guard(self):
        assert "lang:el" in greece_x_route_anchor("Eurojackpot κλήρωση -filter:nativeretweets")

    def test_greeklish_routes_are_never_silenced(self):
        """X does not classify Latin-script Greeklish as Greek; lang:el there
        is a veto, not a filter."""
        assert greece_x_route_anchor("Eurojackpot klirosi") == "Eurojackpot klirosi"

    def test_explicit_market_routes_keep_the_mixed_language_greeks(self):
        q = "Eurojackpot (Greece OR Ελλάδα OR Ellada) -filter:nativeretweets"
        assert greece_x_route_anchor(q) == q

    def test_an_existing_language_operator_is_respected(self):
        q = "Eurojackpot lang:el -filter:nativeretweets"
        assert greece_x_route_anchor(q) == q

    def test_every_planned_x_route_carries_exactly_one_anchor(self):
        """One anchor per route: lang:el XOR an explicit market word.

        The bare Latin subject route gets lang:el — that IS the canonical X
        route, one anchor carried by the language operator. A market-word
        route never gets lang:el stacked on top of it. And the Greeklish
        routes run with NO operator at all, because their market evidence is
        the Greeklish word itself and X cannot tag Latin Greeklish as Greek.
        """
        import re
        queries = _plan(["x"])["sources"][0]["queries"]
        unanchored_latin = []
        for q in queries:
            has_lang = "lang:el" in q
            has_market = bool(re.search(r"(?<!\w)(Greece|Ellada|Hellas|Ελλάδα)(?!\w)", q, re.I))
            greek_script = bool(re.search(r"[Ͱ-Ͽἀ-῿]", q))
            assert not (has_lang and has_market), f"two anchors stacked on one route: {q}"
            if greek_script:
                assert has_lang or has_market, f"greek-script route lost its guard: {q}"
            elif not has_lang and not has_market:
                unanchored_latin.append(q)
        assert unanchored_latin, "the Greeklish routes were silenced or dropped"
        for q in unanchored_latin:
            extra = [t for t in re.findall(r"[a-z]+", q.lower())
                     if t not in {"eurojackpot", "filter", "nativeretweets", "lang", "el", "or", "and"}]
            assert extra, f"a BARE Latin subject route ran with no anchor at all: {q}"


# ---------------------------------------------------------------------------
# Slice C — the bare subject lives ONLY inside a native country filter
# ---------------------------------------------------------------------------
class TestBareSubjectPlacement:
    def test_native_filter_sources_search_the_bare_name(self):
        for source in sorted(NATIVE_MARKET_FILTER_SOURCES):
            queries = _plan([source])["sources"][0]["queries"]
            assert "Eurojackpot" in queries, f"{source} has a native GR filter and lost recall"

    def test_their_actor_inputs_really_carry_the_filter(self):
        plan = _plan(["tiktok", "youtube", "news"])
        seen = {}
        for sp in plan["sources"]:
            blob = str([sr.get("input") for sr in sp.get("subruns") or []])
            seen[sp["source"]] = any(m in blob for m in ("'GR'", "'gr'", "'el'"))
        assert all(seen.values()), seen

    def test_facebook_never_searches_the_bare_name_as_primary(self):
        assert "Eurojackpot" not in _plan(["facebook"])["sources"][0]["queries"]


# ---------------------------------------------------------------------------
# Slices D + E — Instagram real tags, and the bounded post-cleaning probe
# ---------------------------------------------------------------------------
class TestInstagramAndTheProbe:
    def test_instagram_primary_is_a_real_market_anchored_tag(self):
        tags = instagram_discovery_tags(_draft(["instagram"]))
        assert tags[0] == "ευρωτζάκποτ"
        assert "Eurojackpot" not in tags, "the global tag belongs to the probe, not primary"

    def test_no_country_tag_is_ever_fabricated(self):
        for tag in instagram_discovery_tags(_draft(["instagram"])):
            assert "greece" not in tag.lower() and "Ελλάδα" not in tag

    # The form's model demands a market of at least one character, so the
    # planner's defensive no-market branches are reached only by whitespace.
    def test_without_a_market_the_global_tag_leads_as_before(self):
        assert instagram_discovery_tags(_draft(["instagram"], market=" "))[0] == "Eurojackpot"

    @pytest.mark.parametrize("source", ["facebook", "instagram"])
    def test_the_probe_exists_is_bare_and_is_capped(self, source):
        sp = _plan([source])["sources"][0]
        probes = [sr for sr in sp["semantic_topup_subruns"]
                  if sr.get("purpose") == "semantic_broad_probe"]
        assert len(probes) == 1
        cap = semantic_broad_probe_target(sp["target_items"])
        assert probes[0]["target_items"] == cap
        blob = str(probes[0]["input"])
        assert "Eurojackpot" in blob or "eurojackpot" in blob

    def test_the_probe_is_large_enough_to_measure_market_yield_but_bounded(self):
        assert semantic_broad_probe_target(40) == 60
        assert semantic_broad_probe_target(200) == 60
        assert semantic_broad_probe_target(8) == 12
        assert semantic_broad_probe_target(0) == 0

    def test_native_filter_sources_need_no_probe(self):
        for source in ("tiktok", "news"):
            sp = _plan([source])["sources"][0]
            assert not [sr for sr in sp["semantic_topup_subruns"]
                        if sr.get("purpose") == "semantic_broad_probe"]

    def test_no_market_means_no_probe(self):
        sp = _plan(["facebook"], market=" ")["sources"][0]
        assert not [sr for sr in sp["semantic_topup_subruns"]
                    if sr.get("purpose") == "semantic_broad_probe"]
