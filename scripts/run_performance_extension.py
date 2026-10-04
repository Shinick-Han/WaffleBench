"""Prospective load-sensitivity execution of a post-M5 exploratory policy.

Fresh campaign roots only. Nothing reads primary results or refits during search.
Primary policy definitions, manifest and published primary records remain intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from falsify_lab.benchmark import calibrate, preflight
from falsify_lab.exploratory import (LoadSimulator, kernel_candidates, LENGTH_SCALES,
                                     RIDGE, DISPERSION_WEIGHT)
from falsify_lab.policies import rank
from falsify_lab.protocol import load_protocol, canonical_json, Point
from falsify_lab.storage import Campaign

POLICIES = ("adaptive_idw_plus_distance", "random", "kernel_ucb_exploratory")
SEEDS = tuple(range(2001, 2021))
LOADS = (20, 8, 35)
ATTEMPT_CAP, WALL_CAP = 1000, 1500


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def order(points: list[Point], seed: int, purpose: str) -> list[Point]:
    return sorted(points, key=lambda p: (hashlib.sha256(
        f"falsify-extension-v1|{purpose}|{seed}|{p.id}".encode()).hexdigest(), p))


def extension_cost(c: Campaign) -> dict:
    """Count the extension's own events as well as the separate model fit."""
    cost = c.cost()
    queries = c.events("extension_query_admitted")
    results = c.events("extension_query_result")
    decisions = c.events("extension_decision")
    cost["logical_queries"] += len(queries)
    cost["cache_hits"] += sum(bool(e["payload"].get("cache_hit")) for e in results)
    cost["policy_seconds"] = round(cost["policy_seconds"] + sum(
        float(e["payload"]["policy_seconds"]) for e in decisions), 6)
    cost["scope"] = "Separate model fit plus all extension runs; physical attempts also include numerical checks"
    return cost


def run(root: Path) -> dict:
    if root.exists():
        raise ValueError("A fresh output directory is required")
    root.mkdir(parents=True)
    proto = load_protocol()
    plan = {"scope": "Exploratory post-M5 extension; never replaces the frozen primary result",
            "known_before_design": "Original 20f M5 outcomes were seen; 8f and 35f loads were not evaluated before this extension plan",
            "load_ff": LOADS, "seeds": SEEDS, "policies": POLICIES,
            "kernel": {"length_scales": LENGTH_SCALES, "ridge": RIDGE,
                       "dispersion_weight": DISPERSION_WEIGHT, "calibrated_uncertainty": False},
            "budget_per_run": {"calibration": 9, "initial": 3, "subsequent": 12, "total": 24},
            "initial_order": "SHA256 falsify-extension-v1|initial|seed|point_id, first three search points",
            "random_order": "SHA256 falsify-extension-v1|random|seed|point_id",
            "clear_threshold_strict": proto.clear_threshold,
            "grid_and_split_manifest_sha256": proto.manifest_sha256,
            "protocol_sha256": proto.protocol_sha256,
            "loads_override_only": "Each world recalibrates the same five-coefficient form at the same nine points once; model then frozen; no held-out truth queried",
            "postflight": "After all searches in each load, top three kernel seed-2001 search errors rechecked at half-step and tight settings; 0.5% bound",
            "global_physical_attempt_cap": ATTEMPT_CAP, "global_wall_cap_seconds": WALL_CAP,
            "source_sha256": {f: hashlib.sha256((ROOT/f).read_bytes()).hexdigest()
                              for f in ["falsify_lab/exploratory.py", "scripts/run_performance_extension.py", "falsify_lab/policies.py"]}}
    plan_hash = hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    write(root / "plan.json", {"plan_hash": plan_hash, **plan})  # before any simulator call
    started = time.monotonic()
    campaigns, worlds = [], []
    report = {"plan_hash": plan_hash, "status": "running", "primary_result_replaced": False, "worlds": worlds}

    def guard() -> None:
        if time.monotonic()-started >= WALL_CAP or sum(c.cost()["attempts"] for c in campaigns) >= ATTEMPT_CAP:
            raise RuntimeError("Supplementary global cap reached")

    try:
        for load in LOADS:
            guard()
            remaining_attempts = ATTEMPT_CAP - sum(c.cost()["attempts"] for c in campaigns)
            c = Campaign.create(root / f"load-{load}f", "development", LoadSimulator(load),
                                caps={"attempts_max": remaining_attempts, "wall_seconds_max": WALL_CAP-(time.monotonic()-started)},
                                label=f"Post-M5 exploratory load={load}f; plan {plan_hash}")
            campaigns.append(c)
            c.append("extension_plan", {"plan_hash": plan_hash, "load_ff": load, "policy_ids": POLICIES,
                                         "seeds": SEEDS, "primary_result_replaced": False})
            if not preflight(c)["passed"]:
                raise RuntimeError(f"Numerical preflight failed for {load}f")
            model = calibrate(c)
            w = {"load_ff": load, "model_hash": model.model_hash, "runs": []}
            worlds.append(w)
            for seed in SEEDS:
                initial = order(list(proto.search_pool), seed, "initial")[:3]
                random_order = tuple(p.id for p in order(list(proto.search_pool), seed, "random"))
                for policy in POLICIES:
                    rid = f"extension-{policy}-{seed}"
                    c.append("extension_run_opened", {"run_id": rid, "policy": policy, "seed": seed,
                                                       "budget": 24, "initial": [p.id for p in initial]})
                    evidence, ids, queried, rows = [], [], [], []

                    def query(p: Point, phase: str) -> None:
                        if len(rows) >= 24 or p in queried:
                            raise RuntimeError("Supplementary budget or duplicate selection violation")
                        guard()
                        qi = len(rows)+1
                        c.append("extension_query_admitted", {"run_id": rid, "query_index": qi,
                                                               "point_id": p.id, "phase": phase})
                        try:
                            outcome = c.simulate(p, "basic", purpose="extension", run_id=rid)
                        except Exception as exc:
                            c.append("extension_query_result", {"run_id": rid, "query_index": qi,
                                     "point_id": p.id, "phase": phase, "status": "failed", "error": type(exc).__name__})
                            raise  # consumes the admitted query; no replacement or retry
                        obs = outcome.record
                        error = abs(model.predict_tpd_s(p)-obs["tpd_s"])/obs["tpd_s"]
                        result_id = "expres_" + hashlib.sha256(f"{plan_hash}|{load}|{rid}|{qi}|{obs['sim_id']}".encode()).hexdigest()[:20]
                        row = {"run_id": rid, "query_index": qi, "result_id": result_id,
                               "point_id": p.id, "phase": phase, "status": "success", "sim_id": obs["sim_id"],
                               "cache_hit": outcome.cache_hit, "tpd_s": obs["tpd_s"],
                               "predicted_tpd_s": model.predict_tpd_s(p), "abs_relative_error": error,
                               "clear_counterexample": error > proto.clear_threshold}
                        c.append("extension_query_result", row)
                        rows.append(row); evidence.append((p, error)); ids.append(result_id); queried.append(p)

                    for p in proto.training:
                        query(p, "calibration")
                    for p in initial:
                        query(p, "initial")
                    for _ in range(12):
                        candidates = [p for p in proto.search_pool if p not in queried]
                        t0 = time.perf_counter()
                        if policy == "kernel_ucb_exploratory":
                            ranking = kernel_candidates(candidates, evidence)
                            chosen = Point.parse(ranking[0]["point_id"])
                            top = ranking[:5]
                        else:
                            scored = rank(policy, proto, candidates, evidence, queried, random_order)
                            chosen = scored[0].point
                            top = [{"point_id": s.point.id, "score": s.score} for s in scored[:5]]
                        c.append("extension_decision", {"run_id": rid, "query_index": len(rows)+1,
                                 "evidence_result_ids": list(ids), "selected_point_id": chosen.id,
                                 "ranked_candidates": top, "policy_seconds": time.perf_counter()-t0})
                        query(chosen, "search")
                    counts = [q["clear_counterexample"] for q in rows if q["phase"] != "calibration"]
                    result = {"run_id": rid, "policy": policy, "seed": seed, "status": "complete",
                              "logical_queries": len(rows), "search_points": 15,
                              "clear_counterexamples": sum(counts), "initial": [p.id for p in initial]}
                    c.append("extension_run_closed", result);w["runs"].append(result)
            w["summary"] = {p: {"complete_runs": 20, "mean_clear": float(np.mean([r["clear_counterexamples"] for r in w["runs"] if r["policy"] == p]))} for p in POLICIES}
            w["paired_kernel_minus_adaptive"] = [next(r["clear_counterexamples"] for r in w["runs"] if r["seed"] == s and r["policy"] == POLICIES[2])-next(r["clear_counterexamples"] for r in w["runs"] if r["seed"] == s and r["policy"] == POLICIES[0]) for s in SEEDS]
            w["mean_gain_vs_adaptive"] = float(np.mean(w["paired_kernel_minus_adaptive"]))
            validation_rows = [e["payload"] for e in c.events("extension_query_result")
                               if e["payload"]["run_id"] == "extension-kernel_ucb_exploratory-2001"
                               and e["payload"].get("status") == "success" and e["payload"]["phase"] == "search"]
            top = sorted(validation_rows, key=lambda r: (-r["abs_relative_error"], Point.parse(r["point_id"])))[:3]
            stability = []
            for row in top:
                for setting in ("half", "tight"):
                    guard()
                    measured = c.simulate(Point.parse(row["point_id"]), setting, purpose="extension_numerical")
                    difference = abs(measured.record["tpd_s"]-row["tpd_s"])/row["tpd_s"]
                    check = {"point_id": row["point_id"], "setting": setting,
                             "source_result_id": row["result_id"], "relative_difference": difference,
                             "passed": difference <= proto.preflight_max_rel_diff}
                    c.append("extension_postflight", check); stability.append(check)
            w["postflight"] = stability
            if not all(v["passed"] for v in stability):
                raise RuntimeError(f"Supplementary postflight failed for {load}f")
            w["cost"] = extension_cost(c)
            w["logical_queries_including_separate_model_fit"] = 9 + sum(r["logical_queries"] for r in w["runs"])
            w["ledger_events"] = c.verify_ledger()
            write(root / "report.json", report)
        report["status"] = "complete"
    except Exception as exc:
        report.update(status="incomplete", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["physical_attempts"] = sum(c.cost()["attempts"] for c in campaigns)
        report["wall_seconds"] = time.monotonic()-started
        report["limits"] = ["Posthoc method development after observing original 20f results; exploratory, not a new confirmatory primary study.",
                            "8f and 35f load worlds are sensitivity checks in the same generic circuit family, not independent PDKs or silicon.",
                            "No held-out point is queried, no thresholds/parameters retuned, no LLM or human speedup measured."]
        write(root / "report.json", report)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, type=Path)
    args = ap.parse_args()
    result = run(args.root.resolve())
    print(json.dumps({"status": result["status"], "physical_attempts": result["physical_attempts"],
                      "worlds": [{"load_ff": w["load_ff"], "summary": w["summary"],
                                  "mean_gain_vs_adaptive": w["mean_gain_vs_adaptive"]} for w in result["worlds"]]}))
