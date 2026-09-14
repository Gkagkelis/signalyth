from app.services.cleaning import clean_records


def _plan(**kw):
    base = {
        "client": "Eurojackpot", "topic": "Eurojackpot", "market": "Greece",
        "core_terms": ["Eurojackpot"], "context_terms": ["τζακποτ", "κλήρωση"],
        "greeklish_variants": [], "exclusions": [], "target_total": 10,
        "sources": [{"source": "instagram", "target_items": 10}],
    }
    base.update(kw)
    return base


def _row(text, rid="1", **kw):
    row = {
        "id": rid, "platform": "instagram", "text": text,
        "date": "2026-09-11T10:00:00+00:00", "author": f"user{rid}",
        "url": f"https://ig/{rid}", "likes": 1, "comments": 0, "shares": 0,
        "views": 10, "raw_data": {},
    }
    row.update(kw)
    return row


def test_foreign_language_lottery_chatter_is_excluded_as_outside_market():
    rows = [
        _row("Hier sind die aktuellen Gewinnzahlen der Eurojackpot-Ziehung vom 11.09.2026", "de1"),
        _row("Estrazione EuroJackPot n. 74 di venerdi 11 settembre 2026", "it1"),
    ]
    cleaned = clean_records(rows, _plan())["cleaned"]
    for rec in cleaned:
        assert rec["cleaning"]["decision"] == "excluded"
        assert "outside_target_market" in rec["cleaning"]["reasons"]


def test_greek_text_and_english_with_market_reference_pass_the_gate():
    rows = [
        _row("Μεγάλο τζακποτ απόψε στο Eurojackpot, παίζει όλη η Ελλάδα", "gr1"),
        _row("Eurojackpot fever is spreading across Greece before tonight's draw", "en1"),
    ]
    cleaned = clean_records(rows, _plan())["cleaned"]
    for rec in cleaned:
        assert rec["cleaning"]["decision"] != "excluded", rec["cleaning"]["reasons"]
        assert "outside_target_market" not in rec["cleaning"]["reasons"]


def test_comment_inherits_market_via_parent_context_instead_of_being_gated():
    rows = [_row(
        "Was hoping for better odds this week to be honest", "c1",
        evidence_layer="reply", parent_post="p1",
        parent_context="Eurojackpot Ελλάδα: μεγάλη κλήρωση απόψε με τζακποτ 120 εκατ.",
    )]
    cleaned = clean_records(rows, _plan())["cleaned"]
    rec = cleaned[0]
    assert "outside_target_market" not in rec["cleaning"]["reasons"], rec["cleaning"]["reasons"]
    assert rec["cleaning"]["decision"] != "excluded"
