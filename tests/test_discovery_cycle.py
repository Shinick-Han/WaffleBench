"""Posthoc discovery cycle: plan/state rules, input tamper, hash chain, no future leak, cost math, roles.

Every call is a direct fixture call (actors ``fixture-direct:<role>``) on a fresh temporary root copied
from the frozen ``evidence/inspection-improvements-v3`` diagnostics; none of it is Omnigent orchestration.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from discovery_cycle import core
from discovery_cycle.core import CycleError, DiscoveryCycle, prepare

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "evidence" / "inspection-improvements-v3"
A, E, S = "fixture-direct:analyst", "fixture-direct:experimenter", "fixture-direct:supervisor"
H = "The visit count, not ranking, binds most missed DOI in high ceiling scenarios."
L = "Whether unselected admitted DOI dominate unadmitted DOI per lot."
R = "It separates optical admission from budget limits before looking at calibration."


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class CycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="discovery-cycle-"))
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_rm_readonly)
        self.root = self.tmp / "cycle"
        self.prep = prepare(self.root, EVIDENCE)
        self.c = DiscoveryCycle(self.root)

    def events(self) -> list[bytes]:
        return (self.root / "events.jsonl").read_bytes().splitlines()

    def plan(self, test_id="miss_partition", cands=("miss_partition", "capacity_bound"), ev=()):
        return self.c.record_plan(list(cands), test_id, H, L, R, list(ev), A)["plan"]

    def full_cycle(self):
        p1 = self.plan()
        r1 = self.c.run_experiment(p1["plan_id"], E)["result"]
        self.c.record_update(r1["result_id"], "Most missed DOI were admitted but unselected in high ceilings.",
                             "Calibration overconfidence explains shifted scenario ranking errors.",
                             "Run calibration_shift over the same frozen bins.", A)
        p2 = self.plan("calibration_shift", ("calibration_shift", "capacity_bound"), [r1["result_id"]])
        r2 = self.c.run_experiment(p2["plan_id"], E)["result"]
        self.c.record_update(r2["result_id"], "Low contrast shows the largest weighted overconfidence gap.",
                             "Recalibrating per scenario might reduce shifted ranking errors.",
                             "Propose a fresh preregistered held-out recalibration study (not executed).", A)
        return r1, r2

    # ------------------------------------------------ prepare / input
    def test_prepare_copies_and_hashes_input_and_refuses_existing_root(self):
        import hashlib
        src = (EVIDENCE / "quality-diagnostics.json").read_bytes()
        self.assertEqual(self.prep["input_sha256"], hashlib.sha256(src).hexdigest())
        self.assertEqual((self.root / "inputs" / "quality-diagnostics.json").read_bytes(), src)
        with self.assertRaises(CycleError) as cm:
            prepare(self.root, EVIDENCE)
        self.assertEqual(cm.exception.code, "conflict")

    def test_input_tamper_is_refused_on_every_call(self):
        dst = self.root / "inputs" / "quality-diagnostics.json"
        os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
        dst.write_bytes(dst.read_bytes().replace(b'"budget_cu": 360', b'"budget_cu": 720'))
        for call in (lambda: self.c.read_context(A), self.c.status, lambda: self.plan()):
            with self.assertRaises(CycleError) as cm:
                call()
            self.assertEqual(cm.exception.code, "tamper")

    # ------------------------------------------------ hash chain
    def test_hash_chain_rewrite_and_truncation_are_detected(self):
        self.plan()
        lines = self.events()
        ev = json.loads(lines[1])
        ev["data"]["hypothesis"] = "A quietly rewritten hypothesis about something else entirely."
        (self.root / "events.jsonl").write_bytes(b"\n".join([lines[0], json.dumps(ev).encode()]) + b"\n")
        with self.assertRaises(CycleError) as cm:
            self.c.status()
        self.assertEqual(cm.exception.code, "tamper")
        (self.root / "events.jsonl").write_bytes(lines[0] + b"\n")  # valid prefix, but head says 2
        with self.assertRaises(CycleError) as cm:
            self.c.read_context(S)
        self.assertEqual(cm.exception.code, "tamper")

    # ------------------------------------------------ context / leak
    def test_context_has_primary_and_catalog_but_no_oracle_values_before_run(self):
        ctx = self.c.read_context(A)
        self.assertEqual((ctx["primary"]["candidate_mean"], ctx["primary"]["baseline_mean"]), (29.33, 27.38))
        self.assertEqual(ctx["primary"]["ci95"], [1.46, 2.43])
        self.assertEqual(ctx["budget"], {"limit": 2, "used": 0, "remaining": 2})
        self.assertEqual([t["test_id"] for t in ctx["catalog"]], ["miss_partition", "capacity_bound", "calibration_shift"])
        text = json.dumps(ctx)
        for leak in ("272.6", "77.83", "162.48", "never_optically_admitted_doi", "latent_doi_rate", "34.65", "0.2946"):
            self.assertNotIn(leak, text)
        before = self.events()
        self.assertEqual(self.c.read_context(E), ctx | {})  # idempotent read
        self.assertEqual(self.events(), before)
        p = self.plan()
        self.assertNotIn("77.83", json.dumps(self.c.read_context(A)))
        r = self.c.run_experiment(p["plan_id"], E)["result"]
        self.assertIn(r["result_id"], json.dumps(self.c.read_context(A)["completed_results"]))

    # ------------------------------------------------ computations
    def test_miss_partition_conserves_counts(self):
        r = self.c.run_experiment(self.plan()["plan_id"], E)["result"]
        o = r["values"]["overall"]
        self.assertEqual(o["counts"]["latent_doi"], 27260)
        self.assertEqual(o["counts"]["never_optically_admitted_doi"] + o["counts"]["missed_candidate_doi"]
                         + o["counts"]["selected_latent_doi"], 27260)
        self.assertEqual(o["counts"]["tp_reported_positive"] + o["counts"]["sensor_missed_doi"]
                         + o["counts"]["unresolved_doi"], o["counts"]["selected_latent_doi"])
        self.assertAlmostEqual(r["values"]["per_scenario"]["low_contrast"]["optical_recall_ceiling"], 0.2988, 3)
        self.assertTrue(r["privileged_posthoc_oracle"])
        self.assertFalse(r["production_selection"])
        self.assertGreaterEqual(r["elapsed_s"], 0)
        self.assertNotIn("accuracy", json.dumps(r["values"]["labels"]))

    def test_capacity_bound_cost_math(self):
        p = self.plan("capacity_bound")
        v = self.c.run_experiment(p["plan_id"], E)["result"]["values"]
        self.assertEqual(v["optimistic_review_bound"], 38)  # 17 + 9*37 + 4 = 360
        self.assertEqual(v["charged_at_bound_cu"], 350)
        self.assertEqual(17 + 9 * 38 + 4 > 360, True)
        self.assertAlmostEqual(v["current_review_fraction_of_bound"], 34.65 / 38)
        self.assertIn("not achievable gain", v["labels"]["visit_slack_vs_bound"])

    def test_calibration_shift_weighted_gaps(self):
        p = self.plan("calibration_shift", ("calibration_shift", "miss_partition"))
        v = self.c.run_experiment(p["plan_id"], E)["result"]["values"]
        doc = json.loads((EVIDENCE / "quality-diagnostics.json").read_text())
        bins = doc["per_scenario"]["low_contrast"]["calibration_candidates"]["bins"]
        n = sum(b["n"] for b in bins)
        want = sum(b["n"] * abs(b["mean_p"] - b["latent_doi"] / b["n"]) for b in bins) / n
        self.assertAlmostEqual(v["per_scenario"]["low_contrast"]["weighted_abs_gap"], want, 12)
        self.assertGreater(v["per_scenario"]["low_contrast"]["weighted_signed_gap_p_ge_0.2"],
                           v["per_scenario"]["stationary"]["weighted_signed_gap_p_ge_0.2"])

    # ------------------------------------------------ plan / state rules
    def test_plan_rules_and_refusals_do_not_append(self):
        before = self.events()
        bad = [
            (dict(cands=("miss_partition",)), "invalid"),
            (dict(cands=("miss_partition", "miss_partition")), "invalid"),
            (dict(test_id="calibration_shift"), "invalid"),
            (dict(test_id="train_new_model", cands=("miss_partition", "train_new_model")), "approval_required"),
            (dict(ev=("res_0000000000000000",)), "invalid"),
        ]
        for kw, code in bad:
            with self.assertRaises(CycleError) as cm:
                self.plan(**kw)
            self.assertEqual(cm.exception.code, code, kw)
        with self.assertRaises(CycleError) as cm:
            self.c.record_plan(["miss_partition", "capacity_bound"], "miss_partition", "  ", L, R, [], A)
        self.assertEqual(cm.exception.code, "invalid")
        with self.assertRaises(CycleError):
            self.c.record_plan(["miss_partition", "capacity_bound"], "miss_partition", "x1 2 3", L, R, [], A)
        self.assertEqual(self.events(), before)
        p = self.plan()
        with self.assertRaises(CycleError) as cm:
            self.plan()  # one pending plan
        self.assertEqual(cm.exception.code, "conflict")
        r = self.c.run_experiment(p["plan_id"], E)["result"]
        self.assertEqual(self.c.run_experiment(p["plan_id"], E), {"result": r, "replayed": True})
        with self.assertRaises(CycleError) as cm:  # update required first
            self.plan("capacity_bound", ("capacity_bound", "calibration_shift"), [r["result_id"]])
        self.assertEqual(cm.exception.code, "conflict")
        self.c.record_update(r["result_id"], H, L, "Run capacity_bound on the same frozen file.", A)
        with self.assertRaises(CycleError):  # already-run test is no longer a candidate
            self.plan("capacity_bound", ("capacity_bound", "miss_partition"), [r["result_id"]])
        with self.assertRaises(CycleError):  # evidence must cite the previous result
            self.plan("capacity_bound", ("capacity_bound", "calibration_shift"), [])

    def test_full_cycle_finalize_and_budget(self):
        with self.assertRaises(CycleError):
            self.c.finalize(S)
        r1, r2 = self.full_cycle()
        self.assertNotEqual(r1["result_id"], r2["result_id"])
        with self.assertRaises(CycleError) as cm:
            self.plan("capacity_bound", ("capacity_bound", "miss_partition"), [r1["result_id"], r2["result_id"]])
        self.assertIn(cm.exception.code, ("budget", "invalid"))
        ctx = self.c.read_context(S)
        self.assertEqual(ctx["budget"]["remaining"], 0)
        self.assertEqual(ctx["completed_results"][1]["update"]["next_experiment"]["status"], "PROPOSED")
        self.assertFalse(ctx["completed_results"][1]["update"]["next_experiment"]["executed"])
        final = self.c.finalize(S)
        self.assertIsNone(final["final"]["conclusion"])
        self.assertEqual(self.c.finalize(S)["replayed"], True)
        self.assertEqual(self.c.status()["phase"], "finalized")
        types = [json.loads(x)["type"] for x in self.events()]
        self.assertEqual(types, ["prepared", "plan_recorded", "experiment_admitted", "experiment_completed",
                                 "update_recorded", "plan_recorded", "experiment_admitted", "experiment_completed",
                                 "update_recorded", "finalized"])

    def test_failed_admission_blocks_without_retry(self):
        p = self.plan()
        with mock.patch.dict(core.COMPUTE, {"miss_partition": mock.Mock(side_effect=ValueError("boom at C:/x/y"))}):
            with self.assertRaises(CycleError) as cm:
                self.c.run_experiment(p["plan_id"], E)
        self.assertEqual(cm.exception.code, "failed")
        types = [json.loads(x)["type"] for x in self.events()]
        self.assertEqual(types[-2:], ["experiment_admitted", "experiment_failed"])
        self.assertNotIn("C:/x", self.events()[-1].decode())
        n = len(self.events())
        for call in (lambda: self.c.run_experiment(p["plan_id"], E), lambda: self.plan("capacity_bound",
                     ("capacity_bound", "calibration_shift")), lambda: self.c.finalize(S)):
            with self.assertRaises(CycleError):
                call()
        self.assertEqual(len(self.events()), n)
        self.assertEqual(self.c.status()["phase"], "blocked")
        with self.assertRaises(CycleError) as cm:
            self.c.run_experiment("plan_ffffffffffffffff", E)
        self.assertEqual(cm.exception.code, "unknown")

    def test_roles_are_enforced(self):
        p = self.plan()
        for call in (lambda: self.c.run_experiment(p["plan_id"], A),
                     lambda: self.c.record_plan(["miss_partition", "capacity_bound"], "miss_partition", H, L, R, [], E),
                     lambda: self.c.finalize(A), lambda: self.c.read_context("fixture-direct:root"),
                     lambda: self.c.read_context(None)):
            with self.assertRaises(CycleError) as cm:
                call()
            self.assertEqual(cm.exception.code, "role_denied")

    def test_secrets_and_paths_are_sanitized(self):
        p = self.c.record_plan(["miss_partition", "capacity_bound"], "miss_partition",
                               H + " api_key=abc123 see C:/Users/me/secret.txt", L, R, [], A)["plan"]
        self.assertNotIn("abc123", p["hypothesis"])
        self.assertNotIn("C:/Users", p["hypothesis"])


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="discovery-mcp-"))
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_rm_readonly)
        self.root = self.tmp / "cycle"
        prepare(self.root, EVIDENCE)

    def rpc(self, role: str, calls: list[dict]) -> list[dict]:
        env = {**os.environ, "DISCOVERY_CYCLE_ROOT": str(self.root), "DISCOVERY_CYCLE_ROLE": role}
        msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}]
        msgs += [{"jsonrpc": "2.0", "id": i + 1, "method": "tools/call", "params": c} for i, c in enumerate(calls)]
        out = subprocess.run([sys.executable, "-m", "discovery_cycle.mcp_server"], cwd=REPO, env=env, timeout=60,
                             input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True)
        return [json.loads(x)["result"] for x in out.stdout.splitlines()[1:]]

    def test_stdio_roles_and_flow(self):
        res = self.rpc("analyst", [
            {"name": "run_experiment", "arguments": {"plan_id": "plan_0000000000000000"}},
            {"name": "record_plan", "arguments": {"candidates": ["miss_partition", "capacity_bound"],
                                                  "selected_test_id": "miss_partition", "hypothesis": H,
                                                  "expected_learning": L, "reason": R, "evidence_result_ids": [],
                                                  "root": "C:/elsewhere"}},
            {"name": "record_plan", "arguments": {"candidates": ["miss_partition", "capacity_bound"],
                                                  "selected_test_id": "miss_partition", "hypothesis": H,
                                                  "expected_learning": L, "reason": R, "evidence_result_ids": []}},
        ])
        self.assertTrue(res[0]["isError"])
        self.assertEqual(res[0]["structuredContent"]["code"], "role_denied")
        self.assertTrue(res[1]["isError"])
        plan_id = res[2]["structuredContent"]["plan"]["plan_id"]
        res = self.rpc("experimenter", [{"name": "run_experiment", "arguments": {"plan_id": plan_id}}])
        self.assertFalse(res[0]["isError"])
        self.assertEqual(res[0]["structuredContent"]["result"]["test_id"], "miss_partition")
        res = self.rpc("nobody", [{"name": "status", "arguments": {}}])
        self.assertTrue(res[0]["isError"])


if __name__ == "__main__":
    unittest.main()
