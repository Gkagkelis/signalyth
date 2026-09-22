"""The whole chain, end to end: page → its posts → their comments.

Driven through the real `adaptive_expand_after_cleaning` with a fake Actor
runner, because the thing worth testing is the ORDER and the ARITHMETIC:
the operator's pages are served first up to their reserved share, open search
takes the rest, and anything open search cannot deliver comes back to the
pages instead of being written off.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.models import AnalysisDraft
from app.services.cleaning import clean_run
from app.services.normalizer import normalize_dataset
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import adaptive_expand_after_cleaning
from app.services.storage import RunStore

TOPIC = "Allwyn"


def _tweet(tweet_id, text, author, replies=0, day=16):
    return {
        "id": str(tweet_id), "text": text,
        "createdAt": f"Sun Aug {day:02d} 12:03:14 +0000 2026",
        "authorUsername": author, "replyCount": replies,
        "viewCount": 500, "likeCount": 20, "retweetCount": 1,
        "url": f"https://x.com/{author}/status/{tweet_id}", "type": "tweet",
    }


class FakeRunner:
    """Answers the three different questions the pipeline asks an Actor."""

    def __init__(self, *, profile_posts, replies_per_parent, search_items=None):
        self.calls = []
        self.profile_posts = list(profile_posts)
        self.replies_per_parent = dict(replies_per_parent)
        self.search_items = list(search_items or [])
        self._seq = 0

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls.append((actor_id, dict(run_input), max_items))
        meta = {"usageTotalUsd": min(0.01, max_charge_usd), "defaultDatasetId": "fake"}
        if run_input.get("mode") == "profile":
            return meta, self.profile_posts[:max_items]
        if run_input.get("mode") == "replies":
            out = []
            for parent in run_input.get("replyTweetIds") or []:
                for _ in range(int(self.replies_per_parent.get(str(parent), 0))):
                    if len(out) >= max_items:
                        break
                    self._seq += 1
                    out.append(_tweet(900000 + self._seq,
                                      f"Πάλι τίποτα, σχόλιο {self._seq}",
                                      f"person{self._seq}", day=17))
                if len(out) >= max_items:
                    break
            return meta, out
        return meta, self.search_items[:max_items]


def _prepare(tmp_path: Path, *, search_items, pages, comments_target, share):
    draft = AnalysisDraft(
        client=TOPIC, topic=TOPIC, market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=[TOPIC], sources=["x"], sample_mode="perSource",
        per_source={"x": 5}, per_source_comments={"x": comments_target},
        source_pages={"x": pages}, owned_share_pct=share,
        comments=True, max_budget_usd=5.0, smart_search=True,
        report_language="Ελληνικά", additional_context=["OPAP"], exclusions=[],
    )
    plan = build_collection_plan(draft).model_dump(mode="json")
    for row in (plan.get("preflight_forecast") or {}).get("sources", []):
        if row.get("source") == "x":
            row.setdefault("comments", {})["status"] = "verified_available"
            row["comments"]["live_verified"] = True
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "raw-x.json", search_items)
    rows = normalize_dataset("x", search_items)
    store.write(tmp_path / "normalized-x.json", rows)
    store.write(tmp_path / "normalized-all.json", rows)
    store.write(tmp_path / "status.json", {
        "run_id": "T", "budget": {"max_usd": 5.0, "spent_usd": 0.0, "remaining_usd": 5.0},
    })
    report = clean_run(tmp_path, plan=plan)
    return plan, report


#: Posts the operator's own page returns — these carry the real conversation.
OWNED_POSTS = [_tweet(10 + i, f"{TOPIC} τζακ ποτ ανακοίνωση {i}", "opapofficial", replies=50)
               for i in range(4)]


class TestTheOperatorsPagesComeFirst:
    def test_the_page_is_turned_into_posts_and_those_posts_into_comments(self, tmp_path):
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=["@opapofficial"],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=OWNED_POSTS,
                            replies_per_parent={**{str(p["id"]): 20 for p in OWNED_POSTS},
                                                "500": 20})
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
        audit = out["audit"]

        page = (audit.get("page_discovery") or {}).get("x") or {}
        assert page.get("posts_found") == len(OWNED_POSTS), audit.get("warnings")

        modes = [c[1].get("mode") for c in runner.calls]
        assert "profile" in modes, "the page must be asked for its posts"
        assert modes.index("profile") < modes.index("replies"), "posts before comments"

        first_reply_call = next(c for c in runner.calls if c[1].get("mode") == "replies")
        owned_ids = {str(p["id"]) for p in OWNED_POSTS}
        assert set(map(str, first_reply_call[1]["replyTweetIds"])) & owned_ids, \
            "the operator's own posts must be asked first"

    def test_the_reserved_share_is_respected(self, tmp_path):
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=["@opapofficial"],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=OWNED_POSTS,
                            replies_per_parent={**{str(p["id"]): 20 for p in OWNED_POSTS},
                                                "500": 20})
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
        buckets = (out["audit"].get("comment_buckets") or {}).get("x") or {}

        assert buckets["owned_quota"] == 6
        assert buckets["open_quota"] == 4
        assert buckets["owned"] == 6, buckets
        assert buckets["open"] == 4, buckets
        assert buckets["backfill"] == 0, "nothing was missing, so nothing to backfill"


class TestTheBucketIsNeverLeftHalfEmpty:
    def test_when_open_search_returns_nothing_the_pages_fill_the_gap(self, tmp_path):
        """The exact failure of the first Eurojackpot run, now recoverable."""
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=["@opapofficial"],
                                comments_target=10, share=60)
        runner = FakeRunner(
            profile_posts=OWNED_POSTS,
            replies_per_parent={**{str(p["id"]): 20 for p in OWNED_POSTS}, "500": 0},
        )
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
        buckets = (out["audit"].get("comment_buckets") or {}).get("x") or {}

        assert buckets["open"] == 0, "open search genuinely returned nothing"
        assert buckets["backfill"] == 4, buckets
        assert buckets["owned"] + buckets["backfill"] == 10, "the target was still met"

    def test_with_no_pages_given_it_behaves_exactly_as_before(self, tmp_path):
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=[],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=[], replies_per_parent={"500": 20})
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
        buckets = (out["audit"].get("comment_buckets") or {}).get("x") or {}

        assert not [c for c in runner.calls if c[1].get("mode") == "profile"], \
            "no pages given means no page call and no page cost"
        assert buckets["owned"] == 0
        assert buckets["open"] > 0, buckets


class TestTheEvidenceSaysWhereItCameFrom:
    def test_every_comment_is_labelled_owned_or_open(self, tmp_path):
        """Without this label the report cannot state the sampling split."""
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=["@opapofficial"],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=OWNED_POSTS,
                            replies_per_parent={**{str(p["id"]): 20 for p in OWNED_POSTS},
                                                "500": 20})
        adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

        rows = RunStore().read(tmp_path / "normalized-comments-x.json", []) or []
        assert rows, "comments were collected"
        origins = {str(r.get("evidence_origin") or "") for r in rows}
        assert origins <= {"owned", "open", "owned_backfill"}, origins
        assert "owned" in origins and "open" in origins, origins


class TestTheReportCanStateTheSplit:
    def test_the_cleaning_report_counts_both_origins(self, tmp_path):
        """Vangelis has to be able to answer "did you only look at my page?"."""
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=["@opapofficial"],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=OWNED_POSTS,
                            replies_per_parent={**{str(p["id"]): 20 for p in OWNED_POSTS},
                                                "500": 20})
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

        origin = (out["report"] or {}).get("sampling_origin") or {}
        assert origin["owned_pages"] == 6, origin
        assert origin["open_search"] == 4, origin
        assert origin["owned_share_pct"] == 60.0, origin

    def test_a_run_without_pages_reports_no_owned_share(self, tmp_path):
        found = [_tweet(500, f"{TOPIC} Greece κουβέντα", "person_a", replies=5)]
        plan, report = _prepare(tmp_path, search_items=found, pages=[],
                                comments_target=10, share=60)
        runner = FakeRunner(profile_posts=[], replies_per_parent={"500": 20})
        out = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

        origin = (out["report"] or {}).get("sampling_origin") or {}
        assert origin["owned_pages"] == 0
        assert origin["owned_share_pct"] == 0.0
