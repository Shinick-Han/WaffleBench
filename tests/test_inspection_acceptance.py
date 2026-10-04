"""Coordinator checks of research boundaries, independent of worker tests.

Only development lots are generated here. The registered held-out campaign is
never used for threshold, generator or acquisition tuning.
"""
import hashlib
import json
from pathlib import Path
import unittest

import numpy as np

from inspection_review import data, model


ROOT = Path(__file__).resolve().parents[1]


class InspectionAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = data.load_config()
        cls.lots = [data.generate_lot(s, "stationary", cls.config) for s in (100, 101)]
        cls.development = data.generate_lot(200, "stationary", cls.config)
        cls.fitted = model.train_model(cls.lots, cls.config)

    def test_registered_lot_seeds_are_independent(self):
        splits = self.config["splits"]
        train, validation = set(splits["train_seeds"]), set(splits["validation_seeds"])
        test_groups = list(splits["test_seeds_by_scenario"].values())
        tests = [s for group in test_groups for s in group]
        self.assertEqual(len(tests), 60)
        self.assertEqual(len(set(tests)), len(tests))
        self.assertFalse(train & validation)
        self.assertFalse((train | validation) & set(tests))

    def test_geometry_is_actual_die_geometry_and_not_full_wafer_coverage(self):
        public = self.lots[0]["public"]
        self.assertEqual(len(public["site_ids"]), 3915)
        self.assertEqual(len(set(public["site_ids"])), 3915)
        np.testing.assert_array_equal(np.bincount(public["wafer"]), [1305] * 3)
        centers = public["die_xy_mm"]
        for dx in (-4, 4):
            for dy in (-3, 3):
                self.assertTrue(np.all(np.linalg.norm(centers + [dx, dy], axis=1) <= 147 + 1e-9))
        self.assertTrue(np.all(np.abs(public["in_die_xy_mm"]) <= [4, 3]))

    def test_truth_is_private_and_outside_optical_evidence_is_missing(self):
        public, oracle = self.lots[0]["public"], self.lots[0]["oracle"]
        self.assertFalse(set(public) & {"doi", "physical", "kind", "electrical_effect", "review_status", "review_positive"})
        outside = ~public["candidate"]
        self.assertTrue(outside.any())
        self.assertTrue(np.isnan(public["features"][outside, :4]).all())
        self.assertTrue(np.isfinite(public["features"][public["candidate"]]).all())
        self.assertTrue(np.all(~oracle["doi"] | oracle["physical"]))
        self.assertTrue(np.any(oracle["doi"] != oracle["electrical_effect"]))
        self.assertTrue(np.any(oracle["physical"] & ~oracle["doi"]))

    def test_instrument_observations_are_order_independent_and_do_not_reveal_truth(self):
        oracle = self.lots[0]["oracle"]
        samples = [(i, a) for i in range(80) for a in (0, 1)]
        forward = {(i, a): data.review_observation(oracle, i, a) for i, a in samples}
        for i, a in reversed(samples):
            self.assertEqual(forward[i, a], data.review_observation(oracle, i, a))
            self.assertFalse(set(forward[i, a]) & {"doi", "physical", "electrical_effect", "true_kind"})
            if forward[i, a]["status"] != "ok":
                self.assertIsNone(forward[i, a]["reported_doi"])
                self.assertIsNone(forward[i, a]["reported_kind"])

    def test_trained_model_beats_constant_probability_on_development_brier(self):
        public, oracle = self.development["public"], self.development["oracle"]
        mask = public["candidate"]
        pred = model.predict(self.fitted, public["features"])[mask]
        labels = oracle["doi"][mask].astype(float)
        training_labels = np.concatenate([l["oracle"]["doi"][l["public"]["candidate"]] for l in self.lots])
        prior = float(training_labels.mean())
        self.assertTrue(np.all(np.isfinite(pred)))
        self.assertTrue(np.all((pred >= 0) & (pred <= 1)))
        self.assertLess(float(np.mean((pred - labels) ** 2)), float(np.mean((prior - labels) ** 2)))

    def test_online_learning_preserves_frozen_model_and_scaler(self):
        before_hash = model.hash_model(self.fitted)
        before = json.dumps(self.fitted, sort_keys=True)
        row = self.development["public"]["features"][np.flatnonzero(self.development["public"]["candidate"])[0]]
        updated = model.update_model(self.fitted, row, True, self.config)
        self.assertEqual(model.hash_model(self.fitted), before_hash)
        self.assertEqual(json.dumps(self.fitted, sort_keys=True), before)
        self.assertNotEqual(model.hash_model(updated), before_hash)

    def test_legacy_frozen_inputs_preserved(self):
        receipt = json.loads((ROOT / "evidence/inspection-research/protected-before-v1.json").read_text(encoding="utf-8-sig"))
        for path, expected in receipt["protected_sha256"].items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
