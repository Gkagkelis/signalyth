from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import BASE_DIR, PERSISTENT_SECRETS_FILE, settings
from app.services.cloud_persistence import cloud_persistence
from app.services.resilience import split_diagnostic_rows, classify_actor_outcome

SECRETS_FILE = PERSISTENT_SECRETS_FILE
_SECRETS_LOCK = threading.RLock()

NORMALIZED_OUTPUT_FIELDS = [
    "text", "date", "author", "followers", "views", "likes", "comments",
    "shares", "url", "content_type", "parent_post",
]

OUTPUT_ALIASES: dict[str, list[str]] = {
    "text": ["text", "caption", "content", "description", "message", "title", "body", "snippet", "commenttext", "posttext"],
    "date": ["date", "createdat", "created_at", "timestamp", "datetime", "publishedat", "published_at", "time", "postedat"],
    "author": ["author", "username", "user", "ownerusername", "channelname", "profile_name", "name", "publisher"],
    "followers": ["followers", "followerscount", "followercount", "authorfollowers", "subscribers", "subscriberscount"],
    "views": ["views", "viewcount", "playcount", "video_views", "reach"],
    "likes": ["likes", "likecount", "diggcount", "reactions", "reactioncount"],
    "comments": ["comments", "commentcount", "commentscount", "replycount"],
    "shares": ["shares", "sharecount", "reposts", "retweets", "repostcount"],
    "url": ["url", "posturl", "weburl", "link", "permalink", "video_url", "tweeturl", "articleurl"],
    "content_type": ["contenttype", "content_type", "type", "posttype", "mediatype", "itemtype"],
    "parent_post": ["parentpost", "parent_post", "parent", "replyto", "reply_to", "quotedpost", "sourcepost"],
}

INPUT_ALIASES: dict[str, list[str]] = {
    "query": ["query", "search", "searchquery", "searchterm", "keyword", "keywords", "queries", "searchterms", "searchqueries"],
    "urls": ["urls", "starturls", "start_urls", "directurls", "direct_urls", "starturl", "url"],
    "max_items": ["maxitems", "max_items", "resultslimit", "resultslimit", "maxresults", "limit", "maxposts", "maxvideos", "maxcomments"],
    "date_from": ["from", "datefrom", "date_from", "startdate", "start_date", "since", "newerthan", "fromdate", "publishedafter"],
    "date_to": ["to", "dateto", "date_to", "enddate", "end_date", "until", "olderthan", "todate", "publishedbefore"],
    "country": ["country", "countrycode", "country_code", "region", "gl", "location"],
    "language": ["language", "lang", "languagecode", "language_code", "hl"],
    "comments": ["comments", "scrapecomments", "includecomments", "fetchcomments", "downloadcomments"],
}


@dataclass(frozen=True)
class ActorReference:
    raw: str
    normalized: str
    api_id: str
    store_url: str | None


class IntegrationError(RuntimeError):
    pass


class IntegrationNotConfigured(IntegrationError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    out: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if callable(item):
            continue
        if isinstance(item, (str, int, float, bool, type(None), dict, list)):
            out[name] = item
    return out


def parse_actor_reference(raw: str) -> ActorReference:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Actor reference is empty.")

    # Accept Store URLs, Console URLs, owner/name, owner~name, or opaque Actor IDs.
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host not in {"apify.com", "www.apify.com", "console.apify.com"}:
            raise ValueError("Use an Apify Actor URL, owner/actor-name, owner~actor-name, or Actor ID.")
        parts = [p for p in parsed.path.split("/") if p]
        if host == "console.apify.com":
            # console.apify.com/actors/<opaque-id>/... is the canonical console shape.
            if len(parts) >= 2 and parts[0] == "actors":
                text = parts[1]
            else:
                raise ValueError("Could not read an Actor ID from this Apify Console URL.")
        else:
            # Store URL: apify.com/<owner>/<actor-name>[/...]
            if len(parts) < 2:
                raise ValueError("Could not read owner/actor-name from this Apify Store URL.")
            text = f"{parts[0]}/{parts[1]}"

    text = text.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    if "~" in text and "/" not in text:
        owner, name = text.split("~", 1)
        if not owner or not name:
            raise ValueError("Invalid owner~actor-name reference.")
        normalized = f"{owner}/{name}"
        api_id = f"{owner}~{name}"
        store_url = f"https://apify.com/{owner}/{name}"
    elif "/" in text:
        parts = [p for p in text.split("/") if p]
        if len(parts) != 2:
            raise ValueError("Actor reference must be owner/actor-name.")
        owner, name = parts
        normalized = f"{owner}/{name}"
        api_id = f"{owner}~{name}"
        store_url = f"https://apify.com/{owner}/{name}"
    else:
        # Opaque Apify Actor ID.
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", text):
            raise ValueError("Invalid Apify Actor ID.")
        normalized = text
        api_id = text
        store_url = None

    return ActorReference(raw=raw, normalized=normalized, api_id=api_id, store_url=store_url)


def _json_schema_from_openapi(openapi: dict) -> dict:
    if not isinstance(openapi, dict):
        return {}
    # Apify Actor OpenAPI definitions normally expose the Actor input under the POST request body.
    paths = openapi.get("paths") or {}
    for _path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        post = methods.get("post")
        if not isinstance(post, dict):
            continue
        content = ((post.get("requestBody") or {}).get("content") or {})
        for media in ("application/json", "application/json; charset=utf-8"):
            schema = ((content.get(media) or {}).get("schema"))
            if isinstance(schema, dict):
                return resolve_local_refs(schema, openapi)
        for entry in content.values():
            if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
                return resolve_local_refs(entry["schema"], openapi)
    return {}


def resolve_local_refs(value: Any, root: dict, depth: int = 0) -> Any:
    if depth > 12:
        return value
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            current: Any = root
            try:
                for part in ref[2:].split("/"):
                    current = current[part.replace("~1", "/").replace("~0", "~")]
                return resolve_local_refs(current, root, depth + 1)
            except Exception:
                return value
        return {k: resolve_local_refs(v, root, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_local_refs(x, root, depth + 1) for x in value]
    return value


def parse_json_maybe(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _schema_from_example(example: dict) -> dict:
    """Build a conservative schema hint from an Actor example input.

    This is only a fallback for Actors whose published OpenAPI/input schema is
    unavailable. It never marks inferred fields as required.
    """
    if not isinstance(example, dict) or not example:
        return {"type": "object", "properties": {}}
    props: dict[str, dict] = {}
    for key, value in example.items():
        spec: dict[str, Any] = {"example": value}
        if isinstance(value, bool):
            spec["type"] = "boolean"
        elif isinstance(value, int) and not isinstance(value, bool):
            spec["type"] = "integer"
        elif isinstance(value, float):
            spec["type"] = "number"
        elif isinstance(value, list):
            spec["type"] = "array"
        elif isinstance(value, dict):
            spec["type"] = "object"
        elif value is None:
            spec["type"] = "unknown"
        else:
            spec["type"] = "string"
        props[str(key)] = spec
    return {"type": "object", "properties": props}


def _merge_schema_with_example(schema: dict, example: dict) -> dict:
    """Keep the published schema authoritative while adding missing example keys."""
    base = dict(schema or {})
    if not isinstance(base.get("properties"), dict):
        base["properties"] = {}
    inferred = _schema_from_example(example)
    for key, spec in (inferred.get("properties") or {}).items():
        base["properties"].setdefault(key, spec)
    base.setdefault("type", "object")
    return base


def schema_summary(schema: dict) -> dict:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    required = [str(x) for x in required] if isinstance(required, list) else []
    fields = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        field_type = spec.get("type")
        if not field_type:
            if "enum" in spec:
                field_type = "enum"
            elif "oneOf" in spec or "anyOf" in spec:
                field_type = "union"
            else:
                field_type = "unknown"
        fields.append({
            "name": str(name),
            "type": field_type,
            "required": name in required,
            "title": spec.get("title"),
            "description": spec.get("description"),
            "default": spec.get("default"),
            "example": spec.get("example") or (spec.get("examples") or [None])[0] if isinstance(spec.get("examples"), list) else spec.get("example"),
            "enum": spec.get("enum") if isinstance(spec.get("enum"), list) else None,
        })
    return {"fields": fields, "required": required, "field_count": len(fields)}


def _keynorm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def suggest_input_mapping(schema: dict) -> dict[str, str]:
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict):
        return {}
    names = list(props.keys())
    result: dict[str, str] = {}
    for semantic, aliases in INPUT_ALIASES.items():
        alias_norms = [_keynorm(x) for x in aliases]
        best: tuple[int, str] | None = None
        for name in names:
            n = _keynorm(name)
            score = 0
            if n in alias_norms:
                score = 100
            elif any(a in n or n in a for a in alias_norms if len(a) >= 4):
                score = 70
            if score and (best is None or score > best[0]):
                best = (score, name)
        if best:
            result[semantic] = best[1]
    return result




def actor_input_compatibility(source: str, input_mapping: dict) -> dict:
    mapping = input_mapping or {}
    discoverable = bool(mapping.get("query")) or (source == "instagram" and bool(mapping.get("urls")))
    warnings: list[str] = []
    if not discoverable:
        warnings.append("No usable discovery input was mapped. This source needs a query/search field, or URL input for Instagram.")
    if not mapping.get("max_items"):
        warnings.append("No result-limit field was mapped; SIGNALYTH can still pass Apify maxItems at run level, but Actor-internal limits may differ.")
    if not mapping.get("date_from") or not mapping.get("date_to"):
        warnings.append("Exact native From/To is not fully mapped; SIGNALYTH will enforce the requested date window after collection.")
    return {"compatible": discoverable, "warnings": warnings}


def build_probe_input(schema: dict, context: dict | None = None) -> tuple[dict, list[str], dict[str, str]]:
    context = dict(context or {})
    mapping = suggest_input_mapping(schema)
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    probe: dict[str, Any] = {}

    values: dict[str, Any] = {
        "query": context.get("query") or context.get("topic") or "SIGNALYTH",
        "urls": context.get("urls") or [],
        "max_items": int(context.get("max_items") or 3),
        "date_from": context.get("date_from"),
        "date_to": context.get("date_to"),
        "country": context.get("country") or "GR",
        "language": context.get("language") or "el",
        "comments": bool(context.get("comments", False)),
    }
    for semantic, field in mapping.items():
        value = values.get(semantic)
        if value is None:
            continue
        spec = props.get(field) if isinstance(props, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        typ = spec.get("type")
        if semantic == "query" and typ == "array":
            probe[field] = [value]
        elif semantic == "urls" and typ == "array":
            probe[field] = value if isinstance(value, list) else [value]
        elif semantic == "urls" and typ == "string":
            if isinstance(value, list) and value:
                probe[field] = value[0]
            elif value:
                probe[field] = value
        elif semantic in {"date_from", "date_to"} and typ == "array":
            probe[field] = [value]
        elif semantic == "max_items":
            if typ in {"integer", "number", None}:
                probe[field] = int(value)
        else:
            probe[field] = value

    # Fill safe defaults/examples for required fields only. Unknown required fields remain unresolved.
    unresolved: list[str] = []
    for field in required:
        if field in probe:
            continue
        spec = props.get(field, {}) if isinstance(props, dict) else {}
        if not isinstance(spec, dict):
            unresolved.append(field)
            continue
        if spec.get("default") is not None:
            probe[field] = spec["default"]
        elif spec.get("example") is not None:
            probe[field] = spec["example"]
        elif isinstance(spec.get("examples"), list) and spec["examples"]:
            probe[field] = spec["examples"][0]
        elif isinstance(spec.get("enum"), list) and spec["enum"]:
            probe[field] = spec["enum"][0]
        else:
            unresolved.append(field)
    return probe, sorted(unresolved), mapping


def sanitize_actor_input_types(schema: dict, payload: dict) -> tuple[dict, list[str]]:
    """Remove schema-incompatible example/default values before Apify validation.

    Some Actor metadata contains stale or malformed example values. In particular,
    sending an object where the Actor schema declares a string produces errors such
    as ``Field input.@ must be string``. Rather than guessing a semantic conversion,
    SIGNALYTH drops unsafe container values and surfaces required fields back to the
    connection wizard for an explicit value.
    """
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict):
        return dict(payload or {}), []
    cleaned: dict[str, Any] = {}
    mismatches: list[str] = []
    for key, value in dict(payload or {}).items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            cleaned[key] = value
            continue
        typ = spec.get("type")
        try:
            if typ == "string":
                if isinstance(value, str):
                    cleaned[key] = value
                elif isinstance(value, (int, float, bool)) and not isinstance(value, (dict, list)):
                    cleaned[key] = str(value).lower() if isinstance(value, bool) else str(value)
                else:
                    mismatches.append(str(key))
            elif typ == "array":
                cleaned[key] = value if isinstance(value, list) else [value]
            elif typ == "object":
                if isinstance(value, dict):
                    cleaned[key] = value
                else:
                    mismatches.append(str(key))
            elif typ == "boolean":
                if isinstance(value, bool):
                    cleaned[key] = value
                else:
                    mismatches.append(str(key))
            elif typ == "integer":
                if isinstance(value, int) and not isinstance(value, bool):
                    cleaned[key] = value
                elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    cleaned[key] = int(value)
                else:
                    mismatches.append(str(key))
            elif typ == "number":
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cleaned[key] = value
                elif isinstance(value, str):
                    cleaned[key] = float(value.strip())
                else:
                    mismatches.append(str(key))
            else:
                cleaned[key] = value
        except (TypeError, ValueError):
            mismatches.append(str(key))
    return cleaned, sorted(set(mismatches))


def flatten_sample_paths(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Return only scalar output paths safe for direct normalized-field mapping.

    Container paths such as ``author`` when the value is an object are excluded;
    otherwise an auto-map could stringify a whole object instead of selecting
    ``author.username``. Lists are inspected through their first item.
    """
    if depth > 4:
        return []
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.extend(flatten_sample_paths(v, path, depth + 1))
            elif isinstance(v, list):
                if v:
                    out.extend(flatten_sample_paths(v[0], path, depth + 1))
            else:
                out.append(path)
    elif isinstance(value, list) and value:
        out.extend(flatten_sample_paths(value[0], prefix, depth + 1))
    elif prefix:
        out.append(prefix)
    return out


def infer_output_mapping(items: list[dict]) -> dict:
    if not items:
        return {"mapping": {}, "confidence": 0.0, "available_paths": [], "warnings": ["No sample output was returned."]}
    paths: list[str] = []
    seen = set()
    for item in items[:10]:
        for path in flatten_sample_paths(item):
            if path not in seen:
                paths.append(path)
                seen.add(path)
    mapping: dict[str, str] = {}
    scores: list[float] = []
    for semantic, aliases in OUTPUT_ALIASES.items():
        alias_norms = [_keynorm(x) for x in aliases]
        best: tuple[int, str] | None = None
        for path in paths:
            leaf = path.split(".")[-1]
            n = _keynorm(leaf)
            score = 0
            if n in alias_norms:
                score = 100
            elif any(a in n or n in a for a in alias_norms if len(a) >= 4):
                score = 70
            if score and (best is None or score > best[0]):
                best = (score, path)
        if best:
            mapping[semantic] = best[1]
            scores.append(best[0] / 100)
    required_core = ["text", "date", "url"]
    warnings = [f"Could not auto-map {field}." for field in required_core if field not in mapping]
    confidence = round(sum(scores) / max(len(NORMALIZED_OUTPUT_FIELDS), 1), 3)
    return {"mapping": mapping, "confidence": confidence, "available_paths": paths, "warnings": warnings}


def mapping_signature(mapping: dict) -> str:
    raw = json.dumps(mapping or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_apify_client(token: str | None = None):
    token = token or settings.apify_token
    if not token:
        raise IntegrationNotConfigured("Apify is not connected.")
    try:
        from apify_client import ApifyClient
    except ImportError as exc:
        raise IntegrationNotConfigured("apify-client is not installed. Install requirements.txt first.") from exc
    return ApifyClient(token)


def test_apify_connection(token: str | None = None, client: Any | None = None) -> dict:
    client = client or _load_apify_client(token)
    try:
        user = client.user().get()
        data = _to_dict(user)
        if not data:
            raise IntegrationError("Apify accepted the request but returned no account data.")
        return {
            "ok": True,
            "provider": "apify",
            "username": data.get("username"),
            "tested_at": utcnow(),
        }
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"Apify connection test failed: {exc}") from exc


def lookup_actor(actor_ref: str, token: str | None = None, client: Any | None = None) -> dict:
    ref = parse_actor_reference(actor_ref)
    client = client or _load_apify_client(token)
    actor_client = client.actor(ref.normalized)
    try:
        actor = actor_client.get()
        actor_data = _to_dict(actor)
        if not actor_data:
            raise IntegrationError("Actor was not found or is not accessible with this token.")

        openapi: dict = {}
        build_data: dict = {}
        try:
            build_client = actor_client.default_build()
            try:
                openapi = build_client.get_open_api_definition() or {}
            except Exception:
                openapi = {}
            try:
                build_data = _to_dict(build_client.get())
            except Exception:
                build_data = {}
        except Exception:
            pass

        example = actor_data.get("exampleRunInput") or actor_data.get("example_run_input") or {}
        example = _to_dict(example)
        example_input = parse_json_maybe(example.get("body"))

        schema = _json_schema_from_openapi(openapi)
        if not schema:
            schema = parse_json_maybe(build_data.get("inputSchema") or build_data.get("input_schema"))
        # Some public Actors do not expose a useful OpenAPI input schema. In that
        # case, exampleRunInput still gives us safe field/type hints for mapping.
        schema = _merge_schema_with_example(schema, example_input)
        input_summary = schema_summary(schema)
        suggested = suggest_input_mapping(schema)
        return {
            "ok": True,
            "actor": {
                "id": actor_data.get("id"),
                "actor_id": f"{actor_data.get('username')}/{actor_data.get('name')}" if actor_data.get("username") and actor_data.get("name") else ref.normalized,
                "name": actor_data.get("name"),
                "title": actor_data.get("title") or actor_data.get("name"),
                "owner": actor_data.get("username"),
                "description": actor_data.get("description"),
                "is_public": actor_data.get("isPublic", actor_data.get("is_public")),
                "is_deprecated": actor_data.get("isDeprecated", actor_data.get("is_deprecated")),
                "has_no_dataset": actor_data.get("hasNoDataset", actor_data.get("has_no_dataset")),
                "store_url": ref.store_url or (
                    f"https://apify.com/{actor_data.get('username')}/{actor_data.get('name')}"
                    if actor_data.get("username") and actor_data.get("name") else None
                ),
            },
            "input_schema": schema,
            "input_summary": input_summary,
            "input_mapping_suggestion": suggested,
            "example_input": example_input,
            "schema_found": bool(input_summary["field_count"]),
            "looked_up_at": utcnow(),
        }
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"Actor lookup failed: {exc}") from exc


def validate_actor_input(actor_ref: str, run_input: dict, token: str | None = None, client: Any | None = None) -> dict:
    ref = parse_actor_reference(actor_ref)
    client = client or _load_apify_client(token)
    try:
        ok = client.actor(ref.normalized).validate_input(run_input)
        return {"ok": bool(ok), "actor_id": ref.normalized, "validated_at": utcnow()}
    except Exception as exc:
        raise IntegrationError(f"Actor input validation failed: {exc}") from exc


def smoke_test_actor(
    actor_ref: str,
    run_input: dict,
    *,
    max_items: int = 3,
    max_charge_usd: float = 0.10,
    token: str | None = None,
    client: Any | None = None,
) -> dict:
    ref = parse_actor_reference(actor_ref)
    if max_items < 1 or max_items > 10:
        raise ValueError("Smoke test max_items must be between 1 and 10.")
    if max_charge_usd <= 0 or max_charge_usd > 2:
        raise ValueError("Smoke test max_charge_usd must be greater than 0 and at most 2 USD.")
    client = client or _load_apify_client(token)
    try:
        from decimal import Decimal
        actor_client = client.actor(ref.normalized)
        run = actor_client.call(
            run_input=run_input,
            max_items=max_items,
            max_total_charge_usd=Decimal(str(max_charge_usd)),
        )
        run_data = _to_dict(run)
        dataset_id = run_data.get("defaultDatasetId") or run_data.get("default_dataset_id")
        if not dataset_id and hasattr(run, "default_dataset_id"):
            dataset_id = getattr(run, "default_dataset_id")
        if not dataset_id:
            raise IntegrationError("Actor run returned no default dataset.")
        # Read a little beyond the paid-row smoke cap so provider diagnostic rows cannot
        # hide a real sample or, worse, be mistaken for evidence. This does not increase
        # the Actor run's max_items/max_total_charge limits.
        page = client.dataset(dataset_id).list_items(limit=max(20, max_items * 3))
        items = getattr(page, "items", None)
        if items is None and isinstance(page, dict):
            items = page.get("items")
        raw_items = list(items or [])
        data_items, diagnostics = split_diagnostic_rows(raw_items)
        outcome = classify_actor_outcome(run_data, data_items, diagnostics)

        if not data_items:
            raise IntegrationError("Actor smoke test returned an empty dataset / no real data rows; diagnostics cannot verify a route.")
        if outcome != "success":
            raise IntegrationError(
                f"Actor smoke test was not a clean acquisition (outcome={outcome}); "
                "partial/transient/permanent failures cannot be committed as verified."
            )

        sample_items = data_items[:max_items]
        inferred = infer_output_mapping(sample_items)
        return {
            "ok": True,
            "actor_id": ref.normalized,
            "run_id": run_data.get("id"),
            "run_status": run_data.get("status"),
            "usage_total_usd": run_data.get("usageTotalUsd", run_data.get("usage_total_usd")),
            "dataset_id": dataset_id,
            "sample_count": len(sample_items),
            "diagnostic_count": len(diagnostics),
            "sample_items": sample_items,
            "output_mapping": inferred,
            "tested_at": utcnow(),
            "charge_note": "maxItems limits returned results; maxTotalChargeUsd is enforced where supported by the Actor pricing model. Compute-based costs may still apply.",
        }
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"Actor smoke test failed safely: {exc}") from exc


def _load_openai_client(api_key: str | None = None):
    api_key = api_key or settings.openai_api_key
    if not api_key:
        raise IntegrationNotConfigured("OpenAI is not connected.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise IntegrationNotConfigured("openai SDK is not installed. Install requirements.txt first.") from exc
    return OpenAI(api_key=api_key, max_retries=0, timeout=30.0)


def test_openai_connection(api_key: str | None = None, client: Any | None = None, models: list[str] | None = None) -> dict:
    client = client or _load_openai_client(api_key)
    models = models or [settings.signalyth_ai_bulk_model, settings.signalyth_ai_reasoning_model]
    accessible: list[str] = []
    missing: list[str] = []
    try:
        for model in models:
            try:
                result = client.models.retrieve(model)
                model_id = getattr(result, "id", None) or (_to_dict(result).get("id")) or model
                accessible.append(str(model_id))
            except Exception:
                missing.append(model)
        if not accessible:
            raise IntegrationError("OpenAI key was not able to access either configured SIGNALYTH model.")
        return {
            "ok": not missing,
            "provider": "openai",
            "configured_models": models,
            "accessible_models": accessible,
            "unavailable_models": missing,
            "tested_at": utcnow(),
        }
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"OpenAI connection test failed: {exc}") from exc


def smoke_test_openai(api_key: str | None = None, client: Any | None = None, model: str | None = None) -> dict:
    client = client or _load_openai_client(api_key)
    model = model or settings.signalyth_ai_bulk_model
    try:
        response = client.responses.create(
            model=model,
            store=False,
            instructions="Return exactly one compact JSON object matching the requested schema.",
            input="Classify this test mention: 'SIGNALYTH connection test - service works.'",
            text={
                "format": {
                    "type": "json_schema",
                    "name": "signalyth_connection_test",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["ok"]},
                            "language": {"type": "string"},
                        },
                        "required": ["status", "language"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
            max_output_tokens=128,
        )
        text = getattr(response, "output_text", None)
        parsed = json.loads(text or "{}")
        if parsed.get("status") != "ok":
            raise IntegrationError("OpenAI smoke test returned an unexpected structured result.")
        usage_obj = getattr(response, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = {
                "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            }
        return {
            "ok": True,
            "provider": "openai",
            "model": str(getattr(response, "model", model) or model),
            "response_id": getattr(response, "id", None),
            "usage": usage,
            "tested_at": utcnow(),
        }
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(f"OpenAI paid smoke test failed safely: {exc}") from exc


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def write_server_secrets(*, apify_token: str | None = None, openai_api_key: str | None = None, clear_apify: bool = False, clear_openai: bool = False, ai_enabled: bool | None = None, live_collection_enabled: bool | None = None, bulk_model: str | None = None, reasoning_model: str | None = None) -> dict:
    # Credential writes are PATCH/MERGE operations, never replacements. The lock is
    # required because FastAPI sync endpoints can run in different worker threads;
    # without it, two near-simultaneous Save clicks can both read the old file and
    # the last writer can accidentally erase the other provider credential.
    with _SECRETS_LOCK:
        current = _parse_env_file(SECRETS_FILE)
        if apify_token is not None:
            token = apify_token.strip()
            if "\n" in token or "\r" in token:
                raise ValueError("Apify token contains an invalid line break.")
            if len(token) < 12:
                raise ValueError("Apify token looks too short.")
            current["APIFY_TOKEN"] = token
            settings.apify_token = token
        if openai_api_key is not None:
            key = openai_api_key.strip()
            if "\n" in key or "\r" in key:
                raise ValueError("OpenAI API key contains an invalid line break.")
            if len(key) < 20:
                raise ValueError("OpenAI API key looks too short.")
            current["OPENAI_API_KEY"] = key
            settings.openai_api_key = key
        if clear_apify:
            current.pop("APIFY_TOKEN", None)
            settings.apify_token = ""
        if clear_openai:
            current.pop("OPENAI_API_KEY", None)
            settings.openai_api_key = ""
        if ai_enabled is not None:
            current["SIGNALYTH_AI_ENABLED"] = "true" if ai_enabled else "false"
            settings.signalyth_ai_enabled = bool(ai_enabled)
        if live_collection_enabled is not None:
            current["SIGNALYTH_DRY_RUN"] = "false" if live_collection_enabled else "true"
            settings.signalyth_dry_run = not bool(live_collection_enabled)
        for env_key, value, attr in (
            ("SIGNALYTH_AI_BULK_MODEL", bulk_model, "signalyth_ai_bulk_model"),
            ("SIGNALYTH_AI_REASONING_MODEL", reasoning_model, "signalyth_ai_reasoning_model"),
        ):
            if value is not None:
                model_id = value.strip()
                if not re.fullmatch(r"[A-Za-z0-9._:-]{2,120}", model_id):
                    raise ValueError("OpenAI model ID contains unsupported characters.")
                current[env_key] = model_id
                setattr(settings, attr, model_id)

        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# SIGNALYTH local secrets. Never commit or share this file."]
        for key in ("APIFY_TOKEN", "OPENAI_API_KEY", "SIGNALYTH_AI_ENABLED", "SIGNALYTH_DRY_RUN", "SIGNALYTH_AI_BULK_MODEL", "SIGNALYTH_AI_REASONING_MODEL"):
            if key in current:
                lines.append(f"{key}={current[key]}")
        temp = SECRETS_FILE.with_suffix(".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except Exception:
            pass
        temp.replace(SECRETS_FILE)
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except Exception:
            pass
    return integration_status()


def integration_status() -> dict:
    return {
        "apify": {
            "configured": bool(settings.apify_token),
            "token_exposed": False,
            "live_collection_enabled": not settings.signalyth_dry_run,
        },
        "openai": {
            "configured": bool(settings.openai_api_key),
            "key_exposed": False,
            "bulk_model": settings.signalyth_ai_bulk_model,
            "reasoning_model": settings.signalyth_ai_reasoning_model,
            "ai_enabled": settings.signalyth_ai_enabled,
        },
        "secret_storage": {
            "path": str(SECRETS_FILE),
            "server_side_only": True,
            "exists": SECRETS_FILE.exists(),
        },
    }

CANDIDATES_FILE = Path(settings.signalyth_runtime_config_dir) / "actor_candidates.json"


def _load_candidates() -> dict:
    if cloud_persistence.enabled:
        cloud_persistence.restore_config_file(CANDIDATES_FILE.name, CANDIDATES_FILE)
    if not CANDIDATES_FILE.exists():
        return {}
    try:
        data = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_candidates(data: dict) -> None:
    CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CANDIDATES_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(CANDIDATES_FILE)
    cloud_persistence.put_config_file(CANDIDATES_FILE.name, CANDIDATES_FILE)


def save_actor_candidate(source: str, lookup_result: dict) -> dict:
    data = _load_candidates()
    candidate = {
        "source": source,
        "actor": lookup_result.get("actor") or {},
        "input_schema": lookup_result.get("input_schema") or {},
        "input_summary": lookup_result.get("input_summary") or {},
        "input_mapping": lookup_result.get("input_mapping_suggestion") or {},
        "example_input": lookup_result.get("example_input") or {},
        "looked_up_at": lookup_result.get("looked_up_at") or utcnow(),
        "probe_input": None,
        "unresolved_required": [],
        "validated_at": None,
        "smoke_test": None,
        "output_mapping": {},
        "output_mapping_confidence": None,
        "available_output_paths": [],
    }
    data[source] = candidate
    _save_candidates(data)
    return candidate


def get_actor_candidate(source: str) -> dict | None:
    candidate = _load_candidates().get(source)
    return candidate if isinstance(candidate, dict) else None


def update_actor_candidate(source: str, changes: dict) -> dict:
    data = _load_candidates()
    candidate = data.get(source)
    if not isinstance(candidate, dict):
        raise LookupError("No Actor candidate has been looked up for this source.")
    candidate.update(changes)
    data[source] = candidate
    _save_candidates(data)
    return candidate


def clear_actor_candidate(source: str) -> None:
    data = _load_candidates()
    if source in data:
        del data[source]
        _save_candidates(data)
