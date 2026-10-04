"""v4 harness checks on tiny fixture lots only; never real scientific evidence."""
import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from inspection_review import model as v1_model
from inspection_v3 import model as v3_model
from inspection_v3.inference import CompiledPredictor
from inspection_v4 import cli, model

SCENARIOS = ['stationary', 'novel_cluster', 'low_contrast', 'nuisance_heavy', 'process_shift']


def tiny_config():
    """Two scenarios, fixture-only seeds 8700+ outside every protocol split, small fits."""
    config = cli.read_json(cli.PROTOCOL)
    scenarios = ['stationary', 'process_shift']
    rows = lambda base, n: [{'seed': base + 10 * j + i, 'scenario': s} for j, s in enumerate(scenarios) for i in range(n)]  # noqa: E731
    config['v2_splits'] = {'train': rows(8700, 2), 'calibration': rows(8720, 1), 'development': rows(8740, 1), 'test': rows(8760, 1)}
    config['splits'] = {'train_seeds': [r['seed'] for r in config['v2_splits']['train']], 'validation_seeds': [],
                        'test_seeds_by_scenario': {s: [r['seed'] for r in config['v2_splits']['test'] if r['scenario'] == s] for s in scenarios}}
    config['v4_design'].update(scenarios=scenarios, lots_per_scenario={'train': 2, 'calibration': 1, 'development': 1, 'test': 1})
    config['v4_baselines']['cb400/identity'].update(iterations=20, depth=3)
    config['v4_model_grid'] = {'gmm_diag4x3': {**config['v4_model_grid']['gmm_diag4x3'], 'max_iter': 30, 'components_positive': 2, 'components_negative': 2}}
    config['v2_variants'] = [v for v in config['v2_variants'] if v['model'].split('/')[0] in {'logistic', 'cb400', 'gmm_diag4x3'}]
    config['v4_primary']['bootstrap_replicates'] = 50
    return config


class V4Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.config = tiny_config()
        cls.path = cls.base / 'protocol.json'
        cli.write_json(cls.path, cls.config)
        cls.root = cls.base / 'development'
        with patch.object(cli, 'PROTOCOL', cls.path):
            cls.developed = cli.develop(cls.root)
        cls.test_lots_before_freeze = (cls.root / 'lots' / 'test').exists()
        cls.copy = cls.base / 'tamper'
        shutil.copytree(cls.root, cls.copy)
        cls.frozen = cli.freeze(cls.root)
        cls.campaigned = cli.campaign(cls.root)
        cls.reported = cli.report(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def models(self):
        return cli.read_json(self.root / 'models.json')

    # ------------------------------------------------------------ split guards

    def test_real_protocol_seeds_are_fresh_and_balanced(self):
        config = cli.read_json(cli.PROTOCOL)
        cli.check_splits(config)
        formulas = {'train': (8000, 20, 12), 'calibration': (8100, 10, 4), 'development': (8200, 100, 8), 'test': (9000, 100, 24)}
        for split, (base, step, n) in formulas.items():
            expected = [{'seed': base + j * step + i, 'scenario': s} for j, s in enumerate(SCENARIOS) for i in range(n)]
            self.assertEqual(config['v2_splits'][split], expected)
        seeds = [r['seed'] for rows in config['v2_splits'].values() for r in rows]
        self.assertEqual(len(seeds), 240)
        self.assertFalse(set(seeds) & cli.previous_seeds())
        self.assertNotIn(9800, seeds)
        self.assertEqual(config['v4_reserved']['live_demonstration_seeds'], [9800])
        self.assertEqual(config['budgets'], [360])
        self.assertEqual(config['modes'], ['candidate_only'])
        self.assertEqual(len(config['v2_variants']), 22)

    def test_split_guard_rejects_before_any_lot_is_generated(self):
        def bad(edit):
            config = copy.deepcopy(self.config)
            edit(config)
            return config

        def reuse_development_seed(c):
            c['v2_splits']['test'][0]['seed'] = c['v2_splits']['development'][0]['seed']

        def unbalance(c):
            c['v2_splits']['development'][1]['scenario'] = 'stationary'

        cases = {
            'v3 held-out seed': lambda c: c['v2_splits']['development'][0].update(seed=7000),
            'v3 training seed': lambda c: c['v2_splits']['calibration'][0].update(seed=5000),
            'v2/v1 seed': lambda c: c['v2_splits']['development'][0].update(seed=min(cli.previous_seeds())),
            'live reserve': lambda c: c['v2_splits']['calibration'][0].update(seed=9800),
            'cross-split duplicate': reuse_development_seed,
            'unbalanced scenarios': unbalance,
            'train contract mismatch': lambda c: c['splits']['train_seeds'].pop(),
        }
        for name, edit in cases.items():
            with self.subTest(name):
                config = bad(edit)
                with self.assertRaises(cli.HarnessError):
                    cli.check_splits(config)
                path, root = self.base / f'bad-{len(name)}.json', self.base / f'bad-root-{len(name)}'
                cli.write_json(path, config)
                with patch.object(cli, 'PROTOCOL', path), self.assertRaises(cli.HarnessError):
                    cli.develop(root)
                self.assertFalse(root.exists())

    def test_test_lots_are_not_generated_before_freeze(self):
        self.assertFalse(self.test_lots_before_freeze)
        self.assertFalse((self.copy / 'lots' / 'test').exists())
        with self.assertRaises(cli.HarnessError):
            cli.generate(self.copy, 'test', self.config)

    # ------------------------------------------------------------ models and forwarding

    def test_all_models_share_training_and_calibration_lots(self):
        fitted = self.models()
        train_ids = fitted['cb400/identity']['train_ids']
        self.assertEqual(len(train_ids), 4)
        for key, m in fitted.items():
            self.assertEqual(m['train_ids'], train_ids, key)
            self.assertFalse(m['supports_online_update'], key)
        scenarios = {s for _, s in fitted['logistic/identity']['provenance']['train_pairs']}
        self.assertEqual(scenarios, {'stationary', 'process_shift'})
        mixtures = {k: m for k, m in fitted.items() if m['family'] == model.FAMILY}
        self.assertEqual(sorted(k.split('/')[1] for k in mixtures), ['identity', 'platt', 'temperature'])
        calibration_ids = {tuple(m['calibration']['calibration_ids']) for m in mixtures.values()}
        self.assertEqual(len(calibration_ids), 1)
        self.assertEqual(len({m['calibration']['base_model_hash'] for m in mixtures.values()}), 1)

    def test_logistic_keeps_v1_numerics(self):
        fitted = self.models()['logistic/identity']
        self.assertEqual((fitted['fit']['epochs'], fitted['fit']['learning_rate'], fitted['fit']['l2']), (250, .1, .01))
        x = np.random.default_rng(0).normal(size=(5, 7))
        np.testing.assert_array_equal(cli.MODEL_API.predict(fitted, x), v1_model.predict(fitted, x))

    def test_model_api_forwarding_and_immutable_predictions(self):
        fitted = self.models()
        refs = cli.read_json(self.root / 'lot-manifest.json')['development']
        features = cli.load_public(Path(refs[0]['path']))['features']
        gmm = fitted['gmm_diag4x3/platt']
        predictor = cli.predictor_for(gmm)
        self.assertIsInstance(predictor, cli.FrozenMixturePredictor)
        p = predictor.predict(features)
        np.testing.assert_array_equal(p, model.predict(gmm, features))
        np.testing.assert_array_equal(cli.MODEL_API.predict(gmm, features), p)
        self.assertFalse(p.flags.writeable)
        before = predictor.model_hash
        gmm['class_priors']['positive'] = 0.5
        np.testing.assert_array_equal(predictor.predict(features), p)
        self.assertEqual(predictor.model_hash, before)
        with self.assertRaises(AttributeError):
            predictor._snapshot = {}
        cb = fitted['cb400/identity']
        self.assertIsInstance(cli.predictor_for(cb), CompiledPredictor)
        np.testing.assert_allclose(cli.MODEL_API.predict(cb, features), v3_model.predict(cb, features))
        with self.assertRaises(cli.HarnessError):
            cli.MODEL_API.update_model(fitted['logistic/identity'], features[0], True, self.config)
        with self.assertRaises(ValueError):
            cli.FrozenMixturePredictor(cb)

    def test_ledgers_use_frozen_probabilities_without_updates(self):
        fitted = self.models()
        refs = {r['lot_id']: r for r in cli.read_json(self.root / 'lot-manifest.json')['development']}
        development = cli.read_json(self.root / 'development.json')
        self.assertEqual(len(development['runs']), 2 * len(self.config['v2_variants']))
        for run in development['runs']:
            ledger = cli.read_json(Path(run['ledger_path']))
            expected = model.predict(fitted[ledger['variant']['model']], cli.load_public(Path(refs[run['lot_id']]['path']))['features'])
            self.assertFalse(ledger['run']['online_updates_enabled'])
            self.assertLessEqual(ledger['run']['spent'], 360 + 1e-9)
            self.assertEqual(ledger['model_hash'], model.hash_model(fitted[ledger['variant']['model']]))
            for row in ledger['run']['rows']:
                self.assertAlmostEqual(row['baseline_p'], expected[row['site_index']])
                self.assertLessEqual(row['charged'], row['reserved_cost'] + 1e-9)
                self.assertFalse({'oracle', 'doi', 'seed', 'scenario'} & set(row['components']))

    # ------------------------------------------------------------ selection

    def test_candidate_selection_excludes_baselines(self):
        fitted = self.models()
        variants = {v['id']: v for v in self.config['v2_variants']}
        means = [{'variant': vid, 'mean_doi': 1.0, 'mean_spent_cu': 300.0} for vid in variants]
        for row in means:
            if variants[row['variant']]['role'] == 'baseline':
                row['mean_doi'] = 99.0
        means[-1]['mean_spent_cu'] = 100.0
        self.assertEqual(cli.select_candidate(means, self.config, fitted), means[-1]['variant'])
        for row in means:
            row['mean_spent_cu'] = 300.0
        gmm = sorted(v for v in variants if variants[v]['role'] == 'candidate')
        self.assertEqual(cli.select_candidate(means, self.config, fitted), gmm[0])
        with self.assertRaises(cli.HarnessError):
            cli.select_candidate([r for r in means if variants[r['variant']]['role'] == 'baseline'], self.config, fitted)
        selected = cli.read_json(self.root / 'development.json')['selected_candidate']
        self.assertEqual(variants[selected]['role'], 'candidate')
        self.assertEqual(fitted[variants[selected]['model']]['family'], model.FAMILY)

    # ------------------------------------------------------------ freeze and campaign

    def test_freeze_tamper_blocks_campaign_before_test_generation(self):
        cli.freeze(self.copy)
        receipt = cli.read_json(self.copy / 'freeze.json')
        self.assertFalse(receipt['test_generation_has_started'])
        for name in ['inspection_v1', 'inspection_v2', 'inspection_v3', 'inspection_v4']:
            prefix = 'inspection_review/' if name == 'inspection_v1' else name + '/'
            self.assertTrue(any(k.startswith(prefix) for k in receipt['source_hashes']), name)
        for name in ['INSPECTION_V4_CONTRACT.md', 'inspection_v4/protocol.json', 'pyproject.toml', 'uv.lock']:
            self.assertIn(name, receipt['source_hashes'])
        with self.assertRaises(cli.HarnessError):
            cli.freeze(self.copy)
        original = (self.copy / 'models.json').read_bytes()
        fitted = cli.read_json(self.copy / 'models.json')
        fitted['gmm_diag4x3/platt']['class_priors']['positive'] = 0.5
        cli.write_json(self.copy / 'models.json', fitted)
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.copy)
        (self.copy / 'models.json').write_bytes(original)
        tampered = dict(receipt, candidate='cb400_route_full')
        cli.write_json(self.copy / 'freeze.json', tampered)
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.copy)
        cli.write_json(self.copy / 'freeze.json', receipt)
        with patch.object(cli, 'source_hashes', return_value={**receipt['source_hashes'], 'inspection_v4/model.py': '0' * 64}):
            with self.assertRaises(cli.HarnessError):
                cli.campaign(self.copy)
        self.assertFalse((self.copy / 'campaign-started.json').exists())
        self.assertFalse((self.copy / 'lots' / 'test').exists())
        self.assertEqual(cli.verify_freeze(self.copy)['receipt_sha256'], receipt['receipt_sha256'])

    def test_tiny_end_to_end_develop_freeze_campaign_report(self):
        self.assertEqual(self.developed['status'], 'development_complete')
        self.assertEqual(self.frozen['candidate'], self.developed['selected_candidate'])
        self.assertEqual(self.campaigned['status'], 'campaign_complete')
        held = cli.read_json(self.root / 'held-out.json')
        self.assertEqual(held['kind'], 'held_out_synthetic_v4')
        self.assertEqual(len(held['runs']), 2 * len(self.config['v2_variants']))
        summary = held['summary']
        self.assertEqual(summary['candidate'], self.frozen['candidate'])
        self.assertEqual(summary['primary_vs_cb400_route_full']['comparator'], 'cb400_route_full')
        self.assertEqual(summary['primary_vs_cb400_route_full']['role'], 'primary')
        self.assertEqual(summary['secondary_vs_logistic_learned']['comparator'], 'logistic_learned')
        self.assertEqual({r['variant'] for r in summary['all_variants']}, {v['id'] for v in self.config['v2_variants']})
        self.assertNotIn('meets_saving_target', summary['matched_cost_supplementary'])
        self.assertEqual(set(summary['per_scenario_vs_primary_comparator']), {'stationary', 'process_shift'})
        for key, result in held['classification'].items():
            for metric in ['average_precision', 'recall_among_candidates', 'precision', 'brier', 'ece', 'log_loss']:
                self.assertIn(metric, result['aggregate'], key)
            self.assertEqual(set(result['per_scenario']), {'stationary', 'process_shift'})
        progress = cli.read_json(self.root / 'progress-test.json')
        self.assertEqual(progress['runs'], progress['expected_runs'])
        self.assertTrue(Path(self.reported['path']).exists())
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.root)
        with patch.object(cli, 'PROTOCOL', self.path), self.assertRaises(cli.HarnessError):
            cli.develop(self.root)


if __name__ == '__main__':
    unittest.main()
