"""Independent DP ROI counts and raw ledger checks; no engine matcher imported."""
import argparse
import json
from pathlib import Path

import numpy as np

from sem_efficiency.core import digest, now, source_freeze, write
from sem_images import manifest as mf
from sem_images.metrics import label_components


def maximum_matching(candidates, truth):
    # Independent state-set dynamic program, rather than the engine's augmenting paths.
    if len(truth) > 18:
        raise ValueError('independent DP verifier refuses a large truth set')
    possible = {0}
    for row in candidates:
        a = row['bbox_px']
        edges = []
        for j, b in enumerate(truth):
            inter = max(0,min(a[2],b[2])-max(a[0],b[0])) * max(0,min(a[3],b[3])-max(a[1],b[1]))
            area_a=(a[2]-a[0])*(a[3]-a[1]);area_b=(b[2]-b[0])*(b[3]-b[1])
            if inter/(area_a+area_b-inter) >= .25:
                edges.append(j)
        next_states = set(possible)
        for state in possible:
            for j in edges:
                if not state & (1<<j):
                    next_states.add(state | (1<<j))
        possible=next_states
    return max(s.bit_count() for s in possible)


def main(study, manifest_path, source, output):
    study, source = Path(study), Path(source)
    protocol = json.loads((study/'protocol.json').read_text())
    if digest(manifest_path) != protocol['manifest_sha256']:
        raise ValueError('manifest changed')
    if source_freeze(source/'inspection_engine') != protocol['source_hashes']:
        raise ValueError('frozen study package differs from current source')
    for relative, sha in protocol['dependency_hashes'].items():
        if digest(source/relative) != sha:
            raise ValueError('frozen dependency changed')
    manifest=mf.load(manifest_path)
    by_id={r['id']:r for r in manifest.data['items']}
    access=[]
    reader=manifest.reader('calibration',masks_allowed=True,access_log=access)
    summary=json.loads((study/'summary.json').read_text())
    totals={str(t):{k:{'true_positive_rois':0,'unmatched_rois':0,'missed_visible_components':0,'candidates':0}
            for k in ('incumbent_proxy','augmented_retained','shadow_nuisance_filter','shadow_localized_filter')}
            for t in protocol['proxy_baseline_thresholds']}
    positive=set(); checked=0; mask_cache={}; charge_plan_count=0
    for row in json.loads((study/'prospective-results-freeze.json').read_text()):
        if digest(row['result_path']) != row['result_sha256'] or digest(row['cached_probability_path']) != row['cached_probability_sha256']:
            raise ValueError('frozen evidence changed')
        result=json.loads(Path(row['result_path']).read_text())
        iid=row['id']; item=by_id[iid]
        if item['split']!='calibration' or iid not in protocol['pools']['development_evaluation']:
            raise ValueError('verifier refuses unregistered or original-test data')
        if iid not in mask_cache:
            mask_cache[iid]=reader.load_mask(iid)
        labels,count=label_components(mask_cache[iid])
        truth=[]
        for n in range(1,count+1):
            ys,xs=np.nonzero(labels==n)
            truth.append([int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1])
        if count:
            positive.add(iid)
        shadow=[r for r in result['existing'] if r['decision']!='nuisance_review']+result['additional']
        localized=[dict(r,bbox_px=r['localization_suggestion']['bbox_px']) if r.get('localization_suggestion') else r for r in shadow]
        collections={'incumbent_proxy':result['frame']['candidates'],
                     'augmented_retained':result['existing']+result['additional'],
                     'shadow_nuisance_filter':shadow,'shadow_localized_filter':localized}
        archived=json.loads(Path(row['result_path']).with_name(iid+'-evaluation.json').read_text())
        if truth!=archived['truth_boxes_px']:
            raise ValueError('recorded reference box differs from raw supplied mask')
        for name,cs in collections.items():
            tp=maximum_matching(cs,truth)
            counts={'true_positive_rois':tp,'unmatched_rois':len(cs)-tp,
                    'missed_visible_components':len(truth)-tp,'candidates':len(cs)}
            for k,v in counts.items():
                if v!=archived['policies'][name][k]:
                    raise ValueError('independent DP metric differs from recorded result')
                totals[str(row['proxy_threshold'])][name][k]+=v
        plan=json.loads(Path(row['result_path']).with_name(iid+'-plan.json').read_text())
        reserve=sum(r['reserved_s'] for r in plan['admitted'])
        if abs(reserve-plan['reserved_s'])>1e-8 or abs(reserve+plan['remaining_s']-plan['budget_s'])>1e-8:
            raise ValueError('reservation ledger does not balance')
        if plan['frame_id']!=iid or plan['image_sha256']!=item['image_sha256']:
            raise ValueError('plan is bound to the wrong acquired frame')
        if any(r['review_label'] is not None for r in plan['admitted']+plan['deferred']):
            raise ValueError('unreviewed action was given an outcome')
        if plan['unknown_count']!=len(plan['admitted'])+len(plan['deferred'])+len(plan['reacquisition_requests']):
            raise ValueError('unknown count does not match unreviewed candidates')
        charge_plan_count+=1;checked+=1
    for t, policies in totals.items():
        for name, counts in policies.items():
            for k,v in counts.items():
                if v!=summary['by_proxy_threshold'][t]['policies'][name][k]:
                    raise ValueError('aggregate count differs from independently recomputed counts')
    qualification=json.loads((study/'qualification.json').read_text())
    if qualification['automatic_suppression_qualified'] or qualification['commercial_superiority_established']:
        raise ValueError('proxy study improperly authorized commercial/automatic claims')
    if qualification['positive_images']!=len(positive):
        raise ValueError('qualification denominator differs from raw masks')
    upper=1-pow(.05,1/len(positive)) if positive else None
    if upper!=qualification['conditional_zero_loss_upper95']:
        raise ValueError('zero-event conditional bound mismatch')
    evidence={'status':'pass','created_at':now(),'prospective_records':checked,'raw_development_masks':len(mask_cache),
              'policy_metric_rows':checked*4,'balanced_plans':charge_plan_count,
              'source_and_dependency_hashes_verified':True,'original_test_access':False,
              'qualification_gate_verified':True,
              'limits':['Independent DP matching/ledger/aggregate arithmetic; raw component labeling reuses sem_images.metrics.',
                        'Read logs are operational evidence, not OS-enforced test blindness.']}
    write(output,evidence)
    write(Path(output).with_name('verification-access-log.json'),access)
    return evidence


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--study',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--source',required=True);p.add_argument('--out',required=True)
    a=p.parse_args()
    print(json.dumps(main(a.study,a.manifest,a.source,a.out)))
