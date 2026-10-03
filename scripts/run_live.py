"""Run ONE bounded live supervisor session against a prepared demo root and record proof.

Coordinator-operated (launch.ps1 -Live). Run with the Omnigent tool interpreter, which
ships ``omnigent_client``::

    <omnigent-python> scripts/run_live.py --server http://127.0.0.1:6767 --demo-root runs/demo/live-1001

Preconditions (checked, never repaired): the demo root was prepared by
``demo-prepare`` and its live run has no decision yet; both MCP sidecars point
FALSIFY_RUNS_DIR at that root (``launch.ps1 -Setup -RunsDir ...``); the local
server has exactly one registered ``falsify_supervisor`` and one online local host.
No credential or model probe is made.

Flow: GET /v1/agents and /v1/hosts -> JSON POST /v1/sessions {agent_id, host_id,
workspace, reasoning_effort: medium} -> session id saved immediately ->
SessionsChat.send(prompt) -> follow the whole asynchronous workflow (SSE tail of
sessions.stream plus a bounded GET poll of root status, sub-agent subtree and ledger)
until the root is idle with no busy sub-agent for --settle-grace seconds, or the
global --timeout -> full session and sub-agent snapshots -> ledger check of the live
run -> snapshot export. The first send stream ends at the supervisor's first
response ("waiting for the analyst"); that is never treated as the end. Nothing is
ever sent to the session after the one prompt.

Recovery (``--recover-session ID``): re-attach to the session named by this root's
existing proof without sending a prompt, creating a session or running an
experiment; follow it the same way, keep the raw prior proof under the local
live-proof directory and append the attempt to the proof's attempt history.

Outputs:
* ``<demo-root>/omnigent/session-proof.json``: sanitized (no credentials, account
  metadata, host ids or absolute paths); read by the snapshot export.
* ``.omnigent-runtime/live-proof/<session-id>/``: raw stream events and session
  items; local only (gitignored), never published.

Exit code 0 only when every observed supervisor response completed, the workflow
settled (root idle, no busy sub-agent, no root task error) AND the ledger shows the live run closed
complete with at least two analysis updates. A missing result is a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
AGENT_NAME = "falsify_supervisor"
LIVE_RUN_ID = "live-adaptive_idw_plus_distance-1001"
LIVE_BUDGET = 16
MIN_UPDATES = 2
PROOF_REL = Path("omnigent") / "session-proof.json"
PROMPT = (
    "Run the prepared falsify-lab live loop for seed 1001 exactly as your instructions define: "
    "analyst initial plan, experimenter executes the recorded point, analyst next plan, "
    "experimenter executes the recorded point, analyst finalizes and interprets. Then stop."
)
TERMINAL = {
    "response.completed": "completed",
    "response.failed": "failed",
    "response.incomplete": "incomplete",
    "response.cancelled": "cancelled",
}
SECRET_KEYS = {"owner", "user_id", "email", "authorization", "api_key", "token", "access_token", "refresh_token",
               "cookie", "credential", "credentials", "password", "secret", "host_id", "workspace", "cwd", "runner_id",
               "binding_token", "external_session_id"}
_ABS_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]{1,2}|\\\\|/(?:Users|home|tmp|var|private)/)[^\s'\",\]\}]*")
_SECRETISH = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)")


class LiveRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- sanitizing
def scrub_text(text: str) -> str:
    for p in {str(ROOT), str(ROOT).replace("\\", "/"), str(Path.home()), str(Path.home()).replace("\\", "/")}:
        text = text.replace(p, "<path>")
    text = _ABS_PATH.sub("<path>", text)
    return _SECRETISH.sub("<redacted>", text)


def sanitize(obj: Any) -> Any:
    """Drop account/credential keys and absolute paths from a JSON-like value."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str):
        # SDK function arguments/outputs can themselves contain serialized JSON.
        # Scrub its account fields before returning it as text to the proof.
        if obj.lstrip().startswith(("{", "[")):
            try:
                embedded = json.loads(obj)
            except (ValueError, TypeError):
                pass
            else:
                return json.dumps(sanitize(embedded), ensure_ascii=False)
        return scrub_text(obj)
    return obj


# ---------------------------------------------------------------------- local checks
def read_ledger(demo_root: Path) -> list[dict[str, Any]]:
    path = demo_root / "ledger" / "events.jsonl"
    if not path.is_file():
        raise LiveRunError("demo root has no ledger; run demo-prepare first")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def live_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary of the live run straight from the ledger (no falsify_lab import needed)."""
    mine = [e for e in events if e["payload"].get("run_id") == LIVE_RUN_ID]
    opened = next((e["payload"] for e in mine if e["type"] == "run_opened"), None)
    closed = next((e["payload"] for e in mine if e["type"] == "run_closed"), None)
    results = [e["payload"] for e in mine if e["type"] == "query_result"]
    tools = [e["payload"] for e in mine if e["type"] == "mcp_tool_call"]
    return {
        "prepared": any(e["type"] == "live_prepared" and e["payload"].get("run_id") == LIVE_RUN_ID for e in events),
        "campaign_kind": events[0]["payload"].get("kind") if events else None,
        "budget": opened["budget"] if opened else None,
        "decisions": [{"sequence": e["payload"]["sequence"], "selected_point_id": e["payload"]["selected_point_id"],
                       "actor": e["payload"].get("actor"), "candidates": len(e["payload"]["candidates"])}
                      for e in mine if e["type"] == "decision"],
        "search_results": [{"query_index": r["query_index"], "point_id": r["point_id"], "status": r["status"],
                            "result_id": r.get("result_id")} for r in results if r["phase"] == "search"],
        "updates": [{"update_index": e["payload"]["update_index"], "observed_result_id": e["payload"]["observed_result_id"],
                     "selection_changed": e["payload"]["selection_changed"], "actor": e["payload"]["actor"]}
                    for e in mine if e["type"] == "analysis_update"],
        "tool_calls": [{"tool": t["tool"], "actor": t["actor"], "status": t["status"], "result_ids": t.get("result_ids", [])}
                       for t in tools],
        "closed": closed,
        "logical_queries": len([e for e in mine if e["type"] == "query_admitted"]),
        "ledger_events": len(events),
    }


def check_prepared(demo_root: Path) -> dict[str, Any]:
    st = live_state(read_ledger(demo_root))
    check_live_root(st)
    if st["decisions"] or st["closed"]:
        raise LiveRunError("live run already has decisions or is closed; one live session per freshly prepared root")
    return st


def check_sidecars(demo_root: Path) -> None:
    for role in ("analyst", "experimenter"):
        path = ROOT / "agent" / "agents" / role / "tools" / "mcp" / "falsify.yaml"
        if not path.is_file():
            raise LiveRunError(f"{role} MCP sidecar missing; run launch.ps1 -Setup -RunsDir <demo-root>")
        text = path.read_text(encoding="utf-8")
        m = re.search(r'FALSIFY_RUNS_DIR:\s*"([^"]+)"', text)
        if not m or Path(m.group(1)).resolve() != demo_root.resolve():
            raise LiveRunError(f"{role} sidecar points at another root; re-run launch.ps1 -Setup -RunsDir <demo-root> and restart -Server")
        if f'FALSIFY_MCP_ROLE: "{role}"' not in text:
            raise LiveRunError(f"{role} sidecar lacks FALSIFY_MCP_ROLE; re-run launch.ps1 -Setup")


def judge(outcome: str, ledger: dict[str, Any]) -> tuple[str, list[str]]:
    """Final status: completed only with a completed turn and a complete, updated live run."""
    reasons = []
    if outcome != "completed":
        reasons.append(f"session turn ended {outcome}")
    closed = ledger.get("closed")
    if closed is None:
        reasons.append("live run not closed")
    elif closed["status"] != "complete":
        reasons.append(f"live run closed {closed['status']} ({closed['termination_reason']})")
    if len(ledger["updates"]) < MIN_UPDATES:
        reasons.append(f"{len(ledger['updates'])} adaptive updates < {MIN_UPDATES}")
    if any(not str(t["actor"]).startswith("Omnigent ") for t in ledger["tool_calls"]):
        reasons.append("a live tool call was not made through an Omnigent sidecar")
    if not reasons:
        return "completed", []
    return ("timeout" if outcome == "timeout" else "failed"), reasons


# ---------------------------------------------------------------------- session
def _items_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    calls, outputs, texts = [], {}, []
    for it in items:
        kind = it.get("type")
        if kind == "function_call":
            calls.append({"name": it.get("name"), "call_id": it.get("call_id"), "status": it.get("status"),
                          "arguments": str(it.get("arguments", ""))[:2000]})
        elif kind == "function_call_output":
            outputs[it.get("call_id")] = str(it.get("output", ""))[:4000]
        elif kind == "message" and it.get("role") == "assistant":
            for block in it.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    texts.append(str(block.get("text", "")))
    for c in calls:
        if c["name"] in {"ToolSearch", "sys_agent_list"}:
            c["output"] = "<omitted: local tool/agent discovery inventory; raw record retained privately>"
        else:
            c["output"] = outputs.get(c["call_id"])
    names: dict[str, int] = {}
    for c in calls:
        names[str(c["name"])] = names.get(str(c["name"]), 0) + 1
    return {"tool_calls": calls, "tool_call_counts": names, "assistant_text": "\n".join(texts)[-8000:]}


def _dump(ev: Any) -> dict[str, Any]:
    return ev if isinstance(ev, dict) else ev.model_dump(mode="json")


class Recorder:
    """Local raw event log plus every terminal supervisor response observed in one attempt."""

    def __init__(self, raw: Any, session_id: str) -> None:
        self.raw, self.session_id = raw, session_id
        self.responses: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.children: dict[str, dict[str, Any]] = {}
        self.root_status_events: list[dict[str, Any]] = []
        self.stream_errors: list[str] = []
        self.polls = 0

    def record(self, ev: Any, source: str) -> dict[str, Any]:
        d = _dump(ev)
        self.raw.write(json.dumps({"driver_source": source, **d}, default=str) + "\n")
        self.raw.flush()
        etype = d.get("type")
        if etype == "response.output_item.done" and (d.get("item") or {}).get("type") == "function_call":
            it = d["item"]
            self.stream_calls.append({"name": it.get("name"), "call_id": it.get("call_id"), "status": it.get("status"),
                                      "source": source})
        elif etype == "session.child_session.updated":
            self.children.setdefault(d.get("child_session_id"), {}).update(d.get("child") or {})
        elif etype == "session.status" and d.get("conversation_id") in (None, self.session_id):
            self.root_status_events.append({"status": d.get("status"), "source": source, "observed_at": _now()})
        if etype in TERMINAL:
            r = d.get("response") or {}
            if not (r.get("id") and any(x["response_id"] == r.get("id") for x in self.responses)):
                self.responses.append({"response_id": r.get("id"), "event": etype, "status": TERMINAL[etype],
                                       "usage": r.get("usage"), "source": source, "observed_at": _now()})
        return d


async def send_initial(chat: Any, prompt: str, rec: Recorder, deadline: float) -> tuple[str | None, str | None]:
    """Post the single prompt and record its stream. ``(None, None)`` means: follow the workflow.

    ``SessionsChat.send`` returns at the FIRST terminal response, which for this async
    supervisor is usually "waiting for the analyst"; it is never the end of the workflow.
    """
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            async for ev in chat.send(prompt):
                rec.record(ev, "initial_send")
    except TimeoutError:
        return "timeout", "initial send exceeded the global timeout"
    except Exception as exc:  # noqa: BLE001 - e.g. OmnigentError on session.status failed; recorded
        return "failed", f"{type(exc).__name__}: {exc}"
    return None, None


async def follow_workflow(sessions: Any, session_id: str, rec: Recorder, ledger_probe: Any, *, deadline: float,
                          poll_interval: float, settle_grace: float) -> dict[str, Any]:
    """Follow the whole asynchronous workflow (child turns, wake turns) without sending anything.

    A background SSE tail of ``sessions.stream`` records every later response; a bounded
    GET poll (root status, sub-agent subtree, ledger) decides the end. The workflow is
    settled only when the root is idle AND no sub-agent is busy continuously for
    ``settle_grace`` seconds (a child can finish before the root's wake turn starts).
    """
    stop = asyncio.Event()

    async def tail() -> None:
        while not stop.is_set():
            try:
                async for ev in sessions.stream(session_id):
                    rec.record(ev, "follow_stream")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reattach; the poll still decides the outcome
                rec.stream_errors.append(f"{type(exc).__name__}: {scrub_text(str(exc))[:200]}")
            await asyncio.sleep(poll_interval)  # stream closed or failed: reattach

    task = asyncio.create_task(tail())
    settled_since: float | None = None
    last: dict[str, Any] = {}
    try:
        while True:
            snap = await sessions.get(session_id)
            subtree_busy = bool(await sessions.subtree_busy(session_id))
            ledger = ledger_probe()
            rec.polls += 1
            last = {"root_status": snap.status, "subtree_busy": subtree_busy, "ledger_closed": ledger.get("closed"),
                    "updates": len(ledger.get("updates") or []), "polled_at": _now()}
            if snap.status == "failed":
                return {"outcome": "failed", "error": f"root session failed: {snap.last_task_error}", "last_poll": last}
            now = time.monotonic()
            if snap.status != "idle" or subtree_busy:
                settled_since = None
            else:
                settled_since = now if settled_since is None else settled_since
                if now - settled_since >= settle_grace:
                    if ledger.get("closed") is not None:
                        return {"outcome": "settled", "error": None, "last_poll": last}
                    return {"outcome": "failed", "last_poll": last,
                            "error": f"root idle and no sub-agent busy for {settle_grace} s, but the live run is not closed"}
            if now >= deadline:
                return {"outcome": "timeout", "error": "workflow not settled within the global timeout", "last_poll": last}
            await asyncio.sleep(max(0.0, min(poll_interval, deadline - now)))
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - tail shutdown only
            pass


def workflow_outcome(follow: dict[str, Any], responses: list[dict[str, Any]], root_status: str,
                     last_task_error: Any) -> tuple[str, str | None]:
    if follow["outcome"] != "settled":
        return follow["outcome"], follow["error"]
    if root_status == "failed" or last_task_error:
        return "failed", f"root session {root_status}; last_task_error {last_task_error}"
    if not responses:
        return "failed", "no terminal supervisor response observed"
    bad = [r for r in responses if r["status"] != "completed"]
    if bad:
        return bad[-1]["status"], f"supervisor response {bad[-1]['response_id']} ended {bad[-1]['status']}"
    return "completed", None


async def drive(sessions: Any, session_id: str, rec: Recorder, ledger_probe: Any, *, deadline: float,
                poll_interval: float, settle_grace: float, chat: Any = None, prompt: str | None = None,
                interrupt_on_timeout: bool = False) -> dict[str, Any]:
    """Initial send (only when ``chat`` is given), then follow the workflow to its real end."""
    hard, error = (None, None)
    if chat is not None:
        hard, error = await send_initial(chat, prompt or "", rec, deadline)
        if hard is None and not rec.responses:
            rec.stream_errors.append("initial send stream ended without a terminal event")
    if hard is None:
        follow = await follow_workflow(sessions, session_id, rec, ledger_probe, deadline=deadline,
                                       poll_interval=poll_interval, settle_grace=settle_grace)
    else:
        follow = {"outcome": hard, "error": error, "last_poll": None}
    if follow["outcome"] == "timeout" and interrupt_on_timeout:
        try:
            await sessions.interrupt(session_id)
        except Exception as exc:  # noqa: BLE001 - record, the timeout stays the outcome
            follow["error"] += f"; interrupt failed: {type(exc).__name__}"
    snap = await sessions.get(session_id)
    outcome, err = workflow_outcome(follow, rec.responses, snap.status, snap.last_task_error)
    return {"outcome": outcome, "error": err, "follow": follow}


async def _all_items(sessions: Any, session_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = None
    while True:
        page = await sessions.list_items(session_id, limit=1000, after=after)
        items.extend(page)
        if len(page) < 1000 or not page[-1].get("id"):
            return items
        after = page[-1]["id"]


async def collect_snapshot(sessions: Any, session_id: str) -> dict[str, Any]:
    """Full session, items and sub-agent subtree, gathered before judging."""
    snap = await sessions.get(session_id)
    items = await _all_items(sessions, session_id)
    tree = await sessions.child_sessions_tree(session_id)
    child_rows, raw_children = [], {}
    for node in tree:
        cid = node.get("id")
        c_items = await _all_items(sessions, cid)
        c_snap = await sessions.get(cid)
        raw_children[cid] = {"summary": node, "items": c_items}
        child_rows.append({
            "session_id": cid, "parent_id": node.get("parent_id"), "agent_name": node.get("agent_name"),
            "tool": node.get("tool"), "status": c_snap.status, "current_task_status": node.get("current_task_status"),
            "last_total_tokens": c_snap.last_total_tokens, "llm_model": c_snap.llm_model,
            "harness": c_snap.harness, "reasoning_effort": c_snap.reasoning_effort,
            "last_task_error": c_snap.last_task_error, **_items_summary(c_items),
        })
    return {"snap": snap, "items": items, "child_rows": child_rows, "raw_children": raw_children}


async def drive_and_collect(sessions: Any, session_id: str, demo_root: Path, raw_dir: Path, args: argparse.Namespace,
                            *, chat: Any = None, prompt: str | None = None, interrupt_on_timeout: bool = False,
                            prior_responses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with open(raw_dir / "stream-events.jsonl", "w", encoding="utf-8") as raw:
        rec = Recorder(raw, session_id)
        rec.responses.extend(prior_responses or [])
        result = await drive(sessions, session_id, rec, lambda: live_state(read_ledger(demo_root)),
                             deadline=t0 + args.timeout, poll_interval=args.poll_interval,
                             settle_grace=args.settle_grace, chat=chat, prompt=prompt,
                             interrupt_on_timeout=interrupt_on_timeout)
    elapsed = time.monotonic() - t0
    col = await collect_snapshot(sessions, session_id)
    _write_json(raw_dir / "session-items.json", {"session": col["items"], "children": col["raw_children"],
                                                 "child_stream_updates": rec.children})
    snap = col["snap"]
    last = rec.responses[-1] if rec.responses else {}
    return {
        "turn_outcome": result["outcome"],
        "error": result["error"],
        "elapsed_seconds": round(elapsed, 3),
        "finished_at": _now(),
        "response_id": last.get("response_id"),
        "response_status": last.get("status"),
        "usage": last.get("usage"),
        "responses": rec.responses,
        "usage_note": ("responses lists every terminal supervisor response the driver observed (initial send, "
                       "later wake turns, and any carried over from a prior attempt), each with its own usage; "
                       "usage/response_id are the last one. A response that ended while no driver was attached "
                       "is not observed. Sub-agent rows carry each child's last_total_tokens. "
                       "Subscription use is not converted to money."),
        "workflow_follow": {"outcome": result["follow"]["outcome"], "polls": rec.polls,
                            "poll_interval_seconds": args.poll_interval, "settle_grace_seconds": args.settle_grace,
                            "last_poll": result["follow"]["last_poll"], "root_status_events": rec.root_status_events[-100:],
                            "stream_errors": rec.stream_errors[-20:]},
        "session": {"status": snap.status, "llm_model": snap.llm_model, "harness": snap.harness,
                    "reasoning_effort": snap.reasoning_effort, "last_total_tokens": snap.last_total_tokens,
                    "last_task_error": snap.last_task_error},
        "supervisor": {**_items_summary(col["items"]), "stream_function_calls": rec.stream_calls},
        "sub_agents": col["child_rows"],
    }


async def _check_health(http: Any, server: str) -> None:
    health = await http.get(f"{server}/health")
    if health.status_code != 200:
        raise LiveRunError(f"server health check failed ({health.status_code})")


async def run_session(args: argparse.Namespace, demo_root: Path, proof_path: Path, raw_dir: Path) -> dict[str, Any]:
    from omnigent_client import OmnigentClient, SessionsChat

    proof: dict[str, Any] = {"run_id": LIVE_RUN_ID, "agent": AGENT_NAME, "status": "starting", "started_at": _now()}
    async with OmnigentClient(base_url=args.server) as client:
        http = client._http  # carries the SDK's trusted-origin header; loopback only
        await _check_health(http, args.server)
        agents = (await http.get(f"{args.server}/v1/agents", params={"limit": 1000})).json().get("data", [])
        matches = [a for a in agents if isinstance(a, dict) and a.get("name") == AGENT_NAME]
        if len(matches) != 1:
            raise LiveRunError(f"expected exactly one registered {AGENT_NAME}, found {len(matches)}; restart launch.ps1 -Server")
        hosts = (await http.get(f"{args.server}/v1/hosts")).json().get("hosts", [])
        online = [h for h in hosts if isinstance(h, dict) and h.get("status") == "online" and not h.get("sandbox_provider")]
        if args.host_id:
            online = [h for h in online if h.get("host_id") == args.host_id]
        if len(online) != 1:
            raise LiveRunError(f"expected exactly one online local host, found {len(online)}; pass --host-id")
        body = {"agent_id": matches[0]["id"], "host_id": online[0]["host_id"], "workspace": str(ROOT),
                "reasoning_effort": "medium", "title": "falsify-lab live loop seed 1001"}
        resp = await http.post(f"{args.server}/v1/sessions", json=body)
        if resp.status_code >= 300:
            raise LiveRunError(f"session create failed ({resp.status_code}): {scrub_text(resp.text[:500])}")
        session_id = str(resp.json()["id"])
        proof.update({"session_id": session_id, "agent_id": matches[0]["id"], "status": "running",
                      "session_created_at": _now(), "host": "online local foreground host (id kept local)"})
        _write_json(proof_path, proof)  # saved immediately: an interrupted run still names its session
        session = await client.sessions.get(session_id)
        chat = SessionsChat(namespace=client.sessions, files_uploader=None, files_getter=None, session=session)
        proof.update(await drive_and_collect(client.sessions, session_id, demo_root, raw_dir, args, chat=chat,
                                             prompt=PROMPT, interrupt_on_timeout=True))
    return proof


# ---------------------------------------------------------------------- recovery
class RecoveryRefused(LiveRunError):
    """A recovery precondition failed; nothing was written."""


def check_live_root(st: dict[str, Any]) -> None:
    if st["campaign_kind"] != "demo":
        raise LiveRunError(f"campaign kind is {st['campaign_kind']!r}, not a demo campaign")
    if not st["prepared"]:
        raise LiveRunError("live run is not prepared; run demo-prepare first")
    if st["budget"] != LIVE_BUDGET:
        raise LiveRunError(f"live run budget is {st['budget']}, not {LIVE_BUDGET}; prepare a fresh demo root")


def check_recovery(prior: dict[str, Any], session_id: str, st: dict[str, Any]) -> None:
    """The root's prior proof must name this session and its ledger must extend that proof's live run."""
    if prior.get("session_id") != session_id:
        raise RecoveryRefused("prior proof in this demo root names another session; recovery refused")
    if prior.get("run_id") != LIVE_RUN_ID or prior.get("agent") != AGENT_NAME:
        raise RecoveryRefused("prior proof is not for this live run/agent; recovery refused")
    try:
        check_live_root(st)
    except LiveRunError as exc:
        raise RecoveryRefused(str(exc)) from None
    pl = prior.get("ledger_live_run")
    if pl:
        if pl.get("campaign_kind") != st["campaign_kind"] or pl.get("budget") != st["budget"]:
            raise RecoveryRefused("prior proof's ledger run differs from this root's live run; recovery refused")
        if (pl.get("ledger_events", 0) > st["ledger_events"]
                or st["decisions"][:len(pl.get("decisions", []))] != pl.get("decisions", [])
                or st["search_results"][:len(pl.get("search_results", []))] != pl.get("search_results", [])):
            raise RecoveryRefused("ledger does not extend the prior proof's live run; recovery refused")


def prior_attempts(prior: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(prior.get("attempts"), list):
        return list(prior["attempts"])
    return [{"attempt": 1, "mode": "send", "status": prior.get("status"), "turn_outcome": prior.get("turn_outcome"),
             "failure_reasons": prior.get("failure_reasons"), "finished_at": prior.get("finished_at"),
             "response_ids": [prior["response_id"]] if prior.get("response_id") else [],
             "note": "reconstructed from the prior proof's top-level fields"}]


def prior_responses(prior: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(prior.get("responses"), list):
        return [{**r, "from_prior_attempt": True} for r in prior["responses"]]
    if not prior.get("response_id"):
        return []
    status = prior.get("response_status") or "unknown"
    return [{"response_id": prior["response_id"], "event": f"response.{status}", "status": status,
             "usage": prior.get("usage"), "source": "initial_send", "observed_at": prior.get("finished_at"),
             "from_prior_attempt": True}]


async def run_recovery(sessions: Any, args: argparse.Namespace, demo_root: Path, proof_path: Path, raw_dir: Path,
                       prior: dict[str, Any]) -> dict[str, Any]:
    """Re-attach to an existing session: no prompt, no new session, no experiment, no interrupt."""
    session_id = args.recover_session
    try:
        snap = await sessions.get(session_id)
    except Exception as exc:  # noqa: BLE001 - nothing written yet
        raise RecoveryRefused(f"session lookup failed: {type(exc).__name__}") from None
    if prior.get("agent_id") and snap.agent_id != prior["agent_id"]:
        raise RecoveryRefused("session is bound to another agent than the prior proof; recovery refused")
    if not prior.get("agent_id") and snap.agent_name != AGENT_NAME:
        raise RecoveryRefused(f"session agent is not {AGENT_NAME}; recovery refused")
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(proof_path, raw_dir / "prior-session-proof.json")  # raw prior proof kept before overwrite
    proof = {k: prior[k] for k in ("run_id", "agent", "started_at", "session_id", "agent_id", "session_created_at", "host")
             if k in prior}
    proof.update({"status": "recovering", "recovered_at": _now(), "attempts": prior_attempts(prior)})
    try:
        proof.update(await drive_and_collect(sessions, session_id, demo_root, raw_dir, args,
                                             prior_responses=prior_responses(prior)))
    except Exception as exc:  # noqa: BLE001 - recorded as a failed recovery attempt, never success
        proof.update({"turn_outcome": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": _now()})
    return proof


async def recover_session(args: argparse.Namespace, demo_root: Path, proof_path: Path, raw_dir: Path,
                          prior: dict[str, Any]) -> dict[str, Any]:
    from omnigent_client import OmnigentClient

    async with OmnigentClient(base_url=args.server) as client:
        try:
            await _check_health(client._http, args.server)
        except Exception as exc:  # noqa: BLE001 - nothing written yet
            raise RecoveryRefused(f"server not reachable: {type(exc).__name__}: {exc}") from None
        return await run_recovery(client.sessions, args, demo_root, proof_path, raw_dir, prior)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)


def export_snapshot(demo_root: Path) -> dict[str, Any]:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = ROOT / ".venv" / "bin" / "python"
    if not py.is_file():
        return {"ok": False, "error": "project venv missing; run launch.ps1 -Setup, then cli export"}
    proc = subprocess.run([str(py), "-m", "falsify_lab.cli", "export", "--root", str(demo_root), "--focus-run", LIVE_RUN_ID],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    return {"ok": proc.returncode == 0, "returncode": proc.returncode}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default="http://127.0.0.1:6767")
    ap.add_argument("--demo-root", required=True, type=Path)
    ap.add_argument("--host-id", help="pick this online host when several are online")
    ap.add_argument("--timeout", type=float, default=1500.0,
                    help="global seconds for the whole workflow (initial send plus follow-up) before it counts as timed out")
    ap.add_argument("--poll-interval", type=float, default=5.0, help="seconds between GET status polls")
    ap.add_argument("--settle-grace", type=float, default=60.0,
                    help="seconds the root must stay idle with no busy sub-agent before the workflow counts as settled")
    ap.add_argument("--recover-session", metavar="SESSION_ID",
                    help="re-attach to the session named by this root's existing proof; sends nothing, creates nothing")
    args = ap.parse_args(argv)
    if not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", args.server.rstrip("/")):
        print(json.dumps({"ok": False, "error": "only a loopback Omnigent server is allowed"}))
        return 2
    args.server = args.server.rstrip("/")
    demo_root = args.demo_root.resolve()
    proof_path = demo_root / PROOF_REL
    recovering = args.recover_session is not None
    prior: dict[str, Any] = {}
    try:
        if recovering:
            if not proof_path.is_file():
                raise RecoveryRefused("this demo root has no session proof; nothing to recover")
            prior = json.loads(proof_path.read_text(encoding="utf-8"))
            check_recovery(prior, args.recover_session, live_state(read_ledger(demo_root)))
        else:
            if proof_path.exists():
                raise LiveRunError("this demo root already has a session proof; one live session per prepared root "
                                   "(use --recover-session with that proof's session id to re-attach)")
            check_prepared(demo_root)
        check_sidecars(demo_root)
    except LiveRunError as exc:
        print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(str(exc))}, indent=2))
        return 2
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}" + ("-recover" if recovering else "")
    raw_dir = ROOT / ".omnigent-runtime" / "live-proof" / stamp
    try:
        if recovering:
            proof = asyncio.run(recover_session(args, demo_root, proof_path, raw_dir, prior))
        else:
            proof = asyncio.run(run_session(args, demo_root, proof_path, raw_dir))
    except RecoveryRefused as exc:  # before attaching: the prior proof is untouched
        print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(str(exc))}, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never turned into success
        if recovering:  # run_recovery records failures after attaching; this one left the prior proof as it was
            print(json.dumps({"ok": False, "status": "recovery_failed", "proof_written": False,
                              "error": scrub_text(f"{type(exc).__name__}: {exc}")}, indent=2))
            return 1
        proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.exists() else {"run_id": LIVE_RUN_ID, "agent": AGENT_NAME}
        proof.update({"turn_outcome": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": _now()})
        if "session_id" not in proof:
            # No session was created: leave no proof file, so the root stays usable.
            print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(proof["error"])}, indent=2))
            return 1
    ledger = live_state(read_ledger(demo_root))
    status, reasons = judge(proof.get("turn_outcome", "failed"), ledger)
    attempts = proof.get("attempts") if isinstance(proof.get("attempts"), list) else []
    attempts = attempts + [{"attempt": len(attempts) + 1, "mode": "recover" if recovering else "send", "status": status,
                            "turn_outcome": proof.get("turn_outcome"), "error": proof.get("error"),
                            "failure_reasons": reasons, "finished_at": proof.get("finished_at"),
                            "response_ids": [r.get("response_id") for r in proof.get("responses") or []
                                             if not r.get("from_prior_attempt")]}]
    proof.update({"status": status, "failure_reasons": reasons, "ledger_live_run": ledger, "attempts": attempts,
                  "raw_events_local_only": True, "data_label": "real Omnigent SDK session; ngspice demo campaign"})
    _write_json(proof_path, sanitize(proof))
    exported = export_snapshot(demo_root)
    print(json.dumps({"ok": status == "completed", "status": status, "session_id": proof.get("session_id"),
                      "mode": "recover" if recovering else "send", "attempt": len(attempts),
                      "failure_reasons": reasons, "proof": str(PROOF_REL), "snapshot_export": exported,
                      "live_run": {"closed": ledger["closed"], "updates": len(ledger["updates"]),
                                   "logical_queries": ledger["logical_queries"]}}, indent=2, default=str))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
