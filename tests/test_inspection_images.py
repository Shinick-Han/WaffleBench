import copy
import hashlib
import io
import json
import pathlib
import tarfile
import tempfile
import unittest
from unittest import mock

import numpy as np

from inspection_images import core, data, pipeline

CATEGORY = "pcb1"


class StubExtractor:
    """Deterministic (patches=196, dim=8) descriptors from file bytes; records every path it sees."""

    def __init__(self, tag="stub-v1"):
        self.tag = tag
        self.seen = []

    def identity(self):
        return {"backbone": self.tag}

    def extract(self, paths):
        out = []
        for path in paths:
            self.seen.append(pathlib.Path(path).as_posix())
            seed = int.from_bytes(hashlib.sha256(pathlib.Path(path).read_bytes()).digest()[:8], "little")
            out.append(np.random.default_rng(seed).normal(0, 1, (196, 8)).astype(np.float32))
        return np.stack(out)


def make_dataset(base, n_train=40, n_test_normal=5, n_test_anomaly=5):
    base = pathlib.Path(base)
    rows = ["object,split,label,image,mask"]
    entries = ([("train", "normal", f"{CATEGORY}/Data/Images/Normal/{i:04d}.JPG") for i in range(n_train)]
               + [("test", "normal", f"{CATEGORY}/Data/Images/Normal/{9000 + i:04d}.JPG") for i in range(n_test_normal)]
               + [("test", "anomaly", f"{CATEGORY}/Data/Images/Anomaly/{i:03d}.JPG") for i in range(n_test_anomaly)])
    for split, label, rel in entries:
        target = base / "visa" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{rel}-bytes".encode())
        mask = rel.replace("Images", "Masks").replace(".JPG", ".png") if label == "anomaly" else ""
        rows.append(f"{CATEGORY},{split},{label},{rel},{mask}")
    rows.append("pcb2,train,normal,pcb2/Data/Images/Normal/0000.JPG,")
    csv_path = base / "1cls.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return base / "visa", csv_path


def make_protocol(csv_path):
    protocol = copy.deepcopy(pipeline.load_protocol())
    protocol["dataset"]["split_csv_sha256"] = data.sha256_file(csv_path)
    protocol["memory"].update(reservoir_max=500, coreset_size=32)
    protocol["distance_chunk"] = 97
    return protocol


class SplitTests(unittest.TestCase):
    def test_calibration_split_exact_deterministic_disjoint(self):
        ids = [f"pcb1/x/{i:04d}.JPG" for i in range(904)]
        memory, calibration = core.calibration_split(list(reversed(ids)), 0.10, 2026100405)
        self.assertEqual(len(calibration), 90)
        self.assertEqual(len(memory), 814)
        self.assertFalse(set(memory) & set(calibration))
        self.assertEqual(sorted(memory + calibration), ids)
        self.assertEqual((memory, calibration), core.calibration_split(ids, 0.10, 2026100405))
        self.assertNotEqual(calibration, core.calibration_split(ids, 0.10, 1)[1])
        with self.assertRaises(ValueError):
            core.calibration_split(ids + ids[:1], 0.10, 1)

    def test_split_identities_have_no_test_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, csv_path = make_dataset(tmp)
            train, test = data.split_identities(csv_path, CATEGORY)
            self.assertEqual(len(train), 40)
            self.assertEqual(len(test), 10)
            self.assertTrue(all(isinstance(t, str) for t in test))
            self.assertFalse(set(train) & set(test))
            self.assertEqual(sum(data.test_labels(csv_path, CATEGORY).values()), 5)

    def test_split_rejects_unsafe_or_inconsistent_rows(self):
        bad_rows = ["pcb1,train,anomaly,pcb1/a.JPG,",
                    "pcb1,train,normal,../pcb1/a.JPG,",
                    "pcb1,train,normal,/pcb1/a.JPG,",
                    "pcb1,train,normal,pcb2/a.JPG,"]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.csv"
            for row in bad_rows:
                path.write_text("object,split,label,image,mask\n" + row + "\n", encoding="utf-8")
                with self.assertRaises(ValueError, msg=row):
                    data.split_identities(path, CATEGORY)
            path.write_text("object,split,label,image,mask\npcb1,train,normal,pcb1/a.JPG,\n"
                            "pcb1,test,normal,pcb1/a.JPG,\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                data.split_identities(path, CATEGORY)


class CoreTests(unittest.TestCase):
    def test_coreset_deterministic_distinct_and_farthest_start(self):
        x = np.random.default_rng(5).normal(size=(600, 12))
        a = core.greedy_coreset(x, 40, 4, 2026100405)
        b = core.greedy_coreset(x.copy(), 40, 4, 2026100405)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(len(set(a.tolist())), 40)
        z = x @ core.projection_matrix(12, 4, 2026100405)
        self.assertEqual(a[0], int(np.argmax(((z - z.mean(0)) ** 2).sum(1))))
        np.testing.assert_array_equal(core.greedy_coreset(x[:10], 40, 4, 1), np.arange(10))

    def test_reservoir_bounded_sorted_deterministic(self):
        r = core.reservoir_indices(159544, 50000, 2026100405)
        self.assertEqual(len(r), 50000)
        self.assertTrue(np.all(np.diff(r) > 0))
        np.testing.assert_array_equal(r, core.reservoir_indices(159544, 50000, 2026100405))
        np.testing.assert_array_equal(core.reservoir_indices(10, 50, 1), np.arange(10))

    def test_distance_chunking_parity(self):
        rng = np.random.default_rng(1)
        q, bank = rng.normal(size=(301, 7)), rng.normal(size=(53, 7))
        brute = np.sqrt(((q[:, None, :] - bank[None]) ** 2).sum(-1)).min(1)
        for chunk in (1, 7, 300, 301, 10000):
            np.testing.assert_allclose(core.nearest_distance(q, bank, chunk), brute, rtol=1e-9, atol=1e-9)
        imgs = rng.normal(size=(3, 11, 7))
        np.testing.assert_allclose(core.patch_scores(imgs, bank, 5),
                                   [np.sqrt(((im[:, None] - bank[None]) ** 2).sum(-1)).min(1).max() for im in imgs])

    def test_normalization_matches_numpy(self):
        x = np.random.default_rng(2).normal(3, 2, (9, 13, 5)).astype(np.float32)
        x[..., 4] = 1.0
        mean, std = core.normalization_stats(x, chunk=17)
        flat = x.reshape(-1, 5).astype(np.float64)
        np.testing.assert_allclose(mean, flat.mean(0), rtol=1e-9)
        np.testing.assert_allclose(std[:4], flat.std(0)[:4], rtol=1e-6)
        self.assertEqual(std[4], 1.0)

    def test_metrics_known_values(self):
        s = np.array([0.9, 0.8, 0.8, 0.3, 0.1])
        y = np.array([1, 0, 1, 0, 0])
        self.assertAlmostEqual(core.auroc(s, y), (3 + 2 + 0.5) / 6)
        # thresholds 0.9 (P=1, R=.5) then 0.8 (P=2/3, R=1)
        self.assertAlmostEqual(core.average_precision(s, y), 0.5 * 1 + 0.5 * (2 / 3))
        c = core.confusion(s, y, 0.8)
        self.assertEqual((c["true_positive"], c["false_negative"], c["false_positive"], c["true_negative"]), (1, 1, 0, 3))
        self.assertEqual(c["defect_recall"], 0.5)
        self.assertEqual(core.threshold(np.arange(101.0), 95), 95.0)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            core.normalization_stats(np.array([[[np.nan, 1.0]]]))
        with self.assertRaises(ValueError):
            core.threshold([1.0, np.inf], 95)
        with self.assertRaises(ValueError):
            core.auroc([0.1, 0.2], [1, 1])
        with self.assertRaises(ValueError):
            core.auroc([0.1, 0.2], [0, 2])
        with self.assertRaises(ValueError):
            core.confusion([0.1, 0.2], [0, 1], float("nan"))
        with self.assertRaises(ValueError):
            core.nearest_distance(np.ones((2, 3)), np.ones((2, 4)))
        with self.assertRaises(ValueError):
            core.greedy_coreset(np.ones((5, 2)), 0, 2, 1)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.visa, self.csv = make_dataset(self.tmp.name)
        self.protocol = make_protocol(self.csv)
        patcher = mock.patch.object(pipeline, "load_protocol", return_value=self.protocol)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = pathlib.Path(self.tmp.name) / "run"

    def train(self, extractor=None):
        self.extractor = extractor or StubExtractor()
        with mock.patch.object(pipeline, "test_labels", side_effect=AssertionError("label read during train")):
            return pipeline.train(self.root, self.visa, self.csv, self.extractor, self.protocol)

    def test_train_uses_train_images_only_and_train_only_threshold(self):
        freeze = self.train()
        _, test_ids = data.split_identities(self.csv, CATEGORY)
        seen = {pathlib.Path(p).relative_to(self.visa).as_posix() for p in self.extractor.seen}
        self.assertFalse(seen & set(test_ids))
        self.assertEqual(seen, set(freeze["splits"]["memory"]) | set(freeze["splits"]["calibration"]))
        self.assertEqual(len(freeze["splits"]["calibration"]), 4)
        for method, scores in freeze["calibration"]["scores"].items():
            self.assertEqual(sorted(scores), freeze["splits"]["calibration"])
            self.assertAlmostEqual(freeze["calibration"]["thresholds"][method],
                                   float(np.percentile(list(scores.values()), 95)))
        self.assertEqual(freeze["memory"]["coreset"], 32)
        self.assertEqual(freeze["memory"]["reservoir"], 500)
        self.assertEqual(freeze["freeze_digest"], pipeline.canonical_digest(freeze))
        self.assertIn("frozen", json.loads((self.root / "progress.json").read_text())["stage"])

    def test_train_deterministic_and_refuses_reused_root(self):
        first = self.train()
        with self.assertRaises(RuntimeError):
            self.train()
        self.root = pathlib.Path(self.tmp.name) / "run2"
        second = self.train()
        self.assertEqual(first["memory"]["sha256"], second["memory"]["sha256"])
        self.assertEqual(first["calibration"], second["calibration"])

    def test_evaluate_scores_before_reading_labels_and_is_single_shot(self):
        self.train()
        extractor = StubExtractor()
        real_labels = data.test_labels

        def labels_after_scoring(*args):
            _, test_ids = data.split_identities(self.csv, CATEGORY)
            seen = {pathlib.Path(p).relative_to(self.visa).as_posix() for p in extractor.seen}
            self.assertTrue(set(test_ids) <= seen)
            return real_labels(*args)

        with mock.patch.object(pipeline, "test_labels", side_effect=labels_after_scoring):
            result = pipeline.evaluate(self.root, self.visa, self.csv, extractor)
        self.assertEqual(result["counts"], {"test_images": 10, "anomaly": 5, "normal": 5})
        for method in result["methods"].values():
            self.assertTrue(0.0 <= method["image_auroc"] <= 1.0)
            self.assertNotIn("accuracy", json.dumps(method))
        with self.assertRaises(RuntimeError):
            pipeline.evaluate(self.root, self.visa, self.csv, StubExtractor())

    def assert_refused(self, extractor=None):
        with mock.patch.object(pipeline, "test_labels", side_effect=AssertionError("label read")):
            with self.assertRaises(RuntimeError):
                pipeline.evaluate(self.root, self.visa, self.csv, extractor or StubExtractor())
        self.assertFalse((self.root / "evaluation.json").exists())

    def test_tampered_freeze_rejected(self):
        self.train()
        path = self.root / "freeze.json"
        freeze = json.loads(path.read_text())
        freeze["calibration"]["thresholds"]["patch"] = 0.0
        path.write_text(json.dumps(freeze))
        self.assert_refused()

    def test_tampered_freeze_with_recomputed_digest_rejected_by_artifact_or_source(self):
        self.train()
        path = self.root / "freeze.json"
        freeze = json.loads(path.read_text())
        freeze["splits"]["calibration"] = freeze["splits"]["calibration"][1:]
        freeze["freeze_digest"] = pipeline.canonical_digest(freeze)
        path.write_text(json.dumps(freeze))
        self.assert_refused()

    def test_tampered_memory_rejected(self):
        self.train()
        with open(self.root / "memory.npz", "ab") as handle:
            handle.write(b"x")
        self.assert_refused()

    def test_changed_test_image_rejected(self):
        self.train()
        _, test_ids = data.split_identities(self.csv, CATEGORY)
        (self.visa / test_ids[0]).write_bytes(b"changed")
        self.assert_refused()

    def test_changed_model_or_missing_freeze_rejected(self):
        self.train()
        self.assert_refused(StubExtractor(tag="other-model"))
        (self.root / "freeze.json").unlink()
        self.assert_refused()

    def test_changed_source_hash_rejected(self):
        self.train()
        with mock.patch.object(pipeline, "source_hashes", return_value={"inspection_images/core.py": "0"}):
            self.assert_refused()

    def test_changed_python_environment_rejected_before_scoring(self):
        self.train()
        with mock.patch.object(pipeline, "environment", return_value={"python": "different"}):
            self.assert_refused()


class ExtractTests(unittest.TestCase):
    def build_tar(self, path, members):
        with tarfile.open(path, "w") as tar:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    payload = name.encode()
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
                elif kind == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
                    tar.addfile(info)
                elif kind == "hardlink":
                    info.type, info.linkname = tarfile.LNKTYPE, "pcb1/a.JPG"
                    tar.addfile(info)

    def test_extracts_prefix_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = pathlib.Path(tmp) / "a.tar"
            self.build_tar(archive, [("pcb1/a.JPG", "file"), ("pcb2/b.JPG", "file"), ("LICENSE-DATASET", "file")])
            manifest = data.safe_extract(archive, pathlib.Path(tmp) / "out", ["pcb1/", "LICENSE-DATASET"])
            self.assertEqual(sorted(manifest), ["LICENSE-DATASET", "pcb1/a.JPG"])
            self.assertFalse((pathlib.Path(tmp) / "out" / "pcb2").exists())

    def test_rejects_traversal_and_links(self):
        cases = [[("pcb1/../../evil", "file")], [("pcb1/link", "symlink")],
                 [("pcb1/a.JPG", "file"), ("pcb1/hard", "hardlink")]]
        for members in cases:
            with tempfile.TemporaryDirectory() as tmp:
                archive = pathlib.Path(tmp) / "a.tar"
                self.build_tar(archive, members)
                with self.assertRaises(ValueError, msg=members):
                    data.safe_extract(archive, pathlib.Path(tmp) / "out", ["pcb1/"])
                self.assertFalse((pathlib.Path(tmp) / "evil").exists())


if __name__ == "__main__":
    unittest.main()
