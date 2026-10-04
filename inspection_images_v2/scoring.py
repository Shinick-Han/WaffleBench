"""v2 score aggregation and the preregistered paired bootstrap. Distances reuse v1 core read-only."""

import numpy as np

from inspection_images import core


def patch_distances(normalized, bank, chunk=4096):
    """(images, patches) L2 distance of each patch to its nearest bank descriptor (v1 exact chunked search)."""
    x = np.asarray(normalized)
    if x.ndim != 3:
        raise ValueError("normalized descriptors must be (images, patches, dim)")
    n, p, d = x.shape
    return core.nearest_distance(x.reshape(n * p, d), bank, chunk).reshape(n, p)


def aggregate(distances, aggregation, top_k):
    """Image score from per-patch distances: 'max' or 'mean_topk' (mean of the top_k largest)."""
    dist = np.asarray(distances, dtype=np.float64)
    if dist.ndim != 2 or not np.all(np.isfinite(dist)):
        raise ValueError("distances must be a finite (images, patches) array")
    if aggregation == "max":
        if top_k != 1:
            raise ValueError("max aggregation requires top_k == 1")
        return dist.max(axis=1)
    if aggregation == "mean_topk":
        if not 1 <= top_k <= dist.shape[1]:
            raise ValueError("top_k out of range")
        return np.sort(dist, axis=1)[:, -top_k:].mean(axis=1)
    raise ValueError(f"unknown aggregation {aggregation!r}")


def paired_bootstrap(pred_a, pred_b, labels, resamples, seed_stream):
    """Stratified paired image bootstrap of (a - b) defect recall and normal FAR; percentile 95% CI.

    Images are resampled with replacement within each label stratum and the same draw is
    applied to both methods, so per-stratum counts stay fixed.
    """
    a, b, y = (np.asarray(v).astype(int) for v in (pred_a, pred_b, labels))
    if not (a.shape == b.shape == y.shape) or a.ndim != 1:
        raise ValueError("predictions and labels must be equal-length vectors")
    if not np.all(np.isin(a, (0, 1))) or not np.all(np.isin(b, (0, 1))) or not np.all(np.isin(y, (0, 1))):
        raise ValueError("predictions and labels must be 0/1")
    rng = np.random.default_rng(list(seed_stream))
    out = {}
    for name, stratum in (("defect_recall_gain", 1), ("normal_far_difference", 0)):
        idx = np.nonzero(y == stratum)[0]
        if len(idx) == 0:
            raise ValueError("both classes are required")
        diff = a[idx] - b[idx]
        draws = rng.integers(0, len(idx), size=(resamples, len(idx)))
        stats = diff[draws].mean(axis=1)
        lo, hi = np.percentile(stats, [2.5, 97.5])
        out[name] = {"estimate": float(diff.mean()), "ci95": [float(lo), float(hi)], "resamples": int(resamples)}
    return out
