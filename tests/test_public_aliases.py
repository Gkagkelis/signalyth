"""Public-name suggestions are ADVISORY ONLY. The research subject stays exactly
what the client asked: suggestions are shown before the run and never become
search routes or cleaning terms unless the operator adds them explicitly."""
import json
from datetime import date
from unittest.mock import MagicMock, patch

from app.models import AnalysisDraft
import app.services.query_planner as qp


def _draft(**kw):
    base = dict(client="Allwyn", topic="Allwyn", market="Greece",
                date_from=date(2026, 8, 1), date_to=date(2026, 9, 19),
                keywords=["Allwyn"], sources=["x"], sample_mode="perSource",
                sample_target=100, per_source={"x": 100}, comments=False,
                max_budget_usd=5, report_language="Ελληνικά",
                research_type="market", media_handling="separate", smart_search=True)
    base.update(kw)
    return AnalysisDraft(**base)


def _fake_ai(aliases):
    fake = MagicMock()
    fake.output_text = json.dumps({"aliases": aliases})
    return fake


def _queries(plan, source="x"):
    return [r["query"] if isinstance(r, dict) else str(r) for r in plan.query_preview.get(source, [])]


def test_suggestions_are_surfaced_but_never_searched_or_measured():
    with patch("app.config.settings.signalyth_ai_enabled", True), \
         patch("app.config.settings.openai_api_key", "sk-test"), \
         patch("openai.OpenAI") as client:
        client.return_value.responses.create.return_value = _fake_ai(
            ["ΟΠΑΠ", "OPAP", "Ελλάδα", "Allwyn", "ΟΠΑΠ"])
        plan = qp.build_collection_plan(_draft())
    # advisory: cleaned, deduplicated, stoplisted
    assert plan.subject_name_suggestions == ["ΟΠΑΠ", "OPAP"]
    # the subject of measurement is untouched
    assert plan.core_terms == ["Allwyn"]
    assert "ΟΠΑΠ" not in plan.greeklish_variants and "OPAP" not in plan.greeklish_variants
    assert not any("ΟΠΑΠ" in q or "OPAP" in q for q in _queries(plan))


def test_operator_can_opt_in_by_adding_the_name_as_an_alias():
    draft = _draft(keywords=["Allwyn", "ΟΠΑΠ"], keyword_roles={"ΟΠΑΠ": "alias"})
    with patch("app.config.settings.openai_api_key", ""):
        plan = qp.build_collection_plan(draft)
    assert "ΟΠΑΠ" in plan.greeklish_variants
    assert any("ΟΠΑΠ" in q for q in _queries(plan))


def test_suggestions_are_fail_open_and_never_break_planning():
    with patch("app.config.settings.openai_api_key", ""):
        assert qp.build_collection_plan(_draft()).subject_name_suggestions == []
    with patch("app.config.settings.signalyth_ai_enabled", True), \
         patch("app.config.settings.openai_api_key", "sk-test"), \
         patch("openai.OpenAI", side_effect=RuntimeError("provider down")):
        plan = qp.build_collection_plan(_draft())
    assert plan.subject_name_suggestions == [] and plan.core_terms == ["Allwyn"]
    with patch("app.config.settings.signalyth_ai_enabled", True), \
         patch("app.config.settings.openai_api_key", "sk-test"), \
         patch("openai.OpenAI") as client:
        bad = MagicMock(); bad.output_text = "Internal Server Error"
        client.return_value.responses.create.return_value = bad
        plan = qp.build_collection_plan(_draft())
    assert plan.subject_name_suggestions == [] and plan.core_terms == ["Allwyn"]
