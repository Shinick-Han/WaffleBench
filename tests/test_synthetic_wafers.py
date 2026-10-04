"""Independent geometry, observation consistency and leakage boundary checks."""
import csv
import hashlib
import importlib.util
import json
import math
import tempfile
import unittest
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("wafer_generator", ROOT/"scripts/generate_synthetic_wafers.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


class SyntheticWafers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.rows = gen.make_dataset()

    def test_dense_geometry_and_identifiers(self):
        ids = set()
        for wafer in self.data["wafers"]:
            self.assertEqual(len(wafer["dies"]), 1305)
            positions = set()
            for die in wafer["dies"]:
                self.assertNotIn(die["die_id"], ids)
                ids.add(die["die_id"])
                positions.add((die["row"], die["col"]))
                x, y = die["x_mm"], die["y_mm"]
                for dx in (-4, 4):
                    for dy in (-3, 3): self.assertLessEqual(math.hypot(x+dx, y+dy), 147)
            self.assertEqual(len(positions), 1305)
        self.assertEqual(len(ids), 3915)
        self.assertEqual(len(self.rows), 156600)

    def test_bins_reconcile_to_raw_observed_values(self):
        by_die = defaultdict(list)
        for row in self.rows: by_die[row["die_id"]].append(row)
        for wafer in self.data["wafers"]:
            bins = Counter()
            for die in wafer["dies"]:
                rows = by_die[die["die_id"]]
                self.assertEqual(len(rows), 40)
                self.assertEqual(len({r["test_id"] for r in rows}), 40)
                fails, missing = [], 0
                for r in rows:
                    if r["value"] is None:
                        missing += 1; self.assertEqual(r["status"], "missing")
                    elif r["value"] < r["lower"] or r["value"] > r["upper"]:
                        fails.append(r["test_id"]); self.assertEqual(r["status"], "fail")
                    else: self.assertEqual(r["status"], "pass")
                expected = "fail" if fails else "inconclusive" if missing else "pass"
                self.assertEqual(die["bin"], expected)
                self.assertEqual(die["fail_reasons"], fails)
                self.assertEqual(die["tests_missing"], missing)
                bins[expected] += 1
            for b in ("pass", "fail", "inconclusive"):
                self.assertEqual(wafer["summary"][b], bins[b])

    def test_imperfect_inspection_and_benign_defects(self):
        dies = [d for w in self.data["wafers"] for d in w["dies"]]
        self.assertTrue(any(set(d["detections"])-set(d["defects"]) for d in dies), "no simulated false positives")
        self.assertTrue(any(set(d["defects"])-set(d["detections"]) for d in dies), "no simulated misses")
        self.assertTrue(any(d["defects"] and d["bin"] == "pass" for d in dies))
        self.assertTrue(any(len(d["defects"]) > 1 for d in dies))
        self.assertTrue(any(r["value"] is None for r in self.rows))
        catalog = {c["id"]: c for c in self.data["catalog"]}
        sources = {s["id"] for s in self.data["sources"]}
        for c in catalog.values(): self.assertLessEqual(set(c["source_ids"]), sources)
        for d in dies:
            for i in d["defects"]: self.assertTrue(catalog[i]["modeled"])

    def test_authored_spatial_excursions_exist(self):
        w2 = self.data["wafers"][1]["dies"]
        edge = [d for d in w2 if math.hypot(d["x_mm"], d["y_mm"]) > 128]
        core = [d for d in w2 if 45 < math.hypot(d["x_mm"], d["y_mm"]) < 100]
        self.assertGreater(sum(d["bin"] == "fail" for d in edge)/len(edge),
                           sum(d["bin"] == "fail" for d in core)/len(core) + 0.15)
        w3 = self.data["wafers"][2]["dies"]
        scratches = [d for d in w3 if "scratch" in d["defects"]]
        self.assertGreater(len(scratches), 20)
        self.assertTrue(all(abs(d["y_mm"]-0.38*d["x_mm"]+17) < 5 for d in scratches))

    def test_seed_and_archive_reproducibility(self):
        data2, rows2 = gen.make_dataset()
        self.assertEqual(gen.json_bytes(self.data), gen.json_bytes(data2))
        self.assertEqual(self.rows, rows2)
        different, _ = gen.make_dataset(gen.SEED+1)
        self.assertNotEqual(gen.json_bytes(data2), gen.json_bytes(different))
        with tempfile.TemporaryDirectory() as t:
            paths = []
            for n in ("a", "b"):
                base = Path(t)/n
                result = gen.export(base/"raw", base/"web/data.json")
                paths.append((base/"web/synthetic-wafers.zip").read_bytes())
                self.assertEqual(result["counts"]["measurements"], 156600)
                with zipfile.ZipFile(base/"web/synthetic-wafers.zip") as z:
                    manifest = json.loads(z.read("manifest.json"))
                    for name, sha in manifest["sha256"].items():
                        self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), sha)
                    observed = json.loads(z.read("observed-only.json"))
                    self.assertNotIn("catalog", observed)
                    for w in observed["wafers"]:
                        self.assertNotIn("scenario", w)
                        self.assertTrue(all("defects" not in d for d in w["dies"]))
            self.assertEqual(paths[0], paths[1])


if __name__ == "__main__": unittest.main()
