"""Fresh-data v4 development, freeze, independent campaign and report.

python -m inspection_v4.cli develop --root NEW_DIRECTORY
python -m inspection_v4.cli freeze --root DEVELOPMENT_DIRECTORY
python -m inspection_v4.cli campaign --root FROZEN_DIRECTORY
python -m inspection_v4.cli report --root COMPLETED_DIRECTORY

Hypothesis: a class-conditional Gaussian-mixture DOI model (``inspection_v4.model``) on the
same seven public features and historical candidate DOI labels, against freshly fitted
CB400 and logistic baselines. Authored synthetic numeric lots only. The v1 generator,
sensor, CU costs, the v3 harness and the v1/v2/v3 policies are reused unchanged.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import time

import numpy as np

from inspection_review import data
from inspection_review import model as v1_model
from inspection_review.cli import ControlledReviewSimulator, HarnessError, _load_npz, load_public, read_json, save_lot, sha256_file, write_json
from inspection_review.policies import sanitize_public
from inspection_review.reporting import paired_comparison, matched_cost_savings
from inspection_v2 import model as v2_model
from inspection_v2.cli import now, digest, git_head, verify_lot, privileged
from inspection_v3 import harness
from inspection_v3 import model as v3_model
from inspection_v3.inference import compile_predictor
from . import model

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = Path(__file__).with_name('protocol.json')
PRIOR_PROTOCOLS = ('inspection_review/protocol.json', 'inspection_v2/protocol.json', 'inspection_v3/protocol.json')
SPLITS = ('train', 'calibration', 'development', 'test')
FROZEN_FILES = ('frozen-config.json', 'models.json', 'development.json', 'lot-manifest.json')
LIMITATIONS = ['Authored numeric synthetic lots from the unchanged v1 generator and sensor; not SEM-image, factory or physical accuracy.',
    'Classification metrics cover original optical candidate sites only, not all-site physical recall.',
    'Equipment CU (synthetic cost units) and measured software CPU/wall time are separate quantities.',
    'No genuine Jev notes or images: the tabular campaign does not exercise them.']


def source_hashes():
    names = [str(p.relative_to(REPO)).replace('\\', '/') for namespace in ['inspection_review', 'inspection_v2', 'inspection_v3', 'inspection_v4']
             for p in sorted((REPO / namespace).glob('*.py'))]
    names += [*PRIOR_PROTOCOLS, 'inspection_v4/protocol.json', 'DATA_CONTRACT.md', 'INSPECTION_V2_CONTRACT.md',
              'INSPECTION_V3_CONTRACT.md', 'INSPECTION_V4_CONTRACT.md', 'pyproject.toml', 'uv.lock']
    return {name: sha256_file(REPO / name) for name in names}


# ---------------------------------------------------------------- split guards (before any lot is generated or read)

def previous_seeds():
    """Every seed reserved by a v1/v2/v3 split (train, validation, test and all v2/v3 split tables)."""
    previous = set()
    for name in PRIOR_PROTOCOLS:
        old = read_json(REPO / name)
        previous.update(old['splits']['train_seeds'] + old['splits']['validation_seeds'])
        previous.update(s for seeds in old['splits']['test_seeds_by_scenario'].values() for s in seeds)
        for table in ('v2_splits', 'v3_splits'):
            previous.update(v2_model._pair(r)[0] for rows in (old.get(table) or {}).values() for r in rows)
    return previous


def check_splits(config):
    previous = previous_seeds()
    design = config['v4_design']
    scenarios = list(design['scenarios'])
    reserve = set(config['v4_reserved']['live_demonstration_seeds'])
    if set(scenarios) != set(config['splits']['test_seeds_by_scenario']) or len(set(scenarios)) != len(scenarios):
        raise HarnessError('scenario contracts differ')
    if reserve & previous:
        raise HarnessError('live demonstration reserve was used by a previous study')
    known = set()
    for split in SPLITS:
        entries = config['v2_splits'][split]
        if not entries:
            raise HarnessError('empty split: ' + split)
        counts = dict.fromkeys(scenarios, 0)
        for row in entries:
            seed, scenario = v2_model._pair(row)
            if seed < design['minimum_seed'] or seed in previous or seed in reserve or seed in known:
                raise HarnessError(f'duplicate, invalid, reserved or previously used seed {seed} in {split}')
            if scenario not in counts:
                raise HarnessError('unknown scenario')
            counts[scenario] += 1
            known.add(seed)
        expected = design['lots_per_scenario'][split]
        if any(count != expected for count in counts.values()):
            raise HarnessError(f'{split} is not {expected} lots per scenario')
    if config['splits']['validation_seeds']:
        raise HarnessError('v4 has no v1 validation split')
    train_seeds = config['splits']['train_seeds']
    if len(train_seeds) != len(set(train_seeds)) or set(train_seeds) != {r['seed'] for r in config['v2_splits']['train']}:
        raise HarnessError('training seed contracts differ')
    expected_test = {(seed, scenario) for scenario, seeds in config['splits']['test_seeds_by_scenario'].items() for seed in seeds}
    if expected_test != {v2_model._pair(r) for r in config['v2_splits']['test']} or len(expected_test) != len(config['v2_splits']['test']):
        raise HarnessError('test seed contracts differ')
    return {'checked_previous_seeds': len(previous), 'v4_seeds': len(known), 'reserved': sorted(reserve)}


def generator_config(config):
    """Generation view: identical physics/sensor; the v1 stationary-only train request rule is
    replaced by ``check_splits`` (v4 trains on all five scenarios)."""
    view = copy.deepcopy(config)
    view['splits'] = {'train_seeds': [], 'validation_seeds': [],
                      'test_seeds_by_scenario': copy.deepcopy(config['splits']['test_seeds_by_scenario'])}
    return view


def generate(root, split, config):
    if split == 'test' and not (root / 'campaign-started.json').exists():
        raise HarnessError('test lots are generated only by a started campaign')
    view = generator_config(config)
    refs = []
    for row in config['v2_splits'][split]:
        lot = data.generate_lot(row['seed'], row['scenario'], view)
        refs.append(save_lot(root / 'lots' / split / lot['public']['lot_id'], lot, split, row['seed'], row['scenario']))
    return refs


# ---------------------------------------------------------------- models

def _fit_logistic(train, config):
    """Unchanged v1 logistic numerics (``config['model']``) on the same 60 training lots.

    The v2 guards run first on the true ``[seed, scenario]`` identities. v1's own
    stationary-only rule is a v1 split policy, so v1 receives a guard view whose public
    scenario reads ``stationary``; features, labels and fit numerics are untouched.
    """
    identities, _, _ = v2_model._training_examples(train, config)
    view = [{'public': {**lot['public'], 'scenario': 'stationary'}, 'oracle': lot['oracle']} for lot in train]
    v1 = v1_model.train_model(view, {'splits': {'train_seeds': list(config['splits']['train_seeds'])}, 'model': dict(config['model'])})
    return {**v1, 'family': 'logistic', 'base_family': v1['family'], 'supports_online_update': False, 'calibration': None,
            'provenance': {'module': 'inspection_v4.cli', 'delegate': 'inspection_review.model.train_model',
                           'label': v2_model.LABEL_DEFINITION, 'train_pairs': [[seed, scenario] for _, seed, scenario in identities],
                           'split_source': 'v2_splits', 'v1_guard': 'replaced by v4 check_splits and v2 training guards',
                           'imputation': 'train_candidate_mean', 'probability': 'raw_base', 'online_updates': 'disabled_v4',
                           'numpy_version': np.__version__}}


def fit_models(config, train, calibration):
    """Every model on the same training lots; GMM calibrations on the same calibration lots."""
    models, timing = {}, {}

    def timed(key, fn):
        c0, w0 = time.process_time(), time.perf_counter()
        out = fn()
        timing[key] = {'fit_cpu_s': time.process_time() - c0, 'fit_wall_s': time.perf_counter() - w0}
        return out

    models['logistic/identity'] = timed('logistic/identity', lambda: _fit_logistic(train, config))
    settings = copy.deepcopy(config)
    settings['v2_model'] = {**config['v2_model'], **config['v4_baselines']['cb400/identity'], 'family': 'catboost'}
    models['cb400/identity'] = timed('cb400/identity', lambda: v3_model.train_model(train, settings))
    for name, grid in config['v4_model_grid'].items():
        settings = copy.deepcopy(config)
        settings['v4_model'] = dict(grid)
        raw = timed(f'{name}/raw_base', lambda: model.train_model(train, settings))
        for method in config['v4_calibrations']:
            # Calibration timing excludes the shared raw_base fit recorded above.
            models[f'{name}/{method}'] = timed(f'{name}/{method}', lambda: model.fit_calibration(raw, calibration, settings, method=method))
    return models, timing


class FrozenMixturePredictor:
    """Immutable snapshot of a gaussian_mixture model; predictions go through ``model.predict``."""

    __slots__ = ('_snapshot', '_model_hash')

    def __init__(self, fitted):
        snapshot = copy.deepcopy(fitted)
        if snapshot.get('family') != model.FAMILY:
            raise ValueError('FrozenMixturePredictor needs a gaussian_mixture model')
        object.__setattr__(self, '_snapshot', snapshot)
        object.__setattr__(self, '_model_hash', model.hash_model(snapshot))

    def __setattr__(self, name, value):
        raise AttributeError('FrozenMixturePredictor is immutable')

    def __delattr__(self, name):
        raise AttributeError('FrozenMixturePredictor is immutable')

    @property
    def model_hash(self):
        return self._model_hash

    def predict(self, features):
        p = np.array(model.predict(copy.deepcopy(self._snapshot), features), dtype=float, copy=True)
        p.flags.writeable = False
        return p


def predictor_for(fitted):
    if fitted.get('family') == model.FAMILY:
        return FrozenMixturePredictor(fitted)
    return compile_predictor(fitted)


class FrozenModelAPI:
    """Model capability handed to the v3 harness: v4 forwarding (v3 for baselines), no online updates."""

    @staticmethod
    def hash_model(fitted):
        return model.hash_model(fitted)

    @staticmethod
    def predict(fitted, features):
        return model.predict(fitted, features)

    @staticmethod
    def update_model(fitted, feature_row, label, config):
        raise HarnessError('online updates are disabled for every v4 model')


MODEL_API = FrozenModelAPI()


def _frozen_predictions(predictors, features):
    out = {}
    for key, predictor in predictors.items():
        p = np.array(predictor.predict(features), dtype=float, copy=True)
        p.flags.writeable = False
        out[key] = p
    return out


# ---------------------------------------------------------------- runs

def run_matrix(root, split, refs, config, models):
    runs = []
    predictors = {key: predictor_for(m) for key, m in models.items()}
    hashes = {key: model.hash_model(m) for key, m in models.items()}
    per_lot = len(config['v2_variants']) * len(config['modes']) * len(config['budgets'])
    for number, ref in enumerate(refs, 1):
        meta = verify_lot(ref)
        public = load_public(Path(ref['path']))
        predictions = _frozen_predictions(predictors, public['features'])
        views = {key: sanitize_public(public, m['mean'], m['scale']) for key, m in models.items()}
        for variant in config['v2_variants']:
            key = variant['model']
            fitted, frozen_p, view = models[key], predictions[key], views[key]
            if fitted.get('supports_online_update'):
                raise HarnessError('v4 models must be frozen')
            p_sha = hashlib.sha256(frozen_p.tobytes()).hexdigest()
            for mode in config['modes']:
                for budget in config['budgets']:
                    simulator = ControlledReviewSimulator(Path(ref['path']), data)
                    run = harness.run_selection(view, simulator, policy_name=variant['policy'], mode=mode, budget=budget,
                        frozen_model=fitted, frozen_p=frozen_p, model_api=MODEL_API, config=config)
                    calls = [(r['site_index'], a['attempt']) for r in run['rows'] for a in r['attempts']]
                    if calls != simulator.calls:
                        raise HarnessError('sensor calls differ from paid ledger')
                    if run['online_updates_enabled'] or hashlib.sha256(frozen_p.tobytes()).hexdigest() != p_sha:
                        raise HarnessError('frozen probabilities or model changed during selection')
                    metrics = harness.evaluate_run(run, simulator.release_for_evaluation(), public['candidate'], frozen_p, config)
                    ledger = root / 'ledgers' / split / meta['lot_id'] / f"{variant['id']}-{mode}-{budget}.json"
                    write_json(ledger, {'schema_version': 1, 'study': 'v4', 'split': split, 'variant': variant,
                        'lot_id': meta['lot_id'], 'model_hash': hashes[key], 'frozen_p_sha256': p_sha,
                        'selection_reward_semantics': 'latent_doi_probability', 'mode': mode, 'run': run, 'metrics': metrics})
                    runs.append({'status': 'complete', 'lot_id': meta['lot_id'], 'scenario': meta['scenario'], 'policy': variant['id'],
                        'role': variant['role'], 'mode': mode, 'budget': budget, 'metrics': metrics, 'model_hash': hashes[key],
                        'ledger_path': ledger.as_posix(), 'ledger_sha256': sha256_file(ledger)})
        write_json(root / ('progress-' + split + '.json'), {'split': split, 'completed_lots': number, 'total_lots': len(refs),
            'runs': len(runs), 'expected_runs': per_lot * len(refs), 'updated_at': now()})
    return runs


def load_lots(refs):
    lots = []
    for ref in refs:
        verify_lot(ref)
        public = load_public(Path(ref['path']))
        public.update(seed=ref['seed'], scenario=ref['scenario'])
        lots.append({'public': public, 'oracle': _load_npz(Path(ref['path']) / 'oracle.npz')})
    return lots


def classification(models, lots, config):
    """Candidate-only DOI classification (AP/recall/precision/Brier/ECE/log loss), overall and per scenario."""
    settings = config['v4_evaluation']
    threshold, bins = float(settings['threshold']), int(settings['ece_bins'])
    grid = [round(0.1 * i, 1) for i in range(1, 11)]

    def metrics(y, p):
        out = v2_model._metrics(y, p, threshold, bins, grid)
        out['log_loss'] = None if len(y) == 0 else v3_model._nll(v3_model._logit(p), y.astype(float))
        return out

    result = {}
    for key, fitted in models.items():
        predictor = predictor_for(fitted)
        ys, ps, scenarios = [], [], []
        for lot in lots:
            mask = np.asarray(lot['public']['candidate'], dtype=bool)
            x = np.asarray(lot['public']['features'], dtype=float)[mask]
            ys.append(np.asarray(lot['oracle']['doi'], dtype=bool)[mask])
            ps.append(predictor.predict(x) if mask.any() else np.zeros(0))
            scenarios.append(np.full(int(mask.sum()), lot['public']['scenario'], dtype=object))
        y, p, s = np.concatenate(ys), np.concatenate(ps), np.concatenate(scenarios)
        result[key] = {'model_hash': model.hash_model(fitted), 'family': fitted['family'],
            'probability': (fitted.get('provenance') or {}).get('probability'), 'scope': settings['classification_scope'],
            'aggregate': metrics(y, p),
            'per_scenario': {name: metrics(y[s == name], p[s == name]) for name in config['v4_design']['scenarios']}}
    return result


# ---------------------------------------------------------------- selection and summaries

def condition_means(runs, config):
    primary = config['v4_primary']
    out = []
    for variant in config['v2_variants']:
        rows = [r for r in runs if r['policy'] == variant['id'] and r['mode'] == primary['mode'] and r['budget'] == primary['budget']]
        if not rows:
            continue
        timing = lambda name: float(np.mean([r['metrics']['timing'].get(name, 0.0) for r in rows]))  # noqa: E731
        out.append({'variant': variant['id'], 'model': variant['model'], 'policy': variant['policy'], 'role': variant['role'],
            'lots': len(rows), 'mean_doi': float(np.mean([r['metrics'][primary['metric']] for r in rows])),
            'mean_spent_cu': float(np.mean([r['metrics']['spent'] for r in rows])),
            'mean_loading_cost_cu': float(np.mean([r['metrics']['cost_totals']['load'] for r in rows])),
            'mean_stage_cost_cu': float(np.mean([r['metrics']['cost_totals']['stage'] for r in rows])),
            'mean_policy_cpu_s': timing('policy_cpu_s'), 'mean_policy_wall_s': timing('policy_wall_s'),
            'mean_run_cpu_s': timing('run_total_cpu_s')})
    return out


def select_candidate(means, config, models):
    """Highest development mean DOI among gaussian_mixture candidate variants; baselines are never eligible."""
    variants = {v['id']: v for v in config['v2_variants']}
    pool = [r for r in means if variants[r['variant']]['role'] == config['v4_primary']['candidate_role']
            and models[variants[r['variant']]['model']].get('family') == model.FAMILY]
    if not pool:
        raise HarnessError('no gaussian_mixture candidate variant has development results')
    return sorted(pool, key=lambda r: (-r['mean_doi'], r['mean_spent_cu'], r['variant']))[0]['variant']


def same_model_comparator(config, candidate):
    variant = next(v for v in config['v2_variants'] if v['id'] == candidate)
    if variant['policy'] == 'learned':
        return None
    match = [v['id'] for v in config['v2_variants'] if v['model'] == variant['model'] and v['policy'] == 'learned']
    if not match:
        raise HarnessError('missing same-model learned comparator')
    return match[0]


def per_scenario(runs, candidate, comparator, config):
    primary = config['v4_primary']
    out = {}
    for scenario in config['v4_design']['scenarios']:
        def mean(variant):
            values = [r['metrics'][primary['metric']] for r in runs if r['policy'] == variant and r['scenario'] == scenario
                      and r['mode'] == primary['mode'] and r['budget'] == primary['budget']]
            return float(np.mean(values)) if values else None
        a, b = mean(candidate), mean(comparator)
        out[scenario] = {'mean_candidate': a, 'mean_comparator': b,
                         'candidate_below_comparator': None if a is None or b is None else bool(a < b)}
    return out


def summarize(runs, config, candidate):
    primary = config['v4_primary']
    common = dict(mode=primary['mode'], budget=primary['budget'], metric=primary['metric'],
                  seed=primary['bootstrap_seed'], replicates=primary['bootstrap_replicates'])
    same = same_model_comparator(config, candidate)
    cost = config['v4_matched_cost']
    matched = matched_cost_savings(runs, candidate, primary['comparator'], primary['mode'], cost['budget_ledger'], cost['target_true_doi'], 0.0)
    for key in ('saving_target', 'meets_saving_target'):
        matched.pop(key)
    matched.update(role=cost['role'], claim=cost['claim'])
    return {'candidate': candidate,
        'primary_vs_cb400_route_full': paired_comparison(runs, candidate, primary['comparator'], primary=True, target=primary['target_relative_gain'], **common),
        'secondary_vs_logistic_learned': paired_comparison(runs, candidate, 'logistic_learned', **common),
        'secondary_vs_same_model_learned': paired_comparison(runs, candidate, same, **common) if same else
            {'comparator': None, 'reason': 'candidate already uses the learned policy on its model'},
        'per_scenario_vs_primary_comparator': per_scenario(runs, candidate, primary['comparator'], config),
        'all_variants': condition_means(runs, config),
        'matched_cost_supplementary': matched}


# ---------------------------------------------------------------- commands

def develop(root):
    if root.exists():
        raise HarnessError('development requires a new directory')
    config = read_json(PROTOCOL)
    guard = check_splits(config)
    root.mkdir(parents=True)
    write_json(root / 'config.json', config)
    started = time.perf_counter()
    refs = {}
    for split in ('train', 'calibration', 'development'):
        refs[split] = generate(root, split, config)
        write_json(root / 'progress-generate.json', {'generated': {k: len(v) for k, v in refs.items()}, 'updated_at': now()})
    write_json(root / 'lot-manifest.json', refs)
    train = [privileged(r, 'train') for r in refs['train']]
    calibration = [privileged(r, 'calibration') for r in refs['calibration']]
    fitted, fit_timing = fit_models(config, train, calibration)
    write_json(root / 'models.json', fitted)
    runs = run_matrix(root, 'development', refs['development'], config, fitted)
    means = condition_means(runs, config)
    best = select_candidate(means, config, fitted)
    result = {'kind': 'development_only_v4', 'generated_at': now(), 'split_guard': guard, 'runs': runs,
        'summary': summarize(runs, config, best), 'selected_candidate': best,
        'selection_rule': config['v4_primary']['selection_rule'],
        'classification_development': classification(fitted, load_lots(refs['development']), config),
        'fit_timing_software': fit_timing, 'wall_seconds': time.perf_counter() - started,
        'limitations': LIMITATIONS + ['Development selects the candidate; it is never final performance evidence.']}
    write_json(root / 'development.json', result)
    return {'status': 'development_complete', 'root': root.as_posix(), 'runs': len(runs), 'selected_candidate': best,
            'development_preview_not_evidence': result['summary']['primary_vs_cb400_route_full']}


def _verify_lot_receipts(refs):
    for split, items in refs.items():
        if split == 'test':
            raise HarnessError('test lots present in the development manifest')
        for ref in items:
            if verify_lot(ref)['split'] != split:
                raise HarnessError('wrong lot split receipt')


def freeze(root):
    if (root / 'freeze.json').exists() or (root / 'campaign-started.json').exists():
        raise HarnessError('already frozen or started')
    if (root / 'lots' / 'test').exists() or (root / 'test-manifest.json').exists():
        raise HarnessError('test lots exist before freeze')
    config = read_json(root / 'config.json')
    development = read_json(root / 'development.json')
    models = read_json(root / 'models.json')
    refs = read_json(root / 'lot-manifest.json')
    check_splits(config)
    _verify_lot_receipts(refs)
    for run in development['runs']:
        if sha256_file(Path(run['ledger_path'])) != run['ledger_sha256']:
            raise HarnessError('development ledger changed')
    candidate = development['selected_candidate']
    if candidate != select_candidate(condition_means(development['runs'], config), config, models):
        raise HarnessError('development selection does not reproduce')
    config['v4_primary'].update(candidate=candidate, same_model_comparator=same_model_comparator(config, candidate))
    write_json(root / 'frozen-config.json', config)
    receipt = {'schema_version': 1, 'study': 'v4', 'created_at': now(), 'source_commit': git_head(), 'source_hashes': source_hashes(),
        'file_hashes': {f: sha256_file(root / f) for f in FROZEN_FILES},
        'model_hashes': {key: model.hash_model(m) for key, m in models.items()},
        'lot_receipts': {split: {r['lot_id']: r['metadata_sha256'] for r in items} for split, items in refs.items()},
        'candidate': candidate, 'comparator': config['v4_primary']['comparator'], 'test_lots': len(config['v2_splits']['test']),
        'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'catboost': importlib.metadata.version('catboost')},
        'm5_authorization': 'coordinator only, after focused tests and independent boundary checks',
        'test_generation_has_started': False}
    receipt['receipt_sha256'] = digest(receipt)
    write_json(root / 'freeze.json', receipt)
    return {'status': 'frozen', 'candidate': candidate, 'receipt_sha256': receipt['receipt_sha256']}


def verify_freeze(root):
    receipt = read_json(root / 'freeze.json')
    if receipt.get('study') != 'v4' or digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'}) != receipt['receipt_sha256']:
        raise HarnessError('freeze receipt changed')
    if source_hashes() != receipt['source_hashes']:
        raise HarnessError('frozen source changed')
    for filename, expected in receipt['file_hashes'].items():
        if sha256_file(root / filename) != expected:
            raise HarnessError('frozen file changed: ' + filename)
    models = read_json(root / 'models.json')
    if {key: model.hash_model(m) for key, m in models.items()} != receipt['model_hashes']:
        raise HarnessError('frozen model hashes differ')
    refs = read_json(root / 'lot-manifest.json')
    _verify_lot_receipts(refs)
    if {split: {r['lot_id']: r['metadata_sha256'] for r in items} for split, items in refs.items()} != receipt['lot_receipts']:
        raise HarnessError('lot receipts differ')
    config = read_json(root / 'frozen-config.json')
    check_splits(config)
    if config['v4_primary']['candidate'] != receipt['candidate']:
        raise HarnessError('frozen candidate differs')
    return receipt


def campaign(root):
    receipt = verify_freeze(root)
    if (root / 'campaign-started.json').exists():
        raise HarnessError('campaign already started; no silent resume')
    config = read_json(root / 'frozen-config.json')
    models = read_json(root / 'models.json')
    write_json(root / 'campaign-started.json', {'created_at': now(), 'freeze_sha256': receipt['receipt_sha256']})
    started = time.perf_counter()
    refs = generate(root, 'test', config)
    write_json(root / 'test-manifest.json', refs)
    runs = run_matrix(root, 'test', refs, config, models)
    result = {'kind': 'held_out_synthetic_v4', 'generated_at': now(), 'freeze_sha256': receipt['receipt_sha256'], 'runs': runs,
        'summary': summarize(runs, config, receipt['candidate']),
        'classification': classification(models, load_lots(refs), config),
        'wall_seconds': time.perf_counter() - started, 'limitations': LIMITATIONS}
    write_json(root / 'held-out.json', result)
    return {'status': 'campaign_complete', 'runs': len(runs), 'root': root.as_posix(),
            'primary': result['summary']['primary_vs_cb400_route_full'], 'wall_seconds': result['wall_seconds']}


def report(root):
    result = read_json(root / 'held-out.json')
    summary = result['summary']
    primary = summary['primary_vs_cb400_route_full']
    lines = ['# Inspection v4 held-out synthetic results', '',
             'Authored numeric synthetic evidence; not SEM-image, factory or physical accuracy. CU and CPU time are separate.', '',
             f"Candidate `{summary['candidate']}` vs `{primary['comparator']}`: relative gain {primary['relative_gain']}, "
             f"paired 95% CI of difference {primary['ci95_difference']}, success {primary['success']} ({primary['reason']}).", '',
             '| variant | role | mean true DOI | mean CU | mean policy CPU s |', '|---|---|---|---|---|']
    lines += [f"| {r['variant']} | {r['role']} | {r['mean_doi']:.3f} | {r['mean_spent_cu']:.1f} | {r['mean_policy_cpu_s']:.4f} |" for r in summary['all_variants']]
    lines += ['', '```json', json.dumps({k: v for k, v in summary.items() if k != 'all_variants'}, indent=2),
              json.dumps({k: v['aggregate'] for k, v in result['classification'].items()}, indent=2), '```',
              '', *('- ' + text for text in result['limitations']), '']
    path = root / 'report.md'
    path.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    return {'status': 'report_written', 'path': path.as_posix()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['develop', 'freeze', 'campaign', 'report'])
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(globals()[args.command](args.root), allow_nan=False))
        return 0
    except Exception as exc:
        if args.root.exists():
            write_json(args.root / ('FAILED-' + args.command + '.json'), {'created_at': now(), 'stage': args.command, 'exception_type': type(exc).__name__, 'detail': str(exc)})
        print(json.dumps({'status': 'failed', 'stage': args.command, 'error': type(exc).__name__, 'detail': str(exc), 'root': args.root.as_posix()}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
