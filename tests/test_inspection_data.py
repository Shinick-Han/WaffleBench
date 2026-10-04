"""Inspection review v1 generator and model checks (train/dev seeds only, no held-out lots)."""
import math
import unittest

import numpy as np

from inspection_review import data, model

CONFIG = data.load_config()
DEV_SEEDS = {"novel_cluster": 9001, "low_contrast": 9002, "nuisance_heavy": 9003, "process_shift": 9004}
PUBLIC_KEYS = {"lot_id", "seed", "scenario", "site_ids", "wafer", "xy", "die_xy_mm", "in_die_xy_mm",
               "layer", "candidate", "features"}
ORACLE_KEYS = {"doi", "physical", "kind", "electrical_effect", "review_status", "review_positive",
               "review_kind", "review_quality"}


def assert_lots_equal(test, a, b):
    for part in ("public", "oracle"):
        test.assertEqual(set(a[part]), set(b[part]))
        for key, value in a[part].items():
            if isinstance(value, np.ndarray):
                np.testing.assert_array_equal(value, b[part][key], err_msg=key)
            else:
                test.assertEqual(value, b[part][key], key)


class InspectionData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lot = data.generate_lot(100, "stationary", CONFIG)

    def test_config_feature_order(self):
        self.assertEqual(tuple(CONFIG["features"]), data.FEATURES)

    def test_geometry(self):
        p = self.lot["public"]
        g = CONFIG["geometry"]
        self.assertEqual(len(p["site_ids"]), 3915)
        self.assertEqual(len(set(p["site_ids"])), 3915)
        self.assertEqual(np.bincount(p["wafer"]).tolist(), [1305, 1305, 1305])
        die = p["die_xy_mm"]
        corner = np.hypot(np.abs(die[:, 0]) + g["die_width_mm"] / 2, np.abs(die[:, 1]) + g["die_height_mm"] / 2)
        self.assertLessEqual(corner.max(), 147 + 1e-9)
        for axis, pitch in ((0, 8.08), (1, 6.08)):
            steps = die[:, axis] / pitch
            np.testing.assert_allclose(steps, np.round(steps), atol=1e-9)
        self.assertTrue(np.all(np.abs(p["in_die_xy_mm"]) <= [4, 3]))
        np.testing.assert_allclose(p["xy"], (die + p["in_die_xy_mm"]) / 150.0)
        self.assertLess(np.hypot(*p["xy"].T).max(), 1.0)
        self.assertEqual(p["features"].shape, (3915, 7))
        self.assertTrue(set(np.unique(p["layer"])) <= {0, 1})

    def test_split_seeds_and_lot_ids_unique(self):
        splits = CONFIG["splits"]
        pairs = [(s, "stationary") for s in splits["train_seeds"] + splits["validation_seeds"]]
        pairs += [(s, sc) for sc, seeds in splits["test_seeds_by_scenario"].items() for s in seeds]
        seeds = [s for s, _ in pairs]
        self.assertEqual(len(seeds), len(set(seeds)))
        ids = [data.lot_id(s, sc) for s, sc in pairs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.lot["public"]["lot_id"], data.lot_id(100, "stationary"))
        # Identifiers are opaque: no scenario name or seed digits in policy-visible ids.
        for (seed, scenario), lid in zip(pairs, ids):
            self.assertRegex(lid, r"^lot-[0-9a-f]{16}$")
            self.assertNotIn(scenario, lid)
        p = self.lot["public"]
        for sid in p["site_ids"][::97]:
            self.assertTrue(sid.startswith(p["lot_id"] + ":w"))
            self.assertNotIn("stationary", sid)
            self.assertNotIn("100", sid.removeprefix(p["lot_id"]))
        self.assertEqual(data.lot_id(100, "stationary"), data.lot_id(100, "stationary"))
        with self.assertRaises(ValueError):
            data.generate_lot(100, "low_contrast", CONFIG)  # train lots are stationary only
        with self.assertRaises(ValueError):
            data.generate_lot(1100, "stationary", CONFIG)  # test seed of another scenario
        with self.assertRaises(ValueError):
            data.generate_lot(9000, "unknown", CONFIG)

    def test_public_hides_truth_and_outside_optics(self):
        p = self.lot["public"]
        self.assertEqual(set(p), PUBLIC_KEYS)
        self.assertEqual(set(self.lot["oracle"]), ORACLE_KEYS)
        cand, f = p["candidate"], p["features"]
        self.assertTrue(0 < cand.sum() < len(cand))
        self.assertTrue(np.all(np.isnan(f[~cand, :4])))
        self.assertTrue(np.all(np.isfinite(f[cand, :4])))
        self.assertTrue(np.all(np.isfinite(f[:, 4:])))
        self.assertTrue(np.all(f[cand, 0] >= CONFIG["generator"]["optical_threshold"]))
        np.testing.assert_array_equal(f[:, 6], p["layer"])
        np.testing.assert_allclose(f[:, 5], np.hypot(*p["xy"].T))

    def test_oracle_semantics(self):
        o = self.lot["oracle"]
        n = len(o["doi"])
        self.assertTrue(set(np.unique(o["kind"])) <= set(data.KINDS))
        self.assertNotIn("novel", set(o["kind"]))
        np.testing.assert_array_equal(o["doi"], np.isin(o["kind"], data.DOI_KINDS))
        np.testing.assert_array_equal(o["physical"], o["doi"] | (o["kind"] == "benign"))
        self.assertTrue(np.any(o["physical"] & ~o["doi"]))
        self.assertFalse(np.any(o["electrical_effect"] & ~o["doi"]))
        self.assertTrue(np.any(o["doi"] & ~o["electrical_effect"]))
        for key in ("review_status", "review_positive", "review_kind", "review_quality"):
            self.assertEqual(o[key].shape, (n, 2))
        self.assertTrue(set(np.unique(o["review_status"])) <= set(data.REVIEW_STATUSES))
        self.assertFalse(np.any(o["review_positive"] & (o["review_status"] != "ok")))
        np.testing.assert_array_equal(o["review_kind"] != "", o["review_positive"])
        self.assertTrue(np.all((o["review_quality"] >= 0) & (o["review_quality"] <= 1)))
        novel = data.generate_lot(DEV_SEEDS["novel_cluster"], "novel_cluster", CONFIG)["oracle"]
        self.assertIn("novel", set(novel["kind"]))
        self.assertTrue(set(data.KNOWN_KINDS) <= set(novel["kind"]))

    def test_scenario_controls_move_in_expected_direction(self):
        def stats(lot):
            p, o = lot["public"], lot["oracle"]
            return (p["candidate"] & ~o["physical"]).sum(), (p["candidate"] & o["doi"]).sum() / o["doi"].sum()
        base_nuis, base_capture = stats(data.generate_lot(9000, "stationary", CONFIG))
        heavy_nuis, _ = stats(data.generate_lot(DEV_SEEDS["nuisance_heavy"], "nuisance_heavy", CONFIG))
        _, low_capture = stats(data.generate_lot(DEV_SEEDS["low_contrast"], "low_contrast", CONFIG))
        self.assertGreater(heavy_nuis, 2 * base_nuis)
        self.assertLess(low_capture, base_capture - 0.2)

    def test_generation_is_deterministic(self):
        assert_lots_equal(self, self.lot, data.generate_lot(100, "stationary", CONFIG))
        other = data.generate_lot(101, "stationary", CONFIG)
        self.assertFalse(np.array_equal(other["oracle"]["doi"], self.lot["oracle"]["doi"]))

    def test_review_observations_independent_of_order_and_retry(self):
        o = self.lot["oracle"]
        idx = list(range(0, 3915, 37))
        forward = {(i, a): data.review_observation(o, i, a) for i in idx for a in (0, 1)}
        fresh = data.generate_lot(100, "stationary", CONFIG)["oracle"]
        backward = {(i, a): data.review_observation(fresh, i, a) for a in (1, 0) for i in reversed(idx)}
        retry_only = {(i, 1): data.review_observation(fresh, i, 1) for i in idx}
        self.assertEqual(forward, backward)
        for key, value in retry_only.items():
            self.assertEqual(value, forward[key])
        # Attempt streams are independent, not copies.
        self.assertFalse(np.array_equal(o["review_quality"][:, 0], o["review_quality"][:, 1]))
        with self.assertRaises(IndexError):
            data.review_observation(o, 0, 2)
        with self.assertRaises(IndexError):
            data.review_observation(o, -1, 0)

    def test_review_observation_payload(self):
        o = self.lot["oracle"]
        seen = set()
        for i in range(3915):
            for a in (0, 1):
                obs = data.review_observation(o, i, a)
                self.assertEqual(set(obs), {"status", "reported_doi", "reported_kind", "quality"})
                seen.add(obs["status"])
                if obs["status"] != "ok":
                    self.assertIsNone(obs["reported_doi"])
                    self.assertIsNone(obs["reported_kind"])
                elif obs["reported_doi"]:
                    self.assertIn(obs["reported_kind"], data.DOI_KINDS)
                else:
                    self.assertIsNone(obs["reported_kind"])
        self.assertEqual(seen, set(data.REVIEW_STATUSES))
        # Sensor is imperfect in both directions.
        ok0 = o["review_status"][:, 0] == "ok"
        self.assertTrue(np.any(ok0 & o["doi"] & ~o["review_positive"][:, 0]))
        self.assertTrue(np.any(ok0 & ~o["doi"] & o["review_positive"][:, 0]))

    def test_assumptions_recorded(self):
        a = data.GENERATOR_ASSUMPTIONS
        self.assertEqual(set(a["signatures"]), {"none", "nuisance"} | set(data.KINDS[1:]))
        self.assertIn("version", a)


class InspectionModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train = [data.generate_lot(s, "stationary", CONFIG) for s in CONFIG["splits"]["train_seeds"][:4]]
        cls.dev = [data.generate_lot(s, "stationary", CONFIG) for s in CONFIG["splits"]["validation_seeds"][:2]]
        cls.model = model.train_model(cls.train, CONFIG)

    def test_rejects_non_training_lots(self):
        with self.assertRaises(ValueError):
            model.train_model(self.dev, CONFIG)
        with self.assertRaises(ValueError):
            model.train_model([data.generate_lot(DEV_SEEDS["process_shift"], "process_shift", CONFIG)], CONFIG)

    def test_scaler_and_provenance_from_training_candidates(self):
        m = self.model
        self.assertEqual(m["train_ids"], [lot["public"]["lot_id"] for lot in self.train])
        x = np.concatenate([lot["public"]["features"][lot["public"]["candidate"]] for lot in self.train])
        np.testing.assert_allclose(m["mean"], x.mean(axis=0))
        np.testing.assert_allclose(m["scale"], x.std(axis=0))
        self.assertEqual(m["n_train_candidates"], len(x))
        self.assertEqual(m["fit"]["epochs"], CONFIG["model"]["epochs"])

    def test_learns_beyond_intercept(self):
        y = np.concatenate([lot["oracle"]["doi"][lot["public"]["candidate"]] for lot in self.dev]).astype(float)
        x = np.concatenate([lot["public"]["features"][lot["public"]["candidate"]] for lot in self.dev])
        p = model.predict(self.model, x)
        base = self.model["n_train_positive"] / self.model["n_train_candidates"]
        self.assertLess(np.mean((p - y) ** 2), 0.6 * np.mean((base - y) ** 2))
        self.assertGreater(max(abs(c) for c in self.model["coef"]), 0.3)
        report = model.evaluate_model(self.model, self.dev, CONFIG)
        self.assertEqual(report["scope"], "candidates_only")
        self.assertGreater(report["aggregate"]["balanced_accuracy"], 0.75)
        self.assertEqual(len(report["per_lot"]), 2)
        self.assertEqual(report["aggregate"]["n_candidates"], len(y))

    def test_predictions_finite_with_missing_and_extreme_inputs(self):
        rows = np.array([[np.nan] * 4 + [0.1, 0.5, 1.0], [1e9] * 7, [-1e9] * 7, [np.inf] * 7])
        p = model.predict(self.model, rows)
        self.assertEqual(p.shape, (4,))
        self.assertTrue(np.all(np.isfinite(p)) and np.all((p >= 0) & (p <= 1)))
        np.testing.assert_allclose(model.predict(self.model, rows[0]), p[:1])

    def test_update_never_mutates_frozen_model(self):
        frozen_hash = model.hash_model(self.model)
        snapshot = {k: (list(v) if isinstance(v, list) else v) for k, v in self.model.items()}
        row = self.dev[0]["public"]["features"][self.dev[0]["public"]["candidate"]][0]
        before = float(model.predict(self.model, row)[0])
        updated = model.update_model(self.model, row, True, CONFIG)
        self.assertEqual(model.hash_model(self.model), frozen_hash)
        self.assertEqual(self.model, snapshot)
        self.assertNotEqual(model.hash_model(updated), frozen_hash)
        self.assertEqual(updated["parent_hash"], frozen_hash)
        self.assertEqual(updated["online_updates"], 1)
        self.assertEqual(updated["train_ids"], self.model["train_ids"])
        self.assertEqual(updated["mean"], self.model["mean"])
        self.assertGreater(float(model.predict(updated, row)[0]), before)
        down = model.update_model(updated, row, False, CONFIG)
        self.assertLess(float(model.predict(down, row)[0]), float(model.predict(updated, row)[0]))

    def test_hash_is_canonical(self):
        reordered = dict(reversed(list(self.model.items())))
        self.assertEqual(model.hash_model(reordered), model.hash_model(self.model))
        self.assertEqual(len(model.hash_model(self.model)), 64)
        self.assertEqual(model.hash_model(model.train_model(self.train, CONFIG)), model.hash_model(self.model))

    def test_zero_denominators_are_null(self):
        lot = self.dev[0]
        empty = {"public": {**lot["public"], "candidate": np.zeros(3915, dtype=bool)}, "oracle": lot["oracle"]}
        agg = model.evaluate_model(self.model, [empty], CONFIG)["aggregate"]
        self.assertEqual(agg["n_candidates"], 0)
        for key in ("precision", "recall", "balanced_accuracy", "brier"):
            self.assertIsNone(agg[key])


if __name__ == "__main__":
    unittest.main()
