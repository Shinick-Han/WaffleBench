"""Export audited v3 evidence and a predeclared first-lot paid replay; no oracle UI."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

import numpy as np

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from inspection_review.cli import load_public, read_json  # noqa: E402
from inspection_v3 import model  # noqa: E402
from inspection_v3.cli import verify_freeze  # noqa: E402
from inspection_v3.inference import compile_predictor  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8', newline='\n')


def timings(fn, repeats=100):
    for _ in range(5):
        fn()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {'median_ms': statistics.median(samples), 'p95_ms': float(np.quantile(samples, .95)),
            'q25_ms': float(np.quantile(samples, .25)), 'q75_ms': float(np.quantile(samples, .75)), 'repeats': repeats}


def export(root):
    receipt = verify_freeze(root)
    audit = read_json(root / 'audit.json')
    if audit['status'] != 'passed' or audit['freeze_sha256'] != receipt['receipt_sha256']:
        raise ValueError('Export requires the matching passed independent audit')
    held = read_json(root / 'held-out.json')
    config = read_json(root / 'frozen-config.json')
    primary = held['summary']['combined_vs_logistic']
    # First protocol test entry, independent of observed score/outcomes.
    first = config['v2_splits']['test'][0]
    ref = next(r for r in read_json(root / 'test-manifest.json') if r['seed'] == first['seed'] and r['scenario'] == first['scenario'])
    record = next(r for r in held['runs'] if r['lot_id'] == ref['lot_id'] and r['policy'] == receipt['candidate'] and r['mode'] == primary['mode'] and r['budget'] == primary['budget'])
    ledger_path = Path(record['ledger_path'])
    if hashlib.sha256(ledger_path.read_bytes()).hexdigest() != record['ledger_sha256']:
        raise ValueError('Replay ledger digest mismatch')
    ledger = read_json(ledger_path)
    public = load_public(Path(ref['path']))
    fitted = read_json(root / 'models.json')[ledger['variant']['model']]
    features = public['features']
    started = time.perf_counter()
    compiled = compile_predictor(fitted)
    compile_ms = (time.perf_counter() - started) * 1000
    old_p, new_p = model.predict(fitted, features), compiled.predict(features)
    np.testing.assert_allclose(old_p, new_p, rtol=0, atol=2e-14)
    # Alternating blocks reduce drift bias; same frozen model and same feature matrix.
    warm_old = timings(lambda: model.predict(fitted, features))
    warm_new = timings(lambda: compiled.predict(features))
    old_again = timings(lambda: model.predict(fitted, features))
    new_again = timings(lambda: compiled.predict(features))
    profile = {'scope': 'Same frozen cb400/identity model, first protocol test lot, 3915 sites; local sequential CPU microbenchmark, not equipment CU or model-quality gain.',
               'model_hash': model.hash_model(fitted), 'lot_id': ref['lot_id'], 'n_sites': len(features),
               'compile_ms': compile_ms, 'baseline': warm_old, 'compiled': warm_new,
               'second_baseline': old_again, 'second_compiled': new_again,
               'max_absolute_probability_difference': float(np.max(np.abs(old_p-new_p))),
               'warm_speedup': warm_old['median_ms']/warm_new['median_ms']}
    write(root / 'inference-profile.json', profile)
    rows = [{k: row[k] for k in ['step', 'site_id', 'wafer', 'cumulative_spend', 'charged', 'label', 'reported_positive', 'reason', 'baseline_p', 'selection_reward', 'selection_reward_semantics', 'status']} for row in ledger['run']['rows']]
    sites = [{'id': str(sid), 'wafer': int(public['wafer'][i]), 'x_mm': float(public['die_xy_mm'][i, 0]),
              'y_mm': float(public['die_xy_mm'][i, 1]), 'candidate': bool(public['candidate'][i]), 'p': float(new_p[i])}
             for i, sid in enumerate(public['site_ids'])]
    classification = []
    for key, entry in held['classification'].items():
        a = entry['aggregate']
        classification.append({'id': key, **{k:a[k] for k in ['precision','average_precision','brier','ece','fp','fn','tp','tn']}, 'recall': a['recall_among_candidates']})
    result = {'schema_version': 1,
              'study': {'name': 'Inspection v3', 'kind': held['kind'], 'generated_at': held['generated_at'], 'freeze_sha256': receipt['receipt_sha256'], 'source_commit':receipt['source_commit'], 'limitations': held['limitations'] + [
                  '100 independent paired lots; primary 360 CU candidate-only, selected before test.',
                  'The gain combines model and routing changes. Routing alone vs the same CB400 model gained 4.0% (secondary).',
                  'Classification recall applies to optical candidates; unmeasured dies remain unknown.',
                  'The 30% matched-cost saving target was not met. Historical training CU is offline and excludes load/stage costs.',
                  'Omnigent live demonstration has separate provenance; this numerical campaign did not invoke an LLM.']},
              'primary': {'candidate':primary['policy'], 'comparator':primary['comparator'], 'mode':primary['mode'], 'budget':primary['budget'], 'mean_candidate':primary['mean_policy'], 'mean_comparator':primary['mean_comparator'], 'mean_difference':primary['mean_difference'], 'ci95':primary['ci95_difference'], 'relative_gain':primary['relative_gain'], 'target':config['v2_primary']['target_relative_gain'], 'success':primary['success'], 'pairs':primary['complete_pairs']},
              'variants': [{'id':r['variant'], **{k:r[k] for k in ['mean_doi','mean_spent','mean_policy_wall_s']}} for r in held['summary']['primary_condition_means']],
              'classification': classification, 'audit': audit,
              'inference': {'baseline_ms':warm_old['median_ms'], 'compiled_ms':warm_new['median_ms'], 'scope':profile['scope']},
              'replay': {'lot_id':ref['lot_id'], 'variant':receipt['candidate'], 'budget':record['budget'], 'spent':ledger['run']['spent'],
                         'selection_rule':'First protocol test entry, no outcome-dependent choice.',
                         'geometry':{'wafer_diameter_mm':300,'die_width_mm':8,'die_height_mm':6,'scribe_mm':.08,'edge_exclusion_mm':3}, 'sites':sites,'rows':rows}}
    write(APP / 'web/data/inspection-v3.json', result)
    evidence = APP / 'evidence/inspection-improvements-v3'
    for name, contents in [('summary.json',held['summary']), ('audit.json',audit), ('freeze.json',receipt), ('protocol.json',config), ('inference-profile.json',profile), ('classification-aggregate.json', {k:{'aggregate':v['aggregate'], 'scope':{'fit_overlap_lot_ids':v['scope']['fit_overlap_lot_ids'],'excludes':v['scope']['excludes']}} for k,v in held['classification'].items()})]:
        write(evidence / name, contents)
    return {'status':'exported', 'replay_rows':len(rows), 'replay_sites':len(sites), 'primary':result['primary'], 'profile':profile}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    print(json.dumps(export(parser.parse_args().root), allow_nan=False))
