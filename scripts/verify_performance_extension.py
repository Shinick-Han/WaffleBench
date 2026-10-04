"""Read-only audit of the supplementary experiment, including raw measurements.

No simulator or LLM is called. Assertions fail closed; no ledger is rewritten.
Usage: python scripts/verify_performance_extension.py --root <extension> --output <receipt>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from falsify_lab.exploratory import kernel_candidates
from falsify_lab.model import fit
from falsify_lab.policies import rank
from falsify_lab.protocol import Point, canonical_json, load_protocol
from falsify_lab.simulator import render_netlist
from falsify_lab.storage import Campaign


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ordered(points, seed, purpose):
    return sorted(points, key=lambda p: (sha(
        f"falsify-extension-v1|{purpose}|{seed}|{p.id}".encode()), p))


def audit(root: Path) -> dict:
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    plan_hash = plan.pop("plan_hash")
    assert sha(canonical_json(plan).encode()) == plan_hash
    proto = load_protocol()
    assert plan["protocol_sha256"] == proto.protocol_sha256
    assert plan["grid_and_split_manifest_sha256"] == proto.manifest_sha256
    assert plan["load_ff"] == [20, 8, 35]
    assert plan["seeds"] == list(range(2001, 2021))
    assert plan["policies"] == ["adaptive_idw_plus_distance", "random", "kernel_ucb_exploratory"]
    assert plan["kernel"] == {"length_scales": [.6, .6, .25, .5], "ridge": .0001,
                               "dispersion_weight": .05, "calibrated_uncertainty": False}
    assert plan["budget_per_run"] == {"calibration": 9, "initial": 3, "subsequent": 12, "total": 24}
    assert plan["clear_threshold_strict"] == proto.clear_threshold == .11
    assert (plan["global_physical_attempt_cap"], plan["global_wall_cap_seconds"]) == (1000, 1500)
    # A published package includes the exact bytes executed before optimization.
    if (root / "executed_source").exists():
        for name, digest in plan["source_sha256"].items():
            assert sha((root / "executed_source" / name).read_bytes()) == digest
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete" and report["plan_hash"] == plan_hash
    assert report["primary_result_replaced"] is False
    worlds = []
    for load in plan["load_ff"]:
        c = Campaign(root / f"load-{load}f", None)
        ledger_count = c.verify_ledger()
        events = c.events()
        types = Counter(e["type"] for e in events)
        assert types["extension_run_opened"] == types["extension_run_closed"] == 60
        assert types["extension_query_admitted"] == types["extension_query_result"] == 1440
        assert types["extension_decision"] == 720
        assert types["preflight_result"] == 15 and types["extension_postflight"] == 6
        assert types["model_frozen"] == 1 and types["query_admitted"] == 9
        assert not types["posthoc_started"]
        ep = c.events("extension_plan")[0]
        assert ep["payload"]["plan_hash"] == plan_hash and ep["payload"]["load_ff"] == load
        assert ep["seq"] < c.events("attempt_admitted")[0]["seq"]
        assert c.events("preflight_completed")[0]["payload"]["passed"]
        model = c.model()
        assert model and json.loads((c.root / "model.json").read_text()) == model.to_dict()
        assert fit(list(proto.training), list(model.calibration), proto.protocol_sha256,
                   proto.manifest_sha256).to_dict() == model.to_dict()
        assert c.events("model_frozen")[0]["seq"] < c.events("extension_run_opened")[0]["seq"]
        admitted = {e["payload"]["attempt_id"]: e["payload"] for e in c.events("attempt_admitted")}
        held_numerical = [a for a in admitted.values() if Point.parse(a["point_id"]) in proto.held_out]
        assert len(held_numerical) == 3
        assert {a["point_id"] for a in held_numerical} == {"FS|1.800|85.0"}
        assert {a["setting"] for a in held_numerical} == {"basic", "half", "tight"}
        assert all(a["purpose"] == "preflight" for a in held_numerical)
        sims = {}
        for e in c.events("attempt_finished"):
            p = e["payload"]
            assert p["status"] == "success"
            folder = "restricted" if p["restricted"] else "evidence"
            raw = (c.root / folder / "sims" / (p["sim_id"] + ".json")).read_bytes()
            assert sha(raw) == p["record_sha256"]
            rec = json.loads(raw)
            a = admitted[p["attempt_id"]]
            assert rec["attempt_id"] == p["attempt_id"] and rec["point_id"] == a["point_id"]
            pt = Point.parse(rec["point_id"])
            net = (c.root / rec["netlist_file"]).read_bytes()
            expected = render_netlist(pt, rec["setting"]).replace("\ncl out 0 20f\n", f"\ncl out 0 {load}f\n").encode()
            assert net == expected and sha(net) == rec["fingerprint"]["netlist_sha256"]
            assert rec["fingerprint"]["supplementary_load_ff"] == load
            assert "ngspice-47" in rec["fingerprint"]["simulator"]
            assert "22d5cae2bd32b2e39157a8d27bf457122f68285b72a9ebefdf41551b628233ab" in rec["fingerprint"]["simulator"]
            assert "not a foundry PDK" in rec["fingerprint"]["device_models"]
            assert rec["sim_id"] == "sim_" + sha(canonical_json({"point_id": pt.id, **rec["fingerprint"]}).encode())[:20]
            log = (c.root / "attempts" / p["attempt_id"] / "ngspice.log").read_text(encoding="utf-8")
            values = {n: float(v) for n, v in re.findall(r"^\s*(tphl|tplh)\s*=\s*([-+0-9.eE]+)", log, re.M)}
            assert "FALSIFY_DONE" in log and set(values) == {"tphl", "tplh"}
            assert all(0 < v < 4e-9 for v in values.values())
            assert rec["tphl_s"] == values["tphl"] and rec["tplh_s"] == values["tplh"]
            assert rec["tpd_s"] == (values["tphl"] + values["tplh"]) / 2
            sims[rec["sim_id"]] = rec
        closed = []
        for opening in c.events("extension_run_opened"):
            o = opening["payload"]
            assert o["policy"] in plan["policies"] and o["seed"] in plan["seeds"] and o["budget"] == 24
            initial = [p.id for p in ordered(proto.search_pool, o["seed"], "initial")[:3]]
            assert o["initial"] == initial
            rows, evidence, points, ids, pending, decisions = [], [], [], [], None, 0
            for e in events:
                p = e["payload"]
                if p.get("run_id") != o["run_id"]:
                    continue
                if e["type"] == "extension_decision":
                    assert pending is None and len(rows) >= 12
                    assert p["evidence_result_ids"] == ids and p["query_index"] == len(rows) + 1
                    candidates = [pt for pt in proto.search_pool if pt not in points]
                    if o["policy"] == "kernel_ucb_exploratory":
                        top = kernel_candidates(candidates, evidence)[:5]
                    else:
                        ro = tuple(pt.id for pt in ordered(proto.search_pool, o["seed"], "random"))
                        top = [{"point_id": s.point.id, "score": s.score} for s in rank(o["policy"], proto, candidates, evidence, points, ro)[:5]]
                    assert p["ranked_candidates"] == top and p["selected_point_id"] == top[0]["point_id"]
                    assert p["policy_seconds"] >= 0
                    pending = p["selected_point_id"]
                    decisions += 1
                elif e["type"] == "extension_query_admitted":
                    assert p["query_index"] == len(rows) + 1
                    if len(rows) >= 12:
                        assert p["point_id"] == pending and p["phase"] == "search"
                elif e["type"] == "extension_query_result":
                    assert p["status"] == "success" and p["query_index"] == len(rows) + 1
                    # Simulation events can appear between admission and result.
                    qa = [x["payload"] for x in events[:e["seq"]-1] if x["type"] == "extension_query_admitted" and x["payload"].get("run_id") == o["run_id"]][-1]
                    assert all(p[k] == qa[k] for k in ("query_index", "phase", "point_id"))
                    pt = Point.parse(p["point_id"])
                    assert pt not in points
                    if len(rows) < 9:
                        assert pt == proto.training[len(rows)] and p["phase"] == "calibration"
                    else:
                        assert pt in proto.search_pool and pt.id not in {q.id for q in proto.held_out}
                        if len(rows) < 12:
                            assert pt.id == initial[len(rows)-9] and p["phase"] == "initial"
                    rec = sims[p["sim_id"]]
                    pred = model.predict_tpd_s(pt)
                    err = abs(pred-rec["tpd_s"])/rec["tpd_s"]
                    assert p["tpd_s"] == rec["tpd_s"] and p["predicted_tpd_s"] == pred
                    assert p["abs_relative_error"] == err and p["clear_counterexample"] == (err > .11)
                    assert p["result_id"] == "expres_" + sha(f"{plan_hash}|{load}|{o['run_id']}|{p['query_index']}|{rec['sim_id']}".encode())[:20]
                    rows.append(p); evidence.append((pt, err)); points.append(pt); ids.append(p["result_id"])
                    pending = None
                elif e["type"] == "extension_run_closed":
                    assert len(rows) == 24 and decisions == 12 and pending is None
                    assert p["status"] == "complete" and p["logical_queries"] == 24 and p["search_points"] == 15
                    assert p["clear_counterexamples"] == sum(q["clear_counterexample"] for q in rows[9:])
                    closed.append(p)
        assert len({(r["policy"], r["seed"]) for r in closed}) == 60
        for e in c.events("extension_postflight"):
            p = e["payload"]
            src = next(x["payload"] for x in c.events("extension_query_result") if x["payload"]["result_id"] == p["source_result_id"])
            measured = next(r for r in sims.values() if r["point_id"] == p["point_id"] and r["setting"] == p["setting"])
            diff = abs(measured["tpd_s"]-src["tpd_s"])/src["tpd_s"]
            assert p["relative_difference"] == diff and p["passed"] and diff <= .005
            assert e["seq"] > c.events("extension_run_closed")[-1]["seq"]
        means = {p: statistics.mean(r["clear_counterexamples"] for r in closed if r["policy"] == p) for p in plan["policies"]}
        w = next(w for w in report["worlds"] if w["load_ff"] == load)
        assert all(w["summary"][p]["complete_runs"] == 20 and w["summary"][p]["mean_clear"] == means[p] for p in plan["policies"])
        assert w["runs"] == closed
        base_cost = c.cost()
        cost = {**base_cost, "logical_queries": 1449,
                "cache_hits": base_cost["cache_hits"] + sum(e["payload"]["cache_hit"] for e in c.events("extension_query_result")),
                "policy_seconds": round(sum(e["payload"]["policy_seconds"] for e in c.events("extension_decision")), 6)}
        if "cost_reconstruction" in report:
            assert all(w["cost"][key] == value for key, value in cost.items())
        worlds.append({"load_ff": load, "ledger_events": ledger_count, "ledger_head": events[-1]["hash"],
                       "complete_runs": 60, "mean_clear": means, "cost": cost})
    total = sum(w["cost"]["attempts"] for w in worlds)
    assert total == report["physical_attempts"] <= 1000 and report["wall_seconds"] <= 1500
    return {"ok": True, "scope": "Exploratory; primary result unchanged", "plan_hash": plan_hash,
            "complete_runs": 180, "recomputed_decisions": 2160, "verified_raw_measurements": total,
            "logical_queries_including_model_fit": sum(w["cost"]["logical_queries"] for w in worlds),
            "cache_hits": sum(w["cost"]["cache_hits"] for w in worlds),
            "heldout_numerical_preflight_only": {"point_id": "FS|1.800|85.0", "physical_attempts": 9,
                                                 "search_queries": 0, "selection_evidence_rows": 0},
            "worlds": worlds}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = audit(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in result.items() if k != "worlds"}))
