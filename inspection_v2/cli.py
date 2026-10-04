"""Separate v2 synthetic inspection development, freeze, held-out campaign and reports.

python -m inspection_v2.cli develop --root NEW_DIRECTORY
python -m inspection_v2.cli freeze --root DEVELOPMENT_DIRECTORY
python -m inspection_v2.cli campaign --root FROZEN_DIRECTORY
python -m inspection_v2.cli report --root COMPLETED_DIRECTORY
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import time

import numpy as np

from inspection_review import data
from inspection_review.cli import (ControlledReviewSimulator, HarnessError, _load_npz,
                                    load_public, read_json, save_lot, sha256_file, write_json)
from inspection_review.policies import sanitize_public
from inspection_review.reporting import paired_comparison, matched_cost_savings
from . import harness, model

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = Path(__file__).with_name('protocol.json')
SOURCES = tuple(str(p.relative_to(REPO)).replace('\\', '/') for p in sorted((REPO / 'inspection_v2').glob('*.py'))) + (
    'inspection_v2/protocol.json', 'INSPECTION_V2_CONTRACT.md', 'pyproject.toml', 'uv.lock',
    'inspection_review/data.py', 'inspection_review/model.py', 'inspection_review/policies.py',
    'inspection_review/harness.py', 'inspection_review/cli.py', 'inspection_review/reporting.py')

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def git_head():
    return subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'], text=True).strip()

def source_hashes():
    return {name: sha256_file(REPO / name) for name in SOURCES}

def check_splits(config):
    known = set()
    v1 = data.load_config()
    old = set(v1['splits']['train_seeds'] + v1['splits']['validation_seeds'])
    old.update(s for seeds in v1['splits']['test_seeds_by_scenario'].values() for s in seeds)
    for split in ['train','calibration','development','test']:
        entries = config['v2_splits'][split]
        if not entries:
            raise HarnessError(f'empty split: {split}')
        for item in entries:
            seed = item['seed']
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 3000 or seed in known or seed in old:
                raise HarnessError('duplicate, invalid or previously used seed')
            if item['scenario'] not in config['splits']['test_seeds_by_scenario']:
                raise HarnessError('unknown scenario')
            known.add(seed)
    if set(config['splits']['train_seeds']) != {r['seed'] for r in config['v2_splits']['train']}:
        raise HarnessError('training seed contracts differ')

def generate(root, split, config):
    refs = []
    for row in config['v2_splits'][split]:
        lot = data.generate_lot(row['seed'], row['scenario'], config)
        ref = save_lot(root / 'lots' / split / lot['public']['lot_id'], lot, split, row['seed'], row['scenario'])
        refs.append(ref)
    return refs

def verify_lot(ref):
    directory = Path(ref['path'])
    if sha256_file(directory / 'metadata.json') != ref['metadata_sha256']:
        raise HarnessError('lot metadata changed')
    meta = read_json(directory / 'metadata.json')
    for kind in ['public','oracle']:
        # v1 storage receipt carries the exact NPZ bytes, with no pickle.
        expected = meta.get(kind + '_sha256')
        if expected is None:
            expected = meta.get('files', {}).get(kind + '.npz')
        if expected is None or sha256_file(directory / (kind + '.npz')) != expected:
            raise HarnessError('lot data digest missing or changed')
    return meta

def privileged(ref, split):
    meta = verify_lot(ref)
    if split not in ['train','calibration'] or meta['split'] != split:
        raise HarnessError('privileged loader only admits train/calibration')
    public = load_public(Path(ref['path']))
    public.update(seed=meta['seed'], scenario=meta['scenario'])
    return {'public':public, 'oracle':_load_npz(Path(ref['path']) / 'oracle.npz')}

def models_for(config, train, calibration):
    out = {}
    for family in ['logistic','catboost']:
        settings = copy.deepcopy(config)
        settings['v2_model'] = {'family':'logistic'} if family == 'logistic' else {**config['v2_model'],'family':family}
        raw = model.train_model(train, settings)
        out[family + '/identity'] = raw
        if family == 'catboost':
            out[family + '/isotonic'] = model.fit_calibration(raw, calibration, settings, method='isotonic')
    return out

def run_matrix(root, split, refs, config, models):
    runs = []
    for ref in refs:
        meta = verify_lot(ref)
        public = load_public(Path(ref['path']))
        predictions = {key:model.predict(m, public['features']) for key,m in models.items()}
        views = {key:sanitize_public(public, m['mean'], m['scale']) for key,m in models.items()}
        for variant in config['v2_variants']:
            key = variant['family'] + '/' + variant['calibration']
            fitted, frozen_p, view = models[key], predictions[key], views[key]
            for mode in config['modes']:
                for budget in config['budgets']:
                    simulator = ControlledReviewSimulator(Path(ref['path']), data)
                    run = harness.run_selection(view, simulator, policy_name=variant['policy'], mode=mode,
                        budget=budget, frozen_model=fitted, frozen_p=frozen_p, model_api=model, config=config)
                    calls = [(r['site_index'],a['attempt']) for r in run['rows'] for a in r['attempts']]
                    if calls != simulator.calls:
                        raise HarnessError('sensor calls differ from paid ledger')
                    metrics = harness.evaluate_run(run, simulator.release_for_evaluation(), public['candidate'], frozen_p, config)
                    ledger = root / 'ledgers' / split / meta['lot_id'] / f"{variant['id']}-{mode}-{budget}.json"
                    write_json(ledger, {'schema_version':1,'split':split,'variant':variant,
                        'lot_id':meta['lot_id'],'model_hash':model.hash_model(fitted),'frozen_p_sha256':hashlib.sha256(frozen_p.tobytes()).hexdigest(),
                        'mode':mode,'run':run,'metrics':metrics})
                    runs.append({'status':'complete','lot_id':meta['lot_id'],'scenario':meta['scenario'],'policy':variant['id'],
                        'mode':mode,'budget':budget,'metrics':metrics,'model_hash':model.hash_model(fitted),
                        'ledger_path':ledger.as_posix(),'ledger_sha256':sha256_file(ledger)})
    return runs

def summarize(runs, config, candidate=None):
    primary = config['v2_primary']
    candidate = candidate or primary['candidate']
    common = dict(mode=primary['mode'],budget=primary['budget'],metric=primary['metric'],
                  seed=primary['bootstrap_seed'],replicates=primary['bootstrap_replicates'])
    overall = paired_comparison(runs,candidate,primary['comparator'],primary=True,target=primary['target_relative_gain'],**common)
    same_model = paired_comparison(runs,candidate,primary['same_model_comparator'],**common)
    grouped = []
    for variant in config['v2_variants']:
        rows = [r for r in runs if r['policy']==variant['id'] and r['mode']==primary['mode'] and r['budget']==primary['budget']]
        if not rows:
            continue
        grouped.append({'variant':variant['id'],'lots':len(rows),
            'mean_doi':float(np.mean([r['metrics']['true_doi_confirmed'] for r in rows])),
            'mean_confident_fn_found':float(np.mean([r['metrics']['discovered_frozen_confident_false_negatives'] for r in rows])),
            'mean_spent':float(np.mean([r['metrics']['spent'] for r in rows])),
            'mean_policy_wall_s':float(np.mean([r['metrics']['timing']['policy_wall_s'] for r in rows])),
            'mean_loading_cost':float(np.mean([r['metrics']['cost_totals']['load'] for r in rows])),
            'mean_stage_cost':float(np.mean([r['metrics']['cost_totals']['stage'] for r in rows]))})
    return {'combined_vs_v1_learned':overall,'selection_vs_same_model':same_model,'primary_condition_means':grouped,
            'matched_cost':matched_cost_savings(runs,candidate,primary['comparator'],'candidate_only',max(config['budgets']),5,.3)}

def develop(root):
    if root.exists():
        raise HarnessError('development requires a new output directory')
    config = read_json(PROTOCOL)
    check_splits(config)
    root.mkdir(parents=True)
    write_json(root / 'config.json',config)
    started = time.perf_counter()
    refs = {split:generate(root,split,config) for split in ['train','calibration','development']}
    write_json(root / 'lot-manifest.json',refs)
    train = [privileged(r,'train') for r in refs['train']]
    calibration = [privileged(r,'calibration') for r in refs['calibration']]
    fitted = models_for(config,train,calibration)
    write_json(root / 'models.json',fitted)
    runs = run_matrix(root,'development',refs['development'],config,fitted)
    summary = summarize(runs,config)
    # Freeze the deterministic selection rule before ever generating held-out lots.
    choices = [r for r in summary['primary_condition_means'] if r['variant'] in {
        'catboost_learned','calibrated_learned','calibrated_adaptive_audit','calibrated_route','calibrated_adaptive_route'}]
    best = sorted(choices,key=lambda r:(-r['mean_doi'],r['mean_spent'],r['variant']))[0]['variant']
    report = {'kind':'development_only','generated_at':now(),'runs':runs,'summary':summary,
              'selected_candidate':best,'selection_rule':'highest development mean DOI at 360 CU candidate-only; then lower mean spent; then variant ID',
              'wall_seconds':time.perf_counter()-started,'jev_status':'unavailable_no_genuine_notes',
              'limitations':['Authored synthetic numeric observations; no real wafer image accuracy.',
                             'Development comparison is used for candidate selection, not held-out performance.']}
    write_json(root / 'development.json',report)
    return {'status':'development_complete','root':root.as_posix(),'runs':len(runs),'selected_candidate':best,'summary':summary}

def freeze(root):
    if (root / 'freeze.json').exists():
        raise HarnessError('freeze already exists')
    development = read_json(root / 'development.json')
    config = read_json(root / 'config.json')
    models = read_json(root / 'models.json')
    refs = read_json(root / 'lot-manifest.json')
    check_splits(config)
    for split, items in refs.items():
        for r in items:
            verify_lot(r)
    candidate = development['selected_candidate']
    actual_variant = next(v for v in config['v2_variants'] if v['id']==candidate)
    config['v2_primary']['candidate'] = candidate
    config['v2_primary']['same_model_comparator'] = 'catboost_learned' if actual_variant['calibration']=='identity' else 'calibrated_learned'
    write_json(root / 'frozen-config.json',config)
    receipt = {'schema_version':1,'created_at':now(),'source_commit':git_head(),'source_hashes':source_hashes(),
        'config_sha256':sha256_file(root / 'frozen-config.json'),'models_sha256':sha256_file(root / 'models.json'),
        'model_hashes':{k:model.hash_model(v) for k,v in models.items()},'development_sha256':sha256_file(root / 'development.json'),
        'lot_manifest_sha256':sha256_file(root / 'lot-manifest.json'),
        'candidate':candidate,'test_lots':len(config['v2_splits']['test']),
        'environment':{'python':platform.python_version(),'numpy':np.__version__,'catboost':importlib.metadata.version('catboost')},
        'm5_authorization':'coordinator after focused checks; user requested autonomous implementation',
        'test_generation_has_started':False}
    receipt['receipt_sha256'] = digest(receipt)
    write_json(root / 'freeze.json',receipt)
    return {'status':'frozen','candidate':candidate,'receipt_sha256':receipt['receipt_sha256']}

def verify_freeze(root):
    receipt = read_json(root / 'freeze.json')
    hashed = {k:v for k,v in receipt.items() if k!='receipt_sha256'}
    if digest(hashed)!=receipt['receipt_sha256'] or source_hashes()!=receipt['source_hashes']:
        raise HarnessError('freeze source/receipt changed')
    for filename,key in [('frozen-config.json','config_sha256'),('models.json','models_sha256'),
                         ('development.json','development_sha256'),('lot-manifest.json','lot_manifest_sha256')]:
        if sha256_file(root / filename)!=receipt[key]:
            raise HarnessError('frozen evidence changed: '+filename)
    return receipt

def campaign(root):
    receipt = verify_freeze(root)
    if (root / 'campaign-started.json').exists():
        raise HarnessError('campaign already started; no silent resume')
    config = read_json(root / 'frozen-config.json')
    models = read_json(root / 'models.json')
    write_json(root / 'campaign-started.json',{'created_at':now(),'freeze_sha256':receipt['receipt_sha256']})
    started = time.perf_counter()
    refs = generate(root,'test',config)
    write_json(root / 'test-manifest.json',refs)
    runs = run_matrix(root,'test',refs,config,models)
    test_lots = []
    # Oracle opened only now for posthoc classifier metrics; never handed to a policy.
    for ref in refs:
        pub = load_public(Path(ref['path']))
        pub.update(seed=ref['seed'],scenario=ref['scenario'])
        test_lots.append({'public':pub,'oracle':_load_npz(Path(ref['path']) / 'oracle.npz')})
    classification = {key:model.evaluate_model(fitted,test_lots,config) for key,fitted in models.items()}
    report = {'kind':'held_out_synthetic_v2','generated_at':now(),'freeze_sha256':receipt['receipt_sha256'],
              'runs':runs,'summary':summarize(runs,config),'classification':classification,
              'wall_seconds':time.perf_counter()-started,'jev_status':'unavailable_no_genuine_notes',
              'limitations':['Authored synthetic tabular study; not actual SEM or factory equipment performance.',
                             'Combined model/selection effect and same-model selection effect are distinct.',
                             'Equipment costs are authored CU, separately recorded from measured software runtime.']}
    write_json(root / 'held-out.json',report)
    return {'status':'campaign_complete','runs':len(runs),'root':root.as_posix(),'summary':report['summary'],'wall_seconds':report['wall_seconds']}

def report(root):
    result = read_json(root / 'held-out.json')
    lines = ['# Inspection improvements v2 held-out synthetic results','',
        'This is authored synthetic numeric evidence, not factory or SEM-image accuracy.','',
        '```json',json.dumps(result['summary'],indent=2),'```','',*result['limitations']]
    (root / 'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n')
    return {'status':'report_written','path':(root / 'report.md').as_posix()}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['develop','freeze','campaign','report'])
    parser.add_argument('--root',required=True,type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(globals()[args.command](args.root),allow_nan=False))
        return 0
    except Exception as exc:
        if args.root.exists():
            write_json(args.root / ('FAILED-'+args.command+'.json'),{'created_at':now(),'stage':args.command,'exception_type':type(exc).__name__,'detail':str(exc)})
        print(json.dumps({'status':'failed','stage':args.command,'error':type(exc).__name__,'detail':str(exc),'root':args.root.as_posix()}))
        return 1

if __name__=='__main__':
    raise SystemExit(main())
