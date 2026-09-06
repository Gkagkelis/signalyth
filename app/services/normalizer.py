from __future__ import annotations

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
