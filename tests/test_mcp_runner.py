"""Live runner checks without an LLM session, and agent config validation (NON-SCIENTIFIC fixtures).

These tests never contact an Omnigent server; they do not prove a live execution.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_core_support import ROOT, fixture_campaign, fresh_dir
from test_mcp_live import ANALYST, prepared, loop_once

sys.path.insert(0, str(ROOT / "scripts"))
import run_live  # noqa: E402

OMNI_PY = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "omnigent" / "Scripts" / "python.exe"


class RunnerTests(unittest.TestCase):
    def test_sanitize_drops_account_fields_and_paths(self):
        raw = {"session_id": "conv_x", "owner": "someone", "host_id": "host_abc", "workspace": str(ROOT),
               "nested": [{"api_key": "k", "text": f"wrote {ROOT / 'runs' / 'a.json'} via http://127.0.0.1:6767"}]}
        out = run_live.sanitize(raw)
        dumped = json.dumps(out)
        self.assertEqual(out["session_id"], "conv_x")
        for gone in ("owner", "host_id", "workspace", "api_key", str(ROOT).replace("\\", "\\\\")):
            self.assertNotIn(gone, dumped)
        self.assertIn("http://127.0.0.1:6767", dumped)

    def test_embedded_json_and_discovery_inventory_are_sanitized(self):
        embedded = json.dumps({"email": "private@example.test", "host_id": "private_host", "result_id": "res_1"})
        self.assertEqual(json.loads(run_live.sanitize(embedded)), {"result_id": "res_1"})
        items = [{"type": "function_call", "name": "sys_agent_list", "call_id": "a"},
                 {"type": "function_call_output", "call_id": "a", "output": "private inventory"},
                 {"type": "function_call", "name": "simulate_pvt_point", "call_id": "b"},
                 {"type": "function_call_output", "call_id": "b", "output": "scientific result"}]
        summary = run_live._items_summary(items)
        self.assertNotIn("private inventory", json.dumps(summary))
        self.assertEqual(summary["tool_calls"][1]["output"], "scientific result")

    def test_prepared_root_checks(self):
        c = fixture_campaign("runner-fixture-root")
        with self.assertRaisesRegex(run_live.LiveRunError, "no ledger"):
            run_live.check_prepared(c.root / "missing")
        with self.assertRaisesRegex(run_live.LiveRunError, "not a demo campaign"):
            run_live.check_prepared(c.root)

    def test_judge_requires_completed_turn_and_complete_omnigent_run(self):
        c, loop = prepared("runner-judge")
        loop_once(loop)
        loop_once(loop)
        loop.analyze_and_plan(True, ANALYST)
        st = run_live.live_state(run_live.read_ledger(c.root))
        self.assertEqual(st["closed"]["status"], "complete")
        self.assertEqual(len(st["updates"]), 2)
        status, reasons = run_live.judge("completed", st)
        self.assertEqual(status, "failed")  # fixture actors are not Omnigent sidecar calls
        self.assertTrue(any("Omnigent" in r for r in reasons))
        relabeled = {**st, "tool_calls": [{**t, "actor": "Omnigent analyst"} for t in st["tool_calls"]]}
        self.assertEqual(run_live.judge("completed", relabeled), ("completed", []))
        self.assertEqual(run_live.judge("timeout", relabeled)[0], "timeout")
        self.assertEqual(run_live.judge("completed", {**relabeled, "closed": None})[0], "failed")

    def test_cli_refuses_non_loopback_and_unprepared_root(self):
        c = fixture_campaign("runner-cli")
        self.assertEqual(run_live.main(["--server", "http://example.com:6767", "--demo-root", str(c.root)]), 2)
        self.assertEqual(run_live.main(["--demo-root", str(c.root)]), 2)
        self.assertFalse((c.root / run_live.PROOF_REL).exists())


# ---------------------------------------------------------------------- workflow follow-up fakes
# NON-SCIENTIFIC: scripted stand-ins for the SDK sessions namespace and SessionsChat.
SID = "sess_fixture_root"


def _completed(rid: str, status: str = "completed") -> dict:
    return {"type": f"response.{status}", "response": {"id": rid, "status": status, "usage": {"total_tokens": 1}}}


class FakeSessions:
    """``script[k]`` = (root status, subtree busy) at poll k (last entry repeats);
    ``stream_events`` = [(poll index from which it is emitted, event)]."""

    def __init__(self, script, stream_events=(), agent_id="ag_fixture", last_task_error=None):
        self.script, self.stream_events = list(script), list(stream_events)
        self.agent_id, self.last_task_error = agent_id, last_task_error
        self.tick = 0
        self.calls: list[str] = []  # anything that is not a read

    def _state(self):
        return self.script[min(self.tick, len(self.script) - 1)]

    async def get(self, sid):
        status = self._state()[0] if sid == SID else "idle"
        return SimpleNamespace(id=sid, status=status, agent_id=self.agent_id, agent_name="falsify_supervisor",
                               last_task_error=self.last_task_error if sid == SID else None, last_total_tokens=1,
                               llm_model="fixture", harness="fixture", reasoning_effort="medium")

    async def subtree_busy(self, sid):
        busy = self._state()[1]
        self.tick += 1
        return busy

    async def stream(self, sid):
        for at, ev in self.stream_events:
            while self.tick < at:
                await asyncio.sleep(0.002)
            yield ev
        await asyncio.Event().wait()  # an open SSE stream with nothing more to say

    async def list_items(self, sid, limit=100, after=None):
        return []

    async def child_sessions_tree(self, sid):
        return [{"id": "child_fixture", "agent_name": "analyst", "current_task_status": "completed"}]

    async def interrupt(self, sid):
        self.calls.append("interrupt")

    async def post_event(self, sid, event):
        self.calls.append("post_event")

    async def create(self, *a, **kw):
        self.calls.append("create")


class FakeChat:
    def __init__(self, events):
        self.events, self.sent = events, []

    async def send(self, prompt):
        self.sent.append(prompt)
        for ev in self.events:
            yield ev


def _drive(sessions, ledger_probe, *, chat=None, timeout=5.0, interrupt_on_timeout=False):
    rec = run_live.Recorder(io.StringIO(), SID)
    result = asyncio.run(run_live.drive(sessions, SID, rec, ledger_probe, deadline=time.monotonic() + timeout,
                                        poll_interval=0.01, settle_grace=0.1, chat=chat, prompt="fixture",
                                        interrupt_on_timeout=interrupt_on_timeout))
    return result, rec


CLOSED = {"closed": {"status": "complete", "termination_reason": "live_finalized"}, "updates": [{}, {}]}
OPEN = {"closed": None, "updates": []}


class WorkflowFollowTests(unittest.TestCase):
    def test_first_turn_completed_while_child_busy_then_wake_completion(self):
        # child busy, brief idle gap shorter than the grace, wake turn running, then settled.
        script = [("idle", True)] * 4 + [("idle", False)] * 2 + [("running", False)] * 4 + [("idle", False)]
        fake = FakeSessions(script, [(8, _completed("resp_wake"))])
        chat = FakeChat([{"type": "response.created"}, _completed("resp_first")])
        result, rec = _drive(fake, lambda: CLOSED if fake.tick >= 6 else OPEN, chat=chat)
        self.assertEqual(result["outcome"], "completed", result)
        self.assertEqual([r["response_id"] for r in rec.responses], ["resp_first", "resp_wake"])
        self.assertEqual([r["source"] for r in rec.responses], ["initial_send", "follow_stream"])
        self.assertGreater(rec.polls, 10)  # did not stop at the first response.completed
        self.assertEqual((chat.sent, fake.calls), (["fixture"], []))  # one prompt, nothing injected

    def test_closed_ledger_still_waits_for_busy_root_and_children(self):
        script = [("running", False)] * 3 + [("waiting", True)] * 3 + [("idle", True)] * 3 + [("idle", False)]
        fake = FakeSessions(script)
        result, rec = _drive(fake, lambda: CLOSED, chat=FakeChat([_completed("resp_first")]))
        self.assertEqual(result["outcome"], "completed", result)
        self.assertGreaterEqual(fake.tick, 10)
        self.assertEqual(result["follow"]["last_poll"]["root_status"], "idle")
        self.assertFalse(result["follow"]["last_poll"]["subtree_busy"])

    def test_idle_without_closed_ledger_fails_after_grace(self):
        fake = FakeSessions([("idle", False)])
        result, _ = _drive(fake, lambda: OPEN, chat=FakeChat([_completed("resp_first")]))
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("not closed", result["error"])

    def test_global_timeout(self):
        fake = FakeSessions([("running", True)])
        result, _ = _drive(fake, lambda: OPEN, chat=FakeChat([_completed("resp_first")]), timeout=0.15,
                           interrupt_on_timeout=True)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(fake.calls, ["interrupt"])
        quiet = FakeSessions([("running", True)])  # recovery mode never interrupts
        result, _ = _drive(quiet, lambda: OPEN, timeout=0.15)
        self.assertEqual((result["outcome"], quiet.calls), ("timeout", []))

    def test_failed_turns_are_reported(self):
        for status in ("failed", "incomplete", "cancelled"):
            fake = FakeSessions([("idle", False)], [(1, _completed("resp_wake", status))])
            result, _ = _drive(fake, lambda: CLOSED, chat=FakeChat([_completed("resp_first")]))
            self.assertEqual(result["outcome"], status)
        result, _ = _drive(FakeSessions([("running", True), ("failed", False)]), lambda: OPEN,
                           chat=FakeChat([_completed("resp_first")]))
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("root session failed", result["error"])
        result, _ = _drive(FakeSessions([("idle", False)], last_task_error={"code": "x"}), lambda: CLOSED,
                           chat=FakeChat([_completed("resp_first")]))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(run_live.judge("failed", CLOSED | {"tool_calls": []})[0], "failed")


class RecoveryTests(unittest.TestCase):
    def _closed_root(self, name):
        c, loop = prepared(name)
        loop_once(loop)
        loop_once(loop)
        loop.analyze_and_plan(True, ANALYST)
        prior = {"run_id": run_live.LIVE_RUN_ID, "agent": run_live.AGENT_NAME, "session_id": SID, "agent_id": "ag_fixture",
                 "status": "failed", "turn_outcome": "completed", "response_id": "resp_first",
                 "response_status": "completed", "usage": {"total_tokens": 1}, "failure_reasons": ["0 adaptive updates < 2"],
                 "finished_at": "fixture", "data_label": "NON-SCIENTIFIC fixture"}
        proof_path = c.root / run_live.PROOF_REL
        run_live._write_json(proof_path, prior)
        return c, prior, proof_path

    def _args(self, sid=SID):
        return argparse.Namespace(recover_session=sid, timeout=5.0, poll_interval=0.01, settle_grace=0.1)

    def test_recover_same_session_sends_nothing_and_keeps_history(self):
        c, prior, proof_path = self._closed_root("runner-recover")
        raw_before, n_events = proof_path.read_bytes(), len(c.events())
        fake = FakeSessions([("running", False), ("running", False), ("idle", False)], [(1, _completed("resp_final"))])
        raw_dir = fresh_dir("runner-recover-raw")
        proof = asyncio.run(run_live.run_recovery(fake, self._args(), c.root, proof_path, raw_dir, prior))
        self.assertEqual(fake.calls, [])  # no post_event, interrupt or session create
        self.assertEqual(len(c.events()), n_events)  # no experiment, no ledger write
        self.assertEqual((raw_dir / "prior-session-proof.json").read_bytes(), raw_before)
        self.assertEqual(proof["turn_outcome"], "completed", proof.get("error"))
        self.assertEqual([r["response_id"] for r in proof["responses"]], ["resp_first", "resp_final"])
        self.assertTrue(proof["responses"][0]["from_prior_attempt"])
        self.assertEqual(proof["attempts"][0]["status"], "failed")
        self.assertEqual(proof["session_id"], SID)

    def test_mismatched_recovery_is_refused_without_writing(self):
        c, prior, proof_path = self._closed_root("runner-recover-mismatch")
        raw_before = proof_path.read_bytes()
        st = {**run_live.live_state(run_live.read_ledger(c.root)), "campaign_kind": "demo"}
        run_live.check_recovery(prior, SID, st)  # matching prior proof, session and ledger run
        with self.assertRaisesRegex(run_live.RecoveryRefused, "another session"):
            run_live.check_recovery(prior, "sess_other", st)
        with self.assertRaisesRegex(run_live.RecoveryRefused, "live run/agent"):
            run_live.check_recovery({**prior, "run_id": "other-run"}, SID, st)
        with self.assertRaisesRegex(run_live.RecoveryRefused, "not a demo"):
            run_live.check_recovery(prior, SID, {**st, "campaign_kind": "fixture"})
        foreign = {**prior, "ledger_live_run": {**st, "decisions": [{"sequence": 1, "selected_point_id": "elsewhere"}]}}
        with self.assertRaisesRegex(run_live.RecoveryRefused, "does not extend"):
            run_live.check_recovery(foreign, SID, st)
        with self.assertRaisesRegex(run_live.RecoveryRefused, "does not extend"):
            run_live.check_recovery({**prior, "ledger_live_run": {**st, "ledger_events": st["ledger_events"] + 1}}, SID, st)
        raw_dir = fresh_dir("runner-recover-mismatch-raw")
        with self.assertRaisesRegex(run_live.RecoveryRefused, "another agent"):
            asyncio.run(run_live.run_recovery(FakeSessions([("idle", False)], agent_id="ag_other"), self._args(),
                                              c.root, proof_path, raw_dir, prior))
        self.assertFalse(raw_dir.exists())
        self.assertEqual(run_live.main(["--demo-root", str(c.root), "--recover-session", "sess_other"]), 2)
        self.assertEqual(proof_path.read_bytes(), raw_before)
        bare = fixture_campaign("runner-recover-noproof")
        self.assertEqual(run_live.main(["--demo-root", str(bare.root), "--recover-session", SID]), 2)
        self.assertFalse((bare.root / run_live.PROOF_REL).exists())


@unittest.skipUnless(OMNI_PY.is_file(), "Omnigent tool interpreter not installed")
class AgentConfigTests(unittest.TestCase):
    def test_bundle_validates_with_omnigent_parser(self):
        sidecars = [ROOT / "agent" / "agents" / r / "tools" / "mcp" / "falsify.yaml" for r in ("analyst", "experimenter")]
        if not all(p.is_file() for p in sidecars):
            self.skipTest("MCP sidecars not rendered; run launch.ps1 -Setup")
        proc = subprocess.run([str(OMNI_PY), str(ROOT / "scripts" / "validate_agent.py"), str(ROOT / "agent")],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        out = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, out["problems"])
        self.assertEqual(set(out["agents"]), {"falsify_supervisor", "analyst", "experimenter"})
        self.assertEqual(out["agents"]["analyst"]["mcp_allowlists"], {"falsify": ["analyze_and_plan", "read_result"]})
        self.assertEqual(out["agents"]["experimenter"]["mcp_allowlists"], {"falsify": ["read_result", "simulate_pvt_point"]})
        self.assertEqual(out["agents"]["falsify_supervisor"]["mcp_allowlists"], {})
        self.assertEqual({a["tool_call_cap"] for a in out["agents"].values()}, {12, 6})
        self.assertEqual(out["agents"]["falsify_supervisor"]["tool_call_cap"], 12)
        for a in out["agents"].values():
            self.assertEqual((a["model"], a["reasoning_effort"], a["harness"], a["os_env"]), ("claude-opus-5-5", "medium", "claude-sdk", None))


if __name__ == "__main__":
    unittest.main()
