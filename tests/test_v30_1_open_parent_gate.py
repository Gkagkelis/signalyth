from __future__ import annotations
from app.services.relevance_expansion import _comment_parent_candidate_allowed, _comment_seed_refs

def fb_row(idx, *, text, comments, market_score, reasons, flags=None):
    url = f"https://www.facebook.com/example/posts/{1000 + idx}"
    return {
        "id": f"fb-{idx}",
        "platform": "facebook",
        "evidence_layer": "primary",
        "url": url,
        "text": text,
        "comments": comments,
        "likes": 0,
        "shares": 0,
        "views": 0,
        "metric_availability": {"comments_known": True},
        "raw_data": {"postUrl": url},
        "cleaning": {
            "decision": "trusted" if market_score >= 0.25 else "excluded",
            "relevance_score": 0.8,
            "market_score": market_score,
            "spam_score": 0.0,
            "authenticity_status": "low_risk",
            "flags": list(flags or []),
            "reasons": list(reasons),
        },
    }

def test_off_topic_high_engagement_parent_is_rejected():
    row = fb_row(
        1,
        text="Picture muna bago manood #students #collegelife",
        comments=100,
        market_score=0.0,
        reasons=["core_term_missing", "subject_not_mentioned"],
        flags=["core_term_missing", "no_subject_signal"],
    )
    assert _comment_parent_candidate_allowed("facebook", row) is False

def test_direct_subject_but_foreign_market_parent_is_rejected():
    row = fb_row(
        2,
        text="Eurojackpot KNVB Beker",
        comments=80,
        market_score=0.0,
        reasons=["core_term:Eurojackpot", "outside_target_market"],
    )
    assert _comment_parent_candidate_allowed("facebook", row) is False

def test_greek_subject_parent_is_allowed():
    row = fb_row(
        3,
        text="Eurojackpot: σήμερα η κλήρωση για 68 εκατ. ευρώ",
        comments=12,
        market_score=0.84,
        reasons=["core_term:Eurojackpot", "greek_script", "greek_language_pattern"],
    )
    assert _comment_parent_candidate_allowed("facebook", row) is True

def test_context_without_direct_subject_is_not_enough():
    row = fb_row(
        4,
        text="Σήμερα μεγάλη κλήρωση",
        comments=40,
        market_score=0.70,
        reasons=["context_term:κλήρωση", "greek_script", "greek_language_pattern"],
    )
    assert _comment_parent_candidate_allowed("facebook", row) is False

def test_capacity_selection_drops_off_topic_100_comment_parent():
    bad = fb_row(
        5,
        text="Picture muna bago manood #students",
        comments=100,
        market_score=0.0,
        reasons=["core_term_missing", "subject_not_mentioned"],
        flags=["core_term_missing", "no_subject_signal"],
    )
    good = fb_row(
        6,
        text="Eurojackpot στην Ελλάδα: νέα κλήρωση",
        comments=3,
        market_score=0.84,
        reasons=["core_term:Eurojackpot", "greek_script"],
    )
    refs, meta, mode = _comment_seed_refs("facebook", [bad, good], max_seeds=12)
    assert len(refs) == 1
    assert len(meta) == 1
    assert meta[0]["comments"] == 3
    assert "Picture muna" not in meta[0]["text"]
