import copy
import math
import unittest

from app.services.investigations import INDICATOR_REGISTRY
from app.services.visualizations import build_visualization_state, VisualizationCancelled


def inv_value(indicator_id):
    values = {
        "sample_volume": {"analysis_ready_records": 120, "reputation_eligible_records": 96, "organic_opinion_records": 82, "trusted_records": 120, "collected_cleaning_records": 150, "effective_independent_voices": 91.5},
        "market_relevance": {"market":"Greece", "records_with_market_score":120, "high_relevance_records":110, "high_relevance_share":0.9167, "average_market_score":0.88},
        "brand_reputation": {"index": 61.25, "interpretation":"positive"},
        "evidence_confidence": {"score": 83.2, "label":"high", "effective_sample_size":76.1, "note":"Evidence strength only."},
        "sentiment": {"counts":{"positive":48,"negative":28,"neutral":15,"mixed":5}, "weighted_percent":{"positive":52.1,"negative":30.2,"neutral":12.7,"mixed":5.0}, "records":96},
        "emotions": {"counts":{"joy":30,"anger":24,"sadness":8,"fear":4,"disgust":2,"surprise":8,"neutral":6}, "weighted_percent":{"joy":36.0,"anger":29.0,"sadness":9.0,"fear":5.0,"disgust":2.0,"surprise":10.0,"neutral":9.0}, "records":82},
        "stance": {"counts":{"supportive":46,"critical":29,"neutral":15,"mixed":6,"not_applicable":0}, "weighted_percent":{"supportive":50.0,"critical":31.0,"neutral":13.0,"mixed":6.0,"not_applicable":0.0}, "records":96},
        "impact_attention": {"records_with_known_public_metrics":100,"records":120,"known_metric_share":0.8333},
        "effective_independent_voices": 91.5,
        "origin_breakdown": {"media":{"records":40,"record_share_percent":33.3,"attention_share_percent":55.0,"brand_reputation_index":57.0,"reputation_eligible":30},"person":{"records":70,"record_share_percent":58.3,"attention_share_percent":38.0,"brand_reputation_index":64.0,"reputation_eligible":60},"owned":{"records":10,"record_share_percent":8.4,"attention_share_percent":7.0,"brand_reputation_index":None,"reputation_eligible":0}},
        "source_breakdown": {"x":{"records":45,"reputation_eligible":38,"brand_reputation_index":58.0,"average_impact":0.62,"organic_opinion_records":35},"tiktok":{"records":35,"reputation_eligible":30,"brand_reputation_index":66.0,"average_impact":0.77,"organic_opinion_records":29},"news":{"records":40,"reputation_eligible":28,"brand_reputation_index":60.0,"average_impact":0.55,"organic_opinion_records":18}},
        "positive_narrative_drivers": [{"name":"Winning dream","mention_count":22,"reputation_point_contribution":8.4,"average_sentiment":0.7,"average_impact":0.65}],
        "negative_narrative_drivers": [{"name":"Trust concern","mention_count":15,"reputation_point_contribution":-5.1,"average_sentiment":-0.8,"average_impact":0.72}],
        "topic_drivers": [{"name":"Jackpot","mention_count":30,"reputation_point_contribution":5.0,"average_sentiment":0.5},{"name":"Price","mention_count":18,"reputation_point_contribution":-3.0,"average_sentiment":-0.6}],
        "media_influence": [{"author":"News A","records":8,"attention_score_sum":6.4,"weighted_sentiment":-0.2}],
        "people_influence": [{"author":"Creator A","records":4,"attention_score_sum":3.8,"weighted_sentiment":0.6}],
        "time_trends": {"daily_points":3,"daily":[{"date":"2026-08-01","records":20,"brand_reputation_index":62.0,"negative_weight_share":0.20,"anger_opinion_weight_share":0.15,"attention_score_sum":10.0},{"date":"2026-08-02","records":25,"brand_reputation_index":59.0,"negative_weight_share":0.30,"anger_opinion_weight_share":0.25,"attention_score_sum":14.0},{"date":"2026-08-03","records":55,"brand_reputation_index":48.0,"negative_weight_share":0.55,"anger_opinion_weight_share":0.50,"attention_score_sum":31.0}]},
        "numeric_anomalies": [{"date":"2026-08-03","flags":["volume_spike","anger_share_spike"],"volume_robust_z":5.0,"negative_robust_z":3.0,"anger_robust_z":4.8}],
        "data_quality": {"score":91,"label":"high"},
        "source_coverage": {"ratio":0.83,"source_count":3},
        "sample_achievement": {"ratio":0.92,"trusted_records":120},
        "authenticity_risk": {"low_authenticity_records":4,"low_authenticity_share":0.0333},
        "coordination": [{"cluster_id":"coord-1","records":9,"accounts":7,"risk":"suspicious"}],
        "story_syndication": [{"cluster_id":"story-1","records":12,"sources":["news"],"representative_excerpt":"Same story"}],
        "top_mentions": [{"record_id":"r1","platform":"x","author":"u1","date":"2026-08-03","url":"https://x/1","excerpt":"Important mention","sentiment_label":"negative","sentiment_score":-0.9,"emotion":"anger","topic":"Trust","narrative":"Trust concern","impact_score":0.9,"evidence_confidence":0.95}],
    }
    return copy.deepcopy(values[indicator_id])


def pack(all_available=True):
    inventory=[]
    for indicator_id,label,role in INDICATOR_REGISTRY:
        value=inv_value(indicator_id)
        inventory.append({"indicator_id":indicator_id,"label":label,"role":role,"examined":True,"available":all_available,"availability":"available" if all_available else "insufficient_or_not_triggered","value":value if all_available else ({"records":0} if indicator_id in {"sentiment","emotions","stance"} else {})})
    return {
        "contract_version":"signalyth-evidence-pack-v1.1",
        "generated_at":"2026-09-03T12:00:00+00:00",
        "research_context":{"client":"OPAP","topic":"Eurojackpot","market":"Greece","date_from":"2026-08-01","date_to":"2026-08-31"},
        "indicator_inventory":inventory,
        "headline_metrics":{"step5_warnings":["Not all selected sources returned usable data."]},
        "investigations":[{"investigation_id":"i1","question":{"en":"Why?","el":"Γιατί;"},"finding":{"en":"Observed association","el":"Παρατηρούμενη συσχέτιση"},"confidence":{"score":0.8},"presentation":{"candidate":True,"priority_score":90},"causal_status":"not_proven","evidence":[]}],
        "presentation_guardrails":{"must_review_all_indicators":True,"must_preserve_quality_warnings":True,"must_not_present_association_as_causation":True,"must_attach_evidence_to_material_claims":True},
    }


class VisualizationTests(unittest.TestCase):
    def test_complete_pack_reviews_all_25_indicators(self):
        out=build_visualization_state(pack())
        self.assertEqual(out['summary']['indicator_contract']['required'],25)
        self.assertEqual(out['summary']['indicator_contract']['examined'],25)
        self.assertTrue(out['summary']['indicator_contract']['complete'])
        self.assertEqual([x['indicator_id'] for x in out['indicator_review']],[x[0] for x in INDICATOR_REGISTRY])

    def test_does_not_mutate_step6_pack(self):
        p=pack(); original=copy.deepcopy(p)
        build_visualization_state(p)
        self.assertEqual(p,original)

    def test_brand_reputation_uses_exact_step6_value(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='brand_reputation')
        self.assertEqual(c['data']['value'],61.25)

    def test_sentiment_chart_preserves_weighted_percentages(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='sentiment_distribution')
        vals={x['label']:x['value'] for x in c['data']['categories']}
        self.assertEqual(vals['positive'],52.1)
        self.assertEqual(vals['negative'],30.2)

    def test_emotion_chart_keeps_all_registered_emotions_including_small_ones(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='emotion_distribution')
        self.assertEqual([x['label'] for x in c['data']['categories']],['joy','anger','sadness','fear','disgust','surprise','neutral'])

    def test_narrative_driver_chart_is_diverging_and_signed(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='narrative_drivers')
        vals={x['name']:x['contribution'] for x in c['data']['rows']}
        self.assertGreater(vals['Winning dream'],0)
        self.assertLess(vals['Trust concern'],0)

    def test_time_series_retains_raw_series_for_editable_rendering(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='time_trends')
        self.assertEqual(c['data']['rows'][2]['records'],55)
        self.assertEqual(c['data']['rows'][2]['brand_reputation_index'],48.0)
        self.assertIn('anger_opinion_weight_share',c['data']['series'])

    def test_every_chart_is_native_editable_ready_not_raster(self):
        out=build_visualization_state(pack())
        self.assertTrue(out['chart_specs'])
        for c in out['chart_specs']:
            self.assertTrue(c['presentation']['native_editable_ready'])
            self.assertFalse(c['presentation']['render_as_raster'])
            self.assertIsInstance(c['data'],dict)

    def test_top_mentions_retain_record_ids_as_evidence_refs(self):
        out=build_visualization_state(pack())
        c=next(x for x in out['chart_specs'] if x['chart_id']=='top_mentions')
        self.assertEqual(c['evidence_refs'],['r1'])

    def test_quality_warning_and_causality_guardrail_survive(self):
        out=build_visualization_state(pack())
        self.assertIn('Not all selected sources returned usable data.',out['dashboard']['warnings'])
        self.assertIn('Association ≠ proven causality.',out['dashboard']['warnings'])
        self.assertEqual(out['dashboard']['causal_guardrail'],'not_proven')

    def test_investigation_candidates_are_in_dashboard(self):
        out=build_visualization_state(pack())
        self.assertEqual(len(out['dashboard']['investigations']),1)
        self.assertEqual(out['dashboard']['investigations'][0]['investigation_id'],'i1')

    def test_presentation_visual_pack_requires_review_all_indicators(self):
        out=build_visualization_state(pack())
        g=out['presentation_visual_pack']['guardrails']
        self.assertTrue(g['must_review_all_indicators'])
        self.assertTrue(g['native_editable_charts_required'])
        self.assertTrue(g['omission_requires_review_record'])

    def test_missing_indicator_stops_instead_of_silent_omission(self):
        p=pack(); p['indicator_inventory'].pop()
        with self.assertRaises(RuntimeError): build_visualization_state(p)

    def test_duplicate_indicator_stops_instead_of_overwriting(self):
        p=pack(); p['indicator_inventory'][-1]=copy.deepcopy(p['indicator_inventory'][0])
        with self.assertRaises(RuntimeError): build_visualization_state(p)

    def test_missing_review_guardrail_stops(self):
        p=pack(); p['presentation_guardrails']['must_review_all_indicators']=False
        with self.assertRaises(RuntimeError): build_visualization_state(p)

    def test_sparse_pack_produces_no_decorative_empty_metric_charts(self):
        p=pack(all_available=False)
        p['investigations']=[]
        out=build_visualization_state(p)
        self.assertEqual(out['chart_specs'],[])
        self.assertEqual(out['summary']['indicator_contract']['insufficient_data'],25)

    def test_cancel_before_build_stops_without_output(self):
        with self.assertRaises(VisualizationCancelled):
            build_visualization_state(pack(),cancel_check=lambda:True)

    def test_chart_ids_are_unique(self):
        out=build_visualization_state(pack())
        ids=[x['chart_id'] for x in out['chart_specs']]
        self.assertEqual(len(ids),len(set(ids)))

    def test_dashboard_contains_only_sections_with_real_charts(self):
        out=build_visualization_state(pack())
        chart_ids={x['chart_id'] for x in out['chart_specs']}
        for sec in out['dashboard']['sections']:
            self.assertTrue(sec['chart_ids'])
            self.assertTrue(set(sec['chart_ids']).issubset(chart_ids))

    def test_json_numeric_values_are_finite(self):
        p=pack(); p['indicator_inventory'][2]['value']['index']=float('nan')
        out=build_visualization_state(p)
        c=next(x for x in out['chart_specs'] if x['chart_id']=='brand_reputation')
        self.assertTrue(math.isfinite(c['data']['value']))


if __name__=='__main__': unittest.main()
