"""Frozen five-coefficient PVT delay model (research path).

    log(tpd / 1 ps) = b0 + bv*g(VDD) + bt*((T-27)/100) + bn*n + bp*p
    g(V) = log[(V/(V-0.55)^1.3) / (2.5/(2.5-0.55)^1.3)]

n and p are the NMOS/PMOS threshold shifts of the synthetic condition divided by
50 mV. Coefficients are an unregularized ordinary least squares fit on the nine
prescribed calibration observations only. The design matrix must have column
rank 5 before fitting. After freezing, the model is never refit; its hash binds
the coefficients, the calibration evidence and the protocol/manifest hashes.

This replaces the connection-test voltage-only surrogate in experiment.py, which
is never used by the research path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .protocol import Point, canonical_json, sha256_bytes

FORM = "log(tpd/1ps) = b0 + bv*g(VDD) + bt*((T-27)/100) + bn*n + bp*p"
G_FORM = "g(V) = log[(V/(V-0.55)^1.3) / (2.5/(2.5-0.55)^1.3)]"
COEFFICIENT_NAMES = ("b0", "bv", "bt", "bn", "bp")
FEATURES = (
    "intercept",
    "log((V/(V-0.55)^1.3)/(2.5/(2.5-0.55)^1.3))",
    "(T-27)/100",
    "NMOS_dVt/0.05",
    "PMOS_dVt/0.05",
)
VT_EFF = 0.55
ALPHA = 1.3
MODEL_SCHEMA = "falsify-model-v1"


class ModelError(RuntimeError):
    pass


def g(vdd: float) -> float:
    return math.log((vdd / (vdd - VT_EFF) ** ALPHA) / (2.5 / (2.5 - VT_EFF) ** ALPHA))


def features(point: Point) -> list[float]:
    return [1.0, g(point.vdd), (point.temp_c - 27.0) / 100.0, point.dvtn / 0.05, point.dvtp / 0.05]


def design_matrix(points: list[Point]) -> np.ndarray:
    return np.array([features(p) for p in points], dtype=float)


def design_rank(points: list[Point]) -> int:
    return int(np.linalg.matrix_rank(design_matrix(points)))


@dataclass(frozen=True)
class FrozenModel:
    coefficients: tuple[float, ...]
    calibration: tuple[dict[str, Any], ...]  # [{point_id, result_id, sim_id, tpd_s}]
    protocol_sha256: str
    manifest_sha256: str
    rank: int
    model_hash: str

    def predict_tpd_s(self, point: Point) -> float:
        z = sum(c * f for c, f in zip(self.coefficients, features(point)))
        tpd = 1e-12 * math.exp(z)
        if not (math.isfinite(tpd) and tpd > 0):
            raise ModelError(f"non-positive or non-finite prediction at {point.id}")
        return tpd

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MODEL_SCHEMA,
            "form": FORM,
            "g": G_FORM,
            "features": list(FEATURES),
            "coefficient_names": list(COEFFICIENT_NAMES),
            "coefficients": list(self.coefficients),
            "coefficients_hex": [float(c).hex() for c in self.coefficients],
            "calibration": list(self.calibration),
            "protocol_sha256": self.protocol_sha256,
            "manifest_sha256": self.manifest_sha256,
            "design_rank": self.rank,
            "fit": "unregularized ordinary least squares in log space, nine calibration points, never refit",
            "model_hash": self.model_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FrozenModel:
        coeffs = tuple(float.fromhex(h) for h in d["coefficients_hex"])
        model = cls(coeffs, tuple(d["calibration"]), d["protocol_sha256"], d["manifest_sha256"], int(d["design_rank"]), d["model_hash"])
        if _model_hash(model) != d["model_hash"]:
            raise ModelError("stored model hash does not match its content")
        return model


def _model_hash(model: FrozenModel) -> str:
    body = {
        "schema": MODEL_SCHEMA,
        "form": FORM,
        "g": G_FORM,
        "features": list(FEATURES),
        "coefficients_hex": [float(c).hex() for c in model.coefficients],
        "calibration": list(model.calibration),
        "protocol_sha256": model.protocol_sha256,
        "manifest_sha256": model.manifest_sha256,
        "design_rank": model.rank,
    }
    return "model_" + sha256_bytes(canonical_json(body).encode("utf-8"))


def fit(
    training_points: list[Point],
    observations: list[dict[str, Any]],
    protocol_sha256: str,
    manifest_sha256: str,
) -> FrozenModel:
    """Fit and freeze. ``observations`` align with ``training_points`` and carry point_id/result_id/sim_id/tpd_s."""
    if len(training_points) != 9 or len(observations) != 9:
        raise ModelError("the frozen model needs exactly the nine prescribed calibration observations")
    for p, o in zip(training_points, observations):
        if o["point_id"] != p.id:
            raise ModelError(f"calibration observation {o['point_id']} does not match {p.id}")
        if not (o["tpd_s"] > 0 and math.isfinite(o["tpd_s"])):
            raise ModelError(f"non-positive calibration delay at {p.id}")
    rank = design_rank(training_points)
    if rank != 5:
        raise ModelError(f"design matrix column rank is {rank}, expected 5; refusing to fit")
    x = design_matrix(training_points)
    y = np.array([math.log(o["tpd_s"] / 1e-12) for o in observations])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    calibration = tuple(
        {"point_id": o["point_id"], "result_id": o["result_id"], "sim_id": o["sim_id"], "tpd_s": float(o["tpd_s"])}
        for o in observations
    )
    draft = FrozenModel(tuple(float(c) for c in coef), calibration, protocol_sha256, manifest_sha256, rank, "")
    model = FrozenModel(draft.coefficients, calibration, protocol_sha256, manifest_sha256, rank, _model_hash(draft))
    for p in training_points:
        model.predict_tpd_s(p)
    return model


def evaluate(model: FrozenModel, point: Point, simulated_tpd_s: float, clear: float, secondary: float) -> dict[str, Any]:
    """Compare the frozen prediction with one stored simulation. Thresholds are strict (>)."""
    predicted = model.predict_tpd_s(point)
    rel = (predicted - simulated_tpd_s) / simulated_tpd_s
    return {
        "model_hash": model.model_hash,
        "predicted_tpd_s": predicted,
        "simulated_tpd_s": simulated_tpd_s,
        "relative_error": rel,
        "abs_relative_error": abs(rel),
        "clear_counterexample": abs(rel) > clear,
        "secondary_counterexample": abs(rel) > secondary,
    }
