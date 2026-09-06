import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pptx import Presentation

from app.services.intelligence import _time_series, _top_mentions
from app.services.presentation import GOLD_STANDARD_CAPABILITIES, build_presentation_plan, generate_pptx

ROOT = Path(__file__).resolve().parents[1]
GOLD = json.loads((ROOT / 'evals' / 'gold_standard_fixture.json').read_text(encoding='utf-8'))
SPEC = json.loads((ROOT / 'evals' / 'eurojackpot_gold_standard.json').read_text(encoding='utf-8'))


def plan(language='English'):
    return {
        'client': 'OPAP', 'topic': 'Eurojackpot', 'market': 'Greece',
        'date_from': '2026-08-01', 'date_to': '2026-08-31',
        'report_language': language,
    }


class GoldStandardSpecificationTests(unittest.TestCase):
    def test_source_spec_covers_all_27_reference_slides(self):
        slides = [x['slide'] for x in SPEC['source_slide_map']]
        self.assertEqual(slides, list(range(1, 28)))

    def test_source_spec_uses_only_registered_benchmark_capabilities_or_documented_superior_features(self):
        registered = {x[0] for x in GOLD_STANDARD_CAPABILITIES}
        used = {c for row in SPEC['source_slide_map'] for c in row['capabilities']}
        self.assertTrue(used <= registered, used - registered)

    def test_benchmark_is_functional_not_a_fixed_slide_clone(self):
        self.assertIn('not a fixed 27-slide', SPEC['benchmark_principle'])


class GoldStandardPlanTests(unittest.TestCase):
    def setUp(self):
        self.e = copy.deepcopy(GOLD['evidence_pack'])
        self.v = copy.deepcopy(GOLD['visual_pack'])
        self.out = build_presentation_plan(self.v, self.e, plan())

    def test_gold_standard_capability_audit_is_100_percent(self):
        audit = self.out['gold_standard_audit']
        self.assertTrue(audit['complete'])
        self.assertEqual(audit['coverage_percent'], 100.0)
        self.assertFalse([r for r in audit['capabilities'] if r['applicable'] and r['status'] != 'covered'])

    def test_all_reference_capability_families_are_supported(self):
        required = {c for row in SPEC['source_slide_map'] for c in row['capabilities']}
        rows = {x['capability_id']: x for x in self.out['gold_standard_audit']['capabilities']}
        self.assertTrue(required <= set(rows))
        for cap in required:
            if rows[cap]['applicable']:
                self.assertEqual(rows[cap]['status'], 'covered', cap)

    def test_plan_contains_reference_equivalent_sections(self):
        ids = [x['slide_id'] for x in self.out['slides']]
        expected = {
            'cover', 'evidence_base', 'methodology', 'reputation_sentiment',
            'sentiment_evidence', 'emotions', 'emotion_evolution', 'drivers',
            'evolution', 'sources_audiences', 'influence', 'media_evidence',
            'strategic_synthesis', 'conclusions', 'evidence', 'data_integrity',
        }
        self.assertTrue(expected <= set(ids), expected - set(ids))

    def test_media_implication_investigation_is_present(self):
        slides = self.out['slides']
        media_inv = [s for s in slides if 'gold-i3' in (s.get('investigation_ids') or [])]
        self.assertEqual(len(media_inv), 1)
        self.assertEqual(media_inv[0]['slide_type'], 'investigation')

    def test_positive_and_negative_examples_are_both_present(self):
        slide = next(x for x in self.out['slides'] if x['slide_id'] == 'sentiment_evidence')
        labels = {(c.get('source_values') or {}).get('sentiment') for c in slide['claims']}
        self.assertIn('positive', labels)
        self.assertIn('negative', labels)
        self.assertTrue(all(c.get('evidence_refs') for c in slide['claims']))

    def test_emotion_evolution_uses_real_time_series_chart(self):
        slide = next(x for x in self.out['slides'] if x['slide_id'] == 'emotion_evolution')
        self.assertEqual(slide['chart_ids'], ['emotion_trends'])
        chart = next(c for c in self.v['chart_specs'] if c['chart_id'] == 'emotion_trends')
        self.assertEqual(chart['chart_type'], 'time_series')
        self.assertGreaterEqual(len(chart['data']['series']), 2)

    def test_conclusions_remain_last_and_methodology_is_appendix_before_it(self):
        ids = [x['slide_id'] for x in self.out['slides']]
        self.assertEqual(ids[-1], 'conclusions')
        self.assertLess(ids.index('methodology'), ids.index('conclusions'))

    def test_dynamic_deck_is_compact_but_not_underpowered(self):
        # It covers a 27-slide reference deck without blindly cloning it.
        self.assertGreaterEqual(len(self.out['slides']), 16)
        self.assertLess(len(self.out['slides']), 27)

    def test_every_gold_standard_claim_is_traceable(self):
        for claim in self.out['claim_ledger']:
            if claim['claim_type'] != 'guardrail':
                self.assertTrue(claim['indicator_ids'], claim)
            if claim['claim_type'] == 'evidence_example':
                self.assertTrue(claim['evidence_refs'], claim)


class GoldStandardRendererTests(unittest.TestCase):
    def test_gold_standard_pptx_is_editable_and_reopenable(self):
        p = build_presentation_plan(GOLD['visual_pack'], GOLD['evidence_pack'], plan())
        with tempfile.TemporaryDirectory(prefix='sig-gold-') as td:
            out = Path(td) / 'gold.pptx'
            meta = generate_pptx(p, GOLD['visual_pack'], out, ROOT / 'logo.png')
            prs = Presentation(out)
            self.assertEqual(len(prs.slides), meta['slides'])
            with zipfile.ZipFile(out) as z:
                chart_parts = [n for n in z.namelist() if n.startswith('ppt/charts/chart') and n.endswith('.xml')]
                media = [n for n in z.namelist() if n.startswith('ppt/media/')]
            self.assertGreaterEqual(len(chart_parts), 8)
            self.assertLessEqual(len(media), 1)  # logo only; no chart screenshots


class UpstreamEvidenceTests(unittest.TestCase):
    def test_time_series_tracks_all_emotions_not_anger_only(self):
        rows = []
        emotions = ['joy', 'anger', 'sadness', 'fear', 'disgust', 'surprise', 'neutral']
        for day in range(1, 7):
            for i, emotion in enumerate(emotions):
                rows.append({
                    'date': f'2026-08-{day:02d}T12:00:00+00:00',
                    'ai_analysis': {
                        'opinion_eligible': True,
                        'primary_emotion': emotion,
                        'sentiment_label': 'negative' if emotion == 'anger' else 'positive',
                    },
                    'intelligence': {
                        'organic_opinion_weight': 1.0 + (0.1 * i),
                        'reputation_eligible': True,
                        'reputation_weight': 1.0,
                        'independent_voice_weight': 1.0,
                        'impact_score': 0.5,
                    },
                })
        out = _time_series(rows)
        self.assertTrue(out['daily'])
        point = out['daily'][0]
        for emotion in emotions:
            self.assertIn(f'{emotion}_opinion_weight_share', point)

    def test_top_mentions_carry_literal_excerpt_and_origin_for_presentation_evidence(self):
        records = [{
            'id': 'r1', 'platform': 'x', 'author': 'alice', 'date': '2026-08-01', 'url': 'https://x/1',
            'text': '  πραγματικό   evidence κείμενο  ', 'content_type': 'post',
            'ai_analysis': {'sentiment_label': 'negative', 'sentiment_score': -0.9, 'primary_emotion': 'anger', 'topic': 'Trust', 'narrative': 'Trust concern'},
            'intelligence': {'impact_score': 0.9, 'impact_confidence': 0.8, 'evidence_confidence': 0.95, 'origin_group': 'person'},
        }]
        out = _top_mentions(records)
        self.assertEqual(out[0]['excerpt'], 'πραγματικό evidence κείμενο')
        self.assertEqual(out[0]['origin_group'], 'person')
        self.assertEqual(out[0]['content_type'], 'post')


if __name__ == '__main__':
    unittest.main()
