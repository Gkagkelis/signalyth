from __future__ import annotations

import json
import re
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"Missing patch target: {label}")
    return text.replace(old, new, 1)


# 1) Baseline registry: four social comment Actors are curated/configured and ON.
p = Path("config/source_registry.json")
data = json.loads(p.read_text(encoding="utf-8"))
rollout = {
    "x": ("xquik/x-tweet-scraper", "replies", "replyTweetIds", 0.15),
    "tiktok": ("epctex/tiktok-comment-scraper", "comments", "startUrls", 0.30),
    "instagram": ("scrapesmith/instagram-comments-scraper", "comments", "postUrls", 0.50),
    "facebook": ("scraper_one/facebook-comments-scraper", "comments", "postUrls", 0.40),
}
for source, (actor, route, field, price) in rollout.items():
    cfg = data[source]
    cfg["comment_deepening_status"] = "configured"
    cfg["comment_actor_id"] = actor
    cfg["comment_route"] = route
    cfg["comment_input_field"] = field
    cfg["comment_enabled"] = True
    cfg["comment_price_per_1000_hint"] = price
    cfg["comment_rollout_version"] = "comments-production-ready-v1"
    cfg.setdefault("comment_last_live_success_at", None)
for source in ("youtube", "news"):
    cfg = data[source]
    cfg["comment_deepening_status"] = "not_applicable"
    cfg["comment_actor_id"] = None
    cfg["comment_route"] = None
    cfg["comment_input_field"] = None
    cfg["comment_enabled"] = False
    cfg["comment_price_per_1000_hint"] = None
    cfg["comment_rollout_version"] = "comments-production-ready-v1"
    cfg.setdefault("comment_last_live_success_at", None)
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 2) Runtime registry migration. This upgrades already-persisted Vercel/cloud config once,
# while preserving later user OFF choices because the rollout marker is written durably.
p = Path("app/registry.py")
text = p.read_text(encoding="utf-8")
anchor = 'BASE_HISTORY_PATH = BASE_DIR / "config" / "source_registry_history.json"\n'
constants = '''BASE_HISTORY_PATH = BASE_DIR / "config" / "source_registry_history.json"\n\nCOMMENT_ROLLOUT_VERSION = "comments-production-ready-v1"\nCOMMENT_PRODUCTION_ROLLOUT = {\n    "x": {"actor_id": "xquik/x-tweet-scraper", "route": "replies", "input_field": "replyTweetIds", "price": 0.15},\n    "tiktok": {"actor_id": "epctex/tiktok-comment-scraper", "route": "comments", "input_field": "startUrls", "price": 0.30},\n    "instagram": {"actor_id": "scrapesmith/instagram-comments-scraper", "route": "comments", "input_field": "postUrls", "price": 0.50},\n    "facebook": {"actor_id": "scraper_one/facebook-comments-scraper", "route": "comments", "input_field": "postUrls", "price": 0.40},\n}\n'''
text = replace_once(text, anchor, constants, "registry rollout constants")
start = text.index("def load_registry() -> dict:\n")
end = text.index("\n\ndef public_registry() -> dict:", start)
new_load = '''def load_registry() -> dict:\n    _ensure_runtime_file(REGISTRY_PATH, BASE_REGISTRY_PATH)\n    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))\n    changed = False\n    # Backward-compatible defaults plus one-time migration of the curated comment rollout.\n    for source, cfg in data.items():\n        cfg.setdefault("default_actor_id", cfg.get("actor_id"))\n        cfg.setdefault("adapter_mode", "legacy" if cfg.get("actor_id") == cfg.get("default_actor_id") else "generic")\n        cfg.setdefault("actor_status", "legacy_default_unverified")\n        cfg.setdefault("last_verified_at", None)\n        cfg.setdefault("last_smoke_test_at", None)\n        cfg.setdefault("actor_metadata", {})\n        cfg.setdefault("input_mapping", {})\n        cfg.setdefault("input_schema_fields", {})\n        cfg.setdefault("input_template", {})\n        cfg.setdefault("output_mapping", {})\n        cfg.setdefault("mapping_confidence", None)\n        cfg.setdefault("mapping_signature", None)\n        cfg.setdefault("comment_deepening_status", "unverified")\n        cfg.setdefault("comment_actor_id", None)\n        cfg.setdefault("comment_last_smoke_test_at", None)\n        cfg.setdefault("comment_last_live_success_at", None)\n        cfg.setdefault("comment_route", None)\n        cfg.setdefault("comment_input_field", None)\n        cfg.setdefault("comment_output_mapping", {})\n        cfg.setdefault("comment_enabled", False)\n        cfg.setdefault("comment_price_per_1000_hint", None)\n        cfg.setdefault("comment_max_per_parent", 40)\n        cfg.setdefault("comment_max_parents", 12)\n        cfg.setdefault("comment_include_replies", True)\n        cfg.setdefault("comment_rollout_version", None)\n\n        rollout = COMMENT_PRODUCTION_ROLLOUT.get(source)\n        if rollout and cfg.get("comment_actor_id") in (None, "", rollout["actor_id"]):\n            desired_status = "verified" if cfg.get("comment_deepening_status") == "verified" else "configured"\n            desired = {\n                "comment_deepening_status": desired_status,\n                "comment_actor_id": rollout["actor_id"],\n                "comment_route": rollout["route"],\n                "comment_input_field": rollout["input_field"],\n                "comment_price_per_1000_hint": rollout["price"],\n            }\n            for key, value in desired.items():\n                if cfg.get(key) != value:\n                    cfg[key] = value\n                    changed = True\n            if cfg.get("comment_rollout_version") != COMMENT_ROLLOUT_VERSION:\n                # First production migration only: ready by default. The run-level\n                # Comments switch remains the explicit per-analysis paid opt-in.\n                cfg["comment_enabled"] = True\n                cfg["comment_rollout_version"] = COMMENT_ROLLOUT_VERSION\n                changed = True\n        elif source in {"youtube", "news"} and cfg.get("comment_rollout_version") != COMMENT_ROLLOUT_VERSION:\n            cfg.update({\n                "comment_deepening_status": "not_applicable",\n                "comment_actor_id": None,\n                "comment_route": None,\n                "comment_input_field": None,\n                "comment_enabled": False,\n                "comment_price_per_1000_hint": None,\n                "comment_rollout_version": COMMENT_ROLLOUT_VERSION,\n            })\n            changed = True\n\n    if changed:\n        _write_json_atomic(REGISTRY_PATH, data)\n    return data\n'''
text = text[:start] + new_load + text[end:]
text = replace_once(
    text,
    '''    if changes.get("comment_enabled") is True:\n        if current.get("comment_deepening_status") != "verified":\n            raise PermissionError("Comments cannot be enabled until this comment Actor passes the paid live smoke verification.")\n        if not current.get("comment_actor_id"):\n            raise PermissionError("Comments cannot be enabled because no comment Actor is configured for this source.")\n''',
    '''    if changes.get("comment_enabled") is True:\n        if not current.get("comment_actor_id"):\n            raise PermissionError("Comments cannot be enabled because no comment Actor is configured for this source.")\n        if current.get("comment_deepening_status") not in {"configured", "verified"}:\n            raise PermissionError("Comments cannot be enabled because this source has no production-ready comment Actor contract.")\n''',
    "registry comment enable gate",
)
text = replace_once(
    text,
    '        "comment_enabled": False,\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n\n\ndef clear_comment_route_verification',
    '        "comment_enabled": bool(current.get("comment_enabled", False)),\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n\n\ndef clear_comment_route_verification',
    "verification preserves enabled state",
)
start = text.index("def clear_comment_route_verification(source: str, reason: str = \"manual_clear\") -> dict:\n")
end = text.index("\n\ndef rollback_source", start)
new_clear = '''def clear_comment_route_verification(source: str, reason: str = "manual_clear") -> dict:\n    data = load_registry()\n    if source not in data:\n        raise KeyError(source)\n    current = data[source]\n    _append_history(source, current, reason)\n    rollout = COMMENT_PRODUCTION_ROLLOUT.get(source)\n    if rollout:\n        # Clearing a live confirmation must not uninstall a curated production contract.\n        current.update({\n            "comment_deepening_status": "configured",\n            "comment_actor_id": rollout["actor_id"],\n            "comment_last_smoke_test_at": None,\n            "comment_last_live_success_at": None,\n            "comment_route": rollout["route"],\n            "comment_input_field": rollout["input_field"],\n            "comment_output_mapping": {},\n        })\n    else:\n        current.update({\n            "comment_deepening_status": "not_applicable",\n            "comment_actor_id": None,\n            "comment_last_smoke_test_at": None,\n            "comment_last_live_success_at": None,\n            "comment_route": None,\n            "comment_input_field": None,\n            "comment_output_mapping": {},\n            "comment_enabled": False,\n        })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n'''
text = text[:start] + new_clear + text[end:]
p.write_text(text, encoding="utf-8")


# 3) Capability forecast: a curated public-schema contract is operational before a separate smoke run.
p = Path("app/services/source_capabilities.py")
text = p.read_text(encoding="utf-8")
start = text.index("def comments_forecast(source: str, requested: bool, registry_cfg: dict | None = None) -> dict:\n")
end = text.index("\n\ndef _x_id", start)
new_forecast = '''def comments_forecast(source: str, requested: bool, registry_cfg: dict | None = None) -> dict:\n    """Describe whether the configured comment layer can run for this analysis.\n\n    The four production social routes are allowed from their curated public Actor\n    contracts. A successful live run may later upgrade the route to verified, but\n    a separate paid smoke is not a prerequisite for normal collection.\n    """\n    cap = source_capabilities(source)["comment_deepening"]\n    registry_cfg = registry_cfg or {}\n    route_status = str(registry_cfg.get("comment_deepening_status") or "unverified")\n    configured_actor = registry_cfg.get("comment_actor_id") or cap.get("candidate_actor_id")\n    production_scope = source in {"x", "tiktok", "instagram", "facebook"}\n    live_verified = bool(cap.get("live_verified")) or route_status == "verified"\n    contract_ready = bool(production_scope and cap.get("public_schema_known") and configured_actor and route_status in {"configured", "verified"})\n    configured_enabled = bool(registry_cfg.get("comment_enabled", False))\n\n    if not requested:\n        status = "not_requested"\n    elif not production_scope or cap.get("mode") == "not_applicable":\n        status = "not_applicable"\n    elif live_verified and configured_enabled:\n        status = "verified_available"\n    elif contract_ready and configured_enabled:\n        status = "configured_available"\n    elif live_verified and not configured_enabled:\n        status = "verified_disabled"\n    elif contract_ready and not configured_enabled:\n        status = "configured_disabled"\n    elif cap.get("public_schema_known"):\n        status = "candidate_not_configured"\n    else:\n        status = "unknown_requires_configuration"\n    return {\n        "requested": bool(requested),\n        "status": status,\n        **cap,\n        "candidate_actor_id": configured_actor,\n        "live_verified": live_verified,\n        "contract_ready": contract_ready,\n        "enabled": configured_enabled,\n        "registry_route_status": route_status,\n    }\n'''
text = text[:start] + new_forecast + text[end:]
text = text.replace("Build the public-schema input shape for a verified comment/reply route.", "Build the curated public-schema input shape for a comment/reply route.")
p.write_text(text, encoding="utf-8")


# 4) Planner: configured production routes are operational and get a reserved comment budget.
p = Path("app/services/query_planner.py")
text = p.read_text(encoding="utf-8")
text = replace_once(
    text,
    '        if draft.comments and comment.get("status") not in {"verified_available", "not_applicable"}:\n',
    '        if draft.comments and comment.get("status") not in {"verified_available", "configured_available", "not_applicable"}:\n',
    "planner comment blockers",
)
text = replace_once(
    text,
    '''    comments_coverage = {\n        "requested": bool(draft.comments),\n        "fully_live_verified": bool(draft.comments) and not blockers,\n        "verification_blockers": blockers,\n    }\n''',
    '''    comments_coverage = {\n        "requested": bool(draft.comments),\n        "operationally_ready": bool(draft.comments) and not blockers,\n        "fully_live_verified": bool(draft.comments) and not blockers and all(\n            (r.get("comments") or {}).get("live_verified") or (r.get("comments") or {}).get("status") == "not_applicable"\n            for r in rows\n        ),\n        "verification_blockers": blockers,\n    }\n''',
    "planner coverage semantics",
)
text = replace_once(
    text,
    '        and registry[s].get("comment_deepening_status") == "verified"\n',
    '        and registry[s].get("comment_deepening_status") in {"configured", "verified"}\n',
    "planner comment budget",
)
p.write_text(text, encoding="utf-8")


# 5) Comment normalization carries parent text separately for contextual relevance.
p = Path("app/services/comment_deepening.py")
text = p.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    seed_refs: list[str] | None = None,\n    mapping: dict | None = None,\n) -> list[dict]:\n''',
    '''    seed_refs: list[str] | None = None,\n    seed_context: dict[str, str] | None = None,\n    mapping: dict | None = None,\n) -> list[dict]:\n''',
    "comment normalizer signature",
)
text = replace_once(
    text,
    '    seed_refs = [str(x) for x in (seed_refs or []) if str(x or "").strip()]\n',
    '    seed_refs = [str(x) for x in (seed_refs or []) if str(x or "").strip()]\n    seed_context = {str(k): str(v or "").strip() for k, v in (seed_context or {}).items() if str(k or "").strip()}\n',
    "comment seed context init",
)
text = replace_once(
    text,
    '        dt = parse_date(date_raw)\n',
    '        parent_context = seed_context.get(str(parent_post or ""), "")\n        dt = parse_date(date_raw)\n',
    "comment parent context lookup",
)
text = replace_once(
    text,
    '            "parent_post": parent_post,\n',
    '            "parent_post": parent_post,\n            "parent_context": parent_context or None,\n',
    "comment parent context record",
)
p.write_text(text, encoding="utf-8")


# 6) Collection: do not require a visible comment count to seed a parent, and pass parent context.
p = Path("app/services/relevance_expansion.py")
text = p.read_text(encoding="utf-8")
text = text.replace("plus verified comment/reply deepening", "plus configured comment/reply deepening")
text = replace_once(
    text,
    '''    evidence_layer: str = "primary",\n    seed_refs: list[str] | None = None,\n) -> dict:\n''',
    '''    evidence_layer: str = "primary",\n    seed_refs: list[str] | None = None,\n    seed_context: dict[str, str] | None = None,\n) -> dict:\n''',
    "append source seed context signature",
)
text = replace_once(
    text,
    '        new_norm = normalize_comment_dataset(source, data_items, seed_refs=seed_refs or [], mapping=mapping)\n',
    '        new_norm = normalize_comment_dataset(source, data_items, seed_refs=seed_refs or [], seed_context=seed_context or {}, mapping=mapping)\n',
    "append comment normalizer context",
)
text = replace_once(
    text,
    '''    for row in rows:\n        if int(row.get("comments", 0) or 0) <= 0:\n            continue\n        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}\n''',
    '''    for row in rows:\n        # Some discovery Actors do not expose an exact comment count. A relevant\n        # public parent URL is enough to try the bounded comment Actor.\n        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}\n''',
    "comment seed count gate",
)
text = replace_once(
    text,
    '        meta.append({"ref": ref, "comments": int(row.get("comments", 0) or 0), "url": row.get("url")})\n',
    '        meta.append({"ref": ref, "comments": int(row.get("comments", 0) or 0), "url": row.get("url"), "text": str(row.get("text") or "")})\n',
    "comment seed parent text",
)
text = replace_once(
    text,
    '''        evidence_layer: str = "primary",\n        seed_refs: list[str] | None = None,\n    ):\n''',
    '''        evidence_layer: str = "primary",\n        seed_refs: list[str] | None = None,\n        seed_context: dict[str, str] | None = None,\n    ):\n''',
    "do_call seed context signature",
)
text = replace_once(
    text,
    '''                evidence_layer=evidence_layer, seed_refs=seed_refs,\n            )\n''',
    '''                evidence_layer=evidence_layer, seed_refs=seed_refs, seed_context=seed_context,\n            )\n''',
    "do_call passes seed context",
)
text = replace_once(
    text,
    '''            if plan_comment_info.get("status") == "verified_available":\n                comment_info = {**comment_info, "status": "verified_available", "live_verified": True, "enabled": True}\n            if comment_info.get("status") == "verified_disabled":\n                audit["warnings"].append(f"{source}:comment_actor_verified_but_disabled_in_settings")\n                continue\n            if comment_info.get("status") != "verified_available":\n                audit["warnings"].append(f"{source}:comment_deepening_blocked_until_live_route_verification")\n                if source == "x":\n                    audit["warnings"].append("reply_deepening_blocked_until_live_route_verification")\n                continue\n''',
    '''            if plan_comment_info.get("status") in {"verified_available", "configured_available"}:\n                comment_info = {**comment_info, "status": plan_comment_info.get("status"), "enabled": True}\n            if comment_info.get("status") in {"verified_disabled", "configured_disabled"}:\n                audit["warnings"].append(f"{source}:comment_actor_disabled_in_settings")\n                continue\n            if comment_info.get("status") not in {"verified_available", "configured_available"}:\n                audit["warnings"].append(f"{source}:comment_deepening_not_operational")\n                continue\n''',
    "comment operational states",
)
text = replace_once(
    text,
    '''            audit.setdefault("comment_seeds", {})[source] = seed_meta\n            mapping = cfg.get("comment_output_mapping") or None\n            rate = cfg.get("comment_price_per_1000_hint")\n            do_call(\n                source, actor_id, "comment_deepening", inp, wanted, rate, mapping=mapping,\n                evidence_layer="comment", seed_refs=refs,\n            )\n''',
    '''            audit.setdefault("comment_seeds", {})[source] = seed_meta\n            seed_context = {str(m.get("ref")): str(m.get("text") or "") for m in seed_meta if m.get("ref")}\n            mapping = cfg.get("comment_output_mapping") or None\n            rate = cfg.get("comment_price_per_1000_hint")\n            do_call(\n                source, actor_id, "comment_deepening", inp, wanted, rate, mapping=mapping,\n                evidence_layer="comment", seed_refs=refs, seed_context=seed_context,\n            )\n''',
    "comment seed context call",
)
text = text.replace(
    '"Stop at target/budget/source exhaustion; comment routes must be verified + enabled and comments are re-cleaned for relevance."',
    '"Stop at target/budget/source exhaustion; configured comment routes must be enabled and comments are re-cleaned for direct or parent-context relevance."',
)
p.write_text(text, encoding="utf-8")


# 7) Cleaning: parent topic/context can anchor a comment without changing its displayed text.
p = Path("app/services/cleaning.py")
text = p.read_text(encoding="utf-8")
start = text.index("def _relevance_score(row: dict, plan: dict, view: TextView, market_score: float) -> tuple[float, list[str], list[str]]:\n")
end = text.index("\n\ndef _spam_score", start)
new_relevance = '''def _relevance_score(row: dict, plan: dict, view: TextView, market_score: float) -> tuple[float, list[str], list[str]]:\n    core_terms = [x for x in plan.get("core_terms", []) if str(x).strip()]\n    context_terms = [x for x in plan.get("context_terms", []) if str(x).strip()]\n    greeklish_terms = [x for x in plan.get("greeklish_variants", []) if str(x).strip()]\n    exclusions = [x for x in plan.get("exclusions", []) if str(x).strip()]\n\n    layer = str(row.get("evidence_layer") or "primary")\n    parent_context = str(row.get("parent_context") or "").strip() if layer in {"comment", "reply"} else ""\n    parent_view = _text_view(parent_context) if parent_context else None\n\n    direct_core_hits = [x for x in core_terms if _term_present(view.folded, x)]\n    parent_core_hits = [x for x in core_terms if parent_view and _term_present(parent_view.folded, x)]\n    core_hits = list(dict.fromkeys([*direct_core_hits, *parent_core_hits]))\n    core_folded = {_fold(x) for x in core_terms if _fold(x)}\n    context_candidates = [x for x in [*context_terms, *greeklish_terms] if _fold(x) not in core_folded]\n    direct_context_hits = [x for x in context_candidates if _term_present(view.folded, x)]\n    parent_context_hits = [x for x in context_candidates if parent_view and _term_present(parent_view.folded, x)]\n    exclusion_hits = [x for x in exclusions if _term_present(view.folded, x)]\n\n    score = 0.0\n    reasons: list[str] = []\n    flags: list[str] = []\n    if direct_core_hits:\n        score += min(0.72, 0.58 + 0.07 * (len(direct_core_hits) - 1))\n        reasons.extend([f"core_term:{x}" for x in direct_core_hits[:3]])\n    elif parent_core_hits:\n        # A reply may omit the brand/topic because conversational context supplies it.\n        # Parent context is an anchor, not an automatic keep: the lower weight sends\n        # generic replies to semantic review instead of pretending they are direct matches.\n        score += min(0.42, 0.34 + 0.04 * (len(parent_core_hits) - 1))\n        if len(view.tokens) >= 2:\n            score += 0.08\n        reasons.extend([f"parent_core_context:{x}" for x in parent_core_hits[:3]])\n        flags.append("contextual_parent_match")\n\n    if direct_context_hits:\n        score += min(0.22, 0.08 + 0.05 * len(direct_context_hits))\n        reasons.extend([f"context_term:{x}" for x in direct_context_hits[:3]])\n    elif parent_context_hits and parent_core_hits:\n        score += min(0.10, 0.04 + 0.02 * len(parent_context_hits))\n        reasons.extend([f"parent_context_term:{x}" for x in parent_context_hits[:3]])\n    score += 0.16 * market_score\n\n    if exclusion_hits:\n        score -= 0.55\n        reasons.extend([f"exclusion_term:{x}" for x in exclusion_hits[:3]])\n        flags.append("explicit_exclusion_context")\n\n    topic = _fold(plan.get("topic"))\n    if direct_core_hits and len(topic.replace(" ", "")) <= 5 and market_score < 0.35 and not direct_context_hits:\n        score -= 0.32\n        flags.append("ambiguous_short_entity")\n        reasons.append("short_entity_without_market_context")\n\n    if not core_hits:\n        score -= 0.18\n        flags.append("core_term_missing")\n        reasons.append("core_term_missing")\n\n    return max(0.0, min(1.0, score)), reasons, flags\n'''
text = text[:start] + new_relevance + text[end:]
p.write_text(text, encoding="utf-8")


# 8) UI: configured routes are visibly ready and their ON/OFF control is usable.
p = Path("app/main.py")
text = p.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    const c=reg[source]||{};const verified=String(c.comment_deepening_status||'unverified')==='verified';const enabled=!!c.comment_enabled;const max=Number(c.comment_max_per_parent||40);const actor=c.comment_actor_id||'No comment Actor configured';\n''',
    '''    const c=reg[source]||{};const state=String(c.comment_deepening_status||'unverified');const verified=state==='verified';const ready=state==='configured'||verified;const enabled=!!c.comment_enabled;const max=Number(c.comment_max_per_parent||40);const actor=c.comment_actor_id||'No comment Actor configured';\n''',
    "UI ready state",
)
text = replace_once(
    text,
    '''    const sig=[actor,verified,enabled,max].join('|');if(box.dataset.sig===sig)return;box.dataset.sig=sig;\n    box.innerHTML=`<div class="cai-main"><div class="cai-title">Comments / replies Actor</div><div class="cai-meta">${escComment(actor)}</div></div><div class="cai-controls"><span class="cai-state ${verified?'':'cai-warn'}">${verified?(enabled?'VERIFIED · ON':'VERIFIED · OFF'):'NEEDS LIVE VERIFICATION'}</span><label><input type="checkbox" data-comment-toggle="${source}" ${enabled?'checked':''} ${verified?'':'disabled'}> Include comments</label><label>max / parent <input type="number" min="1" max="1000" value="${max}" data-comment-max="${source}"></label></div>`;\n''',
    '''    const sig=[actor,state,enabled,max].join('|');if(box.dataset.sig===sig)return;box.dataset.sig=sig;\n    const stateLabel=verified?(enabled?'LIVE CONFIRMED · ON':'LIVE CONFIRMED · OFF'):(ready?(enabled?'READY · ON':'READY · OFF'):'UNAVAILABLE');\n    box.innerHTML=`<div class="cai-main"><div class="cai-title">Comments / replies Actor</div><div class="cai-meta">${escComment(actor)}</div></div><div class="cai-controls"><span class="cai-state ${ready?'':'cai-warn'}">${stateLabel}</span><label><input type="checkbox" data-comment-toggle="${source}" ${enabled?'checked':''} ${ready?'':'disabled'}> Include comments</label><label>max / parent <input type="number" min="1" max="1000" value="${max}" data-comment-max="${source}"></label></div>`;\n''',
    "UI state label and toggle",
)
text = replace_once(text, "finally{if(verified)toggle.disabled=false}", "finally{if(ready)toggle.disabled=false}", "UI toggle re-enable")
p.write_text(text, encoding="utf-8")


# 9) Model comments/documentation no longer claims a paid smoke prerequisite.
p = Path("app/models.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    "    # from the primary discovery Actor. Enabling remains fail-closed until the\n    # configured comment route has passed the explicit paid live smoke acceptance.\n",
    "    # from the primary discovery Actor. Curated production contracts may be\n    # enabled directly; the per-run Comments switch remains the paid opt-in.\n",
)
p.write_text(text, encoding="utf-8")


# 10) Tests: production configured state + contextual comment relevance.
p = Path("tests/test_comment_deepening.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    "from app.services.comment_deepening import normalize_comment_dataset\n",
    "from app.services.comment_deepening import normalize_comment_dataset\nfrom app.services.cleaning import clean_records\n",
)
old_test = '''def test_forecast_requires_both_verification_and_settings_enable():\n    cfg = {"comment_actor_id": "a/b", "comment_deepening_status": "verified", "comment_enabled": False}\n    assert comments_forecast("facebook", True, cfg)["status"] == "verified_disabled"\n    cfg["comment_enabled"] = True\n    assert comments_forecast("facebook", True, cfg)["status"] == "verified_available"\n'''
new_test = '''def test_forecast_allows_curated_configured_actor_when_enabled():\n    cfg = {"comment_actor_id": "scraper_one/facebook-comments-scraper", "comment_deepening_status": "configured", "comment_enabled": False}\n    assert comments_forecast("facebook", True, cfg)["status"] == "configured_disabled"\n    cfg["comment_enabled"] = True\n    assert comments_forecast("facebook", True, cfg)["status"] == "configured_available"\n    cfg["comment_deepening_status"] = "verified"\n    assert comments_forecast("facebook", True, cfg)["status"] == "verified_available"\n'''
text = replace_once(text, old_test, new_test, "forecast test")
text += '''\n\ndef test_comment_can_be_contextually_relevant_via_parent_without_repeating_topic():\n    rows = [{\n        "id": "comment:x:1", "platform": "x", "text": "Εμένα από το πρωί δεν δουλεύει τίποτα",\n        "date": "2026-09-13T09:00:00+00:00", "author": "person", "url": "https://x.com/u/status/2",\n        "evidence_layer": "reply", "parent_post": "1",\n        "parent_context": "Vodafone: μεγάλη διακοπή στο δίκτυο σήμερα στην Ελλάδα",\n        "likes": 1, "comments": 0, "shares": 0, "views": 0, "raw_data": {},\n    }]\n    plan = {\n        "client": "Vodafone", "topic": "Vodafone", "market": "Greece",\n        "core_terms": ["Vodafone"], "context_terms": ["διακοπή", "δίκτυο"],\n        "greeklish_variants": [], "exclusions": [], "target_total": 1,\n        "sources": [{"source": "x", "target_items": 1}],\n    }\n    result = clean_records(rows, plan)\n    cleaned = result["records"][0]\n    assert "contextual_parent_match" in cleaned["cleaning"]["flags"]\n    assert cleaned["cleaning"]["decision"] != "excluded"\n'''
p.write_text(text, encoding="utf-8")

# Existing hardening test: live verification now upgrades a configured route rather than being mandatory.
p = Path("tests/test_v184_hardening.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    '    assert comments_forecast("x", True, before)["status"] != "verified_available"\n',
    '    assert comments_forecast("x", True, before)["status"] in {"configured_available", "verified_available"}\n',
    1,
)
text = text.replace(
    '    assert saved["comment_enabled"] is False\n    assert comments_forecast("x", True, saved)["status"] == "verified_disabled"\n    enabled = R.update_source("x", {"comment_enabled": True})\n',
    '    assert saved["comment_enabled"] is True\n    assert comments_forecast("x", True, saved)["status"] == "verified_available"\n    enabled = R.update_source("x", {"comment_enabled": True})\n',
    1,
)
text = text.replace(
    '    assert cleared["comment_deepening_status"] == "unverified"\n    assert cleared["comment_enabled"] is False\n    assert comments_forecast("x", True, cleared)["status"] != "verified_available"\n',
    '    assert cleared["comment_deepening_status"] == "configured"\n    assert cleared["comment_actor_id"] == "xquik/x-tweet-scraper"\n    assert comments_forecast("x", True, cleared)["status"] in {"configured_available", "configured_disabled"}\n',
    1,
)
text = text.replace(
    '    assert R.load_registry()["x"]["comment_enabled"] is False\n',
    '    assert R.load_registry()["x"]["comment_enabled"] is True\n',
    1,
)
p.write_text(text, encoding="utf-8")

print("Production-ready comment collection patch applied.")
