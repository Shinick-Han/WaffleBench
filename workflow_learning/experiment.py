"""Grouped chronological development study of recorded working hours per job."""
import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from sem_efficiency import NOTICE
from sem_efficiency.core import digest, now, source_freeze, verify_source, write


def date(value):
    return datetime.strptime(value, '%d.%m.%Y %H:%M:%S')


def load_jobs(path):
    grouped=defaultdict(list)
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            grouped[row['JOB_ID']].append(row)
    jobs=[]; rejected=[]
    for jid,rows in grouped.items():
        try:
            rows.sort(key=lambda r:date(r['TASK_SUBMISSION_DATE']))
            first=rows[0]
            submitted=date(first['JOB_SUBMISSION_DATE'])
            hours=[float(r['TASK_WORKING_TIME']) for r in rows]
            if not hours or any(not math.isfinite(v) or v<0 for v in hours) or sum(hours)<=0:
                raise ValueError('invalid recorded task hours')
            quantity=float(first['JSH_QTY_STRESSED'])
            if not math.isfinite(quantity) or quantity<0:
                raise ValueError('invalid requested quantity')
            jobs.append({'id':jid,'submitted':submitted.isoformat(),'day':submitted.date().isoformat(),
                         'type':first['JOB_BASICTYPE_H'],'package':first['JOB_PACKAGE_H'],
                         'quantity':quantity,'recorded_working_hours':sum(hours),'tasks':len(rows)})
        except (ValueError,KeyError) as e:
            rejected.append({'job':jid,'reason':str(e)})
    return sorted(jobs,key=lambda j:(j['submitted'],j['id'])),rejected


def split(jobs):
    days=sorted({j['day'] for j in jobs})
    if len(days)<5:
        raise ValueError('Insufficient chronology for development study')
    first,second=days[int(.6*len(days))],days[int(.8*len(days))]
    pools={'train':[],'tune':[],'development_evaluation':[]}
    for job in jobs:
        role='train' if job['day']<first else 'tune' if job['day']<second else 'development_evaluation'
        pools[role].append(job)
    if any(not rs for rs in pools.values()):
        raise ValueError('Empty chronological pool')
    return pools,{'tune_first_day':first,'evaluation_first_day':second}


def encode(jobs):
    """No future task count, actual duration, finish date, user or equipment ID features."""
    out=np.zeros((len(jobs),128),np.float64)
    for n,j in enumerate(jobs):
        out[n,0]=1; out[n,1]=math.log1p(j['quantity'])
        for field in ('type','package'):
            token=(field+'='+j[field]).encode()
            h=hashlib.sha256(token).digest()
            out[n,2+int.from_bytes(h[:4],'little')%126]+=1 if h[4]&1 else -1
    return out


def fit(jobs,alpha):
    x=encode(jobs); y=np.log1p([j['recorded_working_hours'] for j in jobs])
    penalty=np.eye(128)*alpha; penalty[0,0]=0
    coef=np.linalg.solve(x.T@x+penalty,x.T@y)
    return coef


def predict(coef,jobs):
    return np.expm1(np.clip(encode(jobs)@coef,0,20))


def scores(truth,pred):
    y=np.array(truth); p=np.array(pred); delta=np.abs(y-p)
    return {'jobs':len(y),'mae_hours':float(delta.mean()),'median_absolute_error_hours':float(np.median(delta)),
            'rmsle':float(np.sqrt(np.mean((np.log1p(y)-np.log1p(p))**2)))}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--csv',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args();root=Path(args.out);root.mkdir(parents=True,exist_ok=False)
    jobs,rejected=load_jobs(args.csv);pools,boundaries=split(jobs)
    protocol={'created_at':now(),'notice':NOTICE,'data_mode':'real','role':'chronological development prediction',
              'source_csv_sha256':digest(args.csv),'job_counts':{k:len(v) for k,v in pools.items()},
              'job_ids':{k:[j['id'] for j in v] for k,v in pools.items()},'boundaries':boundaries,
              'features':['requested basic type','requested package','requested stressed quantity'],
              'target':'sum of recorded TASK_WORKING_TIME per job, hours',
              'candidates':['training-median baseline','log-target ridge alpha 1','alpha 10','alpha 100'],
              'selection':'minimum tuning MAE before development-evaluation scoring',
              'source_hashes':source_freeze(Path(__file__).parent),
              'commercial_validated':False,'restrictions':['No cross-job ID overlap or boundary day overlap',
                'CSV necessarily contains all targets, but evaluation targets do not enter fitting or selection',
                'Earliest exported task metadata assumed to describe job request; as-of availability unverified',
                'Recorded working hours are not elapsed process duration or SEM site time',
                'Completed/logged job selection may bias prospective workload estimates']}
    write(root/'protocol.json',protocol);write(root/'rejected-jobs.json',rejected)
    state={'status':'running','started_at':now(),'commercial_validated':False};write(root/'status.json',state)
    try:
        train,tune,evaluation=(pools[k] for k in ('train','tune','development_evaluation'))
        median=float(np.median([j['recorded_working_hours'] for j in train]))
        models={f'ridge-{a}':fit(train,a) for a in (1,10,100)}
        truth=[j['recorded_working_hours'] for j in tune]
        tuning={name:scores(truth,predict(coef,tune)) for name,coef in models.items()}
        chosen=min(tuning,key=lambda n:(tuning[n]['mae_hours'],n))
        for name,coef in models.items():
            np.save(root/(name+'.npy'),coef,allow_pickle=False)
        write(root/'evaluation-freeze.json',{'chosen':chosen,'tuning':tuning,'median':median,'created_at':now(),
                                            'model_hashes':{n:digest(root/(n+'.npy')) for n in models}})
        predictions={name:predict(coef,evaluation).tolist() for name,coef in models.items()}
        predictions['median_baseline']=[median]*len(evaluation)
        write(root/'predictions.json',{'job_ids':[j['id'] for j in evaluation],'predictions':predictions,
                                       'features':encode(evaluation).tolist(),'evaluation_targets_included':False})
        truth=[j['recorded_working_hours'] for j in evaluation]
        result={name:scores(truth,pred) for name,pred in predictions.items()}
        write(root/'results.json',{'chosen_before_evaluation':chosen,'models':result,'created_at':now()})
        write(root/'reference.json',{'jobs':[{'id':j['id'],'recorded_working_hours':j['recorded_working_hours']}
                                           for j in evaluation]})
        verify_source(Path(__file__).parent,protocol['source_hashes'])
        state.update(status='complete',completed_at=now(),accepted_jobs=len(jobs),rejected_jobs=len(rejected))
        lines=['# IPI recorded task-hour prediction development study','',NOTICE,'',
               f'Train/tune/development evaluation jobs: {len(train)}/{len(tune)}/{len(evaluation)}.',
               f'Tuning-selected candidate: {chosen}. Group and chronological-day separation retained.','',
               '| Candidate | MAE, hours | Median absolute error, hours | RMSLE |',
               '| --- | ---: | ---: | ---: |']
        for name,row in result.items():
            lines.append(f'| {name} | {row["mae_hours"]:.3f} | {row["median_absolute_error_hours"]:.3f} | {row["rmsle"]:.3f} |')
        lines+=['','The prediction target is recorded working hours summed over logged tasks, not actual elapsed duration or SEM dwell time.',
                'This is a component for a future separate workflow simulation. No scheduling improvement or commercial routing result is established.',
                'Input as-of availability must be validated prospectively; no job ID, future finish time, realized task count or actual working hours enters model features.',
                'Source: https://zenodo.org/records/10069426 (CC-BY-4.0). All exported job IDs are retained for provenance, not model inputs.','']
        (root/'RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
        print(json.dumps({'status':'complete','chosen':chosen,'counts':protocol['job_counts'],'metrics':result}),flush=True)
    except Exception as e:
        state.update(status='failed',error=f'{type(e).__name__}: {e}');raise
    finally: write(root/'status.json',state)


if __name__=='__main__':main()
