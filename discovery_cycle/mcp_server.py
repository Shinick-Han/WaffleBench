"""Newline-delimited stdio JSON-RPC MCP server for the posthoc discovery cycle.

``python -m discovery_cycle.mcp_server`` with environment only:

- ``DISCOVERY_CYCLE_ROOT``: one root made by ``discovery_cycle.prepare``.
- ``DISCOVERY_CYCLE_ROLE``: ``analyst`` (read_context, record_plan, record_update), ``experimenter``
  (read_context, run_experiment) or ``supervisor`` (read_context, finalize). ``status`` is read-only
  for every role. Any other value exposes no tools.

Tools take no root or path argument; unknown arguments are refused. Every catalog test is a
pre-authorized read-only computation over the frozen copied posthoc file (privileged posthoc oracle
evaluation, never production selection). The stored actor is ``mcp-sidecar:<role>``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discovery_cycle.core import CycleError, DiscoveryCycle  # noqa: E402
from inspection_live.session import clean_message  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR}
TOOLS = {
    "read_context": {
        "description": ("Primary result, data label, budget (2), the read-only catalog (descriptions/cost/feasibility, "
                        "no result values) and prior completed results with their updates."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "record_plan": {
        "description": ("Analyst: record one plan with >=2 distinct available catalog candidate IDs, the selected one, "
                        "human-readable hypothesis/expected_learning/reason and evidence_result_ids equal to all prior "
                        "completed result IDs. Outside the catalog is refused with approval_required."),
        "inputSchema": {"type": "object", "properties": {
            "candidates": _IDS, "selected_test_id": _STR, "hypothesis": _STR, "expected_learning": _STR,
            "reason": _STR, "evidence_result_ids": _IDS},
            "required": ["candidates", "selected_test_id", "hypothesis", "expected_learning", "reason",
                         "evidence_result_ids"], "additionalProperties": False},
    },
    "run_experiment": {
        "description": ("Experimenter: run exactly the pending plan's read-only computation. Admission is recorded "
                        "before compute; a failure blocks the cycle with no retry."),
        "inputSchema": {"type": "object", "properties": {"plan_id": _STR}, "required": ["plan_id"],
                        "additionalProperties": False},
    },
    "record_update": {
        "description": ("Analyst: bind the latest result and store an interpretation, next hypothesis and next "
                        "experiment as PROPOSED (not executed)."),
        "inputSchema": {"type": "object", "properties": {
            "result_id": _STR, "interpretation": _STR, "next_hypothesis": _STR, "next_experiment": _STR},
            "required": ["result_id", "interpretation", "next_hypothesis", "next_experiment"],
            "additionalProperties": False},
    },
    "finalize": {
        "description": "Supervisor: close the cycle after two results, two updates and no pending plan.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "status": {
        "description": "Read-only phase, budget, IDs and head hash.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}
ROLE_TOOLS = {"analyst": ("read_context", "record_plan", "record_update", "status"),
              "experimenter": ("read_context", "run_experiment", "status"),
              "supervisor": ("read_context", "finalize", "status")}


class ToolDenied(Exception):
    pass


def _role() -> str:
    return os.environ.get("DISCOVERY_CYCLE_ROLE", "")


_CYCLE: DiscoveryCycle | None = None


def _cycle() -> DiscoveryCycle:
    global _CYCLE
    if _CYCLE is None:
        root = os.environ.get("DISCOVERY_CYCLE_ROOT")
        if not root:
            raise ToolDenied("DISCOVERY_CYCLE_ROOT is not set")
        _CYCLE = DiscoveryCycle(root)
    return _CYCLE


def _check_args(tool: str, args: Any) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolDenied("arguments must be an object")
    schema = TOOLS[tool]["inputSchema"]
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ToolDenied(f"unknown arguments: {sorted(unknown)[:5]}")
    missing = set(schema.get("required", [])) - set(args)
    if missing:
        raise ToolDenied(f"missing arguments: {sorted(missing)}")
    for name, value in args.items():
        kind = schema["properties"][name]["type"]
        ok = (isinstance(value, str) if kind == "string"
              else isinstance(value, list) and all(isinstance(v, str) for v in value))
        if not ok:
            raise ToolDenied(f"argument {name} must be {kind}")
    return args


def call_tool(tool: str, args: Any) -> dict:
    role = _role()
    if tool not in ROLE_TOOLS.get(role, ()):
        raise ToolDenied(f"tool {tool} is not available to role {role or '<unset>'}")
    args = _check_args(tool, args)
    actor = f"mcp-sidecar:{role}"
    c = _cycle()
    if tool == "status":
        return c.status()
    if tool == "read_context":
        return c.read_context(actor)
    if tool == "finalize":
        return c.finalize(actor)
    return getattr(c, tool)(**args, actor=actor)


def _tool_result(payload: dict, error: bool) -> dict:
    text = json.dumps(payload, sort_keys=True, allow_nan=False)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": error}


def handle(msg: Any) -> dict | None:
    """One JSON-RPC message in, one response (or None for notifications) out."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None
    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return ok({"protocolVersion": version if isinstance(version, str) else PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "discovery-cycle", "version": "1"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, **TOOLS[n]} for n in ROLE_TOOLS.get(_role(), ())]})
    if method == "tools/call":
        if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "invalid tools/call params"}}
        if params["name"] not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            return ok(_tool_result(call_tool(params["name"], params.get("arguments")), False))
        except CycleError as exc:
            return ok(_tool_result({"error": clean_message(str(exc))[:500], "code": exc.code}, True))
        except ToolDenied as exc:
            return ok(_tool_result({"error": clean_message(str(exc))[:500], "code": "role_denied"}, True))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, never a crash of the server
            return ok(_tool_result({"error": f"{type(exc).__name__}: {clean_message(str(exc))[:500]}"}, True))
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply, allow_nan=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    serve()
