"""v31.9 — the comment layer must not re-clean the whole run after every call.

clean_run() re-scores the entire collected dataset and takes tens of seconds;
it used to run after EVERY paid comment call, so a 10-call comment layer paid
the full cleaning cost ten times over. Comment progress is measured from the
normalized-comments file, not from the cleaning report, so one cleaning pass
per finished source keeps the report exactly as correct while removing the
dominant non-Apify share of the layer's wall time. Primary-evidence calls
(semantic refill, adaptive refinement) still re-clean per call, because they
steer by the refreshed trusted shortfall.
"""
from __future__ import annotations

import app.services.relevance_expansion as rx
from app.services.storage import RunStore

from tests.test_v31_5_per_call_checkpoint import HarvestRunner, _prepare


def test_cleaning_runs_once_per_source_not_after_every_comment_call(tmp_path, monkeypatch):
    plan, report, store = _prepare(tmp_path)
    cleans: list[int] = []
    real_clean = rx.clean_run
    monkeypatch.setattr(
        rx, "clean_run",
        lambda *a, **k: (cleans.append(1) or real_clean(*a, **k)),
    )

    runner = HarvestRunner("9")
    result = rx.adaptive_expand_after_cleaning(tmp_path, plan, report, runner=runner)

    reply_calls = sum(1 for _, inp in runner.calls if inp.get("mode") == "replies")
    assert reply_calls >= 2, f"expected multiple paid comment calls, got {reply_calls}"
    assert len(cleans) == 1, (
        f"cleaning ran {len(cleans)} times for one source's comment layer; "
        "it must run once, after the source finishes"
    )

    # The comments were still filed and still entered the final report.
    comments = store.read(tmp_path / "normalized-comments-x.json", []) or []
    assert len(comments) >= 6
    row = ((store.read(tmp_path / "status.json", {}) or {})
           .get("comment_deepening") or {}).get("x") or {}
    assert row.get("collected") == len(comments)
    assert result["audit"]["comment_evidence_total"] == len(comments)
