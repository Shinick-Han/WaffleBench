"""Standardized L2 logistic DOI model for optical candidates (numpy only).

Fitted on training-split candidates only, with oracle.doi as the separate training
annotation. Scaler (mean/scale) comes from the training candidates and is frozen;
missing features are filled with the training mean. Online updates are single SGD
steps that return a new model and never mutate their input.
"""
from __future__ import annotations

import copy
import hashlib
import json

import numpy as np

from .data import FEATURES

LOGIT_CLIP = 30.0


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -LOGIT_CLIP, LOGIT_CLIP)))


def _standardize(model, features):
    x = np.asarray(features, dtype=float)
    if x.ndim == 1:
        x = x[None, :]
    if x.ndim != 2 or x.shape[1] != len(model["feature_names"]):
        raise ValueError(f"features must have shape (m, {len(model['feature_names'])})")
    mean, scale = np.asarray(model["mean"]), np.asarray(model["scale"])
    x = np.where(np.isfinite(x), x, mean)
    return (x - mean) / scale


def _logits(model, z):
    return z @ np.asarray(model["coef"]) + float(model["intercept"])


def train_model(lots, config):
    train_seeds = set(config["splits"]["train_seeds"])
    params = config["model"]
    if not lots:
        raise ValueError("no training lots")
    xs, ys, ids = [], [], []
    for lot in lots:
        public = lot["public"]
        if public["scenario"] != "stationary" or public["seed"] not in train_seeds:
            raise ValueError(f"lot {public['lot_id']} is not a training-split lot")
        mask = np.asarray(public["candidate"], dtype=bool)
        xs.append(np.asarray(public["features"], dtype=float)[mask])
        ys.append(np.asarray(lot["oracle"]["doi"], dtype=bool)[mask])
        ids.append(public["lot_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate training lot")
    x, y = np.concatenate(xs), np.concatenate(ys).astype(float)
    if len(y) == 0:
        raise ValueError("no training candidates")

    mean = np.nanmean(x, axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    x = np.where(np.isfinite(x), x, mean)
    scale = x.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    z = (x - mean) / scale

    l2, lr, epochs = float(params["l2"]), float(params["learning_rate"]), int(params["epochs"])
    w, b = np.zeros(z.shape[1]), 0.0
    m = len(y)
    for _ in range(epochs):
        p = _sigmoid(z @ w + b)
        err = p - y
        w = w - lr * (z.T @ err / m + l2 * w)
        b = b - lr * float(err.mean())
    p = np.clip(_sigmoid(z @ w + b), 1e-12, 1 - 1e-12)
    loss = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)) + 0.5 * l2 * float(w @ w))

    return {
        "family": params["family"],
        "feature_names": list(FEATURES),
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "coef": [float(v) for v in w],
        "intercept": float(b),
        "train_ids": ids,
        "n_train_candidates": int(m),
        "n_train_positive": int(y.sum()),
        "fit": {"l2": l2, "learning_rate": lr, "epochs": epochs, "final_objective": loss},
        "online_updates": 0,
        "parent_hash": None,
    }


def hash_model(model):
    canonical = json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def predict(model, features):
    return _sigmoid(_logits(model, _standardize(model, features)))


def update_model(model, feature_row, label, config):
    """One logistic SGD step with the frozen scaler; returns a new model."""
    row = np.asarray(feature_row, dtype=float)
    if row.ndim != 1:
        raise ValueError("feature_row must be one site's feature vector")
    lr, l2 = float(config["model"]["online_learning_rate"]), float(config["model"]["l2"])
    z = _standardize(model, row)[0]
    w = np.asarray(model["coef"], dtype=float)
    err = float(_sigmoid(z @ w + float(model["intercept"]))) - float(bool(label))
    new = copy.deepcopy(model)
    new["coef"] = [float(v) for v in w - lr * (err * z + l2 * w)]
    new["intercept"] = float(model["intercept"]) - lr * err
    new["online_updates"] = int(model["online_updates"]) + 1
    new["parent_hash"] = hash_model(model)
    return new


def _ratio(num, den):
    return None if den == 0 else num / den


def _metrics(y, p, threshold):
    pred = p >= threshold
    tp, fp = int(np.sum(pred & y)), int(np.sum(pred & ~y))
    fn, tn = int(np.sum(~pred & y)), int(np.sum(~pred & ~y))
    recall, specificity = _ratio(tp, tp + fn), _ratio(tn, tn + fp)
    balanced = None if recall is None or specificity is None else (recall + specificity) / 2
    brier = None if len(y) == 0 else float(np.mean((p - y) ** 2))
    return {"n_candidates": int(len(y)), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": _ratio(tp, tp + fp), "recall": recall,
            "balanced_accuracy": balanced, "brier": brier}


def evaluate_model(model, lots, config):
    """Candidate-only classification metrics; not an all-site accuracy."""
    threshold = float(config["model"]["classification_threshold"])
    ys, ps, per_lot = [], [], []
    for lot in lots:
        mask = np.asarray(lot["public"]["candidate"], dtype=bool)
        y = np.asarray(lot["oracle"]["doi"], dtype=bool)[mask]
        p = predict(model, np.asarray(lot["public"]["features"], dtype=float)[mask]) if mask.any() else np.zeros(0)
        ys.append(y)
        ps.append(p)
        per_lot.append({"lot_id": lot["public"]["lot_id"], **_metrics(y, p, threshold)})
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    p = np.concatenate(ps) if ps else np.zeros(0)
    return {"model_hash": hash_model(model), "threshold": threshold, "scope": "candidates_only",
            "aggregate": _metrics(y, p, threshold), "per_lot": per_lot}
