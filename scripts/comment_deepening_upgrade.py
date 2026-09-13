from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if new in text:
        return
    if old not in text:
        raise RuntimeError(f"Patch anchor not found in {path}: {old[:120]!r}")
    write(path, text.replace(old, new, 1))


# ---------------------------------------------------------------------------
# 1. Baseline registry: install comment Actors, keep them OFF + unverified.
# ---------------------------------------------------------------------------
registry_path = ROOT / "config" / "source_registry.json"
registry = json.loads(registry_path.read_text(encoding="utf-8"))
comment_defaults = {
    "x": {
        "comment_actor_id": "xquik/x-tweet-scraper",
        "comment_route": "replies",
        "comment_input_field": "replyTweetIds",
        "comment_price_per_1000_hint": 0.15,
    },
    "tiktok": {
        "comment_actor_id": "epctex/tiktok-comment-scraper",
        "comment_route": "comments",
        "comment_input_field": "startUrls",
        "comment_price_per_1000_hint": 0.30,
    },
    "instagram": {
        "comment_actor_id": "scrapesmith/instagram-comments-scraper",
        "comment_route": "comments",
        "comment_input_field": "postUrls",
        "comment_price_per_1000_hint": 0.50,
    },
    "facebook": {
        "comment_actor_id": "scraper_one/facebook-comments-scraper",
        "comment_route": "comments",
        "comment_input_field": "postUrls",
        "comment_price_per_1000_hint": 0.30,
    },
}
for source, cfg in registry.items():
    cfg.setdefault("comment_enabled", False)
    cfg.setdefault("comment_price_per_1000_hint", None)
    cfg.setdefault("comment_max_per_parent", 40)
    cfg.setdefault("comment_max_parents", 12)
    cfg.setdefault("comment_include_replies", True)
    if source in comment_defaults:
        cfg.update(comment_defaults[source])
        # Installation is not verification. Paid live acceptance happens later.
        cfg["comment_deepening_status"] = "unverified"
        cfg["comment_last_smoke_test_at"] = None
        cfg["comment_output_mapping"] = {}
        cfg["comment_enabled"] = False
registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. API model: expose independent comment Actor settings via existing PATCH.
# ---------------------------------------------------------------------------
replace_once(
    "app/models.py",
    '''class SourceConfigUpdate(BaseModel):\n    actor_id: str | None = Field(default=None, min_length=3, max_length=200)\n    locked: bool | None = None\n    enabled: bool | None = None\n    price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)\n''',
    '''class SourceConfigUpdate(BaseModel):\n    actor_id: str | None = Field(default=None, min_length=3, max_length=200)\n    locked: bool | None = None\n    enabled: bool | None = None\n    price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)\n    # Comment/reply collection is a second evidence layer, independently switchable\n    # from the primary discovery Actor. Enabling remains fail-closed until the\n    # configured comment route has passed the explicit paid live smoke acceptance.\n    comment_enabled: bool | None = None\n    comment_price_per_1000_hint: float | None = Field(default=None, ge=0, le=10000)\n    comment_max_per_parent: int | None = Field(default=None, ge=1, le=1000)\n    comment_max_parents: int | None = Field(default=None, ge=1, le=100)\n    comment_include_replies: bool | None = None\n''',
)


# ---------------------------------------------------------------------------
# 3. Registry: persist toggles without destroying verification state.
# ---------------------------------------------------------------------------
replace_once(
    "app/registry.py",
    '''        cfg.setdefault("comment_output_mapping", {})\n    return data\n''',
    '''        cfg.setdefault("comment_output_mapping", {})\n        cfg.setdefault("comment_enabled", False)\n        cfg.setdefault("comment_price_per_1000_hint", None)\n        cfg.setdefault("comment_max_per_parent", 40)\n        cfg.setdefault("comment_max_parents", 12)\n        cfg.setdefault("comment_include_replies", True)\n    return data\n''',
)
replace_once(
    "app/registry.py",
    '''    allowed = {"actor_id", "locked", "enabled", "price_per_1000_hint"}\n    current = data[source]\n''',
    '''    allowed = {\n        "actor_id", "locked", "enabled", "price_per_1000_hint",\n        "comment_enabled", "comment_price_per_1000_hint",\n        "comment_max_per_parent", "comment_max_parents", "comment_include_replies",\n    }\n    current = data[source]\n    if changes.get("comment_enabled") is True:\n        if current.get("comment_deepening_status") != "verified":\n            raise PermissionError("Comments cannot be enabled until this comment Actor passes the paid live smoke verification.")\n        if not current.get("comment_actor_id"):\n            raise PermissionError("Comments cannot be enabled because no comment Actor is configured for this source.")\n''',
)
replace_once(
    "app/registry.py",
    '''        "comment_output_mapping": {},\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n\n\ndef rollback_source''',
    '''        "comment_output_mapping": {},\n        "comment_enabled": False,\n    })\n    _write_json_atomic(REGISTRY_PATH, data)\n    return deepcopy(current)\n\n\ndef rollback_source''',
)


# ---------------------------------------------------------------------------
# 4. Source capability contract + source-specific input adapters.
# ---------------------------------------------------------------------------
write(
    "app/services/source_capabilities.py",
    r'''from __future__ import annotations

from copy import deepcopy
import math
import re

# Public-schema capability knowledge. This is NOT a live verification record.
# Companion Actors remain OFF until explicitly paid-smoke-tested and committed.
SOURCE_CAPABILITIES = {
    "x": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "same_actor",
            "candidate_actor_id": "xquik/x-tweet-scraper",
            "input_route": "replies",
            "input_field": "replyTweetIds",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Primary Actor exposes direct-reply collection via mode=replies + replyTweetIds.",
        },
    },
    "instagram": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "companion_actor",
            "candidate_actor_id": "scrapesmith/instagram-comments-scraper",
            "input_route": "comments",
            "input_field": "postUrls",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Companion Actor accepts post/reel URLs and returns comments plus nested replies.",
        },
    },
    "facebook": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "companion_actor",
            "candidate_actor_id": "scraper_one/facebook-comments-scraper",
            "input_route": "comments",
            "input_field": "postUrls",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Companion Actor accepts Facebook post URLs and returns comment evidence.",
        },
    },
    "tiktok": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "companion_actor",
            "candidate_actor_id": "epctex/tiktok-comment-scraper",
            "input_route": "comments",
            "input_field": "startUrls",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Companion Actor accepts TikTok video/photo URLs; includeReplies is supported.",
        },
    },
    "youtube": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "companion_actor",
            "candidate_actor_id": "apidojo/youtube-comments-scraper",
            "input_route": "comments",
            "input_field": "startUrls",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Available for a later phase; not enabled by this four-social rollout.",
        },
    },
    "news": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "not_applicable",
            "candidate_actor_id": None,
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "News is publisher/article evidence; on-site comments are outside the default contract.",
        },
    },
}


def source_capabilities(source: str) -> dict:
    return deepcopy(SOURCE_CAPABILITIES.get(source, {
        "discovery": "unknown",
        "comment_deepening": {
            "mode": "unknown",
            "candidate_actor_id": None,
            "public_schema_known": False,
            "live_verified": False,
            "enabled": False,
            "note": "Capabilities unknown until schema inspection and live smoke verification.",
        },
    }))


def comments_forecast(source: str, requested: bool, registry_cfg: dict | None = None) -> dict:
    """Fail closed: installed/known is not the same as verified or enabled."""
    cap = source_capabilities(source)["comment_deepening"]
    registry_cfg = registry_cfg or {}
    route_status = str(registry_cfg.get("comment_deepening_status") or "unverified")
    configured_actor = registry_cfg.get("comment_actor_id") or cap.get("candidate_actor_id")
    live_verified = bool(cap.get("live_verified")) or route_status == "verified"
    configured_enabled = bool(registry_cfg.get("comment_enabled", False))
    if not requested:
        status = "not_requested"
    elif cap.get("mode") == "not_applicable":
        status = "not_applicable"
    elif live_verified and configured_enabled:
        status = "verified_available"
    elif live_verified and not configured_enabled:
        status = "verified_disabled"
    elif cap.get("public_schema_known"):
        status = "candidate_requires_live_verification"
    else:
        status = "unknown_requires_verification"
    return {
        "requested": bool(requested),
        "status": status,
        **cap,
        "candidate_actor_id": configured_actor,
        "live_verified": live_verified,
        "enabled": configured_enabled,
        "registry_route_status": route_status,
    }


def _x_id(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    match = re.search(r"/status/(\d+)", text, flags=re.I)
    return match.group(1) if match else text


def build_comment_deepening_input(
    source: str,
    seed_refs: list[str],
    max_items: int,
    *,
    max_per_parent: int | None = None,
    include_replies: bool = True,
) -> dict:
    """Build the public-schema input shape for a verified comment/reply route."""
    cap = source_capabilities(source).get("comment_deepening") or {}
    if cap.get("mode") == "not_applicable":
        raise ValueError(f"Comment deepening is not applicable to source {source}.")
    refs = [str(x).strip() for x in seed_refs if str(x or "").strip()]
    refs = list(dict.fromkeys(refs))[:40]
    if not refs:
        raise ValueError("At least one seed reference is required.")
    max_items = max(1, int(max_items))
    per_parent = max(1, int(max_per_parent or math.ceil(max_items / max(1, len(refs)))))

    if source == "x":
        return {"mode": "replies", "replyTweetIds": [_x_id(x) for x in refs], "maxItems": max_items}
    if source == "tiktok":
        return {"startUrls": refs, "includeReplies": bool(include_replies), "maxItems": max_items}
    if source == "instagram":
        # ScrapeSmith's schema uses a per-parent limit rather than maxItems.
        return {"postUrls": refs, "maxCommentsPerPost": per_parent, "sortOrder": "popular"}
    if source == "facebook":
        # Scraper One uses resultsLimit per post and Facebook's all/newest/relevant order.
        return {"postUrls": refs, "resultsLimit": per_parent, "commentsSortType": "all"}
    if source == "youtube":
        return {"startUrls": refs, "maxItems": max_items}
    field = cap.get("input_field")
    if not field:
        raise ValueError(f"No comment-route input field is known for source {source}.")
    return {field: refs, "maxItems": max_items}


def build_comment_smoke_input(source: str, seed_refs: list[str], max_items: int) -> dict:
    """Build a capped one-time paid smoke input; live validation remains mandatory."""
    capped = max(1, min(10, int(max_items)))
    return build_comment_deepening_input(
        source,
        seed_refs,
        capped,
        max_per_parent=capped,
        include_replies=True,
    )
''',
)


# ---------------------------------------------------------------------------
# 5. Dedicated comment normalizer. Posts and comments stay traceable separately.
# ---------------------------------------------------------------------------
write(
    "app/services/comment_deepening.py",
    r'''from __future__ import annotations

from hashlib import sha1
import re
from typing import Any

from app.services.normalizer import parse_date


def _nested(item: dict, path: str):
    cur: Any = item
    for part in str(path or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first(item: dict, paths: tuple[str, ...], default=None):
    for path in paths:
        value = _nested(item, path)
        if value not in (None, ""):
            return value
    return default


def _mapped(item: dict, mapping: dict | None, semantic: str, fallbacks: tuple[str, ...], default=None):
    path = (mapping or {}).get(semantic)
    if path:
        value = _nested(item, path)
        if value not in (None, ""):
            return value
    return _first(item, fallbacks, default)


def _int(value) -> int:
    try:
        return max(0, int(float(value or 0)))
    except Exception:
        return 0


def _video_id(value: str) -> str | None:
    match = re.search(r"/video/(\d+)", str(value or ""))
    return match.group(1) if match else None


def _instagram_flatten(items: list[dict]) -> list[dict]:
    out: list[dict] = []

    def visit(raw: dict, parent_comment_id: str | None = None, inherited_post: str | None = None):
        if not isinstance(raw, dict):
            return
        row = dict(raw)
        replies = row.pop("replies", []) or []
        if inherited_post and not row.get("postUrl"):
            row["postUrl"] = inherited_post
        if parent_comment_id:
            row["__parent_comment_id"] = parent_comment_id
            row["__is_reply"] = True
        out.append(row)
        this_id = str(row.get("commentId") or row.get("id") or "") or parent_comment_id
        for reply in replies if isinstance(replies, list) else []:
            visit(reply, this_id, row.get("postUrl") or inherited_post)

    for item in items:
        visit(item)
    return out


def _parent_from_seed(source: str, raw: dict, seed_refs: list[str]) -> str | None:
    if source in {"instagram", "facebook"}:
        return str(raw.get("postUrl") or raw.get("inputUrl") or (seed_refs[0] if len(seed_refs) == 1 else "")) or None
    if source == "tiktok":
        aweme = str(raw.get("aweme_id") or raw.get("awemeId") or raw.get("videoId") or "")
        if aweme:
            for ref in seed_refs:
                if _video_id(ref) == aweme:
                    return ref
        return seed_refs[0] if len(seed_refs) == 1 else None
    if source == "x":
        parent_id = str(raw.get("inReplyToId") or raw.get("inReplyToTweetId") or raw.get("sourceTweetId") or "")
        if parent_id:
            return parent_id
        return seed_refs[0] if len(seed_refs) == 1 else None
    return seed_refs[0] if len(seed_refs) == 1 else None


def normalize_comment_dataset(
    source: str,
    items: list[dict],
    *,
    seed_refs: list[str] | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """Normalize comment/reply Actors into the same evidence contract as primary rows.

    Every record keeps its parent post plus an explicit evidence_layer, so downstream
    analysis can use comments while still separating publisher content from audience response.
    """
    seed_refs = [str(x) for x in (seed_refs or []) if str(x or "").strip()]
    raw_items = _instagram_flatten(items) if source == "instagram" else [x for x in items if isinstance(x, dict)]
    out: list[dict] = []
    seen: set[str] = set()

    for raw in raw_items:
        if source == "x":
            text = _mapped(raw, mapping, "text", ("text", "fullText", "tweetText"), "")
            date_raw = _mapped(raw, mapping, "date", ("createdAt", "timestamp", "date"))
            author = _mapped(raw, mapping, "author", ("authorUsername", "author.username", "username"))
            native_id = _first(raw, ("id", "tweetId", "restId"))
            likes = _int(_mapped(raw, mapping, "likes", ("likeCount", "likes"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("replyCount", "commentsCount"), 0))
            shares = _int(_mapped(raw, mapping, "shares", ("retweetCount", "repostCount"), 0))
            url = _mapped(raw, mapping, "url", ("url", "tweetUrl"))
            layer = "reply"
            parent_comment_id = None
        elif source == "tiktok":
            text = _mapped(raw, mapping, "text", ("text", "commentText"), "")
            date_raw = _mapped(raw, mapping, "date", ("create_time", "createdAt", "createTime", "timestamp"))
            author = _mapped(raw, mapping, "author", ("user.unique_id", "user.uniqueId", "user.nickname", "username"))
            native_id = _first(raw, ("cid", "id", "commentId"))
            likes = _int(_mapped(raw, mapping, "likes", ("digg_count", "likeCount", "likesCount"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("reply_comment_total", "replyCount", "repliesCount"), 0))
            shares = 0
            url = _mapped(raw, mapping, "url", ("url", "commentUrl"))
            parent_comment_id = str(raw.get("reply_id") or raw.get("parentId") or "") or None
            layer = "reply" if parent_comment_id not in (None, "0") else "comment"
        elif source == "instagram":
            text = _mapped(raw, mapping, "text", ("text", "commentText"), "")
            date_raw = _mapped(raw, mapping, "date", ("timestamp", "createdAt", "created_at"))
            author = _mapped(raw, mapping, "author", ("username", "ownerUsername", "owner"))
            native_id = _first(raw, ("commentId", "id"))
            likes = _int(_mapped(raw, mapping, "likes", ("likesCount", "likeCount"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("repliesCount", "childCommentCount", "replyCount"), 0))
            shares = 0
            url = _mapped(raw, mapping, "url", ("commentUrl", "url"))
            parent_comment_id = str(raw.get("__parent_comment_id") or raw.get("parentCommentId") or "") or None
            layer = "reply" if raw.get("__is_reply") or parent_comment_id else "comment"
        elif source == "facebook":
            text = _mapped(raw, mapping, "text", ("commentText", "text", "message"), "")
            date_raw = _mapped(raw, mapping, "date", ("timestamp", "createdAt", "created_at"))
            author = _mapped(raw, mapping, "author", ("author.name", "authorName", "username"))
            native_id = _first(raw, ("id", "legacyId", "commentId"))
            likes = _int(_mapped(raw, mapping, "likes", ("reactionsCount", "likesCount", "likeCount"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("replyCount", "repliesCount"), 0))
            shares = 0
            url = _mapped(raw, mapping, "url", ("url", "commentUrl"))
            parent_comment_id = str(raw.get("parentCommentId") or raw.get("parent_id") or "") or None
            layer = "reply" if parent_comment_id else "comment"
        else:
            text = _mapped(raw, mapping, "text", ("text", "commentText", "content"), "")
            date_raw = _mapped(raw, mapping, "date", ("timestamp", "createdAt", "date"))
            author = _mapped(raw, mapping, "author", ("author", "username", "user.name"))
            native_id = _first(raw, ("id", "commentId"))
            likes = _int(_mapped(raw, mapping, "likes", ("likes", "likeCount"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("replyCount", "repliesCount"), 0))
            shares = 0
            url = _mapped(raw, mapping, "url", ("url", "commentUrl"))
            parent_comment_id = None
            layer = "comment"

        text = str(text or "").strip()
        if not text:
            continue
        parent_post = _parent_from_seed(source, raw, seed_refs)
        dt = parse_date(date_raw)
        raw_id = str(native_id or "").strip()
        stable = raw_id or sha1(f"{source}|{parent_post}|{author}|{text}".encode("utf-8", errors="ignore")).hexdigest()[:24]
        record_id = f"comment:{source}:{stable}"
        if record_id in seen:
            continue
        seen.add(record_id)
        if isinstance(author, dict):
            author = author.get("username") or author.get("name") or author.get("unique_id")
        metric_availability = {
            "followers_known": False,
            "views_known": False,
            "likes_known": True,
            "comments_known": replies > 0,
            "shares_known": False,
        }
        out.append({
            "id": record_id,
            "platform": source,
            "text": text,
            "date": dt.isoformat() if dt else None,
            "author": str(author) if author not in (None, "") else None,
            "followers": 0,
            "views": 0,
            "likes": likes,
            "comments": replies,
            "shares": shares,
            "url": str(url) if url else parent_post,
            "content_type": layer,
            "parent_post": parent_post,
            "parent_comment_id": parent_comment_id,
            "comment_id": raw_id or stable,
            "evidence_layer": layer,
            "collection_route": "comment_deepening",
            "metric_availability": metric_availability,
            "metric_coverage": round(sum(1 for v in metric_availability.values() if v) / 5.0, 4),
            "metric_paths": {},
            "normalization": {
                "date_raw": date_raw,
                "mapping_used": dict(mapping or {}),
                "contract": "comment-evidence-normalizer-v1",
            },
            "raw_data": raw,
        })
    return out
''',
)


# ---------------------------------------------------------------------------
# 6. Adaptive expansion: comments are a real second layer, not only shortfall fill.
# ---------------------------------------------------------------------------
write(
    "app/services/relevance_expansion.py",
    r'''from __future__ import annotations

from datetime import date
import math
from pathlib import Path
from typing import Callable

from app.registry import output_mapping_for, load_registry
from app.services.apify_service import ApifyRunner
from app.services.cleaning import clean_run
from app.services.collector import in_range
from app.services.comment_deepening import normalize_comment_dataset
from app.services.resilience import run_actor_resilient, split_diagnostic_rows
from app.services.normalizer import normalize_dataset
from app.services.smart_collection import (
    canonical_topic,
    detect_dominant_entity_collisions,
    refine_x_input_from_source_plan,
)
from app.services.storage import RunStore
from app.services.source_capabilities import build_comment_deepening_input, comments_forecast


def _x_source_plan(plan: dict) -> dict | None:
    for sp in plan.get("sources", []) or []:
        if sp.get("source") == "x":
            return sp
    return None


def _remaining_budget(status: dict, plan: dict) -> float:
    budget = status.get("budget") if isinstance(status.get("budget"), dict) else {}
    if budget.get("remaining_usd") not in (None, ""):
        try:
            return max(0.0, float(budget.get("remaining_usd")))
        except Exception:
            pass
    max_budget = float(plan.get("max_budget_usd", 0) or 0)
    spent = float(budget.get("spent_usd", 0) or 0)
    return max(0.0, max_budget - spent)


def _max_affordable_items(remaining: float, rate_per_1000: float | None, wanted: int) -> int:
    wanted = max(0, int(wanted))
    if wanted <= 0 or remaining <= 0:
        return 0
    if rate_per_1000 in (None, 0):
        return wanted
    try:
        affordable = int((remaining * 1000) // float(rate_per_1000))
    except Exception:
        return wanted
    return max(0, min(wanted, affordable))


def _charge_cap(remaining: float, rate_per_1000: float | None, items: int) -> float:
    if remaining <= 0 or items <= 0:
        return 0.0
    if rate_per_1000 in (None, 0):
        return min(remaining, 0.25)
    expected = float(rate_per_1000) * int(items) / 1000
    return min(remaining, max(0.001, expected * 1.25))


def _append_source_items(
    folder: Path,
    source: str,
    items: list[dict],
    date_from: date,
    date_to: date,
    *,
    mapping: dict | None = None,
    evidence_layer: str = "primary",
    seed_refs: list[str] | None = None,
) -> dict:
    store = RunStore()
    normalized_source_path = folder / f"normalized-{source}.json"
    normalized_all_path = folder / "normalized-all.json"
    data_items, diagnostics = split_diagnostic_rows(items)

    if evidence_layer == "comment":
        raw_path = folder / f"raw-comments-{source}.json"
        comment_norm_path = folder / f"normalized-comments-{source}.json"
        raw_existing = store.read(raw_path, []) or []
        raw_all = [*raw_existing, *[x for x in items if isinstance(x, dict)]]
        store.write(raw_path, raw_all)
        new_norm = normalize_comment_dataset(source, data_items, seed_refs=seed_refs or [], mapping=mapping)
        # Comments outside the requested research window are not analysis evidence.
        new_norm = [r for r in new_norm if in_range(r, date_from, date_to)]
        existing_comments = store.read(comment_norm_path, []) or []
        comment_dedup = {str(r.get("id")): r for r in [*existing_comments, *new_norm] if r.get("id")}
        store.write(comment_norm_path, list(comment_dedup.values()))
    else:
        raw_path = folder / f"raw-{source}.json"
        raw_existing = store.read(raw_path, []) or []
        raw_all = [*raw_existing, *[x for x in items if isinstance(x, dict)]]
        store.write(raw_path, raw_all)
        use_mapping = mapping if mapping is not None else output_mapping_for(source)
        new_norm = normalize_dataset(source, data_items, mapping=use_mapping)
        new_norm = [r for r in new_norm if in_range(r, date_from, date_to)]

    existing_source = store.read(normalized_source_path, []) or []
    existing_all = store.read(normalized_all_path, []) or []
    source_dedup = {str(r.get("id")): r for r in [*existing_source, *new_norm] if r.get("id")}
    all_dedup = {str(r.get("id")): r for r in [*existing_all, *new_norm] if r.get("id")}
    store.write(normalized_source_path, list(source_dedup.values()))
    store.write(normalized_all_path, list(all_dedup.values()))
    before_ids = {str(r.get("id")) for r in existing_all if r.get("id")}
    return {
        "raw_added": len(items),
        "diagnostic_added": len(diagnostics),
        "in_range_normalized_added": sum(1 for r in new_norm if str(r.get("id")) not in before_ids),
        "normalized_total": len(all_dedup),
        "evidence_layer": evidence_layer,
    }


def _comment_seed_refs(source: str, cleaned: list[dict], max_seeds: int = 40) -> tuple[list[str], list[dict]]:
    rows = [r for r in cleaned if str(r.get("platform") or "") == source and str(r.get("evidence_layer") or "primary") == "primary"]
    rows.sort(key=lambda r: (int(r.get("comments", 0) or 0), int(r.get("likes", 0) or 0)), reverse=True)
    refs, meta, seen = [], [], set()
    for row in rows:
        if int(row.get("comments", 0) or 0) <= 0:
            continue
        raw = row.get("raw_data") if isinstance(row.get("raw_data"), dict) else {}
        if source == "x":
            ref = raw.get("id") or raw.get("tweetId") or raw.get("tweet_id")
        else:
            ref = row.get("url") or raw.get("url") or raw.get("postUrl") or raw.get("webVideoUrl") or raw.get("link")
        ref = str(ref or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        meta.append({"ref": ref, "comments": int(row.get("comments", 0) or 0), "url": row.get("url")})
        if len(refs) >= max_seeds:
            break
    return refs, meta


def _update_budget(folder: Path, charged: float) -> dict:
    store = RunStore()
    status = store.read(folder / "status.json", {}) or {}
    budget = dict(status.get("budget") or {})
    max_usd = float(budget.get("max_usd", 0) or 0)
    spent = float(budget.get("spent_usd", 0) or 0) + max(0.0, float(charged or 0))
    spent = min(max_usd, spent) if max_usd > 0 else spent
    budget.update({"spent_usd": round(spent, 6), "remaining_usd": round(max(0.0, max_usd - spent), 6)})
    status["budget"] = budget
    store.write_status_folder(folder, status)
    return status


def adaptive_expand_after_cleaning(
    folder: Path,
    plan: dict,
    initial_report: dict,
    cancel_check: Callable[[], bool] | None = None,
    runner=None,
) -> dict:
    """Bounded adaptive discovery plus verified comment/reply deepening.

    Parent seeds come from the already-cleaned relevant evidence. Comments are then
    normalized as a separate evidence layer and cleaning is rerun, so a comment is
    not considered relevant merely because it sits under a relevant post.
    """
    cancel_check = cancel_check or (lambda: False)
    store = RunStore()
    shortfall = int(initial_report.get("trusted_sample_shortfall", 0) or 0)
    comments_requested = bool(plan.get("comments_requested"))
    audit = {
        "strategy_version": "smart-collection-v2.2-comment-layer",
        "status": "not_needed",
        "initial_trusted_shortfall": shortfall,
        "comments_requested": comments_requested,
        "steps": [], "warnings": [],
    }
    if plan.get("search_strategy_version") not in {"smart-collection-v2", "master30-search-v1"}:
        audit["reason"] = "strategy_not_supported"
        store.write(folder / "smart-collection-expansion.json", audit)
        return {"report": initial_report, "audit": audit}
    if shortfall <= 0 and not comments_requested:
        audit["reason"] = "trusted_target_met_and_comments_not_requested"
        store.write(folder / "smart-collection-expansion.json", audit)
        return {"report": initial_report, "audit": audit}

    date_from = date.fromisoformat(str(plan["date_from"]))
    date_to = date.fromisoformat(str(plan["date_to"]))
    if runner is None:
        try:
            runner = ApifyRunner()
        except Exception as exc:
            audit["reason"] = "adaptive_runner_unavailable"
            audit["warnings"].append(str(exc))
            store.write(folder / "smart-collection-expansion.json", audit)
            return {"report": initial_report, "audit": audit}

    report = initial_report
    audit["status"] = "attempted"
    registry = load_registry()

    def do_call(
        source: str,
        actor_id: str,
        kind: str,
        actor_input: dict,
        wanted: int,
        rate: float | None = None,
        mapping: dict | None = None,
        *,
        evidence_layer: str = "primary",
        seed_refs: list[str] | None = None,
    ):
        nonlocal report
        if cancel_check():
            audit["warnings"].append(f"{source}:{kind}:cancelled_before_call")
            return False
        status = store.read(folder / "status.json", {}) or {}
        remaining = _remaining_budget(status, plan)
        allowed = _max_affordable_items(remaining, rate, wanted)
        if allowed <= 0:
            audit["warnings"].append(f"{source}:{kind}:budget_exhausted")
            return False
        actor_input = dict(actor_input)
        # Only Actors whose public schema actually contains maxItems receive it.
        if "maxItems" in actor_input:
            actor_input["maxItems"] = min(int(actor_input.get("maxItems") or allowed), allowed)
        cap = _charge_cap(remaining, rate, allowed)
        if cap <= 0:
            audit["warnings"].append(f"{source}:{kind}:no_safe_charge_cap")
            return False
        try:
            resilient = run_actor_resilient(
                runner, actor_id, actor_input, max_items=allowed,
                max_charge_usd=cap, rate_per_1000=rate, max_calls=4,
            )
            charged = resilient.accounted_cost_usd
            _update_budget(folder, charged)
            combined = [*resilient.items, *resilient.diagnostics]
            append = _append_source_items(
                folder, source, combined, date_from, date_to, mapping=mapping,
                evidence_layer=evidence_layer, seed_refs=seed_refs,
            )
            report = clean_run(folder, plan=plan, cancel_check=cancel_check)
            audit["steps"].append({
                "source": source, "kind": kind, "actor_id": actor_id,
                "requested_items": allowed, "max_charge_usd": round(cap, 6),
                "accounted_cost_usd": round(charged, 6), "returned_items": len(resilient.items),
                "diagnostic_rows": len(resilient.diagnostics), "resilience_status": resilient.status,
                "attempts": resilient.attempts, "adaptive_actions": resilient.adaptive_actions,
                "failure_kinds": resilient.failure_kinds, "append": append,
                "trusted_after": int(report.get("trusted_records", 0) or 0),
                "trusted_shortfall_after": int(report.get("trusted_sample_shortfall", 0) or 0),
                "actor_meta": [dict(m or {}) for m in resilient.metas],
            })
            if resilient.status == "failed":
                audit["warnings"].append(f"{source}:{kind}:resilient_failure")
                return False
            return True
        except Exception as exc:
            audit["warnings"].append(f"{source}:{kind}:orchestrator_failed:{exc}")
            return False

    # Normal adaptive discovery is still driven only by a genuine sample shortfall.
    xplan = _x_source_plan(plan)
    if xplan and shortfall > 0:
        actor_id = str(xplan.get("actor_id") or "")
        if actor_id.casefold() == "xquik/x-tweet-scraper":
            normalized = store.read(folder / "normalized-all.json", []) or []
            core = canonical_topic(str(plan.get("topic") or ""), str(plan.get("market") or ""))
            collisions = detect_dominant_entity_collisions(normalized, core)
            audit["detected_collisions"] = collisions
            if collisions:
                exclusions = [x.get("exclude_term") for x in collisions if x.get("exclude_term")]
                wanted = min(max(shortfall, 1) * 2, int(plan.get("target_total", shortfall) or shortfall))
                inp, buckets = refine_x_input_from_source_plan(xplan, exclusions, wanted)
                audit["refinement_buckets"] = buckets
                if inp:
                    do_call("x", actor_id, "adaptive_refinement", inp, wanted, xplan.get("price_per_1000_hint"))
                    shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)
        else:
            audit["warnings"].append("x:adaptive_refinement_blocked_for_unverified_actor_capability")

    # Comment deepening is independent from shortfall: it is requested audience evidence.
    if comments_requested:
        cleaned = store.read(folder / "cleaning" / "cleaned.json", []) or []
        selected = [sp for sp in (plan.get("sources") or []) if sp.get("source")]
        for sp in selected:
            if cancel_check():
                break
            source = str(sp.get("source"))
            if source not in {"x", "tiktok", "instagram", "facebook"}:
                continue
            cfg = registry.get(source) or {}
            comment_info = comments_forecast(source, True, cfg)
            if comment_info.get("status") == "verified_disabled":
                audit["warnings"].append(f"{source}:comment_actor_verified_but_disabled_in_settings")
                continue
            if comment_info.get("status") != "verified_available":
                audit["warnings"].append(f"{source}:comment_deepening_blocked_until_live_route_verification")
                continue
            # Durable idempotence: a resumed run must not pay for the same layer twice.
            existing_comments = store.read(folder / f"normalized-comments-{source}.json", []) or []
            if existing_comments:
                audit["warnings"].append(f"{source}:comment_deepening_already_collected")
                continue
            actor_id = str(cfg.get("comment_actor_id") or comment_info.get("candidate_actor_id") or "")
            if not actor_id:
                audit["warnings"].append(f"{source}:comment_deepening_missing_actor")
                continue
            max_parents = max(1, min(40, int(cfg.get("comment_max_parents") or 12)))
            max_per_parent = max(1, min(1000, int(cfg.get("comment_max_per_parent") or 40)))
            refs, seed_meta = _comment_seed_refs(source, cleaned, max_seeds=max_parents)
            if not refs:
                audit["warnings"].append(f"{source}:no_relevant_parent_with_comments")
                continue
            max_possible = len(refs) * max_per_parent
            # Second layer should add depth without allowing one viral thread to dominate.
            baseline = max(20, int(math.ceil(int(sp.get("target_items", 0) or 0) * 0.35)))
            wanted = min(max_possible, max(baseline, min(shortfall, max_possible)))
            try:
                inp = build_comment_deepening_input(
                    source, refs, wanted,
                    max_per_parent=max_per_parent,
                    include_replies=bool(cfg.get("comment_include_replies", True)),
                )
            except ValueError as exc:
                audit["warnings"].append(f"{source}:comment_input_unavailable:{exc}")
                continue
            audit.setdefault("comment_seeds", {})[source] = seed_meta
            mapping = cfg.get("comment_output_mapping") or None
            rate = cfg.get("comment_price_per_1000_hint")
            do_call(
                source, actor_id, "comment_deepening", inp, wanted, rate, mapping=mapping,
                evidence_layer="comment", seed_refs=refs,
            )
            shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)

    final_shortfall = int(report.get("trusted_sample_shortfall", 0) or 0)
    audit["final_trusted_shortfall"] = final_shortfall
    audit["comment_evidence_total"] = sum(
        len(store.read(folder / f"normalized-comments-{s}.json", []) or [])
        for s in ("x", "tiktok", "instagram", "facebook")
    )
    audit["status"] = "target_met" if final_shortfall <= 0 else "exhausted_or_shortfall"
    audit["stop_rule"] = "Stop at target/budget/source exhaustion; comment routes must be verified + enabled and comments are re-cleaned for relevance."
    store.write(folder / "smart-collection-expansion.json", audit)
    return {"report": report, "audit": audit}
''',
)


# ---------------------------------------------------------------------------
# 7. Run lifecycle: invoke the adaptive layer whenever comments were requested,
#    even when the primary analyzable target was already met.
# ---------------------------------------------------------------------------
replace_once(
    "app/services/run_manager.py",
    '''            if int(report.get("trusted_sample_shortfall", 0) or 0) > 0 and plan.get("search_strategy_version") in {"smart-collection-v2", "master30-search-v1"}:\n''',
    '''            needs_adaptive_layer = int(report.get("trusted_sample_shortfall", 0) or 0) > 0 or bool(plan.get("comments_requested"))\n            if needs_adaptive_layer and plan.get("search_strategy_version") in {"smart-collection-v2", "master30-search-v1"}:\n''',
)


# ---------------------------------------------------------------------------
# 8. Reserve part of the user's hard budget for comments only when at least one
#    selected comment route is already verified + enabled.
# ---------------------------------------------------------------------------
replace_once(
    "app/services/query_planner.py",
    '''    if selected:\n        weights={s:max(0.05,float(estimates[s] or 0.05)) for s in selected}; den=sum(weights.values())\n        source_budgets={s:float(draft.max_budget_usd)*weights[s]/den for s in selected}\n    else: source_budgets={}\n''',
    '''    live_comment_sources = [\n        s for s in selected\n        if draft.comments\n        and bool(registry[s].get("comment_enabled", False))\n        and registry[s].get("comment_deepening_status") == "verified"\n    ]\n    primary_budget = float(draft.max_budget_usd) * (0.75 if live_comment_sources else 1.0)\n    if selected:\n        weights={s:max(0.05,float(estimates[s] or 0.05)) for s in selected}; den=sum(weights.values())\n        source_budgets={s:primary_budget*weights[s]/den for s in selected}\n    else: source_budgets={}\n''',
)


# ---------------------------------------------------------------------------
# 9. Semantic refill must never overwrite already collected comment evidence.
# ---------------------------------------------------------------------------
replace_once(
    "app/services/semantic_refill.py",
    '''            dedup={str(r.get("id")):r for r in normalized if r.get("id")}; normalized=list(dedup.values())[:max(target*2,target)]\n            before=store.read(folder/f"normalized-{source}.json",[]) or []; before_ids={str(r.get("id")) for r in before if r.get("id")}\n            store.write(folder/f"normalized-{source}.json",normalized)\n''',
    '''            dedup={str(r.get("id")):r for r in normalized if r.get("id")}; normalized=list(dedup.values())[:max(target*2,target)]\n            # Comment/reply evidence is a separate acquired layer and must survive a\n            # later primary semantic refill. Merge it back before rewriting the source file.\n            comment_rows=store.read(folder/f"normalized-comments-{source}.json",[]) or []\n            normalized_dedup={str(r.get("id")):r for r in [*normalized,*comment_rows] if r.get("id")}\n            normalized=list(normalized_dedup.values())\n            before=store.read(folder/f"normalized-{source}.json",[]) or []; before_ids={str(r.get("id")) for r in before if r.get("id")}\n            store.write(folder/f"normalized-{source}.json",normalized)\n''',
)


# ---------------------------------------------------------------------------
# 10. Settings UI: compact source-level enable/disable controls, backed by the
#     existing /api/sources PATCH endpoint. No secret is exposed client-side.
# ---------------------------------------------------------------------------
main_anchor = '''    html=html.replace("</body>",panel+"</body>") if "</body>" in html else html+panel\n    return HTMLResponse(html)\n'''
comment_panel = r"""    comment_panel = r'''<style>
#commentActorSettings{position:fixed;left:18px;bottom:18px;z-index:99998;width:min(440px,calc(100vw - 36px));background:#fff;border:1px solid #d8d5cf;border-radius:14px;box-shadow:0 14px 45px rgba(0,0,0,.14);font-family:Inter,Arial,sans-serif;color:#181818}
#commentActorSettings summary{cursor:pointer;padding:12px 14px;font-weight:750;font-size:13px}
#commentActorSettings .ca-body{padding:0 14px 14px;font-size:12px;max-height:55vh;overflow:auto}
.ca-row{border-top:1px solid #eee9e2;padding:10px 0}.ca-head{display:flex;justify-content:space-between;gap:12px;align-items:center}.ca-name{font-weight:750}.ca-meta{font-size:10px;color:#6b665f;overflow-wrap:anywhere;margin-top:4px}.ca-status{font-size:10px;font-weight:700}.ca-controls{display:flex;align-items:center;gap:10px;margin-top:7px}.ca-controls input[type=number]{width:62px;padding:4px;border:1px solid #d4cec6;border-radius:6px}.ca-warn{color:#9a5c18}
</style><details id="commentActorSettings"><summary>Comment Actors · Settings</summary><div class="ca-body"><div style="color:#6b665f;margin-bottom:8px">Second evidence layer for X, TikTok, Instagram and Facebook. Installed ≠ verified; ON is allowed only after live acceptance.</div><div id="commentActorRows">Loading…</div></div></details><script>
(()=>{
const wanted=['x','tiktok','instagram','facebook'];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function patchSource(source,payload){const r=await fetch(`/api/sources/${source}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!r.ok){let msg='Update failed';try{msg=(await r.json()).detail||msg}catch(e){}alert(msg);throw new Error(msg)}return r.json()}
async function load(){try{const reg=await fetch('/api/sources').then(r=>r.json());const root=document.getElementById('commentActorRows');root.innerHTML=wanted.map(source=>{const c=reg[source]||{};const verified=c.comment_deepening_status==='verified';const enabled=!!c.comment_enabled;const status=verified?(enabled?'VERIFIED · ON':'VERIFIED · OFF'):'CONFIGURED · NEEDS LIVE VERIFICATION';return `<div class="ca-row"><div class="ca-head"><div><span class="ca-name">${esc(c.label||source.toUpperCase())}</span><div class="ca-meta">${esc(c.comment_actor_id||'No comment actor configured')}</div></div><span class="ca-status ${verified?'':'ca-warn'}">${status}</span></div><div class="ca-controls"><label><input type="checkbox" data-comment-toggle="${source}" ${enabled?'checked':''} ${verified?'':'disabled'}> Include comments</label><label>max / parent <input type="number" min="1" max="1000" value="${Number(c.comment_max_per_parent||40)}" data-comment-max="${source}"></label></div></div>`}).join('');root.querySelectorAll('[data-comment-toggle]').forEach(el=>el.addEventListener('change',async()=>{try{await patchSource(el.dataset.commentToggle,{comment_enabled:el.checked});await load()}catch(e){el.checked=!el.checked}}));root.querySelectorAll('[data-comment-max]').forEach(el=>el.addEventListener('change',async()=>{const v=Math.max(1,Math.min(1000,Number(el.value||40)));try{await patchSource(el.dataset.commentMax,{comment_max_per_parent:v})}catch(e){}await load()}));}catch(e){const root=document.getElementById('commentActorRows');if(root)root.textContent='Could not load comment Actor settings.'}}
load();
})();
</script>'''
    html=html.replace("</body>",panel+comment_panel+"</body>") if "</body>" in html else html+panel+comment_panel
    return HTMLResponse(html)
"""
replace_once("app/main.py", main_anchor, comment_panel)


# ---------------------------------------------------------------------------
# 11. Offline unit coverage (no Apify/OpenAI calls).
# ---------------------------------------------------------------------------
write(
    "tests/test_comment_deepening.py",
    r'''from app.models import SourceConfigUpdate
from app.services.comment_deepening import normalize_comment_dataset
from app.services.source_capabilities import build_comment_deepening_input, comments_forecast


def test_comment_input_shapes_are_actor_specific():
    assert build_comment_deepening_input("x", ["https://x.com/u/status/123"], 7)["replyTweetIds"] == ["123"]
    assert build_comment_deepening_input("tiktok", ["https://www.tiktok.com/@u/video/123"], 9)["includeReplies"] is True
    ig = build_comment_deepening_input("instagram", ["https://www.instagram.com/p/ABC/"], 12, max_per_parent=5)
    assert ig == {"postUrls": ["https://www.instagram.com/p/ABC/"], "maxCommentsPerPost": 5, "sortOrder": "popular"}
    fb = build_comment_deepening_input("facebook", ["https://www.facebook.com/x/posts/1"], 12, max_per_parent=6)
    assert fb["resultsLimit"] == 6 and fb["commentsSortType"] == "all"


def test_instagram_comments_and_nested_replies_become_separate_evidence():
    rows = normalize_comment_dataset("instagram", [{
        "postUrl": "https://www.instagram.com/p/ABC/", "commentId": "c1", "commentUrl": "https://ig/c1",
        "text": "top", "timestamp": 1700000000, "likesCount": 2, "username": "a",
        "replies": [{"commentId": "r1", "text": "reply", "timestamp": 1700000001, "username": "b"}],
    }], seed_refs=["https://www.instagram.com/p/ABC/"])
    assert len(rows) == 2
    assert rows[0]["evidence_layer"] == "comment"
    assert rows[1]["evidence_layer"] == "reply"
    assert rows[1]["parent_comment_id"] == "c1"
    assert all(r["parent_post"] == "https://www.instagram.com/p/ABC/" for r in rows)


def test_tiktok_comment_normalization_keeps_parent_video():
    rows = normalize_comment_dataset("tiktok", [{
        "cid": "55", "text": "service issue", "create_time": 1700000000,
        "digg_count": 3, "reply_comment_total": 1, "aweme_id": "123",
        "user": {"unique_id": "person"},
    }], seed_refs=["https://www.tiktok.com/@u/video/123"])
    assert rows[0]["parent_post"].endswith("/video/123")
    assert rows[0]["text"] == "service issue"
    assert rows[0]["author"] == "person"


def test_facebook_comment_normalization_matches_actor_shape():
    rows = normalize_comment_dataset("facebook", [{
        "commentText": "hello", "id": "fb1", "timestamp": 1742045152000,
        "url": "https://facebook.com/post?comment_id=1", "postUrl": "https://facebook.com/post",
        "author": {"name": "Zack"}, "reactionsCount": "5",
    }], seed_refs=["https://facebook.com/post"])
    assert rows[0]["parent_post"] == "https://facebook.com/post"
    assert rows[0]["author"] == "Zack"
    assert rows[0]["likes"] == 5


def test_x_reply_normalization_is_reply_layer():
    rows = normalize_comment_dataset("x", [{
        "id": "r1", "text": "reply", "createdAt": "2026-01-01T00:00:00Z",
        "authorUsername": "u", "inReplyToId": "123", "url": "https://x.com/u/status/r1",
    }], seed_refs=["123"])
    assert rows[0]["evidence_layer"] == "reply"
    assert rows[0]["parent_post"] == "123"


def test_forecast_requires_both_verification_and_settings_enable():
    cfg = {"comment_actor_id": "a/b", "comment_deepening_status": "verified", "comment_enabled": False}
    assert comments_forecast("facebook", True, cfg)["status"] == "verified_disabled"
    cfg["comment_enabled"] = True
    assert comments_forecast("facebook", True, cfg)["status"] == "verified_available"


def test_source_update_model_exposes_comment_controls():
    row = SourceConfigUpdate(comment_enabled=True, comment_max_per_parent=55, comment_max_parents=9)
    assert row.comment_enabled is True
    assert row.comment_max_per_parent == 55
    assert row.comment_max_parents == 9
''',
)

print("Comment deepening upgrade applied.")
