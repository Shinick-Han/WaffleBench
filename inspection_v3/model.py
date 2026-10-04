"""Inspection v3 DOI models: v2 base models plus rank-preserving parametric calibration.

Synthetic development study only. Training, raw prediction, online updates, hashing and
the identity/isotonic calibrations are delegated unchanged to ``inspection_v2.model``.
This module adds two independent calibrators on the raw base logit
``s = logit(clip(p_raw, LOGIT_EPS, 1 - LOGIT_EPS))``:

* ``platt``: ``p = sigmoid(slope * s + intercept)`` with ``slope`` in
  ``[SLOPE_BOUNDS]`` (strictly positive, so the raw ranking is preserved).
* ``temperature``: ``p = sigmoid(s / temperature)`` (``slope = 1 / temperature``,
  ``intercept = 0``) with the same slope bounds.

Both are fitted by a deterministic box-constrained Newton method (numpy only) that
minimises the candidate log loss on lots whose ``[seed, scenario]`` pair is approved for
calibration and whose seed is reserved by no other split. The fitted model is a new plain
JSON dict; the base model, its seven-feature mean/scale and its raw probability are kept,
and the base hash is recorded. Calibrated models never update online.

Probability semantics: ``predict_raw`` is always the uncalibrated base DOI probability;
``predict`` is the calibrated probability when the model is calibrated. Ties are only
introduced where the raw probability is clipped at ``LOGIT_EPS`` or the calibrated value
saturates in float64; ``predict_calibrated_logit`` exposes the unsaturated score.
"""
from __future__ import annotations

import copy
import itertools

import numpy as np

from inspection_v2 import model as v2

CALIBRATION_METHODS = ("identity", "isotonic", "platt", "temperature")
PARAMETRIC_METHODS = ("platt", "temperature")
LOGIT_EPS = 1e-12
SLOPE_BOUNDS = (1e-3, 50.0)
INTERCEPT_BOUNDS = (-30.0, 30.0)
_MAX_ITER = 200
_TOL = 1e-10
PROBABILITY_SEMANTICS = {
    "raw_base": "uncalibrated base-model DOI probability on optical candidate sites",
    "calibrated_platt": "sigmoid(slope * logit(raw) + intercept) fitted on calibration candidates",
    "calibrated_temperature": "sigmoid(logit(raw) / temperature) fitted on calibration candidates",
}

# Unchanged v2 APIs.
hash_model = v2.hash_model
train_model = v2.train_model
predict_raw = v2.predict_raw
update_model = v2.update_model


# ---------------------------------------------------------------- numerics

def _sigmoid(t):
    t = np.asarray(t, dtype=float)
    out = np.empty_like(t)
    pos = t >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-t[pos]))
    e = np.exp(t[~pos])
    out[~pos] = e / (1.0 + e)
    return out


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), LOGIT_EPS, 1.0 - LOGIT_EPS)
    return np.log(p) - np.log1p(-p)


def _nll(t, y):
    """Mean logistic log loss, stable for large |t|."""
    return float(np.mean(np.logaddexp(0.0, t) - y * t))


def _fit_affine_logit(s, y, fit_intercept):
    """Deterministic box-constrained Newton fit of sigmoid(a*s + b) to labels y.

    Convex objective; free variables take a damped Newton step, bound-active variables
    (gradient pushing outwards) stay fixed, and an Armijo backtracking line search on the
    projected point guarantees monotone decrease.
    """
    s = np.asarray(s, dtype=float)
    y = np.asarray(y, dtype=float)
    lo = np.array([SLOPE_BOUNDS[0], INTERCEPT_BOUNDS[0]])
    hi = np.array([SLOPE_BOUNDS[1], INTERCEPT_BOUNDS[1]])
    k = 2 if fit_intercept else 1
    lo, hi = lo[:k], hi[:k]
    design = np.column_stack([s, np.ones_like(s)])[:, :k]
    theta = np.clip(np.array([1.0, 0.0])[:k], lo, hi)
    f = _nll(design @ theta, y)
    converged, it = False, 0
    for it in range(1, _MAX_ITER + 1):
        p = _sigmoid(design @ theta)
        g = design.T @ (p - y) / len(y)
        h = (design * (p * (1.0 - p))[:, None]).T @ design / len(y)
        free = ~(((theta <= lo) & (g > 0)) | ((theta >= hi) & (g < 0)))
        if not free.any() or np.max(np.abs(g[free])) < _TOL:
            converged = True
            break
        d = np.zeros(k)
        hf = h[np.ix_(free, free)] + 1e-12 * np.eye(int(free.sum()))
        d[free] = -np.linalg.solve(hf, g[free])
        if g @ d >= 0:  # numerically degenerate Hessian: fall back to steepest descent
            d = np.where(free, -g, 0.0)
        step, accepted = 1.0, False
        for _ in range(60):
            cand = np.clip(theta + step * d, lo, hi)
            fc = _nll(design @ cand, y)
            if fc <= f + 1e-4 * (g @ (cand - theta)):
                accepted = True
                break
            step *= 0.5
        if not accepted or np.max(np.abs(cand - theta)) < 1e-15:
            converged = True
            break
        theta, f = cand, fc
    slope = float(theta[0])
    intercept = float(theta[1]) if fit_intercept else 0.0
    at_bound = bool(np.any(theta <= lo) or np.any(theta >= hi))
    return slope, intercept, {"method": "projected_newton_armijo", "iterations": it,
                              "converged": converged, "at_bound": at_bound,
                              "train_log_loss": float(f),
                              "identity_log_loss": _nll(s, y)}


# ---------------------------------------------------------------- calibration

def _approved_and_reserved(config):
    """Calibration pairs and every other split's seeds (v1, v2_splits, v3_splits)."""
    v3_splits = config.get("v3_splits") or {}
    if "calibration" in v3_splits:
        approved = v2._v2_pairs({"v2_splits": v3_splits}, "calibration")
    else:
        approved = v2._v2_pairs(config, "calibration")
    reserved = v2._reserved_seeds(config, exclude="calibration")
    for name in v3_splits:
        if name != "calibration":
            for seed, _ in v2._v2_pairs({"v2_splits": v3_splits}, name):
                reserved.setdefault(seed, f"v3_splits.{name}")
    return set(approved), reserved


def _calibration_examples(model, lots, config):
    if not lots:
        raise ValueError("no calibration lots")
    approved, reserved = _approved_and_reserved(config)
    train_seeds = {seed for seed, _ in model["provenance"]["train_pairs"]}
    identities = [v2._lot_identity(lot) for lot in lots]
    v2._check_unique(identities, "calibration")
    for lot_id, seed, scenario in identities:
        if (seed, scenario) not in approved:
            raise ValueError(f"lot {lot_id} is not an approved calibration pair")
        if seed in train_seeds or lot_id in model["train_ids"]:
            raise ValueError(f"calibration lot {lot_id} overlaps model training data")
        if seed in reserved:
            raise ValueError(f"calibration seed {seed} overlaps split {reserved[seed]}")
    x, y = v2._candidate_examples(lots)
    if len(y) == 0:
        raise ValueError("no calibration candidates")
    if y.all() or not y.any():
        raise ValueError("calibration candidates need both DOI and non-DOI labels")
    return identities, x, y


def fit_calibration(model, lots, config, method="platt"):
    """Return a new calibrated model; ``model`` is never modified."""
    if method not in CALIBRATION_METHODS:
        raise ValueError(f"calibration method must be one of {CALIBRATION_METHODS}")
    if method not in PARAMETRIC_METHODS:
        return v2.fit_calibration(model, lots, config, method=method)
    if model.get("calibration") is not None:
        raise ValueError("model is already calibrated; calibrate the raw base model")
    identities, x, y = _calibration_examples(model, lots, config)
    s = _logit(predict_raw(model, x))
    slope, intercept, optimizer = _fit_affine_logit(s, y, fit_intercept=(method == "platt"))
    probability = f"calibrated_{method}"
    calibration = {"method": method, "module": "inspection_v3.model",
                   "slope": slope, "intercept": intercept,
                   "logit_clip_eps": LOGIT_EPS,
                   "slope_bounds": list(SLOPE_BOUNDS), "intercept_bounds": list(INTERCEPT_BOUNDS),
                   "optimizer": optimizer,
                   "calibration_ids": [i[0] for i in identities],
                   "calibration_pairs": [[seed, scenario] for _, seed, scenario in identities],
                   "n_candidates": int(len(y)), "n_positive": int(y.sum()),
                   "base_model_hash": hash_model(model),
                   "base_supports_online_update": bool(model.get("supports_online_update")),
                   "base_probability": model["provenance"]["probability"],
                   "fit_scope": "candidates_only",
                   "probability_semantics": PROBABILITY_SEMANTICS[probability]}
    if method == "temperature":
        calibration["temperature"] = 1.0 / slope
    new = copy.deepcopy(model)
    new["calibration"] = calibration
    new["supports_online_update"] = False
    new["provenance"] = {**new["provenance"], "probability": probability}
    return new


def base_model(model):
    """Reconstruct the raw base model of a v3-calibrated model and verify its hash."""
    calibration = model.get("calibration")
    if calibration is None:
        return copy.deepcopy(model)
    if calibration["method"] not in PARAMETRIC_METHODS:
        raise ValueError("base reconstruction is only recorded for v3 parametric calibration")
    base = copy.deepcopy(model)
    base["calibration"] = None
    base["supports_online_update"] = calibration["base_supports_online_update"]
    base["provenance"] = {**base["provenance"], "probability": calibration["base_probability"]}
    if hash_model(base) != calibration["base_model_hash"]:
        raise ValueError("base model hash mismatch; calibrated model was altered")
    return base


# ---------------------------------------------------------------- prediction

def _parametric(model):
    calibration = model.get("calibration")
    if calibration is not None and calibration["method"] in PARAMETRIC_METHODS:
        return calibration
    return None


def predict_calibrated_logit(model, features):
    """Calibrated logit for v3 parametric models (strictly monotone in the clipped raw logit)."""
    calibration = _parametric(model)
    if calibration is None:
        raise ValueError("model has no v3 parametric calibration")
    s = _logit(predict_raw(model, features))
    return calibration["slope"] * s + calibration["intercept"]


def predict(model, features):
    """DOI probability under the model's frozen definition (calibrated if calibrated)."""
    if _parametric(model) is None:
        return v2.predict(model, features)
    return _sigmoid(predict_calibrated_logit(model, features))


# ---------------------------------------------------------------- evaluation

def evaluate_model(model, lots, config):
    """Candidate-only metrics; not an all-site or physical recall estimate."""
    params = config.get("v2_model") or {}
    threshold = float(params.get("classification_threshold",
                                 (config.get("model") or {}).get("classification_threshold", 0.5)))
    bins = int(params.get("ece_bins", 10))
    grid = [round(0.1 * i, 1) for i in range(1, 11)]
    fit_ids = set(model["train_ids"]) | set((model.get("calibration") or {}).get("calibration_ids", []))
    ys, ps, per_lot = [], [], []
    for lot in lots:
        mask = np.asarray(lot["public"]["candidate"], dtype=bool)
        y = np.asarray(lot["oracle"]["doi"], dtype=bool)[mask]
        p = predict(model, np.asarray(lot["public"]["features"], dtype=float)[mask]) if mask.any() else np.zeros(0)
        ys.append(y)
        ps.append(p)
        per_lot.append({"lot_id": lot["public"]["lot_id"], "scenario": lot["public"].get("scenario"),
                        **v2._metrics(y, p, threshold, bins, grid)})
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    p = np.concatenate(ps) if ps else np.zeros(0)
    aggregate = v2._metrics(y, p, threshold, bins, grid)
    aggregate["log_loss"] = None if len(y) == 0 else _nll(_logit(p), y.astype(float))
    lot_ids = [entry["lot_id"] for entry in per_lot]
    probability = model["provenance"]["probability"]
    return {"model_hash": hash_model(model), "family": model["family"],
            "probability": probability,
            "probability_semantics": PROBABILITY_SEMANTICS.get(probability, probability),
            "threshold": threshold,
            "scope": {"unit": "optical_candidate_sites", "lot_ids": lot_ids,
                      "fit_overlap_lot_ids": sorted(set(lot_ids) & fit_ids),
                      "excludes": "non-candidate sites; not all-site physical recall"},
            "aggregate": aggregate, "per_lot": per_lot}


# ---------------------------------------------------------------- hyperparameter grid

def catboost_grid(config, grid):
    """Expand ``{param: [values]}`` into labelled v2 CatBoost configs (no training).

    Deterministic order (sorted keys, listed values). Selection among the returned configs
    must use development lots only; this helper neither fits nor scores anything.
    """
    if not isinstance(grid, dict) or not grid:
        raise ValueError("grid must be a non-empty {param: [values]} mapping")
    allowed = {"iterations", "depth", "learning_rate", "l2_leaf_reg", "random_seed"}
    unknown = set(grid) - allowed
    if unknown:
        raise ValueError(f"unsupported catboost grid params {sorted(unknown)}")
    keys = sorted(grid)
    values = [list(grid[k]) for k in keys]
    if any(not v for v in values):
        raise ValueError("every grid param needs at least one value")
    out = []
    for combo in itertools.product(*values):
        cfg = copy.deepcopy(config)
        v2_model = dict(cfg.get("v2_model") or {})
        if v2_model.get("family") != "catboost":
            raise ValueError("catboost_grid requires v2_model.family == 'catboost'")
        overrides = dict(zip(keys, combo))
        if "learning_rate" in overrides:
            v2_model.pop("lr", None)
        if "random_seed" in overrides:
            v2_model.pop("seed", None)
        v2_model.update(overrides)
        cfg["v2_model"] = v2_model
        label = ",".join(f"{k}={v}" for k, v in overrides.items())
        out.append({"label": label, "params": overrides, "config": cfg})
    return out
