"""Actual ngspice development check: numerical preflight (15 runs) and nine-point calibration.

Labelled DEVELOPMENT: an isolated campaign under runs/development/, never benchmark
results. Skipped when FALSIFY_NGSPICE does not point at an ngspice binary.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from test_core_support import fresh_dir

from falsify_lab import benchmark as bm
from falsify_lab.protocol import full_grid
from falsify_lab.reporting import export_snapshot
from falsify_lab.simulator import NgspiceSimulator
from falsify_lab.storage import Campaign

BIN = os.environ.get("FALSIFY_NGSPICE")


@unittest.skipUnless(BIN and Path(BIN).exists(), "FALSIFY_NGSPICE not set to an ngspice binary")
class NgspiceDevelopmentTests(unittest.TestCase):
    def test_preflight_and_calibration_with_real_ngspice(self):
        c = Campaign.create(fresh_dir("ngspice-dev"), "development", NgspiceSimulator(BIN), label="development: preflight + calibration")
        pf = bm.preflight(c)
        self.assertEqual(len(c.events("preflight_result")), 15)
        self.assertTrue(all(ch["measured"] for ch in pf["checks"]), pf)
        m = bm.calibrate(c)
        self.assertEqual(m.rank, 5)
        self.assertTrue(all(m.predict_tpd_s(p) > 0 for p in full_grid()))
        cal = c.run_state("calibration")
        self.assertEqual(cal["closed"]["status"], "complete")
        self.assertTrue(all(q["observation"]["tphl_s"] > 0 and q["observation"]["tplh_s"] > 0 for q in cal["queries"]))
        self.assertIn("ngspice-47", c.info["simulator"])
        path, snap = export_snapshot(c.root)
        usage = c.cost()
        report = {
            "label": "DEVELOPMENT validation (not benchmark results)",
            "root": str(c.root),
            "preflight": pf,
            "model": m.to_dict(),
            "usage": usage,
            "snapshot": str(path),
        }
        (c.root / "development_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
        print("\nDEVELOPMENT_NGSPICE", json.dumps({"root": str(c.root), "preflight_passed": pf["passed"], "model_hash": m.model_hash, "usage": usage}))
        self.assertEqual(usage["attempts"], 24)  # 15 preflight + 9 calibration, no cache sharing with restricted evidence
        self.assertEqual(usage["campaign_kind"], "development")
        self.assertEqual(snap["data_mode"], "real")


if __name__ == "__main__":
    unittest.main()
