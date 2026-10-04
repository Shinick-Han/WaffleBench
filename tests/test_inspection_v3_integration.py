"""Coordinator checks on fresh development, never primary test outcomes."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from inspection_v3 import cli, harness, model
from inspection_review.policies import sanitize_public


class V3Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.config = cli.read_json(cli.PROTOCOL)
        cls.config['v2_splits']['train'] = cls.config['v2_splits']['train'][:2]
        cls.config['splits']['train_seeds'] = [r['seed'] for r in cls.config['v2_splits']['train']]
        cls.config['v2_splits']['calibration'] = cls.config['v2_splits']['calibration'][:2]
        cls.config['v2_splits']['development'] = cls.config['v2_splits']['development'][:1]
        cls.config['v3_model_grid'] = {'cb400': {'iterations': 20, 'depth': 3, 'learning_rate': .06}}
        cls.config['v3_yield']['iterations'] = 20
        cls.config['v2_variants'] = [v for v in cls.config['v2_variants'] if v['id'] in {
            'logistic_learned', 'cb400_learned', 'cb400_platt_learned', 'cb400_temperature_learned',
            'cb400_route_half', 'cb400_route_full', 'yield_greedy', 'yield_route'}]
        cls.config['modes'] = ['candidate_only']
        cls.config['budgets'] = [120, 360]
        cls.path = cls.base / 'protocol.json'
        cli.write_json(cls.path, cls.config)
        cls.root = cls.base / 'development'
        with patch.object(cli, 'PROTOCOL', cls.path):
            cls.result = cli.develop(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_new_splits_exclude_previous_studies(self):
        self.assertEqual(self.result['runs'], 16)
        self.assertFalse((self.root / 'lots/test').exists())
        bad = copy.deepcopy(self.config)
        bad['v2_splits']['development'][0]['seed'] = 4000
        with self.assertRaises(Exception):
            cli.check_splits(bad)

    def test_equal_training_and_original_logistic_settings(self):
        fitted = cli.read_json(self.root / 'models.json')
        self.assertEqual(fitted['logistic/identity']['fit']['learning_rate'], .1)
        self.assertEqual(fitted['logistic/identity']['fit']['epochs'], 250)
        self.assertEqual(fitted['logistic/identity']['train_ids'], fitted['cb400/identity']['train_ids'])

    def test_yield_score_keeps_doi_probability_independent(self):
        result = cli.read_json(self.root / 'development.json')
        refs = {r['lot_id']: r for r in cli.read_json(self.root / 'lot-manifest.json')['development']}
        fitted = cli.read_json(self.root / 'models.json')
        for run in result['runs']:
            ledger = cli.read_json(Path(run['ledger_path']))
            key = ledger['variant']['model']
            public = cli.load_public(Path(refs[run['lot_id']]['path']))
            expected = model.predict(fitted[key], public['features'])
            is_yield = ledger['variant'].get('reward') == 'review_yield'
            self.assertEqual(ledger['selection_reward_semantics'], 'reported_review_positive' if is_yield else 'latent_doi_probability')
            for row in ledger['run']['rows']:
                self.assertAlmostEqual(row['baseline_p'], expected[row['site_index']])
                self.assertLessEqual(row['charged'], row['reserved_cost'] + 1e-9)
                self.assertFalse({'oracle', 'doi', 'seed', 'scenario'} & set(row['components']))
            self.assertLessEqual(ledger['run']['spent'], run['budget'] + 1e-9)
            self.assertFalse(ledger['run']['online_updates_enabled'])

    def test_development_truth_cannot_be_loaded_as_training(self):
        ref = cli.read_json(self.root / 'lot-manifest.json')['development'][0]
        with self.assertRaises(Exception):
            cli.privileged(ref, 'train')

    def test_invalid_reward_rejected_before_sensor_call(self):
        ref = cli.read_json(self.root / 'lot-manifest.json')['development'][0]
        public = cli.load_public(Path(ref['path']))
        fitted = cli.read_json(self.root / 'models.json')['cb400/identity']
        view = sanitize_public(public, fitted['mean'], fitted['scale'])
        calls = []
        with self.assertRaises(ValueError):
            harness.run_selection(view, lambda i, a: calls.append((i, a)), policy_name='yield_greedy', mode='candidate_only',
                budget=120, frozen_model=fitted, frozen_p=model.predict(fitted, public['features']), model_api=model,
                config=self.config, selection_reward=np.full(view.n, np.nan))
        self.assertEqual(calls, [])

    def test_freeze_protects_yield_artifact_before_test_generation(self):
        receipt = cli.freeze(self.root)
        self.assertEqual(receipt['status'], 'frozen')
        cli.verify_freeze(self.root)
        path = self.root / 'review-yield.json'
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        try:
            with self.assertRaises(Exception):
                cli.campaign(self.root)
            self.assertFalse((self.root / 'lots/test').exists())
        finally:
            path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
