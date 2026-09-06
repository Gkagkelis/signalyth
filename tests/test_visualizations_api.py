import copy
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient


class VisualizationsAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        from app.main import app, store, manager
        store.root=Path(cls.tmp.name); manager.store=store
        cls.store=store; cls.client=TestClient(app)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def payload(self):
        return {"client":"OPAP","topic":"Eurojackpot","market":"Greece","date_from":"2026-08-01","date_to":"2026-08-31","keywords":["Eurojackpot"],"sources":["x"],"sample_mode":"perSource","sample_target":5,"per_source":{"x":5},"comments":False,"max_budget_usd":5,"smart_search":True,"report_language":"Ελληνικά","additional_context":["ΟΠΑΠ"],"exclusions":["KNVB"]}

    def ready(self):
        rows=[]
        cases=[
            (0.8,'positive','joy','Winning dream',1,1000),
            (0.6,'positive','joy','Winning dream',2,2000),
            (-0.8,'negative','anger','Trust concern',3,5000),
            (-0.7,'negative','anger','Trust concern',4,7000),
            (0.1,'neutral','neutral','General information',5,800),
        ]
        for i,(sent,label,emo,narr,day,views) in enumerate(cases,1):
            rows.append({
                "id":str(i),"platform":"x","text":f"Eurojackpot {narr}","date":f"2026-08-{day:02d}T10:00:00+00:00","author":f"u{i}","followers":100*i,"views":views,"likes":20*i,"comments":2*i,"shares":i,"url":f"https://x/{i}","content_type":"post","parent_post":None,"raw_data":{"views":views},
                "cleaning":{"decision":"trusted","confidence":0.95,"authenticity_score":100,"market_score":0.95,"origin_class":"earned_person","content_class":"organic","account_type":"person_or_creator","organic_eligible":True,"independent_voice_weight":1.0,"story_cluster_id":None,"coordination_cluster_id":None},
                "ai_analysis":{"decision":"ready","semantic_relevance":"relevant","relevance_confidence":0.95,"sentiment_label":label,"sentiment_score":sent,"sentiment_confidence":0.95,"primary_emotion":emo,"emotion_intensity":0.8,"emotion_confidence":0.95,"target_stance":"supportive" if sent>0.2 else ("critical" if sent<-.2 else "neutral"),"topic":"Lottery experience","narrative":narr,"overall_confidence":0.95,"opinion_eligible":True}
            })
        return rows

    def make_run(self, through_step6=True):
        run_id=self.client.post('/api/runs?start=false',json=self.payload()).json()['run_id']
        folder=self.store.folder_for(run_id)
        if through_step6:
            self.store.write(folder/'analysis'/'analysis-ready.json',self.ready())
            self.store.write(folder/'cleaning'/'report.json',{'data_quality_score':90,'data_quality_label':'high','source_coverage_ratio':1.0,'sample_achievement_ratio':1.0,'trusted_records':5,'review_records':0,'excluded_records':0,'total_records':5})
            self.assertEqual(self.client.post(f'/api/runs/{run_id}/intelligence').status_code,200)
            self.assertEqual(self.client.post(f'/api/runs/{run_id}/investigations').status_code,200)
        return run_id,folder

    def test_health_reports_v12(self):
        self.assertEqual(self.client.get('/api/health').json()['version'],'1.8.4')

    def test_step7_requires_step6(self):
        run_id,_=self.make_run(False)
        r=self.client.post(f'/api/runs/{run_id}/visualizations')
        self.assertEqual(r.status_code,409)

    def test_build_persists_complete_visual_state(self):
        run_id,folder=self.make_run()
        r=self.client.post(f'/api/runs/{run_id}/visualizations')
        self.assertEqual(r.status_code,200)
        body=r.json(); self.assertEqual(body['indicator_contract']['required'],25); self.assertTrue(body['indicator_contract']['complete'])
        self.assertTrue(body['native_editable_ready'])
        for name in ('summary.json','chart-specs.json','indicator-review.json','dashboard.json','presentation-visual-pack.json','audit.json','methodology.json'):
            self.assertTrue((folder/'visualizations'/name).exists(),name)
        st=self.store.read_status(run_id)
        self.assertEqual(st['visualizations']['status'],'succeeded')
        self.assertEqual(st['phase'],'visualizations_ready')

    def test_dashboard_endpoint_returns_real_chart_specs(self):
        run_id,_=self.make_run(); self.assertEqual(self.client.post(f'/api/runs/{run_id}/visualizations').status_code,200)
        r=self.client.get(f'/api/runs/{run_id}/dashboard'); self.assertEqual(r.status_code,200)
        body=r.json(); self.assertTrue(body['charts']); self.assertTrue(body['dashboard']['sections'])
        ids={x['chart_id'] for x in body['charts']}; self.assertIn('brand_reputation',ids); self.assertIn('sentiment_distribution',ids)

    def test_presentation_visual_pack_requires_native_editable_charts(self):
        run_id,_=self.make_run(); self.client.post(f'/api/runs/{run_id}/visualizations')
        r=self.client.get(f'/api/runs/{run_id}/presentation-visual-pack'); self.assertEqual(r.status_code,200)
        p=r.json(); self.assertTrue(p['guardrails']['native_editable_charts_required']); self.assertTrue(p['guardrails']['must_review_all_indicators'])
        self.assertEqual(len(p['indicator_review']),25)

    def test_rebuilding_step6_marks_step7_stale(self):
        run_id,folder=self.make_run(); self.client.post(f'/api/runs/{run_id}/visualizations')
        pack=self.store.read(folder/'investigations'/'evidence-pack.json'); pack['headline_metrics']['step5_warnings'].append('changed')
        self.store.write(folder/'investigations'/'evidence-pack.json',pack)
        g=self.client.get(f'/api/runs/{run_id}/visualizations'); self.assertTrue(g.json()['stale'])
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/dashboard').status_code,409)
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/presentation-visual-pack').status_code,409)

    def test_force_rebuild_refreshes_stale_visual_pack(self):
        run_id,folder=self.make_run(); first=self.client.post(f'/api/runs/{run_id}/visualizations').json()
        pack=self.store.read(folder/'investigations'/'evidence-pack.json'); pack['headline_metrics']['step5_warnings'].append('changed'); self.store.write(folder/'investigations'/'evidence-pack.json',pack)
        second=self.client.post(f'/api/runs/{run_id}/visualizations?force=true')
        self.assertEqual(second.status_code,200); self.assertNotEqual(first['input_hash'],second.json()['input_hash']); self.assertFalse(self.client.get(f'/api/runs/{run_id}/visualizations').json()['stale'])

    def test_step7_research_frame_mismatch_fails_safely(self):
        run_id,folder=self.make_run(); plan=self.store.read(folder/'plan.json'); plan['market']='Italy'; self.store.write(folder/'plan.json',plan)
        r=self.client.post(f'/api/runs/{run_id}/visualizations')
        self.assertEqual(r.status_code,409); self.assertIn('research context mismatch',r.json()['detail'])
        self.assertTrue((folder/'investigations'/'evidence-pack.json').exists())

    def test_unknown_run_returns_404(self):
        for suffix in ('visualizations','dashboard','presentation-visual-pack'):
            self.assertEqual(self.client.get('/api/runs/not-a-run/'+suffix).status_code,404)

    def test_duplicate_chart_build_is_idempotent_without_force(self):
        run_id,_=self.make_run(); a=self.client.post(f'/api/runs/{run_id}/visualizations').json(); b=self.client.post(f'/api/runs/{run_id}/visualizations').json()
        self.assertEqual(a['input_hash'],b['input_hash']); self.assertEqual(a['generated_at'],b['generated_at'])


if __name__=='__main__': unittest.main()
