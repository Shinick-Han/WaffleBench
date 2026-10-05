"""Read-only independent arithmetic checks on completed development artifacts."""
import argparse
import json
from pathlib import Path

import numpy as np

from sem_efficiency.core import digest, now, write
from sem_images import manifest as mf


def close(a,b):
    if a is None or b is None:
        assert a is b
    else:
        assert abs(a-b)<=1e-9*max(1,abs(a),abs(b)),(a,b)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);args=p.parse_args();root=Path(args.root)
    sem=root/'experiment-v1'; protocol=json.loads((sem/'protocol.json').read_text())
    frozen=json.loads((sem/'evaluation-freeze.json').read_text())
    results=json.loads((sem/'results.json').read_text())
    manifest=mf.load(root.parent/'sem-build-20261005/integrated-pretrained64-e3-r2/split/manifest.json')
    expected=set(protocol['pools']['development_evaluation'])
    assert not expected & {r['id'] for r in manifest.items('test')}
    reader=manifest.reader('calibration',masks_allowed=True)
    truths={i:reader.load_mask(i) for i in expected}
    checked=0
    for name,result in results.items():
        receipt=json.loads((sem/'predictions'/name/'development_evaluation'/'receipt.json').read_text())
        assert {r['id'] for r in receipt['items']}==expected
        for mode in ('fixed','tuned'):
            counts=dict(tp=0,fp=0,tn=0,fn=0)
            threshold=result[mode]['threshold']
            for row in receipt['items']:
                assert digest(row['file'])==row['sha256']
                prediction=np.load(row['file'],allow_pickle=False)>=threshold
                truth=truths[row['id']]
                counts['tp']+=int((prediction & truth).sum())
                counts['fp']+=int((prediction & ~truth).sum())
                counts['fn']+=int((~prediction & truth).sum())
                counts['tn']+=int((~prediction & ~truth).sum())
            reported=result[mode]['summary']['pixel']
            assert all(reported[k]==counts[k] for k in counts)
            close(reported['dice'],2*counts['tp']/(2*counts['tp']+counts['fp']+counts['fn']))
            checked+=1
    for name,row in frozen['training'].items():
        assert digest(sem/(name+'.pt'))==row['model_sha256']
    logs=json.loads((sem/'access-log.json').read_text())
    assert not any(isinstance(r,list) and r[0]=='test' for r in logs)
    assert not any(isinstance(r,dict) and r.get('role')=='test' for r in logs)
    routing=root/'routing-development-v1';count=unknown=0
    for file in (routing/'runs').glob('*.json'):
        record=json.loads(file.read_text());run=record['run'];reported=record['metrics']
        job=json.loads((routing/'jobs'/f'{record["regime"]}-{record["lot"]}.json').read_text())
        truth=job['archive']['reference']['doi_by_site']
        ids=[r['site_id'] for r in run['rows']];assert len(ids)==len(set(ids))
        positive=[r['site_id'] for r in run['rows'] if r['reported_doi'] is True]
        confirmed=sum(truth[i] for i in positive);false=len(positive)-confirmed
        assert confirmed==reported['confirmed_reference_doi']
        assert false==reported['false_confirmations']
        assert sum(truth.values())-confirmed==reported['escapes']
        charged=sum(r['charged_s'] for r in run['rows'])
        close(charged,run['spent_s']);assert charged<=run['budget_s']+1e-9
        for row in run['rows']:
            assert row['charged_s']<=row['reserved_s']+1e-9
            if row['final_status']!='ok':
                assert row['reported_doi'] is None;unknown+=1
        count+=1
    assert count==640
    workflow=root/'workflow-development-v1'
    reference=json.loads((workflow/'reference.json').read_text())['jobs']
    predictions=json.loads((workflow/'predictions.json').read_text())
    report=json.loads((workflow/'results.json').read_text())
    assert predictions['job_ids']==[j['id'] for j in reference]
    target=np.array([j['recorded_working_hours'] for j in reference])
    for name,values in predictions['predictions'].items():
        close(float(np.abs(target-np.array(values)).mean()),report['models'][name]['mae_hours'])
    wf_protocol=json.loads((workflow/'protocol.json').read_text())
    pools={k:set(v) for k,v in wf_protocol['job_ids'].items()}
    assert not pools['train'] & pools['development_evaluation']
    assert not pools['train'] & pools['tune']
    assert not pools['tune'] & pools['development_evaluation']
    summary={'status':'pass','checked_at':now(),'sem_metric_summaries_recomputed':checked,
             'sem_development_images':len(expected),'original_test_access_logged':False,
             'routing_runs_recomputed':count,'unknown_final_reports_checked':unknown,
             'workflow_jobs':len(target),'model_and_prediction_hashes':'pass',
             'physical_equipment_superiority':False,
             'limits':'Reader logs are operational evidence, not OS-enforced blindness. Component metrics use existing implementation; pixel/reward/cost/MAE arithmetic independently recomputed.'}
    write(root/'independent-verification.json',summary)
    print(json.dumps(summary))


if __name__=='__main__':main()
