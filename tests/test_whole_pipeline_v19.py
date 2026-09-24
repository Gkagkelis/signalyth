"""One run, from nothing to a finished report, through the real worker.

Everything below the Actors and the model is the real code: the run manager,
collection, cleaning, the comment layer with the operator's pages, AI analysis,
the intelligence engine. Only the two things that cost money are faked.

This exists because the app kept failing in ways no unit test could see — the
pieces each worked, the whole did not. A green suite that never runs a whole
run is not evidence that a run works.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.models import AnalysisDraft
from app.services.ai_analysis import ProviderBatchResult
from app.services.query_planner import build_collection_plan
from app.services.run_manager import RunManager
from app.services.storage import RunStore

TERMINAL = {"cancelled", "succeeded", "completed_shortfall", "completed_with_errors",
            "failed", "interrupted"}

#: Real Greek opinion, the kind that sits under a brand's own post.
COMMENT_TEXTS = [
    "Πάλι τίποτα, κάθε βδομάδα τα ίδια.",
    "Στημένα είναι όλα, ποτέ δεν κερδίζει Έλληνας.",
    "Επιτέλους πληρώθηκα κανονικά, όλα καλά.",
    "Έπαιξα πέντε στήλες και δεν βρήκα ούτε έναν αριθμό.",
    "Ο ΟΠΑΠ κερδίζει πάντα, εμείς ποτέ.",
    "Καλή τύχη σε όλους, εγώ το ευχαριστιέμαι.",
]


def _wait_terminal(store: RunStore, run_id: str, timeout=90.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = store.read_status(run_id)
        if last.get("status") in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run never finished: status={last.get('status')} phase={last.get('phase')}")


def _real_post_url(source: str, author: str, i) -> str:
    """A correctly shaped permalink for each platform.

    The fakes used to return `https://facebook.example/...`, which no real URL
    guard can accept. A suite built on impossible URLs cannot notice that the
    code never checks them — which is how a page URL reached a comment Actor in
    production while 717 tests stayed green.
    """
    if source == "facebook":
        return f"https://www.facebook.com/{author}/posts/{i}"
    if source == "instagram":
        return f"https://www.instagram.com/p/POST{i}/"
    if source == "tiktok":
        return f"https://www.tiktok.com/@{author}/video/70000000000000000{i}"
    return f"https://x.com/{author}/status/170000000000000000{i}"


class FakeApify:
    """Serves the three questions the pipeline asks, like the real Actors do."""

    calls: list[tuple[str, dict]] = []

    def __init__(self):
        pass

    @staticmethod
    def _post(i, source, author="newspage"):
        return {
            "id": f"{source}-post-{i}",
            "text": f"Eurojackpot: τζακ ποτ {100 + i} εκατ. ευρώ στην κλήρωση της Τρίτης.",
            "timestamp": "2026-09-10T12:00:00Z", "createdAt": "2026-09-10T12:00:00Z",
            "url": _real_post_url(source, author, i),
            "postUrl": _real_post_url(source, author, i),
            "webVideoUrl": _real_post_url(source, author, i),
            "authorUsername": author, "ownerUsername": author,
            "commentsCount": 25, "replyCount": 25, "likesCount": 40, "likeCount": 40,
            "viewCount": 900, "type": "post",
        }

    @staticmethod
    def _comment(i, source, parent):
        return {
            "id": f"{source}-cmt-{i}",
            "text": COMMENT_TEXTS[i % len(COMMENT_TEXTS)],
            "timestamp": "2026-09-11T09:00:00Z", "createdAt": "2026-09-11T09:00:00Z",
            "url": _real_post_url(source, "commenter", 900000 + i),
            "postUrl": parent, "profileName": f"user{i}", "authorUsername": f"user{i}",
            "likesCount": 2, "likeCount": 2,
        }

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append((actor_id, dict(run_input)))
        meta = {"id": actor_id, "defaultDatasetId": "d",
                "usageTotalUsd": min(0.01, max_charge_usd)}
        source = ("facebook" if "facebook" in actor_id else
                  "instagram" if "instagram" in actor_id else
                  "tiktok" if "tiktok" in actor_id else "x")

        # Comments: the input carries parent references.
        parents = (run_input.get("postUrls") or run_input.get("startUrls")
                   or run_input.get("replyTweetIds") or [])
        is_comment_call = bool(run_input.get("postUrls") or run_input.get("replyTweetIds")
                               or run_input.get("mode") == "replies")
        if is_comment_call and parents:
            first = parents[0]
            parent = first.get("url") if isinstance(first, dict) else str(first)
            return meta, [self._comment(i, source, parent) for i in range(min(max_items, 40))]

        # Page discovery: the input names pages/profiles.
        if run_input.get("directUrls") or run_input.get("profiles") or run_input.get("twitterHandles") \
                or (run_input.get("startUrls") and not is_comment_call):
            return meta, [self._post(500 + i, source, author="brandpage")
                          for i in range(min(max_items, 6))]

        # Otherwise: keyword search.
        return meta, [self._post(i, source) for i in range(min(max_items, 12))]


class FakeModel:
    """A model that answers every record confidently, so nothing is left pending."""

    def __init__(self, *a, **k):
        pass

    def analyze_batch(self, records, context, tier):
        items = []
        for row in records:
            text = str(row.get("text") or "")
            negative = any(w in text for w in ("τίποτα", "Στημένα", "δεν βρήκα", "ποτέ"))
            items.append({
                "record_id": row["record_id"],
                "semantic_relevance": "relevant", "relevance_score": 0.92,
                "relevance_confidence": 0.95,
                "relevance_reason": "Αναφέρεται άμεσα στο παιχνίδι.",
                "target_entity": "Eurojackpot",
                "target_stance": "critical" if negative else "supportive",
                "sentiment_label": "negative" if negative else "positive",
                "sentiment_score": -0.8 if negative else 0.7,
                "sentiment_confidence": 0.93,
                "primary_emotion": "anger" if negative else "joy",
                "secondary_emotion": "neutral",
                "emotion_intensity": 0.7, "emotion_confidence": 0.9,
                "topic": "Eurojackpot", "narrative": "πληρωμές και τύχη",
                "sarcasm": False, "sarcasm_confidence": 0.05,
                "language": "greek", "evidence_quotes": [],
                "overall_confidence": 0.95,
            })
        return ProviderBatchResult(items=items, model="fake-model", response_id=None,
                                   usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})


class WholePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.manager = RunManager(store=self.store, max_workers=1)
        FakeApify.calls = []
        self._ai = settings.signalyth_ai_enabled
        self._key = settings.openai_api_key
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "test-key"

    def tearDown(self):
        settings.signalyth_ai_enabled = self._ai
        settings.openai_api_key = self._key
        self.manager.shutdown(wait=True)
        self.tmp.cleanup()

    def _plan(self):
        draft = AnalysisDraft(
            client="ΟΠΑΠ", topic="Eurojackpot", market="Greece",
            date_from=date(2026, 8, 23), date_to=date(2026, 9, 21),
            keywords=["κλήρωση", "τζακ ποτ"],
            sources=["facebook", "instagram"],
            sample_mode="perSource",
            per_source={"facebook": 12, "instagram": 12},
            per_source_comments={"facebook": 40, "instagram": 20},
            source_pages={"facebook": ["facebook.com/brandpage"],
                          "instagram": ["instagram.com/brandpage"]},
            owned_share_pct=60,
            comments=True, max_budget_usd=5.0, smart_search=True,
            report_language="Ελληνικά",
        )
        plan = build_collection_plan(draft).model_dump(mode="json")
        for row in (plan.get("preflight_forecast") or {}).get("sources", []):
            if row.get("source") in {"facebook", "instagram"}:
                row.setdefault("comments", {})["status"] = "verified_available"
                row["comments"]["live_verified"] = True
        return plan

    def test_a_whole_run_produces_a_report_built_on_comments(self):
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id)

        folder = self.store.folder_for(run_id)

        # 1. It must not have died.
        self.assertNotEqual(done.get("status"), "failed",
                            msg=f"fatal_error={done.get('fatal_error')}")

        # 2. Posts were collected.
        self.assertGreater(int(done.get("normalized_total") or 0), 0, "nothing was collected")

        # 3. Comments were actually collected — the whole point.
        comments = []
        for source in ("facebook", "instagram"):
            comments += self.store.read(folder / f"normalized-comments-{source}.json", []) or []
        self.assertTrue(comments, f"no comments at all: {done.get('comment_deepening')}")

        # 4. The operator's pages were used, and they led the layer.
        buckets = ((done.get("adaptive_collection") or {}).get("summary") or {}).get("comment_buckets") or {}
        self.assertTrue(buckets, "the comment layer never ran")
        self.assertTrue(any(int(b.get("owned") or 0) > 0 for b in buckets.values()),
                        f"the operator's pages produced nothing: {buckets}")

        # 5. Comments reached the analysis, not just the disk.
        analysed = self.store.read(folder / "analysis" / "analyzed.json", []) or []
        self.assertTrue(analysed, "the AI analysed nothing")
        self.assertTrue(
            [r for r in analysed if str(r.get("evidence_layer") or "") == "comment"],
            "comments were collected but never analysed",
        )

        # 6. Step 5 produced a real score — the failure the operator kept seeing.
        summary = self.store.read(folder / "intelligence" / "summary.json", None)
        self.assertIsInstance(summary, dict, f"no intelligence summary; fatal={done.get('fatal_error')}")
        self.assertIsNotNone((summary.get("brand_reputation") or {}).get("index"),
                             "no Brand Reputation was computed")

        # 7. The report can state where the evidence came from.
        origin = (self.store.read(folder / "cleaning" / "report.json", {}) or {}).get("sampling_origin") or {}
        self.assertGreater(int(origin.get("owned_pages") or 0), 0, origin)

    def test_the_evidence_survives_the_worker_being_replaced(self):
        """Vercel kills workers mid-run; the evidence must not die with them."""
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id)

        folder = self.store.folder_for(run_id)
        claimed = int(done.get("normalized_total") or 0)
        on_disk = len(self.store.read(folder / "normalized-all.json", []) or [])
        # The workspace holds the posts the status counted PLUS the comments the
        # deepening layer added; it must never hold less than the status claims.
        self.assertGreaterEqual(on_disk, claimed,
                                "the status claims more evidence than the workspace holds")
        candidates = self.store.read(folder / "cleaning" / "semantic-candidates.json", []) or []
        self.assertTrue(candidates, "the cleaned evidence is missing from the workspace")


if __name__ == "__main__":
    unittest.main()


class _CloudWithoutArchive:
    """A mirror that kept the run's settings but lost its evidence.

    This is the production failure, reproduced: the replacement worker gets
    plan + status back and nothing else.
    """

    def __init__(self, plan, status):
        self.plan, self.status = plan, status

    enabled = True
    requested = True

    def restore_run_archive(self, run_id, folder):
        return False

    def restore_run_metadata(self, run_id, folder):
        import json
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        (folder / "status.json").write_text(json.dumps(self.status), encoding="utf-8")
        return True

    def put_json(self, *a, **k):
        return None

    def get_json(self, *a, **k):
        return None

    def get_archive_meta(self, *a, **k):
        return None

    def persist_run_archive(self, *a, **k):
        return None

    def delete_run(self, *a, **k):
        return None

    def list_run_ids(self, *a, **k):
        return []


class EvidenceLossTest(WholePipelineTest):
    """What must happen when the evidence cannot be restored."""

    def test_a_run_stops_instead_of_reporting_on_evidence_it_lost(self):
        import shutil

        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            done = _wait_terminal(self.store, run_id)

        folder = self.store.folder_for(run_id)
        plan = self.store.read(folder / "plan.json", {})
        status = self.store.read(folder / "status.json", {})
        collected = int(status.get("normalized_total") or 0)
        self.assertGreater(collected, 0)

        # The worker is replaced and the archive cannot be restored.
        shutil.rmtree(folder)
        self.store.cloud = _CloudWithoutArchive(plan, status)
        self.manager.store = self.store

        ok, reason = self.store.evidence_intact(run_id)
        self.assertFalse(ok, "losing every record must not read as intact")
        self.assertIn(str(collected), reason)

        blocked = self.manager._evidence_guard(run_id)
        self.assertFalse(blocked, "the pipeline must refuse to continue")
        after = self.store.read_status(run_id)
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["phase"], "evidence_unavailable")
        self.assertIn("missing evidence", after["fatal_error"])

    def test_an_intact_workspace_is_allowed_through(self):
        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            _wait_terminal(self.store, run_id)

        ok, reason = self.store.evidence_intact(run_id)
        self.assertTrue(ok, reason)
        self.assertTrue(self.manager._evidence_guard(run_id))

    def test_a_run_that_has_collected_nothing_yet_is_not_blocked(self):
        """The guard must not stop a run before it has anything to lose."""
        run_id = self.store.create(self._plan())[0]
        ok, _ = self.store.evidence_intact(run_id)
        self.assertTrue(ok)


class _LocalCloud:
    """A durable mirror backed by a folder — behaves like the real one."""

    enabled = True
    requested = True

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.json: dict[str, dict] = {}

    def _zip(self, run_id):
        return self.root / f"{run_id}.zip"

    def persist_run_archive(self, run_id, folder):
        # Written atomically, like the real one: a half-written archive is worse
        # than no archive, because it looks restorable.
        import zipfile
        from datetime import datetime, timezone
        target = self._zip(run_id)
        tmp = target.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(Path(folder).rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=path.relative_to(folder).as_posix())
        tmp.replace(target)
        return datetime.now(timezone.utc).isoformat()

    def restore_run_archive(self, run_id, folder):
        import zipfile
        src = self._zip(run_id)
        if not src.is_file():
            return False
        Path(folder).mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(src) as zf:
                zf.extractall(folder)
        except zipfile.BadZipFile:
            return False
        return True

    def restore_run_metadata(self, run_id, folder):
        return False

    def require(self):
        return None

    def put_json(self, run_id, name, payload):
        self.json[f"{run_id}/{name}"] = payload

    def get_json(self, run_id, name):
        return self.json.get(f"{run_id}/{name}")

    def get_archive_meta(self, run_id):
        return None

    def delete_run(self, run_id):
        self._zip(run_id).unlink(missing_ok=True)

    def list_run_ids(self, limit=100):
        return sorted((p.stem for p in self.root.glob("*.zip")), reverse=True)[:limit]


class WorkerReplacedTest(WholePipelineTest):
    """Vercel replaces the worker constantly. The run has to survive it."""

    def test_the_run_recovers_from_the_archive_after_the_machine_is_wiped(self):
        import shutil

        cloud_dir = Path(self.tmp.name) / "_mirror"
        self.store.cloud = _LocalCloud(cloud_dir)

        run_id = self.store.create(self._plan())[0]
        with patch("app.services.collector.ApifyRunner", FakeApify), \
             patch("app.services.relevance_expansion.ApifyRunner", FakeApify), \
             patch("app.services.ai_analysis.OpenAIResponsesProvider", FakeModel):
            self.manager.enqueue(run_id)
            first = _wait_terminal(self.store, run_id)
        self.assertNotEqual(first.get("status"), "failed", first.get("fatal_error"))

        folder = self.store.folder_for(run_id)
        before_records = len(self.store.read(folder / "normalized-all.json", []) or [])
        before_comments = sum(
            len(self.store.read(folder / f"normalized-comments-{s}.json", []) or [])
            for s in ("facebook", "instagram"))
        self.assertGreater(before_comments, 0, "the first pass collected no comments")

        # The machine that ran it is gone. Everything local disappears —
        # INCLUDING its process. `_wait_terminal` returns the moment the status
        # turns terminal, but the worker thread may still be inside its final
        # checkpoint, writing into this very folder; wiping it under a live
        # writer produced an intermittent FileNotFoundError that failed a
        # different test each run. The process dies first, then the disk.
        self.manager.shutdown(wait=True)
        shutil.rmtree(folder)
        self.assertFalse(folder.exists())

        # A fresh worker must find the evidence again — all of it.
        restored = self.store.folder_for(run_id)
        after_records = len(self.store.read(restored / "normalized-all.json", []) or [])
        after_comments = sum(
            len(self.store.read(restored / f"normalized-comments-{s}.json", []) or [])
            for s in ("facebook", "instagram"))

        self.assertEqual(after_records, before_records,
                         "records were lost when the machine was replaced")
        self.assertEqual(after_comments, before_comments,
                         "comments were lost when the machine was replaced")

        ok, reason = self.store.evidence_intact(run_id)
        self.assertTrue(ok, reason)

        # And the finished analysis is still there, so Step 5 has something to read.
        self.assertTrue((restored / "analysis" / "analysis-ready.json").exists(),
                        "the analysis output did not survive")
        summary = self.store.read(restored / "intelligence" / "summary.json", None)
        self.assertIsInstance(summary, dict, "the report did not survive")


class CachedReportTest(unittest.TestCase):
    """The cached analysis report must never outlive the files it summarises.

    A replacement worker restored a run whose report.json came back while
    analysis-ready.json did not. The short circuit saw a matching hash, returned
    "analysis already done", and Step 5 then found nothing to aggregate — which
    is the error the operator saw three times in one afternoon.
    """

    FULL = dict(
        semantic_relevance="relevant", relevance_score=0.9, relevance_confidence=0.9,
        relevance_reason="r", target_entity="T", target_stance="neutral",
        sentiment_label="neutral", sentiment_score=0.0, sentiment_confidence=0.9,
        primary_emotion="neutral", secondary_emotion="neutral", emotion_intensity=0.3,
        emotion_confidence=0.8, topic="T", narrative="n", sarcasm=False,
        sarcasm_confidence=0.02, language="greek", evidence_quotes=[], overall_confidence=0.9,
    )

    class _Provider:
        calls = 0

        def analyze_batch(self, records, context, tier):
            CachedReportTest._Provider.calls += 1
            return ProviderBatchResult(
                items=[{"record_id": r["record_id"], **CachedReportTest.FULL} for r in records],
                model="m", response_id=None,
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    def setUp(self):
        from uuid import uuid4
        self.store = RunStore()
        self.folder = self.store.root / "runs" / f"20260922T200000Z-{uuid4().hex[:8]}"
        (self.folder / "cleaning").mkdir(parents=True, exist_ok=True)
        (self.folder / "analysis").mkdir(parents=True, exist_ok=True)
        self.store.write(self.folder / "plan.json",
                         {"client": "C", "topic": "T", "market": "Greece", "core_terms": ["T"]})
        self.store.write(self.folder / "cleaning" / "semantic-candidates.json",
                         [{"id": "1", "text": "Το T είναι καλό", "platform": "x",
                           "cleaning": {"confidence": 0.8}}])
        CachedReportTest._Provider.calls = 0

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_healthy_second_pass_reuses_the_cached_report(self):
        from app.services.ai_analysis import analyze_run
        analyze_run(self.folder, provider=self._Provider())
        self.assertTrue((self.folder / "analysis" / "analysis-ready.json").exists())
        before = CachedReportTest._Provider.calls
        analyze_run(self.folder, provider=self._Provider())
        self.assertEqual(CachedReportTest._Provider.calls, before,
                         "an unchanged run must not be re-analysed")

    def test_a_report_whose_files_are_gone_is_rebuilt_not_trusted(self):
        from app.services.ai_analysis import analyze_run
        analyze_run(self.folder, provider=self._Provider())
        (self.folder / "analysis" / "analysis-ready.json").unlink()

        analyze_run(self.folder, provider=self._Provider())

        rebuilt = self.store.read(self.folder / "analysis" / "analysis-ready.json", None)
        self.assertIsInstance(rebuilt, list, "the missing output was not rebuilt")
        self.assertTrue(rebuilt, "the rebuilt output is empty")

    def test_the_same_holds_when_analyzed_json_is_the_one_missing(self):
        from app.services.ai_analysis import analyze_run
        analyze_run(self.folder, provider=self._Provider())
        (self.folder / "analysis" / "analyzed.json").unlink()

        analyze_run(self.folder, provider=self._Provider())

        self.assertTrue((self.folder / "analysis" / "analyzed.json").exists())
