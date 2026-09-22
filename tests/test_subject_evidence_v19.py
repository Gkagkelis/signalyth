"""Comments under an operator-pasted URL must survive the subject gate.

A comment rarely repeats the brand name — under a Eurojackpot post people write
"πάλι τίποτα", not "πάλι τίποτα στο Eurojackpot". The cleaner keeps such a reply
only because the PARENT post carries the subject (`contextual_parent_match`).

A ranked seed carries the parent's scraped text. An operator-pasted URL does not:
we never scraped that post. Before this fix its seed text was "", so every comment
harvested from the operator's own hand-picked threads was excluded as
`subject_not_mentioned` — the feature deleted precisely the evidence it existed to
collect.
"""
from __future__ import annotations

from app.services.cleaning import clean_records
from app.services.relevance_expansion import operator_parent_context

PLAN = {
    "client": "ΟΠΑΠ",
    "topic": "Eurojackpot",
    "market": "Greece",
    "core_terms": ["Eurojackpot", "ευρωτζάκποτ"],
    "date_from": "2026-08-23",
    "date_to": "2026-09-22",
}

SCRAPED_PARENT = "Eurojackpot: Τζακ ποτ 120 εκατ. ευρώ στην αποψινή κλήρωση."

#: What people actually write under a lottery post: an opinion, no brand name.
BARE_OPINIONS = [
    "Πάλι τίποτα, κάθε βδομάδα τα ίδια.",
    "Στημένα είναι όλα, ποτέ δεν κερδίζει Έλληνας.",
    "Έπαιξα πέντε στήλες και δεν βρήκα ούτε έναν αριθμό.",
    "Επιτέλους πληρώθηκα κανονικά, όλα καλά.",
]


def _comment(rid: str, text: str, parent: str) -> dict:
    return {
        "id": rid, "platform": "facebook", "text": text,
        "evidence_layer": "comment", "content_type": "comment",
        "parent_context": parent or None,
        "date": "2026-09-10T12:00:00Z",
        "url": f"https://facebook.com/p/{rid}",
        "author": f"user{rid}", "author_id": f"u{rid}",
        "engagement": {"likes": 4, "comments": 1, "shares": 0},
    }


def _decisions(parent: str) -> list[str]:
    rows = [_comment(str(i), text, parent) for i, text in enumerate(BARE_OPINIONS)]
    out = clean_records(rows, PLAN)
    return [r["cleaning"]["decision"] for r in out["cleaned"]]


class TestOperatorParentContext:
    def test_the_subject_is_recorded_as_the_parent(self):
        ctx = operator_parent_context(PLAN)
        assert "Eurojackpot" in ctx
        assert "ΟΠΑΠ" in ctx

    def test_it_survives_a_plan_with_no_core_terms(self):
        assert "Eurojackpot" in operator_parent_context({"topic": "Eurojackpot"})

    def test_an_empty_plan_yields_no_false_context(self):
        assert operator_parent_context({}) == ""


class TestCommentsSurvive:
    def test_without_parent_text_every_opinion_is_thrown_away(self):
        """The regression this fix exists for — pinned so it cannot come back."""
        assert _decisions("") == ["excluded"] * len(BARE_OPINIONS)

    def test_the_operator_assertion_keeps_them(self):
        decisions = _decisions(operator_parent_context(PLAN))
        assert "excluded" not in decisions, decisions
        assert set(decisions) == {"review"}

    def test_an_operator_url_is_worth_no_more_than_a_ranked_seed(self):
        """Pasting a link must not buy a record more trust than scraping it."""
        assert _decisions(operator_parent_context(PLAN)) == _decisions(SCRAPED_PARENT)


class TestSeedMetaWiring:
    def test_operator_seeds_are_built_with_that_context(self):
        """The helper is worthless if the seed_meta still ships text=""."""
        import inspect

        from app.services import relevance_expansion

        src = inspect.getsource(relevance_expansion.adaptive_expand_after_cleaning)
        assert "operator_parent_context(plan)" in src, "operator seeds must carry the subject"
        assert '"text": ""' not in src, "an operator seed must never ship empty parent text"


class TestAliasesCountAsTheSubject:
    """"Άλλα ονόματα που μετρούν" has to mean what it says.

    An alias is another NAME for the same subject — "ευρωτζάκποτ" IS Eurojackpot.
    It used to be planned as mere context, so a post written only in Greek script
    failed the subject gate, scored 0.11 and was demoted to review instead of
    counting as a direct mention.
    """

    def _draft(self):
        from app.models import AnalysisDraft

        return AnalysisDraft(
            client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
            date_from="2026-08-23", date_to="2026-09-22",
            keywords=["ευρωτζάκποτ", "euro jackpot", "ΟΠΑΠ"],
            keyword_roles={"ευρωτζάκποτ": "alias", "euro jackpot": "alias",
                           "ΟΠΑΠ": "context"},
            sources=["facebook"], sample_mode="perSource",
            per_source={"facebook": 150}, per_source_comments={"facebook": 500},
            comments=True,
        )

    def _plan(self):
        from app.services.query_planner import build_terms

        core, context, aliases = build_terms(self._draft())
        return {**PLAN, "core_terms": core, "context_terms": context,
                "greeklish_variants": aliases}

    def _post(self, rid, text):
        return {"id": rid, "platform": "facebook", "text": text,
                "evidence_layer": "primary", "content_type": "post",
                "date": "2026-09-10T12:00:00Z",
                "url": f"https://facebook.com/p/{rid}",
                "author": f"u{rid}", "author_id": f"u{rid}",
                "engagement": {"likes": 20, "comments": 5, "shares": 1}}

    def _decide(self, text):
        out = clean_records([self._post("1", text)], self._plan())
        return out["cleaned"][0]["cleaning"]

    def test_an_alias_is_planned_as_a_core_term(self):
        from app.services.query_planner import build_terms

        core, _, _ = build_terms(self._draft())
        assert core[0] == "Eurojackpot", "the topic stays the primary search route"
        assert "ευρωτζάκποτ" in core

    def test_a_post_using_only_the_alias_counts(self):
        c = self._decide("Το ευρωτζάκποτ πάλι δεν έδωσε τίποτα, κρίμα.")
        assert c["decision"] == "trusted", c["reasons"]
        assert any(r.startswith("core_term:") for r in c["reasons"])

    def test_a_plain_context_word_still_does_not_count_as_the_subject(self):
        """Promoting aliases must not promote ordinary context with them."""
        c = self._decide("Ο ΟΠΑΠ πάλι δεν έδωσε τίποτα στην κλήρωση, κρίμα.")
        assert c["decision"] == "review"
        assert "core_term_missing" in c["reasons"]


class TestTheModelSeesTheParentPost:
    """The model must be shown the post a comment sits under — not its link.

    A comment record carries `parent_post` (a URL) and `parent_context` (the
    post's text). The model was handed the URL, so when it tried to work out
    what "πάλι τίποτα" was about it saw
    "https://www.facebook.com/…/posts/123" and nothing else. It answered
    `uncertain`, and the record went to the review queue instead of the report.
    """

    COMMENT = {
        "id": "comment:facebook:abc123",
        "platform": "facebook",
        "text": "Πάλι τίποτα, κάθε βδομάδα τα ίδια.",
        "content_type": "comment",
        "evidence_layer": "comment",
        "parent_post": "https://www.facebook.com/opapofficial/posts/123456789",
        "parent_context": "Eurojackpot: Τζακ ποτ 120 εκατ. ευρώ στην αποψινή κλήρωση.",
        "cleaning": {"relevance_score": 0.52, "content_class": "organic"},
    }

    def _payload(self, row):
        from app.services.ai_analysis import _record_for_model

        return _record_for_model(row)[0]

    def test_the_post_text_reaches_the_model(self):
        assert self._payload(self.COMMENT)["parent_context"] == self.COMMENT["parent_context"]

    def test_a_url_is_never_sent_as_context(self):
        """The regression itself: a link told the model nothing."""
        payload = self._payload(self.COMMENT)
        assert "facebook.com" not in str(payload["parent_context"])

    def test_a_comment_with_only_a_link_sends_no_context(self):
        row = dict(self.COMMENT)
        row.pop("parent_context")
        assert self._payload(row)["parent_context"] is None

    def test_a_plain_post_is_unaffected(self):
        row = {"id": "p1", "platform": "facebook", "text": "Κάτι λέμε", "cleaning": {}}
        assert self._payload(row)["parent_context"] is None

    def test_a_parent_supplied_as_an_object_still_works(self):
        row = dict(self.COMMENT)
        row.pop("parent_context")
        row["parent_post"] = {"text": "Το κείμενο της δημοσίευσης"}
        assert self._payload(row)["parent_context"] == "Το κείμενο της δημοσίευσης"

    def test_the_cache_notices_the_change(self):
        """Annotations cached from before the fix must not be reused."""
        from app.services.ai_analysis import _content_fingerprint

        ctx = {"client": "ΟΠΑΠ", "topic": "Eurojackpot"}
        blind = dict(self.COMMENT)
        blind.pop("parent_context")
        assert _content_fingerprint(self.COMMENT, ctx, "m", "bulk") != \
            _content_fingerprint(blind, ctx, "m", "bulk")
