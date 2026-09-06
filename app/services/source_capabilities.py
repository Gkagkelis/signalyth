from __future__ import annotations

from copy import deepcopy

# Public-schema capability knowledge. This is NOT a live verification record.
# It exists so SIGNALYTH can forecast what a source can plausibly support before a paid smoke test.
# Companion Actors remain disabled until explicitly live-smoke-tested and committed.
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
            "enabled": True,
            "note": "Primary Actor exposes direct-reply collection; live acceptance still requires paid smoke verification.",
        },
    },
    "instagram": {
        "discovery": "primary_actor",
        "comment_deepening": {
            "mode": "same_actor",
            "candidate_actor_id": "apify/instagram-scraper",
            "input_route": "comments",
            "input_field": "directUrls",
            "public_schema_known": True,
            "live_verified": False,
            "enabled": False,
            "note": "Same Actor publicly exposes comments mode, but SIGNALYTH will not auto-activate it before a paid smoke test.",
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
            "note": "Requires a separate comments Actor; candidate must be independently smoke-tested and committed.",
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
            "note": "Requires a separate comments Actor; candidate must be independently smoke-tested and committed.",
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
            "note": "Requires a separate comments Actor; candidate must be independently smoke-tested and committed.",
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
            "note": "News is treated as publisher/article evidence; on-site comments are not part of the default collection contract.",
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
            "note": "Capabilities unknown until Actor schema inspection and live smoke verification.",
        },
    }))


def comments_forecast(source: str, requested: bool, registry_cfg: dict | None = None) -> dict:
    """Return a fail-closed comment/reply capability forecast.

    Public schema knowledge is not treated as live verification. A route becomes
    executable only after a separate comment-deepening smoke test has explicitly
    recorded ``comment_deepening_status=verified`` in the source registry.
    """
    cap = source_capabilities(source)["comment_deepening"]
    registry_cfg = registry_cfg or {}
    route_status = str(registry_cfg.get("comment_deepening_status") or "unverified")
    configured_actor = registry_cfg.get("comment_actor_id") or cap.get("candidate_actor_id")
    live_verified = bool(cap.get("live_verified")) or route_status == "verified"
    enabled = bool(cap.get("enabled")) or route_status == "verified"
    if not requested:
        status = "not_requested"
    elif cap.get("mode") == "not_applicable":
        status = "not_applicable"
    elif enabled and live_verified:
        status = "verified_available"
    elif enabled and cap.get("public_schema_known"):
        status = "available_but_unverified"
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
        "enabled": enabled,
        "registry_route_status": route_status,
    }


def build_comment_deepening_input(source: str, seed_refs: list[str], max_items: int) -> dict:
    """Build a source-specific comment/reply input from already discovered seeds.

    This function does not grant trust. Execution is allowed only when the registry
    separately records a clean paid live verification for the route.
    """
    cap = source_capabilities(source).get("comment_deepening") or {}
    if cap.get("mode") == "not_applicable":
        raise ValueError(f"Comment deepening is not applicable to source {source}.")
    field = cap.get("input_field")
    if not field:
        raise ValueError(f"No comment-route input field is known for source {source}.")
    refs = [str(x).strip() for x in seed_refs if str(x or "").strip()]
    if not refs:
        raise ValueError("At least one seed reference is required.")
    inp = {field: refs[:40]}
    route = cap.get("input_route")
    if source == "x" and route:
        inp["mode"] = route
    inp["maxItems"] = max(1, int(max_items))
    return inp


def build_comment_smoke_input(source: str, seed_refs: list[str], max_items: int) -> dict:
    """Build a capped one-time paid smoke input; live validation is still mandatory."""
    inp = build_comment_deepening_input(source, seed_refs, max(1, min(10, int(max_items))))
    inp["maxItems"] = max(1, min(10, int(max_items)))
    return inp
