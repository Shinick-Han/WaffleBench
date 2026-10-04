"""Newline-delimited stdio JSON-RPC MCP server for the paid inspection live session.

``python -m inspection_live.mcp_server`` with environment only:

- ``INSPECTION_LIVE_ROOT``: one root made by ``inspection_live.prepare``.
- ``INSPECTION_MCP_ROLE``: ``analyst`` (analyze_and_plan, read_result) or ``experimenter``
  (review_site, read_result). Any other value exposes no tools.

Tools take no root, path, shell, seed or scenario argument; unknown arguments are refused.
Sibling role processes share the session's cross-process lock. The stored actor is a sidecar
role label only (``mcp-sidecar:<role>``); genuine Omnigent attribution needs the runner's own
SDK session/function-call proof.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspection_live.session import InspectionLive, LiveError, clean_message  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
TOOLS = {
    "analyze_and_plan": {
        "description": ("Analyst: record one analysis_update per new paid result and record (or return the pending) "
                        "deterministic cb400_route_full decision with >=2 candidates, selected_site_id, decision_sequence, "
                        "score parts, evidence_result_ids and remaining budget. finalize=true needs >=2 executed reviews "
                        "and no pending decision; it shows an unrecorded preview and closes the run."),
        "inputSchema": {"type": "object", "properties": {"finalize": {"type": "boolean", "default": False}},
                        "additionalProperties": False},
    },
    "review_site": {
        "description": ("Experimenter: pay for exactly the pending recorded selection (site_id and decision_sequence copied "
                        "from the analyst's decision). Full CU reservation precedes the sensor; a failed/missing first "
                        "attempt is retried once and billed. Anything else is denied without observation."),
        "inputSchema": {"type": "object", "properties": {"site_id": {"type": "string"}, "decision_sequence": {"type": "integer"}},
                        "required": ["site_id", "decision_sequence"], "additionalProperties": False},
    },
    "read_result": {
        "description": "Read one already observed paid result by result_id. Unknown or unobserved IDs are denied.",
        "inputSchema": {"type": "object", "properties": {"result_id": {"type": "string"}},
                        "required": ["result_id"], "additionalProperties": False},
    },
}
ROLE_TOOLS = {"analyst": ("analyze_and_plan", "read_result"), "experimenter": ("review_site", "read_result")}


class ToolDenied(Exception):
    pass


def _role() -> str:
    return os.environ.get("INSPECTION_MCP_ROLE", "")


_SESSION: InspectionLive | None = None


def _session() -> InspectionLive:
    global _SESSION
    if _SESSION is None:
        root = os.environ.get("INSPECTION_LIVE_ROOT")
        if not root:
            raise ToolDenied("INSPECTION_LIVE_ROOT is not set")
        _SESSION = InspectionLive(root)
    return _SESSION


def _check_args(tool: str, args: Any) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolDenied("arguments must be an object")
    schema = TOOLS[tool]["inputSchema"]
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ToolDenied(f"unknown arguments: {sorted(unknown)}")
    missing = set(schema.get("required", [])) - set(args)
    if missing:
        raise ToolDenied(f"missing arguments: {sorted(missing)}")
    for name, value in args.items():
        kind = schema["properties"][name]["type"]
        ok = {"boolean": isinstance(value, bool), "string": isinstance(value, str),
              "integer": isinstance(value, int) and not isinstance(value, bool)}[kind]
        if not ok:
            raise ToolDenied(f"argument {name} must be {kind}")
    return args


def call_tool(tool: str, args: Any) -> dict:
    role = _role()
    if tool not in ROLE_TOOLS.get(role, ()):
        raise ToolDenied(f"tool {tool} is not available to role {role or '<unset>'}")
    args = _check_args(tool, args)
    actor = f"mcp-sidecar:{role}"
    s = _session()
    if tool == "analyze_and_plan":
        return s.analyze_and_plan(args.get("finalize", False), actor=actor)
    if tool == "review_site":
        return s.review_site(args["site_id"], args["decision_sequence"], actor=actor)
    return s.read_result(args["result_id"], actor=actor)


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
        return None  # notifications (initialized, cancelled) need no reply
    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return ok({"protocolVersion": version if isinstance(version, str) else PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "inspection-live", "version": "1"}})
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
        except (ToolDenied, LiveError) as exc:
            return ok(_tool_result({"error": clean_message(str(exc))}, True))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, never a crash of the server
            return ok(_tool_result({"error": f"{type(exc).__name__}: {clean_message(str(exc))}"}, True))
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
