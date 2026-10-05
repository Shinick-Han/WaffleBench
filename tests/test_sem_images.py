"""Fixture-mode tests for sem_images. Fixture manifests use data_mode='fixture'; nothing here is real data."""

import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import numpy as np
from PIL import Image

from sem_images import backends, cascade, cli, manifest as mf, metrics, model as sem_model, tiling

HAVE_TORCH = importlib.util.find_spec("torch") is not None
HAVE_SMP = importlib.util.find_spec("segmentation_models_pytorch") is not None
REPO = pathlib.Path(__file__).resolve().parents[1]


def _png(path, arr):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path, format="PNG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_fixture(root, spec=None):
    """spec: list of (id, split, height, width, class, defect boxes)."""
    rng = np.random.default_rng(0)
    spec = spec or [("tr0", "train", 80, 96, "1", [(20, 30, 10, 12)]),
                    ("tr1", "train", 70, 64, "2", [(5, 5, 6, 6)]),
                    ("ca0", "calibration", 90, 100, "1", [(40, 50, 8, 9), (70, 10, 4, 4)]),
                    ("ca1", "calibration", 64, 70, "6", []),
                    ("te0", "test", 75, 81, "2", [(30, 30, 12, 12)])]
    items = []
    for iid, split, h, w, cls, boxes in spec:
        img = (rng.random((h, w)) * 60 + 60).astype(np.uint8)
        mask = np.zeros((h, w), dtype=np.uint8)
        for y, x, bh, bw in boxes:
            img[y:y + bh, x:x + bw] = 230
            mask[y:y + bh, x:x + bw] = 255
        items.append({"id": iid, "image": f"images/{iid}.png", "mask": f"masks/{iid}.png", "defect_class": cls,
                      "width": w, "height": h, "image_sha256": _png(root / "images" / f"{iid}.png", img),
                      "mask_sha256": _png(root / "masks" / f"{iid}.png", mask), "duplicate_group": f"g-{iid}",
                      "source_group": None, "split": split})
    data = {"schema_version": 1, "dataset_id": "carinthia-s", "data_mode": "fixture",
            "created_at": "2026-10-05T00:00:00+00:00", "root": str(root), "license": "fixture",
            "limitations": ["synthetic fixture for tests only"], "split": {"seed": 0}, "items": items}
    path = root / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


class TilingTests(unittest.TestCase):
    def test_plan_covers_with_padding_and_coordinates(self):
        tp = tiling.plan(300, 520, tile=256, overlap=64)
        self.assertEqual([t["x0"] for t in tp["tiles"][:3]], [0, 192, 384])
        self.assertEqual({t["y0"] for t in tp["tiles"]}, {0, 192})
        last = tp["tiles"][-1]
        self.assertEqual(last["valid"], {"x0": 384, "y0": 192, "x1": 520, "y1": 300})
        self.assertEqual(last["valid_in_tile"], {"x": 0, "y": 0, "w": 136, "h": 108})
        self.assertTrue(last["padded"])
        self.assertFalse(tp["tiles"][0]["padded"])

    def test_extract_stitch_roundtrip_preserves_image_and_mask(self):
        rng = np.random.default_rng(1)
        for h, w in [(300, 520), (100, 37), (256, 256), (257, 513)]:
            img = rng.random((h, w))
            mask = rng.random((h, w)) > 0.9
            tp = tiling.plan(h, w, tile=64, overlap=16)
            tiles, valid = tiling.extract(img, tp)
            np.testing.assert_allclose(tiling.stitch(tiles, tp), img, atol=1e-12)
            mtiles, _ = tiling.extract(mask.astype(np.float32), tp)
            self.assertTrue(np.array_equal(tiling.stitch(mtiles, tp) > 0.5, mask))
            # valid region equals the in-image part of each tile
            for k, t in enumerate(tp["tiles"]):
                v = t["valid"]
                self.assertEqual(int(valid[k].sum()), (v["x1"] - v["x0"]) * (v["y1"] - v["y0"]))
                np.testing.assert_array_equal(tiles[k][valid[k]].reshape(v["y1"] - v["y0"], -1),
                                              img[v["y0"]:v["y1"], v["x0"]:v["x1"]])

    def test_padding_values_never_reach_stitched_map(self):
        img = np.random.default_rng(2).random((90, 70))
        tp = tiling.plan(90, 70, tile=64, overlap=16)
        tiles, valid = tiling.extract(img, tp)
        poisoned = tiles.copy()
        poisoned[~valid] = 1e9
        np.testing.assert_allclose(tiling.stitch(poisoned, tp), img, atol=1e-9)
        self.assertEqual(tiling.stitch(poisoned, tp).shape, (90, 70))

    def test_image_smaller_than_tile_and_constant_padding(self):
        img = np.ones((10, 20))
        tp = tiling.plan(10, 20, tile=64, overlap=16)
        self.assertEqual(len(tp["tiles"]), 1)
        tiles, valid = tiling.extract(img, tp, pad_mode="constant")
        self.assertEqual(tiles.shape, (1, 64, 64))
        self.assertEqual(int(valid.sum()), 200)
        self.assertEqual(float(tiles[0, 20:, :].sum()), 0.0)

    def test_overlap_blending_is_weighted(self):
        tp = tiling.plan(64, 112, tile=64, overlap=16)
        self.assertEqual(len(tp["tiles"]), 2)
        maps = np.stack([np.zeros((64, 64)), np.ones((64, 64))])
        out = tiling.stitch(maps, tp)
        self.assertEqual(out[0, 47], 0.0)
        self.assertEqual(out[0, 64], 1.0)
        row = out[0, 48:64]
        self.assertTrue(np.all(np.diff(row) > 0) and 0 < row[0] < 0.5 < row[-1] < 1)

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            tiling.plan(10, 10, tile=64, overlap=64)
        with self.assertRaises(ValueError):
            tiling.stitch(np.zeros((2, 64, 64)), tiling.plan(10, 10, tile=64, overlap=16))


class MetricsTests(unittest.TestCase):
    def test_pixel_counts_dice_iou(self):
        truth = np.zeros((10, 10), bool); truth[2:6, 2:6] = True
        pred = np.zeros((10, 10), bool); pred[4:8, 4:8] = True
        c = metrics.pixel_counts(pred, truth)
        self.assertEqual(c, {"tp": 4, "fp": 12, "fn": 12, "tn": 72})
        d = metrics.dice_iou(c)
        self.assertAlmostEqual(d["dice"], 8 / 32)
        self.assertAlmostEqual(d["iou"], 4 / 28)
        self.assertEqual(metrics.dice_iou({"tp": 0, "fp": 0, "fn": 0, "tn": 5}), {"dice": None, "iou": None})

    def test_component_recall_threshold_and_bins(self):
        truth = np.zeros((40, 40), bool)
        truth[1:3, 1:3] = True      # 4 px, missed
        truth[10:30, 10:30] = True  # 400 px
        pred = np.zeros((40, 40), bool)
        pred[10:30, 10:20] = True   # IoU 0.5
        pred[35:38, 35:38] = True   # stray component
        m = metrics.component_matches(pred, truth, 0.25)
        self.assertEqual([c["detected"] for c in m["components"]], [False, True])
        self.assertEqual(m["predicted_components_without_gt_overlap"], 1)
        strict = metrics.component_matches(pred, truth, 0.6)
        self.assertEqual([c["detected"] for c in strict["components"]], [False, False])
        row = {"id": "a", "defect_class": "1", "counts": metrics.pixel_counts(pred, truth), **m}
        empty = {"id": "b", "defect_class": "6", "counts": metrics.pixel_counts(pred, np.zeros_like(truth)),
                 "components": [], "predicted_components": 2, "predicted_components_without_gt_overlap": 2}
        agg = metrics.aggregate([row, empty], 0.25)
        cr = agg["component_recall"]
        self.assertEqual(cr["overall"], {"detected": 1, "denominator": 2, "recall": 0.5})
        self.assertEqual(cr["by_class"], {"1": {"detected": 1, "denominator": 2, "recall": 0.5}})
        self.assertEqual(cr["by_size_px"]["0-64px"]["denominator"], 1)
        self.assertEqual(cr["by_size_px"]["256-1024px"]["detected"], 1)
        self.assertIsNone(agg["image_level_false_alarm_rate"]["value"])
        self.assertEqual(agg["empty_mask_images"], {"count": 1, "with_any_predicted_component": 1,
                                                    "by_class": {"6": 1}})

    def test_connected_components_8_connectivity_fallback(self):
        mask = np.zeros((5, 5), bool); mask[0, 0] = mask[1, 1] = mask[4, 4] = True
        with mock.patch.dict(sys.modules, {"scipy": None}):
            labels, n = metrics.label_components(mask)
        self.assertEqual(n, 2)
        self.assertEqual(labels[0, 0], labels[1, 1])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path, self.data = make_fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _expect(self, mutate, text):
        data = json.loads(json.dumps(self.data))
        mutate(data)
        with self.assertRaisesRegex(mf.ManifestError, text):
            mf.validate(data, allow_fixture=True)

    def test_fixture_mode_requires_opt_in(self):
        with self.assertRaisesRegex(mf.ManifestError, "data_mode"):
            mf.load(self.path)
        m = mf.load(self.path, allow_fixture=True)
        self.assertEqual(m.report["counts"], {"train": 2, "calibration": 2, "test": 1})
        self.assertTrue(any("source_group" in w for w in m.report["warnings"]))

    def test_contract_violations(self):
        self._expect(lambda d: d["items"][1].update(id="tr0"), "duplicate item id")
        self._expect(lambda d: d["items"][4].update(duplicate_group="g-tr0"), "spans splits")
        self._expect(lambda d: d["items"][0].update(image="../x.png"), "inside root")
        self._expect(lambda d: d["items"][0].update(image="C:/x.png"), "inside root")
        self._expect(lambda d: d["items"][0].update(defect_class="normal"), "normal")
        self._expect(lambda d: d["items"][0].update(synthetic=True), "synthetic")
        self._expect(lambda d: d["items"][0].update(split="val"), "split")
        self._expect(lambda d: d.update(dataset_id="other"), "dataset_id")
        self._expect(lambda d: d.pop("split"), "split metadata")
        self._expect(lambda d: d["items"][0].pop("source_group"), "missing keys")
        self._expect(lambda d: d.update(root="relative/dir"), "absolute")

    def test_mask_gate_and_hash_check(self):
        m = mf.load(self.path, allow_fixture=True)
        reader = m.reader("calibration", masks_allowed=False)
        self.assertEqual(reader.load_image("ca0").shape, (90, 100))
        with self.assertRaises(mf.MaskAccessError):
            reader.load_mask("ca0")
        with self.assertRaises(mf.ManifestError):
            reader.load_image("tr0")  # other split unreachable
        _png(self.root / "images" / "ca0.png", np.zeros((90, 100), np.uint8))
        with self.assertRaisesRegex(mf.ManifestError, "sha256 mismatch"):
            reader.load_image("ca0")


class LazyDependencyTests(unittest.TestCase):
    def test_package_import_does_not_import_torch_or_smp(self):
        code = ("import sys; import sem_images.cli, sem_images.pipeline, sem_images.backends, sem_images.cascade; "
                "print(int('torch' in sys.modules), int('segmentation_models_pytorch' in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["0", "0"])

    def test_missing_smp_gives_clear_error(self):
        with mock.patch.dict(sys.modules, {"segmentation_models_pytorch": None}):
            with self.assertRaisesRegex(sem_model.BackendUnavailable, "d2f65c5e"):
                sem_model.import_smp()

    def test_missing_torch_gives_clear_error(self):
        with mock.patch.dict(sys.modules, {"torch": None}):
            with self.assertRaisesRegex(sem_model.BackendUnavailable, "images-env"):
                sem_model.import_torch()

    def test_missing_explicit_weights_never_downloads(self):
        if not (HAVE_TORCH and HAVE_SMP):
            self.skipTest("torch/smp not installed")
        with self.assertRaisesRegex(FileNotFoundError, "no download"):
            sem_model.model_config(encoder_weights_path="Z:/does/not/exist.pt")

    def test_explicit_encoder_weights_strict_keys(self):
        if not (HAVE_TORCH and HAVE_SMP):
            self.skipTest("torch/smp not installed")
        import torch
        import torchvision
        torch.manual_seed(0)
        state = torchvision.models.resnet18(weights=None).state_dict()  # random init, no download
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "resnet18.pt"
            torch.save(state, path)
            cfg = sem_model.model_config(encoder_weights_path=path)
            self.assertEqual(cfg["encoder_weights_sha256"], mf.sha256_file(path))
            net = sem_model.build(cfg)  # fc.weight / fc.bias dropped deliberately
            expected = state["conv1.weight"].sum(dim=1, keepdim=True)
            self.assertTrue(torch.equal(net.encoder.state_dict()["conv1.weight"], expected))
            bad = dict(state)
            bad["extra.weight"] = torch.zeros(1)
            with self.assertRaisesRegex(ValueError, "unexpected"):
                sem_model.load_encoder_state(sem_model.build(sem_model.model_config()).encoder, bad, 1)
            bad = {k: v for k, v in state.items() if k != "layer1.0.conv1.weight"}
            with self.assertRaisesRegex(ValueError, "missing"):
                sem_model.load_encoder_state(sem_model.build(sem_model.model_config()).encoder, bad, 1)
            torch.save({**state, "fc.bias": torch.ones(1000)}, path)
            with self.assertRaisesRegex(ValueError, "hash differs"):
                sem_model.build(cfg)

    def test_legacy_checkpoint_without_bn_counters_is_zero_filled(self):
        if not (HAVE_TORCH and HAVE_SMP):
            self.skipTest("torch/smp not installed")
        import torch
        import torchvision
        torch.manual_seed(0)
        full = torchvision.models.resnet18(weights=None).state_dict()
        legacy = {k: v for k, v in full.items() if not k.endswith(".num_batches_tracked")}
        self.assertEqual(len(full) - len(legacy), 20)
        snapshot = {k: v.clone() for k, v in legacy.items()}
        fresh = lambda: sem_model.build(sem_model.model_config()).encoder  # noqa: E731
        encoder = fresh()
        for k, v in encoder.state_dict().items():
            if k.endswith(".num_batches_tracked"):
                v.fill_(7)  # prove the loader overwrites, not keeps, the counters
        filled = sem_model.load_encoder_state(encoder, legacy, 1)
        self.assertEqual(len(filled), 20)
        self.assertTrue(all(k.endswith(".num_batches_tracked") for k in filled))
        loaded = encoder.state_dict()
        for k in filled:
            self.assertEqual(loaded[k].dtype, torch.long)
            self.assertEqual(loaded[k].shape, torch.Size([]))
            self.assertEqual(int(loaded[k]), 0)
        self.assertTrue(torch.equal(loaded["layer1.0.bn1.running_mean"], legacy["layer1.0.bn1.running_mean"]))
        # input mapping not mutated: same keys and identical tensors
        self.assertEqual(set(legacy), set(snapshot))
        self.assertTrue(all(torch.equal(legacy[k], snapshot[k]) for k in snapshot))
        # missing learned weight or BN statistic is still rejected
        for key in ("layer2.0.bn1.weight", "layer2.0.bn1.running_var"):
            bad = {k: v for k, v in legacy.items() if k != key}
            with self.assertRaisesRegex(ValueError, "missing"):
                sem_model.load_encoder_state(fresh(), bad, 1)
        # any other unexpected key is rejected, including a stray counter-like key
        for key in ("extra.weight", "layer9.bn1.num_batches_tracked"):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                sem_model.load_encoder_state(fresh(), {**legacy, key: torch.zeros(1)}, 1)
        # wrong shape is rejected by the strict load
        with self.assertRaisesRegex(RuntimeError, "size mismatch"):
            sem_model.load_encoder_state(fresh(), {**legacy, "layer1.0.conv1.weight": torch.zeros(1, 1, 3, 3)}, 1)
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "legacy.pth"
            torch.save(legacy, path)
            cfg = sem_model.model_config(encoder_weights_path=path)
            self.assertIn("num_batches_tracked", cfg["encoder_weights_compat"])
            self.assertIsNone(sem_model.model_config()["encoder_weights_compat"])
            sem_model.build(cfg)


@unittest.skipUnless(HAVE_TORCH and HAVE_SMP, "torch and segmentation_models_pytorch required (images-env)")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        from sem_images import pipeline
        self.pipeline = pipeline
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path, self.data = make_fixture(self.root / "data")
        self.manifest = mf.load(self.path, allow_fixture=True)
        self.run = self.root / "run"
        self.log = []
        self.frozen = pipeline.train(self.manifest, self.run, epochs=1, max_train_images=4, tile=64, overlap=16,
                                     threads=1, access_log=self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_training_reads_only_train_and_freezes(self):
        self.assertEqual({s for s, _, _ in self.log}, {"train"})
        self.assertEqual({(k, i) for _, k, i in self.log},
                         {("image", "tr0"), ("mask", "tr0"), ("image", "tr1"), ("mask", "tr1")})
        self.assertEqual(self.frozen["model"]["sha256"], mf.sha256_file(self.run / "model.pt"))
        self.assertEqual(self.frozen["data_mode"], "fixture")
        self.assertIn("created_at", self.frozen)
        self.assertIn("Post-hackathon", self.frozen["notice"])
        self.assertEqual(set(self.frozen["split_identity"]), {"train", "calibration", "test"})
        with self.assertRaises(self.pipeline.ProtocolError):
            self.pipeline.train(self.manifest, self.run, tile=64, overlap=16)

    def test_same_seed_reproduces_trained_weights(self):
        import torch
        other = self.root / "run2"
        self.pipeline.train(self.manifest, other, epochs=1, max_train_images=4, tile=64, overlap=16, threads=1)
        a = torch.load(self.run / "model.pt", weights_only=True)
        b = torch.load(other / "model.pt", weights_only=True)
        self.assertTrue(all(torch.equal(a[k], b[k]) for k in a))

    def test_training_with_augmentation_records_synthetic_provenance(self):
        log = []
        frozen = self.pipeline.train(self.manifest, self.root / "aug", epochs=1, max_train_images=2, tile=64,
                                     overlap=16, threads=1, augment_copies=1, access_log=log)
        aug = frozen["training"]["augmentation"]
        self.assertEqual(len(aug["synthetic_samples"]), 2)
        self.assertTrue(all(p["synthetic"] for p in aug["synthetic_samples"]))
        self.assertEqual({s for s, _, _ in log}, {"train"})

    def test_predictions_persist_before_any_mask_read(self):
        log = []
        result = self.pipeline.evaluate(self.manifest, self.run, "calibration", threads=1, access_log=log)
        kinds = [k for _, k, _ in log]
        first_mask = kinds.index("mask")
        self.assertEqual(kinds[:first_mask], ["image", "image"])
        self.assertEqual(kinds[first_mask:], ["mask", "mask"])
        receipt = json.loads((self.run / "predictions-calibration.json").read_text(encoding="utf-8"))
        self.assertFalse(receipt["masks_read"])
        shapes = {r["id"]: (r["height"], r["width"]) for r in receipt["items"]}
        self.assertEqual(shapes, {"ca0": (90, 100), "ca1": (64, 70)})
        for row in receipt["items"]:
            self.assertEqual(np.load(self.run / row["file"]).shape, shapes[row["id"]])
        self.assertEqual(result["role"], "development-calibration")
        self.assertEqual(result["summary"]["component_recall"]["overall"]["denominator"], 2)
        self.assertEqual(result["summary"]["empty_mask_images"]["count"], 1)
        self.assertNotIn("te0", json.dumps(result))
        self.assertEqual(result["summary"]["resize_bias"]["components"], 2)
        self.assertFalse(any(s == "test" for s, _, _ in log))

    def test_test_split_opt_in_and_no_rescoring(self):
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "confirm-test"):
            self.pipeline.predict(self.manifest, self.run, "test", threads=1)
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "never train"):
            self.pipeline.predict(self.manifest, self.run, "train", threads=1)
        r = self.pipeline.evaluate(self.manifest, self.run, "test", threads=1, confirm_test=True)
        self.assertEqual(r["role"], "test")
        self.assertTrue((self.run / "test-receipt.json").exists())
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "rescoring"):
            self.pipeline.evaluate(self.manifest, self.run, "test", threads=1, confirm_test=True)
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "rescoring"):
            self.pipeline.predict(self.manifest, self.run, "test", threads=1, confirm_test=True)

    def test_model_and_split_mismatch_rejected(self):
        data = json.loads(json.dumps(self.data))
        data["items"][2]["defect_class"] = "3"
        alt = self.root / "data" / "alt.json"
        alt.write_text(json.dumps(data), encoding="utf-8")
        alt_manifest = mf.load(alt, allow_fixture=True)
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "calibration split identity"):
            self.pipeline.predict(alt_manifest, self.run, "calibration", threads=1)
        self.pipeline.predict(self.manifest, self.run, "calibration", threads=1)
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "already exists"):
            self.pipeline.predict(self.manifest, self.run, "calibration", threads=1)
        pred = next((self.run / "predictions" / "calibration").glob("*.npy"))
        np.save(pred, np.ones((90, 100), np.float16))
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "changed after receipt"):
            self.pipeline.evaluate(self.manifest, self.run, "calibration", threads=1)
        with open(self.run / "model.pt", "ab") as f:
            f.write(b"x")
        with self.assertRaisesRegex(self.pipeline.ProtocolError, "model file hash"):
            self.pipeline.evaluate(self.manifest, self.run, "calibration", threads=1)


class MaskConventionTests(unittest.TestCase):
    def test_grey_masks_rejected_by_default_and_threshold_needs_declaration(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            path, data = make_fixture(root)
            grey = np.zeros((90, 100), np.uint8)
            grey[40:48, 50:59] = 255
            grey[39, 50:59] = 128  # anti-aliased edge
            data["items"][2]["mask_sha256"] = _png(root / "masks" / "ca0.png", grey)
            path.write_text(json.dumps(data), encoding="utf-8")
            m = mf.load(path, allow_fixture=True)
            self.assertEqual(m.report["mask_convention"], {"type": "binary_strict"})
            reader = m.reader("calibration", masks_allowed=True)
            with self.assertRaisesRegex(mf.MaskFormatError, "grey levels"):
                reader.load_mask("ca0")
            self.assertEqual(int(reader.load_mask("ca1").sum()), 0)
            data["mask_convention"] = {"type": "threshold", "threshold": 128}
            with self.assertRaisesRegex(mf.ManifestError, "development_only"):
                mf.validate(data, allow_fixture=True)
            data["mask_convention"].update(development_only=True, provenance="fixture test")
            path.write_text(json.dumps(data), encoding="utf-8")
            m2 = mf.load(path, allow_fixture=True)
            self.assertEqual(int(m2.reader("calibration", True).load_mask("ca0").sum()), 8 * 9 + 9)
            self.assertNotEqual(m.split_identity("calibration"), m2.split_identity("calibration"))
            self.assertTrue(any("threshold" in w for w in m2.report["warnings"]))

    def test_data_worker_mask_policy_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            _, data = make_fixture(pathlib.Path(d))
            data["mask_policy"] = {"mode": "strict-binary", "soft_threshold": None, "development_only": False}
            self.assertEqual(mf.validate(data, allow_fixture=True)["mask_convention"], {"type": "binary_strict"})
            data["mask_policy"] = {"mode": "threshold-soft", "soft_threshold": 128, "development_only": False}
            with self.assertRaisesRegex(mf.ManifestError, "development_only"):
                mf.validate(data, allow_fixture=True)
            data["mask_policy"]["development_only"] = True
            conv = mf.validate(data, allow_fixture=True)["mask_convention"]
            self.assertEqual((conv["type"], conv["threshold"]), ("threshold", 128))
            data["mask_policy"] = {"mode": "otsu"}
            with self.assertRaisesRegex(mf.ManifestError, "unknown mask_policy"):
                mf.validate(data, allow_fixture=True)
            data["mask_policy"] = {"mode": "strict-binary"}
            data["mask_convention"] = {"type": "binary_strict"}
            with self.assertRaisesRegex(mf.ManifestError, "not both"):
                mf.validate(data, allow_fixture=True)

    def test_class_coverage_reported(self):
        with tempfile.TemporaryDirectory() as d:
            m = mf.load(make_fixture(pathlib.Path(d))[0], allow_fixture=True)
            self.assertEqual(m.report["class_counts"]["calibration"], {"1": 1, "6": 1})
            self.assertTrue(any("'6'" in w and "train" in w for w in m.report["warnings"]))


class SamplingAugmentationTests(unittest.TestCase):
    def test_stratified_train_sampling_keeps_minority_class(self):
        from sem_images import pipeline
        spec = [(f"a{i}", "train", 32, 32, "1", [(4, 4, 3, 3)]) for i in range(10)]
        spec += [("b0", "train", 32, 32, "5", [(4, 4, 3, 3)]), ("c0", "calibration", 32, 32, "1", [])]
        with tempfile.TemporaryDirectory() as d:
            m = mf.load(make_fixture(pathlib.Path(d), spec)[0], allow_fixture=True)
            ids, counts = pipeline.stratified_train_ids(m.reader("train", False), 3, np.random.default_rng(0))
            self.assertIn("b0", ids)
            self.assertEqual(counts, {"1": {"chosen": 2, "available": 10}, "5": {"chosen": 1, "available": 1}})
            again, _ = pipeline.stratified_train_ids(m.reader("train", False), 3, np.random.default_rng(0))
            self.assertEqual(ids, again)

    def test_augmentation_train_only_deterministic_with_provenance(self):
        from sem_images import augment
        img = np.full((48, 48), 0.4, np.float32)
        mask = np.zeros((48, 48), bool)
        a = augment.augment(img, mask, "x", 0, 1, "train")
        b = augment.augment(img, mask, "x", 0, 1, "train")
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])
        self.assertTrue(a[2]["synthetic"])
        self.assertEqual(int(a[1].sum()), a[2]["generated_px"])
        self.assertTrue(np.all(a[0][~a[1]] == np.float32(0.4)))
        for split in ("calibration", "test"):
            with self.assertRaises(augment.AugmentationScopeError):
                augment.augment(img, mask, "x", 0, 1, split)


class ResizeBiasRegistrationTests(unittest.TestCase):
    def test_resize_bias_reference(self):
        r = metrics.resize_bias(480, 480, [3, 10, 400], target=224)
        self.assertEqual(r["native_tiles"]["scale"], 1.0)
        self.assertAlmostEqual(r["square_resize"]["aspect_distortion"], 1.0)
        self.assertEqual(r["square_resize"]["components_below_1px"], 1)
        self.assertEqual(r["square_resize"]["components_below_4px"], 2)
        wide = metrics.resize_bias(300, 600, [8], target=448)
        self.assertAlmostEqual(wide["square_resize"]["aspect_distortion"], 0.5)
        self.assertEqual(wide["aspect_preserving_resize"]["aspect_distortion"], 1.0)

    def test_registration_explicit_failure(self):
        from sem_images import registration
        self.assertEqual(registration.plan_reference_registration([])["status"], "not_applicable")
        with mock.patch.dict(sys.modules, {"cv2": None}):
            with self.assertRaises(sem_model.BackendUnavailable):
                registration.register(np.zeros((8, 8)), np.zeros((8, 8)))
        if importlib.util.find_spec("cv2") is not None:
            with self.assertRaises(registration.RegistrationFailed):
                registration.register(np.zeros((64, 64)), np.zeros((64, 64)))


class CascadeTests(unittest.TestCase):
    def test_gate_skips_logged_and_misses_required(self):
        ticks = iter(range(100))
        log = cascade.run(["a", "b", "c"], {"a": 0.9, "b": 0.1, "c": 0.7}.get,
                          lambda i: i == "a", 0.5, clock=lambda: next(ticks))
        self.assertEqual([r["decided_by"] for r in log["items"]], ["slow", "gate_skip", "slow"])
        with self.assertRaises(cascade.GateMissEvaluationRequired):
            cascade.summarize(log)
        misses = cascade.evaluate_misses(log, {"a": True, "b": True, "c": True})
        self.assertEqual((misses["gate_misses"], misses["slow_misses"]), (1, 1))
        self.assertAlmostEqual(misses["end_to_end_recall"], 1 / 3)
        s = cascade.summarize(log, misses)
        self.assertEqual((s["gate_skips"], s["sent_to_slow"]), (1, 2))
        self.assertEqual(s["latency_seconds"]["slow"]["n"], 2)


class BackendTests(unittest.TestCase):
    def test_configs_are_not_outcomes_and_pins_match(self):
        cfgs = backends.backend_configs()
        for name in ("patchcore", "efficientad", "draem"):
            self.assertEqual(cfgs[name]["status"], "not_run")
            self.assertIsNone(cfgs[name]["outcome"])
            self.assertFalse(cfgs[name]["prerequisites"][0]["satisfied"])
        self.assertEqual(cfgs["patchcore"]["source"]["sha"], "fcaa92f124fb1ad74a7acf56726decd4b27cbcad")
        self.assertEqual(cfgs["draem"]["source"]["license_spdx"], "MIT")
        self.assertEqual(cfgs["sahi_reference"]["mapping"]["overlap_height_ratio"], 0.25)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(len(backends.export(d)), 4)

    def test_patchcore_tile_stitching_adapter(self):
        tp = tiling.plan(100, 150, tile=64, overlap=16)
        low = [np.full((8, 8), float(k)) for k in range(len(tp["tiles"]))]
        out = backends.stitch_patchcore_tile_maps(low, tp)
        self.assertEqual(out.shape, (100, 150))
        self.assertEqual(out[0, 0], 0.0)
        with self.assertRaises(ValueError):
            backends.stitch_patchcore_tile_maps(low[:-1], tp)


class CliTests(unittest.TestCase):
    def test_validate_and_error_exit(self):
        with tempfile.TemporaryDirectory() as d:
            path, _ = make_fixture(pathlib.Path(d))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["validate", "--manifest", str(path), "--allow-fixture-manifest"]), 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "valid")
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["validate", "--manifest", str(path)]), 1)
            self.assertEqual(json.loads(buf.getvalue())["error"], "ManifestError")


if __name__ == "__main__":
    unittest.main()
