import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["SIGNALYTH_DATA_DIR"] = cls.tmp.name
        from app.main import app, store, manager
        store.root = Path(cls.tmp.name)
        manager.store = store
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def payload(self):
        return {
            "client": "OPAP", "topic": "Eurojackpot", "market": "Greece",
            "date_from": "2026-08-01", "date_to": "2026-08-31",
            "keywords": ["Eurojackpot"], "sources": ["x", "news"],
            "sample_mode": "perSource", "sample_target": 1000,
            "per_source": {"x": 300, "news": 700}, "comments": False,
            "max_budget_usd": 5, "smart_search": True,
            "report_language": "Ελληνικά", "additional_context": ["ΟΠΑΠ"],
            "exclusions": ["KNVB"],
        }

    def test_health(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['version'], '1.8.4')
        self.assertIn('max_parallel_runs', r.json())

    def test_plan_is_non_destructive(self):
        before = len(self.client.get('/api/runs').json())
        r = self.client.post('/api/plan', json=self.payload())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['target_total'], 1000)
        after = len(self.client.get('/api/runs').json())
        self.assertEqual(after, before)

    def test_create_in_dry_run_persists_but_does_not_collect(self):
        r = self.client.post('/api/runs?start=true', json=self.payload())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body['started'])
        self.assertEqual(body['status'], 'planned')
        self.assertEqual(body['reason'], 'dry_run')
        detail = self.client.get('/api/runs/' + body['run_id'])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['status'], 'planned')
        self.assertEqual(detail.json()['topic'], 'Eurojackpot')

    def test_create_with_start_false_is_explicit_plan_only(self):
        r = self.client.post('/api/runs?start=false', json=self.payload())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['started'])
        self.assertEqual(r.json()['reason'], 'start=false')

    def test_cancel_planned_run_is_safe_and_terminal(self):
        created = self.client.post('/api/runs?start=false', json=self.payload()).json()
        r = self.client.post(f"/api/runs/{created['run_id']}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'cancelled')
        self.assertTrue(r.json()['cancel_requested'])
        self.assertEqual(r.json()['progress']['percent'], 100)

    def test_start_is_blocked_in_dry_run(self):
        created = self.client.post('/api/runs?start=false', json=self.payload()).json()
        r = self.client.post(f"/api/runs/{created['run_id']}/start")
        self.assertEqual(r.status_code, 409)
        self.assertIn('DRY_RUN', r.json()['detail'])

    def test_unknown_run_returns_404(self):
        self.assertEqual(self.client.get('/api/runs/not-a-real-run').status_code, 404)
        self.assertEqual(self.client.post('/api/runs/not-a-real-run/cancel').status_code, 404)

    def test_source_registry_can_change_actor_without_code(self):
        current = self.client.get('/api/sources').json()['facebook']
        old = current['actor_id']
        r = self.client.patch('/api/sources/facebook', json={'actor_id': 'example/facebook-replacement', 'locked': False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['actor_id'], 'example/facebook-replacement')
        self.assertFalse(r.json()['locked'])
        self.client.patch('/api/sources/facebook', json={'actor_id': old, 'locked': current['locked']})

    def test_locked_actor_cannot_change_by_accident(self):
        current = self.client.get('/api/sources').json()['x']
        self.assertTrue(current['locked'])
        r = self.client.patch('/api/sources/x', json={'actor_id': 'accidental/replacement'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.get('/api/sources').json()['x']['actor_id'], current['actor_id'])

    def test_bad_date_rejected(self):
        p = self.payload(); p['date_from'] = '2026-09-01'; p['date_to'] = '2026-08-31'
        r = self.client.post('/api/plan', json=p)
        self.assertEqual(r.status_code, 422)


if __name__ == '__main__':
    unittest.main()
