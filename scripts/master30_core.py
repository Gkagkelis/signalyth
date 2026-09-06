from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(rel: str, text: str):
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def must_replace(rel: str, old: str, new: str, count: int = 1):
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Master30 patch anchor not found in {rel}: {old[:120]!r}")
    replaced = text.replace(old, new, count)
    path.write_text(replaced, encoding="utf-8")


# ---------------- Models: research scope, keyword roles, query preview, top-up contracts ----------------
must_replace(
    "app/models.py",
    'SampleMode = Literal["automatic", "perSource"]\n',
    'SampleMode = Literal["automatic", "perSource"]\n'
    'SearchStrategy = Literal["topic_first", "context_first", "balanced_smart"]\n'
    'KeywordRole = Literal["context", "required_context", "alias", "exclude", "watch"]\n'
)
must_replace(
    "app/models.py",
    '    keywords: list[str] = Field(min_length=1, max_length=50)\n',
    '    keywords: list[str] = Field(default_factory=list, max_length=50)\n'
)
must_replace(
    "app/models.py",
    '    exclusions: list[str] = Field(default_factory=list, max_length=50)\n',
    '    exclusions: list[str] = Field(default_factory=list, max_length=50)\n'
    '    search_strategy: SearchStrategy = "balanced_smart"\n'
    '    keyword_roles: dict[str, KeywordRole] = Field(default_factory=dict)\n'
    '    query_overrides: dict[str, list[str]] = Field(default_factory=dict)\n'
    '    benchmark: dict[str, object] | None = None\n'
)
must_replace(
    "app/models.py",
    'class SourcePlan(BaseModel):\n    source: SourceName\n    actor_id: str\n    target_items: int\n    estimated_cost_usd: float | None\n    price_per_1000_hint: float | None = None\n    date_strategy: str\n    market_strategy: str\n    queries: list[str]\n    subruns: list[SubRunPlan]\n    intent_buckets: list[dict] = Field(default_factory=list)\n',
    'class SourcePlan(BaseModel):\n'
    '    source: SourceName\n'
    '    actor_id: str\n'
    '    target_items: int\n'
    '    estimated_cost_usd: float | None\n'
    '    price_per_1000_hint: float | None = None\n'
    '    date_strategy: str\n'
    '    market_strategy: str\n'
    '    queries: list[str]\n'
    '    subruns: list[SubRunPlan]\n'
    '    topup_subruns: list[SubRunPlan] = Field(default_factory=list)\n'
    '    semantic_topup_subruns: list[SubRunPlan] = Field(default_factory=list)\n'
    '    intent_buckets: list[dict] = Field(default_factory=list)\n'
    '    query_preview: list[dict] = Field(default_factory=list)\n'
    '    source_budget_usd: float = 0.0\n'
    '    source_contract_version: str = "actor-contract-v3"\n'
)
must_replace(
    "app/models.py",
    '    preflight_forecast: dict = Field(default_factory=dict)\n    sources: list[SourcePlan]\n',
    '    preflight_forecast: dict = Field(default_factory=dict)\n'
    '    search_strategy: SearchStrategy = "balanced_smart"\n'
    '    keyword_roles: dict[str, str] = Field(default_factory=dict)\n'
    '    query_preview: dict[str, list[dict]] = Field(default_factory=dict)\n'
    '    topup_policy: str = "shared_source_target_until_analyzable_or_exhausted"\n'
    '    master_spec_version: str = "SIGNALYTH-master30-v1"\n'
    '    benchmark: dict[str, object] | None = None\n'
    '    sources: list[SourcePlan]\n'
)


NORMALIZER = r'''from __future__ import annotations

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha1
import re

# Universal fallbacks. Source-specific paths below are tried first.
TEXT_KEYS = (
    "text", "caption", "postText", "content", "description", "desc", "title", "body", "message", "snippet",
)
DATE_KEYS = (
    "date", "createdAt", "created_at", "timestamp", "publishedAt", "published_at", "createTime", "createTimeISO",
    "takenAtIso", "takenAt", "published", "publishDate", "uploadDate", "postedAt",
)
AUTHOR_KEYS = (
    "authorUsername", "author.username", "author.name", "userName", "username", "ownerUsername", "channelName",
    "channel.name", "pageName", "sourceName", "ownerFullName", "author",
)
URL_KEYS = ("url", "postUrl", "webVideoUrl", "link", "inputUrl", "originalUrl", "canonicalUrl")
FOLLOWER_KEYS = (
    "followers", "followerCount", "authorFollowers", "followersCount", "authorMeta.followers", "authorStats.followerCount",
    "author.followerCount", "channel.subscribers", "channel.subscriberCount",
)
VIEW_KEYS = (
    "views", "viewCount", "playCount", "videoPlayCount", "igPlayCount", "fbPlayCount", "stats.playCount",
)
LIKE_KEYS = (
    "likesCount", "likeCount", "diggCount", "likes", "fbLikeCount", "reactionsCount", "reactionCount", "stats.diggCount",
)
COMMENT_KEYS = (
    "commentsCount", "commentCount", "replyCount", "comments", "stats.commentCount",
)
SHARE_KEYS = (
    "shares", "shareCount", "sharesCount", "retweetCount", "repostCount", "stats.shareCount",
)

SOURCE_PATHS = {
    "x": {
        "text": ("text", "fullText", "tweetText"),
        "date": ("createdAt", "timestamp", "date"),
        "author": ("authorUsername", "author.username", "username"),
        "followers": ("authorFollowers", "author.followers", "author.followersCount"),
        "views": ("viewCount", "views"), "likes": ("likeCount", "likes"),
        "comments": ("replyCount", "commentsCount"), "shares": ("retweetCount", "repostCount", "shares"),
        "url": ("url", "tweetUrl"), "native_id": ("id", "tweetId", "restId"),
    },
    "tiktok": {
        "text": ("desc", "text", "caption"),
        "date": ("createTime", "createTimeISO", "timestamp"),
        "author": ("authorMeta.name", "authorMeta.nickName", "author.uniqueId", "authorMeta.uniqueId", "author.username"),
        "followers": ("authorStats.followerCount", "authorMeta.fans", "author.followerCount"),
        "views": ("stats.playCount", "playCount"), "likes": ("stats.diggCount", "diggCount"),
        "comments": ("stats.commentCount", "commentCount"), "shares": ("stats.shareCount", "shareCount"),
        "url": ("webVideoUrl", "url"), "native_id": ("id", "videoId"),
    },
    "instagram": {
        "text": ("caption", "text", "alt"),
        "date": ("timestamp", "takenAtIso", "takenAt", "createdAt"),
        "author": ("ownerUsername", "owner.username", "username"),
        "followers": ("owner.followersCount", "ownerFollowersCount", "followersCount"),
        "views": ("videoPlayCount", "igPlayCount", "videoViewCount"), "likes": ("likesCount", "likeCount"),
        "comments": ("commentsCount", "commentCount"), "shares": ("sharesCount", "shareCount"),
        "url": ("url", "postUrl", "inputUrl"), "native_id": ("id", "shortCode", "shortcode"),
    },
    "facebook": {
        "text": ("postText", "text", "message"),
        "date": ("timestamp", "date", "createdAt"),
        "author": ("author.name", "pageName", "authorName", "author.nameText"),
        "followers": ("author.followers", "pageFollowers", "followersCount"),
        "views": ("viewsCount", "viewCount", "videoViewCount"), "likes": ("reactionsCount", "likesCount", "likeCount"),
        "comments": ("commentsCount", "commentCount"), "shares": ("sharesCount", "shareCount", "shares"),
        "url": ("url", "postUrl"), "native_id": ("postId", "id", "facebookId"),
    },
    "youtube": {
        "text": ("title", "description", "text"),
        "date": ("publishDate", "uploadDate", "publishedAt", "published_at", "date"),
        "author": ("channel.name", "channelName", "channel.title", "author"),
        "followers": ("channel.subscribers", "channel.subscriberCount", "subscribersCount"),
        "views": ("viewCount", "views"), "likes": ("likeCount", "likes"),
        "comments": ("commentCount", "commentsCount", "comments"), "shares": ("shareCount", "shares"),
        "url": ("url", "videoUrl", "webVideoUrl"), "native_id": ("id", "videoId"),
    },
    "news": {
        "text": ("snippet", "title", "description", "content"),
        "date": ("publishedAt", "published_at", "date", "published"),
        "author": ("sourceName", "source.name", "publisher", "author"),
        "followers": (), "views": (), "likes": (), "comments": (), "shares": (),
        "url": ("originalUrl", "link", "url"), "native_id": ("id", "articleId"),
    },
}


def nested(item, key):
    cur = item
    for part in str(key or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def first(item, keys, default=None):
    for k in keys:
        v = nested(item, k)
        if v not in (None, ""):
            return v
    return default


def _paths(source: str, semantic: str, fallback=()):
    return tuple((SOURCE_PATHS.get(source) or {}).get(semantic) or ()) + tuple(fallback or ())


def mapped(item: dict, mapping: dict | None, field: str, fallback_keys=(), default=None):
    path = (mapping or {}).get(field)
    if path:
        value = nested(item, path)
        if value not in (None, ""):
            return value
    return first(item, fallback_keys, default)


def as_int(v):
    try:
        return max(0, int(float(v or 0)))
    except Exception:
        return 0


def _metric(item: dict, mapping: dict | None, source: str, field: str, fallback_keys):
    path = (mapping or {}).get(field)
    if path:
        raw = nested(item, path)
        if raw not in (None, ""):
            return as_int(raw), True, path
    for key in _paths(source, field, fallback_keys):
        raw = nested(item, key)
        if raw not in (None, ""):
            return as_int(raw), True, key
    return 0, False, None


def parse_date(v, now: datetime | None = None):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        value = float(v)
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    s0 = str(v).strip()
    # Numeric epoch sometimes arrives as a string.
    try:
        if re.fullmatch(r"\d{9,16}(?:\.\d+)?", s0):
            return parse_date(float(s0), now=now)
    except Exception:
        pass
    s = s0.replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s0)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s0, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    m = re.fullmatch(r"(?i)\s*(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\s*", s0)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        base = now or datetime.now(timezone.utc)
        days = n * {"day": 1, "week": 7, "month": 30, "year": 365}.get(unit, 0)
        if unit == "hour": return base - timedelta(hours=n)
        if unit == "minute": return base - timedelta(minutes=n)
        return base - timedelta(days=days)
    return None


def classify_source_row(source: str, item: dict) -> str:
    if not isinstance(item, dict):
        return "diagnostic"
    # Provider accounting/error rows must never become evidence.
    if any(k in item for k in ("errorReason", "errorMessage", "requestError", "debugMessage")) and not any(k in item for k in ("text", "caption", "postText", "title", "desc")):
        return "diagnostic"
    if source == "instagram":
        has_content = any(first(item, (k,)) not in (None, "") for k in ("caption", "timestamp", "takenAtIso", "shortCode", "ownerUsername"))
        # Hashtag search discovery rows commonly expose hashtag/name/count/URL but no post timestamp/caption.
        metadata_hint = any(k in item for k in ("hashtag", "postsCount", "postCount", "profilePicUrl", "searchResult"))
        if metadata_hint and not has_content:
            return "metadata"
    if source == "tiktok" and any(k in item for k in ("desc", "createTime", "webVideoUrl", "id")):
        return "content"
    if source == "facebook" and any(k in item for k in ("postText", "timestamp", "postId", "url")):
        return "content"
    if source == "youtube" and any(k in item for k in ("videoId", "publishDate", "uploadDate", "title", "url")):
        return "content"
    if source == "news" and any(k in item for k in ("title", "snippet", "publishedAt", "link", "originalUrl")):
        return "content"
    if source == "x" and any(k in item for k in ("text", "createdAt", "tweetId", "id", "url")):
        return "content"
    text = first(item, TEXT_KEYS)
    url = first(item, URL_KEYS)
    return "content" if text not in (None, "") or url not in (None, "") else "metadata"


def normalize_item(source: str, item: dict, mapping: dict | None = None):
    if classify_source_row(source, item) != "content":
        return None
    text = str(mapped(item, mapping, "text", _paths(source, "text", TEXT_KEYS), "") or "").strip()
    url = mapped(item, mapping, "url", _paths(source, "url", URL_KEYS))
    author = mapped(item, mapping, "author", _paths(source, "author", AUTHOR_KEYS))
    # Never stringify an entire nested author object.
    if isinstance(author, dict):
        author = first(author, ("username", "name", "uniqueId", "nickName"))
    raw_date = mapped(item, mapping, "date", _paths(source, "date", DATE_KEYS))
    dt = parse_date(raw_date)
    if not text and not url:
        return None
    native_id = first(item, _paths(source, "native_id", ()))
    mid = str(native_id).strip() if native_id not in (None, "") else sha1(f"{source}|{url}|{author}|{text}".encode("utf-8", errors="ignore")).hexdigest()[:24]
    followers, followers_known, followers_path = _metric(item, mapping, source, "followers", FOLLOWER_KEYS)
    views, views_known, views_path = _metric(item, mapping, source, "views", VIEW_KEYS)
    likes, likes_known, likes_path = _metric(item, mapping, source, "likes", LIKE_KEYS)
    comments, comments_known, comments_path = _metric(item, mapping, source, "comments", COMMENT_KEYS)
    shares, shares_known, shares_path = _metric(item, mapping, source, "shares", SHARE_KEYS)
    availability = {
        "followers_known": followers_known, "views_known": views_known, "likes_known": likes_known,
        "comments_known": comments_known, "shares_known": shares_known,
    }
    known = sum(1 for v in availability.values() if v)
    return {
        "id": mid, "platform": source, "text": text, "date": dt.isoformat() if dt else None,
        "author": str(author) if author not in (None, "") else None,
        "followers": followers, "views": views, "likes": likes, "comments": comments, "shares": shares,
        "url": str(url) if url else None,
        "content_type": mapped(item, mapping, "content_type", ("type", "productType", "kind", "postType")),
        "parent_post": mapped(item, mapping, "parent_post", ("parentPost", "parentData", "quotedTweet", "retweetedTweet")),
        "metric_availability": availability,
        "metric_coverage": round(known / 5.0, 4),
        "metric_paths": {"followers": followers_path, "views": views_path, "likes": likes_path, "comments": comments_path, "shares": shares_path},
        "normalization": {"date_raw": raw_date, "mapping_used": dict(mapping or {}), "contract": "universal-normalizer-v3"},
        "raw_data": item,
    }


def normalize_dataset(source: str, items: list[dict], mapping: dict | None = None):
    return normalize_dataset_with_audit(source, items, mapping=mapping)["rows"]


def normalize_dataset_with_audit(source: str, items: list[dict], mapping: dict | None = None):
    out, seen = [], set()
    metadata, diagnostics = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = classify_source_row(source, item)
        if kind == "diagnostic":
            diagnostics.append(item); continue
        if kind == "metadata":
            metadata.append(item); continue
        row = normalize_item(source, item, mapping=mapping)
        if row and row["id"] not in seen:
            seen.add(row["id"]); out.append(row)
    return {
        "rows": out,
        "metadata": metadata,
        "diagnostics": diagnostics,
        "content_rows": len(out),
        "metadata_rows": len(metadata),
        "diagnostic_rows": len(diagnostics),
        "mapping": dict(mapping or {}),
    }
'''
write("app/services/normalizer.py", NORMALIZER)


SCHEMA_MAPPING = r'''from __future__ import annotations

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
'''
write("app/services/schema_mapping.py", SCHEMA_MAPPING)


QUERY_PLANNER = r'''from __future__ import annotations

import copy
import math
import re
from datetime import date, timedelta

from app.models import AnalysisDraft, CollectionPlan, SourcePlan, SubRunPlan
from app.registry import load_registry
from app.services.smart_collection import x_search_input, canonical_topic
from app.services.source_capabilities import comments_forecast

GREEK_MAP = {
    "α":"a","ά":"a","β":"v","γ":"g","δ":"d","ε":"e","έ":"e","ζ":"z","η":"i","ή":"i",
    "θ":"th","ι":"i","ί":"i","ϊ":"i","ΐ":"i","κ":"k","λ":"l","μ":"m","ν":"n","ξ":"x",
    "ο":"o","ό":"o","π":"p","ρ":"r","σ":"s","ς":"s","τ":"t","υ":"y","ύ":"y","ϋ":"y","ΰ":"y",
    "φ":"f","χ":"ch","ψ":"ps","ω":"o","ώ":"o"
}


def uniq(items):
    out, seen = [], set()
    for raw in items:
        value = str(raw or "").strip()
        key = value.casefold()
        if value and key not in seen:
            out.append(value); seen.add(key)
    return out


def greeklish(term: str) -> str:
    result = "".join(GREEK_MAP.get(ch.lower(), ch) for ch in str(term or ""))
    return result if result.casefold() != str(term or "").casefold() else ""


def hashtag(term: str) -> str:
    cleaned = re.sub(r"[^\w\u0370-\u03FF\u1F00-\u1FFF]+", "", str(term or ""), flags=re.UNICODE)
    return cleaned.strip("_")


def allocate_equal(total: int, sources: list[str]) -> dict[str, int]:
    if not sources: return {}
    base, rem = divmod(max(0, int(total)), len(sources))
    return {s: base + (1 if i < rem else 0) for i, s in enumerate(sources)}


def split_target(total: int, n: int) -> list[int]:
    total, n = max(0, int(total)), max(0, int(n))
    if total <= 0 or n <= 0: return []
    n = min(n, total); base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def batched(values: list, size: int) -> list[list]:
    size = max(1, int(size)); return [values[i:i+size] for i in range(0, len(values), size)]


def _role_map(draft: AnalysisDraft) -> dict[str, str]:
    result = {}
    for k, v in (draft.keyword_roles or {}).items():
        term = str(k or "").strip()
        if term: result[term.casefold()] = str(v)
    return result


def _role_terms(draft: AnalysisDraft, role: str) -> list[str]:
    roles = _role_map(draft)
    values = []
    for term in [*draft.keywords, *draft.additional_context]:
        default = "context"
        if roles.get(str(term).casefold(), default) == role:
            values.append(term)
    return uniq(values)


def build_terms(draft: AnalysisDraft):
    topic = canonical_topic(str(draft.topic or "").strip(), str(draft.market or ""))
    core = uniq([topic])
    role_aliases = _role_terms(draft, "alias")
    context = uniq([*_role_terms(draft, "context"), *_role_terms(draft, "required_context"), *_role_terms(draft, "watch")])
    # Backward-compatible explicit additional_context is context unless assigned another role.
    if draft.market.casefold() == "greece":
        market_context = ["Greece", "Ελλάδα", "Ellada"]
    else:
        market_context = [draft.market]
    context = uniq([*context, *market_context])
    aliases = uniq([*role_aliases, greeklish(topic)])
    return core, context, aliases


def base_queries(core: list[str], context: list[str], glish: list[str]) -> list[str]:
    if not core: return []
    primary = core[0]
    return uniq([primary, *glish, *[f"{primary} {c}" for c in context if c and c.casefold() != primary.casefold()]])[:12]


def _research_routes(draft: AnalysisDraft) -> dict:
    topic = canonical_topic(draft.topic, draft.market)
    roles = _role_map(draft)
    aliases = uniq([x for x in draft.keywords if roles.get(str(x).casefold()) == "alias"] + ([greeklish(topic)] if greeklish(topic) else []))
    required = uniq([x for x in [*draft.keywords, *draft.additional_context] if roles.get(str(x).casefold(), "context") == "required_context"])
    contexts = uniq([x for x in [*draft.keywords, *draft.additional_context] if roles.get(str(x).casefold(), "context") == "context"])
    watch = uniq([x for x in [*draft.keywords, *draft.additional_context] if roles.get(str(x).casefold(), "context") == "watch"])
    excludes = uniq([*draft.exclusions, *[x for x in draft.keywords if roles.get(str(x).casefold()) == "exclude"]])
    anchored_required = [f"{topic} {x}" for x in required]
    anchored_context = [f"{topic} {x}" for x in contexts]
    anchored_watch = [f"{topic} {x}" for x in watch]
    if draft.search_strategy == "topic_first":
        primary = uniq([topic, *aliases])
        topups = uniq([*anchored_required, *anchored_context, *anchored_watch])
    elif draft.search_strategy == "context_first":
        primary = uniq([*anchored_required, *anchored_context]) or [topic]
        topups = uniq([*[f"{a} {x}" for a in aliases for x in [*required, *contexts] if x], *anchored_watch])
    else:
        anchored = uniq([*anchored_required, *anchored_context])
        primary = uniq([topic, *aliases, *anchored[:2]])
        topups = uniq([*anchored[2:], *anchored_watch])
    return {"topic": topic, "aliases": aliases, "required": required, "contexts": contexts, "watch": watch, "excludes": excludes,
            "primary": primary, "topups": topups}


def _prioritized_source_queries(draft: AnalysisDraft, queries: list[str], limit: int) -> list[str]:
    routes = _research_routes(draft)
    return uniq([*routes["primary"], *routes["topups"], *queries])[:max(1, int(limit))]


def _facebook_safe_query(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= 100: return value
    clipped = value[:100].rstrip(); return clipped.rsplit(" ", 1)[0].rstrip() if " " in clipped else clipped


def _facebook_queries(draft: AnalysisDraft, queries: list[str], limit: int = 6) -> list[str]:
    return uniq([_facebook_safe_query(q) for q in _prioritized_source_queries(draft, queries, limit) if q])[:limit]


def _tiktok_date_range(draft: AnalysisDraft, today: date | None = None) -> str:
    today = today or date.today()
    if draft.date_from > today: return "ALL_TIME"
    age = max(0, (today - draft.date_from).days)
    if age <= 1: return "YESTERDAY"
    if draft.date_from.isocalendar()[:2] == today.isocalendar()[:2]: return "THIS_WEEK"
    if draft.date_from.year == today.year and draft.date_from.month == today.month: return "THIS_MONTH"
    if age <= 90: return "LAST_THREE_MONTHS"
    if age <= 180: return "LAST_SIX_MONTHS"
    return "ALL_TIME"


def _youtube_upload_date(draft: AnalysisDraft, today: date | None = None) -> str:
    today = today or date.today()
    if draft.date_from > today: return "all"
    age = max(0, (today - draft.date_from).days)
    if age <= 1: return "t"
    if age <= 7: return "w"
    if age <= 31: return "m"
    if age <= 366: return "y"
    return "all"


def news_queries_for_capacity(draft: AnalysisDraft, queries: list[str], target: int) -> list[str]:
    topic = canonical_topic(draft.topic, draft.market)
    needed = max(1, min(8, int(math.ceil(max(1, int(target)) / 500))))
    extras = [f"{topic} νέα", f"{topic} ανακοίνωση", f"{topic} συνέντευξη", f"{topic} media"] if draft.market.casefold()=="greece" else [f"{topic} news", f"{topic} announcement", f"{topic} interview", f"{topic} media"]
    return uniq([*queries, *extras])[:max(needed, min(8, len(queries)+len(extras)))]


def per_source_targets(draft: AnalysisDraft) -> dict[str, int]:
    if draft.sample_mode == "perSource":
        return {s: int(draft.per_source.get(s, 0) or 0) for s in draft.sources}
    return allocate_equal(draft.sample_target, draft.sources)


def budget_for_subrun(source_budget: float, shares: list[int], idx: int) -> float:
    total = sum(shares) or 1
    raw = max(0.0, float(source_budget)) * max(0, int(shares[idx])) / total
    return math.floor(raw * 100_000_000) / 100_000_000


def instagram_discovery_tags(draft: AnalysisDraft) -> list[str]:
    routes = _research_routes(draft); topic = routes["topic"]
    tags = [hashtag(topic)]
    for alias in routes["aliases"][:1]: tags.append(hashtag(alias))
    for term in [*routes["required"], *routes["contexts"]][:2]: tags.append(hashtag(f"{topic} {term}"))
    return uniq([t for t in tags if t])[:4]


def _sub(actor: str, inp: dict, target: int, cap: float, purpose: str, draft: AnalysisDraft, exact=True):
    return SubRunPlan(actor_id=actor, input=inp, target_items=max(1, int(target)), max_charge_usd=max(0.000001, float(cap)),
                      exact_post_filter=exact, post_filter_from=draft.date_from if exact else None,
                      post_filter_to=draft.date_to if exact else None, purpose=purpose, max_attempt_calls=2)


def _caps(source_budget: float, n_topups: int) -> tuple[float, list[float]]:
    budget = max(0.001, float(source_budget))
    if n_topups <= 0: return budget, []
    primary = budget * 0.70
    remain = max(0.0, budget-primary)
    return primary, [remain/n_topups for _ in range(n_topups)]


def _query_preview(source: str, primary: list[str], topups: list[str], draft: AnalysisDraft) -> list[dict]:
    rows = []
    for i, q in enumerate(primary): rows.append({"route":"primary","query":q,"order":i+1,"editable":True})
    for i, q in enumerate(topups): rows.append({"route":"topup","query":q,"order":len(primary)+i+1,"editable":True})
    for row in rows:
        row.update({"source":source,"date_from":draft.date_from.isoformat(),"date_to":draft.date_to.isoformat(),"strategy":draft.search_strategy})
    return rows


def _apply_override(draft: AnalysisDraft, source: str, primary: list[str], topups: list[str]) -> tuple[list[str], list[str]]:
    overrides = uniq((draft.query_overrides or {}).get(source) or [])
    return (overrides[:5], overrides[5:]) if overrides else (primary, topups)


def make_generic_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], cfg: dict, source_budget: float) -> SourcePlan:
    mapping = cfg.get("input_mapping") or {}; query_field = mapping.get("query"); urls_field = mapping.get("urls")
    if not query_field and not (source == "instagram" and urls_field): raise ValueError(f"Verified Actor for {source} has no usable discovery input mapping.")
    routes = _research_routes(draft); primary, topups = _apply_override(draft, source, routes["primary"], routes["topups"])
    base = copy.deepcopy(cfg.get("input_template") or {}); semantic = "query" if query_field else "urls"; field = query_field or urls_field
    value = primary[:5] or [draft.topic]
    if semantic == "urls": value = [f"https://www.instagram.com/explore/tags/{hashtag(x).lower()}/" for x in value]
    base[field] = value if isinstance(base.get(field), list) else value[0]
    max_field = mapping.get("max_items")
    if max_field: base[max_field] = target
    primary_cap, top_caps = _caps(source_budget, min(3, len(topups)))
    subruns = [_sub(cfg["actor_id"], base, target, primary_cap, "primary_discovery", draft)]
    topup_subruns = []
    for i, q in enumerate(topups[:3]):
        inp = copy.deepcopy(base); val = f"https://www.instagram.com/explore/tags/{hashtag(q).lower()}/" if semantic=="urls" else q
        inp[field] = [val] if isinstance(base.get(field), list) else val
        topup_subruns.append(_sub(cfg["actor_id"], inp, target, top_caps[i], f"topup_{i+1}", draft))
    return SourcePlan(source=source, actor_id=cfg["actor_id"], target_items=target, estimated_cost_usd=None,
                      price_per_1000_hint=cfg.get("price_per_1000_hint"), date_strategy=cfg.get("date_support","post_filter_exact"),
                      market_strategy=cfg.get("market_support","mapped_or_relevance_filter"), queries=uniq([*primary,*topups]),
                      subruns=subruns, topup_subruns=topup_subruns, semantic_topup_subruns=copy.deepcopy(topup_subruns),
                      query_preview=_query_preview(source,primary,topups,draft), source_budget_usd=source_budget)


def make_source_plan(source: str, target: int, draft: AnalysisDraft, queries: list[str], registry: dict, source_budget: float) -> SourcePlan:
    cfg = registry[source]
    if cfg.get("adapter_mode") == "generic": return make_generic_source_plan(source,target,draft,queries,cfg,source_budget)
    actor = cfg["actor_id"]; rate = cfg.get("price_per_1000_hint"); est = round(target*rate/1000,4) if rate is not None else None
    routes = _research_routes(draft); primary, topups = _apply_override(draft,source,routes["primary"],routes["topups"])
    intent_buckets = []
    if source == "x" and draft.search_strategy == "balanced_smart" and draft.smart_search:
        xin, intent_buckets = x_search_input(draft, target)
        primary = uniq([*primary, *(xin.get("searchTerms") or [])])[:9]
    primary = primary or [routes["topic"]]
    pcap, tcaps = _caps(source_budget, min(4, max(1,len(topups))))
    until_exclusive = draft.date_to + timedelta(days=1)
    subruns, topup_subruns = [], []
    if source == "x":
        inp={"mode":"search","searchTerms":primary[:9],"maxItems":target,"includeSearchTerms":True,"queryType":"Latest",
             "since":f"{draft.date_from.isoformat()}_00:00:00_UTC","until":f"{until_exclusive.isoformat()}_00:00:00_UTC"}
        subruns=[_sub(actor,inp,target,pcap,"primary_global",draft)]
        tq=topups or primary
        tin={**inp,"searchTerms":tq[:9],"queryType":"Latest + Top","maxItems":target}
        topup_subruns=[_sub(actor,tin,target,(tcaps[0] if tcaps else source_budget*0.3),"topup_latest_plus_top",draft)]
    elif source == "tiktok":
        inp={"search":primary[:5],"maxItems":target,"location":"GR" if draft.market.casefold()=="greece" else None,
             "dateRange":_tiktok_date_range(draft),"sortType":"DATE_POSTED" if (draft.date_to-draft.date_from).days<=7 else "RELEVANCE"}
        inp={k:v for k,v in inp.items() if v is not None}; subruns=[_sub(actor,inp,target,pcap,"primary_global",draft)]
        for i,batch in enumerate(batched(topups,5)[:2]):
            tin={**inp,"search":batch,"maxItems":target,"sortType":"RELEVANCE"}
            topup_subruns.append(_sub(actor,tin,target,tcaps[min(i,len(tcaps)-1)] if tcaps else source_budget*0.15,f"topup_{i+1}",draft))
    elif source == "instagram":
        tags=instagram_discovery_tags(draft) or [hashtag(routes["topic"])]
        primary_url=f"https://www.instagram.com/explore/tags/{tags[0].lower()}/"
        inp={"directUrls":[primary_url],"resultsType":"posts","resultsLimit":target,"onlyPostsNewerThan":draft.date_from.isoformat(),"addParentData":True}
        subruns=[_sub(actor,inp,target,pcap,"primary_hashtag_posts",draft)]
        for i,tag in enumerate(tags[1:4]):
            tin={**inp,"directUrls":[f"https://www.instagram.com/explore/tags/{tag.lower()}/"],"resultsLimit":target}
            topup_subruns.append(_sub(actor,tin,target,tcaps[min(i,len(tcaps)-1)] if tcaps else source_budget*0.1,f"topup_hashtag_{i+1}",draft))
    elif source == "facebook":
        q=uniq([_facebook_safe_query(x) for x in [*primary,*topups] if x]); primary_q=q[0] if q else _facebook_safe_query(routes["topic"])
        inp={"query":primary_q,"resultsCount":target,"searchType":"latest","startDate":draft.date_from.isoformat(),"endDate":draft.date_to.isoformat()}
        subruns=[_sub(actor,inp,target,pcap,"primary_topic",draft,exact=False)]
        for i,qv in enumerate(q[1:5]):
            tin={**inp,"query":qv,"resultsCount":target}
            topup_subruns.append(_sub(actor,tin,target,tcaps[min(i,len(tcaps)-1)] if tcaps else source_budget*0.075,f"topup_query_{i+1}",draft,exact=False))
    elif source == "youtube":
        inp={"keywords":primary[:5],"gl":"gr" if draft.market.casefold()=="greece" else "us","hl":"el" if draft.market.casefold()=="greece" else "en",
             "uploadDate":_youtube_upload_date(draft),"sort":"r","maxItems":target}
        subruns=[_sub(actor,inp,target,pcap,"primary_global",draft)]
        for i,batch in enumerate(batched(topups,5)[:2]):
            tin={**inp,"keywords":batch,"maxItems":target}
            topup_subruns.append(_sub(actor,tin,target,tcaps[min(i,len(tcaps)-1)] if tcaps else source_budget*0.15,f"topup_{i+1}",draft))
    elif source == "news":
        nq=news_queries_for_capacity(draft,uniq([*primary,*topups]),target); first_batch=nq[:5] or [routes["topic"]]
        per=min(500,max(1,math.ceil(target/max(1,len(first_batch)))))
        inp={"queries":first_batch,"language":"el" if draft.market.casefold()=="greece" else "en-US","country":"GR" if draft.market.casefold()=="greece" else "US",
             "maxArticles":per,"fromDate":draft.date_from.isoformat(),"toDate":draft.date_to.isoformat(),"resolveUrls":True}
        subruns=[_sub(actor,inp,target,pcap,"primary_global",draft,exact=False)]
        rest=nq[5:8]
        if rest:
            tin={**inp,"queries":rest,"maxArticles":min(500,max(1,math.ceil(target/len(rest))))}
            topup_subruns=[_sub(actor,tin,target,(tcaps[0] if tcaps else source_budget*0.3),"topup_news",draft,exact=False)]
    preview=_query_preview(source,primary,topups,draft)
    return SourcePlan(source=source,actor_id=actor,target_items=target,estimated_cost_usd=est,price_per_1000_hint=rate,
                      date_strategy=cfg.get("date_support","post_filter_exact"),market_strategy=cfg.get("market_support","query_context"),
                      queries=uniq([*primary,*topups]),subruns=subruns,topup_subruns=topup_subruns,
                      semantic_topup_subruns=copy.deepcopy(topup_subruns or subruns),intent_buckets=intent_buckets,
                      query_preview=preview,source_budget_usd=source_budget,source_contract_version="actor-contract-v3")


def _preflight_forecast(draft: AnalysisDraft, plans: list[SourcePlan], registry: dict) -> dict:
    rows=[]
    for sp in plans:
        cfg=registry.get(sp.source,{})
        rows.append({"source":sp.source,"actor_id":sp.actor_id,"actor_verified":cfg.get("actor_status")=="verified",
                     "planned_primary_calls":len(sp.subruns),"available_topup_routes":len(sp.topup_subruns),
                     "target_semantics":"final_analyzable_unique_in_range","comments":comments_forecast(sp.source,bool(draft.comments),cfg)})
    return {"version":"collection-preflight-master30-v1","mode":"static_contract_audit","risk_level":"low" if all(r["actor_verified"] for r in rows) else "medium",
            "selected_sources":[p.source for p in plans],"planned_actor_batches":sum(len(p.subruns) for p in plans),
            "failure_isolation":"per Actor call/source","budget_guard":"global hard cap + source envelope",
            "sample_rule":"shared source target; top-up until target, source exhaustion, relevance floor or budget",
            "query_safety":{"client_field_used_for_discovery":False,"search_strategy":draft.search_strategy,"query_preview_available":True},"sources":rows}


def build_collection_plan(draft: AnalysisDraft) -> CollectionPlan:
    registry=load_registry(); core,context,aliases=build_terms(draft); queries=base_queries(core,context,aliases) if draft.smart_search else uniq([draft.topic,*draft.keywords])
    targets=per_source_targets(draft); selected=[s for s in draft.sources if targets.get(s,0)>0]
    estimates={s:(None if registry[s].get("price_per_1000_hint") is None else targets[s]*registry[s].get("price_per_1000_hint")/1000) for s in selected}
    has_unknown=any(v is None for v in estimates.values()); known_total=sum(v or 0 for v in estimates.values())
    budget_check="unknown" if has_unknown else ("within_budget" if known_total<=draft.max_budget_usd else "estimate_over_budget")
    # Allocate the user's acquisition budget across sources. Source-specific pricing is enforced by Apify's charge cap plus the global guard.
    if selected:
        weights={s:max(0.05,float(estimates[s] or 0.05)) for s in selected}; den=sum(weights.values())
        source_budgets={s:float(draft.max_budget_usd)*weights[s]/den for s in selected}
    else: source_budgets={}
    plans=[make_source_plan(s,targets[s],draft,queries,registry,source_budgets[s]) for s in selected]
    preview={p.source:p.query_preview for p in plans}
    return CollectionPlan(client=draft.client,topic=draft.topic,market=draft.market,date_from=draft.date_from,date_to=draft.date_to,
                          sample_mode=draft.sample_mode,target_total=sum(p.target_items for p in plans),estimated_cost_usd=None if has_unknown else round(known_total,4),
                          max_budget_usd=draft.max_budget_usd,comments_requested=draft.comments,deepening_strategy="important_content_only" if draft.comments else "disabled",
                          budget_check=budget_check,rebalancing_enabled=(draft.sample_mode=="automatic"),rebalance_max_rounds=2,
                          core_terms=core,context_terms=context,greeklish_variants=aliases,exclusions=uniq([*draft.exclusions,*_role_terms(draft,"exclude")]),
                          search_strategy_version="master30-search-v1",target_semantics="requested_final_analyzable_unique_in_range",
                          resilience_policy_version="multisource-resilience-master30-v1",preflight_forecast=_preflight_forecast(draft,plans,registry),sources=plans,
                          search_strategy=draft.search_strategy,keyword_roles={str(k):str(v) for k,v in (draft.keyword_roles or {}).items()},query_preview=preview,
                          topup_policy="shared_source_target_until_analyzable_or_exhausted",master_spec_version="SIGNALYTH-master30-v1",benchmark=draft.benchmark)
'''
write("app/services/query_planner.py", QUERY_PLANNER)

print("master30 core patches applied")
