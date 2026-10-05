"""Image evidence -> incumbent triage + new candidates -> bounded review plan.

Scores are uncalibrated segmentation scores, never physical DOI probabilities.
Existing detections are retained. Nuisance suggestions require human review.
No mask, truth, class label or unacquired image is accepted by this runtime API.
"""
from dataclasses import asdict, dataclass
import copy
import math

import numpy as np

from sem_images.metrics import label_components
from . import NOTICE


@dataclass(frozen=True)
class Config:
    grow_threshold: float = 0.35
    seed_threshold: float = 0.8
    support_threshold: float = 0.8
    minimum_area_px: int = 4
    review_bound_s: float = 10.0

    def validate(self):
        for name in ('grow_threshold', 'seed_threshold', 'support_threshold'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f'{name} must be finite in (0,1]')
        if self.grow_threshold > self.seed_threshold:
            raise ValueError('grow threshold must not exceed seed threshold')
        if type(self.minimum_area_px) is not int or self.minimum_area_px < 1:
            raise ValueError('minimum_area_px must be a positive integer')
        positive(self.review_bound_s, 'review_bound_s')
        return self


def positive(value, name, zero=False):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite number')
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f'{name} outside allowed range')
    return float(value)


def validate_frame(frame):
    allowed = {'schema_version', 'frame_id', 'width', 'height', 'image_sha256',
               'model_sha256', 'data_mode', 'baseline_source', 'quality', 'candidates'}
    if not isinstance(frame, dict) or set(frame) != allowed:
        raise ValueError('frame must use the exact prospective schema; no labels/truth/future fields')
    if type(frame['schema_version']) is not int or frame['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    for k in ('frame_id', 'baseline_source'):
        if not isinstance(frame[k], str) or not frame[k].strip():
            raise ValueError(f'{k} must be nonempty')
    for k in ('image_sha256', 'model_sha256'):
        if not isinstance(frame[k], str) or len(frame[k]) != 64 or any(c not in '0123456789abcdef' for c in frame[k]):
            raise ValueError(f'{k} must be a lowercase SHA-256')
    if frame['data_mode'] not in ('real_image_development', 'instrument_shadow', 'synthetic_fixture'):
        raise ValueError('unsupported data_mode')
    if frame['quality'] not in ('valid', 'unknown', 'invalid'):
        raise ValueError('quality must be valid, unknown or invalid')
    for k in ('width', 'height'):
        if type(frame[k]) is not int or frame[k] < 1:
            raise ValueError('image dimensions must be positive integers')
    if not isinstance(frame['candidates'], list):
        raise ValueError('candidates must be an explicit list, including when empty')
    ids = set()
    for c in frame['candidates']:
        if not isinstance(c, dict) or set(c) != {'candidate_id', 'bbox_px', 'review_bound_s'}:
            raise ValueError('candidate must contain only id, bbox_px and review bound')
        cid = c['candidate_id']
        if not isinstance(cid, str) or not cid or cid in ids or cid.startswith('discovery:'):
            raise ValueError('invalid, duplicate or reserved candidate id')
        ids.add(cid)
        box = c['bbox_px']
        if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError('bbox_px requires four integer pixel coordinates')
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= frame['width'] and 0 <= y0 < y1 <= frame['height']):
            raise ValueError('bbox is outside the measured image')
        positive(c['review_bound_s'], 'review_bound_s')
    return copy.deepcopy(frame)


def components(probability, coverage, config):
    """Hysteresis: grow connected regions, require a strong seed in each region.

    Coverage holes never contribute; small regions are counted, not claimed absent.
    Bounding boxes use exclusive right/bottom edges, in the original image pixels.
    """
    labels, count = label_components((probability >= config.grow_threshold) & coverage)
    rows, discarded = [], {'below_minimum_area': 0, 'without_strong_seed': 0}
    for n in range(1, count + 1):
        ys, xs = np.nonzero(labels == n)
        scores = probability[ys, xs]
        if len(xs) < config.minimum_area_px:
            discarded['below_minimum_area'] += 1
            continue
        if scores.max() < config.seed_threshold:
            discarded['without_strong_seed'] += 1
            continue
        rows.append({'component_id': n, 'bbox_px': [int(xs.min()), int(ys.min()),
                     int(xs.max()) + 1, int(ys.max()) + 1], 'area_px': len(xs),
                     'peak_score': float(scores.max()), 'mean_score': float(scores.mean())})
    return labels, rows, discarded


def analyze(frame, probability, coverage=None, config=None):
    frame = validate_frame(frame)
    config = (config or Config()).validate()
    p = np.asarray(probability)
    shape = (frame['height'], frame['width'])
    if p.shape != shape or p.dtype.kind not in 'fiu' or not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        raise ValueError('probability must be a finite [0,1] numeric native-size map')
    valid = np.ones(shape, dtype=bool) if coverage is None else np.asarray(coverage)
    if valid.shape != shape or valid.dtype != np.dtype(bool):
        raise ValueError('coverage must be an explicit native-size bool array')
    usable = valid if frame['quality'] == 'valid' else np.zeros(shape, dtype=bool)
    labels, found, discarded = components(p, usable, config)
    by_component = {r['component_id']: r for r in found}
    incumbent_mask = np.zeros(shape, dtype=bool)
    existing = []
    for c in frame['candidates']:
        x0, y0, x1, y1 = c['bbox_px']
        incumbent_mask[y0:y1, x0:x1] = True
        roi_valid = usable[y0:y1, x0:x1]
        covered = bool(roi_valid.all())
        observed = p[y0:y1, x0:x1][roi_valid]
        score = float(observed.max()) if observed.size else None
        if not covered:
            decision, reason = 'request_reacquisition', 'invalid/unknown quality or incomplete acquired coverage'
        elif score >= config.support_threshold:
            decision, reason = 'supported_existing', 'AI score supports the incumbent candidate; review still required'
        else:
            decision, reason = 'nuisance_review', 'AI does not strongly support this candidate; do not discard without independent review'
        linked = []
        for component_id in np.unique(labels[y0:y1, x0:x1]):
            if int(component_id) in by_component:
                n = int(component_id)
                linked.append((int(np.count_nonzero(labels[y0:y1, x0:x1] == n)), n))
        refinement = None
        if covered and linked:
            _, chosen_id = max(linked, key=lambda v: (v[0], -v[1]))
            region = by_component[chosen_id]
            refinement = {'bbox_px': list(region['bbox_px']), 'component_id': chosen_id,
                          'score': region['peak_score'],
                          'reason': 'strong-seeded native image region intersecting this incumbent ROI',
                          'physical_stage_action': False}
        existing.append({**c, 'origin': 'incumbent', 'decision': decision, 'reason': reason,
                         'score': score, 'coverage_complete': covered,
                         'retained': True, 'review_label': None, 'localization_suggestion': refinement})
    new, associated = [], []
    for r in found:
        region = labels == r['component_id']
        overlap = int(np.count_nonzero(region & incumbent_mask))
        entry = {**r, 'overlap_existing_px': overlap}
        if overlap:
            associated.append(entry)
        else:
            new.append({**entry, 'candidate_id': 'discovery:' + str(r['component_id']),
                        'origin': 'additional_scan', 'decision': 'request_confirmation',
                        'reason': 'AI region outside all incumbent candidate boxes',
                        'score': r['peak_score'], 'review_bound_s': config.review_bound_s,
                        'review_label': None})
    actions = []
    for r in existing + new:
        # Reacquisition is listed separately: a review bound is not its capture cost.
        if r['decision'] == 'request_reacquisition':
            continue
        score = r['score']
        actions.append({'candidate_id': r['candidate_id'], 'origin': r['origin'],
                        'action': 'independent_review', 'decision': r['decision'],
                        'bbox_px': r['bbox_px'], 'score': score,
                        'suggested_bbox_px': r.get('localization_suggestion', {}).get('bbox_px') if r.get('localization_suggestion') else None,
                        'reserved_s': float(r['review_bound_s']),
                        'priority': score / r['review_bound_s'], 'status': 'pending',
                        'review_label': None})
    actions.sort(key=lambda r: (-r['priority'], r['candidate_id']))
    return {'schema_version': 1, 'notice': NOTICE, 'frame': frame,
            'config': asdict(config), 'existing': existing, 'additional': new,
            'associated_regions': associated, 'review_actions': actions,
            'reacquisition_requests': [r for r in existing if r['decision'] == 'request_reacquisition'],
            'frame_reacquisition_required': not bool(usable.all()),
            'coverage': {'acquired_pixels': int(valid.sum()), 'usable_pixels': int(usable.sum()),
                         'total_pixels': p.size, 'full_frame_usable': bool(usable.all())},
            'excluded_regions': discarded, 'automatic_suppression_enabled': False,
            'physical_labels_confirmed': False, 'hardware_connected': False,
            'commercial_validated': False,
            'limits': ['Scores are not calibrated site-level defect probabilities.',
                       'Only acquired usable pixels were scanned; outside-image material is unknown.',
                       'Any overlap with an incumbent box associates a region; adjacent misses may remain unresolved.',
                       'Localization suggestions do not change incumbent boxes or authorize physical stage commands.',
                       'Nuisance suggestions retain the incumbent candidate pending independent review.',
                       'Pixel coordinates cannot become physical stage coordinates without a calibrated transform.']}


def plan(result, budget_s):
    """Deterministic score-per-bound queue, not a claim of an optimal scheduler."""
    budget_s = positive(budget_s, 'budget_s', zero=True)
    remaining = budget_s
    admitted, deferred = [], []
    for row in result['review_actions']:
        if row['reserved_s'] <= remaining:
            admitted.append(copy.deepcopy(row))
            remaining -= row['reserved_s']
        else:
            deferred.append(copy.deepcopy(row))
    return {'schema_version': 1, 'frame_id': result['frame']['frame_id'],
            'image_sha256': result['frame']['image_sha256'],
            'model_sha256': result['frame']['model_sha256'], 'data_mode': result['frame']['data_mode'],
            'budget_s': budget_s, 'reserved_s': budget_s - remaining,
            'remaining_s': remaining, 'admitted': admitted, 'deferred': deferred,
            'reacquisition_requests': copy.deepcopy(result['reacquisition_requests']),
            'unknown_count': len(admitted) + len(deferred) + len(result['reacquisition_requests']),
            'cost_basis': 'declared independent-review bounds; no stage movement or capture included',
            'hardware_connected': False}


def apply_reviews(plan_result, events):
    """Actual independent-review outcomes, explicit charge, no unpaid outcome access.

    Returns all candidates including unresolved and deferred. No default negative.
    Review false -> rejected after observed review; not an automatic model deletion.
    """
    out = copy.deepcopy(plan_result)
    if not isinstance(events, list):
        raise ValueError('review events must be a list')
    if not isinstance(out, dict) or type(out.get('schema_version')) is not int or out['schema_version'] != 1:
        raise ValueError('invalid review plan')
    budget = positive(out['budget_s'], 'budget_s', zero=True)
    if not isinstance(out.get('admitted'), list) or not isinstance(out.get('deferred'), list):
        raise ValueError('invalid review action lists')
    ids = set()
    for r in out['admitted'] + out['deferred']:
        cid = r.get('candidate_id')
        if not isinstance(cid, str) or cid in ids or r.get('status') != 'pending' or r.get('review_label') is not None:
            raise ValueError('review plan must contain unique, unresolved pending actions')
        ids.add(cid)
        positive(r['reserved_s'], 'reserved_s')
    if sum(r['reserved_s'] for r in out['admitted']) > budget + 1e-9:
        raise ValueError('review plan exceeds admitted budget')
    allowed = {r['candidate_id']: r for r in out['admitted']}
    seen = set()
    charged = 0.0
    for e in events:
        if not isinstance(e, dict) or set(e) != {'frame_id', 'candidate_id', 'status', 'label', 'charged_s', 'evidence_id', 'source'}:
            raise ValueError('invalid review event schema')
        if e['frame_id'] != out['frame_id']:
            raise ValueError('review belongs to a different acquired frame')
        cid = e['candidate_id']
        if cid not in allowed or cid in seen:
            raise ValueError('cannot consume a deferred/unadmitted or duplicate review')
        seen.add(cid)
        row = allowed[cid]
        if e['source'] not in ('human_review', 'independent_lab'):
            raise ValueError('review must declare a human or independent-lab source; another model score is not confirmation')
        if e['status'] not in ('ok', 'failed', 'missing'):
            raise ValueError('invalid review status')
        if e['status'] == 'ok':
            if type(e['label']) is not bool or not isinstance(e['evidence_id'], str) or not e['evidence_id']:
                raise ValueError('ok requires an observed bool label and evidence id')
        elif e['label'] is not None:
            raise ValueError('failed/missing reviews must remain unknown')
        seconds = positive(e['charged_s'], 'charged_s', zero=True)
        if seconds > row['reserved_s']:
            raise ValueError('review charge exceeded its admission bound')
        row.update(status=e['status'], review_label=e['label'],
                   charged_s=seconds, evidence_id=e['evidence_id'], review_source=e['source'])
        charged += seconds
    out['charged_s'] = charged
    out['remaining_after_charges_s'] = out['budget_s'] - charged
    out['review_positive'] = sum(r['review_label'] is True for r in out['admitted'])
    out['review_rejected'] = sum(r['review_label'] is False for r in out['admitted'])
    out['unknown_count'] = sum(r['review_label'] is None for r in out['admitted'] + out['deferred']) + len(out.get('reacquisition_requests',[]))
    out['physical_truth_independently_verified'] = False
    out['label_basis'] = 'caller-supplied human/independent-lab review; engine verifies accounting, not physical truth'
    return out
