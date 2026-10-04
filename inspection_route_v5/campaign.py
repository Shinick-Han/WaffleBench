"""Route v5 study stages: build, dev, freeze, test (independent, one directory per study).

build  --root NEW [--receipt V3_ROOT]  verify and reference the frozen v3 receipt/model, generate dev lots
dev    --root ROOT                     fixed beam3 vs incumbent on development lots (not evidence)
freeze --root ROOT                     hash all new/reused sources, receipt, model, env and dev lot bytes
test   --root ROOT                     one-shot held-out campaign; refused unless frozen and unstarted

Outputs are aggregated summaries and per-run ledgers. Hidden truth stays inside the lot
directories (``oracle.npz``) and is read only after a run finished, for evaluation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import time

import numpy as np

from inspection_review import data
from inspection_review.cli import ControlledReviewSimulator, HarnessError, load_public, read_json, save_lot, sha256_file, write_json
from inspection_review.policies import sanitize_public
from inspection_review.reporting import paired_comparison
from inspection_v2.cli import now, digest, verify_lot
from inspection_v3 import harness as v3_harness, model
from inspection_v3.inference import compile_predictor
from . import harness

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = Path(__file__).with_name('protocol.json')
PROTOCOL_MD = REPO / 'INSPECTION_ROUTE_V5_PROTOCOL.md'
OWNED = ['inspection_route_v5', 'scripts/run_inspection_route_v5.py', 'tests/test_inspection_route_v5.py',
         'INSPECTION_ROUTE_V5_PROTOCOL.md']
REUSED_NAMESPACES = ['inspection_review', 'inspection_v2', 'inspection_v3']
RECEIPT_FILES = ['freeze.json', 'frozen-config.json', 'models.json']


def source_names():
    names = [p for ns in ['inspection_route_v5'] + REUSED_NAMESPACES for p in sorted((REPO / ns).glob('*.py'))]
    names += [REPO / ns / 'protocol.json' for ns in ['inspection_route_v5'] + REUSED_NAMESPACES]
    names += [REPO / f for f in ['scripts/run_inspection_route_v5.py', 'INSPECTION_ROUTE_V5_PROTOCOL.md',
                                 'INSPECTION_V3_CONTRACT.md', 'INSPECTION_V2_CONTRACT.md', 'pyproject.toml', 'uv.lock']]
    return [str(p.relative_to(REPO)).replace('\\', '/') for p in names]


def source_hashes():
    return {name: sha256_file(REPO / name) for name in source_names()}


def environment():
    return {'python': platform.python_version(), 'numpy': np.__version__, 'catboost': importlib.metadata.version('catboost')}


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args], text=True).strip()


def lot_rows(protocol, split):
    s = protocol['splits'][split]
    return [{'seed': s['base_seed'] + k * 100 + i, 'scenario': name}
            for k, name in enumerate(protocol['scenarios']) for i in range(s['lots_per_scenario'])]


def _seeds_in(obj, out):
    if isinstance(obj, dict):
        for key, x in obj.items():
            if key == 'seed' and isinstance(x, int) and not isinstance(x, bool):
                out.add(x)
            elif key.endswith('seeds') and isinstance(x, list):
                out.update(s for s in x if isinstance(s, int) and not isinstance(s, bool))
            if key.endswith('seeds_by_scenario') and isinstance(x, dict):
                out.update(s for seeds in x.values() for s in seeds)
            _seeds_in(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _seeds_in(x, out)
    return out


def check_splits(protocol, v3_config):
    """Fresh, disjoint seeds that no tabular v1-v4 protocol or the frozen v3 receipt used."""
    previous = _seeds_in(data.load_config(), set())
    for path in ['inspection_v2/protocol.json', 'inspection_v3/protocol.json', 'inspection_v4/protocol.json']:
        _seeds_in(read_json(REPO / path), previous)
    _seeds_in(v3_config, previous)
    seen = set()
    for split in ['development', 'test']:
        for row in lot_rows(protocol, split):
            if row['seed'] in previous or row['seed'] in seen:
                raise HarnessError('duplicate or previously used seed')
            if row['scenario'] not in v3_config['splits']['test_seeds_by_scenario']:
                raise HarnessError('unknown scenario')
            seen.add(row['seed'])


def verify_receipt(receipt_root, protocol):
    """Exact frozen v3 receipt, config and cb400/identity model. Returns (v3 config, model, hashes)."""
    receipt_root = Path(receipt_root)
    exp = protocol['receipt']
    receipt = read_json(receipt_root / 'freeze.json')
    if receipt['receipt_sha256'] != exp['receipt_sha256'] or \
            digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'}) != exp['receipt_sha256']:
        raise HarnessError('v3 receipt changed')
    hashes = {f: sha256_file(receipt_root / f) for f in RECEIPT_FILES}
    if hashes['frozen-config.json'] != exp['frozen_config_sha256'] or hashes['models.json'] != exp['models_sha256'] or \
            receipt['file_hashes']['frozen-config.json'] != exp['frozen_config_sha256'] or \
            receipt['file_hashes']['models.json'] != exp['models_sha256']:
        raise HarnessError('v3 receipt files changed')
    config = read_json(receipt_root / 'frozen-config.json')
    if config['v2_primary']['candidate'] != exp['incumbent_variant'] or receipt['candidate'] != exp['incumbent_variant']:
        raise HarnessError('incumbent differs from the frozen v3 candidate')
    fitted = read_json(receipt_root / 'models.json')[exp['model_key']]
    if model.hash_model(fitted) != exp['model_hash'] or receipt['model_hashes'][exp['model_key']] != exp['model_hash']:
        raise HarnessError('frozen model changed')
    if environment() != receipt['environment']:
        raise HarnessError('environment differs from the frozen v3 receipt')
    return config, fitted, hashes


def study_config(v3_config, protocol):
    """Frozen v3 config unchanged plus a ``route_v5`` block read only by the beam planner."""
    config = copy.deepcopy(v3_config)
    config['route_v5'] = {'beam': dict(protocol['beam'])}
    return config


def generate(root, split, protocol, config):
    refs = []
    for row in lot_rows(protocol, split):
        lot = data.generate_lot(row['seed'], row['scenario'], config)
        refs.append(save_lot(root / 'lots' / split / lot['public']['lot_id'], lot, split, row['seed'], row['scenario']))
    return refs


def lot_bytes(refs):
    out = {}
    for ref in refs:
        d = Path(ref['path'])
        out[ref['lot_id']] = {f: sha256_file(d / f) for f in ['metadata.json', 'public.npz', 'oracle.npz']}
    return out


def run_lots(root, split, refs, protocol, config, fitted):
    runs = []
    predictor = compile_predictor(fitted)
    for n, ref in enumerate(refs, 1):
        meta = verify_lot(ref)
        if meta['split'] != split:
            raise HarnessError('wrong lot split receipt')
        public = load_public(Path(ref['path']))
        frozen_p = predictor.predict(public['features'])
        view = sanitize_public(public, fitted['mean'], fitted['scale'])
        for variant in protocol['variants']:
            simulator = ControlledReviewSimulator(Path(ref['path']), data)
            run = harness.run_selection(view, simulator, policy_name=variant['policy'], mode=protocol['mode'],
                                        budget=protocol['budget'], frozen_model=fitted, frozen_p=frozen_p,
                                        model_api=model, config=config)
            if [(r['site_index'], a['attempt']) for r in run['rows'] for a in r['attempts']] != simulator.calls:
                raise HarnessError('sensor calls differ from paid ledger')
            metrics = v3_harness.evaluate_run(run, simulator.release_for_evaluation(), public['candidate'], frozen_p, config)
            ledger = root / 'ledgers' / split / meta['lot_id'] / f"{variant['id']}.json"
            write_json(ledger, {'schema_version': 1, 'study': 'route_v5', 'split': split, 'variant': variant,
                                'lot_id': meta['lot_id'], 'model_hash': model.hash_model(fitted),
                                'frozen_p_sha256': hashlib.sha256(frozen_p.tobytes()).hexdigest(),
                                'selection_reward_semantics': 'latent_doi_probability', 'mode': protocol['mode'],
                                'run': run, 'metrics': metrics})
            runs.append({'status': 'complete', 'lot_id': meta['lot_id'], 'scenario': meta['scenario'],
                         'policy': variant['id'], 'mode': protocol['mode'], 'budget': protocol['budget'],
                         'metrics': metrics, 'ledger_path': ledger.as_posix(), 'ledger_sha256': sha256_file(ledger)})
        write_json(root / f'progress-{split}.json', {'completed_lots': n, 'total_lots': len(refs), 'updated_at': now()})
    return runs


def summarize(runs, protocol):
    p = protocol['primary']
    primary = paired_comparison(runs, p['candidate'], p['comparator'], p['mode'], p['budget'], p['metric'],
                                p['bootstrap_seed'], p['bootstrap_replicates'], target=p['target_relative_gain'], primary=True)
    means = []
    for variant in protocol['variants']:
        rows = [r for r in runs if r['policy'] == variant['id']]
        by_scenario = {s: float(np.mean([r['metrics'][p['metric']] for r in rows if r['scenario'] == s]))
                       for s in protocol['scenarios'] if any(r['scenario'] == s for r in rows)}
        means.append({'variant': variant['id'], 'lots': len(rows),
                      'mean_true_doi_confirmed': float(np.mean([r['metrics']['true_doi_confirmed'] for r in rows])),
                      'mean_by_scenario': by_scenario,
                      'mean_spent': float(np.mean([r['metrics']['spent'] for r in rows])),
                      'mean_attempted_sites': float(np.mean([r['metrics']['attempted_sites'] for r in rows])),
                      'mean_loading_cost': float(np.mean([r['metrics']['cost_totals']['load'] for r in rows])),
                      'mean_stage_cost': float(np.mean([r['metrics']['cost_totals']['stage'] for r in rows])),
                      'secondary_mean_policy_wall_s': float(np.mean([r['metrics']['timing']['policy_wall_s'] for r in rows])),
                      'secondary_mean_policy_cpu_s': float(np.mean([r['metrics']['timing']['policy_cpu_s'] for r in rows]))})
    return {'primary': primary, 'condition_means': means,
            'timing_note': 'software policy timing only; secondary, not equipment CU'}


def build(root, receipt_root=None):
    if root.exists():
        raise HarnessError('build requires a new directory')
    protocol = read_json(PROTOCOL)
    receipt_root = Path(receipt_root or protocol['receipt']['default_root'])
    v3_config, _, hashes = verify_receipt(receipt_root, protocol)
    check_splits(protocol, v3_config)
    config = study_config(v3_config, protocol)
    root.mkdir(parents=True)
    write_json(root / 'protocol.json', protocol)
    write_json(root / 'config.json', config)
    write_json(root / 'receipt-ref.json', {'receipt_root': receipt_root.resolve().as_posix(), 'file_hashes': hashes,
                                            'environment': environment(), 'created_at': now()})
    refs = generate(root, 'development', protocol, config)
    write_json(root / 'lot-manifest.json', {'development': refs})
    return {'status': 'built', 'root': root.as_posix(), 'development_lots': len(refs)}


def _load(root):
    protocol = read_json(root / 'protocol.json')
    ref = read_json(root / 'receipt-ref.json')
    v3_config, fitted, hashes = verify_receipt(ref['receipt_root'], protocol)
    if hashes != ref['file_hashes'] or read_json(root / 'config.json') != study_config(v3_config, protocol):
        raise HarnessError('study config or receipt reference changed')
    return protocol, read_json(root / 'config.json'), fitted, ref


def develop(root):
    if (root / 'development.json').exists() or (root / 'freeze.json').exists():
        raise HarnessError('development already complete')
    protocol, config, fitted, _ = _load(root)
    if protocol != read_json(PROTOCOL):
        raise HarnessError('study protocol differs from source protocol')
    refs = read_json(root / 'lot-manifest.json')['development']
    started = time.perf_counter()
    runs = run_lots(root, 'development', refs, protocol, config, fitted)
    result = {'kind': 'development_only_route_v5', 'generated_at': now(), 'runs': runs, 'summary': summarize(runs, protocol),
              'selection': 'none: fixed beam3 settings preregistered; no hyperparameter scan',
              'wall_seconds': time.perf_counter() - started,
              'limitations': ['Authored numeric synthetic observations; not real SEM accuracy.',
                              'Development results are not held-out evidence.']}
    write_json(root / 'development.json', result)
    return {'status': 'development_complete', 'runs': len(runs), 'summary': result['summary']}


def _freeze_body(root, protocol):
    ref = read_json(root / 'receipt-ref.json')
    refs = read_json(root / 'lot-manifest.json')['development']
    return {'schema_version': 1, 'study': 'route_v5', 'source_hashes': source_hashes(),
            'study_file_hashes': {f: sha256_file(root / f) for f in
                                  ['protocol.json', 'config.json', 'receipt-ref.json', 'lot-manifest.json', 'development.json']},
            'receipt_root': ref['receipt_root'], 'receipt_file_hashes': ref['file_hashes'],
            'receipt_sha256': protocol['receipt']['receipt_sha256'], 'model_hash': protocol['receipt']['model_hash'],
            'environment': environment(), 'development_lot_bytes': lot_bytes(refs),
            'test_lots': lot_rows(protocol, 'test'), 'primary': protocol['primary']}


def freeze(root):
    if (root / 'freeze.json').exists() or (root / 'test-started.json').exists():
        raise HarnessError('already frozen or started')
    if not (root / 'development.json').exists():
        raise HarnessError('development must complete before freeze')
    if (root / 'lots' / 'test').exists():
        raise HarnessError('test lots exist before freeze')
    protocol, *_ = _load(root)
    if protocol != read_json(PROTOCOL):
        raise HarnessError('study protocol differs from source protocol')
    if git('status', '--porcelain', '--', *source_names()):
        raise HarnessError('frozen sources must be committed (clean working tree)')
    body = _freeze_body(root, protocol)
    body.update(created_at=now(), source_commit=git('rev-parse', 'HEAD'), test_generation_has_started=False,
                m5_authorization='coordinator only; the actual test stage is run by the root coordinator')
    body['receipt_sha256_v5'] = digest(body)
    write_json(root / 'freeze.json', body)
    return {'status': 'frozen', 'receipt_sha256_v5': body['receipt_sha256_v5'], 'source_commit': body['source_commit']}


def verify_freeze(root):
    if not (root / 'freeze.json').exists():
        raise HarnessError('test refused: study is not frozen')
    frozen = read_json(root / 'freeze.json')
    if digest({k: v for k, v in frozen.items() if k != 'receipt_sha256_v5'}) != frozen['receipt_sha256_v5']:
        raise HarnessError('freeze receipt changed')
    protocol, *_ = _load(root)
    current = _freeze_body(root, protocol)
    for key, value in current.items():
        if frozen[key] != value:
            raise HarnessError('frozen input changed: ' + key)
    return frozen


def test(root):
    if (root / 'test-started.json').exists():
        raise HarnessError('test already started; one-shot, no silent resume')
    frozen = verify_freeze(root)
    if (root / 'lots' / 'test').exists():
        raise HarnessError('test lots exist before the marker')
    protocol, config, fitted, _ = _load(root)
    write_json(root / 'test-started.json', {'created_at': now(), 'freeze_sha256': frozen['receipt_sha256_v5']})
    started = time.perf_counter()
    refs = generate(root, 'test', protocol, config)
    write_json(root / 'test-manifest.json', refs)
    runs = run_lots(root, 'test', refs, protocol, config, fitted)
    result = {'kind': 'held_out_synthetic_route_v5', 'generated_at': now(), 'freeze_sha256': frozen['receipt_sha256_v5'],
              'runs': runs, 'summary': summarize(runs, protocol), 'wall_seconds': time.perf_counter() - started,
              'limitations': ['Authored synthetic numeric evidence, not SEM-image or factory performance.',
                              'Equipment CU and measured CPU time are separate; timing is secondary.']}
    write_json(root / 'held-out.json', result)
    return {'status': 'test_complete', 'runs': len(runs), 'summary': result['summary']}


STAGES = {'build': build, 'dev': develop, 'freeze': freeze, 'test': test}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('stage', choices=list(STAGES))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, default=None, help='frozen v3 receipt directory (build only)')
    args = parser.parse_args(argv)
    try:
        out = build(args.root, args.receipt) if args.stage == 'build' else STAGES[args.stage](args.root)
        print(json.dumps(out, allow_nan=False))
        return 0
    except Exception as exc:
        if args.root.exists():
            write_json(args.root / f'FAILED-{args.stage}.json', {'created_at': now(), 'stage': args.stage,
                                                                 'exception_type': type(exc).__name__, 'detail': str(exc)})
        print(json.dumps({'status': 'failed', 'stage': args.stage, 'error': type(exc).__name__, 'detail': str(exc)}))
        return 1
