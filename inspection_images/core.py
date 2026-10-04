"""Numpy-only pieces of the PCB1 image pilot: splits, memory bank, scoring, metrics.

Inspired by PatchCore (arXiv:2106.08265); no PatchCore code is reused. Adaptations are
listed in IMAGE_PILOT_PROTOCOL.md. Nothing here reads test labels.
"""

import math

import numpy as np


def _finite(name, array):
    array = np.asarray(array, dtype=np.float64)
    if array.size == 0:
        raise ValueError(f"{name} is empty")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or infinite values")
    return array


def calibration_split(train_ids, fraction, seed):
    """Deterministically reserve floor(fraction*n) official normal-train images for calibration."""
    ids = sorted(train_ids)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate train image ids")
    if not 0 < fraction < 1:
        raise ValueError("calibration fraction must be in (0, 1)")
    n_cal = max(1, int(math.floor(len(ids) * fraction)))
    if n_cal >= len(ids):
        raise ValueError("too few train images for a calibration split")
    order = np.random.default_rng([seed, 1]).permutation(len(ids))
    calibration = sorted(ids[i] for i in order[:n_cal])
    memory = sorted(ids[i] for i in order[n_cal:])
    return memory, calibration


def _finite32(name, array):
    array = np.asarray(array, dtype=np.float32)
    if array.size == 0:
        raise ValueError(f"{name} is empty")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or infinite values")
    return array


def normalization_stats(descriptors, chunk=65536):
    """Per-dimension mean/std over (images, patches, dim) memory-train descriptors (two-pass, float64)."""
    x = _finite32("descriptors", descriptors)
    x = x.reshape(-1, x.shape[-1])
    total = np.zeros(x.shape[1])
    for lo in range(0, len(x), chunk):
        total += x[lo:lo + chunk].sum(axis=0, dtype=np.float64)
    mean = total / len(x)
    sq = np.zeros(x.shape[1])
    for lo in range(0, len(x), chunk):
        sq += np.sum((x[lo:lo + chunk] - mean) ** 2, axis=0)
    std = np.sqrt(sq / len(x))
    std = np.where(std < 1e-6, 1.0, std)
    return mean, std


def normalize(descriptors, mean, std):
    x = _finite32("descriptors", descriptors)
    return (x - mean.astype(np.float32)) / std.astype(np.float32)


def reservoir_indices(total, limit, seed):
    """Sorted bounded deterministic subset of flat descriptor indices."""
    if total <= 0:
        raise ValueError("no descriptors")
    if total <= limit:
        return np.arange(total)
    return np.sort(np.random.default_rng([seed, 2]).choice(total, size=limit, replace=False))


def projection_matrix(dim, projection_dim, seed):
    rng = np.random.default_rng([seed, 3])
    return rng.standard_normal((dim, projection_dim)) / math.sqrt(projection_dim)


def greedy_coreset(features, size, projection_dim, seed):
    """Farthest-point greedy coreset in a seeded random projection; returns selected indices."""
    x = _finite("coreset features", features)
    if x.ndim != 2:
        raise ValueError("coreset features must be 2-D")
    n = len(x)
    if size <= 0:
        raise ValueError("coreset size must be positive")
    if size >= n:
        return np.arange(n)
    z = x @ projection_matrix(x.shape[1], projection_dim, seed)
    start = int(np.argmax(np.sum((z - z.mean(axis=0)) ** 2, axis=1)))
    selected = [start]
    min_dist = np.sum((z - z[start]) ** 2, axis=1)
    for _ in range(size - 1):
        nxt = int(np.argmax(min_dist))
        selected.append(nxt)
        np.minimum(min_dist, np.sum((z - z[nxt]) ** 2, axis=1), out=min_dist)
    return np.asarray(selected)


def nearest_distance(queries, bank, chunk=4096):
    """Min L2 distance from each query row to bank rows, computed in float64 chunks."""
    q = _finite("queries", queries)
    b = _finite("bank", bank)
    if q.ndim != 2 or b.ndim != 2 or q.shape[1] != b.shape[1]:
        raise ValueError("queries and bank must be 2-D with equal width")
    if chunk <= 0:
        raise ValueError("chunk must be positive")
    bb = np.sum(b * b, axis=1)
    out = np.empty(len(q))
    for lo in range(0, len(q), chunk):
        part = q[lo:lo + chunk]
        d2 = np.sum(part * part, axis=1)[:, None] + bb[None, :] - 2.0 * part @ b.T
        out[lo:lo + chunk] = np.sqrt(np.maximum(d2.min(axis=1), 0.0))
    return out


def patch_scores(normalized, bank, chunk=4096):
    """Image score = max patch nearest-memory distance. normalized: (images, patches, dim)."""
    x = _finite("normalized descriptors", normalized)
    n, p, d = x.shape
    return nearest_distance(x.reshape(n * p, d), bank, chunk).reshape(n, p).max(axis=1)


def global_vectors(normalized):
    return _finite32("normalized descriptors", normalized).mean(axis=1, dtype=np.float64)


def global_scores(normalized, memory_global, chunk=4096):
    return nearest_distance(global_vectors(normalized), memory_global, chunk)


def threshold(calibration_scores, percentile):
    scores = _finite("calibration scores", calibration_scores)
    return float(np.percentile(scores, percentile, method="linear"))


def _labels(labels, n):
    y = np.asarray(labels)
    if y.shape != (n,) or not np.all(np.isin(y, (0, 1))):
        raise ValueError("labels must be a 0/1 vector matching scores")
    if y.min() == y.max():
        raise ValueError("both classes are required")
    return y.astype(int)


def auroc(scores, labels):
    """Rank AUROC (ties count half). A ranking statistic, never an accuracy."""
    s = _finite("scores", scores)
    y = _labels(labels, len(s))
    pos, neg = s[y == 1], s[y == 0]
    greater = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * ties) / (len(pos) * len(neg)))


def average_precision(scores, labels):
    """Step-wise AP: sum over distinct thresholds of (recall_k - recall_{k-1}) * precision_k."""
    s = _finite("scores", scores)
    y = _labels(labels, len(s))
    order = np.argsort(-s, kind="mergesort")
    s, y = s[order], y[order]
    cut = np.r_[np.nonzero(np.diff(s))[0], len(s) - 1]
    tp = np.cumsum(y)[cut]
    fp = (cut + 1) - tp
    precision = tp / (tp + fp)
    recall = tp / y.sum()
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def confusion(scores, labels, thr):
    s = _finite("scores", scores)
    y = _labels(labels, len(s))
    if not math.isfinite(thr):
        raise ValueError("threshold must be finite")
    pred = s > thr
    tp = int(np.sum(pred & (y == 1)))
    fn = int(np.sum(~pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    tn = int(np.sum(~pred & (y == 0)))
    return {"threshold": thr, "rule": "score > threshold => predicted defect",
            "true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn,
            "defect_recall": tp / (tp + fn), "normal_false_alarm_rate": fp / (fp + tn)}
