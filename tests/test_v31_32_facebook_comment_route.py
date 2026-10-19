"""v31.32 — Facebook's comment route sent the wrong field name and collected nothing.

The configured Facebook comment Actor is dami_studio/facebook-comments-scraper.
Its live schema, read through the app's own Actor lookup on 19 Oct 2026:

    startUrls              array
    resultsLimit           integer   (per post, max 50000)
    includeNestedComments  boolean
    maxRepliesPerComment   integer   (max 1000)
    onlyCommentsNewerThan  string
    viewOption             string

SIGNALYTH was still sending `postUrls` and `commentsSortType` — the field names
of scraper_one/facebook-comments-scraper, the Actor this one replaced. The
failure was silent in the only way that matters: run 20261017T174324Z asked for
300 comments under 34 Facebook posts and collected ZERO, surfacing only as a
failed comment pass on the second largest source in the study.

The field name belongs to the ACTOR, not to the source, so it is chosen from
the configured Actor and not hard-coded per platform.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.source_capabilities import (
    MAX_REPLIES_PER_COMMENT,
    build_comment_deepening_input,
)


POST = "https://www.facebook.com/SNFCC/posts/pfbid0abc123"
NEW_ACTOR = "dami_studio/facebook-comments-scraper"
OLD_ACTOR = "scraper_one/facebook-comments-scraper"


def _build(actor_id: str, *, include_replies: bool = True, date_from=date(2026, 4, 17)):
    return build_comment_deepening_input(
        "facebook", [POST], 300,
        max_per_parent=120,
        include_replies=include_replies,
        date_from=date_from,
        date_to=date(2026, 10, 17),
        actor_id=actor_id,
    )


# ---------- the defect ----------

def test_the_configured_actor_is_asked_with_its_own_field_name():
    built = _build(NEW_ACTOR)
    # v31.35: `startUrls` is a requestListSources field, so each entry is an
    # object carrying a `url` key.
    assert built["startUrls"] == [{"url": POST}]
    assert "postUrls" not in built, (
        "postUrls belongs to the Actor this one replaced; sending it collects nothing")
    assert "commentsSortType" not in built, "that field does not exist on this Actor"


def test_the_replaced_actor_still_gets_its_own_shape():
    """The branch is on the Actor, not the platform: an older configuration
    must keep working rather than break the other way round."""
    built = _build(OLD_ACTOR)
    assert built["postUrls"] == [POST]
    assert "startUrls" not in built


def test_an_unknown_actor_falls_back_to_the_documented_older_shape():
    built = _build("someone/unheard-of-scraper")
    assert "postUrls" in built


# ---------- the capability the vendor added ----------

def test_replies_are_requested_when_the_source_wants_them():
    built = _build(NEW_ACTOR, include_replies=True)
    assert built["includeNestedComments"] is True
    assert built["maxRepliesPerComment"] == MAX_REPLIES_PER_COMMENT
    assert 1 <= MAX_REPLIES_PER_COMMENT <= 1000, "the Actor's own bound"


def test_replies_are_not_requested_when_the_source_does_not_want_them():
    built = _build(NEW_ACTOR, include_replies=False)
    assert "includeNestedComments" not in built
    assert "maxRepliesPerComment" not in built


def test_the_native_date_filter_is_used_so_old_comments_are_never_paid_for():
    built = _build(NEW_ACTOR, date_from=date(2026, 4, 17))
    assert built["onlyCommentsNewerThan"] == "2026-04-17"


def test_no_date_filter_is_invented_when_the_run_has_no_lower_bound():
    built = _build(NEW_ACTOR, date_from=None)
    assert "onlyCommentsNewerThan" not in built


# ---------- the per-post limit is still the registry's to set ----------

@pytest.mark.parametrize("per_parent", [5, 120, 500])
def test_the_per_post_limit_is_passed_through(per_parent):
    built = build_comment_deepening_input(
        "facebook", [POST], 1000, max_per_parent=per_parent,
        include_replies=True, date_from=None, date_to=None, actor_id=NEW_ACTOR,
    )
    assert built["resultsLimit"] == per_parent


def test_other_sources_are_untouched_by_the_actor_argument():
    """The argument is additive: every other source keeps its own shape."""
    tweet = build_comment_deepening_input(
        "x", ["1234567890"], 50, max_per_parent=25, include_replies=True,
        date_from=None, date_to=None, actor_id=NEW_ACTOR,
    )
    assert tweet["mode"] == "replies" and tweet["replyTweetIds"] == ["1234567890"]

    insta = build_comment_deepening_input(
        "instagram", ["https://www.instagram.com/p/Cabc123/"], 50,
        max_per_parent=25, include_replies=True, date_from=None, date_to=None,
        actor_id=NEW_ACTOR,
    )
    assert insta["directUrls"] == ["https://www.instagram.com/p/Cabc123/"]
