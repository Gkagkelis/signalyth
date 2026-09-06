from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import AnalysisDraft
from app.services.collector import _resize_input
from app.services.query_planner import build_collection_plan
from app.services.source_capabilities import build_comment_smoke_input, comments_forecast
import app.registry as R

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]


def _draft(*, sources=SOURCES, target=1000, budget=1.0):
    return AnalysisDraft(
        client="Internal Team", topic="Allwyn", market="Greece",
        date_from=date(2026,8,15), date_to=date(2026,8,31),
        keywords=["Allwyn", "OPAP"], sources=list(sources), sample_mode="automatic",
        sample_target=target, comments=True, max_budget_usd=budget,
        smart_search=True, report_language="Ελληνικά",
    )


@pytest.mark.parametrize("budget", [0.001, 0.0001, 0.00001, 0.000001])
def test_ultra_tiny_global_budget_is_never_inflated(budget):
    plan = build_collection_plan(_draft(budget=budget, target=1000))
    caps = sum(sr.max_charge_usd for sp in plan.sources for sr in sp.subruns)
    assert caps <= budget + 1e-12
    assert all(sr.max_charge_usd >= 0 for sp in plan.sources for sr in sp.subruns)


def test_facebook_planner_input_can_cover_its_logical_target():
    plan = build_collection_plan(_draft(sources=["facebook"], target=1000, budget=10))
    sp = plan.sources[0]
    assert sum(sr.target_items for sr in sp.subruns) == 1000
    assert all(int(sr.input.get("resultsCount", 0)) >= sr.target_items for sr in sp.subruns)


def test_facebook_rebalance_resize_does_not_reintroduce_200_cap():
    out = _resize_input("facebook", {"query":"Allwyn", "resultsCount":250}, 250, 600)
    assert out["resultsCount"] == 600


def test_comment_smoke_inputs_are_fail_closed_and_source_specific():
    x = build_comment_smoke_input("x", ["12345"], 3)
    assert x == {"replyTweetIds":["12345"], "mode":"replies", "maxItems":3}
    fb = build_comment_smoke_input("facebook", ["https://facebook.test/post/1"], 3)
    assert fb["postUrls"] == ["https://facebook.test/post/1"]
    assert "mode" not in fb
    with pytest.raises(ValueError, match="not applicable"):
        build_comment_smoke_input("news", ["https://news.test/1"], 3)


def test_registry_comment_route_verification_is_separate_and_reversible(tmp_path, monkeypatch):
    reg = tmp_path / "source_registry.json"
    hist = tmp_path / "source_registry_history.json"
    reg.write_text(Path("config/source_registry.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(R, "REGISTRY_PATH", reg)
    monkeypatch.setattr(R, "HISTORY_PATH", hist)
    before = R.load_registry()["x"]
    assert comments_forecast("x", True, before)["status"] != "verified_available"
    saved = R.commit_comment_route_verification(
        "x", actor_id="xquik/x-tweet-scraper", smoke_tested_at="2026-09-05T10:00:00+00:00",
        route="replies", input_field="replyTweetIds", output_mapping={"text":"text"},
    )
    assert saved["comment_deepening_status"] == "verified"
    assert comments_forecast("x", True, saved)["status"] == "verified_available"
    cleared = R.clear_comment_route_verification("x")
    assert cleared["comment_deepening_status"] == "unverified"
    assert comments_forecast("x", True, cleared)["status"] != "verified_available"


def test_comment_route_endpoint_requires_explicit_paid_confirmation():
    client = TestClient(app)
    r = client.post("/api/sources/x/comments/smoke", json={"seed_refs":["123"], "confirm_paid_smoke_test":False})
    assert r.status_code == 422
    assert "Explicit" in r.json()["detail"]


def test_comment_route_endpoint_only_commits_after_clean_paid_smoke(monkeypatch, tmp_path):
    reg = tmp_path / "source_registry.json"
    hist = tmp_path / "source_registry_history.json"
    reg.write_text(Path("config/source_registry.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(R, "REGISTRY_PATH", reg)
    monkeypatch.setattr(R, "HISTORY_PATH", hist)
    # main imported these functions, so patch the names used by the endpoint.
    monkeypatch.setattr("app.main.public_registry", R.public_registry)
    monkeypatch.setattr("app.main.commit_comment_route_verification", R.commit_comment_route_verification)
    monkeypatch.setattr("app.main.validate_actor_input", lambda actor_id, run_input: {"ok":True,"actor_id":actor_id,"validated_at":"now"})
    monkeypatch.setattr("app.main.smoke_test_actor", lambda *a, **k: {
        "ok":True,"actor_id":"xquik/x-tweet-scraper","run_id":"r1","run_status":"SUCCEEDED",
        "sample_count":1,"diagnostic_count":0,"tested_at":"2026-09-05T10:00:00+00:00",
        "output_mapping":{"mapping":{"text":"text","date":"createdAt","url":"url"}},
        "sample_items":[{"text":"reply"}],
    })
    client = TestClient(app)
    r = client.post("/api/sources/x/comments/smoke", json={
        "seed_refs":["123"], "max_items":3, "max_charge_usd":0.1, "confirm_paid_smoke_test":True
    })
    assert r.status_code == 200, r.text
    assert r.json()["comment_deepening_status"] == "verified"
    assert "sample_items" not in r.json()["smoke_test"]
    assert R.load_registry()["x"]["comment_deepening_status"] == "verified"

def _declared_capacity(source: str, inp: dict) -> int | None:
    if source == "x": return int(inp.get("maxItems", 0) or 0)
    if source == "tiktok": return int(inp.get("maxItems", 0) or 0)
    if source == "instagram": return int(inp.get("resultsLimit", 0) or 0)
    if source == "facebook": return int(inp.get("resultsCount", 0) or 0)
    if source == "youtube": return int(inp.get("maxItems", 0) or 0)
    if source == "news": return int(inp.get("maxArticles", 0) or 0) * max(1, len(inp.get("queries") or []))
    return None


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("target", [1, 7, 100, 1000, 3000])
def test_default_actor_subrun_inputs_have_capacity_for_logical_targets(source, target):
    plan = build_collection_plan(_draft(sources=[source], target=target, budget=100))
    for sr in plan.sources[0].subruns:
        cap = _declared_capacity(source, sr.input)
        assert cap is not None and cap >= sr.target_items, (source, target, sr.target_items, sr.input)
