"""Routing PoC choice policies (development fixtures only, no reserved synthetic test)."""

from __future__ import annotations

import copy
import itertools
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routing_poc import policies as pol  # noqa: E402

COST = {"wafer_load_s": 20.0, "settle_s": 0.5, "move_um_per_s": 1000.0, "retry_limit": 1}


def cand(site, wafer, x, y, prior, cap=2.0, inf=0.5):
    return {
        "site_id": site, "wafer_id": wafer, "x_um": float(x), "y_um": float(y),
        "prior_p": prior, "optical_observed_at": "2026-10-05T00:00:00+00:00",
        "recipe_id": "r1", "capture_bound_s": cap, "inference_bound_s": inf,
    }


def with_cost(cands, current, cost=COST):
    out = []
    for c in cands:
        c = dict(c)
        c["cost"] = pol.project_cost(c, current, cost)
        out.append(c)
    return out


def state(cands, current=None, remaining=1000.0, history=None, cost=COST, explicit_cost=True):
    s = {
        "job_id": "job-dev",
        "candidates": with_cost(cands, current, cost),
        "remaining_s": remaining,
        "current": current,
        "history": history or [],
    }
    if explicit_cost:
        s["cost"] = dict(cost)
    return s


def no_audit(st, policy):
    return pol.select(st, random.Random(0), policy=policy, audit_epsilon=0.0)


class StateWhitelistTests(unittest.TestCase):
    def setUp(self):
        self.base = state([cand("a", "w1", 0, 0, 0.5), cand("b", "w1", 10, 0, 0.4)])

    def test_rejects_hidden_state_keys(self):
        for key in ("reference", "archive", "truth", "seed", "scenario", "budget_s"):
            s = copy.deepcopy(self.base)
            s[key] = {"a": True}
            with self.assertRaisesRegex(ValueError, "non-whitelisted"):
                no_audit(s, "risk_only")

    def test_rejects_hidden_candidate_features(self):
        for key in ("doi", "label", "image_path", "embedding", "mask", "truth", "sem_image"):
            s = copy.deepcopy(self.base)
            s["candidates"][0][key] = 1
            with self.assertRaisesRegex(ValueError, "non-whitelisted"):
                no_audit(s, "beam_route")

    def test_rejects_bad_numbers(self):
        bad = [("prior_p", 1.5), ("prior_p", True), ("x_um", math.nan),
               ("capture_bound_s", 0.0), ("inference_bound_s", -1.0)]
        for key, value in bad:
            s = copy.deepcopy(self.base)
            s["candidates"][0][key] = value
            with self.assertRaises(ValueError, msg=key):
                no_audit(s, "risk_only")
        s = copy.deepcopy(self.base)
        s["remaining_s"] = math.inf
        with self.assertRaises(ValueError):
            no_audit(s, "risk_only")

    def test_rejects_bad_epsilon_policy_and_rng(self):
        with self.assertRaises(ValueError):
            pol.select(self.base, random.Random(0), audit_epsilon=1.5)
        with self.assertRaises(ValueError):
            pol.select(self.base, random.Random(0), audit_epsilon=True)
        with self.assertRaises(ValueError):
            pol.select(self.base, random.Random(0), policy="oracle")
        with self.assertRaises(ValueError):
            pol.select(self.base, 0)

    def test_does_not_mutate_caller_state(self):
        before = copy.deepcopy(self.base)
        for policy in pol.POLICIES:
            pol.select(self.base, random.Random(3), policy=policy)
        self.assertEqual(self.base, before)

    def test_reselection_of_paid_site_is_inconsistent(self):
        s = copy.deepcopy(self.base)
        s["history"] = [{"site_id": "a", "reported_doi": True}]
        with self.assertRaisesRegex(ValueError, "offered again"):
            no_audit(s, "risk_only")

    def test_mismatched_declared_cost_raises(self):
        s = copy.deepcopy(self.base)
        s["candidates"][1]["cost"]["reserved_s"] += 3.0
        with self.assertRaisesRegex(ValueError, "disagrees"):
            no_audit(s, "beam_route")


class UnknownHistoryTests(unittest.TestCase):
    def test_null_or_missing_report_is_unknown_not_negative(self):
        hist = [
            {"site_id": "p", "reported_doi": True, "attempts": [{"status": "ok"}]},
            {"site_id": "n", "reported_doi": False},
            {"site_id": "u1", "reported_doi": None, "attempts": [{"status": "missing"}]},
            {"site_id": "u2", "attempts": [{"status": "failed"}, {"status": "failed"}]},
        ]
        s = state([cand("a", "w1", 0, 0, 0.5)], history=hist)
        choice = no_audit(s, "beam_route")
        self.assertEqual(choice["components"]["history"],
                         {"reported_positive": 1, "reported_negative": 1, "unknown": 2})

    def test_non_bool_report_rejected(self):
        for report in (0, "false", 0.0):
            s = state([cand("a", "w1", 0, 0, 0.5)], history=[{"site_id": "u", "reported_doi": report}])
            with self.assertRaises(ValueError):
                no_audit(s, "risk_only")

    def test_history_outcomes_do_not_change_choice(self):
        cands = [cand("a", "w1", 0, 0, 0.5), cand("b", "w1", 50, 0, 0.6), cand("c", "w2", 0, 0, 0.7)]
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        picks = set()
        for report in (True, False, None):
            s = state(cands, current=cur, history=[{"site_id": "z", "reported_doi": report}])
            for policy in pol.POLICIES:
                picks.add((policy, no_audit(s, policy)["site_id"]))
        self.assertEqual(len(picks), len(pol.POLICIES))


class AffordabilityTests(unittest.TestCase):
    def test_none_when_nothing_affordable(self):
        s = state([cand("a", "w1", 0, 0, 0.9)], remaining=1.0)
        for policy in pol.POLICIES:
            self.assertIsNone(pol.select(s, random.Random(0), policy=policy, audit_epsilon=0.5))
        self.assertIsNone(no_audit(state([]), "beam_route"))

    def test_only_affordable_sites_and_full_retry_reserve(self):
        # Load 20 + settle 0.5 + 2.5 + retry 2.5 = 25.5 reserved; first only 23.
        cheap = cand("cheap", "w1", 0, 0, 0.1, cap=0.5, inf=0.0)
        rich = cand("rich", "w1", 0, 0, 0.9)
        s = state([cheap, rich], remaining=24.0)
        self.assertEqual(s["candidates"][1]["cost"]["first_s"], 23.0)
        self.assertEqual(s["candidates"][1]["cost"]["reserved_s"], 25.5)
        for policy in pol.POLICIES:
            for seed in range(30):
                choice = pol.select(s, random.Random(seed), policy=policy, audit_epsilon=0.5)
                self.assertEqual(choice["site_id"], "cheap")
                self.assertEqual(choice["propensity"], 1.0)
        s["remaining_s"] = 25.5
        self.assertEqual(no_audit(s, "risk_only")["site_id"], "rich")


class ProjectionTests(unittest.TestCase):
    def test_project_cost_movement_switch_and_retry(self):
        c = cand("a", "w1", 3000, 4000, 0.5)
        same = pol.project_cost(c, {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}, COST)
        self.assertEqual(same["load_s"], 0.0)
        self.assertAlmostEqual(same["move_s"], 5.0)
        self.assertAlmostEqual(same["first_s"], 5.0 + 0.5 + 2.5)
        self.assertAlmostEqual(same["reserved_s"], 5.0 + 0.5 + 5.0)
        switch = pol.project_cost(c, {"wafer_id": "w2", "x_um": 3000.0, "y_um": 4000.0}, COST)
        self.assertEqual((switch["load_s"], switch["move_s"]), (20.0, 0.0))
        cold = pol.project_cost(c, None, dict(COST, retry_limit=0))
        self.assertAlmostEqual(cold["reserved_s"], cold["first_s"])
        self.assertAlmostEqual(cold["first_s"], 23.0)

    def test_inferred_params_match_explicit(self):
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        cands = [cand("a", "w1", 600, 800, 0.2), cand("b", "w2", 0, 0, 0.3)]
        s = pol.sanitize_state(state(cands, current=cur, explicit_cost=False))
        params, source = pol.resolve_cost_params(s)
        self.assertEqual(source, "inferred")
        for key, value in COST.items():
            self.assertAlmostEqual(params[key], value)

    def test_missing_move_rate_refuses_beam_but_not_immediate_policies(self):
        # No wafer mounted and no state.cost: stage speed cannot be inferred.
        s = state([cand("a", "w1", 0, 0, 0.2), cand("b", "w1", 900, 0, 0.3)], explicit_cost=False)
        params, source = pol.resolve_cost_params(pol.sanitize_state(s))
        self.assertIsNone(params["move_um_per_s"])
        self.assertEqual(source, "inferred_move_rate_unavailable")
        with self.assertRaisesRegex(ValueError, "move_um_per_s"):
            no_audit(s, "beam_route")
        with self.assertRaisesRegex(ValueError, "move_um_per_s"):
            pol.select(s, random.Random(0), policy="beam_route", audit_epsilon=1.0)
        self.assertEqual(no_audit(s, "risk_only")["site_id"], "b")
        self.assertEqual(no_audit(s, "risk_per_second")["site_id"], "b")
        s = state([cand("a", "w1", 0, 0, 0.2)])
        self.assertEqual(no_audit(s, "beam_route")["components"]["cost_source"], "state_cost")

    def test_project_cost_never_zeroes_unknown_movement(self):
        params = dict(COST, move_um_per_s=None)
        prev = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        with self.assertRaisesRegex(ValueError, "move_um_per_s is unknown"):
            pol.project_cost(cand("a", "w1", 1, 0, 0.5), prev, params)
        self.assertEqual(pol.project_cost(cand("a", "w1", 0, 0, 0.5), prev, params)["move_s"], 0.0)
        self.assertEqual(pol.project_cost(cand("a", "w2", 7, 7, 0.5), prev, params)["move_s"], 0.0)

    def test_beam_prefers_cluster_over_isolated_risk(self):
        # Mounted on w1. Isolated w2 site has the highest prior but needs a 20 s load;
        # a cheap w1 cluster yields more prior per projected second over three steps.
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        cands = [
            cand("w2_hot", "w2", 0, 0, 0.95),
            cand("c1", "w1", 100, 0, 0.5), cand("c2", "w1", 200, 0, 0.5), cand("c3", "w1", 300, 0, 0.5),
            cand("far", "w1", 90000, 0, 0.55),
        ]
        s = state(cands, current=cur)
        self.assertEqual(no_audit(s, "risk_only")["site_id"], "w2_hot")
        self.assertEqual(no_audit(s, "risk_per_second")["site_id"], "c1")
        beam = no_audit(s, "beam_route")
        self.assertEqual(beam["site_id"], "c1")
        self.assertEqual(beam["components"]["path"], ["c1", "c2", "c3"])
        expected = sum(pol.project_cost(c, p, COST)["reserved_s"] for c, p in [
            (cands[1], cur),
            (cands[2], {"wafer_id": "w1", "x_um": 100.0, "y_um": 0.0}),
            (cands[3], {"wafer_id": "w1", "x_um": 200.0, "y_um": 0.0})])
        self.assertAlmostEqual(beam["components"]["path_reserved_s"], expected)
        self.assertAlmostEqual(beam["components"]["path_prior"], 1.5)

    def test_beam_lookahead_beats_greedy_on_movement(self):
        # Greedy ratio picks 'near' (slightly better alone); beam sees that 'gate'
        # opens a tight three-site cluster while 'near' is isolated.
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        cost = dict(COST, move_um_per_s=100.0)
        cands = [
            cand("near", "w1", 100, 0, 0.30),
            cand("gate", "w1", 0, 150, 0.30),
            cand("k1", "w1", 0, 160, 0.30), cand("k2", "w1", 0, 170, 0.30),
        ]
        s = state(cands, current=cur, cost=cost)
        self.assertEqual(no_audit(s, "risk_per_second")["site_id"], "near")
        beam = no_audit(s, "beam_route")
        self.assertEqual(beam["site_id"], "gate")
        self.assertEqual(beam["components"]["path"], ["gate", "k1", "k2"])

    def test_beam_paths_respect_remaining_budget(self):
        # First step 6.5 s (1 s move), each tight follow-up ~5.51 s: two fit in 13 s.
        cur = {"wafer_id": "w1", "x_um": -1000.0, "y_um": 0.0}
        cands = [cand(f"s{i}", "w1", 10 * i, 0, 0.5) for i in range(5)]
        s = state(cands, current=cur, remaining=13.0)
        beam = no_audit(s, "beam_route")
        self.assertLessEqual(beam["components"]["path_reserved_s"], 13.0)
        self.assertEqual(len(beam["components"]["path"]), 2)

    def test_feasible_short_prefix_beats_extensions(self):
        # A cheap high-prior single site must stay eligible even though low-prior,
        # expensive extensions from it are feasible within the budget.
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        cands = [cand("hot", "w1", 0, 0, 0.9),
                 cand("x1", "w1", 5000, 0, 0.05), cand("x2", "w1", 0, 5000, 0.05)]
        s = state(cands, current=cur)
        beam = no_audit(s, "beam_route")
        self.assertEqual(beam["site_id"], "hot")
        self.assertEqual(beam["components"]["path"], ["hot"])
        self.assertAlmostEqual(beam["components"]["path_reserved_s"], 5.5)
        self.assertAlmostEqual(beam["components"]["score"], 0.9 / 5.5)
        # Two-site prefix best: after a 2 s approach the tight pair beats both the single
        # site and the pair plus an expensive low-prior third.
        far = {"wafer_id": "w1", "x_um": -2000.0, "y_um": 0.0}
        cands = [cand("p1", "w1", 0, 0, 0.6), cand("p2", "w1", 1, 0, 0.6),
                 cand("x1", "w1", 9000, 0, 0.01)]
        beam = no_audit(state(cands, current=far), "beam_route")
        self.assertEqual(beam["components"]["path"], ["p1", "p2"])

    def test_shortlist_is_bounded_and_covers_wafers(self):
        cands = []
        for w in range(5):
            for i in range(30):
                cands.append(cand(f"w{w}_{i:02d}", f"w{w}", i * 50, w * 50, 0.01 * (i % 7) + 0.01 * w))
        s = state(cands, current={"wafer_id": "w0", "x_um": 0.0, "y_um": 0.0})
        clean = pol.sanitize_state(s)
        params, _ = pol.resolve_cost_params(clean)
        short = pol._shortlist(clean["candidates"], params)
        self.assertLessEqual(len(short), pol.SHORTLIST_GLOBAL + pol.SHORTLIST_PER_WAFER * 5)
        self.assertEqual({c["wafer_id"] for c in short}, {f"w{w}" for w in range(5)})
        choice = no_audit(s, "beam_route")
        self.assertLessEqual(len(choice["components"]["path"]), pol.BEAM_DEPTH)
        self.assertEqual(choice["components"]["shortlist_size"], len(short))


class MixtureTests(unittest.TestCase):
    def setUp(self):
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        self.state = state([cand(f"s{i}", "w1", 100 * i, 0, 0.1 + 0.05 * i) for i in range(5)], current=cur)

    def test_exact_propensities(self):
        eps, n = 0.1, 5
        for policy in pol.POLICIES:
            best = no_audit(self.state, policy)["site_id"]
            seen = set()
            for seed in range(400):
                choice = pol.select(self.state, random.Random(seed), policy=policy, audit_epsilon=eps)
                expected = eps / n + (1 - eps) if choice["site_id"] == best else eps / n
                self.assertEqual(choice["propensity"], expected)
                self.assertEqual(choice["components"]["deterministic_site_id"], best)
                if not choice["audit"]:
                    self.assertEqual(choice["site_id"], best)
                    self.assertEqual(choice["reason"], f"{policy}_best")
                else:
                    self.assertEqual(choice["reason"], "audit_uniform")
                seen.add(choice["site_id"])
            self.assertEqual(seen, {f"s{i}" for i in range(5)})  # positive support for all
            total = (n - 1) * (eps / n) + (eps / n + 1 - eps)
            self.assertAlmostEqual(total, 1.0)

    def test_epsilon_zero_and_one(self):
        for seed in range(20):
            c0 = pol.select(self.state, random.Random(seed), policy="risk_only", audit_epsilon=0.0)
            self.assertEqual((c0["site_id"], c0["propensity"], c0["audit"]), ("s4", 1.0, False))
            c1 = pol.select(self.state, random.Random(seed), policy="risk_only", audit_epsilon=1.0)
            self.assertTrue(c1["audit"])
            self.assertAlmostEqual(c1["propensity"], 0.2)

    def test_seeded_audit_reproducible_and_shared_across_policies(self):
        def run(policy, seed):
            rng = random.Random(seed)
            return [pol.select(self.state, rng, policy=policy) for _ in range(50)]

        a, b = run("beam_route", 7), run("beam_route", 7)
        self.assertEqual(a, b)
        flags = {p: [c["audit"] for c in run(p, 7)] for p in pol.POLICIES}
        self.assertEqual(flags["risk_only"], flags["beam_route"])
        self.assertEqual(flags["risk_per_second"], flags["beam_route"])
        audits = sum(c["audit"] for seed in range(40) for c in run("risk_only", seed))
        self.assertTrue(0.05 * 2000 < audits < 0.15 * 2000, audits)

    def test_make_selector(self):
        sel = pol.make_selector("risk_per_second", audit_epsilon=0.0)
        self.assertEqual(sel(self.state, random.Random(1))["reason"], "risk_per_second_best")
        with self.assertRaises(ValueError):
            pol.make_selector("nope")


class TieTests(unittest.TestCase):
    def test_exact_ties_break_by_site_id(self):
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        names = ["m", "b", "z", "d"]
        for perm in itertools.permutations(names):
            cands = [cand(n, "w1", 100, 0, 0.4) for n in perm]  # identical position/cost/prior
            s = state(cands, current=cur)
            for policy in pol.POLICIES:
                self.assertEqual(no_audit(s, policy)["site_id"], "b", (policy, perm))
            self.assertEqual(no_audit(s, "beam_route")["components"]["path"], ["b", "d", "m"])

    def test_candidate_order_does_not_change_outcome(self):
        cur = {"wafer_id": "w1", "x_um": 0.0, "y_um": 0.0}
        cands = [cand(f"s{i}", f"w{i % 3}", 37 * i % 500, 91 * i % 700, (i * 13 % 17) / 17) for i in range(30)]
        s1 = state(cands, current=cur)
        s2 = state(list(reversed(cands)), current=cur)
        for policy in pol.POLICIES:
            for seed in range(5):
                self.assertEqual(pol.select(s1, random.Random(seed), policy=policy),
                                 pol.select(s2, random.Random(seed), policy=policy))


if __name__ == "__main__":
    unittest.main()
