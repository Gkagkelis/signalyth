import copy
import unittest

from app.config import settings
from app.services.ai_analysis import ProviderBatchResult, analyze_records


class FastSemanticProvider:
    def __init__(self): self.calls=0
    def analyze_batch(self, records, context, tier):
        self.calls += 1
        items=[]
        for r in records:
            text=r['text']
            items.append({
                'record_id':r['record_id'],'semantic_relevance':'relevant','relevance_score':0.96,'relevance_confidence':0.96,
                'relevance_reason':'Direct target mention','target_entity':'Eurojackpot','target_stance':'neutral',
                'sentiment_label':'neutral','sentiment_score':0.0,'sentiment_confidence':0.95,
                'primary_emotion':'neutral','secondary_emotion':'none','emotion_intensity':0.05,'emotion_confidence':0.95,
                'topic':'Lottery discussion','narrative':'Discusses Eurojackpot.','sarcasm':False,'sarcasm_confidence':0.95,
                'language':'greek','evidence_quotes':[text[:24]],'overall_confidence':0.95,
            })
        return ProviderBatchResult(items=items,model='fake-bulk',response_id=f'r{self.calls}',usage={'input_tokens':100,'output_tokens':100,'total_tokens':200})


def make_rows(n):
    rows=[]
    for i in range(n):
        rows.append({
            'id':str(i),'platform':'x','text':f'Eurojackpot Ελλάδα mention {i}','date':'2026-08-15T10:00:00+00:00',
            'author':f'u{i}','followers':10,'views':100,'likes':1,'comments':0,'shares':0,'url':f'https://e/{i}',
            'content_type':'post','parent_post':None,'raw_data':{},
            'cleaning':{'decision':'trusted','relevance_score':0.9,'market_score':0.9,'account_type':'person_or_creator','content_class':'organic','origin_class':'earned_person','organic_eligible':True},
        })
    return rows


def plan():
    return {'client':'OPAP','topic':'Eurojackpot','market':'Greece','date_from':'2026-08-01','date_to':'2026-08-31','core_terms':['Eurojackpot'],'context_terms':['OPAP','Ελλάδα'],'greeklish_variants':['ellada'],'exclusions':['KNVB'],'report_language':'Ελληνικά'}


class AIStressTests(unittest.TestCase):
    def test_3000_record_semantic_pipeline_preserves_every_record_and_partition(self):
        old_budget=settings.signalyth_ai_max_cost_usd
        settings.signalyth_ai_max_cost_usd=10.0
        try:
            rows=make_rows(3000); before=copy.deepcopy(rows); provider=FastSemanticProvider()
            result=analyze_records(rows,plan(),provider,batch_size=25)
            self.assertEqual(rows,before)
            self.assertEqual(len(result['analyzed']),3000)
            self.assertEqual(len(result['analysis_ready']),3000)
            self.assertEqual(len(result['review_queue']),0)
            self.assertEqual(len(result['excluded']),0)
            self.assertEqual(provider.calls,120)
            self.assertEqual({r['id'] for r in result['analyzed']},{str(i) for i in range(3000)})
        finally:
            settings.signalyth_ai_max_cost_usd=old_budget

    def test_3000_record_second_pass_can_be_entirely_cache_backed(self):
        old_budget=settings.signalyth_ai_max_cost_usd
        settings.signalyth_ai_max_cost_usd=10.0
        try:
            rows=make_rows(3000); p1=FastSemanticProvider(); first=analyze_records(rows,plan(),p1,batch_size=50)
            p2=FastSemanticProvider(); second=analyze_records(rows,plan(),p2,batch_size=50,cache=first['cache'])
            self.assertEqual(p2.calls,0)
            self.assertEqual(second['report']['cache_hits'],3000)
            self.assertEqual(len(second['analysis_ready']),3000)
        finally:
            settings.signalyth_ai_max_cost_usd=old_budget

if __name__=='__main__': unittest.main()
