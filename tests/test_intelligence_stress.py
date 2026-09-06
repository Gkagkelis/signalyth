import math
import random
import unittest

from app.services.intelligence import compute_intelligence


def make_row(i, rng):
    sentiment = rng.uniform(-1, 1)
    label = 'positive' if sentiment > .15 else ('negative' if sentiment < -.15 else 'neutral')
    stance = 'supportive' if sentiment > .15 else ('critical' if sentiment < -.15 else 'neutral')
    origin = rng.choice(['earned_person', 'earned_media', 'owned', 'earned_organization'])
    content = {'earned_person':'organic','earned_media':'news','owned':'owned','earned_organization':'organic'}[origin]
    opinion = origin == 'earned_person'
    if origin == 'earned_media' and rng.random() < .65:
        stance = 'not_applicable'
    return {
        'id': str(i), 'platform': rng.choice(['x','tiktok','instagram','facebook','youtube','news']), 'text': f'text {i}',
        'date': f'2026-08-{1 + i % 28:02d}T10:00:00+00:00', 'author': f'a{i%250}',
        'followers': rng.randint(0, 100000), 'views': rng.randint(0, 1000000), 'likes': rng.randint(0, 10000),
        'comments': rng.randint(0, 1000), 'shares': rng.randint(0, 1000), 'url': f'https://e/{i}', 'raw_data': {},
        'cleaning': {'decision':'trusted','confidence':rng.uniform(.6,1),'authenticity_score':rng.randint(70,100),
                     'origin_class':origin,'content_class':content,'account_type':'person_or_creator' if origin=='earned_person' else 'media' if origin=='earned_media' else 'brand_owned' if origin=='owned' else 'organization',
                     'organic_eligible':opinion,'independent_voice_weight':rng.choice([1,1,1,.5,.25]),'story_cluster_id':None,'coordination_cluster_id':None},
        'ai_analysis': {'decision':'ready','semantic_relevance':'relevant','relevance_confidence':rng.uniform(.6,1),
                        'sentiment_label':label,'sentiment_score':sentiment,'sentiment_confidence':rng.uniform(.6,1),
                        'primary_emotion':rng.choice(['joy','anger','sadness','fear','disgust','surprise','neutral']),
                        'emotion_intensity':rng.random(),'emotion_confidence':rng.uniform(.6,1),'target_stance':stance,
                        'topic':rng.choice(['A','B','C','D']),'narrative':rng.choice(['N1','N2','N3','N4','N5']),
                        'overall_confidence':rng.uniform(.6,1),'opinion_eligible':opinion}
    }


def plan(n):
    return {'client':'c','topic':'t','market':'Greece','date_from':'2026-08-01','date_to':'2026-08-31','target_total':n,
            'sources':[{'source':s,'target_items':max(1,n//6)} for s in ['x','tiktok','instagram','facebook','youtube','news']]}

QUALITY={'data_quality_score':85,'source_coverage_ratio':1.0,'sample_achievement_ratio':1.0}


class IntelligenceStressTests(unittest.TestCase):
    def test_5000_record_pipeline_stays_finite_and_partitioned(self):
        rng=random.Random(12345)
        rows=[make_row(i,rng) for i in range(5000)]
        result=compute_intelligence(rows,plan(len(rows)),QUALITY)
        self.assertEqual(len(result['records']),5000)
        self.assertGreaterEqual(result['summary']['brand_reputation']['index'],0)
        self.assertLessEqual(result['summary']['brand_reputation']['index'],100)
        stack=[result]
        while stack:
            v=stack.pop()
            if isinstance(v,dict): stack.extend(v.values())
            elif isinstance(v,list): stack.extend(v)
            elif isinstance(v,float): self.assertTrue(math.isfinite(v))

    def test_25_randomized_datasets_keep_reputation_and_confidence_in_range(self):
        for seed in range(25):
            rng=random.Random(seed)
            n=rng.randint(25,500)
            result=compute_intelligence([make_row(i,rng) for i in range(n)],plan(n),QUALITY)
            rep=result['summary']['brand_reputation']['index']
            if rep is not None:
                self.assertGreaterEqual(rep,0); self.assertLessEqual(rep,100)
            conf=result['summary']['confidence']['score']
            self.assertGreaterEqual(conf,0); self.assertLessEqual(conf,100)

    def test_order_does_not_change_core_aggregate(self):
        rng=random.Random(77)
        rows=[make_row(i,rng) for i in range(250)]
        a=compute_intelligence(rows,plan(len(rows)),QUALITY)['summary']
        shuffled=list(rows); random.Random(88).shuffle(shuffled)
        b=compute_intelligence(shuffled,plan(len(rows)),QUALITY)['summary']
        self.assertEqual(a['brand_reputation'],b['brand_reputation'])
        self.assertEqual(a['sentiment'],b['sentiment'])
        self.assertEqual(a['source_breakdown'],b['source_breakdown'])

    def test_viral_extreme_metrics_never_create_nonfinite_weight(self):
        rng=random.Random(2)
        rows=[make_row(i,rng) for i in range(20)]
        rows[0].update({'views':10**100,'likes':10**100,'comments':10**100,'shares':10**100,'followers':10**100})
        result=compute_intelligence(rows,plan(len(rows)),QUALITY)
        intel=result['records'][0]['intelligence']
        self.assertTrue(math.isfinite(intel['impact_score']))
        self.assertTrue(math.isfinite(intel['reputation_weight']))
        self.assertLessEqual(intel['impact_score'],1)


if __name__=='__main__': unittest.main()
