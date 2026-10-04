"""Inspection v3 selection-policy tests with small hand-built public views (NON-SCIENTIFIC fixtures).

The local ``drive`` loop mirrors the v1 harness admission/charging rules (reserve before
observing, retry billed on failure, failed/missing never a label). Config values here are
test fixtures, not frozen experiment config.
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
from inspection_v3 import policies as v3

COST = {"wafer_load": 8.0, "stage_base": 1.0, "stage_per_normalized_distance": 2.0, "dwell": 8.0,
        "retry_dwell": 4.0, "outside_rescan": 12.0, "retry_limit": 1}
SEL = {"audit_period": 5, "rescan_period": 10, "spatial_bandwidth": 0.22, "spatial_prior_strength": 4.0,
       "spatial_weight": 0.4, "uncertainty_weight": 0.35, "diversity_weight": 0.15, "novelty_weight": 0.35}


def config(v2sel=None, **v3sel):
    return {"cost": dict(COST), "selection": dict(SEL), "model": {"classification_threshold": 0.5},
            "v2_selection": dict(v2sel or {}), "v3_selection": v3sel}


def view_of(wafer, xy, candidate=None, seed=0):
    n = len(wafer)
    rng = np.random.default_rng(seed)
    cand = np.ones(n, bool) if candidate is None else np.asarray(candidate, bool)
    feats = rng.normal(size=(n, 7))
    feats[~cand, :4] = np.nan
    public = {"lot_id": "fixture", "site_ids": [f"s{i}" for i in range(n)], "wafer": np.asarray(wafer),
              "xy": np.asarray(xy, float), "layer": np.zeros(n, int), "candidate": cand, "features": feats}
    return sanitize_public(public, [0.0] * 7, [1.0] * 7)


def state_of(view, p, mode="candidate_only", reward=None):
    p = np.asarray(p, float)
    s = SelectionState(view, p, {"frozen": True}, p, mode, SEL, 0.5)
    if reward is not None:
        r = np.array(reward, float)
        r.flags.writeable = False
        s.selection_reward = r
    return s


def random_lot(seed=3, n_per_wafer=30, cand_rate=0.6, n_wafers=3):
    rng = np.random.default_rng(seed)
    n = n_wafers * n_per_wafer
    wafer = np.repeat(np.arange(n_wafers), n_per_wafer)
    xy = rng.uniform(-1, 1, (n, 2))
    cand = rng.random(n) < cand_rate
    p = np.clip(rng.beta(1.2, 4.0, n), 0.001, 0.999)
    return view_of(wafer, xy, cand, seed), p


def snapshot(state):
    return (state.visited.copy().tolist(), list(state.selected), list(state.labeled), state.current_wafer,
            None if state.current_xy is None else state.current_xy.tolist(),
            np.nan_to_num(state.label, nan=-1).tolist(), state.frozen_p.tolist(), state.online_p.tolist())


def drive(policy, view, p, outcome, budget, mode="candidate_only", expose_budget=False, reward=None):
    """outcome(i, attempt) -> 'ok_pos' | 'ok_neg' | 'failure' | 'missing'."""
    state = state_of(view, p, mode, reward)
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
        before = snapshot(state)
        choice = policy.select(state, cv["max"], affordable)
        assert snapshot(state) == before, "select mutated state"
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
        rows.append({"i": i, "reason": choice.reason, "score": choice.score, "comp": choice.components,
                     "charged": charged, "reserved": reserved})
    return rows, spent, state


def brute_force(state, reward, max_cost, affordable, budget, gamma, full, size=16, per_wafer=2):
    """Reference loop over shortlist x all allowed second sites using v2.second_step_costs."""
    cost = v2._cost_config(config())
    single = reward / max_cost
    short = v3.shortlist(single, state.view.wafer, affordable, size, per_wafer)
    allowed = state.allowed()
    best = None
    for i in short:
        c1 = float(max_cost[i])
        c2 = v2.second_step_costs(state, int(i), cost)
        cand = []
        for j in np.flatnonzero(allowed):
            if j == i or c1 + c2[j] > budget + 1e-9:
                continue
            den = c1 + gamma * c2[j] if full else c1 + c2[j]
            cand.append(((reward[i] + gamma * reward[j]) / den, -int(j)))
        value, j = (max(cand)[0], -max(cand)[1]) if cand else (float(single[i]), None)
        key = (value, float(single[i]), -int(i))
        if best is None or key > best[0]:
            best = (key, int(i), j)
    return best[1], best[0][0], best[2]


FAIL_SOME = lambda i, a: "failure" if i % 4 == 0 else ("missing" if i % 7 == 0 else "ok_neg")  # noqa: E731


class FactoryTests(unittest.TestCase):
    def test_prior_names_delegate_unchanged(self):
        for name in v1.POLICIES:
            self.assertIs(type(v3.make_policy(name, 11, None)), type(v1.make_policy(name, 11)))
        for name in v2.V2_POLICIES:
            a, b = v3.make_policy(name, 11, config()), v2.make_policy(name, 11, config())
            self.assertIs(type(a), type(b))
            self.assertEqual(a.seed, 11)

    def test_new_policies_frozen_and_config_validated(self):
        for name in v3.V3_POLICIES:
            pol = v3.make_policy(name, 1, config())
            self.assertFalse(pol.updates_model)
            self.assertEqual(pol.name, name)
        self.assertEqual(v3.POLICIES, v2.POLICIES + v3.V3_POLICIES)
        with self.assertRaises(ValueError):
            v3.make_policy("nope", 1, config())
        with self.assertRaises(ValueError):
            v3.make_policy("route_vectorized", 1, {"v3_selection": {}})  # no cost contract
        self.assertEqual(v3.selection_config({}), v3.V3_SELECTION_DEFAULTS)
        for bad in ({"gamma": 1.5}, {"gamma": -0.1}, {"gamma": float("nan")}, {"gamma": True},
                    {"chunk_elements": 0}, {"chunk_elements": 2.5}, {"typo": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                v3.make_policy("route_full_gamma", 1, config(**bad))

    def test_cost_contract_mismatch_and_empty_affordable_rejected(self):
        view, p = random_lot()
        state = state_of(view, p)
        cv = harness.cost_vectors(state, COST)
        for name in v3.V3_POLICIES:
            pol = v3.make_policy(name, 1, config())
            with self.assertRaises(ValueError):
                pol.select(state, cv["max"] + 1.0, state.allowed())
            with self.assertRaises(ValueError):
                pol.select(state, cv["max"], np.zeros(view.n, bool))


class RewardTests(unittest.TestCase):
    def test_invalid_selection_reward_rejected(self):
        view, p = random_lot()
        cv = harness.cost_vectors(state_of(view, p), COST)
        bad = [np.full(view.n, np.nan), np.full(view.n, 1.2), np.full(view.n, -0.1), np.ones(view.n - 1),
               np.full(view.n, np.inf)]
        for r in bad:
            state = state_of(view, p)
            state.selection_reward = r
            for name in v3.V3_POLICIES:
                with self.assertRaises(ValueError, msg=name):
                    v3.make_policy(name, 0, config()).select(state, cv["max"], state.allowed())

    def test_custom_reward_drives_choice_and_is_logged(self):
        view = view_of([0, 0, 0, 0], [[0, 0], [0.1, 0], [0.2, 0], [0.3, 0]])
        p = [0.1, 0.9, 0.2, 0.2]
        reward = [0.1, 0.05, 0.8, 0.8]  # yield-adjusted reward reverses sites 1 and 2
        frozen = state_of(view, p)
        custom = state_of(view, p, reward=reward)
        frozen.record_visit(0)
        custom.record_visit(0)
        cv = harness.cost_vectors(frozen, COST)
        for name in v3.V3_POLICIES:
            a = v3.make_policy(name, 0, config()).select(frozen, cv["max"], frozen.allowed())
            b = v3.make_policy(name, 0, config()).select(custom, cv["max"], custom.allowed())
            self.assertEqual(a.index, 1, name)
            self.assertEqual(a.components["reward_source"], "frozen_p")
            self.assertEqual(b.index, 2, name)
            self.assertEqual(b.components["reward_source"], "state_selection_reward")
            self.assertEqual(b.components["selection_reward"], 0.8)
            self.assertEqual(b.components["frozen_p"], 0.2)
        g = v3.make_policy("yield_greedy", 0, config()).select(custom, cv["max"], custom.allowed())
        self.assertAlmostEqual(g.score, 0.8 / cv["max"][2])

    def test_online_probability_ignored(self):
        view, p = random_lot()
        state, ref = state_of(view, p), state_of(view, p)
        state.set_online({"online": True}, 1.0 - p)
        cv = harness.cost_vectors(state, COST)
        for name in v3.V3_POLICIES:
            a = v3.make_policy(name, 3, config()).select(state, cv["max"], state.allowed())
            b = v3.make_policy(name, 3, config()).select(ref, cv["max"], ref.allowed())
            self.assertEqual((a.index, a.score), (b.index, b.score), name)

    def test_components_hold_only_public_fields(self):
        view, p = random_lot()
        rows, _, _ = drive(v3.make_policy("yield_route", 0, config()), view, p, FAIL_SOME, 120, reward=p * 0.7)
        banned = ("oracle", "online", "seed", "scenario", "truth", "label")
        for r in rows:
            for k in r["comp"]:
                self.assertFalse(any(b in k for b in banned), k)


class ParityTests(unittest.TestCase):
    def _compare(self, rows_a, rows_b):
        self.assertEqual([r["i"] for r in rows_a], [r["i"] for r in rows_b])
        for a, b in zip(rows_a, rows_b):
            self.assertEqual(a["score"], b["score"])
            for k in ("single_step_value", "lookahead_value", "max_cost", "next_site_index", "next_site_id",
                      "pair_reserved_cost", "wafer_switch_now", "wafer_switch_next", "shortlist_size",
                      "budget_for_lookahead", "budget_source"):
                self.assertEqual(a["comp"][k], b["comp"][k], k)

    def test_route_vectorized_equals_v2_route_aware_gamma_half(self):
        cfg = config({"lookahead_weight": 0.5})
        for seed, budget, expose, mode in ((3, 150, False, "candidate_only"), (5, 150, True, "candidate_only"),
                                           (7, 90, True, "with_rescan"), (9, 400, False, "with_rescan"),
                                           (11, 2000, False, "candidate_only")):
            view, p = random_lot(seed=seed, n_wafers=4)
            a = drive(v3.make_policy("route_vectorized", 0, cfg), view, p, FAIL_SOME, budget, mode, expose)
            b = drive(v2.make_policy("route_aware", 0, cfg), view, p, FAIL_SOME, budget, mode, expose)
            self._compare(a[0], b[0])
            self.assertEqual(a[1], b[1])
            self.assertTrue(all(r["comp"]["gamma"] == 0.5 and r["comp"]["gamma_scope"] == "reward_only"
                                for r in a[0]))

    def test_parity_with_tiny_shortlist_and_single_row_chunks(self):
        v2sel = {"lookahead_weight": 0.5, "shortlist_size": 3, "route_per_wafer": 1}
        view, p = random_lot(seed=21, n_wafers=3)
        a = drive(v3.make_policy("route_vectorized", 0, config(v2sel, chunk_elements=1)), view, p, FAIL_SOME, 200,
                  expose_budget=True)
        b = drive(v2.make_policy("route_aware", 0, config(v2sel)), view, p, FAIL_SOME, 200, expose_budget=True)
        self._compare(a[0], b[0])
        self.assertGreater(a[0][0]["comp"]["pair_chunks"], 1)

    def test_yield_greedy_equals_learned_under_frozen(self):
        view, p = random_lot(seed=4)
        a = drive(v3.make_policy("yield_greedy", 0, config()), view, p, FAIL_SOME, 200, mode="with_rescan")
        b = drive(v1.make_policy("learned", 0), view, p, FAIL_SOME, 200, mode="with_rescan")
        self.assertEqual([r["i"] for r in a[0]], [r["i"] for r in b[0]])

    def test_full_gamma_matches_brute_force_and_chunks(self):
        view, p = random_lot(seed=8, n_wafers=4)
        reward = np.clip(p * np.linspace(0.3, 1.0, view.n), 0, 1)
        state = state_of(view, p, reward=reward)
        rng = np.random.default_rng(0)
        for _ in range(6):
            state.record_visit(int(rng.choice(np.flatnonzero(state.allowed()))))
        cv = harness.cost_vectors(state, COST)
        state.remaining_budget = float(np.median(cv["max"])) * 1.6  # binding for many pairs
        aff = state.allowed() & (cv["max"] <= state.remaining_budget)
        for gamma in (1.0, 0.5, 0.0):
            want = brute_force(state, reward, cv["max"], aff, state.remaining_budget, gamma, True)
            for chunk in (1, 7, 1 << 20):
                ch = v3.make_policy("route_full_gamma", 0, config(gamma=gamma, chunk_elements=chunk)).select(
                    state, cv["max"], aff)
                self.assertEqual(ch.index, want[0])
                self.assertAlmostEqual(ch.score, want[1], places=12)
                self.assertEqual(ch.components["next_site_index"], want[2])
                self.assertEqual(ch.components["gamma_scope"], "reward_and_cost")
        want = brute_force(state, reward, cv["max"], aff, state.remaining_budget, 0.5, False)
        ch = v3.make_policy("route_vectorized", 0, config({"lookahead_weight": 0.5})).select(state, cv["max"], aff)
        self.assertEqual((ch.index, ch.components["next_site_index"]), (want[0], want[2]))
        self.assertEqual(ch.score, want[1])

    def test_full_gamma_discounts_cost_as_well_as_reward(self):
        view = view_of([0, 0, 0], [[0, 0], [0.1, 0], [0.2, 0]])
        state = state_of(view, [0.1, 0.6, 0.6])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        ch = v3.make_policy("route_full_gamma", 0, config(gamma=0.5)).select(state, cv["max"], state.allowed())
        c1 = float(cv["max"][ch.index])
        c2 = ch.components["pair_reserved_cost"] - c1
        self.assertAlmostEqual(ch.score, (0.6 + 0.5 * 0.6) / (c1 + 0.5 * c2))
        nv = v3.make_policy("route_vectorized", 0, config({"lookahead_weight": 0.5})).select(
            state, cv["max"], state.allowed())
        self.assertAlmostEqual(nv.score, (0.6 + 0.5 * 0.6) / (c1 + c2))


class CostAndBudgetTests(unittest.TestCase):
    def test_pair_costs_match_v2_and_harness_including_return_and_retry(self):
        view, p = random_lot(cand_rate=0.5)
        state = state_of(view, p)
        state.record_visit(int(np.flatnonzero(view.wafer == 0)[0]))
        state.record_visit(int(np.flatnonzero(view.wafer == 1)[0]))  # wafer 0 previously loaded
        cost = v2._cost_config(config())
        rows = np.array([int(np.flatnonzero(view.wafer == w)[2]) for w in (0, 1, 2)])
        cols = np.flatnonzero(state.allowed())
        got = v3.pair_costs(state, rows, cols, cost)
        for r, i in enumerate(rows):
            np.testing.assert_array_equal(got[r], v2.second_step_costs(state, int(i), cost)[cols])
            sim = copy.deepcopy(state)
            sim.record_visit(int(i))
            np.testing.assert_allclose(got[r], harness.cost_vectors(sim, COST)["max"][cols])
        # from wafer 1, returning to the previously loaded wafer 0 pays the load and the retry reserve
        back = got[1][view.wafer[cols] == 0]
        self.assertTrue(np.all(back >= COST["wafer_load"] + COST["stage_base"] + COST["dwell"]
                               + COST["retry_dwell"] * COST["retry_limit"]))

    def test_second_site_outside_first_affordable_set(self):
        # site 2 is too expensive now (wafer switch) but becomes the cheap follow-up of site 3
        wafer = [0, 0, 1, 1]
        xy = [[0, 0], [0.9, 0.9], [0, 0], [0.02, 0]]
        view = view_of(wafer, xy)
        state = state_of(view, [0.05, 0.05, 0.9, 0.9])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        aff = np.array([False, True, False, True])  # harness admission mask excludes site 2
        state.remaining_budget = 1000.0
        for name in ("route_vectorized", "route_full_gamma", "yield_route"):
            ch = v3.make_policy(name, 0, config()).select(state, cv["max"], aff)
            self.assertEqual(ch.index, 3, name)
            self.assertEqual(ch.components["next_site_index"], 2, name)
            self.assertFalse(ch.components["wafer_switch_next"])

    def test_budget_binding_masks_pairs_and_falls_back_to_single(self):
        view = view_of([0, 0, 1], [[0, 0], [0.1, 0], [0, 0]])
        state = state_of(view, [0.2, 0.9, 0.9])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        state.remaining_budget = float(cv["max"][1]) + 0.5  # only one more step fits
        aff = state.allowed() & (cv["max"] <= state.remaining_budget)
        for name in v3.V3_POLICIES:
            ch = v3.make_policy(name, 0, config()).select(state, cv["max"], aff)
            self.assertEqual(ch.index, 1, name)
            self.assertEqual(ch.components["budget_source"], "state_remaining_budget")
            self.assertEqual(ch.components["remaining_budget"], state.remaining_budget)
            if name != "yield_greedy":
                self.assertIsNone(ch.components["next_site_index"])
                self.assertEqual(ch.score, ch.components["single_step_value"])
                self.assertEqual(ch.components["pair_reserved_cost"], float(cv["max"][1]))

    def test_partial_pair_availability_prefers_feasible_pair_row(self):
        # a single-step-best site with no feasible pair competes on its single value only
        wafer = [0, 0, 0, 1]
        xy = [[0, 0], [0.05, 0], [0.1, 0], [0, 0]]
        view = view_of(wafer, xy)
        state = state_of(view, [0.1, 0.3, 0.3, 0.3])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        state.remaining_budget = float(cv["max"][1] + cv["max"][1]) + 0.5
        aff = state.allowed() & (cv["max"] <= state.remaining_budget)
        want = brute_force(state, np.asarray(state.frozen_p), cv["max"], aff, state.remaining_budget, 1.0, True)
        ch = v3.make_policy("route_full_gamma", 0, config()).select(state, cv["max"], aff)
        self.assertEqual((ch.index, ch.components["next_site_index"]), (want[0], want[2]))
        self.assertTrue(aff[ch.index])

    def test_runs_respect_reservation_and_replan_every_observation(self):
        view, p = random_lot(seed=12, n_wafers=4)
        for name in v3.V3_POLICIES:
            for expose in (False, True):
                rows, spent, state = drive(v3.make_policy(name, 4, config()), view, p, FAIL_SOME, 180,
                                           mode="with_rescan", expose_budget=expose, reward=p * 0.9)
                self.assertLessEqual(spent, 180 + 1e-9)
                self.assertTrue(rows)
                self.assertTrue(any(r["charged"] == r["reserved"] for r in rows))  # retry billed
                self.assertEqual(len(set(r["i"] for r in rows)), len(rows))
        # receding horizon: next decision is recomputed, not the stored plan
        rows, _, _ = drive(v3.make_policy("route_full_gamma", 0, config()), view, p, FAIL_SOME, 400)
        self.assertTrue(all("lookahead_value" in r["comp"] for r in rows))


class DeterminismTests(unittest.TestCase):
    def test_ties_choose_lowest_site_index(self):
        view = view_of([0, 0, 0, 0], [[0, 0], [0.5, 0], [-0.5, 0], [1.5, 0]])  # 1->2 and 1->3 equidistant
        state = state_of(view, [0.1, 0.5, 0.5, 0.5])
        state.record_visit(0)
        cv = harness.cost_vectors(state, COST)
        for name in v3.V3_POLICIES:
            ch = v3.make_policy(name, 0, config()).select(state, cv["max"], state.allowed())
            self.assertEqual(ch.index, 1, name)
            if name != "yield_greedy":
                self.assertEqual(ch.components["next_site_index"], 2, name)

    def test_repeatable_independent_of_seed_and_global_rng(self):
        view, p = random_lot(seed=6)
        for name in v3.V3_POLICIES:
            a = [r["i"] for r in drive(v3.make_policy(name, 1, config()), view, p, FAIL_SOME, 200)[0]]
            np.random.seed(123)
            b = [r["i"] for r in drive(v3.make_policy(name, 99, config()), view, p, FAIL_SOME, 200)[0]]
            self.assertEqual(a, b, name)

    def test_reward_array_not_mutated_and_copy_read_only(self):
        view, p = random_lot()
        reward = np.clip(p + 0.05, 0, 1)
        state = state_of(view, p)
        state.selection_reward = reward  # writable caller array
        keep = reward.copy()
        cv = harness.cost_vectors(state, COST)
        for name in v3.V3_POLICIES:
            v3.make_policy(name, 0, config()).select(state, cv["max"], state.allowed())
        np.testing.assert_array_equal(reward, keep)
        r, src = v3.selection_reward(state)
        self.assertFalse(r.flags.writeable)
        self.assertEqual(src, "state_selection_reward")
        self.assertTrue(math.isfinite(float(r.sum())))


if __name__ == "__main__":
    unittest.main()
