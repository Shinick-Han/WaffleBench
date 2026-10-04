"""Inspection v2 review-selection policies (synthetic development study).

Interface: ``make_policy(name, seed, config)`` returns an object with the frozen v1
``select(state, max_cost, affordable) -> Choice`` contract, where ``state`` is the v1
``SelectionState``. The eight v1 names delegate unchanged to the v1 factory. New names:

- ``adaptive_audit``: frozen-probability exploitation, an adaptively sized random audit
  of frozen-negative ORIGINAL candidates, and (with_rescan only) a separately scheduled
  outside-candidate rescan.
- ``route_aware``: frozen probability per total equipment cost with a bounded,
  deterministic 2-step lookahead over the CU cost contract.
- ``adaptive_route``: ``adaptive_audit`` branching whose exploit and rescan branches are
  chosen by the ``route_aware`` lookahead.

Information boundary: new policies read only the public view, the FROZEN probability,
and labels of successful paid reviews already recorded in ``SelectionState``. They never
update the model (``updates_model = False``), never read ``online_p``, never simulate
review outcomes and never mutate ``state``. Every decision is recomputed from the
current state (replanning after each paid observation); no plan is cached.

Cost: ``max_cost`` from the harness is authoritative for the first step and is checked
against the v1 CU contract recomputed from ``config['cost']``. A hypothetical second
step after site i costs ``wafer_load`` when its wafer differs from i's (also when
returning to a previously loaded wafer), ``stage_base`` always, movement from i only on
the same wafer, ``dwell``, ``outside_rescan`` outside the original candidates, and the
full ``retry_dwell * retry_limit`` reserve. A pair is feasible only when both maximum
(reserved) costs fit the remaining budget. The remaining budget is read from an
optional numeric ``state.remaining_budget`` (a v2 harness may expose it); otherwise it
is bounded from the masks: when some allowed site is unaffordable the budget is binding
and ``max(max_cost[affordable])`` is used as a conservative lower bound, else every
pair is treated as feasible (logged as ``budget_source``). Lookahead feasibility only
affects scoring; the returned site is always inside ``affordable``.

Adaptive audit (``audit_min``/``audit_max`` are hard fractions of decisions so far):
  k = decision number, a = audits already made.
  forced audit when a < floor(audit_min * k); audit forbidden when a + 1 > audit_max * k;
  otherwise audit when a < target * k, with
  target = audit_min + (audit_max - audit_min) * min(1, u_audit / u_exploit),
  theta_a = (s * m_a + sum y_audit) / (s + n_audit)   [m_a: mean frozen p of audit pool]
  rho     = (s + sum y_exploit) / (s + sum p_exploit) [paid exploit labels vs frozen p]
  u_audit = theta_a / E_pi[max_cost],  u_exploit = rho * max(p / max_cost).
The audit pool is affordable original candidates with frozen p < classification
threshold; the site is drawn with the policy's local seeded RNG with probability
proportional to 1/max_cost (or uniform). The logged propensity is CONDITIONAL on the
deterministic audit branch and the current pool; it is not a full-policy propensity and
supports no unbiased off-policy-evaluation claim. Failed or missing reviews are billed
by the harness and contribute no label here. ``audit_min = audit_max = 0`` disables
audits (ablation); ``audit_min = 0`` alone keeps an adaptive but optional audit.

Conservative defaults live in ``V2_SELECTION_DEFAULTS`` (overridden by
``config['v2_selection']``); the coordinator freezes the experiment values.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from inspection_review import policies as v1
from inspection_review.policies import Choice, Policy, SelectionState

V1_POLICIES = v1.POLICIES
V2_POLICIES = ("adaptive_audit", "route_aware", "adaptive_route")
POLICIES = V1_POLICIES + V2_POLICIES
EPS = 1e-9
COST_KEYS = ("wafer_load", "stage_base", "stage_per_normalized_distance", "dwell", "retry_dwell",
             "outside_rescan", "retry_limit")

V2_SELECTION_DEFAULTS = {
    "audit_min": 0.05,              # hard floor of audit decisions (0 = ablation allowed)
    "audit_max": 0.25,              # hard ceiling of audit decisions
    "audit_prior_strength": 4.0,    # pseudo-count s of the frozen-score prior
    "audit_sampling": "inverse_cost",  # or "uniform"
    "rescan_period": 10,            # with_rescan: every k-th decision is an outside rescan
    "shortlist_size": 16,          # first-step shortlist by single-step value
    "route_per_wafer": 2,           # plus the best sites of every wafer (switch options)
    "lookahead_weight": 1.0,      # weight of the second-step reward
}
_SAMPLING = ("inverse_cost", "uniform")


def selection_config(config: dict | None) -> dict:
    """Validated v2 selection settings: defaults overridden by ``config['v2_selection']``."""
    given = dict((config or {}).get("v2_selection") or {})
    unknown = set(given) - set(V2_SELECTION_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown v2_selection keys {sorted(unknown)}")
    c = {**V2_SELECTION_DEFAULTS, **given}
    lo, hi = float(c["audit_min"]), float(c["audit_max"])
    if not (0.0 <= lo <= hi <= 1.0):
        raise ValueError("require 0 <= audit_min <= audit_max <= 1")
    if float(c["audit_prior_strength"]) <= 0:
        raise ValueError("audit_prior_strength must be positive")
    if c["audit_sampling"] not in _SAMPLING:
        raise ValueError(f"audit_sampling must be one of {_SAMPLING}")
    for k in ("rescan_period", "shortlist_size", "route_per_wafer"):
        if int(c[k]) != c[k] or int(c[k]) < 1:
            raise ValueError(f"{k} must be a positive integer")
    if not 0.0 <= float(c["lookahead_weight"]) <= 1.0:
        raise ValueError("lookahead_weight must be in [0, 1]")
    return {"audit_min": lo, "audit_max": hi, "audit_prior_strength": float(c["audit_prior_strength"]),
            "audit_sampling": c["audit_sampling"], "rescan_period": int(c["rescan_period"]),
            "shortlist_size": int(c["shortlist_size"]), "route_per_wafer": int(c["route_per_wafer"]),
            "lookahead_weight": float(c["lookahead_weight"])}


def _cost_config(config: dict | None) -> dict:
    cost = (config or {}).get("cost")
    if not isinstance(cost, dict) or any(k not in cost for k in COST_KEYS):
        raise ValueError(f"config['cost'] must define {COST_KEYS}")
    out = {k: float(cost[k]) for k in COST_KEYS}
    if out["retry_limit"] != int(out["retry_limit"]) or out["retry_limit"] < 0:
        raise ValueError("retry_limit must be a non-negative integer")
    if any(out[k] < 0 for k in COST_KEYS):
        raise ValueError("cost terms must be non-negative")
    return out


def _reserve(cost: dict) -> float:
    return cost["retry_dwell"] * int(cost["retry_limit"])


def first_step_costs(state: SelectionState, cost: dict) -> np.ndarray:
    """Maximum (reserved) cost of every site from the current stage position (v1 contract)."""
    v = state.view
    if state.current_wafer is None:
        switch = np.ones(v.n, dtype=bool)
        dist = np.zeros(v.n)
    else:
        switch = v.wafer != state.current_wafer
        dist = np.linalg.norm(v.xy - state.current_xy, axis=1)
    return (np.where(switch, cost["wafer_load"], 0.0) + cost["stage_base"]
            + cost["stage_per_normalized_distance"] * np.where(switch, 0.0, dist)
            + cost["dwell"] + np.where(v.candidate, 0.0, cost["outside_rescan"]) + _reserve(cost))


def second_step_costs(state: SelectionState, i: int, cost: dict) -> np.ndarray:
    """Maximum cost of every site if the stage were at site i. Reads state, never mutates it."""
    v = state.view
    switch = v.wafer != v.wafer[i]
    dist = np.linalg.norm(v.xy - v.xy[i], axis=1)
    return (np.where(switch, cost["wafer_load"], 0.0) + cost["stage_base"]
            + cost["stage_per_normalized_distance"] * np.where(switch, 0.0, dist)
            + cost["dwell"] + np.where(v.candidate, 0.0, cost["outside_rescan"]) + _reserve(cost))


def remaining_budget(state: SelectionState, max_cost: np.ndarray, affordable: np.ndarray) -> tuple[float, str]:
    """(budget used for lookahead feasibility, its source)."""
    exact = getattr(state, "remaining_budget", None)
    if exact is not None and not isinstance(exact, bool) and np.isfinite(float(exact)):
        return float(exact), "state_remaining_budget"
    if (state.allowed() & ~affordable).any():
        return float(np.max(max_cost[affordable])), "inferred_lower_bound"
    return math.inf, "unbinding_all_allowed_affordable"


def _order(score: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """idx sorted by score descending, ties by lower site index (deterministic)."""
    return idx[np.lexsort((idx, -score[idx]))]


def _check_inputs(state: SelectionState, max_cost: np.ndarray, affordable: np.ndarray, cost: dict) -> None:
    affordable = np.asarray(affordable, bool)
    if affordable.shape != (state.view.n,) or np.asarray(max_cost).shape != (state.view.n,):
        raise ValueError("max_cost/affordable must have one entry per site")
    if not affordable.any():
        raise ValueError("no affordable site")
    if (affordable & ~state.allowed()).any():
        raise ValueError("affordable includes a disallowed or visited site")
    own = first_step_costs(state, cost)
    if not np.allclose(own[affordable], np.asarray(max_cost, float)[affordable], rtol=0, atol=1e-6):
        raise ValueError("harness max_cost disagrees with the CU cost contract in config['cost']")


def _py(x: Any) -> Any:
    return x.item() if isinstance(x, np.generic) else x


class _V2Policy(Policy):
    updates_model = False
    uses_spatial = False

    def __init__(self, seed: int, config: dict | None):
        super().__init__(seed)
        self.v2 = selection_config(config)
        self.cost = _cost_config(config)

    def _done(self, state, i, reason, score, comp, affordable) -> Choice:
        if not affordable[i]:
            raise RuntimeError(f"{self.name} chose unaffordable site {i}")
        comp = {k: _py(v) for k, v in comp.items()}
        comp.setdefault("frozen_p", float(state.frozen_p[i]))
        return Choice(int(i), reason, float(score), comp, [])


class RouteAwarePolicy(_V2Policy):
    """Frozen probability per total CU with a bounded deterministic 2-step lookahead."""

    name = "route_aware"

    def route_pick(self, state, max_cost, mask, budget) -> tuple[int, float, dict]:
        reward = np.asarray(state.frozen_p, float)
        max_cost = np.asarray(max_cost, float)
        single = reward / max_cost
        idx = np.flatnonzero(mask)
        short = list(_order(single, idx)[: self.v2["shortlist_size"]])
        for w in np.unique(state.view.wafer[idx]):
            short += list(_order(single, idx[state.view.wafer[idx] == w])[: self.v2["route_per_wafer"]])
        short = np.unique(np.asarray(short, dtype=int))
        allowed = state.allowed()
        gamma = self.v2["lookahead_weight"]
        best = None
        for i in short:
            c1 = float(max_cost[i])
            c2 = second_step_costs(state, int(i), self.cost)
            nxt = allowed.copy()
            nxt[i] = False
            nxt &= c1 + c2 <= budget + EPS
            if nxt.any():
                pair = (reward[i] + gamma * reward) / (c1 + c2)
                j = int(_order(pair, np.flatnonzero(nxt))[0])
                value, pair_cost = float(pair[j]), c1 + float(c2[j])
            else:
                j, value, pair_cost = None, float(single[i]), c1
            key = (value, float(single[i]), -int(i))
            if best is None or key > best[0]:
                best = (key, int(i), j, pair_cost)
        _, i, j, pair_cost = best
        v = state.view
        comp = {"single_step_value": float(single[i]), "lookahead_value": best[0][0], "max_cost": float(max_cost[i]),
                "next_site_index": j, "next_site_id": None if j is None else v.site_ids[j],
                "pair_reserved_cost": pair_cost,
                "wafer_switch_now": bool(state.current_wafer is None or v.wafer[i] != state.current_wafer),
                "wafer_switch_next": None if j is None else bool(v.wafer[j] != v.wafer[i]),
                "shortlist_size": int(short.size), "budget_for_lookahead": None if math.isinf(budget) else budget}
        return i, best[0][0], comp

    def select(self, state, max_cost, affordable):
        affordable = np.asarray(affordable, bool)
        _check_inputs(state, max_cost, affordable, self.cost)
        budget, src = remaining_budget(state, max_cost, affordable)
        i, value, comp = self.route_pick(state, max_cost, affordable, budget)
        comp["budget_source"] = src
        return self._done(state, i, "frozen_probability_two_step_route_per_cost", value, comp, affordable)


class AdaptiveAuditPolicy(_V2Policy):
    """Frozen exploitation + bounded adaptive random audit + scheduled outside rescan."""

    name = "adaptive_audit"
    routed = False

    def __init__(self, seed: int, config: dict | None):
        super().__init__(seed, config)
        self.threshold = float(((config or {}).get("model") or {}).get("classification_threshold", 0.5))
        self.rng = np.random.default_rng(seed)
        self.branch: dict[int, str] = {}
        self._state: SelectionState | None = None
        self._route = RouteAwarePolicy(seed, config) if self.routed else None

    def _history(self, state):
        audits, ya, ye, pe = 0, [], [], []
        for i in state.selected:
            b = self.branch.get(int(i))
            audits += b == "audit"
            y = state.label[i]
            if np.isnan(y):
                continue  # failed or missing review: billed, never a label
            if b == "audit":
                ya.append(float(y))
            elif b == "exploit":
                ye.append(float(y))
                pe.append(float(state.frozen_p[i]))
        return audits, ya, ye, pe

    def _pick(self, state, max_cost, mask, budget):
        if self._route is not None:
            return self._route.route_pick(state, max_cost, mask, budget)
        score = np.asarray(state.frozen_p, float) / np.asarray(max_cost, float)
        i = int(_order(score, np.flatnonzero(mask))[0])
        return i, float(score[i]), {"single_step_value": float(score[i]), "max_cost": float(max_cost[i])}

    def select(self, state, max_cost, affordable):
        affordable = np.asarray(affordable, bool)
        max_cost = np.asarray(max_cost, float)
        _check_inputs(state, max_cost, affordable, self.cost)
        if self._state is None:
            self._state = state
        elif self._state is not state:
            raise RuntimeError("an adaptive policy instance serves exactly one run")
        c = self.v2
        k = state.step
        budget, src = remaining_budget(state, max_cost, affordable)
        cand = state.view.candidate
        p = np.asarray(state.frozen_p, float)
        base = {"decision": k, "budget_source": src,
                "budget_for_lookahead": None if math.isinf(budget) else budget}

        if state.mode == "with_rescan" and k % c["rescan_period"] == 0:
            outside = affordable & ~cand
            if outside.any():
                i, value, comp = self._pick(state, max_cost, outside, budget)
                self.branch[i] = "rescan"
                return self._done(state, i, "scheduled_outside_rescan", value,
                                  {**base, **comp, "branch": "rescan"}, affordable)
            base["rescan_skipped"] = "no_affordable_outside_site"

        audits, ya, ye, pe = self._history(state)
        pool = affordable & cand & (p < self.threshold)
        n_pool = int(pool.sum())
        s = c["audit_prior_strength"]
        exploit_ratio = float(np.max(p[affordable] / max_cost[affordable]))
        rho = (s + sum(ye)) / (s + sum(pe))
        if n_pool:
            w = 1.0 / max_cost[pool] if c["audit_sampling"] == "inverse_cost" else np.ones(n_pool)
            pi = w / w.sum()
            m_a = float(p[pool].mean())
            theta = (s * m_a + sum(ya)) / (s + len(ya))
            u_audit = theta / float(pi @ max_cost[pool])
        else:
            theta = u_audit = None
        u_exploit = rho * exploit_ratio
        lo, hi = c["audit_min"], c["audit_max"]
        if u_audit is None:
            target = lo
        else:
            target = lo + (hi - lo) * (min(1.0, u_audit / u_exploit) if u_exploit > 0 else 1.0)
        floor_needed = audits < math.floor(lo * k + EPS)
        ceiling_ok = audits + 1 <= hi * k + EPS
        if not ceiling_ok:
            want, why = False, "audit_ceiling"
        elif floor_needed:
            want, why = True, "audit_floor"
        elif audits < target * k - EPS:
            want, why = True, "adaptive_target"
        else:
            want, why = False, "adaptive_target_met"
        diag = {**base, "audits_before": audits, "audit_target_fraction": target,
                "audit_posterior_theta": theta, "audit_labels": len(ya), "audit_label_positives": sum(ya),
                "exploit_calibration_rho": rho, "exploit_labels": len(ye), "u_audit": u_audit,
                "u_exploit": u_exploit, "audit_pool_size": n_pool, "branch_rule": why}

        if want and n_pool:
            sites = np.flatnonzero(pool)
            pos = int(self.rng.choice(n_pool, p=pi))
            i = int(sites[pos])
            self.branch[i] = "audit"
            diag.update({"branch": "audit", "selection_propensity": float(pi[pos]),
                         "propensity_scope": "conditional_on_deterministic_audit_branch_and_current_pool",
                         "audit_sampling": c["audit_sampling"], "max_cost": float(max_cost[i]),
                         "ope_claim": "none"})
            return self._done(state, i, "adaptive_random_frozen_negative_audit", float(p[i] / max_cost[i]),
                              diag, affordable)
        if want:
            diag["branch_rule"] = why + "_unmet_no_audit_pool"
        i, value, comp = self._pick(state, max_cost, affordable, budget)
        self.branch[i] = "exploit"
        reason = "frozen_probability_two_step_route_per_cost" if self.routed else "frozen_probability_per_cost"
        return self._done(state, i, reason, value, {**diag, **comp, "branch": "exploit"}, affordable)


class AdaptiveRoutePolicy(AdaptiveAuditPolicy):
    name = "adaptive_route"
    routed = True


_V2 = {"adaptive_audit": AdaptiveAuditPolicy, "route_aware": RouteAwarePolicy, "adaptive_route": AdaptiveRoutePolicy}


def make_policy(name: str, seed: int, config: dict | None = None) -> Policy:
    """v1 names delegate unchanged to the frozen v1 factory; v2 names need ``config['cost']``."""
    if name in V1_POLICIES:
        return v1.make_policy(name, seed)
    if name not in _V2:
        raise ValueError(f"unknown policy {name}")
    return _V2[name](seed, config)
