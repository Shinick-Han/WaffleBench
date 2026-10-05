"""Offline only: independent mask components and one-to-one ROI matching."""
import numpy as np
from sem_images.metrics import label_components


def truth_boxes(mask):
    labels, count = label_components(mask)
    boxes = []
    for n in range(1, count + 1):
        ys, xs = np.nonzero(labels == n)
        boxes.append([int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1])
    return boxes


def box_iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = width * height
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / union


def match_candidates(candidates, boxes, threshold=0.25):
    """Maximum-cardinality bipartite match; each ROI/defect counts at most once.

    Localization criterion is bounding-box IoU, distinct from pixel Dice and the
    older segmentation union-of-overlapping-components metric.
    """
    edges = [[j for j, box in enumerate(boxes) if box_iou(c['bbox_px'], box) >= threshold]
             for c in candidates]
    owner = {}

    def augment(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or augment(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(candidates)):
        augment(i, set())
    return {str(j): candidates[i]['candidate_id'] for j, i in sorted(owner.items())}


def evaluate(frame, result, mask, threshold=0.25):
    if np.asarray(mask).shape != (frame['height'], frame['width']):
        raise ValueError('independent mask is not in the native frame coordinates')
    boxes = truth_boxes(mask)
    incumbent = frame['candidates']
    augmented = result['existing'] + result['additional']
    # Development ablation only. Runtime never automatically suppresses these.
    shadow = [r for r in result['existing'] if r['decision'] != 'nuisance_review'] + result['additional']
    localized = [{**r, 'bbox_px': r['localization_suggestion']['bbox_px']}
                 if r.get('localization_suggestion') else r for r in shadow]
    collections = {'incumbent_proxy': incumbent, 'augmented_retained': augmented,
                   'shadow_nuisance_filter': shadow, 'shadow_localized_filter': localized}
    matches, rows = {}, {}
    for name, candidates in collections.items():
        matched = match_candidates(candidates, boxes, threshold)
        matches[name] = matched
        tp, n = len(matched), len(candidates)
        rows[name] = {'true_positive_rois': tp, 'unmatched_rois': n-tp,
                      'missed_visible_components': len(boxes)-tp, 'candidates': n,
                      'matched_gt_indices': list(matched),
                      'candidate_precision': tp/n if n else None,
                      'visible_component_recall': tp/len(boxes) if boxes else None}
    b = set(matches['incumbent_proxy'])
    a = set(matches['augmented_retained'])
    s = set(matches['shadow_nuisance_filter'])
    # Count only baseline-unmatched truth recovered by a new candidate itself.
    eligible_boxes = [box for j, box in enumerate(boxes) if str(j) not in b]
    recovered = match_candidates(result['additional'], eligible_boxes, threshold)
    return {'ground_truth_visible_components': len(boxes), 'truth_boxes_px': boxes,
            'box_iou_threshold': threshold, 'matching': 'one-to-one maximum-cardinality bbox IoU',
            'policies': rows, 'new_visible_components_found': len(recovered),
            'baseline_matches_lost_after_augmentation': len(b-a),
            'baseline_matches_lost_in_shadow_filter': len(b-s),
            'baseline_matches_lost_in_localized_filter': len(b-set(matches['shadow_localized_filter'])),
            'localization_misses_recovered': len(set(matches['shadow_localized_filter']) - b),
            'nuisance_suggestions': sum(r['decision'] == 'nuisance_review' for r in result['existing']),
            'real_instrument_baseline_available': False,
            'production_false_alarm_rate': None}


def aggregate(rows):
    out = {'images': len(rows), 'visible_components': sum(r['ground_truth_visible_components'] for r in rows),
           'new_visible_components_found': sum(r['new_visible_components_found'] for r in rows),
           'baseline_matches_lost_after_augmentation': sum(r['baseline_matches_lost_after_augmentation'] for r in rows),
           'baseline_matches_lost_in_shadow_filter': sum(r['baseline_matches_lost_in_shadow_filter'] for r in rows),
           'baseline_matches_lost_in_localized_filter': sum(r['baseline_matches_lost_in_localized_filter'] for r in rows),
           'localization_misses_recovered': sum(r['localization_misses_recovered'] for r in rows),
           'nuisance_suggestions': sum(r['nuisance_suggestions'] for r in rows), 'policies': {}}
    for name in ('incumbent_proxy', 'augmented_retained', 'shadow_nuisance_filter', 'shadow_localized_filter'):
        cols = ('true_positive_rois', 'unmatched_rois', 'missed_visible_components', 'candidates')
        values = {k: sum(r['policies'][name][k] for r in rows) for k in cols}
        values['candidate_precision'] = values['true_positive_rois']/values['candidates'] if values['candidates'] else None
        values['visible_component_recall'] = values['true_positive_rois']/out['visible_components'] if out['visible_components'] else None
        out['policies'][name] = values
    return out
