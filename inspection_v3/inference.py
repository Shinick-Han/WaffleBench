"""Compiled, immutable inspection v3 DOI predictors for repeated warm prediction.

Synthetic development study only. ``compile_predictor(model)`` deep-copies a v2/v3 model
dict once, hashes it once (``inspection_v2.model.hash_model``), loads a CatBoost booster
once through ``inspection_v2.model._booster`` and keeps a private copy of the native
classifier, and freezes every numeric parameter as a read-only float64 array.

Snapshot semantics: the compiled predictor depends only on the model as it was at compile
time. Mutating the source dict afterwards changes neither its outputs nor ``model_hash``.
Warm ``predict``/``predict_raw`` calls perform no JSON serialization, hashing, file I/O or
model mutation; they take only public feature rows (no oracle or other hidden inputs).

Numerics mirror the reference implementations:

* ``predict_raw``: logistic follows ``inspection_review.model.predict`` (train-mean
  imputation, standardization, clipped sigmoid); CatBoost follows
  ``inspection_v2.model.predict_raw`` (same imputation/standardization, native
  ``predict_proba`` class-1 column).
* ``predict``: identity/no calibration returns the raw probability; isotonic applies the
  same ``np.interp`` as ``inspection_v2.model``; platt/temperature apply
  ``inspection_v3.model``'s ``sigmoid(slope * logit(raw) + intercept)``.
"""
from __future__ import annotations

import copy

import numpy as np

from inspection_review import model as v1_model
from inspection_v2 import model as v2
from inspection_v3 import model as v3

FAMILIES = ("logistic", "catboost")
CALIBRATIONS = (None, "identity", "isotonic", "platt", "temperature")


def _frozen(values):
    arr = np.array(values, dtype=float)
    arr.flags.writeable = False
    return arr


class CompiledPredictor:
    """Immutable prediction snapshot of one model; build with ``compile_predictor``."""

    __slots__ = ("_family", "_method", "_model_hash", "_n_features", "_mean", "_scale",
                 "_coef", "_intercept", "_clf", "_iso_x", "_iso_y", "_slope", "_cal_intercept")

    def __init__(self, model):
        snapshot = copy.deepcopy(model)
        family = snapshot.get("family")
        if family not in FAMILIES:
            raise ValueError(f"unknown model family {family!r}")
        calibration = snapshot.get("calibration")
        method = None if calibration is None else calibration.get("method")
        if method not in CALIBRATIONS:
            raise ValueError(f"unsupported calibration method {method!r}")
        put = lambda name, value: object.__setattr__(self, name, value)  # noqa: E731
        put("_family", family)
        put("_method", method)
        put("_model_hash", v2.hash_model(snapshot))
        put("_n_features", len(snapshot["feature_names"]))
        put("_mean", _frozen(snapshot["mean"]))
        put("_scale", _frozen(snapshot["scale"]))
        if self._mean.shape != (self._n_features,) or self._scale.shape != (self._n_features,):
            raise ValueError("model mean/scale do not match feature_names")
        put("_coef", None)
        put("_intercept", None)
        put("_clf", None)
        if family == "logistic":
            put("_coef", _frozen(snapshot["coef"]))
            put("_intercept", float(snapshot["intercept"]))
        else:
            # One native load via the v2 path, then a private copy so no shared cache
            # entry can alias this predictor's classifier.
            put("_clf", v2._booster(snapshot).copy())
        put("_iso_x", None)
        put("_iso_y", None)
        put("_slope", None)
        put("_cal_intercept", None)
        if method == "isotonic":
            put("_iso_x", _frozen(calibration["x"]))
            put("_iso_y", _frozen(calibration["y"]))
        elif method in v3.PARAMETRIC_METHODS:
            put("_slope", calibration["slope"])
            put("_cal_intercept", calibration["intercept"])

    def __setattr__(self, name, value):
        raise AttributeError("CompiledPredictor is immutable")

    def __delattr__(self, name):
        raise AttributeError("CompiledPredictor is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("CompiledPredictor is not serializable; compile from the model dict")

    def __repr__(self):
        return (f"CompiledPredictor(family={self._family!r}, calibration={self._method!r}, "
                f"model_hash={self._model_hash[:12]!r})")

    @property
    def model_hash(self):
        """``hash_model`` of the source model at compile time."""
        return self._model_hash

    @property
    def family(self):
        return self._family

    @property
    def calibration_method(self):
        return self._method

    def _standardize(self, features):
        x = np.asarray(features, dtype=float)
        if x.ndim == 1:
            x = x[None, :]
        if x.ndim != 2 or x.shape[1] != self._n_features:
            raise ValueError(f"features must have shape (n, {self._n_features})")
        return (np.where(np.isfinite(x), x, self._mean) - self._mean) / self._scale

    def predict_raw(self, features):
        """Uncalibrated base-model DOI probability, shape ``(n,)``."""
        z = self._standardize(features)
        if self._family == "logistic":
            return v1_model._sigmoid(z @ self._coef + self._intercept)
        if len(z) == 0:
            return np.zeros(0)
        return self._clf.predict_proba(z)[:, 1].astype(float)

    def predict(self, features):
        """DOI probability under the model's frozen definition (calibrated if calibrated)."""
        p = self.predict_raw(features)
        if self._method is None or self._method == "identity":
            return p
        if self._method == "isotonic":
            return np.interp(p, self._iso_x, self._iso_y)
        return v3._sigmoid(self._slope * v3._logit(p) + self._cal_intercept)


def compile_predictor(model):
    """Return an immutable ``CompiledPredictor`` snapshot of ``model`` (never modified)."""
    return CompiledPredictor(model)
