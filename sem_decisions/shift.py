"""Independent image/feature shift diagnostic (pure NumPy; no Alibi Detect code or API).

``feature_shift`` compares measured reference features (e.g. the calibration split) with
current features using the two-sample Kolmogorov-Smirnov statistic per feature. The test
statistic is the maximum KS over features; its p-value comes from a seeded permutation of
pooled rows, which accounts for the multiple features jointly. This uses measured inputs
only and needs no labels.

``label_shift`` is a separate test that runs only when labels for the current data are
actually observed (paid reviews); otherwise it returns ``unavailable_labels_not_observed``
rather than inferring anything from hidden truth.

``calibrate_false_alert`` estimates the diagnostic's false-alert rate by repeatedly
splitting the reference set into two disjoint halves (no shift by construction) and
counting alerts at ``alpha``.

Limitations: detecting input drift does not prove reduced defect recall; failure to detect
drift does not establish exchangeability. Permutation p-values assume rows are
exchangeable under the null (correlated tiles from one image violate this).
"""

from __future__ import annotations

import math

import numpy as np


def _count(name: str, v, minimum: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum} (got {v!r})")
    return int(v)


def _alpha(alpha) -> float:
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) \
            or not 0 < alpha < 1:
        raise ValueError("alpha must be a finite number in (0, 1)")
    return float(alpha)


def _binary(name: str, y) -> np.ndarray:
    a = np.asarray(y)
    if a.ndim != 1 or a.size == 0 or (a.dtype != bool and not np.issubdtype(a.dtype, np.number)):
        raise ValueError(f"{name} must be a non-empty 1-D array of observed 0/1 labels")
    a = a.astype(float)
    if not np.all(np.isin(a, (0.0, 1.0))):
        raise ValueError(f"{name} must contain only observed 0/1 labels")
    return a


def ks_statistic(a: np.ndarray, b: np.ndarray) -> float:
    a = np.sort(np.asarray(a, float))
    b = np.sort(np.asarray(b, float))
    if a.size == 0 or b.size == 0:
        raise ValueError("KS needs non-empty samples")
    grid = np.concatenate([a, b])
    fa = np.searchsorted(a, grid, side="right") / a.size
    fb = np.searchsorted(b, grid, side="right") / b.size
    return float(np.max(np.abs(fa - fb)))


def _clean(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] < 2:
        raise ValueError("features must be (n>=2, d)")
    if not np.all(np.isfinite(x)):
        raise ValueError("features must be finite; drop or flag invalid acquisitions first")
    return x


def _max_ks(ref: np.ndarray, cur: np.ndarray) -> tuple[float, list[float]]:
    per = [ks_statistic(ref[:, j], cur[:, j]) for j in range(ref.shape[1])]
    return max(per), per


def feature_shift(reference: np.ndarray, current: np.ndarray, *, alpha: float = 0.05,
                  n_permutations: int = 199, seed: int = 0) -> dict:
    ref, cur = _clean(reference), _clean(current)
    if ref.shape[1] != cur.shape[1]:
        raise ValueError("reference/current feature dimensions differ")
    alpha = _alpha(alpha)
    n_permutations = _count("n_permutations", n_permutations)
    stat, per = _max_ks(ref, cur)
    pooled = np.vstack([ref, cur])
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(int(n_permutations)):
        perm = rng.permutation(pooled.shape[0])
        s, _ = _max_ks(pooled[perm[: ref.shape[0]]], pooled[perm[ref.shape[0]:]])
        exceed += s >= stat - 1e-12
    p_value = (exceed + 1) / (int(n_permutations) + 1)
    return {"test": "max_feature_ks_permutation", "statistic": stat, "per_feature_ks": per,
            "p_value": p_value, "alpha": alpha, "shift_detected": bool(p_value <= alpha),
            "no_alert_establishes_exchangeability": False,
            "n_reference": int(ref.shape[0]), "n_current": int(cur.shape[0]), "uses_labels": False,
            "caveat": "input drift is not proof of recall loss; no alert is not proof of exchangeability"}


def calibrate_false_alert(reference: np.ndarray, *, alpha: float = 0.05, n_splits: int = 20,
                          n_permutations: int = 99, seed: int = 0) -> dict:
    ref = _clean(reference)
    alpha = _alpha(alpha)
    n_splits = _count("n_splits", n_splits)
    n_permutations = _count("n_permutations", n_permutations)
    if ref.shape[0] < 4:
        raise ValueError("need at least 4 reference rows to split")
    rng = np.random.default_rng(seed)
    alerts = 0
    for s in range(int(n_splits)):
        perm = rng.permutation(ref.shape[0])
        half = ref.shape[0] // 2
        r = feature_shift(ref[perm[:half]], ref[perm[half:]], alpha=alpha, n_permutations=n_permutations,
                          seed=seed + 1 + s)
        alerts += r["shift_detected"]
    return {"alpha": alpha, "n_splits": int(n_splits), "false_alerts": int(alerts),
            "false_alert_rate": alerts / int(n_splits),
            "note": "null splits of the reference set; rate is an estimate with sampling error"}


def label_shift(reference_labels: np.ndarray, current_observed_labels: np.ndarray | None, *,
                alpha: float = 0.05, n_permutations: int = 199, seed: int = 0) -> dict:
    if current_observed_labels is None or len(current_observed_labels) == 0:
        return {"test": "label_rate_permutation", "status": "unavailable_labels_not_observed",
                "shift_detected": None, "uses_labels": True}
    alpha = _alpha(alpha)
    n_permutations = _count("n_permutations", n_permutations)
    r = _binary("reference_labels", reference_labels)
    c = _binary("current_observed_labels", current_observed_labels)
    stat = abs(r.mean() - c.mean())
    pooled = np.concatenate([r, c])
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(int(n_permutations)):
        perm = rng.permutation(pooled.size)
        exceed += abs(pooled[perm[: r.size]].mean() - pooled[perm[r.size:]].mean()) >= stat - 1e-12
    p_value = (exceed + 1) / (int(n_permutations) + 1)
    return {"test": "label_rate_permutation", "status": "observed_labels_only", "statistic": float(stat),
            "p_value": p_value, "shift_detected": bool(p_value <= alpha), "uses_labels": True,
            "caveat": "observed labels come from selected reviews and are not a random sample"}
