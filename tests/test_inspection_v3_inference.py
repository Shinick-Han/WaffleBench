import builtins
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from inspection_v2 import model as v2
from inspection_v3 import model as m
from inspection_v3.inference import CompiledPredictor, compile_predictor
from tests.test_inspection_v3_model import CALIB, TRAIN, make_config


def _features():
    x = np.concatenate([lot["public"]["features"] for lot in CALIB])
    x = x.copy()
    x[3, 2] = np.inf
    x[5, :] = np.nan
    return x


X = _features()


def _models():
    out = {}
    for family in ("logistic", "catboost"):
        config = make_config(family)
        base = m.train_model(TRAIN, config)
        out[(family, None)] = base
        for method in ("identity", "isotonic", "platt", "temperature"):
            out[(family, method)] = m.fit_calibration(base, CALIB, config, method=method)
    return out


def _forbid(*_, **__):
    raise AssertionError("warm predict performed forbidden work")


class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = _models()

    def test_warm_outputs_match_reference_for_every_family_and_calibration(self):
        for key, model in self.models.items():
            with self.subTest(key=key):
                compiled = compile_predictor(model)
                self.assertEqual(compiled.model_hash, m.hash_model(model))
                for _ in range(2):  # cold-then-warm repeat gives identical numbers
                    np.testing.assert_allclose(compiled.predict_raw(X), m.predict_raw(model, X),
                                               rtol=0, atol=1e-15)
                    np.testing.assert_allclose(compiled.predict(X), m.predict(model, X),
                                               rtol=0, atol=1e-15)
                self.assertEqual(compiled.predict(X).shape, (len(X),))

    def test_exact_numerics_for_logistic_and_parametric(self):
        for method in (None, "isotonic", "platt", "temperature"):
            model = self.models[("logistic", method)]
            compiled = compile_predictor(model)
            np.testing.assert_array_equal(compiled.predict(X), m.predict(model, X))
            np.testing.assert_array_equal(compiled.predict_raw(X), m.predict_raw(model, X))

    def test_empty_one_row_nan_and_shape_validation(self):
        for key, model in self.models.items():
            with self.subTest(key=key):
                compiled = compile_predictor(model)
                empty = np.zeros((0, 7))
                self.assertEqual(compiled.predict(empty).shape, (0,))
                self.assertEqual(compiled.predict_raw(empty).shape, (0,))
                np.testing.assert_allclose(compiled.predict(X[7]), m.predict(model, X[7:8]),
                                           rtol=0, atol=1e-15)
                nan_row = np.full((1, 7), np.nan)
                p = compiled.predict(nan_row)
                self.assertTrue(np.isfinite(p).all())
                np.testing.assert_allclose(p, m.predict(model, nan_row), rtol=0, atol=1e-15)
                for bad in (np.zeros((3, 6)), np.zeros(8), np.zeros((2, 3, 7))):
                    with self.assertRaises(ValueError):
                        compiled.predict(bad)

    def test_json_roundtrip_model_compiles_to_same_snapshot(self):
        for key, model in self.models.items():
            with self.subTest(key=key):
                again = json.loads(json.dumps(model))
                a, b = compile_predictor(model), compile_predictor(again)
                self.assertEqual(a.model_hash, b.model_hash)
                self.assertEqual(compile_predictor(model).model_hash, a.model_hash)
                np.testing.assert_array_equal(a.predict(X), b.predict(X))

    def test_unknown_family_or_calibration_rejected(self):
        bad = copy.deepcopy(self.models[("logistic", None)])
        bad["family"] = "forest"
        with self.assertRaises(ValueError):
            compile_predictor(bad)
        bad = copy.deepcopy(self.models[("logistic", "platt")])
        bad["calibration"]["method"] = "beta"
        with self.assertRaises(ValueError):
            compile_predictor(bad)


class SnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = _models()

    def test_warm_calls_do_no_json_hash_file_io_or_model_mutation(self):
        for key, model in self.models.items():
            with self.subTest(key=key):
                source = copy.deepcopy(model)
                compiled = compile_predictor(source)
                expected = compiled.predict(X), compiled.predict_raw(X)  # warm-up
                expected_hash = m.hash_model(model)
                with mock.patch.object(json, "dumps", _forbid), \
                        mock.patch.object(json, "dump", _forbid), \
                        mock.patch.object(hashlib, "sha256", _forbid), \
                        mock.patch.object(builtins, "open", _forbid), \
                        mock.patch.object(io, "open", _forbid), \
                        mock.patch.object(os, "fdopen", _forbid), \
                        mock.patch.object(tempfile, "mkstemp", _forbid), \
                        mock.patch.object(copy, "deepcopy", _forbid), \
                        mock.patch.object(v2, "_booster", _forbid), \
                        mock.patch.object(v2, "hash_model", _forbid):
                    for _ in range(3):
                        np.testing.assert_array_equal(compiled.predict(X), expected[0])
                        np.testing.assert_array_equal(compiled.predict_raw(X), expected[1])
                    self.assertEqual(compiled.model_hash, expected_hash)
                self.assertEqual(source, model)  # compile and predict never touch the source

    def test_source_mutation_after_compile_changes_neither_output_nor_hash(self):
        for key, model in self.models.items():
            with self.subTest(key=key):
                source = copy.deepcopy(model)
                compiled = compile_predictor(source)
                before, raw_before, h = compiled.predict(X), compiled.predict_raw(X), compiled.model_hash
                source["mean"][0] += 5.0
                source["scale"][1] *= 3.0
                if source["family"] == "logistic":
                    source["coef"][0] = -source["coef"][0]
                    source["intercept"] += 2.0
                else:
                    tree = source["booster"]["oblivious_trees"][0]
                    tree["leaf_values"] = [v + 1.0 for v in tree["leaf_values"]]
                calibration = source.get("calibration")
                if calibration and "slope" in calibration:
                    calibration["slope"] *= 2.0
                    calibration["intercept"] += 1.0
                if calibration and "y" in calibration:
                    calibration["y"].reverse()
                source["family"] = "forest"
                v2._BOOSTER_CACHE.clear()  # a shared cache flush cannot affect the snapshot
                np.testing.assert_array_equal(compiled.predict(X), before)
                np.testing.assert_array_equal(compiled.predict_raw(X), raw_before)
                self.assertEqual(compiled.model_hash, h)
                self.assertNotEqual(m.hash_model(source), h)

    def test_snapshot_is_immutable_and_params_read_only(self):
        compiled = compile_predictor(self.models[("logistic", "isotonic")])
        with self.assertRaises(AttributeError):
            compiled.model_hash = "x"
        with self.assertRaises(AttributeError):
            compiled._coef = None
        with self.assertRaises(AttributeError):
            del compiled._mean
        for arr in (compiled._mean, compiled._scale, compiled._coef, compiled._iso_x, compiled._iso_y):
            self.assertFalse(arr.flags.writeable)
            with self.assertRaises(ValueError):
                arr[0] = 1.0
        self.assertIs(copy.deepcopy(compiled), compiled)
        self.assertIsInstance(compiled, CompiledPredictor)

    def test_catboost_classifier_is_private(self):
        model = self.models[("catboost", "platt")]
        a, b = compile_predictor(model), compile_predictor(model)
        self.assertIsNot(a._clf, b._clf)
        self.assertIsNot(a._clf, v2._booster(model))
        self.assertEqual(a.model_hash, b.model_hash)

    def test_outputs_are_fresh_writable_arrays(self):
        compiled = compile_predictor(self.models[("catboost", None)])
        p = compiled.predict(X)
        p[:] = -1.0
        self.assertTrue((compiled.predict(X) >= 0).all())


if __name__ == "__main__":
    unittest.main()
