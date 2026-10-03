"""Policy determinism, ties, shared starts and outcome independence (NON-SCIENTIFIC fixtures)."""

from __future__ import annotations

import unittest

from test_core_support import fixture_campaign

from falsify_lab import benchmark as bm
from falsify_lab.policies import build_decision, rank
from falsify_lab.protocol import Point, distance, load_protocol
from falsify_lab.reporting import run_summary
from falsify_lab.simulator import FixtureSimulator, fixture_delay

PROTO = load_protocol()


def other_world(point: Point, setting: str) -> tuple[float, float]:
    """A different synthetic outcome landscape (still NON-SCIENTIFIC)."""
    a, b = fixture_delay(point, setting)
    bump = 1.0 + 0.4 * (point.temp_c > 50) * (point.vdd < 2.3)
    return a * bump, b / bump ** 0.5


class TieAndCandidateTests(unittest.TestCase):
    def test_exact_ties_break_lexically(self):
        cands = list(PROTO.search_pool)
        evidence = [(p, 0.0) for p in PROTO.training]
        r = rank("idw_without_distance", PROTO, cands, evidence, list(PROTO.training))
        self.assertTrue(all(s.score == 0.0 for s in r))
        self.assertEqual([s.point for s in r], sorted(cands))
        self.assertEqual(r[0].point.id, min(cands).id)
        self.assertEqual(r[0].point.corner, "FF")

    def test_coincident_optima_still_give_two_distinct_candidates(self):
        far, near = Point.parse("FF|1.200|-40.0"), Point.parse("TT|2.900|125.0")
        ev_pt = Point.parse("TT|3.300|125.0")
        evidence = [(ev_pt, 0.5)]  # constant IDW -> exploitation tie -> lexical FF; FF is also the maximin optimum
        for policy in ("adaptive_idw_plus_distance", "idw_without_distance", "space_filling"):
            d = build_decision(policy, PROTO, [far, near], evidence, [ev_pt], (), evidence, ["res_x"])
            ids = [c["point_id"] for c in d["candidates"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(ids, [far.id, near.id])
            self.assertEqual([c["role"] for c in d["candidates"]], ["exploitation", "exploration"])
            self.assertEqual(d["selected_point_id"], rank(policy, PROTO, [far, near], evidence, [ev_pt])[0].point.id)
            self.assertEqual(d["selected_point_id"], far.id)
            explo = d["candidates"][1]
            self.assertEqual(explo["min_normalized_distance"], distance(near, ev_pt))  # genuine score components
            self.assertEqual(explo["idw_predicted_abs_error"], 0.5)
        single = build_decision("space_filling", PROTO, [far], evidence, [ev_pt], (), evidence, [])
        self.assertEqual(len(single["candidates"]), 1)


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = fixture_campaign("policies-world-a")
        cls.b = fixture_campaign("policies-world-b", FixtureSimulator(variant="b", delay_fn=other_world))
        for c in (cls.a, cls.b):
            bm.calibrate(c)
            for pol in PROTO.policies:
                bm.run_policy(c, pol, 1004)

    def test_budget_and_shared_initial_queries(self):
        rep = PROTO.replicate(1004)
        prefixes = set()
        for pol in PROTO.policies:
            st = self.a.run_state(f"benchmark-{pol}-1004")
            self.assertEqual(st["used"], 24)
            self.assertEqual(st["closed"]["status"], "complete")
            phases = [q["phase"] for q in st["queries"]]
            self.assertEqual(phases, ["calibration"] * 9 + ["initial"] * 3 + ["search"] * 12)
            ids = [q["point_id"] for q in st["queries"]]
            self.assertEqual(ids[9:12], [p.id for p in rep.initial])
            self.assertEqual(len(set(ids[9:])), 15)
            self.assertTrue(set(ids[9:]) <= {p.id for p in PROTO.search_pool})
            prefixes.add(tuple(ids[:12]))
            self.assertEqual(len(st["decisions"]), 12)
            for d in st["decisions"]:
                self.assertGreaterEqual(len({c["point_id"] for c in d["candidates"]}), 2)
                self.assertNotIn("tpd_s", str(d["candidates"]))
        self.assertEqual(len(prefixes), 1)

    def test_geometric_policies_ignore_outcomes(self):
        for pol in ("random", "space_filling"):
            sa = run_summary(self.a, f"benchmark-{pol}-1004")
            sb = run_summary(self.b, f"benchmark-{pol}-1004")
            self.assertEqual(sa["selection_point_ids"], sb["selection_point_ids"], pol)
        self.assertNotEqual(self.a.model().model_hash, self.b.model().model_hash)
        rnd = run_summary(self.a, "benchmark-random-1004")["selection_point_ids"]
        order = [pid for pid in PROTO.replicate(1004).random_order]
        self.assertEqual(rnd, order[:15])

    def test_determinism_across_campaigns(self):
        c = fixture_campaign("policies-world-a-repeat")
        bm.calibrate(c)
        bm.run_policy(c, "adaptive_idw_plus_distance", 1004)
        s1 = run_summary(self.a, "benchmark-adaptive_idw_plus_distance-1004")
        s2 = run_summary(c, "benchmark-adaptive_idw_plus_distance-1004")
        self.assertEqual(s1["selection_point_ids"], s2["selection_point_ids"])
        d1 = [(d["selected_point_id"], d["rank_before"], d["rank_after"], d["selection_changed"]) for d in self.a.run_state(s1["run_id"])["decisions"]]
        d2 = [(d["selected_point_id"], d["rank_before"], d["rank_after"], d["selection_changed"]) for d in c.run_state(s2["run_id"])["decisions"]]
        self.assertEqual(d1, d2)


if __name__ == "__main__":
    unittest.main()
