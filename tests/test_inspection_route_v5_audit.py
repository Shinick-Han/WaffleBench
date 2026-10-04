"""Independent route v5 auditor on a tiny fixture campaign; never real scientific evidence.

The fixture is built by the frozen route v5 stages (two scenarios, one development and one
test lot each, fixture-only seeds 13900+/14900+, 200 bootstrap replicates) in a temporary
directory outside the repository, from the real frozen v3 receipt (skipped when absent).
Every tamper test edits a fresh copy, re-signs the ledger byte receipt where relevant (so the
deeper recomputation must catch it) and requires a specific finding code.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from inspection_review.cli import read_json, write_json
from inspection_review.reporting import stratified_paired_bootstrap
from inspection_route_v5 import campaign

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / 'scripts'))
import audit_inspection_route_v5 as auditor  # noqa: E402


def tiny_protocol():
    protocol = read_json(campaign.PROTOCOL)
    protocol['study_id'] = 'inspection-route-v5-audit-fixture'
    protocol['scenarios'] = ['stationary', 'process_shift']
    protocol['splits'] = {'development': {'base_seed': 13900, 'lots_per_scenario': 1},
                          'test': {'base_seed': 14900, 'lots_per_scenario': 1}}
    protocol['primary']['bootstrap_replicates'] = 200
    return protocol


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RouteV5Audit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        receipt = Path(read_json(campaign.PROTOCOL)['receipt']['default_root'])
        if not (receipt / 'freeze.json').is_file():
            raise unittest.SkipTest('frozen v3 receipt not present on this machine')
        if campaign.git('status', '--porcelain', '--', *campaign.source_names()):
            raise unittest.SkipTest('frozen sources are not committed; the fixture freeze would refuse')
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        path = cls.base / 'protocol.json'
        write_json(path, tiny_protocol())
        cls.pristine = cls.base / 'pristine'
        with patch.object(campaign, 'PROTOCOL', path):
            campaign.build(cls.pristine)
            campaign.develop(cls.pristine)
            campaign.freeze(cls.pristine)
        campaign.test(cls.pristine)
        before = {p: sha(p) for p in cls.pristine.rglob('*') if p.is_file()}
        cls.result = auditor.audit(cls.pristine, strict=False, replay_development=True)
        cls.untouched = before == {p: sha(p) for p in cls.pristine.rglob('*') if p.is_file()}
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

    def fails(self, root, code):
        with self.assertRaises(auditor.AuditError) as caught:
            auditor.audit(root, strict=False)
        self.assertIn(code, caught.exception.codes, caught.exception.findings[:5])
        return caught.exception

    def ledger(self, root, variant, lot_index=0):
        held = read_json(root / 'held-out.json')
        record = [r for r in held['runs'] if r['policy'] == variant][lot_index]
        path = root / 'ledgers' / 'test' / record['lot_id'] / f'{variant}.json'
        return path, read_json(path)

    def resign(self, root, path, ledger):
        """Write a tampered ledger and update its run record so only deep recomputation can catch it."""
        write_json(path, ledger)
        held = read_json(root / 'held-out.json')
        for r in held['runs']:
            if Path(r['ledger_path']).parts[-2:] == path.parts[-2:]:
                r['ledger_sha256'] = sha(path)
                r['metrics'] = ledger['metrics']
        write_json(root / 'held-out.json', held)

    def beam_row(self, ledger, min_length=1):
        return next(r for r in ledger['run']['rows'] if r['components']['planned_length'] >= min_length)

    # ------------------------------------------------------------ pass

    def test_pristine_passes_and_is_read_only(self):
        r = self.result
        self.assertEqual(r['status'], 'passed')
        self.assertTrue(self.untouched, 'audit changed a file in the campaign root')
        self.assertFalse((self.pristine / 'audit.json').exists())
        c = r['checks']
        self.assertEqual(c['test_runs_replayed'], 4)
        self.assertEqual(c['development_runs_replayed'], 4)
        self.assertEqual(c['protected_files_preserved'], 8)
        self.assertGreater(c['test_paid_rows_replayed'], 40)
        self.assertEqual(c['beam_decisions_exact_unique_best'] + c['beam_decisions_in_protocol_tie_band']
                         + c['incumbent_decisions_exact'], c['test_paid_rows_replayed'] + self._dev_rows())
        held = read_json(self.pristine / 'held-out.json')['summary']['primary']
        for key in ('mean_policy', 'mean_comparator', 'mean_difference', 'relative_gain', 'success'):
            self.assertEqual(held[key], r['primary_recomputed'][key])

    def _dev_rows(self):
        dev = read_json(self.pristine / 'development.json')['runs']
        return sum(read_json(Path(self.pristine, 'ledgers', 'development', d['lot_id'], d['policy'] + '.json'))['metrics']['attempted_sites']
                   for d in dev)

    def test_bootstrap_matches_preregistered_scheme(self):
        rng = np.random.default_rng(5)
        diffs = rng.integers(-3, 4, size=37).astype(float).tolist()
        strata = [['a', 'b', 'c'][i % 3] for i in range(37)]
        self.assertEqual(auditor.stratified_bootstrap(diffs, strata, 2026100407, 500),
                         stratified_paired_bootstrap(diffs, strata, 2026100407, 500))

    # ------------------------------------------------------------ ledger tampering

    def test_tampered_charge_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        row = ledger['run']['rows'][2]
        row['cost']['stage'] += 0.5
        row['charged'] += 0.5
        self.resign(root, path, ledger)
        self.fails(root, 'cost_breakdown')

    def test_tampered_reservation_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_full')
        ledger['run']['rows'][1]['reserved_cost'] -= 4.0
        self.resign(root, path, ledger)
        self.fails(root, 'reserved_cost')

    def test_skipped_retry_charge_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        row = ledger['run']['rows'][0]
        row['cost']['retry'] = 4.0 - row['cost']['retry']
        self.resign(root, path, ledger)
        self.fails(root, 'retry_billing')

    def test_planned_duplicate_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        comp = self.beam_row(ledger, 2)['components']
        comp['planned_site_indices'][1] = comp['planned_site_indices'][0]
        comp['planned_site_ids'][1] = comp['planned_site_ids'][0]
        self.resign(root, path, ledger)
        self.fails(root, 'planned_duplicate')

    def test_planned_cost_and_total_fail(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        comp = self.beam_row(ledger, 2)['components']
        comp['planned_step_reserved_costs'][1] -= 8.0  # drop a wafer load / understate movement
        comp['planned_reserved_total'] -= 8.0
        self.resign(root, path, ledger)
        exc = self.fails(root, 'planned_cost')
        self.assertIn('planned_total', exc.codes)

    def test_planned_reward_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        comp = self.beam_row(ledger, 2)['components']
        comp['planned_rewards'][-1] = min(1.0, comp['planned_rewards'][-1] + 0.01)
        self.resign(root, path, ledger)
        self.fails(root, 'planned_reward')

    def test_tampered_frozen_probability_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_full')
        row = ledger['run']['rows'][0]
        row['selection_reward'] = row['baseline_p'] = row['online_p'] = row['baseline_p'] * 0.5
        self.resign(root, path, ledger)
        self.fails(root, 'frozen_probability')

    def test_tampered_beam_counts_and_sensor_fail(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        ledger['run']['rows'][3]['components']['beam_counts'][-1] -= 1
        attempt = ledger['run']['rows'][4]['attempts'][0]
        attempt['reported_doi'] = not attempt['reported_doi'] if attempt['reported_doi'] is not None else True
        self.resign(root, path, ledger)
        exc = self.fails(root, 'beam_counts')
        self.assertIn('sensor_table', exc.codes)

    def test_duplicate_visit_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        rows = ledger['run']['rows']
        rows.insert(2, json.loads(json.dumps(rows[1])))
        for k, r in enumerate(rows, 1):
            r['step'] = k
        self.resign(root, path, ledger)
        self.fails(root, 'duplicate_visit')

    def test_tampered_metric_fails(self):
        root = self.fresh()
        path, ledger = self.ledger(root, 'cb400_route_beam3')
        ledger['metrics']['true_doi_confirmed'] += 1
        self.resign(root, path, ledger)
        self.fails(root, 'metric_mismatch')

    def test_missing_run_fails(self):
        root = self.fresh()
        held = read_json(root / 'held-out.json')
        held['runs'] = held['runs'][1:]
        write_json(root / 'held-out.json', held)
        self.fails(root, 'missing_run')

    # ------------------------------------------------------------ receipt / summary tampering

    def test_tampered_freeze_fails(self):
        root = self.fresh()
        frozen = read_json(root / 'freeze.json')
        frozen['model_hash'] = '0' * 64
        write_json(root / 'freeze.json', frozen)
        self.fails(root, 'freeze_digest')

    def test_resigned_freeze_with_wrong_lot_bytes_fails(self):
        root = self.fresh()
        frozen = read_json(root / 'freeze.json')
        lot = sorted(frozen['development_lot_bytes'])[0]
        frozen['development_lot_bytes'][lot]['oracle.npz'] = '0' * 64
        frozen['receipt_sha256_v5'] = auditor._digest({k: v for k, v in frozen.items() if k != 'receipt_sha256_v5'})
        write_json(root / 'freeze.json', frozen)
        for name in ('test-started.json', 'held-out.json'):
            doc = read_json(root / name)
            doc['freeze_sha256'] = frozen['receipt_sha256_v5']
            write_json(root / name, doc)
        self.fails(root, 'frozen_lot_bytes')

    def test_strict_mode_rejects_fixture_freeze(self):
        with self.assertRaises(auditor.AuditError) as caught:
            auditor.audit(self.pristine, strict=True)
        self.assertIn('freeze_pinned', caught.exception.codes)

    def test_tampered_summary_mean_and_ci_fail(self):
        for field, change in (('mean_difference', lambda x: x + 0.1), ('ci95_difference', lambda x: [x[0] + 0.5, x[1]]),
                              ('success', lambda x: not x)):
            with self.subTest(field=field):
                root = self.fresh()
                held = read_json(root / 'held-out.json')
                held['summary']['primary'][field] = change(held['summary']['primary'][field])
                write_json(root / 'held-out.json', held)
                self.fails(root, 'summary_mismatch')

    def test_tampered_test_start_marker_fails(self):
        root = self.fresh()
        started = read_json(root / 'test-started.json')
        started['freeze_sha256'] = '1' * 64
        write_json(root / 'test-started.json', started)
        self.fails(root, 'test_start_receipt')

    def test_changed_protected_hash_fails(self):
        manifest = read_json(auditor.PROTECTED)
        name = sorted(manifest['protected_sha256'])[0]
        manifest['protected_sha256'][name] = 'f' * 64
        path = self.base / 'protected.json'
        write_json(path, manifest)
        with patch.object(auditor, 'PROTECTED', path):
            with self.assertRaises(auditor.AuditError) as caught:
                auditor.audit(self.pristine, strict=False)
        self.assertIn('protected_hash', caught.exception.codes)

    def test_cli_refuses_output_inside_root_and_writes_nothing(self):
        out = self.pristine / 'audit.json'
        proc = subprocess.run([sys.executable, str(APP / 'scripts' / 'audit_inspection_route_v5.py'), str(self.pristine),
                               '--out', str(out)], capture_output=True, text=True, cwd=APP)
        self.assertNotEqual(proc.returncode, 0)  # strict CLI fails on the fixture freeze before any write
        self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
