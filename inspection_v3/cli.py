"""Fresh-data v3 development, freeze, independent campaign and report."""
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
from inspection_review.cli import ControlledReviewSimulator, HarnessError, _load_npz, load_public, read_json, sha256_file, write_json
from inspection_review.policies import sanitize_public
from inspection_review.reporting import paired_comparison, matched_cost_savings
from inspection_v2.cli import now, digest, git_head, generate, verify_lot, privileged
from . import harness, model, sensor_yield
from .inference import compile_predictor

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = Path(__file__).with_name('protocol.json')


def source_hashes():
    names = [str(p.relative_to(REPO)).replace('\\', '/') for namespace in ['inspection_review', 'inspection_v2', 'inspection_v3'] for p in sorted((REPO / namespace).glob('*.py'))]
    names += ['inspection_review/protocol.json', 'inspection_v2/protocol.json', 'inspection_v3/protocol.json', 'INSPECTION_V3_CONTRACT.md', 'INSPECTION_V2_CONTRACT.md', 'pyproject.toml', 'uv.lock']
    return {name: sha256_file(REPO / name) for name in names}


def check_splits(config):
    previous = set()
    for old in [data.load_config(), read_json(REPO / 'inspection_v2/protocol.json')]:
        previous.update(old['splits']['train_seeds'] + old['splits']['validation_seeds'])
        previous.update(s for seeds in old['splits']['test_seeds_by_scenario'].values() for s in seeds)
        previous.update(r['seed'] for rows in old.get('v2_splits', {}).values() for r in rows)
    known = set()
    scenarios = set(config['splits']['test_seeds_by_scenario'])
    for split in ['train', 'calibration', 'development', 'test']:
        entries = config['v2_splits'][split]
        if not entries:
            raise HarnessError('empty split: ' + split)
        for row in entries:
            seed = row['seed']
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 5000 or seed in previous or seed in known:
                raise HarnessError('duplicate, invalid or previously used seed')
            if row['scenario'] not in scenarios:
                raise HarnessError('unknown scenario')
            known.add(seed)
    if set(config['splits']['train_seeds']) != {r['seed'] for r in config['v2_splits']['train']}:
        raise HarnessError('training seed contracts differ')
    expected_test = {(seed, scenario) for scenario, seeds in config['splits']['test_seeds_by_scenario'].items() for seed in seeds}
    if expected_test != {(r['seed'], r['scenario']) for r in config['v2_splits']['test']}:
        raise HarnessError('test seed contracts differ')


def models_for(config, train, calibration):
    settings = copy.deepcopy(config)
    settings['v2_model'] = {'family': 'logistic'}
    out = {'logistic/identity': model.train_model(train, settings)}
    for name, grid in config['v3_model_grid'].items():
        settings = copy.deepcopy(config)
        settings['v2_model'] = {**config['v2_model'], **grid, 'family': 'catboost'}
        if 'learning_rate' in grid:
            settings['v2_model'].pop('lr', None)
        raw = model.train_model(train, settings)
        out[name + '/identity'] = raw
        if name == 'cb400':
            for method in ['platt', 'temperature', 'isotonic']:
                out[name + '/' + method] = model.fit_calibration(raw, calibration, settings, method=method)
    return out


def comparator_for(config, variant):
    matches = [v for v in config['v2_variants'] if v['model'] == variant['model'] and v['policy'] == 'learned']
    if not matches:
        raise HarnessError('missing same-model comparator')
    return matches[0]['id']


def run_matrix(root, split, refs, config, models, yield_model):
    runs = []
    compiled = {key: compile_predictor(m) for key, m in models.items()}
    compiled_yield = compile_predictor(yield_model)
    for ref in refs:
        meta = verify_lot(ref)
        public = load_public(Path(ref['path']))
        predictions = {key: predictor.predict(public['features']) for key, predictor in compiled.items()}
        views = {key: sanitize_public(public, m['mean'], m['scale']) for key, m in models.items()}
        review_yield = np.clip(compiled_yield.predict_raw(public['features']), 0.0, 1.0)
        for variant in config['v2_variants']:
            key = variant['model']
            fitted, frozen_p, view = models[key], predictions[key], views[key]
            reward = review_yield if variant.get('reward') == 'review_yield' else None
            for mode in config['modes']:
                for budget in config['budgets']:
                    simulator = ControlledReviewSimulator(Path(ref['path']), data)
                    run = harness.run_selection(view, simulator, policy_name=variant['policy'], mode=mode, budget=budget,
                        frozen_model=fitted, frozen_p=frozen_p, model_api=model, config=config, selection_reward=reward)
                    calls = [(r['site_index'], a['attempt']) for r in run['rows'] for a in r['attempts']]
                    if calls != simulator.calls:
                        raise HarnessError('sensor calls differ from paid ledger')
                    metrics = harness.evaluate_run(run, simulator.release_for_evaluation(), public['candidate'], frozen_p, config)
                    ledger = root / 'ledgers' / split / meta['lot_id'] / f"{variant['id']}-{mode}-{budget}.json"
                    write_json(ledger, {'schema_version': 1, 'study': 'v3', 'split': split, 'variant': variant,
                        'lot_id': meta['lot_id'], 'model_hash': model.hash_model(fitted),
                        'frozen_p_sha256': hashlib.sha256(frozen_p.tobytes()).hexdigest(),
                        'selection_reward_sha256': hashlib.sha256((reward if reward is not None else frozen_p).tobytes()).hexdigest(),
                        'selection_reward_semantics': 'reported_review_positive' if reward is not None else 'latent_doi_probability',
                        'mode': mode, 'run': run, 'metrics': metrics})
                    runs.append({'status': 'complete', 'lot_id': meta['lot_id'], 'scenario': meta['scenario'], 'policy': variant['id'],
                        'mode': mode, 'budget': budget, 'metrics': metrics, 'model_hash': model.hash_model(fitted),
                        'ledger_path': ledger.as_posix(), 'ledger_sha256': sha256_file(ledger)})
        write_json(root / ('progress-' + split + '.json'), {'completed_lots': len(runs) // (len(config['v2_variants']) * len(config['modes']) * len(config['budgets'])), 'total_lots': len(refs), 'runs': len(runs), 'updated_at': now()})
    return runs


def summarize(runs, config, candidate=None):
    primary = config['v2_primary']
    candidate = candidate or primary['candidate']
    selected = next((v for v in config['v2_variants'] if v['id'] == candidate), None)
    common = dict(mode=primary['mode'], budget=primary['budget'], metric=primary['metric'],
        seed=primary['bootstrap_seed'], replicates=primary['bootstrap_replicates'])
    grouped = []
    for variant in config['v2_variants']:
        rows = [r for r in runs if r['policy'] == variant['id'] and r['mode'] == primary['mode'] and r['budget'] == primary['budget']]
        if rows:
            grouped.append({'variant': variant['id'], 'lots': len(rows),
                'mean_doi': float(np.mean([r['metrics']['true_doi_confirmed'] for r in rows])),
                'mean_spent': float(np.mean([r['metrics']['spent'] for r in rows])),
                'mean_policy_wall_s': float(np.mean([r['metrics']['timing']['policy_wall_s'] for r in rows])),
                'mean_loading_cost': float(np.mean([r['metrics']['cost_totals']['load'] for r in rows])),
                'mean_stage_cost': float(np.mean([r['metrics']['cost_totals']['stage'] for r in rows]))})
    return {'combined_vs_logistic': paired_comparison(runs, candidate, primary['comparator'], primary=True, target=primary['target_relative_gain'], **common) if selected else None,
        'selection_vs_same_model': paired_comparison(runs, candidate, comparator_for(config, selected), **common) if selected else None,
        'primary_condition_means': grouped,
        'matched_cost': matched_cost_savings(runs, candidate, primary['comparator'], 'candidate_only', max(config['budgets']), 5, .3) if selected else None}


def develop(root):
    if root.exists():
        raise HarnessError('development requires a new directory')
    config = read_json(PROTOCOL)
    check_splits(config)
    root.mkdir(parents=True)
    write_json(root / 'config.json', config)
    started = time.perf_counter()
    refs = {split: generate(root, split, config) for split in ['train', 'calibration', 'development']}
    write_json(root / 'lot-manifest.json', refs)
    train = [privileged(r, 'train') for r in refs['train']]
    calibration = [privileged(r, 'calibration') for r in refs['calibration']]
    fitted = models_for(config, train, calibration)
    yield_model = sensor_yield.fit_review_yield(train, config)
    write_json(root / 'models.json', fitted)
    write_json(root / 'review-yield.json', yield_model)
    runs = run_matrix(root, 'development', refs['development'], config, fitted, yield_model)
    initial = summarize(runs, config)
    choices = [r for r in initial['primary_condition_means'] if r['variant'] != config['v2_primary']['comparator']]
    best = sorted(choices, key=lambda r: (-r['mean_doi'], r['mean_spent'], r['variant']))[0]['variant']
    result = {'kind': 'development_only_v3', 'generated_at': now(), 'runs': runs,
        'summary': summarize(runs, config, best), 'selected_candidate': best,
        'selection_rule': 'highest development mean confirmed DOI at 360 CU candidate-only; then lower mean spent; then variant ID',
        'wall_seconds': time.perf_counter() - started, 'jev_status': 'unavailable_no_genuine_notes',
        'limitations': ['Authored numeric synthetic observations; not real SEM accuracy.', 'Development selects candidates, never final performance evidence.']}
    write_json(root / 'development.json', result)
    return {'status': 'development_complete', 'root': root.as_posix(), 'runs': len(runs), 'selected_candidate': best, 'summary': result['summary']}


def freeze(root):
    if (root / 'freeze.json').exists() or (root / 'campaign-started.json').exists():
        raise HarnessError('already frozen or started')
    config = read_json(root / 'config.json')
    development = read_json(root / 'development.json')
    models = read_json(root / 'models.json')
    refs = read_json(root / 'lot-manifest.json')
    check_splits(config)
    for split, items in refs.items():
        for ref in items:
            if verify_lot(ref)['split'] != split:
                raise HarnessError('wrong lot split receipt')
    candidate = development['selected_candidate']
    variant = next(v for v in config['v2_variants'] if v['id'] == candidate)
    config['v2_primary'].update(candidate=candidate, same_model_comparator=comparator_for(config, variant))
    write_json(root / 'frozen-config.json', config)
    receipt = {'schema_version': 1, 'study': 'v3', 'created_at': now(), 'source_commit': git_head(), 'source_hashes': source_hashes(),
        'file_hashes': {f: sha256_file(root / f) for f in ['frozen-config.json', 'models.json', 'review-yield.json', 'development.json', 'lot-manifest.json']},
        'model_hashes': {key: model.hash_model(m) for key, m in models.items()}, 'candidate': candidate,
        'test_lots': len(config['v2_splits']['test']),
        'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'catboost': importlib.metadata.version('catboost')},
        'm5_authorization': 'coordinator after focused tests, integration and independent boundary checks; autonomous user request',
        'test_generation_has_started': False}
    receipt['receipt_sha256'] = digest(receipt)
    write_json(root / 'freeze.json', receipt)
    return {'status': 'frozen', 'candidate': candidate, 'receipt_sha256': receipt['receipt_sha256']}


def verify_freeze(root):
    receipt = read_json(root / 'freeze.json')
    if digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'}) != receipt['receipt_sha256'] or source_hashes() != receipt['source_hashes']:
        raise HarnessError('frozen source or receipt changed')
    for filename, expected in receipt['file_hashes'].items():
        if sha256_file(root / filename) != expected:
            raise HarnessError('frozen file changed: ' + filename)
    return receipt


def campaign(root):
    receipt = verify_freeze(root)
    if (root / 'campaign-started.json').exists():
        raise HarnessError('campaign already started; no silent resume')
    config = read_json(root / 'frozen-config.json')
    models = read_json(root / 'models.json')
    yield_model = read_json(root / 'review-yield.json')
    write_json(root / 'campaign-started.json', {'created_at': now(), 'freeze_sha256': receipt['receipt_sha256']})
    started = time.perf_counter()
    refs = generate(root, 'test', config)
    write_json(root / 'test-manifest.json', refs)
    runs = run_matrix(root, 'test', refs, config, models, yield_model)
    lots = []
    for ref in refs:
        public = load_public(Path(ref['path']))
        public.update(seed=ref['seed'], scenario=ref['scenario'])
        lots.append({'public': public, 'oracle': _load_npz(Path(ref['path']) / 'oracle.npz')})
    result = {'kind': 'held_out_synthetic_v3', 'generated_at': now(), 'freeze_sha256': receipt['receipt_sha256'], 'runs': runs,
        'summary': summarize(runs, config), 'classification': {key: model.evaluate_model(m, lots, config) for key, m in models.items()},
        'wall_seconds': time.perf_counter() - started, 'jev_status': 'unavailable_no_genuine_notes',
        'limitations': ['Authored synthetic numeric evidence, not SEM-image or factory performance.', 'Review yield and latent DOI probabilities have different meanings.', 'Equipment CU and measured CPU time are separate.']}
    write_json(root / 'held-out.json', result)
    return {'status': 'campaign_complete', 'runs': len(runs), 'root': root.as_posix(), 'summary': result['summary'], 'wall_seconds': result['wall_seconds']}


def report(root):
    result = read_json(root / 'held-out.json')
    path = root / 'report.md'
    path.write_text('# Inspection v3 held-out synthetic results\n\nAuthored numeric synthetic evidence; not factory accuracy.\n\n```json\n' + json.dumps(result['summary'], indent=2) + '\n```\n', encoding='utf-8', newline='\n')
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
