from __future__ import annotations

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
    seed_context: dict[str, str] | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """Normalize comment/reply Actors into the same evidence contract as primary rows.

    Every record keeps its parent post plus an explicit evidence_layer, so downstream
    analysis can use comments while still separating publisher content from audience response.
    """
    seed_refs = [str(x) for x in (seed_refs or []) if str(x or "").strip()]
    seed_context = {str(k): str(v or "").strip() for k, v in (seed_context or {}).items() if str(k or "").strip()}
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
        parent_context = seed_context.get(str(parent_post or ""), "")
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
            "parent_context": parent_context or None,
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
