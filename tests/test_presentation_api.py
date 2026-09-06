import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

class PresentationAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        from app.main import app, store, manager
        store.root=Path(cls.tmp.name); manager.store=store
        cls.store=store; cls.client=TestClient(app)
        cls.run_id, cls.folder = cls.make_ready_run_static()
        r=cls.client.post(f'/api/runs/{cls.run_id}/visualizations')
        assert r.status_code==200, r.text
        er=cls.client.post(f'/api/runs/{cls.run_id}/exports')
        assert er.status_code==200, er.text
        cls.export_summary=er.json()

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    @classmethod
    def payload(cls):
        return {"client":"OPAP","topic":"Eurojackpot","market":"Greece","date_from":"2026-08-01","date_to":"2026-08-31","keywords":["Eurojackpot"],"sources":["x"],"sample_mode":"perSource","sample_target":5,"per_source":{"x":5},"comments":False,"max_budget_usd":5,"smart_search":True,"report_language":"Ελληνικά","additional_context":["ΟΠΑΠ"],"exclusions":["KNVB"]}

    @classmethod
    def ready(cls):
        rows=[]
        cases=[(0.8,'positive','joy','Winning dream',1,1000),(0.6,'positive','joy','Winning dream',2,2000),(-0.8,'negative','anger','Trust concern',3,5000),(-0.7,'negative','anger','Trust concern',4,7000),(0.1,'neutral','neutral','General information',5,800)]
        for i,(sent,label,emo,narr,day,views) in enumerate(cases,1):
            rows.append({"id":str(i),"platform":"x","text":f"Eurojackpot {narr}","date":f"2026-08-{day:02d}T10:00:00+00:00","author":f"u{i}","followers":100*i,"views":views,"likes":20*i,"comments":2*i,"shares":i,"url":f"https://x/{i}","content_type":"post","parent_post":None,"raw_data":{"views":views},"cleaning":{"decision":"trusted","confidence":0.95,"authenticity_score":100,"market_score":0.95,"origin_class":"earned_person","content_class":"organic","account_type":"person_or_creator","organic_eligible":True,"independent_voice_weight":1.0,"story_cluster_id":None,"coordination_cluster_id":None},"ai_analysis":{"decision":"ready","semantic_relevance":"relevant","relevance_confidence":0.95,"sentiment_label":label,"sentiment_score":sent,"sentiment_confidence":0.95,"primary_emotion":emo,"emotion_intensity":0.8,"emotion_confidence":0.95,"target_stance":"supportive" if sent>0.2 else ("critical" if sent<-.2 else "neutral"),"topic":"Lottery experience","narrative":narr,"overall_confidence":0.95,"opinion_eligible":True}})
        return rows

    @classmethod
    def make_ready_run_static(cls, through_step6=True):
        run_id=cls.client.post('/api/runs?start=false',json=cls.payload()).json()['run_id']
        folder=cls.store.folder_for(run_id)
        if through_step6:
            cls.store.write(folder/'analysis'/'analysis-ready.json',cls.ready())
            cls.store.write(folder/'cleaning'/'report.json',{'data_quality_score':90,'data_quality_label':'high','source_coverage_ratio':1.0,'sample_achievement_ratio':1.0,'trusted_records':5,'review_records':0,'excluded_records':0,'total_records':5})
            assert cls.client.post(f'/api/runs/{run_id}/intelligence').status_code==200
            assert cls.client.post(f'/api/runs/{run_id}/investigations').status_code==200
        return run_id,folder

    def test_health_reports_v13(self):
        self.assertEqual(self.client.get('/api/health').json()['version'],'1.8.4')

    def test_step8_requires_step7(self):
        run_id,_=self.make_ready_run_static(through_step6=True)
        r=self.client.post(f'/api/runs/{run_id}/exports')
        self.assertEqual(r.status_code,409)

    def test_build_exports_endpoint_persists_status(self):
        st=self.store.read_status(self.run_id)
        self.assertEqual(st['exports']['status'],'succeeded')
        self.assertEqual(st['phase'],'exports_ready')
        self.assertEqual(st['exports']['summary']['indicator_contract']['examined'],25)

    def test_get_exports_returns_summary_and_manifest(self):
        r=self.client.get(f'/api/runs/{self.run_id}/exports')
        self.assertEqual(r.status_code,200);body=r.json();self.assertIn('summary',body);self.assertIn('manifest',body);self.assertGreaterEqual(len(body['manifest']['files']),4)

    def test_export_bundle_contains_required_file_types(self):
        files=self.client.get(f'/api/runs/{self.run_id}/exports').json()['manifest']['files']
        types={x['type'] for x in files};self.assertTrue({'pptx','docx','json','csv'} <= types)

    def test_download_authorized_export_file(self):
        files=self.client.get(f'/api/runs/{self.run_id}/exports').json()['manifest']['files'];name=next(x['name'] for x in files if x['type']=='pptx')
        r=self.client.get(f'/api/runs/{self.run_id}/exports/{name}')
        self.assertEqual(r.status_code,200);self.assertGreater(len(r.content),1000)

    def test_path_traversal_export_download_blocked(self):
        self.assertEqual(self.client.get(f'/api/runs/{self.run_id}/exports/..%2Fplan.json').status_code,404)

    def test_unknown_run_export_returns_404(self):
        self.assertEqual(self.client.get('/api/runs/not-a-run/exports').status_code,404)

    def test_step7_change_makes_exports_stale(self):
        folder=self.folder
        pack=self.store.read(folder/'visualizations'/'presentation-visual-pack.json');pack['warnings'].append('changed after exports')
        self.store.write(folder/'visualizations'/'presentation-visual-pack.json',pack)
        r=self.client.get(f'/api/runs/{self.run_id}/exports')
        self.assertEqual(r.status_code,200);self.assertTrue(r.json()['summary']['stale'])
        files=r.json()['manifest']['files'];name=files[0]['name']
        self.assertEqual(self.client.get(f'/api/runs/{self.run_id}/exports/{name}').status_code,409)

if __name__=='__main__': unittest.main()
