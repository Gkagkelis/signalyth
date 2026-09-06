from __future__ import annotations
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha1

TEXT_KEYS = ("text","caption","postText","content","description","title","body","message","snippet")
DATE_KEYS = ("date","createdAt","created_at","timestamp","publishedAt","published_at","createTimeISO","takenAtIso","published")
AUTHOR_KEYS = ("authorUsername","author.username","userName","username","ownerUsername","channelName","pageName","sourceName","ownerFullName","author")
URL_KEYS = ("url","postUrl","webVideoUrl","link","inputUrl","originalUrl")
FOLLOWER_KEYS = ("followers","followerCount","authorFollowers","followersCount","authorMeta.followers")
VIEW_KEYS = ("views","viewCount","playCount","videoPlayCount","igPlayCount","fbPlayCount")
LIKE_KEYS = ("likesCount","likeCount","diggCount","likes","fbLikeCount")
COMMENT_KEYS = ("commentsCount","commentCount","replyCount")
SHARE_KEYS = ("shares","shareCount","retweetCount")


def nested(item, key):
    cur = item
    for part in str(key or "").split('.'):
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


def mapped(item: dict, mapping: dict | None, field: str, fallback_keys=(), default=None):
    path = (mapping or {}).get(field)
    if path:
        value = nested(item, path)
        if value not in (None, ""):
            return value
    return first(item, fallback_keys, default)


def as_int(v):
    try: return int(float(v or 0))
    except Exception: return 0


def parse_date(v):
    if v in (None, ""): return None
    if isinstance(v, datetime): return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int,float)):
        value = float(v)
        if value > 10_000_000_000: value /= 1000
        try: return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception: return None
    s = str(v).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # X/Twitter commonly returns RFC-2822-like timestamps such as
    # "Sun Aug 16 12:03:14 +0000 2026".  These must survive exact date filtering.
    try:
        dt = parsedate_to_datetime(str(v).strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def normalize_item(source: str, item: dict, mapping: dict | None = None):
    text = str(mapped(item, mapping, "text", TEXT_KEYS, "") or "").strip()
    url = mapped(item, mapping, "url", URL_KEYS)
    author = mapped(item, mapping, "author", AUTHOR_KEYS)
    dt = parse_date(mapped(item, mapping, "date", DATE_KEYS))
    if not text and not url: return None
    mid = sha1(f"{source}|{url}|{author}|{text}".encode("utf-8", errors="ignore")).hexdigest()[:24]
    return {
        "id": mid, "platform": source, "text": text, "date": dt.isoformat() if dt else None,
        "author": str(author) if author else None,
        "followers": as_int(mapped(item, mapping, "followers", FOLLOWER_KEYS)),
        "views": as_int(mapped(item, mapping, "views", VIEW_KEYS)),
        "likes": as_int(mapped(item, mapping, "likes", LIKE_KEYS)),
        "comments": as_int(mapped(item, mapping, "comments", COMMENT_KEYS)),
        "shares": as_int(mapped(item, mapping, "shares", SHARE_KEYS)),
        "url": str(url) if url else None,
        "content_type": mapped(item, mapping, "content_type", ("type", "productType", "kind")),
        "parent_post": mapped(item, mapping, "parent_post", ("parentPost", "parentData")),
        "raw_data": item
    }


def normalize_dataset(source: str, items: list[dict], mapping: dict | None = None):
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict): continue
        row = normalize_item(source, item, mapping=mapping)
        if row and row["id"] not in seen:
            seen.add(row["id"]); out.append(row)
    return out
