from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch(path: str, old: str, new: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"Compatibility patch anchor not found in {path}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Preserve the legacy distinction for the same-Actor X reply route: it is publicly
# known/available but still not trusted for paid production until live verification.
patch(
    "app/services/source_capabilities.py",
    '''    elif live_verified and not configured_enabled:\n        status = "verified_disabled"\n    elif cap.get("public_schema_known"):\n        status = "candidate_requires_live_verification"\n''',
    '''    elif live_verified and not configured_enabled:\n        status = "verified_disabled"\n    elif cap.get("mode") == "same_actor" and cap.get("public_schema_known"):\n        status = "available_but_unverified"\n    elif cap.get("public_schema_known"):\n        status = "candidate_requires_live_verification"\n''',
)

# A successful explicit paid smoke acceptance installs + enables that route by
# default. Settings can disable it later without deleting its verification record.
patch(
    "app/registry.py",
    '''        "comment_output_mapping": deepcopy(output_mapping or {}),\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n''',
    '''        "comment_output_mapping": deepcopy(output_mapping or {}),\n        "comment_enabled": True,\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n''',
)

# Old plans already contain the live-verification snapshot in preflight_forecast.
# Respect that immutable run contract even if the mutable registry changes later.
patch(
    "app/services/relevance_expansion.py",
    '''    if comments_requested:\n        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []\n        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]\n        for sp in selected:\n''',
    '''    if comments_requested:\n        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []\n        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]\n        forecast_rows = {r.get("source"): r for r in ((plan.get("preflight_forecast") or {}).get("sources") or [])}\n        for sp in selected:\n''',
)
patch(
    "app/services/relevance_expansion.py",
    '''            cfg = registry.get(source) or {}\n            comment_info = comments_forecast(source, True, cfg)\n            if comment_info.get("status") == "verified_disabled":\n''',
    '''            cfg = registry.get(source) or {}\n            comment_info = comments_forecast(source, True, cfg)\n            plan_comment_info = (forecast_rows.get(source) or {}).get("comments") or {}\n            if plan_comment_info.get("status") == "verified_available":\n                comment_info = {**comment_info, "status": "verified_available", "live_verified": True, "enabled": True}\n            if comment_info.get("status") == "verified_disabled":\n''',
)
patch(
    "app/services/relevance_expansion.py",
    '''            if comment_info.get("status") != "verified_available":\n                audit["warnings"].append(f"{source}:comment_deepening_blocked_until_live_route_verification")\n                continue\n''',
    '''            if comment_info.get("status") != "verified_available":\n                audit["warnings"].append(f"{source}:comment_deepening_blocked_until_live_route_verification")\n                if source == "x":\n                    audit["warnings"].append("reply_deepening_blocked_until_live_route_verification")\n                continue\n''',
)

print("Comment deepening compatibility patches applied.")
