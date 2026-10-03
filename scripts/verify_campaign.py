"""Independently audit a finished primary ledger and exported statistics."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from falsify_lab.storage import Campaign
from falsify_lab.protocol import Point


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    c = Campaign(args.root, None)
    events = c.verify_ledger()
    assert c.kind == "primary", "Primary campaign required"
    snapshot = json.loads((args.root / "snapshot.json").read_text(encoding="utf-8"))
    b = snapshot["benchmark"]
    assert b["status"] == "complete" and not b["not_run"]
    rids = c.run_ids("benchmark")
    assert len(rids) == 40
    counts = {}
    all_closed_seq = []
    known = {}
    for event in c.events():
        p, typ = event["payload"], event["type"]
        rid = p.get("run_id")
        if typ == "query_result" and p["status"] == "success":
            known.setdefault(rid, set()).add(p["result_id"])
        if typ == "decision":
            assert set(p["evidence_result_ids"]) <= known.get(rid, set()), "Future/foreign evidence in decision"
            assert len({x["point_id"] for x in p["candidates"]}) >= 2
        if typ == "run_closed":
            all_closed_seq.append(event["seq"])
    posthoc_start = c.events("posthoc_started")[0]["seq"]
    assert max(all_closed_seq) < posthoc_start, "Posthoc truth was revealed before search closed"
    for rid in rids:
        st = c.run_state(rid)
        assert st["closed"]["status"] == "complete" and st["used"] == 24
        qs = st["queries"]
        assert len(qs) == 24 and all(q["status"] == "success" for q in qs)
        initial = [q["point_id"] for q in qs if q["phase"] == "initial"]
        expected = next(rep for rep in c.protocol.replicates if rep.seed == st["seed"])
        assert initial == [p.id for p in expected.initial], "Shared initial points differ"
        search = [q for q in qs if q["phase"] in ("initial", "search")]
        assert len(search) == 15 and len({q["point_id"] for q in search}) == 15
        assert all(c.protocol.partition(Point.parse(q["point_id"])) == "search_pool" for q in search)
        clear = 0
        cumulative = []
        for q in search:
            obs, ev = q["observation"], q["evaluation"]
            assert obs["tphl_s"] > 0 and obs["tplh_s"] > 0
            assert math.isclose(obs["tpd_s"], (obs["tphl_s"] + obs["tplh_s"]) / 2, rel_tol=1e-12)
            err = abs(ev["predicted_tpd_s"] - obs["tpd_s"]) / obs["tpd_s"]
            assert math.isclose(err, ev["abs_relative_error"], rel_tol=1e-12)
            assert ev["clear_counterexample"] == (err > .11)
            assert ev["secondary_counterexample"] == (err > .10)
            clear += int(err > .11)
            cumulative.append(clear)
        exported = next(r for p in b["policies"] for r in p["runs"] if r["run_id"] == rid)
        assert exported["cumulative_clear"] == cumulative and exported["clear_count"] == clear
        counts.setdefault(st["policy"], {})[st["seed"]] = clear
    seeds = list(range(1001, 1011))
    diffs = np.array([counts["adaptive_idw_plus_distance"][s] - counts["random"][s] for s in seeds])
    sampled = np.random.Generator(np.random.PCG64(20261004)).integers(0, 10, size=(10000, 10))
    interval = np.quantile(diffs[sampled].mean(axis=1), [.025, .975], method="linear")
    mean = float(diffs.mean())
    assert math.isclose(b["primary"]["mean_difference"], mean, abs_tol=1e-12)
    assert np.allclose(b["primary"]["ci95"], interval, atol=1e-12)
    assert b["primary"]["success"] == bool(mean >= 2 and interval[0] > 0)
    assert b["primary"]["complete_pairs"] == 10
    assert b["reference"]["points_evaluated"] == 175 and not b["reference"]["missing_points"]
    assert b["held_out"]["model_errors"]["points"] == 40 and not b["held_out"]["missing_points"]
    assert snapshot["preflight"]["passed"]
    assert len(b["postflight"]["checks"]) == 3 and all(x["within_limit"] for x in b["postflight"]["checks"])
    cost = c.cost()
    assert cost["attempts"] <= 1200 and cost["wall_seconds"] <= 3600 and cost["failures"] == 0
    out = {"ok": True, "ledger_events": events, "ledger_head_hash": c.events()[-1]["hash"],
           "verified_runs": 40, "verified_queries_per_run": 24, "policy_counts": counts,
           "paired_differences": diffs.tolist(), "independent_mean_difference": mean,
           "independent_ci95": interval.tolist(), "primary_success": b["primary"]["success"],
           "preflight_passed": True, "postflight_passed": True, "cost": cost}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
