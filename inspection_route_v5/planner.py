"""Bounded 3-step receding-horizon beam planner (inspection route v5, synthetic study).

``route_beam3`` reuses the frozen v3 selection reward, cost contract and input checks
(``inspection_v3.policies._V3Policy``) and only replaces the plan:

* Level 1: the v3 shortlist of affordable sites, top ``first_size`` by single-step value
  ``reward / max_cost`` plus top ``per_wafer`` per wafer (ties: lower site index).
* Level 2: from every level-1 site, the top ``next_size`` by ``reward / step_cost`` plus top
  ``per_wafer`` per wafer, among allowed, not-yet-planned sites whose cumulative reserved
  cost fits the remaining budget.
* Level 3: the same expansion from every level-2 path.

Each step's reserved cost is projected exactly as the harness charges it from the previous
stage position: ``wafer_load`` on any wafer change, ``stage_base``, movement only on the same
wafer, ``dwell``, ``outside_rescan`` outside the original candidates and the full retry reserve
(``inspection_v3.policies.pair_costs``). The first step uses the harness ``max_cost``. Every
feasible 1-, 2- and 3-step path is scored by ``sum(reward) / sum(reserved cost)``. Ties: highest
value (paths within a relative ``TIE_RTOL`` of the best value are equal, since float sums of
equal ratios differ in the last bits), then highest first-step single value, then the
lexicographically smallest planned site-ID tuple (a shorter prefix sorts first). Only the first site is returned; the plan is recomputed after every observation.

The policy never reads ``online_p``, the oracle, seeds or scenarios, never updates a model
and never mutates ``state``.
"""
from __future__ import annotations

import math

import numpy as np

from inspection_v3 import policies as v3p

EPS = v3p.EPS
TIE_RTOL = 1e-12
BEAM_DEFAULTS = {"first_size": 16, "next_size": 16, "per_wafer": 2, "depth": 3}


def beam_config(config: dict | None) -> dict:
    given = dict((config or {}).get("route_v5", {}).get("beam") or {})
    unknown = set(given) - set(BEAM_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown route_v5.beam keys {sorted(unknown)}")
    c = {**BEAM_DEFAULTS, **given}
    for k, x in c.items():
        if isinstance(x, bool) or int(x) != x or int(x) < 1:
            raise ValueError(f"{k} must be a positive integer")
    if c["depth"] != 3:
        raise ValueError("route_beam3 is fixed at depth 3")
    return {k: int(x) for k, x in c.items()}


def _expand(state, paths, ccum, reward, cols, budget, cost, size, per_wafer):
    """Children of every path: (child paths, cumulative reserved cost, last step cost, parent row)."""
    v = state.view
    last = paths[:, -1]
    step = v3p.pair_costs(state, last, cols, cost)
    total = ccum[:, None] + step
    feas = total <= budget + EPS
    for d in range(paths.shape[1]):
        feas &= cols[None, :] != paths[:, d][:, None]
    out_p, out_c, out_s, out_r = [], [], [], []
    for r in range(paths.shape[0]):
        ok = np.flatnonzero(feas[r])
        if ok.size == 0:
            continue
        score = np.full(cols.size, -np.inf)
        score[ok] = reward[cols[ok]] / step[r, ok]
        pick = list(v3p.v2._order(score, ok)[:size])
        wafers = v.wafer[cols[ok]]
        for w in np.unique(wafers):
            pick += list(v3p.v2._order(score, ok[wafers == w])[:per_wafer])
        pick = np.unique(np.asarray(pick, dtype=int))
        out_p.append(np.hstack([np.repeat(paths[r][None, :], pick.size, axis=0), cols[pick][:, None]]))
        out_c.append(total[r, pick])
        out_s.append(step[r, pick])
        out_r.append(np.full(pick.size, r))
    if not out_p:
        return np.empty((0, paths.shape[1] + 1), dtype=int), np.empty(0), np.empty(0), np.empty(0, dtype=int)
    return np.vstack(out_p), np.concatenate(out_c), np.concatenate(out_s), np.concatenate(out_r)


def plan_beam3(state, reward, max_cost, mask, budget, cost, beam):
    """Best feasible path (as a dict) under the summed reward / summed reserved cost rule."""
    v = state.view
    single = reward / max_cost
    first = v3p.shortlist(single, v.wafer, mask & (max_cost <= budget + EPS), beam["first_size"], beam["per_wafer"])
    cols = np.flatnonzero(state.allowed())
    levels = [{"paths": first[:, None], "cum": max_cost[first].astype(float), "steps": max_cost[first][:, None].astype(float)}]
    for _ in range(beam["depth"] - 1):
        prev = levels[-1]
        if prev["paths"].shape[0] == 0:
            break
        p, c, s, parent = _expand(state, prev["paths"], prev["cum"], reward, cols, budget, cost,
                                  beam["next_size"], beam["per_wafer"])
        steps = np.hstack([prev["steps"][parent], s[:, None]]) if p.shape[0] else np.empty((0, p.shape[1]))
        levels.append({"paths": p, "cum": c, "steps": steps})
    scored = []
    for lv in levels:
        if lv["paths"].shape[0]:
            val = reward[lv["paths"]].sum(axis=1) / lv["cum"]
            scored.append((lv, val, single[lv["paths"][:, 0]]))
    top = max(float(val.max()) for _, val, _ in scored)
    floor = top - TIE_RTOL * abs(top)  # float sums of equal ratios differ in the last bits
    top_single = max(float(s[val >= floor].max()) for _, val, s in scored if (val >= floor).any())
    best = None
    for lv, val, s in scored:
        for k in np.flatnonzero((val >= floor) & (s == top_single)):
            key = (tuple(v.site_ids[j] for j in lv["paths"][k]), -float(val[k]))
            if best is None or key < best[0]:
                best = (key, lv, int(k))
    key, lv, k = best
    path = [int(j) for j in lv["paths"][k]]
    return {"index": path[0], "value": -key[1], "single": top_single, "path": path,
            "step_costs": [float(x) for x in lv["steps"][k]], "rewards": [float(reward[j]) for j in path],
            "reserved_total": float(lv["cum"][k]), "beam_counts": [int(l["paths"].shape[0]) for l in levels]}


class RouteBeam3Policy(v3p._V3Policy):
    """Bounded deterministic 3-step beam over summed reward per summed reserved cost."""

    name = "route_beam3"
    reason = "selection_reward_three_step_beam_route"

    def __init__(self, seed: int, config: dict | None):
        super().__init__(seed, config)
        self.beam = beam_config(config)

    def select(self, state, max_cost, affordable):
        affordable, max_cost, reward, src, budget, bsrc = self._inputs(state, max_cost, affordable)
        plan = plan_beam3(state, reward, max_cost, affordable, budget, self.cost, self.beam)
        v = state.view
        i = plan["index"]
        comp = {"single_step_value": plan["single"], "lookahead_value": plan["value"],
                "max_cost": float(max_cost[i]), "planned_site_indices": plan["path"],
                "planned_site_ids": [v.site_ids[j] for j in plan["path"]],
                "planned_step_reserved_costs": plan["step_costs"], "planned_rewards": plan["rewards"],
                "planned_reserved_total": plan["reserved_total"], "planned_length": len(plan["path"]),
                "beam_counts": plan["beam_counts"], "beam": dict(self.beam),
                "budget_for_lookahead": None if math.isinf(budget) else budget}
        return self._done(state, i, plan["value"], comp, affordable, reward, src, budget, bsrc)


POLICIES = ("route_full_gamma", "route_beam3")


def make_policy(name: str, seed: int, config: dict | None = None):
    """Local factory: the incumbent delegates unchanged to the frozen v3 factory."""
    if name == "route_full_gamma":
        return v3p.make_policy(name, seed, config)
    if name == "route_beam3":
        return RouteBeam3Policy(seed, config)
    raise ValueError(f"unknown route v5 policy {name}")
