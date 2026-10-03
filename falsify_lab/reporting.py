"""Posthoc statistics, benchmark report and snapshot JSON v1 (DATA_CONTRACT.md).

Everything here is derived from the campaign ledger and stored evidence. Missing
data stays null or empty; nothing is filled with placeholder or mockup values.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .benchmark import ADAPTIVE, RUNNER, preview_decision
from .model import COEFFICIENT_NAMES, FORM, G_FORM
from .policies import LABELS, idw_predict
from .protocol import HIGH_VDD_MIN, LOW_VDD_MAX, Point, Protocol, full_grid, load_protocol
from .storage import Campaign

PROJECT = "Falsify Lab Adaptive Circuit Experiments"
# Live-loop ledger events written by falsify_lab/mcp_server.py (genuine MCP tool records).
UPDATE_EVENT = "analysis_update"
TOOL_EVENT = "mcp_tool_call"
# Sanitized Omnigent SDK session proof written by scripts/run_live.py inside the demo root.
SESSION_PROOF = Path("omnigent") / "session-proof.json"

BASE_LIMITATIONS = [
    "Generic SPICE Level-1 device cards on one CMOS inverter; not a foundry PDK, FinFET, TCAD or silicon result.",
    "Process conditions are synthetic Vt/KP shifts; their names do not denote foundry corners.",
    "Results are conditional on this circuit, the frozen five-coefficient model, the 175-point grid and the ten fixed starting points.",
    "The ten seeds are replicates of starting-point variation in one computational world, not ten independent device experiments.",
    "IDW scores are a learning-progress heuristic, not probabilities, information gain or calibrated uncertainty.",
    "The policy comparison measures deterministic selection rules; it does not measure LLM intelligence or speed-up versus human researchers.",
    "Posthoc full-grid evaluation cost is reported separately and is not hidden as computational savings.",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- statistics
def paired_bootstrap(diffs: list[float], replicates: int, seed: int, interval: tuple[float, float]) -> list[float]:
    """Percentile bootstrap of the mean paired difference, resampling seed pairs jointly.

    RNG: numpy Generator(PCG64(seed)); indices integers(0, n, size=(replicates, n));
    quantiles with method="linear".
    """
    d = np.asarray(diffs, dtype=float)
    rng = np.random.Generator(np.random.PCG64(seed))
    idx = rng.integers(0, len(d), size=(replicates, len(d)))
    means = d[idx].mean(axis=1)
    lo, hi = np.quantile(means, list(interval), method="linear")
    return [float(lo), float(hi)]


def paired_comparison(
    protocol: Protocol,
    by_seed_a: dict[int, dict[str, Any]],
    by_seed_b: dict[int, dict[str, Any]],
    primary: bool,
) -> dict[str, Any]:
    """Seed-paired difference a - b in unique clear counterexamples; only complete pairs count."""
    pa = protocol.primary_analysis
    need = int(pa["complete_paired_seeds_required"])
    pairs, excluded = [], []
    for rep in protocol.replicates:
        a, b = by_seed_a.get(rep.seed), by_seed_b.get(rep.seed)
        if a is None or b is None:
            excluded.append({"seed": rep.seed, "reason": "run not executed"})
        elif a["status"] != "complete" or b["status"] != "complete":
            excluded.append({"seed": rep.seed, "reason": f"incomplete run(s): {a['status']}/{b['status']}"})
        else:
            pairs.append({"seed": rep.seed, "a": a["clear_count"], "b": b["clear_count"], "difference": a["clear_count"] - b["clear_count"]})
    out: dict[str, Any] = {
        "complete_pairs": len(pairs),
        "paired_differences": pairs,
        "excluded_pairs": excluded,
        "mean_difference": None,
        "ci95": None,
        "reason": None,
    }
    if primary:
        out["success"] = None
    if len(pairs) < need:
        out["reason"] = f"{len(pairs)} of {need} complete seed pairs; criterion not evaluated"
        return out
    diffs = [p["difference"] for p in pairs]
    mean = float(np.mean(diffs))
    ci = paired_bootstrap(diffs, int(pa["bootstrap_replicates"]), int(pa["bootstrap_seed"]), tuple(pa["interval"]))
    out.update({"mean_difference": mean, "ci95": ci, "bootstrap": {"replicates": int(pa["bootstrap_replicates"]), "seed": int(pa["bootstrap_seed"]), "quantile_method": pa["quantile_method"], "rng": "numpy PCG64"}})
    if primary:
        gain = float(pa["practical_mean_gain_min"])
        out["success"] = bool(mean >= gain and ci[0] > 0)
        out["reason"] = f"success requires mean difference >= {gain:g} and CI lower bound > 0"
    return out


# ---------------------------------------------------------------------- run summaries
def run_summary(c: Campaign, run_id: str) -> dict[str, Any]:
    st = c.run_state(run_id)
    search = [q for q in st["queries"] if q["phase"] in ("initial", "search")]
    seen: set[str] = set()
    clear: list[str] = []
    secondary: list[str] = []
    cumulative = []
    for q in search:
        if q["status"] == "success" and q["point_id"] not in seen:
            seen.add(q["point_id"])
            ev = q.get("evaluation") or {}
            if ev.get("clear_counterexample"):
                clear.append(q["point_id"])
            if ev.get("secondary_counterexample"):
                secondary.append(q["point_id"])
        cumulative.append(len(clear))

    def reach(k: int) -> int | None:
        return next((i + 1 for i, v in enumerate(cumulative) if v >= k), None)

    attempts = [e["payload"] for e in c.events("attempt_admitted") if e["payload"].get("run_id") == run_id]
    att_ids = {a["attempt_id"] for a in attempts}
    finished = [e["payload"] for e in c.events("attempt_finished") if e["payload"]["attempt_id"] in att_ids]
    closed = st["closed"]
    return {
        "run_id": run_id,
        "run_kind": st["run_kind"],
        "policy": st["policy"],
        "seed": st["seed"],
        "status": closed["status"] if closed else "open",
        "termination_reason": closed["termination_reason"] if closed else None,
        "model_hash": st["model_hash"],
        "budget": {"limit": st["budget"], "used": st["used"], "remaining": st["remaining"]},
        "clear_count": len(clear),
        "secondary_count": len(secondary),
        "clear_point_ids": clear,
        "cumulative_clear": cumulative,
        "cumulative_clear_area": sum(cumulative),
        "first_clear_query": reach(1),
        "fifth_clear_query": reach(5),
        "query_index_basis": "search queries 1..15 (shared initial 3 + 12 choices); null = not reached",
        "selection_point_ids": [q["point_id"] for q in search],
        "policy_selection_point_ids": [q["point_id"] for q in search if q["phase"] == "search"],
        "failed_point_ids": [q["point_id"] for q in st["queries"] if q["status"] != "success"],
        "cost": {
            "logical_queries": st["used"],
            "physical_attempts": len(attempts),
            "cache_hits": sum(1 for q in st["queries"] if q.get("cache_hit")),
            "repeats": sum(1 for q in st["queries"] if q.get("repeat")),
            "failures": sum(1 for q in st["queries"] if q["status"] != "success"),
            "simulation_seconds": round(sum(float(f.get("wall_time_s") or 0) for f in finished), 6),
            "policy_seconds": round(sum(float(d.get("policy_seconds") or 0) for d in st["decisions"]), 6),
        },
    }


def _final_evidence(c: Campaign, run_id: str) -> list[tuple[Point, float]]:
    seen, out = set(), []
    for q in c.run_state(run_id)["queries"]:
        if q["status"] == "success" and q["point_id"] not in seen:
            seen.add(q["point_id"])
            out.append((Point.parse(q["point_id"]), q["evaluation"]["abs_relative_error"]))
    return out


def posthoc_truth(c: Campaign) -> dict[str, dict[str, Any]]:
    return {e["payload"]["point_id"]: e["payload"] for e in c.events("posthoc_result") if e["payload"]["status"] == "success"}


def _err_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errs = [r["evaluation"]["abs_relative_error"] for r in rows]
    return {
        "points": len(rows),
        "clear_counterexamples": sum(r["evaluation"]["clear_counterexample"] for r in rows),
        "secondary_counterexamples": sum(r["evaluation"]["secondary_counterexample"] for r in rows),
        "mean_abs_relative_error": float(np.mean(errs)) if errs else None,
        "max_abs_relative_error": float(np.max(errs)) if errs else None,
    }


def benchmark_report(c: Campaign) -> dict[str, Any] | None:
    started = c.events("benchmark_started")
    if not started:
        return None
    proto = c.protocol
    finished = c.events("benchmark_finished")
    planned = started[0]["payload"]
    summaries = {rid: run_summary(c, rid) for rid in c.run_ids("benchmark")}
    policies = []
    by_policy: dict[str, dict[int, dict[str, Any]]] = {}
    for pol in proto.policies:
        runs = [s for s in summaries.values() if s["policy"] == pol]
        runs.sort(key=lambda s: s["seed"])
        by_policy[pol] = {s["seed"]: s for s in runs}
        complete = [s["cumulative_clear"] for s in runs if s["status"] == "complete"]
        policies.append({
            "id": pol,
            "label": LABELS[pol],
            "runs": runs,
            "complete_runs": len(complete),
            "mean_cumulative_clear": [float(x) for x in np.mean(complete, axis=0)] if complete else None,
        })
    primary = paired_comparison(proto, by_policy[ADAPTIVE], by_policy["random"], primary=True)
    primary["comparison"] = "adaptive_idw_plus_distance minus random"
    secondary = []
    for other in ("space_filling", "idw_without_distance"):
        comp = paired_comparison(proto, by_policy[ADAPTIVE], by_policy[other], primary=False)
        comp["comparison"] = f"adaptive_idw_plus_distance minus {other}"
        secondary.append(comp)

    truth = posthoc_truth(c)
    held_out = None
    reference = None
    if truth:
        held_rows = [truth[p.id] for p in proto.held_out if p.id in truth]
        idw_maps = []
        for s in summaries.values():
            if s["status"] == "open":
                continue
            ev = _final_evidence(c, s["run_id"])
            diffs = [abs(idw_predict(Point.parse(r["point_id"]), ev) - r["evaluation"]["abs_relative_error"]) for r in held_rows]
            idw_maps.append({"run_id": s["run_id"], "policy": s["policy"], "seed": s["seed"], "status": s["status"], "idw_map_mae": float(np.mean(diffs)) if diffs else None})
        held_out = {
            "model_errors": _err_stats(held_rows),
            "missing_points": [p.id for p in proto.held_out if p.id not in truth],
            "idw_error_map_mae": idw_maps,
            "idw_error_map_mae_by_policy": {
                pol: (float(np.mean(v)) if (v := [m["idw_map_mae"] for m in idw_maps if m["policy"] == pol and m["status"] == "complete" and m["idw_map_mae"] is not None]) else None)
                for pol in proto.policies
            },
            "note": "IDW error-map accuracy on held-out points; not a retraining of the delay model.",
        }
        rows = list(truth.values())
        reference = {
            "points_evaluated": len(rows),
            "missing_points": [p.id for p in full_grid() if p.id not in truth],
            "by_partition": {part: _err_stats([r for r in rows if r.get("partition") == part]) for part in ("training", "search_pool", "held_out", "prior_excluded")},
            "low_vdd": _err_stats([r for r in rows if Point.parse(r["point_id"]).vdd <= LOW_VDD_MAX]),
            "high_vdd": _err_stats([r for r in rows if Point.parse(r["point_id"]).vdd >= HIGH_VDD_MIN]),
            "h2_note": "Exploratory full-grid comparison (VDD <= 1.8 V vs >= 2.9 V); not a confirmatory test.",
        }
    cost = c.cost()
    posthoc_attempts = cost["attempts_by_purpose"].get("posthoc", 0)
    fin = finished[-1]["payload"] if finished else None
    return {
        "status": fin["status"] if fin else "running",
        "reason": fin["reason"] if fin else None,
        "planned_runs": planned["planned_runs"],
        "not_run": fin["not_run"] if fin else [r for r in planned["planned_runs"] if r not in summaries],
        "policies": policies,
        "primary": primary,
        "secondary_comparisons": secondary,
        "held_out": held_out,
        "reference": reference,
        "postflight": (c.events("postflight_completed") or [{"payload": None}])[-1]["payload"],
        "cost": {**cost, "posthoc_attempts_separate_from_research_budget": posthoc_attempts},
        "limitations": [
            "Primary success is judged only with ten complete adaptive/random seed pairs; otherwise it is null.",
            "Unreached first/fifth counterexamples are null, never zero or success.",
        ],
    }


# ---------------------------------------------------------------------- snapshot
def _focus_run(c: Campaign) -> str | None:
    ids = c.run_ids()
    for prefix in ("live-", f"benchmark-{ADAPTIVE}-1001", f"reproduce-{ADAPTIVE}-1001"):
        hit = [r for r in ids if r.startswith(prefix)]
        if hit:
            return hit[0]
    return "calibration" if "calibration" in ids else None


def _kind_limitations(c: Campaign) -> list[str]:
    k = c.kind
    if k == "fixture":
        return ["NON-SCIENTIFIC FIXTURE DATA: produced by a synthetic test simulator, not ngspice."]
    if k == "development":
        return ["Development validation campaign: labelled development, never benchmark results."]
    if k == "reproduce":
        return ["Short reproduction check isolated from the primary campaign; not primary evidence."]
    if k == "demo":
        return ["Prepared live-demonstration campaign (seed 1001); not the policy benchmark."]
    return []


def empty_snapshot(protocol: Protocol | None = None, note: str = "No stored evidence at this root.") -> dict[str, Any]:
    protocol = protocol or load_protocol()
    return {
        "schema_version": 1,
        "data_mode": "real",
        "generated_at": _now(),
        "project": PROJECT,
        "protocol_hash": protocol.protocol_sha256,
        "manifest_hash": protocol.manifest_sha256,
        "limitations": [note, *BASE_LIMITATIONS],
        "campaign": None,
        "run": None,
        "model": None,
        "observations": [],
        "evaluations": [],
        "decisions": [],
        "trace": [],
        "coverage": [],
        "benchmark": None,
        "cost": None,
        "preflight": None,
        "next_candidates": [],
        "updates": [],
        "omnigent_session": None,
    }


def _session_proof(c: Campaign, focus: str | None) -> dict[str, Any] | None:
    """The sanitized SDK session proof for the focus live run, as recorded by the runner (never synthesized)."""
    path = c.root / SESSION_PROOF
    if focus is None or not path.is_file():
        return None
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(proof, dict) or proof.get("run_id") != focus:
        return None
    return {"source": "scripts/run_live.py omnigent_client session transcript (sanitized)", **proof}


def build_snapshot(c: Campaign, focus_run: str | None = None) -> dict[str, Any]:
    proto = c.protocol
    snap = empty_snapshot(proto)
    snap["data_mode"] = "fixture" if c.kind == "fixture" else "real"
    snap["limitations"] = [*_kind_limitations(c), *BASE_LIMITATIONS]
    head = c.events()[-1]
    snap["campaign"] = {
        "kind": c.kind,
        "label": c.info["label"],
        "scientific": c.info["scientific"],
        "simulator": c.info["simulator"],
        "created_at": c.events()[0]["ts"],
        "ledger_events": head["seq"],
        "ledger_head_hash": head["hash"],
        "manifest_sha256": c.info["manifest_sha256"],
    }
    m = c.model()
    if m is not None:
        snap["model"] = {
            "model_hash": m.model_hash,
            "coefficients": dict(zip(COEFFICIENT_NAMES, m.coefficients)),
            "calibration_result_ids": [r["result_id"] for r in m.calibration],
            "form": f"{FORM}; {G_FORM}",
            "design_rank": m.rank,
        }
    pf = c.events("preflight_completed")
    if pf:
        snap["preflight"] = pf[-1]["payload"]  # relative differences only; no restricted delays
    focus = focus_run or _focus_run(c)
    queried: dict[str, dict[str, Any]] = {}
    if focus is not None:
        s = run_summary(c, focus)
        st = c.run_state(focus)
        snap["run"] = {
            "run_id": focus, "kind": st["run_kind"], "policy": st["policy"], "seed": st["seed"], "status": s["status"],
            "model_hash": st["model_hash"], "budget": s["budget"], "termination_reason": s["termination_reason"],
            "clear_count": s["clear_count"], "secondary_count": s["secondary_count"],
        }
        result_by_decision = {}
        updates = [e["payload"] for e in c.events(UPDATE_EVENT) if e["payload"].get("run_id") == focus]
        update_by_result = {u["observed_result_id"]: u for u in updates}
        snap["updates"] = updates
        for q in st["queries"]:
            queried.setdefault(q["point_id"], q)
            if q.get("decision_sequence"):
                result_by_decision[q["decision_sequence"]] = q.get("result_id")
            if q["status"] != "success":
                continue
            obs = c.get_observation(focus, q["result_id"])
            snap["observations"].append({
                "result_id": q["result_id"], "point_id": q["point_id"], "pvt": obs["pvt"], "phase": q["phase"],
                "tphl_s": obs["tphl_s"], "tplh_s": obs["tplh_s"], "tpd_s": obs["tpd_s"], "cache_hit": q["cache_hit"],
                "wall_time_s": obs["wall_time_s"],
                "provenance": {k: obs["provenance"][k] for k in ("netlist_sha256", "simulator", "raw_meas_lines", "sim_id", "setting")},
            })
            if q.get("evaluation"):
                ev = q["evaluation"]
                snap["evaluations"].append({"result_id": q["result_id"], "point_id": q["point_id"], "pvt": obs["pvt"], **ev})
        for d in st["decisions"]:
            observed = result_by_decision.get(d["sequence"])
            # rank_before/rank_after keep their recorded meaning (impact of the previous
            # observation on this choice); the update after this decision's own result is
            # attached separately, only when a real analysis_update exists.
            upd = update_by_result.get(observed) if observed else None
            snap["decisions"].append({
                "sequence": d["sequence"], "evidence_result_ids": d["evidence_result_ids"], "candidates": d["candidates"],
                "selected_point_id": d["selected_point_id"], "remaining_budget": d["remaining_budget"],
                "rank_before": d["rank_before"], "rank_after": d["rank_after"], "selection_changed": d["selection_changed"],
                "observed_result_id": observed, "actor": d.get("actor", RUNNER),
                "policy_seconds": d.get("policy_seconds"),
                "post_result_update": None if upd is None else {
                    "observed_result_id": upd["observed_result_id"], "rank_before": upd["rank_before"],
                    "rank_after": upd["rank_after"], "selection_changed": upd["selection_changed"],
                    "next_candidates": upd["next_candidates"], "update_index": upd["update_index"],
                    "next_decision": upd["next_decision"], "actor": upd["actor"],
                },
            })
        preview = preview_decision(c, focus)
        snap["next_candidates"] = preview["candidates"] if preview else []
        snap["evaluations_note"] = "Evaluations of the focus run only; held-out/full-grid results appear under benchmark after posthoc."
    camp_types = {"campaign_created", "model_frozen", "preflight_completed", "benchmark_started", "benchmark_finished",
                  "posthoc_started", "posthoc_completed", "postflight_completed", "cap_reached", "live_prepared", "reproduce_completed"}
    run_types = {"run_opened", "query_result", "decision", "run_closed", UPDATE_EVENT, TOOL_EVENT}
    # trace.sequence is the global ledger number; decision_sequence/query_index are run-local.
    decision_by_query: dict[int, int] = {}
    tool_by_query: dict[int, dict[str, Any]] = {}
    if focus is not None:
        decision_by_query = {q["query_index"]: q["decision_sequence"] for q in c.run_state(focus)["queries"] if q.get("decision_sequence")}
        tool_by_query = {e["payload"]["query_index"]: e["payload"] for e in c.events(TOOL_EVENT)
                         if e["payload"].get("run_id") == focus and e["payload"].get("query_index")}
    for e in c.events():
        p = e["payload"]
        if e["type"] in camp_types or (focus is not None and p.get("run_id") == focus and e["type"] in run_types):
            rids = [x for x in [p.get("result_id"), p.get("observed_result_id"), *p.get("result_ids", [])] if x]
            rids = list(dict.fromkeys(rids))
            detail = {k: p[k] for k in ("phase", "point_id", "status", "selected_point_id", "reason", "termination_reason",
                                        "query_index", "decision_sequence", "tool", "finalize", "update_index",
                                        "selection_changed", "next_selected_point_id", "next_decision") if k in p}
            if e["type"] == "decision":
                detail["decision_sequence"] = p["sequence"]
            actor = p.get("actor", RUNNER)
            if e["type"] == "query_result":
                if p["query_index"] in decision_by_query:
                    detail["decision_sequence"] = decision_by_query[p["query_index"]]
                tool = tool_by_query.get(p["query_index"])
                if tool is not None:  # executed by a recorded MCP tool call, not the runner
                    actor, detail["via_tool"] = tool["actor"], tool["tool"]
            snap["trace"].append({"sequence": e["seq"], "actor": actor, "action": e["type"], "result_ids": rids, "detail": detail, "timestamp": e["ts"]})
    proof = _session_proof(c, focus)
    if proof is not None:
        snap["omnigent_session"] = proof
    truth = posthoc_truth(c)
    for p in full_grid():
        part = proto.partition(p)
        q = queried.get(p.id)
        row = {"point_id": p.id, "pvt": p.pvt, "partition": part, "state": "unobserved",
               "predicted_tpd_s": m.predict_tpd_s(p) if m else None, "observed_tpd_s": None,
               "abs_relative_error": None, "result_id": None, "truth_source": None}
        if part == "training":
            row["state"] = "calibration"
        elif part == "held_out":
            row["state"] = "held_out"
        if q is not None and q["status"] != "success" and part != "training":
            row["state"] = "failed"
        elif q is not None and q["status"] == "success":
            if part != "training":
                row["state"] = "observed"
            row.update({"observed_tpd_s": q["observation"]["tpd_s"], "result_id": q["result_id"], "truth_source": "run",
                        "abs_relative_error": (q.get("evaluation") or {}).get("abs_relative_error")})
        elif p.id in truth:
            t = truth[p.id]
            row.update({"observed_tpd_s": t["tpd_s"], "result_id": t["sim_id"], "truth_source": "posthoc",
                        "abs_relative_error": t["evaluation"]["abs_relative_error"]})
        snap["coverage"].append(row)
    snap["benchmark"] = benchmark_report(c)
    snap["cost"] = c.cost()
    return snap


def export_snapshot(root: Path | str, focus_run: str | None = None) -> tuple[Path, dict[str, Any]]:
    root = Path(root)
    events = root / "ledger" / "events.jsonl"
    if events.exists() and events.stat().st_size > 0:
        snap = build_snapshot(Campaign(root, None), focus_run)
    else:
        snap = empty_snapshot()
    root.mkdir(parents=True, exist_ok=True)
    out = root / "snapshot.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8", newline="\n")
    tmp.replace(out)
    return out, snap
