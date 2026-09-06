import json
import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient


class CleaningApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["SIGNALYTH_DATA_DIR"] = cls.tmp.name
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
            "sources": ["x"], "sample_mode": "perSource", "sample_target": 2, "per_source": {"x": 2},
            "comments": False, "max_budget_usd": 5, "smart_search": True,
            "report_language": "Ελληνικά", "additional_context": ["ΟΠΑΠ"], "exclusions": ["KNVB"],
        }

    def normalized(self):
        return [
            {"id":"a","platform":"x","text":"Eurojackpot generic mention","date":"2026-08-15T10:00:00+00:00","author":"u","followers":1,"views":0,"likes":0,"comments":0,"shares":0,"url":"https://x/a","content_type":"post","parent_post":None,"raw_data":{}},
            {"id":"b","platform":"x","text":"Eurojackpot KNVB football final","date":"2026-08-15T11:00:00+00:00","author":"u2","followers":1,"views":0,"likes":0,"comments":0,"shares":0,"url":"https://x/b","content_type":"post","parent_post":None,"raw_data":{}},
        ]

    def make_run_with_data(self):
        created = self.client.post('/api/runs?start=false', json=self.payload()).json()
        folder = self.store.folder_for(created['run_id'])
        self.store.write(folder / 'normalized-all.json', self.normalized())
        return created['run_id']

    def test_health_version_is_v09(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['version'], '1.8.4')

    def test_manual_clean_requires_normalized_data(self):
        created = self.client.post('/api/runs?start=false', json=self.payload()).json()
        r = self.client.post(f"/api/runs/{created['run_id']}/clean")
        self.assertEqual(r.status_code, 409)

    def test_manual_clean_exposes_quality_report(self):
        run_id = self.make_run_with_data()
        r = self.client.post(f'/api/runs/{run_id}/clean')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['total_records'], 2)
        self.assertIn('data_quality_score', r.json())
        g = self.client.get(f'/api/runs/{run_id}/cleaning')
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()['ruleset_version'], '0.8.0')

    def test_review_queue_and_decision_api(self):
        run_id = self.make_run_with_data()
        self.client.post(f'/api/runs/{run_id}/clean')
        q = self.client.get(f'/api/runs/{run_id}/review')
        self.assertEqual(q.status_code, 200)
        items = q.json()['items']
        self.assertTrue(items)
        record_id = items[0]['id']
        r = self.client.post(f'/api/runs/{run_id}/review/{record_id}', json={'action':'keep','note':'verified'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['record']['cleaning']['decision'], 'trusted')
        q2 = self.client.get(f'/api/runs/{run_id}/review').json()['items']
        self.assertNotIn(record_id, [x['id'] for x in q2])

    def test_review_unknown_record_404(self):
        run_id = self.make_run_with_data()
        self.client.post(f'/api/runs/{run_id}/clean')
        r = self.client.post(f'/api/runs/{run_id}/review/nope', json={'action':'exclude'})
        self.assertEqual(r.status_code, 404)

    def test_invalid_review_action_is_rejected(self):
        run_id = self.make_run_with_data()
        self.client.post(f'/api/runs/{run_id}/clean')
        r = self.client.post(f'/api/runs/{run_id}/review/a', json={'action':'maybe'})
        self.assertEqual(r.status_code, 422)


if __name__ == '__main__':
    unittest.main()
