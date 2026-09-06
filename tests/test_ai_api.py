import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.services.ai_analysis import ProviderBatchResult, analyze_run


class TinyProvider:
    def analyze_batch(self, records, context, tier):
        items=[]
        for r in records:
            text=r.get('text') or ''
            items.append({
                'record_id':r['record_id'],'semantic_relevance':'relevant','relevance_score':0.95,'relevance_confidence':0.95,
                'relevance_reason':'Relevant to target','target_entity':'Eurojackpot','target_stance':'neutral',
                'sentiment_label':'neutral','sentiment_score':0.0,'sentiment_confidence':0.95,
                'primary_emotion':'neutral','secondary_emotion':'none','emotion_intensity':0.1,'emotion_confidence':0.95,
                'topic':'Lottery discussion','narrative':'Discusses Eurojackpot.','sarcasm':False,'sarcasm_confidence':0.95,
                'language':'greek','evidence_quotes':[text[:20]] if text else [],'overall_confidence':0.95,
            })
        return ProviderBatchResult(items=items,model='fake',response_id='resp-api',usage={'input_tokens':10,'output_tokens':10,'total_tokens':20})


class AIAPItests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        from app.main import app, store, manager
        store.root=Path(cls.tmp.name)
        manager.store=store
        cls.store=store
        cls.client=TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.old_enabled=settings.signalyth_ai_enabled
        self.old_key=settings.openai_api_key
        settings.signalyth_ai_enabled=False
        settings.openai_api_key=''

    def tearDown(self):
        settings.signalyth_ai_enabled=self.old_enabled
        settings.openai_api_key=self.old_key

    def payload(self):
        return {
            'client':'OPAP','topic':'Eurojackpot','market':'Greece','date_from':'2026-08-01','date_to':'2026-08-31',
            'keywords':['Eurojackpot'],'sources':['x'],'sample_mode':'perSource','sample_target':1,'per_source':{'x':1},
            'comments':False,'max_budget_usd':5,'smart_search':True,'report_language':'Ελληνικά',
            'additional_context':['ΟΠΑΠ'],'exclusions':['KNVB'],
        }

    def trusted(self):
        return [{
            'id':'a','platform':'x','text':'Eurojackpot Ελλάδα ΟΠΑΠ','date':'2026-08-15T10:00:00+00:00','author':'u',
            'followers':10,'views':100,'likes':5,'comments':1,'shares':0,'url':'https://x/a','content_type':'post','parent_post':None,
            'raw_data':{},'cleaning':{'decision':'trusted','relevance_score':0.9,'market_score':0.9,'account_type':'person_or_creator',
            'content_class':'organic','origin_class':'earned_person','organic_eligible':True}
        }]

    def make_cleaned_run(self):
        created=self.client.post('/api/runs?start=false',json=self.payload()).json()
        folder=self.store.folder_for(created['run_id'])
        self.store.write(folder/'cleaning'/'trusted.json',self.trusted())
        return created['run_id'],folder

    def test_health_exposes_v09_ai_connection_state(self):
        r=self.client.get('/api/health')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['version'],'1.8.4')
        self.assertIn('openai_connected',r.json())
        self.assertIn('ai_enabled',r.json())

    def test_analyze_requires_step3_trusted_sample(self):
        created=self.client.post('/api/runs?start=false',json=self.payload()).json()
        settings.signalyth_ai_enabled=True
        settings.openai_api_key='fake'
        r=self.client.post(f"/api/runs/{created['run_id']}/analyze")
        self.assertEqual(r.status_code,409)

    def test_analyze_is_blocked_when_ai_disabled(self):
        run_id,_=self.make_cleaned_run()
        r=self.client.post(f'/api/runs/{run_id}/analyze')
        self.assertEqual(r.status_code,409)
        self.assertIn('disabled',r.json()['detail'].lower())

    def test_analyze_is_blocked_when_openai_key_missing(self):
        run_id,_=self.make_cleaned_run()
        settings.signalyth_ai_enabled=True
        r=self.client.post(f'/api/runs/{run_id}/analyze')
        self.assertEqual(r.status_code,409)
        self.assertIn('openai',r.json()['detail'].lower())

    def test_analyze_endpoint_updates_status_without_real_network(self):
        run_id,_=self.make_cleaned_run()
        settings.signalyth_ai_enabled=True
        settings.openai_api_key='fake'
        fake_report={'ruleset_version':'0.9.0','prompt_version':'signalyth-semantic-v0.9','generated_at':'2026-09-03T12:00:00+00:00','analysis_ready_records':1,'review_records':0}
        with patch('app.main.analyze_run',return_value=fake_report) as mocked:
            r=self.client.post(f'/api/runs/{run_id}/analyze')
        self.assertEqual(r.status_code,200)
        mocked.assert_called_once()
        status=self.store.read_status(run_id)
        self.assertEqual(status['analysis']['status'],'succeeded')
        self.assertEqual(status['phase'],'analyzed')

    def test_get_analysis_and_review_api_use_persisted_real_layers(self):
        run_id,folder=self.make_cleaned_run()
        analyze_run(folder,plan=self.store.read_plan(run_id),provider=TinyProvider())
        r=self.client.get(f'/api/runs/{run_id}/analysis')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['analysis_ready_records'],1)
        q=self.client.get(f'/api/runs/{run_id}/analysis/review')
        self.assertEqual(q.status_code,200)
        self.assertEqual(q.json()['items'],[])

    def test_ai_review_unknown_record_returns_404(self):
        run_id,folder=self.make_cleaned_run()
        analyze_run(folder,plan=self.store.read_plan(run_id),provider=TinyProvider())
        r=self.client.post(f'/api/runs/{run_id}/analysis/review/nope',json={'action':'exclude'})
        self.assertEqual(r.status_code,404)

    def test_ai_review_validates_human_sentiment_consistency(self):
        run_id,folder=self.make_cleaned_run()
        analyze_run(folder,plan=self.store.read_plan(run_id),provider=TinyProvider())
        r=self.client.post(f'/api/runs/{run_id}/analysis/review/a',json={'action':'keep','sentiment_label':'positive','sentiment_score':-0.9})
        self.assertEqual(r.status_code,422)


if __name__=='__main__':
    unittest.main()
