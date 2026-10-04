"""Mock-free integration: real data/model through prepare -> reproduce -> report in a new root.

Uses the actual generator, logistic model and protocol.json (12 train + 4 validation lots).
Never runs the primary campaign and must never generate held-out test lots.
"""

from __future__ import annotations

import dataclasses
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from inspection_review import cli, data, model
from inspection_review.policies import sanitize_public


def _tree_bytes(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


class RealPrepareIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="inspection-prepare-"))
        cls.root = cls.tmp / "newroot"
        cls.config = data.load_config()
        cls.prep = cli.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _manifest(self, split):
        return [m for m in json.loads((self.root / "dataset_manifest.json").read_text())["lots"] if m["split"] == split]

    def _assert_no_heldout(self):
        self.assertFalse((self.root / "lots" / "test").exists())
        self.assertFalse((self.root / "campaign").exists())

    def test_prepare_trains_on_12_train_lots_and_freezes(self):
        self.assertEqual(self.prep["lots"], 16)
        train, valid = self._manifest("train"), self._manifest("validation")
        self.assertEqual(len(train), 12)
        self.assertEqual(len(valid), 4)
        m = json.loads((self.root / "model.json").read_text())
        self.assertEqual(len(m["train_ids"]), 12)
        self.assertEqual(m["train_ids"], [t["lot_id"] for t in train])
        self.assertTrue(set(m["train_ids"]).isdisjoint(v["lot_id"] for v in valid))
        mv = json.loads((self.root / "model_validation.json").read_text())["metrics"]
        self.assertEqual(len(mv["per_lot"]), 4)
        agg = mv["aggregate"]
        self.assertGreater(agg["n_candidates"], 0)
        for k in ("brier", "balanced_accuracy", "recall", "precision"):
            self.assertIsNotNone(agg[k], k)
            self.assertTrue(math.isfinite(agg[k]), k)
        fr, frozen = cli.verify_freeze(self.root, self.config, model, cli.REPO, cli.SOURCE_FILES)
        self.assertEqual(fr["receipt_sha256"], self.prep["receipt_sha256"])
        self.assertEqual(model.hash_model(frozen), fr["model_hash"])
        self._assert_no_heldout()

    def test_only_privileged_loader_carries_seed_and_scenario(self):
        for split in ("train", "validation"):
            entry = self._manifest(split)[0]
            d = Path(entry["path"])
            priv = cli.load_lot_for_training(d)["public"]
            self.assertEqual(priv["seed"], entry["seed"])
            self.assertEqual(priv["scenario"], "stationary")
            pub = cli.load_public(d)
            self.assertTrue({"seed", "scenario", "metadata"}.isdisjoint(pub))
            fr = json.loads((self.root / "freeze.json").read_text())
            view = sanitize_public(pub, fr["impute_mean"], fr["impute_sd"])
            names = {f.name for f in dataclasses.fields(view)}
            self.assertTrue({"seed", "scenario"}.isdisjoint(names))
            self.assertFalse(hasattr(view, "seed") or hasattr(view, "scenario"))

    def test_privileged_loader_rejects_test_lots(self):
        src = Path(self._manifest("validation")[0]["path"])
        fake = self.tmp / "scratch" / "test-split-lot"
        shutil.copytree(src, fake)
        meta = json.loads((fake / "metadata.json").read_text())
        for split in ("test", None):
            if split is None:
                meta.pop("split")
            else:
                meta["split"] = split
            (fake / "metadata.json").write_text(json.dumps(meta))
            with self.assertRaises(cli.HarnessError):
                cli.load_lot_for_training(fake)

    def test_reproduce_and_report_are_development_only(self):
        before = _tree_bytes(self.root / "lots")
        out = cli.reproduce(self.root, lots=1, budgets=[120])
        self.assertIn("development", out["label"])
        self.assertEqual(out["kind"], "development")
        self.assertEqual(out["lots"], 1)
        self.assertEqual(out["runs"], len(self.config["policies"]) * len(self.config["modes"]))
        src = Path(out["out"])
        status = json.loads((src / "status.json").read_text())
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["kind"], "development")
        self.assertEqual(status["budgets"], [120])
        metrics = json.loads((src / "metrics.json").read_text())
        self.assertEqual([l["lot_id"] for l in metrics["lots"]], [self._manifest("validation")[0]["lot_id"]])
        self.assertTrue(all(r["budget"] == 120 for r in metrics["runs"]))
        self._assert_no_heldout()

        stored = {k: v for k, v in _tree_bytes(src).items()}
        rep = cli.report(self.root, source=src)
        self.assertEqual(rep["kind"], "development")
        self.assertFalse(rep["primary_success"])
        body = json.loads(Path(rep["report"]).read_text())
        self.assertEqual(body["primary"]["conclusion"], "inconclusive")
        after = _tree_bytes(src)
        for k, v in stored.items():
            self.assertEqual(after[k], v, f"report modified stored output {k}")
        self.assertEqual(set(after) - set(stored), {"report.json", "report.md"})
        self.assertEqual(_tree_bytes(self.root / "lots"), before)
        self._assert_no_heldout()


if __name__ == "__main__":
    unittest.main()
