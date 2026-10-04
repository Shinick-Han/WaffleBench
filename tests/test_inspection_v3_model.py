import copy
import json
import unittest

import numpy as np

from inspection_v2 import model as v2
from inspection_v3 import model as m


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


def nll(p, y):
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log1p(-p)))


class DelegationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = m.train_model(TRAIN, cls.config)

    def test_training_and_baseline_predictions_match_v2_exactly(self):
        ref = v2.train_model(TRAIN, self.config)
        self.assertEqual(m.hash_model(self.model), v2.hash_model(ref))
        x = CALIB[0]["public"]["features"]
        np.testing.assert_array_equal(m.predict(self.model, x), v2.predict(ref, x))
        np.testing.assert_array_equal(m.predict_raw(self.model, x), v2.predict_raw(ref, x))
        for method in ("identity", "isotonic"):
            a = m.fit_calibration(self.model, CALIB, self.config, method=method)
            b = v2.fit_calibration(ref, CALIB, self.config, method=method)
            self.assertEqual(m.hash_model(a), v2.hash_model(b))
            np.testing.assert_array_equal(m.predict(a, x), v2.predict(b, x))
        isotonic = v2.fit_calibration(ref, CALIB, self.config, method="isotonic")
        for model in (ref, isotonic):
            mine = m.evaluate_model(model, TRAIN + CALIB, self.config)
            theirs = v2.evaluate_model(model, TRAIN + CALIB, self.config)
            self.assertTrue(mine.pop("probability_semantics"))
            mine["aggregate"].pop("log_loss")
            self.assertEqual(mine, theirs)  # identical v2 schema, values and overlap ids

    def test_logistic_baseline_unchanged_and_online_update_delegated(self):
        config = make_config("logistic")
        model = m.train_model(TRAIN, config)
        x = CALIB[1]["public"]["features"]
        np.testing.assert_array_equal(m.predict(model, x), v2.predict(v2.train_model(TRAIN, config), x))
        updated = m.update_model(model, TRAIN[0]["public"]["features"][0], True, config)
        self.assertNotEqual(m.hash_model(updated), m.hash_model(model))

    def test_catboost_grid_is_deterministic_and_does_not_train(self):
        grid = m.catboost_grid(self.config, {"depth": [2, 3], "learning_rate": [0.1]})
        self.assertEqual([g["label"] for g in grid], ["depth=2,learning_rate=0.1", "depth=3,learning_rate=0.1"])
        self.assertEqual(grid[0]["config"]["v2_model"]["depth"], 2)
        self.assertEqual(self.config["v2_model"]["depth"], 3)
        with self.assertRaises(ValueError):
            m.catboost_grid(self.config, {"loss_function": ["RMSE"]})
        with self.assertRaises(ValueError):
            m.catboost_grid(make_config("logistic"), {"depth": [2]})


class ParametricCalibrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = m.train_model(TRAIN, cls.config)
        cls.platt = m.fit_calibration(cls.model, CALIB, cls.config, method="platt")
        cls.temp = m.fit_calibration(cls.model, CALIB, cls.config, method="temperature")

    def test_new_json_model_preserves_base(self):
        before = copy.deepcopy(self.model)
        model = m.fit_calibration(self.model, CALIB, self.config, method="platt")
        self.assertEqual(self.model, before)
        self.assertIsNone(self.model["calibration"])
        cal = model["calibration"]
        self.assertEqual(cal["base_model_hash"], m.hash_model(self.model))
        self.assertEqual(cal["calibration_ids"], ["lot-3001-stationary", "lot-3002-low_contrast"])
        self.assertEqual(cal["calibration_pairs"], [[3001, "stationary"], [3002, "low_contrast"]])
        self.assertEqual(cal["fit_scope"], "candidates_only")
        self.assertGreater(cal["slope"], 0)
        self.assertTrue(cal["optimizer"]["converged"])
        self.assertEqual(model["provenance"]["probability"], "calibrated_platt")
        self.assertFalse(model["supports_online_update"])
        self.assertEqual(len(model["mean"]), 7)
        self.assertEqual(model["mean"], self.model["mean"])
        self.assertEqual(model["scale"], self.model["scale"])
        self.assertEqual(m.hash_model(m.base_model(model)), m.hash_model(self.model))
        x = CALIB[0]["public"]["features"]
        np.testing.assert_array_equal(m.predict_raw(model, x), m.predict(self.model, x))
        self.assertEqual(self.temp["calibration"]["intercept"], 0.0)
        self.assertAlmostEqual(self.temp["calibration"]["temperature"] * self.temp["calibration"]["slope"], 1.0)

    def test_json_reload_equivalence(self):
        for model in (self.platt, self.temp):
            restored = json.loads(json.dumps(model, allow_nan=False))
            self.assertEqual(m.hash_model(restored), m.hash_model(model))
            x = CALIB[1]["public"]["features"]
            np.testing.assert_array_equal(m.predict(restored, x), m.predict(model, x))

    def test_deterministic(self):
        again = m.fit_calibration(self.model, CALIB, self.config, method="platt")
        self.assertEqual(m.hash_model(again), m.hash_model(self.platt))

    def test_rank_preservation(self):
        x = np.concatenate([lot["public"]["features"] for lot in CALIB + TRAIN])
        raw = m.predict_raw(self.platt, x)
        for model in (self.platt, self.temp):
            cal = m.predict(model, x)
            order = np.argsort(raw, kind="mergesort")
            self.assertTrue(np.all(np.diff(cal[order]) >= 0))
            self.assertEqual(len(np.unique(cal)), len(np.unique(raw)))
            logit = m.predict_calibrated_logit(model, x)
            self.assertTrue(np.all(np.diff(logit[order]) >= 0))

    def test_stable_for_exact_zero_and_one(self):
        cal = {"method": "platt", "slope": 3.0, "intercept": 0.5}
        p = np.array([0.0, 1e-300, 0.3, 1 - 1e-17, 1.0])
        out = m._sigmoid(cal["slope"] * m._logit(p) + cal["intercept"])
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertTrue(np.all((out >= 0) & (out <= 1)))
        self.assertTrue(np.all(np.diff(out) >= 0))
        np.testing.assert_array_equal(m._sigmoid(np.array([-1e6, 0.0, 1e6])), [0.0, 0.5, 1.0])
        # Perfectly separated labels including exact 0/1 raw scores stay finite and bounded.
        s = m._logit(np.array([0.0, 0.0, 0.2, 0.8, 1.0, 1.0]))
        y = np.array([0, 0, 0, 1, 1, 1], dtype=float)
        with np.errstate(all="raise"):
            slope, intercept, opt = m._fit_affine_logit(s, y, fit_intercept=True)
        self.assertTrue(np.isfinite([slope, intercept, opt["train_log_loss"]]).all())
        self.assertTrue(m.SLOPE_BOUNDS[0] <= slope <= m.SLOPE_BOUNDS[1])

    def test_rejects_recalibration_and_online_updates(self):
        with self.assertRaises(ValueError):
            m.fit_calibration(self.platt, CALIB, self.config, method="temperature")
        with self.assertRaises(ValueError):
            m.fit_calibration(self.model, CALIB, self.config, method="beta")
        logistic_config = make_config("logistic")
        logistic = m.train_model(TRAIN, logistic_config)
        cal = m.fit_calibration(logistic, CALIB, logistic_config, method="platt")
        self.assertTrue(logistic["supports_online_update"])
        with self.assertRaises(ValueError):
            m.update_model(cal, TRAIN[0]["public"]["features"][0], True, logistic_config)
        self.assertEqual(m.hash_model(m.base_model(cal)), m.hash_model(logistic))

    def test_tampered_model_fails_base_check(self):
        tampered = copy.deepcopy(self.platt)
        tampered["mean"][0] += 1.0
        with self.assertRaises(ValueError):
            m.base_model(tampered)

    def test_evaluate_reports_semantics_and_scope(self):
        with self.assertRaises(KeyError):  # v2 evaluation cannot apply v3 parametric calibration
            v2.evaluate_model(self.platt, CALIB, self.config)
        report = m.evaluate_model(self.platt, TRAIN + CALIB, self.config)
        base = v2.evaluate_model(self.model, TRAIN + CALIB, self.config)
        self.assertEqual(set(report) - {"probability_semantics"}, set(base))
        self.assertEqual(set(report["aggregate"]) - {"log_loss"}, set(base["aggregate"]))
        self.assertEqual([set(e) for e in report["per_lot"]], [set(e) for e in base["per_lot"]])
        self.assertEqual(report["scope"]["fit_overlap_lot_ids"],
                         sorted(l["public"]["lot_id"] for l in TRAIN + CALIB))
        mask = np.concatenate([l["public"]["candidate"] for l in TRAIN + CALIB])
        x = np.concatenate([l["public"]["features"] for l in TRAIN + CALIB])[mask]
        y = np.concatenate([l["oracle"]["doi"] for l in TRAIN + CALIB])[mask]
        p = m.predict(self.platt, x)
        self.assertAlmostEqual(report["aggregate"]["brier"], float(np.mean((p - y) ** 2)), places=12)
        self.assertNotEqual(report["aggregate"]["brier"], base["aggregate"]["brier"])
        report = m.evaluate_model(self.platt, CALIB, self.config)
        self.assertEqual(report["probability"], "calibrated_platt")
        self.assertIn("sigmoid", report["probability_semantics"])
        self.assertEqual(report["scope"]["fit_overlap_lot_ids"], sorted(l["public"]["lot_id"] for l in CALIB))
        self.assertIsNotNone(report["aggregate"]["log_loss"])


class LeakageGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = m.train_model(TRAIN, cls.config)

    def assertRejected(self, lots, config=None):
        for method in m.PARAMETRIC_METHODS:
            with self.assertRaises(ValueError):
                m.fit_calibration(self.model, lots, config or self.config, method=method)

    def test_rejects_unapproved_train_and_reserved_seeds(self):
        self.assertRejected([make_lot(3003)])                       # not approved
        self.assertRejected([make_lot(3002)])                       # wrong scenario for pair
        self.assertRejected([CALIB[0], CALIB[0]])                   # duplicate
        self.assertRejected([])
        for seed, scenario in ((100, "stationary"), (200, "stationary"), (1000, "stationary"),
                               (3100, "stationary")):
            config = make_config()
            config["v2_splits"]["calibration"].append([seed, scenario])
            self.assertRejected([make_lot(seed, scenario)], config)
        lot = make_lot(3001)
        lot["public"]["seed"] = None
        self.assertRejected([lot])
        same_id = make_lot(3001, lot_id="lot-100-stationary")
        self.assertRejected([same_id])

    def test_v3_splits_are_separate_and_guarded(self):
        config = make_config()
        config["v3_splits"] = {"calibration": [[4001, "stationary"]],
                               "dev": [[4100, "stationary"]], "test": [[4200, "stationary"]]}
        model = m.fit_calibration(self.model, [make_lot(4001)], config, method="platt")
        self.assertEqual(model["calibration"]["calibration_pairs"], [[4001, "stationary"]])
        self.assertRejected([CALIB[0]], config)                    # v2 pair not approved for v3
        for seed in (4100, 4200):
            bad = copy.deepcopy(config)
            bad["v3_splits"]["calibration"].append([seed, "stationary"])
            self.assertRejected([make_lot(seed)], bad)

    def test_single_class_rejected(self):
        lot = make_lot(3001)
        lot["oracle"]["doi"] = np.zeros_like(lot["oracle"]["doi"])
        self.assertRejected([lot])


class CalibrationQualityTest(unittest.TestCase):
    def test_fitter_recovers_and_improves_held_out_crafted_scores(self):
        rng = np.random.default_rng(11)
        def sample(n):
            raw = rng.uniform(0.001, 0.999, n)
            true_p = m._sigmoid(0.4 * m._logit(raw) - 0.7)  # overconfident and biased raw scores
            return raw, (rng.uniform(size=n) < true_p).astype(float), true_p
        raw_fit, y_fit, _ = sample(4000)
        raw_hold, y_hold, true_hold = sample(4000)
        slope, intercept, opt = m._fit_affine_logit(m._logit(raw_fit), y_fit, fit_intercept=True)
        self.assertTrue(opt["converged"])
        self.assertAlmostEqual(slope, 0.4, delta=0.06)
        self.assertAlmostEqual(intercept, -0.7, delta=0.12)
        cal_hold = m._sigmoid(slope * m._logit(raw_hold) + intercept)
        self.assertLess(nll(cal_hold, y_hold), nll(raw_hold, y_hold) - 0.05)
        self.assertLess(np.mean((cal_hold - y_hold) ** 2), np.mean((raw_hold - y_hold) ** 2))
        self.assertLess(nll(cal_hold, y_hold), nll(true_hold, y_hold) + 0.01)
        t_slope, t_int, _ = m._fit_affine_logit(m._logit(raw_fit), y_fit, fit_intercept=False)
        self.assertEqual(t_int, 0.0)
        cal_t = m._sigmoid(t_slope * m._logit(raw_hold))
        self.assertLess(nll(cal_t, y_hold), nll(raw_hold, y_hold))

    def test_overfit_catboost_held_out_log_loss_improves(self):
        config = make_config(iterations=400, depth=6, learning_rate=0.5)
        config["v2_splits"]["test"].append([3101, "stationary"])
        model = m.train_model(TRAIN, config)
        held_out = [make_lot(3100), make_lot(3101)]
        raw = m.evaluate_model(model, held_out, config)["aggregate"]
        for method in m.PARAMETRIC_METHODS:
            cal = m.fit_calibration(model, CALIB, config, method=method)
            report = m.evaluate_model(cal, held_out, config)
            self.assertEqual(report["scope"]["fit_overlap_lot_ids"], [])
            self.assertLess(report["aggregate"]["log_loss"], raw["log_loss"])
            self.assertAlmostEqual(report["aggregate"]["average_precision"], raw["average_precision"],
                                   places=12)


if __name__ == "__main__":
    unittest.main()
