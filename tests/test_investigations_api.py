import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class InvestigationsAPITests(unittest.TestCase):
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
            "sources": ["x"], "sample_mode": "perSource", "sample_target": 3, "per_source": {"x": 3},
            "comments": False, "max_budget_usd": 5, "smart_search": True,
            "report_language": "Ελληνικά", "additional_context": ["ΟΠΑΠ"], "exclusions": ["KNVB"],
        }

    def ready(self):
        out=[]
        for i,(sent,label,emo,narr,day) in enumerate([
            (0.7,'positive','joy','Winning dream',5),
            (-0.8,'negative','anger','Price concern',20),
            (-0.9,'negative','anger','Price concern',21),
        ],1):
            out.append({
                "id": str(i), "platform": "x", "text": f"Eurojackpot {narr}", "date": f"2026-08-{day:02d}T10:00:00+00:00",
                "author": f"u{i}", "followers": 100*i, "views": 1000*i, "likes": 20*i, "comments": 2*i, "shares": i,
                "url": f"https://x/{i}", "content_type": "post", "parent_post": None, "raw_data": {"views":1000*i},
                "cleaning": {"decision":"trusted", "confidence":0.95, "authenticity_score":100, "origin_class":"earned_person",
                             "content_class":"organic", "account_type":"person_or_creator", "organic_eligible":True,
                             "independent_voice_weight":1.0, "story_cluster_id":None, "coordination_cluster_id":None},
                "ai_analysis": {"decision":"ready", "semantic_relevance":"relevant", "relevance_confidence":0.95,
                                "sentiment_label":label, "sentiment_score":sent, "sentiment_confidence":0.95,
                                "primary_emotion":emo, "emotion_intensity":0.8, "emotion_confidence":0.95,
                                "target_stance":"supportive" if sent>0 else "critical", "topic":"Lottery experience", "narrative":narr,
                                "overall_confidence":0.95, "opinion_eligible":True}
            })
        return out

    def make_run(self, with_intelligence=True):
        run_id = self.client.post('/api/runs?start=false', json=self.payload()).json()['run_id']
        folder = self.store.folder_for(run_id)
        if with_intelligence:
            self.store.write(folder/'analysis'/'analysis-ready.json', self.ready())
            self.store.write(folder/'cleaning'/'report.json', {'data_quality_score':90,'source_coverage_ratio':1.0,'sample_achievement_ratio':1.0,'trusted_records':3,'review_records':0,'excluded_records':0})
            r=self.client.post(f'/api/runs/{run_id}/intelligence')
            self.assertEqual(r.status_code,200)
        return run_id,folder

    def test_health_reports_v11(self):
        r=self.client.get('/api/health')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['version'],'1.8.4')

    def test_investigations_require_step5(self):
        run_id,_=self.make_run(with_intelligence=False)
        r=self.client.post(f'/api/runs/{run_id}/investigations')
        self.assertEqual(r.status_code,409)

    def test_build_get_and_evidence_pack(self):
        run_id,folder=self.make_run()
        r=self.client.post(f'/api/runs/{run_id}/investigations')
        self.assertEqual(r.status_code,200)
        body=r.json()
        self.assertEqual(body['indicator_contract']['required'],25)
        self.assertTrue(body['indicator_contract']['complete'])
        self.assertTrue((folder/'investigations'/'evidence-pack.json').exists())
        g=self.client.get(f'/api/runs/{run_id}/investigations')
        self.assertEqual(g.status_code,200)
        self.assertFalse(g.json()['stale'])
        p=self.client.get(f'/api/runs/{run_id}/evidence-pack')
        self.assertEqual(p.status_code,200)
        self.assertTrue(p.json()['presentation_guardrails']['must_review_all_indicators'])
        status=self.store.read_status(run_id)
        self.assertEqual(status['investigations']['status'],'succeeded')
        self.assertEqual(status['phase'],'investigations_ready')

    def test_rebuilding_step5_marks_step6_stale(self):
        run_id,folder=self.make_run()
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/investigations').status_code,200)
        ready=self.ready()
        ready[0]['ai_analysis']['sentiment_score']=-0.2
        ready[0]['ai_analysis']['sentiment_label']='negative'
        self.store.write(folder/'analysis'/'analysis-ready.json',ready)
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/intelligence').status_code,200)
        status=self.store.read_status(run_id)
        self.assertEqual(status['investigations']['status'],'stale')
        get=self.client.get(f'/api/runs/{run_id}/investigations')
        self.assertTrue(get.json()['stale'])
        pack=self.client.get(f'/api/runs/{run_id}/evidence-pack')
        self.assertEqual(pack.status_code,409)

    def test_stale_step5_blocks_step6(self):
        run_id,folder=self.make_run()
        ready=self.ready(); ready.append(copy.deepcopy(ready[0])); ready[-1]['id']='extra'
        self.store.write(folder/'analysis'/'analysis-ready.json',ready)
        r=self.client.post(f'/api/runs/{run_id}/investigations')
        self.assertEqual(r.status_code,409)

    def test_step6_failure_preserves_step5_files(self):
        run_id,folder=self.make_run()
        rows=self.store.read(folder/'intelligence'/'records.json')
        rows.append(copy.deepcopy(rows[0]))
        self.store.write(folder/'intelligence'/'records.json',rows)
        r=self.client.post(f'/api/runs/{run_id}/investigations')
        self.assertEqual(r.status_code,500)
        self.assertTrue((folder/'intelligence'/'summary.json').exists())
        self.assertTrue((folder/'intelligence'/'records.json').exists())
        self.assertEqual(self.store.read_status(run_id)['investigations']['status'],'failed')

    def test_unknown_run_returns_404(self):
        self.assertEqual(self.client.get('/api/runs/not-a-run/investigations').status_code,404)
        self.assertEqual(self.client.get('/api/runs/not-a-run/evidence-pack').status_code,404)


if __name__=='__main__':
    unittest.main()
