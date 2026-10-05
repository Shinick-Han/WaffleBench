"""Evidence sufficiency, separate from engineering test success."""
import math


def zero_event_upper_bound(independent_units, alpha=0.05):
    """One-sided exact binomial bound ONLY conditional on independent units."""
    if type(independent_units) is not int or independent_units < 0:
        raise ValueError('independent_units must be a nonnegative integer')
    if not 0 < alpha < 1 or not math.isfinite(alpha):
        raise ValueError('alpha must be in (0,1)')
    return 1 - alpha ** (1/independent_units) if independent_units else None


def development_gate(summary, positive_images, image_loss_events, required_max_miss=0.01):
    """Never turns a consumed proxy study into commercial authorization."""
    if type(image_loss_events) is not int or not 0 <= image_loss_events <= positive_images:
        raise ValueError('invalid loss-event count')
    bound = zero_event_upper_bound(positive_images) if image_loss_events == 0 else None
    return {'automatic_suppression_qualified': False, 'commercial_superiority_established': False,
            'status': 'development_only', 'matched_visible_defects': summary['policies']['shadow_localized_filter']['true_positive_rois'],
            'positive_images': positive_images, 'images_with_baseline_match_loss': image_loss_events,
            'conditional_zero_loss_upper95': bound, 'required_max_added_miss': required_max_miss,
            'conditional_bound_meets_target': bound is not None and bound <= required_max_miss,
            'bound_assumptions_established': False,
            'blockers': ['No paired actual instrument outputs.',
                         'No independently sampled production normals or full unflagged-area truth.',
                         'Consumed image-level development pool, without lot/acquisition groups.',
                         'No measured instrument/review end-to-end costs.',
                         'No qualified pixel-to-stage transform or live instrument adapter.']}
