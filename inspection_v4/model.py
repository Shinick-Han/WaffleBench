"""Inspection v4 DOI hypothesis: supervised class-conditional Gaussian mixtures.

Synthetic development study only. A fresh-data hypothesis next to the frozen v2/v3
logistic and CatBoost models; those are delegated to ``inspection_v3.model`` unchanged
whenever ``config['v4_model']`` is absent (or the model family is not ``gaussian_mixture``).

Mixture model (``config['v4_model']``, see ``DEFAULT_V4_MODEL``):

* Same seven admitted public features (``inspection_review.data.FEATURES``) and the same
  label (``oracle.doi`` on original optical candidate sites) as the logistic/CatBoost
  baselines. Public ``seed``/``scenario`` metadata is used only by the split guards,
  never as a feature; no generator constants, signatures or latent kinds are used.
* Split guards run on lot identities before any outcome is read and reuse the v2
  helpers. Without ``config['v4_splits']`` the v2 conventions apply unchanged
  (``splits.train_seeds`` + ``v2_splits``). With ``v4_splits`` (same ``[seed, scenario]``
  format) every training lot needs an explicit ``v4_splits.train`` pair and its seed may
  belong to no prior non-train split; calibration pairs come from
  ``v4_splits.calibration`` and their seeds may belong to no other split at all.
* Missing/non-finite features are imputed with the train-candidate mean and
  standardized with the train scaler (frozen ``mean``/``scale``, seven values each).
  Imputed or out-of-train-range rows are scored by the mixture tails: this is explicit
  unsupported extrapolation, reported as a diagnostic, never an all-site recall claim.
* Per class, a K-component mixture (diagonal covariance by default, bounded full
  covariance optional) is fitted by deterministic seeded EM (k-means++ seeding from
  ``numpy.random.default_rng([random_seed, class])``) with additive ``reg_covar``
  covariance floors and log-sum-exp responsibilities. The DOI probability is the
  posterior ``sigmoid(log pi1 + log p(z|1) - log pi0 - log p(z|0))``.
* Models are plain JSON dicts, frozen (``supports_online_update=False``). Calibration
  (identity/isotonic/platt/temperature) returns a new model that keeps the base fields
  and records the base hash, calibration ids/pairs and fit scope.
"""
from __future__ import annotations

import copy

import numpy as np

from inspection_review.data import FEATURES
from inspection_v2 import model as v2
from inspection_v3 import model as v3

FAMILY = "gaussian_mixture"
COVARIANCE_TYPES = ("diag", "full")
CALIBRATION_METHODS = v3.CALIBRATION_METHODS
PARAMETRIC_METHODS = v3.PARAMETRIC_METHODS
LOGIT_EPS = v3.LOGIT_EPS
LOGIT_CLIP = float(np.log1p(-LOGIT_EPS) - np.log(LOGIT_EPS))
LABEL_DEFINITION = v2.LABEL_DEFINITION
DEFAULT_V4_MODEL = {"family": FAMILY, "components_positive": 4, "components_negative": 3,
                    "covariance_type": "diag", "max_iter": 100, "random_seed": 2026100404,
                    "reg_covar": 1e-3, "tol": 1e-6}
_OPTIONAL_KEYS = ("classification_threshold", "ece_bins")
MAX_COMPONENTS = {"diag": 16, "full": 8}
MAX_ITER = 1000
REG_COVAR_BOUNDS = {"diag": (1e-9, 1.0), "full": (1e-6, 1.0)}
EXTRAPOLATION = ("unsupported: imputed or out-of-train-range candidates are scored by "
                 "mixture tails; not an all-site or physical recall estimate")
PROBABILITY_SEMANTICS = {
    "raw_base": "class-conditional Gaussian-mixture posterior DOI probability on optical candidate sites",
    "calibrated_identity": "raw mixture posterior (identity calibration recorded on calibration candidates)",
    "calibrated_isotonic": "isotonic map of the raw mixture posterior fitted on calibration candidates",
    "calibrated_platt": "sigmoid(slope * clip(mixture logit) + intercept) fitted on calibration candidates",
    "calibrated_temperature": "sigmoid(clip(mixture logit) / temperature) fitted on calibration candidates",
}

hash_model = v2.hash_model


def _is_mixture(model):
    return model.get("family") == FAMILY


# ---------------------------------------------------------------- config

def _mixture_params(config):
    """Validated v4_model params, or None when the config selects a v2/v3 baseline."""
    raw = config.get("v4_model")
    if raw is None:
        return None
    if raw.get("family") != FAMILY:
        raise ValueError(f"v4_model.family must be {FAMILY!r}, got {raw.get('family')!r}")
    unknown = set(raw) - set(DEFAULT_V4_MODEL) - set(_OPTIONAL_KEYS)
    if unknown:
        raise ValueError(f"unsupported v4_model keys {sorted(unknown)}")
    params = {**DEFAULT_V4_MODEL, **{k: raw[k] for k in DEFAULT_V4_MODEL if k in raw}}
    cov = params["covariance_type"]
    if cov not in COVARIANCE_TYPES:
        raise ValueError(f"v4_model.covariance_type must be one of {COVARIANCE_TYPES}")
    for key in ("components_positive", "components_negative", "max_iter", "random_seed"):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise ValueError(f"v4_model.{key} must be an int")
        params[key] = int(value)
    for key in ("components_positive", "components_negative"):
        if not 1 <= params[key] <= MAX_COMPONENTS[cov]:
            raise ValueError(f"v4_model.{key} must be in [1, {MAX_COMPONENTS[cov]}] for {cov}")
    if not 1 <= params["max_iter"] <= MAX_ITER:
        raise ValueError(f"v4_model.max_iter must be in [1, {MAX_ITER}]")
    if params["random_seed"] < 0:
        raise ValueError("v4_model.random_seed must be non-negative")
    lo, hi = REG_COVAR_BOUNDS[cov]
    params["reg_covar"] = float(params["reg_covar"])
    params["tol"] = float(params["tol"])
    if not lo <= params["reg_covar"] <= hi:
        raise ValueError(f"v4_model.reg_covar must be in [{lo}, {hi}] for {cov}")
    if not 0.0 < params["tol"] < 1.0:
        raise ValueError("v4_model.tol must be in (0, 1)")
    return params


# ---------------------------------------------------------------- split guards

def _split_pairs(splits, name):
    return v2._v2_pairs({"v2_splits": splits}, name)


def _prior_reserved(config, keep):
    """Seeds of every v1/v2/v3/v4 split, except split names for which ``keep`` is true."""
    reserved = {}
    for seed, label in v2._reserved_seeds(config, exclude=None).items():
        if not keep(label):
            reserved[seed] = label
    for prefix in ("v2_splits", "v3_splits", "v4_splits"):
        splits = config.get(prefix) or {}
        for name in splits:
            label = f"{prefix}.{name}"
            if keep(label):
                continue
            for seed, _ in _split_pairs(splits, name):
                reserved.setdefault(seed, label)
    return reserved


def _training_examples(lots, config):
    """Identity/duplicate/split guards before any outcome is read, then candidates."""
    v4_splits = config.get("v4_splits")
    if v4_splits is None:
        return "v2_splits", v2._training_examples(lots, config)
    if not lots:
        raise ValueError("no training lots")
    approved = _split_pairs(v4_splits, "train")
    if not approved:
        raise ValueError("v4_splits.train is empty")
    train_labels = {"train_seeds", "v2_splits.train", "v3_splits.train", "v4_splits.train"}
    reserved = _prior_reserved(config, keep=lambda label: label in train_labels)
    for seed, scenario in approved:
        if seed in reserved:
            raise ValueError(f"v4_splits.train seed {seed} overlaps split {reserved[seed]}")
    identities = [v2._lot_identity(lot) for lot in lots]
    v2._check_unique(identities, "training")
    for lot_id, seed, scenario in identities:
        if (seed, scenario) not in set(approved):
            raise ValueError(f"lot {lot_id} is not an approved v4_splits.train pair")
    guard = {"splits": {"train_seeds": sorted({seed for seed, _ in approved})},
             "v2_splits": {"train": [list(p) for p in approved],
                           "calibration": [list(p) for p in _split_pairs(v4_splits, "calibration")]}}
    return "v4_splits", v2._training_examples(lots, guard)


def _approved_and_reserved(config):
    v4_splits = config.get("v4_splits") or {}
    if "calibration" not in v4_splits:
        return v3._approved_and_reserved(config)
    approved = set(_split_pairs(v4_splits, "calibration"))
    reserved = _prior_reserved(config, keep=lambda label: label == "v4_splits.calibration")
    return approved, reserved


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


# ---------------------------------------------------------------- mixture numerics

def _logsumexp(a, axis):
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    return np.squeeze(m, axis=axis) + np.log(np.sum(np.exp(a - m), axis=axis))


def _component_log_density(z, means, covariances, covariance_type):
    """log N(z | mean_k, cov_k) for every row and component, shape (n, K)."""
    n, d = z.shape
    out = np.empty((n, len(means)))
    for k, (mean, cov) in enumerate(zip(means, covariances)):
        diff = z - mean
        if covariance_type == "diag":
            maha = np.sum(diff * diff / cov, axis=1)
            logdet = float(np.sum(np.log(cov)))
        else:
            chol = np.linalg.cholesky(cov)
            y = np.linalg.solve(chol, diff.T)
            maha = np.sum(y * y, axis=0)
            logdet = 2.0 * float(np.sum(np.log(np.diag(chol))))
        out[:, k] = -0.5 * (d * np.log(2.0 * np.pi) + logdet + maha)
    return out


def _class_log_likelihood(z, mixture, covariance_type):
    weights = np.asarray(mixture["weights"], dtype=float)
    means = np.asarray(mixture["means"], dtype=float)
    covariances = np.asarray(mixture["covariances"], dtype=float)
    return _logsumexp(np.log(weights) + _component_log_density(z, means, covariances, covariance_type), axis=1)


def _kmeans_pp(z, k, rng):
    """Deterministic (given rng) k-means++ seeding on distinct rows."""
    centers = [z[int(rng.integers(len(z)))]]
    d2 = np.sum((z - centers[0]) ** 2, axis=1)
    for _ in range(1, k):
        total = float(d2.sum())
        if total <= 0.0:
            break
        idx = int(np.searchsorted(np.cumsum(d2), rng.random() * total, side="right"))
        centers.append(z[min(idx, len(z) - 1)])
        d2 = np.minimum(d2, np.sum((z - centers[-1]) ** 2, axis=1))
    return np.array(centers)


def _m_step(z, resp, reg_covar, covariance_type, fallback_cov):
    n, d = z.shape
    nk = resp.sum(axis=0)
    weights, means, covariances = [], [], []
    for k in range(resp.shape[1]):
        if nk[k] < 1e-8:
            # Collapsed component: keep it alive at the global moments with a tiny weight.
            weights.append(1e-8)
            means.append(z.mean(axis=0))
            covariances.append(fallback_cov)
            continue
        r = resp[:, k]
        mean = r @ z / nk[k]
        diff = z - mean
        if covariance_type == "diag":
            cov = r @ (diff * diff) / nk[k] + reg_covar
        else:
            cov = (diff * r[:, None]).T @ diff / nk[k] + reg_covar * np.eye(d)
            cov = 0.5 * (cov + cov.T)
        weights.append(nk[k] / n)
        means.append(mean)
        covariances.append(cov)
    weights = np.asarray(weights)
    return weights / weights.sum(), np.asarray(means), np.asarray(covariances)


def _fit_class_mixture(z, k, params, class_index):
    covariance_type, reg = params["covariance_type"], params["reg_covar"]
    n, d = z.shape
    k_eff = min(k, len(np.unique(z, axis=0)))
    rng = np.random.default_rng([params["random_seed"], class_index])
    if covariance_type == "diag":
        fallback = z.var(axis=0) + reg
    else:
        fallback = np.atleast_2d(np.cov(z, rowvar=False, bias=True)) + reg * np.eye(d) if n > 1 else reg * np.eye(d)
    centers = _kmeans_pp(z, k_eff, rng)
    k_eff = len(centers)
    assign = np.argmin(((z[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
    resp = np.zeros((n, k_eff))
    resp[np.arange(n), assign] = 1.0
    weights, means, covariances = _m_step(z, resp, reg, covariance_type, fallback)
    previous, converged, iterations = -np.inf, False, 0
    lower_bound = previous
    for iterations in range(1, params["max_iter"] + 1):
        log_joint = np.log(weights) + _component_log_density(z, means, covariances, covariance_type)
        log_norm = _logsumexp(log_joint, axis=1)
        lower_bound = float(log_norm.mean())
        if abs(lower_bound - previous) < params["tol"]:
            converged = True
            break
        previous = lower_bound
        resp = np.exp(log_joint - log_norm[:, None])
        weights, means, covariances = _m_step(z, resp, reg, covariance_type, fallback)
    mixture = {"weights": [float(w) for w in weights],
               "means": means.tolist(), "covariances": covariances.tolist()}
    mixture["log_likelihood"] = float(_class_log_likelihood(z, mixture, covariance_type).mean())
    fit = {"components_requested": int(k), "components": int(k_eff), "iterations": int(iterations),
           "converged": converged, "n": int(n)}
    return mixture, fit


# ---------------------------------------------------------------- training

def train_model(lots, config):
    params = _mixture_params(config)
    if params is None:
        return v3.train_model(lots, config)
    split_source, (identities, x, y) = _training_examples(lots, config)
    if y.all() or not y.any():
        raise ValueError("gaussian_mixture training candidates need both DOI and non-DOI labels")
    mean, scale = v2._train_scaler(x)
    z = (np.where(np.isfinite(x), x, mean) - mean) / scale
    positive, fit_pos = _fit_class_mixture(z[y], params["components_positive"], params, 1)
    negative, fit_neg = _fit_class_mixture(z[~y], params["components_negative"], params, 0)
    n_pos = int(y.sum())
    return {
        "family": FAMILY,
        "feature_names": list(FEATURES),
        "mean": [float(v) for v in mean],
        "scale": [float(v) for v in scale],
        "covariance_type": params["covariance_type"],
        "mixtures": {"positive": positive, "negative": negative},
        "class_priors": {"positive": n_pos / len(y), "negative": (len(y) - n_pos) / len(y)},
        "train_support": {"z_min": [float(v) for v in z.min(axis=0)],
                          "z_max": [float(v) for v in z.max(axis=0)]},
        "train_ids": [lot_id for lot_id, _, _ in identities],
        "n_train_candidates": int(len(y)),
        "n_train_positive": n_pos,
        "fit": {**{k: params[k] for k in DEFAULT_V4_MODEL}, "positive": fit_pos, "negative": fit_neg,
                "initialization": "kmeans++ default_rng([random_seed, class]) then hard assignment",
                "covariance_floor": "additive reg_covar"},
        "supports_online_update": False,
        "calibration": None,
        "provenance": {"module": "inspection_v4.model", "label": LABEL_DEFINITION,
                       "train_pairs": [[seed, scenario] for _, seed, scenario in identities],
                       "split_source": split_source, "fit_scope": "original_candidates_only",
                       "imputation": "train_candidate_mean", "extrapolation": EXTRAPOLATION,
                       "probability": "raw_base", "numpy_version": np.__version__},
    }


# ---------------------------------------------------------------- prediction

def predict_raw_logit(model, features):
    """Unclipped mixture posterior log-odds of DOI."""
    if not _is_mixture(model):
        raise ValueError("predict_raw_logit requires a gaussian_mixture model")
    z = v2._standardize(model, features)
    if len(z) == 0:
        return np.zeros(0)
    cov_type, priors = model["covariance_type"], model["class_priors"]
    lp = _class_log_likelihood(z, model["mixtures"]["positive"], cov_type)
    ln = _class_log_likelihood(z, model["mixtures"]["negative"], cov_type)
    return (np.log(priors["positive"]) - np.log(priors["negative"])) + lp - ln


def predict_raw(model, features):
    """Uncalibrated base DOI probability (v2/v3 delegation for non-mixture models)."""
    if not _is_mixture(model):
        return v3.predict_raw(model, features)
    return v3._sigmoid(predict_raw_logit(model, features))


def _clipped_logit(model, features):
    return np.clip(predict_raw_logit(model, features), -LOGIT_CLIP, LOGIT_CLIP)


def predict_calibrated_logit(model, features):
    calibration = model.get("calibration")
    if not _is_mixture(model):
        return v3.predict_calibrated_logit(model, features)
    if calibration is None or calibration["method"] not in PARAMETRIC_METHODS:
        raise ValueError("model has no parametric calibration")
    return calibration["slope"] * _clipped_logit(model, features) + calibration["intercept"]


def predict(model, features):
    """DOI probability under the model's frozen definition (calibrated if calibrated)."""
    if not _is_mixture(model):
        return v3.predict(model, features)
    calibration = model.get("calibration")
    if calibration is None or calibration["method"] == "identity":
        return predict_raw(model, features)
    if calibration["method"] == "isotonic":
        return np.interp(predict_raw(model, features), calibration["x"], calibration["y"])
    return v3._sigmoid(predict_calibrated_logit(model, features))


def support_diagnostic(model, features):
    """Per-row flags: imputed (non-finite input) and outside the train z-range."""
    x = np.asarray(features, dtype=float)
    if x.ndim == 1:
        x = x[None, :]
    z = v2._standardize(model, x)
    imputed = ~np.isfinite(x).all(axis=1)
    lo, hi = np.asarray(model["train_support"]["z_min"]), np.asarray(model["train_support"]["z_max"])
    outside = ((z < lo) | (z > hi)).any(axis=1)
    return imputed, outside


# ---------------------------------------------------------------- calibration

def fit_calibration(model, lots, config, method="platt"):
    """Return a new calibrated model; ``model`` is never modified."""
    if not _is_mixture(model):
        return v3.fit_calibration(model, lots, config, method=method)
    if method not in CALIBRATION_METHODS:
        raise ValueError(f"calibration method must be one of {CALIBRATION_METHODS}")
    if model.get("calibration") is not None:
        raise ValueError("model is already calibrated; calibrate the raw base model")
    identities, x, y = _calibration_examples(model, lots, config)
    probability = f"calibrated_{method}"
    calibration = {"method": method, "module": "inspection_v4.model",
                   "calibration_ids": [i[0] for i in identities],
                   "calibration_pairs": [[seed, scenario] for _, seed, scenario in identities],
                   "n_candidates": int(len(y)), "n_positive": int(y.sum()),
                   "base_model_hash": hash_model(model),
                   "base_supports_online_update": bool(model.get("supports_online_update")),
                   "base_probability": model["provenance"]["probability"],
                   "fit_scope": "candidates_only",
                   "probability_semantics": PROBABILITY_SEMANTICS[probability]}
    if method == "isotonic":
        thresholds, fitted = v2._isotonic(predict_raw(model, x), y)
        calibration["x"] = [float(v) for v in thresholds]
        calibration["y"] = [float(v) for v in fitted]
    elif method in PARAMETRIC_METHODS:
        s = _clipped_logit(model, x)
        slope, intercept, optimizer = v3._fit_affine_logit(s, y, fit_intercept=(method == "platt"))
        calibration.update({"slope": slope, "intercept": intercept, "logit_clip": LOGIT_CLIP,
                            "slope_bounds": list(v3.SLOPE_BOUNDS),
                            "intercept_bounds": list(v3.INTERCEPT_BOUNDS), "optimizer": optimizer})
        if method == "temperature":
            calibration["temperature"] = 1.0 / slope
    new = copy.deepcopy(model)
    new["calibration"] = calibration
    new["supports_online_update"] = False
    new["provenance"] = {**new["provenance"], "probability": probability}
    return new


def base_model(model):
    """Reconstruct the raw base model of a calibrated model and verify its hash."""
    if not _is_mixture(model):
        return v3.base_model(model)
    calibration = model.get("calibration")
    if calibration is None:
        return copy.deepcopy(model)
    base = copy.deepcopy(model)
    base["calibration"] = None
    base["supports_online_update"] = calibration["base_supports_online_update"]
    base["provenance"] = {**base["provenance"], "probability": calibration["base_probability"]}
    if hash_model(base) != calibration["base_model_hash"]:
        raise ValueError("base model hash mismatch; calibrated model was altered")
    return base


# ---------------------------------------------------------------- online update

def update_model(model, feature_row, label, config):
    if _is_mixture(model):
        raise ValueError("gaussian_mixture models are frozen; online updates are rejected")
    return v3.update_model(model, feature_row, label, config)


# ---------------------------------------------------------------- evaluation

def evaluate_model(model, lots, config):
    """Candidate-only metrics (v3 schema); not an all-site or physical recall estimate."""
    if not _is_mixture(model):
        return v3.evaluate_model(model, lots, config)
    params = config.get("v4_model") or {}
    threshold = float(params.get("classification_threshold",
                                 (config.get("model") or {}).get("classification_threshold", 0.5)))
    bins = int(params.get("ece_bins", 10))
    grid = [round(0.1 * i, 1) for i in range(1, 11)]
    fit_ids = set(model["train_ids"]) | set((model.get("calibration") or {}).get("calibration_ids", []))
    ys, ps, per_lot = [], [], []
    n_imputed = n_outside = 0
    for lot in lots:
        mask = np.asarray(lot["public"]["candidate"], dtype=bool)
        y = np.asarray(lot["oracle"]["doi"], dtype=bool)[mask]
        x = np.asarray(lot["public"]["features"], dtype=float)[mask]
        p = predict(model, x) if mask.any() else np.zeros(0)
        if mask.any():
            imputed, outside = support_diagnostic(model, x)
            n_imputed += int(imputed.sum())
            n_outside += int(outside.sum())
        ys.append(y)
        ps.append(p)
        per_lot.append({"lot_id": lot["public"]["lot_id"], "scenario": lot["public"].get("scenario"),
                        **v2._metrics(y, p, threshold, bins, grid)})
    y = np.concatenate(ys) if ys else np.zeros(0, dtype=bool)
    p = np.concatenate(ps) if ps else np.zeros(0)
    aggregate = v2._metrics(y, p, threshold, bins, grid)
    aggregate["log_loss"] = None if len(y) == 0 else v3._nll(v3._logit(p), y.astype(float))
    lot_ids = [entry["lot_id"] for entry in per_lot]
    probability = model["provenance"]["probability"]
    return {"model_hash": hash_model(model), "family": model["family"],
            "probability": probability,
            "probability_semantics": PROBABILITY_SEMANTICS.get(probability, probability),
            "threshold": threshold,
            "scope": {"unit": "optical_candidate_sites", "lot_ids": lot_ids,
                      "fit_overlap_lot_ids": sorted(set(lot_ids) & fit_ids),
                      "excludes": "non-candidate sites; not all-site physical recall",
                      "n_imputed_candidates": n_imputed,
                      "n_outside_train_support": n_outside,
                      "extrapolation": EXTRAPOLATION},
            "aggregate": aggregate, "per_lot": per_lot}
