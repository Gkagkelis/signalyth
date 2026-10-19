"""v31.37 — the comment layer stops being a black box.

A route can be refused by the Actor on every call and the run screen says only
"shortfall"; the reason is written to smart-collection-expansion.json in the
run folder and no endpoint ever exposed it. Run 20261019T170033Z spent its
whole $3 envelope on refused calls and nothing in the API could say so.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture()
def client():
    return TestClient(main.app)


def _make_run(tmp_plan: dict) -> str:
    run_id, folder = main.store.create(tmp_plan)
    return run_id


def test_unknown_run_is_a_404(client):
    assert client.get("/api/runs/does-not-exist/diagnostics").status_code == 404


def test_it_returns_the_plan_the_run_was_started_from(client):
    run_id = _make_run({"client": "ΚΠΙΣΝ", "topic": "ΚΠΙΣΝ", "sources": []})
    body = client.get(f"/api/runs/{run_id}/diagnostics").json()
    assert body["run_id"] == run_id
    assert body["plan"]["client"] == "ΚΠΙΣΝ"


def test_it_surfaces_the_expansion_warnings(client):
    run_id = _make_run({"client": "ΚΠΙΣΝ", "topic": "ΚΠΙΣΝ", "sources": []})
    folder = main.store.folder_for(run_id)
    main.store.write(folder / "smart-collection-expansion.json", {
        "warnings": ["facebook:comment_deepening:actor_failed:Input is not valid"],
        "steps": [{"noise": "x"} for _ in range(200)],
    })
    body = client.get(f"/api/runs/{run_id}/diagnostics").json()
    assert body["expansion_audit"]["warnings"] == [
        "facebook:comment_deepening:actor_failed:Input is not valid"
    ]


def test_the_long_step_log_is_not_dumped(client):
    run_id = _make_run({"client": "ΚΠΙΣΝ", "topic": "ΚΠΙΣΝ", "sources": []})
    folder = main.store.folder_for(run_id)
    main.store.write(folder / "smart-collection-expansion.json", {
        "warnings": [], "steps": [{"noise": "x"} for _ in range(200)],
    })
    body = client.get(f"/api/runs/{run_id}/diagnostics").json()
    assert "steps" not in body["expansion_audit"]


def test_a_run_without_an_audit_still_answers(client):
    run_id = _make_run({"client": "ΚΠΙΣΝ", "topic": "ΚΠΙΣΝ", "sources": []})
    body = client.get(f"/api/runs/{run_id}/diagnostics").json()
    assert body["expansion_audit"] == {}


def test_no_credential_is_echoed(client):
    run_id = _make_run({"client": "ΚΠΙΣΝ", "topic": "ΚΠΙΣΝ", "sources": []})
    raw = json.dumps(client.get(f"/api/runs/{run_id}/diagnostics").json()).casefold()
    for secret in ("apify_token", "openai_api_key", "api_key", "token"):
        assert secret not in raw
