"""Protocol, netlist and frozen-model checks (NON-SCIENTIFIC fixture data where simulated)."""

from __future__ import annotations

import math
import unittest

from test_core_support import fixture_campaign

from falsify_lab import benchmark as bm
from falsify_lab import experiment
from falsify_lab import model as model_mod
from falsify_lab.protocol import Point, full_grid, load_protocol
from falsify_lab.simulator import render_netlist


class ProtocolTests(unittest.TestCase):
    def test_partitions_and_seeds_follow_manifest(self):
        p = load_protocol()  # re-derives split and orders from the SHA256 rules
        self.assertEqual((len(p.training), len(p.held_out), len(p.search_pool), len(p.prior_excluded)), (9, 40, 125, 1))
        self.assertEqual([r.seed for r in p.replicates], list(range(1001, 1011)))
        self.assertEqual(p.manifest["protocol_sha256"], p.protocol_sha256)
        for rep in p.replicates:
            self.assertEqual([x.id for x in rep.initial], list(rep.random_order[:3]))
        self.assertEqual((p.clear_threshold, p.secondary_threshold), (0.11, 0.10))

    def test_coordinates_normalized(self):
        for pt in full_grid():
            self.assertTrue(all(0.0 <= v <= 1.0 for v in pt.coords), pt)
        self.assertEqual(Point.parse("FF|1.200|-40.0").coords, (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(Point.parse("SS|3.300|125.0").coords, (1.0, 1.0, 1.0, 1.0))


class NetlistTests(unittest.TestCase):
    def test_basic_setting_is_connection_netlist_and_others_differ(self):
        for pid in ("TT|2.500|27.0", "SF|1.200|-40.0"):
            pt = Point.parse(pid)
            legacy = experiment.render_netlist(experiment.PVT.checked(pt.corner, pt.vdd, pt.temp_c))
            self.assertEqual(render_netlist(pt, "basic"), legacy)
            self.assertIn(".tran 0.5p 6n", render_netlist(pt, "half"))
            tight = render_netlist(pt, "tight")
            self.assertIn(".tran 0.5p 6n", tight)
            self.assertIn("reltol=1e-4 vntol=1e-7 abstol=1e-13", tight)


class ModelTests(unittest.TestCase):
    def test_rank_and_rank_deficiency(self):
        p = load_protocol()
        self.assertEqual(model_mod.design_rank(list(p.training)), 5)
        tt_only = [x for x in p.training if x.corner == "TT"]
        self.assertLess(model_mod.design_rank(tt_only), 5)
        obs = [{"point_id": x.id, "result_id": "r", "sim_id": "s", "tpd_s": 1e-10} for x in tt_only * 2][:9]
        with self.assertRaises(model_mod.ModelError):
            model_mod.fit((tt_only * 2)[:9], obs, p.protocol_sha256, p.manifest_sha256)

    def test_fit_freeze_hash_and_positive_predictions(self):
        c = fixture_campaign("model-freeze")
        m = bm.calibrate(c)
        self.assertEqual(bm.calibrate(c).model_hash, m.model_hash)  # frozen once, returned thereafter
        self.assertEqual(m.rank, 5)
        self.assertEqual(len(m.coefficients), 5)
        self.assertTrue(all(m.predict_tpd_s(pt) > 0 for pt in full_grid()))
        # Same fixture evidence -> identical frozen model in an independent campaign.
        self.assertEqual(bm.calibrate(fixture_campaign("model-freeze-2")).coefficients, m.coefficients)
        # Stored model is tamper-evident.
        d = m.to_dict()
        d["coefficients_hex"][0] = float(m.coefficients[0] + 1e-6).hex()
        with self.assertRaises(model_mod.ModelError):
            model_mod.FrozenModel.from_dict(d)
        with self.assertRaises(Exception):
            c.freeze_model(m)  # never overwritten

    def test_strict_thresholds(self):
        c = fixture_campaign("thresholds")
        m = bm.calibrate(c)
        pt = Point.parse("TT|1.500|0.0")
        pred = m.predict_tpd_s(pt)
        for err, clear, sec in ((0.11, False, True), (0.10, False, False), (0.1101, True, True)):
            ev = model_mod.evaluate(m, pt, pred / (1 + err), 0.11, 0.10)
            self.assertTrue(math.isclose(ev["abs_relative_error"], err, rel_tol=1e-9))
            # values at float distance of a threshold are judged by the strict ">" on the computed error
            self.assertEqual(ev["clear_counterexample"], ev["abs_relative_error"] > 0.11)
            self.assertEqual(ev["secondary_counterexample"], ev["abs_relative_error"] > 0.10)
            if err == 0.1101:
                self.assertEqual((ev["clear_counterexample"], ev["secondary_counterexample"]), (clear, sec))


if __name__ == "__main__":
    unittest.main()
