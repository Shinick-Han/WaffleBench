"""Independent read-only audit of a completed inspection route v5 campaign root.

python scripts/audit_inspection_route_v5.py CAMPAIGN_ROOT [--out AUDIT_JSON] [--replay-development]

Reads only the campaign root, the frozen v3 receipt it references and the repository
sources. It never writes into the campaign root, never selects actions for the campaign and
never trusts a stored PASS/FAIL: the freeze receipt, sources (working tree AND the recorded
commit's git blobs), environment, model, config, every lot's bytes and regeneration, every
paid row of every held-out ledger, every sensor attempt, every bill and the primary paired
statistics are recomputed and compared.

Independence. ``inspection_route_v5`` (planner/harness/campaign), the v3 policies/harness and
``inspection_review.reporting`` are NOT imported. ``route_beam3`` and ``route_full_gamma``
decisions are replayed by a reimplementation of INSPECTION_ROUTE_V5_PROTOCOL.md and of the v3
documented pair contract; costs, metrics and the stratified paired bootstrap are recomputed
here. Trusted read-only helpers (scope recorded in the receipt): the frozen v1 generator and
sensor table ``inspection_review.data`` (``generate_lot``, ``review_observation``, ``lot_id``),
the compiled frozen predictor ``inspection_v3.inference.compile_predictor`` cross-checked
against ``inspection_v3.model.predict``, and ``inspection_review.cli`` npz/json readers.

``audit(root)`` returns a structured receipt; on any finding it raises ``AuditError`` with
every finding and nothing is written. Critical checks never use Python ``assert``.
Authored synthetic numeric data only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
import time

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from inspection_review import data  # noqa: E402  trusted frozen generator + sensor table
from inspection_review.cli import _load_npz, read_json, write_json  # noqa: E402  trusted readers
from inspection_v3 import model as v3model  # noqa: E402  trusted frozen model API (reference predict)
from inspection_v3.inference import compile_predictor  # noqa: E402  trusted compiled predictor

PROTECTED = APP / 'evidence' / 'inspection-research' / 'protected-before-v1.json'
PROTOCOL = APP / 'inspection_route_v5' / 'protocol.json'
NAMESPACES = ('inspection_route_v5', 'inspection_review', 'inspection_v2', 'inspection_v3')
REUSED = NAMESPACES[1:]
EXTRA_SOURCES = ('scripts/run_inspection_route_v5.py', 'INSPECTION_ROUTE_V5_PROTOCOL.md', 'INSPECTION_V3_CONTRACT.md',
                 'INSPECTION_V2_CONTRACT.md', 'pyproject.toml', 'uv.lock')
STUDY_FILES = ('protocol.json', 'config.json', 'receipt-ref.json', 'lot-manifest.json', 'development.json')
RECEIPT_FILES = ('freeze.json', 'frozen-config.json', 'models.json')
REQUIRED = STUDY_FILES + ('freeze.json', 'test-started.json', 'test-manifest.json', 'held-out.json')
LOT_FILES = ('metadata.json', 'public.npz', 'oracle.npz')

# Pinned identities of the real campaign (strict mode only).
STRICT = {'freeze_sha256': '7fa2e9dd91ab399efd814237eb707ca09baf44cfa282903dd126132d62eac1c1',
          'source_commit': 'f748064b40ef452fd5cb17a85ff982bdc731108f',
          'environment': {'python': '3.12.10', 'numpy': '2.3.3', 'catboost': '1.2.10'},
          'test_lots': 100, 'development_lots': 40, 'protected': 8}
CANDIDATE, INCUMBENT = 'cb400_route_beam3', 'cb400_route_full'
POLICY_OF = {INCUMBENT: 'route_full_gamma', CANDIDATE: 'route_beam3'}
REASONS = {'route_full_gamma': 'selection_reward_two_step_route_full_gamma',
           'route_beam3': 'selection_reward_three_step_beam_route'}

EPS = 1e-9          # harness admission tolerance
COST_TOL = 1e-9     # CU reconstruction
SCORE_TOL = 1e-12   # relative tolerance for values / protocol tie band
PARITY_TOL = 1e-12  # compiled predictor vs reference model.predict
MAX_FINDINGS = 200

HIDDEN_KEYS = frozenset({'seed', 'scenario', 'oracle', 'doi', 'physical', 'kind', 'electrical_effect',
                         'review_status', 'review_positive', 'review_kind', 'review_quality'})
ROW_KEYS = frozenset({'step', 'site_index', 'site_id', 'wafer', 'original_candidate', 'reason', 'score', 'components',
                      'evidence_refs', 'selection_reward', 'selection_reward_semantics', 'baseline_p', 'online_p',
                      'frozen_negative', 'frozen_confident_negative', 'reserved_cost', 'cost', 'charged',
                      'cumulative_spend', 'attempts', 'status', 'label', 'reported_positive', 'policy_cpu_s',
                      'policy_wall_s', 'model_update_cpu_s', 'model_update_wall_s'})
ATTEMPT_KEYS = frozenset({'attempt', 'status', 'reported_doi', 'reported_kind', 'quality'})
COST_KEYS = ('load', 'stage', 'dwell', 'outside_rescan', 'retry')
COMMON_COMPONENTS = frozenset({'selection_reward', 'reward_source', 'frozen_p', 'site_id', 'budget_source', 'remaining_budget',
                               'single_step_value', 'lookahead_value', 'max_cost', 'budget_for_lookahead'})
ROUTE_COMPONENTS = COMMON_COMPONENTS | {'next_site_index', 'next_site_id', 'next_selection_reward', 'pair_reserved_cost',
                                        'wafer_switch_now', 'wafer_switch_next', 'shortlist_size', 'pairs_evaluated',
                                        'pair_chunks', 'gamma', 'gamma_scope'}
BEAM_COMPONENTS = COMMON_COMPONENTS | {'planned_site_indices', 'planned_site_ids', 'planned_step_reserved_costs',
                                       'planned_rewards', 'planned_reserved_total', 'planned_length', 'beam_counts', 'beam'}
RUN_KEYS = frozenset({'status', 'lot_id', 'scenario', 'policy', 'mode', 'budget', 'metrics', 'ledger_path', 'ledger_sha256'})
NOTE_KEYS = ('first_novel_identification_note', 'true_novel_doi_first_encounter_note', 'confirmed_electrical_potential_note')
UNVERIFIED_METRIC_KEYS = frozenset({'timing', *NOTE_KEYS})
LIMITATIONS = [
    'Read-only posthoc audit of authored numeric synthetic lots (unchanged v1 generator and sensor table); not SEM-image, '
    'factory, yield or physical accuracy evidence.',
    'Trusted helpers: inspection_review.data (generate_lot, review_observation, lot_id) as the frozen generator/sensor '
    'oracle; inspection_v3.inference.compile_predictor and inspection_v3.model.predict for frozen_p (cross-checked against '
    'each other and against the ledger frozen_p_sha256; the model is checked by hash, never refitted); inspection_review.cli '
    'npz/json readers. A defect shared by the frozen generator/sensor and the campaign would not be detected.',
    'route_beam3 and route_full_gamma are replayed by an independent reimplementation of the written protocol and the v3 '
    'pair contract; values within a 1e-12 relative band are protocol ties and resolved by the protocol tie rule.',
    'The bootstrap reproduces the preregistered algorithm (PCG64 seed, per-scenario integer draws, percentile 2.5/97.5); '
    'independence is in code, not in the resampling scheme.',
    'Software CPU/wall timing fields are checked only for internal consistency, never re-measured; they are not equipment CU.',
    'An audit PASS certifies ledger integrity and recomputed statistics; it is not a claim that the hypothesis succeeded.',
]


class AuditError(RuntimeError):
    """Raised when any audit check fails; ``findings`` lists recorded failures (capped)."""

    def __init__(self, findings, total=None):
        self.findings = list(findings)
        self.total = len(self.findings) if total is None else total
        super().__init__(f'{self.total} audit finding(s): {", ".join(sorted(self.codes))}')

    @property
    def codes(self):
        return {f['code'] for f in self.findings}


class Findings:
    def __init__(self):
        self.items, self.counts = [], Counter()

    def check(self, ok, code, detail='', **where):
        if not ok:
            self.counts[code] += 1
            if len(self.items) < MAX_FINDINGS:
                self.items.append({'code': code, 'detail': detail, **where})
        return bool(ok)

    def fatal(self, code, detail='', **where):
        self.check(False, code, detail, **where)
        raise AuditError(self.items, sum(self.counts.values()))

    def raise_if_any(self):
        if self.counts:
            raise AuditError(self.items, sum(self.counts.values()))


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _close(a, b, tol=SCORE_TOL):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


def _same(a, b, tol=COST_TOL):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return a == b and type(a) is type(b)
    return _close(a, b, tol)


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- protected files, sources, receipts

def verify_protected(F):
    manifest = read_json(PROTECTED)['protected_sha256']
    F.check(len(manifest) == STRICT['protected'], 'protected_count', f'{len(manifest)} protected files, expected 8')
    for name, expected in sorted(manifest.items()):
        path = APP / name
        F.check(path.is_file() and _sha_file(path) == expected, 'protected_hash', 'protected file changed or missing', file=name)
    return len(manifest)


def source_names():
    names = [p.relative_to(APP).as_posix() for ns in NAMESPACES for p in sorted((APP / ns).glob('*.py'))]
    names += [f'{ns}/protocol.json' for ns in NAMESPACES] + list(EXTRA_SOURCES)
    return sorted(names)


def _git_blob_sha(commit, name):
    try:
        blob = subprocess.run(['git', '-C', str(APP), 'cat-file', 'blob', f'{commit}:{name}'], capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    return hashlib.sha256(blob).hexdigest()


def verify_freeze(root, frozen, protocol, strict, F):
    """Recompute every frozen field independently, plus working tree and commit-blob source identity."""
    body = {k: v for k, v in frozen.items() if k != 'receipt_sha256_v5'}
    F.check(_digest(body) == frozen.get('receipt_sha256_v5'), 'freeze_digest', 'freeze receipt digest does not match its body')
    F.check(frozen.get('study') == 'route_v5' and frozen.get('schema_version') == 1, 'freeze_identity', 'not a route_v5 freeze')
    F.check(frozen.get('test_generation_has_started') is False, 'freeze_state', 'freeze recorded test generation')
    if strict:
        F.check(frozen.get('receipt_sha256_v5') == STRICT['freeze_sha256'], 'freeze_pinned', 'freeze digest differs from the pinned primary freeze')
        F.check(frozen.get('source_commit') == STRICT['source_commit'], 'source_commit_pinned', 'source commit differs from the pinned commit')
    names = source_names()
    stored = frozen.get('source_hashes') or {}
    F.check(set(stored) == set(names), 'frozen_source_set', f'frozen source set differs: {sorted(set(stored) ^ set(names))}')
    commit = frozen.get('source_commit')
    for name in names:
        F.check(_sha_file(APP / name) == stored.get(name), 'frozen_source_mismatch', 'working-tree source differs from freeze', file=name)
        F.check(_git_blob_sha(commit, name) == stored.get(name), 'frozen_source_commit', 'source at the recorded commit differs from freeze', file=name)
    for name in STUDY_FILES:
        F.check(_sha_file(root / name) == (frozen.get('study_file_hashes') or {}).get(name), 'study_file_hash', 'study file changed', file=name)
    F.check(set(frozen.get('study_file_hashes') or {}) == set(STUDY_FILES), 'study_file_hash', 'unexpected study file set')
    env = {'python': platform.python_version(), 'numpy': np.__version__, 'catboost': _catboost_version()}
    F.check(frozen.get('environment') == env, 'environment', f'auditor environment {env} differs from freeze')
    if strict:
        F.check(env == STRICT['environment'], 'environment', 'environment differs from python 3.12.10 / numpy 2.3.3 / catboost 1.2.10')
    F.check(frozen.get('primary') == protocol['primary'], 'freeze_primary', 'frozen primary endpoint differs from protocol')
    F.check(frozen.get('test_lots') == lot_rows(protocol, 'test'), 'freeze_test_lots', 'frozen test lot list differs from protocol seeds')
    rec = protocol['receipt']
    F.check(frozen.get('receipt_sha256') == rec['receipt_sha256'] and frozen.get('model_hash') == rec['model_hash'],
            'freeze_receipt_ref', 'freeze cites another v3 receipt/model')
    return names


def _catboost_version():
    import importlib.metadata
    return importlib.metadata.version('catboost')


def verify_v3_receipt(root, frozen, protocol, F):
    """Exact v3 receipt, frozen config and model; reused v1-v3 sources equal the v3 receipt's (unchanged incumbent)."""
    ref = read_json(root / 'receipt-ref.json')
    rroot = Path(ref['receipt_root'])
    rec = protocol['receipt']
    F.check(ref['receipt_root'] == frozen.get('receipt_root'), 'receipt_ref', 'freeze and receipt-ref cite different v3 roots')
    for name in RECEIPT_FILES:
        if not F.check((rroot / name).is_file(), 'receipt_missing', 'v3 receipt file missing', file=name):
            F.raise_if_any()
    hashes = {f: _sha_file(rroot / f) for f in RECEIPT_FILES}
    F.check(hashes == ref.get('file_hashes') == frozen.get('receipt_file_hashes'), 'receipt_file_hash', 'v3 receipt file bytes changed')
    receipt = read_json(rroot / 'freeze.json')
    F.check(_digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'}) == receipt.get('receipt_sha256') == rec['receipt_sha256'],
            'receipt_digest', 'v3 receipt digest differs')
    F.check(hashes['frozen-config.json'] == rec['frozen_config_sha256'] == (receipt.get('file_hashes') or {}).get('frozen-config.json')
            and hashes['models.json'] == rec['models_sha256'] == (receipt.get('file_hashes') or {}).get('models.json'),
            'receipt_file_hash', 'v3 frozen-config/models digests differ')
    F.check(receipt.get('candidate') == rec['incumbent_variant'] == INCUMBENT, 'incumbent', 'incumbent is not the frozen v3 candidate')
    F.check(receipt.get('environment') == frozen.get('environment'), 'environment', 'v3 receipt environment differs')
    fitted = read_json(rroot / 'models.json')[rec['model_key']]
    F.check(_digest(fitted) == rec['model_hash'] == (receipt.get('model_hashes') or {}).get(rec['model_key']), 'model_hash', 'frozen model changed')
    F.check(fitted.get('supports_online_update') is False, 'model_not_frozen', 'frozen model allows online updates')
    v3src = receipt.get('source_hashes') or {}
    reused = {k: v for k, v in (frozen.get('source_hashes') or {}).items() if k.split('/')[0] in REUSED}
    for name, value in sorted(reused.items()):
        F.check(v3src.get(name) == value, 'incumbent_source_changed', 'reused source differs from the frozen v3 receipt', file=name)
    v3config = read_json(rroot / 'frozen-config.json')
    expected = json.loads(json.dumps(v3config))
    expected['route_v5'] = {'beam': dict(protocol['beam'])}
    F.check(read_json(root / 'config.json') == expected, 'study_config', 'study config is not the frozen v3 config plus route_v5.beam')
    return fitted, len(reused)


def lot_rows(protocol, split):
    s = protocol['splits'][split]
    return [{'seed': s['base_seed'] + k * 100 + i, 'scenario': name}
            for k, name in enumerate(protocol['scenarios']) for i in range(s['lots_per_scenario'])]


def verify_protocol(root, protocol, strict, F):
    if strict:
        F.check(protocol == read_json(PROTOCOL), 'protocol_mismatch', 'campaign protocol differs from inspection_route_v5/protocol.json')
        F.check(len(lot_rows(protocol, 'test')) == STRICT['test_lots'] and len(lot_rows(protocol, 'development')) == STRICT['development_lots'],
                'protocol_scale', 'lot counts differ from 40 development / 100 test')
    F.check(protocol.get('mode') == 'candidate_only' and protocol.get('budget') == 360, 'protocol_scope', 'mode/budget differ from candidate_only/360')
    F.check([v['id'] for v in protocol['variants']] == [INCUMBENT, CANDIDATE] and all(POLICY_OF[v['id']] == v['policy'] for v in protocol['variants']),
            'protocol_variants', 'variants differ from incumbent route_full_gamma + candidate route_beam3')
    F.check(protocol['beam'] == {'first_size': 16, 'next_size': 16, 'per_wafer': 2, 'depth': 3}, 'protocol_beam', 'beam settings differ')
    p = protocol['primary']
    F.check(p['candidate'] == CANDIDATE and p['comparator'] == INCUMBENT and p['metric'] == 'true_doi_confirmed'
            and p['target_relative_gain'] == 0.05 and (not strict or (p['bootstrap_seed'] == 2026100407 and p['bootstrap_replicates'] == 10000)),
            'protocol_primary', 'primary endpoint differs from the preregistration')
    seeds = [r['seed'] for s in ('development', 'test') for r in lot_rows(protocol, s)]
    F.check(len(seeds) == len(set(seeds)), 'seed_overlap', 'development and test seeds overlap')


def verify_timeline(root, frozen, F):
    started = read_json(root / 'test-started.json')
    held = read_json(root / 'held-out.json')
    manifest = read_json(root / 'test-manifest.json')
    F.check(started.get('freeze_sha256') == frozen.get('receipt_sha256_v5') == held.get('freeze_sha256'), 'test_start_receipt',
            'test start / held-out do not cite this freeze')
    F.check(held.get('kind') == 'held_out_synthetic_route_v5', 'heldout_identity', 'held-out kind differs')
    times = [frozen.get('created_at'), started.get('created_at')] + [m.get('generated_at') for m in manifest] + [held.get('generated_at')]
    try:
        parsed = [datetime.fromisoformat(t) for t in times]
        F.check(parsed[0] <= parsed[1] <= min(parsed[2:-1]) and max(parsed[2:-1]) <= parsed[-1], 'timeline',
                'freeze -> test-started -> test lots -> held-out order violated')
    except (TypeError, ValueError):
        F.check(False, 'timeline', 'timestamps missing or malformed')


# ---------------------------------------------------------------- lots

def _as_stored(arrays, skip=()):
    out = {}
    for key, value in arrays.items():
        if key in skip:
            continue
        a = np.asarray(value)
        out[key] = a.astype(str) if a.dtype == object else a
    return out


def _arrays_equal(a, b):
    if a.keys() != b.keys():
        return False
    for key in a:
        x, y = np.asarray(a[key]), np.asarray(b[key])
        if x.shape != y.shape:
            return False
        if x.dtype.kind == 'f' or y.dtype.kind == 'f':
            if not np.array_equal(x.astype(float), y.astype(float), equal_nan=True):
                return False
        elif not np.array_equal(x, y):
            return False
    return True


def verify_lots(root, split, rows, refs, config, frozen_bytes, F):
    F.check(len(refs) == len(rows), 'lot_manifest', f'{split}: {len(refs)} lots, expected {len(rows)}')
    out = []
    for row, ref in zip(rows, refs):
        seed, scenario = int(row['seed']), row['scenario']
        lot_id = data.lot_id(seed, scenario)
        where = {'split': split, 'lot_id': lot_id}
        if not F.check(ref.get('lot_id') == lot_id and ref.get('seed') == seed and ref.get('scenario') == scenario and ref.get('split') == split,
                       'lot_identity', 'manifest entry is not the expected seed/scenario lot', **where):
            continue
        d = root / 'lots' / split / lot_id
        F.check(Path(ref.get('path', '')).parts[-3:] == ('lots', split, lot_id), 'lot_path', 'manifest path is not canonical', **where)
        if not F.check(all((d / f).is_file() for f in LOT_FILES), 'lot_missing', 'lot file missing', **where):
            continue
        meta = read_json(d / 'metadata.json')
        F.check(_sha_file(d / 'metadata.json') == ref.get('metadata_sha256'), 'lot_receipt', 'metadata receipt differs', **where)
        F.check(meta.get('split') == split and meta.get('seed') == seed and meta.get('scenario') == scenario and meta.get('lot_id') == lot_id,
                'lot_identity', 'metadata identity differs', **where)
        for kind in ('public', 'oracle'):
            F.check(_sha_file(d / f'{kind}.npz') == meta.get(f'{kind}_sha256') == ref.get(f'{kind}_sha256'), 'lot_file_hash',
                    f'{kind}.npz digest differs', **where)
        if frozen_bytes is not None:
            F.check(frozen_bytes.get(lot_id) == {f: _sha_file(d / f) for f in LOT_FILES}, 'frozen_lot_bytes',
                    'development lot bytes differ from the freeze', **where)
        public, oracle = _load_npz(d / 'public.npz'), _load_npz(d / 'oracle.npz')
        F.check(not (set(public) & (HIDDEN_KEYS | set(oracle))), 'hidden_truth_key', 'public lot file carries hidden truth keys', **where)
        regen = data.generate_lot(seed, scenario, config)
        F.check(_arrays_equal(public, _as_stored(regen['public'], ('seed', 'scenario', 'metadata'))), 'lot_regeneration',
                'public arrays differ from the frozen generator', **where)
        F.check(_arrays_equal(oracle, _as_stored(regen['oracle'])), 'lot_regeneration', 'oracle arrays differ from the frozen generator', **where)
        out.append({'lot_id': lot_id, 'seed': seed, 'scenario': scenario, 'dir': d, 'public': public, 'oracle': oracle})
    if frozen_bytes is not None:
        F.check(set(frozen_bytes) == {l['lot_id'] for l in out}, 'frozen_lot_bytes', 'frozen development lot set differs')
    return out


def frozen_probabilities(public, fitted, predictor, F, where):
    features = np.asarray(public['features'], dtype=float)
    p = np.asarray(predictor.predict(features), dtype=float)
    ref = np.asarray(v3model.predict(fitted, features), dtype=float)
    gap = float(np.max(np.abs(p - ref))) if p.size else 0.0
    F.check(p.shape == ref.shape == (len(public['candidate']),) and gap <= PARITY_TOL, 'predictor_parity',
            f'compiled predictor differs from model.predict by {gap!r}', **where)
    F.check(np.isfinite(p).all() and ((p >= 0) & (p <= 1)).all(), 'prediction_invalid', 'frozen_p is not a probability', **where)
    return p


# ---------------------------------------------------------------- independent cost model

class Lot:
    """Public geometry and cost contract for one lot (no hidden truth)."""

    def __init__(self, public, config):
        self.wafer = np.asarray(public['wafer'])
        self.xy = np.asarray(public['xy'], float)
        self.cand = np.asarray(public['candidate'], bool)
        self.ids = [str(s) for s in public['site_ids']]
        self.n = len(self.ids)
        c = config['cost']
        self.load, self.base, self.per = float(c['wafer_load']), float(c['stage_base']), float(c['stage_per_normalized_distance'])
        self.dwell, self.outside, self.retry_dwell = float(c['dwell']), float(c['outside_rescan']), float(c['retry_dwell'])
        self.retry_limit = int(c['retry_limit'])
        self.reserve = self.retry_dwell * self.retry_limit

    def first(self, cur_wafer, cur_xy):
        """Per-site bill parts and full reservation from the current stage position."""
        if cur_wafer is None:
            switch, dist = np.ones(self.n, bool), np.zeros(self.n)
        else:
            switch, dist = self.wafer != cur_wafer, np.linalg.norm(self.xy - cur_xy, axis=1)
        load = np.where(switch, self.load, 0.0)
        stage = self.base + self.per * np.where(switch, 0.0, dist)
        dwell = np.full(self.n, self.dwell)
        outside = np.where(self.cand, 0.0, self.outside)
        return {'load': load, 'stage': stage, 'dwell': dwell, 'outside': outside, 'max': load + stage + dwell + outside + self.reserve}

    def step(self, rows, cols):
        """Reserved cost of each col site with the stage at each row site: load on any wafer change (also a
        return), movement only on the same wafer, dwell, outside rescan, full retry reserve."""
        switch = self.wafer[cols][None, :] != self.wafer[rows][:, None]
        dist = np.linalg.norm(self.xy[cols][None, :, :] - self.xy[rows][:, None, :], axis=2)
        outside = np.where(self.cand[cols], 0.0, self.outside)[None, :]
        return (np.where(switch, self.load, 0.0) + self.base + self.per * np.where(switch, 0.0, dist)
                + self.dwell + outside + self.reserve)


def _top(score, idx, k):
    """idx ordered by score desc, then lower site index; first k."""
    return idx[np.lexsort((idx, -score[idx]))][:k]


def _beam_pick(score, ok, wafer_of_ok, size, per_wafer):
    pick = set(_top(score, ok, size).tolist())
    for w in np.unique(wafer_of_ok):
        pick.update(_top(score, ok[wafer_of_ok == w], per_wafer).tolist())
    return sorted(pick)


def beam_plan(lot, p, max_cost, affordable, allowed, remaining, beam):
    """Protocol replay of route_beam3: every feasible 1/2/3-step path and the protocol choice."""
    single = p / max_cost
    idx = np.flatnonzero(affordable & (max_cost <= remaining + EPS))
    first = _beam_pick(single, idx, lot.wafer[idx], beam['first_size'], beam['per_wafer'])
    cols = np.flatnonzero(allowed)
    level = [((i,), (float(max_cost[i]),)) for i in first]
    levels = [level]
    for _ in range(beam['depth'] - 1):
        if not level:
            break
        nxt = []
        last = np.array([path[-1] for path, _ in level], dtype=int)
        step = lot.step(last, cols)
        for r, (path, costs) in enumerate(level):
            cum = 0.0
            for x in costs:
                cum += x
            feasible = (cum + step[r] <= remaining + EPS)
            for j in path:
                feasible &= cols != j
            ok = np.flatnonzero(feasible)
            if ok.size == 0:
                continue
            score = np.full(cols.size, -np.inf)
            score[ok] = p[cols[ok]] / step[r, ok]
            for k in _beam_pick(score, ok, lot.wafer[cols[ok]], beam['next_size'], beam['per_wafer']):
                nxt.append((path + (int(cols[k]),), costs + (float(step[r, k]),)))
        level = nxt
        levels.append(level)
    scored = []
    for lv in levels:
        for path, costs in lv:
            total, reward = 0.0, 0.0
            for x in costs:
                total += x
            for j in path:
                reward += float(p[j])
            scored.append((reward / total, float(single[path[0]]), path, costs, total))
    if not scored:
        return None
    top = max(s[0] for s in scored)
    band = [s for s in scored if s[0] >= top - SCORE_TOL * abs(top)]
    top_single = max(s[1] for s in band)
    best = min((s for s in band if s[1] == top_single), key=lambda s: (tuple(lot.ids[j] for j in s[2]), -s[0]))
    return {'value': best[0], 'single': best[1], 'path': list(best[2]), 'costs': list(best[3]), 'total': best[4],
            'counts': [len(lv) for lv in levels], 'top': top, 'band': len(band)}


def route_plan(lot, p, max_cost, affordable, allowed, remaining, settings):
    """v3 route_full_gamma (gamma 1) replay: shortlist by single value, best feasible pair value."""
    single = p / max_cost
    idx = np.flatnonzero(affordable)
    short = np.asarray(_beam_pick(single, idx, lot.wafer[idx], settings['shortlist_size'], settings['route_per_wafer']), dtype=int)
    cols = np.flatnonzero(allowed)
    g = settings['gamma']
    c1 = max_cost[short][:, None]
    c2 = lot.step(short, cols)
    total = c1 + c2
    feasible = (cols[None, :] != short[:, None]) & (total <= remaining + EPS)
    with np.errstate(divide='ignore', invalid='ignore'):
        pv = np.where(feasible, (p[short][:, None] + g * p[cols][None, :]) / (c1 + g * c2), -np.inf)
    has = feasible.any(axis=1)
    jpos = np.argmax(pv, axis=1)
    value = np.where(has, pv[np.arange(short.size), jpos], single[short])
    k = int(np.lexsort((short, -single[short], -value))[0])
    step = max(1, settings['chunk_elements'] // max(1, cols.size))
    return {'short': short, 'cols': cols, 'value': value, 'has': has, 'next': np.where(has, cols[jpos], -1),
            'k': k, 'chunks': -(-short.size // step)}


# ---------------------------------------------------------------- ledger replay

def _success(a):
    return a['status'] == 'ok' and a['reported_doi'] is not None


def replay_run(ledger, lot_rec, p, policy, protocol, config, F, where, stats):
    lot = Lot(lot_rec['public'], config)
    oracle = lot_rec['oracle']
    thr = config['model']
    beam = protocol['beam']
    settings = {'shortlist_size': int(config['v2_selection']['shortlist_size']), 'route_per_wafer': int(config['v2_selection']['route_per_wafer']),
                'gamma': float(config['v3_selection']['gamma']), 'chunk_elements': int(config['v3_selection']['chunk_elements'])}
    F.check(settings['gamma'] == 1.0, 'route_gamma', 'incumbent gamma is not 1', **where)
    run = ledger['run']
    budget = float(protocol['budget'])
    visited = np.zeros(lot.n, bool)
    spent, cw, cxy = 0.0, None, None
    out = []
    for step, row in enumerate(run['rows'], 1):
        w = {**where, 'step': step}
        if not F.check(isinstance(row, dict) and set(row) == ROW_KEYS, 'row_schema', 'row keys differ', **w):
            return None
        F.check(row['step'] == step, 'row_schema', 'step numbering differs', **w)
        i = row['site_index']
        if not F.check(_int(i) and 0 <= i < lot.n, 'site_index', 'site index invalid', **w):
            return None
        allowed = ~visited & lot.cand
        cv = lot.first(cw, cxy)
        affordable = allowed & (spent + cv['max'] <= budget + EPS)
        remaining = budget - spent
        F.check(not visited[i], 'duplicate_visit', 'selected site already visited', **w)
        F.check(bool(lot.cand[i]), 'site_not_allowed', 'candidate_only run selected a non-candidate site', **w)
        F.check(bool(affordable[i]), 'admission_budget', 'full reservation did not fit the remaining budget', **w)
        F.check(row['site_id'] == lot.ids[i] and row['wafer'] == int(lot.wafer[i]) and row['original_candidate'] is bool(lot.cand[i]),
                'row_identity', 'site id, wafer or candidate flag differs', **w)
        comp = row['components'] if isinstance(row['components'], dict) else {}
        hidden = (HIDDEN_KEYS & set(comp)) | {k for e in row['evidence_refs'] for k in (e if isinstance(e, dict) else {}) if k in HIDDEN_KEYS}
        F.check(not hidden and row['evidence_refs'] == [], 'hidden_truth_key', 'selection record carries hidden keys or evidence', **w)
        pi = float(p[i])
        F.check(row['baseline_p'] == pi and row['online_p'] == pi and row['selection_reward'] == pi, 'frozen_probability',
                'paid choice probability differs from recomputed frozen_p', **w)
        F.check(row['selection_reward_semantics'] == 'latent_doi_probability', 'reward_semantics', 'reward is not latent frozen_p', **w)
        F.check(row['frozen_negative'] is bool(pi < thr['classification_threshold'])
                and row['frozen_confident_negative'] is bool(pi < thr['confident_negative_threshold']), 'frozen_flags', 'frozen flags differ', **w)
        F.check(row['model_update_cpu_s'] == 0 and row['model_update_wall_s'] == 0, 'online_update', 'model update time on a frozen run', **w)
        F.check(row['reason'] == REASONS[policy], 'decision_reason', 'reason differs from the policy', **w)
        max_i = float(cv['max'][i])
        F.check(_close(row['reserved_cost'], max_i, COST_TOL), 'reserved_cost', 'reservation differs from load+stage+dwell+outside+retry reserve', **w)
        F.check(comp.get('reward_source') == 'frozen_p' and comp.get('selection_reward') == pi and comp.get('frozen_p') == pi
                and comp.get('site_id') == lot.ids[i], 'reward_components', 'reward components differ', **w)
        F.check(comp.get('budget_source') == 'state_remaining_budget' and _close(comp.get('remaining_budget'), remaining, COST_TOL)
                and _close(comp.get('budget_for_lookahead'), remaining, COST_TOL), 'remaining_budget', 'remaining budget differs', **w)
        F.check(_close(comp.get('max_cost'), max_i, COST_TOL), 'reserved_cost', 'component max_cost differs', **w)
        if policy == 'route_beam3':
            check_beam(row, comp, lot, p, cv['max'], affordable, allowed, remaining, beam, i, F, w, stats)
        else:
            check_route(row, comp, lot, p, cv['max'], affordable, allowed, remaining, spent, budget, settings, i, cw, F, w, stats)
        attempts = row['attempts']
        if not F.check(isinstance(attempts, list) and 1 <= len(attempts) <= 1 + lot.retry_limit
                       and all(isinstance(a, dict) and set(a) == ATTEMPT_KEYS for a in attempts), 'attempt_schema', 'attempts malformed', **w):
            return None
        observed = []
        for number, a in enumerate(attempts):
            obs = data.review_observation(oracle, i, number)
            F.check(a['attempt'] == number and all(a[f] == obs[f] for f in ('status', 'reported_doi', 'reported_kind', 'quality')),
                    'sensor_table', 'attempt differs from the authored sensor table', attempt=number, **w)
            observed.append({'attempt': number, **obs})
        need_retry = not _success(observed[0]) and lot.retry_limit >= 1
        F.check(len(attempts) == (2 if need_retry else 1), 'retry_rule', 'retry made iff the first attempt failed/missing', **w)
        if len(attempts) == 1 and need_retry:
            observed.append({'attempt': 1, **data.review_observation(oracle, i, 1)})
        bill = {'load': float(cv['load'][i]), 'stage': float(cv['stage'][i]), 'dwell': float(cv['dwell'][i]),
                'outside_rescan': float(cv['outside'][i]), 'retry': lot.retry_dwell if need_retry else 0.0}
        F.check(isinstance(row['cost'], dict) and set(row['cost']) == set(COST_KEYS), 'cost_schema', 'bill keys differ', **w)
        for key in COST_KEYS:
            F.check(_close((row['cost'] or {}).get(key), bill[key], COST_TOL), 'retry_billing' if key == 'retry' else 'cost_breakdown',
                    f'{key} bill differs', **w)
        charged = sum(bill.values())
        F.check(_close(row['charged'], charged, COST_TOL), 'charged_cost', 'charged CU differs', **w)
        F.check(charged <= max_i + EPS, 'charged_cost', 'charge exceeds reservation', **w)
        spent += charged
        F.check(_close(row['cumulative_spend'], spent, COST_TOL) and spent <= budget + EPS, 'cumulative_spend', 'cumulative spend differs or exceeds budget', **w)
        ok = [o for o in observed if _success(o)]
        label = ok[-1]['reported_doi'] if ok else None
        positive = bool(any(o['reported_doi'] for o in ok))
        F.check(row['label'] == label and row['reported_positive'] is positive and row['status'] == ('ok' if ok else observed[-1]['status']),
                'reported_label', 'label/status differ from the sensor table', **w)
        out.append({'i': i, 'p': pi, 'reason': row['reason'], 'attempts': observed, 'cost': bill, 'cumulative_spend': spent,
                    'label': label, 'reported_positive': positive})
        visited[i] = True
        cw, cxy = lot.wafer[i], lot.xy[i]
    allowed = ~visited & lot.cand
    if not allowed.any():
        stop = 'pool_exhausted'
    else:
        stop = 'none_affordable'
        F.check(not (allowed & (spent + lot.first(cw, cxy)['max'] <= budget + EPS)).any(), 'premature_stop', 'an affordable site remained', **where)
    F.check(run['stop_reason'] == stop, 'stop_reason', 'stop reason differs', **where)
    F.check(_close(run['spent'], spent, COST_TOL) and _close(run['remaining'], budget - spent, COST_TOL) and run['budget'] == budget,
            'run_spend', 'run spend/remaining/budget differ', **where)
    timing = run.get('timing') or {}
    F.check(_close(sum(r['policy_wall_s'] for r in run['rows']), timing.get('policy_wall_s', -1), 1e-9)
            and _close(sum(r['policy_cpu_s'] for r in run['rows']), timing.get('policy_cpu_s', -1), 1e-9), 'timing_consistency',
            'row timing does not sum to run timing', **where)
    return {'rows': out, 'spent': spent, 'remaining': budget - spent, 'budget': budget, 'stop_reason': stop}


def check_beam(row, comp, lot, p, max_cost, affordable, allowed, remaining, beam, i, F, w, stats):
    if not F.check(set(comp) == BEAM_COMPONENTS, 'component_schema', 'beam components differ', **w):
        return
    F.check(comp['beam'] == beam, 'beam_config', 'logged beam settings differ from protocol', **w)
    path, costs, rewards = comp['planned_site_indices'], comp['planned_step_reserved_costs'], comp['planned_rewards']
    ok_shape = (isinstance(path, list) and 1 <= len(path) <= beam['depth'] and all(_int(j) and 0 <= j < lot.n for j in path)
                and isinstance(costs, list) and isinstance(rewards, list) and len(costs) == len(rewards) == len(path)
                and comp['planned_length'] == len(path))
    if not F.check(ok_shape, 'planned_schema', 'planned path malformed', **w):
        return
    # projected plan checked on its own terms: unique, allowed, exact projected costs, fits remaining budget
    F.check(path[0] == i, 'planned_first', 'executed site is not the first planned site', **w)
    F.check(len(set(path)) == len(path), 'planned_duplicate', 'planned path repeats a site', **w)
    F.check(all(bool(allowed[j]) for j in path), 'planned_disallowed', 'planned path contains a visited or non-candidate site', **w)
    F.check(comp['planned_site_ids'] == [lot.ids[j] for j in path], 'planned_ids', 'planned site IDs differ from indices', **w)
    F.check(all(r == float(p[j]) for r, j in zip(rewards, path)), 'planned_reward', 'planned reward differs from frozen_p', **w)
    expect = [float(max_cost[path[0]])] + [float(lot.step(np.array([a]), np.array([b]))[0, 0]) for a, b in zip(path, path[1:])]
    F.check(all(_close(x, y, COST_TOL) for x, y in zip(costs, expect)), 'planned_cost',
            'planned step reservation differs from load/movement/dwell/outside/full retry projection', **w)
    total = sum(expect)
    F.check(_close(comp['planned_reserved_total'], total, COST_TOL), 'planned_total', 'planned reserved total differs', **w)
    F.check(total <= remaining + EPS, 'planned_budget', 'planned path exceeds the remaining budget', **w)
    value = sum(float(p[j]) for j in path) / total
    F.check(_close(row['score'], value) and _close(comp['lookahead_value'], value), 'planned_score', 'score is not summed reward / summed cost', **w)
    F.check(_close(comp['single_step_value'], float(p[i]) / float(max_cost[i])), 'decision_score', 'single-step value differs', **w)
    plan = beam_plan(lot, p, max_cost, affordable, allowed, remaining, beam)
    if not F.check(plan is not None, 'decision_mismatch', 'no feasible beam path exists at this decision', **w):
        return
    F.check(comp['beam_counts'] == plan['counts'], 'beam_counts', f"beam counts {comp['beam_counts']} != replay {plan['counts']}", **w)
    F.check(value >= plan['top'] - SCORE_TOL * abs(plan['top']), 'decision_not_optimal', 'planned value below the best feasible beam path', **w)
    same = plan['path'] == path
    F.check(same, 'decision_mismatch', f'replayed plan {plan["path"]} != logged {path}', **w)
    stats['beam_exact' if same and plan['band'] == 1 else 'beam_tie_band'] += 1
    stats['beam_planned_length_' + str(len(path))] += 1


def check_route(row, comp, lot, p, max_cost, affordable, allowed, remaining, spent, budget, settings, i, cw, F, w, stats):
    if not F.check(set(comp) == ROUTE_COMPONENTS, 'component_schema', 'route_full_gamma components differ', **w):
        return
    plan = route_plan(lot, p, max_cost, affordable, allowed, remaining, settings)
    max_i, pi = float(max_cost[i]), float(p[i])
    F.check(comp['gamma'] == 1.0 and comp['gamma_scope'] == 'reward_and_cost', 'route_gamma', 'route gamma is not full-pair 1', **w)
    F.check(_close(comp['single_step_value'], pi / max_i), 'decision_score', 'single-step value differs', **w)
    F.check(comp['shortlist_size'] == int(plan['short'].size) and comp['pairs_evaluated'] == int(plan['short'].size * plan['cols'].size)
            and comp['pair_chunks'] == plan['chunks'], 'route_shortlist', 'shortlist bookkeeping differs', **w)
    F.check(comp['wafer_switch_now'] is (cw is None or bool(lot.wafer[i] != cw)), 'route_wafer_switch', 'wafer switch flag differs', **w)
    chosen = int(plan['short'][plan['k']])
    F.check(chosen == i, 'decision_mismatch', f'replayed incumbent choice {chosen} != logged {i}', **w)
    pos = np.flatnonzero(plan['short'] == i)
    if not F.check(pos.size == 1, 'decision_not_optimal', 'route choice outside the shortlist', **w):
        return
    k = int(pos[0])
    value = float(plan['value'][k])
    F.check(_close(row['score'], value) and _close(comp['lookahead_value'], value), 'decision_score', 'route pair score differs', **w)
    stats['route_exact'] += 1
    j = comp['next_site_index']
    if not plan['has'][k]:
        F.check(j is None and comp['next_site_id'] is None and _close(comp['pair_reserved_cost'], max_i, COST_TOL), 'route_next_site',
                'second site reported where none fits', **w)
        return
    if not F.check(_int(j) and 0 <= j < lot.n and j == int(plan['next'][k]), 'route_next_site', 'second site differs from replay', **w):
        return
    second = float(lot.step(np.array([i]), np.array([j]))[0, 0])
    F.check(bool(allowed[j]) and j != i and comp['next_site_id'] == lot.ids[j] and comp['next_selection_reward'] == float(p[j])
            and comp['wafer_switch_next'] is bool(lot.wafer[j] != lot.wafer[i]), 'route_next_site', 'second-site components differ', **w)
    F.check(_close(comp['pair_reserved_cost'], max_i + second, COST_TOL) and spent + max_i + second <= budget + EPS, 'route_pair_cost',
            'pair reservation differs or exceeds budget', **w)


# ---------------------------------------------------------------- metrics (oracle, posthoc only)

def _ratio(a, b):
    return None if b == 0 else float(a) / float(b)


def recompute_metrics(rep, oracle, candidate, p, config):
    thr, conf = float(config['model']['classification_threshold']), float(config['model']['confident_negative_threshold'])
    doi = np.asarray(oracle['doi'], bool)
    kind = np.asarray(oracle['kind']).astype(str)
    elec = np.asarray(oracle['electrical_effect'], bool)
    candidate = np.asarray(candidate, bool)
    rows = rep['rows']
    idx = np.array([r['i'] for r in rows], dtype=int)
    positive = np.array([r['reported_positive'] for r in rows], dtype=bool)
    resolved = np.array([r['label'] is not None for r in rows], dtype=bool)
    confirmed = doi[idx] & positive if rows else np.zeros(0, bool)
    conf_idx = idx[confirmed]
    statuses = [a['status'] for r in rows for a in r['attempts']]
    in_cand = candidate[conf_idx]
    per_kind = {}
    for k in sorted(set(kind[doi].tolist())):
        total = int((doi & (kind == k)).sum())
        got = int((kind[conf_idx] == k).sum())
        per_kind[k] = {'confirmed': got, 'total': total, 'capture': _ratio(got, total)}
    novel_ident = novel_enc = None
    for r, tc in zip(rows, confirmed):
        true_novel = bool(doi[r['i']] and kind[r['i']] == 'novel')
        if true_novel and novel_enc is None:
            novel_enc = r['cumulative_spend']
        rep_novel = any(a['status'] == 'ok' and a['reported_doi'] and a['reported_kind'] == 'novel' for a in r['attempts'])
        if tc and true_novel and rep_novel and novel_ident is None:
            novel_ident = r['cumulative_spend']
    visited = np.zeros(len(doi), bool)
    visited[idx] = True
    audits = [r for r in rows if 'audit' in r['reason']]
    cand_doi, all_doi = int((doi & candidate).sum()), int(doi.sum())
    return {
        'review_reported_positives': int(positive.sum()), 'true_doi_confirmed': int(confirmed.sum()),
        'reported_false_positives': int((positive & ~doi[idx]).sum()) if rows else 0,
        'reviewed_true_doi_missed': int((doi[idx] & resolved & ~positive).sum()) if rows else 0,
        'unresolved_sites': int((~resolved).sum()), 'unresolved_true_doi': int((doi[idx] & ~resolved).sum()) if rows else 0,
        'candidate_doi': cand_doi, 'all_doi': all_doi, 'candidate_detection_ceiling': _ratio(cand_doi, all_doi),
        'candidate_capture': _ratio(int(in_cand.sum()), cand_doi), 'confirmed_true_doi_in_candidates': int(in_cand.sum()),
        'confirmed_true_doi_outside_candidates': int((~in_cand).sum()), 'allsite_recall': _ratio(int(confirmed.sum()), all_doi),
        'discovered_frozen_false_negatives': int(((p < thr)[conf_idx] & in_cand).sum()),
        'discovered_frozen_confident_false_negatives': int(((p < conf)[conf_idx] & in_cand).sum()),
        'oracle_full_map_frozen_false_negatives_candidates': int((doi & candidate & (p < thr)).sum()),
        'oracle_full_map_frozen_confident_false_negatives_candidates': int((doi & candidate & (p < conf)).sum()),
        'per_kind_capture': per_kind, 'first_novel_identification_cost': novel_ident,
        'true_novel_doi_first_encounter_cost': novel_enc,
        'true_confirmation_cumulative_spend': [r['cumulative_spend'] for r, tc in zip(rows, confirmed) if tc],
        'confirmed_electrical_potential': int(elec[conf_idx].sum()),
        'audits': len(audits), 'audits_confident_negative': sum(r['p'] < conf for r in audits),
        'audit_true_doi_confirmed': sum(bool(doi[r['i']]) and r['reported_positive'] for r in audits),
        'attempted_sites': len(rows), 'attempts': len(statuses),
        'failures': sum(s in ('failure', 'failed', 'error') for s in statuses),
        'missing': sum(1 for r in rows for a in r['attempts'] if a['status'] == 'ok' and a['reported_doi'] is None)
                   + sum(s not in ('ok', 'failure', 'failed', 'error') for s in statuses),
        'retries': sum(len(r['attempts']) > 1 for r in rows),
        'cost_totals': {k: float(sum(r['cost'][k] for r in rows)) for k in COST_KEYS},
        'spent': rep['spent'], 'remaining': rep['remaining'], 'budget': rep['budget'], 'stop_reason': rep['stop_reason'],
        'unvisited_sites': int((~visited).sum()), 'unvisited_true_doi': int((doi & ~visited).sum()), 'online_updates_enabled': False,
    }


def compare_metrics(stored, recomputed, F, where):
    keys = set(stored) - UNVERIFIED_METRIC_KEYS
    F.check(keys == set(recomputed), 'metric_schema', f'metric keys differ: {sorted(keys ^ set(recomputed))}', **where)
    for key in sorted(keys & set(recomputed)):
        F.check(_same(stored[key], recomputed[key]), 'metric_mismatch', f'{key}: stored {stored[key]!r} != recomputed {recomputed[key]!r}', **where)


# ---------------------------------------------------------------- run matrix

def audit_runs(root, split, lots, records, protocol, config, fitted, F, stats, replay=True):
    variants = {v['id']: v for v in protocol['variants']}
    expected = {(lot['lot_id'], vid) for lot in lots for vid in variants}
    seen = Counter((r.get('lot_id'), r.get('policy')) for r in records)
    for key, count in sorted(seen.items(), key=str):
        F.check(count == 1, 'duplicate_run', f'{split}: run appears {count} times', run=list(key))
        F.check(key in expected, 'unexpected_run', f'{split}: run outside the frozen matrix', run=list(key))
    for key in sorted(expected - set(seen)):
        F.check(False, 'missing_run', f'{split}: expected run missing', run=list(key))
    F.check(len(records) == len(expected), 'run_count', f'{split}: {len(records)} runs, expected {len(expected)}')
    by_lot = defaultdict(list)
    for r in records:
        by_lot[r.get('lot_id')].append(r)
    predictor = compile_predictor(fitted)
    model_hash = protocol['receipt']['model_hash']
    out = []
    for lot in lots:
        wl = {'split': split, 'lot_id': lot['lot_id']}
        p = frozen_probabilities(lot['public'], fitted, predictor, F, wl)
        p_sha = hashlib.sha256(np.ascontiguousarray(p).tobytes()).hexdigest()
        for record in by_lot.get(lot['lot_id'], []):
            if (record.get('lot_id'), record.get('policy')) not in expected or seen[(record.get('lot_id'), record.get('policy'))] != 1:
                continue
            variant = variants[record['policy']]
            where = {**wl, 'variant': variant['id']}
            F.check(set(record) == RUN_KEYS and record['status'] == 'complete' and record['scenario'] == lot['scenario']
                    and record['mode'] == protocol['mode'] and record['budget'] == protocol['budget'], 'run_record', 'run record identity differs', **where)
            path = root / 'ledgers' / split / lot['lot_id'] / f"{variant['id']}.json"
            F.check(Path(record.get('ledger_path', '')).parts[-4:] == path.parts[-4:], 'ledger_path', 'ledger path not canonical', **where)
            if not F.check(path.is_file(), 'ledger_missing', 'ledger file missing', **where):
                continue
            F.check(_sha_file(path) == record.get('ledger_sha256'), 'ledger_hash', 'ledger bytes differ from the run record', **where)
            ledger = read_json(path)
            F.check(ledger.get('metrics') == record.get('metrics'), 'metric_mismatch', 'run record metrics differ from ledger', **where)
            if not replay:
                out.append({'lot_id': lot['lot_id'], 'scenario': lot['scenario'], 'policy': variant['id'], 'metrics': ledger['metrics']})
                continue
            F.check(ledger.get('schema_version') == 1 and ledger.get('study') == 'route_v5' and ledger.get('split') == split
                    and ledger.get('variant') == variant and ledger.get('lot_id') == lot['lot_id'] and ledger.get('mode') == protocol['mode'],
                    'ledger_identity', 'ledger identity differs', **where)
            run = ledger.get('run') or {}
            F.check(ledger.get('model_hash') == model_hash and run.get('online_model_hash') == model_hash, 'model_hash', 'ledger model hash differs', **where)
            F.check(run.get('online_updates_enabled') is False, 'online_update', 'online updates enabled', **where)
            F.check(ledger.get('frozen_p_sha256') == p_sha, 'frozen_probability', 'frozen_p array differs from recomputation', **where)
            F.check(ledger.get('selection_reward_semantics') == 'latent_doi_probability', 'reward_semantics', 'reward semantics differ', **where)
            rep = replay_run(ledger, lot, p, variant['policy'], protocol, config, F, where, stats)
            if rep is None:
                continue
            metrics = recompute_metrics(rep, lot['oracle'], lot['public']['candidate'], p, config)
            compare_metrics(ledger.get('metrics') or {}, metrics, F, where)
            F.check(_same((ledger.get('metrics') or {}).get('timing'), run.get('timing')), 'timing_consistency', 'metric timing differs from run timing', **where)
            stats[f'{split}_runs'] += 1
            stats[f'{split}_rows'] += len(rep['rows'])
            stats[f'{split}_attempts'] += sum(len(r['attempts']) for r in rep['rows'])
            stats[f'{split}_retries'] += sum(len(r['attempts']) > 1 for r in rep['rows'])
            out.append({'lot_id': lot['lot_id'], 'scenario': lot['scenario'], 'policy': variant['id'],
                        'metrics': {**metrics, 'timing': run.get('timing')}})
    return out


# ---------------------------------------------------------------- statistics (independent of inspection_review.reporting)

def stratified_bootstrap(diffs, strata, seed, replicates):
    d = np.asarray(diffs, float)
    s = np.asarray(strata)
    rng = np.random.Generator(np.random.PCG64(seed))
    sums = np.zeros(replicates)
    for name in sorted(set(strata)):
        members = np.flatnonzero(s == name)
        sums += d[members[rng.integers(0, members.size, size=(replicates, members.size))]].sum(axis=1)
    means = sums / d.size
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def primary_stats(records, protocol):
    p = protocol['primary']
    table = defaultdict(dict)
    for r in records:
        table[r['lot_id']][r['policy']] = r
    a, b, strata = [], [], []
    for lot in sorted(table):
        ra, rb = table[lot].get(p['candidate']), table[lot].get(p['comparator'])
        if ra is None or rb is None:
            continue
        a.append(float(ra['metrics'][p['metric']]))
        b.append(float(rb['metrics'][p['metric']]))
        strata.append(ra['scenario'])
    diffs = [x - y for x, y in zip(a, b)]
    if not diffs:
        return {'complete_pairs': 0, 'success': False, 'reason': 'no complete pairs'}
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    ci = stratified_bootstrap(diffs, strata, p['bootstrap_seed'], p['bootstrap_replicates'])
    rel = None if mb == 0 else ma / mb - 1.0
    ok = rel is not None and rel >= p['target_relative_gain'] and ci[0] > 0
    by_scenario = {}
    for s in protocol['scenarios']:
        ds = [d for d, t in zip(diffs, strata) if t == s]
        if ds:
            by_scenario[s] = {'lots': len(ds), 'mean_difference': sum(ds) / len(ds),
                              'candidate_wins': sum(d > 0 for d in ds), 'comparator_wins': sum(d < 0 for d in ds), 'ties': sum(d == 0 for d in ds)}
    return {'policy': p['candidate'], 'comparator': p['comparator'], 'mode': p['mode'], 'budget': p['budget'], 'metric': p['metric'],
            'complete_pairs': len(diffs), 'mean_policy': ma, 'mean_comparator': mb, 'mean_difference': sum(diffs) / len(diffs),
            'ci95_difference': ci, 'relative_gain': rel, 'target_relative_gain': p['target_relative_gain'],
            'relative_gain_ci95_on_comparator_mean': [ci[0] / mb, ci[1] / mb] if mb else None,
            'candidate_wins': sum(d > 0 for d in diffs), 'comparator_wins': sum(d < 0 for d in diffs), 'ties': sum(d == 0 for d in diffs),
            'by_scenario': by_scenario, 'success': bool(ok),
            'reason': 'relative gain >= target and CI lower bound > 0' if ok else
                      'comparator mean is zero; relative gain null' if rel is None else 'target not met'}


def condition_means(records, protocol):
    out = []
    for v in protocol['variants']:
        rows = [r for r in records if r['policy'] == v['id']]
        if not rows:
            continue
        m = lambda f: float(np.mean([f(r['metrics']) for r in rows]))  # noqa: E731
        out.append({'variant': v['id'], 'lots': len(rows), 'mean_true_doi_confirmed': m(lambda x: x['true_doi_confirmed']),
                    'mean_by_scenario': {s: float(np.mean([r['metrics']['true_doi_confirmed'] for r in rows if r['scenario'] == s]))
                                         for s in protocol['scenarios'] if any(r['scenario'] == s for r in rows)},
                    'mean_spent': m(lambda x: x['spent']), 'mean_attempted_sites': m(lambda x: x['attempted_sites']),
                    'mean_loading_cost': m(lambda x: x['cost_totals']['load']), 'mean_stage_cost': m(lambda x: x['cost_totals']['stage']),
                    'secondary_mean_policy_wall_s': m(lambda x: x['timing']['policy_wall_s']),
                    'secondary_mean_policy_cpu_s': m(lambda x: x['timing']['policy_cpu_s'])})
    return out


PRIMARY_FIELDS = ('policy', 'comparator', 'mode', 'budget', 'metric', 'complete_pairs', 'mean_policy', 'mean_comparator',
                  'mean_difference', 'ci95_difference', 'relative_gain', 'success', 'reason')


def verify_summary(summary, records, protocol, F, label):
    computed = primary_stats(records, protocol)
    stored = summary.get('primary') or {}
    for key in PRIMARY_FIELDS:
        F.check(_same(stored.get(key), computed.get(key), SCORE_TOL), 'summary_mismatch',
                f'{label} primary.{key}: stored {stored.get(key)!r} != recomputed {computed.get(key)!r}')
    means = condition_means(records, protocol)
    smeans = summary.get('condition_means') or []
    F.check(len(smeans) == len(means), 'summary_mismatch', f'{label} condition_means count differs')
    for s, m in zip(smeans, means):
        F.check(_same(s, m, SCORE_TOL), 'summary_mismatch', f"{label} condition means differ for {m['variant']}")
    return computed, means


# ---------------------------------------------------------------- entry point

def sanitize(obj, mapping):
    if isinstance(obj, dict):
        return {k: sanitize(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v, mapping) for v in obj]
    if isinstance(obj, str):
        for raw, token in mapping:
            obj = obj.replace(raw, token)
        return obj
    return obj


def audit(root, *, strict=True, replay_development=False):
    """Audit a completed route v5 root without writing to it. ``strict=False`` (tests only) skips the
    pinned freeze/commit/scale identities and protocol equality."""
    cpu0, wall0 = time.process_time(), time.perf_counter()
    root = Path(root).resolve()
    F = Findings()
    if not root.is_dir():
        F.fatal('root_missing', 'campaign root does not exist')
    if _inside(root, APP):
        F.fatal('root_inside_repo', 'campaign roots must live outside the repository')
    for name in REQUIRED:
        F.check((root / name).is_file(), 'file_missing', 'required campaign file missing', file=name)
    F.raise_if_any()
    protocol = read_json(root / 'protocol.json')
    frozen = read_json(root / 'freeze.json')
    protected = verify_protected(F)
    verify_protocol(root, protocol, strict, F)
    sources = verify_freeze(root, frozen, protocol, strict, F)
    fitted, reused = verify_v3_receipt(root, frozen, protocol, F)
    verify_timeline(root, frozen, F)
    F.raise_if_any()  # nothing below is meaningful against an altered freeze

    config = read_json(root / 'config.json')
    dev_refs = read_json(root / 'lot-manifest.json').get('development', [])
    dev_lots = verify_lots(root, 'development', lot_rows(protocol, 'development'), dev_refs, config, frozen['development_lot_bytes'], F)
    test_lots = verify_lots(root, 'test', lot_rows(protocol, 'test'), read_json(root / 'test-manifest.json'), config, None, F)
    F.raise_if_any()

    stats = Counter()
    development = read_json(root / 'development.json')
    dev_records = audit_runs(root, 'development', dev_lots, development.get('runs', []), protocol, config, fitted, F, stats,
                             replay=replay_development)
    held = read_json(root / 'held-out.json')
    test_records = audit_runs(root, 'test', test_lots, held.get('runs', []), protocol, config, fitted, F, stats)
    F.raise_if_any()
    dev_primary, _ = verify_summary(development.get('summary') or {}, dev_records, protocol, F, 'development')
    primary, means = verify_summary(held.get('summary') or {}, test_records, protocol, F, 'held-out')
    F.raise_if_any()

    by = {m['variant']: m for m in means}
    wall_ratio = by[CANDIDATE]['secondary_mean_policy_wall_s'] / by[INCUMBENT]['secondary_mean_policy_wall_s']
    cpu_ratio = (by[CANDIDATE]['secondary_mean_policy_cpu_s'] / by[INCUMBENT]['secondary_mean_policy_cpu_s']
                 if by[INCUMBENT]['secondary_mean_policy_cpu_s'] else None)
    ref = read_json(root / 'receipt-ref.json')
    result = {
        'status': 'passed', 'scope': 'full_protocol' if strict else 'fixture_scale', 'study': 'route_v5',
        'generated_at': datetime.now(timezone.utc).isoformat(), 'root': root.as_posix(), 'v3_receipt_root': ref['receipt_root'],
        'freeze_sha256': frozen['receipt_sha256_v5'], 'source_commit': frozen['source_commit'],
        'candidate': CANDIDATE, 'comparator': INCUMBENT,
        'checks': {'protected_files_preserved': protected, 'frozen_sources_verified_worktree_and_commit': len(sources),
                   'reused_sources_equal_v3_receipt': reused, 'study_files_verified': len(STUDY_FILES),
                   'development_lots_regenerated_and_byte_matched': len(dev_lots), 'test_lots_regenerated': len(test_lots),
                   'test_runs_replayed': stats['test_runs'], 'expected_test_runs': 2 * len(test_lots),
                   'test_paid_rows_replayed': stats['test_rows'], 'test_sensor_attempts_recomputed': stats['test_attempts'],
                   'test_retries_recomputed': stats['test_retries'],
                   'development_runs_replayed': stats['development_runs'],
                   'development_ledgers_hash_checked': len(dev_records),
                   'beam_decisions_exact_unique_best': stats['beam_exact'], 'beam_decisions_in_protocol_tie_band': stats['beam_tie_band'],
                   'beam_planned_length': {k[-1]: v for k, v in sorted(stats.items()) if k.startswith('beam_planned_length_')},
                   'incumbent_decisions_exact': stats['route_exact'],
                   'duplicate_visits': 0, 'disallowed_sites': 0, 'budget_violations': 0, 'hidden_truth_keys': 0, 'online_updates': 0,
                   'predictor_parity_tolerance': PARITY_TOL},
        'primary_recomputed': primary,
        'development_primary_recomputed_not_evidence': dev_primary,
        'condition_means_recomputed': means,
        'secondary_software_timing': {'candidate_mean_policy_wall_s': by[CANDIDATE]['secondary_mean_policy_wall_s'],
                                      'incumbent_mean_policy_wall_s': by[INCUMBENT]['secondary_mean_policy_wall_s'],
                                      'wall_ratio_candidate_over_incumbent': wall_ratio, 'cpu_ratio_candidate_over_incumbent': cpu_ratio,
                                      'note': 'software policy seconds per run on one workstation; not factory time and not equipment CU'},
        'hypothesis_note': 'Audit PASS certifies ledgers and recomputed statistics; primary_recomputed.success is the hypothesis outcome.',
        'audit_script_sha256': _sha_file(Path(__file__)),
        'audit_cpu_s': time.process_time() - cpu0, 'audit_wall_s': time.perf_counter() - wall0,
        'limitations': LIMITATIONS + ([] if replay_development else
                                      ['Development ledgers were hash- and summary-checked from stored metrics only, not replayed row by row.'])
                       + ([] if strict else ['Fixture scale: pinned freeze/commit, protocol equality and 40/100 lot scale not enforced.']),
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('root', type=Path)
    parser.add_argument('--out', type=Path, default=None, help='write the sanitized receipt here (never inside the campaign root)')
    parser.add_argument('--replay-development', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, replay_development=args.replay_development)
        mapping = [(result['root'], '<campaign_root>'), (result['v3_receipt_root'], '<v3_receipt_root>')]
        clean = sanitize(result, mapping)
        if args.out is not None:
            if _inside(args.out, args.root):
                raise ValueError('audit output must not be written inside the campaign root')
            write_json(args.out, clean)
        print(json.dumps(clean, allow_nan=False))
        return 0
    except AuditError as exc:
        print(json.dumps({'status': 'failed', 'finding_count': exc.total, 'codes': sorted(exc.codes), 'findings': exc.findings},
                         default=str))
        return 1
    except Exception as exc:  # malformed inputs are failures, never a PASS
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__, 'detail': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
