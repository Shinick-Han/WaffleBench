"""Inspection live core: paid admission order, billing, denial, idempotency, tamper, crash, locking.

Every call here is a direct fixture call (actor ``fixture-direct``) on a tiny freshly frozen
fixture campaign; none of it is Omnigent orchestration or scientific evidence.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

from inspection_review.cli import save_lot, write_json
from inspection_v2.cli import privileged
from inspection_v3 import cli as v3cli
from inspection_v3 import model, sensor_yield
from inspection_review import data
from inspection_live import session as live
from inspection_live import mcp_server as ms

REPO = Path(__file__).resolve().parents[1]
ACTOR = "fixture-direct (not Omnigent)"
SEED = 9800


def build_campaign(base: Path) -> Path:
    """Tiny real freeze: one train lot, small CatBoost, v3 freeze() over unchanged sources."""
    config = v3cli.read_json(v3cli.PROTOCOL)
    config["v2_splits"]["train"] = config["v2_splits"]["train"][:1]
    config["splits"]["train_seeds"] = [r["seed"] for r in config["v2_splits"]["train"]]
    config["v3_model_grid"] = {"cb400": {"iterations": 20, "depth": 3, "learning_rate": .06}}
    config["v3_yield"]["iterations"] = 20
    root = base / "campaign"
    root.mkdir()
    row = config["v2_splits"]["train"][0]
    lot = data.generate_lot(row["seed"], row["scenario"], config)
    ref = save_lot(root / "lots/train" / lot["public"]["lot_id"], lot, "train", row["seed"], row["scenario"])
    train = [privileged(ref, "train")]
    settings = copy.deepcopy(config)
    settings["v2_model"] = {**config["v2_model"], **config["v3_model_grid"]["cb400"], "family": "catboost"}
    write_json(root / "config.json", config)
    write_json(root / "models.json", {"cb400/identity": model.train_model(train, settings)})
    write_json(root / "review-yield.json", sensor_yield.fit_review_yield(train, config))
    write_json(root / "development.json", {"kind": "fixture_only", "selected_candidate": "cb400_route_full"})
    write_json(root / "lot-manifest.json", {"train": [ref]})
    v3cli.freeze(root)
    return root


class LiveCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.source_before = v3cli.source_hashes()
        cls.campaign = build_campaign(cls.base)
        cls.campaign_bytes = {p: p.read_bytes() for p in cls.campaign.rglob("*") if p.is_file()}
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fresh(self, **kw) -> live.InspectionLive:
        type(self).n += 1
        root = self.base / f"live{self.n}"
        live.prepare(root, self.campaign, **kw)
        return live.InspectionLive(root)

    def events(self, s) -> list[dict]:
        return [json.loads(x) for x in (s.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def step(self, s):
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        return d, s.review_site(d["selected_site_id"], d["decision_sequence"], actor=ACTOR)["result"]

    # ------------------------------------------------------------ prepare
    def test_prepare_fresh_lot_and_frozen_inputs_unchanged(self):
        s = self.fresh()
        m = s.manifest
        self.assertEqual(m["variant"]["id"], "cb400_route_full")
        self.assertEqual(m["budget"], 120.0)
        self.assertEqual(m["max_reviews"], 4)
        self.assertNotIn('"seed"', json.dumps(m))
        self.assertNotIn('"scenario"', json.dumps(m))
        self.assertEqual({p: p.read_bytes() for p in self.campaign.rglob("*") if p.is_file()}, self.campaign_bytes)
        self.assertEqual(v3cli.source_hashes(), self.source_before)
        with self.assertRaises(live.LiveError):
            live.prepare(s.root, self.campaign)  # existing root
        with self.assertRaises(live.LiveError):
            live.prepare(self.base / "used-seed", self.campaign, seed=7000)  # primary test seed
        self.assertFalse((self.base / "used-seed").exists())

    def test_prepare_rejects_tampered_campaign(self):
        bad = self.base / "bad-campaign"
        import shutil
        shutil.copytree(self.campaign, bad)
        models = json.loads((bad / "models.json").read_text(encoding="utf-8"))
        models["cb400/identity"]["mean"][0] += 1.0
        (bad / "models.json").write_text(json.dumps(models), encoding="utf-8")
        with self.assertRaises(live.LiveError):
            live.prepare(self.base / "never", bad)
        self.assertFalse((self.base / "never").exists())

    def test_prepare_rejects_different_frozen_candidate(self):
        receipt = copy.deepcopy(v3cli.verify_freeze(self.campaign))
        receipt['candidate'] = 'yield_route'
        root = self.base / 'wrong-candidate'
        with mock.patch.object(v3cli, 'verify_freeze', return_value=receipt):
            with self.assertRaisesRegex(live.LiveError, 'same cb400_route_full'):
                live.prepare(root, self.campaign)
        self.assertFalse(root.exists())

    # ------------------------------------------------------------ normal loop
    def test_full_loop_updates_once_and_finalizes(self):
        s = self.fresh()
        plan = s.analyze_and_plan(actor=ACTOR)
        d1 = plan["decision"]
        self.assertEqual(plan["status"], "decision_recorded")
        self.assertGreaterEqual(len({c["site_id"] for c in d1["candidates"]}), 2)
        self.assertEqual(d1["candidates"][0]["site_id"], d1["selected_site_id"])
        self.assertTrue(all(c["reserved_cost"] <= 120 for c in d1["candidates"]))
        self.assertEqual(d1["evidence_result_ids"], [])
        self.assertIn("rule", d1["score_parts"])
        # idempotent pending decision
        n = len(self.events(s))
        again = s.analyze_and_plan(actor=ACTOR)
        self.assertEqual(again["status"], "decision_pending")
        self.assertEqual(again["decision"], d1)
        self.assertEqual(len(self.events(s)), n)
        with self.assertRaises(live.LiveError):
            s.analyze_and_plan(True, actor=ACTOR)  # pending decision blocks finalize
        r1 = s.review_site(d1["selected_site_id"], 1, actor=ACTOR)["result"]
        for key in ("result_id", "site_id", "decision_sequence", "reported_doi", "reported_kind", "quality", "status",
                    "attempts", "charged", "cumulative_spend", "baseline_p", "selection_reward",
                    "selection_reward_semantics", "selection_reward_meaning", "reserved_cost", "cost", "reported_positive"):
            self.assertIn(key, r1)
        self.assertEqual(r1["selection_reward_semantics"], "latent_doi_probability")
        self.assertIn("does not prove", r1["negative_report_meaning"])
        with self.assertRaises(live.LiveError):
            s.analyze_and_plan(True, actor=ACTOR)  # fewer than two reviews
        p2 = s.analyze_and_plan(actor=ACTOR)
        self.assertEqual([u["observed_result_id"] for u in p2["new_updates"]], [r1["result_id"]])
        self.assertEqual(p2["decision"]["evidence_result_ids"], [r1["result_id"]])
        self.assertEqual(s.analyze_and_plan(actor=ACTOR)["status"], "decision_pending")
        self.assertEqual(len(s.status()["updates"]), 1)  # no duplicate update for the same result
        r2 = s.review_site(p2["decision"]["selected_site_id"], 2, actor=ACTOR)["result"]
        self.assertAlmostEqual(r2["cumulative_spend"], r1["charged"] + r2["charged"])
        fin = s.analyze_and_plan(True, actor=ACTOR)
        self.assertEqual(fin["status"], "closed")
        self.assertFalse(fin["next_preview_unrecorded"] is not None and fin["next_preview_unrecorded"]["recorded"])
        ups = [u["observed_result_id"] for u in s.status()["updates"]]
        self.assertEqual(ups, [r1["result_id"], r2["result_id"]])
        self.assertEqual(len([e for e in self.events(s) if e["type"] == "decision"]), 2)
        self.assertEqual(s.analyze_and_plan(actor=ACTOR)["status"], "closed")
        with self.assertRaises(live.LiveError):
            s.review_site(p2["decision"]["selected_site_id"], 3, actor=ACTOR)

    def test_reconstruction_matches_across_instances(self):
        s = self.fresh()
        self.step(s)
        p = s.analyze_and_plan(actor=ACTOR)
        other = live.InspectionLive(s.root)
        self.assertEqual(other.analyze_and_plan(actor=ACTOR)["decision"], p["decision"])
        self.assertEqual(other.status(), s.status())

    # ------------------------------------------------------------ denials
    def test_wrong_repeated_unknown_calls_denied_without_charge(self):
        s = self.fresh()
        with self.assertRaises(live.LiveError):
            s.review_site(s.view.site_ids[0], 1, actor=ACTOR)  # nothing recorded yet
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        other = next(c["site_id"] for c in d["candidates"] if c["site_id"] != d["selected_site_id"])
        for site, seq in [(other, 1), (d["selected_site_id"], 2), (d["selected_site_id"], True), ("nope", 1)]:
            with self.assertRaises(live.LiveError):
                s.review_site(site, seq, actor=ACTOR)
        self.assertEqual(s.status()["budget"]["spent"], 0)
        self.assertFalse([e for e in self.events(s) if e["type"] == "admission"])
        r = s.review_site(d["selected_site_id"], 1, actor=ACTOR)["result"]
        with self.assertRaises(live.LiveError):
            s.review_site(d["selected_site_id"], 1, actor=ACTOR)  # duplicate execution
        for rid in ["ir_0000000000000000", "res_0123456789abcdef", "../x", 5]:
            with self.assertRaises(live.LiveError):
                s.read_result(rid, actor=ACTOR)
        self.assertEqual(s.read_result(r["result_id"], actor=ACTOR)["result_id"], r["result_id"])
        self.assertEqual(len([e for e in self.events(s) if e["type"] == "admission"]), 1)

    def test_review_cap_and_budget_cap(self):
        s = self.fresh(max_reviews=2)
        self.step(s)
        self.step(s)
        out = s.analyze_and_plan(actor=ACTOR)
        self.assertEqual(out["status"], "review_cap_reached")
        self.assertIsNone(out["pending_decision"])
        self.assertEqual(s.analyze_and_plan(True, actor=ACTOR)["status"], "closed")
        tight = self.fresh(budget=30)
        _, r = self.step(tight)
        out = tight.analyze_and_plan(actor=ACTOR)
        if out["status"] == "decision_recorded":
            self.assertLessEqual(r["charged"] + out["decision"]["candidates"][0]["reserved_cost"], 30 + 1e-9)
        else:
            self.assertEqual(out["status"], "none_affordable")

    # ------------------------------------------------------------ paid order and billing
    def test_reservation_persisted_before_sensor_and_retry_billed(self):
        s = self.fresh()
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        seen = []
        real = s._sensor

        def sensor(i, attempt):
            ev = self.events(s)
            seen.append((ev[-1]["type"], ev[-1]["payload"]["reserved_cost"]))
            if attempt == 0:
                return {"status": "missing", "reported_doi": None, "reported_kind": None, "quality": 0.2}
            return real(i, attempt)

        with mock.patch.object(s, "_sensor", sensor):
            r = s.review_site(d["selected_site_id"], 1, actor=ACTOR)["result"]
        self.assertEqual([t for t, _ in seen], ["admission", "admission"])
        self.assertEqual(seen[0][1], d["candidates"][0]["reserved_cost"])
        self.assertEqual(len(r["attempts"]), 2)
        self.assertEqual(r["cost"]["retry"], 4.0)
        self.assertAlmostEqual(r["charged"], r["reserved_cost"])
        self.assertAlmostEqual(s.status()["budget"]["spent"], r["reserved_cost"])

    def test_crash_after_admission_stays_charged_and_blocks(self):
        s = self.fresh()
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        with mock.patch.object(s, "_sensor", side_effect=RuntimeError("sensor process died")):
            with self.assertRaises(RuntimeError):
                s.review_site(d["selected_site_id"], 1, actor=ACTOR)
        reopened = live.InspectionLive(s.root)
        st = reopened.status()
        self.assertEqual(st["blocked"]["reason"], "incomplete_admission")
        self.assertAlmostEqual(st["budget"]["spent"], d["candidates"][0]["reserved_cost"])
        self.assertEqual(st["observations"], [])
        rid = st["blocked"]["result_ids"][0]
        with self.assertRaises(live.LiveError):
            reopened.read_result(rid, actor=ACTOR)
        with self.assertRaises(live.LiveError):
            reopened.review_site(d["selected_site_id"], 1, actor=ACTOR)  # no silent retry
        with self.assertRaises(live.LiveError):
            reopened.analyze_and_plan(actor=ACTOR)
        self.assertEqual(len([e for e in self.events(s) if e["type"] == "admission"]), 1)

    # ------------------------------------------------------------ tamper
    def test_input_and_ledger_tamper_rejected(self):
        s = self.fresh()
        m = json.loads((s.root / "live.json").read_text(encoding="utf-8"))
        m["budget"] = 1000.0
        (s.root / "live.json").write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(live.LiveError):
            live.InspectionLive(s.root)
        for target in ["inputs/frozen_p.npy", "inputs/model.json", "inputs/freeze.json", "private/lot/public.npz"]:
            s = self.fresh()
            path = s.root / target
            raw = bytearray(path.read_bytes())
            raw[-2] ^= 1
            path.write_bytes(bytes(raw))
            with self.assertRaises(live.LiveError, msg=target):
                live.InspectionLive(s.root)
        s = self.fresh()
        self.step(s)
        lines = (s.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        e = json.loads(lines[-2])
        e["payload"]["charged"] = 0.0
        lines[-2] = json.dumps(e)
        (s.root / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(live.LiveError):
            s.status()
        s = self.fresh()
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        oracle = s.root / "private/lot/oracle.npz"
        raw = bytearray(oracle.read_bytes())
        raw[-2] ^= 1
        oracle.write_bytes(bytes(raw))
        with self.assertRaises(live.LiveError):
            s.review_site(d["selected_site_id"], 1, actor=ACTOR)
        self.assertTrue(s.status()["blocked"])  # admission happened first: conservatively charged

    def test_source_change_rejected_on_load(self):
        s = self.fresh()
        changed = dict(v3cli.source_hashes(), **{"inspection_v3/policies.py": "0" * 64})
        with mock.patch.object(v3cli, "source_hashes", return_value=changed):
            with self.assertRaises(live.LiveError):
                live.InspectionLive(s.root)

    # ------------------------------------------------------------ public-only views
    def test_public_views_carry_no_seed_scenario_or_oracle(self):
        s = self.fresh()
        _, r = self.step(s)
        texts = [json.dumps(s.status()), json.dumps(s.analyze_and_plan(actor=ACTOR)), json.dumps(r)]
        for text in texts:
            for word in ['"seed"', '"scenario"', "stationary", "oracle", '"review_quality"', '"physical"', '"doi"', "electrical"]:
                self.assertNotIn(word, text)
        self.assertFalse(any("oracle" in k for k in vars(s)))
        with mock.patch.object(live, "_load_npz", side_effect=AssertionError("oracle opened while planning")):
            s.analyze_and_plan(actor=ACTOR)
            s.status()

    # ------------------------------------------------------------ cross-process exclusion
    def test_cross_process_concurrent_review_admits_once(self):
        s = self.fresh()
        d = s.analyze_and_plan(actor=ACTOR)["decision"]
        go = self.base / f"go{self.n}"
        script = (
            "import json,sys,time,pathlib\n"
            f"sys.path.insert(0,{str(REPO)!r})\n"
            "from inspection_live import InspectionLive, LiveError\n"
            "s=InspectionLive(sys.argv[1])\n"
            "go=pathlib.Path(sys.argv[2])\n"
            "while not go.exists(): time.sleep(0.005)\n"
            "try:\n s.review_site(sys.argv[3], 1, actor='fixture-process'); print('ok')\n"
            "except LiveError as e: print('denied')\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(s.root), str(go), d["selected_site_id"]],
                                  stdout=subprocess.PIPE, text=True) for _ in range(3)]
        time.sleep(4)
        go.write_text("go")
        outs = sorted(p.communicate(timeout=120)[0].strip() for p in procs)
        self.assertEqual(outs, ["denied", "denied", "ok"])
        self.assertEqual(len([e for e in self.events(s) if e["type"] == "admission"]), 1)

    def test_lock_excludes_other_process(self):
        s = self.fresh()
        script = (f"import sys,time\nsys.path.insert(0,{str(REPO)!r})\n"
                  "from pathlib import Path\nfrom inspection_live.session import exclusive_lock\n"
                  "with exclusive_lock(Path(sys.argv[1])):\n print('held', flush=True)\n time.sleep(1.5)\n")
        p = subprocess.Popen([sys.executable, "-c", script, str(s.root / ".lock")], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        t0 = time.monotonic()
        s.status()
        self.assertGreater(time.monotonic() - t0, 0.8)
        p.wait(timeout=30)
        p.stdout.close()

    # ------------------------------------------------------------ MCP surface
    def test_mcp_roles_strict_arguments_and_stdio(self):
        s = self.fresh()
        def call(role, name, args):
            with mock.patch.dict(os.environ, {"INSPECTION_LIVE_ROOT": str(s.root), "INSPECTION_MCP_ROLE": role}):
                ms._SESSION = None
                return ms.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
        self.assertTrue(call("experimenter", "analyze_and_plan", {})["isError"])
        self.assertTrue(call("analyst", "analyze_and_plan", {"root": "x"})["isError"])
        self.assertTrue(call("", "read_result", {"result_id": "ir_0000000000000000"})["isError"])
        plan = call("analyst", "analyze_and_plan", {})
        self.assertFalse(plan["isError"])
        d = plan["structuredContent"]["decision"]
        self.assertEqual(d["actor"], "mcp-sidecar:analyst")
        self.assertTrue(call("analyst", "review_site", {"site_id": d["selected_site_id"], "decision_sequence": 1})["isError"])
        self.assertTrue(call("experimenter", "review_site", {"site_id": d["selected_site_id"], "decision_sequence": "1"})["isError"])
        res = call("experimenter", "review_site", {"site_id": d["selected_site_id"], "decision_sequence": 1})
        self.assertFalse(res["isError"])
        rid = res["structuredContent"]["result"]["result_id"]
        self.assertFalse(call("analyst", "read_result", {"result_id": rid})["isError"])
        for schema in ms.TOOLS.values():
            props = set(schema["inputSchema"]["properties"])
            self.assertFalse(props & {"root", "path", "seed", "scenario", "command"})
        env = {**os.environ, "INSPECTION_LIVE_ROOT": str(s.root), "INSPECTION_MCP_ROLE": "analyst"}
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_result", "arguments": {"result_id": rid}}}]
        out = subprocess.run([sys.executable, "-m", "inspection_live.mcp_server"], input="".join(json.dumps(m) + "\n" for m in msgs),
                             capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
        replies = [json.loads(x) for x in out.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]], ["analyze_and_plan", "read_result"])
        self.assertEqual(replies[2]["result"]["structuredContent"]["result_id"], rid)
        self.assertNotIn(str(s.root), out.stdout)


if __name__ == "__main__":
    unittest.main()
