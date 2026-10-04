"""Route v5 selection loop: a scoped copy of ``inspection_v3.harness.run_selection`` with an
injected ``policy_factory`` (default: the local route v5 factory). No monkeypatching and no
change to frozen v1-v4 source.

Invariants preserved from v3, explicitly: v1 ``cost_vectors`` reservation (load, stage, dwell,
outside rescan, full retry reserve); ``affordable = allowed & spent + max <= budget``;
``state.remaining_budget`` set before every decision; first attempt then at most
``retry_limit`` retries only after a failed/missing first attempt, charged ``retry_dwell``;
charged <= reserved and spend <= budget enforced; ``record_visit`` on every attempted site;
only successful reports become labels; online updates only for policies that declare them
(none here); identical row schema. The ``review`` closure is the only path to outcomes.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import numpy as np

from inspection_review.harness import cost_vectors, _success, _clean_obs, _py, EPS
from inspection_review.policies import PolicyInput, SelectionState, policy_seed
from .planner import make_policy as local_policy_factory


def run_selection(view: PolicyInput, review: Callable[[int, int], dict], *, policy_name: str, mode: str,
                  budget: float, frozen_model: dict, frozen_p: np.ndarray, model_api: Any, config: dict,
                  selection_reward: np.ndarray | None = None,
                  policy_factory: Callable[[str, int, dict], Any] = local_policy_factory) -> dict:
    run_cpu_start, run_wall_start = time.process_time(), time.perf_counter()
    cost = config["cost"]
    sel = config["selection"]
    mcfg = config["model"]
    budget = float(budget)
    retry_limit = int(cost["retry_limit"])
    policy = policy_factory(policy_name, policy_seed(config.get("study_id", ""), policy_name, view.lot_id), config)
    state = SelectionState(view, frozen_p, frozen_model, frozen_p, mode, sel, mcfg["classification_threshold"])
    if selection_reward is not None:
        reward = np.array(selection_reward, dtype=float, copy=True)
        if reward.shape != (view.n,) or not np.isfinite(reward).all() or ((reward < 0) | (reward > 1)).any():
            raise ValueError("selection_reward must be a finite probability for every site")
        reward.flags.writeable = False
        state.selection_reward = reward
    rows: list[dict] = []
    spent = 0.0
    stop = None
    timing = {"policy_cpu_s": 0.0, "policy_wall_s": 0.0, "model_update_cpu_s": 0.0, "model_update_wall_s": 0.0}
    while True:
        allowed = state.allowed()
        if not allowed.any():
            stop = "pool_exhausted"
            break
        cv = cost_vectors(state, cost)
        affordable = allowed & (spent + cv["max"] <= budget + EPS)
        if not affordable.any():
            stop = "none_affordable"
            break
        state.remaining_budget = budget - spent
        c0, w0 = time.process_time(), time.perf_counter()
        choice = policy.select(state, cv["max"], affordable)
        pc, pw = time.process_time() - c0, time.perf_counter() - w0
        timing["policy_cpu_s"] += pc
        timing["policy_wall_s"] += pw
        i = choice.index
        if not affordable[i]:
            raise RuntimeError(f"policy {policy_name} chose unaffordable or disallowed site {i}")
        online_p_at_choice = float(state.online_p[i])
        breakdown = {"load": float(cv["load"][i]), "stage": float(cv["stage"][i]), "dwell": float(cv["dwell"][i]),
                     "outside_rescan": float(cv["outside"][i]), "retry": 0.0}
        reserved = float(cv["max"][i])
        attempts = [_clean_obs(review(i, 0), 0)]
        if not _success(attempts[0]) and retry_limit >= 1:
            attempts.append(_clean_obs(review(i, 1), 1))
            breakdown["retry"] = float(cost["retry_dwell"])
        charged = sum(breakdown.values())
        if charged > reserved + EPS:
            raise RuntimeError("charged cost exceeded reservation")
        spent += charged
        if spent > budget + EPS:
            raise RuntimeError("spend exceeded budget")
        state.record_visit(i)
        ok = [a for a in attempts if _success(a)]
        label = ok[-1]["reported_doi"] if ok else None
        mu_c = mu_w = 0.0
        if label is not None:
            state.record_label(i, label)
            if policy.updates_model and frozen_model.get("supports_online_update", True):
                c0, w0 = time.process_time(), time.perf_counter()
                new_model = model_api.update_model(state.online_model, view.features_imputed[i], bool(label), config)
                state.set_online(new_model, np.asarray(model_api.predict(new_model, view.features_imputed), float))
                mu_c, mu_w = time.process_time() - c0, time.perf_counter() - w0
                timing["model_update_cpu_s"] += mu_c
                timing["model_update_wall_s"] += mu_w
        rows.append({
            "step": len(rows) + 1, "site_index": int(i), "site_id": view.site_ids[i], "wafer": _py(view.wafer[i]),
            "original_candidate": bool(view.candidate[i]), "reason": choice.reason, "score": choice.score,
            "components": choice.components, "evidence_refs": choice.evidence,
            "selection_reward": float(state.selection_reward[i]) if selection_reward is not None else float(state.frozen_p[i]),
            "selection_reward_semantics": "reported_review_positive" if selection_reward is not None else "latent_doi_probability",
            "baseline_p": float(state.frozen_p[i]), "online_p": online_p_at_choice,
            "frozen_negative": bool(state.frozen_p[i] < mcfg["classification_threshold"]),
            "frozen_confident_negative": bool(state.frozen_p[i] < mcfg["confident_negative_threshold"]),
            "reserved_cost": reserved, "cost": breakdown, "charged": charged, "cumulative_spend": spent,
            "attempts": attempts, "status": "ok" if ok else attempts[-1]["status"],
            "label": label, "reported_positive": bool(any(a["reported_doi"] for a in ok)),
            "policy_cpu_s": pc, "policy_wall_s": pw, "model_update_cpu_s": mu_c, "model_update_wall_s": mu_w,
        })
    timing["run_total_cpu_s"] = time.process_time() - run_cpu_start
    timing["run_total_wall_s"] = time.perf_counter() - run_wall_start
    return {"rows": rows, "spent": spent, "remaining": budget - spent, "budget": budget, "stop_reason": stop,
            "timing": timing, "online_model_hash": model_api.hash_model(state.online_model),
            "online_updates_enabled": bool(policy.updates_model and frozen_model.get("supports_online_update", True))}
