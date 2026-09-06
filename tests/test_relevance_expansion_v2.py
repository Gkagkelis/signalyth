from datetime import date
from pathlib import Path

from app.models import AnalysisDraft
from app.services.cleaning import clean_run
from app.services.normalizer import normalize_dataset
from app.services.query_planner import build_collection_plan
from app.services.relevance_expansion import adaptive_expand_after_cleaning
from app.services.storage import RunStore


class FakeRunner:
    def __init__(self, search_items=None, reply_items=None):
        self.calls = []
        self.search_items = list(search_items or [])
        self.reply_items = list(reply_items or [])

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls.append((actor_id, dict(run_input), max_items, max_charge_usd))
        items = self.reply_items if run_input.get("mode") == "replies" else self.search_items
        return {"usageTotalUsd": min(0.01, max_charge_usd), "defaultDatasetId": "fake"}, items[:max_items]


def draft(target=5, comments=True):
    return AnalysisDraft(
        client="Allwyn", topic="Allwyn", market="Greece",
        date_from=date(2026, 8, 15), date_to=date(2026, 8, 31),
        keywords=["Allwyn"], sources=["x"], sample_mode="automatic", sample_target=target,
        comments=comments, max_budget_usd=1.0, smart_search=True, report_language="Ελληνικά",
        additional_context=["OPAP"], exclusions=[],
    )


def raw(tweet_id, text, author, replies=0, day=16):
    return {
        "id": str(tweet_id), "text": text,
        "createdAt": f"Sun Aug {day:02d} 12:03:14 +0000 2026",
        "authorUsername": author, "replyCount": replies,
        "viewCount": 100, "likeCount": 2, "retweetCount": 0,
        "url": f"https://x.com/{author}/status/{tweet_id}", "type": "tweet",
    }


def prepare(tmp_path: Path, items: list[dict], target=5, comments=True, verify_reply_route=True):
    plan = build_collection_plan(draft(target=target, comments=comments)).model_dump(mode="json")
    if comments and verify_reply_route:
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") == "x":
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
    store = RunStore()
    store.write(tmp_path / "plan.json", plan)
    store.write(tmp_path / "raw-x.json", items)
    rows = normalize_dataset("x", items)
    store.write(tmp_path / "normalized-x.json", rows)
    store.write(tmp_path / "normalized-all.json", rows)
    store.write(tmp_path / "status.json", {
        "run_id": "T", "budget": {"max_usd": 1.0, "spent_usd": 0.05, "remaining_usd": 0.95}
    })
    report = clean_run(tmp_path, plan=plan)
    return plan, report


def test_adaptive_refinement_splits_dominant_context_and_recleans(tmp_path):
    initial = [
        raw(100, "Allwyn Arena Greece match report", "media1", replies=1),
        raw(101, "Allwyn Arena Greece match report two", "media2", replies=1),
        raw(102, "Allwyn Arena Greece match report three", "media3", replies=1),
        raw(200, "Allwyn OPAP Greece logo change review", "person1", replies=1, day=17),
    ]
    plan, report = prepare(tmp_path, initial, target=5, comments=True)
    assert report["trusted_sample_shortfall"] == 1
    runner = FakeRunner(search_items=[raw(300, "Allwyn OPAP Greece customer experience", "person2", day=18)])
    result = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    assert runner.calls and runner.calls[0][1]["mode"] == "search"
    assert any("-arena" in q.lower() for q in runner.calls[0][1]["searchTerms"])
    assert any("allwyn arena" in q.lower() and "-arena" not in q.lower() for q in runner.calls[0][1]["searchTerms"])
    assert result["report"]["trusted_sample_shortfall"] == 0
    assert result["audit"]["status"] == "target_met"
    assert (tmp_path / "smart-collection-expansion.json").exists()


def test_reply_deepening_runs_when_comments_on_and_no_collision(tmp_path):
    initial = [
        raw(400, "Allwyn OPAP Greece customer experience", "person1", replies=3),
        raw(401, "Allwyn Greece sponsorship discussion", "person2", replies=0),
    ]
    plan, report = prepare(tmp_path, initial, target=3, comments=True)
    assert report["trusted_sample_shortfall"] == 1
    runner = FakeRunner(reply_items=[{
        **raw(500, "I do not like this Allwyn change Greece", "replyuser", replies=0, day=18),
        "type": "reply", "inReplyToId": "400",
    }])
    result = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    assert runner.calls and runner.calls[0][1]["mode"] == "replies"
    assert runner.calls[0][1]["replyTweetIds"] == ["400"]
    assert result["report"]["trusted_sample_shortfall"] == 0


def test_adaptive_expansion_never_exceeds_remaining_budget(tmp_path):
    initial = [raw(600, "Allwyn Greece customer experience", "person1", replies=100)]
    plan, report = prepare(tmp_path, initial, target=1000, comments=True)
    store = RunStore()
    status = store.read(tmp_path / "status.json")
    status["budget"] = {"max_usd": 0.051, "spent_usd": 0.05, "remaining_usd": 0.001}
    store.write(tmp_path / "status.json", status)
    runner = FakeRunner(reply_items=[raw(700, "Allwyn Greece reply", "p2")])
    adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    # $0.15 / 1000 at $0.001 remaining permits at most 6 items.
    assert runner.calls[0][2] <= 6
    assert runner.calls[0][3] <= 0.001 + 1e-9


def test_reply_deepening_is_blocked_until_route_live_verified(tmp_path):
    initial = [raw(800, "Allwyn Greece customer experience", "person1", replies=5)]
    plan, report = prepare(tmp_path, initial, target=2, comments=True, verify_reply_route=False)
    runner = FakeRunner(reply_items=[raw(801, "Allwyn Greece reply", "person2", day=18)])
    result = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)
    assert runner.calls == []
    assert "reply_deepening_blocked_until_live_route_verification" in result["audit"]["warnings"]
    assert result["report"]["trusted_sample_shortfall"] == 1
