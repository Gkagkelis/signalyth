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


def _facebook_flatten(items: list[dict]) -> list[dict]:
    """Flatten nested Facebook replies while remaining safe if Actor already emits them separately."""
    out: list[dict] = []

    def visit(raw: dict, parent_comment_id: str | None = None, inherited_post: str | None = None):
        if not isinstance(raw, dict):
            return
        row = dict(raw)
        nested = row.pop("comments", []) or []
        if inherited_post and not (row.get("facebookUrl") or row.get("inputUrl")):
            row["facebookUrl"] = inherited_post
        if parent_comment_id and not row.get("replyToCommentId"):
            row["replyToCommentId"] = parent_comment_id
        out.append(row)
        this_id = str(row.get("commentId") or row.get("id") or "") or parent_comment_id
        parent_post = str(row.get("facebookUrl") or row.get("inputUrl") or inherited_post or "")
        for reply in nested if isinstance(nested, list) else []:
            visit(reply, this_id, parent_post)

    for item in items:
        visit(item)
    return out


def _parent_from_seed(source: str, raw: dict, seed_refs: list[str]) -> str | None:
    if source == "instagram":
        return str(raw.get("postUrl") or raw.get("inputUrl") or (seed_refs[0] if len(seed_refs) == 1 else "")) or None
    if source == "facebook":
        candidate = str(
            raw.get("facebookUrl")
            or raw.get("postUrl")
            or raw.get("inputUrl")
            or ""
        ).strip()
        if candidate:
            return candidate
        return seed_refs[0] if len(seed_refs) == 1 else None
    if source == "tiktok":
        aweme = str(raw.get("aweme_id") or raw.get("awemeId") or raw.get("videoId") or "")
        if aweme:
            for ref in seed_refs:
                if _video_id(ref) == aweme:
                    return ref
        return seed_refs[0] if len(seed_refs) == 1 else None
    if source == "x":
        # Thread mode returns descendants whose immediate inReplyToId may be
        # another reply. Preserve the ROOT research parent instead. Xquik exposes
        # sourceTweetId/sourceTarget in engagement modes and conversationId on
        # tweet rows; any of these can map a nested row back to the requested root.
        seed_set = {str(x) for x in seed_refs}
        for key in ("sourceTweetId", "sourceTarget", "conversationId"):
            candidate = str(raw.get(key) or "")
            if candidate and candidate in seed_set:
                return candidate
        immediate = str(raw.get("inReplyToId") or raw.get("inReplyToTweetId") or "")
        if immediate and immediate in seed_set:
            return immediate
        return seed_refs[0] if len(seed_refs) == 1 else None
    return seed_refs[0] if len(seed_refs) == 1 else None


def normalize_comment_dataset(
    source: str,
    items: list[dict],
    *,
    seed_refs: list[str] | None = None,
    seed_context: dict[str, str | dict] | None = None,
    mapping: dict | None = None,
) -> list[dict]:
    """Normalize comment/reply Actors into the same evidence contract as primary rows.

    Every record keeps its parent post plus an explicit evidence_layer, so downstream
    analysis can use comments while still separating publisher content from audience response.
    """
    seed_refs = [str(x) for x in (seed_refs or []) if str(x or "").strip()]
    seed_context = {str(k): v for k, v in (seed_context or {}).items() if str(k or "").strip()}
    if source == "instagram":
        raw_items = _instagram_flatten(items)
    elif source == "facebook":
        raw_items = _facebook_flatten(items)
    else:
        raw_items = [x for x in items if isinstance(x, dict)]
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
            parent_comment_id = str(raw.get("inReplyToId") or raw.get("inReplyToTweetId") or "") or None
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
            date_raw = _mapped(raw, mapping, "date", ("date", "timestamp", "createdAt", "created_at"))
            author = _mapped(raw, mapping, "author", ("profileName", "name", "author.name", "authorName", "username"))
            native_id = _first(raw, ("commentId", "id", "legacyId"))
            likes = _int(_mapped(raw, mapping, "likes", ("likesCount", "reactionsCount", "likeCount"), 0))
            replies = _int(_mapped(raw, mapping, "comments", ("commentsCount", "replyCount", "repliesCount"), 0))
            shares = 0
            url = _mapped(raw, mapping, "url", ("commentUrl", "url"))
            parent_comment_id = str(
                raw.get("replyToCommentId")
                or raw.get("parentCommentId")
                or raw.get("parent_id")
                or _nested(raw, "parentComment.commentId")
                or ""
            ) or None
            try:
                threading_depth = int(raw.get("threadingDepth") or 0)
            except Exception:
                threading_depth = 0
            layer = "reply" if parent_comment_id or threading_depth > 0 else "comment"
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

        # X thread mode may include the requested root itself. It is a parent
        # discovery row, not audience evidence, so never normalize it as a reply.
        if source == "x" and raw_id and raw_id in set(seed_refs):
            continue

        context_payload = seed_context.get(str(parent_post or ""), "")
        if isinstance(context_payload, dict):
            parent_context = str(context_payload.get("text") or "").strip()
            try:
                parent_market_score = float(context_payload.get("market_score") or 0.0)
            except Exception:
                parent_market_score = 0.0
            parent_subject_qualified = bool(context_payload.get("subject_qualified"))
            parent_qualification_tier = str(context_payload.get("qualification_tier") or "").strip() or None
        else:
            parent_context = str(context_payload or "").strip()
            parent_market_score = 0.0
            parent_subject_qualified = False
            parent_qualification_tier = None

        if source == "x" and parent_comment_id and str(parent_comment_id) == str(parent_post or ""):
            # Direct reply to root; only nested replies have a comment parent.
            parent_comment_id = None

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
            "parent_market_score": round(parent_market_score, 4) if parent_market_score > 0 else 0.0,
            "parent_subject_qualified": parent_subject_qualified,
            "parent_qualification_tier": parent_qualification_tier,
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
