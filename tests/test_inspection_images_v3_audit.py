"""Adversarial tests for the independent PCB3 replication auditor.

Tamper tests copy the committed evidence (freeze/test-scores/evaluation) into a temporary
directory, edit one thing, and where useful re-seal the outer receipt (freeze digest,
test-scores sha256) so the deeper recomputation must catch it. Each tamper requires a
specific finding and no written audit receipt. The committed evidence is never modified.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / 'scripts'))
import audit_inspection_images_v3 as auditor  # noqa: E402

EVIDENCE = APP / 'evidence' / 'inspection-images-v3'


def load(name):
    return json.loads((EVIDENCE / name).read_text(encoding='utf-8'))


class MetricReimplementationTests(unittest.TestCase):
    def test_strict_threshold_excludes_equal_score(self):
        c = auditor.confusion([1.0, 2.0, 2.0, 3.0], [0, 1, 0, 1], 2.0)
        self.assertEqual(c, {'tp': 1, 'fn': 1, 'fp': 0, 'tn': 2})

    def test_auroc_counts_ties_half(self):
        self.assertEqual(auditor.pairwise_auroc([0.5, 0.5, 0.9, 0.1], [1, 0, 1, 0]), 0.875)

    def test_ap_enters_tied_scores_as_one_group(self):
        ap = auditor.tied_group_ap([0.8, 0.8, 0.5, 0.1], [1, 0, 1, 0])
        self.assertAlmostEqual(ap, 0.5 * 0.5 + 0.5 * (2 / 3), places=15)

    def test_linear_percentile_matches_type7(self):
        self.assertEqual(auditor.linear_percentile([4, 1, 3, 2], 95), 3.85)

    def test_integer_decision_boundaries(self):
        # Exactly +10 of 100 defects and 10 of 101 normals pass; one fewer TP or one more FP fails.
        self.assertTrue(auditor.integer_decision(54, 44, 10, 100, 101, 0.01)['success'])
        self.assertFalse(auditor.integer_decision(53, 44, 3, 100, 101, 0.01)['recall_gain_at_least_10pp'])
        self.assertFalse(auditor.integer_decision(55, 44, 11, 100, 101, 0.01)['primary_far_at_most_10pct'])
        self.assertFalse(auditor.integer_decision(55, 44, 3, 100, 101, 0.0)['bootstrap_ci_lower_above_zero'])


class RealEvidenceAuditTests(unittest.TestCase):
    def test_committed_evidence_passes_read_only(self):
        before = {n: hashlib.sha256((EVIDENCE / n).read_bytes()).hexdigest() for n in auditor.REPORT_INPUTS}
        receipt = auditor.audit(EVIDENCE)
        after = {n: hashlib.sha256((EVIDENCE / n).read_bytes()).hexdigest() for n in auditor.REPORT_INPUTS}
        self.assertEqual(before, after)
        self.assertEqual(receipt['report_input_sha256'], before)
        self.assertTrue(receipt['decision']['success'])
        self.assertTrue(all(receipt['headline_guard'].values()))
        self.assertEqual(receipt['splits'], {'memory': 815, 'calibration': 90, 'test': 201})
        self.assertEqual(len(receipt['errors']['primary448_missed_defects']), 45)
        self.assertEqual(len(receipt['errors']['primary448_false_alarms']), 3)

    def test_committed_audit_receipt_agrees_with_recomputation(self):
        committed = load('audit.json')
        receipt = json.loads(json.dumps(auditor.audit(EVIDENCE)))
        for key in ('report_input_sha256', 'freeze_digest', 'decision', 'bootstrap', 'methods', 'counts'):
            self.assertEqual(committed[key], receipt[key], key)
        self.assertEqual(committed['images']['images_rehashed'], 1106)
        self.assertEqual(committed['images']['mismatches'], 0)
        self.assertNotRegex(json.dumps(committed), r'(?<![A-Za-z])[A-Za-z]:[\\/]')

    def test_cli_reports_pass(self):
        self.assertEqual(auditor.main([str(EVIDENCE)]), 0)


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.docs = {n: load(n) for n in auditor.REPORT_INPUTS}
        self.raw = {n: (EVIDENCE / n).read_bytes() for n in auditor.REPORT_INPUTS}
        self.missed = load('audit.json')['errors']['primary448_missed_defects'][-1]['image']

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, reseal_freeze=False, reseal_scores=False, raw=None):
        docs = self.docs
        if reseal_freeze:
            digest = auditor.canonical_digest(docs['freeze.json'])
            for n in auditor.REPORT_INPUTS:
                docs[n]['freeze_digest'] = digest
        for n in auditor.REPORT_INPUTS:
            if raw and n in raw:
                (self.dir / n).write_bytes(raw[n])
            elif docs[n] == json.loads(self.raw[n]) and not reseal_freeze:
                (self.dir / n).write_bytes(self.raw[n])
            else:
                (self.dir / n).write_text(json.dumps(docs[n], indent=2, sort_keys=True), encoding='utf-8')
        if reseal_scores or reseal_freeze:
            docs['evaluation.json']['test_scores_sha256'] = hashlib.sha256(
                (self.dir / 'test-scores.json').read_bytes()).hexdigest()
            (self.dir / 'evaluation.json').write_text(json.dumps(docs['evaluation.json'], indent=2, sort_keys=True),
                                                      encoding='utf-8')

    def expect(self, fragment, **kw):
        out = self.dir / 'audit.json'
        with self.assertRaises(auditor.AuditError) as ctx:
            auditor.audit(self.dir, out=out, **kw)
        self.assertTrue(any(fragment in f for f in ctx.exception.findings), ctx.exception.findings)
        self.assertFalse(out.exists())
        return ctx.exception.findings

    def set_primary_score(self, image, value):
        self.docs['test-scores.json']['scores']['primary448'][image] = value
        for row in self.docs['evaluation.json']['images']:
            if row['image'] == image:
                row['primary448_score'] = value

    def test_untouched_copy_passes(self):
        self.write()
        self.assertTrue(auditor.audit(self.dir)['decision']['success'])

    def test_modified_threshold_without_reseal_breaks_digest(self):
        self.docs['freeze.json']['calibration']['thresholds']['primary448'] += 1.0
        self.write()
        self.expect('freeze digest does not match')

    def test_modified_threshold_resealed_is_not_calibration_percentile(self):
        self.docs['freeze.json']['calibration']['thresholds']['primary448'] -= 0.5
        self.write(reseal_freeze=True)
        self.expect('threshold primary448 is not the calibration 95th percentile')

    def test_modified_calibration_score_resealed(self):
        cal = self.docs['freeze.json']['calibration']['scores']['baseline224']
        low = min(cal, key=cal.get)
        cal[low] = max(cal.values()) + 5.0
        self.write(reseal_freeze=True)
        self.expect('threshold baseline224 is not the calibration 95th percentile')

    def test_modified_test_score_breaks_scores_sha(self):
        ids = self.docs['freeze.json']['splits']['test_unlabeled']
        self.docs['test-scores.json']['scores']['primary448'][ids[0]] += 0.25
        self.write()
        self.expect('test-scores.json bytes differ')

    def test_modified_test_score_resealed_changes_confusion(self):
        thr = self.docs['freeze.json']['calibration']['thresholds']['primary448']
        self.set_primary_score(self.missed, thr + 1.0)
        self.write(reseal_scores=True)
        self.expect('primary448 confusion counts differ from recomputation')

    def test_score_equal_to_threshold_is_not_a_defect(self):
        thr = self.docs['freeze.json']['calibration']['thresholds']['primary448']
        self.set_primary_score(self.missed, thr)
        self.write(reseal_scores=True)
        try:
            auditor.audit(self.dir)
        except auditor.AuditError as exc:
            self.assertFalse([f for f in exc.findings if 'confusion' in f], exc.findings)

    def test_modified_reported_count(self):
        ft = self.docs['evaluation.json']['methods']['primary448']['frozen_threshold']
        ft['true_positive'], ft['false_negative'] = 56, 44
        self.write()
        self.expect('primary448 confusion counts differ from recomputation')

    def test_modified_reported_recall_rate(self):
        self.docs['evaluation.json']['methods']['baseline224']['frozen_threshold']['defect_recall'] = 0.45
        self.write()
        self.expect('baseline224 recall/FAR differ from counts')

    def test_modified_label(self):
        self.docs['evaluation.json']['images'][0]['label'] ^= 1
        self.write()
        self.expect('evaluation labels disagree')

    def test_modified_counts_block(self):
        self.docs['evaluation.json']['counts']['normal'] = 100
        self.write()
        self.expect('test counts differ')

    def test_modified_auroc_and_ap(self):
        self.docs['evaluation.json']['methods']['primary448']['image_auroc_secondary'] = 0.93
        self.docs['evaluation.json']['methods']['baseline224']['image_average_precision_secondary'] += 1e-6
        self.write()
        self.expect('primary448 AUROC differs from pairwise recomputation')
        self.expect('baseline224 AP differs from recomputation')

    def test_modified_bootstrap_ci(self):
        b = self.docs['evaluation.json']['primary_endpoint']['primary_vs_baseline_bootstrap']
        b['defect_recall_gain']['ci95'][0] = 0.03
        self.write()
        self.expect('bootstrap defect_recall_gain differs from replay')

    def test_secondary_cannot_be_swapped_in_as_primary(self):
        self.docs['freeze.json']['primary_endpoint']['comparison'] = 'highres448max minus baseline224'
        self.write(reseal_freeze=True)
        self.expect('primary endpoint differs between protocol/freeze/evaluation')

    def test_flipped_decision(self):
        self.docs['evaluation.json']['primary_endpoint']['decision']['primary_far_at_most_10pct'] = False
        self.write()
        self.expect('preregistered decision differs')

    def test_broken_freeze_json(self):
        self.write(raw={'freeze.json': self.raw['freeze.json'][:-200]})
        self.expect('freeze.json is not valid JSON')

    def test_freeze_missing_section_resealed(self):
        del self.docs['freeze.json']['calibration']
        self.write(reseal_freeze=True)
        self.expect('malformed or missing input')

    def test_freeze_test_usage_guard(self):
        self.docs['freeze.json']['test_usage']['in_thresholds'] = True
        self.write(reseal_freeze=True)
        self.expect('test-usage guard')

    def test_freeze_source_hash_tampered(self):
        self.docs['freeze.json']['sources']['inspection_images_v3/pipeline.py'] = '0' * 64
        self.write(reseal_freeze=True)
        self.expect('source hash mismatch for inspection_images_v3/pipeline.py')

    def test_frozen_v2_dependency_hash_tampered(self):
        self.docs['freeze.json']['sources']['inspection_images_v2/scoring.py'] = '0' * 64
        self.write(reseal_freeze=True)
        self.expect('frozen dependency inspection_images_v2/scoring.py differs from its protocol pin')

    def test_repository_source_edited_after_freeze(self):
        self.write()
        repo = self.dir / 'repo'
        for rel in self.docs['freeze.json']['sources']:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(APP / rel, repo / rel)
        with open(repo / 'inspection_images_v3' / 'pipeline.py', 'ab') as fh:
            fh.write(b'\n# edited after freeze\n')
        self.expect('source hash mismatch for inspection_images_v3/pipeline.py', repo_root=repo)

    def test_environment_tampered(self):
        env = self.docs['freeze.json']['environment']
        env['packages'] = [p if not p.startswith('torch==') else 'torch==2.15.0+cpu' for p in env['packages']]
        self.write(reseal_freeze=True)
        self.expect('freeze environment differs from protocol environment pins')

    def test_model_weights_tampered(self):
        self.docs['freeze.json']['model']['r448']['extractor']['weights_sha256'] = 'a' * 64
        self.write(reseal_freeze=True)
        self.expect('feature sets use different backbone weights')

    def test_weights_file_mismatch(self):
        self.write()
        fake = self.dir / 'resnet18-f37072fd.pth'
        fake.write_bytes(b'not the weights')
        self.expect('weights file differs from freeze', weights=fake)

    def test_absolute_path_in_evidence(self):
        self.docs['evaluation.json']['note'] = 'C:\\Users\\someone\\run'
        self.write()
        self.expect('evaluation.json contains an absolute local path')

    def test_freeze_digest_chain_to_scores(self):
        self.docs['test-scores.json']['freeze_digest'] = 'f' * 64
        self.write(reseal_scores=True)
        self.expect('test-scores freeze_digest differs')

    def test_test_identity_leak_into_calibration(self):
        f = self.docs['freeze.json']
        f['splits']['calibration'][0] = f['splits']['test_unlabeled'][0]
        self.write(reseal_freeze=True)
        self.expect('test identities overlap memory/calibration')

    def test_missing_input(self):
        self.write()
        (self.dir / 'test-scores.json').unlink()
        self.expect('missing report input test-scores.json')

    def test_missing_images_in_data_dir(self):
        self.write()
        (self.dir / 'visa').mkdir()
        self.expect('image(s) differ from freeze byte hashes', data_dir=self.dir / 'visa')

    def test_run_root_copy_mismatch(self):
        self.write()
        other = self.dir / 'run'
        other.mkdir()
        for n in auditor.REPORT_INPUTS:
            (other / n).write_bytes(self.raw[n])
        (other / 'evaluation.json').write_bytes(self.raw['evaluation.json'] + b' ')
        self.expect('evidence copies differ from run root', run_root=other)

    def test_headline_guard_is_separate_from_recomputation(self):
        # A wrong stated headline fails only the guard; the recomputation itself stays clean.
        self.write()
        wrong = json.loads(json.dumps(auditor.HEADLINE))
        wrong['confusion']['primary448']['tp'] = 56
        with mock.patch.object(auditor, 'HEADLINE', wrong):
            findings = self.expect('headline guard: recomputed confusion differs')
        self.assertTrue(all(f.startswith('headline guard') for f in findings), findings)


if __name__ == '__main__':
    unittest.main()
