"""Optional modAL / apricot entry points and a self-named NumPy fallback.

Neither modAL nor apricot is a project dependency. The optional entry points import the
upstream package lazily and raise ``BackendUnavailable`` when it is missing; they are
targeted at the pinned commits in ``pins.py`` and have NOT been executed in this
repository's checks. Every result carries ``backend`` so a NumPy fallback can never be
reported as upstream library execution.

The fallback implements two deterministic routines:
  * uncertainty ranking by 4p(1-p) (ties: lowest index),
  * greedy facility-location subset selection on Euclidean similarity
    ``s = max_dist - dist`` (ties: lowest index). It is O(n^2) memory; callers should keep
    candidate sets small (a full similarity matrix becomes excessive for large n).
These are budget-agnostic utilities; cost, wafer movement and retry reserves are applied by
``policy.SemBudgetAuditPolicy``.
"""

from __future__ import annotations

import importlib
from typing import Any

import numpy as np

from .pins import pin

FALLBACK = "numpy_deterministic_fallback (not modAL/apricot)"


class BackendUnavailable(ImportError):
    pass


def _import(module: str, label: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BackendUnavailable(f"{label} is not installed; target pin {pin(label)['url']}") from exc


def available_backends() -> dict[str, dict]:
    """Configured (pinned entry point exists) vs installed vs executed, kept distinct.
    ``executed`` is always False here: this function only probes imports."""
    out = {}
    for module, label in (("modAL", "modAL"), ("apricot", "apricot"), ("mapie", "MAPIE")):
        try:
            importlib.import_module(module)
            installed = True
        except ImportError:
            installed = False
        out[label] = {"configured": True, "pin": pin(label)["sha"], "installed": installed, "executed": False}
    return out


def uncertainty_rank(p: np.ndarray, n: int) -> dict:
    p = np.clip(np.asarray(p, float), 0.0, 1.0)
    u = 4.0 * p * (1.0 - p)
    idx = np.arange(p.size)
    order = idx[np.lexsort((idx, -u))][:n]
    return {"backend": FALLBACK, "upstream_executed": False, "indices": order.tolist(),
            "scores": u[order].tolist()}


def facility_location(x: np.ndarray, n: int) -> dict:
    x = np.asarray(x, float)
    m = x.shape[0]
    n = min(int(n), m)
    if m == 0 or n <= 0:
        return {"backend": FALLBACK, "upstream_executed": False, "indices": [], "gains": []}
    d = np.linalg.norm(x[:, None, :] - x[None, :, :], axis=2)
    s = d.max() - d
    best = np.zeros(m)
    chosen: list[int] = []
    gains: list[float] = []
    avail = np.ones(m, bool)
    for _ in range(n):
        gain = np.maximum(s, best[:, None]).sum(axis=0) - best.sum()
        gain = np.where(avail, gain, -np.inf)
        j = int(np.flatnonzero(gain == gain.max())[0])
        chosen.append(j)
        gains.append(float(gain[j]))
        avail[j] = False
        best = np.maximum(best, s[:, j])
    return {"backend": FALLBACK, "upstream_executed": False, "indices": chosen, "gains": gains}


def modal_uncertainty_query(estimator: Any, x: np.ndarray, n: int) -> dict:
    """modAL ``uncertainty_sampling`` on a fitted sklearn-style classifier (optional)."""
    unc = _import("modAL.uncertainty", "modAL")
    idx, _ = unc.uncertainty_sampling(estimator, x, n_instances=int(n))
    return {"backend": "modAL.uncertainty.uncertainty_sampling", "pin": pin("modAL"),
            "upstream_executed": True, "indices": [int(i) for i in np.atleast_1d(idx)]}


def apricot_facility_location(x: np.ndarray, n: int) -> dict:
    """apricot ``FacilityLocationSelection`` ranking (optional)."""
    ap = _import("apricot", "apricot")
    sel = ap.FacilityLocationSelection(int(n), metric="euclidean").fit(np.asarray(x, float))
    return {"backend": "apricot.FacilityLocationSelection", "pin": pin("apricot"),
            "upstream_executed": True, "indices": [int(i) for i in sel.ranking]}
