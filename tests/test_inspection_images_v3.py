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

from inspection_images import core, data
from inspection_images_v2 import scoring
from inspection_images_v3 import cli, pipeline

CATEGORY = "pcb3"
WEIGHTS_DIR = pathlib.Path(r"C:\Users\user\hacknation7th\output\inspection-image-build\torch-weights")


class StubExtractor:
    """Deterministic (patches, dim=8) descriptors from file bytes; records every path it sees."""

    def __init__(self, patches, tag="stub-v3"):
        self.patches = patches
        self.tag = tag
        self.seen = []

    def identity(self):
        return {"backbone": self.tag, "patches": self.patches}

    def extract(self, paths):
        out = []
        for path in paths:
            self.seen.append(pathlib.Path(path).as_posix())
            seed = int.from_bytes(hashlib.sha256(pathlib.Path(path).read_bytes()).digest()[:8], "little")
            out.append(np.random.default_rng([seed, self.patches]).normal(0, 1, (self.patches, 8)).astype(np.float32))
        return np.stack(out)


def stub_extractors(tag="stub-v3"):
    return {"r224": StubExtractor(196, tag), "r448": StubExtractor(784, tag)}


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
    protocol["dataset"]["expected_counts"] = {"train_normal": 40, "test_total_unlabeled": 10}
    protocol["reservoir_max"] = 500
    protocol["feature_sets"]["r224"]["coreset_size"] = 32
    protocol["feature_sets"]["r448"]["coreset_size"] = 64
    protocol["distance_chunk"] = 97
    protocol["primary_endpoint"]["bootstrap"]["resamples"] = 200
    return protocol


class ProtocolTests(unittest.TestCase):
    def test_preregistered_design_constants(self):
        p = pipeline.load_protocol()
        self.assertEqual(p["seed"], 2026100408)
        self.assertEqual(p["primary_endpoint"]["bootstrap"]["seed_stream"], [2026100408, 7])
        self.assertEqual(p["primary_endpoint"]["bootstrap"]["resamples"], 10000)
        self.assertEqual(p["dataset"]["category"], "pcb3")
        self.assertEqual(p["dataset"]["archive_extract_prefixes"], ["pcb3/", "split_csv/1cls.csv", "LICENSE-DATASET"])
        self.assertEqual(p["dataset"]["expected_counts"]["train_normal"], 905)
        self.assertEqual(p["dataset"]["expected_counts"]["test_total_unlabeled"], 201)
        self.assertEqual(p["environment_pins"], {"python": "3.12.10", "numpy": "2.5.2", "torch": "2.14.1+cpu"})
        self.assertEqual(p["feature_sets"]["r224"], {"resize": [224, 224], "grid": [14, 14], "coreset_size": 1024})
        self.assertEqual(p["feature_sets"]["r448"], {"resize": [448, 448], "grid": [28, 28], "coreset_size": 2048})
        self.assertEqual((p["reservoir_max"], p["projection_dim"], p["threshold_percentile"]), (50000, 16, 95.0))
        m = p["methods"]
        self.assertEqual((m["baseline224"]["aggregation"], m["baseline224"]["top_k"]), ("max", 1))
        self.assertEqual((m["primary448"]["aggregation"], m["primary448"]["top_k"]), ("mean_topk", 3))
        self.assertEqual(m["highres448max"]["role"], "secondary_descriptive")
        self.assertEqual([k for k, v in m.items() if v["role"] == "primary"], ["primary448"])

    def test_design_identical_to_frozen_v2_except_category_seed_study(self):
        v2 = json.loads((pipeline.REPO_ROOT / "inspection_images_v2/protocol.json").read_text(encoding="utf-8"))
        v3 = pipeline.load_protocol()
        for key in ("calibration_fraction", "threshold_percentile", "normalization", "image_common", "backbone",
                    "projection_dim", "reservoir_max", "distance_chunk", "feature_sets", "methods", "method_reference"):
            self.assertEqual(v3[key], v2[key], key)
        for key in ("comparison", "metric", "success_requires_all", "secondary_descriptive", "attribution"):
            self.assertEqual(v3["primary_endpoint"][key], v2["primary_endpoint"][key], key)
        same = {k: v for k, v in v2["dataset"].items() if k not in ("category", "archive_extract_prefixes", "expected_counts")}
        self.assertEqual({k: v3["dataset"][k] for k in same}, same)

    def test_frozen_v1_v2_files_match_pins(self):
        hashes = pipeline.source_hashes()
        self.assertEqual(pipeline.check_frozen_dependencies(pipeline.load_protocol(), hashes), [])
        self.assertEqual(set(pipeline.load_protocol()["frozen_dependency_sha256"]), set(pipeline.FROZEN_DEPENDENCIES))

    def test_current_environment_matches_pins(self):
        self.assertEqual(pipeline.check_environment_pins(pipeline.load_protocol(), pipeline.environment()), [])
        bad = {"python": "3.12.10", "packages": ["numpy==2.5.2", "torch==2.14.1"]}
        self.assertEqual(len(pipeline.check_environment_pins(pipeline.load_protocol(), bad)), 1)

    def test_sources_cover_v1_dependencies_and_protocol_doc(self):
        hashes = pipeline.source_hashes()
        for rel in pipeline.FROZEN_DEPENDENCIES + ["IMAGE_PILOT_V3_PROTOCOL.md", "inspection_images_v3/protocol.json",
                                                   "inspection_images_v3/pipeline.py", "inspection_images_v3/cli.py",
                                                   "inspection_images_v2/scoring.py", "inspection_images/core.py"]:
            self.assertIn(rel, hashes)


class ExtractorShapeTests(unittest.TestCase):
    def test_actual_resnet18_grid_448_and_224(self):
        try:
            import torch  # noqa: F401
            from PIL import Image
            from inspection_images.features import ResNet18Extractor
        except ImportError:
            self.skipTest("CPU torch environment not available")
        if not (WEIGHTS_DIR / "resnet18-f37072fd.pth").exists():
            self.skipTest("shared ResNet18 weights not present")
        protocol = pipeline.load_protocol()
        with tempfile.TemporaryDirectory() as tmp:
            img = pathlib.Path(tmp) / "x.png"
            Image.fromarray(np.random.default_rng(0).integers(0, 255, (300, 500, 3), dtype=np.uint8)).save(img)
            for name, patches in (("r448", 784), ("r224", 196)):
                fs = protocol["feature_sets"][name]
                cfg = {**{k: protocol["image_common"][k] for k in ("mean", "std")}, "resize": fs["resize"]}
                x = ResNet18Extractor(WEIGHTS_DIR, cfg, batch=2, threads=2).extract([img])
                self.assertEqual(x.shape, (1, patches, 384))
                self.assertEqual(patches, fs["grid"][0] * fs["grid"][1])
                self.assertEqual(x.dtype, np.float32)


class ScoringTests(unittest.TestCase):
    def test_calibration_split_905(self):
        ids = [f"pcb3/x/{i:04d}.JPG" for i in range(905)]
        memory, calibration = core.calibration_split(list(reversed(ids)), 0.10, 2026100408)
        self.assertEqual((len(memory), len(calibration)), (815, 90))
        self.assertFalse(set(memory) & set(calibration))
        self.assertEqual((memory, calibration), core.calibration_split(ids, 0.10, 2026100408))

    def test_coreset_and_reservoir_deterministic(self):
        x = np.random.default_rng(5).normal(size=(3000, 24))
        a = core.greedy_coreset(x, 256, 16, 2026100406)
        np.testing.assert_array_equal(a, core.greedy_coreset(x.copy(), 256, 16, 2026100406))
        self.assertEqual(len(set(a.tolist())), 256)
        r = core.reservoir_indices(811 * 784, 50000, 2026100406)
        self.assertEqual(len(r), 50000)
        np.testing.assert_array_equal(r, core.reservoir_indices(811 * 784, 50000, 2026100406))

    def test_top3_and_max_aggregation_math(self):
        d = np.array([[1.0, 5.0, 3.0, 4.0, 2.0], [0.5, 0.5, 0.5, 9.0, 0.1]])
        np.testing.assert_allclose(scoring.aggregate(d, "mean_topk", 3), [4.0, (9.0 + 0.5 + 0.5) / 3])
        np.testing.assert_allclose(scoring.aggregate(d, "max", 1), [5.0, 9.0])
        np.testing.assert_allclose(scoring.aggregate(d, "mean_topk", 1), [5.0, 9.0])
        for bad in (("max", 3), ("mean_topk", 0), ("mean_topk", 6), ("median", 1)):
            with self.assertRaises(ValueError):
                scoring.aggregate(d, *bad)
        rng = np.random.default_rng(3)
        imgs, bank = rng.normal(size=(4, 9, 5)), rng.normal(size=(17, 5))
        np.testing.assert_allclose(scoring.aggregate(scoring.patch_distances(imgs, bank, 7), "max", 1),
                                   core.patch_scores(imgs, bank, 7))

    def test_patch_distance_chunk_parity(self):
        rng = np.random.default_rng(1)
        imgs, bank = rng.normal(size=(5, 13, 7)), rng.normal(size=(41, 7))
        brute = np.sqrt(((imgs[:, :, None, :] - bank[None, None]) ** 2).sum(-1)).min(-1)
        for chunk in (1, 6, 64, 65, 10000):
            np.testing.assert_allclose(scoring.patch_distances(imgs, bank, chunk), brute, rtol=1e-9, atol=1e-9)

    def test_paired_bootstrap(self):
        y = np.array([1] * 10 + [0] * 10)
        same = np.array([1, 0] * 10)
        out = scoring.paired_bootstrap(same, same, y, 500, [1, 7])
        self.assertEqual(out["defect_recall_gain"], {"estimate": 0.0, "ci95": [0.0, 0.0], "resamples": 500})
        a = np.r_[np.ones(10), np.zeros(10)]
        b = np.r_[np.zeros(10), np.zeros(10)]
        out = scoring.paired_bootstrap(a, b, y, 500, [1, 7])
        self.assertEqual(out["defect_recall_gain"]["ci95"], [1.0, 1.0])
        self.assertEqual(out, scoring.paired_bootstrap(a, b, y, 500, [1, 7]))
        with self.assertRaises(ValueError):
            scoring.paired_bootstrap(a, b, np.ones(20), 10, [1, 7])

    def test_primary_decision_uses_integer_counts(self):
        boot = {"defect_recall_gain": {"ci95": [0.01, 0.2]}}
        base = {"true_positive": 33, "false_positive": 4}
        # 0.43 - 0.33 is 0.0999... in float; the integer rule counts it as exactly 10 points.
        decision = pipeline.primary_decision(base, {"true_positive": 43, "false_positive": 10}, 100, 100, boot)
        self.assertTrue(decision["success"])
        self.assertFalse(pipeline.primary_decision(base, {"true_positive": 42, "false_positive": 0}, 100, 100, boot)["success"])
        self.assertFalse(pipeline.primary_decision(base, {"true_positive": 60, "false_positive": 11}, 100, 100, boot)["success"])
        low = {"defect_recall_gain": {"ci95": [0.0, 0.2]}}
        self.assertFalse(pipeline.primary_decision(base, {"true_positive": 60, "false_positive": 0}, 100, 100, low)["success"])


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

    def train(self, extractors=None):
        self.extractors = extractors or stub_extractors()
        with mock.patch.object(pipeline, "test_labels", side_effect=AssertionError("label read during train")):
            return pipeline.train(self.root, self.visa, self.csv, self.extractors, self.protocol)

    def test_train_normal_only_shared_split_cal_only_thresholds(self):
        freeze = self.train()
        _, test_ids = data.split_identities(self.csv, CATEGORY)
        allowed = set(freeze["splits"]["memory"]) | set(freeze["splits"]["calibration"])
        for extractor in self.extractors.values():
            seen = {pathlib.Path(p).relative_to(self.visa).as_posix() for p in extractor.seen}
            self.assertFalse(seen & set(test_ids))
            self.assertEqual(seen, allowed)
        self.assertEqual(len(freeze["splits"]["calibration"]), 4)
        self.assertEqual(set(freeze["calibration"]["thresholds"]), {"baseline224", "primary448", "highres448max"})
        for method, scores in freeze["calibration"]["scores"].items():
            self.assertEqual(sorted(scores), freeze["splits"]["calibration"])
            self.assertEqual(freeze["calibration"]["thresholds"][method],
                             float(np.percentile(list(scores.values()), 95)))
        self.assertEqual(freeze["memory"]["r224"]["coreset"], 32)
        self.assertEqual(freeze["memory"]["r448"]["coreset"], 64)
        self.assertEqual(freeze["memory"]["r448"]["patches_per_image"], 784)
        self.assertEqual(freeze["memory"]["r448"]["reservoir"], 500)
        self.assertFalse(any(v for k, v in freeze["test_usage"].items() if k != "only_bytes_hashed"))
        self.assertEqual(freeze["freeze_digest"], pipeline.canonical_digest(freeze))
        self.assertEqual(json.loads((self.root / "progress.json").read_text())["stage"], "frozen")
        self.assertFalse((self.root / "test-scores.json").exists())

    def test_highres_max_shares_primary_memory_and_differs_only_in_aggregation(self):
        freeze = self.train()
        cal = freeze["calibration"]["scores"]
        for image in freeze["splits"]["calibration"]:
            self.assertGreaterEqual(cal["highres448max"][image], cal["primary448"][image])

    def test_train_deterministic_and_refuses_reused_root(self):
        first = self.train()
        with self.assertRaises(RuntimeError):
            self.train()
        self.root = pathlib.Path(self.tmp.name) / "run2"
        second = self.train()
        self.assertEqual(first["memory"], second["memory"])
        self.assertEqual(first["calibration"], second["calibration"])

    def test_extractor_grid_mismatch_rejected(self):
        with self.assertRaises(RuntimeError):
            self.train({"r224": StubExtractor(196), "r448": StubExtractor(196)})

    def test_test_leak_into_train_rejected(self):
        rows = self.csv.read_text(encoding="utf-8").splitlines()
        leaked = rows + [f"{CATEGORY},test,normal,{CATEGORY}/Data/Images/Normal/0000.JPG,"]
        self.csv.write_text("\n".join(leaked) + "\n", encoding="utf-8")
        self.protocol["dataset"]["split_csv_sha256"] = data.sha256_file(self.csv)
        with self.assertRaises(ValueError):
            self.train()

    def test_evaluate_writes_scores_before_labels_and_is_single_shot(self):
        self.train()
        extractors = stub_extractors()
        real_labels = data.test_labels

        def labels_after_scoring(*args):
            _, test_ids = data.split_identities(self.csv, CATEGORY)
            self.assertTrue((self.root / "evaluation.started").exists())
            written = json.loads((self.root / "test-scores.json").read_text())
            for method in ("baseline224", "primary448", "highres448max"):
                self.assertEqual(sorted(written["scores"][method]), test_ids)
            for extractor in extractors.values():
                seen = {pathlib.Path(p).relative_to(self.visa).as_posix() for p in extractor.seen}
                self.assertTrue(set(test_ids) <= seen)
            return real_labels(*args)

        with mock.patch.object(pipeline, "test_labels", side_effect=labels_after_scoring) as labels:
            result = pipeline.evaluate(self.root, self.visa, self.csv, extractors)
        self.assertEqual(labels.call_count, 1)
        self.assertEqual(result["counts"], {"test_images": 10, "anomaly": 5, "normal": 5})
        self.assertEqual(set(result["primary_endpoint"]["decision"]),
                         {"recall_gain_at_least_10pp", "bootstrap_ci_lower_above_zero", "primary_far_at_most_10pct", "success"})
        self.assertNotIn("accuracy", json.dumps(result["methods"]))
        self.assertEqual(result["test_scores_sha256"], data.sha256_file(self.root / "test-scores.json"))
        with self.assertRaises(RuntimeError):
            pipeline.evaluate(self.root, self.visa, self.csv, stub_extractors())

    def assert_refused(self, extractors=None):
        with mock.patch.object(pipeline, "test_labels", side_effect=AssertionError("label read")):
            with self.assertRaises(RuntimeError):
                pipeline.evaluate(self.root, self.visa, self.csv, extractors or stub_extractors())
        self.assertFalse((self.root / "evaluation.json").exists())
        self.assertFalse((self.root / "test-scores.json").exists())

    def test_started_marker_blocks_resumption(self):
        self.train()
        (self.root / "evaluation.started").write_text("x")
        self.assert_refused()

    def test_tampered_threshold_rejected_even_with_recomputed_digest(self):
        self.train()
        path = self.root / "freeze.json"
        freeze = json.loads(path.read_text())
        freeze["calibration"]["thresholds"]["primary448"] = 0.0
        path.write_text(json.dumps(freeze))
        self.assert_refused()
        freeze["freeze_digest"] = pipeline.canonical_digest(freeze)
        path.write_text(json.dumps(freeze))
        self.assert_refused()

    def test_tampered_split_with_recomputed_digest_rejected(self):
        self.train()
        path = self.root / "freeze.json"
        freeze = json.loads(path.read_text())
        freeze["splits"]["calibration"] = freeze["splits"]["calibration"][1:]
        freeze["freeze_digest"] = pipeline.canonical_digest(freeze)
        path.write_text(json.dumps(freeze))
        self.assert_refused()

    def test_tampered_or_missing_memory_rejected(self):
        self.train()
        with open(self.root / "memory_r448.npz", "ab") as handle:
            handle.write(b"x")
        self.assert_refused()
        (self.root / "memory_r448.npz").unlink()
        self.assert_refused()

    def test_changed_test_or_train_image_rejected(self):
        self.train()
        train_ids, test_ids = data.split_identities(self.csv, CATEGORY)
        (self.visa / test_ids[0]).write_bytes(b"changed")
        self.assert_refused()
        (self.visa / test_ids[0]).write_bytes(f"{test_ids[0]}-bytes".encode())
        (self.visa / train_ids[3]).write_bytes(b"changed")
        self.assert_refused()

    def test_changed_model_or_missing_freeze_rejected(self):
        self.train()
        self.assert_refused(stub_extractors(tag="other-model"))
        self.assert_refused({"r224": StubExtractor(196)})
        (self.root / "freeze.json").unlink()
        self.assert_refused()

    def test_changed_source_or_dependency_rejected(self):
        self.train()
        with mock.patch.object(pipeline, "source_hashes", return_value={"inspection_images/core.py": "0"}):
            self.assert_refused()
        changed = dict(pipeline.source_hashes(), **{"inspection_images/features.py": "0" * 64})
        with mock.patch.object(pipeline, "source_hashes", return_value=changed):
            self.assert_refused()
        with mock.patch.object(pipeline, "FROZEN_DEPENDENCIES", pipeline.FROZEN_DEPENDENCIES + ["inspection_images/missing.py"]):
            with self.assertRaises(RuntimeError):
                pipeline.source_hashes()
            self.assert_refused()

    def test_changed_transitive_dependency_rejected_even_if_freeze_rehashed(self):
        self.train()
        changed = dict(pipeline.source_hashes(), **{"inspection_images_v2/scoring.py": "0" * 64})
        path = self.root / "freeze.json"
        freeze = json.loads(path.read_text())
        freeze["sources"] = changed
        freeze["freeze_digest"] = pipeline.canonical_digest(freeze)
        path.write_text(json.dumps(freeze))
        with mock.patch.object(pipeline, "source_hashes", return_value=changed):
            self.assert_refused()

    def test_train_refuses_changed_frozen_dependency_or_environment(self):
        changed = dict(pipeline.source_hashes(), **{"inspection_images_v2/pipeline.py": "0" * 64})
        with mock.patch.object(pipeline, "source_hashes", return_value=changed):
            with self.assertRaises(RuntimeError):
                self.train()
        self.assertFalse(self.root.exists())
        with mock.patch.object(pipeline, "environment", return_value={"python": "3.12.10", "packages": ["numpy==2.4.0"]}):
            with self.assertRaises(RuntimeError):
                self.train()
        self.assertFalse(self.root.exists())

    def test_strict_threshold_counts_equal_score_as_normal(self):
        c = core.confusion(np.array([1.0, 2.0, 2.0]), np.array([1, 1, 0]), 2.0)
        self.assertEqual((c["true_positive"], c["false_positive"]), (0, 0))

    def test_changed_environment_rejected_before_scoring(self):
        self.train()
        with mock.patch.object(pipeline, "environment", return_value={"python": "different"}):
            self.assert_refused()

    def test_verify_does_not_score_or_read_labels(self):
        self.train()
        extractors = stub_extractors()
        with mock.patch.object(pipeline, "test_labels", side_effect=AssertionError("label read")):
            pipeline.verify_freeze(self.root, self.visa, self.csv, extractors)
        self.assertTrue(all(not e.seen for e in extractors.values()))
        self.assertFalse((self.root / "evaluation.started").exists())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)

    def make_archive(self, extra=()):
        v1 = self.base / "v1"
        (v1 / "downloads").mkdir(parents=True)
        (v1 / "sources").mkdir()
        csv_text = ("object,split,label,image,mask\n"
                    "pcb3,train,normal,pcb3/Data/Images/Normal/0000.JPG,\n"
                    "pcb3,test,anomaly,pcb3/Data/Images/Anomaly/000.JPG,pcb3/Data/Masks/Anomaly/000.png\n"
                    "pcb2,train,normal,pcb2/Data/Images/Normal/0000.JPG,\n").encode()
        (v1 / "sources" / "1cls.csv").write_bytes(csv_text)
        members = {"pcb3/Data/Images/Normal/0000.JPG": b"n", "pcb3/Data/Images/Anomaly/000.JPG": b"a",
                   "pcb2/Data/Images/Normal/0000.JPG": b"other", "pcb1/x.JPG": b"other",
                   "split_csv/1cls.csv": csv_text, "LICENSE-DATASET": b"cc-by", **dict(extra)}
        archive = v1 / "downloads" / "VisA_20220922.tar"
        with tarfile.open(archive, "w") as tar:
            for name, body in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        protocol = copy.deepcopy(pipeline.load_protocol())
        protocol["dataset"]["split_csv_sha256"] = data.sha256_file(v1 / "sources" / "1cls.csv")
        protocol["dataset"]["archive_bytes"] = archive.stat().st_size
        protocol["dataset"]["archive_sha256_observed"] = data.sha256_file(archive)
        return v1, protocol

    def test_prepare_data_extracts_only_pcb3_license_split_and_reverifies(self):
        v1, protocol = self.make_archive()
        build = self.base / "v3"
        paths = cli._paths(build, v1)
        with mock.patch.object(pipeline, "load_protocol", return_value=protocol), \
                mock.patch.object(data, "test_labels", side_effect=AssertionError("label read")):
            out = cli.prepare_data(paths)
            self.assertEqual((out["status"], out["train_images"], out["test_images_unlabeled"]), ("extracted", 1, 1))
            self.assertEqual(cli.prepare_data(paths)["status"], "verified")
        names = {p.relative_to(build / "visa").as_posix() for p in (build / "visa").rglob("*") if p.is_file()}
        self.assertEqual(names, {"pcb3/Data/Images/Normal/0000.JPG", "pcb3/Data/Images/Anomaly/000.JPG",
                                 "split_csv/1cls.csv", "LICENSE-DATASET"})
        (build / "visa" / "pcb3/Data/Images/Normal/0000.JPG").write_bytes(b"tampered")
        with mock.patch.object(pipeline, "load_protocol", return_value=protocol):
            with self.assertRaises(RuntimeError):
                cli.prepare_data(paths)

    def test_prepare_data_rejects_unsafe_member(self):
        v1, protocol = self.make_archive(extra={"pcb3/../../escape.txt": b"x"})
        with mock.patch.object(pipeline, "load_protocol", return_value=protocol):
            with self.assertRaises(ValueError):
                cli.prepare_data(cli._paths(self.base / "v3", v1))
        self.assertFalse((self.base / "escape.txt").exists())

    def test_cli_refuses_v1_build_and_root_outside_build(self):
        v1 = self.base / "v1"
        for argv in (["verify", "--build-dir", str(v1), "--v1-build-dir", str(v1), "--root", str(v1 / "r")],
                     ["verify", "--build-dir", str(v1 / "sub"), "--v1-build-dir", str(v1), "--root", str(v1 / "sub/r")],
                     ["verify", "--build-dir", str(self.base / "v3"), "--v1-build-dir", str(v1),
                      "--root", str(self.base / "elsewhere")]):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(argv), 1)
            self.assertEqual(json.loads(out.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
