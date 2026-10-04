import copy
import json
import unittest

import numpy as np

from inspection_review.data import FEATURES
from inspection_v2 import model as v2_model
from inspection_v3 import sensor_yield as sy


def make_lot(seed, scenario="stationary", n=300, lot_id=None):
    rng = np.random.default_rng([seed, len(scenario)])
    signal = rng.normal(0.8, 0.3, n)
    candidate = signal >= 0.62
    features = rng.normal(0, 1, (n, 7))
    features[:, 0] = signal
    features[~candidate, :4] = np.nan
    p_pos = 1 / (1 + np.exp(-4 * (signal - 0.9)))
    status = np.where(rng.random((n, 2)) < 0.1, "failure", "ok").astype("<U7")
    positive = (status == "ok") & (rng.random((n, 2)) < p_pos[:, None])
    oracle = {"doi": np.zeros(n, dtype=bool),  # latent DOI deliberately unrelated to reports
              "review_status": status, "review_positive": positive,
              "review_kind": np.where(positive, "particle", "").astype("<U8"),
              "review_quality": np.full((n, 2), 0.5)}
    public = {"lot_id": lot_id or f"lot-{seed}-{scenario}", "seed": seed, "scenario": scenario,
              "candidate": candidate, "features": features}
    return {"public": public, "oracle": oracle}


def make_config(**overrides):
    config = {
        "splits": {"train_seeds": [100, 101, 102], "validation_seeds": [],
                   "test_seeds_by_scenario": {"stationary": [1000], "novel_cluster": [1100]}},
        "v2_splits": {"train": [[100, "stationary"], [102, "stationary"]],
                      "calibration": [[3001, "stationary"]],
                      "development": [[3200, "stationary"]], "test": [[4000, "stationary"]]},
        "cost": {"dwell": 8.0, "retry_dwell": 4.0, "retry_limit": 1},
        "v3_yield": {"iterations": 40, "depth": 3, "learning_rate": 0.2, "random_seed": 7,
                     "thread_count": 2, "l2_leaf_reg": 3.0},
    }
    config["v3_yield"].update(overrides)
    return config


class ExplodingOracle(dict):
    def __getitem__(self, key):
        raise AssertionError(f"oracle accessed before guard: {key}")


def exploding(lot):
    return {"public": lot["public"], "oracle": ExplodingOracle()}


TRAIN = [make_lot(100), make_lot(102)]


class GuardTest(unittest.TestCase):
    def test_wrong_seed_duplicate_and_overlap_rejected_before_oracle(self):
        config = make_config()
        bad = [[make_lot(999)], [make_lot(101)], [make_lot(3200)], [make_lot(4000)], [make_lot(100, "novel_cluster")],
               [make_lot(100), make_lot(100, lot_id="other")], [make_lot(100), make_lot(100)]]
        for lots in bad:
            with self.assertRaises(ValueError):
                sy.fit_review_yield([exploding(lot) for lot in lots], config)
        overlap = make_config()
        overlap["v2_splits"]["development"].append([102, "stationary"])
        with self.assertRaises(ValueError):
            sy.fit_review_yield([exploding(lot) for lot in TRAIN], overlap)
        no_settings = make_config()
        del no_settings["v3_yield"]
        with self.assertRaises(ValueError):
            sy.fit_review_yield(TRAIN, no_settings)


class LabelTest(unittest.TestCase):
    def _lot(self, status, positive):
        lot = make_lot(100, n=len(status))
        lot["public"]["candidate"] = np.ones(len(status), dtype=bool)
        lot["oracle"]["doi"] = np.ones(len(status), dtype=bool)  # latent truth all DOI
        lot["oracle"]["review_status"] = np.array(status, dtype="<U7")
        lot["oracle"]["review_positive"] = np.array(positive, dtype=bool)
        return lot

    def test_retry_rule_and_exhausted_are_not_positive(self):
        lot = self._lot([["failure", "ok"], ["missing", "failure"], ["ok", "ok"], ["ok", "ok"]],
                        [[False, True], [False, False], [False, True], [True, False]])
        _, y, counts, cost = sy._observed_examples([lot], make_config())
        # retry positive observed; exhausted -> 0; ok-negative stops (second attempt unread); ok-positive.
        np.testing.assert_array_equal(y, [True, False, False, True])
        self.assertEqual(counts["attempts"], 6)
        self.assertEqual(counts["retries"], 2)
        self.assertEqual(counts["exhausted"], 1)
        self.assertEqual((counts["failure"], counts["missing"]), (2, 1))
        self.assertEqual(cost["total_included"], 4 * 8.0 + 2 * 4.0)
        self.assertIn("wafer_load", cost["omitted_components"])
        self.assertFalse(cost["charged_to_evaluation_budget"])

    def test_labels_ignore_latent_doi(self):
        config = make_config()
        flipped = copy.deepcopy(TRAIN)
        for lot in flipped:
            lot["oracle"]["doi"] = ~lot["oracle"]["doi"]
            lot["oracle"]["kind"] = np.full(len(lot["oracle"]["doi"]), "void")
        a, b = sy.fit_review_yield(TRAIN, config), sy.fit_review_yield(flipped, config)
        self.assertEqual(sy.hash_model(a), sy.hash_model(b))


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config()
        cls.model = sy.fit_review_yield(TRAIN, cls.config)

    def test_artifact_is_plain_json_without_oracle_arrays(self):
        model = self.model
        text = json.dumps(model, allow_nan=False)
        self.assertEqual(model["feature_names"], list(FEATURES))
        self.assertEqual(model["target"], "reported_review_positive")
        self.assertFalse(model["online"])
        self.assertFalse(model["supports_online_update"])
        self.assertIn("not", model["provenance"])
        for key in ("doi", "review_status", "review_positive", "kind", "electrical_effect"):
            self.assertNotIn(f'"{key}"', text)
        n_cand = sum(int(lot["public"]["candidate"].sum()) for lot in TRAIN)
        self.assertEqual(model["n_train_candidates"], n_cand)
        self.assertEqual(model["historical_reviews"]["sites"], n_cand)
        restored = json.loads(text)
        self.assertEqual(sy.hash_model(restored), sy.hash_model(model))
        self.assertEqual(sy.hash_model(sy.fit_review_yield(TRAIN, self.config)), sy.hash_model(model))

    def test_predict_empty_nan_roundtrip_and_v2_delegate(self):
        x = make_lot(3200)["public"]["features"]
        p = sy.predict_review_yield(self.model, x)
        self.assertEqual(p.shape, (len(x),))
        self.assertTrue(np.all((p >= 0) & (p <= 1)))
        np.testing.assert_array_equal(p, v2_model.predict_raw(self.model, x))
        restored = json.loads(json.dumps(self.model))
        np.testing.assert_array_equal(sy.predict_review_yield(restored, x), p)
        self.assertEqual(sy.predict_review_yield(self.model, np.zeros((0, 7))).shape, (0,))
        np.testing.assert_allclose(sy.predict_review_yield(self.model, np.full((1, 7), np.nan)),
                                   sy.predict_review_yield(self.model, np.asarray(self.model["mean"])))
        with self.assertRaises(ValueError):
            sy.predict_review_yield({**self.model, "target": "doi"}, x)

    def test_evaluate_guards_split_and_counts_separately(self):
        result = sy.evaluate_review_yield(self.model, [make_lot(3200)], self.config)
        self.assertEqual(result["target"], "reported_review_positive")
        self.assertGreater(result["reviews"]["attempts"], 0)
        for lots, split in (([make_lot(100)], "development"), ([make_lot(3200)], "train"),
                            ([make_lot(4000)], "development")):
            with self.assertRaises(ValueError):
                sy.evaluate_review_yield(self.model, [exploding(l) for l in lots], self.config, split)


if __name__ == "__main__":
    unittest.main()
