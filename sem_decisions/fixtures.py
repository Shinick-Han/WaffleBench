"""Small SYNTHETIC fixtures and a development simulation loop (never scientific evidence).

``simulate`` mirrors the inspection harness admission/charging rules: a site is admissible
only when ``spent + max_cost`` (retry reserve included) fits the budget; the first attempt
is charged, the retry is charged only after a failed first attempt; a failed review is
billed and yields no label. The synthetic outcome table lives only inside the loop and is
never passed to the policy.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from inspection_review.harness import cost_vectors
from inspection_review.policies import SelectionState, sanitize_public

from . import SYNTHETIC_LABEL

COST = {"wafer_load": 8.0, "stage_base": 1.0, "stage_per_normalized_distance": 2.0, "dwell": 8.0,
        "retry_dwell": 4.0, "outside_rescan": 12.0, "retry_limit": 1}
SELECTION = {"spatial_bandwidth": 0.22, "spatial_prior_strength": 4.0}


def view_of(wafer, xy, candidate=None, seed: int = 0, n_features: int = 7):
    n = len(wafer)
    rng = np.random.default_rng(seed)
    cand = np.ones(n, bool) if candidate is None else np.asarray(candidate, bool)
    feats = rng.normal(size=(n, n_features))
    public = {"lot_id": "synthetic-fixture", "site_ids": [f"s{i}" for i in range(n)], "wafer": np.asarray(wafer),
              "xy": np.asarray(xy, float), "layer": np.zeros(n, int), "candidate": cand, "features": feats}
    return sanitize_public(public, [0.0] * n_features, [1.0] * n_features)


def state_of(view, p, mode: str = "candidate_only", threshold: float = 0.5) -> SelectionState:
    p = np.asarray(p, float)
    return SelectionState(view, p, {"frozen": True}, p, mode, dict(SELECTION), threshold)


def random_lot(seed: int, n_per_wafer: int = 20, wafers: int = 3):
    rng = np.random.default_rng(seed)
    n = wafers * n_per_wafer
    wafer = np.repeat(np.arange(wafers), n_per_wafer)
    xy = rng.uniform(-1, 1, (n, 2))
    p = np.clip(rng.beta(1.2, 4.0, n), 0.001, 0.999)
    return view_of(wafer, xy, None, seed), p


def simulate(policy, view, p, budget: float, *, seed: int, fail_rate: float = 0.1, cost: dict | None = None) -> dict:
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget < 0:
        raise ValueError("budget must be a finite non-negative number")
    if isinstance(fail_rate, bool) or not 0.0 <= float(fail_rate) <= 1.0:
        raise ValueError("fail_rate must be in [0, 1]")
    cost = dict(COST if cost is None else cost)
    for k, v in cost.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"cost.{k} must be a finite non-negative number")
    if int(cost["retry_limit"]) != cost["retry_limit"]:
        raise ValueError("cost.retry_limit must be an integer")
    rng = np.random.default_rng(seed)
    truth = rng.random(view.n) < p  # synthetic outcome table, loop-private
    fails = rng.random((view.n, 2)) < fail_rate
    state = state_of(view, p)
    spent, rows = 0.0, []
    while state.allowed().any():
        cv = cost_vectors(state, cost)
        affordable = state.allowed() & (spent + cv["max"] <= budget + 1e-9)
        if not affordable.any():
            break
        state.remaining_budget = budget - spent
        choice = policy.select(state, cv["max"], affordable)
        i = choice.index
        if not affordable[i]:
            raise RuntimeError("policy chose an unaffordable site")
        charged = float(cv["first"][i])
        ok = not fails[i, 0]
        if not ok and int(cost["retry_limit"]) > 0:
            charged += float(cost["retry_dwell"])
            ok = not fails[i, 1]
        spent += charged
        if spent > budget + 1e-9:
            raise RuntimeError("budget invariant violated")
        state.record_visit(i)
        if ok:
            state.record_label(i, bool(truth[i]))
        rows.append({"site_id": view.site_ids[i], "reason": choice.reason, "charged": charged,
                     "status": "ok" if ok else "failed", "reported_doi": bool(truth[i]) if ok else None})
    return {"label": SYNTHETIC_LABEL, "budget": budget, "spent": spent, "decisions": len(rows),
            "audits": sum(r["reason"].startswith("audit") for r in rows),
            "synthetic_doi_found": sum(bool(r["reported_doi"]) for r in rows), "rows": rows}


def fixture_manifest(root: str = "C:/synthetic-fixture-root") -> dict:
    """Tiny manifest in the common schema, data_mode 'synthetic_fixture' (never 'real')."""
    items = []
    spec = [("a", "train", "g1"), ("b", "train", "g1"), ("c", "train", "g2"), ("d", "calibration", "g3"),
            ("e", "calibration", "g4"), ("f", "calibration", "g5"), ("g", "test", "g6"), ("h", "test", "g7")]
    for n, (iid, split, group) in enumerate(spec):
        items.append({"id": iid, "image": f"images/{iid}.png", "mask": f"masks/{iid}.png",
                      "defect_class": "fixture_class_A" if n % 2 else "fixture_class_B", "width": 64, "height": 64,
                      "image_sha256": hashlib.sha256(f"img-{iid}".encode()).hexdigest(),
                      "mask_sha256": hashlib.sha256(f"mask-{iid}".encode()).hexdigest(),
                      "duplicate_group": group, "source_group": None, "split": split})
    return {"schema_version": 1, "dataset_id": "carinthia-s", "data_mode": "synthetic_fixture",
            "created_at": "2026-10-05T00:00:00Z", "root": root, "license": "fixture only",
            "limitations": [SYNTHETIC_LABEL], "split": {"method": "fixture"}, "items": items}
