"""Every paid Actor input must use only DOCUMENTED fields and enum values.

The values below were read verbatim from each Actor's public input schema on
apify.com (2026-09-25). A field that is not in the schema is either ignored
or rejected by the Actor — in both cases we pay for a request that does not
do what the code believes it does. Two such cases reached main during the
v31 work: an invented `skipPinnedPosts` on apify/instagram-scraper and
`sortOrder: "recent"` on scrapesmith/instagram-comments-scraper (the enum is
popular | recent_activity).

When an Actor's schema changes, update the allow-lists here from the schema
page — never from memory.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan, semantic_broad_probe_target
from app.services.source_capabilities import build_comment_deepening_input

# Documented input fields per discovery Actor (verbatim from apify.com).
DISCOVERY_FIELDS = {
    "xquik/x-tweet-scraper": {
        "mode", "searchTerms", "searchQuery", "startUrls", "twitterHandles", "tweetIds",
        "listIds", "conversationIds", "maxItems", "maxItemsPerTarget", "includeSearchTerms",
        "queryType", "since", "until", "sinceTime", "untilTime", "withinTime",
        "min_faves", "max_faves", "min_retweets", "max_retweets", "min_replies", "max_replies",
        "outputVariant", "fieldStyle", "outputPreset",
    },
    "epctex/tiktok-search-scraper": {
        "search", "startUrls", "maxItems", "endPage", "dateRange", "location", "sortType",
        "customMapFunction", "proxy",
    },
    "apify/instagram-scraper": {
        "directUrls", "resultsType", "resultsLimit", "onlyPostsNewerThan", "search",
        "searchType", "searchLimit", "addParentData",
    },
    "scraper_one/facebook-posts-search": {
        "query", "resultsCount", "searchType", "location", "startDate", "endDate",
    },
    "apidojo/youtube-scraper": {
        "startUrls", "keywords", "youtubeHandles", "gl", "hl", "uploadDate", "duration",
        "sort", "maxItems", "customMapFunction",
    },
    "logiover/google-news-scraper": {
        "queries", "topic", "geoLocation", "topHeadlines", "language", "country",
        "timeWindow", "fromDate", "toDate", "includeSources", "excludeSources",
        "maxArticles", "resolveUrls", "extractThumbnails", "proxyConfiguration",
    },
}

# Documented enum values that the planner emits.
ENUMS = {
    ("epctex/tiktok-search-scraper", "dateRange"): {
        "DEFAULT", "ALL_TIME", "YESTERDAY", "THIS_WEEK", "THIS_MONTH",
        "LAST_THREE_MONTHS", "LAST_SIX_MONTHS"},
    ("epctex/tiktok-search-scraper", "sortType"): {"RELEVANCE", "MOST_LIKED", "DATE_POSTED"},
    ("scraper_one/facebook-posts-search", "searchType"): {"top", "latest"},
    ("apidojo/youtube-scraper", "uploadDate"): {"all", "l", "t", "w", "m", "y"},
    ("apidojo/youtube-scraper", "sort"): {"r", "v"},
}

SOURCES = ["x", "tiktok", "instagram", "facebook", "youtube", "news"]


def _draft(sources=SOURCES, **kw):
    base = dict(
        client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
        date_from=date(2026, 9, 17), date_to=date(2026, 9, 23),
        keywords=["κλήρωση"], sources=list(sources), sample_mode="perSource",
        per_source={s: 20 for s in sources},
        per_source_comments={s: 50 for s in sources if s not in ("news", "youtube")},
        comments=True, max_budget_usd=5.0, smart_search=True, report_language="Ελληνικά",
    )
    base.update(kw)
    return AnalysisDraft(**base)


def _all_subruns(plan: dict):
    for sp in plan["sources"]:
        for kind in ("subruns", "topup_subruns", "semantic_topup_subruns"):
            for sr in sp.get(kind) or []:
                yield sp["source"], sr


class TestDiscoveryInputsUseOnlyDocumentedFields:
    @pytest.mark.parametrize("keywords", [["κλήρωση"], []])
    def test_no_invented_fields_reach_a_paid_actor(self, monkeypatch, keywords):
        monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _d: [])
        plan = build_collection_plan(_draft(keywords=keywords)).model_dump(mode="json")
        seen = set()
        for source, sr in _all_subruns(plan):
            allowed = DISCOVERY_FIELDS.get(sr["actor_id"])
            if allowed is None:
                continue  # a generic/unknown actor is covered by its own registry test
            extra = set(sr["input"]) - allowed
            assert not extra, f"{source}/{sr['purpose']} sends undocumented field(s) {sorted(extra)}"
            seen.add(sr["actor_id"])
        assert seen, "no planned actor was checked"

    def test_enum_values_are_the_documented_ones(self, monkeypatch):
        monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _d: [])
        plan = build_collection_plan(_draft()).model_dump(mode="json")
        for source, sr in _all_subruns(plan):
            for (actor, field), values in ENUMS.items():
                if sr["actor_id"] == actor and field in sr["input"]:
                    assert sr["input"][field] in values, f"{source}: {field}={sr['input'][field]!r}"

    def test_youtube_country_and_language_codes_are_lowercase_as_documented(self, monkeypatch):
        monkeypatch.setattr("app.services.query_planner.suggest_public_names", lambda _d: [])
        plan = build_collection_plan(_draft(["youtube"])).model_dump(mode="json")
        for _, sr in _all_subruns(plan):
            assert sr["input"]["gl"] == "gr" and sr["input"]["hl"] == "el", sr["input"]


class TestCommentInputsUseOnlyDocumentedFields:
    def test_instagram_sort_order_is_a_documented_enum_value(self):
        inp = build_comment_deepening_input("instagram", ["https://www.instagram.com/p/ABC/"], 40,
                                            max_per_parent=40)
        assert set(inp) <= {"postUrls", "maxCommentsPerPost", "sortOrder"}
        assert inp["sortOrder"] in {"popular", "recent_activity"}
        # Dated research wants the newest activity, not all-time favourites.
        assert inp["sortOrder"] == "recent_activity"

    def test_tiktok_comment_input_is_documented(self):
        inp = build_comment_deepening_input("tiktok", ["https://www.tiktok.com/@u/video/7"], 40,
                                            max_per_parent=40)
        assert set(inp) <= {"startUrls", "includeReplies", "endPage", "maxItems", "customMapFunction"}

    def test_facebook_comment_input_is_documented_for_the_registered_actor(self):
        inp = build_comment_deepening_input(
            "facebook", ["https://www.facebook.com/page/posts/1"], 40, max_per_parent=40)
        # scraper_one/facebook-comments-scraper: postUrls, resultsLimit, commentsSortType
        assert set(inp) <= {"postUrls", "resultsLimit", "commentsSortType"}, inp
        assert inp.get("commentsSortType") in {None, "relevant", "newest", "all"}

    def test_x_comment_input_fields_are_documented(self):
        inp = build_comment_deepening_input("x", ["123"], 40, max_per_parent=40)
        documented = {"mode", "replyTweetIds", "threadTweetIds", "tweetIds", "conversationIds",
                      "maxItems", "maxItemsPerTarget", "since", "until"}
        assert set(inp) <= documented, inp
        assert inp["mode"] in {"replies", "thread", "search"}


class TestTheProbeStaysAMinorityRoute:
    def test_probe_never_exceeds_half_the_target_or_twenty(self):
        for target in (8, 20, 40, 100, 500):
            cap = semantic_broad_probe_target(target)
            assert cap <= 20
            assert cap <= max(5, -(-target // 2))


class TestRefillNeverReBuysAPaidSearch:
    def test_identical_routes_already_run_by_the_collector_are_recognised(self):
        from app.services.relevance_expansion import _already_paid_route_signatures, _route_signature
        inp = {"query": "Eurojackpot Ελλάδα", "resultsCount": 7, "searchType": "latest",
               "startDate": "2026-09-17", "endDate": "2026-09-23"}
        sp = {"source": "facebook", "actor_id": "scraper_one/facebook-posts-search",
              "subruns": [{"actor_id": "scraper_one/facebook-posts-search", "input": inp},
                          {"actor_id": "scraper_one/facebook-posts-search",
                           "input": {**inp, "query": "Eurojackpot Greece"}}],
              "topup_subruns": []}
        status = {"sources": {"facebook": {"subruns": [
            {"status": "succeeded"}, {"status": "skipped_target_met"}]}}}
        paid = _already_paid_route_signatures(status, sp)
        # The same search with a different COUNT is still the same paid search.
        assert _route_signature(sp["actor_id"], {**inp, "resultsCount": 30}) in paid
        # A route the collector skipped never ran, so the refill may use it.
        assert _route_signature(sp["actor_id"], {**inp, "query": "Eurojackpot Greece"}) not in paid


class TestPageDiscoveryInputsUseOnlyDocumentedFields:
    """The operator's own pages/accounts are turned into post URLs first."""

    def test_x_page_discovery_uses_a_documented_mode(self):
        from app.services.source_capabilities import build_page_discovery_input
        inp = build_page_discovery_input("x", ["@opap_gr", "https://x.com/eurojackpot"], 20)
        assert inp["mode"] in {"legacy", "tweet", "tweets", "search", "profileTweets", "profileReplies",
                               "profileMedia", "profileLikes", "listTweets", "article", "replies",
                               "quotes", "thread", "retweeters", "favoriters"}
        assert inp["mode"] == "profileTweets"          # the Posts tab of a handle
        assert inp["twitterHandles"] == ["opap_gr", "eurojackpot"]
        assert set(inp) <= {"twitterHandles", "mode", "maxItems", "maxItemsPerTarget"}

    def test_instagram_page_discovery_sends_only_documented_fields(self):
        from app.services.source_capabilities import build_page_discovery_input
        inp = build_page_discovery_input("instagram", ["opap_gr"], 20, date_from="2026-08-25")
        assert set(inp) <= {"directUrls", "resultsType", "resultsLimit", "onlyPostsNewerThan",
                            "search", "searchType", "searchLimit", "addParentData"}
        assert inp["directUrls"] == ["https://www.instagram.com/opap_gr"]

    def test_facebook_and_tiktok_page_discovery_match_their_schemas(self):
        from app.services.source_capabilities import build_page_discovery_input
        fb = build_page_discovery_input("facebook", ["facebook.com/OPAP.gr"], 20,
                                        date_from="2026-08-25", date_to="2026-09-24")
        assert set(fb) <= {"startUrls", "resultsLimit", "captionText", "onlyPostsNewerThan", "onlyPostsOlderThan"}
        assert fb["startUrls"] == [{"url": "https://facebook.com/OPAP.gr"}]
        tt = build_page_discovery_input("tiktok", ["@opap.gr"], 20, date_from="2026-08-25", date_to="2026-09-24")
        assert set(tt) <= {"profiles", "resultsPerPage", "profileSorting", "excludePinnedPosts",
                           "oldestPostDateUnified", "newestPostDate"}
        assert tt["profileSorting"] in {"latest", "popular", "oldest"}
        assert tt["profiles"] == ["opap.gr"]
