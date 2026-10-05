import contextlib
import csv
import io
import json
import pathlib
import tempfile
import unittest

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only without the image venv
    np = Image = None

from sem_data import cli
from sem_data.audit import AuditError, IMAGE_LEVEL_LIMITATION, NonBinaryMaskError, build_audit, read_mask
from sem_data.manifest import validate_manifest


def _gray(seed, size=(16, 12)):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, size=(size[1], size[0]), dtype=np.uint8), "L")


def _mask(size=(16, 12), box=(4, 4, 8, 8), value=255):
    array = np.zeros((size[1], size[0]), dtype=np.uint8)
    x0, y0, x1, y1 = box
    array[y0:y1, x0:x1] = value
    return Image.fromarray(array, "L")


class Fixture:
    """Writes a Carinthia-S shaped tree: data/carinthia-s.csv, data/images, data/masks."""

    def __init__(self, root: pathlib.Path, extra_columns=(), delimiter=","):
        self.delimiter = delimiter
        self.root = root
        self.base = root / "data"
        (self.base / "images").mkdir(parents=True)
        (self.base / "masks").mkdir(parents=True)
        self.rows = []
        self.extra_columns = tuple(extra_columns)

    def add(self, item_id, label, image=None, mask=None, image_path=None, mask_path=None, **extra):
        image_path = image_path or f"images/{item_id}.png"
        mask_path = mask_path or f"masks/{item_id}.png"
        if image is not None:
            image.save(self.base / image_path)
        if mask is not None:
            mask.save(self.base / mask_path)
        self.rows.append({"image_path": image_path, "mask_path": mask_path,
                          "filename": item_id, "label": label, **extra})

    def write(self):
        columns = ["image_path", "mask_path", "filename", "label", *self.extra_columns]
        with open(self.base / "carinthia-s.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter=self.delimiter)
            writer.writeheader()
            writer.writerows(self.rows)
        return self.root


@unittest.skipIf(Image is None, "numpy and Pillow are required")
class AuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.fixture = Fixture(self.tmp / "root")

    def tearDown(self):
        self._tmp.cleanup()

    def reasons(self, summary):
        return {r["id"]: r["reason"] for r in summary["rejections"]}

    def test_valid_pairs_and_contract_fields(self):
        self.fixture.add("a", "1", _gray(1), _mask())
        self.fixture.add("b", "6.0", _gray(2), _mask(box=(0, 0, 0, 0)))
        _gray(3).save(self.fixture.base / "images" / "c.jpg", quality=90)
        _mask(value=1).save(self.fixture.base / "masks" / "c.png")
        self.fixture.add("c", "2", image_path="images/c.jpg", mask_path="masks/c.png")
        manifest, summary = build_audit(self.fixture.write(), created_at="2026-10-05T00:00:00+00:00")
        self.assertEqual(validate_manifest(manifest, require_split=False, check_files=True), [])
        self.assertEqual([i["id"] for i in manifest["items"]], ["a", "b", "c"])
        item = manifest["items"][0]
        self.assertEqual(item["image"], "data/images/a.png")
        self.assertEqual((item["width"], item["height"]), (16, 12))
        self.assertIsNone(item["source_group"])
        self.assertEqual(manifest["items"][1]["defect_class"], "6")
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["mask_value_conventions"], {"0-only": 1, "0/1": 1, "0/255": 1})
        self.assertEqual(summary["semantic_mask_observations"]["6"]["empty_mask"], 1)
        self.assertIsNone(manifest["units"]["pixel_size_nm"])
        self.assertIn(IMAGE_LEVEL_LIMITATION, manifest["limitations"])
        self.assertEqual(manifest["license"]["id"], "CC-BY-4.0")

    def test_missing_mask_image_wrong_dimension_nonbinary(self):
        self.fixture.add("ok", "1", _gray(1), _mask())
        self.fixture.add("nomask", "1", _gray(2))
        self.fixture.add("noimage", "1", None, _mask())
        self.fixture.add("dims", "1", _gray(3), _mask(size=(16, 13)))
        gradient = Image.fromarray(np.tile(np.arange(16, dtype=np.uint8), (12, 1)), "L")
        self.fixture.add("gray", "1", _gray(4), gradient)
        self.fixture.add("offset", "1", _gray(5), Image.fromarray(np.full((12, 16), 7, np.uint8) + _mask_array(), "L"))
        rgb = np.zeros((12, 16, 3), np.uint8)
        rgb[2, 2] = (255, 0, 0)
        self.fixture.add("color", "1", _gray(6), Image.fromarray(rgb, "RGB"))
        self.fixture.add("nolabel", "", _gray(7), _mask())
        (self.fixture.base / "images" / "broken.png").write_bytes(b"not an image")
        _mask().save(self.fixture.base / "masks" / "broken.png")
        self.fixture.add("broken", "1")
        manifest, summary = build_audit(self.fixture.write())
        self.assertEqual([i["id"] for i in manifest["items"]], ["ok"])
        self.assertEqual(self.reasons(summary), {
            "nomask": "missing_mask", "noimage": "missing_image", "dims": "dimension_mismatch",
            "gray": "nonbinary_mask", "offset": "nonbinary_mask", "color": "nonbinary_mask",
            "nolabel": "missing_label", "broken": "undecodable",
        })

    def test_path_containment_and_duplicate_ids(self):
        outside = self.tmp / "outside.png"
        _gray(1).save(outside)
        self.fixture.add("escape", "1", None, _mask(), image_path="../../outside.png")
        self.fixture.add("abs", "1", None, _mask(), image_path=str(outside))
        self.fixture.add("twice", "1", _gray(2), _mask())
        self.fixture.add("twice", "1", image_path="images/twice.png", mask_path="masks/twice.png")
        manifest, summary = build_audit(self.fixture.write())
        self.assertEqual(manifest["items"], [])
        reasons = [(r["id"], r["reason"]) for r in summary["rejections"]]
        self.assertIn(("escape", "path_not_contained"), reasons)
        self.assertIn(("abs", "path_not_contained"), reasons)
        self.assertEqual([r for r in reasons if r[0] == "twice"], [("twice", "duplicate_id")] * 2)

    def test_exact_duplicates_grouped_and_conflicts_rejected(self):
        same = _gray(9)
        self.fixture.add("d1", "1", same, _mask())
        self.fixture.add("d2", "1", same, _mask())
        other = _gray(10)
        self.fixture.add("c1", "1", other, _mask())
        self.fixture.add("c2", "3", other, _mask())
        third = _gray(11)
        self.fixture.add("m1", "2", third, _mask())
        self.fixture.add("m2", "2", third, _mask(box=(1, 1, 3, 3)))
        manifest, summary = build_audit(self.fixture.write())
        items = {i["id"]: i for i in manifest["items"]}
        self.assertEqual(sorted(items), ["d1", "d2"])
        self.assertEqual(items["d1"]["duplicate_group"], items["d2"]["duplicate_group"])
        self.assertEqual(self.reasons(summary), {
            "c1": "conflicting_duplicate_annotation", "c2": "conflicting_duplicate_annotation",
            "m1": "conflicting_duplicate_annotation", "m2": "conflicting_duplicate_annotation",
        })
        self.assertEqual(summary["duplicates"]["conflicting_groups_rejected"], 2)

    def test_source_group_column_is_preserved(self):
        fixture = Fixture(self.tmp / "grouped", extra_columns=("lot",))
        fixture.add("a", "1", _gray(1), _mask(), lot="L1")
        fixture.add("b", "1", _gray(2), _mask(), lot="")
        manifest, summary = build_audit(fixture.write())
        groups = {i["id"]: i["source_group"] for i in manifest["items"]}
        self.assertEqual(groups, {"a": "L1", "b": None})
        self.assertEqual(summary["source_groups"], {"column": "lot", "available": True})
        self.assertNotIn(IMAGE_LEVEL_LIMITATION, manifest["limitations"])

    def test_soft_edge_masks_strict_by_default_and_opt_in_threshold(self):
        soft = np.zeros((12, 16), np.uint8)
        soft[4:8, 4:8] = 255
        soft[3, 4:8] = 90
        soft[8, 4:8] = 200
        self.fixture.add("soft", "2", _gray(1), Image.fromarray(soft, "L"))
        self.fixture.add("hard", "3", _gray(2), _mask())
        root = self.fixture.write()
        manifest, summary = build_audit(root)
        self.assertEqual([i["id"] for i in manifest["items"]], ["hard"])
        self.assertEqual(summary["classes_without_accepted_items"], ["2"])
        self.assertEqual(summary["rejections_by_class"], {"2": 1})
        self.assertEqual(manifest["mask_policy"]["mode"], "strict-binary")
        self.assertTrue(any("Source classes with no accepted items: 2" in x for x in manifest["limitations"]))
        manifest, summary = build_audit(root, soft_mask_threshold=128)
        self.assertEqual([i["id"] for i in manifest["items"]], ["hard", "soft"])
        self.assertEqual(summary["mask_value_conventions"], {"0/255": 1, "soft-0..255>=128": 1})
        self.assertEqual(summary["scarce_classes"], ["2", "3"])
        with self.assertRaises(ValueError):
            build_audit(root, soft_mask_threshold=0)
        self.assertTrue(manifest["mask_policy"]["development_only"])
        soft_item = next(i for i in manifest["items"] if i["id"] == "soft")
        self.assertEqual(int(read_mask(manifest, soft_item).sum()), 20)
        strict, _ = build_audit(root)
        with self.assertRaises(NonBinaryMaskError):
            read_mask(strict, soft_item)
        self.assertEqual(int(read_mask(strict, strict["items"][0]).sum()), 16)

    def test_semicolon_table_like_the_real_archive(self):
        fixture = Fixture(self.tmp / "semi", delimiter=";")
        fixture.add("a", "6", _gray(1), _mask(box=(0, 0, 0, 0)))
        fixture.add("b", "5", _gray(2), _mask())
        manifest, summary = build_audit(fixture.write())
        self.assertEqual(summary["class_counts"], {"5": 1, "6": 1})
        self.assertEqual(manifest["source"]["table_delimiter"], ";")

    def test_missing_table_fails(self):
        with self.assertRaises(AuditError):
            build_audit(self.fixture.root)

    def test_audit_is_deterministic_and_cli_refuses_output_inside_data(self):
        self.fixture.add("a", "1", _gray(1), _mask())
        root = self.fixture.write()
        first = self.tmp / "out1"
        second = self.tmp / "out2"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["audit", "--data", str(root), "--out", str(first),
                                       "--created-at", "2026-10-05T00:00:00+00:00"]), 0)
            self.assertEqual(cli.main(["audit", "--data", str(root), "--out", str(second),
                                       "--created-at", "2026-10-05T00:00:00+00:00"]), 0)
            self.assertEqual(cli.main(["audit", "--data", str(root), "--out", str(root / "out")]), 1)
        for name in ("audit_manifest.json", "audit_summary.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        self.assertFalse((root / "out").exists())
        self.assertEqual(json.loads((first / "audit_manifest.json").read_text())["split"], None)


def _mask_array():
    return np.array(_mask(value=100))


if __name__ == "__main__":
    unittest.main()
