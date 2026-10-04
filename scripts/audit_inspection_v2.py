"""Independent posthoc ledger audit. Never selects actions or changes frozen results."""
import hashlib
import json
import math
from pathlib import Path
import sys
from datetime import datetime, timezone
import numpy as np

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from inspection_review import data
from inspection_review.cli import read_json, load_public, _load_npz
from inspection_v2 import model

root = Path(sys.argv[1])
config = read_json(root / 'frozen-config.json')
models = read_json(root / 'models.json')
receipt = read_json(root / 'freeze.json')
heldout = read_json(root / 'held-out.json')
refs = {r['lot_id']:r for r in read_json(root / 'test-manifest.json')}
assert len(refs)==len(config['v2_splits']['test'])
assert len(heldout['runs'])==len(refs)*len(config['v2_variants'])*len(config['modes'])*len(config['budgets'])
protected = read_json(APP / 'evidence/inspection-research/protected-before-v1.json')['protected_sha256']
for file,digest in protected.items():
    assert hashlib.sha256((APP / file).read_bytes()).hexdigest()==digest, file
cache = {}
total_rows = total_attempts = audits = 0
for run_record in heldout['runs']:
    ledger_path = Path(run_record['ledger_path'])
    assert hashlib.sha256(ledger_path.read_bytes()).hexdigest()==run_record['ledger_sha256']
    ledger = read_json(ledger_path)
    lot_id = run_record['lot_id']
    if lot_id not in cache:
        lotdir = Path(refs[lot_id]['path'])
        pub = load_public(lotdir)
        oracle = _load_npz(lotdir/'oracle.npz')
        preds = {key:model.predict(m,pub['features']) for key,m in models.items()}
        cache[lot_id] = (pub,oracle,preds)
    pub,oracle,preds = cache[lot_id]
    key = ledger['variant']['family']+'/'+ledger['variant']['calibration']
    frozen_p = preds[key]
    assert hashlib.sha256(frozen_p.tobytes()).hexdigest()==ledger['frozen_p_sha256']
    assert model.hash_model(models[key])==ledger['model_hash']
    spent = 0.
    previous = None
    visited = set()
    confirmed = confident_fn = fn = 0
    for row in ledger['run']['rows']:
        i = row['site_index']
        assert 0<=i<len(pub['site_ids']) and i not in visited
        visited.add(i)
        assert row['site_id']==pub['site_ids'][i]
        cand = bool(pub['candidate'][i])
        assert cand==row['original_candidate']
        if run_record['mode']=='candidate_only':
            assert cand
        assert math.isclose(row['baseline_p'],float(frozen_p[i]),abs_tol=1e-12)
        assert not {'seed','scenario','oracle','doi','physical','kind','electrical_effect','review_positive','review_status'} & set(row['components'])
        for ev in row['evidence_refs']:
            assert ev['site_id'] in {pub['site_ids'][j] for j in visited if j!=i}
        c = config['cost']
        switch = previous is None or pub['wafer'][previous]!=pub['wafer'][i]
        movement = 0. if switch else float(np.linalg.norm(pub['xy'][i]-pub['xy'][previous]))
        expected = {'load':c['wafer_load'] if switch else 0.,
                    'stage':c['stage_base']+c['stage_per_normalized_distance']*movement,
                    'dwell':c['dwell'], 'outside_rescan':0. if cand else c['outside_rescan'], 'retry':0.}
        reservation = sum(expected.values())+c['retry_dwell']*c['retry_limit']
        assert math.isclose(reservation,row['reserved_cost'],abs_tol=1e-9)
        assert spent+reservation<=run_record['budget']+1e-9
        observations = []
        for attempt in row['attempts']:
            obs = data.review_observation(oracle,i,attempt['attempt'])
            for field in ['status','reported_doi','reported_kind','quality']:
                assert obs[field]==attempt[field]
            observations.append(obs)
        first_ok = observations[0]['status']=='ok' and observations[0]['reported_doi'] is not None
        assert len(observations)==(1 if first_ok else 2)
        if len(observations)==2:
            expected['retry'] = c['retry_dwell']
        for field,value in expected.items():
            assert math.isclose(row['cost'][field],value,abs_tol=1e-9)
        charged = sum(expected.values())
        assert math.isclose(charged,row['charged'],abs_tol=1e-9)
        spent += charged
        assert math.isclose(spent,row['cumulative_spend'],abs_tol=1e-8)
        ok = [o for o in observations if o['status']=='ok' and o['reported_doi'] is not None]
        label = ok[-1]['reported_doi'] if ok else None
        assert label==row['label']
        rep_pos = bool(any(o['reported_doi'] for o in ok))
        assert rep_pos==row['reported_positive']
        true_confirm = bool(oracle['doi'][i] and rep_pos)
        confirmed += true_confirm
        fn += true_confirm and cand and frozen_p[i]<config['model']['classification_threshold']
        confident_fn += true_confirm and cand and frozen_p[i]<config['model']['confident_negative_threshold']
        if 'audit' in row['reason']:
            audits += 1
            assert cand and row['frozen_negative']
            if row['reason']=='adaptive_random_frozen_negative_audit':
                comp = row['components']
                assert 0<comp['selection_propensity']<=1
                assert comp['ope_claim']=='none'
        if key.startswith('catboost'):
            assert not ledger['run']['online_updates_enabled']
            assert row['model_update_wall_s']==0
        previous = i
        total_rows += 1
        total_attempts += len(observations)
    assert math.isclose(spent,ledger['run']['spent'],abs_tol=1e-8)
    assert spent<=run_record['budget']+1e-9
    for metric,value in [('true_doi_confirmed',confirmed),('discovered_frozen_false_negatives',fn),
                         ('discovered_frozen_confident_false_negatives',confident_fn)]:
        assert ledger['metrics'][metric]==run_record['metrics'][metric]==value
result = {'status':'passed','generated_at':datetime.now(timezone.utc).isoformat(),
          'runs_checked':len(heldout['runs']),'independent_lots':len(refs),'selected_sites_across_runs':total_rows,
          'sensor_attempts_recomputed':total_attempts,'audit_decisions':audits,
          'budget_violations':0,'hidden_truth_fields_in_components':0,'frozen_p_mismatches':0,
          'protected_files_preserved':len(protected),'freeze_sha256':receipt['receipt_sha256'],
          'audit_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'limitations':['Posthoc audit of authored synthetic data; does not establish factory performance.']}
(root/'audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8',newline='\n')
print(json.dumps(result))
