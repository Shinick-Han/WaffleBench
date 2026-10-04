"""Independent posthoc audit of a completed inspection v4 campaign root.

python scripts/audit_inspection_v4.py CAMPAIGN_ROOT

Reads only the frozen receipt, frozen config/models, saved lot files and saved ledgers. It
never selects actions, never changes frozen results and never trusts a summary's asserted
PASS: every paid choice, admission, sensor attempt, bill, metric, paired CI and the candidate
classification aggregates are recomputed from the saved inputs and compared. The selection
policies and harness are NOT imported; ``learned`` and ``route_full_gamma`` decisions are
replayed by an independent reimplementation of their documented contract. The frozen v1
generator/sensor table (``inspection_review.data``) and the frozen model API
(``inspection_v4.model.predict``, compiled v3 predictor for baselines) are reused as inputs.

``audit(root)`` returns a structured receipt and writes ``<root>/audit.json`` only after
every check passed; on any finding it raises ``AuditError`` (all findings attached) and
writes nothing. The CLI prints JSON and returns nonzero on failure. Critical checks never
use Python ``assert``. Authored synthetic numeric data only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from inspection_review import data  # noqa: E402
from inspection_review.cli import _load_npz, read_json, write_json  # noqa: E402
from inspection_v3.inference import compile_predictor  # noqa: E402
from inspection_v4 import cli as v4cli  # noqa: E402
from inspection_v4 import model as v4model  # noqa: E402

PROTECTED = APP / 'evidence' / 'inspection-research' / 'protected-before-v1.json'
PROTOCOL = APP / 'inspection_v4' / 'protocol.json'
NAMESPACES = ('inspection_review', 'inspection_v2', 'inspection_v3', 'inspection_v4')
PRIOR_PROTOCOLS = ('inspection_review/protocol.json', 'inspection_v2/protocol.json', 'inspection_v3/protocol.json')
SOURCE_DOCS = ('inspection_v4/protocol.json', 'DATA_CONTRACT.md', 'INSPECTION_V2_CONTRACT.md',
               'INSPECTION_V3_CONTRACT.md', 'INSPECTION_V4_CONTRACT.md', 'pyproject.toml', 'uv.lock')
FROZEN_FILES = ('frozen-config.json', 'models.json', 'development.json', 'lot-manifest.json')
REQUIRED_FILES = ('config.json', 'freeze.json', 'campaign-started.json', 'test-manifest.json', 'held-out.json') + FROZEN_FILES
STRICT = {'test_lots': 120, 'variants': 22, 'protected': 8, 'modes': ['candidate_only'], 'budgets': [360]}
POLICIES = ('learned', 'route_full_gamma')

EPS = 1e-9          # harness admission tolerance
COST_TOL = 1e-9     # CU reconstruction
SCORE_TOL = 1e-12   # relative tolerance for scores/values and decision ties
PARITY_TOL = 1e-14  # compiled baseline predictor vs reference model.predict
METRIC_TOL = 1e-9   # classification metric recomputation
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
LEARNED_COMPONENTS = frozenset({'frozen_p', 'max_cost'})
ROUTE_COMPONENTS = frozenset({'selection_reward', 'reward_source', 'frozen_p', 'site_id', 'budget_source', 'remaining_budget',
                              'single_step_value', 'lookahead_value', 'max_cost', 'next_site_index', 'next_site_id',
                              'next_selection_reward', 'pair_reserved_cost', 'wafer_switch_now', 'wafer_switch_next',
                              'shortlist_size', 'pairs_evaluated', 'pair_chunks', 'gamma', 'gamma_scope',
                              'budget_for_lookahead'})
REASONS = {'learned': 'frozen_probability_per_cost', 'route_full_gamma': 'selection_reward_two_step_route_full_gamma'}
LIMITATIONS = [
    'Posthoc audit of authored numeric synthetic lots from the unchanged v1 generator and sensor; not SEM-image, factory or physical accuracy.',
    'The frozen generator/sensor table (inspection_review.data) and frozen model API (inspection_v4.model.predict, compiled v3 '
    'predictor for baselines) are trusted inputs; lots are regenerated and compared, models are checked by hash, not refitted.',
    'learned and route_full_gamma choices are replayed by an independent reimplementation; equal-value ties within a 1e-12 '
    'relative tolerance are accepted.',
    'Software CPU/wall timing fields, risk-coverage curves and the recorded source_commit are not independently verified.',
    'An audit PASS certifies ledger integrity and recomputed statistics; it is not a claim that the hypothesis succeeded.',
]


class AuditError(RuntimeError):
    """Raised when any audit check fails; ``findings`` lists every recorded failure."""

    def __init__(self, findings, total=None):
        self.findings = list(findings)
        self.total = len(self.findings) if total is None else total
        codes = sorted({f['code'] for f in self.findings})
        super().__init__(f'{self.total} audit finding(s): {", ".join(codes)}')

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
    return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


def _same(a, b, tol=METRIC_TOL):
    """Structural equality with float tolerance (lists/dicts recursively)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return a == b
    return _close(a, b, tol)


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- receipts, sources, protected files

def source_hashes(repo=APP):
    names = [p.relative_to(repo).as_posix() for ns in NAMESPACES for p in sorted((repo / ns).glob('*.py'))]
    names += [*PRIOR_PROTOCOLS, *SOURCE_DOCS]
    return {name: _sha_file(repo / name) for name in names}


def verify_protected(F, repo=APP):
    manifest = read_json(PROTECTED)['protected_sha256']
    F.check(len(manifest) == STRICT['protected'], 'protected_count', f'{len(manifest)} protected files, expected 8')
    for name, expected in sorted(manifest.items()):
        path = repo / name
        F.check(path.is_file() and _sha_file(path) == expected, 'protected_hash', 'prior protected file changed or missing', file=name)
    return len(manifest)


def verify_freeze(root, receipt, F):
    body = {k: v for k, v in receipt.items() if k != 'receipt_sha256'}
    F.check(receipt.get('study') == 'v4', 'freeze_receipt_study', 'freeze receipt is not a v4 receipt')
    F.check(_digest(body) == receipt.get('receipt_sha256'), 'freeze_receipt_digest', 'freeze receipt digest does not match its body')
    F.check(receipt.get('test_generation_has_started') is False, 'freeze_receipt_state', 'freeze recorded test generation')
    current, frozen = source_hashes(), receipt.get('source_hashes') or {}
    for name in sorted(set(current) | set(frozen)):
        F.check(current.get(name) == frozen.get(name), 'frozen_source_mismatch', 'current source differs from frozen receipt', file=name)
    for name in FROZEN_FILES:
        F.check(_sha_file(root / name) == (receipt.get('file_hashes') or {}).get(name), 'frozen_file_hash', 'frozen file changed', file=name)
    F.check(set(receipt.get('file_hashes') or {}) == set(FROZEN_FILES), 'frozen_file_hash', 'unexpected frozen file set')
    started = read_json(root / 'campaign-started.json')
    F.check(started.get('freeze_sha256') == receipt.get('receipt_sha256'), 'campaign_start_receipt', 'campaign start does not cite this freeze')
    return len(current)


# ---------------------------------------------------------------- config

def _expected_frozen_config(config):
    expected = read_json(PROTOCOL)
    expected['v4_primary'].update(candidate=config['v4_primary'].get('candidate'),
                                  same_model_comparator=config['v4_primary'].get('same_model_comparator'))
    return expected


def verify_config(root, config, receipt, models, fixture, F):
    try:
        v4cli.check_splits(config)
    except Exception as exc:  # frozen split guard; any failure is a finding
        F.check(False, 'split_guard', f'{type(exc).__name__}: {exc}')
    if not fixture:
        F.check(read_json(root / 'config.json') == read_json(PROTOCOL), 'protocol_mismatch', 'development config differs from inspection_v4/protocol.json')
        F.check(config == _expected_frozen_config(config), 'protocol_mismatch', 'frozen config differs from protocol beyond the frozen candidate')
        F.check(len(config['v2_splits']['test']) == STRICT['test_lots'], 'protocol_scale', f"{len(config['v2_splits']['test'])} test lots, expected 120")
        F.check(len(config['v2_variants']) == STRICT['variants'], 'protocol_scale', f"{len(config['v2_variants'])} variants, expected 22")
        F.check(config['modes'] == STRICT['modes'] and config['budgets'] == STRICT['budgets'], 'protocol_scale', 'modes/budgets differ from candidate_only/360')
    ids = [v['id'] for v in config['v2_variants']]
    F.check(len(ids) == len(set(ids)), 'variant_duplicate', 'duplicate variant ids in frozen config')
    for v in config['v2_variants']:
        F.check(v['policy'] in POLICIES, 'unsupported_policy', 'variant policy cannot be replayed', variant=v['id'])
        F.check(v['model'] in models, 'variant_model_missing', 'variant model absent from models.json', variant=v['id'])
    F.check(set(config['modes']) == {'candidate_only'}, 'unsupported_mode', 'only candidate_only is replayed')
    F.check(float((config.get('v3_selection') or {}).get('gamma', 1.0)) == 1.0, 'route_gamma', 'v4 route_full_gamma must use gamma 1')
    primary = config['v4_primary']
    F.check(primary.get('candidate') == receipt.get('candidate'), 'candidate_mismatch', 'frozen config candidate differs from receipt')
    F.check(primary.get('comparator') == receipt.get('comparator') == 'cb400_route_full', 'comparator_mismatch', 'primary comparator is not cb400_route_full')
    F.check(primary.get('same_model_comparator') == same_model_comparator(config, primary.get('candidate')),
            'candidate_mismatch', 'frozen same-model comparator does not follow from the candidate')


def same_model_comparator(config, candidate):
    variant = next((v for v in config['v2_variants'] if v['id'] == candidate), None)
    if variant is None or variant['policy'] == 'learned':
        return None
    return next((v['id'] for v in config['v2_variants'] if v['model'] == variant['model'] and v['policy'] == 'learned'), None)


def verify_models(models, receipt, F):
    hashes = {key: _digest(m) for key, m in models.items()}
    F.check(hashes == receipt.get('model_hashes'), 'model_hash', 'model hashes differ from freeze receipt')
    for key, m in models.items():
        F.check(m.get('supports_online_update') is False, 'model_not_frozen', 'model allows online updates', model=key)
        F.check(hashes[key] == v4model.hash_model(m), 'model_hash', 'model API hash differs from canonical digest', model=key)
    return hashes


# ---------------------------------------------------------------- lots

def lot_dir(root, split, lot_id):
    return root / 'lots' / split / lot_id


def _stored_arrays(directory, kind):
    return _load_npz(directory / f'{kind}.npz')


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


def verify_split_lots(root, split, rows, refs, config, F):
    """Exact expected lot identities, receipts, hidden-key separation and regeneration from seed."""
    F.check(len(refs) == len(rows), 'lot_manifest', f'{split}: {len(refs)} lots, expected {len(rows)}')
    view = v4cli.generator_config(config)
    out = []
    for row, ref in zip(rows, refs):
        seed, scenario = int(row['seed']), row['scenario']
        lot_id = data.lot_id(seed, scenario)
        where = {'split': split, 'lot_id': lot_id}
        if not F.check(ref.get('lot_id') == lot_id and ref.get('seed') == seed and ref.get('scenario') == scenario,
                       'lot_identity', 'manifest entry is not the expected seed/scenario lot', **where):
            continue
        directory = lot_dir(root, split, lot_id)
        F.check(Path(ref['path']).parts[-3:] == ('lots', split, lot_id), 'lot_path', 'manifest path is not the canonical lot path', **where)
        meta_path = directory / 'metadata.json'
        if not F.check(meta_path.is_file(), 'lot_missing', 'lot metadata missing', **where):
            continue
        meta = read_json(meta_path)
        F.check(_sha_file(meta_path) == ref.get('metadata_sha256'), 'lot_receipt', 'metadata receipt differs', **where)
        F.check(meta.get('split') == split and meta.get('seed') == seed and meta.get('scenario') == scenario and meta.get('lot_id') == lot_id,
                'lot_identity', 'metadata identity differs', **where)
        for kind in ('public', 'oracle'):
            F.check(_sha_file(directory / f'{kind}.npz') == meta.get(f'{kind}_sha256'), 'lot_file_hash', f'{kind}.npz digest differs', **where)
        public, oracle = _stored_arrays(directory, 'public'), _stored_arrays(directory, 'oracle')
        F.check(not (set(public) & (HIDDEN_KEYS | set(oracle))), 'hidden_truth_key', 'public lot file carries hidden truth keys', **where)
        regenerated = data.generate_lot(seed, scenario, view)
        F.check(_arrays_equal(public, _as_stored(regenerated['public'], ('seed', 'scenario', 'metadata'))), 'lot_regeneration',
                'public arrays differ from the frozen generator', **where)
        F.check(_arrays_equal(oracle, _as_stored(regenerated['oracle'])), 'lot_regeneration', 'oracle arrays differ from the frozen generator', **where)
        out.append({'lot_id': lot_id, 'seed': seed, 'scenario': scenario, 'dir': directory, 'meta': meta})
    return out


# ---------------------------------------------------------------- predictions

class Predictions:
    """One public/oracle load and one prediction array per model per lot."""

    def __init__(self, models):
        self.models = models
        self.compiled = {k: compile_predictor(m) for k, m in models.items() if m.get('family') != v4model.FAMILY}

    def predict(self, key, features, F, where):
        fitted = self.models[key]
        reference = np.asarray(v4model.predict(fitted, features), dtype=float)
        if key not in self.compiled:
            return reference
        compiled = np.asarray(self.compiled[key].predict(features), dtype=float)
        gap = float(np.max(np.abs(compiled - reference))) if compiled.size else 0.0
        F.check(compiled.shape == reference.shape and gap <= PARITY_TOL, 'baseline_parity',
                f'compiled baseline differs from model.predict by {gap!r}', model=key, **where)
        return compiled

    def lot(self, directory, F, where):
        public, oracle = _stored_arrays(directory, 'public'), _stored_arrays(directory, 'oracle')
        features = np.asarray(public['features'], dtype=float)
        mask = np.asarray(public['candidate'], dtype=bool)
        p, p_candidates = {}, {}
        for key in self.models:
            p[key] = self.predict(key, features, F, where)
            p_candidates[key] = self.predict(key, features[mask], F, where) if mask.any() else np.zeros(0)
            if not F.check(p[key].shape == (len(mask),) and np.isfinite(p[key]).all() and ((p[key] >= 0) & (p[key] <= 1)).all(),
                           'prediction_invalid', 'prediction is not a finite probability per site', model=key, **where):
                p[key] = np.clip(np.nan_to_num(p[key], nan=0.0), 0.0, 1.0)
        return public, oracle, p, p_candidates


# ---------------------------------------------------------------- independent replay

def _first_costs(public, current_wafer, current_xy, c):
    wafer, xy, cand = public['wafer'], np.asarray(public['xy'], float), np.asarray(public['candidate'], bool)
    n = len(wafer)
    if current_wafer is None:
        switch, dist = np.ones(n, dtype=bool), np.zeros(n)
    else:
        switch, dist = wafer != current_wafer, np.linalg.norm(xy - current_xy, axis=1)
    load = np.where(switch, c['wafer_load'], 0.0)
    stage = c['stage_base'] + c['stage_per_normalized_distance'] * np.where(switch, 0.0, dist)
    dwell = np.full(n, c['dwell'])
    outside = np.where(cand, 0.0, c['outside_rescan'])
    reserve = np.full(n, c['retry_dwell'] * int(c['retry_limit']))
    return {'load': load, 'stage': stage, 'dwell': dwell, 'outside': outside, 'max': load + stage + dwell + outside + reserve}


def _pair_costs(public, rows, cols, c):
    """Reserved cost of a hypothetical second site j after site i: travel on the same wafer,
    wafer_load on any wafer change (including a return), dwell, outside rescan, retry reserve."""
    wafer, xy, cand = public['wafer'], np.asarray(public['xy'], float), np.asarray(public['candidate'], bool)
    switch = wafer[cols][None, :] != wafer[rows][:, None]
    dist = np.linalg.norm(xy[cols][None, :, :] - xy[rows][:, None, :], axis=2)
    outside = np.where(cand[cols], 0.0, c['outside_rescan'])[None, :]
    return (np.where(switch, c['wafer_load'], 0.0) + c['stage_base'] + c['stage_per_normalized_distance'] * np.where(switch, 0.0, dist)
            + c['dwell'] + outside + c['retry_dwell'] * int(c['retry_limit']))


def _ordered(score, idx):
    return idx[np.lexsort((idx, -score[idx]))]


def _route_plan(public, p, max_cost, affordable, allowed, remaining, c, settings):
    single = p / max_cost
    idx = np.flatnonzero(affordable)
    short = list(_ordered(single, idx)[:settings['shortlist_size']])
    for w in np.unique(public['wafer'][idx]):
        short += list(_ordered(single, idx[public['wafer'][idx] == w])[:settings['route_per_wafer']])
    short = np.unique(np.asarray(short, dtype=int))
    cols = np.flatnonzero(allowed)
    g = settings['gamma']
    c1 = max_cost[short][:, None]
    c2 = _pair_costs(public, short, cols, c)
    total = c1 + c2
    feasible = (cols[None, :] != short[:, None]) & (total <= remaining + EPS)
    with np.errstate(divide='ignore', invalid='ignore'):
        pair_value = np.where(feasible, (p[short][:, None] + g * p[cols][None, :]) / (c1 + g * c2), -np.inf)
    has = feasible.any(axis=1)
    best_j = np.argmax(pair_value, axis=1)
    value = np.where(has, pair_value[np.arange(short.size), best_j], single[short])
    k = int(np.lexsort((short, -single[short], -value))[0])
    step = max(1, settings['chunk_elements'] // max(1, cols.size))
    return {'short': short, 'cols': cols, 'single': single, 'value': value, 'has': has, 'best_j': best_j,
            'pair_value': pair_value, 'total': total, 'choice': int(short[k]), 'best_value': float(value[k]),
            'chunks': -(-short.size // step)}


def _success(attempt):
    return attempt['status'] == 'ok' and attempt['reported_doi'] is not None


def replay_run(ledger, public, oracle, p, policy, mode, budget, config, F, where, stats):
    """Rebuild every admission, sensor attempt and bill of one ledger; return recomputed rows."""
    c = {k: float(v) for k, v in config['cost'].items() if k != 'unit'}
    c['retry_limit'] = int(config['cost']['retry_limit'])
    thresholds = config['model']
    settings = {**config['v2_selection'], **config['v3_selection']}
    settings['gamma'] = float(settings['gamma'])
    run = ledger['run']
    rows = run['rows']
    cand = np.asarray(public['candidate'], bool)
    site_ids = [str(s) for s in public['site_ids']]
    n = len(site_ids)
    visited = np.zeros(n, dtype=bool)
    spent, current_wafer, current_xy = 0.0, None, None
    budget = float(budget)
    out = []
    for step, row in enumerate(rows, 1):
        w = {**where, 'step': step}
        if not F.check(set(row) == ROW_KEYS, 'row_schema', f'row keys differ: {sorted(set(row) ^ ROW_KEYS)}', **w):
            return None
        F.check(row['step'] == step, 'row_schema', 'step numbering differs', **w)
        i = row['site_index']
        if not F.check(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n, 'site_index', 'site index invalid', **w):
            return None
        allowed = ~visited & cand if mode == 'candidate_only' else ~visited
        cv = _first_costs(public, current_wafer, current_xy, c)
        affordable = allowed & (spent + cv['max'] <= budget + EPS)
        F.check(not visited[i], 'site_revisited', 'selected site already visited', **w)
        F.check(bool(allowed[i]), 'site_not_allowed', 'selected site is outside the allowed pool', **w)
        F.check(bool(affordable[i]), 'admission_budget', 'full retry reserve did not fit the remaining budget before the sensor', **w)
        F.check(row['site_id'] == site_ids[i] and row['wafer'] == int(public['wafer'][i]) and row['original_candidate'] == bool(cand[i]),
                'row_identity', 'site id, wafer or candidate flag differs', **w)
        hidden = (HIDDEN_KEYS & set(row['components'])) | {k for e in row['evidence_refs'] for k in e if k in HIDDEN_KEYS}
        F.check(not hidden, 'hidden_truth_key', f'selection-time record carries {sorted(hidden)}', **w)
        F.check(row['evidence_refs'] == [], 'row_schema', 'learned/route_full_gamma record no evidence refs', **w)
        pi = float(p[i])
        F.check(row['baseline_p'] == pi and row['online_p'] == pi and row['selection_reward'] == pi, 'row_probability',
                'paid choice probability differs from the recomputed frozen prediction', **w)
        F.check(row['selection_reward_semantics'] == 'latent_doi_probability', 'row_probability', 'reward semantics differ', **w)
        F.check(row['frozen_negative'] == bool(pi < thresholds['classification_threshold'])
                and row['frozen_confident_negative'] == bool(pi < thresholds['confident_negative_threshold']),
                'frozen_flags', 'frozen negative flags differ', **w)
        F.check(row['model_update_cpu_s'] == 0 and row['model_update_wall_s'] == 0, 'model_updated', 'model update time on a frozen run', **w)
        F.check(row['reason'] == REASONS[policy], 'decision_reason', 'decision reason differs from policy', **w)
        comp = row['components']
        max_i = float(cv['max'][i])
        F.check(_close(row['reserved_cost'], max_i, COST_TOL), 'reserved_cost', 'reservation differs from full first-step cost + retry reserve', **w)
        if policy == 'learned':
            F.check(set(comp) == LEARNED_COMPONENTS, 'component_schema', 'learned components differ', **w)
            score = p / cv['max']
            best = float(np.max(np.where(affordable, score, -np.inf)))
            F.check(_close(row['score'], score[i]) and comp.get('frozen_p') == pi and _close(comp.get('max_cost'), max_i, COST_TOL),
                    'decision_score', 'learned score/components differ', **w)
            ok = bool(affordable[i]) and score[i] >= best - SCORE_TOL * max(1.0, abs(best))
            F.check(ok, 'decision_not_optimal', 'learned choice is not the best affordable probability per CU', **w)
            stats['exact_decisions' if int(np.argmax(np.where(affordable, score, -np.inf))) == i else 'tie_decisions'] += 1
        else:
            check_route(row, comp, public, p, cv, affordable, allowed, budget - spent, spent, budget, c, settings, i,
                        current_wafer, F, w, stats)
        # ---- sensor attempts from the exact authored table
        attempts = row['attempts']
        if not F.check(isinstance(attempts, list) and 1 <= len(attempts) <= 1 + c['retry_limit']
                       and all(set(a) == ATTEMPT_KEYS for a in attempts), 'attempt_schema', 'attempt records malformed', **w):
            return None
        observed = []
        for number, attempt in enumerate(attempts):
            obs = data.review_observation(oracle, i, number)
            F.check(attempt['attempt'] == number, 'sensor_sequence', 'attempt numbering differs from the sensor sequence', **w)
            F.check(all(attempt[f] == obs[f] for f in ('status', 'reported_doi', 'reported_kind', 'quality')), 'sensor_table',
                    'attempt differs from the authored review table', attempt=number, **w)
            observed.append({'attempt': number, **obs})
        need_retry = not _success(observed[0]) and c['retry_limit'] >= 1
        F.check(len(attempts) == (2 if need_retry else 1), 'retry_rule', 'retry made iff the first attempt failed or was missing', **w)
        if len(attempts) == 1 and need_retry:
            observed.append({'attempt': 1, **data.review_observation(oracle, i, 1)})
        expected = {'load': float(cv['load'][i]), 'stage': float(cv['stage'][i]), 'dwell': float(cv['dwell'][i]),
                    'outside_rescan': float(cv['outside'][i]), 'retry': c['retry_dwell'] if need_retry else 0.0}
        F.check(set(row['cost']) == set(COST_KEYS), 'cost_schema', 'cost breakdown keys differ', **w)
        for key in COST_KEYS:
            F.check(_close(row['cost'].get(key), expected[key], COST_TOL), 'retry_billing' if key == 'retry' else 'cost_breakdown',
                    f'{key} bill differs', **w)
        charged = sum(expected.values())
        F.check(_close(row['charged'], charged, COST_TOL), 'charged_cost', 'charged CU differs from load/stage/dwell/outside/retry', **w)
        F.check(charged <= max_i + EPS, 'charged_cost', 'charge exceeds reservation', **w)
        spent += charged
        F.check(_close(row['cumulative_spend'], spent, COST_TOL) and spent <= budget + EPS, 'cumulative_spend', 'cumulative spend differs or exceeds budget', **w)
        ok = [o for o in observed if _success(o)]
        label = ok[-1]['reported_doi'] if ok else None
        positive = bool(any(o['reported_doi'] for o in ok))
        F.check(row['label'] == label and row['reported_positive'] == positive, 'reported_label', 'reported label differs from the sensor table', **w)
        F.check(row['status'] == ('ok' if ok else observed[-1]['status']), 'reported_label', 'row status differs', **w)
        out.append({'i': i, 'candidate': bool(cand[i]), 'p': pi, 'reason': row['reason'], 'attempts': observed, 'cost': expected,
                    'cumulative_spend': spent, 'label': label, 'reported_positive': positive})
        visited[i] = True
        current_wafer, current_xy = public['wafer'][i], np.asarray(public['xy'][i], float)
    allowed = ~visited & cand if mode == 'candidate_only' else ~visited
    if not allowed.any():
        stop = 'pool_exhausted'
    else:
        cv = _first_costs(public, current_wafer, current_xy, c)
        stop = 'none_affordable'
        F.check(not (allowed & (spent + cv['max'] <= budget + EPS)).any(), 'premature_stop', 'an affordable allowed site remained', **where)
    F.check(run['stop_reason'] == stop, 'stop_reason', 'stop reason differs', **where)
    F.check(_close(run['spent'], spent, COST_TOL) and _close(run['remaining'], budget - spent, COST_TOL) and run['budget'] == budget,
            'run_spend', 'run spend/remaining/budget differ', **where)
    return {'rows': out, 'spent': spent, 'remaining': budget - spent, 'budget': budget, 'stop_reason': stop}


def check_route(row, comp, public, p, cv, affordable, allowed, remaining, spent, budget, c, settings, i, current_wafer, F, w, stats):
    F.check(set(comp) == ROUTE_COMPONENTS, 'component_schema', 'route_full_gamma components differ', **w)
    if set(comp) != ROUTE_COMPONENTS:
        return
    plan = _route_plan(public, p, cv['max'], affordable, allowed, remaining, c, settings)
    short = plan['short']
    max_i = float(cv['max'][i])
    pi = float(p[i])
    F.check(comp['gamma'] == 1.0 == settings['gamma'] and comp['gamma_scope'] == 'reward_and_cost', 'route_gamma', 'route gamma is not full-pair 1', **w)
    F.check(comp['reward_source'] == 'frozen_p' and comp['selection_reward'] == pi and comp['frozen_p'] == pi
            and comp['site_id'] == str(public['site_ids'][i]), 'row_probability', 'route reward components differ', **w)
    F.check(comp['budget_source'] == 'state_remaining_budget' and _close(comp['remaining_budget'], remaining, COST_TOL)
            and _close(comp['budget_for_lookahead'], remaining, COST_TOL), 'route_remaining_budget', 'route remaining budget differs', **w)
    F.check(_close(comp['max_cost'], max_i, COST_TOL) and _close(comp['single_step_value'], pi / max_i), 'decision_score', 'route single-step value differs', **w)
    F.check(comp['shortlist_size'] == int(short.size) and comp['pairs_evaluated'] == int(short.size * plan['cols'].size)
            and comp['pair_chunks'] == plan['chunks'], 'route_shortlist', 'route shortlist bookkeeping differs', **w)
    switch_now = current_wafer is None or bool(public['wafer'][i] != current_wafer)
    F.check(comp['wafer_switch_now'] == switch_now, 'route_wafer_switch', 'wafer switch flag differs', **w)
    pos = np.flatnonzero(short == i)
    if not F.check(pos.size == 1, 'decision_not_optimal', 'route choice is outside the shortlist', **w):
        return
    k = int(pos[0])
    value = float(plan['value'][k])
    best = plan['best_value']
    F.check(_close(row['score'], value) and _close(comp['lookahead_value'], value), 'decision_score', 'route pair score differs', **w)
    F.check(value >= best - SCORE_TOL * max(1.0, abs(best)), 'decision_not_optimal', 'route choice is not the best shortlisted pair value', **w)
    stats['exact_decisions' if plan['choice'] == i else 'tie_decisions'] += 1
    j = comp['next_site_index']
    if not plan['has'][k]:
        F.check(j is None and comp['next_site_id'] is None and comp['next_selection_reward'] is None and comp['wafer_switch_next'] is None
                and _close(comp['pair_reserved_cost'], max_i, COST_TOL), 'route_next_site', 'route reported a second site where none fits', **w)
        return
    if not F.check(isinstance(j, int) and not isinstance(j, bool) and 0 <= j < len(p), 'route_next_site', 'hypothetical second site invalid', **w):
        return
    F.check(bool(allowed[j]) and j != i, 'route_next_site', 'hypothetical second site is visited, disallowed or the first site', **w)
    F.check(comp['next_site_id'] == str(public['site_ids'][j]) and comp['next_selection_reward'] == float(p[j])
            and comp['wafer_switch_next'] == bool(public['wafer'][j] != public['wafer'][i]), 'route_next_site', 'second-site components differ', **w)
    second = float(_pair_costs(public, np.array([i]), np.array([j]), c)[0, 0])
    pair_cost = max_i + second
    F.check(_close(comp['pair_reserved_cost'], pair_cost, COST_TOL), 'route_pair_cost', 'pair reserved cost differs from travel + wafer return + reserve', **w)
    F.check(spent + pair_cost <= budget + EPS, 'route_pair_budget', 'hypothetical pair does not fit the remaining budget', **w)
    g = settings['gamma']
    pair_value = (pi + g * float(p[j])) / (max_i + g * second)
    F.check(_close(pair_value, value), 'route_pair_cost', 'pair value of the reported second site differs from the score', **w)


# ---------------------------------------------------------------- metrics (independent oracle, posthoc only)

def _ratio(a, b):
    return None if b == 0 else float(a) / float(b)


def recompute_metrics(replayed, oracle, candidate, p, config):
    thr, conf = float(config['model']['classification_threshold']), float(config['model']['confident_negative_threshold'])
    doi = np.asarray(oracle['doi'], bool)
    kind = np.asarray(oracle['kind']).astype(str)
    elec = np.asarray(oracle['electrical_effect'], bool)
    candidate = np.asarray(candidate, bool)
    rows = replayed['rows']
    idx = np.array([r['i'] for r in rows], dtype=int)
    positive = np.array([r['reported_positive'] for r in rows], dtype=bool)
    resolved = np.array([r['label'] is not None for r in rows], dtype=bool)
    confirmed = doi[idx] & positive if rows else np.zeros(0, bool)
    conf_idx = idx[confirmed]
    statuses = [a['status'] for r in rows for a in r['attempts']]
    in_cand = candidate[conf_idx]
    frozen_neg, frozen_conf = p < thr, p < conf
    per_kind = {}
    for k in sorted(set(kind[doi].tolist())):
        total = int((doi & (kind == k)).sum())
        got = int((kind[conf_idx] == k).sum())
        per_kind[k] = {'confirmed': got, 'total': total, 'capture': _ratio(got, total)}
    novel_ident = novel_encounter = None
    for r, tc in zip(rows, confirmed):
        true_novel = bool(doi[r['i']] and kind[r['i']] == 'novel')
        if true_novel and novel_encounter is None:
            novel_encounter = r['cumulative_spend']
        reported_novel = any(a['status'] == 'ok' and a['reported_doi'] and a['reported_kind'] == 'novel' for a in r['attempts'])
        if tc and true_novel and reported_novel and novel_ident is None:
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
        'discovered_frozen_false_negatives': int((frozen_neg[conf_idx] & in_cand).sum()),
        'discovered_frozen_confident_false_negatives': int((frozen_conf[conf_idx] & in_cand).sum()),
        'oracle_full_map_frozen_false_negatives_candidates': int((doi & candidate & frozen_neg).sum()),
        'oracle_full_map_frozen_confident_false_negatives_candidates': int((doi & candidate & frozen_conf).sum()),
        'per_kind_capture': per_kind, 'first_novel_identification_cost': novel_ident,
        'true_novel_doi_first_encounter_cost': novel_encounter,
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
        'spent': replayed['spent'], 'remaining': replayed['remaining'], 'budget': replayed['budget'],
        'stop_reason': replayed['stop_reason'], 'unvisited_sites': int((~visited).sum()),
        'unvisited_true_doi': int((doi & ~visited).sum()), 'online_updates_enabled': False,
    }


NOTE_KEYS = ('first_novel_identification_note', 'true_novel_doi_first_encounter_note', 'confirmed_electrical_potential_note')
UNVERIFIED_METRIC_KEYS = frozenset({'timing', *NOTE_KEYS})


def compare_metrics(stored, recomputed, F, where):
    keys = set(stored) - UNVERIFIED_METRIC_KEYS
    F.check(keys == set(recomputed), 'metric_schema', f'metric keys differ: {sorted(keys ^ set(recomputed))}', **where)
    for key in sorted(keys & set(recomputed)):
        F.check(_same(stored[key], recomputed[key], COST_TOL), 'metric_mismatch', f'{key}: stored {stored[key]!r} != recomputed {recomputed[key]!r}', **where)


# ---------------------------------------------------------------- statistics (independent of inspection_review.reporting)

def _mean(xs):
    return float(np.mean(xs)) if xs else None


def bootstrap_ci(diffs, strata, seed, replicates):
    if not diffs:
        return None
    d = np.asarray(diffs, float)
    rng = np.random.Generator(np.random.PCG64(seed))
    total = np.zeros(replicates)
    for s in sorted(set(strata)):
        group = np.flatnonzero(np.asarray(strata) == s)
        total += d[group[rng.integers(0, group.size, size=(replicates, group.size))]].sum(axis=1)
    means = total / d.size
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def paired(table, a, b, primary_cfg, *, target=None, primary=False):
    metric = primary_cfg['metric']
    lots = sorted(table)
    da, db, strata = [], [], []
    for lot in lots:
        ra, rb = table[lot].get(a), table[lot].get(b)
        if ra is None or rb is None:
            continue
        da.append(float(ra['metrics'][metric]))
        db.append(float(rb['metrics'][metric]))
        strata.append(ra['scenario'])
    ma, mb = _mean(da), _mean(db)
    diff = [x - y for x, y in zip(da, db)]
    ci = bootstrap_ci(diff, strata, primary_cfg['bootstrap_seed'], primary_cfg['bootstrap_replicates'])
    rel = None if ma is None or not mb else ma / mb - 1.0
    out = {'policy': a, 'comparator': b, 'complete_pairs': len(diff), 'mean_policy': ma, 'mean_comparator': mb,
           'mean_difference': _mean(diff), 'ci95_difference': ci, 'relative_gain': rel}
    if primary:
        ok = rel is not None and rel >= target and ci is not None and ci[0] > 0
        out['success'] = bool(ok)
        out['reason'] = ('relative gain >= target and CI lower bound > 0' if ok else 'no complete pairs' if not diff else
                         'comparator mean is zero; relative gain null' if rel is None else 'target not met')
    return out


def matched_cost(table, a, b, target):
    both = only_a = only_b = neither = total = 0
    pairs = []
    for lot in sorted(table):
        ra, rb = table[lot].get(a), table[lot].get(b)
        if ra is None or rb is None:
            continue
        total += 1
        sa, sb = ra['metrics']['true_confirmation_cumulative_spend'], rb['metrics']['true_confirmation_cumulative_spend']
        ca = sa[target - 1] if len(sa) >= target else None
        cb = sb[target - 1] if len(sb) >= target else None
        if ca is not None and cb is not None:
            both += 1
            pairs.append((ca, cb))
        elif ca is not None:
            only_a += 1
        elif cb is not None:
            only_b += 1
        else:
            neither += 1
    mean_a, mean_b = _mean([x for x, _ in pairs]), _mean([y for _, y in pairs])
    return {'lots': total, 'both_attained': both, 'only_policy_attained': only_a, 'only_comparator_attained': only_b,
            'neither_attained': neither, 'mean_cost_policy_common': mean_a, 'mean_cost_comparator_common': mean_b,
            'saving_of_means_common': None if not pairs or not mean_b else 1.0 - mean_a / mean_b,
            'mean_paired_saving_common': _mean([1.0 - x / y for x, y in pairs if y > 0])}


def variant_means(records, config):
    primary = config['v4_primary']
    out = {}
    for v in config['v2_variants']:
        rows = [r for r in records if r['policy'] == v['id'] and r['mode'] == primary['mode'] and r['budget'] == primary['budget']]
        if rows:
            out[v['id']] = {'lots': len(rows), 'mean_doi': float(np.mean([r['metrics'][primary['metric']] for r in rows])),
                            'mean_spent_cu': float(np.mean([r['metrics']['spent'] for r in rows])),
                            'mean_loading_cost_cu': float(np.mean([r['metrics']['cost_totals']['load'] for r in rows])),
                            'mean_stage_cost_cu': float(np.mean([r['metrics']['cost_totals']['stage'] for r in rows]))}
    return out


def select_candidate(means, config, models):
    variants = {v['id']: v for v in config['v2_variants']}
    pool = [(-m['mean_doi'], m['mean_spent_cu'], vid) for vid, m in means.items()
            if variants[vid]['role'] == config['v4_primary']['candidate_role'] and models[variants[vid]['model']].get('family') == v4model.FAMILY]
    return min(pool)[2] if pool else None


def verify_summary(summary, records, config, candidate, F):
    primary = config['v4_primary']
    table = defaultdict(dict)
    for r in records:
        if r['mode'] == primary['mode'] and r['budget'] == primary['budget']:
            table[r['lot_id']][r['policy']] = r
    fields = ('policy', 'comparator', 'complete_pairs', 'mean_policy', 'mean_comparator', 'mean_difference', 'ci95_difference', 'relative_gain')
    F.check(summary.get('candidate') == candidate, 'summary_mismatch', 'summary candidate differs from frozen candidate')
    computed = {'primary_vs_cb400_route_full': paired(table, candidate, primary['comparator'], primary, target=primary['target_relative_gain'], primary=True),
                'secondary_vs_logistic_learned': paired(table, candidate, 'logistic_learned', primary)}
    same = same_model_comparator(config, candidate)
    if same:
        computed['secondary_vs_same_model_learned'] = paired(table, candidate, same, primary)
    else:
        F.check((summary.get('secondary_vs_same_model_learned') or {}).get('comparator') is None, 'summary_mismatch', 'unexpected same-model comparator')
    for name, value in computed.items():
        stored = summary.get(name) or {}
        keys = fields + (('success', 'reason') if name.startswith('primary') else ())
        for key in keys:
            F.check(_same(stored.get(key), value[key], SCORE_TOL), 'summary_mismatch', f'{name}.{key}: stored {stored.get(key)!r} != recomputed {value[key]!r}')
        F.check(stored.get('metric') == primary['metric'] and stored.get('mode') == primary['mode'] and stored.get('budget') == primary['budget'],
                'summary_mismatch', f'{name} scope differs')
    for scenario in config['v4_design']['scenarios']:
        def mean(variant):
            values = [r['metrics'][primary['metric']] for lot in table.values() for r in [lot.get(variant)] if r and r['scenario'] == scenario]
            return float(np.mean(values)) if values else None
        a, b = mean(candidate), mean(primary['comparator'])
        expected = {'mean_candidate': a, 'mean_comparator': b, 'candidate_below_comparator': None if a is None or b is None else bool(a < b)}
        F.check(_same((summary.get('per_scenario_vs_primary_comparator') or {}).get(scenario), expected, SCORE_TOL),
                'summary_mismatch', f'per-scenario comparison differs for {scenario}')
    means = variant_means(records, config)
    stored_means = {r['variant']: r for r in summary.get('all_variants') or []}
    F.check(set(stored_means) == set(means), 'summary_mismatch', 'all_variants does not list every variant exactly once')
    for vid, m in means.items():
        for key, value in m.items():
            F.check(_same((stored_means.get(vid) or {}).get(key), value, SCORE_TOL), 'summary_mismatch', f'all_variants {vid}.{key} differs')
    cost = config['v4_matched_cost']
    matched = matched_cost(table, candidate, primary['comparator'], cost['target_true_doi'])
    stored = summary.get('matched_cost_supplementary') or {}
    for key, value in matched.items():
        F.check(_same(stored.get(key), value, SCORE_TOL), 'summary_mismatch', f'matched_cost_supplementary.{key} differs')
    F.check('meets_saving_target' not in stored and stored.get('role') == 'supplementary', 'summary_mismatch', 'matched cost carries a saving claim')
    return computed, means


# ---------------------------------------------------------------- classification (candidate sites, posthoc)

def classification_metrics(y, p, threshold, bins):
    y = np.asarray(y, bool)
    p = np.asarray(p, float)
    n, positives = len(y), int(y.sum())
    pred = p >= threshold
    tp, fp, fn, tn = int((pred & y).sum()), int((pred & ~y).sum()), int((~pred & y).sum()), int((~pred & ~y).sum())
    ap = None
    if positives:
        order = np.argsort(-p, kind='mergesort')
        ys, ps = y[order], p[order]
        hits = np.cumsum(ys)
        ends = np.r_[ps[1:] != ps[:-1], True]
        recall, precision = hits[ends] / positives, hits[ends] / (np.flatnonzero(ends) + 1)
        ap = float(np.sum(np.diff(np.r_[0.0, recall]) * precision))
    ece = None
    if n:
        bucket = np.minimum((p * bins).astype(int), bins - 1)
        ece = float(sum((bucket == b).sum() * abs(float(p[bucket == b].mean()) - float(y[bucket == b].mean()))
                        for b in range(bins) if (bucket == b).any()) / n)
    clipped = np.clip(p, 1e-12, 1.0 - 1e-12)
    t = np.log(clipped) - np.log1p(-clipped)
    return {'n_candidates': n, 'n_positive': positives, 'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
            'precision': _ratio(tp, tp + fp), 'recall_among_candidates': _ratio(tp, tp + fn), 'average_precision': ap,
            'brier': None if n == 0 else float(np.mean((p - y) ** 2)), 'ece': ece,
            'log_loss': None if n == 0 else float(np.mean(np.logaddexp(0.0, t) - y * t))}


def verify_classification(stored, collected, models, hashes, config, F):
    settings = config['v4_evaluation']
    threshold, bins = float(settings['threshold']), int(settings['ece_bins'])
    F.check(set(stored) == set(models), 'classification_mismatch', 'classification does not cover every model exactly once')
    for key in models:
        ys = np.concatenate(collected[key]['y']) if collected[key]['y'] else np.zeros(0, bool)
        ps = np.concatenate(collected[key]['p']) if collected[key]['p'] else np.zeros(0)
        ss = np.concatenate(collected[key]['s']) if collected[key]['s'] else np.zeros(0, dtype=object)
        entry = stored.get(key) or {}
        F.check(entry.get('model_hash') == hashes[key] and entry.get('family') == models[key].get('family'), 'classification_mismatch',
                'classification model identity differs', model=key)
        parts = {'aggregate': classification_metrics(ys, ps, threshold, bins)}
        parts.update({f'per_scenario.{s}': classification_metrics(ys[ss == s], ps[ss == s], threshold, bins) for s in config['v4_design']['scenarios']})
        for name, metrics in parts.items():
            got = entry.get('aggregate') if name == 'aggregate' else (entry.get('per_scenario') or {}).get(name.split('.', 1)[1])
            for metric, value in metrics.items():
                F.check(_same((got or {}).get(metric), value, METRIC_TOL), 'classification_mismatch',
                        f'{name}.{metric}: stored {(got or {}).get(metric)!r} != recomputed {value!r}', model=key)


# ---------------------------------------------------------------- run matrix

def ledger_path(root, split, lot_id, variant, mode, budget):
    return root / 'ledgers' / split / lot_id / f'{variant}-{mode}-{budget}.json'


def audit_runs(root, split, lots, records, config, models, hashes, predictions, F, stats, collect=None):
    """Exact run identities, then per-lot replay of every ledger with one load/prediction per lot."""
    variants = {v['id']: v for v in config['v2_variants']}
    expected = {(lot['lot_id'], v['id'], mode, budget) for lot in lots for v in config['v2_variants'] for mode in config['modes'] for budget in config['budgets']}
    seen = Counter((r.get('lot_id'), r.get('policy'), r.get('mode'), r.get('budget')) for r in records)
    for key, count in sorted(seen.items(), key=str):
        F.check(count == 1, 'duplicate_run', f'{split}: run identity appears {count} times', run=list(key))
        F.check(key in expected, 'unexpected_run', f'{split}: run identity outside the frozen matrix', run=list(key))
    for key in sorted(expected - set(seen)):
        F.check(False, 'missing_run', f'{split}: expected run identity missing', run=list(key))
    F.check(len(records) == len(expected), 'run_count', f'{split}: {len(records)} runs, expected {len(expected)}')
    by_lot = defaultdict(list)
    for record in records:
        by_lot[record.get('lot_id')].append(record)
    recomputed = []
    for lot in lots:
        where_lot = {'split': split, 'lot_id': lot['lot_id']}
        public, oracle, p, p_candidates = predictions.lot(lot['dir'], F, where_lot)
        if collect is not None:
            mask = np.asarray(public['candidate'], bool)
            y = np.asarray(oracle['doi'], bool)[mask]
            for key in models:
                collect[key]['y'].append(y)
                collect[key]['p'].append(p_candidates[key])
                collect[key]['s'].append(np.full(int(mask.sum()), lot['scenario'], dtype=object))
        p_sha = {key: hashlib.sha256(np.ascontiguousarray(p[key]).tobytes()).hexdigest() for key in p}
        for record in by_lot.get(lot['lot_id'], []):
            identity = (record.get('lot_id'), record.get('policy'), record.get('mode'), record.get('budget'))
            if identity not in expected or seen[identity] != 1:
                continue
            variant = variants[record['policy']]
            where = {**where_lot, 'variant': variant['id'], 'mode': record['mode'], 'budget': record['budget']}
            path = ledger_path(root, split, lot['lot_id'], variant['id'], record['mode'], record['budget'])
            F.check(Path(record.get('ledger_path', '')).parts[-4:] == path.parts[-4:], 'ledger_path', 'ledger path is not canonical', **where)
            if not F.check(path.is_file(), 'ledger_missing', 'ledger file missing', **where):
                continue
            F.check(_sha_file(path) == record.get('ledger_sha256'), 'ledger_hash', 'ledger bytes differ from the run record', **where)
            ledger = read_json(path)
            key = variant['model']
            F.check(record.get('status') == 'complete' and record.get('scenario') == lot['scenario'] and record.get('role') == variant['role']
                    and record.get('model_hash') == hashes[key], 'run_record', 'run record identity differs', **where)
            F.check(ledger.get('schema_version') == 1 and ledger.get('study') == 'v4' and ledger.get('split') == split
                    and ledger.get('variant') == variant and ledger.get('lot_id') == lot['lot_id'] and ledger.get('mode') == record['mode'],
                    'ledger_identity', 'ledger identity differs', **where)
            F.check(ledger.get('model_hash') == hashes[key] and ledger['run'].get('online_model_hash') == hashes[key],
                    'model_hash', 'ledger model hash differs from the frozen model', **where)
            F.check(ledger['run'].get('online_updates_enabled') is False, 'model_updated', 'online updates enabled', **where)
            F.check(ledger.get('frozen_p_sha256') == p_sha[key], 'prediction_hash', 'frozen prediction array differs from recomputation', **where)
            F.check(ledger.get('selection_reward_semantics') == 'latent_doi_probability', 'row_probability', 'reward semantics differ', **where)
            replayed = replay_run(ledger, public, oracle, p[key], variant['policy'], record['mode'], record['budget'], config, F, where, stats)
            if replayed is None:
                continue
            metrics = recompute_metrics(replayed, oracle, public['candidate'], p[key], config)
            compare_metrics(ledger.get('metrics') or {}, metrics, F, where)
            F.check(ledger.get('metrics') == record.get('metrics'), 'metric_mismatch', 'run record metrics differ from ledger metrics', **where)
            stats['runs'] += 1
            stats['rows'] += len(replayed['rows'])
            stats['attempts'] += sum(len(r['attempts']) for r in replayed['rows'])
            stats['retries'] += sum(len(r['attempts']) > 1 for r in replayed['rows'])
            stats['route_rows' if variant['policy'] == 'route_full_gamma' else 'learned_rows'] += len(replayed['rows'])
            recomputed.append({'lot_id': lot['lot_id'], 'scenario': lot['scenario'], 'policy': variant['id'], 'mode': record['mode'],
                               'budget': record['budget'], 'metrics': metrics})
    return recomputed


# ---------------------------------------------------------------- entry point

def audit(root, *, fixture=False, write_receipt=True):
    """Audit a completed v4 campaign root. ``fixture=True`` (tests only) derives the run matrix from
    the frozen config instead of enforcing the full 120 x 22 protocol scale and protocol equality."""
    cpu0, wall0 = time.process_time(), time.perf_counter()
    root = Path(root).resolve()
    F = Findings()
    if not root.is_dir():
        F.fatal('root_missing', 'campaign root does not exist', root=root.as_posix())
    if _inside(root, APP):
        F.fatal('root_inside_repo', 'runtime campaign roots must live outside the repository', root=root.as_posix())
    for name in REQUIRED_FILES:
        F.check((root / name).is_file(), 'file_missing', 'required campaign file missing', file=name)
    F.raise_if_any()
    stale = (root / 'audit.json').is_file()
    receipt = read_json(root / 'freeze.json')
    config = read_json(root / 'frozen-config.json')
    models = read_json(root / 'models.json')
    protected = verify_protected(F)
    sources = verify_freeze(root, receipt, F)
    verify_config(root, config, receipt, models, fixture, F)
    hashes = verify_models(models, receipt, F)
    F.raise_if_any()  # nothing below is meaningful against an unfrozen or altered setup

    manifest = read_json(root / 'lot-manifest.json')
    F.check(set(manifest) == {'train', 'calibration', 'development'}, 'lot_manifest', 'development manifest splits differ')
    receipts = {split: {r['lot_id']: r['metadata_sha256'] for r in items} for split, items in manifest.items()}
    F.check(receipts == receipt.get('lot_receipts'), 'lot_receipt', 'lot receipts differ from freeze receipt')
    lots = {split: verify_split_lots(root, split, config['v2_splits'][split], manifest.get(split, []), config, F)
            for split in ('train', 'calibration', 'development')}
    test_refs = read_json(root / 'test-manifest.json')
    lots['test'] = verify_split_lots(root, 'test', config['v2_splits']['test'], test_refs, config, F)
    F.check(len(lots['test']) == receipt.get('test_lots') == len(config['v2_splits']['test']), 'lot_manifest', 'test lot count differs from receipt')
    F.raise_if_any()

    predictions = Predictions(models)
    stats = Counter()
    development = read_json(root / 'development.json')
    dev_records = audit_runs(root, 'development', lots['development'], development.get('runs', []), config, models, hashes, predictions, F, stats)
    dev_means = variant_means(dev_records, config)
    reproduced = select_candidate(dev_means, config, models)
    F.check(reproduced == receipt.get('candidate') == development.get('selected_candidate'), 'candidate_selection',
            f'development selection reproduces {reproduced!r}, receipt has {receipt.get("candidate")!r}')
    heldout = read_json(root / 'held-out.json')
    F.check(heldout.get('kind') == 'held_out_synthetic_v4' and heldout.get('freeze_sha256') == receipt.get('receipt_sha256'),
            'heldout_identity', 'held-out result does not cite this freeze')
    collect = {key: {'y': [], 'p': [], 's': []} for key in models}
    test_records = audit_runs(root, 'test', lots['test'], heldout.get('runs', []), config, models, hashes, predictions, F, stats, collect)
    F.raise_if_any()  # statistics are only recomputed from a complete, verified matrix

    candidate = receipt['candidate']
    computed, means = verify_summary(heldout.get('summary') or {}, test_records, config, candidate, F)
    verify_classification(heldout.get('classification') or {}, collect, models, hashes, config, F)
    F.raise_if_any()

    result = {
        'status': 'passed', 'scope': 'fixture_scale' if fixture else 'full_protocol', 'study': 'v4',
        'generated_at': datetime.now(timezone.utc).isoformat(), 'root': root.as_posix(),
        'freeze_sha256': receipt['receipt_sha256'], 'source_commit_recorded': receipt.get('source_commit'),
        'candidate': candidate, 'comparator': config['v4_primary']['comparator'],
        'checks': {'protected_files_preserved': protected, 'frozen_sources_verified': sources, 'models_verified': len(models),
                   'lots_regenerated': {k: len(v) for k, v in lots.items()}, 'variants': len(config['v2_variants']),
                   'development_runs_replayed': len(dev_records), 'test_runs_replayed': len(test_records),
                   'expected_test_runs': len(lots['test']) * len(config['v2_variants']) * len(config['modes']) * len(config['budgets']),
                   'selected_sites_replayed': stats['rows'], 'learned_decisions': stats['learned_rows'], 'route_decisions': stats['route_rows'],
                   'decisions_exact_argmax': stats['exact_decisions'], 'decisions_equal_value_ties': stats['tie_decisions'],
                   'sensor_attempts_recomputed': stats['attempts'], 'retries_recomputed': stats['retries'],
                   'duplicate_or_missing_runs': 0, 'budget_violations': 0, 'hidden_truth_keys': 0, 'online_updates': 0,
                   'baseline_parity_tolerance': PARITY_TOL, 'classification_models_recomputed': len(models)},
        'development_selection_reproduced': reproduced,
        'primary_recomputed': computed['primary_vs_cb400_route_full'],
        'secondary_recomputed': {k: v for k, v in computed.items() if k != 'primary_vs_cb400_route_full'},
        'variant_means_recomputed': means,
        'hypothesis_note': 'Audit PASS certifies ledgers and recomputed statistics; primary_recomputed.success is the hypothesis outcome.',
        'audit_script_sha256': _sha_file(Path(__file__)),
        'audit_cpu_s': time.process_time() - cpu0, 'audit_wall_s': time.perf_counter() - wall0,
        'stale_audit_replaced': stale,
        'limitations': LIMITATIONS + (['Fixture scale: the 120 x 22 protocol matrix and protocol equality were not enforced.'] if fixture else []),
    }
    if write_receipt:
        write_json(root / 'audit.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('root', type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root)
        print(json.dumps(result, allow_nan=False))
        return 0
    except AuditError as exc:
        stale = (args.root / 'audit.json').is_file()
        print(json.dumps({'status': 'failed', 'root': args.root.as_posix(), 'finding_count': exc.total,
                          'codes': sorted(exc.codes), 'findings': exc.findings, 'audit_json_written': False,
                          'stale_audit_json_present': stale}, default=str))
        return 1
    except Exception as exc:  # malformed inputs are failures, never a PASS
        print(json.dumps({'status': 'failed', 'root': args.root.as_posix(), 'error': type(exc).__name__, 'detail': str(exc),
                          'audit_json_written': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
