import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class IntelligenceAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        from app.main import app, store, manager
        store.root = Path(cls.tmp.name)
        manager.store = store
        cls.store = store
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def payload(self):
        return {
            "client": "OPAP", "topic": "Eurojackpot", "market": "Greece",
            "date_from": "2026-08-01", "date_to": "2026-08-31", "keywords": ["Eurojackpot"],
            "sources": ["x"], "sample_mode": "perSource", "sample_target": 1, "per_source": {"x": 1},
            "comments": False, "max_budget_usd": 5, "smart_search": True,
            "report_language": "Ελληνικά", "additional_context": ["ΟΠΑΠ"], "exclusions": ["KNVB"],
        }

    def ready(self):
        return [{
            "id": "a", "platform": "x", "text": "Eurojackpot Ελλάδα ΟΠΑΠ", "date": "2026-08-15T10:00:00+00:00",
            "author": "u", "followers": 100, "views": 1000, "likes": 50, "comments": 4, "shares": 3,
            "url": "https://x/a", "content_type": "post", "parent_post": None, "raw_data": {},
            "cleaning": {"decision": "trusted", "confidence": 0.95, "authenticity_score": 100,
                         "origin_class": "earned_person", "content_class": "organic", "account_type": "person_or_creator",
                         "organic_eligible": True, "independent_voice_weight": 1.0},
            "ai_analysis": {"decision": "ready", "semantic_relevance": "relevant", "relevance_confidence": 0.95,
                            "sentiment_label": "positive", "sentiment_score": 0.6, "sentiment_confidence": 0.95,
                            "primary_emotion": "joy", "emotion_intensity": 0.8, "emotion_confidence": 0.95,
                            "target_stance": "supportive", "topic": "Winning", "narrative": "Winning is aspirational",
                            "overall_confidence": 0.95, "opinion_eligible": True}
        }]

    def make_run(self, with_ready=True):
        created = self.client.post('/api/runs?start=false', json=self.payload()).json()
        folder = self.store.folder_for(created['run_id'])
        if with_ready:
            self.store.write(folder / 'analysis' / 'analysis-ready.json', self.ready())
            self.store.write(folder / 'cleaning' / 'report.json', {
                'data_quality_score': 90, 'source_coverage_ratio': 1.0, 'sample_achievement_ratio': 1.0
            })
        return created['run_id'], folder

    def test_health_reports_v10(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['version'], '1.8.4')

    def test_intelligence_requires_step4_ready_evidence(self):
        run_id, _ = self.make_run(with_ready=False)
        r = self.client.post(f'/api/runs/{run_id}/intelligence')
        self.assertEqual(r.status_code, 409)

    def test_build_and_get_intelligence(self):
        run_id, folder = self.make_run()
        r = self.client.post(f'/api/runs/{run_id}/intelligence')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['brand_reputation']['index'], 80.0)
        self.assertTrue((folder / 'intelligence' / 'summary.json').exists())
        g = self.client.get(f'/api/runs/{run_id}/intelligence')
        self.assertEqual(g.status_code, 200)
        self.assertFalse(g.json()['stale'])
        status = self.store.read_status(run_id)
        self.assertEqual(status['intelligence']['status'], 'succeeded')
        self.assertEqual(status['phase'], 'intelligence_ready')

    def test_unknown_run_returns_404(self):
        r = self.client.get('/api/runs/not-a-run/intelligence')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main()
