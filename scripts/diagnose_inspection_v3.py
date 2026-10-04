"""Posthoc quality diagnostics for the frozen v3 cb400_route_full candidate-only 360-CU test ledgers.

Explains why a 3,915-die lot receives only ~36 paid SEM reviews and where the remaining errors
come from. Evaluation only: the privileged oracle is read after the campaign to label outcomes;
it never selects, scores, replays or changes any action. Nothing is tuned and no campaign, API
or sensor call is made. The only file written is <root>/quality-diagnostics.json, and only after
the freeze receipt, the matching passed audit and every ledger/lot byte receipt verify.

Authored synthetic tabular evidence; not SEM-image accuracy, factory throughput or wafer yield.
Does not replace the preregistered primary comparison.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
from inspection_review.cli import HarnessError, _load_npz, load_public, read_json, sha256_file  # noqa: E402
from inspection_v2.cli import verify_lot  # noqa: E402
from inspection_v3 import model  # noqa: E402
from inspection_v3.cli import verify_freeze  # noqa: E402

CANDIDATE = 'cb400_route_full'
MODE = 'candidate_only'
BUDGET = 360
BIN_EDGES = (0.0, 0.2, 0.5, 0.8, 0.95, 1.0)
HIGH_P = 0.99
OUTPUT = 'quality-diagnostics.json'
AUDIT_SCRIPT = APP / 'scripts' / 'audit_inspection_v3.py'


class DiagnosticRefusal(Exception):
    """Inputs are not the frozen, audited v3 campaign; nothing is written."""


def _refuse(condition, message):
    if not condition:
        raise DiagnosticRefusal(message)


def _sha_array(values):
    return hashlib.sha256(np.ascontiguousarray(values).tobytes()).hexdigest()


# ---------------------------------------------------------------- receipts

def verify_inputs(root):
    """Freeze receipt + matching passed audit + campaign shape. Raises DiagnosticRefusal."""
    root = Path(root)
    try:
        receipt = verify_freeze(root)
    except (HarnessError, OSError, KeyError, ValueError) as exc:
        raise DiagnosticRefusal('freeze receipt did not verify: ' + str(exc)) from exc
    sha = receipt['receipt_sha256']
    _refuse((root / 'campaign-started.json').exists(), 'campaign never started')
    _refuse(read_json(root / 'campaign-started.json').get('freeze_sha256') == sha, 'campaign start receipt mismatch')
    _refuse((root / 'audit.json').exists(), 'no audit.json; run scripts/audit_inspection_v3.py first')
    audit = read_json(root / 'audit.json')
    _refuse(audit.get('status') == 'passed', 'audit did not pass')
    _refuse(audit.get('freeze_sha256') == sha, 'audit belongs to a different freeze receipt')
    _refuse(audit.get('audit_script_sha256') == sha256_file(AUDIT_SCRIPT), 'audit produced by a different audit script')
    heldout = read_json(root / 'held-out.json')
    _refuse(heldout.get('freeze_sha256') == sha, 'held-out results belong to a different freeze receipt')
    refs = read_json(root / 'test-manifest.json')
    _refuse(audit.get('runs_checked') == len(heldout['runs']), 'audit run count differs from held-out runs')
    _refuse(audit.get('independent_lots') == len(refs) == receipt['test_lots'], 'test lot count mismatch')
    config = read_json(root / 'frozen-config.json')
    _refuse(receipt['candidate'] == CANDIDATE == config['v2_primary']['candidate'], 'frozen candidate is not ' + CANDIDATE)
    _refuse(config['v2_primary']['mode'] == MODE and config['v2_primary']['budget'] == BUDGET, 'primary scope changed')
    return receipt, audit, config, heldout, refs


# ---------------------------------------------------------------- pure diagnostics

def optimistic_visit_bound(budget, cost):
    """Loose optimistic maximum number of paid reviews within `budget`.

    Uses the minimum unchanged CU fees: the first review pays wafer_load + stage_base + dwell,
    every later review stage_base + dwell (zero movement, never another wafer load). Every
    admission must still hold the FULL retry reserve (retry_dwell * retry_limit), exactly as the
    harness reserves it, but no failure/missing retry is ever charged. This is an upper bound on
    visit count, not an achievable oracle planner nor a factory throughput figure.
    """
    reserve = float(cost['retry_dwell']) * int(cost['retry_limit'])
    first = float(cost['wafer_load']) + float(cost['stage_base']) + float(cost['dwell'])
    later = float(cost['stage_base']) + float(cost['dwell'])
    visits, spent = 0, 0.0
    while True:
        fee = first if visits == 0 else later
        if spent + fee + reserve > budget + 1e-9:
            break
        spent += fee
        visits += 1
    return {'max_visits': visits, 'charged_at_bound': spent, 'unspent_at_bound': budget - spent,
            'first_review_min_cu': first, 'later_review_min_cu': later, 'retry_reserve_cu': reserve,
            'assumptions': ['minimum unchanged CU fees from the frozen config',
                            'one wafer load only; zero stage movement after the first review',
                            'full retry reserve required at every admission',
                            'zero failures/missing reviews, so no retry is charged',
                            'not an achievable oracle planner and not factory throughput']}


def review_outcome(row):
    """Paid observation class of one ledger row. Unresolved is never a physical negative."""
    ok = [a for a in row['attempts'] if a['status'] == 'ok' and a['reported_doi'] is not None]
    if not ok:
        return 'unresolved'
    return 'reported_positive' if any(a['reported_doi'] for a in ok) else 'reported_negative'


def lot_counts(candidate, doi, p, rows):
    """Count conservation for one lot. `doi` is posthoc latent truth (evaluation only)."""
    candidate = np.asarray(candidate, dtype=bool)
    doi = np.asarray(doi, dtype=bool)
    p = np.asarray(p, dtype=float)
    selected = np.zeros(len(doi), dtype=bool)
    out = {k: 0 for k in ['tp_reported_positive', 'fp_reported_positive', 'sensor_missed_doi', 'reported_negative_non_doi',
                          'unresolved_doi', 'unresolved_non_doi', 'retried_reviews', 'attempts_ok', 'attempts_failure',
                          'attempts_missing', 'attempts_other', 'selected_non_candidate', 'selected_p_ge_099',
                          'selected_p_ge_099_doi']}
    for row in rows:
        i = row['site_index']
        if selected[i]:
            raise ValueError('site selected twice')
        selected[i] = True
        outcome = review_outcome(row)
        latent = bool(doi[i])
        if outcome == 'reported_positive':
            out['tp_reported_positive' if latent else 'fp_reported_positive'] += 1
        elif outcome == 'reported_negative':
            out['sensor_missed_doi' if latent else 'reported_negative_non_doi'] += 1
        else:
            out['unresolved_doi' if latent else 'unresolved_non_doi'] += 1
        out['retried_reviews'] += len(row['attempts']) > 1
        for attempt in row['attempts']:
            key = 'attempts_' + attempt['status']
            out[key if key in out else 'attempts_other'] += 1
        out['selected_non_candidate'] += not candidate[i]
        if p[i] >= HIGH_P:
            out['selected_p_ge_099'] += 1
            out['selected_p_ge_099_doi'] += latent
    cand_doi = candidate & doi
    out.update(dies=len(doi), optical_candidates=int(candidate.sum()), latent_doi=int(doi.sum()),
               candidate_doi=int(cand_doi.sum()), never_optically_admitted_doi=int((doi & ~candidate).sum()),
               selected=int(selected.sum()), selected_latent_doi=int((selected & doi).sum()),
               selected_non_doi=int((selected & ~doi).sum()),
               selected_candidate_doi=int((selected & cand_doi).sum()),
               selected_non_candidate_doi=int((selected & doi & ~candidate).sum()),
               missed_candidate_doi=int((cand_doi & ~selected).sum()),
               unselected_candidate_doi_p_ge_099=int((cand_doi & ~selected & (p >= HIGH_P)).sum()),
               candidates_p_ge_099=int((candidate & (p >= HIGH_P)).sum()))
    out['reported_positives'] = out['tp_reported_positive'] + out['fp_reported_positive']
    out['unresolved'] = out['unresolved_doi'] + out['unresolved_non_doi']
    check_conservation(out)
    return out


def check_conservation(c):
    rules = [
        (c['latent_doi'], c['never_optically_admitted_doi'] + c['candidate_doi']),
        (c['candidate_doi'], c['missed_candidate_doi'] + c['selected_candidate_doi']),
        (c['latent_doi'], c['never_optically_admitted_doi'] + c['missed_candidate_doi'] + c['selected_candidate_doi']),
        (c['selected_latent_doi'], c['selected_candidate_doi'] + c['selected_non_candidate_doi']),
        (c['selected'], c['selected_latent_doi'] + c['selected_non_doi']),
        (c['selected_latent_doi'], c['tp_reported_positive'] + c['sensor_missed_doi'] + c['unresolved_doi']),
        (c['selected_non_doi'], c['fp_reported_positive'] + c['reported_negative_non_doi'] + c['unresolved_non_doi']),
    ]
    for left, right in rules:
        if left != right:
            raise ValueError(f'count conservation failed: {left} != {right}')


def calibration_table(p, doi, mask):
    """Fixed bins [0,.2,.5,.8,.95,1] plus p>=.99, against posthoc latent truth among `mask`."""
    p = np.asarray(p, dtype=float)[mask]
    y = np.asarray(doi, dtype=bool)[mask]
    rows = []
    for k, (lo, hi) in enumerate(zip(BIN_EDGES[:-1], BIN_EDGES[1:])):
        inside = (p >= lo) & ((p <= hi) if k == len(BIN_EDGES) - 2 else (p < hi))
        rows.append(_bin(f'[{lo},{hi}' + (']' if k == len(BIN_EDGES) - 2 else ')'), p[inside], y[inside]))
    return {'bins': rows, 'p_ge_0.99': _bin('[0.99,1]', p[p >= HIGH_P], y[p >= HIGH_P]),
            'n': int(len(p)), 'latent_doi': int(y.sum())}


def _bin(label, p, y):
    n = int(len(p))
    return {'bin': label, 'n': n, 'latent_doi': int(y.sum()), 'mean_p': float(p.mean()) if n else None,
            'latent_doi_rate': float(y.mean()) if n else None, 'gap_mean_p_minus_rate': float(p.mean() - y.mean()) if n else None}


def threshold_confusion(p, doi, candidate, threshold):
    p, doi, c = np.asarray(p, dtype=float), np.asarray(doi, dtype=bool), np.asarray(candidate, dtype=bool)
    pred = p >= threshold
    tp, fp = int((c & pred & doi).sum()), int((c & pred & ~doi).sum())
    fn, tn = int((c & ~pred & doi).sum()), int((c & ~pred & ~doi).sum())
    return {'threshold': threshold, 'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
            'precision': tp / (tp + fp) if tp + fp else None, 'candidate_recall': tp / (tp + fn) if tp + fn else None}


# ---------------------------------------------------------------- campaign scan

def _selected_runs(heldout):
    runs = [r for r in heldout['runs'] if Path(r['ledger_path']).name == f'{CANDIDATE}-{MODE}-{BUDGET}.json']
    for r in runs:
        _refuse(r['mode'] == MODE and float(r['budget']) == BUDGET, 'unexpected selected run scope')
    return runs


def diagnose(root, write=True):
    root = Path(root)
    receipt, audit, config, heldout, refs = verify_inputs(root)
    models = read_json(root / 'models.json')
    cost = config['cost']
    threshold = float(config['model']['classification_threshold'])
    runs = {r['lot_id']: r for r in _selected_runs(heldout)}
    _refuse(len(runs) == len(refs), 'selected candidate ledger missing for some test lot')
    bound = optimistic_visit_bound(BUDGET, cost)
    scenarios = {}
    model_key = None
    for ref in refs:
        try:
            verify_lot(ref)
        except HarnessError as exc:
            raise DiagnosticRefusal('test lot receipt: ' + str(exc)) from exc
        record = runs[ref['lot_id']]
        ledger_path = Path(record['ledger_path'])
        _refuse(sha256_file(ledger_path) == record['ledger_sha256'], 'ledger bytes changed: ' + ledger_path.name)
        ledger = read_json(ledger_path)
        _refuse(ledger['variant']['id'] == CANDIDATE and ledger['mode'] == MODE, 'ledger variant mismatch')
        model_key = ledger['variant']['model']
        frozen = models[model_key]
        _refuse(model.hash_model(frozen) == ledger['model_hash'] == receipt['model_hashes'][model_key], 'model hash mismatch')
        public = load_public(Path(ref['path']))
        oracle = _load_npz(Path(ref['path']) / 'oracle.npz')  # posthoc evaluation only
        p = model.predict(frozen, public['features'])
        _refuse(_sha_array(p) == ledger['frozen_p_sha256'], 'frozen probabilities do not reproduce')
        rows = ledger['run']['rows']
        counts = lot_counts(public['candidate'], oracle['doi'], p, rows)
        metrics = record['metrics']
        for mine, theirs in [('tp_reported_positive', 'true_doi_confirmed'), ('selected', 'attempted_sites'),
                             ('fp_reported_positive', 'reported_false_positives'), ('reported_positives', 'review_reported_positives'),
                             ('candidate_doi', 'candidate_doi'), ('latent_doi', 'all_doi'), ('unresolved', 'unresolved_sites')]:
            _refuse(counts[mine] == metrics[theirs], f'recount {mine} != ledger metric {theirs}')
        s = scenarios.setdefault(ref['scenario'], {'lots': 0, 'counts': {}, 'p': [], 'doi': [], 'cand': [], 'sel': [],
                                                    'cost': {}, 'policy_wall_s': [], 'stop_reasons': {}, 'remaining': []})
        s['lots'] += 1
        for k, v in counts.items():
            s['counts'][k] = s['counts'].get(k, 0) + v
        sel = np.zeros(len(p), dtype=bool)
        sel[[r['site_index'] for r in rows]] = True
        s['p'].append(p)
        s['doi'].append(oracle['doi'].astype(bool))
        s['cand'].append(public['candidate'].astype(bool))
        s['sel'].append(sel)
        for k, v in metrics['cost_totals'].items():
            s['cost'][k] = s['cost'].get(k, 0.0) + float(v)
        s['policy_wall_s'].append(float(metrics['timing']['policy_wall_s']))
        s['stop_reasons'][ledger['run']['stop_reason']] = s['stop_reasons'].get(ledger['run']['stop_reason'], 0) + 1
        s['remaining'].append(float(ledger['run']['remaining']))
    per_scenario = {name: _scenario_summary(s, bound, cost, threshold) for name, s in sorted(scenarios.items())}
    total = {k: sum(v['counts'][k] for v in per_scenario.values()) for k in next(iter(per_scenario.values()))['counts']}
    check_conservation(total)
    lots = sum(v['lots'] for v in per_scenario.values())
    result = {
        'schema_version': 1, 'kind': 'inspection_v3_quality_diagnostics', 'scope': 'posthoc_diagnostic_authored_synthetic',
        'replaces_primary': False, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'freeze_sha256': receipt['receipt_sha256'], 'audit_sha256': sha256_file(root / 'audit.json'),
        'audit_status': audit['status'], 'candidate': CANDIDATE, 'model': model_key, 'mode': MODE, 'budget_cu': BUDGET,
        'lots': lots, 'diagnostic_script_sha256': sha256_file(Path(__file__)),
        'visit_bound': bound,
        'overall': {'counts': total, 'mean_per_lot': {k: v / lots for k, v in total.items()},
                    'mean_visits_per_lot': total['selected'] / lots,
                    'visits_over_optimistic_bound': total['selected'] / lots / bound['max_visits'],
                    'dies_reviewed_fraction': total['selected'] / total['dies'],
                    'min_cu_to_review_every_die_per_lot': _min_cover_cu(total['dies'] / lots, config, cost),
                    'min_cu_to_review_every_candidate_per_lot': _min_cover_cu(total['optical_candidates'] / lots, config, cost)},
        'per_scenario': per_scenario,
        'definitions': {
            'latent_doi': 'posthoc oracle truth; evaluation only, never used for actions',
            'optical_recall_ceiling': 'candidate_doi / latent_doi: the best any candidate-only policy can reach',
            'never_optically_admitted_doi': 'latent DOI the optical sensor never offered as a candidate',
            'missed_candidate_doi': 'candidate DOI that the 360-CU route did not select',
            'unresolved': 'every paid attempt failed or was missing; unknown, never called good or a physical negative',
            'sensor_missed_doi': 'selected latent DOI that the paid review reported negative (sensor error)',
            'ranking_error': 'selected non-DOI sites (choice error under the frozen probabilities and route costs)',
            'threshold_classification': 'confusion at the frozen 0.5 threshold over all original candidates; not used by the route policy',
            'policy_wall_s_total_per_lot': 'TOTAL selection-call wall time per lot run, not per decision'},
        'limitations': [
            'Authored synthetic tabular evidence; not SEM-image accuracy, factory throughput or wafer yield.',
            'Total dies per lot do not imply every die receives SEM review; only the selected candidates are paid reviews.',
            'Historical training labels are assumed available; offline learning CU is excluded from the online budget.',
            'No Jev or free-text notes were used or invented; no new gain is claimed and the preregistered primary is unchanged.',
            'Visit bound is loose and optimistic (zero movement, zero failures); not an achievable oracle planner.',
            'Posthoc diagnostic only; nothing here was tuned, rerun or used to select actions.']}
    if write:
        text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n'
        (root / OUTPUT).write_text(text, encoding='utf-8', newline='\n')
    return result


def _min_cover_cu(sites, config, cost):
    wafers = int(config['geometry']['wafers_per_lot'])
    return wafers * float(cost['wafer_load']) + sites * (float(cost['stage_base']) + float(cost['dwell']))


def _scenario_summary(s, bound, cost, threshold):
    p, doi, cand, sel = (np.concatenate(s[k]) for k in ['p', 'doi', 'cand', 'sel'])
    c, n = s['counts'], s['lots']
    visits = c['selected'] / n
    stage_min = float(cost['stage_base']) * c['selected']
    cost_mean = {k: v / n for k, v in s['cost'].items()}
    wall = np.asarray(s['policy_wall_s'])
    return {
        'lots': n, 'counts': c, 'mean_per_lot': {k: v / n for k, v in c.items()},
        'optical_recall_ceiling': c['candidate_doi'] / c['latent_doi'] if c['latent_doi'] else None,
        'visits': {'mean_per_lot': visits, 'optimistic_bound': bound['max_visits'],
                   'shortfall_vs_bound': bound['max_visits'] - visits,
                   'mean_cost_cu': cost_mean,
                   'mean_stage_movement_cu_above_minimum': (s['cost'].get('stage', 0.0) - stage_min) / n,
                   'mean_extra_wafer_loads_cu': cost_mean.get('load', 0.0) - float(cost['wafer_load']),
                   'mean_retry_cu': cost_mean.get('retry', 0.0),
                   'mean_unspent_cu': float(np.mean(s['remaining'])),
                   'stop_reasons': s['stop_reasons']},
        'calibration_candidates': calibration_table(p, doi, cand),
        'calibration_selected': calibration_table(p, doi, sel),
        'error_separation': {
            'threshold_classification_candidates': threshold_confusion(p, doi, cand, threshold),
            'ranking': {'selected_non_doi': c['selected_non_doi'], 'selected_latent_doi': c['selected_latent_doi'],
                        'selected_p_ge_099': c['selected_p_ge_099'], 'selected_p_ge_099_doi': c['selected_p_ge_099_doi'],
                        'candidates_p_ge_099': c['candidates_p_ge_099'],
                        'unselected_candidate_doi_p_ge_099': c['unselected_candidate_doi_p_ge_099']},
            'sensor': {'sensor_missed_doi': c['sensor_missed_doi'], 'fp_reported_positive': c['fp_reported_positive'],
                       'unresolved_doi': c['unresolved_doi'], 'unresolved_non_doi': c['unresolved_non_doi'],
                       'retried_reviews': c['retried_reviews'],
                       'attempts': {k: c['attempts_' + k] for k in ['ok', 'failure', 'missing', 'other']}}},
        'policy_timing': {'policy_wall_s_total_per_lot_mean': float(wall.mean()),
                          'policy_wall_s_total_per_lot_median': float(np.median(wall)),
                          'derived_mean_wall_s_per_decision': float(wall.sum() / c['selected']) if c['selected'] else None,
                          'note': 'TOTAL selection-call time per lot; per-decision value is derived by division'}}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print('usage: diagnose_inspection_v3.py <v3-root>', file=sys.stderr)
        return 2
    try:
        result = diagnose(Path(argv[0]))
    except DiagnosticRefusal as exc:
        print(json.dumps({'status': 'refused', 'detail': str(exc)}))
        return 1
    o = result['overall']
    print(json.dumps({'status': 'written', 'path': (Path(argv[0]) / OUTPUT).as_posix(), 'lots': result['lots'],
                      'mean_visits_per_lot': o['mean_visits_per_lot'], 'visit_bound': result['visit_bound']['max_visits'],
                      'mean_confirmed_per_lot': o['mean_per_lot']['tp_reported_positive']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
