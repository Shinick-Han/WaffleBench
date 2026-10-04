"""Focused checks for the route v5 beam planner, copied selection loop and stage guards.

Never runs the held-out test stage on real seeds.
"""
import copy
import itertools
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from inspection_review import data
from inspection_review.cli import HarnessError, read_json
from inspection_review.harness import cost_vectors
from inspection_review.policies import SelectionState, sanitize_public
from inspection_route_v5 import campaign, harness, planner
from inspection_v3 import harness as v3_harness, model as v3_model, policies as v3p

REPO = Path(__file__).resolve().parents[1]
CONFIG = read_json(REPO / 'inspection_v3/protocol.json')
CONFIG['route_v5'] = {'beam': dict(planner.BEAM_DEFAULTS)}
COST = CONFIG['cost']
HIDDEN = ('oracle', 'doi', 'seed', 'scenario', 'kind', 'electrical', 'online_p')


def tiny_view(n, wafers, seed, candidate=None):
    rng = np.random.default_rng(seed)
    public = {'lot_id': f'lot-tiny-{seed}', 'site_ids': [f's{i:03d}' for i in range(n)],
              'wafer': np.array([i % wafers for i in range(n)]), 'xy': rng.uniform(-1, 1, (n, 2)),
              'layer': np.zeros(n, int), 'candidate': np.ones(n, bool) if candidate is None else candidate,
              'features': rng.normal(size=(n, 7))}
    return sanitize_public(public, np.zeros(7), np.ones(7))


def make_state(view, p, mode='candidate_only'):
    return SelectionState(view, p, {}, p, mode, CONFIG['selection'], CONFIG['model']['classification_threshold'])


def step_cost(view, prev, j, current=None):
    """Independent CU reservation from stage position ``prev`` (site index) or ``current`` (wafer, xy)."""
    c = {k: float(COST[k]) for k in ['wafer_load', 'stage_base', 'stage_per_normalized_distance', 'dwell',
                                      'outside_rescan', 'retry_dwell']}
    if prev is None and current is None:
        switch, dist = True, 0.0
    else:
        w, xy = (view.wafer[prev], view.xy[prev]) if prev is not None else current
        switch = view.wafer[j] != w
        dist = 0.0 if switch else float(np.linalg.norm(view.xy[j] - xy))
    return ((c['wafer_load'] if switch else 0.0) + c['stage_base'] + c['stage_per_normalized_distance'] * dist
            + c['dwell'] + (0.0 if view.candidate[j] else c['outside_rescan']) + c['retry_dwell'] * int(COST['retry_limit']))


def select(state, budget):
    cv = cost_vectors(state, COST)
    affordable = state.allowed() & (cv['max'] <= budget + 1e-9)
    state.remaining_budget = budget
    return planner.make_policy('route_beam3', 0, CONFIG).select(state, cv['max'], affordable), cv, affordable


def brute_force(state, reward, budget):
    v = state.view
    cur = None if state.current_wafer is None else (state.current_wafer, state.current_xy)
    allowed = np.flatnonzero(state.allowed())
    best = None
    for length in (1, 2, 3):
        for path in itertools.permutations(allowed, length):
            costs = [step_cost(v, None, path[0], cur)] + [step_cost(v, a, b) for a, b in zip(path, path[1:])]
            if sum(costs) > budget + 1e-9:
                continue
            key = (-sum(reward[list(path)]) / sum(costs), -reward[path[0]] / costs[0], tuple(v.site_ids[j] for j in path))
            best = key if best is None or key < best else best
    return best


class BeamPlanner(unittest.TestCase):
    def test_exhaustive_on_small_instances_matches_brute_force(self):
        for seed in range(12):
            view = tiny_view(9, 1 + seed % 3, seed)
            p = np.random.default_rng(100 + seed).uniform(0.02, 0.9, view.n)
            state = make_state(view, p)
            if seed % 2:
                state.record_visit(seed % view.n)
            budget = [22.0, 40.0, 70.0, 200.0][seed % 4]
            choice, cv, affordable = select(state, budget)
            exp = brute_force(state, p, budget)
            comp = choice.components
            self.assertAlmostEqual(comp['lookahead_value'], -exp[0], places=12)
            self.assertEqual(tuple(comp['planned_site_ids']), exp[2])

    def test_reservation_projection_reload_and_budget(self):
        view = tiny_view(60, 3, 7)
        p = np.random.default_rng(3).uniform(0.0, 1.0, view.n)
        state = make_state(view, p)
        state.record_visit(4)
        for budget in (14.5, 30.0, 45.0, 360.0):
            choice, cv, affordable = select(state, budget)
            comp = choice.components
            path = comp['planned_site_indices']
            self.assertEqual(len(set(path)), len(path))
            self.assertTrue(all(state.allowed()[j] for j in path))
            costs = [step_cost(view, None, path[0], (state.current_wafer, state.current_xy))]
            costs += [step_cost(view, a, b) for a, b in zip(path, path[1:])]
            self.assertEqual(costs[0], float(cv['max'][path[0]]))
            np.testing.assert_allclose(comp['planned_step_reserved_costs'], costs, rtol=0, atol=1e-12)
            self.assertLessEqual(sum(costs), budget + 1e-9)
            self.assertAlmostEqual(comp['planned_reserved_total'], sum(costs), places=12)
            np.testing.assert_allclose(comp['planned_rewards'], p[path])
            self.assertAlmostEqual(comp['lookahead_value'], p[path].sum() / sum(costs), places=12)
            self.assertEqual(comp['beam'], planner.BEAM_DEFAULTS)
            self.assertEqual(len(comp['beam_counts']), 3 if budget > 30 else len(comp['beam_counts']))
        # A return to a previously loaded wafer pays the full wafer load again.
        self.assertEqual(step_cost(view, 0, 1), COST['wafer_load'] + COST['stage_base'] + COST['dwell'] + COST['retry_dwell'])
        reload = planner.v3p.pair_costs(state, np.array([0]), np.array([1, 3]), planner.v3p.v2._cost_config(CONFIG))[0]
        self.assertEqual(reload[0], step_cost(view, 0, 1))
        self.assertEqual(reload[1], step_cost(view, 0, 3))

    def test_ties_by_first_single_then_lexicographic_ids(self):
        n = 6
        view = sanitize_public({'lot_id': 'tie', 'site_ids': ['b', 'd', 'a', 'c', 'f', 'e'], 'wafer': np.zeros(n, int),
                                'xy': np.zeros((n, 2)), 'layer': np.zeros(n, int), 'candidate': np.ones(n, bool),
                                'features': np.zeros((n, 7))}, np.zeros(7), np.ones(7))
        p = np.full(n, 0.4)
        state = make_state(view, p)
        state.record_visit(0)  # same wafer, same position: every path has an equal value and first single value
        choice, *_ = select(state, 360.0)
        self.assertEqual(choice.components['planned_site_ids'], ['a'])  # shortest lexicographic equal tuple
        self.assertEqual(choice.index, 2)
        p = p.copy()
        p[[3, 4]] = 0.9  # 'c' and 'f' tie on value and first single; 'c' wins lexicographically
        choice, *_ = select(make_state(view, p), 360.0)
        self.assertEqual(choice.components['planned_site_ids'][:2], ['c', 'f'])

    def test_no_hidden_inputs_and_state_unchanged(self):
        view = tiny_view(40, 3, 5)
        p = np.random.default_rng(9).uniform(0.0, 1.0, view.n)
        state = make_state(view, p)
        a, *_ = select(state, 120.0)
        state.online_p = np.full(view.n, np.nan)
        state.oracle = {'doi': np.ones(view.n, bool)}
        b, *_ = select(state, 120.0)
        self.assertEqual(a.index, b.index)
        self.assertEqual(a.components, b.components)
        self.assertFalse(state.visited.any())
        self.assertFalse(any(any(h in key for h in HIDDEN) for key in a.components))
        self.assertFalse(planner.RouteBeam3Policy.updates_model)

    def test_beam_config_is_fixed(self):
        with self.assertRaises(ValueError):
            planner.beam_config({'route_v5': {'beam': {'depth': 4}}})
        with self.assertRaises(ValueError):
            planner.beam_config({'route_v5': {'beam': {'width': 4}}})
        with self.assertRaises(ValueError):
            planner.make_policy('route_vectorized', 0, CONFIG)


class SelectionLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lot = data.generate_lot(11000, 'stationary', CONFIG)
        cls.public, cls.oracle = lot['public'], lot['oracle']
        cls.view = sanitize_public(cls.public, np.zeros(7), np.ones(7))
        z = np.nan_to_num(cls.view.zscores[:, 0])
        cls.p = 1.0 / (1.0 + np.exp(-(z - 1.0)))

    def _run(self, module, policy, **kw):
        calls = []

        def review(i, attempt):
            calls.append((int(i), int(attempt)))
            return data.review_observation(self.oracle, int(i), int(attempt))
        out = module.run_selection(self.view, review, policy_name=policy, mode='candidate_only', budget=360,
                                   frozen_model={}, frozen_p=self.p, model_api=v3_model, config=CONFIG, **kw)
        self.assertEqual(calls, [(r['site_index'], a['attempt']) for r in out['rows'] for a in r['attempts']])
        return out

    @staticmethod
    def _strip(run):
        timing = {'policy_cpu_s', 'policy_wall_s', 'model_update_cpu_s', 'model_update_wall_s'}
        return [{k: v for k, v in r.items() if k not in timing} for r in run['rows']]

    def test_new_loop_matches_incumbent_v3_loop(self):
        old = self._run(v3_harness, 'route_full_gamma')
        new = self._run(harness, 'route_full_gamma')
        self.assertEqual(self._strip(old), self._strip(new))
        for key in ('spent', 'remaining', 'budget', 'stop_reason', 'online_model_hash', 'online_updates_enabled'):
            self.assertEqual(old[key], new[key])
        injected = self._run(harness, 'route_full_gamma', policy_factory=v3p.make_policy)
        self.assertEqual(self._strip(old), self._strip(injected))

    def test_beam_run_respects_cost_contract(self):
        run = self._run(harness, 'route_beam3')
        self.assertGreater(len(run['rows']), 5)
        self.assertLessEqual(run['spent'], 360 + 1e-9)
        for r in run['rows']:
            comp = r['components']
            self.assertEqual(comp['planned_site_indices'][0], r['site_index'])
            self.assertEqual(comp['planned_step_reserved_costs'][0], r['reserved_cost'])
            self.assertLessEqual(comp['planned_reserved_total'], comp['remaining_budget'] + 1e-9)
            self.assertLessEqual(r['charged'], r['reserved_cost'] + 1e-9)
        sites = [r['site_index'] for r in run['rows']]
        self.assertEqual(len(sites), len(set(sites)))
        self.assertTrue(all(self.view.candidate[sites]))


class Stages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'study'

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_plan_is_fresh_and_disjoint(self):
        protocol = read_json(campaign.PROTOCOL)
        dev, test = campaign.lot_rows(protocol, 'development'), campaign.lot_rows(protocol, 'test')
        self.assertEqual(len(dev), 40)
        self.assertEqual(len(test), 100)
        self.assertEqual(dev[8], {'seed': 11100, 'scenario': 'novel_cluster'})
        self.assertEqual(test[-1], {'seed': 12419, 'scenario': 'process_shift'})
        campaign.check_splits(protocol, CONFIG)
        bad = copy.deepcopy(protocol)
        bad['splits']['test']['base_seed'] = 7000
        with self.assertRaises(HarnessError):
            campaign.check_splits(bad, CONFIG)

    def test_test_refused_until_frozen_and_one_shot(self):
        self.root.mkdir()
        with self.assertRaisesRegex(HarnessError, 'not frozen'):
            campaign.test(self.root)
        self.assertFalse((self.root / 'test-started.json').exists())
        (self.root / 'test-started.json').write_text('{}')
        with self.assertRaisesRegex(HarnessError, 'one-shot'):
            campaign.test(self.root)
        self.assertFalse((self.root / 'lots').exists())

    def test_freeze_requires_development_and_rejects_test_lots(self):
        self.root.mkdir()
        with self.assertRaisesRegex(HarnessError, 'development'):
            campaign.freeze(self.root)
        (self.root / 'development.json').write_text('{}')
        (self.root / 'lots' / 'test').mkdir(parents=True)
        with self.assertRaisesRegex(HarnessError, 'test lots'):
            campaign.freeze(self.root)

    def test_build_refuses_existing_root_and_changed_receipt(self):
        self.root.mkdir()
        with self.assertRaises(HarnessError):
            campaign.build(self.root)
        protocol = read_json(campaign.PROTOCOL)
        receipt = Path(protocol['receipt']['default_root'])
        if not (receipt / 'freeze.json').exists():
            self.skipTest('frozen v3 receipt not present on this machine')
        bad = copy.deepcopy(protocol)
        bad['receipt']['model_hash'] = '0' * 64
        with self.assertRaisesRegex(HarnessError, 'model'):
            campaign.verify_receipt(receipt, bad)
        config, fitted, hashes = campaign.verify_receipt(receipt, protocol)
        self.assertEqual(config['v2_primary']['candidate'], 'cb400_route_full')
        self.assertEqual(v3_model.hash_model(fitted), protocol['receipt']['model_hash'])

    def test_freeze_body_covers_new_and_reused_sources(self):
        names = campaign.source_names()
        for name in ['inspection_route_v5/planner.py', 'inspection_route_v5/harness.py', 'inspection_route_v5/campaign.py',
                     'inspection_route_v5/protocol.json', 'INSPECTION_ROUTE_V5_PROTOCOL.md', 'scripts/run_inspection_route_v5.py',
                     'inspection_v3/policies.py', 'inspection_v3/harness.py', 'inspection_review/harness.py',
                     'inspection_v2/policies.py', 'pyproject.toml', 'uv.lock']:
            self.assertIn(name, names)


if __name__ == '__main__':
    unittest.main()
