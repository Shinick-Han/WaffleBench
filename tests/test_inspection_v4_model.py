import copy
import json
import unittest

import numpy as np

from inspection_v2 import model as v2
from inspection_v3 import model as v3
from inspection_v4 import model as m


def make_lot(seed, scenario="stationary", n=300, lot_id=None, bimodal=False):
    """Crafted test lot: no generator physics; bimodal makes DOI a nonlinear function."""
    rng = np.random.default_rng([seed, len(scenario), 4])
    features = rng.normal(0, 1, (n, 7))
    if bimodal:
        # DOI sits in two lobes on feature 0 (|x| large); non-DOI in the middle.
        doi = rng.random(n) < 0.35
        lobes = np.where(rng.random(n) < 0.5, -2.5, 2.5)
        features[:, 0] = np.where(doi, lobes + rng.normal(0, 0.45, n), rng.normal(0, 0.7, n))
        candidate = np.ones(n, dtype=bool)
    else:
        candidate = features[:, 0] > -0.8
        doi = (features[:, 0] + 0.8 * features[:, 1] ** 2 + rng.normal(0, 0.5, n)) > 1.2
        features[~candidate, :4] = np.nan
    public = {"lot_id": lot_id or f"lot-{seed}-{scenario}", "seed": seed, "scenario": scenario,
              "candidate": candidate, "features": features}
    return {"public": public, "oracle": {"doi": doi}}


def make_config(**overrides):
    config = {
        "splits": {"train_seeds": [100, 101], "validation_seeds": [200],
                   "test_seeds_by_scenario": {"stationary": [1000]}},
        "v2_splits": {"train": [[101, "novel_cluster"]], "calibration": [[3001, "stationary"]],
                      "test": [[3100, "stationary"]]},
        "v4_splits": {"train": [[4100, "stationary"], [4101, "low_contrast"]],
                      "calibration": [[4200, "stationary"], [4201, "process_shift"]],
                      "test": [[4300, "stationary"]]},
        "model": {"family": "numpy_l2_logistic", "l2": 0.01, "epochs": 150, "learning_rate": 0.1,
                  "classification_threshold": 0.5, "online_learning_rate": 0.08},
        "v4_model": dict(m.DEFAULT_V4_MODEL),
    }
    config["v4_model"].update(overrides)
    return config


TRAIN = [make_lot(4100), make_lot(4101, "low_contrast")]
CALIB = [make_lot(4200), make_lot(4201, "process_shift")]
TEST = [make_lot(4300)]


class ExplodingOracle(dict):
    def __getitem__(self, key):
        raise RuntimeError("oracle read before split guard")

    def get(self, key, default=None):
        raise RuntimeError("oracle read before split guard")


def exploding(lot):
    out = {"public": lot["public"], "oracle": ExplodingOracle()}
    return out


class MixtureModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = m.train_model(TRAIN, cls.config)

    def test_deterministic_and_json_roundtrip(self):
        again = m.train_model(TRAIN, self.config)
        self.assertEqual(m.hash_model(again), m.hash_model(self.model))
        restored = json.loads(json.dumps(self.model, allow_nan=False))
        self.assertEqual(m.hash_model(restored), m.hash_model(self.model))
        x = TEST[0]["public"]["features"]
        np.testing.assert_array_equal(m.predict(restored, x), m.predict(self.model, x))
        other = m.train_model(TRAIN, make_config(random_seed=7))
        self.assertNotEqual(m.hash_model(other), m.hash_model(self.model))

    def test_model_schema_and_no_hidden_features(self):
        model = self.model
        self.assertEqual(model["family"], "gaussian_mixture")
        self.assertEqual(model["feature_names"], list(m.FEATURES))
        self.assertEqual(len(model["mean"]), 7)
        self.assertEqual(len(model["scale"]), 7)
        self.assertFalse(model["supports_online_update"])
        self.assertEqual(model["train_ids"], ["lot-4100-stationary", "lot-4101-low_contrast"])
        self.assertEqual(model["provenance"]["split_source"], "v4_splits")
        self.assertEqual(len(model["mixtures"]["positive"]["weights"]), 4)
        self.assertEqual(len(model["mixtures"]["negative"]["weights"]), 3)
        for mix in model["mixtures"].values():
            self.assertEqual(np.asarray(mix["means"]).shape[1], 7)
            self.assertEqual(np.asarray(mix["covariances"]).shape[1:], (7,))
        self.assertAlmostEqual(sum(model["class_priors"].values()), 1.0)
        # Seed/scenario/lot id and extra oracle fields never influence the fit.
        renamed = []
        for lot in TRAIN:
            lot = copy.deepcopy(lot)
            lot["oracle"]["kind"] = np.array(["novel"] * len(lot["oracle"]["doi"]))
            lot["oracle"]["latent"] = np.arange(len(lot["oracle"]["doi"]), dtype=float)
            lot["public"]["extra_hidden"] = np.ones(len(lot["oracle"]["doi"]))
            renamed.append(lot)
        same = m.train_model(renamed, self.config)
        self.assertEqual(same["mixtures"], self.model["mixtures"])
        self.assertEqual(same["mean"], self.model["mean"])

    def test_probability_range_empty_and_nan(self):
        x = np.concatenate([lot["public"]["features"] for lot in TEST + CALIB])
        p = m.predict(self.model, x)
        self.assertTrue(np.all(np.isfinite(p)) and np.all((p >= 0) & (p <= 1)))
        self.assertEqual(m.predict(self.model, np.zeros((0, 7))).shape, (0,))
        nan_row = np.full((2, 7), np.nan)
        nan_row[1, 3] = 1e6  # far outside the train support
        p = m.predict(self.model, nan_row)
        self.assertTrue(np.all(np.isfinite(p)))
        imputed, outside = m.support_diagnostic(self.model, nan_row)
        self.assertEqual(imputed.tolist(), [True, True])
        self.assertEqual(outside.tolist(), [False, True])
        with self.assertRaises(ValueError):
            m.predict(self.model, np.zeros((3, 6)))

    def test_guards_run_before_outcomes(self):
        bad = make_lot(100)  # v1 train seed, not an approved v4 pair
        with self.assertRaisesRegex(ValueError, "not an approved v4_splits.train"):
            m.train_model([exploding(TRAIN[0]), exploding(bad)], self.config)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            m.train_model([exploding(TRAIN[0]), exploding(TRAIN[0])], self.config)
        no_seed = copy.deepcopy(TRAIN[0])
        no_seed["public"].pop("seed")
        with self.assertRaisesRegex(ValueError, "privileged"):
            m.train_model([exploding(no_seed)], self.config)
        with self.assertRaisesRegex(ValueError, "approved calibration"):
            m.fit_calibration(self.model, [exploding(CALIB[0]), exploding(TRAIN[0])], self.config)
        with self.assertRaisesRegex(RuntimeError, "oracle read"):  # valid identities reach outcomes
            m.train_model([exploding(TRAIN[0])], self.config)

    def test_fresh_seed_guard(self):
        config = make_config()
        config["v4_splits"]["train"].append([200, "stationary"])  # a v1 validation seed
        with self.assertRaisesRegex(ValueError, "overlaps split validation_seeds"):
            m.train_model(TRAIN, config)
        config = make_config()
        config["v4_splits"]["calibration"].append([4100, "novel_cluster"])
        with self.assertRaisesRegex(ValueError, "overlaps split"):
            m.train_model(TRAIN, config)
        config = make_config()
        config["v4_splits"]["calibration"] = [[3100, "stationary"]]  # prior v2 test seed
        with self.assertRaisesRegex(ValueError, "overlaps split v2_splits.test"):
            m.fit_calibration(self.model, [make_lot(3100)], config)
        with self.assertRaisesRegex(ValueError, "not an approved calibration"):
            m.fit_calibration(self.model, TEST, self.config)

    def test_train_calibration_overlap_rejected(self):
        config = make_config()
        config["v4_splits"]["calibration"].append([4100, "stationary"])
        with self.assertRaises(ValueError):
            m.fit_calibration(self.model, [TRAIN[0]], config)

    def test_config_validation(self):
        for bad in ({"covariance_type": "spherical"}, {"components_positive": 0},
                    {"components_positive": 9, "covariance_type": "full"}, {"max_iter": 10**6},
                    {"reg_covar": 0.0}, {"unknown": 1}, {"family": "catboost"}):
            config = make_config()
            config["v4_model"].update(bad)
            with self.assertRaises(ValueError):
                m.train_model(TRAIN, config)

    def test_calibration_ranking_and_immutable_base(self):
        before = copy.deepcopy(self.model)
        x = np.concatenate([lot["public"]["features"] for lot in TEST + CALIB])
        raw_logit = m.predict_raw_logit(self.model, x)
        order = np.argsort(raw_logit, kind="mergesort")
        for method in m.CALIBRATION_METHODS:
            cal = m.fit_calibration(self.model, CALIB, self.config, method=method)
            self.assertEqual(self.model, before)
            info = cal["calibration"]
            self.assertEqual(info["base_model_hash"], m.hash_model(self.model))
            self.assertEqual(info["calibration_ids"], ["lot-4200-stationary", "lot-4201-process_shift"])
            self.assertEqual(info["fit_scope"], "candidates_only")
            self.assertEqual(cal["provenance"]["probability"], f"calibrated_{method}")
            self.assertEqual(cal["mean"], self.model["mean"])
            self.assertEqual(cal["scale"], self.model["scale"])
            self.assertEqual(m.hash_model(m.base_model(cal)), m.hash_model(self.model))
            np.testing.assert_array_equal(m.predict_raw(cal, x), m.predict_raw(self.model, x))
            p = m.predict(cal, x)
            self.assertTrue(np.all(np.diff(p[order]) >= -1e-15))
            restored = json.loads(json.dumps(cal, allow_nan=False))
            np.testing.assert_array_equal(m.predict(restored, x), p)
            with self.assertRaises(ValueError):
                m.fit_calibration(cal, CALIB, self.config, method="platt")
            with self.assertRaises(ValueError):
                m.update_model(cal, x[0], True, self.config)
            if method in m.PARAMETRIC_METHODS:
                self.assertGreater(info["slope"], 0)
                logit = m.predict_calibrated_logit(cal, x)
                self.assertTrue(np.all(np.diff(logit[order]) >= 0))
        tampered = m.fit_calibration(self.model, CALIB, self.config, method="platt")
        tampered["mixtures"]["positive"]["weights"][0] += 0.1
        with self.assertRaises(ValueError):
            m.base_model(tampered)
        with self.assertRaises(ValueError):
            m.fit_calibration(self.model, CALIB, self.config, method="beta")

    def test_online_update_rejected(self):
        with self.assertRaisesRegex(ValueError, "frozen"):
            m.update_model(self.model, TEST[0]["public"]["features"][0], True, self.config)

    def test_evaluate_schema_parity_and_scope(self):
        report = m.evaluate_model(self.model, TRAIN + TEST, self.config)
        base_config = make_config()
        del base_config["v4_model"], base_config["v4_splits"]
        base_config["v2_splits"] = {"train": [], "calibration": [[3001, "stationary"]]}
        logistic = v3.train_model([make_lot(100)], {**base_config, "v2_model": {"family": "logistic"}})
        theirs = v3.evaluate_model(logistic, TEST, base_config)
        self.assertEqual(set(report), set(theirs))
        self.assertEqual(set(report["aggregate"]), set(theirs["aggregate"]))
        self.assertEqual([set(e) for e in report["per_lot"]][0], set(theirs["per_lot"][0]))
        self.assertTrue(set(theirs["scope"]) <= set(report["scope"]))
        self.assertEqual(report["scope"]["fit_overlap_lot_ids"], sorted(self.model["train_ids"]))
        self.assertIn("unsupported", report["scope"]["extrapolation"])
        self.assertGreater(report["scope"]["n_imputed_candidates"] + 1, 0)
        mask = TEST[0]["public"]["candidate"]
        p = m.predict(self.model, TEST[0]["public"]["features"][mask])
        y = TEST[0]["oracle"]["doi"][mask]
        self.assertAlmostEqual(report["per_lot"][-1]["brier"], float(np.mean((p - y) ** 2)), places=12)
        self.assertEqual(m.evaluate_model(self.model, [], self.config)["aggregate"]["log_loss"], None)


class DelegationTest(unittest.TestCase):
    def test_baselines_delegate_unchanged(self):
        config = make_config()
        del config["v4_model"], config["v4_splits"]
        config["v2_model"] = {"family": "logistic"}
        lots = [make_lot(100)]
        ours = m.train_model(lots, config)
        ref = v3.train_model(lots, config)
        self.assertEqual(m.hash_model(ours), v3.hash_model(ref))
        x = TEST[0]["public"]["features"]
        np.testing.assert_array_equal(m.predict(ours, x), v3.predict(ref, x))
        calib = [make_lot(3001)]
        for method in m.CALIBRATION_METHODS:
            a = m.fit_calibration(ours, calib, config, method=method)
            b = v3.fit_calibration(ref, calib, config, method=method)
            self.assertEqual(m.hash_model(a), v3.hash_model(b))
        self.assertEqual(m.evaluate_model(ours, TEST, config), v3.evaluate_model(ref, TEST, config))
        updated = m.update_model(ours, x[0], True, config)
        self.assertEqual(updated, v3.update_model(ref, x[0], True, config))


class NonlinearHypothesisTest(unittest.TestCase):
    def test_bimodal_mixture_beats_linear_baseline_on_fresh_crafted_lots(self):
        # Self-contained crafted seeds; no coordinator splits involved.
        config = {"splits": {"train_seeds": [11, 12]}, "v2_splits": {},
                  "model": {"family": "numpy_l2_logistic", "l2": 0.01, "epochs": 200,
                            "learning_rate": 0.1, "classification_threshold": 0.5},
                  "v2_model": {"family": "logistic"},
                  "v4_model": {"family": "gaussian_mixture", "components_positive": 4,
                               "components_negative": 3, "covariance_type": "diag",
                               "max_iter": 100, "random_seed": 2026100404, "reg_covar": 1e-3}}
        train = [make_lot(11, bimodal=True), make_lot(12, bimodal=True)]
        fresh = [make_lot(51, bimodal=True, n=600), make_lot(52, bimodal=True, n=600)]
        mixture = m.train_model(train, config)
        linear = m.train_model(train, {k: v for k, v in config.items() if k != "v4_model"})
        self.assertEqual(linear["family"], "logistic")
        mix_ap = m.evaluate_model(mixture, fresh, config)["aggregate"]["average_precision"]
        lin_ap = m.evaluate_model(linear, fresh, config)["aggregate"]["average_precision"]
        prevalence = m.evaluate_model(mixture, fresh, config)["aggregate"]["prevalence"]
        self.assertLess(lin_ap, prevalence + 0.15)
        self.assertGreater(mix_ap, 0.9)
        self.assertGreater(mix_ap - lin_ap, 0.3)


class ConditioningTest(unittest.TestCase):
    def test_poorly_conditioned_features_stay_finite(self):
        lots = []
        for seed, scenario in ((4100, "stationary"), (4101, "low_contrast")):
            lot = make_lot(seed, scenario)
            f = lot["public"]["features"]
            f[:, 5] = 3.0                      # constant column
            f[:, 6] = f[:, 4] * (1 + 1e-12)    # collinear column
            f[:, 2] = np.round(f[:, 2])        # heavily tied values
            lots.append(lot)
        for cov in ("diag", "full"):
            config = make_config(covariance_type=cov, reg_covar=1e-6, components_positive=8,
                                 components_negative=8)
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                model = m.train_model(lots, config)
                x = np.concatenate([lot["public"]["features"] for lot in lots + TEST])
                logit = m.predict_raw_logit(model, x)
                p = m.predict(model, x)
            self.assertTrue(np.all(np.isfinite(logit)))
            self.assertTrue(np.all((p >= 0) & (p <= 1)))
            for mix in model["mixtures"].values():
                self.assertTrue(np.all(np.isfinite(mix["covariances"])))
                self.assertAlmostEqual(sum(mix["weights"]), 1.0)

    def test_tiny_class_caps_components(self):
        lot = make_lot(4100)
        doi = np.zeros_like(lot["oracle"]["doi"])
        cand = np.flatnonzero(lot["public"]["candidate"])
        doi[cand[:2]] = True
        lot["oracle"]["doi"] = doi
        model = m.train_model([lot], make_config())
        self.assertEqual(model["fit"]["positive"]["components"], 2)
        self.assertTrue(np.all(np.isfinite(m.predict(model, lot["public"]["features"]))))


if __name__ == "__main__":
    unittest.main()
