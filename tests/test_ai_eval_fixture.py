import json
import unittest
from pathlib import Path

class AIEvalFixtureTests(unittest.TestCase):
    def test_semantic_eval_set_has_greek_greeklish_sarcasm_and_irrelevance(self):
        path=Path(__file__).resolve().parents[1]/'evals'/'semantic_cases.json'
        cases=json.loads(path.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(cases),20)
        ids={c['id'] for c in cases}
        self.assertEqual(len(ids),len(cases))
        self.assertTrue(any(c['expected']['language']==['greeklish'] for c in cases))
        self.assertTrue(any(True in c['expected']['sarcasm'] for c in cases))
        self.assertTrue(any('irrelevant' in c['expected']['relevance'] for c in cases))
        self.assertTrue(any(any('\u0370' <= ch <= '\u03ff' for ch in c['text']) for c in cases))

    def test_each_eval_case_defines_all_core_expected_fields(self):
        cases=json.loads((Path(__file__).resolve().parents[1]/'evals'/'semantic_cases.json').read_text(encoding='utf-8'))
        required={'relevance','sentiment','emotion','sarcasm','language','stance'}
        for case in cases:
            self.assertEqual(set(case['expected']),required,case['id'])
            self.assertTrue(all(isinstance(v,list) and v for v in case['expected'].values()),case['id'])

if __name__=='__main__': unittest.main()
