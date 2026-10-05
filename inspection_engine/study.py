"""Frozen development comparison on already-consumed calibration images.

No original test image is opened. Proxy incumbents are this same model's thresholded
components, not actual equipment outputs. This is an integration/ablation study.
"""
import argparse
from dataclasses import asdict
import json
import time
from pathlib import Path

import numpy as np

from sem_efficiency.core import digest, now, source_freeze, verify_source, write
from sem_images import manifest as mf
from . import NOTICE
from .core import Config, analyze, components, plan
from .evaluation import aggregate, evaluate
from .qualification import development_gate


def proxy_candidates(p, threshold, bound_s):
    cfg = Config(grow_threshold=threshold, seed_threshold=threshold, minimum_area_px=1)
    _, found, _ = components(p, np.ones(p.shape, dtype=bool), cfg)
    return [{'candidate_id': 'proxy:' + str(r['component_id']), 'bbox_px': r['bbox_px'],
             'review_bound_s': bound_s} for r in found]


def run(old_study, manifest_path, out):
    old, out = Path(old_study), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    mf_path = Path(manifest_path)
    protocol = json.loads((old / 'protocol.json').read_text())
    if digest(mf_path) != protocol['manifest_sha256']:
        raise ValueError('source manifest hash differs from earlier registered study')
    manifest = mf.load(mf_path)
    by_id = {r['id']: r for r in manifest.data['items']}
    pools = {k: protocol['pools'][k] for k in ('tune', 'development_evaluation')}
    if set(pools['tune']) & set(pools['development_evaluation']):
        raise ValueError('tuning/evaluation roles overlap')
    groups = {}
    for role, ids in pools.items():
        for iid in ids:
            item = by_id[iid]
            if item['split'] != 'calibration':
                raise ValueError('only the declared consumed calibration pools are accepted')
            for key in ('duplicate_group', 'image_sha256', 'source_group'):
                if item.get(key) is None:
                    continue
                group = (key, item[key])
                if group in groups and groups[group] != role:
                    raise ValueError('group overlaps development roles')
                groups[group] = role
    root = Path(__file__).parent
    frozen_source = source_freeze(root)
    dependency_paths = [root.parent/'sem_images'/'manifest.py', root.parent/'sem_images'/'metrics.py',
                        root.parent/'sem_efficiency'/'core.py']
    dependencies = {str(p.relative_to(root.parent)): digest(p) for p in dependency_paths}
    cfg = Config()
    proto = {'schema_version': 1, 'created_at': now(), 'notice': NOTICE,
             'evidence_role': 'consumed-pool development integration, NOT confirmatory test',
             'model_sha256': protocol['base_model_sha256'], 'manifest_sha256': digest(mf_path),
             'source_hashes': frozen_source, 'pools': pools, 'config': asdict(cfg),
             'dependency_hashes': dependencies,
             'proxy_baseline_thresholds': [0.5, 0.95],
             'primary_proxy_threshold': 0.5, 'strict_proxy_is_stress_only': True,
             'nuisance_threshold_grid': [0.6, 0.7, 0.8, 0.9, 0.95],
             'selection': 'most proxy unmatched-ROI removals with no baseline matched-GT losses on tune; lowest threshold tie',
             'quality_assumption': 'valid for image-coordinate integration only; not instrument quality qualification',
             'roi_matching': 'one-to-one maximum-cardinality bbox IoU >= 0.25',
             'original_test_access': False, 'commercial_validated': False}
    write(out / 'protocol.json', proto)
    access = []
    reader = manifest.reader('calibration', masks_allowed=True, access_log=access)
    loaded = {}
    for role in pools:
        receipt_path = old / 'predictions' / 'frozen_baseline' / role / 'receipt.json'
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('masks_read_for_prediction') is not False:
            raise ValueError('cached inference did not declare mask-free prediction')
        if set(r['id'] for r in receipt['items']) != set(pools[role]):
            raise ValueError('prediction receipt ids differ from declared development pool')
        loaded[role] = []
        for row in receipt['items']:
            path = Path(row['file'])
            if digest(path) != row['sha256']:
                raise ValueError('cached prediction hash mismatch')
            p = np.load(path, allow_pickle=False).astype(np.float32)
            item = by_id[row['id']]
            if p.shape != (item['height'], item['width']):
                raise ValueError('cached map differs from native frame shape')
            frame = {'schema_version': 1, 'frame_id': row['id'], 'width': item['width'],
                     'height': item['height'], 'image_sha256': item['image_sha256'],
                     'model_sha256': protocol['base_model_sha256'], 'data_mode': 'real_image_development',
                     'quality': 'valid', 'baseline_source': 'same-model threshold-component software proxy',
                     'candidates': []}
            loaded[role].append((frame, p, row))
    # Read tune masks only for declared selection; evaluation labels are still closed.
    tune_masks = {f['frame_id']: reader.load_mask(f['frame_id']) for f, _, _ in loaded['tune']}
    selection = []
    for t in proto['nuisance_threshold_grid']:
        rows = []
        for frame, p, _ in loaded['tune']:
            frame = dict(frame, candidates=proxy_candidates(p, 0.5, cfg.review_bound_s))
            result = analyze(frame, p, config=Config(support_threshold=t))
            rows.append(evaluate(frame, result, tune_masks[frame['frame_id']]))
        summary = aggregate(rows)
        baseline = summary['policies']['incumbent_proxy']
        shadow = summary['policies']['shadow_nuisance_filter']
        selection.append({'threshold': t, 'summary': summary,
                          'unmatched_roi_reduction': baseline['unmatched_rois'] - shadow['unmatched_rois']})
    eligible = [r for r in selection if r['summary']['baseline_matches_lost_in_shadow_filter'] == 0]
    chosen = sorted(eligible, key=lambda r: (-r['unmatched_roi_reduction'], r['threshold']))[0]['threshold'] if eligible else 0.6
    cfg = Config(support_threshold=chosen)
    write(out / 'selection.json', {'created_at': now(), 'chosen_threshold': chosen, 'candidates': selection,
                                 'automatic_suppression_qualified': False,
                                 'reason': 'proxy and small consumed image-level tune sample; no instrument safety evidence'})
    write(out / 'evaluation-freeze.json', {'created_at': now(), 'config': asdict(cfg),
                                         'source_hashes': frozen_source, 'original_test_access': False})
    write(out / 'config.json', asdict(cfg))
    records = []
    # Persist every prospective analysis and plan BEFORE any evaluation mask read.
    start = time.perf_counter()
    for baseline_threshold in proto['proxy_baseline_thresholds']:
        for frame, p, cached in loaded['development_evaluation']:
            frame = dict(frame, candidates=proxy_candidates(p, baseline_threshold, cfg.review_bound_s))
            t0 = time.perf_counter()
            result = analyze(frame, p, config=cfg)
            prefix = out / 'frames' / str(baseline_threshold) / frame['frame_id']
            write(prefix.with_suffix('.json'), result)
            scheduled = plan(result, 120.0)
            write(prefix.with_name(prefix.name + '-plan.json'), scheduled)
            records.append({'id': frame['frame_id'], 'proxy_threshold': baseline_threshold,
                            'frame': frame, 'result': result, 'result_path': str(prefix.with_suffix('.json')),
                            'result_sha256': digest(prefix.with_suffix('.json')),
                            'cached_probability_path': cached['file'],
                            'cached_probability_sha256': cached['sha256'],
                            'analysis_and_plan_seconds': time.perf_counter()-t0})
    write(out / 'prospective-results-freeze.json', [{k: r[k] for k in
          ('id', 'proxy_threshold', 'result_path', 'result_sha256', 'cached_probability_path', 'cached_probability_sha256')}
          for r in records])
    masks = {}
    for r in records:
        if digest(r['result_path']) != r['result_sha256']:
            raise ValueError('prospective result changed before independent evaluation')
        if r['id'] not in masks:
            masks[r['id']] = reader.load_mask(r['id'])
        r['evaluation'] = evaluate(r['frame'], r['result'], masks[r['id']])
        # Per-record physical class is deliberately absent from runtime inputs.
        write(Path(r['result_path']).with_name(r['id'] + '-evaluation.json'), r['evaluation'])
    summaries = {str(t): aggregate([r['evaluation'] for r in records if r['proxy_threshold'] == t])
                 for t in proto['proxy_baseline_thresholds']}
    primary_rows = [r['evaluation'] for r in records if r['proxy_threshold'] == 0.5]
    write(out/'qualification.json',development_gate(summaries['0.5'],
          sum(r['ground_truth_visible_components'] > 0 for r in primary_rows),
          sum(r['baseline_matches_lost_in_localized_filter'] > 0 for r in primary_rows)))
    write(out / 'summary.json', {'created_at': now(), 'notice': NOTICE, 'data_mode': 'real_image_development',
                               'config': asdict(cfg), 'by_proxy_threshold': summaries,
                               'seconds_excluding_cached_inference': time.perf_counter()-start,
                               'original_test_access': False, 'commercial_validated': False,
                               'production_false_alarm_rate': None,
                               'limits': ['Same-model software proxies, no actual equipment baseline.',
                                          'Consumed development images and no lot identifiers; no fresh generalization proof.',
                                          'Unmatched ROIs describe supplied masks, not production nuisance rate.',
                                          'Shadow filtering is an offline ablation and is disabled in runtime.',
                                          'Localization recovery corrects proxy ROI matching; it is not a newly observed physical defect.',
                                          'No physical scan coordinates, hardware acquisition or measured review cost.']})
    write(out / 'access-log.json', access)
    verify_source(root, frozen_source)
    if {str(p.relative_to(root.parent)): digest(p) for p in dependency_paths} != dependencies:
        raise ValueError('imported study dependency changed during evaluation')
    write(out / 'status.json', {'status': 'complete', 'created_at': now(), 'notice': NOTICE,
                               'prospective_records': len(records), 'evaluation_images': len(masks),
                               'source_freeze_verified': True, 'original_test_access': False})
    return summaries


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--old-study', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    print(json.dumps(run(args.old_study, args.manifest, args.out), indent=2))
