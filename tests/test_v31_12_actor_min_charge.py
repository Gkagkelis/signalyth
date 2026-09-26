"""v31.12 — Actors with a minimum run charge must not fail whole routes.

Run 20260926T112403Z (NBG): clockworks/tiktok-scraper refuses to start below
$0.50 and apify/facebook-search-scraper below $0.024. The collector's caps,
computed from expected item cost alone, sat below those minimums, both routes
failed with "Maximum cost per run is less than the allowed minimum", and the
circuit breaker rightly skipped the rest — two of six sources collected
nothing. The minimum is a CAP, not a charge: Apify bills only what runs use,
so lifting the cap to the Actor's floor costs nothing extra.
"""
from __future__ import annotations

from app.services.collector import apply_actor_min_charge
from app.services.relevance_expansion import (
    _comment_minimum_attempt_charge_usd,
    _probe_items,
)
from app.services.storage import RunStore


def test_route_caps_are_lifted_to_the_actor_minimum_when_affordable():
    # The exact production shapes: a $0.0208 cap against clockworks' $0.50.
    assert apply_actor_min_charge(0.0208, 0.50, headroom=4.9) == 0.50
    assert apply_actor_min_charge(0.010, 0.024, headroom=4.9) == 0.024
    # Already above the minimum: untouched.
    assert apply_actor_min_charge(0.80, 0.50, headroom=4.9) == 0.80
    # No minimum: untouched.
    assert apply_actor_min_charge(0.0208, 0.0, headroom=4.9) == 0.0208
    # The minimum does not fit in the remaining budget: skip, never overspend.
    assert apply_actor_min_charge(0.0208, 0.50, headroom=0.10) == 0.0


def test_comment_minimum_combines_event_floor_and_actor_floor():
    # A non-TikTok source with an actor floor gets exactly that floor.
    assert _comment_minimum_attempt_charge_usd("facebook", {"postUrls": ["u"]}, 20, 0.4,
                                               actor_min_charge_usd=0.25) == 0.25
    # TikTok keeps its event arithmetic but the larger actor floor wins.
    ev = _comment_minimum_attempt_charge_usd(
        "tiktok", {"startUrls": ["a", "b"], "includeReplies": True}, 10, 0.3)
    assert 0 < ev < 0.5
    assert _comment_minimum_attempt_charge_usd(
        "tiktok", {"startUrls": ["a", "b"], "includeReplies": True}, 10, 0.3,
        actor_min_charge_usd=0.5) == 0.5


class CapturingRunner:
    def __init__(self):
        self.caps = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.caps.append(max_charge_usd)
        return {"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, [{"id": "1"}]


def _probe_fixture(tmp_path, remaining):
    store = RunStore()
    store.write(tmp_path / "status.json", {
        "run_id": "T",
        "budget": {"max_usd": 5.0, "spent_usd": round(5.0 - remaining, 6),
                   "remaining_usd": remaining},
    })
    plan = {"max_budget_usd": 5.0}
    audit = {"warnings": [], "steps": []}
    return store, plan, audit


def test_probe_caps_are_lifted_to_the_actor_minimum(tmp_path):
    store, plan, audit = _probe_fixture(tmp_path, remaining=4.9)
    runner = CapturingRunner()
    rows = _probe_items(tmp_path, plan, "tiktok", "clockworks/tiktok-scraper",
                        {"searchQueries": ["x"]}, 10, 1.7, audit, store, runner,
                        cancel_check=lambda: False, min_charge_usd=0.5)
    assert rows and runner.caps and runner.caps[0] >= 0.5


def test_probe_is_skipped_when_the_minimum_does_not_fit(tmp_path):
    store, plan, audit = _probe_fixture(tmp_path, remaining=0.10)
    runner = CapturingRunner()
    rows = _probe_items(tmp_path, plan, "tiktok", "clockworks/tiktok-scraper",
                        {"searchQueries": ["x"]}, 10, 1.7, audit, store, runner,
                        cancel_check=lambda: False, min_charge_usd=0.5)
    assert rows == [] and not runner.caps
    assert any("minimum_actor_charge_exceeds_remaining_budget" in w for w in audit["warnings"])


def test_baseline_registry_carries_the_known_minimums():
    import json
    from pathlib import Path
    d = json.loads(Path("config/source_registry.json").read_text())
    assert d["tiktok"]["price_min_charge_usd"] == 0.5
    assert d["tiktok"]["comment_price_min_charge_usd"] == 0.5
    assert d["facebook"]["price_min_charge_usd"] == 0.03


def test_patch_model_carries_the_minimum_charge_fields():
    # The API's PATCH body model silently drops unknown fields, which is how
    # run 20260926T121905Z lost the minimums a second time. Pin them here.
    from app.models import SourceConfigUpdate
    upd = SourceConfigUpdate(price_min_charge_usd=0.5, comment_price_min_charge_usd=0.5)
    dumped = upd.model_dump(exclude_none=True)
    assert dumped == {"price_min_charge_usd": 0.5, "comment_price_min_charge_usd": 0.5}
