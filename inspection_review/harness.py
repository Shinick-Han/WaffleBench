"""Budgeted review-selection loop, cost admission and post-hoc evaluation.

Authority boundary: ``run_selection`` holds the oracle only to hand it to the review
simulator for the exact site/attempt the policy selected. Policies receive
``SelectionState`` (public whitelist + past selected reviews). ``evaluate_run`` reads
the oracle only after the selection loop has finished.

Cost (synthetic equipment cost units, never seconds or currency) for site i:
  wafer_load        if no wafer is loaded or wafer[i] differs (each switch, revisits too)
  stage_base        every site, including the first site and a wafer switch
  stage_per_normalized_distance * |xy_i - xy_current|   movement on the loaded wafer only
                    (movement is zero on the first site and on a wafer switch)
  dwell             every first attempt, also when it fails or is missing
  outside_rescan    when i is outside the original optical candidates
  retry_dwell * retry_limit is reserved at admission; the retry is charged only when
  the first attempt fails or is missing, whatever the retry outcome.
Admission never reads the review outcome; spend never exceeds the budget.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import numpy as np

from .policies import PolicyInput, SelectionState, make_policy, policy_seed

EPS = 1e-9


def cost_vectors(state: SelectionState, cost: dict) -> dict[str, np.ndarray]:
    v = state.view
    n = v.n
    if state.current_wafer is None:
        switch = np.ones(n, dtype=bool)
        dist = np.zeros(n)
    else:
        switch = v.wafer != state.current_wafer
        dist = np.linalg.norm(v.xy - state.current_xy, axis=1)
    load = np.where(switch, float(cost["wafer_load"]), 0.0)
    stage = float(cost["stage_base"]) + float(cost["stage_per_normalized_distance"]) * np.where(switch, 0.0, dist)
    dwell = np.full(n, float(cost["dwell"]))
    outside = np.where(v.candidate, 0.0, float(cost["outside_rescan"]))
    reserve = np.full(n, float(cost["retry_dwell"]) * int(cost["retry_limit"]))
    first = load + stage + dwell + outside
    return {"load": load, "stage": stage, "dwell": dwell, "outside": outside, "first": first,
            "max": first + reserve}


def _success(obs: dict) -> bool:
    return obs.get("status") == "ok" and obs.get("reported_doi") is not None


def _clean_obs(obs: dict, attempt: int) -> dict:
    rd = obs.get("reported_doi")
    q = obs.get("quality")
    return {"attempt": attempt, "status": str(obs.get("status")),
            "reported_doi": None if rd is None else bool(rd),
            "reported_kind": None if obs.get("reported_kind") is None else str(obs.get("reported_kind")),
            "quality": None if q is None else float(q)}


def run_selection(view: PolicyInput, review: Callable[[int, int], dict], *, policy_name: str, mode: str,
                  budget: float, frozen_model: dict, frozen_p: np.ndarray, model_api: Any, config: dict) -> dict:
    """One independent run for one lot/policy/mode/budget. ``review(i, attempt)`` is the
    simulator closure over the hidden oracle; it is the only path to review outcomes."""
    run_cpu_start, run_wall_start = time.process_time(), time.perf_counter()
    cost = config["cost"]
    sel = config["selection"]
    mcfg = config["model"]
    budget = float(budget)
    retry_limit = int(cost["retry_limit"])
    policy = make_policy(policy_name, policy_seed(config.get("study_id", ""), policy_name, view.lot_id))
    state = SelectionState(view, frozen_p, frozen_model, frozen_p, mode, sel, mcfg["classification_threshold"])
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
            if policy.updates_model:
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
            "timing": timing, "online_model_hash": model_api.hash_model(state.online_model)}


def _py(x: Any) -> Any:
    return x.item() if isinstance(x, np.generic) else x


def _ratio(a: float, b: float) -> float | None:
    return None if b == 0 else float(a) / float(b)


def evaluate_run(run: dict, oracle: dict, candidate: np.ndarray, frozen_p: np.ndarray, config: dict) -> dict:
    """Post-hoc metrics. Called only after ``run_selection`` returned."""
    mcfg = config["model"]
    thr, conf = float(mcfg["classification_threshold"]), float(mcfg["confident_negative_threshold"])
    doi = np.asarray(oracle["doi"], bool)
    kind = np.asarray(oracle["kind"]).astype(str)
    elec = np.asarray(oracle["electrical_effect"], bool)
    candidate = np.asarray(candidate, bool)
    rows = run["rows"]
    idx = np.array([r["site_index"] for r in rows], dtype=int)
    rep_pos = np.array([r["reported_positive"] for r in rows], dtype=bool)
    resolved = np.array([r["label"] is not None for r in rows], dtype=bool)
    true_conf = doi[idx] & rep_pos if rows else np.zeros(0, bool)
    conf_idx = idx[true_conf]
    n_attempts = sum(len(r["attempts"]) for r in rows)
    statuses = [a["status"] for r in rows for a in r["attempts"]]
    failures = sum(s in ("failure", "failed", "error") for s in statuses)
    missing = sum(1 for r in rows for a in r["attempts"] if a["status"] == "ok" and a["reported_doi"] is None) + \
        sum(s not in ("ok", "failure", "failed", "error") for s in statuses)
    cand_doi, all_doi = int((doi & candidate).sum()), int(doi.sum())
    in_cand = candidate[conf_idx]
    per_kind = {}
    for k in sorted(set(kind[doi].tolist())):
        total = int((doi & (kind == k)).sum())
        per_kind[k] = {"confirmed": int((kind[conf_idx] == k).sum()), "total": total,
                       "capture": _ratio(int((kind[conf_idx] == k).sum()), total)}
    novel_ident_cost = novel_encounter_cost = None
    for r, tc in zip(rows, true_conf):
        true_novel = bool(doi[r["site_index"]] and kind[r["site_index"]] == "novel")
        if true_novel and novel_encounter_cost is None:
            novel_encounter_cost = r["cumulative_spend"]
        reported_novel = any(a["status"] == "ok" and a["reported_doi"] and a["reported_kind"] == "novel"
                             for a in r["attempts"])
        if tc and true_novel and reported_novel and novel_ident_cost is None:
            novel_ident_cost = r["cumulative_spend"]
    truly_confirmed_spend = [r["cumulative_spend"] for r, tc in zip(rows, true_conf) if tc]
    audits = [r for r in rows if r["reason"] == "scheduled_frozen_negative_audit"]
    audit_true = [r for r in audits if doi[r["site_index"]] and r["reported_positive"]]
    frozen_neg = np.asarray(frozen_p) < thr
    frozen_conf = np.asarray(frozen_p) < conf
    cost_tot = {k: float(sum(r["cost"][k] for r in rows)) for k in ("load", "stage", "dwell", "outside_rescan", "retry")}
    visited = np.zeros(len(doi), bool)
    visited[idx] = True
    return {
        "review_reported_positives": int(rep_pos.sum()),
        "true_doi_confirmed": int(true_conf.sum()),
        "reported_false_positives": int((rep_pos & ~doi[idx]).sum()) if rows else 0,
        "reviewed_true_doi_missed": int((doi[idx] & resolved & ~rep_pos).sum()) if rows else 0,
        "unresolved_sites": int((~resolved).sum()),
        "unresolved_true_doi": int((doi[idx] & ~resolved).sum()) if rows else 0,
        "candidate_doi": cand_doi, "all_doi": all_doi,
        "candidate_detection_ceiling": _ratio(cand_doi, all_doi),
        "candidate_capture": _ratio(int(in_cand.sum()), cand_doi),
        "confirmed_true_doi_in_candidates": int(in_cand.sum()),
        "confirmed_true_doi_outside_candidates": int((~in_cand).sum()),
        "allsite_recall": _ratio(int(true_conf.sum()), all_doi),
        "discovered_frozen_false_negatives": int((frozen_neg[conf_idx] & in_cand).sum()),
        "discovered_frozen_confident_false_negatives": int((frozen_conf[conf_idx] & in_cand).sum()),
        "oracle_full_map_frozen_false_negatives_candidates": int((doi & candidate & frozen_neg).sum()),
        "oracle_full_map_frozen_confident_false_negatives_candidates": int((doi & candidate & frozen_conf).sum()),
        "per_kind_capture": per_kind,
        "first_novel_identification_cost": novel_ident_cost,
        "first_novel_identification_note": "true novel DOI AND paid review reported DOI with kind novel",
        "true_novel_doi_first_encounter_cost": novel_encounter_cost,
        "true_novel_doi_first_encounter_note": "oracle posthoc: first reviewed site that is a true novel DOI, any outcome",
        "true_confirmation_cumulative_spend": truly_confirmed_spend,
        "confirmed_electrical_potential": int(elec[conf_idx].sum()),
        "confirmed_electrical_potential_note": "posthoc simulated electrical_effect flag; not measured yield",
        "audits": len(audits),
        "audits_confident_negative": sum(r["frozen_confident_negative"] for r in audits),
        "audit_true_doi_confirmed": len(audit_true),
        "attempted_sites": len(rows), "attempts": n_attempts, "failures": failures, "missing": missing,
        "retries": sum(len(r["attempts"]) > 1 for r in rows),
        "cost_totals": cost_tot, "spent": run["spent"], "remaining": run["remaining"], "budget": run["budget"],
        "stop_reason": run["stop_reason"],
        "unvisited_sites": int((~visited).sum()), "unvisited_true_doi": int((doi & ~visited).sum()),
        "timing": run["timing"],
    }
