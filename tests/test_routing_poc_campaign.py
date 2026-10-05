"""Experiment-scope checks for the routing PoC: fixtures, metrics, campaign, CLI.

Campaign mechanics run against small test doubles of the adapter/policy modules (defined
here, labeled as doubles) so they do not depend on sibling scopes. Integration with the
real ``routing_poc.contracts``/``replay``/``policies`` runs only when those modules are
importable (in-tree or from sibling worktrees) and is skipped otherwise. The reserved
synthetic test split is never executed here; only its refusal guards are checked.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import routing_poc  # noqa: E402

# Sibling worktrees (read-only) may provide the adapter/policy modules before integration.
for _sib in ("adapter", "policies"):
    _p = ROOT.parent / f"wafflebench-routing-{_sib}-20261005" / "routing_poc"
    if _p.is_dir() and str(_p) not in list(routing_poc.__path__):
        with contextlib.suppress(AttributeError):
            routing_poc.__path__.append(str(_p))

from routing_poc import campaign, cli, fixtures, metrics  # noqa: E402


def _real_modules():
    try:
        import routing_poc.contracts as c
        import routing_poc.policies as p
        import routing_poc.replay as r
    except ImportError:
        return None
    return c, r, p


REAL = _real_modules()
CANDIDATE_KEYS = {"site_id", "wafer_id", "x_um", "y_um", "prior_p", "optical_observed_at", "recipe_id",
                  "capture_bound_s", "inference_bound_s"}


# ---------------------------------------------------------------- test doubles (not production)
def _double_cost(c, current, cost):
    load = 0.0 if current and current["wafer_id"] == c["wafer_id"] else cost["wafer_load_s"]
    move = (math.hypot(c["x_um"] - current["x_um"], c["y_um"] - current["y_um"]) / cost["move_um_per_s"]
            if current and current["wafer_id"] == c["wafer_id"] else 0.0)
    first = load + move + cost["settle_s"] + c["capture_bound_s"] + c["inference_bound_s"]
    return {"load_s": load, "move_s": move, "settle_s": cost["settle_s"], "first_s": first,
            "reserved_s": first + cost["retry_limit"] * (c["capture_bound_s"] + c["inference_bound_s"])}


def _double_run_loop(job, archive, selector, budget_s, seed=0, observer=None):
    import random
    rng = random.Random(seed)
    remaining, current, rows, history, spent = float(budget_s), None, [], [], 0.0
    left = {c["site_id"]: c for c in job["candidates"]}
    stop = "no_affordable_choice"
    while left:
        cands = [{**c, "cost": _double_cost(c, current, job["cost"])} for c in left.values()]
        choice = selector(copy.deepcopy({"job_id": job["job_id"], "candidates": cands, "remaining_s": remaining,
                                         "current": current, "history": history}), rng)
        if choice is None:
            break
        c = next(x for x in cands if x["site_id"] == choice["site_id"])
        if c["cost"]["reserved_s"] > remaining:
            stop = "inadmissible"
            break
        atts = archive["observations"].get(c["site_id"], [])[:1 + job["cost"]["retry_limit"]]
        charged = c["cost"]["load_s"] + c["cost"]["move_s"] + c["cost"]["settle_s"]
        used, final = [], None
        for a in atts:
            charged += a["capture_s"] + a["inference_s"]
            used.append(a)
            if a["status"] == "ok":
                final = a["reported_doi"]
                break
        if not atts:
            charged += c["capture_bound_s"] + c["inference_bound_s"]
        remaining -= charged
        spent += charged
        current = {"wafer_id": c["wafer_id"], "x_um": c["x_um"], "y_um": c["y_um"]}
        rows.append({"site_id": c["site_id"], "choice": choice, "attempts": used, "reported_doi": final,
                     "charged_s": charged, "reserved_s": c["cost"]["reserved_s"], "cumulative_s": spent,
                     "resource_parts": c["cost"]})
        history.append({"site_id": c["site_id"], "reported_doi": final})
        del left[c["site_id"]]
    return {"job_id": job["job_id"], "data_mode": job["data_mode"], "budget_s": budget_s, "spent_s": spent,
            "remaining_s": remaining, "rows": rows, "decision_wall_s": 0.0, "observer_wall_s": 0.0,
            "stop_reason": stop, "commercial_validated": False}


def _double_select(state, rng, policy="beam_route", audit_epsilon=0.1):
    ok = [c for c in state["candidates"] if c["cost"]["reserved_s"] <= state["remaining_s"]]
    if not ok:
        return None
    key = {"risk_only": lambda c: c["prior_p"],
           "risk_per_second": lambda c: c["prior_p"] / c["cost"]["reserved_s"],
           "beam_route": lambda c: c["prior_p"] / c["cost"]["first_s"]}[policy]
    best = sorted(ok, key=lambda c: (-key(c), c["site_id"]))[0]
    return {"site_id": best["site_id"], "reason": "double", "propensity": 1.0, "audit": False, "components": {}}


def _doubles():
    contracts = types.ModuleType("routing_poc.contracts")
    contracts.__file__ = __file__
    contracts.validate_job = lambda job: copy.deepcopy(job)
    contracts.validate_archive = lambda job, archive: copy.deepcopy(archive)
    replay = types.ModuleType("routing_poc.replay")
    replay.__file__ = __file__
    replay.run_loop = mock.Mock(side_effect=_double_run_loop)
    policies = types.ModuleType("routing_poc.policies")
    policies.__file__ = __file__
    policies.select = _double_select
    return {"routing_poc.contracts": contracts, "routing_poc.replay": replay, "routing_poc.policies": policies}


def _small_protocol(**changes):
    p = copy.deepcopy(campaign.PROTOCOL)
    p.update(development_seeds=[12000, 12001, 12002], budgets_s=[120])
    p.update(changes)
    return p


# ---------------------------------------------------------------- fixtures
class FixtureProvenanceTests(unittest.TestCase):
    def test_shape_whitelist_and_provenance(self):
        for regime in fixtures.REGIMES:
            job, archive = fixtures.make_job(12000, regime)
            self.assertEqual(job["data_mode"], "synthetic")
            self.assertEqual(len(job["candidates"]), 120)
            self.assertEqual(len({c["wafer_id"] for c in job["candidates"]}), 3)
            self.assertTrue(job["provenance"]["synthetic"])
            self.assertFalse(job["provenance"]["commercial_validated"])
            self.assertNotIn("seed", json.dumps(job["provenance"]))
            self.assertNotIn("12000", job["job_id"])
            for c in job["candidates"]:
                self.assertEqual(set(c), CANDIDATE_KEYS)
                self.assertTrue(0.0 <= c["prior_p"] <= 1.0)
            text = json.dumps(job)
            for hidden in ("doi", "reference", "reported", "image", "observations"):
                self.assertNotIn(hidden, text)
            ref = archive["reference"]
            self.assertTrue(ref["complete"])
            self.assertEqual(set(ref["doi_by_site"]), {c["site_id"] for c in job["candidates"]})
            self.assertTrue(all(isinstance(v, bool) for v in ref["doi_by_site"].values()))

    def test_deterministic_and_regimes_differ(self):
        self.assertEqual(fixtures.make_job(12003, "clustered"), fixtures.make_job(12003, "clustered"))
        a, _ = fixtures.make_job(12003, "clustered")
        b, _ = fixtures.make_job(12003, "diffuse")
        self.assertNotEqual(a["job_id"], b["job_id"])
        self.assertGreater(a["cost"]["wafer_load_s"], b["cost"]["wafer_load_s"])
        self.assertLess(a["cost"]["move_um_per_s"], b["cost"]["move_um_per_s"])
        with self.assertRaises(ValueError):
            fixtures.make_job(1, "custom")

    def test_times_bounds_and_imperfect_attempts(self):
        statuses = set()
        for seed in campaign.PROTOCOL["development_seeds"]:
            job, archive = fixtures.make_job(seed, "clustered")
            cutoff = job["cutoff_utc"]
            bounds = {c["site_id"]: c for c in job["candidates"]}
            for c in job["candidates"]:
                self.assertLessEqual(c["optical_observed_at"], cutoff)
            for sid, atts in archive["observations"].items():
                self.assertTrue(1 <= len(atts) <= 2)
                if len(atts) == 2:
                    self.assertIn(atts[0]["status"], ("failed", "missing"))
                for a in atts:
                    statuses.add(a["status"])
                    self.assertGreater(a["observed_at"], cutoff)
                    self.assertLessEqual(a["capture_s"], bounds[sid]["capture_bound_s"])
                    self.assertLessEqual(a["inference_s"], bounds[sid]["inference_bound_s"])
                    if a["status"] == "ok":
                        self.assertIsInstance(a["reported_doi"], bool)
                    else:
                        self.assertIsNone(a["reported_doi"])
        self.assertEqual(statuses, {"ok", "failed", "missing"})

    def test_reports_are_imperfect_and_not_the_reference(self):
        disagree = 0
        for seed in campaign.PROTOCOL["development_seeds"]:
            _, archive = fixtures.make_job(seed, "diffuse")
            ref = archive["reference"]["doi_by_site"]
            for sid, atts in archive["observations"].items():
                if atts[-1]["status"] == "ok" and atts[-1]["reported_doi"] != ref[sid]:
                    disagree += 1
        self.assertGreater(disagree, 0)

    @unittest.skipIf(REAL is None, "deferred: routing_poc.contracts not available in this worktree")
    def test_fixtures_pass_adapter_validation(self):
        contracts = REAL[0]
        for regime in fixtures.REGIMES:
            job, archive = fixtures.make_job(12000, regime)
            contracts.validate_archive(contracts.validate_job(job), archive)


# ---------------------------------------------------------------- metrics
def _mini(mode="synthetic", reference=None, observations=None):
    cands = [{"site_id": s, "wafer_id": "w", "x_um": 0.0, "y_um": 0.0, "prior_p": 0.5,
              "optical_observed_at": "2026-10-05T00:00:00+00:00", "recipe_id": "r",
              "capture_bound_s": 1.0, "inference_bound_s": 0.0} for s in ("a", "b", "c", "d", "e")]
    job = {"schema_version": 1, "job_id": "j", "data_mode": mode, "cutoff_utc": "2026-10-05T00:00:00+00:00",
           "cost": {}, "candidates": cands, "provenance": {}}
    archive = {"job_id": "j", "observations": observations if observations is not None else {s: [{}] for s in "abcde"}}
    if reference is not None:
        archive["reference"] = reference
    return job, archive


def _run(rows):
    return {"job_id": "j", "data_mode": "synthetic", "budget_s": 10, "spent_s": 5, "remaining_s": 5,
            "rows": [{"site_id": s, "reported_doi": r, "attempts": [{}]} for s, r in rows],
            "decision_wall_s": 0.01, "observer_wall_s": 0.0, "stop_reason": "x", "commercial_validated": False}


class MetricsTests(unittest.TestCase):
    REF = {"complete": True, "source": "synthetic", "doi_by_site": {"a": True, "b": False, "c": True,
                                                                     "d": True, "e": False}}

    def test_paired_complete_reference_metrics(self):
        job, archive = _mini(reference=self.REF)
        m = metrics.evaluate(job, archive, _run([("a", True), ("b", True), ("c", False), ("d", None)]))
        self.assertEqual(m["confirmed_reference_doi"], 1)
        self.assertEqual(m["false_confirmations"], 1)
        self.assertEqual(m["detector_positive_reports"], 2)
        self.assertEqual(m["total_reference_doi"], 3)
        self.assertEqual(m["escapes"], 2)
        self.assertAlmostEqual(m["candidate_recall"], 1 / 3)
        self.assertEqual(m["escape_breakdown"], {"unselected": 0, "detector_negative": 1, "unknown_final": 1})
        self.assertIsNone(m["wafer_wide_recall"])
        self.assertFalse(m["commercial_validated"])

    def test_unknown_final_is_not_negative(self):
        job, archive = _mini(reference=self.REF)
        m = metrics.evaluate(job, archive, _run([("d", None), ("e", None)]))
        self.assertEqual(m["unknown_final_reports"], 2)
        self.assertEqual(m["detector_negative_reports"], 0)
        self.assertEqual(m["false_confirmations"], 0)

    def test_partial_log_without_reference_is_unidentifiable(self):
        job, archive = _mini(mode="log_replay", observations={"a": [{}], "b": [{}]})
        m = metrics.evaluate(job, archive, _run([("a", True), ("b", False)]))
        for k in ("confirmed_reference_doi", "false_confirmations", "total_reference_doi", "escapes",
                  "candidate_recall"):
            self.assertIsNone(m[k], k)
        self.assertEqual(m["detector_positive_reports"], 1)
        self.assertFalse(m["counterfactual_support"]["available"])
        self.assertIn("unidentifiable", m["counterfactual_support"]["reason"])
        self.assertIn("log replay", m["evidence_label"])

    def test_incomplete_reference_nulls_recall_and_escapes(self):
        ref = {"complete": False, "source": "partial", "doi_by_site": {"a": True, "b": None}}
        job, archive = _mini(mode="log_replay", reference=ref)
        m = metrics.evaluate(job, archive, _run([("a", True)]))
        self.assertEqual(m["confirmed_reference_doi"], 1)
        self.assertIsNone(m["escapes"])
        self.assertIsNone(m["candidate_recall"])
        # A positive report on a site with null reference is never promoted to truth.
        m2 = metrics.evaluate(job, archive, _run([("b", True)]))
        self.assertIsNone(m2["confirmed_reference_doi"])
        self.assertIsNone(m2["false_confirmations"])
        # complete=True with a null entry is still incomplete.
        ref3 = {"complete": True, "source": "x", "doi_by_site": {**self.REF["doi_by_site"], "e": None}}
        job3, archive3 = _mini(reference=ref3)
        self.assertIsNone(metrics.evaluate(job3, archive3, _run([("a", True)]))["candidate_recall"])

    def test_invalid_runs_rejected(self):
        job, archive = _mini(reference=self.REF)
        with self.assertRaises(metrics.MetricsError):
            metrics.evaluate(job, archive, _run([("a", True), ("a", True)]))
        with self.assertRaises(metrics.MetricsError):
            metrics.evaluate(job, archive, _run([("zz", True)]))
        with self.assertRaises(metrics.MetricsError):
            metrics.evaluate(job, archive, _run([("a", "yes")]))
        with self.assertRaises(metrics.MetricsError):
            metrics.evaluate(job, archive, {**_run([]), "job_id": "other"})


# ---------------------------------------------------------------- bootstrap
class LotBootstrapTests(unittest.TestCase):
    def test_constant_and_reproducible(self):
        r = campaign.paired_lot_bootstrap({"l1": 2.0, "l2": 2.0, "l3": 2.0}, 2000, 2026100511, "k")
        self.assertEqual(r["ci95"], [2.0, 2.0])
        self.assertTrue(r["ci_excludes_zero"])
        self.assertEqual(r["resampling_unit"], "lot")
        d = {f"l{i}": float(i % 3 - 1) for i in range(8)}
        self.assertEqual(campaign.paired_lot_bootstrap(d, 2000, 2026100511, "k"),
                         campaign.paired_lot_bootstrap(dict(reversed(list(d.items()))), 2000, 2026100511, "k"))
        r = campaign.paired_lot_bootstrap(d, 2000, 2026100511, "k")
        self.assertEqual(r["n_lots"], 8)
        self.assertLessEqual(r["ci95"][0], r["mean_difference"])
        self.assertGreaterEqual(r["ci95"][1], r["mean_difference"])

    def test_degenerate(self):
        self.assertIsNone(campaign.paired_lot_bootstrap({}, 2000, 1)["mean_difference"])
        one = campaign.paired_lot_bootstrap({"l": 1.0}, 2000, 1)
        self.assertEqual(one["mean_difference"], 1.0)
        self.assertIsNone(one["ci95"])


# ---------------------------------------------------------------- campaign
class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "camp"
        self.mods = _doubles()
        patcher = mock.patch.dict(sys.modules, self.mods)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _load(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_development_run_artifacts_and_pairing(self):
        res = campaign.run_campaign(self.out, protocol=_small_protocol())
        self.assertEqual(res["status"], "complete")
        self.assertFalse(res["commercial_validated"])
        self.assertEqual(res["runs"], 3 * 2 * 1 * 3)
        status, proto = self._load("status.json"), self._load("protocol.json")
        self.assertEqual(status["status"], "complete")
        self.assertEqual(proto["protocol_sha256"], campaign.canonical_hash(proto["protocol"]))
        self.assertTrue(self._load("source_verification.json")["matches"])
        self.assertEqual(len(list((self.out / "jobs").glob("*.json"))), 6)
        self.assertEqual(len(list((self.out / "runs").glob("*.json"))), 18)
        # paired: one archive per lot, every policy replayed with that same archive object content
        calls = self.mods["routing_poc.replay"].run_loop.call_args_list
        by_job = {}
        for c in calls:
            job, archive = c.args[0], c.args[1]
            by_job.setdefault(job["job_id"], []).append(json.dumps(archive, sort_keys=True))
            self.assertIn(c.kwargs["seed"], {campaign.policy_seed(job["job_id"], p)
                                             for p in campaign.PROTOCOL["policies"]})
        for archives in by_job.values():
            self.assertEqual(len(archives), 3)
            self.assertEqual(len(set(archives)), 1)
        summary = self._load("summary.json")
        self.assertFalse(summary["commercial_validated"])
        self.assertEqual(summary["evidence_mode"], "synthetic")
        self.assertIn("never wafer-wide", summary["recall_scope"])
        cell = summary["cells"][0]
        comp = [p for p in cell["paired"] if p["metric"] == "confirmed_reference_doi"][0]
        self.assertEqual(comp["n_lots"], 3)
        self.assertEqual(comp["resampling_unit"], "lot")
        self.assertIn("decision_wall_s_total", cell["policies"]["beam_route"])
        self.assertTrue(any("No commercial ROI" in x for x in summary["limitations"]))
        keys = json.dumps(summary["cells"]).lower()
        for claim in ("superior", "success", "winner"):
            self.assertNotIn(claim, keys)

    def test_policy_rng_independent_of_latent_seed(self):
        campaign.run_campaign(self.out, protocol=_small_protocol(development_seeds=[12004]))
        seeds = {c.kwargs["seed"] for c in self.mods["routing_poc.replay"].run_loop.call_args_list}
        self.assertNotIn(12004, seeds)
        job_ids = {fixtures.job_id_for(12004, r) for r in fixtures.REGIMES}
        expected = {campaign.policy_seed(j, p) for j in job_ids for p in campaign.PROTOCOL["policies"]}
        self.assertEqual(seeds, expected)

    def test_refuses_existing_output(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("x")
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol())
        self.assertEqual([p.name for p in self.out.iterdir()], ["keep.txt"])

    def test_development_test_separation(self):
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol(development_seeds=[12000, 22005]))
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol(test_seeds=[1, 2]))
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, split="test")
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, split="test", protocol=_small_protocol(), confirm_synthetic_test=True)
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol(), confirm_synthetic_test=True)
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol(commercial_validated=True))
        self.assertFalse(self.out.exists())
        self.mods["routing_poc.replay"].run_loop.assert_not_called()
        self.assertEqual(campaign.PROTOCOL["test_seeds"], list(range(22000, 22040)))
        self.assertEqual(campaign.PROTOCOL["development_seeds"], list(range(12000, 12008)))
        self.assertEqual(campaign.PROTOCOL["budgets_s"], [120, 240])
        self.assertEqual(campaign.PROTOCOL["bootstrap"]["replicates"], 2000)
        self.assertEqual(campaign.PROTOCOL["bootstrap"]["seed"], 2026100511)

    def test_source_drift_fails_and_preserves_artifacts(self):
        real = campaign._source_digests
        first = real()
        drifted = {**first, "routing_poc.policies": {"status": "hashed", "path": "x", "sha256": "0" * 64}}
        with mock.patch.object(campaign, "_source_digests", side_effect=[first, drifted]):
            with self.assertRaises(campaign.CampaignError):
                campaign.run_campaign(self.out, protocol=_small_protocol(development_seeds=[12000]))
        self.assertEqual(self._load("status.json")["status"], "failed")
        self.assertEqual(self._load("failure.json")["phase"], "verify_sources")
        self.assertEqual(self._load("source_verification.json")["drift"], ["routing_poc.policies"])
        self.assertEqual(len(list((self.out / "runs").glob("*.json"))), 6)
        self.assertFalse((self.out / "summary.json").exists())

    def test_failure_mid_run_preserves_partial_artifacts(self):
        rl = self.mods["routing_poc.replay"].run_loop
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("boom")
            return _double_run_loop(*a, **k)
        rl.side_effect = flaky
        with self.assertRaises(RuntimeError):
            campaign.run_campaign(self.out, protocol=_small_protocol())
        status = self._load("status.json")
        self.assertEqual((status["status"], status["phase"]), ("failed", "execute"))
        self.assertIn("boom", self._load("failure.json")["traceback"])
        self.assertEqual(len(list((self.out / "runs").glob("*.json"))), 2)
        self.assertTrue((self.out / "protocol.json").exists())
        self.assertTrue(list((self.out / "jobs").glob("*.json")))

    def test_rejects_run_claiming_commercial_validation(self):
        rl = self.mods["routing_poc.replay"].run_loop
        rl.side_effect = lambda *a, **k: {**_double_run_loop(*a, **k), "commercial_validated": True}
        with self.assertRaises(campaign.CampaignError):
            campaign.run_campaign(self.out, protocol=_small_protocol(development_seeds=[12000]))
        self.assertEqual(self._load("status.json")["status"], "failed")


# ---------------------------------------------------------------- CLI
class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        patcher = mock.patch.dict(sys.modules, _doubles())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, json.loads(buf.getvalue())

    def test_benchmark_requires_confirmation(self):
        code, out = self._cli("benchmark", "--out", str(self.d / "b"))
        self.assertEqual(code, 1)
        self.assertIn("--confirm-synthetic-test", out["error"])
        self.assertFalse((self.d / "b").exists())

    def test_replay_partial_log_labels_and_unknowns(self):
        job, archive = fixtures.make_job(12000, "clustered")
        job["data_mode"] = "log_replay"
        keep = [c["site_id"] for c in job["candidates"][:10]]
        archive = {"job_id": job["job_id"], "observations": {k: archive["observations"][k] for k in keep}}
        (self.d / "job.json").write_text(json.dumps(job))
        (self.d / "arch.json").write_text(json.dumps(archive))
        code, out = self._cli("replay", "--job", str(self.d / "job.json"), "--archive", str(self.d / "arch.json"),
                              "--out", str(self.d / "r"), "--budget-s", "120", "--policy", "risk_only")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["evidence_mode"], "log_replay")
        m = out["metrics"]
        self.assertIsNone(m["candidate_recall"])
        self.assertIsNone(m["escapes"])
        self.assertFalse(m["counterfactual_support"]["available"])
        self.assertFalse(out["commercial_validated"])
        self.assertTrue((self.d / "r" / "metrics.json").exists())
        code, out = self._cli("replay", "--job", str(self.d / "job.json"), "--archive", str(self.d / "arch.json"),
                              "--out", str(self.d / "r"), "--budget-s", "120", "--policy", "risk_only")
        self.assertEqual(code, 1)


# ---------------------------------------------------------------- integration with real sibling modules
@unittest.skipIf(REAL is None, "deferred: real routing_poc.contracts/replay/policies not importable")
class RealIntegrationTests(unittest.TestCase):
    def test_small_development_campaign_with_real_modules(self):
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "c"
            res = campaign.run_campaign(out, protocol=_small_protocol(development_seeds=[12000, 12001]))
            self.assertEqual(res["status"], "complete")
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["commercial_validated"])
            for f in (out / "runs").glob("*.json"):
                rec = json.loads(f.read_text(encoding="utf-8"))
                self.assertLessEqual(rec["run"]["spent_s"], rec["budget_s"] + 1e-9)
                self.assertFalse(rec["metrics"]["commercial_validated"])


if __name__ == "__main__":
    unittest.main()
