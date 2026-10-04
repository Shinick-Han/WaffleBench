"""Inspection v2 DOI models: frozen CatBoost or v1-delegated logistic, plus calibration.

Synthetic development study only. Models are plain JSON-serializable dicts:

* Training uses only lots whose privileged (restored) ``public.seed`` is a train seed;
  a non-stationary training lot additionally needs its explicit ``[seed, scenario]``
  pair in ``config['v2_splits']['train']``. Examples are optical candidates labelled
  with ``oracle.doi``.
* Missing/non-finite features are imputed with the train-candidate mean and
  standardized with the train scaler; both are frozen in the model.
* CatBoost (1.2.10, required; no fallback) is stored as its deterministic JSON tree
  structure and never updated online (``supports_online_update=False``).
* Calibration returns a new model fitted only on independent seeds listed in
  ``config['v2_splits']['calibration']``; the raw base model and scaler are kept.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile

import numpy as np

from inspection_review import model as v1_model
from inspection_review.data import FEATURES

CATBOOST_VERSION = "1.2.10"
FAMILIES = ("logistic", "catboost")
CALIBRATION_METHODS = ("identity", "isotonic")
LABEL_DEFINITION = "oracle.doi on optical candidate sites"
_CATBOOST_KEYS = ("features_info", "oblivious_trees", "scale_and_bias")
_BOOSTER_CACHE: dict = {}


def hash_model(model):
    canonical = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- split guards

def _pair(entry):
    if isinstance(entry, dict):
        seed, scenario = entry.get("seed"), entry.get("scenario")
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        seed, scenario = entry
    else:
        raise ValueError(f"split entry {entry!r} must be [seed, scenario] or {{seed, scenario}}")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or not isinstance(scenario, str):
        raise ValueError(f"split entry {entry!r} needs an int seed and str scenario")
    return int(seed), scenario


def _v2_pairs(config, name):
    entries = (config.get("v2_splits") or {}).get(name) or []
    pairs = [_pair(e) for e in entries]
    if len(set(pairs)) != len(pairs):
        raise ValueError(f"duplicate seed/scenario pair in v2_splits.{name}")
    return pairs


def _lot_identity(lot):
    public = lot["public"]
    seed, scenario = public.get("seed"), public.get("scenario")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or not isinstance(scenario, str):
        raise ValueError(f"lot {public.get('lot_id')!r} lacks restored privileged seed/scenario metadata")
    return public["lot_id"], int(seed), scenario


def _check_unique(identities, what):
    ids = [i[0] for i in identities]
    pairs = [(i[1], i[2]) for i in identities]
    if len(set(ids)) != len(ids) or len(set(pairs)) != len(pairs):
        raise ValueError(f"duplicate {what} lot")


def _reserved_seeds(config, exclude):
    """Seeds of every split other than ``exclude`` (v1 splits plus all v2_splits keys)."""
    splits = config.get("splits") or {}
    seeds = {}
    for name in ("train_seeds", "validation_seeds"):
        if name != exclude:
            for s in splits.get(name) or []:
                seeds.setdefault(int(s), name)
    if exclude != "test":
        for scenario, test_seeds in (splits.get("test_seeds_by_scenario") or {}).items():
            for s in test_seeds:
                seeds.setdefault(int(s), f"test:{scenario}")
    for name in (config.get("v2_splits") or {}):
        if name != exclude:
            for seed, _ in _v2_pairs(config, name):
                seeds.setdefault(seed, f"v2_splits.{name}")
    return seeds


def _training_examples(lots, config):
    if not lots:
        raise ValueError("no training lots")
    train_seeds = {int(s) for s in config["splits"]["train_seeds"]}
    approved = set(_v2_pairs(config, "train"))
    for seed, scenario in approved:
        if seed not in train_seeds:
            raise ValueError(f"v2_splits.train pair ({seed}, {scenario!r}) is not a train seed")
    calibration_seeds = {seed for seed, _ in _v2_pairs(config, "calibration")}
    if calibration_seeds & train_seeds:
        raise ValueError("calibration seeds overlap train seeds")
    identities = [_lot_identity(lot) for lot in lots]
    _check_unique(identities, "training")
    for lot_id, seed, scenario in identities:
        if seed not in train_seeds:
            raise ValueError(f"lot {lot_id} seed {seed} is not a train seed")
        if scenario != "stationary" and (seed, scenario) not in approved:
            raise ValueError(f"non-stationary lot {lot_id} is not an approved v2_splits.train pair")
    x, y = _candidate_examples(lots)
    if len(y) == 0:
        raise ValueError("no training candidates")
    return identities, x, y


def _candidate_examples(lots):
    xs, ys = [], []
    for lot in lots:
        mask = np.asarray(lot["public"]["candidate"], dtype=bool)
        xs.append(np.asarray(lot["public"]["features"], dtype=float)[mask])
        ys.append(np.asarray(lot["oracle"]["doi"], dtype=bool)[mask])
    x = np.concatenate(xs) if xs else np.zeros((0, len(FEATURES)))
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    if x.ndim != 2 or x.shape[1] != len(FEATURES):
        raise ValueError(f"lot features must have {len(FEATURES)} columns")
    return x, y


# ---------------------------------------------------------------- training

def _model_params(config):
    params = dict(config.get("v2_model") or {})
    family = params.get("family")
    if family not in FAMILIES:
        raise ValueError(f"v2_model.family must be one of {FAMILIES}, got {family!r}")
    return family, params


def _train_scaler(x):
    with np.errstate(all="ignore"):
        finite = np.where(np.isfinite(x), x, np.nan)
        mean = np.nanmean(finite, axis=0) if len(x) else np.full(x.shape[1], np.nan)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    filled = np.where(np.isfinite(x), x, mean)
    scale = filled.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return mean, scale


def _import_catboost():
    try:
        import catboost
    except ImportError as exc:  # no silent fallback to another family
        raise RuntimeError(f"catboost=={CATBOOST_VERSION} is required for family 'catboost'") from exc
    if catboost.__version__ != CATBOOST_VERSION:
        raise RuntimeError(f"catboost=={CATBOOST_VERSION} required, found {catboost.__version__}")
    return catboost


def _catboost_settings(params):
    for a, b in (("learning_rate", "lr"), ("random_seed", "seed")):
        if a in params and b in params and params[a] != params[b]:
            raise ValueError(f"v2_model has conflicting {a!r} and {b!r}")
    settings = {"iterations": params.get("iterations"), "depth": params.get("depth"),
                "learning_rate": params.get("learning_rate", params.get("lr")),
                "random_seed": params.get("random_seed", params.get("seed")),
                "thread_count": params.get("thread_count"),
                "l2_leaf_reg": params.get("l2_leaf_reg", 3.0)}
    missing = [k for k, v in settings.items() if v is None]
    if missing:
        raise ValueError(f"v2_model for catboost requires {missing}")
    return {"iterations": int(settings["iterations"]), "depth": int(settings["depth"]),
            "learning_rate": float(settings["learning_rate"]),
            "random_seed": int(settings["random_seed"]),
            "thread_count": int(settings["thread_count"]),
            "l2_leaf_reg": float(settings["l2_leaf_reg"])}


def _fit_catboost(z, y, settings):
    catboost = _import_catboost()
    if y.all() or not y.any():
        raise ValueError("catboost training candidates need both DOI and non-DOI labels")
    clf = catboost.CatBoostClassifier(
        iterations=settings["iterations"], depth=settings["depth"],
        learning_rate=settings["learning_rate"], random_seed=settings["random_seed"],
        l2_leaf_reg=settings["l2_leaf_reg"], thread_count=settings["thread_count"],
        loss_function="Logloss",
        verbose=False, allow_writing_files=False)
    clf.fit(z, y.astype(int))
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        clf.save_model(path, format="json")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    finally:
        os.unlink(path)
    # Keep only the deterministic tree structure (drop guid, timestamps, train paths).
    booster = {k: raw[k] for k in _CATBOOST_KEYS}
    booster["model_info"] = {"class_params": raw["model_info"]["class_params"]}
    return booster


def train_model(lots, config):
    family, params = _model_params(config)
    identities, x, y = _training_examples(lots, config)
    train_pairs = [[seed, scenario] for _, seed, scenario in identities]
    provenance = {"module": "inspection_v2.model", "label": LABEL_DEFINITION,
                  "train_pairs": train_pairs, "imputation": "train_candidate_mean",
                  "probability": "raw_base", "numpy_version": np.__version__}
    if family == "logistic":
        if any(scenario != "stationary" for _, _, scenario in identities):
            raise ValueError("logistic delegates to the frozen v1 model and is stationary-only")
        v1 = v1_model.train_model(lots, _v1_config(config, params))
        model = {**v1, "family": "logistic", "base_family": v1["family"],
                 "supports_online_update": True, "calibration": None,
                 "provenance": {**provenance, "delegate": "inspection_review.model.train_model"}}
        return model

    settings = _catboost_settings(params)
    mean, scale = _train_scaler(x)
    z = (np.where(np.isfinite(x), x, mean) - mean) / scale
    booster = _fit_catboost(z, y, settings)
    return {
        "family": "catboost",
        "feature_names": list(FEATURES),
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "booster": booster,
        "train_ids": [lot_id for lot_id, _, _ in identities],
        "n_train_candidates": int(len(y)),
        "n_train_positive": int(y.sum()),
        "fit": {**settings, "loss_function": "Logloss"},
        "supports_online_update": False,
        "calibration": None,
        "provenance": {**provenance, "catboost_version": CATBOOST_VERSION},
    }


def _v1_config(config, params):
    base = dict(config.get("model") or {})
    for v2_key, v1_key in (("l2", "l2"), ("iterations", "epochs"), ("epochs", "epochs"),
                           ("learning_rate", "learning_rate"), ("lr", "learning_rate"),
                           ("online_learning_rate", "online_learning_rate")):
        if v2_key in params:
            base[v1_key] = params[v2_key]
    base.setdefault("family", "numpy_l2_logistic")
    missing = [k for k in ("l2", "epochs", "learning_rate") if k not in base]
    if missing:
        raise ValueError(f"logistic model requires {missing} in v2_model or model")
    return {"splits": {"train_seeds": list(config["splits"]["train_seeds"])}, "model": base}


# ---------------------------------------------------------------- prediction

def _standardize(model, features):
    x = np.asarray(features, dtype=float)
    if x.ndim == 1:
        x = x[None, :]
    if x.ndim != 2 or x.shape[1] != len(model["feature_names"]):
        raise ValueError(f"features must have shape (n, {len(model['feature_names'])})")
    mean, scale = np.asarray(model["mean"], dtype=float), np.asarray(model["scale"], dtype=float)
    return (np.where(np.isfinite(x), x, mean) - mean) / scale


def _booster(model):
    booster = model["booster"]
    key = hashlib.sha256(json.dumps(booster, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    clf = _BOOSTER_CACHE.get(key)
    if clf is None:
        catboost = _import_catboost()
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(booster, fh)
            clf = catboost.CatBoostClassifier()
            clf.load_model(path, format="json")
        finally:
            os.unlink(path)
        _BOOSTER_CACHE[key] = clf
    return clf


def predict_raw(model, features):
    """Uncalibrated base-model DOI probability."""
    if model["family"] == "logistic":
        return v1_model.predict(model, features)
    if model["family"] == "catboost":
        z = _standardize(model, features)
        if len(z) == 0:
            return np.zeros(0)
        return _booster(model).predict_proba(z)[:, 1].astype(float)
    raise ValueError(f"unknown model family {model['family']!r}")


def _apply_calibration(calibration, p):
    if calibration is None or calibration["method"] == "identity":
        return p
    return np.interp(p, calibration["x"], calibration["y"])


def predict(model, features):
    """DOI probability under the model's frozen definition (calibrated if calibrated)."""
    return _apply_calibration(model.get("calibration"), predict_raw(model, features))


# ---------------------------------------------------------------- calibration

def _isotonic(x, y):
    """Pool-adjacent-violators on unique sorted x; returns (thresholds, fitted values)."""
    ux, inverse = np.unique(x, return_inverse=True)
    w = np.bincount(inverse).astype(float)
    v = np.bincount(inverse, weights=y.astype(float)) / w
    blocks = []  # [value, weight, count]
    for vi, wi in zip(v, w):
        blocks.append([vi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b = blocks.pop()
            a = blocks[-1]
            a[0] = (a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1])
            a[1] += b[1]
            a[2] += b[2]
    fitted = np.repeat([b[0] for b in blocks], [b[2] for b in blocks])
    return ux, fitted


def fit_calibration(model, lots, config, method="isotonic"):
    if method not in CALIBRATION_METHODS:
        raise ValueError(f"calibration method must be one of {CALIBRATION_METHODS}")
    if model.get("calibration") is not None:
        raise ValueError("model is already calibrated; calibrate the raw base model")
    if not lots:
        raise ValueError("no calibration lots")
    approved = set(_v2_pairs(config, "calibration"))
    reserved = _reserved_seeds(config, exclude="calibration")
    train_seeds = {seed for seed, _ in model["provenance"]["train_pairs"]}
    identities = [_lot_identity(lot) for lot in lots]
    _check_unique(identities, "calibration")
    for lot_id, seed, scenario in identities:
        if (seed, scenario) not in approved:
            raise ValueError(f"lot {lot_id} is not an approved v2_splits.calibration pair")
        if seed in train_seeds or lot_id in model["train_ids"]:
            raise ValueError(f"calibration lot {lot_id} overlaps model training data")
        if seed in reserved:
            raise ValueError(f"calibration seed {seed} overlaps split {reserved[seed]}")
    x, y = _candidate_examples(lots)
    if len(y) == 0:
        raise ValueError("no calibration candidates")
    raw = predict_raw(model, x)
    calibration = {"method": method, "calibration_ids": [i[0] for i in identities],
                   "calibration_pairs": [[s, sc] for _, s, sc in identities],
                   "n_candidates": int(len(y)), "n_positive": int(y.sum()),
                   "base_model_hash": hash_model(model), "fit_scope": "candidates_only"}
    if method == "isotonic":
        thresholds, fitted = _isotonic(raw, y)
        calibration["x"] = [float(v) for v in thresholds]
        calibration["y"] = [float(v) for v in fitted]
    new = copy.deepcopy(model)
    new["calibration"] = calibration
    new["supports_online_update"] = False
    new["provenance"] = {**new["provenance"], "probability": f"calibrated_{method}"}
    return new


# ---------------------------------------------------------------- online update

def update_model(model, feature_row, label, config):
    if model["family"] != "logistic" or not model.get("supports_online_update"):
        raise ValueError(f"model family {model['family']!r} does not support online updates")
    if model.get("calibration") is not None:
        raise ValueError("calibrated models are frozen; online updates are rejected")
    _, params = _model_params(config)
    return v1_model.update_model(model, feature_row, label, _v1_config(config, params))


# ---------------------------------------------------------------- evaluation

def _ratio(num, den):
    return None if den == 0 else num / den


def _average_precision(y, p):
    positives = int(y.sum())
    if positives == 0:
        return None
    order = np.argsort(-p, kind="mergesort")
    ys, ps = y[order], p[order]
    tp = np.cumsum(ys)
    last = np.r_[ps[1:] != ps[:-1], True]  # end of each tied-score group
    tp_at, k_at = tp[last], np.flatnonzero(last) + 1
    recall = tp_at / positives
    precision = tp_at / k_at
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def _ece(y, p, bins):
    if len(y) == 0:
        return None
    idx = np.minimum((p * bins).astype(int), bins - 1)
    total = 0.0
    for b in range(bins):
        sel = idx == b
        if sel.any():
            total += sel.sum() * abs(float(p[sel].mean()) - float(y[sel].mean()))
    return float(total / len(y))


def _risk_coverage(y, p, threshold, grid):
    n = len(y)
    if n == 0:
        return {"aurc": None, "points": []}
    confidence = np.maximum(p, 1.0 - p)
    order = np.argsort(-confidence, kind="mergesort")
    errors = np.cumsum((p[order] >= threshold) != y[order])
    risk = errors / np.arange(1, n + 1)
    points = []
    for c in grid:
        k = max(1, int(np.ceil(c * n)))
        points.append({"coverage": float(c), "n": k, "risk": float(risk[k - 1])})
    return {"aurc": float(risk.mean()), "points": points}


def _metrics(y, p, threshold, bins, grid):
    pred = p >= threshold
    tp, fp = int(np.sum(pred & y)), int(np.sum(pred & ~y))
    fn, tn = int(np.sum(~pred & y)), int(np.sum(~pred & ~y))
    return {"n_candidates": int(len(y)), "n_positive": int(y.sum()),
            "prevalence": _ratio(int(y.sum()), len(y)),
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": _ratio(tp, tp + fp), "recall_among_candidates": _ratio(tp, tp + fn),
            "average_precision": _average_precision(y, p),
            "brier": None if len(y) == 0 else float(np.mean((p - y) ** 2)),
            "ece": _ece(y, p, bins), "ece_bins": bins,
            "risk_coverage": _risk_coverage(y, p, threshold, grid)}


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
                        **_metrics(y, p, threshold, bins, grid)})
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    p = np.concatenate(ps) if ps else np.zeros(0)
    lot_ids = [entry["lot_id"] for entry in per_lot]
    return {"model_hash": hash_model(model), "family": model["family"],
            "probability": model["provenance"]["probability"], "threshold": threshold,
            "scope": {"unit": "optical_candidate_sites", "lot_ids": lot_ids,
                      "fit_overlap_lot_ids": sorted(set(lot_ids) & fit_ids),
                      "excludes": "non-candidate sites; not all-site physical recall"},
            "aggregate": _metrics(y, p, threshold, bins, grid), "per_lot": per_lot}
