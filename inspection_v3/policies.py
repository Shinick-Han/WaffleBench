"""Inspection v3 review-selection policies (synthetic development study).

Interface: ``make_policy(name, seed, config)`` returns an object with the frozen v1
``select(state, max_cost, affordable) -> Choice`` contract, where ``state`` is the v1
``SelectionState``. Every existing v1/v2 name delegates unchanged to the v2 factory (which
delegates v1 names to the frozen v1 factory). New names:

- ``yield_greedy``: selection reward per maximum (reserved) CU cost, single step.
- ``route_vectorized``: the v2 ``route_aware`` planner computed as a chunked pair matrix.
  Same shortlist, same numerator-only discount ``(r_i + w r_j) / (c_i + c_j)`` with
  ``w = v2_selection.lookahead_weight``; under the frozen probability it reproduces
  ``route_aware`` exactly (same choice, same lookahead value).
- ``route_full_gamma``: the same planner with a FULL-pair discount
  ``(r_i + g r_j) / (c_i + g c_j)``, ``g = v3_selection.gamma`` (default 1, i.e. the
  undiscounted reward rate of the pair). The discount applies to the second step's
  reward and its cost alike, never to the numerator only.
- ``yield_route``: ``route_full_gamma`` planner on the selection (yield) reward.

Selection reward: the read-only ``state.selection_reward`` when the harness exposes it
(e.g. a detection-yield-adjusted probability), else ``state.frozen_p``. It must be finite,
in [0, 1] and have one entry per site; it is copied read-only and logged as
``reward_source``. The policies never read ``online_p``, oracle, seed or scenario fields,
never update a model (``updates_model = False``) and never mutate ``state``. Every decision
is recomputed from the current state (receding horizon after every observation).

Cost and admission follow v2 exactly: ``max_cost``/``affordable`` from the harness are
authoritative for the first action and are checked against the v1 CU contract
(``inspection_v2.policies._check_inputs``). A hypothetical second action from site i pays
``wafer_load`` on any wafer change (also a return to a previously loaded wafer),
``stage_base``, movement only on the same wafer, ``dwell``, ``outside_rescan`` outside the
original candidates and the full retry reserve. A pair is feasible only when both reserved
costs fit ``inspection_v2.policies.remaining_budget``. The second site ranges over every
allowed site (not only the first step's affordable set). A shortlist row with no feasible
second site falls back to its single-step value. Ties: highest value, then highest
single-step value, then lowest site index; the best second site is the lowest index among
equal pair values. Rows are evaluated in bounded chunks of at most
``v3_selection.chunk_elements`` pair cells, never as a full N x N matrix.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from inspection_review.policies import Choice, Policy, SelectionState
from inspection_v2 import policies as v2

EPS = v2.EPS
PRIOR_POLICIES = v2.POLICIES
V3_POLICIES = ("yield_greedy", "route_vectorized", "route_full_gamma", "yield_route")
POLICIES = PRIOR_POLICIES + V3_POLICIES

V3_SELECTION_DEFAULTS = {
    "gamma": 1.0,               # full-pair discount of the second step (reward and cost)
    "chunk_elements": 1 << 20,  # max pair cells evaluated per chunk
}


def selection_config(config: dict | None) -> dict:
    """Validated v3 selection settings: defaults overridden by ``config['v3_selection']``."""
    given = dict((config or {}).get("v3_selection") or {})
    unknown = set(given) - set(V3_SELECTION_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown v3_selection keys {sorted(unknown)}")
    c = {**V3_SELECTION_DEFAULTS, **given}
    gamma = c["gamma"]
    if isinstance(gamma, bool) or not math.isfinite(float(gamma)) or not 0.0 <= float(gamma) <= 1.0:
        raise ValueError("gamma must be in [0, 1]")
    chunk = c["chunk_elements"]
    if isinstance(chunk, bool) or int(chunk) != chunk or int(chunk) < 1:
        raise ValueError("chunk_elements must be a positive integer")
    return {"gamma": float(gamma), "chunk_elements": int(chunk)}


def selection_reward(state: SelectionState) -> tuple[np.ndarray, str]:
    """(read-only reward copy, source). Validated finite, in [0, 1], one entry per site."""
    given = getattr(state, "selection_reward", None)
    src = "frozen_p" if given is None else "state_selection_reward"
    r = np.array(state.frozen_p if given is None else given, dtype=float, copy=True)
    if r.shape != (state.view.n,):
        raise ValueError(f"{src} must have one entry per site")
    if not np.all(np.isfinite(r)) or np.any(r < 0.0) or np.any(r > 1.0):
        raise ValueError(f"{src} must be finite and in [0, 1]")
    r.flags.writeable = False
    return r, src


def shortlist(single: np.ndarray, wafer: np.ndarray, mask: np.ndarray, size: int, per_wafer: int) -> np.ndarray:
    """v2 shortlist: top ``size`` of ``mask`` by single-step value plus top ``per_wafer`` per wafer."""
    idx = np.flatnonzero(mask)
    short = list(v2._order(single, idx)[:size])
    for w in np.unique(wafer[idx]):
        short += list(v2._order(single, idx[wafer[idx] == w])[:per_wafer])
    return np.unique(np.asarray(short, dtype=int))


def pair_costs(state: SelectionState, rows: np.ndarray, cols: np.ndarray, cost: dict) -> np.ndarray:
    """Reserved cost of every column site if the stage were at each row site (len(rows) x len(cols)).

    Row-wise identical, term by term, to ``inspection_v2.policies.second_step_costs``.
    """
    v = state.view
    switch = v.wafer[cols][None, :] != v.wafer[rows][:, None]
    dist = np.linalg.norm(v.xy[cols][None, :, :] - v.xy[rows][:, None, :], axis=2)
    outside = np.where(v.candidate[cols], 0.0, cost["outside_rescan"])[None, :]
    return (np.where(switch, cost["wafer_load"], 0.0) + cost["stage_base"]
            + cost["stage_per_normalized_distance"] * np.where(switch, 0.0, dist)
            + cost["dwell"] + outside + v2._reserve(cost))


def plan_pairs(state: SelectionState, reward: np.ndarray, max_cost: np.ndarray, mask: np.ndarray,
               budget: float, cost: dict, *, size: int, per_wafer: int, gamma: float, full_gamma: bool,
               chunk_elements: int) -> dict:
    """Best first site of the shortlist by its best feasible two-step pair value."""
    v = state.view
    single = reward / max_cost
    short = shortlist(single, v.wafer, mask, size, per_wafer)
    cols = np.flatnonzero(state.allowed())
    n_rows = short.size
    value = np.empty(n_rows)
    nxt = np.full(n_rows, -1, dtype=int)
    pair_cost = np.empty(n_rows)
    step = max(1, chunk_elements // max(1, cols.size))
    chunks = 0
    for a in range(0, n_rows, step):
        rows = short[a:a + step]
        chunks += 1
        c1 = max_cost[rows][:, None]
        c2 = pair_costs(state, rows, cols, cost)
        total = c1 + c2
        feas = (cols[None, :] != rows[:, None]) & (total <= budget + EPS)
        num = reward[rows][:, None] + gamma * reward[cols][None, :]
        den = c1 + gamma * c2 if full_gamma else total
        with np.errstate(divide="ignore", invalid="ignore"):
            val = np.where(feas, num / den, -np.inf)
        jpos = np.argmax(val, axis=1)  # first maximum: lowest site index among equal values
        has = feas.any(axis=1)
        r = np.arange(rows.size)
        value[a:a + rows.size] = np.where(has, val[r, jpos], single[rows])
        nxt[a:a + rows.size] = np.where(has, cols[jpos], -1)
        pair_cost[a:a + rows.size] = np.where(has, total[r, jpos], max_cost[rows])
    k = int(np.lexsort((short, -single[short], -value))[0])
    j = int(nxt[k])
    return {"index": int(short[k]), "value": float(value[k]), "single": float(single[short[k]]),
            "next": None if j < 0 else j, "pair_cost": float(pair_cost[k]), "shortlist_size": int(n_rows),
            "pairs_evaluated": int(n_rows * cols.size), "chunks": chunks}


def _py(x: Any) -> Any:
    return x.item() if isinstance(x, np.generic) else x


class _V3Policy(Policy):
    updates_model = False
    uses_spatial = False
    reason = ""

    def __init__(self, seed: int, config: dict | None):
        super().__init__(seed)
        self.v2 = v2.selection_config(config)
        self.v3 = selection_config(config)
        self.cost = v2._cost_config(config)

    def _inputs(self, state, max_cost, affordable):
        affordable = np.asarray(affordable, bool)
        max_cost = np.asarray(max_cost, float)
        v2._check_inputs(state, max_cost, affordable, self.cost)
        if not np.all(max_cost[affordable] > 0):
            raise ValueError("max_cost must be positive on affordable sites")
        reward, src = selection_reward(state)
        budget, bsrc = v2.remaining_budget(state, max_cost, affordable)
        return affordable, max_cost, reward, src, budget, bsrc

    def _done(self, state, i, score, comp, affordable, reward, src, budget, bsrc) -> Choice:
        if not affordable[i]:
            raise RuntimeError(f"{self.name} chose unaffordable site {i}")
        comp = {"selection_reward": float(reward[i]), "reward_source": src, "frozen_p": float(state.frozen_p[i]),
                "site_id": state.view.site_ids[i], "budget_source": bsrc,
                "remaining_budget": None if math.isinf(budget) else budget, **comp}
        return Choice(int(i), self.reason, float(score), {k: _py(x) for k, x in comp.items()}, [])


class YieldGreedyPolicy(_V3Policy):
    """Selection reward per maximum reserved CU cost (single step)."""

    name = "yield_greedy"
    reason = "selection_reward_per_cost"

    def select(self, state, max_cost, affordable):
        affordable, max_cost, reward, src, budget, bsrc = self._inputs(state, max_cost, affordable)
        single = reward / max_cost
        i = int(v2._order(single, np.flatnonzero(affordable))[0])
        comp = {"single_step_value": float(single[i]), "max_cost": float(max_cost[i])}
        return self._done(state, i, single[i], comp, affordable, reward, src, budget, bsrc)


class RouteVectorizedPolicy(_V3Policy):
    """v2 route_aware semantics (numerator-only lookahead weight) as a chunked pair matrix."""

    name = "route_vectorized"
    reason = "selection_reward_two_step_route_per_cost"
    full_gamma = False

    def gamma(self) -> float:
        return self.v3["gamma"] if self.full_gamma else self.v2["lookahead_weight"]

    def select(self, state, max_cost, affordable):
        affordable, max_cost, reward, src, budget, bsrc = self._inputs(state, max_cost, affordable)
        g = self.gamma()
        plan = plan_pairs(state, reward, max_cost, affordable, budget, self.cost,
                          size=self.v2["shortlist_size"], per_wafer=self.v2["route_per_wafer"], gamma=g,
                          full_gamma=self.full_gamma, chunk_elements=self.v3["chunk_elements"])
        v = state.view
        i, j = plan["index"], plan["next"]
        comp = {"single_step_value": plan["single"], "lookahead_value": plan["value"],
                "max_cost": float(max_cost[i]), "next_site_index": j,
                "next_site_id": None if j is None else v.site_ids[j],
                "next_selection_reward": None if j is None else float(reward[j]),
                "pair_reserved_cost": plan["pair_cost"],
                "wafer_switch_now": bool(state.current_wafer is None or v.wafer[i] != state.current_wafer),
                "wafer_switch_next": None if j is None else bool(v.wafer[j] != v.wafer[i]),
                "shortlist_size": plan["shortlist_size"], "pairs_evaluated": plan["pairs_evaluated"],
                "pair_chunks": plan["chunks"], "gamma": g,
                "gamma_scope": "reward_and_cost" if self.full_gamma else "reward_only",
                "budget_for_lookahead": None if math.isinf(budget) else budget}
        return self._done(state, i, plan["value"], comp, affordable, reward, src, budget, bsrc)


class RouteFullGammaPolicy(RouteVectorizedPolicy):
    name = "route_full_gamma"
    reason = "selection_reward_two_step_route_full_gamma"
    full_gamma = True


class YieldRoutePolicy(RouteFullGammaPolicy):
    name = "yield_route"
    reason = "selection_reward_two_step_route_full_gamma"


_V3 = {"yield_greedy": YieldGreedyPolicy, "route_vectorized": RouteVectorizedPolicy,
       "route_full_gamma": RouteFullGammaPolicy, "yield_route": YieldRoutePolicy}


def make_policy(name: str, seed: int, config: dict | None = None) -> Policy:
    """v1/v2 names delegate unchanged to the v2 factory; v3 names need ``config['cost']``."""
    if name in PRIOR_POLICIES:
        return v2.make_policy(name, seed, config)
    if name not in _V3:
        raise ValueError(f"unknown policy {name}")
    return _V3[name](seed, config)
