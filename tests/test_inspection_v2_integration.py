"""Coordinator integration checks on fresh development lots, never held-out data."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inspection_v2 import cli, model

class V2Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.config = cli.read_json(cli.PROTOCOL)
        cls.config['v2_splits']['train'] = cls.config['v2_splits']['train'][:2]
        cls.config['splits']['train_seeds'] = [r['seed'] for r in cls.config['v2_splits']['train']]
        cls.config['v2_splits']['calibration'] = cls.config['v2_splits']['calibration'][:2]
        cls.config['v2_splits']['development'] = cls.config['v2_splits']['development'][:1]
        cls.config['v2_model']['iterations'] = 20
        cls.config['v2_variants'] = [v for v in cls.config['v2_variants'] if v['id'] in {
            'v1_logistic_learned','v1_logistic_falsify','catboost_learned','calibrated_learned','calibrated_route'}]
        cls.config['modes'] = ['candidate_only']
        cls.config['budgets'] = [120,360]
        cls.path = cls.base / 'protocol.json'
        cli.write_json(cls.path,cls.config)
        cls.root = cls.base / 'development'
        with patch.object(cli,'PROTOCOL',cls.path):
            cls.result = cli.develop(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_pipeline_uses_fresh_split_and_no_test_generation(self):
        self.assertEqual(self.result['runs'],10)
        self.assertFalse((self.root / 'lots/test').exists())
        self.assertEqual(self.result['status'],'development_complete')
        with self.assertRaises(Exception):
            cli.develop(self.root)

    def test_legacy_baseline_training_parameters_remain_original(self):
        fitted = cli.read_json(self.root / 'models.json')['logistic/identity']
        self.assertEqual(fitted['fit']['learning_rate'],.1)
        self.assertEqual(fitted['fit']['epochs'],250)

    def test_ledger_reservations_and_policy_input_boundary(self):
        result = cli.read_json(self.root / 'development.json')
        for run in result['runs']:
            ledger = cli.read_json(Path(run['ledger_path']))
            self.assertLessEqual(ledger['run']['spent'],run['budget']+1e-9)
            for row in ledger['run']['rows']:
                self.assertLessEqual(row['charged'],row['reserved_cost']+1e-9)
                self.assertTrue(row['original_candidate'])
                self.assertFalse({'doi','oracle','scenario','seed'} & set(row['components']))
                if run['policy']=='calibrated_route':
                    self.assertEqual(row['components']['budget_source'],'state_remaining_budget')

    def test_model_capability_controls_online_update(self):
        result = cli.read_json(self.root / 'development.json')
        for run in result['runs']:
            ledger = cli.read_json(Path(run['ledger_path']))
            if run['policy']=='v1_logistic_falsify':
                self.assertTrue(ledger['run']['online_updates_enabled'])
            if 'calibrated' in run['policy'] or 'catboost' in run['policy']:
                self.assertFalse(ledger['run']['online_updates_enabled'])

    def test_training_loader_rejects_development_truth_access(self):
        refs = cli.read_json(self.root / 'lot-manifest.json')
        with self.assertRaises(Exception):
            cli.privileged(refs['development'][0],'development')

    def test_freeze_rejects_config_tampering_before_any_test_lot(self):
        frozen = cli.freeze(self.root)
        self.assertEqual(frozen['status'],'frozen')
        cli.verify_freeze(self.root)
        frozen_config = self.root / 'frozen-config.json'
        original = frozen_config.read_bytes()
        frozen_config.write_bytes(original+b'\n')
        try:
            with self.assertRaises(Exception):
                cli.campaign(self.root)
            self.assertFalse((self.root / 'lots/test').exists())
        finally:
            frozen_config.write_bytes(original)

if __name__=='__main__':
    unittest.main()
