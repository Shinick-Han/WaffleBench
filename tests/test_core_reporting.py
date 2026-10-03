"""Paired bootstrap, null handling and snapshot agreement (NON-SCIENTIFIC fixtures)."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

import numpy as np
from test_core_support import fixture_campaign, fresh_dir

from falsify_lab import benchmark as bm
from falsify_lab import cli
from falsify_lab.protocol import load_protocol
from falsify_lab.reporting import build_snapshot, export_snapshot, paired_bootstrap, paired_comparison

PROTO = load_protocol()
TOP_KEYS = {"schema_version", "data_mode", "generated_at", "project", "protocol_hash", "limitations", "run", "model",
            "observations", "evaluations", "decisions", "trace", "coverage", "benchmark", "cost"}


def _runs(values: dict[int, int], status: str = "complete") -> dict[int, dict]:
    return {s: {"status": status, "clear_count": v} for s, v in values.items()}


class StatisticsTests(unittest.TestCase):
    def test_bootstrap_is_seeded_paired_linear(self):
        diffs = [4, 5, 5, 5, 5, 0, 2, 4, 5, 4]
        ci = paired_bootstrap(diffs, 10000, 20261004, (0.025, 0.975))
        self.assertEqual(ci, paired_bootstrap(diffs, 10000, 20261004, (0.025, 0.975)))
        rng = np.random.Generator(np.random.PCG64(20261004))
        means = np.asarray(diffs, float)[rng.integers(0, 10, size=(10000, 10))].mean(axis=1)
        self.assertEqual(ci, [float(np.quantile(means, 0.025, method="linear")), float(np.quantile(means, 0.975, method="linear"))])
        self.assertEqual(paired_bootstrap([3] * 10, 10000, 20261004, (0.025, 0.975)), [3.0, 3.0])

    def test_primary_requires_ten_complete_pairs(self):
        seeds = [r.seed for r in PROTO.replicates]
        a = _runs({s: 5 for s in seeds})
        b = _runs({s: 1 for s in seeds})
        full = paired_comparison(PROTO, a, b, primary=True)
        self.assertEqual(full["complete_pairs"], 10)
        self.assertEqual(full["mean_difference"], 4.0)
        self.assertEqual(full["ci95"], [4.0, 4.0])
        self.assertTrue(full["success"])
        b[1005]["status"] = "incomplete"
        part = paired_comparison(PROTO, a, b, primary=True)
        self.assertEqual(part["complete_pairs"], 9)
        self.assertIsNone(part["success"])
        self.assertIsNone(part["mean_difference"])
        self.assertIsNone(part["ci95"])
        self.assertIn("9 of 10", part["reason"])
        self.assertEqual(part["excluded_pairs"][0]["seed"], 1005)
        del a[1001]
        self.assertIn("not executed", paired_comparison(PROTO, a, b, primary=True)["excluded_pairs"][0]["reason"])
        small = paired_comparison(PROTO, _runs({s: 2 for s in seeds}), _runs({s: 1 for s in seeds}), primary=True)
        self.assertFalse(small["success"])  # mean gain 1 < 2
        self.assertNotIn("success", paired_comparison(PROTO, a, b, primary=False))


class SnapshotTests(unittest.TestCase):
    def test_empty_snapshot_and_cli_export(self):
        root = fresh_dir("empty-export")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["export", "--root", str(root)])
        self.assertEqual(rc, 0)
        snap = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue(TOP_KEYS <= set(snap))
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["protocol_hash"], PROTO.protocol_sha256)
        for k in ("run", "model", "benchmark", "cost"):
            self.assertIsNone(snap[k])
        for k in ("observations", "evaluations", "decisions", "trace", "coverage"):
            self.assertEqual(snap[k], [])
        self.assertFalse((root / "ledger").exists())  # export performs no experiment

    def test_primary_cli_guard(self):
        root = fresh_dir("primary-guard")
        with redirect_stdout(io.StringIO()):
            rc = cli.main(["benchmark", "--kind", "primary", "--root", str(root)])
        self.assertEqual(rc, 1)
        self.assertFalse((root / "ledger").exists())

    def test_snapshot_agrees_with_stored_evidence(self):
        c = fixture_campaign("snapshot-agreement")
        bm.preflight(c)
        bm.calibrate(c)
        rid = bm.open_and_seed(c, "live", "adaptive_idw_plus_distance", 1001)
        bm.step(c, rid)
        bm.step(c, rid)
        snap = build_snapshot(c)
        json.dumps(snap, allow_nan=False)
        self.assertEqual(snap["data_mode"], "fixture")
        self.assertTrue(any("NON-SCIENTIFIC" in x for x in snap["limitations"]))
        self.assertEqual(snap["run"]["run_id"], rid)
        st = c.run_state(rid)
        self.assertEqual(snap["run"]["budget"], {"limit": 16, "used": 14, "remaining": 2})  # live cap 9 + 3 + 4
        self.assertEqual(len(snap["observations"]), 14)
        for o in snap["observations"]:
            stored = c.get_observation(rid, o["result_id"])
            self.assertEqual((o["tpd_s"], o["tphl_s"], o["tplh_s"]), (stored["tpd_s"], stored["tphl_s"], stored["tplh_s"]))
        for e in snap["evaluations"]:
            self.assertEqual(e["clear_counterexample"], e["abs_relative_error"] > 0.11)
            self.assertEqual(e["secondary_counterexample"], e["abs_relative_error"] > 0.10)
            self.assertEqual(e["model_hash"], snap["model"]["model_hash"])
        self.assertEqual([d["observed_result_id"] for d in snap["decisions"]], [q["result_id"] for q in st["queries"] if q["phase"] == "search"])
        self.assertGreaterEqual(len(snap["next_candidates"]), 2)
        cov = {r["point_id"]: r for r in snap["coverage"]}
        self.assertEqual(len(cov), 175)
        held = [r for r in cov.values() if r["state"] == "held_out"]
        self.assertEqual(len(held), 40)
        self.assertTrue(all(r["observed_tpd_s"] is None and r["abs_relative_error"] is None for r in held))
        self.assertEqual(sum(r["state"] == "observed" for r in cov.values()), 5)
        self.assertEqual(snap["cost"]["logical_queries"], len(c.events("query_admitted")))
        self.assertEqual(snap["cost"]["attempts"], len(c.events("attempt_admitted")))
        self.assertTrue(all(t["actor"] == "deterministic runner" for t in snap["trace"]))
        self.assertIsNone(snap["benchmark"])
        self.assertNotIn("tpd_s", json.dumps(snap["preflight"]))  # restricted delays stay out
        # After every run closes, posthoc truth fills held-out coverage.
        c.close_run(rid, "test stop")
        bm.posthoc(c)
        cov2 = {r["point_id"]: r for r in build_snapshot(c)["coverage"]}
        self.assertTrue(all(cov2[r["point_id"]]["observed_tpd_s"] is not None and cov2[r["point_id"]]["truth_source"] == "posthoc" for r in held))
        path, snap3 = export_snapshot(c.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run"]["status"], "incomplete")

    def test_development_benchmark_subset_report(self):
        c = fixture_campaign("bench-subset")
        res = bm.run_benchmark(c, seeds=[1001, 1002])
        self.assertEqual(res["status"], "complete")
        rep = build_snapshot(c)["benchmark"]
        self.assertEqual(rep["primary"]["complete_pairs"], 2)
        self.assertIsNone(rep["primary"]["success"])
        self.assertEqual(len(rep["policies"]), 4)
        self.assertEqual(rep["held_out"]["model_errors"]["points"], 40)
        self.assertEqual(rep["reference"]["points_evaluated"], 175)
        for pol in rep["policies"]:
            for r in pol["runs"]:
                self.assertEqual(len(r["cumulative_clear"]), 15)
                if r["first_clear_query"] is None:
                    self.assertEqual(r["clear_count"], 0)
        with self.assertRaises(Exception):
            bm.run_benchmark(c, seeds=[1003])  # never resumed or extended


if __name__ == "__main__":
    unittest.main()
