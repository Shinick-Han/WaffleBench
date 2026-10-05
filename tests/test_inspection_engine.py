"""Boundary, actual coupling, accounting and independent matching tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from inspection_engine.core import Config, analyze, apply_reviews, plan
from inspection_engine.evaluation import evaluate, match_candidates


def frame(candidates=None, quality='valid'):
    return {'schema_version': 1, 'frame_id': 'fixture', 'width': 20, 'height': 20,
            'image_sha256': 'a'*64, 'model_sha256': 'b'*64, 'data_mode': 'synthetic_fixture',
            'baseline_source': 'test fixture', 'quality': quality,
            'candidates': candidates if candidates is not None else [candidate('old', [1,1,5,5])]}


def candidate(cid, box, cost=10):
    return {'candidate_id': cid, 'bbox_px': box, 'review_bound_s': cost}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.p = np.zeros((20,20), dtype=np.float32)
        self.p[2:4,2:4] = .95
        self.p[12:15,12:15] = .9

    def test_new_outside_incumbent_and_no_duplicate_inside(self):
        result = analyze(frame(), self.p)
        self.assertEqual(len(result['additional']), 1)
        self.assertEqual(result['additional'][0]['bbox_px'], [12,12,15,15])
        self.assertEqual(len(result['associated_regions']), 1)
        self.assertTrue(result['existing'][0]['retained'])
        self.assertIsNone(result['additional'][0]['review_label'])

    def test_ai_evidence_changes_actual_plan_order(self):
        f = frame([candidate('a', [1,1,5,5]), candidate('z', [12,12,16,16])])
        r1 = plan(analyze(f, self.p), 10)
        p2 = self.p.copy()
        p2[2:4,2:4] = .1
        r2 = plan(analyze(f, p2), 10)
        self.assertEqual(r1['admitted'][0]['candidate_id'], 'a')
        self.assertEqual(r2['admitted'][0]['candidate_id'], 'z')

    def test_nuisance_is_retained_and_not_physical_negative(self):
        r = analyze(frame(), np.zeros_like(self.p))
        self.assertEqual(r['existing'][0]['decision'], 'nuisance_review')
        self.assertTrue(r['existing'][0]['retained'])
        self.assertIsNone(r['existing'][0]['review_label'])
        self.assertFalse(r['automatic_suppression_enabled'])
        self.assertEqual(plan(r, 10)['unknown_count'], 1)

    def test_hysteresis_uses_native_coordinates(self):
        p = self.p.copy()
        p[11:16,11:16] = .4
        p[12:15,12:15] = .9
        r = analyze(frame(), p)
        self.assertEqual(r['additional'][0]['bbox_px'], [11,11,16,16])
        self.assertEqual(r['additional'][0]['area_px'], 25)

    def test_refinement_is_proposal_and_original_box_unchanged(self):
        f = frame([candidate('fragment', [12,12,13,13])])
        r = analyze(f, self.p)
        self.assertEqual(r['existing'][0]['bbox_px'], [12,12,13,13])
        self.assertEqual(r['existing'][0]['localization_suggestion']['bbox_px'], [12,12,15,15])
        action=next(a for a in r['review_actions'] if a['candidate_id']=='fragment')
        self.assertEqual(action['bbox_px'], [12,12,13,13])
        self.assertFalse(r['existing'][0]['localization_suggestion']['physical_stage_action'])
        mask = np.zeros_like(self.p, dtype=bool)
        mask[12:15,12:15] = True
        e = evaluate(f, r, mask)
        self.assertEqual(e['localization_misses_recovered'],1)
        self.assertEqual(e['new_visible_components_found'],0)

    def test_uncovered_and_invalid_never_negative_or_new_candidates(self):
        covered = np.ones_like(self.p, dtype=bool)
        covered[1:5,1:5] = False
        covered[12:15,12:15] = False
        r = analyze(frame(), self.p, covered)
        self.assertEqual(r['existing'][0]['decision'], 'request_reacquisition')
        self.assertEqual(r['additional'], [])
        for quality in ('unknown', 'invalid'):
            r = analyze(frame(quality=quality), self.p)
            self.assertEqual(r['review_actions'], [])
            self.assertEqual(r['coverage']['usable_pixels'], 0)
            self.assertEqual(plan(r,10)['unknown_count'],1)
            self.assertEqual(apply_reviews(plan(r,10),[])['unknown_count'],1)

    def test_below_minimum_and_seed_exclusions_are_reported(self):
        p = np.zeros_like(self.p)
        p[10,10] = .9
        p[1:4,1:4] = .4
        r = analyze(frame([]), p)
        self.assertEqual(r['excluded_regions']['below_minimum_area'], 1)
        self.assertEqual(r['excluded_regions']['without_strong_seed'], 1)
        self.assertEqual(r['additional'], [])

    def test_reject_truth_and_bad_coordinates(self):
        for key in ('truth', 'mask', 'label', 'future'):
            f = frame()
            f[key] = 'forbidden'
            with self.assertRaises(ValueError):
                analyze(f, self.p)
        for box in ([0,0,21,20], [0,0,0,2], [True,0,2,2]):
            with self.assertRaises(ValueError):
                analyze(frame([candidate('x',box)]), self.p)

    def test_reject_nonfinite_scores_and_fake_coverage(self):
        for v in (float('nan'), float('inf'), -.1, 1.1):
            p = self.p.copy()
            p[0,0] = v
            with self.assertRaises(ValueError):
                analyze(frame(), p)
        with self.assertRaises(ValueError):
            analyze(frame(), self.p, np.ones_like(self.p))

    def test_budget_and_failed_reviews_remain_unknown(self):
        q = plan(analyze(frame(), self.p), 10)
        self.assertEqual(len(q['admitted']), 1)
        self.assertEqual(len(q['deferred']), 1)
        cid = q['admitted'][0]['candidate_id']
        event = {'frame_id': 'fixture', 'candidate_id': cid, 'status': 'failed', 'label': None,
                 'charged_s': 7, 'evidence_id': 'failure-1', 'source': 'human_review'}
        r = apply_reviews(q, [event])
        self.assertEqual(r['unknown_count'], 2)
        self.assertEqual(r['charged_s'], 7)
        self.assertEqual(r['remaining_after_charges_s'], 3)
        self.assertEqual(q['admitted'][0]['status'], 'pending')

    def test_only_paid_independent_review_can_reject(self):
        q = plan(analyze(frame(), np.zeros_like(self.p)), 10)
        event = {'frame_id': 'fixture', 'candidate_id': 'old', 'status': 'ok', 'label': False,
                 'charged_s': 5, 'evidence_id': 'review-1', 'source': 'human_review'}
        r = apply_reviews(q, [event])
        self.assertEqual(r['review_rejected'], 1)
        self.assertEqual(r['unknown_count'], 0)
        for change in ({'candidate_id': 'unknown'}, {'charged_s': 11}, {'label': None}):
            with self.assertRaises(ValueError):
                apply_reviews(q, [{**event, **change}])
        with self.assertRaises(ValueError):
            apply_reviews(q, [event, event])
        with self.assertRaises(ValueError):
            apply_reviews(r, [event])
        with self.assertRaises(ValueError):
            apply_reviews(q, [{**event,'source':'same_model'}])
        with self.assertRaises(ValueError):
            apply_reviews(q, [{**event,'frame_id':'different-acquisition'}])

    def test_forged_plan_over_budget_rejected(self):
        q=plan(analyze(frame(),self.p),10)
        q['admitted'].extend(q['deferred'])
        q['deferred']=[]
        with self.assertRaises(ValueError):
            apply_reviews(q,[])

    def test_one_to_one_matching_prevents_duplicate_credit(self):
        cs = [candidate('a', [1,1,5,5]), candidate('b', [1,1,5,5])]
        self.assertEqual(len(match_candidates(cs, [[1,1,5,5]])), 1)
        # Greedy local matching would miss a match; augmenting paths must recover it.
        cs = [candidate('a', [0,0,8,4]), candidate('b', [0,0,4,4])]
        self.assertEqual(len(match_candidates(cs, [[0,0,4,4],[4,0,8,4]])), 2)

    def test_recovery_must_be_baseline_absent_and_new_candidate(self):
        mask = np.zeros_like(self.p, dtype=bool)
        mask[2:4,2:4] = True
        mask[12:15,12:15] = True
        r = analyze(frame(), self.p)
        e = evaluate(frame(), r, mask)
        self.assertEqual(e['new_visible_components_found'], 1)
        self.assertEqual(e['policies']['augmented_retained']['true_positive_rois'], 2)
        self.assertEqual(e['baseline_matches_lost_after_augmentation'], 0)

    def test_cli_cached_inference_and_immutable_output(self):
        from inspection_engine.cli import main
        from sem_efficiency.core import digest
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'frame.json').write_text(json.dumps(frame()))
            np.save(root/'p.npy', self.p, allow_pickle=False)
            args = ['analyze','--frame',str(root/'frame.json'),'--probability',str(root/'p.npy'),
                    '--probability-sha256',digest(root/'p.npy'),'--out',str(root/'out')]
            self.assertEqual(main(args), 0)
            result = json.loads((root/'out'/'analysis.json').read_text())
            self.assertEqual(len(result['additional']),1)
            before = digest(root/'out'/'analysis.json')
            self.assertEqual(main(args),1)
            self.assertEqual(digest(root/'out'/'analysis.json'),before)

    def test_bad_config_or_budget_rejected(self):
        for c in (Config(grow_threshold=.9,seed_threshold=.8), Config(minimum_area_px=True),
                  Config(review_bound_s=float('nan'))):
            with self.assertRaises(ValueError):
                analyze(frame(), self.p, config=c)
        for b in (-1,float('nan'),True):
            with self.assertRaises(ValueError):
                plan(analyze(frame(),self.p), b)

    def test_small_zero_loss_sample_does_not_qualify_suppression(self):
        from inspection_engine.qualification import development_gate, zero_event_upper_bound
        summary={'policies':{'shadow_localized_filter':{'true_positive_rois':43}}}
        gate=development_gate(summary,43,0)
        self.assertFalse(gate['automatic_suppression_qualified'])
        self.assertFalse(gate['conditional_bound_meets_target'])
        self.assertAlmostEqual(gate['conditional_zero_loss_upper95'],0.06729675341386787)
        self.assertIsNone(zero_event_upper_bound(0))


if __name__ == '__main__':
    unittest.main()
