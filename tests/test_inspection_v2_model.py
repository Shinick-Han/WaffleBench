import copy
import json
import unittest
from unittest import mock

import numpy as np

from inspection_review import model as v1_model
from inspection_review.data import generate_lot, load_config
from inspection_v2 import model as m


def make_lot(seed, scenario="stationary", n=400, lot_id=None):
    rng = np.random.default_rng([seed, len(scenario)])
    signal = rng.normal(0.8, 0.3, n)
    candidate = signal >= 0.62
    latent = 2.5 * (signal - 0.9) + rng.normal(0, 0.6, n)
    doi = latent > 0.4
    features = rng.normal(0, 1, (n, 7))
    features[:, 0] = signal
    features[:, 1] = latent + rng.normal(0, 0.5, n)
    features[~candidate, :4] = np.nan
    public = {"lot_id": lot_id or f"lot-{seed}-{scenario}", "seed": seed, "scenario": scenario,
              "candidate": candidate, "features": features}
    return {"public": public, "oracle": {"doi": doi}}


def make_config(family="catboost", **overrides):
    config = {
        "splits": {"train_seeds": [100, 101, 102, 103], "validation_seeds": [200],
                   "test_seeds_by_scenario": {"stationary": [1000], "novel_cluster": [1100]}},
        "v2_splits": {"train": [[101, "novel_cluster"]],
                      "calibration": [[3001, "stationary"], [3002, "low_contrast"]],
                      "test": [[3100, "stationary"]]},
        "model": {"family": "numpy_l2_logistic", "l2": 0.01, "epochs": 100, "learning_rate": 0.1,
                  "classification_threshold": 0.5, "online_learning_rate": 0.08},
        "v2_model": {"family": family, "iterations": 40, "depth": 3, "learning_rate": 0.2,
                     "random_seed": 7, "thread_count": 2, "l2_leaf_reg": 3},
    }
    config["v2_model"].update(overrides)
    return config


TRAIN = [make_lot(100), make_lot(102)]
CALIB = [make_lot(3001), make_lot(3002, "low_contrast")]


class CatBoostTrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = m.train_model(TRAIN, cls.config)

    def test_json_serializable_frozen_model_fields(self):
        model = self.model
        self.assertEqual(model["family"], "catboost")
        self.assertFalse(model["supports_online_update"])
        self.assertEqual(model["train_ids"], ["lot-100-stationary", "lot-102-stationary"])
        self.assertEqual(model["provenance"]["catboost_version"], "1.2.10")
        self.assertEqual(model["provenance"]["probability"], "raw_base")
        self.assertEqual(model["fit"]["thread_count"], 2)
        restored = json.loads(json.dumps(model, allow_nan=False))
        self.assertEqual(m.hash_model(restored), m.hash_model(model))
        x = TRAIN[0]["public"]["features"][:20]
        np.testing.assert_array_equal(m.predict(restored, x), m.predict(model, x))

    def test_training_is_deterministic_and_seed_sensitive(self):
        again = m.train_model(TRAIN, self.config)
        self.assertEqual(m.hash_model(again), m.hash_model(self.model))
        other = m.train_model(TRAIN, make_config(random_seed=8))
        self.assertNotEqual(m.hash_model(other), m.hash_model(self.model))

    def test_imputation_uses_train_candidate_mean_only(self):
        x = np.concatenate([lot["public"]["features"][lot["public"]["candidate"]] for lot in TRAIN])
        np.testing.assert_allclose(self.model["mean"], np.nanmean(x, axis=0))
        missing = np.full((1, 7), np.nan)
        np.testing.assert_allclose(m.predict(self.model, missing),
                                   m.predict(self.model, np.asarray(self.model["mean"])[None, :]))
        self.assertEqual(m.predict(self.model, np.full((1, 7), np.inf)).shape, (1,))

    def test_predict_shape_range_and_no_mutation(self):
        before = m.hash_model(self.model)
        x = CALIB[0]["public"]["features"]
        p = m.predict(self.model, x)
        self.assertEqual(p.shape, (len(x),))
        self.assertTrue(np.all((p >= 0) & (p <= 1)))
        self.assertEqual(m.hash_model(self.model), before)
        with self.assertRaises(ValueError):
            m.predict(self.model, np.zeros((2, 6)))
        self.assertEqual(m.predict(self.model, np.zeros((0, 7))).shape, (0,))

    def test_serialized_inference_matches_in_memory_catboost(self):
        import catboost
        x = np.concatenate([lot["public"]["features"][lot["public"]["candidate"]] for lot in TRAIN])
        y = np.concatenate([lot["oracle"]["doi"][lot["public"]["candidate"]] for lot in TRAIN])
        mean, scale = np.asarray(self.model["mean"]), np.asarray(self.model["scale"])
        z = (np.where(np.isfinite(x), x, mean) - mean) / scale
        clf = catboost.CatBoostClassifier(iterations=40, depth=3, learning_rate=0.2, random_seed=7,
                                          l2_leaf_reg=3, thread_count=2, loss_function="Logloss",
                                          verbose=False, allow_writing_files=False)
        clf.fit(z, y.astype(int))
        xt = CALIB[1]["public"]["features"]
        zt = (np.where(np.isfinite(xt), xt, mean) - mean) / scale
        np.testing.assert_allclose(m.predict(self.model, xt), clf.predict_proba(zt)[:, 1],
                                   rtol=0, atol=1e-12)

    def test_conflicting_seed_keys_rejected(self):
        with self.assertRaises(ValueError):
            m.train_model(TRAIN, make_config(seed=8))

    def test_catboost_never_updates_online(self):
        with self.assertRaises(ValueError):
            m.update_model(self.model, TRAIN[0]["public"]["features"][0], True, self.config)

    def test_learns_signal(self):
        report = m.evaluate_model(self.model, CALIB, self.config)
        self.assertGreater(report["aggregate"]["average_precision"], report["aggregate"]["prevalence"])


class SplitGuardTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config(iterations=5)

    def test_rejects_non_train_seed(self):
        for seed in (200, 1000, 3001):
            with self.assertRaises(ValueError):
                m.train_model([make_lot(100), make_lot(seed)], self.config)

    def test_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100), make_lot(100)], self.config)
        with self.assertRaises(ValueError):  # same seed/scenario under another id
            m.train_model([make_lot(100), make_lot(100, lot_id="other")], self.config)
        with self.assertRaises(ValueError):  # same id, different lot
            m.train_model([make_lot(100), make_lot(102, lot_id="lot-100-stationary")], self.config)

    def test_requires_restored_metadata(self):
        lot = make_lot(100)
        del lot["public"]["seed"]
        with self.assertRaises(ValueError):
            m.train_model([lot], self.config)
        lot = make_lot(100)
        lot["public"]["seed"] = True
        with self.assertRaises(ValueError):
            m.train_model([lot], self.config)

    def test_nonstationary_train_needs_explicit_pair(self):
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100), make_lot(102, "novel_cluster")], self.config)
        model = m.train_model([make_lot(100), make_lot(101, "novel_cluster")], self.config)
        self.assertIn([101, "novel_cluster"], model["provenance"]["train_pairs"])

    def test_rejects_inconsistent_split_config(self):
        bad = make_config(iterations=5)
        bad["v2_splits"]["train"] = [[3001, "novel_cluster"]]
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100)], bad)
        bad = make_config(iterations=5)
        bad["v2_splits"]["calibration"] = [[102, "stationary"]]
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100)], bad)
        bad = make_config(iterations=5)
        bad["v2_splits"]["train"] = [[101, "novel_cluster"], [101, "novel_cluster"]]
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100)], bad)

    def test_rejects_bad_model_config(self):
        with self.assertRaises(ValueError):
            m.train_model(TRAIN, make_config(family="xgboost"))
        config = make_config()
        del config["v2_model"]["depth"]
        with self.assertRaises(ValueError):
            m.train_model(TRAIN, config)
        with self.assertRaises(ValueError):
            m.train_model([], self.config)

    def test_no_silent_fallback_on_wrong_catboost(self):
        import catboost
        with mock.patch.object(catboost, "__version__", "1.2.7"):
            with self.assertRaises(RuntimeError):
                m.train_model(TRAIN, self.config)

    def test_single_class_rejected(self):
        lot = make_lot(100)
        lot["oracle"]["doi"] = np.zeros_like(lot["oracle"]["doi"])
        with self.assertRaises(ValueError):
            m.train_model([lot], self.config)


class LogisticTest(unittest.TestCase):
    def setUp(self):
        self.config = make_config(family="logistic")
        del self.config["v2_model"]["learning_rate"]
        self.config["v2_model"]["iterations"] = 100

    def test_delegates_to_v1_and_updates_without_mutation(self):
        model = m.train_model(TRAIN, self.config)
        self.assertEqual(model["family"], "logistic")
        self.assertTrue(model["supports_online_update"])
        v1 = v1_model.train_model(TRAIN, {"splits": self.config["splits"], "model": self.config["model"]})
        x = CALIB[0]["public"]["features"]
        np.testing.assert_allclose(m.predict(model, x), v1_model.predict(v1, x))
        before = m.hash_model(model)
        new = m.update_model(model, x[0], True, self.config)
        self.assertEqual(m.hash_model(model), before)
        self.assertEqual(new["online_updates"], 1)
        self.assertEqual(new["parent_hash"], before)
        json.dumps(new, allow_nan=False)

    def test_stationary_only(self):
        with self.assertRaises(ValueError):
            m.train_model([make_lot(100), make_lot(101, "novel_cluster")], self.config)

    def test_calibrated_logistic_is_frozen(self):
        model = m.fit_calibration(m.train_model(TRAIN, self.config), CALIB, self.config)
        self.assertFalse(model["supports_online_update"])
        with self.assertRaises(ValueError):
            m.update_model(model, CALIB[0]["public"]["features"][0], True, self.config)


class CalibrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.base = m.train_model(TRAIN, cls.config)

    def test_isotonic_returns_new_model_and_preserves_base(self):
        base_hash = m.hash_model(self.base)
        cal = m.fit_calibration(self.base, CALIB, self.config)
        self.assertEqual(m.hash_model(self.base), base_hash)
        self.assertIsNone(self.base["calibration"])
        c = cal["calibration"]
        self.assertEqual(c["base_model_hash"], base_hash)
        self.assertEqual(c["calibration_ids"], ["lot-3001-stationary", "lot-3002-low_contrast"])
        self.assertEqual(cal["provenance"]["probability"], "calibrated_isotonic")
        self.assertEqual(cal["mean"], self.base["mean"])
        self.assertEqual(cal["booster"], self.base["booster"])
        self.assertTrue(np.all(np.diff(c["y"]) >= 0))
        x = make_lot(3100)["public"]["features"]
        np.testing.assert_array_equal(m.predict_raw(cal, x), m.predict(self.base, x))
        raw, calibrated = m.predict_raw(cal, x), m.predict(cal, x)
        self.assertTrue(np.all(np.diff(calibrated[np.argsort(raw)]) >= -1e-12))
        json.dumps(cal, allow_nan=False)
        self.assertNotEqual(m.hash_model(cal), base_hash)

    def test_isotonic_reduces_calibration_brier(self):
        cal = m.fit_calibration(self.base, CALIB, self.config)
        raw = m.evaluate_model(self.base, CALIB, self.config)["aggregate"]["brier"]
        fitted = m.evaluate_model(cal, CALIB, self.config)
        self.assertLessEqual(fitted["aggregate"]["brier"], raw + 1e-12)
        self.assertEqual(fitted["scope"]["fit_overlap_lot_ids"],
                         ["lot-3001-stationary", "lot-3002-low_contrast"])

    def test_identity_records_provenance_without_transform(self):
        cal = m.fit_calibration(self.base, CALIB, self.config, method="identity")
        x = CALIB[0]["public"]["features"]
        np.testing.assert_array_equal(m.predict(cal, x), m.predict(self.base, x))
        self.assertEqual(cal["calibration"]["method"], "identity")
        with self.assertRaises(ValueError):
            m.fit_calibration(self.base, CALIB, self.config, method="platt")

    def test_rejects_unapproved_overlapping_or_duplicate_lots(self):
        cases = [
            [make_lot(3003)],                       # not approved
            [make_lot(3001, "novel_cluster")],      # approved seed, wrong scenario
            [make_lot(100)],                        # train lot
            [make_lot(3001), make_lot(3001)],       # duplicate
            [make_lot(3001), make_lot(3002, "low_contrast", lot_id="lot-3001-stationary")],
            [],
        ]
        for lots in cases:
            with self.assertRaises(ValueError):
                m.fit_calibration(self.base, lots, self.config)
        for seed, split in ((3100, "test"), (200, "validation"), (1000, "v1 test"), (103, "train")):
            config = copy.deepcopy(self.config)
            config["v2_splits"]["calibration"].append([seed, "stationary"])
            with self.assertRaises(ValueError, msg=split):
                m.fit_calibration(self.base, [make_lot(seed)], config)

    def test_rejects_recalibration(self):
        cal = m.fit_calibration(self.base, CALIB, self.config)
        with self.assertRaises(ValueError):
            m.fit_calibration(cal, CALIB, self.config)


class MetricsTest(unittest.TestCase):
    def test_pav(self):
        x, y = m._isotonic(np.array([0.4, 0.1, 0.3, 0.2, 0.2]), np.array([1, 0, 0, 1, 0]))
        np.testing.assert_allclose(x, [0.1, 0.2, 0.3, 0.4])
        np.testing.assert_allclose(y, [0.0, 1 / 3, 1 / 3, 1.0])

    def test_average_precision_ece_risk_coverage(self):
        y = np.array([True, False, True, False])
        p = np.array([0.9, 0.8, 0.7, 0.1])
        self.assertAlmostEqual(m._average_precision(y, p), (1.0 + 2 / 3) / 2)
        self.assertEqual(m._average_precision(np.array([True, False]), np.array([0.5, 0.5])), 0.5)
        self.assertIsNone(m._average_precision(np.zeros(3, bool), np.ones(3)))
        self.assertAlmostEqual(m._ece(np.array([True, False]), np.array([0.95, 0.05]), 10), 0.05)
        rc = m._risk_coverage(y, p, 0.5, [0.5, 1.0])
        self.assertEqual(rc["points"][0], {"coverage": 0.5, "n": 2, "risk": 0.0})
        self.assertEqual(rc["points"][1]["risk"], 0.25)

    def test_evaluate_scope_is_candidates_only(self):
        config = make_config(iterations=10)
        model = m.train_model(TRAIN, config)
        lot = make_lot(3100)
        report = m.evaluate_model(model, [lot], config)
        self.assertEqual(report["scope"]["unit"], "optical_candidate_sites")
        self.assertEqual(report["scope"]["fit_overlap_lot_ids"], [])
        self.assertEqual(report["aggregate"]["n_candidates"], int(lot["public"]["candidate"].sum()))
        self.assertEqual(report["model_hash"], m.hash_model(model))
        for key in ("average_precision", "brier", "ece", "risk_coverage", "recall_among_candidates"):
            self.assertIn(key, report["aggregate"])
        self.assertNotIn("recall", report["aggregate"])
        empty = m.evaluate_model(model, [], config)
        self.assertEqual(empty["aggregate"]["n_candidates"], 0)
        self.assertIsNone(empty["aggregate"]["brier"])
        self.assertEqual(m.evaluate_model(model, TRAIN, config)["scope"]["fit_overlap_lot_ids"],
                         ["lot-100-stationary", "lot-102-stationary"])


class GeneratedLotIntegrationTest(unittest.TestCase):
    def test_v1_generated_lots_train_calibrate_evaluate(self):
        v1 = load_config()
        config = make_config(iterations=30)
        config["splits"] = v1["splits"]
        config["model"] = v1["model"]
        config["v2_splits"] = {"train": [], "calibration": [[3001, "stationary"]],
                               "test": [[3101, "stationary"]]}
        model = m.train_model([generate_lot(100, "stationary", v1)], config)
        cal = m.fit_calibration(model, [generate_lot(3001, "stationary", v1)], config)
        report = m.evaluate_model(cal, [generate_lot(3101, "stationary", v1)], config)
        self.assertGreater(report["aggregate"]["n_candidates"], 0)
        self.assertEqual(report["probability"], "calibrated_isotonic")


if __name__ == "__main__":
    unittest.main()
