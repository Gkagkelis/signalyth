from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
import re
from urllib.parse import parse_qs, urlparse

# Public-schema capability knowledge. This is NOT a live verification record.
# Central acquisition contracts. These encode provider-specific semantics that
# materially affect recall, batching, date/market guarantees and cost. They are
# intentionally descriptive rather than pretending every provider has the same
# knobs.
DISCOVERY_ACTOR_CONTRACTS = {
    "x": {
        "actor_id": "xquik/x-tweet-scraper",
        "target_field": "searchTerms",
        "safe_target_batch": 2,
        "limit_field": "maxItems",
        "limit_scope": "global",
        "per_target_limit_field": "maxItemsPerTarget",
        "sort_field": "queryType",
        "date_support": "native_exact_inclusive_from_exclusive_until",
        "market_support": "query_language_geo",
        "broad_recall": "subject_plus_lang_el_or_explicit_market; Greeklish must not be forced through lang:el",
    },
    "tiktok": {
        "actor_id": "epctex/tiktok-search-scraper",
        "target_field": "search",
        "safe_target_batch": 1,
        "limit_field": "maxItems",
        "limit_scope": "global",
        "sort_field": "sortType",
        "date_support": "native_coarse_then_exact_post_filter",
        "market_support": "location_iso_country",
        "broad_recall": "bare subject allowed because native location=GR qualifies the market route",
    },
    "instagram": {
        "actor_id": "apify/instagram-scraper",
        "target_field": "directUrls",
        "safe_target_batch": 1,
        "limit_field": "resultsLimit",
        "limit_scope": "per_source",
        "sort_field": None,
        "date_support": "native_lower_bound_then_exact_post_filter",
        "market_support": "no_country_wide_filter",
        "broad_recall": "bare subject hashtag only as bounded post-cleaning semantic probe on market runs",
    },
    "facebook": {
        "actor_id": "scraper_one/facebook-posts-search",
        "target_field": "query",
        "safe_target_batch": 1,
        "limit_field": "resultsCount",
        "limit_scope": "per_query",
        "query_max_length": 100,
        "sort_field": "searchType",
        "date_support": "native_exact",
        "market_support": "query_context; location is pinned-place only, not country-wide Greece",
        "broad_recall": "bare subject only as bounded post-cleaning semantic probe on market runs",
    },
    "youtube": {
        "actor_id": "apidojo/youtube-scraper",
        "target_field": "keywords",
        "safe_target_batch": 1,
        "limit_field": "maxItems",
        "limit_scope": "global",
        "sort_field": "sort",
        "date_support": "native_coarse_then_exact_post_filter",
        "market_support": "gl_country_plus_hl_language",
        "broad_recall": "bare subject allowed because gl=GR/hl=el qualify the market route",
    },
    "news": {
        "actor_id": "logiover/google-news-scraper",
        "target_field": "queries",
        "safe_target_batch": 2,
        "limit_field": "maxArticles",
        "limit_scope": "per_query_feed",
        "limit_max": 500,
        "sort_field": None,
        "date_support": "native_exact",
        "market_support": "country_plus_language",
        "broad_recall": "bare subject allowed because country=GR/language=el qualify the market route",
    },
}


def discovery_actor_contract(source: str) -> dict:
    return deepcopy(DISCOVERY_ACTOR_CONTRACTS.get(str(source or "").casefold(), {}))


# Acquisition contracts live here so provider semantics are not scattered across
# orchestration code. These are deliberately conservative research defaults:
# coverage and resumability matter more than minimizing Actor call count.
COMMENT_ACTOR_CONTRACTS = {
    # Xquik's maxItems is global across a run; maxItemsPerTarget prevents one
    # parent from consuming the whole quota. mode=replies is the documented
    # audience-reply route (direct replies).
    "x": {
        "parent_batch_limit": 2,
        "limit_scope": "global",
        "reply_depth": "direct",
        "sort": "actor_default",
    },
    # clockworks/tiktok-comments-scraper (live schema read 2026-09-26):
    # commentsPerPost is a PER-POST cap over the postURLs list, so multiple
    # parents batch safely into one run instead of one call per video.
    "tiktok": {
        "parent_batch_limit": 5,
        "limit_scope": "per_parent",
        "reply_depth": "nested_when_includeReplies",
        "sort": "actor_default",
    },
    # apify/instagram-comment-scraper (official; live schema read 2026-09-26):
    # resultsLimit is PER POST over directUrls; includeNestedComments extracts
    # replies. Small batches bound worker wall time.
    "instagram": {
        "parent_batch_limit": 5,
        "limit_scope": "per_parent",
        "reply_depth": "nested_when_includeReplies",
        "sort": "actor_default",
    },
    # Scraper One uses a per-post resultsLimit and newest-first ordering. Keep
    # the five-parent cap from the production hotfix; do not leak that limit to
    # other actors. This actor does not expose a nested-reply control.
    "facebook": {
        "parent_batch_limit": 5,
        "limit_scope": "per_parent",
        "reply_depth": "top_level",
        "sort": "newest",
    },
}


def comment_actor_contract(source: str) -> dict:
    """Curated orchestration semantics for one comment/reply Actor."""
    return deepcopy(COMMENT_ACTOR_CONTRACTS.get(str(source or "").casefold(), {
        "parent_batch_limit": 1,
        "limit_scope": "global",
        "reply_depth": "unknown",
        "sort": "actor_default",
    }))


def comment_parent_batch_limit(source: str) -> int:
    """Maximum concrete parent refs sent in one comment Actor call."""
    return max(1, int(comment_actor_contract(source).get("parent_batch_limit") or 1))


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
            "note": "Scraper One accepts concrete Facebook post URLs, a per-post result limit, and newest/relevant/all sorting. Exact date bounds are enforced after normalization.",
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
    """Describe whether the configured comment layer can run for this analysis.

    The four production social routes are allowed from their curated public Actor
    contracts. A successful live run may later upgrade the route to verified, but
    a separate paid smoke is not a prerequisite for normal collection.
    """
    cap = source_capabilities(source)["comment_deepening"]
    registry_cfg = registry_cfg or {}
    route_status = str(registry_cfg.get("comment_deepening_status") or "unverified")
    configured_actor = registry_cfg.get("comment_actor_id") or cap.get("candidate_actor_id")
    production_scope = source in {"x", "tiktok", "instagram", "facebook"}
    live_verified = bool(cap.get("live_verified")) or route_status == "verified"
    contract_ready = bool(production_scope and cap.get("public_schema_known") and configured_actor and route_status in {"configured", "verified"})
    configured_enabled = bool(registry_cfg.get("comment_enabled", False))

    if not requested:
        status = "not_requested"
    elif not production_scope or cap.get("mode") == "not_applicable":
        status = "not_applicable"
    elif live_verified and configured_enabled:
        status = "verified_available"
    elif contract_ready and configured_enabled:
        status = "configured_available"
    elif live_verified and not configured_enabled:
        status = "verified_disabled"
    elif contract_ready and not configured_enabled:
        status = "configured_disabled"
    elif cap.get("public_schema_known"):
        status = "candidate_not_configured"
    else:
        status = "unknown_requires_configuration"
    return {
        "requested": bool(requested),
        "status": status,
        **cap,
        "orchestration_contract": comment_actor_contract(source),
        "candidate_actor_id": configured_actor,
        "live_verified": live_verified,
        "contract_ready": contract_ready,
        "enabled": configured_enabled,
        "registry_route_status": route_status,
    }


def _x_id(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    match = re.search(r"/status/(\d+)", text, flags=re.I)
    return match.group(1) if match else text



# ---------------------------------------------------------------------------
# The last line before a paid comment Actor is called.
#
# A page or profile URL is a DISCOVERY input: it answers "which posts exist".
# A comment Actor answers "which comments sit under THIS post", and handing it
# a page instead of a post is not an error it reports — it churns and returns
# nothing, which reads from the outside like a slow or broken provider.
# That is exactly what happened in production: the Facebook comment Actor was
# called with facebook.com/<page> and came back empty.
# ---------------------------------------------------------------------------

def _normalised_url(value: str):
    """Parse a social URL without accepting a bare profile/handle as a post."""
    text = str(value or "").strip()
    if not text:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.I):
        text = "https://" + text.lstrip("/")
    try:
        return urlparse(text)
    except Exception:
        return None


def is_comment_parent_ref(source: str, value: str) -> bool:
    """Return True only for a concrete post/video/tweet reference.

    Page/profile URLs are discovery inputs, never comment inputs. This function
    is the last-line guard before paid comment/reply Actors are called.
    """
    text = str(value or "").strip()
    if not text:
        return False

    source = str(source or "").casefold()
    if source == "x":
        if text.isdigit():
            return True
        parsed = _normalised_url(text)
        if not parsed:
            return False
        host = parsed.netloc.casefold().split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        if host not in {"x.com", "twitter.com", "mobile.twitter.com"}:
            return False
        return re.search(r"/status/\d+(?:/|$)", parsed.path, flags=re.I) is not None

    parsed = _normalised_url(text)
    if not parsed:
        return False
    host = parsed.netloc.casefold().split(":", 1)[0]
    path = parsed.path or "/"
    low_path = path.casefold()
    query = parse_qs(parsed.query or "")

    if source == "facebook":
        clean_host = host
        for prefix in ("www.", "m.", "web."):
            if clean_host.startswith(prefix):
                clean_host = clean_host[len(prefix):]
        if clean_host == "fb.watch":
            return low_path not in {"", "/"}
        if clean_host not in {"facebook.com", "fb.com"}:
            return False
        if re.search(r"/(?:posts|videos|reel|reels|photos)/[^/]+", low_path):
            return True
        if re.search(r"/share/(?:p|v|r)/[^/]+", low_path):
            return True
        if low_path.rstrip("/") in {"/permalink.php", "/story.php", "/photo.php", "/watch"}:
            return bool(query)
        if {"story_fbid", "fbid", "v"} & set(query):
            return True
        return False

    if source == "instagram":
        clean_host = host[4:] if host.startswith("www.") else host
        if clean_host != "instagram.com":
            return False
        return re.search(r"/(?:p|reel|reels|tv)/[^/]+", low_path) is not None

    if source == "tiktok":
        clean_host = host[4:] if host.startswith("www.") else host
        if clean_host in {"vm.tiktok.com", "vt.tiktok.com"}:
            return low_path not in {"", "/"}
        if clean_host != "tiktok.com":
            return False
        return (
            re.search(r"/@[^/]+/(?:video|photo)/\d+", low_path) is not None
            or re.search(r"/t/[^/]+", low_path) is not None
        )

    # Not enabled by this patch; retained for the existing future-facing helper.
    if source == "youtube":
        clean_host = host[4:] if host.startswith("www.") else host
        if clean_host == "youtu.be":
            return low_path not in {"", "/"}
        if clean_host not in {"youtube.com", "m.youtube.com"}:
            return False
        if low_path.startswith(("/shorts/", "/live/")):
            return len(low_path.strip("/").split("/")) >= 2
        return low_path == "/watch" and bool(query.get("v"))

    return False

def build_comment_deepening_input(
    source: str,
    seed_refs: list[str],
    max_items: int,
    *,
    max_per_parent: int | None = None,
    include_replies: bool = True,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Build the curated public-schema input shape for a comment/reply route."""
    cap = source_capabilities(source).get("comment_deepening") or {}
    if cap.get("mode") == "not_applicable":
        raise ValueError(f"Comment deepening is not applicable to source {source}.")
    refs = [str(x).strip() for x in seed_refs if str(x or "").strip()]
    refs = list(dict.fromkeys(refs))[:40]
    if not refs:
        raise ValueError("At least one seed reference is required.")
    # Anything that is not a concrete post/video/tweet is dropped here rather
    # than paid for. Refusing outright when nothing survives is deliberate: a
    # call with only a page reference can never return comments.
    refs = [r for r in refs if is_comment_parent_ref(source, r)]
    if not refs:
        raise ValueError(
            f"No concrete {source} post reference to collect comments under; "
            "page or profile references are discovery inputs, not comment inputs."
        )
    max_items = max(1, int(max_items))
    per_parent = max(1, int(max_per_parent or math.ceil(max_items / max(1, len(refs)))))

    if source == "x":
        # `mode: replies` + `replyTweetIds` is the DOCUMENTED audience-reply
        # route ("every row has inReplyToId equal to the requested tweet").
        # `mode: thread` exists in the schema but its semantics are not
        # documented, and the Actor's own examples use "thread" for an author's
        # chained posts. The one production run on thread mode
        # (20260925T002306Z-65236947) never finished the X comment pass.
        # Nested replies have a documented route of their own (conversationIds).
        out = {
            "mode": "replies",
            "replyTweetIds": [_x_id(x) for x in refs],
            "maxItems": max_items,
            "maxItemsPerTarget": max(1, per_parent),
        }
        if date_from:
            out["since"] = f"{date_from.isoformat()}_00:00:00_UTC"
        if date_to:
            until_exclusive = date_to + timedelta(days=1)
            out["until"] = f"{until_exclusive.isoformat()}_00:00:00_UTC"
        return out
    if source == "tiktok":
        # clockworks/tiktok-comments-scraper (live schema read via the app's
        # actor lookup, 2026-09-26): postURLs + commentsPerPost (PER POST);
        # maxRepliesPerComment pulls a bounded number of replies per comment.
        out = {"postURLs": refs, "commentsPerPost": per_parent}
        if include_replies:
            out["maxRepliesPerComment"] = 5
        return out
    if source == "instagram":
        # apify/instagram-comment-scraper (official; live schema read via the
        # app's actor lookup, 2026-09-26): directUrls is required, resultsLimit
        # is PER POST, includeNestedComments extracts replies.
        return {
            "directUrls": refs,
            "resultsLimit": per_parent,
            "includeNestedComments": bool(include_replies),
        }
    if source == "facebook":
        # scraper_one/facebook-comments-scraper: resultsLimit is PER POST and
        # newest-first is essential for bounded windows. The actor has no native
        # date input, so SIGNALYTH enforces date_from/date_to after normalization.
        # Five-parent batching is enforced by comment_parent_batch_limit().
        return {
            "postUrls": refs,
            "resultsLimit": per_parent,
            "commentsSortType": "newest",
        }
    if source == "youtube":
        return {"startUrls": refs, "maxItems": max_items}
    field = cap.get("input_field")
    if not field:
        raise ValueError(f"No comment-route input field is known for source {source}.")
    return {field: refs, "maxItems": max_items}


#: How a page/profile reference must be written for each source's discovery Actor.
PAGE_REF_STYLE = {
    "facebook": "page_url",
    "instagram": "profile_url",
    "tiktok": "username",
    "x": "username",
}


def normalise_page_ref(source: str, raw: str) -> str:
    """Accept whatever the operator pastes and hand each Actor what it wants.

    People paste a full URL, a bare @handle, or a URL with tracking junk on the
    end. Two of the four Actors want a username and two want a URL, and getting
    that wrong costs a paid call that returns nothing — so normalise here, once.
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    value = value.split("?")[0].split("#")[0].rstrip("/")
    style = PAGE_REF_STYLE.get(source)
    if style == "username":
        tail = value.rsplit("/", 1)[-1] if "/" in value else value
        return tail.lstrip("@").strip()
    low = value.lower()
    if low.startswith("http"):
        return value
    # "facebook.com/x" and "www.facebook.com/x" are already addresses; only a
    # bare handle needs the host put in front of it.
    if low.startswith(("www.", "m.")) or "." in low.split("/", 1)[0]:
        return "https://" + value.lstrip("/")
    host = {"facebook": "https://www.facebook.com/",
            "instagram": "https://www.instagram.com/"}.get(source, "https://")
    return host + value.lstrip("@/")


def build_page_discovery_input(
    source: str,
    page_refs: list[str],
    max_posts: int,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Ask a source for the posts of the pages the operator named.

    This is the first half of "give me the comments of this page": no comment
    Actor accepts a page, so the page must first be turned into post URLs.
    """
    refs = [normalise_page_ref(source, x) for x in page_refs]
    refs = [x for x in dict.fromkeys(refs) if x]
    if not refs:
        raise ValueError("At least one page reference is required.")
    limit = max(1, int(max_posts))

    if source == "facebook":
        out = {"startUrls": [{"url": u} for u in refs], "resultsLimit": limit}
        if date_from:
            out["onlyPostsNewerThan"] = date_from
        if date_to:
            out["onlyPostsOlderThan"] = date_to
        return out
    if source == "instagram":
        # (`skipPinnedPosts` is not a documented apify/instagram-scraper field;
        # pinned posts are handled by SIGNALYTH's exact date post-filter.)
        out = {"directUrls": refs, "resultsType": "posts", "resultsLimit": limit,
               "addParentData": True}
        if date_from:
            out["onlyPostsNewerThan"] = date_from
        return out
    if source == "tiktok":
        out = {"profiles": refs, "resultsPerPage": limit, "profileSorting": "latest",
               "excludePinnedPosts": True}
        if date_from:
            out["oldestPostDateUnified"] = date_from
        if date_to:
            out["newestPostDate"] = date_to
        return out
    if source == "x":
        # Documented mode for a handle's own timeline is `profileTweets`
        # ("matches the Posts tab on X"). `profile` is not a mode value.
        return {"twitterHandles": refs, "mode": "profileTweets", "maxItems": limit,
                "maxItemsPerTarget": max(1, limit // max(1, len(refs)))}
    raise ValueError(f"Page discovery is not configured for source {source}.")


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
