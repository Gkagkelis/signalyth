"""Comments are first-class evidence with their own per-source target.

Before this, the comment layer silently received 35% of the POST target
(28 comments for a source asking for 80 posts) and was ranked purely by
engagement — which, for a brand that sponsors a league, buys match coverage
instead of customer conversation. These tests pin the new contract:

  * the operator's per-source comment number is what gets requested
  * enough parents are opened to actually reach it
  * parents are ranked by real conversation, not by loudness
  * operator-supplied URLs are collected first
"""
from __future__ import annotations

from app.models import AnalysisDraft
from app.services.relevance_expansion import (
    operator_seed_urls,
    parent_heat_score,
    _comment_seed_refs,
)


def _post(pid: str, *, comments=0, likes=0, relevance=0.8, content_class="organic",
          decision="trusted", organic=True, platform="facebook", origin_class="organic"):
    return {
        "platform": platform,
        "evidence_layer": "primary",
        "url": f"https://facebook.com/page/posts/{pid}",
        "text": f"post {pid}",
        "comments": comments,
        "likes": likes,
        "metric_availability": {"comments_known": True},
        "cleaning": {
            "decision": decision,
            "relevance_score": relevance,
            # v30.1: the open-parent gate checks the fields every real cleaned
            # row carries; a relevant row has a core_term hit and market score.
            "market_score": 0.6 if relevance > 0 else 0.0,
            "reasons": (["core_term:topic"] if relevance > 0 else []),
            "content_class": content_class,
            "origin_class": origin_class,
            "organic_eligible": organic,
        },
    }


class TestPerSourceCommentTarget:
    def test_draft_accepts_a_comment_target_per_source(self):
        draft = AnalysisDraft(
            client="Stoiximan", topic="Stoiximan", market="Greece",
            date_from="2026-09-14", date_to="2026-09-20",
            sources=["facebook", "x"], sample_mode="perSource",
            per_source={"facebook": 30, "x": 30},
            per_source_comments={"facebook": 300, "x": 200},
            comments=True,
        )
        assert draft.per_source_comments["facebook"] == 300
        assert draft.per_source_comments["x"] == 200

    def test_a_comment_only_run_is_valid(self):
        """Posts exist only to reach their comments: zero post target is legal."""
        draft = AnalysisDraft(
            client="B", topic="B", market="Greece",
            date_from="2026-09-14", date_to="2026-09-20",
            sources=["facebook"], sample_mode="perSource",
            per_source={"facebook": 0},
            per_source_comments={"facebook": 400},
            comments=True,
        )
        assert draft.per_source_comments["facebook"] == 400

    def test_negative_comment_target_is_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            AnalysisDraft(
                client="B", topic="B", market="Greece",
                date_from="2026-09-14", date_to="2026-09-20",
                sources=["facebook"], sample_mode="perSource",
                per_source={"facebook": 10},
                per_source_comments={"facebook": -5},
            )

    def test_plan_carries_the_comment_target(self):
        from app.services.query_planner import build_collection_plan
        draft = AnalysisDraft(
            client="Stoiximan", topic="Stoiximan", market="Greece",
            date_from="2026-09-14", date_to="2026-09-20",
            sources=["facebook"], sample_mode="perSource",
            per_source={"facebook": 30},
            per_source_comments={"facebook": 300},
            comments=True, smart_search=False,
            comment_seed_urls=["https://facebook.com/stoiximan/posts/999"],
        )
        plan = build_collection_plan(draft)
        assert plan.per_source_comments["facebook"] == 300
        assert plan.comment_target_total == 300
        assert plan.comment_seed_urls == ["https://facebook.com/stoiximan/posts/999"]


class TestParentRanking:
    def test_customer_complaint_outranks_a_louder_score_bulletin(self):
        """The bug that produced a 6-record report: a league bulletin with more
        engagement was a 'better' parent than a thread full of complaints."""
        complaint = _post("complaint", comments=120, likes=200, relevance=0.9,
                          content_class="organic")
        bulletin = _post("bulletin", comments=400, likes=3000, relevance=0.35,
                         content_class="announcement", organic=False,
                         origin_class="earned_media")
        assert parent_heat_score(complaint) > parent_heat_score(bulletin)

    def test_ranking_puts_the_conversation_first(self):
        rows = [
            _post("promo", comments=900, likes=5000, relevance=0.3,
                  content_class="promotional", organic=False),
            _post("talk", comments=60, likes=30, relevance=0.95),
        ]
        refs, meta, mode = _comment_seed_refs("facebook", rows, max_seeds=5)
        assert mode == "reported_comments"
        assert refs[0].endswith("/talk"), refs
        assert meta[0]["heat"] > meta[1]["heat"]


class TestOperatorSeeds:
    def test_urls_are_routed_to_their_own_platform(self):
        plan = {"comment_seed_urls": [
            "https://www.facebook.com/stoiximan/posts/1",
            "https://www.instagram.com/p/abc/",
            "https://x.com/user/status/5",
            "https://www.tiktok.com/@u/video/9",
        ]}
        assert operator_seed_urls(plan, "facebook") == ["https://www.facebook.com/stoiximan/posts/1"]
        assert operator_seed_urls(plan, "instagram") == ["https://www.instagram.com/p/abc/"]
        assert operator_seed_urls(plan, "x") == ["https://x.com/user/status/5"]
        assert operator_seed_urls(plan, "tiktok") == ["https://www.tiktok.com/@u/video/9"]

    def test_twitter_legacy_domain_counts_as_x(self):
        plan = {"comment_seed_urls": ["https://twitter.com/user/status/7"]}
        assert operator_seed_urls(plan, "x") == ["https://twitter.com/user/status/7"]

    def test_blank_and_duplicate_urls_are_dropped(self):
        plan = {"comment_seed_urls": [
            "https://facebook.com/a/posts/1", "  ", "https://facebook.com/a/posts/1", "",
        ]}
        assert operator_seed_urls(plan, "facebook") == ["https://facebook.com/a/posts/1"]

    def test_no_urls_for_a_source_returns_nothing(self):
        plan = {"comment_seed_urls": ["https://facebook.com/a/posts/1"]}
        assert operator_seed_urls(plan, "tiktok") == []


class TestPositiveCommentsSurvive:
    """Praise must reach the report exactly like criticism.

    A short "μπράβο παιδιά" under a brand post never names the brand, so the
    subject gate would drop it on its own. Parent context is what keeps it —
    and this pins that both praise and criticism survive, so the sentiment
    split measures the audience and not the filter.
    """

    PARENT = "Stoiximan: νέα προσφορά για τους παίκτες μας"

    def _comment(self, text, cid):
        return {
            "id": cid, "platform": "facebook", "evidence_layer": "comment",
            "parent_post": "p1", "parent_context": self.PARENT, "text": text,
            "author": f"user{cid}", "url": f"https://fb.com/page/posts/{cid}",
            "published_at": "2026-09-18T10:00:00Z", "likes": 5, "comments": 0,
            "raw_data": {}, "metric_availability": {"comments_known": True},
        }

    def _plan(self):
        return {
            "client": "Stoiximan", "topic": "Stoiximan", "market": "Greece",
            "core_terms": ["Stoiximan"], "context_terms": ["στοίχημα", "Ελλάδα"],
            "greeklish_variants": ["stoiximan"], "exclusions": [], "target_total": 100,
        }

    def test_praise_and_criticism_both_reach_semantic_analysis(self):
        from app.services.cleaning import clean_records
        praise = [
            "Τέλειοι είστε! Πάντα πληρώνετε στην ώρα σας",
            "Μπράβο παιδιά",
            "Η καλύτερη εφαρμογή, πολύ εύκολη στη χρήση",
            "Ευχαριστώ για την άμεση εξυπηρέτηση!",
        ]
        criticism = [
            "Απαράδεκτοι, 5 μέρες περιμένω ανάληψη",
            "ΑΠΑΤΕΩΝΕΣ μην τους εμπιστεύεστε",
        ]
        rows = [self._comment(t, str(i)) for i, t in enumerate([*praise, *criticism])]
        out = clean_records(rows, self._plan())

        # semantic_candidates is what the AI stage actually reads.
        analysed = {r["id"] for r in out["semantic_candidates"]}
        praise_ids = {str(i) for i in range(len(praise))}
        criticism_ids = {str(i) for i in range(len(praise), len(praise) + len(criticism))}

        assert praise_ids <= analysed, f"praise dropped: {praise_ids - analysed}"
        assert criticism_ids <= analysed, f"criticism dropped: {criticism_ids - analysed}"

    def test_parent_ranking_ignores_sentiment(self):
        """Choosing which posts to buy comments from must not prefer outrage."""
        angry = _post("angry", comments=100, likes=100, relevance=0.9)
        angry["text"] = "ΑΠΑΡΑΔΕΚΤΟΙ απατεώνες ντροπή σας"
        happy = _post("happy", comments=100, likes=100, relevance=0.9)
        happy["text"] = "Τέλειοι, ευχαριστώ για την εξυπηρέτηση"
        assert parent_heat_score(angry) == parent_heat_score(happy)
