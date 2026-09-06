import copy
import json
import math
import random
import unittest

from app.services.visualizations import build_visualization_state
from tests.test_visualizations import pack


def walk(v):
    if isinstance(v, dict):
        for x in v.values(): yield from walk(x)
    elif isinstance(v, list):
        for x in v: yield from walk(x)
    else:
        yield v


class VisualizationStressTests(unittest.TestCase):
    def test_100_randomized_metric_packs_produce_json_safe_specs(self):
        rng=random.Random(20260903)
        for _ in range(100):
            p=pack()
            for row in p['indicator_inventory']:
                k=row['indicator_id']; v=row['value']
                if k=='brand_reputation': v['index']=rng.uniform(0,100)
                elif k=='evidence_confidence': v['score']=rng.uniform(0,100)
                elif k in {'sentiment','emotions','stance'}:
                    for label in list(v['weighted_percent']): v['weighted_percent'][label]=rng.uniform(0,100)
                elif k=='market_relevance': v['average_market_score']=rng.random(); v['high_relevance_share']=rng.random()
                elif k=='authenticity_risk': v['low_authenticity_share']=rng.random()
            out=build_visualization_state(p)
            json.dumps(out,allow_nan=False)
            self.assertEqual(out['summary']['indicator_contract']['examined'],25)
            self.assertTrue(out['summary']['native_editable_ready'])

    def test_nonfinite_nested_evidence_is_sanitized(self):
        p=pack(); top=next(x for x in p['indicator_inventory'] if x['indicator_id']=='top_mentions'); top['value'][0]['impact_score']=float('inf'); top['value'][0]['evidence_confidence']=float('nan')
        out=build_visualization_state(p); json.dumps(out,allow_nan=False)
        c=next(x for x in out['chart_specs'] if x['chart_id']=='top_mentions')
        self.assertIsNone(c['data']['rows'][0]['impact_score']); self.assertIsNone(c['data']['rows'][0]['evidence_confidence'])

    def test_output_chart_order_is_deterministic(self):
        a=build_visualization_state(pack()); b=build_visualization_state(pack())
        self.assertEqual([x['chart_id'] for x in a['chart_specs']],[x['chart_id'] for x in b['chart_specs']])
        self.assertEqual(a['presentation_visual_pack']['presentation_chart_order'],b['presentation_visual_pack']['presentation_chart_order'])

    def test_long_untrusted_labels_remain_data_not_markup(self):
        p=pack(); topic=next(x for x in p['indicator_inventory'] if x['indicator_id']=='topic_drivers'); topic['value'][0]['name']='<img src=x onerror=alert(1)>'+('A'*5000)
        out=build_visualization_state(p); c=next(x for x in out['chart_specs'] if x['chart_id']=='topic_drivers')
        self.assertTrue(c['data']['rows'][0]['name'].startswith('<img'))
        # Service preserves evidence text; escaping belongs to the UI renderer.
        self.assertNotIn('&lt;img',c['data']['rows'][0]['name'])

    def test_full_result_contains_no_nonfinite_numbers(self):
        out=build_visualization_state(pack())
        for x in walk(out):
            if isinstance(x,float): self.assertTrue(math.isfinite(x))


if __name__=='__main__': unittest.main()
