"""Budget-aware SEM review/audit policy with the frozen ``select(state, max_cost, affordable)``
contract (``inspection_review.policies.Policy``). Post-hackathon development only.

Information boundary. The policy reads only, and never writes:
  ``state.view`` (public whitelist: site ids, wafer, xy, candidate flag, z-scored features),
  ``state.frozen_p`` (frozen model probabilities), ``state.threshold``,
  ``state.selected`` / ``state.visited`` / ``state.label`` / ``state.labeled`` (observed
  history), optional ``state.embedding`` (read-only (n, d) image embedding) and optional
  numeric ``state.remaining_budget``.
It never reads ``online_p``/the online model, never updates a model, and never samples a
random number (the constructor seed is accepted for factory compatibility and unused).
Before deciding it runs a NAME-BASED guard that rejects a state whose attribute names
contain oracle/truth/mask/seed/scenario/hidden/future/ground, and a consistency check
that rejects labels for sites that were never selected (a future observation leaking in).
This guard is a tripwire, not a guarantee: a caller that hides truth under an innocuous
name is not detected. The existing factory state also exposes read-only public metadata
(site ids, wafer, xy, layer, candidate flag); the policy uses it only for costs, ids and
the trace. Image class labels and masks are never policy inputs.

Costs. ``max_cost`` and ``affordable`` from the harness are authoritative. The returned
site is always inside ``affordable`` (the harness already reserves the retry allowance in
``max_cost``). When ``config['cost']`` is given, ``max_cost`` must equal the CU contract of
``inspection_review.harness.cost_vectors`` (wafer load on each switch, stage base, movement
on the loaded wafer only, dwell, outside rescan, retry_dwell * retry_limit reserve); the
decision trace then itemizes these components. Without it the trace records only the
authoritative maximum.

Two objectives are kept separate and never summed:
  yield    = frozen_p / max_cost                         (expected DOI per reserved CU)
  learning = (w_u * 4p(1-p) + w_n * novelty + w_d * diversity) / max_cost
  novelty   = 1 - exp(-0.5 * mean(z^2)) on the embedding (z-scored public features by default)
  diversity = min(1, nearest selected-site embedding distance / embedding_scale), 1 if none.

Audit allocation (hard bounds on the fraction of AUDIT decisions; k = decision number,
a = audit decisions already taken and still present in ``state.selected``):
  forced    when a < floor(audit_min * k)
  forbidden when a + 1 > audit_max * k
  optional  when a < audit_target * k
An audit picks the best learning/cost site among affordable sites with frozen p below the
classification threshold; otherwise the exploit branch picks the best yield/cost site
among all affordable sites. Ties go to the lowest site index. An empty audit pool falls
back to exploitation and is logged. Audit bookkeeping is the policy's own record of its
returned choices; it is reset when a new state object arrives and pruned when the state's
history does not contain a recorded choice, so repeated calls at one step are idempotent.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from inspection_review.harness import cost_vectors
from inspection_review.policies import Choice, Policy, SelectionState

EPS = 1e-9
NAME = "sem_budget_audit"
BACKEND = "numpy_deterministic (no modAL/apricot execution)"
COST_KEYS = ("wafer_load", "stage_base", "stage_per_normalized_distance", "dwell", "retry_dwell",
             "outside_rescan", "retry_limit")
FORBIDDEN_FIELD_PARTS = ("oracle", "truth", "mask", "scenario", "seed", "hidden", "future", "ground")
DEFAULTS = {
    "audit_min": 0.05,
    "audit_target": 0.15,
    "audit_max": 0.25,
    "uncertainty_weight": 0.5,
    "novelty_weight": 0.25,
    "diversity_weight": 0.25,
    "embedding_scale": 1.0,
}


class InformationBoundaryError(ValueError):
    """The state exposes data a prospective policy must not read."""


def _real(name: str, v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite real number (got {v!r})")
    return float(v)


def decision_config(config: dict | None) -> dict:
    given = dict((config or {}).get("sem_decisions") or {})
    unknown = set(given) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown sem_decisions keys {sorted(unknown)}")
    c = {k: _real(k, v) for k, v in {**DEFAULTS, **given}.items()}
    if not 0.0 <= c["audit_min"] <= c["audit_target"] <= c["audit_max"] <= 1.0:
        raise ValueError("require 0 <= audit_min <= audit_target <= audit_max <= 1")
    w = [c["uncertainty_weight"], c["novelty_weight"], c["diversity_weight"]]
    if min(w) < 0 or not math.isclose(sum(w), 1.0, abs_tol=1e-9):
        raise ValueError("learning weights must be non-negative and sum to 1")
    if c["embedding_scale"] <= 0:
        raise ValueError("embedding_scale must be positive")
    return c


def _cost_config(config: dict | None) -> dict | None:
    cost = (config or {}).get("cost")
    if cost is None:
        return None
    if not isinstance(cost, dict) or any(k not in cost for k in COST_KEYS):
        raise ValueError(f"config['cost'] must define {COST_KEYS}")
    vals = {k: _real(f"cost.{k}", cost[k]) for k in COST_KEYS}
    if any(v < 0 for v in vals.values()) or vals["retry_limit"] != int(vals["retry_limit"]):
        raise ValueError("cost terms must be non-negative and retry_limit an integer")
    vals["retry_limit"] = int(vals["retry_limit"])
    return vals


def guard_state(state: SelectionState) -> None:
    """Raise InformationBoundaryError on oracle-like fields or future observations."""
    names = list(vars(state)) + list(vars(state.view))
    cfg = getattr(state, "cfg", None)
    if isinstance(cfg, dict):
        names += [str(k) for k in cfg]
    bad = sorted({n for n in names if any(p in n.lower() for p in FORBIDDEN_FIELD_PARTS)})
    if bad:
        raise InformationBoundaryError(f"state exposes forbidden fields {bad}")
    n = state.view.n
    selected = set(int(i) for i in state.selected)
    labeled = set(int(i) for i in state.labeled)
    if not labeled <= selected:
        raise InformationBoundaryError("labels recorded for sites that were never selected (future observation)")
    lab = np.asarray(state.label, float)
    has = np.flatnonzero(~np.isnan(lab))
    if lab.shape != (n,) or not set(int(i) for i in has) <= selected:
        raise InformationBoundaryError("label array holds outcomes of unselected sites (future observation)")
    if not set(np.flatnonzero(np.asarray(state.visited, bool)).tolist()) == selected:
        raise InformationBoundaryError("visited mask disagrees with selected history")


def _order(score: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """idx by score descending; ties by lowest site index."""
    return idx[np.lexsort((idx, -score[idx]))]


def _py(x: Any) -> Any:
    return x.item() if isinstance(x, np.generic) else x


class SemBudgetAuditPolicy(Policy):
    name = NAME
    updates_model = False
    uses_spatial = False

    def __init__(self, seed: int = 0, config: dict | None = None):
        super().__init__(seed)
        self.cfg = decision_config(config)
        self.cost = _cost_config(config)
        self._state_id: int | None = None
        self._audit_steps: dict[int, int] = {}  # step -> site index returned as an audit

    # -- read-only derived quantities -------------------------------------------------
    def embedding(self, state: SelectionState) -> np.ndarray:
        e = getattr(state, "embedding", None)
        e = state.view.zscores if e is None else e
        e = np.nan_to_num(np.asarray(e, float), nan=0.0, posinf=0.0, neginf=0.0)
        if e.ndim != 2 or e.shape[0] != state.view.n:
            raise ValueError("embedding must be (n_sites, d)")
        return e

    def learning_terms(self, state: SelectionState) -> dict[str, np.ndarray]:
        p = np.clip(np.asarray(state.frozen_p, float), 0.0, 1.0)
        e = self.embedding(state)
        novelty = 1.0 - np.exp(-0.5 * np.mean(e * e, axis=1))
        sel = np.asarray(state.selected, dtype=int)
        if sel.size:
            d = np.min(np.linalg.norm(e[:, None, :] - e[sel][None, :, :], axis=2), axis=1)
            diversity = np.minimum(1.0, d / self.cfg["embedding_scale"])
        else:
            diversity = np.ones(state.view.n)
        unc = 4.0 * p * (1.0 - p)
        c = self.cfg
        score = c["uncertainty_weight"] * unc + c["novelty_weight"] * novelty + c["diversity_weight"] * diversity
        return {"uncertainty": unc, "novelty": novelty, "diversity": diversity, "learning": score}

    def _audits_before(self, state: SelectionState) -> int:
        if self._state_id != id(state):
            self._state_id, self._audit_steps = id(state), {}
        step = state.step
        sel = list(state.selected)
        self._audit_steps = {s: i for s, i in self._audit_steps.items()
                             if s < step and s - 1 < len(sel) and sel[s - 1] == i}
        return len(self._audit_steps)

    def _check(self, state, max_cost, affordable):
        affordable = np.asarray(affordable, bool)
        max_cost = np.asarray(max_cost, float)
        n = state.view.n
        if affordable.shape != (n,) or max_cost.shape != (n,):
            raise ValueError("max_cost/affordable must have one entry per site")
        if not affordable.any():
            raise ValueError("no affordable site")
        if (affordable & ~state.allowed()).any():
            raise ValueError("affordable includes a disallowed or already visited site")
        if not np.all(np.isfinite(max_cost[affordable]) & (max_cost[affordable] > 0)):
            raise ValueError("max_cost must be finite and positive on affordable sites")
        parts = None
        if self.cost is not None:
            parts = cost_vectors(state, self.cost)
            if not np.allclose(parts["max"][affordable], max_cost[affordable], rtol=0, atol=1e-6):
                raise ValueError("harness max_cost disagrees with config['cost'] (movement/wafer/retry reserve)")
        return affordable, max_cost, parts

    def select(self, state: SelectionState, max_cost: np.ndarray, affordable: np.ndarray) -> Choice:
        guard_state(state)
        affordable, max_cost, parts = self._check(state, max_cost, affordable)
        p = np.asarray(state.frozen_p, float)
        terms = self.learning_terms(state)
        yield_pc = p / np.where(affordable, max_cost, 1.0)
        learn_pc = terms["learning"] / np.where(affordable, max_cost, 1.0)
        k = state.step
        a = self._audits_before(state)
        c = self.cfg
        forced = a < math.floor(c["audit_min"] * k + EPS)
        forbidden = a + 1 > c["audit_max"] * k + EPS
        optional = a < c["audit_target"] * k - EPS
        pool = affordable & (p < state.threshold)
        audit = (forced or optional) and not forbidden and bool(pool.any())
        if audit:
            i = int(_order(learn_pc, np.flatnonzero(pool))[0])
            reason, score = "audit_learning_per_cost", float(learn_pc[i])
            self._audit_steps[k] = i
        else:
            i = int(_order(yield_pc, np.flatnonzero(affordable))[0])
            reason, score = "exploit_expected_doi_per_cost", float(yield_pc[i])
        if not affordable[i]:
            raise RuntimeError("selected site is not affordable")
        budget, bsrc = self._budget(state, max_cost, affordable)
        cost = {"max_cost": float(max_cost[i]), "source": "harness_max_cost"}
        if parts is not None:
            cost.update({"wafer_load": float(parts["load"][i]), "stage": float(parts["stage"][i]),
                         "dwell": float(parts["dwell"][i]), "outside_rescan": float(parts["outside"][i]),
                         "retry_reserve": float(parts["max"][i] - parts["first"][i]),
                         "first_attempt": float(parts["first"][i])})
        cost["wafer_switch"] = bool(state.current_wafer is None or state.view.wafer[i] != state.current_wafer)
        comp = {
            "backend": BACKEND,
            "yield_objective": {"frozen_p": float(p[i]), "expected_doi_per_cost": float(yield_pc[i])},
            "learning_objective": {k2: float(terms[k2][i]) for k2 in ("uncertainty", "novelty", "diversity",
                                                                      "learning")}
            | {"learning_per_cost": float(learn_pc[i])},
            "audit": {"decision": k, "audits_before": a, "forced": forced, "forbidden": forbidden,
                      "optional": optional, "pool_size": int(pool.sum()), "branch": "audit" if audit else "exploit",
                      "pool_empty_fallback": bool((forced or optional) and not forbidden and not pool.any())},
            "cost": cost,
            "budget": {"remaining": budget, "source": bsrc,
                       "remaining_after_reserve": None if budget is None else budget - float(max_cost[i])},
            "tie_break": "lowest_site_index",
            "site_id": state.view.site_ids[i],
        }
        return Choice(i, reason, score, _jsonable(comp), [])

    @staticmethod
    def _budget(state, max_cost, affordable) -> tuple[float | None, str]:
        exact = getattr(state, "remaining_budget", None)
        if exact is not None:
            exact = _real("state.remaining_budget", exact)
            if exact < 0:
                raise ValueError("state.remaining_budget must be non-negative")
            if float(np.max(max_cost[affordable])) > float(exact) + EPS:
                raise ValueError("affordable site exceeds state.remaining_budget")
            return float(exact), "state_remaining_budget"
        return None, "not_exposed_harness_affordable_mask_authoritative"


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return _py(x)


def make_policy(seed: int = 0, config: dict | None = None) -> SemBudgetAuditPolicy:
    return SemBudgetAuditPolicy(seed, config)
