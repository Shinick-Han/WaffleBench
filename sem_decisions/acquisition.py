"""Acquisition-quality assessment and repeat-image action protocol.

No hardware is connected: every output carries ``hardware_connected: False`` and the
``capture`` callable is supplied by the caller (a fixture, an offline replay or a future
instrument adapter).

Inputs are MEASURED image quality features and quality flags only. Truth-mask metrics
(IoU, Dice, mask area, ground-truth labels, ...) are rejected because they are not
available before review.

Quality status per capture:
  valid    every required feature present, finite, within thresholds, and no flag raised
  invalid  a feature outside its threshold or a raised artefact flag
  unknown  a required feature missing or non-finite (cannot be judged)

Protocol: capture once, then repeat while the latest capture is not valid, the retry cap
is not reached, and the explicit repeat cost and time still fit the remaining budget and
time. Outcomes are kept distinct:
  observed_positive / observed_negative  only from a valid capture with a detector result
  invalid_acquisition                    the last capture was invalid
  unknown                                the last capture was unknown or detector gave None
An invalid or unknown acquisition is never converted into a physical negative.

Threshold defaults are DEVELOPMENT DEFAULTS, NOT INSTRUMENT-CALIBRATED; every output says so
in ``thresholds_status``. Cost, time, budget and retry parameters must be finite real
numbers (booleans, NaN and infinity are rejected).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

REQUIRED_FEATURES = ("snr", "contrast", "sharpness", "saturation_fraction")
QUALITY_FLAGS = ("charging_artifact", "drift_blur", "out_of_focus", "truncated_frame")
FORBIDDEN_PARTS = ("mask", "iou", "dice", "truth", "ground", "label", "oracle", "defect_class")
OUTCOMES = ("observed_positive", "observed_negative", "invalid_acquisition", "unknown")
THRESHOLDS_STATUS = "development_default_not_instrument_calibrated"


def _finite(name: str, v, *, integer: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} must be a finite non-negative number (got {v!r})")
    if integer and int(v) != v:
        raise ValueError(f"{name} must be an integer (got {v!r})")
    return float(v)


@dataclass(frozen=True)
class QualityThresholds:
    min_snr: float = 3.0
    min_contrast: float = 0.05
    min_sharpness: float = 0.1
    max_saturation_fraction: float = 0.02
    status: str = THRESHOLDS_STATUS

    def __post_init__(self):
        for k in ("min_snr", "min_contrast", "min_sharpness", "max_saturation_fraction"):
            _finite(k, getattr(self, k))


@dataclass(frozen=True)
class RepeatCosts:
    capture_cost: float
    repeat_cost: float
    capture_time_s: float
    repeat_time_s: float
    retry_cap: int

    def __post_init__(self):
        for k in ("capture_cost", "repeat_cost", "capture_time_s", "repeat_time_s"):
            _finite(k, getattr(self, k))
        _finite("retry_cap", self.retry_cap, integer=True)

    @property
    def max_cost(self) -> float:
        return self.capture_cost + self.retry_cap * self.repeat_cost

    @property
    def max_time_s(self) -> float:
        return self.capture_time_s + self.retry_cap * self.repeat_time_s


def _reject_truth(measured: dict) -> None:
    bad = sorted(k for k in measured if any(p in str(k).lower() for p in FORBIDDEN_PARTS))
    if bad:
        raise ValueError(f"truth/mask-derived inputs are not allowed in prospective quality decisions: {bad}")


def assess_quality(measured: dict, thr: QualityThresholds = QualityThresholds()) -> dict:
    _reject_truth(measured)
    reasons, missing = [], []
    vals = {}
    for k in REQUIRED_FEATURES:
        v = measured.get(k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = math.nan
        if not math.isfinite(v):
            missing.append(k)
        vals[k] = v
    flags = [f for f in QUALITY_FLAGS if measured.get(f) is True]
    unknown_flags = [f for f in QUALITY_FLAGS if measured.get(f) not in (True, False, None)]
    if unknown_flags:
        raise ValueError(f"quality flags must be booleans or null: {unknown_flags}")
    if not missing:
        if vals["snr"] < thr.min_snr:
            reasons.append("snr_below_min")
        if vals["contrast"] < thr.min_contrast:
            reasons.append("contrast_below_min")
        if vals["sharpness"] < thr.min_sharpness:
            reasons.append("sharpness_below_min")
        if vals["saturation_fraction"] > thr.max_saturation_fraction:
            reasons.append("saturation_above_max")
    reasons += [f"flag:{f}" for f in flags]
    if reasons:
        status = "invalid"
    elif missing:
        status = "unknown"
        reasons = [f"missing:{k}" for k in missing]
    else:
        status = "valid"
    return {"status": status, "reasons": reasons, "thresholds_status": thr.status}


def classify_outcome(quality_status: str, detector_positive: bool | None) -> str:
    if quality_status == "invalid":
        return "invalid_acquisition"
    if quality_status != "valid" or detector_positive is None:
        return "unknown"
    return "observed_positive" if detector_positive else "observed_negative"


@dataclass
class ProtocolResult:
    outcome: str
    attempts: list = field(default_factory=list)
    cost: float = 0.0
    time_s: float = 0.0
    stop_reason: str = ""
    hardware_connected: bool = False
    thresholds_status: str = THRESHOLDS_STATUS

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "attempts": self.attempts, "cost": self.cost, "time_s": self.time_s,
                "stop_reason": self.stop_reason, "hardware_connected": False,
                "thresholds_status": self.thresholds_status}


def run_repeat_protocol(capture: Callable[[int], dict], detector: Callable[[dict], bool | None],
                        costs: RepeatCosts, *, remaining_budget: float, remaining_time_s: float,
                        thr: QualityThresholds = QualityThresholds()) -> ProtocolResult:
    """``capture(attempt)`` returns measured features/flags; ``detector(measured)`` returns
    True/False/None on a valid capture only. Spend never exceeds budget/time."""
    remaining_budget = _finite("remaining_budget", remaining_budget)
    remaining_time_s = _finite("remaining_time_s", remaining_time_s)
    if costs.capture_cost > remaining_budget + 1e-9 or costs.capture_time_s > remaining_time_s + 1e-9:
        return ProtocolResult("unknown", [], 0.0, 0.0, "first_capture_unaffordable", thresholds_status=thr.status)
    res = ProtocolResult("unknown", thresholds_status=thr.status)
    attempt = 0
    while True:
        step_cost = costs.capture_cost if attempt == 0 else costs.repeat_cost
        step_time = costs.capture_time_s if attempt == 0 else costs.repeat_time_s
        measured = capture(attempt)
        if not isinstance(measured, dict):
            raise ValueError("capture must return a dict of measured features/flags")
        measured = dict(measured)
        q = assess_quality(measured, thr)
        res.cost += step_cost
        res.time_s += step_time
        res.attempts.append({"attempt": attempt, "action": "capture" if attempt == 0 else "repeat",
                             "cost": step_cost, "time_s": step_time, "quality": q})
        if q["status"] == "valid":
            det = detector(measured)
            if det not in (True, False, None):
                raise ValueError("detector must return True, False or None")
            res.outcome = classify_outcome("valid", det)
            res.stop_reason = "valid_capture"
            break
        res.outcome = classify_outcome(q["status"], None)
        if attempt >= costs.retry_cap:
            res.stop_reason = "retry_cap_reached"
            break
        if (res.cost + costs.repeat_cost > remaining_budget + 1e-9
                or res.time_s + costs.repeat_time_s > remaining_time_s + 1e-9):
            res.stop_reason = "repeat_unaffordable"
            break
        attempt += 1
    return res
