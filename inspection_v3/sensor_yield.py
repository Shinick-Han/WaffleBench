"""Inspection v3 review-yield model: P(reported-review-positive | public optical features).

Authored synthetic development study only. The model predicts whether one historical
review sequence (first attempt, retry on failure/missing, stop at the first ``ok``) of
an original optical candidate ends in a *reported* DOI-positive observation. It is not a
true DOI probability, not a latent kind/size/electrical estimate and makes no accuracy
or yield-improvement claim.

* Seed/scenario/duplicate guards are delegated to ``inspection_v2.model._training_examples``
  and run before any review outcome is read; its latent-DOI labels are discarded.
* Labels come only from ``inspection_review.data.review_observation``: 1 for an ok
  observation with ``reported_doi=True``; 0 for an ok negative report and for an
  exhausted failure/missing sequence.
* Historical acquisition is counted separately from any evaluation budget and covers
  only dwell and retry_dwell (wafer load, stage movement and rescan are omitted).
* Fitting reuses the v2 scaler and JSON CatBoost primitives on the seven public
  features with ``config['v3_yield']`` settings. The returned dict is v2-compatible
  (``family='catboost'``) so ``inspection_v2.model.predict_raw`` scores it; it is frozen
  (no online update) and is meant only as an immutable selection reward, kept separate
  from the frozen DOI probability.
"""
from __future__ import annotations

import numpy as np

from inspection_review.data import FEATURES, review_observation
from inspection_v2 import model as v2_model

TARGET = "reported_review_positive"
LABEL_DEFINITION = ("1 iff a historical review of an original optical candidate ended in an ok "
                    "observation with reported_doi=True under the retry rule (retry on "
                    "failure/missing, stop at first ok); ok-negative and exhausted "
                    "failure/missing sequences are 0. Not latent DOI, kind, size or electrical.")
ACQUISITION_INCLUDED = ("dwell", "retry_dwell")
ACQUISITION_OMITTED = ("wafer_load", "stage_base", "stage_per_normalized_distance", "outside_rescan")
hash_model = v2_model.hash_model


# ---------------------------------------------------------------- guards

def _guard_split_overlap(config):
    """v2_splits.train seeds must not appear in any other split (v1 or v2)."""
    train = {seed for seed, _ in v2_model._v2_pairs(config, "train")}
    splits = config.get("splits") or {}
    others = {int(s): "validation_seeds" for s in splits.get("validation_seeds") or []}
    for scenario, seeds in (splits.get("test_seeds_by_scenario") or {}).items():
        others.update({int(s): f"test:{scenario}" for s in seeds})
    for name in config.get("v2_splits") or {}:
        if name != "train":
            others.update({seed: f"v2_splits.{name}" for seed, _ in v2_model._v2_pairs(config, name)})
    for seed in sorted(train & set(others)):
        raise ValueError(f"v2_splits.train seed {seed} overlaps split {others[seed]}")


# ---------------------------------------------------------------- observed labels

def _retry_limit(config):
    limit = config["cost"]["retry_limit"]
    if isinstance(limit, bool) or int(limit) != limit or limit < 0:
        raise ValueError("retry_limit must be a non-negative integer")
    return int(limit)


def _review_sequence(oracle, index, retry_limit):
    """Observable attempts for one site: retry on failure/missing, stop at the first ok."""
    attempts = []
    for attempt in range(retry_limit + 1):
        obs = review_observation(oracle, index, attempt)
        attempts.append(obs)
        if obs["status"] == "ok" and obs["reported_doi"] is not None:
            break
    return attempts


def _observed_examples(lots, config):
    """Public features and reported-review labels for original optical candidates."""
    retry_limit = _retry_limit(config)
    cost = config["cost"]
    counts = {"sites": 0, "attempts": 0, "retries": 0, "ok": 0, "failure": 0, "missing": 0,
              "reported_positive": 0, "reported_negative": 0, "exhausted": 0}
    xs, ys = [], []
    for lot in lots:
        mask = np.asarray(lot["public"]["candidate"], dtype=bool)
        features = np.asarray(lot["public"]["features"], dtype=float)
        idx = np.flatnonzero(mask)
        labels = np.zeros(len(idx), dtype=bool)
        for k, i in enumerate(idx):
            attempts = _review_sequence(lot["oracle"], int(i), retry_limit)
            counts["sites"] += 1
            counts["attempts"] += len(attempts)
            counts["retries"] += len(attempts) - 1
            for obs in attempts:
                counts[obs["status"]] += 1
            last = attempts[-1]
            if last["status"] == "ok" and last["reported_doi"] is not None:
                labels[k] = bool(last["reported_doi"])
                counts["reported_positive" if labels[k] else "reported_negative"] += 1
            else:
                counts["exhausted"] += 1
        xs.append(features[idx])
        ys.append(labels)
    x = np.concatenate(xs) if xs else np.zeros((0, len(FEATURES)))
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    dwell = float(cost["dwell"]) * counts["sites"]
    retry = float(cost["retry_dwell"]) * counts["retries"]
    acquisition = {"unit": "synthetic_equipment_cost_unit",
                   "included_components": list(ACQUISITION_INCLUDED),
                   "omitted_components": list(ACQUISITION_OMITTED),
                   "dwell": dwell, "retry_dwell": retry, "total_included": dwell + retry,
                   "charged_to_evaluation_budget": False,
                   "note": "dwell + retry_dwell only; not a full equipment or route cost"}
    return x, y, counts, acquisition


# ---------------------------------------------------------------- fit / predict

def _settings(config):
    params = dict(config.get("v3_yield") or {})
    if not params:
        raise ValueError("config v3_yield CatBoost settings are required")
    params.pop("family", None)
    return v2_model._catboost_settings(params)


def fit_review_yield(lots, config):
    # Identity guards first (public metadata only); every lot must be an explicit
    # v2_splits.train pair, then v2 seed/duplicate guards. Its latent-DOI labels are discarded.
    approved = set(v2_model._v2_pairs(config, "train"))
    for lot in lots:
        lot_id, seed, scenario = v2_model._lot_identity(lot)
        if (seed, scenario) not in approved:
            raise ValueError(f"lot {lot_id} is not an approved v2_splits.train pair")
    _guard_split_overlap(config)
    identities, _, _ = v2_model._training_examples(lots, config)
    settings = _settings(config)
    x, y, counts, acquisition = _observed_examples(lots, config)
    if len(y) == 0:
        raise ValueError("no historical candidate reviews")
    if y.all() or not y.any():
        raise ValueError("review-yield training needs both reported-positive and other outcomes")
    mean, scale = v2_model._train_scaler(x)
    z = (np.where(np.isfinite(x), x, mean) - mean) / scale
    booster = v2_model._fit_catboost(z, y, settings)
    return {
        "family": "catboost",
        "target": TARGET,
        "feature_names": list(FEATURES),
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "booster": booster,
        "train_ids": [lot_id for lot_id, _, _ in identities],
        "n_train_candidates": int(len(y)),
        "n_train_positive": int(y.sum()),
        "fit": {**settings, "loss_function": "Logloss"},
        "historical_reviews": counts,
        "historical_acquisition_cost": acquisition,
        "online": False,
        "supports_online_update": False,
        "calibration": None,
        "provenance": {
            "module": "inspection_v3.sensor_yield",
            "label": LABEL_DEFINITION,
            "probability": "reported_review_positive",
            "prediction": "reported-review-positive probability for one retry-rule review sequence",
            "not": "true DOI probability; no accuracy or yield-improvement claim",
            "data": "authored historical synthetic review measurements",
            "train_pairs": [[seed, scenario] for _, seed, scenario in identities],
            "imputation": "train_candidate_mean",
            "catboost_version": v2_model.CATBOOST_VERSION,
            "numpy_version": np.__version__,
        },
    }


def predict_review_yield(model, features):
    """Reported-review-positive probability in [0, 1]; NaN/inf imputed with the train mean."""
    if model.get("target") != TARGET:
        raise ValueError("not a reported_review_positive model")
    return np.clip(v2_model.predict_raw(model, features), 0.0, 1.0)


def evaluate_review_yield(model, lots, config, split="development"):
    """Reported-review metrics on a non-train v2 split; its reviews are counted separately."""
    if split == "train" or split not in (config.get("v2_splits") or {}):
        raise ValueError(f"evaluation split {split!r} must be a non-train v2_splits key")
    approved = set(v2_model._v2_pairs(config, split))
    train_seeds = {p[0] for p in model["provenance"]["train_pairs"]}
    identities = [v2_model._lot_identity(lot) for lot in lots]
    v2_model._check_unique(identities, "evaluation")
    for lot_id, seed, scenario in identities:
        if (seed, scenario) not in approved:
            raise ValueError(f"lot {lot_id} is not an approved v2_splits.{split} pair")
        if seed in train_seeds or lot_id in model["train_ids"]:
            raise ValueError(f"evaluation lot {lot_id} overlaps model training data")
    x, y, counts, acquisition = _observed_examples(lots, config)
    p = predict_review_yield(model, x)
    threshold = float(config["v3_yield"].get("classification_threshold", 0.5))
    grid = [round(0.1 * i, 1) for i in range(1, 11)]
    return {"model_hash": hash_model(model), "target": TARGET, "split": split,
            "lot_ids": [i[0] for i in identities], "threshold": threshold,
            "scope": "original optical candidates; reported-review labels, not true DOI",
            "reviews": counts, "acquisition_cost": acquisition,
            "aggregate": v2_model._metrics(y, p, threshold, 10, grid)}
