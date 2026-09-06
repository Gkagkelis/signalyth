from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from app.config import BASE_DIR, settings
from app.services.cloud_persistence import cloud_persistence

RUNTIME_CONFIG_DIR = Path(settings.signalyth_runtime_config_dir)
REGISTRY_PATH = RUNTIME_CONFIG_DIR / "source_registry.json"
HISTORY_PATH = RUNTIME_CONFIG_DIR / "source_registry_history.json"
BASE_REGISTRY_PATH = BASE_DIR / "config" / "source_registry.json"
BASE_HISTORY_PATH = BASE_DIR / "config" / "source_registry_history.json"


def _ensure_runtime_file(path: Path, baseline: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if cloud_persistence.enabled and cloud_persistence.restore_config_file(path.name, path):
        return
    if not path.exists() and baseline.exists():
        path.write_bytes(baseline.read_bytes())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)
    if path.parent == RUNTIME_CONFIG_DIR:
        cloud_persistence.put_config_file(path.name, path)


def load_registry() -> dict:
    _ensure_runtime_file(REGISTRY_PATH, BASE_REGISTRY_PATH)
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    # Backward-compatible defaults for releases before the live connection manager.
    for source, cfg in data.items():
        cfg.setdefault("default_actor_id", cfg.get("actor_id"))
        cfg.setdefault("adapter_mode", "legacy" if cfg.get("actor_id") == cfg.get("default_actor_id") else "generic")
        cfg.setdefault("actor_status", "legacy_default_unverified")
        cfg.setdefault("last_verified_at", None)
        cfg.setdefault("last_smoke_test_at", None)
        cfg.setdefault("actor_metadata", {})
        cfg.setdefault("input_mapping", {})
        cfg.setdefault("input_schema_fields", {})
        cfg.setdefault("input_template", {})
        cfg.setdefault("output_mapping", {})
        cfg.setdefault("mapping_confidence", None)
        cfg.setdefault("mapping_signature", None)
        cfg.setdefault("comment_deepening_status", "unverified")
        cfg.setdefault("comment_actor_id", None)
        cfg.setdefault("comment_last_smoke_test_at", None)
        cfg.setdefault("comment_route", None)
        cfg.setdefault("comment_input_field", None)
        cfg.setdefault("comment_output_mapping", {})
    return data


def public_registry() -> dict:
    return deepcopy(load_registry())


def load_history() -> dict:
    _ensure_runtime_file(HISTORY_PATH, BASE_HISTORY_PATH)
    if not HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def source_history(source: str) -> list[dict]:
    return deepcopy(load_history().get(source, []))


def _append_history(source: str, config: dict, reason: str) -> None:
    history = load_history()
    entries = list(history.get(source, []))
    entries.append({"saved_at": _utcnow(), "reason": reason, "config": deepcopy(config)})
    history[source] = entries[-20:]
    _write_json_atomic(HISTORY_PATH, history)


def update_source(source: str, changes: dict) -> dict:
    """Backward-compatible low-level update.

    Changing an Actor through this endpoint clears prior live verification and mappings.
    The v1.8 UI uses commit_actor_configuration() after a successful lookup/smoke test.
    """
    data = load_registry()
    if source not in data:
        raise KeyError(source)
    allowed = {"actor_id", "locked", "enabled", "price_per_1000_hint"}
    current = data[source]
    requested_actor = changes.get("actor_id")
    actor_changed = requested_actor is not None and requested_actor != current.get("actor_id")
    if actor_changed and current.get("locked", False) and changes.get("locked") is not False:
        raise PermissionError("Unlock the source before changing its Actor.")
    if actor_changed:
        _append_history(source, current, "manual_actor_change")
    for key, value in changes.items():
        if key in allowed and value is not None:
            data[source][key] = value
    if actor_changed:
        data[source].update({
            "adapter_mode": "legacy" if requested_actor == current.get("default_actor_id") else "generic",
            "actor_status": "unverified",
            "last_verified_at": None,
            "last_smoke_test_at": None,
            "actor_metadata": {},
            "input_mapping": {},
            "input_schema_fields": {},
            "input_template": {},
            "output_mapping": {},
            "mapping_confidence": None,
            "mapping_signature": None,
            "comment_deepening_status": "unverified",
            "comment_actor_id": None,
            "comment_last_smoke_test_at": None,
            "comment_route": None,
            "comment_input_field": None,
            "comment_output_mapping": {},
        })
    _write_json_atomic(REGISTRY_PATH, data)
    return deepcopy(data[source])


def commit_actor_configuration(
    source: str,
    *,
    actor_id: str,
    actor_metadata: dict,
    input_mapping: dict,
    input_schema_fields: dict,
    input_template: dict,
    output_mapping: dict,
    mapping_confidence: float | None,
    verified_at: str,
    smoke_tested_at: str,
) -> dict:
    data = load_registry()
    if source not in data:
        raise KeyError(source)
    current = data[source]
    if current.get("locked", False) and actor_id != current.get("actor_id"):
        raise PermissionError("Unlock the source before changing its Actor.")
    missing = [name for name in ("text", "date", "url") if not output_mapping.get(name)]
    if missing:
        raise ValueError("Cannot lock Actor: output mapping is missing " + ", ".join(missing) + ".")
    if not smoke_tested_at:
        raise ValueError("A live smoke test is required before setting a new default Actor.")

    actor_changed = actor_id != current.get("actor_id")
    if actor_changed or current.get("actor_status") != "verified":
        _append_history(source, current, "before_verified_actor_commit")

    from app.services.integrations import mapping_signature

    if actor_changed:
        # A new primary Actor may expose different reply/comment semantics. Re-verify
        # the deepening route rather than inheriting stale trust.
        current.update({
            "comment_deepening_status": "unverified",
            "comment_actor_id": None,
            "comment_last_smoke_test_at": None,
            "comment_route": None,
            "comment_input_field": None,
            "comment_output_mapping": {},
        })

    current.update({
        "actor_id": actor_id,
        "adapter_mode": "legacy" if actor_id == current.get("default_actor_id") else "generic",
        "locked": True,
        "actor_status": "verified",
        "last_verified_at": verified_at,
        "last_smoke_test_at": smoke_tested_at,
        "actor_metadata": deepcopy(actor_metadata or {}),
        "input_mapping": deepcopy(input_mapping or {}),
        "input_schema_fields": deepcopy(input_schema_fields or {}),
        "input_template": deepcopy(input_template or {}),
        "output_mapping": deepcopy(output_mapping or {}),
        "mapping_confidence": mapping_confidence,
        "mapping_signature": mapping_signature(output_mapping or {}),
    })
    _write_json_atomic(REGISTRY_PATH, data)
    return deepcopy(current)


def commit_comment_route_verification(
    source: str,
    *,
    actor_id: str,
    smoke_tested_at: str,
    route: str | None = None,
    input_field: str | None = None,
    output_mapping: dict | None = None,
) -> dict:
    """Record a separately paid/live-verified comment or reply route.

    Public schema knowledge alone never calls a route verified. This function is
    intended to be called only after live input validation and a clean paid smoke
    run with real data rows.
    """
    data = load_registry()
    if source not in data:
        raise KeyError(source)
    if not actor_id or not smoke_tested_at:
        raise ValueError("Comment-route verification requires Actor ID and paid smoke-test timestamp.")
    current = data[source]
    _append_history(source, current, "before_comment_route_verification")
    current.update({
        "comment_deepening_status": "verified",
        "comment_actor_id": actor_id,
        "comment_last_smoke_test_at": smoke_tested_at,
        "comment_route": route,
        "comment_input_field": input_field,
        "comment_output_mapping": deepcopy(output_mapping or {}),
    })
    _write_json_atomic(REGISTRY_PATH, data)
    return deepcopy(current)


def clear_comment_route_verification(source: str, reason: str = "manual_clear") -> dict:
    data = load_registry()
    if source not in data:
        raise KeyError(source)
    current = data[source]
    _append_history(source, current, reason)
    current.update({
        "comment_deepening_status": "unverified",
        "comment_actor_id": None,
        "comment_last_smoke_test_at": None,
        "comment_route": None,
        "comment_input_field": None,
        "comment_output_mapping": {},
    })
    _write_json_atomic(REGISTRY_PATH, data)
    return deepcopy(current)


def rollback_source(source: str) -> dict:
    data = load_registry()
    if source not in data:
        raise KeyError(source)
    history = load_history()
    entries = list(history.get(source, []))
    if not entries:
        raise LookupError("No previous Actor configuration is available for rollback.")
    previous = entries.pop()["config"]
    # Rollback is a safety action: restore the previous Actor configuration in
    # locked state so a replacement cannot remain accidentally editable/live.
    previous["locked"] = True
    _append_history(source, data[source], "rollback_replaced_current")
    # _append_history reloaded history; remove the consumed target in a fresh structure afterwards.
    refreshed = load_history()
    refreshed_entries = refreshed.get(source, [])
    # Find and remove one entry matching the consumed config from before the rollback marker.
    for idx in range(len(refreshed_entries) - 2, -1, -1):
        if refreshed_entries[idx].get("config") == previous:
            refreshed_entries.pop(idx)
            break
    refreshed[source] = refreshed_entries[-20:]
    _write_json_atomic(HISTORY_PATH, refreshed)
    data[source] = deepcopy(previous)
    _write_json_atomic(REGISTRY_PATH, data)
    return deepcopy(data[source])


def output_mapping_for(source: str) -> dict:
    cfg = load_registry().get(source) or {}
    mapping = cfg.get("output_mapping") or {}
    return deepcopy(mapping if isinstance(mapping, dict) else {})
