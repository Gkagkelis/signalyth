from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"Missing compatibility target: {label}")
    return text.replace(old, new, 1)


# 1) Seed selection: known-zero comment counts are skipped; unknown counts may be tried.
p = Path("app/services/relevance_expansion.py")
text = p.read_text(encoding="utf-8")
old = '''    for row in rows:\n        # Some discovery Actors do not expose an exact comment count. A relevant\n        # public parent URL is enough to try the bounded comment Actor.\n        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}\n'''
new = '''    for row in rows:\n        comments_n = int(row.get("comments", 0) or 0)\n        availability = row.get("metric_availability") if isinstance(row.get("metric_availability"), dict) else {}\n        comments_known = bool(availability.get("comments_known"))\n        # Do not pay for a parent the discovery Actor explicitly says has zero comments.\n        # If the count is unavailable, a bounded attempt is still allowed.\n        if comments_known and comments_n <= 0:\n            continue\n        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}\n'''
text = replace_once(text, old, new, "known-zero comment seed guard")
p.write_text(text, encoding="utf-8")


# 2) Update matrix expectation to the intentionally new production contract.
p = Path("tests/test_multisource_matrix_v182.py")
text = p.read_text(encoding="utf-8")
old = '''def test_comments_forecast_never_claims_unverified_full_coverage():\n    plan = build_collection_plan(draft(SOURCES, comments=True))\n    cc = plan.preflight_forecast["comments_coverage"]\n    assert cc["fully_live_verified"] is False\n    assert "facebook" in cc["verification_blockers"]\n    assert "tiktok" in cc["verification_blockers"]\n    assert "youtube" in cc["verification_blockers"]\n    rows = {r["source"]: r for r in plan.preflight_forecast["sources"]}\n    assert rows["news"]["comments"]["status"] == "not_applicable"\n    assert rows["x"]["comments"]["status"] == "available_but_unverified"\n'''
new = '''def test_comments_forecast_distinguishes_operational_readiness_from_live_confirmation():\n    plan = build_collection_plan(draft(SOURCES, comments=True))\n    cc = plan.preflight_forecast["comments_coverage"]\n    assert cc["operationally_ready"] is True\n    assert cc["fully_live_verified"] is False\n    assert cc["verification_blockers"] == []\n    rows = {r["source"]: r for r in plan.preflight_forecast["sources"]}\n    for source in ("x", "tiktok", "instagram", "facebook"):\n        assert rows[source]["comments"]["status"] in {"configured_available", "verified_available"}\n    assert rows["youtube"]["comments"]["status"] == "not_applicable"\n    assert rows["news"]["comments"]["status"] == "not_applicable"\n'''
text = replace_once(text, old, new, "matrix production comment readiness test")
p.write_text(text, encoding="utf-8")


# 3) A configured production X contract must run without a separate smoke prerequisite.
p = Path("tests/test_relevance_expansion_v2.py")
text = p.read_text(encoding="utf-8")
old = '''def test_reply_deepening_is_blocked_until_route_live_verified(tmp_path):\n    initial = [raw(800, "Allwyn Greece customer experience", "person1", replies=5)]\n    plan, report = prepare(tmp_path, initial, target=2, comments=True, verify_reply_route=False)\n    runner = FakeRunner(reply_items=[raw(801, "Allwyn Greece reply", "person2", day=18)])\n    result = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)\n    assert runner.calls == []\n    assert "reply_deepening_blocked_until_live_route_verification" in result["audit"]["warnings"]\n    assert result["report"]["trusted_sample_shortfall"] == 1\n'''
new = '''def test_reply_deepening_runs_from_configured_production_contract_without_separate_smoke(tmp_path):\n    initial = [raw(800, "Allwyn Greece customer experience", "person1", replies=5)]\n    plan, report = prepare(tmp_path, initial, target=2, comments=True, verify_reply_route=False)\n    runner = FakeRunner(reply_items=[{\n        **raw(801, "Allwyn Greece reply", "person2", day=18),\n        "type": "reply", "inReplyToId": "800",\n    }])\n    result = adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)\n    assert runner.calls and runner.calls[0][1]["mode"] == "replies"\n    assert runner.calls[0][1]["replyTweetIds"] == ["800"]\n    assert "x:comment_deepening_not_operational" not in result["audit"]["warnings"]\n    assert result["report"]["trusted_sample_shortfall"] == 0\n'''
text = replace_once(text, old, new, "configured production X deepening test")
p.write_text(text, encoding="utf-8")

print("Production comment compatibility updates applied.")
