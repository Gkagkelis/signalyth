"""v31.23 — a report with no analysis behind it must never ship, at any size.

Run 20260927T085252Z-cdddbdc0: four records, OpenAI credit exhausted
(balance -$0.36), every provider call failed — and the run still completed,
generated exports, and produced a polished deck reading "neutral" for every
record. A reprocess of the same run did it a second time. Handed to a client,
that report looks like a finding ("the conversation is neutral") when in
truth nothing was ever analysed.

The outage guard existed but demanded MORE THAN ONE FULL BATCH before it
would fire:

    len(failed) == len(enriched) and len(enriched) > max(1, batch_size)

with signalyth_ai_batch_size = 12. Any run whose trusted sample was twelve
records or fewer sailed straight through it.

Two rules now:
1. Size never excuses it. Total provider failure raises, at 1 record or 500.
2. Only what the model was ASKED about counts. A record with no text, or one
   stopped by the cost guard, is a SIGNALYTH decision and must not mask a
   dead provider — nor, on its own, accuse a healthy one.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.ai_analysis import AIAnalysisProviderOutage


class DeadProvider:
    """Every call fails, exactly as an exhausted account behaves."""

    def __init__(self, message="Error code: 429 - insufficient_quota"):
        self.message = message
        self.calls = 0

    def analyze_batch(self, records, context, tier):
        self.calls += 1
        raise RuntimeError(self.message)


PLAN = {
    "client": "Μανώλης Χριστοδουλάκης",
    "topic": "Μανώλης Χριστοδουλάκης",
    "market": "Greece",
    "date_from": "2026-09-01",
    "date_to": "2026-09-26",
    "report_language": "Ελληνικά",
    "research_type": "political",
}


def _rows(n: int, *, text="Πολύ σωστά τα είπε ο βουλευτής σήμερα"):
    return [
        {"id": f"r{i}", "platform": "youtube", "text": f"{text} {i}",
         "date": "2026-09-12T10:00:00+00:00", "author": f"u{i}",
         "evidence_layer": "comment", "likes": 1, "views": 0,
         "comments": 0, "shares": 0, "followers": 0, "url": f"https://x/{i}"}
        for i in range(n)
    ]


def test_the_guard_no_longer_waits_for_a_full_batch():
    """The exact hole the live run fell through."""
    import inspect
    from app.services import ai_analysis
    src = inspect.getsource(ai_analysis._analysis_report)
    assert "len(enriched) > max(1, int(settings.signalyth_ai_batch_size))" not in src, (
        "total provider failure must not require more records than a batch")
    assert "asked_failed" in src and "len(asked_failed) == len(asked)" in src


def test_four_failed_records_raise_instead_of_shipping_a_neutral_report():
    """Four records is the size that shipped a fake report in production."""
    from app.services.ai_analysis import analyze_records
    rows = _rows(4)
    assert len(rows) <= int(settings.signalyth_ai_batch_size), (
        "the regression only reproduces below one full batch")
    with pytest.raises(AIAnalysisProviderOutage):
        analyze_records(rows, PLAN, DeadProvider())


def test_even_a_single_failed_record_raises():
    from app.services.ai_analysis import analyze_records
    with pytest.raises(AIAnalysisProviderOutage):
        analyze_records(_rows(1), PLAN, DeadProvider())


def test_the_provider_error_reaches_the_operator():
    """The run card has to name the real cause — exhausted credit, bad key."""
    from app.services.ai_analysis import analyze_records
    with pytest.raises(AIAnalysisProviderOutage) as excinfo:
        analyze_records(_rows(3), PLAN, DeadProvider("insufficient_quota"))
    assert "insufficient_quota" in str(excinfo.value)


def test_records_never_sent_to_the_model_do_not_accuse_it():
    """A textless record is SIGNALYTH's own decision, not provider evidence."""
    from app.services.ai_analysis import analyze_records
    rows = [{"id": "empty1", "platform": "youtube", "text": "   ",
             "date": "2026-09-12T10:00:00+00:00", "author": "u",
             "evidence_layer": "comment", "likes": 0, "views": 0,
             "comments": 0, "shares": 0, "followers": 0, "url": "https://x/1"}]
    provider = DeadProvider()
    report = analyze_records(rows, PLAN, provider)
    assert provider.calls == 0, "a textless record must never reach the model"
    summary = report.get("report") if isinstance(report.get("report"), dict) else report
    assert int(summary.get("provider_failure_records") or 0) == 0
