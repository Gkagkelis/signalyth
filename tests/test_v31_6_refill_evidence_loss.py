"""v31.6 — the semantic refill must never delete paid evidence.

Run 20260925T130636Z-c55323ad reached the finish line (analysis, intelligence,
charts all succeeded) and then FAILED at exports with "collected 75 records but
only 69 are in this workspace". Nothing was actually lost by the worker
replacement: the semantic refill had rewritten normalized-facebook.json from
raw and truncated the WHOLE rebuilt file to 2x target (48 -> 40 rows), deleting
six already-paid records — while the status sample counter kept saying 75, so
the evidence guard read the honest workspace as data loss.

Contracts:
1. The refill's candidate-headroom cap applies only to the NEW rows the refill
   call bought; already-collected evidence is never dropped.
2. After the refill rebuilds normalized-all, the status counter matches it.
3. A full reprocess reconciles a stale counter left behind by the old bug, so
   the operator can rebuild the report from the evidence that exists.
"""
from __future__ import annotations

from pathlib import Path

from app.services.semantic_refill import semantic_refill
from app.services.storage import RunStore


def _raw(tweet_id, day=16):
    return {
        "id": str(tweet_id), "text": f"Allwyn Greece discussion {tweet_id}",
        "createdAt": f"Sun Aug {day:02d} 12:03:14 +0000 2026",
        "authorUsername": f"user{tweet_id}", "replyCount": 0,
        "viewCount": 100, "likeCount": 2, "retweetCount": 0,
        "url": f"https://x.com/user{tweet_id}/status/{tweet_id}", "type": "tweet",
    }


def _plan():
    return {
        "date_from": "2026-08-15", "date_to": "2026-08-31", "target_total": 60,
        "sources": [{
            "source": "x", "target_items": 20, "price_per_1000_hint": 0.15,
            "topup_subruns": [{
                "actor_id": "xquik/x-tweet-scraper", "purpose": "topup_1",
                "input": {"mode": "search", "searchTerms": ["Allwyn"], "maxItems": 10},
                "target_items": 10, "max_charge_usd": 0.05,
            }],
        }],
    }


class FakeRunner:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.calls.append((actor_id, dict(run_input)))
        return {"usageTotalUsd": 0.001, "defaultDatasetId": "d"}, self.items[:max_items]


def test_refill_keeps_every_already_collected_record_and_reconciles_the_counter(tmp_path: Path):
    store = RunStore()
    plan = _plan()
    # 48 already-collected, already-paid records: more than the 2x-target (40)
    # cap that used to truncate the rebuilt file.
    existing_raw = [_raw(i) for i in range(1, 49)]
    from app.services.normalizer import normalize_dataset
    existing = normalize_dataset("x", existing_raw)
    assert len(existing) == 48
    store.write(tmp_path / "raw-x.json", existing_raw)
    store.write(tmp_path / "normalized-x.json", existing)
    store.write(tmp_path / "normalized-all.json", existing)
    store.write(tmp_path / "plan.json", plan)
    (tmp_path / "analysis").mkdir()
    store.write(tmp_path / "analysis" / "analysis-ready.json", [])
    # The stale counter the production run died with: bigger than the file.
    store.write(tmp_path / "status.json", {
        "run_id": "T", "normalized_total": 75,
        "budget": {"max_usd": 5.0, "spent_usd": 0.0, "remaining_usd": 5.0},
    })

    runner = FakeRunner([_raw(i, day=18) for i in (100, 101, 102)])
    audit = semantic_refill(tmp_path, plan, runner=runner)
    assert runner.calls, "the refill route never ran"

    after = store.read(tmp_path / "normalized-x.json", []) or []
    after_ids = {str(r.get("id")) for r in after}
    # Every one of the 48 paid records survives; only NEW rows are capped.
    missing = {str(i) for i in range(1, 49)} - after_ids
    assert not missing, f"the refill dropped paid records: {sorted(missing)}"
    assert {"100", "101", "102"} <= after_ids
    assert audit["added_normalized"] == 3

    normalized_all = store.read(tmp_path / "normalized-all.json", []) or []
    assert len(normalized_all) == 51
    status = store.read(tmp_path / "status.json", {}) or {}
    # The counter now describes the file it claims to count.
    assert int(status.get("normalized_total")) == 51


def test_full_reprocess_reconciles_a_stale_counter_so_the_evidence_guard_passes(tmp_path: Path):
    store = RunStore()
    store.root = tmp_path
    run_id, folder = store.create({"sources": [], "max_budget_usd": 5.0, "target_total": 60})
    rows = [{"id": str(i), "platform": "facebook"} for i in range(69)]
    store.write(folder / "normalized-all.json", rows)
    status = store.read_status(run_id)
    status["normalized_total"] = 75
    store.write_status(run_id, status)

    # An ordinary resume must STILL stop: a shrunken workspace may be real loss.
    ok, why = store.evidence_intact(run_id)
    assert not ok and "69" in why

    # The operator's explicit rebuild reconciles the counter with what exists...
    assert store.reconcile_normalized_total(folder) == 69
    assert int(store.read_status(run_id).get("normalized_total")) == 69
    # ...and the guard then lets the rebuilt report proceed.
    ok, why = store.evidence_intact(run_id)
    assert ok, why


def test_reconcile_is_a_noop_when_the_counter_is_already_honest(tmp_path: Path):
    store = RunStore()
    store.root = tmp_path
    run_id, folder = store.create({"sources": [], "max_budget_usd": 5.0, "target_total": 60})
    store.write(folder / "normalized-all.json", [{"id": "1"}])
    status = store.read_status(run_id)
    status["normalized_total"] = 1
    store.write_status(run_id, status)
    stamp_before = store.read_status(run_id).get("updated_at")
    assert store.reconcile_normalized_total(folder) == 1
    assert int(store.read_status(run_id).get("normalized_total")) == 1
    assert store.read_status(run_id).get("updated_at") == stamp_before
