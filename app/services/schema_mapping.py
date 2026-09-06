from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.services.normalizer import SOURCE_PATHS, nested, normalize_dataset_with_audit, parse_date

SEMANTICS = ("text", "date", "author", "followers", "views", "likes", "comments", "shares", "url")
ALIASES = {
    "text": ("text", "caption", "posttext", "content", "description", "desc", "title", "body", "message", "snippet"),
    "date": ("date", "createdat", "timestamp", "publishedat", "createtime", "takenatiso", "publishdate", "uploaddate", "postedat"),
    "author": ("author", "username", "ownerusername", "channelname", "name", "publisher", "source"),
    "followers": ("followers", "followerscount", "followercount", "subscribers"),
    "views": ("views", "viewcount", "playcount", "reach"),
    "likes": ("likes", "likecount", "likescount", "diggcount", "reactionscount"),
    "comments": ("comments", "commentcount", "commentscount", "replycount"),
    "shares": ("shares", "sharecount", "sharescount", "retweetcount", "repostcount"),
    "url": ("url", "posturl", "webvideourl", "link", "permalink", "originalurl"),
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def flatten_scalar_paths(value: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > 5:
        return []
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.extend(flatten_scalar_paths(v, path, depth + 1))
            elif isinstance(v, list):
                if v and isinstance(v[0], (dict, list)):
                    out.extend(flatten_scalar_paths(v[0], path, depth + 1))
            else:
                out.append((path, v))
    return out


def deterministic_mapping(source: str, rows: list[dict]) -> dict:
    samples = [r for r in rows[:12] if isinstance(r, dict)]
    available = []
    seen = set()
    for row in samples:
        for path, value in flatten_scalar_paths(row):
            if path not in seen:
                seen.add(path); available.append((path, value))
    mapping = {}
    confidence = {}
    # First use known source contracts when present in the sample.
    for semantic in SEMANTICS:
        for path in (SOURCE_PATHS.get(source) or {}).get(semantic, ()):
            if any(p == path and v not in (None, "") for p, v in available):
                mapping[semantic] = path; confidence[semantic] = 1.0; break
    for semantic in SEMANTICS:
        if semantic in mapping:
            continue
        alias_norms = {_key(a) for a in ALIASES[semantic]}
        best = None
        for path, value in available:
            leaf = _key(path.split(".")[-1])
            score = 0.0
            if leaf in alias_norms: score = 0.95
            elif any(a in leaf or leaf in a for a in alias_norms if len(a) >= 4): score = 0.72
            if semantic in {"followers", "views", "likes", "comments", "shares"} and value not in (None, ""):
                try: float(value)
                except Exception: score *= 0.2
            if semantic == "date" and value not in (None, "") and parse_date(value) is not None:
                score += 0.04
            if best is None or score > best[0]:
                best = (score, path)
        if best and best[0] >= 0.70:
            mapping[semantic] = best[1]; confidence[semantic] = round(min(1.0, best[0]), 3)
    return {"mapping": mapping, "confidence": confidence, "available_paths": [p for p, _ in available], "method": "deterministic"}


def _openai_mapping(source: str, rows: list[dict], available_paths: list[str]) -> dict:
    if not settings.signalyth_ai_enabled or not settings.openai_api_key or not available_paths:
        return {}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=30.0)
        examples = []
        for row in rows[:3]:
            sample = {}
            for path in available_paths[:80]:
                value = nested(row, path)
                if value not in (None, ""):
                    s = str(value)
                    sample[path] = s[:120]
            examples.append(sample)
        properties = {k: {"type": ["string", "null"]} for k in SEMANTICS}
        schema = {"type": "object", "additionalProperties": False, "properties": properties, "required": list(SEMANTICS)}
        response = client.responses.create(
            model=settings.signalyth_ai_bulk_model,
            store=False,
            instructions=(
                "Map source output paths to SIGNALYTH semantics. Use only an exact path from available_paths or null. "
                "Do not guess from values alone when ambiguous. Date must be a publication/creation time, author a person/page/channel, "
                "and numeric engagement fields must match their semantic meaning. Return JSON only."
            ),
            input=json.dumps({"source": source, "available_paths": available_paths[:120], "examples": examples}, ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": "signalyth_output_mapping", "schema": schema, "strict": True}},
            max_output_tokens=700,
        )
        decoded = json.loads(response.output_text or "{}")
        return {k: v for k, v in decoded.items() if isinstance(v, str) and v in available_paths}
    except Exception:
        return {}


def recover_mapping(source: str, rows: list[dict], current: dict | None = None) -> dict:
    det = deterministic_mapping(source, rows)
    mapping = dict(current or {})
    mapping.update(det["mapping"])
    critical = {"text", "date"}
    missing = [x for x in critical if x not in mapping]
    ai_used = False
    if missing:
        ai = _openai_mapping(source, rows, det["available_paths"])
        if ai:
            mapping.update(ai); ai_used = True
    audit = normalize_dataset_with_audit(source, rows, mapping=mapping)
    dated = sum(1 for r in audit["rows"] if r.get("date"))
    texted = sum(1 for r in audit["rows"] if str(r.get("text") or "").strip())
    valid = bool(audit["rows"]) and texted > 0 and dated > 0
    return {
        "mapping": mapping,
        "valid": valid,
        "normalized_rows": len(audit["rows"]),
        "dated_rows": dated,
        "text_rows": texted,
        "available_paths": det["available_paths"],
        "deterministic_confidence": det["confidence"],
        "openai_fallback_used": ai_used,
        "contract": "schema-recovery-v1",
    }
