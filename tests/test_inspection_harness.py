"""Harness tests with small mock public arrays and a mock logistic model (NON-SCIENTIFIC fixtures)."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from inspection_review import cli, harness, reporting
from inspection_review.policies import POLICIES, PUBLIC_WHITELIST, SelectionState, sanitize_public

N_PER_WAFER = 24
KINDS = np.array(["particle", "bridge", "novel", "none"])


def mock_config() -> dict:
    return {
        "study_id": "mock-study",
        "splits": {"train_seeds": [1, 2], "validation_seeds": [3],
                   "test_seeds_by_scenario": {"stationary": [10, 11], "novel_cluster": [20, 21]}},
        "model": {"classification_threshold": 0.5, "confident_negative_threshold": 0.1, "online_learning_rate": 0.1},
        "cost": {"wafer_load": 8.0, "stage_base": 1.0, "stage_per_normalized_distance": 2.0, "dwell": 8.0,
                 "retry_dwell": 4.0, "outside_rescan": 12.0, "retry_limit": 1},
        "budgets": [40, 90], "primary_budget": 40, "modes": ["candidate_only", "with_rescan"],
        "policies": list(POLICIES),
        "selection": {"audit_period": 5, "rescan_period": 10, "spatial_bandwidth": 0.22, "spatial_prior_strength": 4.0,
                      "spatial_weight": 0.4, "uncertainty_weight": 0.35, "diversity_weight": 0.15, "novelty_weight": 0.35},
        "statistics": {"bootstrap_seed": 7, "bootstrap_replicates": 200, "primary_comparator": "learned",
                       "primary_mode": "candidate_only", "primary_metric": "true_doi_confirmed",
                       "success_relative_gain_target": 0.2, "cost_reduction_target": 0.3, "matched_capture_target": 2},
    }


def mock_lot(seed: int, scenario: str, config: dict | None = None) -> dict:
    rng = np.random.default_rng(seed)
    n = 3 * N_PER_WAFER
    wafer = np.repeat(np.arange(3), N_PER_WAFER)
    xy = rng.uniform(-1, 1, size=(n, 2))
    cand = rng.random(n) < 0.5
    feats = rng.normal(size=(n, 7))
    feats[~cand, :4] = np.nan
    doi = rng.random(n) < 0.3
    kind = np.where(doi, KINDS[rng.integers(0, 3, n)], "none")
    status = np.where(rng.random((n, 2)) < 0.15, "failure", "ok")
    status = np.where(rng.random((n, 2)) < 0.1, "missing", status)
    positive = np.where(rng.random((n, 2)) < 0.9, doi[:, None], ~doi[:, None])
    return {"public": {"lot_id": hashlib.sha256(f"{seed}".encode()).hexdigest()[:12], "seed": seed, "scenario": scenario,
                       "site_ids": np.array([f"s{seed}-{i}" for i in range(n)]), "wafer": wafer, "xy": xy,
                       "die_xy_mm": xy * 140, "in_die_xy_mm": rng.uniform(0, 1, (n, 2)), "layer": wafer % 2,
                       "candidate": cand, "features": feats},
            "oracle": {"doi": doi, "physical": doi.copy(), "kind": kind, "electrical_effect": doi & (rng.random(n) < 0.5),
                       "review_status": status, "review_positive": positive,
                       "review_kind": np.where(positive, np.where(kind == "none", "particle", kind)[:, None], ""),
                       "review_quality": rng.uniform(0.5, 1, (n, 2))}}


def review_observation(oracle, i, attempt):
    st = str(oracle["review_status"][i, attempt])
    if st != "ok":
        return {"status": st, "reported_doi": None, "reported_kind": None, "quality": 0.0}
    pos = bool(oracle["review_positive"][i, attempt])
    return {"status": "ok", "reported_doi": pos, "reported_kind": str(oracle["review_kind"][i, attempt]) if pos else None,
            "quality": float(oracle["review_quality"][i, attempt])}


def _sig(z):
    return 1.0 / (1.0 + np.exp(-z))


def train_model(lots, config):
    w = np.zeros(7)
    for lot in lots:
        f = np.nan_to_num(np.asarray(lot["public"]["features"], float))
        w += f[np.asarray(lot["oracle"]["doi"], bool)].mean(axis=0) * 0.3
    return {"w": w.tolist(), "b": -0.5}


def predict(model, features):
    return _sig(np.nan_to_num(np.asarray(features, float)) @ np.asarray(model["w"]) + model["b"])


def update_model(model, row, label, config):
    p = float(predict(model, np.asarray(row)[None, :])[0])
    g = (float(label) - p) * config["model"]["online_learning_rate"]
    return {"w": (np.asarray(model["w"]) + g * np.nan_to_num(np.asarray(row, float))).tolist(), "b": model["b"] + g}


def hash_model(model):
    return hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()


def evaluate_model(model, lots, config):
    return {"accuracy": 0.5, "lots": len(lots)}


MODEL = SimpleNamespace(train_model=train_model, predict=predict, update_model=update_model,
                        hash_model=hash_model, evaluate_model=evaluate_model)
DATA = SimpleNamespace(load_config=lambda path=None: mock_config(), generate_lot=mock_lot,
                       review_observation=review_observation)
MEAN, SD = [0.0] * 7, [1.0] * 7


def _setup(seed=5):
    cfg = mock_config()
    lot = mock_lot(seed, "stationary")
    view = sanitize_public(lot["public"], MEAN, SD)
    model = train_model([mock_lot(1, "s")], cfg)
    return cfg, lot, view, model, predict(model, view.features_imputed)


def _run(cfg, view, oracle, model, fp, policy, mode="candidate_only", budget=90, review=None):
    review = review or (lambda i, a: review_observation(oracle, i, a))
    return harness.run_selection(view, review, policy_name=policy, mode=mode, budget=budget, frozen_model=model,
                                 frozen_p=fp, model_api=MODEL, config=cfg)


class BoundaryTests(unittest.TestCase):
    def test_policy_input_is_whitelist_without_seed_scenario_oracle(self):
        _, lot, view, *_ = _setup()
        fields = set(view.__dataclass_fields__)
        self.assertTrue({"seed", "scenario", "doi", "kind", "review_status"}.isdisjoint(fields))
        self.assertTrue(set(PUBLIC_WHITELIST) <= fields)
        self.assertFalse(view.features.flags.writeable)

    def test_oracle_invariance_under_mutated_future_truth(self):
        cfg, lot, view, model, fp = _setup()
        for mode in ("candidate_only", "with_rescan"):
            for pol in POLICIES:
                a = _run(cfg, view, lot["oracle"], model, fp, pol, mode)
                seen = {r["site_index"] for r in a["rows"]}
                mutated = copy.deepcopy(lot["oracle"])
                for i in range(view.n):
                    if i not in seen:
                        mutated["doi"][i] = not mutated["doi"][i]
                        mutated["kind"][i] = "novel"
                        mutated["review_status"][i] = ["missing", "failure"]
                        mutated["review_positive"][i] = ~mutated["review_positive"][i]
                b = _run(cfg, view, mutated, model, fp, pol, mode)
                self.assertEqual([r["site_index"] for r in a["rows"]], [r["site_index"] for r in b["rows"]], (pol, mode))

    def test_only_chosen_sites_and_attempts_are_reviewed(self):
        cfg, lot, view, model, fp = _setup()
        calls = []

        def review(i, a):
            calls.append((i, a))
            return review_observation(lot["oracle"], i, a)

        run = _run(cfg, view, lot["oracle"], model, fp, "falsify", review=review)
        expected = [(r["site_index"], a["attempt"]) for r in run["rows"] for a in r["attempts"]]
        self.assertEqual(calls, expected)
        self.assertEqual(len({r["site_index"] for r in run["rows"]}), len(run["rows"]))

    def test_candidate_only_never_reviews_outside(self):
        cfg, lot, view, model, fp = _setup()
        for pol in POLICIES:
            run = _run(cfg, view, lot["oracle"], model, fp, pol, budget=400)
            self.assertTrue(all(r["original_candidate"] for r in run["rows"]), pol)
            self.assertTrue(all(r["cost"]["outside_rescan"] == 0 for r in run["rows"]))

    def test_rescan_outside_costs_extra_and_falsify_schedules_it(self):
        cfg, lot, view, model, fp = _setup()
        run = _run(cfg, view, lot["oracle"], model, fp, "falsify", "with_rescan", budget=400)
        tenth = [r for r in run["rows"] if r["step"] == 10]
        self.assertTrue(tenth)
        # later slots may fall back when no outside site is affordable
        for r in tenth + [r for r in run["rows"] if r["reason"] == "scheduled_outside_rescan"]:
            self.assertEqual(r["reason"], "scheduled_outside_rescan")
            self.assertFalse(r["original_candidate"])
            self.assertEqual(r["cost"]["outside_rescan"], 12.0)


class CostTests(unittest.TestCase):
    def test_first_and_switch_charge_load_and_stage_base_without_movement(self):
        cfg, lot, view, model, fp = _setup()
        state = SelectionState(view, fp, model, fp, "candidate_only", cfg["selection"], 0.5)
        cv = harness.cost_vectors(state, cfg["cost"])
        np.testing.assert_allclose(cv["stage"], 1.0)
        np.testing.assert_allclose(cv["load"], 8.0)
        i = int(np.flatnonzero(view.wafer == 0)[0])
        state.record_visit(i)
        cv = harness.cost_vectors(state, cfg["cost"])
        other = view.wafer != 0
        np.testing.assert_allclose(cv["stage"][other], 1.0)
        np.testing.assert_allclose(cv["load"][other], 8.0)
        same = np.flatnonzero(view.wafer == 0)
        d = np.linalg.norm(view.xy[same] - view.xy[i], axis=1)
        np.testing.assert_allclose(cv["stage"][same], 1.0 + 2.0 * d)
        np.testing.assert_allclose(cv["load"][same], 0.0)
        np.testing.assert_allclose(cv["max"] - cv["first"], 4.0)

    def test_retry_charged_on_failure_or_missing_and_budget_never_exceeded(self):
        cfg, lot, view, model, fp = _setup()
        o = lot["oracle"]
        o["review_status"][:, 0] = np.where(np.arange(view.n) % 2 == 0, "failure", "missing")
        o["review_status"][:, 1] = "ok"
        for b in (20, 37, 90):
            run = _run(cfg, view, o, model, fp, "learned", budget=b)
            self.assertLessEqual(run["spent"], b)
            for r in run["rows"]:
                self.assertEqual(len(r["attempts"]), 2)
                self.assertEqual(r["cost"]["retry"], 4.0)
                self.assertEqual(r["cost"]["dwell"], 8.0)
            self.assertEqual(run["stop_reason"], "none_affordable")

    def test_admission_does_not_peek_at_review_outcome(self):
        cfg, lot, view, model, fp = _setup()
        good, bad = copy.deepcopy(lot["oracle"]), copy.deepcopy(lot["oracle"])
        good["review_status"][:] = "ok"
        bad["review_status"][:, 0] = "failure"
        ra = _run(cfg, view, good, model, fp, "learned", budget=60)
        rb = _run(cfg, view, bad, model, fp, "learned", budget=60)
        # same reservation rule: the first decision is identical; failures only add retry charge
        self.assertEqual(ra["rows"][0]["site_index"], rb["rows"][0]["site_index"])
        self.assertEqual(ra["rows"][0]["reserved_cost"], rb["rows"][0]["reserved_cost"])
        self.assertLessEqual(len(rb["rows"]), len(ra["rows"]))
        self.assertTrue(all(r["label"] is None or r["attempts"][-1]["status"] == "ok" for r in rb["rows"]))

    def test_failed_selection_marked_visited_and_not_a_label(self):
        cfg, lot, view, model, fp = _setup()
        o = lot["oracle"]
        o["review_status"][:] = "failure"
        run = _run(cfg, view, o, model, fp, "falsify", budget=90)
        self.assertTrue(all(r["label"] is None for r in run["rows"]))
        self.assertEqual(len({r["site_index"] for r in run["rows"]}), len(run["rows"]))
        self.assertEqual(run["online_model_hash"], hash_model(model))
        m = harness.evaluate_run(run, o, view.candidate, fp, cfg)
        self.assertEqual(m["true_doi_confirmed"], 0)
        self.assertEqual(m["unresolved_sites"], len(run["rows"]))


class EvaluationTests(unittest.TestCase):
    def test_frozen_counterexamples_fixed_threshold_candidates_only_and_novel_identification(self):
        cfg, lot, view, model, fp = _setup()
        n = view.n
        o = lot["oracle"]
        o["review_status"][:] = "ok"
        cand = view.candidate
        inside, outside = int(np.flatnonzero(cand)[0]), int(np.flatnonzero(~cand)[0])
        third = int(np.flatnonzero(cand)[1])
        fp2 = np.full(n, 0.9)
        fp2[inside], fp2[outside], fp2[third] = 0.05, 0.05, 0.3
        for i in (inside, outside, third):
            o["doi"][i] = True
            o["review_positive"][i] = True
        o["kind"][inside], o["review_kind"][inside] = "novel", "particle"  # misreported kind
        o["kind"][third], o["review_kind"][third] = "novel", "novel"
        rows = [{"site_index": i, "reported_positive": True, "label": True, "reason": "x",
                 "frozen_confident_negative": fp2[i] < 0.1, "cumulative_spend": float(k + 1) * 10,
                 "cost": {k2: 0.0 for k2 in ("load", "stage", "dwell", "outside_rescan", "retry")},
                 "attempts": [review_observation(o, i, 0) | {"attempt": 0}]}
                for k, i in enumerate((inside, outside, third))]
        run = {"rows": rows, "spent": 30.0, "remaining": 0.0, "budget": 30.0, "stop_reason": "x", "timing": {}}
        m = harness.evaluate_run(run, o, cand, fp2, cfg)
        self.assertEqual(m["true_doi_confirmed"], 3)
        self.assertEqual(m["discovered_frozen_false_negatives"], 2)
        self.assertEqual(m["discovered_frozen_confident_false_negatives"], 1)
        self.assertEqual(m["confirmed_true_doi_outside_candidates"], 1)
        self.assertEqual(m["true_novel_doi_first_encounter_cost"], 10.0)
        self.assertEqual(m["first_novel_identification_cost"], 30.0)
        self.assertEqual(cfg["model"]["classification_threshold"], 0.5)

    def test_ledger_has_no_hidden_truth_fields(self):
        cfg, lot, view, model, fp = _setup()
        run = _run(cfg, view, lot["oracle"], model, fp, "falsify", "with_rescan", budget=400)
        text = json.dumps(run["rows"])
        for k in ('"doi"', '"kind"', '"electrical_effect"', '"physical"', '"scenario"', '"seed"'):
            self.assertNotIn(k, text)
        audits = [r for r in run["rows"] if r["reason"] == "scheduled_frozen_negative_audit"]
        self.assertTrue(all(r["baseline_p"] < 0.5 for r in audits))
        labeled = set()
        for r in run["rows"]:
            for ev in r["evidence_refs"]:
                self.assertIn(ev["site_id"], labeled)
            if r["label"] is not None:
                labeled.add(r["site_id"])


def _metric_stub(v):
    return {**{k: v for k in reporting.SCHEDULER_METRICS}, "true_confirmation_cumulative_spend": [10.0] * v,
            "candidate_capture": None, "allsite_recall": 0.5, "candidate_detection_ceiling": 0.8,
            "cost_totals": {k: 1.0 for k in ("load", "stage", "dwell", "outside_rescan", "retry")}}


class StatsTests(unittest.TestCase):
    def _runs(self, a_vals, b_vals, spends=None):
        runs = []
        for k, (a, b) in enumerate(zip(a_vals, b_vals)):
            for pol, v in (("falsify", a), ("learned", b)):
                sp = (spends or {}).get((pol, k), [])
                runs.append({"lot_id": f"L{k}", "scenario": "s" + str(k % 2), "policy": pol, "mode": "candidate_only",
                             "budget": 40, "status": "complete",
                             "metrics": {"true_doi_confirmed": v, "true_confirmation_cumulative_spend": sp}})
        return runs

    def test_null_relative_gain_when_baseline_zero(self):
        out = reporting.paired_comparison(self._runs([1, 2, 0, 1], [0, 0, 0, 0]), "falsify", "learned",
                                          "candidate_only", 40, "true_doi_confirmed", 7, 200, target=0.2, primary=True)
        self.assertIsNone(out["relative_gain"])
        self.assertFalse(out["success"])

    def test_paired_bootstrap_lot_unit_seeded(self):
        runs = self._runs([3, 4, 5, 6], [2, 2, 2, 2])
        a = reporting.paired_comparison(runs, "falsify", "learned", "candidate_only", 40, "true_doi_confirmed", 7, 200,
                                        target=0.2, primary=True)
        b = reporting.paired_comparison(runs, "falsify", "learned", "candidate_only", 40, "true_doi_confirmed", 7, 200,
                                        target=0.2, primary=True)
        self.assertEqual(a, b)
        self.assertEqual(a["complete_pairs"], 4)
        self.assertAlmostEqual(a["mean_difference"], 2.5)
        self.assertTrue(a["success"])
        self.assertEqual(reporting.stratified_paired_bootstrap([2.0] * 4, ["a", "a", "b", "b"], 1, 50), [2.0, 2.0])

    def _full_primary(self, drop_lot=False):
        cfg = mock_config()
        runs = []
        for sc, seeds in cfg["splits"]["test_seeds_by_scenario"].items():
            for seed in seeds:
                if drop_lot and seed == seeds[-1] and sc == "novel_cluster":
                    continue
                for p in cfg["policies"]:
                    for m in cfg["modes"]:
                        for b in cfg["budgets"]:
                            v = 5 if p == "falsify" else 1
                            runs.append({"lot_id": f"{sc}{seed}", "scenario": sc, "seed": seed, "policy": p, "mode": m,
                                         "budget": b, "status": "complete",
                                         "metrics": _metric_stub(v + seed % 2)})
        return cfg, {"kind": "primary_campaign", "runs": runs}

    def test_primary_success_requires_complete_primary_campaign(self):
        cfg, metrics = self._full_primary()
        ok = reporting.build_report(metrics, cfg, {"state": "completed"}, None, None)
        self.assertTrue(ok["completeness"]["eligible"])
        self.assertTrue(ok["primary"]["success"])
        cfg, missing = self._full_primary(drop_lot=True)
        rep = reporting.build_report(missing, cfg, {"state": "completed"}, None, None)
        self.assertFalse(rep["primary"]["success"])
        self.assertEqual(rep["primary"]["conclusion"], "inconclusive")
        self.assertIn("expected runs missing", rep["primary"]["reason"])
        self.assertIsNotNone(rep["primary"]["mean_difference"])  # descriptive summary kept
        failed = reporting.build_report(metrics, cfg, {"state": "failed"}, None, None)
        self.assertFalse(failed["primary"]["success"])
        dev = reporting.build_report({**metrics, "kind": "development"}, cfg, {"state": "completed"}, None, None)
        self.assertFalse(dev["primary"]["success"])

    def test_matched_cost_savings_common_lots_and_attainment(self):
        spends = {("falsify", 0): [10, 20], ("learned", 0): [20, 40], ("falsify", 1): [5, 9], ("learned", 1): [7]}
        out = reporting.matched_cost_savings(self._runs([2, 2], [2, 1], spends), "falsify", "learned",
                                             "candidate_only", 40, 2, 0.3)
        self.assertEqual((out["both_attained"], out["only_policy_attained"]), (1, 1))
        self.assertAlmostEqual(out["saving_of_means_common"], 0.5)
        none = reporting.matched_cost_savings(self._runs([0], [0]), "falsify", "learned", "candidate_only", 40, 2, 0.3)
        self.assertIsNone(none["saving_of_means_common"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        for f in cli.SOURCE_FILES:
            p = self.repo / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {f}\n")
        self.root = self.tmp / "out"
        self.kw = {"data_api": DATA, "model_api": MODEL, "repo": self.repo}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepare_campaign_report_and_artifact_separation(self):
        res = cli.prepare(self.root, **self.kw)
        self.assertEqual(res["lots"], 3)
        self.assertFalse((self.root / "lots" / "test").exists())
        fr = json.loads((self.root / "freeze.json").read_text())
        with self.assertRaises(cli.HarnessError):
            cli.prepare(self.root, **self.kw)
        out = cli.campaign(self.root, _plan=[("stationary", 10), ("novel_cluster", 20)], **self.kw)
        self.assertEqual(out["runs"], 2 * 8 * 2 * 2)
        st = json.loads((self.root / "campaign" / "status.json").read_text())
        self.assertEqual(st["state"], "completed")
        lot = next((self.root / "lots" / "test").iterdir())
        meta = json.loads((lot / "metadata.json").read_text())
        self.assertGreater(meta["generated_at"], fr["frozen_at"])
        with np.load(lot / "public.npz", allow_pickle=False) as z:
            self.assertTrue({"seed", "scenario", "doi", "kind"}.isdisjoint(z.files))
        with np.load(lot / "oracle.npz", allow_pickle=False) as z:
            self.assertIn("doi", z.files)
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.root, _plan=[("stationary", 11)], **self.kw)
        rep = cli.report(self.root, **self.kw)
        self.assertTrue(Path(rep["markdown"]).exists())
        stored = json.loads(Path(rep["report"]).read_text())
        self.assertEqual(stored["primary"]["complete_pairs"], 2)
        self.assertEqual(stored["kind"], "development")
        # development runs never yield a primary success, even with favorable numbers
        self.assertFalse(stored["primary"]["success"])
        self.assertEqual(stored["primary"]["conclusion"], "inconclusive")
        self.assertIn("not primary_campaign", stored["primary"]["reason"])

    def test_report_refuses_tampered_ledger_metadata_or_validation(self):
        cli.prepare(self.root, **self.kw)
        cli.campaign(self.root, _plan=[("stationary", 10)], **self.kw)
        ledger = next((self.root / "campaign" / "ledgers").rglob("*.jsonl"))
        original = ledger.read_bytes()
        ledger.write_bytes(original + b"\n")
        with self.assertRaises(cli.HarnessError) as cm:
            cli.report(self.root, **self.kw)
        self.assertIn("ledger changed", str(cm.exception))
        ledger.write_bytes(original)
        cli.report(self.root, **self.kw)
        meta = next((self.root / "lots" / "train").iterdir()) / "metadata.json"
        meta.write_text(meta.read_text().replace('"train"', '"validation"'))
        with self.assertRaises(cli.HarnessError) as cm:
            cli.report(self.root, **self.kw)
        self.assertIn("stored lot changed", str(cm.exception))

    def test_model_validation_tamper_rejected(self):
        cli.prepare(self.root, **self.kw)
        (self.root / "model_validation.json").write_text("{}")
        with self.assertRaises(cli.HarnessError) as cm:
            cli.campaign(self.root, _plan=[("stationary", 10)], **self.kw)
        self.assertIn("model validation", str(cm.exception))

    def test_freeze_tamper_rejected(self):
        cli.prepare(self.root, **self.kw)
        (self.repo / "inspection_review/policies.py").write_text("# changed\n")
        with self.assertRaises(cli.HarnessError) as cm:
            cli.campaign(self.root, _plan=[("stationary", 10)], **self.kw)
        self.assertIn("source files changed", str(cm.exception))
        self.assertFalse((self.root / "campaign").exists())

    def test_model_tamper_rejected(self):
        cli.prepare(self.root, **self.kw)
        m = json.loads((self.root / "model.json").read_text())
        m["b"] = 3.0
        (self.root / "model.json").write_text(json.dumps(m))
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.root, _plan=[("stationary", 10)], **self.kw)

    def test_failed_campaign_keeps_artifacts_and_refuses_resume(self):
        cli.prepare(self.root, **self.kw)
        boom = SimpleNamespace(**{**vars(DATA), "review_observation": lambda o, i, a: 1 / 0})
        with self.assertRaises(ZeroDivisionError):
            cli.campaign(self.root, _plan=[("stationary", 10)], **{**self.kw, "data_api": boom})
        st = json.loads((self.root / "campaign" / "status.json").read_text())
        self.assertEqual(st["state"], "failed")
        with self.assertRaises(cli.HarnessError):
            cli.campaign(self.root, _plan=[("stationary", 10)], **self.kw)

    def test_reproduce_is_development_on_validation(self):
        cli.prepare(self.root, **self.kw)
        out = cli.reproduce(self.root, **self.kw)
        self.assertIn("development", out["label"])
        self.assertFalse((self.root / "campaign").exists())
        self.assertFalse((self.root / "lots" / "test").exists())

    def test_main_prints_json_and_nonzero_on_failure(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["report", "--root", str(self.tmp / "missing")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
