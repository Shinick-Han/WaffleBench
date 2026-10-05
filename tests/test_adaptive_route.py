import copy
import random
import unittest

import torch

from adaptive_route.policy import Actor, Selector, features
from routing_poc import fixtures, policies, replay


def first_state():
    job, archive = fixtures.make_job(40000)
    captured = []
    def selector(state, rng):
        captured.append(copy.deepcopy(state)); return None
    replay.run_loop(job, archive, selector, 120)
    return captured[0]


class LearnedRoutingTests(unittest.TestCase):
    def test_zero_residual_matches_risk_per_second(self):
        state = first_state()
        actor = Actor()
        c, x, base, _ = features(state)
        best = c[int(torch.argmax(actor.scores(x,base)))]['site_id']
        expected = policies.select(state, random.Random(4), policy='risk_per_second', audit_epsilon=0)
        self.assertEqual(best, expected['site_id'])

    def test_hidden_label_or_fixture_seed_is_rejected(self):
        for key in ('reference','seed','image_path'):
            state = first_state(); state[key] = 9
            with self.assertRaises(ValueError):
                features(state)

    def test_identifier_permutation_does_not_change_numeric_features(self):
        state = first_state()
        before = {c['site_id']:x for c,x in zip(features(state)[0],features(state)[1])}
        changed = copy.deepcopy(state)
        for c in changed['candidates']:
            c['site_id']='renamed-'+c['site_id']
        after = {c['site_id']:x for c,x in zip(features(changed)[0],features(changed)[1])}
        for i,x in before.items():
            torch.testing.assert_close(x,after['renamed-'+i])

    def test_budget_and_unknown_behavior_preserved(self):
        job,archive = fixtures.make_job(40003)
        run = replay.run_loop(job,archive,Selector(Actor()),120,seed=9)
        self.assertLessEqual(run['spent_s'],120)
        self.assertEqual(len(run['rows']),len({r['site_id'] for r in run['rows']}))
        for row in run['rows']:
            if row['final_status']!='ok':
                self.assertIsNone(row['reported_doi'])
            self.assertLessEqual(row['charged_s'],row['reserved_s']+1e-9)

    def test_nonfinite_actor_falls_back_to_budgeted_baseline(self):
        actor=Actor()
        with torch.no_grad(): actor.residual[0]=float('nan')
        state=first_state()
        choice=Selector(actor)(state,random.Random(2))
        expected=policies.select(state,random.Random(2),policy='risk_per_second',audit_epsilon=.1)
        self.assertEqual(choice,expected)

    def test_no_affordable_action_stops(self):
        state=first_state(); state['remaining_s']=0
        self.assertIsNone(Selector(Actor())(state,random.Random(2)))


if __name__=='__main__': unittest.main()
