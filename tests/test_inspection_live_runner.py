"""Inspection live runner checks with stub core and scripted SDK fakes (NON-SCIENTIFIC fixtures).

Nothing here contacts an Omnigent server, the core, a model or the API; none of it is live
orchestration evidence. Every directory is a fresh ``tempfile.mkdtemp`` and is never deleted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import run_inspection_live as rl  # noqa: E402

OMNI_PY = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "omnigent" / "Scripts" / "python.exe"
PWSH = shutil.which("pwsh") or shutil.which("powershell")
SERVER = "http://127.0.0.1:6771"
SID = "conv_fixture_root"
AN_CHILD, EX_CHILD = "conv_fixture_analyst", "conv_fixture_experimenter"
R1, R2 = "ir_" + "a" * 16, "ir_" + "b" * 16
S1, S2 = "W01-S013", "W02-S077"
SIDE_A, SIDE_E = "mcp-sidecar:analyst", "mcp-sidecar:experimenter"


def tmpdir(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"insp-runner-{tag}-"))


def write_sidecars(agent_dir: Path, live_root: Path, *, analyst_root: Path | None = None) -> None:
    for agent, role, tools in ((rl.ANALYST, "analyst", "analyze_and_plan, read_result"),
                               (rl.EXPERIMENTER, "experimenter", "review_site, read_result")):
        root = analyst_root if (analyst_root and agent == rl.ANALYST) else live_root
        d = agent_dir / "agents" / agent / "tools" / "mcp"
        d.mkdir(parents=True, exist_ok=True)
        (d / "inspection.yaml").write_text(
            "name: inspection\ntransport: stdio\n"
            f'command: "{Path(sys.executable).as_posix()}"\nargs: ["-m", "inspection_live.mcp_server"]\nenv:\n'
            f'  INSPECTION_LIVE_ROOT: "{root.as_posix()}"\n  INSPECTION_MCP_ROLE: "{role}"\n'
            f'  PYTHONPATH: "{ROOT.as_posix()}"\n  PYTHONUTF8: "1"\ntimeout: 120\ntools: [{tools}]\n', encoding="utf-8")


# ---------------------------------------------------------------------- stub core (status() shape)
def fresh_status() -> dict:
    return {"kind": "inspection_live", "lot_id": "lot_fixture", "budget": {"limit": 120.0, "spent": 0.0, "remaining": 120.0},
            "max_reviews": 4, "reviews_admitted": 0, "reviews_executed": 0, "pending_decision": None, "decisions": [],
            "observations": [], "updates": [], "closed": None, "blocked": None}


def closed_status(actor_a: str = SIDE_A) -> dict:
    dec = [{"decision_sequence": 1, "selected_site_id": S1, "actor": actor_a,
            "candidates": [{"site_id": S1}, {"site_id": "W01-S002"}]},
           {"decision_sequence": 2, "selected_site_id": S2, "actor": actor_a,
            "candidates": [{"site_id": S2}, {"site_id": "W02-S010"}]}]
    obs = [{"result_id": R1, "site_id": S1, "decision_sequence": 1, "status": "ok", "charged": 30.0},
           {"result_id": R2, "site_id": S2, "decision_sequence": 2, "status": "ok", "charged": 30.0}]
    upd = [{"update_index": 1, "actor": actor_a, "observed_result_id": R1},
           {"update_index": 2, "actor": actor_a, "observed_result_id": R2}]
    return {**fresh_status(), "budget": {"limit": 120.0, "spent": 60.0, "remaining": 60.0}, "reviews_admitted": 2,
            "reviews_executed": 2, "decisions": dec, "observations": obs, "updates": upd,
            "closed": {"status": "finalized", "actor": actor_a, "reviews_executed": 2, "updates": 2}}


class StubCore:
    def __init__(self, final: dict | None = None):
        self.state, self.final = fresh_status(), final if final is not None else closed_status()
        self.calls = 0

    def finish(self):
        self.state = self.final

    def __call__(self):
        self.calls += 1
        return json.loads(json.dumps(self.state))


# ---------------------------------------------------------------------- scripted SDK items
# Sanitized shapes of the real Omnigent 0.16 claude-sdk session items (live-9800a raw artifacts): a record
# whose ``model`` equals its ``response_id`` is the adapter's SDK mirror (true tool_use id); any other record
# is the dispatch bridge's execution record. In a response that first calls the SDK-internal ToolSearch, every
# later MCP dispatch record is stamped with the PREVIOUS tool_use id and the SDK mirror is persisted as well.
LABEL = rl.AGENT_NAME
CHILD = {i: f"conv_fixture_child_{i}" for i in range(1, 6)}
TITLE = {1: "analyst-plan-1", 2: "experimenter-review-1", 3: "analyst-plan-2", 4: "experimenter-review-2",
         5: "analyst-finalize"}
_n = iter(range(10 ** 6))


def rec(name, cid, args, output, resp, model):
    iid = f"item{next(_n):05d}"
    return [{"id": iid, "response_id": resp, "type": "function_call", "status": "completed", "model": model,
             "name": name, "call_id": cid, "arguments": json.dumps(args)},
            {"id": iid + "o", "response_id": resp, "type": "function_call_output", "status": "completed",
             "call_id": cid, "output": output if isinstance(output, str) else json.dumps(output)}]


def fifo_turn(resp, calls, search="select:mcp__omnigent__sys_session_send"):
    """ToolSearch, then each call twice: dispatch record stamped with the previous id + its SDK mirror."""
    ids = [f"toolu_{resp}_{k}" for k in range(len(calls) + 1)]
    items = rec("ToolSearch", ids[0], {"query": search}, [{"type": "tool_reference", "tool_name": "x"}], resp, resp)
    for k, (name, args, out) in enumerate(calls, 1):
        items += rec(name, ids[k - 1], args, out, resp, LABEL)
        items += rec(name, ids[k], args, {"result": out} if isinstance(out, str) else out, resp, resp)
    return items


def plain_turn(resp, calls):
    items = []
    for k, (name, args, out) in enumerate(calls):
        items += rec(name, f"toolu_{resp}_{k}", args, out, resp, LABEL)
    return items


def send(i, agent=None, child=None, out_agent=None):
    agent = agent or rl.DELEGATION_ORDER[i - 1]
    child = child or CHILD[i]
    return ("sys_session_send", {"agent": agent, "title": TITLE[i], "args": f"Step {i}"},
            {"task_id": child, "handle_id": child, "conversation_id": child, "kind": "sub_agent",
             "agent": out_agent or agent, "title": TITLE[i], "status": "launching"})


def inbox(child=None):
    return ("sys_read_inbox", {}, f"[System: sub-agent task {child} completed]" if child
            else "Inbox is empty — no completed tasks.")


def supervisor_items(order=None, extra_first=(), child_of=None):
    order = order or rl.DELEGATION_ORDER
    child_of = child_of or CHILD
    first = [send(1, order[0], child_of[1])] + list(extra_first) + [inbox()]
    items = fifo_turn("resp_sup_1", first)
    for i in range(2, len(order) + 1):
        items += plain_turn(f"resp_sup_{i}", [inbox(child_of[i - 1]), send(i, order[i - 1], child_of[i])])
    return items + plain_turn("resp_sup_end", [inbox(child_of[len(order)])])


def user_turn(text, turn):
    """The user message a delegation opens in its child (Omnigent persists the dispatched ``args`` verbatim)."""
    return [{"id": f"item{next(_n):05d}", "response_id": turn, "type": "message", "role": "user",
             "status": "completed", "content": [{"type": "input_text", "text": text}]}]


def step_outputs(second_rid=R2):
    budget = {"limit": 120.0, "spent": 0, "remaining": 120.0}
    return {
        1: ("inspection__analyze_and_plan", {"finalize": False},
            {"status": "decision_recorded", "budget": budget, "new_updates": [],
             "decision": {"decision_sequence": 1, "selected_site_id": S1, "actor": SIDE_A}}),
        2: ("inspection__review_site", {"site_id": S1, "decision_sequence": 1},
            {"status": "observed", "budget": budget,
             "result": {"result_id": R1, "site_id": S1, "decision_sequence": 1, "status": "ok"}}),
        3: ("inspection__analyze_and_plan", {"finalize": False},
            {"status": "decision_recorded", "budget": budget,
             "decision": {"decision_sequence": 2, "selected_site_id": S2, "evidence_result_ids": [R1]},
             "new_updates": [{"update_index": 1, "observed_result_id": R1, "observed_site_id": S1}]}),
        4: ("inspection__review_site", {"site_id": S2, "decision_sequence": 2},
            {"status": "observed", "budget": budget,
             "result": {"result_id": second_rid, "site_id": S2, "decision_sequence": 2, "status": "ok"}}),
        5: ("inspection__analyze_and_plan", {"finalize": True},
            {"status": "closed", "budget": budget, "closed": {"status": "finalized", "actor": SIDE_A},
             "new_updates": [{"update_index": 2, "observed_result_id": R2, "observed_site_id": S2}]}),
    }


def child_node(i, agent=None, parent=SID, **kw):
    agent = agent or rl.DELEGATION_ORDER[i - 1]
    return {"id": CHILD[i], "title": f"{agent}:{TITLE[i]}", "kind": "sub_agent", "agent_id": "ag_fixture",
            "agent_name": None, "tool": agent, "session_name": TITLE[i], "parent_id": parent,
            "parent_session_id": parent, "current_task_status": "completed", **kw}


def children(outputs=None, nodes=None, items=None):
    outputs = outputs or step_outputs()
    kids = {}
    for i in range(1, 6):
        name, args, out = outputs[i]
        it = (items or {}).get(i)
        kids[CHILD[i]] = {"summary": (nodes or {}).get(i) or child_node(i),
                          "items": user_turn(f"Step {i}", f"turn_child_{i}") + (
                              it if it is not None else fifo_turn(f"resp_child_{i}", [(name, args, out)],
                                                                  search=f"select:mcp__omnigent__{name}"))}
    return kids


# live-9800b shape: sys_session_send reuses one analyst conversation (turns 1, 3, 5) and one experimenter
# conversation (turns 2, 4); only each child's first response starts with ToolSearch (stale-FIFO pair).
REUSE = {1: AN_CHILD, 2: EX_CHILD, 3: AN_CHILD, 4: EX_CHILD, 5: AN_CHILD}


def reused_turn(i, outputs=None, text=None, resp=None, extra=()):
    name, args, out = (outputs or step_outputs())[i]
    body = (fifo_turn if i <= 2 else plain_turn)(resp or f"resp_child_{i}", [(name, args, out), *extra])
    return user_turn(f"Step {i}" if text is None else text, f"turn_child_{i}") + body


def reused_children(turns=None, nodes=None):
    turns = {AN_CHILD: [reused_turn(i) for i in (1, 3, 5)], EX_CHILD: [reused_turn(i) for i in (2, 4)], **(turns or {})}
    nodes = {AN_CHILD: child_node(1, id=AN_CHILD), EX_CHILD: child_node(2, id=EX_CHILD), **(nodes or {})}
    return {cid: {"summary": nodes[cid], "items": [x for t in turns[cid] for x in t]} for cid in (AN_CHILD, EX_CHILD)}


# ---------------------------------------------------------------------- SDK fakes
def terminal(rid, status="completed"):
    return {"type": f"response.{status}", "response": {"id": rid, "status": status, "usage": {"total_tokens": 1}}}


class FakeSessions:
    """``script[k]`` = (root status, subtree busy) at poll k (last repeats); ``on_settle`` runs at the last entry."""

    def __init__(self, script, core: StubCore, items=None, kids=None, runner_id="runner_secret_123456"):
        self.script, self.core, self.tick = list(script), core, 0
        self.items = items if items is not None else supervisor_items()
        self.kids = kids if kids is not None else children()
        self.runner_id = runner_id
        self.calls: list[str] = []

    def _state(self):
        return self.script[min(self.tick, len(self.script) - 1)]

    async def get(self, sid):
        status = self._state()[0] if sid == SID else "idle"
        return SimpleNamespace(id=sid, status=status, agent_id="ag_fixture", agent_name=rl.AGENT_NAME, last_task_error=None,
                               last_total_tokens=1, llm_model="fixture", harness="fixture", reasoning_effort="medium",
                               runner_id=self.runner_id)

    async def subtree_busy(self, sid):
        busy = self._state()[1]
        self.tick += 1
        if self.tick >= len(self.script) and not busy:
            self.core.finish()
        return busy

    async def stream(self, sid):
        await asyncio.Event().wait()
        yield {}  # pragma: no cover

    async def list_items(self, sid, limit=100, after=None):
        if sid == SID:
            return self.items
        return self.kids.get(sid, {}).get("items", [])

    async def child_sessions_tree(self, sid):
        return [c["summary"] for c in self.kids.values()]

    async def interrupt(self, sid):
        self.calls.append("interrupt")

    async def post_event(self, sid, event):
        self.calls.append("post_event")

    async def create(self, *a, **kw):
        self.calls.append("create")


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self):
        self.posts: list[str] = []

    async def get(self, url, params=None):
        if url.endswith("/health"):
            return Resp(200, {"status": "ok"})
        if url.endswith("/v1/agents"):
            return Resp(200, {"data": [{"id": "ag_fixture", "name": rl.AGENT_NAME}, {"id": "ag_other", "name": "falsify_supervisor"}]})
        if url.endswith("/v1/hosts"):
            return Resp(200, {"hosts": [{"host_id": "host_secret_abcdef", "status": "online", "name": "box-private-name"}]})
        raise AssertionError(url)

    async def post(self, url, json=None):
        self.posts.append(url)
        return Resp(201, {"id": SID})


def make_client(sessions: FakeSessions, http: FakeHttp):
    class FakeClient:
        def __init__(self, base_url):
            assert base_url == SERVER
            self._http, self.sessions = http, sessions

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return FakeClient


class FakeChat:
    def __init__(self, events, proof_path: Path):
        self.events, self.proof_path, self.sent = events, proof_path, []
        self.proof_at_send = None

    async def send(self, prompt):
        self.proof_at_send = json.loads(self.proof_path.read_text(encoding="utf-8"))
        self.sent.append(prompt)
        for ev in self.events:
            yield ev


class Env:
    def __init__(self, tag, script, events=(terminal("resp_1"),), **kw):
        self.base = tmpdir(tag)
        self.live = self.base / "live"
        self.live.mkdir()
        self.agent = self.base / "agent"
        write_sidecars(self.agent, self.live)
        self.runtime = self.base / "runtime"
        self.core = StubCore(kw.pop("final", None))
        self.sessions = FakeSessions(script, self.core, **kw)
        self.http = FakeHttp()
        self.proof_path = self.live / rl.PROOF_REL
        self.chats: list[FakeChat] = []
        self.events = list(events)

    def chat_factory(self, client, session):
        chat = FakeChat(self.events, self.proof_path)
        self.chats.append(chat)
        return chat

    def main(self, *extra, chat_factory=None, timeout="5"):
        argv = ["--server", SERVER, "--live-root", str(self.live), "--poll-interval", "0.005", "--settle-grace", "0",
                "--timeout", timeout, *extra]
        return rl.main(argv, client_cls=make_client(self.sessions, self.http), chat_factory=chat_factory or self.chat_factory,
                       core_probe=self.core, agent_dir=self.agent, runtime=self.runtime)

    def proof(self):
        return json.loads(self.proof_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- tests
class AsyncWorkflowTests(unittest.TestCase):
    def test_first_response_with_busy_child_is_insufficient_and_one_prompt(self):
        busy = [("idle", True)] * 4 + [("running", False)] * 2 + [("idle", False)]
        env = Env("complete", busy)
        self.assertEqual(env.main(), 0)
        p = env.proof()
        self.assertEqual(p["status"], "completed", p["failure_reasons"])
        self.assertGreaterEqual(p["workflow_follow"]["polls"], 7)  # kept following after the first response
        self.assertEqual(len(env.chats), 1)
        self.assertEqual(env.chats[0].sent, [rl.PROMPT])
        self.assertEqual(env.http.posts, [f"{SERVER}/v1/sessions"])  # exactly one session created
        self.assertEqual(env.sessions.calls, [])  # no extra event, create or interrupt
        self.assertEqual(env.chats[0].proof_at_send["session_id"], SID)  # saved before the prompt
        self.assertEqual(env.chats[0].proof_at_send["status"], "running")
        self.assertEqual(p["orchestration"]["delegation_count"], 5)
        self.assertEqual(len(p["core"]["updates"]), 2)

    def test_busy_child_until_deadline_is_timeout_preserved(self):
        env = Env("timeout", [("idle", True)])
        self.assertEqual(env.main(timeout="0.3"), 1)
        p = env.proof()
        self.assertEqual(p["status"], "timeout")
        self.assertEqual(p["responses"][0]["status"], "completed")  # the first response alone is not completion
        self.assertIn("interrupt", env.sessions.calls)
        self.assertTrue(any("workflow ended timeout" in r for r in p["failure_reasons"]))
        raw = list((env.runtime / "live-proof").iterdir())
        self.assertEqual(len(raw), 1)
        self.assertTrue((raw[0] / "stream-events.jsonl").is_file())
        self.assertTrue((raw[0] / "session-items.json").is_file())

    def test_incomplete_response_is_preserved(self):
        env = Env("incomplete", [("idle", False)], events=[terminal("resp_x", "incomplete")])
        self.assertEqual(env.main(), 1)
        self.assertEqual(env.proof()["status"], "incomplete")

    def test_recovery_never_creates_prompts_or_interrupts(self):
        env = Env("recover", [("idle", True)])
        self.assertEqual(env.main(timeout="0.3"), 1)
        env.sessions.calls.clear()
        env.http.posts.clear()
        env.sessions.script, env.sessions.tick = [("idle", True), ("idle", False)], 0

        def no_chat(*a):
            raise AssertionError("recovery must not build a chat")

        self.assertEqual(env.main("--recover-session", SID, chat_factory=no_chat), 0)
        p = env.proof()
        self.assertEqual(p["status"], "completed", p["failure_reasons"])
        self.assertEqual(env.http.posts, [])
        self.assertEqual(env.sessions.calls, [])
        self.assertEqual([a["mode"] for a in p["attempts"]], ["send", "recover"])
        self.assertEqual(len(env.chats), 1)
        self.assertTrue(any(r.get("from_prior_attempt") for r in p["responses"]))

    def test_recovery_refused_for_another_session_leaves_proof(self):
        env = Env("recover-refused", [("idle", True)])
        env.main(timeout="0.3")
        before = env.proof_path.read_bytes()
        self.assertEqual(env.main("--recover-session", "conv_other"), 2)
        self.assertEqual(env.proof_path.read_bytes(), before)
        self.assertEqual(env.main("--recover-session", SID, "--live-root", str(env.base / "missing")), 2)

    def test_failure_before_session_writes_no_proof(self):
        env = Env("no-host", [("idle", False)])

        async def no_hosts(url, params=None):
            return Resp(200, {"hosts": []}) if url.endswith("/v1/hosts") else await FakeHttp.get(env.http, url, params)

        env.http.get = no_hosts
        self.assertEqual(env.main(), 1)
        self.assertFalse(env.proof_path.exists())
        self.assertEqual(env.http.posts, [])


class ProofTests(unittest.TestCase):
    def orch(self, sup=None, kids=None, core=None):
        return rl.orchestration_proof(supervisor_items() if sup is None else sup, children() if kids is None else kids,
                                      closed_status() if core is None else core, root_id=SID)

    def assertReason(self, orch, text):
        self.assertTrue(any(text in r for r in orch["reasons"]), orch["reasons"])

    def test_closed_core_without_sdk_orchestration_is_not_genuine(self):
        core = closed_status()
        orch = rl.orchestration_proof([], {}, core, root_id=SID)
        status, reasons = rl.judge("completed", core, orch)
        self.assertEqual(status, "failed")
        self.assertTrue(any("delegations" in r for r in reasons))
        self.assertTrue(any("review_site" in r for r in reasons))

    def test_direct_fixture_actors_are_rejected(self):
        core = closed_status(actor_a="fixture-direct (not Omnigent)")
        reasons = rl.judge_core(core)
        self.assertTrue(any("not an MCP sidecar label" in r for r in reasons))
        self.assertEqual(rl.judge_core(closed_status()), [])

    def test_real_shape_none_agent_name_maps_through_dispatch_output(self):
        o = self.orch()
        self.assertEqual(o["reasons"], [])
        self.assertEqual(o["delegation_count"], 5)
        self.assertEqual(o["raw_dispatch_records"], 6)  # the stale-stamped first send stays visible
        rules = [(a["rule"], a["name"]) for a in o["collapsed_record_artifacts"]]
        self.assertIn(("stale_fifo_call_id", "sys_session_send"), rules)
        self.assertEqual(sum(r == "stale_fifo_call_id" for r, _ in rules), 7)  # 2 root + 5 children
        self.assertEqual([c["agent"] for c in sorted(o["child_sessions"], key=lambda c: c["delegation"])],
                         rl.DELEGATION_ORDER)
        self.assertEqual(len(o["specialist_tool_calls"]), 5)
        self.assertTrue(all(len(c["record_item_ids"]) == 2 for c in o["specialist_tool_calls"]))

    def test_conflicting_or_unknown_agent_fails(self):
        nodes = {1: child_node(1, tool=rl.EXPERIMENTER)}
        self.assertReason(self.orch(kids=children(nodes=nodes)), "tool 'inspection_experimenter' conflicts")
        nodes = {1: child_node(1, title=f"{rl.EXPERIMENTER}:{TITLE[1]}")}
        self.assertReason(self.orch(kids=children(nodes=nodes)), "title 'inspection_experimenter' conflicts")
        first = fifo_turn("resp_sup_1", [send(1, out_agent="inspection_other"), inbox()])
        sup = first + [x for x in supervisor_items() if not x["response_id"].endswith("_1")]
        self.assertReason(self.orch(sup=sup), "dispatch output names 'inspection_other'")
        order = ["inspection_other"] + rl.DELEGATION_ORDER[1:]
        o = self.orch(sup=supervisor_items(order), kids=children(nodes={1: child_node(1, agent="inspection_other")}))
        self.assertReason(o, "unknown agent 'inspection_other'")
        self.assertReason(o, "order")

    def test_distinct_extra_dispatch_stays_visible_and_fails(self):
        # The model really issued a second send (its own SDK tool_use id): never collapsed.
        o = self.orch(sup=supervisor_items(extra_first=[send(1)]))
        self.assertEqual(o["delegation_count"], 6)
        self.assertReason(o, "expected exactly 5")
        self.assertReason(o, "reuses child session")
        # Two dispatch records with distinct call ids and no SDK mirror evidence: two calls.
        rest = [x for x in supervisor_items() if not x["response_id"].endswith("_1")]
        o = self.orch(sup=plain_turn("resp_sup_1", [send(1), send(1)]) + rest)
        self.assertEqual(o["delegation_count"], 6)
        self.assertEqual([a for a in o["collapsed_record_artifacts"] if "child_session_id" not in a], [])
        self.assertReason(o, "expected exactly 5")
        # A stale-stamped dispatch record whose output names another child is not collapsed.
        sup = supervisor_items()
        stale = next(i for i, it in enumerate(sup) if it["type"] == "function_call_output" and CHILD[1] in it["output"])
        sup[stale] = {**sup[stale], "output": sup[stale]["output"].replace(CHILD[1], "conv_fixture_other")}
        self.assertReason(self.orch(sup=sup), "expected exactly 5")

    def test_duplicate_call_id_collapses_only_on_exact_sdk_duplicate(self):
        sup = supervisor_items()
        last = [x for x in sup if x.get("call_id") == "toolu_resp_sup_5_1"]  # the step-5 send and its output
        self.assertEqual(len(last), 2)
        o = self.orch(sup=sup + [{**x, "id": x["id"] + "dup"} for x in last])
        self.assertEqual([a["rule"] for a in o["collapsed_record_artifacts"]].count("exact_duplicate"), 1)
        self.assertEqual(o["delegation_count"], 5, o["reasons"])
        self.assertEqual(o["reasons"], [])
        changed = [{**x, "id": x["id"] + "chg"} for x in last]
        changed[0]["arguments"] = json.dumps({"agent": rl.ANALYST, "title": TITLE[5], "args": "Step 6"})
        o = self.orch(sup=sup + changed)
        self.assertEqual(o["delegation_count"], 6)
        self.assertReason(o, "expected exactly 5")

    def test_foreign_or_nested_children_fail(self):
        kids = children()
        kids["conv_fixture_foreign"] = {"summary": {**child_node(1), "id": "conv_fixture_foreign"},
                                        "items": fifo_turn("resp_foreign", [step_outputs()[1]])}
        o = self.orch(kids=kids)
        self.assertReason(o, "was not created by an accepted supervisor delegation")
        self.assertReason(o, "None called unexpected tool")
        self.assertReason(self.orch(kids=children(nodes={2: child_node(2, parent=CHILD[1])})),
                          "not a direct child of the root session")
        nested = children(items={1: fifo_turn("resp_child_1", [step_outputs()[1], send(2)])})
        self.assertReason(self.orch(kids=nested), "called unexpected tool None__sys_session_send")

    def test_missing_tool_results_fail(self):
        items = fifo_turn("resp_child_2", [step_outputs()[2]], search="select:review")
        items = [it for it in items if not (it["type"] == "function_call_output" and "result_id" in it["output"])]
        o = self.orch(kids=children(items={2: items}))
        self.assertReason(o, "has no tool result")
        self.assertReason(o, "has 2 review_site calls")  # without outputs nothing proves the records are one call

    def test_exactly_five_ordered_delegations(self):
        self.assertEqual(self.orch()["reasons"], [])
        swapped = [rl.EXPERIMENTER, rl.ANALYST, rl.ANALYST, rl.EXPERIMENTER, rl.ANALYST]
        self.assertReason(self.orch(sup=supervisor_items(swapped)), "order")
        self.assertReason(self.orch(sup=supervisor_items(rl.DELEGATION_ORDER[:4])), "expected exactly 5")

    def test_result_site_sequence_crosschecks(self):
        o = self.orch(kids=children(outputs=step_outputs(second_rid="ir_" + "c" * 16)))
        self.assertReason(o, "core paid results")
        self.assertReason(o, "did not return the analysis update")
        outs = step_outputs()
        name, args, out = outs[4]
        outs[4] = (name, {**args, "site_id": "W09-S999"}, out)
        o = self.orch(kids=children(outputs=outs))
        self.assertReason(o, "not the decision recorded in delegation 3")
        self.assertReason(o, "site/sequence differ from its arguments")
        outs = step_outputs()
        name, args, out = outs[5]
        outs[5] = (name, {"finalize": False}, {**out, "status": "decision_recorded"})
        o = self.orch(kids=children(outputs=outs))
        self.assertReason(o, "finalize=False")
        self.assertReason(o, "core closure")
        sup = supervisor_items() + plain_turn("resp_sup_x", [("sys_os_shell", {"cmd": "dir"}, "listing")])
        self.assertReason(self.orch(sup=sup), "supervisor called unexpected tool sys_os_shell")

    def test_core_judgement(self):
        self.assertEqual(rl.judge_core(None), ["core status unavailable"])
        one = closed_status()
        one["updates"] = one["updates"][:1]
        self.assertTrue(any("distinct analysis updates" in r for r in rl.judge_core(one)))
        dup = closed_status()
        dup["updates"][1]["observed_result_id"] = R1
        self.assertTrue(any("without an analysis update" in r for r in rl.judge_core(dup)))
        blocked = {**closed_status(), "blocked": {"reason": "incomplete_admission"}, "closed": None}
        self.assertGreaterEqual(len(rl.judge_core(blocked)), 2)
        with self.assertRaises(rl.LiveRunError):
            rl.check_fresh(closed_status())
        with self.assertRaises(rl.LiveRunError):
            rl.check_fresh({**fresh_status(), "budget": {"limit": 60.0}})
        rl.check_fresh(fresh_status())


class ReusedChildTests(unittest.TestCase):
    """Five delegations over two reused child conversations (live-9800b shape)."""

    def orch(self, sup=None, kids=None, core=None):
        return rl.orchestration_proof(supervisor_items(child_of=REUSE) if sup is None else sup,
                                      reused_children() if kids is None else kids,
                                      closed_status() if core is None else core, root_id=SID)

    def assertReason(self, orch, text):
        self.assertTrue(any(text in r for r in orch["reasons"]), orch["reasons"])

    def test_reused_children_map_each_turn_to_its_delegation(self):
        o = self.orch()
        self.assertEqual(o["reasons"], [])
        self.assertEqual(o["delegation_count"], 5)
        self.assertEqual(o["raw_dispatch_records"], 6)
        self.assertEqual([d["reuses_child_of_delegation"] for d in o["delegations"]], [None, None, 1, 2, 3])
        rows = {c["child_session_id"]: c for c in o["child_sessions"]}
        self.assertEqual(rows[AN_CHILD]["delegations"], [1, 3, 5])
        self.assertEqual(rows[EX_CHILD]["delegations"], [2, 4])
        self.assertEqual(sorted((c["delegation"], c["agent"]) for c in o["specialist_tool_calls"]),
                         list(enumerate(rl.DELEGATION_ORDER, 1)))
        self.assertEqual(len({c["sdk_call_id"] for c in o["specialist_tool_calls"]}), 5)
        self.assertEqual([a["delegation"] for a in o["collapsed_record_artifacts"] if "child_session_id" in a], [1, 2])
        self.assertEqual(rl.judge("completed", closed_status(), o), ("completed", []))

    def test_reused_children_end_to_end(self):
        env = Env("reuse", [("idle", True), ("idle", False)], items=supervisor_items(child_of=REUSE),
                  kids=reused_children())
        self.assertEqual(env.main(), 0)
        p = env.proof()
        self.assertEqual(p["status"], "completed", p["failure_reasons"])
        self.assertEqual(p["orchestration"]["delegation_count"], 5)
        self.assertTrue(all("_message" not in d for d in p["orchestration"]["delegations"]))

    def test_reuse_by_another_agent_fails(self):
        o = self.orch(sup=supervisor_items(child_of={**REUSE, 4: AN_CHILD}))
        self.assertReason(o, f"delegation 4 reuses child session {AN_CHILD} of delegation 1 for another agent")
        self.assertReason(o, "delegation 4 (inspection_experimenter) has 0 review_site calls")
        self.assertReason(o, f"child session {EX_CHILD} has 2 delegated turns but 1 accepted")
        # Child metadata must agree with the agent of its first delegation.
        kids = reused_children(nodes={EX_CHILD: child_node(2, id=EX_CHILD, tool=rl.ANALYST)})
        self.assertReason(self.orch(kids=kids), "tool 'inspection_analyst' conflicts")

    def test_duplicate_or_extra_primary_calls(self):
        # A genuine second analyze_and_plan (its own SDK tool_use id) in turn 3 stays a separate call.
        extra = reused_turn(3, extra=[step_outputs()[3]])
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), extra, reused_turn(5)]}))
        self.assertReason(o, "delegation 3 (inspection_analyst) has 2 analyze_and_plan calls")
        # An exact duplicate record (same call id, arguments and output) still collapses.
        t3 = reused_turn(3)
        dup = t3 + [{**x, "id": x["id"] + "dup"} for x in t3 if x["type"].startswith("function_call")]
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), dup, reused_turn(5)]}))
        self.assertEqual(o["reasons"], [])
        self.assertIn(("exact_duplicate", 3), [(a["rule"], a.get("delegation")) for a in o["collapsed_record_artifacts"]])
        # The same SDK call id answering two different delegations is not one call.
        t5 = reused_turn(5)
        t5 = [{**x, "call_id": "toolu_resp_child_3_0"} if "call_id" in x else x for x in t5]
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), reused_turn(3), t5]}))
        self.assertReason(o, "SDK call id toolu_resp_child_3_0 recurs in another turn")

    def test_missing_extra_or_misordered_turns_refuse_mapping(self):
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), reused_turn(3)]}))
        self.assertReason(o, f"delegation 5 reuses child session {AN_CHILD} without a matching ordered turn")
        self.assertReason(o, "delegation 3 (inspection_analyst) has 0 analyze_and_plan calls")  # nothing guessed
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), reused_turn(3), reused_turn(5),
                                                             reused_turn(5, text="Step 6", resp="resp_child_6")]}))
        self.assertReason(o, f"child session {AN_CHILD} has 4 delegated turns but 3 accepted")
        # Turn texts out of dispatch order: the mapping is refused, nothing is assigned.
        swapped = [reused_turn(1), reused_turn(3, text="Step 5"), reused_turn(5, text="Step 3")]
        o = self.orch(kids=reused_children(turns={AN_CHILD: swapped}))
        self.assertReason(o, f"child session {AN_CHILD} turn 2 is not the message dispatched by delegation 3")
        self.assertReason(o, "is not mapped to an accepted delegation")
        self.assertReason(o, "delegation 5 (inspection_analyst) has 0 analyze_and_plan calls")
        # Turns in order but the child finalized before planning: the step chain rejects it.
        early_final = [reused_turn(1), reused_turn(5, text="Step 3"), reused_turn(3, text="Step 5")]
        o = self.orch(kids=reused_children(turns={AN_CHILD: early_final}))
        self.assertReason(o, "delegation 3 analyze_and_plan finalize=True, expected False")
        self.assertReason(o, "core closure does not match")
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), reused_turn(3, resp="resp_child_1"),
                                                             reused_turn(5)]}))
        self.assertReason(o, "SDK response resp_child_1 spans 2 delegated turns")
        early = plain_turn("resp_child_0", [step_outputs()[1]]) + reused_turn(1)
        o = self.orch(kids=reused_children(turns={AN_CHILD: [early, reused_turn(3), reused_turn(5)]}))
        self.assertReason(o, "precedes every delegated turn")
        out_late = reused_turn(3)
        moved = [x for x in out_late if x["type"] == "function_call_output"]
        late = [[x for x in out_late if x not in moved], reused_turn(5) + moved]
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), *late]}))
        self.assertReason(o, "has its output in another turn")

    def test_foreign_or_nested_child_still_fails_with_reuse(self):
        kids = reused_children()
        kids["conv_fixture_foreign"] = {"summary": child_node(1, id="conv_fixture_foreign"),
                                        "items": reused_turn(1)}
        o = self.orch(kids=kids)
        self.assertReason(o, "conv_fixture_foreign (")
        self.assertReason(o, "was not created by an accepted supervisor delegation")
        kids = reused_children(nodes={EX_CHILD: child_node(2, id=EX_CHILD, parent=AN_CHILD)})
        self.assertReason(self.orch(kids=kids), "not a direct child of the root session")
        nested = reused_turn(3, extra=[send(4, child=EX_CHILD)])
        o = self.orch(kids=reused_children(turns={AN_CHILD: [reused_turn(1), nested, reused_turn(5)]}))
        self.assertReason(o, "called unexpected tool None__sys_session_send")


class SanitizeTests(unittest.TestCase):
    def test_recursive_sanitization(self):
        inner = json.dumps({"email": "who@example.test", "host_id": "host_secret_abcdef", "result_id": R1,
                            "path": str(ROOT / "runs" / "x.json")})
        raw = {
            "session_id": SID, "owner": "someone", "workspace": str(ROOT), "url": SERVER,
            "nested": [{"api_key": "k", "text": f"wrote {ROOT / 'a.json'} and /home/someone/x and C:\\\\Users\\\\x\\\\y"},
                       {"output": inner, "prefixed": "[System: returned: " + json.dumps({"runner_id": "rn_1", "ok": R2}) + "]",
                        "double": json.dumps(json.dumps({"access_token": "tok_secret", "v": 1})),
                        "secret": "token sk-ABCDEFGHIJKLMNOPQRSTUV and Bearer abcdefghijkl",
                        "runtime": "bound to runner_secret_123456"}],
        }
        out = rl.sanitize(raw, frozenset({"runner_secret_123456"}))
        dumped = json.dumps(out)
        for gone in ("someone", "who@example.test", "host_secret_abcdef", "rn_1", "tok_secret", "sk-ABCDEF",
                     "abcdefghijkl", "runner_secret_123456", "Users", str(ROOT).replace("\\", "\\\\")):
            self.assertNotIn(gone, dumped)
        self.assertNotIn("owner", out)
        self.assertNotIn("workspace", out)
        self.assertEqual(out["session_id"], SID)
        self.assertEqual(out["url"], SERVER)
        self.assertIn(R1, dumped)
        self.assertIn(R2, dumped)
        self.assertEqual(json.loads(out["nested"][1]["output"])["result_id"], R1)

    def test_written_proof_has_no_runtime_ids(self):
        env = Env("sanitized", [("idle", False)])
        self.assertEqual(env.main(), 0)
        text = env.proof_path.read_text(encoding="utf-8")
        for gone in ("runner_secret_123456", "host_secret_abcdef", "box-private-name", str(ROOT), ROOT.as_posix()):
            self.assertNotIn(gone, text)
        self.assertIn(R1, text)


class CliTests(unittest.TestCase):
    def test_refuses_other_servers_and_unfit_roots(self):
        env = Env("cli", [("idle", False)])
        for url in ("http://127.0.0.1:6767", "http://example.com:6771", "https://127.0.0.1:6771"):
            self.assertEqual(env.main("--server", url), 2)
        env.core.state = closed_status()
        self.assertEqual(env.main(), 2)  # not a fresh root
        self.assertFalse(env.proof_path.exists())
        other = Env("cli-sidecar", [("idle", False)])
        write_sidecars(other.agent, other.live, analyst_root=other.base / "elsewhere")
        self.assertEqual(other.main(), 2)
        self.assertEqual(other.http.posts, [])

    def test_existing_proof_refuses_second_session(self):
        env = Env("second", [("idle", False)])
        self.assertEqual(env.main(), 0)
        self.assertEqual(env.main(), 2)
        self.assertEqual(env.http.posts, [f"{SERVER}/v1/sessions"])


class BundleTests(unittest.TestCase):
    def test_static_bundle_shape(self):
        texts = {rl.AGENT_NAME: (ROOT / "inspection_agent" / "config.yaml").read_text(encoding="utf-8")}
        for a in (rl.ANALYST, rl.EXPERIMENTER):
            texts[a] = (ROOT / "inspection_agent" / "agents" / a / "config.yaml").read_text(encoding="utf-8")
        for name, t in texts.items():
            self.assertRegex(t, rf"(?m)^name: {name}$")
            self.assertIn("model: claude-opus-5-5", t)
            self.assertIn("reasoning_effort: medium", t)
            self.assertIn("harness: claude-sdk", t)
            self.assertRegex(t, r"(?m)^skills: none$")
            self.assertNotRegex(t, r"(?m)^\s*os_env\s*:")
            self.assertIn(f"limit: {rl.TOOL_CAPS[name]}", t)
        self.assertIn("- inspection_analyst\n    - inspection_experimenter", texts[rl.AGENT_NAME])
        ignore = (ROOT / "inspection_agent" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("agents/*/tools/mcp/inspection.yaml", ignore)

    @unittest.skipUnless(OMNI_PY.is_file(), "installed Omnigent tool interpreter not found")
    def test_bundle_validates_with_installed_omnigent(self):
        base = tmpdir("bundle")
        bundle = base / "inspection_agent"
        shutil.copytree(ROOT / "inspection_agent", bundle, ignore=shutil.ignore_patterns("tools"))
        write_sidecars(bundle, base / "live")
        code =("import sys, json; sys.path.insert(0, sys.argv[1]); import run_inspection_live as r; "
                "from pathlib import Path; rep = r.validate_bundle(Path(sys.argv[2])); print(json.dumps(rep['problems']))")
        direct = subprocess.run([str(OMNI_PY), "-c", code, str(ROOT / "scripts"), str(bundle)], capture_output=True,
                                text=True, encoding="utf-8", timeout=300, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(direct.returncode, 0, direct.stderr[-2000:])
        self.assertEqual(json.loads(direct.stdout.strip().splitlines()[-1]), [])


class LauncherTests(unittest.TestCase):
    TEXT = (ROOT / "scripts" / "launch_inspection.ps1").read_text(encoding="utf-8")

    def test_launcher_static_isolation(self):
        t = self.TEXT
        code = re.sub(r"<#.*?#>", "", t, flags=re.S)
        code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotRegex(code, r"omni(\.exe)?\s+stop")
        self.assertNotIn("6767", code)
        self.assertNotRegex(code, r"uv\s+sync")
        self.assertIn("$Port = 6771", code)
        self.assertIn("'.inspection-live-runtime'", code)
        starts = re.findall(r"Start-Process[^\n]*", code)
        self.assertEqual(len(starts), 2)
        self.assertTrue(all("-WindowStyle Hidden" in s for s in starts))
        self.assertIn("seed=9800, budget=120, max_reviews=4", code)

    def _identity(self, pid: int) -> dict:
        ps = ("$p = Get-Process -Id %d; $c = Get-CimInstance Win32_Process -Filter 'ProcessId=%d'; "
              "@{pid=%d; start_ticks=$p.StartTime.ToUniversalTime().Ticks; path=[string]$p.Path; command=[string]$c.CommandLine} "
              "| ConvertTo-Json -Compress") % (pid, pid, pid)
        out = subprocess.run([PWSH, "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout)

    def _stop(self, runtime: Path, recs: list[dict]) -> subprocess.CompletedProcess:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "inspection-pids.json").write_text(json.dumps({"url": SERVER, "processes": recs}), encoding="utf-8")
        return subprocess.run([PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(ROOT / "scripts" / "launch_inspection.ps1"), "-Stop", "-RuntimeDir", str(runtime)],
                              capture_output=True, text=True, timeout=120)

    def _sleeper(self, *args):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)", *args])

    @unittest.skipUnless(PWSH and os.name == "nt", "PowerShell on Windows required")
    def test_stop_only_kills_verified_owned_processes(self):
        base = tmpdir("pids")
        reused = self._sleeper("--port", "6771")
        foreign = self._sleeper("--port", "6767")
        owned = self._sleeper("--port", "6771")
        try:
            time.sleep(0.5)
            stale = {**self._identity(reused.pid), "start_ticks": 1, "role": "server"}  # PID reuse: other start time
            r1 = self._stop(base / "rt1", [stale])
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIsNone(reused.poll())
            wrong_port = {**self._identity(foreign.pid), "role": "host"}  # exact identity but not our port
            self._stop(base / "rt2", [wrong_port])
            self.assertIsNone(foreign.poll())
            mine = {**self._identity(owned.pid), "role": "server"}
            r3 = self._stop(base / "rt3", [mine])
            self.assertEqual(r3.returncode, 0, r3.stderr + r3.stdout)
            owned.wait(timeout=20)
            self.assertIsNotNone(owned.poll())
            self.assertFalse((base / "rt3" / "inspection-pids.json").exists())
            self.assertEqual((base / "rt3" / ".gitignore").read_text(encoding="utf-8").strip(), "*")
        finally:
            for p in (reused, foreign, owned):
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=20)


if __name__ == "__main__":
    unittest.main()
