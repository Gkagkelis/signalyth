from __future__ import annotations

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


def build_comment_deepening_input(
    source: str,
    seed_refs: list[str],
    max_items: int,
    *,
    max_per_parent: int | None = None,
    include_replies: bool = True,
) -> dict:
    """Build the curated public-schema input shape for a comment/reply route."""
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
        return {"twitterHandles": refs, "mode": "profile", "maxItems": limit}
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
