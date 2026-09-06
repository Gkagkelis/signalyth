import json
import math
import random
import unittest

from app.services.intelligence import compute_intelligence
from app.services.investigations import compute_investigations, INDICATOR_REGISTRY


def plan(target):
    return {
        'client':'Stress','topic':'Eurojackpot','market':'Greece',
        'date_from':'2026-08-01','date_to':'2026-08-31','target_total':target,
        'sources':[{'source':'x','target_items':target//3},{'source':'tiktok','target_items':target//3},{'source':'news','target_items':target//3}],
    }


def quality(target, score=90, coverage=1.0):
    return {'data_quality_score':score,'source_coverage_ratio':coverage,'sample_achievement_ratio':1.0,'trusted_records':target,'review_records':0,'excluded_records':0}


def make_row(i, rng):
    day=(i%31)+1
    platform=rng.choice(['x','tiktok','news'])
    is_media=platform=='news'
    sentiment=rng.uniform(-1,1)
    label='positive' if sentiment>0.2 else ('negative' if sentiment<-0.2 else 'neutral')
    emotion=rng.choice(['joy','anger','sadness','fear','disgust','surprise','neutral'])
    narrative=rng.choice(['Winning dream','Price concern','Trust concern','Lifestyle fantasy','Routine'])
    topic=rng.choice(['Trust','Price','Winnings','Lifestyle','Draw'])
    coord='c-'+str(i%7) if i%401==0 else None
    story='s-'+str(i%17) if is_media and i%37==0 else None
    impact=max(1,int(rng.lognormvariate(5,1.2)))
    return {
        'id':str(i),'platform':platform,'text':f'{narrative} evidence {i}','date':f'2026-08-{day:02d}T10:00:00+00:00','author':f'a-{i%400}',
        'followers':impact,'views':impact*10,'likes':impact//10,'comments':impact//50,'shares':impact//80,'url':f'https://e/{i}','content_type':'post','parent_post':None,'raw_data':{'views':impact*10},
        'cleaning':{'decision':'trusted','confidence':0.9,'authenticity_score':45 if coord else 95,'origin_class':'earned_media' if is_media else 'earned_person','content_class':'news' if is_media else 'organic','account_type':'media' if is_media else 'person_or_creator','organic_eligible':not is_media,'independent_voice_weight':0.2 if story else 1.0,'story_cluster_id':story,'coordination_cluster_id':coord},
        'ai_analysis':{'decision':'ready','semantic_relevance':'relevant','relevance_confidence':0.9,'sentiment_label':label,'sentiment_score':sentiment,'sentiment_confidence':0.9,'primary_emotion':emotion,'emotion_intensity':0.6,'emotion_confidence':0.9,'target_stance':'not_applicable' if is_media else ('supportive' if sentiment>0.2 else ('critical' if sentiment<-0.2 else 'neutral')),'topic':topic,'narrative':narrative,'overall_confidence':0.9,'opinion_eligible':not is_media},
    }


def assert_finite(testcase, obj):
    if isinstance(obj, dict):
        for v in obj.values(): assert_finite(testcase, v)
    elif isinstance(obj, list):
        for v in obj: assert_finite(testcase, v)
    elif isinstance(obj, float):
        testcase.assertTrue(math.isfinite(obj))


class InvestigationStressTests(unittest.TestCase):
    def test_5000_record_investigation_pack_is_finite_and_complete(self):
        rng=random.Random(1106)
        rows=[make_row(i,rng) for i in range(1,5001)]
        intel=compute_intelligence(rows,plan(5000),quality(5000))
        result=compute_investigations(intel['records'],intel['summary'],intel['time_series'],plan(5000),quality(5000))
        self.assertEqual(result['summary']['indicator_contract']['required'],len(INDICATOR_REGISTRY))
        self.assertTrue(result['summary']['indicator_contract']['complete'])
        self.assertEqual(len(result['indicator_inventory']),25)
        assert_finite(self,result)
        # Must be JSON serializable with no NaN/Infinity shortcuts.
        json.dumps(result,allow_nan=False)

    def test_25_randomized_datasets_keep_guardrails_and_bounds(self):
        for seed in range(25):
            rng=random.Random(seed)
            n=rng.randint(30,250)
            rows=[make_row(i,rng) for i in range(1,n+1)]
            q=quality(n,score=rng.randint(45,100),coverage=rng.uniform(.4,1.0))
            intel=compute_intelligence(rows,plan(n),q)
            result=compute_investigations(intel['records'],intel['summary'],intel['time_series'],plan(n),q)
            self.assertTrue(result['summary']['indicator_contract']['complete'])
            self.assertEqual(result['summary']['indicator_contract']['omitted'],0)
            for inv in result['investigations']:
                self.assertEqual(inv['causal_status'],'not_proven')
                self.assertGreaterEqual(inv['confidence']['score'],0)
                self.assertLessEqual(inv['confidence']['score'],100)
                self.assertGreaterEqual(inv['priority_score'],0)
                self.assertLessEqual(inv['priority_score'],100)
            json.dumps(result,allow_nan=False)

    def test_extreme_public_metrics_do_not_break_investigation_pack(self):
        rng=random.Random(7)
        rows=[make_row(i,rng) for i in range(1,80)]
        rows[0]['views']=10**18; rows[0]['likes']=10**17; rows[0]['followers']=10**16
        intel=compute_intelligence(rows,plan(len(rows)),quality(len(rows)))
        result=compute_investigations(intel['records'],intel['summary'],intel['time_series'],plan(len(rows)),quality(len(rows)))
        assert_finite(self,result)
        json.dumps(result,allow_nan=False)


if __name__=='__main__':
    unittest.main()
