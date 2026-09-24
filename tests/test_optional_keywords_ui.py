from __future__ import annotations

from datetime import date
from pathlib import Path

from app.models import AnalysisDraft


def test_analysis_draft_allows_empty_keywords():
    draft = AnalysisDraft(
        client="OPAP",
        topic="Eurojackpot",
        market="Greece",
        date_from=date(2026, 9, 14),
        date_to=date(2026, 9, 23),
        keywords=[],
        sources=["facebook"],
        sample_mode="perSource",
        per_source={"facebook": 20},
        per_source_comments={"facebook": 50},
        comments=True,
        max_budget_usd=5,
    )
    assert draft.keywords == []


def test_frontend_run_validation_does_not_require_keywords():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "const enough=formState.topic.trim()&&formState.market;" in html
    assert "datesValid&&selected&&sampleValid&&b>0" in html
    assert "datesValid&&formState.keywords.trim()&&selected" not in html


def test_frontend_marks_keywords_optional():
    html = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert "Main keywords (optional)" in html
    assert "Βασικά keywords (προαιρετικό)" in html
