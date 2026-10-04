"""Independent v4 auditor on a tiny fixture campaign; never real scientific evidence.

The fixture is built by the frozen v4 CLI (two scenarios, fixture-only seeds 8700+) in a
temporary directory outside the repository. Every tamper test edits a fresh copy, keeps the
ledger byte receipts consistent where relevant (so the deeper recomputation must catch it),
and requires a specific finding plus no audit.json.
"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from inspection_v4 import cli

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / 'scripts'))
import audit_inspection_v4 as auditor  # noqa: E402

PYTHON = sys.executable


def tiny_config():
    """Two scenarios, fixture-only seeds outside every protocol split, small fits."""
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


class V4Audit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        path = cls.base / 'protocol.json'
        cli.write_json(path, tiny_config())
        cls.pristine = cls.base / 'pristine'
        with patch.object(cli, 'PROTOCOL', path):
            cli.develop(cls.pristine)
        cli.freeze(cls.pristine)
        cli.campaign(cls.pristine)
        cls.passed = cls.base / 'passed'
        shutil.copytree(cls.pristine, cls.passed)
        cls.result = auditor.audit(cls.passed, fixture=True)
        cls.copies = 0

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    # ------------------------------------------------------------ helpers

    def fresh(self):
        type(self).copies += 1
        root = self.base / f'c{self.copies}'
        shutil.copytree(self.pristine, root)
        return root

    def fails(self, root, code, **kwargs):
        with self.assertRaises(auditor.AuditError) as caught:
            auditor.audit(root, fixture=True, **kwargs)
        self.assertIn(code, caught.exception.codes, caught.exception.findings[:5])
        self.assertFalse((root / 'audit.json').exists())
        return caught.exception

    def heldout_ledgers(self, root, policy=None):
        held = cli.read_json(root / 'held-out.json')
        variants = {v['id']: v for v in cli.read_json(root / 'frozen-config.json')['v2_variants']}
        for index, run in enumerate(held['runs']):
            if policy is None or variants[run['policy']]['policy'] == policy:
                path = auditor.ledger_path(root, 'test', run['lot_id'], run['policy'], run['mode'], run['budget'])
                yield index, path, cli.read_json(path)

    def rewrite_ledger(self, root, index, path, ledger):
        """Edit a ledger and refresh its byte receipt, so only recomputation can catch the edit."""
        cli.write_json(path, ledger)
        held = cli.read_json(root / 'held-out.json')
        held['runs'][index]['ledger_sha256'] = cli.sha256_file(path)
        cli.write_json(root / 'held-out.json', held)

    def tamper_row(self, edit, policy=None, where=lambda row: True):
        root = self.fresh()
        for index, path, ledger in self.heldout_ledgers(root, policy):
            rows = [r for r in ledger['run']['rows'] if where(r)]
            if rows:
                edit(rows[0], ledger)
                self.rewrite_ledger(root, index, path, ledger)
                return root
        self.fail('fixture has no matching ledger row')

    # ------------------------------------------------------------ pass

    def test_clean_fixture_passes_with_recomputed_receipt(self):
        r = self.result
        self.assertEqual(r['status'], 'passed')
        self.assertEqual(r['scope'], 'fixture_scale')
        self.assertEqual(cli.read_json(self.passed / 'audit.json'), json.loads(json.dumps(r)))
        checks = r['checks']
        self.assertEqual(checks['protected_files_preserved'], 8)
        self.assertEqual(checks['test_runs_replayed'], checks['expected_test_runs'])
        self.assertEqual(checks['test_runs_replayed'], 2 * 10)
        self.assertEqual(checks['development_runs_replayed'], 2 * 10)
        self.assertGreater(checks['route_decisions'], 0)
        self.assertGreater(checks['learned_decisions'], 0)
        self.assertGreater(checks['retries_recomputed'], 0)
        self.assertEqual(checks['decisions_exact_argmax'] + checks['decisions_equal_value_ties'], checks['selected_sites_replayed'])
        self.assertEqual(checks['lots_regenerated'], {'train': 4, 'calibration': 2, 'development': 2, 'test': 2})
        held = cli.read_json(self.passed / 'held-out.json')['summary']['primary_vs_cb400_route_full']
        for key in ('mean_policy', 'mean_comparator', 'ci95_difference', 'relative_gain', 'success'):
            self.assertEqual(r['primary_recomputed'][key], held[key])
        self.assertTrue(any('synthetic' in text for text in r['limitations']))
        self.assertTrue(any('Fixture scale' in text for text in r['limitations']))

    def test_strict_mode_enforces_full_protocol_scale(self):
        root = self.fresh()
        with self.assertRaises(auditor.AuditError) as caught:
            auditor.audit(root)
        self.assertTrue({'protocol_scale', 'protocol_mismatch'} <= caught.exception.codes)
        self.assertFalse((root / 'audit.json').exists())

    def test_cli_prints_failure_json_and_returns_nonzero(self):
        root = self.fresh()
        proc = subprocess.run([PYTHON, str(APP / 'scripts' / 'audit_inspection_v4.py'), str(root)], capture_output=True, text=True, cwd=APP)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out['status'], 'failed')
        self.assertFalse(out['audit_json_written'])
        self.assertIn('protocol_scale', out['codes'])
        self.assertFalse((root / 'audit.json').exists())

    def test_root_inside_repository_is_rejected(self):
        with self.assertRaises(auditor.AuditError) as caught:
            auditor.audit(APP / 'evidence', fixture=True)
        self.assertIn('root_inside_repo', caught.exception.codes)

    # ------------------------------------------------------------ paid rows

    def test_paid_row_probability_tamper(self):
        def edit(row, ledger):
            row['baseline_p'] = row['baseline_p'] * 0.5 + 0.25
        self.fails(self.tamper_row(edit), 'row_probability')

    def test_charged_tamper(self):
        def edit(row, ledger):
            row['charged'] += 1.0
        self.fails(self.tamper_row(edit), 'charged_cost')

    def test_retry_billing_tamper(self):
        def edit(row, ledger):
            row['cost']['retry'] = 4.0
            row['charged'] += 4.0
        self.fails(self.tamper_row(edit, where=lambda r: len(r['attempts']) == 1), 'retry_billing')

    def test_dropped_retry_breaks_sensor_rule(self):
        def edit(row, ledger):
            row['attempts'] = row['attempts'][:1]
            row['cost']['retry'] = 0.0
            row['charged'] -= 4.0
        self.fails(self.tamper_row(edit, where=lambda r: len(r['attempts']) == 2), 'retry_rule')

    def test_attempt_table_truth_tamper(self):
        def edit(row, ledger):
            attempt = row['attempts'][-1]
            attempt['reported_doi'] = not attempt['reported_doi']
            row['label'] = attempt['reported_doi']
            row['reported_positive'] = bool(attempt['reported_doi'])
        error = self.fails(self.tamper_row(edit, where=lambda r: r['attempts'][-1]['status'] == 'ok'), 'sensor_table')
        self.assertIn('reported_label', error.codes)

    def test_route_pair_budget_tamper(self):
        def edit(row, ledger):
            row['components']['pair_reserved_cost'] = 10_000.0
        self.fails(self.tamper_row(edit, 'route_full_gamma', lambda r: r['components']['next_site_index'] is not None), 'route_pair_cost')

    def test_route_next_site_visited_tamper(self):
        def edit(row, ledger):
            previous = ledger['run']['rows'][row['step'] - 2]
            row['components'].update(next_site_index=previous['site_index'], next_site_id=previous['site_id'])
        self.fails(self.tamper_row(edit, 'route_full_gamma', lambda r: r['step'] > 1 and r['components']['next_site_index'] is not None),
                   'route_next_site')

    def test_hidden_truth_key_in_components(self):
        def edit(row, ledger):
            row['components']['doi'] = True
        self.fails(self.tamper_row(edit, 'learned'), 'hidden_truth_key')

    def test_truncated_ledger_is_a_premature_stop(self):
        def edit(row, ledger):
            first = ledger['run']['rows'][0]
            ledger['run']['rows'] = [first]
        root = self.tamper_row(edit, 'learned', lambda r: r['step'] == 1)
        error = self.fails(root, 'premature_stop')
        self.assertIn('metric_mismatch', error.codes)

    # ------------------------------------------------------------ run matrix and summaries

    def test_duplicate_run_identity(self):
        root = self.fresh()
        held = cli.read_json(root / 'held-out.json')
        held['runs'][1] = copy.deepcopy(held['runs'][0])
        cli.write_json(root / 'held-out.json', held)
        error = self.fails(root, 'duplicate_run')
        self.assertIn('missing_run', error.codes)

    def test_asserted_summary_is_not_trusted(self):
        root = self.fresh()
        held = cli.read_json(root / 'held-out.json')
        primary = held['summary']['primary_vs_cb400_route_full']
        primary.update(success=True, reason='relative gain >= target and CI lower bound > 0', relative_gain=0.5)
        cli.write_json(root / 'held-out.json', held)
        self.fails(root, 'summary_mismatch')

    def test_classification_aggregate_is_recomputed(self):
        root = self.fresh()
        held = cli.read_json(root / 'held-out.json')
        key = next(iter(held['classification']))
        held['classification'][key]['aggregate']['brier'] += 0.01
        cli.write_json(root / 'held-out.json', held)
        self.fails(root, 'classification_mismatch')

    # ------------------------------------------------------------ frozen inputs

    def test_protected_file_mismatch(self):
        manifest = cli.read_json(auditor.PROTECTED)
        name = sorted(manifest['protected_sha256'])[0]
        manifest['protected_sha256'][name] = '0' * 64
        path = self.base / 'protected.json'
        cli.write_json(path, manifest)
        with patch.object(auditor, 'PROTECTED', path):
            error = self.fails(self.fresh(), 'protected_hash')
        self.assertEqual(error.findings[0]['file'], name)

    def test_frozen_source_mismatch_with_valid_receipt_digest(self):
        root = self.fresh()
        receipt = cli.read_json(root / 'freeze.json')
        receipt['source_hashes']['inspection_v4/model.py'] = '0' * 64
        receipt['receipt_sha256'] = auditor._digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
        cli.write_json(root / 'freeze.json', receipt)
        error = self.fails(root, 'frozen_source_mismatch')
        self.assertNotIn('freeze_receipt_digest', error.codes)

    def test_receipt_edit_without_digest(self):
        root = self.fresh()
        receipt = cli.read_json(root / 'freeze.json')
        receipt['candidate'] = 'cb400_route_full'
        cli.write_json(root / 'freeze.json', receipt)
        self.fails(root, 'freeze_receipt_digest')

    def test_model_tamper(self):
        root = self.fresh()
        models = cli.read_json(root / 'models.json')
        models['gmm_diag4x3/platt']['class_priors']['positive'] = 0.5
        cli.write_json(root / 'models.json', models)
        error = self.fails(root, 'model_hash')
        self.assertIn('frozen_file_hash', error.codes)

    def test_oracle_rewrite_with_fresh_receipts_fails_regeneration(self):
        root = self.fresh()
        refs = cli.read_json(root / 'test-manifest.json')
        directory = auditor.lot_dir(root, 'test', refs[0]['lot_id'])
        oracle = auditor._load_npz(directory / 'oracle.npz')
        oracle['review_positive'] = ~oracle['review_positive']
        np.savez(directory / 'oracle.npz', **oracle)
        meta = cli.read_json(directory / 'metadata.json')
        meta['oracle_sha256'] = cli.sha256_file(directory / 'oracle.npz')
        cli.write_json(directory / 'metadata.json', meta)
        refs[0]['metadata_sha256'] = cli.sha256_file(directory / 'metadata.json')
        cli.write_json(root / 'test-manifest.json', refs)
        self.fails(root, 'lot_regeneration')


if __name__ == '__main__':
    unittest.main()
