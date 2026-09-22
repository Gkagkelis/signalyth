"""Giving the operator's own pages priority in the comment layer.

The conversation about a brand very often sits under the brand's OWN posts —
the operator can see that with his eyes, the search ranking cannot. No comment
Actor accepts a page, so a page has to be turned into its posts first.

These tests pin the three decisions that make that useful:
  * a reserved share of each source's comment target goes to those pages,
  * the rest goes to open search,
  * and whatever open search fails to deliver comes BACK to the pages, so the
    layer is never left half empty the way the first Eurojackpot run was.
"""
from __future__ import annotations

import pytest

from app.services.relevance_expansion import (
    PARENT_LOOKBACK_DAYS,
    _post_ref_from_row,
    operator_page_refs,
    owned_comment_quota,
    parent_search_from,
)
from app.services.source_capabilities import (
    build_page_discovery_input,
    normalise_page_ref,
)


class TestWhatTheOperatorPastes:
    """People paste a URL, a handle, or something with tracking junk on it."""

    @pytest.mark.parametrize("raw", [
        "https://www.facebook.com/allwyngr/?ref=page_internal",
        "facebook.com/allwyngr",
        "www.facebook.com/allwyngr/",
    ])
    def test_facebook_always_becomes_an_address(self, raw):
        out = normalise_page_ref("facebook", raw)
        assert out.startswith("http")
        assert out.endswith("/allwyngr")
        assert "?" not in out

    @pytest.mark.parametrize("raw,expected", [
        ("https://www.tiktok.com/@opapofficial", "opapofficial"),
        ("@opapofficial", "opapofficial"),
        ("opapofficial", "opapofficial"),
    ])
    def test_tiktok_always_becomes_a_username(self, raw, expected):
        assert normalise_page_ref("tiktok", raw) == expected

    def test_x_takes_the_handle_out_of_a_link(self):
        assert normalise_page_ref("x", "https://x.com/opap_sa") == "opap_sa"

    def test_the_same_page_pasted_twice_is_collected_once(self):
        refs = operator_page_refs(
            {"source_pages": {"tiktok": ["@opapofficial", "https://www.tiktok.com/@opapofficial"]}},
            "tiktok",
        )
        assert refs == ["opapofficial"]

    def test_pages_for_another_source_are_ignored(self):
        plan = {"source_pages": {"facebook": ["facebook.com/x"], "tiktok": ["@y"]}}
        assert operator_page_refs(plan, "tiktok") == ["y"]


class TestEachActorGetsItsOwnShape:
    """A wrong field name is a paid call that returns nothing."""

    def test_facebook_gets_page_urls_and_a_date_window(self):
        out = build_page_discovery_input(
            "facebook", ["facebook.com/allwyngr"], 60,
            date_from="2026-07-24", date_to="2026-09-21")
        assert out["startUrls"] == [{"url": "https://facebook.com/allwyngr"}]
        assert out["onlyPostsNewerThan"] == "2026-07-24"
        assert out["onlyPostsOlderThan"] == "2026-09-21"
        assert out["resultsLimit"] == 60

    def test_tiktok_gets_usernames_not_urls(self):
        out = build_page_discovery_input("tiktok", ["https://www.tiktok.com/@opapofficial"], 40,
                                         date_from="2026-07-24", date_to="2026-09-21")
        assert out["profiles"] == ["opapofficial"]
        assert out["oldestPostDateUnified"] == "2026-07-24"

    def test_instagram_asks_for_posts_from_the_profile(self):
        out = build_page_discovery_input("instagram", ["instagram.com/allwyngr/"], 30,
                                         date_from="2026-07-24")
        assert out["directUrls"] == ["https://instagram.com/allwyngr"]
        assert out["resultsType"] == "posts", "comments cannot be asked of a profile"

    def test_x_gets_handles(self):
        assert build_page_discovery_input("x", ["@opap_sa"], 25)["twitterHandles"] == ["opap_sa"]

    def test_a_source_with_no_page_route_says_so(self):
        with pytest.raises(ValueError):
            build_page_discovery_input("news", ["example.com"], 10)

    def test_no_pages_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError):
            build_page_discovery_input("facebook", ["   "], 10)


class TestParentsAreSearchedFurtherBack:
    def test_the_window_opens_earlier_for_parents(self):
        from datetime import date

        assert parent_search_from(date(2026, 8, 23)) == "2026-07-24"
        assert PARENT_LOOKBACK_DAYS == 30

    def test_why_it_matters(self):
        """A post from before the window still collects comments inside it."""
        from datetime import date

        window_start = date(2026, 9, 17)
        assert parent_search_from(window_start) < window_start.isoformat()


class TestTheSplit:
    def test_the_reserved_share_is_what_it_says(self):
        assert owned_comment_quota(250, 60) == 150
        assert owned_comment_quota(100, 40) == 40

    def test_zero_share_leaves_everything_to_open_search(self):
        assert owned_comment_quota(250, 0) == 0

    def test_full_share_leaves_nothing(self):
        assert owned_comment_quota(250, 100) == 250

    def test_a_missing_setting_falls_back_to_the_default(self):
        assert owned_comment_quota(250, None) == 150

    def test_a_tiny_target_still_reserves_something(self):
        """Rounding must never silently zero the operator's own pages."""
        assert owned_comment_quota(1, 60) == 1


class TestReadingPostsBackFromAPage:
    def test_a_facebook_post_gives_its_url_and_text(self):
        ref, text = _post_ref_from_row("facebook", {
            "url": "https://www.facebook.com/allwyngr/posts/123", "text": "Τζακ ποτ 120 εκατ."})
        assert ref.endswith("/posts/123")
        assert "Τζακ ποτ" in text

    def test_an_x_post_gives_its_id_because_that_is_what_replies_need(self):
        ref, _ = _post_ref_from_row("x", {"id": "1770", "url": "https://x.com/a/status/1770"})
        assert ref == "1770"

    def test_a_tiktok_video_gives_its_watch_url(self):
        ref, _ = _post_ref_from_row("tiktok", {"webVideoUrl": "https://www.tiktok.com/@a/video/9"})
        assert ref.endswith("/video/9")

    def test_a_row_with_nothing_usable_is_dropped_not_guessed(self):
        assert _post_ref_from_row("facebook", {"likes": 3}) == ("", "")
        assert _post_ref_from_row("facebook", "not a row") == ("", "")
