"""Posthoc v3 quality diagnostics on a tiny hand-built fixture; never real scientific evidence.

Each test builds its own fixture that mimics a frozen, audited v3 root (receipt over the live sources, passed audit,
byte receipts for lots and ledgers) with two scenarios of twelve sites each. Tamper tests
require a refusal, no quality-diagnostics.json and byte-identical campaign files.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from inspection_review.cli import sha256_file
from inspection_v2.cli import digest
from inspection_v3 import model
from inspection_v3.cli import source_hashes

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / 'scripts'))
import diagnose_inspection_v3 as diag  # noqa: E402

COST = {'dwell': 8.0, 'outside_rescan': 12.0, 'retry_dwell': 4.0, 'retry_limit': 1, 'stage_base': 1.0,
        'stage_per_normalized_distance': 2.0, 'unit': 'synthetic_equipment_cost_unit', 'wafer_load': 8.0}
FEATURES = ['signal', 'size', 'texture', 'design_delta', 'process_context', 'radius', 'layer']
OK_POS = {'attempt': 0, 'status': 'ok', 'reported_doi': True, 'reported_kind': 'bridge', 'quality': 0.9}
OK_NEG = {'attempt': 0, 'status': 'ok', 'reported_doi': False, 'reported_kind': None, 'quality': 0.9}
FAIL = {'attempt': 0, 'status': 'failure', 'reported_doi': None, 'reported_kind': None, 'quality': 0.1}
MISS = {'attempt': 1, 'status': 'missing', 'reported_doi': None, 'reported_kind': None, 'quality': 0.1}


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')


def _tiny_model():
    return {'family': 'logistic', 'calibration': None, 'feature_names': FEATURES, 'mean': [0.0] * 7,
            'scale': [1.0] * 7, 'coef': [3.0, 0, 0, 0, 0, 0, 0], 'intercept': 0.0, 'supports_online_update': False}


# Twelve sites per lot: candidates 0..7, DOI at 0,1,2,3,8,9 (8,9 never optically admitted).
DOI = np.array([1, 1, 1, 1, 0, 0, 0, 0, 1, 1, 0, 0], dtype=bool)
CAND = np.array([1] * 8 + [0] * 4, dtype=bool)
SIGNAL = np.array([2.0, 1.5, 1.0, -0.5, 1.2, -1.0, -2.0, 0.1, 3.0, 0.0, -1.0, -1.0])
# Selected rows: DOI confirmed, DOI sensor-missed, DOI unresolved (failure + missing), non-DOI negative, non-DOI false positive.
ROWS = [(0, [OK_POS]), (1, [OK_NEG]), (2, [FAIL, MISS]), (4, [OK_NEG]), (5, [OK_POS])]


def build_root(base):
    root = Path(base)
    mdl = _tiny_model()
    models = {'cb400/identity': mdl}
    config = {'cost': COST, 'model': {'classification_threshold': 0.5}, 'geometry': {'wafers_per_lot': 1},
              'v2_primary': {'candidate': 'cb400_route_full', 'mode': 'candidate_only', 'budget': 360}}
    _write(root / 'frozen-config.json', config)
    _write(root / 'models.json', models)
    for name in ['review-yield.json', 'development.json', 'lot-manifest.json']:
        _write(root / name, {'fixture': True})
    receipt = {'schema_version': 1, 'study': 'v3', 'source_hashes': source_hashes(), 'candidate': 'cb400_route_full',
               'file_hashes': {f: sha256_file(root / f) for f in ['frozen-config.json', 'models.json', 'review-yield.json', 'development.json', 'lot-manifest.json']},
               'model_hashes': {'cb400/identity': model.hash_model(mdl)}, 'test_lots': 2}
    receipt['receipt_sha256'] = digest(receipt)
    _write(root / 'freeze.json', receipt)
    _write(root / 'campaign-started.json', {'freeze_sha256': receipt['receipt_sha256']})
    refs, runs = [], []
    for k, scenario in enumerate(['stationary', 'process_shift']):
        lot_id = f'lot-fixture{k}'
        lotdir = root / 'lots' / 'test' / lot_id
        lotdir.mkdir(parents=True)
        features = np.zeros((12, 7))
        features[:, 0] = SIGNAL
        np.savez(lotdir / 'public.npz', lot_id=np.array(lot_id), features=features, candidate=CAND,
                 site_ids=np.array([f'{lot_id}:s{i}' for i in range(12)]), wafer=np.zeros(12, dtype=int), xy=np.zeros((12, 2)))
        np.savez(lotdir / 'oracle.npz', doi=DOI)
        _write(lotdir / 'metadata.json', {'split': 'test', 'scenario': scenario, 'public_sha256': sha256_file(lotdir / 'public.npz'),
                                          'oracle_sha256': sha256_file(lotdir / 'oracle.npz')})
        refs.append({'lot_id': lot_id, 'path': lotdir.as_posix(), 'scenario': scenario, 'split': 'test',
                     'metadata_sha256': sha256_file(lotdir / 'metadata.json')})
        p = model.predict(mdl, features)
        rows = [{'site_index': i, 'attempts': copy.deepcopy(a)} for i, a in ROWS]
        ledger = {'variant': {'id': 'cb400_route_full', 'model': 'cb400/identity', 'policy': 'route_full_gamma'},
                  'mode': 'candidate_only', 'model_hash': model.hash_model(mdl),
                  'frozen_p_sha256': hashlib.sha256(p.tobytes()).hexdigest(),
                  'run': {'rows': rows, 'stop_reason': 'none_affordable', 'remaining': 7.5}}
        path = root / 'ledgers' / 'test' / lot_id / 'cb400_route_full-candidate_only-360.json'
        _write(path, ledger)
        metrics = {'true_doi_confirmed': 1, 'attempted_sites': 5, 'reported_false_positives': 1, 'review_reported_positives': 2,
                   'candidate_doi': 4, 'all_doi': 6, 'unresolved_sites': 1,
                   'cost_totals': {'dwell': 40.0, 'load': 8.0, 'stage': 7.0, 'retry': 4.0, 'outside_rescan': 0.0},
                   'timing': {'policy_wall_s': 0.025}}
        runs.append({'lot_id': lot_id, 'ledger_path': path.as_posix(), 'ledger_sha256': sha256_file(path),
                     'mode': 'candidate_only', 'budget': 360, 'scenario': scenario, 'metrics': metrics})
    _write(root / 'test-manifest.json', refs)
    _write(root / 'held-out.json', {'freeze_sha256': receipt['receipt_sha256'], 'runs': runs})
    _write(root / 'audit.json', {'status': 'passed', 'freeze_sha256': receipt['receipt_sha256'], 'runs_checked': 2,
                                 'independent_lots': 2, 'audit_script_sha256': sha256_file(diag.AUDIT_SCRIPT)})
    return root


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(Path(root).rglob('*')) if p.is_file()}


class QualityDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.copies = 0

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fresh(self):
        type(self).copies += 1
        # Receipts carry absolute lot/ledger paths, so each test gets its own freshly built root.
        return build_root(self.base / f'c{self.copies}')

    def refuses(self, root, text):
        before = tree_hashes(root)
        with self.assertRaises(diag.DiagnosticRefusal) as caught:
            diag.diagnose(root)
        self.assertIn(text, str(caught.exception))
        self.assertFalse((root / diag.OUTPUT).exists())
        self.assertEqual(before, tree_hashes(root))

    # ------------------------------------------------------------ end to end

    def test_fixture_counts_conserve_and_only_output_is_added(self):
        root = self.fresh()
        before = tree_hashes(root)
        result = diag.diagnose(root)
        after = tree_hashes(root)
        self.assertEqual(set(after) - set(before), {diag.OUTPUT})
        self.assertEqual({k: v for k, v in after.items() if k != diag.OUTPUT}, before)
        written = json.loads((root / diag.OUTPUT).read_text(encoding='utf-8'))
        self.assertEqual(written['freeze_sha256'], result['freeze_sha256'])
        self.assertFalse(written['replaces_primary'])
        c = result['overall']['counts']
        self.assertEqual((c['dies'], c['latent_doi'], c['candidate_doi'], c['never_optically_admitted_doi']), (24, 12, 8, 4))
        self.assertEqual((c['selected'], c['selected_latent_doi'], c['selected_non_doi']), (10, 6, 4))
        self.assertEqual((c['tp_reported_positive'], c['fp_reported_positive'], c['sensor_missed_doi'], c['unresolved_doi']), (2, 2, 2, 2))
        self.assertEqual(c['missed_candidate_doi'], 2)
        self.assertEqual(c['latent_doi'], c['never_optically_admitted_doi'] + c['missed_candidate_doi'] + c['selected_candidate_doi'])
        self.assertEqual((c['attempts_ok'], c['attempts_failure'], c['attempts_missing'], c['retried_reviews']), (8, 2, 2, 2))
        s = result['per_scenario']['stationary']
        self.assertAlmostEqual(s['optical_recall_ceiling'], 4 / 6)
        self.assertEqual(s['visits']['optimistic_bound'], 38)
        self.assertEqual(s['visits']['mean_per_lot'], 5)
        self.assertEqual(s['policy_timing']['policy_wall_s_total_per_lot_mean'], 0.025)
        self.assertAlmostEqual(s['policy_timing']['derived_mean_wall_s_per_decision'], 0.005)
        self.assertEqual(sum(b['n'] for b in s['calibration_candidates']['bins']), 8)

    # ------------------------------------------------------------ pure parts

    def test_missing_candidate_ceiling_separates_never_admitted_doi(self):
        counts = diag.lot_counts(CAND, DOI, np.full(12, 0.5), [])
        self.assertEqual(counts['never_optically_admitted_doi'], 2)
        self.assertEqual(counts['missed_candidate_doi'], 4)
        self.assertEqual(counts['selected'], 0)
        # Every candidate DOI reviewed and confirmed still leaves the optical ceiling below 1.
        rows = [{'site_index': i, 'attempts': [OK_POS]} for i in range(4)]
        full = diag.lot_counts(CAND, DOI, np.full(12, 0.5), rows)
        self.assertEqual((full['missed_candidate_doi'], full['never_optically_admitted_doi'], full['tp_reported_positive']), (0, 2, 4))

    def test_failed_review_is_unresolved_not_physical_negative(self):
        row = {'site_index': 0, 'attempts': [FAIL, MISS]}
        self.assertEqual(diag.review_outcome(row), 'unresolved')
        for doi in (True, False):
            labels = DOI.copy()
            labels[0] = doi
            c = diag.lot_counts(CAND, labels, np.zeros(12), [row])
            self.assertEqual(c['unresolved'], 1)
            self.assertEqual(c['reported_negative_non_doi'] + c['sensor_missed_doi'], 0)
        retry_ok = {'site_index': 0, 'attempts': [FAIL, {**OK_NEG, 'attempt': 1}]}
        self.assertEqual(diag.review_outcome(retry_ok), 'reported_negative')

    def test_conservation_violation_is_detected(self):
        c = diag.lot_counts(CAND, DOI, np.zeros(12), [{'site_index': 0, 'attempts': [OK_POS]}])
        c['tp_reported_positive'] += 1
        with self.assertRaises(ValueError):
            diag.check_conservation(c)
        with self.assertRaises(ValueError):
            diag.lot_counts(CAND, DOI, np.zeros(12), [{'site_index': 0, 'attempts': [OK_POS]}] * 2)

    def test_visit_bound_holds_full_retry_reserve(self):
        bound = diag.optimistic_visit_bound(360, COST)
        # 17 + 9(n-1) + 4 <= 360  ->  n = 38, charged 350.
        self.assertEqual((bound['max_visits'], bound['charged_at_bound'], bound['retry_reserve_cu']), (38, 350.0, 4.0))
        without_reserve = diag.optimistic_visit_bound(360, {**COST, 'retry_limit': 0})
        self.assertEqual(without_reserve['max_visits'], 39)
        self.assertEqual(diag.optimistic_visit_bound(30, COST)['max_visits'], 2)
        self.assertEqual(diag.optimistic_visit_bound(29.9, COST)['max_visits'], 1)
        self.assertEqual(diag.optimistic_visit_bound(20, COST)['max_visits'], 0)

    def test_calibration_bins_are_fixed_and_closed_at_one(self):
        p = np.array([0.0, 0.2, 0.5, 0.8, 0.95, 0.99, 1.0, 0.1])
        y = np.array([0, 1, 1, 1, 1, 1, 0, 0], dtype=bool)
        table = diag.calibration_table(p, y, np.ones(8, dtype=bool))
        self.assertEqual([b['n'] for b in table['bins']], [2, 1, 1, 1, 3])
        self.assertEqual((table['p_ge_0.99']['n'], table['p_ge_0.99']['latent_doi']), (2, 1))
        self.assertIsNone(diag.calibration_table(p, y, np.zeros(8, dtype=bool))['bins'][0]['mean_p'])

    # ------------------------------------------------------------ refusal, no mutation

    def test_refuses_audit_for_other_freeze(self):
        root = self.fresh()
        audit = json.loads((root / 'audit.json').read_text(encoding='utf-8'))
        audit['freeze_sha256'] = '0' * 64
        _write(root / 'audit.json', audit)
        self.refuses(root, 'different freeze receipt')

    def test_refuses_failed_or_missing_audit(self):
        root = self.fresh()
        audit = json.loads((root / 'audit.json').read_text(encoding='utf-8'))
        _write(root / 'audit.json', {**audit, 'status': 'failed'})
        self.refuses(root, 'audit did not pass')
        (root / 'audit.json').unlink()
        self.refuses(root, 'no audit.json')

    def test_refuses_changed_freeze_or_ledger(self):
        root = self.fresh()
        _write(root / 'models.json', {'cb400/identity': {**_tiny_model(), 'intercept': 1.0}})
        self.refuses(root, 'freeze receipt did not verify')
        root = self.fresh()
        ledger = next((root / 'ledgers').rglob('*.json'))
        ledger.write_text(ledger.read_text(encoding='utf-8').replace('"none_affordable"', '"budget"'), encoding='utf-8')
        self.refuses(root, 'ledger bytes changed')


if __name__ == '__main__':
    unittest.main()
