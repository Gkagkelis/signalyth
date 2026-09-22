"""No page URL may reach a comment Actor. Proven through the whole pipeline.

This is the production failure that 717 passing tests did not see, because
every fake Actor in the suite returned well-formed post permalinks. A fake that
always behaves correctly cannot reveal that the code never checks its input.

The Facebook page-discovery Actor really does return rows whose `url` is the
PAGE, with the post permalink in another field. That row became a "parent", the
page URL went to the comments Actor, and the Actor churned and returned nothing
for $0.00 — which read, from the outside, like a slow or broken provider.

So the fake here behaves badly on purpose.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import settings
from app.models import AnalysisDraft
from app.services.query_planner import build_collection_plan
from app.services.run_manager import RunManager
from app.services.source_capabilities import (
    build_comment_deepening_input,
    is_comment_parent_ref,
)
from app.services.storage import RunStore

from tests.test_whole_pipeline_v19 import FakeApify, FakeModel, _wait_terminal

PAGE = "https://facebook.com/allwyngr.official.account"
POST = "https://www.facebook.com/allwyngr.official.account/posts/123456789"


class AwkwardPageActor(FakeApify):
    """Page discovery that puts the PAGE in `url`, as the real one does."""

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        is_page_call = bool(run_input.get("directUrls") or run_input.get("profiles")
                            or run_input.get("twitterHandles")
                            or (run_input.get("startUrls")
                                and not (run_input.get("postUrls")
                                         or run_input.get("replyTweetIds"))))
        if is_page_call:
            type(self).calls.append((actor_id, dict(run_input)))
            return ({"id": actor_id, "defaultDatasetId": "d", "usageTotalUsd": 0.01},
                    [{
                        # What the real Actor hands back: the page in `url`,
                        # the actual permalink somewhere else.
                        "url": PAGE,
                        "postUrl": f"https://www.facebook.com/allwyngr.official.account/posts/{i}",
                        "text": f"Eurojackpot τζακ ποτ {i}",
                        "timestamp": "2026-09-10T12:00:00Z",
                        "createdAt": "2026-09-10T12:00:00Z",
                        "commentsCount": 30, "likesCount": 40, "viewCount": 900,
                        "authorUsername": "allwyngr", "type": "post",
                    } for i in range(1, 4)])
        return super().run(actor_id, run_input,
                           max_items=max_items, max_charge_usd=max_charge_usd)


class TheGuardItself(unittest.TestCase):
    def test_a_page_is_refused_and_a_post_is_accepted(self):
        self.assertFalse(is_comment_parent_ref("facebook", PAGE))
        self.assertTrue(is_comment_parent_ref("facebook", POST))

    def test_a_call_with_only_a_page_is_refused_rather_than_paid_for(self):
        with pytest.raises(ValueError):
            build_comment_deepening_input("facebook", [PAGE], 40, max_per_parent=40)

    def test_a_mixed_list_keeps_only_the_post(self):
        payload = build_comment_deepening_input("facebook", [PAGE, POST], 40, max_per_parent=40)
        self.assertEqual(payload["postUrls"], [POST])


class NoPageReachesACommentActor(unittest.TestCase):
    """The whole run, with a page-discovery Actor that behaves like the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.manager = RunManager(store=self.store, max_workers=1)
        FakeApify.calls = []
        self._ai, self._key = settings.signalyth_ai_enabled, settings.openai_api_key
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "test-key"

    def tearDown(self):
        settings.signalyth_ai_enabled, settings.openai_api_key = self._ai, self._key
        self.manager.shutdown(wait=True)
        self.tmp.cleanup()

    def _plan(self):
        draft = AnalysisDraft(
            client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
            date_from=date(2026, 8, 23), date_to=date(2026, 9, 21),
            keywords=["κλήρωση", "τζακ ποτ"], sources=["facebook"],
            sample_mode="perSource", per_source={"facebook": 12},
            per_source_comments={"facebook": 40},
            source_pages={"facebook": [PAGE]}, owned_share_pct=60,
            comments=True, max_budget_usd=5.0, smart_search=True,
            report_language="Ελληνικά",
        )
        plan = build_collection_plan(draft).model_dump(mode="json")
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") == "facebook":
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
        return plan

    def test_the_comment_actor_is_never_handed_a_page(self):
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", AwkwardPageActor), \
             patch("app.services.relevance_expansion.ApifyRunner", AwkwardPageActor), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id, timeout=180.0)

        comment_calls = [(a, i) for a, i in FakeApify.calls
                         if i.get("postUrls") or i.get("replyTweetIds")
                         or i.get("mode") == "replies"]
        self.assertTrue(comment_calls, "the comment layer never ran, so this proves nothing")

        for actor_id, run_input in comment_calls:
            for ref in (run_input.get("postUrls") or []):
                self.assertTrue(
                    is_comment_parent_ref("facebook", str(ref)),
                    f"{actor_id} was handed {ref!r}, which is not a post",
                )
                self.assertNotIn(
                    str(ref).rstrip("/"), {PAGE.rstrip("/"), PAGE.replace("https://", "")},
                    f"{actor_id} was handed the page itself",
                )

        self.assertNotEqual(done.get("status"), "failed",
                            msg=f"fatal_error={done.get('fatal_error')}")

    def test_the_permalink_is_what_gets_used(self):
        """Not just "no page" — the real post must actually be picked up."""
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", AwkwardPageActor), \
             patch("app.services.relevance_expansion.ApifyRunner", AwkwardPageActor), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            _wait_terminal(self.store, run_id, timeout=180.0)

        sent = [str(r) for a, i in FakeApify.calls for r in (i.get("postUrls") or [])]
        self.assertTrue(
            [r for r in sent if "/posts/" in r],
            f"the page's real posts were never collected from: {sent[:5]}",
        )

    def test_comments_still_arrive(self):
        """A guard that blocks everything would also pass the test above."""
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", AwkwardPageActor), \
             patch("app.services.relevance_expansion.ApifyRunner", AwkwardPageActor), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id, timeout=180.0)

        folder = self.store.folder_for(run_id)
        comments = self.store.read(folder / "normalized-comments-facebook.json", []) or []
        self.assertTrue(comments,
                        f"no comments collected at all: {done.get('comment_deepening')}")


if __name__ == "__main__":
    unittest.main()
