"""v31.28 — the international visitor's voice, for place subjects only.

The Greek-market gate qualifies a record by LANGUAGE: Greek script, Greeklish,
or one of a handful of English market terms. For a brand that is right. For a
MONUMENT it deletes the finding: a German review of a venue in Athens writes
"Athen", which is not in GREECE_MARKET_TERMS, and a Hebrew or Chinese one is
capped at 0.10 by the dominant-foreign-script rule — both land under the 0.15
exclusion threshold and are thrown away before anyone reads them.

So `place_subject` is an OPT-IN on the draft, default off. Off, the gate behaves
exactly as it does today. On, mentioning the subject IS market evidence, because
the subject is a physical place in that market.

Separately, `language` only has five buckets and collapses German, Dutch, Hebrew
and Chinese into "other". `language_name` is a NEW field beside it that names the
language; nothing that reads `language` today is affected.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import AnalysisDraft
from app.services.ai_analysis import LANGUAGES, OUTPUT_SCHEMA, PROMPT_VERSION
from app.services.cleaning import clean_records
from app.services.query_planner import build_collection_plan


SUBJECT = "ΚΠΙΣΝ"
LATIN_NAME = "SNFCC"

# How a foreign visitor actually writes it: the venue's Latin name, no Greek
# letters, and the city in their own language ("Athen", not "Athens").
GERMAN = "Das SNFCC in Athen ist wunderschön, aber die Parkplätze sind ein Albtraum."
DUTCH = "Het SNFCC is prachtig, maar er zijn te weinig schaduwplekken."
HEBREW = "ביקרנו ב-SNFCC והיה מרהיב, אבל קשה להגיע לשם."
CHINESE = "我们参观了 SNFCC，非常漂亮，但是交通很不方便。"
GREEK = "Το ΚΠΙΣΝ είναι υπέροχο αλλά το πάρκινγκ είναι εφιάλτης."


def _plan(*, place_subject: bool) -> dict:
    return {
        "client": SUBJECT,
        "topic": SUBJECT,
        "market": "Greece",
        "date_from": "2026-04-17",
        "date_to": "2026-10-17",
        "target_total": 10,
        "core_terms": [SUBJECT, LATIN_NAME],
        "context_terms": ["Σταύρος Νιάρχος"],
        "greeklish_variants": [],
        "exclusions": [],
        "place_subject": place_subject,
        "sources": [{"source": "x"}, {"source": "news"}],
    }


def _row(i: int, text: str) -> dict:
    return {
        "id": str(i), "platform": "x", "text": text,
        "date": "2026-07-15T10:00:00+00:00", "author": f"visitor{i}",
        "followers": 40, "views": 0, "likes": 3, "comments": 0, "shares": 0,
        "url": f"https://example.test/x/{i}", "content_type": "post",
        "parent_post": None, "raw_data": {},
    }


def _decisions(texts: list[str], *, place_subject: bool) -> dict[str, str]:
    rows = [_row(i, t) for i, t in enumerate(texts, start=1)]
    result = clean_records(rows, _plan(place_subject=place_subject))
    return {rec["text"]: rec["cleaning"]["decision"] for rec in result["cleaned"]}


# ---------- 1. default OFF: today's behaviour, unchanged ----------

def test_default_is_off_and_foreign_voices_stay_excluded():
    """The regression guard: nothing changes for an analysis that does not opt in."""
    assert AnalysisDraft.model_fields["place_subject"].default is False

    decisions = _decisions([GERMAN, DUTCH, HEBREW, CHINESE, GREEK], place_subject=False)
    for text in (GERMAN, DUTCH, HEBREW, CHINESE):
        assert decisions[text] == "excluded", (
            f"without the opt-in the market gate must behave exactly as before: {text[:30]}")
    assert decisions[GREEK] != "excluded", "the Greek record is unaffected either way"


# ---------- 2. opt-in ON: the same records survive ----------

@pytest.mark.parametrize("text", [GERMAN, DUTCH, HEBREW, CHINESE, GREEK])
def test_place_subject_keeps_the_visitor_whatever_language_they_write_in(text):
    decisions = _decisions([text], place_subject=True)
    assert decisions[text] != "excluded", (
        "a visitor naming the venue is market evidence; the venue is IN the market")


def test_the_market_reason_says_why_it_was_kept():
    result = clean_records([_row(1, GERMAN)], _plan(place_subject=True))
    reasons = result["cleaned"][0]["cleaning"]["reasons"]
    assert "place_subject_is_in_market" in reasons, reasons


# ---------- 3. the opt-in is not a floodgate ----------

def test_a_record_that_never_mentions_the_place_is_still_excluded():
    """Opting in must not turn the run into an open firehose of foreign chatter."""
    unrelated = "Wir waren gestern im Pergamonmuseum in Berlin, sehr schön."
    decisions = _decisions([unrelated, GERMAN], place_subject=True)
    assert decisions[unrelated] == "excluded"
    assert decisions[GERMAN] != "excluded"


def test_the_flag_travels_from_the_draft_into_the_plan():
    def draft(place: bool) -> AnalysisDraft:
        return AnalysisDraft(
            client=SUBJECT, topic=SUBJECT, market="Greece",
            date_from=date(2026, 4, 17), date_to=date(2026, 10, 17),
            keywords=[SUBJECT], sources=["news"], sample_mode="perSource",
            per_source={"news": 5}, comments=False, max_budget_usd=1.0,
            smart_search=True, report_language="Ελληνικά",
            additional_context=[], exclusions=[], place_subject=place,
        )

    assert build_collection_plan(draft(False)).model_dump(mode="json")["place_subject"] is False
    assert build_collection_plan(draft(True)).model_dump(mode="json")["place_subject"] is True


# ---------- 4. language_name is additive ----------

def test_language_name_is_requested_without_touching_the_old_field():
    item = OUTPUT_SCHEMA["properties"]["items"]["items"]
    assert "language_name" in item["required"]
    assert item["properties"]["language_name"] == {"type": "string", "maxLength": 40}
    # The closed vocabulary is untouched, so every existing reader is safe.
    assert item["properties"]["language"] == {"type": "string", "enum": list(LANGUAGES)}
    assert set(LANGUAGES) == {"greek", "english", "greeklish", "mixed", "other"}
    # A cached annotation from before this change has no language_name, so the
    # prompt version must move or the cache would serve incomplete records.
    assert PROMPT_VERSION == "signalyth-semantic-v1.3"


def test_named_languages_are_counted_and_ranked():
    ready = [
        {"ai_analysis": {"sentiment_label": "positive", "primary_emotion": "joy",
                         "language": "other", "language_name": name,
                         "overall_confidence": 0.9, "flags": []}}
        for name in ("German", "German", "German", "Dutch", "Dutch", "Hebrew")
    ]
    counts: dict[str, int] = {}
    for r in ready:
        n = str(r["ai_analysis"].get("language_name") or "").strip() or "Unknown"
        counts[n] = counts.get(n, 0) + 1
    ranked = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    assert list(ranked) == ["German", "Dutch", "Hebrew"], ranked



def test_a_missing_language_name_never_fails_a_whole_batch():
    """One absent value must cost one label, not a batch of real evidence."""
    from app.services.ai_analysis import AnnotationModel

    payload = {
        "record_id": "r1", "semantic_relevance": "relevant", "relevance_score": 0.9,
        "relevance_confidence": 0.9, "relevance_reason": "names the venue",
        "target_entity": "ΚΠΙΣΝ", "target_stance": "supportive",
        "sentiment_label": "positive", "sentiment_score": 0.6, "sentiment_confidence": 0.8,
        "primary_emotion": "joy", "secondary_emotion": "none", "emotion_intensity": 0.5,
        "emotion_confidence": 0.8, "topic": "visit", "narrative": "praises the venue",
        "sarcasm": False, "sarcasm_confidence": 0.0, "language": "other",
        "evidence_quotes": [], "overall_confidence": 0.85,
    }
    assert AnnotationModel(**payload).language_name == "Unknown"
