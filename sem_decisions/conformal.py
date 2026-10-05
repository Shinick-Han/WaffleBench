"""Split-conformal prediction sets and abstention for binary defect decisions.

Calibration uses an INDEPENDENT labeled calibration split: its ids must be disjoint from
the model-fit and test ids and its duplicate groups must not appear in either
(``check_independent``). Nonconformity score = 1 - p(true class). With n calibration
scores and miscoverage alpha, qhat is the ceil((n + 1)(1 - alpha))-th smallest score
(infinite when that rank exceeds n, which makes every set {0, 1}).

Decision: {1} -> positive, {0} -> negative, {0, 1} or {} -> abstain and request review. An
uncertain decision therefore never silently becomes "good".

Guarantee: marginal coverage >= 1 - alpha holds only IF calibration and test examples are
exchangeable. Exchangeability is an assumption that this module never certifies: a shift
diagnostic that does not alert does not establish it. ``coverage_statement`` always
reports the empirical guarantee as unverified and gives only a CONDITIONAL theoretical
statement, valid only when the caller explicitly sets ``exchangeability_assumed=True``,
the independence evidence matches the model, and a shift diagnostic ran without alerting.
Coverage of prediction sets is not a guarantee of wafer defect recall.

Inputs are validated, never silently repaired: probabilities must be finite and in [0, 1],
labels observed 0/1 and aligned with the probabilities, and the independence evidence's
``n_calibration`` must equal the number of calibration scores.

``mapie_split_conformal`` is an OPTIONAL wrapper over the pinned MAPIE commit; MAPIE is not
a dependency and the wrapper was not executed in this repository's checks. It provides no
guarantee under drift either.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .pins import pin

EXCHANGEABILITY_CAVEAT = ("valid only if calibration and test data are exchangeable; drift, selection of "
                          "reviewed sites or correlated tiles void the guarantee; not a recall guarantee")


class CalibrationError(ValueError):
    pass


def _probs(p: Any, name: str = "p_positive") -> np.ndarray:
    a = np.asarray(p)
    if a.dtype == bool or not np.issubdtype(a.dtype, np.number):
        raise CalibrationError(f"{name} must be numeric probabilities")
    a = a.astype(float)
    if a.ndim != 1 or a.size == 0:
        raise CalibrationError(f"{name} must be a non-empty 1-D array")
    if not np.all(np.isfinite(a)) or np.any((a < 0) | (a > 1)):
        raise CalibrationError(f"{name} must be finite and within [0, 1]")
    return a


def _labels(y: Any, n: int) -> np.ndarray:
    a = np.asarray(y)
    if a.ndim != 1 or a.size != n:
        raise CalibrationError("labels must be a 1-D array aligned with the probabilities")
    if a.dtype != bool and not np.issubdtype(a.dtype, np.number):
        raise CalibrationError("labels must be observed 0/1")
    a = a.astype(float)
    if not np.all(np.isin(a, (0.0, 1.0))):
        raise CalibrationError("labels must be observed 0/1; unknown or invalid items are excluded upstream")
    return a.astype(int)


def check_independent(fit_ids: Iterable[str], calibration_ids: Iterable[str], test_ids: Iterable[str],
                      groups: Mapping[str, Any] | None = None) -> dict:
    fit, cal, test = set(fit_ids), set(calibration_ids), set(test_ids)
    if not cal:
        raise CalibrationError("calibration split is empty")
    errs = []
    if cal & fit:
        errs.append(f"{len(cal & fit)} calibration ids also used for fitting")
    if cal & test:
        errs.append(f"{len(cal & test)} calibration ids also in test")
    if groups is not None:
        missing = [i for i in cal | fit | test if i not in groups]
        if missing:
            errs.append(f"{len(missing)} ids lack a duplicate group")
        else:
            gc = {groups[i] for i in cal}
            if gc & {groups[i] for i in fit | test}:
                errs.append("calibration duplicate groups overlap fit/test")
    if errs:
        raise CalibrationError("; ".join(errs))
    return {"independent": True, "duplicate_groups_checked": groups is not None, "n_calibration": len(cal)}


@dataclass(frozen=True)
class SplitConformal:
    alpha: float
    qhat: float
    n_calibration: int

    def sets(self, p_positive: np.ndarray) -> list[tuple[int, ...]]:
        p1 = _probs(p_positive)
        out = []
        for p in p1:
            s = tuple(c for c, pc in ((0, 1.0 - p), (1, p)) if 1.0 - pc <= self.qhat + 1e-12)
            out.append(s)
        return out

    def decide(self, p_positive: np.ndarray) -> list[dict]:
        out = []
        for s in self.sets(p_positive):
            d = "positive" if s == (1,) else "negative" if s == (0,) else "abstain_request_review"
            out.append({"set": list(s), "decision": d})
        return out


def calibrate(p_positive: np.ndarray, labels: np.ndarray, alpha: float, *,
              independence: dict | None = None) -> SplitConformal:
    if independence is None or not independence.get("independent"):
        raise CalibrationError("pass check_independent(...) evidence for the calibration split")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) \
            or not 0 < alpha < 1:
        raise CalibrationError("alpha must be a finite number in (0, 1)")
    p = _probs(p_positive)
    y = _labels(labels, p.size)
    if independence.get("n_calibration") != p.size:
        raise CalibrationError(f"independence evidence covers {independence.get('n_calibration')} calibration ids "
                               f"but {p.size} calibration scores were given")
    scores = np.sort(np.where(y == 1, 1.0 - p, p))
    n = scores.size
    rank = math.ceil((n + 1) * (1 - alpha))
    qhat = math.inf if rank > n else float(scores[rank - 1])
    return SplitConformal(float(alpha), qhat, int(n))


def empirical_coverage(model: SplitConformal, p_positive: np.ndarray, labels: np.ndarray) -> float:
    """Observed set-contains-label fraction on these items (a measurement, not a guarantee)."""
    p = _probs(p_positive)
    y = _labels(labels, p.size)
    return float(np.mean([int(t) in s for s, t in zip(model.sets(p), y)]))


def coverage_statement(model: SplitConformal, shift_report: dict | None, independence: dict | None, *,
                       exchangeability_assumed: bool = False) -> dict:
    """Empirical coverage is always unverified. The conditional theoretical statement
    (coverage >= 1 - alpha IF exchangeable) is valid only with an explicit assumption,
    matching independence evidence and a shift diagnostic that ran without alerting."""
    reasons = []
    if exchangeability_assumed is not True:
        reasons.append("exchangeability not explicitly assumed; a non-alerting diagnostic does not establish it")
    if independence is None or not independence.get("independent"):
        reasons.append("calibration independence not verified")
    elif independence.get("n_calibration") != model.n_calibration:
        reasons.append("independence evidence does not match the calibrated model")
    if shift_report is None:
        reasons.append("no shift diagnostic was run on the deployment data")
    elif shift_report.get("shift_detected") is not False:
        reasons.append("shift diagnostic alerted or was inconclusive: exchangeability assumption rejected")
    return {"target_marginal_coverage": 1.0 - model.alpha, "empirical_guarantee": "unverified",
            "empirical_coverage_certified": False, "exchangeability_assumed": exchangeability_assumed is True,
            "conditional_statement_valid": not reasons,
            "conditional_statement": f"marginal coverage >= {1.0 - model.alpha:.4g} IF calibration and test data "
                                     "are exchangeable (assumed, not verified)",
            "invalid_reasons": reasons, "caveat": EXCHANGEABILITY_CAVEAT}


def mapie_split_conformal(estimator: Any, x_cal: np.ndarray, y_cal: np.ndarray, x_test: np.ndarray,
                          alpha: float) -> dict:  # pragma: no cover - optional dependency
    """Optional MAPIE ``SplitConformalClassifier`` (MAPIE >= 1.0 API, prefit estimator)."""
    try:
        cls = importlib.import_module("mapie.classification").SplitConformalClassifier
    except (ImportError, AttributeError) as exc:
        raise ImportError(f"MAPIE SplitConformalClassifier unavailable; target pin {pin('MAPIE')['url']}") from exc
    m = cls(estimator=estimator, confidence_level=1 - alpha, prefit=True).conformalize(x_cal, y_cal)
    _, sets = m.predict_set(x_test)
    return {"backend": "mapie.classification.SplitConformalClassifier", "pin": pin("MAPIE"),
            "upstream_executed": True,
            "sets": np.asarray(sets).tolist(), "guarantee_under_drift": False, "caveat": EXCHANGEABILITY_CAVEAT}
