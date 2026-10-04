"""Inspection v2 selection-policy tests with small hand-built public views (NON-SCIENTIFIC fixtures).

The local ``drive`` loop mirrors the v1 harness admission/charging rules (reserve before
observing, retry billed on failure, failed/missing never a label) without touching the
v1 policy registry. Config values here are test fixtures, not frozen experiment config.
"""

from __future__ import annotations

import copy
import math
import unittest

import numpy as np

from inspection_review import harness
from inspection_review import policies as v1
from inspection_review.policies import SelectionState, sanitize_public
from inspection_v2 import policies as v2

COST = {"wafer_load": 8.0, "stage_base": 1.0, "stage_per_normalized_distance": 2.0, "dwell": 8.0,
        "retry_dwell": 4.0, "outside_rescan": 12.0, "retry_limit": 1}
SEL = {"audit_period": 5, "rescan_period": 10, "spatial_bandwidth": 0.22, "spatial_prior_strength": 4.0,
       "spatial_weight": 0.4, "uncertainty_weight": 0.35, "diversity_weight": 0.15, "novelty_weight": 0.35}


def config(**v2sel):
    return {"cost": dict(COST), "selection": dict(SEL), "model": {"classification_threshold": 0.5},
            "v2_selection": v2sel}


def view_of(wafer, xy, candidate=None, seed=0):
    n = len(wafer)
    rng = np.random.default_rng(seed)
    cand = np.ones(n, bool) if candidate is None else np.asarray(candidate, bool)
    feats = rng.normal(size=(n, 7))
    feats[~cand, :4] = np.nan
    public = {"lot_id": "fixture", "site_ids": [f"s{i}" for i in range(n)], "wafer": np.asarray(wafer),
              "xy": np.asarray(xy, float), "layer": np.zeros(n, int), "candidate": cand, "features": feats}
    return sanitize_public(public, [0.0] * 7, [1.0] * 7)


def state_of(view, p, mode="candidate_only"):
    p = np.asarray(p, float)
    return SelectionState(view, p, {"frozen": True}, p, mode, SEL, 0.5)


def random_lot(seed=3, n_per_wafer=30, cand_rate=0.6):
    rng = np.random.default_rng(seed)
    n = 3 * n_per_wafer
    wafer = np.repeat(np.arange(3), n_per_wafer)
    xy = rng.uniform(-1, 1, (n, 2))
    cand = rng.random(n) < cand_rate
    p = np.clip(rng.beta(1.2, 4.0, n), 0.001, 0.999)
    return view_of(wafer, xy, cand, seed), p


def drive(policy, view, p, outcome, budget, mode="candidate_only", expose_budget=False):
    """outcome(i, attempt) -> 'ok_pos' | 'ok_neg' | 'failure' | 'missing'."""
    state = state_of(view, p, mode)
    spent, rows = 0.0, []
    while True:
        allowed = state.allowed()
        if not allowed.any():
            break
        cv = harness.cost_vectors(state, COST)
        affordable = allowed & (spent + cv["max"] <= budget + 1e-9)
        if not affordable.any():
            break
        if expose_budget:
            state.remaining_budget = budget - spent
        before = (state.visited.copy(), list(state.selected), list(state.labeled), state.current_wafer)
        choice = policy.select(state, cv["max"], affordable)
        after = (state.visited.copy(), list(state.selected), list(state.labeled), state.current_wafer)
        assert np.array_equal(before[0], after[0]) and before[1:] == after[1:], "select mutated state"
        i = choice.index
        assert affordable[i], "unaffordable choice"
        reserved = float(cv["max"][i])
        charged = float(cv["first"][i])
        res = [outcome(i, 0)]
        if not res[0].startswith("ok"):
            res.append(outcome(i, 1))
            charged += COST["retry_dwell"]
        assert charged <= reserved + 1e-9
        spent += charged
        assert spent <= budget + 1e-9
        state.record_visit(i)
        ok = [r for r in res if r.startswith("ok")]
        if ok:
            state.record_label(i, ok[-1] == "ok_pos")
        rows.append({"i": i, "reason": choice.reason, "comp": choice.components, "charged": charged,
                     "reserved": reserved, "label": ok[-1] if ok else None})
    return rows, spent, state


ALL_FAIL = lambda i, a: "failure"  # noqa: E731
ALL_NEG = lambda i, a: "ok_neg"  # noqa: E731


class FactoryTests(unittest.TestCase):
    def test_v1_names_delegate_to_unchanged_v1_classes(self):
        for name in v1.POLICIES:
            a, b = v2.make_policy(name, 11, None), v1.make_policy(name, 11)
            self.assertIs(type(a), type(b))
            self.assertEqual(a.seed, 11)

    def test_new_policies_frozen_and_unknown_rejected(self):
        for name in v2.V2_POLICIES:
            pol = v2.make_policy(name, 1, config())
            self.assertFalse(pol.updates_model)
            self.assertEqual(pol.name, name)
        with self.assertRaises(ValueError):
            v2.make_policy("nope", 1, config())
        with self.assertRaises(ValueError):
            v2.make_policy("route_aware", 1, {"v2_selection": {}})  # no cost contract

    def test_config_validation_and_defaults(self):
        self.assertEqual(v2.selection_config({}), {**v2.V2_SELECTION_DEFAULTS, "audit_min": 0.05, "audit_max": 0.25})
        for bad in ({"audit_min": 0.3, "audit_max": 0.2}, {"audit_max": 1.5}, {"typo": 1},
                    {"audit_sampling": "greedy"}, {"shortlist_size": 0}, {"rescan_period": 2.5},
                    {"lookahead_weight": 2.0}, {"audit_prior_strength": 0}):
            with self.assertRaises(ValueError, msg=str(bad)):
                v2.selection_config({"v2_selection": bad})

    def test_cost_contract_mismatch_rejected(self):
        view, p = random_lot()
        state = state_of(view, p)
        cv = harness.cost_vectors(state, COST)
        pol = v2.make_policy("route_aware", 1, config())
        with self.assertRaises(ValueError):
            pol.select(state, cv["max"] + 1.0, state.allowed())
        with self.assertRaises(ValueError):
            pol.select(state, cv["max"], np.zeros(view.n, bool))


class CostLookaheadTests(unittest.TestCase):
    def test_second_step_costs_match_harness_after_hypothetical_visit(self):
        view, p = random_lot()
        state = state_of(view, p)
        state.record_visit(int(np.flatnonzero(view.wafer == 0)[0]))
        snap = (state.visited.copy(), list(state.selected), state.current_wafer)
        for i in (int(np.flatnonzero(view.wafer == 0)[3]), int(np.flatnonzero(view.wafer == 2)[1])):
            got = v2.second_step_costs(state, i, v2._cost_config(config()))
            sim = copy.deepcopy(state)
            sim.record_visit(i)
            np.testing.assert_allclose(got, harness.cost_vectors(sim, COST)["max"])
        # revisit of a previously loaded wafer pays the load again
        i1 = int(np.flatnonzero(view.wafer == 1)[0])
        c = v2.second_step_costs(state, i1, v2._cost_config(config()))
        self.assertTrue(np.all(c[view.wafer == 0] >= COST["wafer_load"] + COST["stage_base"] + COST["dwell"]))
        self.assertEqual((state.visited.copy().tolist(), list(state.selected), state.current_wafer),
                         (snap[0].tolist(), snap[1], snap[2]))
        np.testing.assert_allclose(v2.first_step_costs(state, v2._cost_config(config())),
                                   harness.cost_vectors(state, COST)["max"])

    def test_route_defers_adverse_switch_that_greedy_takes(self):
        # stage on wafer 0 at origin; two cheap mid-p sites here, one high-p site on wafer 1
        wafer = [0, 0, 0, 1, 1]
        xy = [[0, 0], [0.1, 0], [0.15, 0], [0, 0], [0.9, 0.9]]
        p = [0.01, 0.5, 0.5, 0.95, 0.01]
        view = view_of(wafer, xy)
        state = state_of(view, p)
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        aff = state.allowed()
        greedy = v1.make_policy("learned", 0).select(state, cv["max"], aff).index
        self.assertEqual(greedy, 3)
        ch = v2.make_policy("route_aware", 0, config()).select(state, cv["max"], aff)
        self.assertEqual(ch.index, 1)
        self.assertEqual(ch.components["next_site_index"], 3)
        self.assertFalse(ch.components["wafer_switch_now"])
        self.assertTrue(ch.components["wafer_switch_next"])
        self.assertAlmostEqual(ch.components["pair_reserved_cost"], cv["max"][1] + 21.0)

    def test_route_takes_switch_when_follow_up_on_new_wafer_is_cheap(self):
        wafer = [0, 0, 1, 1]
        xy = [[0, 0], [0.8, 0.8], [0, 0], [0.05, 0]]
        p = [0.01, 0.30, 0.9, 0.9]
        view = view_of(wafer, xy)
        state = state_of(view, p)
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        ch = v2.make_policy("route_aware", 0, config()).select(state, cv["max"], state.allowed())
        self.assertIn(ch.index, (2, 3))
        self.assertTrue(ch.components["wafer_switch_now"])
        self.assertFalse(ch.components["wafer_switch_next"])

    def test_budget_masks_second_step_and_single_choice(self):
        view = view_of([0, 0, 1], [[0, 0], [0.1, 0], [0, 0]])
        state = state_of(view, [0.2, 0.9, 0.9])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        state.remaining_budget = float(cv["max"][1]) + 0.5  # only one more step fits
        aff = state.allowed() & (cv["max"] <= state.remaining_budget)
        self.assertEqual(int(aff.sum()), 1)
        for name in v2.V2_POLICIES:
            ch = v2.make_policy(name, 0, config()).select(state, cv["max"], aff)
            self.assertEqual(ch.index, 1, name)
            if "next_site_index" in ch.components:
                self.assertIsNone(ch.components["next_site_index"])
                self.assertEqual(ch.components["budget_source"], "state_remaining_budget")

    def test_inferred_budget_source(self):
        view, p = random_lot()
        state = state_of(view, p)
        cv = harness.cost_vectors(state, COST)
        self.assertEqual(v2.remaining_budget(state, cv["max"], state.allowed())[1],
                         "unbinding_all_allowed_affordable")
        state.record_visit(int(np.flatnonzero(view.candidate)[0]))
        cv = harness.cost_vectors(state, COST)
        aff = state.allowed() & (cv["max"] < np.max(cv["max"][state.allowed()]))
        b, src = v2.remaining_budget(state, cv["max"], aff)
        self.assertEqual(src, "inferred_lower_bound")
        self.assertLessEqual(b, float(np.max(cv["max"])))

    def test_route_runs_respect_reservation_and_replan(self):
        view, p = random_lot()
        for expose in (False, True):
            rows, spent, state = drive(v2.make_policy("route_aware", 4, config()), view, p,
                                       lambda i, a: "failure" if i % 4 == 0 else "ok_neg", 150, expose_budget=expose)
            self.assertLessEqual(spent, 150 + 1e-9)
            self.assertTrue(rows)
            self.assertTrue(all(view.candidate[r["i"]] for r in rows))
            # the planned next site is a suggestion: the following decision is recomputed
            self.assertTrue(all("lookahead_value" in r["comp"] for r in rows))

    def test_deterministic_ties_choose_lowest_index(self):
        view = view_of([0, 0, 0], [[0, 0], [0.5, 0], [-0.5, 0]])
        state = state_of(view, [0.1, 0.5, 0.5])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        self.assertEqual(v2.make_policy("route_aware", 0, config()).select(state, cv["max"], state.allowed()).index, 1)


class AdaptiveAuditTests(unittest.TestCase):
    def _audits(self, rows):
        return [r for r in rows if r["comp"].get("branch") == "audit"]

    def _check_bounds(self, rows, lo, hi):
        audits = 0
        for k, r in enumerate(rows, start=1):
            audits += r["comp"].get("branch") == "audit"
            self.assertLessEqual(audits, hi * k + 1e-9)
            if r["comp"].get("audit_pool_size", 0) > 0 and r["comp"].get("branch") != "rescan":
                self.assertGreaterEqual(audits, math.floor(lo * k + 1e-9))

    def test_no_labels_all_failures_keeps_bounds_and_no_label_updates(self):
        view, p = random_lot()
        for name in ("adaptive_audit", "adaptive_route"):
            rows, spent, state = drive(v2.make_policy(name, 7, config(audit_min=0.1, audit_max=0.3)), view, p,
                                       ALL_FAIL, 300)
            self.assertEqual(state.labeled, [])
            self.assertTrue(all(r["charged"] == r["reserved"] for r in rows))  # retry billed
            self._check_bounds(rows, 0.1, 0.3)
            self.assertTrue(all(r["comp"]["audit_labels"] == 0 and r["comp"]["exploit_labels"] == 0 for r in rows))
            self.assertGreaterEqual(len(self._audits(rows)), math.floor(0.1 * len(rows)))

    def test_rare_labels_drive_audit_toward_floor_and_audits_stay_in_candidates(self):
        view, p = random_lot(n_per_wafer=60)
        rows, _, _ = drive(v2.make_policy("adaptive_audit", 9, config(audit_min=0.05, audit_max=0.4)), view, p,
                           lambda i, a: "ok_pos" if i == 5 else "ok_neg", 600)
        self._check_bounds(rows, 0.05, 0.4)
        audits = self._audits(rows)
        self.assertTrue(audits)
        for r in audits:
            self.assertTrue(view.candidate[r["i"]])
            self.assertLess(p[r["i"]], 0.5)
            self.assertGreater(r["comp"]["selection_propensity"], 0.0)
            self.assertLessEqual(r["comp"]["selection_propensity"], 1.0)
            self.assertEqual(r["comp"]["ope_claim"], "none")
            self.assertIn("conditional", r["comp"]["propensity_scope"])
        late = rows[-1]["comp"]
        self.assertLess(late["audit_posterior_theta"], float(p[(p < 0.5) & view.candidate].mean()))

    def test_positive_audit_labels_raise_target(self):
        view, p = random_lot(n_per_wafer=60)
        neg, _, _ = drive(v2.make_policy("adaptive_audit", 9, config(audit_min=0.0, audit_max=0.5)), view, p,
                          ALL_NEG, 500)
        pos, _, _ = drive(v2.make_policy("adaptive_audit", 9, config(audit_min=0.0, audit_max=0.5)), view, p,
                          lambda i, a: "ok_pos" if p[i] < 0.5 else "ok_neg", 500)
        self.assertGreater(pos[-1]["comp"]["audit_target_fraction"], neg[-1]["comp"]["audit_target_fraction"])
        self.assertGreaterEqual(len(self._audits(pos)), len(self._audits(neg)))

    def test_audit_min_zero_ablation_and_disabled(self):
        view, p = random_lot()
        rows, _, _ = drive(v2.make_policy("adaptive_audit", 2, config(audit_min=0.0, audit_max=0.0)), view, p,
                           ALL_NEG, 300)
        self.assertEqual(self._audits(rows), [])
        self.assertTrue(all(r["reason"] == "frozen_probability_per_cost" for r in rows))
        rows, _, _ = drive(v2.make_policy("adaptive_audit", 2, config(audit_min=0.0, audit_max=0.3)), view, p,
                           ALL_NEG, 300)
        self._check_bounds(rows, 0.0, 0.3)

    def test_failed_audit_review_gives_no_audit_label(self):
        view, p = random_lot()
        rows, _, state = drive(v2.make_policy("adaptive_audit", 5, config(audit_min=0.2, audit_max=0.4)), view, p,
                               lambda i, a: "missing" if p[i] < 0.5 else "ok_pos", 300)
        self.assertTrue(self._audits(rows))
        self.assertTrue(all(r["comp"]["audit_labels"] == 0 for r in rows))
        self.assertTrue(all(np.isnan(state.label[r["i"]]) for r in self._audits(rows)))

    def test_deterministic_given_seed_and_local_rng(self):
        view, p = random_lot()
        cfg = config(audit_min=0.2, audit_max=0.4)
        a = [r["i"] for r in drive(v2.make_policy("adaptive_audit", 13, cfg), view, p, ALL_NEG, 300)[0]]
        np.random.seed(0)
        b = [r["i"] for r in drive(v2.make_policy("adaptive_audit", 13, cfg), view, p, ALL_NEG, 300)[0]]
        self.assertEqual(a, b)
        seeds = {tuple(r["i"] for r in drive(v2.make_policy("adaptive_audit", s, cfg), view, p, ALL_NEG, 300)[0])
                 for s in range(5)}
        self.assertGreater(len(seeds), 1)

    def test_outside_rescan_only_in_with_rescan_and_scheduled(self):
        view, p = random_lot(cand_rate=0.5)
        cfg = config(rescan_period=4)
        for name in ("adaptive_audit", "adaptive_route"):
            rows, _, _ = drive(v2.make_policy(name, 1, cfg), view, p, ALL_NEG, 400)
            self.assertTrue(all(view.candidate[r["i"]] for r in rows))
            rows, _, _ = drive(v2.make_policy(name, 1, cfg), view, p, ALL_NEG, 400, mode="with_rescan")
            resc = [(k, r) for k, r in enumerate(rows, start=1) if r["reason"] == "scheduled_outside_rescan"]
            self.assertTrue(resc)
            for k, r in resc:
                self.assertEqual(k % 4, 0)
                self.assertFalse(view.candidate[r["i"]])
            for r in self._audits(rows):
                self.assertTrue(view.candidate[r["i"]])

    def test_single_affordable_choice_and_no_audit_pool(self):
        view = view_of([0, 0, 0], [[0, 0], [0.1, 0], [0.2, 0]])
        state = state_of(view, [0.9, 0.9, 0.9])  # no frozen negatives: audit pool empty
        cv = harness.cost_vectors(state, COST)
        aff = np.array([False, True, False])
        ch = v2.make_policy("adaptive_audit", 0, config(audit_min=1.0, audit_max=1.0)).select(state, cv["max"], aff)
        self.assertEqual(ch.index, 1)
        self.assertEqual(ch.components["branch"], "exploit")
        self.assertTrue(ch.components["branch_rule"].endswith("unmet_no_audit_pool"))

    def test_policy_instance_bound_to_one_run(self):
        view, p = random_lot()
        pol = v2.make_policy("adaptive_audit", 0, config())
        s1, s2 = state_of(view, p), state_of(view, p)
        cv = harness.cost_vectors(s1, COST)
        pol.select(s1, cv["max"], s1.allowed())
        with self.assertRaises(RuntimeError):
            pol.select(s2, cv["max"], s2.allowed())

    def test_uses_frozen_not_online_probability(self):
        view, p = random_lot()
        state = state_of(view, p)
        state.set_online({"online": True}, 1.0 - p)  # adversarial online scores must be ignored
        cv = harness.cost_vectors(state, COST)
        ref = state_of(view, p)
        for name in v2.V2_POLICIES:
            a = v2.make_policy(name, 3, config()).select(state, cv["max"], state.allowed()).index
            b = v2.make_policy(name, 3, config()).select(ref, cv["max"], ref.allowed()).index
            self.assertEqual(a, b, name)


if __name__ == "__main__":
    unittest.main()
