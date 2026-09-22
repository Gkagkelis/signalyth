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


def test_excluded_parents_never_become_comment_seeds():
    from app.services.relevance_expansion import _comment_seed_refs
    cleaned = [
        {"platform": "instagram", "evidence_layer": "primary", "comments": 50, "likes": 900,
         "url": "https://www.instagram.com/p/DE0001/", "text": "Gewinnzahlen Eurojackpot", "raw_data": {},
         "metric_availability": {"comments_known": True},
         "cleaning": {"decision": "excluded", "reasons": ["outside_target_market"]}},
        {"platform": "instagram", "evidence_layer": "primary", "comments": 3, "likes": 5,
         "url": "https://www.instagram.com/p/GR0001/", "text": "Μεγάλο τζακποτ απόψε", "raw_data": {},
         "metric_availability": {"comments_known": True},
         "cleaning": {"decision": "trusted", "reasons": []}},
    ]
    refs, meta, mode = _comment_seed_refs("instagram", cleaned, max_seeds=10)
    assert refs == ["https://www.instagram.com/p/GR0001/"], refs
    assert mode == "reported_comments", mode


def test_all_zero_comment_counts_still_probe_instead_of_skipping():
    """Discovery Actors routinely report commentsCount: 0 for every search hit.

    Treating that as "no comments anywhere" silently skipped the whole comment
    layer for every source. A reported zero is now only a ranking signal: when
    no parent reports comments, the most engaged relevant parents are probed.
    """
    from app.services.relevance_expansion import _comment_seed_refs, UNRELIABLE_COUNT_PROBE_PARENTS
    cleaned = [
        {"platform": "facebook", "evidence_layer": "primary", "comments": 0, "likes": likes,
         "url": f"https://www.facebook.com/page/posts/{i}", "text": "Stoiximan", "raw_data": {},
         "metric_availability": {"comments_known": True},
         "cleaning": {"decision": "trusted", "reasons": []}}
        for i, likes in enumerate([5, 900, 40, 120, 7, 300, 60, 20], start=1)
    ]
    refs, meta, mode = _comment_seed_refs("facebook", cleaned, max_seeds=12)
    assert mode == "probe_unreliable_counts", mode
    # Bounded probe, most engaged parent first.
    assert len(refs) == UNRELIABLE_COUNT_PROBE_PARENTS, refs
    assert refs[0] == "https://www.facebook.com/page/posts/2", refs

    # An excluded parent stays excluded even in the probe path.
    for row in cleaned:
        row["cleaning"] = {"decision": "excluded", "reasons": ["outside_target_market"]}
    refs, _meta, mode = _comment_seed_refs("facebook", cleaned, max_seeds=12)
    assert refs == [] and mode == "no_relevant_parent_rows", (refs, mode)


def test_offtopic_greece_hashtag_content_is_excluded_as_subject_not_mentioned():
    # TikTok location/hashtag discovery returns #greece travel content that never
    # mentions the subject: it must be excluded before paid analysis, not reviewed.
    rows = [
        _row("#LifeIsGood #greece\U0001F1EC\U0001F1F7 #europe #applyingforjobs", "t1"),
        _row("Klio cruise, must do in Thessaloniki #boatcruise #greece", "t2"),
        _row("My favourite 5 items to purchase from a Greek supermarket", "t3"),
    ]
    cleaned = clean_records(rows, _plan())["cleaned"]
    for rec in cleaned:
        assert rec["cleaning"]["decision"] == "excluded", rec["cleaning"]["reasons"]
        assert "subject_not_mentioned" in rec["cleaning"]["reasons"]


def test_comment_without_subject_survives_via_parent_context():
    rows = [_row(
        "I hope this is finally my week", "c1",
        evidence_layer="reply", parent_post="p1",
        parent_context="Eurojackpot Ελλάδα: αποψινή κλήρωση με τζακποτ 120 εκατ.",
    )]
    rec = clean_records(rows, _plan())["cleaned"][0]
    assert "subject_not_mentioned" not in rec["cleaning"]["reasons"]
    assert rec["cleaning"]["decision"] != "excluded"


def test_market_terms_in_urls_and_latin_hashtags_do_not_pass_the_gate():
    rows = [_row(
        "EUROJACKPOT Results for Tuesday. Winning numbers 4 5 7 48 50. "
        "Full results at greece-powerball.co.za #GreecePowerball #LotteryResults", "sa1",
    )]
    rec = clean_records(rows, _plan())["cleaned"][0]
    assert rec["cleaning"]["decision"] == "excluded"
    assert "outside_target_market" in rec["cleaning"]["reasons"]


def test_dominant_foreign_script_content_is_outside_market():
    rows = [_row(
        "Nhiều người nghĩ rằng có nhiều tiền là phước. "
        "Chúc may mắn Eurojackpot!", "vn1",
    )]
    rec = clean_records(rows, _plan())["cleaned"][0]
    assert rec["cleaning"]["decision"] == "excluded"
    assert "outside_target_market" in rec["cleaning"]["reasons"]


def test_syndicated_copies_across_pages_count_once():
    text = ("Οι αριθμοί της κλήρωσης για τα 23 εκατομμύρια ευρώ στο Eurojackpot: "
            "οι τυχεροί αριθμοί που ανέδειξε η κληρωτίδα της Τρίτης είναι 3 17 30 35 43 "
            "και μοιράζουν τα κέρδη στους νικητές της πρώτης κατηγορίας")
    rows = [_row(text, "a1"), _row(text, "a2"), _row(text, "a3")]
    cleaned = clean_records(rows, _plan())["cleaned"]
    excluded = [r for r in cleaned if r["cleaning"]["decision"] == "excluded"]
    assert len(excluded) == 2
    for rec in excluded:
        assert "duplicate_not_independent_evidence" in rec["cleaning"]["reasons"]


def test_draw_results_bulletins_are_media_announcements_not_organic_voices():
    rows = [_row(
        "Eurojackpot: Οι τυχεροί αριθμοί που έβγαλε η κλήρωση της Παρασκευής 21/8/2026 "
        "μοιράζουν 31 εκατ. ευρώ στους νικητές", "m1",
    )]
    rec = clean_records(rows, _plan())["cleaned"][0]
    assert rec["cleaning"]["content_class"] == "announcement"
    assert rec["cleaning"]["origin_class"] == "earned_media"
    genuine = clean_records([_row(
        "Άμα κερδίσω στο eurojackpot θα πληρώνω εγώ τους bodyguard να σε προστατεύουνε", "g1")],
        _plan())["cleaned"][0]
    assert genuine["cleaning"]["content_class"] != "announcement"
    assert genuine["cleaning"]["decision"] != "excluded"


def test_greeklish_subject_mentions_are_inside_the_greek_market():
    # A Greek writing in Latin script ("eurotzakpot") must never be dropped as
    # foreign, while the same subject in German/Italian still stays out.
    p = _plan()
    p["greeklish_variants"] = ["eurotzakpot", "tzakpot"]
    greeklish = clean_records([
        _row("eurotzakpot pali tipota, ta idia kai ta idia", "gk1"),
        _row("Epaiksa eurotzakpot kai kerdisa 20 euro!", "gk2"),
    ], p)["cleaned"]
    for rec in greeklish:
        assert rec["cleaning"]["decision"] != "excluded", rec["cleaning"]["reasons"]
        assert "greeklish_subject_variant" in rec["cleaning"]["reasons"]
    foreign = clean_records([
        _row("Eurojackpot Gewinnzahlen vom Freitag: 3, 17, 30", "de1"),
        _row("Estrazione EuroJackPot di venerdi 11 settembre", "it1"),
    ], p)["cleaned"]
    for rec in foreign:
        assert rec["cleaning"]["decision"] == "excluded"
        assert "outside_target_market" in rec["cleaning"]["reasons"]


def test_every_genuine_greek_mention_survives_whatever_its_tone():
    p = _plan()
    p["context_terms"] = ["κλήρωση", "τζακποτ", "ΟΠΑΠ"]
    rows = [
        _row("Κέρδισα 50 ευρώ στο eurojackpot χαχαχα, πλούτισα!", "s1"),
        _row("Τι απάτη είναι αυτό το eurojackpot, ποτέ δεν κερδίζει κανείς", "s2"),
        _row("Πάλι τζίφος στο eurojackpot. 12 χρόνια παίζω, ούτε ένα πεντάρι", "s3"),
        _row("Αν κερδίσω το eurojackpot φεύγω αύριο για Μαλδίβες", "s4"),
        _row("Μπράβο ρε ΟΠΑΠ, 39 εκατ. στο eurojackpot και πάλι κανένας Έλληνας", "s5"),
    ]
    for rec in clean_records(rows, p)["cleaned"]:
        assert rec["cleaning"]["decision"] != "excluded", rec["cleaning"]["reasons"]
