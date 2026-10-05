import contextlib
import copy
import io
import json
import pathlib
import tempfile
import unittest

from sem_data import DEFAULT_SEED, LICENSE, cli
from sem_data.manifest import ManifestError, is_contained_relative, validate_manifest
from sem_data.split import inference_view, make_split


def _item(index, label, group=None, source=None):
    digest = f"{index:064x}"
    return {
        "id": f"img{index:04d}",
        "image": f"data/images/img{index:04d}.jpg",
        "mask": f"data/masks/img{index:04d}.png",
        "defect_class": label,
        "width": 64,
        "height": 48,
        "image_sha256": digest,
        "mask_sha256": digest,
        "duplicate_group": group or f"dup-{index:024x}",
        "source_group": source,
        "split": None,
    }


def _manifest(items, root):
    return {
        "schema_version": 1, "dataset_id": "carinthia-s", "data_mode": "real",
        "created_at": "2026-10-05T00:00:00+00:00", "root": str(root), "license": dict(LICENSE),
        "limitations": [], "split": None, "items": items,
    }


class SplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        items = [_item(i, "6" if i % 3 else "1") for i in range(200)]
        items += [_item(200 + i, "5") for i in range(4)]
        # Exact duplicates share a group and must land in one split.
        for i in range(204, 214):
            items.append(_item(i, "1", group="dup-shared-a"))
        for i in range(214, 220):
            items.append(_item(i, "6", group="dup-shared-b"))
        self.manifest = _manifest(items, self.tmp / "data")

    def tearDown(self):
        self._tmp.cleanup()

    def test_split_is_valid_and_leak_free(self):
        result, summary = make_split(self.manifest)
        self.assertEqual(validate_manifest(result), [])
        self.assertEqual(result["split"]["seed"], DEFAULT_SEED)
        for group in ("dup-shared-a", "dup-shared-b"):
            self.assertEqual(len({i["split"] for i in result["items"] if i["duplicate_group"] == group}), 1)
        counts = summary["counts"]
        self.assertEqual(sum(counts.values()), 220)
        self.assertTrue(130 <= counts["train"] <= 175, counts)
        self.assertTrue(20 <= counts["calibration"] <= 45, counts)
        self.assertTrue(20 <= counts["test"] <= 45, counts)
        self.assertIn("image-level only", result["split"]["group_level"])
        self.assertTrue(result["split"]["test_sealed"])
        # Class "5" has four items: allocation 3/1/0 leaves it absent from test, and that is recorded.
        self.assertEqual(result["split"]["class_counts"]["test"].get("5"), None)
        self.assertEqual(result["split"]["classes_absent"]["test"], ["5"])
        self.assertEqual(summary["classes_absent"]["train"], [])
        self.assertIsNone(self.manifest["items"][0]["split"], "input must not be mutated")

    def test_split_is_deterministic_and_order_independent(self):
        first, _ = make_split(self.manifest)
        shuffled = copy.deepcopy(self.manifest)
        shuffled["items"].reverse()
        second, _ = make_split(shuffled)
        assign = lambda m: {i["id"]: i["split"] for i in m["items"]}
        self.assertEqual(assign(first), assign(second))
        other, _ = make_split(self.manifest, seed=DEFAULT_SEED + 1)
        self.assertNotEqual(assign(first), assign(other))

    def test_source_groups_are_kept_together(self):
        items = [_item(i, "1", source=f"lot{i % 5}") for i in range(50)]
        result, _ = make_split(_manifest(items, self.tmp))
        for lot in range(5):
            self.assertEqual(len({i["split"] for i in result["items"] if i["source_group"] == f"lot{lot}"}), 1)
        self.assertEqual(result["split"]["grouping"], ["duplicate_group", "source_group"])

    def test_validator_detects_leakage_and_bad_fields(self):
        result, _ = make_split(self.manifest)
        leaked = copy.deepcopy(result)
        members = [i for i in leaked["items"] if i["duplicate_group"] == "dup-shared-a"]
        members[0]["split"] = "test" if members[0]["split"] != "test" else "train"
        self.assertTrue(any("spans splits" in e for e in validate_manifest(leaked)))
        broken = copy.deepcopy(result)
        broken["items"][1]["id"] = broken["items"][0]["id"]
        broken["items"][2]["image"] = "../escape.jpg"
        broken["items"][3]["split"] = "validation"
        broken["data_mode"] = "synthetic"
        errors = validate_manifest(broken)
        self.assertTrue(any("duplicate id" in e for e in errors))
        self.assertTrue(any("not contained" in e for e in errors))
        self.assertTrue(any("split must be one of" in e for e in errors))
        self.assertIn("data_mode must be 'real'", errors)

    def test_rejects_invalid_input(self):
        bad = copy.deepcopy(self.manifest)
        bad["items"][0]["image"] = "C:/abs.jpg"
        with self.assertRaises(ManifestError):
            make_split(bad)
        with self.assertRaises(ManifestError):
            make_split(_manifest([], self.tmp))
        with self.assertRaises(ValueError):
            make_split(self.manifest, fractions=(0.5, 0.5, 0.5))

    def test_inference_view_hides_masks_labels_and_test(self):
        result, _ = make_split(self.manifest)
        view = inference_view(result, "calibration")
        self.assertTrue(view)
        for entry in view:
            self.assertEqual(set(entry), {"id", "image", "width", "height", "image_sha256"})
        with self.assertRaises(PermissionError):
            inference_view(result, "test")

    def test_contained_relative(self):
        for path in ("data/a.jpg", "a"):
            self.assertTrue(is_contained_relative(path))
        for path in ("", "/a", "../a", "a/../b", "C:/a", "c:a", "a\\b", "a//b", "./a"):
            self.assertFalse(is_contained_relative(path), path)

    def test_cli_split_is_byte_deterministic(self):
        source = self.tmp / "audit_manifest.json"
        source.write_text(json.dumps(self.manifest), encoding="utf-8")
        outs = [self.tmp / "s1", self.tmp / "s2"]
        with contextlib.redirect_stdout(io.StringIO()):
            for out in outs:
                self.assertEqual(cli.main(["split", "--manifest", str(source), "--out", str(out),
                                           "--seed", str(DEFAULT_SEED)]), 0)
            self.assertEqual(cli.main(["validate", "--manifest", str(outs[0] / "manifest.json")]), 0)
        self.assertEqual((outs[0] / "manifest.json").read_bytes(), (outs[1] / "manifest.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
