"""Choice policies for the optical-candidate to SEM-review routing PoC.

Post-hackathon development, excluded from judging. See ROUTING_POC_CONTRACT.md.

Every policy shares the same frozen public priors and the same uniform exploration
mixture: with probability ``audit_epsilon`` a uniformly random affordable site is
chosen (an audit), otherwise the policy's deterministic best site. The returned
``propensity`` is the exact conditional probability of the chosen site under that
mixture. No online training, oracle, label, image or latent fixture value is read.

``beam_route`` is a bounded depth-three beam heuristic over a shortlist. It is not
claimed to be globally optimal.

Pure Python standard library only.
"""

from __future__ import annotations

import math
import random

POLICIES = ("risk_only", "risk_per_second", "beam_route")

BEAM_DEPTH = 3
BEAM_WIDTH = 16
SHORTLIST_GLOBAL = 12
SHORTLIST_PER_WAFER = 4

STATE_KEYS = frozenset({"job_id", "candidates", "remaining_s", "current", "history", "cost"})
REQUIRED_STATE_KEYS = frozenset({"job_id", "candidates", "remaining_s", "current", "history"})
CANDIDATE_KEYS = frozenset({
    "site_id", "wafer_id", "x_um", "y_um", "prior_p", "optical_observed_at",
    "recipe_id", "capture_bound_s", "inference_bound_s", "cost",
})
CANDIDATE_COST_KEYS = frozenset({"load_s", "move_s", "settle_s", "first_s", "reserved_s"})
CURRENT_KEYS = frozenset({"wafer_id", "x_um", "y_um"})
JOB_COST_KEYS = frozenset({"wafer_load_s", "settle_s", "move_um_per_s", "retry_limit"})

# Relative/absolute tolerance used only to compare declared candidate costs with
# their projection from public cost parameters (never for budget admission).
_COST_TOL = 1e-6


# --------------------------------------------------------------------------- validation

def _number(value, name, *, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, got {type(value).__name__}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _exact_keys(obj, allowed, required, name):
    if not isinstance(obj, dict):
        raise ValueError(f"{name} must be a dict")
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ValueError(f"{name} has non-whitelisted keys: {extra}")
    missing = sorted(required - set(obj))
    if missing:
        raise ValueError(f"{name} is missing keys: {missing}")


def _sanitize_current(current):
    if current is None:
        return None
    _exact_keys(current, CURRENT_KEYS, CURRENT_KEYS, "state.current")
    return {
        "wafer_id": _text(current["wafer_id"], "current.wafer_id"),
        "x_um": _number(current["x_um"], "current.x_um"),
        "y_um": _number(current["y_um"], "current.y_um"),
    }


def _sanitize_job_cost(cost):
    if cost is None:
        return None
    _exact_keys(cost, JOB_COST_KEYS, JOB_COST_KEYS, "state.cost")
    retry = cost["retry_limit"]
    if isinstance(retry, bool) or not isinstance(retry, int) or retry not in (0, 1):
        raise ValueError("state.cost.retry_limit must be integer 0 or 1")
    return {
        "wafer_load_s": _number(cost["wafer_load_s"], "cost.wafer_load_s", minimum=0.0),
        "settle_s": _number(cost["settle_s"], "cost.settle_s", minimum=0.0),
        "move_um_per_s": _number(cost["move_um_per_s"], "cost.move_um_per_s", positive=True),
        "retry_limit": retry,
    }


def _sanitize_candidate(cand, index):
    name = f"candidates[{index}]"
    _exact_keys(cand, CANDIDATE_KEYS, CANDIDATE_KEYS, name)
    prior = _number(cand["prior_p"], f"{name}.prior_p")
    if not 0.0 <= prior <= 1.0:
        raise ValueError(f"{name}.prior_p must be in [0, 1]")
    cost = cand["cost"]
    _exact_keys(cost, CANDIDATE_COST_KEYS, CANDIDATE_COST_KEYS, f"{name}.cost")
    clean_cost = {k: _number(cost[k], f"{name}.cost.{k}", minimum=0.0) for k in sorted(CANDIDATE_COST_KEYS)}
    if clean_cost["reserved_s"] + _COST_TOL < clean_cost["first_s"]:
        raise ValueError(f"{name}.cost.reserved_s is below first_s")
    if clean_cost["first_s"] <= 0:
        raise ValueError(f"{name}.cost.first_s must be positive")
    return {
        "site_id": _text(cand["site_id"], f"{name}.site_id"),
        "wafer_id": _text(cand["wafer_id"], f"{name}.wafer_id"),
        "x_um": _number(cand["x_um"], f"{name}.x_um"),
        "y_um": _number(cand["y_um"], f"{name}.y_um"),
        "prior_p": prior,
        "optical_observed_at": _text(cand["optical_observed_at"], f"{name}.optical_observed_at"),
        "recipe_id": _text(cand["recipe_id"], f"{name}.recipe_id"),
        "capture_bound_s": _number(cand["capture_bound_s"], f"{name}.capture_bound_s", positive=True),
        "inference_bound_s": _number(cand["inference_bound_s"], f"{name}.inference_bound_s", minimum=0.0),
        "cost": clean_cost,
    }


def _sanitize_history(history):
    """Project paid history rows to (site_id, reported_doi) only.

    A missing or null report stays unknown; it is never counted as negative.
    """
    if not isinstance(history, list):
        raise ValueError("state.history must be a list")
    out = []
    for i, row in enumerate(history):
        if not isinstance(row, dict):
            raise ValueError(f"history[{i}] must be a dict")
        site = row.get("site_id", row.get("selected_id"))
        site = _text(site, f"history[{i}].site_id")
        report = row.get("reported_doi")
        if report is not None and not isinstance(report, bool):
            raise ValueError(f"history[{i}].reported_doi must be bool or null")
        out.append({"site_id": site, "reported_doi": report})
    return out


def sanitize_state(state):
    """Return a whitelisted deep copy of a selector state, or raise ValueError."""
    _exact_keys(state, STATE_KEYS, REQUIRED_STATE_KEYS, "state")
    if not isinstance(state["candidates"], list):
        raise ValueError("state.candidates must be a list")
    candidates = [_sanitize_candidate(c, i) for i, c in enumerate(state["candidates"])]
    ids = [c["site_id"] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("state.candidates has duplicate site_id")
    history = _sanitize_history(state["history"])
    paid = {row["site_id"] for row in history}
    overlap = sorted(paid.intersection(ids))
    if overlap:
        raise ValueError(f"already selected sites offered again: {overlap}")
    return {
        "job_id": _text(state["job_id"], "state.job_id"),
        "candidates": candidates,
        "remaining_s": _number(state["remaining_s"], "state.remaining_s", minimum=0.0),
        "current": _sanitize_current(state["current"]),
        "history": history,
        "cost": _sanitize_job_cost(state.get("cost")),
    }


def history_counts(history):
    """Counts of paid reports; null stays unknown and is never negative."""
    counts = {"reported_positive": 0, "reported_negative": 0, "unknown": 0}
    for row in history:
        if row["reported_doi"] is True:
            counts["reported_positive"] += 1
        elif row["reported_doi"] is False:
            counts["reported_negative"] += 1
        else:
            counts["unknown"] += 1
    return counts


# --------------------------------------------------------------------------- cost model

def _close(a, b):
    return abs(a - b) <= _COST_TOL * max(1.0, abs(a), abs(b))


def _distance(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def project_cost(cand, prev, params):
    """Projected public cost of acting on ``cand`` from position ``prev``.

    Mirrors the contract: load when no wafer is mounted or the wafer changes, Euclidean
    stage movement only on the same wafer, settle, public capture/inference bounds and a
    full retry reserve. ``params['move_um_per_s']`` may be None when it could not be
    inferred; a same-wafer move between different coordinates then raises ValueError
    instead of being projected as zero.
    """
    bound = cand["capture_bound_s"] + cand["inference_bound_s"]
    if prev is None or prev["wafer_id"] != cand["wafer_id"]:
        load, move = params["wafer_load_s"], 0.0
    else:
        load = 0.0
        speed = params["move_um_per_s"]
        dist = _distance(prev["x_um"], prev["y_um"], cand["x_um"], cand["y_um"])
        if dist == 0:
            move = 0.0
        elif speed is None:
            raise ValueError(
                f"cannot project same-wafer movement to {cand['site_id']}: move_um_per_s is unknown")
        else:
            move = dist / speed
    first = load + move + params["settle_s"] + bound
    return {
        "load_s": load,
        "move_s": move,
        "settle_s": params["settle_s"],
        "first_s": first,
        "reserved_s": first + params["retry_limit"] * bound,
    }


def resolve_cost_params(state):
    """Public cost parameters from explicit state cost or inferred from candidate costs.

    Returns (params, source). Explicit parameters are checked against every candidate's
    declared current-relative cost; a mismatch raises instead of silently projecting a
    different cost model. Inferred ``move_um_per_s`` is None (source
    ``inferred_move_rate_unavailable``) when no same-wafer movement is observable.
    """
    candidates, current = state["candidates"], state["current"]
    if state["cost"] is not None:
        params = dict(state["cost"])
        for c in candidates:
            projected = project_cost(c, current, params)
            for key in CANDIDATE_COST_KEYS:
                if not _close(projected[key], c["cost"][key]):
                    raise ValueError(
                        f"candidate {c['site_id']} cost.{key}={c['cost'][key]} disagrees with "
                        f"projection {projected[key]} from state.cost")
        return params, "state_cost"

    params = {"wafer_load_s": None, "settle_s": None, "move_um_per_s": None, "retry_limit": None}
    for c in candidates:
        cost = c["cost"]
        bound = c["capture_bound_s"] + c["inference_bound_s"]
        if params["settle_s"] is None:
            params["settle_s"] = cost["settle_s"]
        elif not _close(params["settle_s"], cost["settle_s"]):
            raise ValueError("candidate costs disagree on settle_s")
        retry = (cost["reserved_s"] - cost["first_s"]) / bound
        retry_int = int(round(retry))
        if retry_int not in (0, 1) or not _close(retry, retry_int):
            raise ValueError(f"candidate {c['site_id']} reserve is not a 0/1 retry multiple")
        if params["retry_limit"] is None:
            params["retry_limit"] = retry_int
        elif params["retry_limit"] != retry_int:
            raise ValueError("candidate costs disagree on retry_limit")
        if current is None or current["wafer_id"] != c["wafer_id"]:
            if params["wafer_load_s"] is None:
                params["wafer_load_s"] = cost["load_s"]
            elif not _close(params["wafer_load_s"], cost["load_s"]):
                raise ValueError("candidate costs disagree on wafer load")
        else:
            dist = _distance(current["x_um"], current["y_um"], c["x_um"], c["y_um"])
            if dist > 0 and cost["move_s"] > 0 and params["move_um_per_s"] is None:
                params["move_um_per_s"] = dist / cost["move_s"]
    if params["settle_s"] is None:
        params["settle_s"] = 0.0
    if params["retry_limit"] is None:
        params["retry_limit"] = 0
    source = "inferred"
    if params["wafer_load_s"] is None:
        # Every candidate is on the mounted wafer, so no projected path can switch wafers.
        params["wafer_load_s"] = 0.0
    if params["move_um_per_s"] is None:
        source = "inferred_move_rate_unavailable"
    return params, source


# --------------------------------------------------------------------------- policies

def _ratio(prior, seconds):
    return prior / seconds if seconds > 0 else 0.0


def _best_by(candidates, key):
    """Deterministic argmax; exact ties break by ascending site_id."""
    return min(candidates, key=lambda c: (-key(c), c["site_id"]))


def _shortlist(affordable, params):
    """Top global risk-per-second sites plus a few movement-independent picks per wafer."""
    ranked = sorted(affordable, key=lambda c: (-_ratio(c["prior_p"], c["cost"]["reserved_s"]), c["site_id"]))
    chosen = {c["site_id"]: c for c in ranked[:SHORTLIST_GLOBAL]}
    by_wafer = {}
    for c in affordable:
        by_wafer.setdefault(c["wafer_id"], []).append(c)
    for wafer in sorted(by_wafer):
        def intrinsic(c):
            bound = c["capture_bound_s"] + c["inference_bound_s"]
            return _ratio(c["prior_p"], params["settle_s"] + (1 + params["retry_limit"]) * bound)
        picks = sorted(by_wafer[wafer], key=lambda c: (-intrinsic(c), c["site_id"]))
        for c in picks[:SHORTLIST_PER_WAFER]:
            chosen.setdefault(c["site_id"], c)
    return sorted(chosen.values(), key=lambda c: c["site_id"])


def _path_key(path):
    """Best ratio first; exact ties use the site-id sequence (frozen contract)."""
    prior, seconds, ids = path["prior"], path["reserved_s"], path["ids"]
    return (-_ratio(prior, seconds), ids)


def beam_plan(state, params, remaining_s):
    """Bounded depth-three beam over a shortlist; returns the best path dict or None.

    Step one uses each candidate's declared current-relative cost. Later steps project
    load/move/settle/bounds from the previous site. Paths are scored by
    sum(prior_p) / sum(projected reserved seconds) and must fit the remaining budget.
    Every feasible prefix generated (depth one to three, extendable or not) stays
    eligible as the best final path; only the set expanded at the next depth is pruned
    to ``BEAM_WIDTH``.
    """
    if params["move_um_per_s"] is None:
        raise ValueError(
            "beam_route needs move_um_per_s: pass state.cost or a current-relative "
            "same-wafer candidate cost from which the stage speed can be inferred")
    affordable = [c for c in state["candidates"] if c["cost"]["reserved_s"] <= remaining_s]
    if not affordable:
        return None, 0
    shortlist = _shortlist(affordable, params)
    beam = [{
        "ids": (c["site_id"],),
        "prior": c["prior_p"],
        "reserved_s": c["cost"]["reserved_s"],
        "last": c,
    } for c in shortlist]
    beam.sort(key=_path_key)
    best = beam[0]
    beam = beam[:BEAM_WIDTH]
    for _depth in range(1, BEAM_DEPTH):
        extended = []
        for path in beam:
            last = path["last"]
            prev = {"wafer_id": last["wafer_id"], "x_um": last["x_um"], "y_um": last["y_um"]}
            for c in shortlist:
                if c["site_id"] in path["ids"]:
                    continue
                step = project_cost(c, prev, params)["reserved_s"]
                total = path["reserved_s"] + step
                if total > remaining_s:
                    continue
                extended.append({
                    "ids": path["ids"] + (c["site_id"],),
                    "prior": path["prior"] + c["prior_p"],
                    "reserved_s": total,
                    "last": c,
                })
        if not extended:
            break
        extended.sort(key=_path_key)
        best = min(best, extended[0], key=_path_key)
        beam = extended[:BEAM_WIDTH]
    return best, len(shortlist)


def _deterministic_best(policy, state, affordable, remaining_s):
    if policy == "risk_only":
        best = _best_by(affordable, lambda c: c["prior_p"])
        return best, {"score": best["prior_p"]}
    if policy == "risk_per_second":
        best = _best_by(affordable, lambda c: _ratio(c["prior_p"], c["cost"]["reserved_s"]))
        return best, {"score": _ratio(best["prior_p"], best["cost"]["reserved_s"])}
    params, source = resolve_cost_params(state)
    path, shortlist_size = beam_plan(state, params, remaining_s)
    by_id = {c["site_id"]: c for c in affordable}
    best = by_id[path["ids"][0]]
    return best, {
        "score": _ratio(path["prior"], path["reserved_s"]),
        "path": list(path["ids"]),
        "path_prior": path["prior"],
        "path_reserved_s": path["reserved_s"],
        "shortlist_size": shortlist_size,
        "cost_source": source,
    }


def select(state, rng, policy="beam_route", audit_epsilon=0.1):
    """Choose the next site or return None when nothing is affordable.

    The returned choice has ``site_id``, ``reason``, ``propensity`` (exact conditional
    selection probability under the epsilon-uniform mixture), ``audit`` and
    ``components``. One ``rng.random()`` draw is consumed per call with at least one
    affordable site, plus one ``rng.randrange`` draw on an audit.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")
    if not isinstance(rng, random.Random):
        raise ValueError("rng must be a random.Random instance")
    epsilon = _number(audit_epsilon, "audit_epsilon", minimum=0.0)
    if epsilon > 1.0:
        raise ValueError("audit_epsilon must be <= 1")
    clean = sanitize_state(state)
    remaining_s = clean["remaining_s"]
    affordable = sorted(
        (c for c in clean["candidates"] if c["cost"]["reserved_s"] <= remaining_s),
        key=lambda c: c["site_id"])
    if not affordable:
        return None
    n = len(affordable)
    best, detail = _deterministic_best(policy, clean, affordable, remaining_s)

    audit = rng.random() < epsilon
    chosen = affordable[rng.randrange(n)] if audit else best
    if n == 1:
        propensity = 1.0
    elif chosen["site_id"] == best["site_id"]:
        propensity = epsilon / n + (1.0 - epsilon)
    else:
        propensity = epsilon / n

    if audit:
        reason = "audit_uniform"
    else:
        reason = f"{policy}_best"
    components = {
        "policy": policy,
        "audit_epsilon": epsilon,
        "n_affordable": n,
        "deterministic_site_id": best["site_id"],
        "prior_p": chosen["prior_p"],
        "first_s": chosen["cost"]["first_s"],
        "reserved_s": chosen["cost"]["reserved_s"],
        "remaining_s": remaining_s,
        "history": history_counts(clean["history"]),
    }
    components.update(detail)
    return {
        "site_id": chosen["site_id"],
        "reason": reason,
        "propensity": propensity,
        "audit": audit,
        "components": components,
    }


def make_selector(policy="beam_route", audit_epsilon=0.1):
    """Return a ``selector(state, rng)`` closure for ``routing_poc.replay.run_loop``."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")

    def selector(state, rng):
        return select(state, rng, policy=policy, audit_epsilon=audit_epsilon)

    selector.policy = policy
    selector.audit_epsilon = audit_epsilon
    return selector
