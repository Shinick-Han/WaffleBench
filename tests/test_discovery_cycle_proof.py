"""Reject altered or extra SDK calls against the published genuine cycle."""
import copy
import json
import unittest
from pathlib import Path
from scripts.run_discovery_cycle import verify

EVIDENCE = Path(__file__).resolve().parents[1] / 'evidence/discovery-cycle'

@unittest.skipUnless((EVIDENCE / 'omnigent/sdk-records.json').is_file(), 'published cycle not available')
class ProofTest(unittest.TestCase):
    def setUp(self):
        self.capture = json.loads((EVIDENCE / 'omnigent/sdk-records.json').read_text(encoding='utf-8'))

    def test_real_published_sdk_record_passes(self):
        self.assertEqual(verify(EVIDENCE, self.capture)['status'], 'passed')

    def test_noncompleted_workflow_is_not_proof(self):
        self.capture['outcome'] = 'failed'
        self.assertEqual(verify(EVIDENCE, self.capture)['status'], 'failed')

    def test_missing_child_is_not_proof(self):
        self.capture['children'].pop(next(iter(self.capture['children'])))
        self.assertEqual(verify(EVIDENCE, self.capture)['status'], 'failed')

    def test_genuine_extra_execution_survives_mirror_normalization(self):
        child = next(c for c in self.capture['children'].values()
                     if any(i.get('name') == 'discovery__run_experiment' for i in c['items']))
        items = child['items']
        index = max(i for i, item in enumerate(items) if item.get('name') == 'discovery__run_experiment')
        call = copy.deepcopy(items[index])
        result = copy.deepcopy(next(x for x in items[index+1:] if x.get('type') == 'function_call_output' and x.get('call_id') == call['call_id']))
        call['id'], result['id'] = 'fixture-extra-call', 'fixture-extra-output'
        call['call_id'] = result['call_id'] = 'fixture-genuine-new-sdk-call'
        call['response_id'] = call['model'] = 'fixture-new-response'
        items.extend([call, result])
        receipt = verify(EVIDENCE, self.capture)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('no extra specialist mutations', receipt['failure_reasons'])

    def test_changed_plan_arguments_are_rejected(self):
        for child in self.capture['children'].values():
            for item in child['items']:
                if item.get('name') == 'discovery__record_plan':
                    args = item['arguments']
                    if isinstance(args, str):
                        args = json.loads(args)
                    args['hypothesis'] = 'Altered hypothesis not authored in this ledger.'
                    item['arguments'] = args
        receipt = verify(EVIDENCE, self.capture)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('cycle 1 SDK plan arguments match ledger', receipt['failure_reasons'])

if __name__ == '__main__':
    unittest.main()
