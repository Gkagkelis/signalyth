from __future__ import annotations
import json
from pathlib import Path
import pytest
from app import registry as R


def seed(tmp_path, *, locked=True):
    reg = tmp_path / "source_registry.json"
    hist = tmp_path / "history.json"
    data = {"x": {
        "label":"X", "actor_id":"old/actor", "default_actor_id":"old/actor",
        "locked":locked, "enabled":True, "price_per_1000_hint":1.0,
        "date_support":"native_exact", "market_support":"query",
        "adapter_mode":"legacy", "actor_status":"legacy_default_unverified",
        "last_verified_at":None, "last_smoke_test_at":None, "actor_metadata":{},
        "input_mapping":{}, "input_schema_fields":{}, "input_template":{},
        "output_mapping":{}, "mapping_confidence":None, "mapping_signature":None,
    }}
    reg.write_text(json.dumps(data))
    return reg, hist


def test_manual_actor_change_requires_unlock_and_clears_verification(tmp_path, monkeypatch):
    reg, hist = seed(tmp_path, locked=True)
    monkeypatch.setattr(R, "REGISTRY_PATH", reg); monkeypatch.setattr(R, "HISTORY_PATH", hist)
    with pytest.raises(PermissionError): R.update_source("x", {"actor_id":"new/actor"})
    R.update_source("x", {"locked":False})
    out = R.update_source("x", {"actor_id":"new/actor"})
    assert out["actor_id"] == "new/actor"
    assert out["actor_status"] == "unverified"
    assert out["output_mapping"] == {}
    assert R.source_history("x")


def test_commit_requires_smoke_and_core_mapping(tmp_path, monkeypatch):
    reg, hist = seed(tmp_path, locked=False)
    monkeypatch.setattr(R, "REGISTRY_PATH", reg); monkeypatch.setattr(R, "HISTORY_PATH", hist)
    kw = dict(source="x", actor_id="new/actor", actor_metadata={}, input_mapping={"query":"q"},
              input_schema_fields={"q":{"type":"string"}}, input_template={"q":"x"},
              output_mapping={"text":"text","date":"date","url":"url"}, mapping_confidence=.9,
              verified_at="2026-09-04T10:00:00+00:00", smoke_tested_at="2026-09-04T10:01:00+00:00")
    bad = dict(kw); bad["smoke_tested_at"] = ""
    with pytest.raises(ValueError): R.commit_actor_configuration(**bad)
    bad = dict(kw); bad["output_mapping"] = {"text":"text","date":"date"}
    with pytest.raises(ValueError): R.commit_actor_configuration(**bad)
    out = R.commit_actor_configuration(**kw)
    assert out["locked"] is True and out["actor_status"] == "verified"
    assert out["adapter_mode"] == "generic"
    assert out["output_mapping"]["url"] == "url"
    assert len(out["mapping_signature"]) == 16


def test_rollback_restores_previous_config(tmp_path, monkeypatch):
    reg, hist = seed(tmp_path, locked=False)
    monkeypatch.setattr(R, "REGISTRY_PATH", reg); monkeypatch.setattr(R, "HISTORY_PATH", hist)
    R.update_source("x", {"actor_id":"new/actor"})
    assert R.load_registry()["x"]["actor_id"] == "new/actor"
    restored = R.rollback_source("x")
    assert restored["actor_id"] == "old/actor"
    assert restored["locked"] is True
