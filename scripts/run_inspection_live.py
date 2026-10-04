"""Run ONE bounded Omnigent inspection live session against a freshly prepared live root.

Coordinator-operated (``scripts/launch_inspection.ps1 -Live``). Run with the Omnigent
tool interpreter, which ships ``omnigent_client``::

    <omnigent-python> scripts/run_inspection_live.py --server http://127.0.0.1:6771 --live-root <live-root>

Separate live demonstration (INSPECTION_LIVE_CONTRACT.md), never the v3 primary
benchmark. The frozen deterministic planner chooses every site; Omnigent only carries
decision sequences, site IDs and result IDs between ``inspection_analyst`` and
``inspection_experimenter``.

Preconditions (checked, never repaired): the live root was made by
``inspection_live.prepare`` and has no decision, admission or closure yet; both rendered
MCP sidecars point INSPECTION_LIVE_ROOT at that root with their own role; the loopback
server on port 6771 has exactly one registered ``inspection_supervisor`` and one online
local host. Core state is read only through ``InspectionLive(root).status()`` in the
sidecar interpreter (a subprocess), never from private files.

Flow: GET /v1/agents and /v1/hosts -> JSON POST /v1/sessions -> session id saved to the
proof immediately -> exactly one ``SessionsChat.send(prompt)`` -> follow the whole
asynchronous workflow (SSE tail plus bounded GET polls of root status and sub-agent
subtree) until the root stays idle with no busy sub-agent for ``--settle-grace`` seconds,
or the global ``--timeout`` -> full session and subtree snapshots -> core status ->
orchestration proof -> judgement. The first send stream usually ends at the supervisor's
first response while a specialist is still busy; that is never treated as the end.
Nothing is sent to the session after the one prompt.

Recovery (``--recover-session ID``): re-attach to the session named by this root's
existing proof without creating a session, sending a prompt or interrupting; follow and
judge it the same way and append the attempt to the proof's history.

Outputs:
* ``<live-root>/omnigent/session-proof.json``: sanitized (no credentials, account fields,
  runtime host/runner IDs, host name or absolute paths).
* ``.inspection-live-runtime/live-proof/<stamp>/``: raw stream events, session items and
  core status; local only, never published.

Exit code 0 only when every observed supervisor response completed, the workflow settled,
the supervisor made exactly five accepted delegations analyst -> experimenter -> analyst ->
experimenter -> analyst bound to real child sessions, the specialists' own SDK tool calls
carry the core's result IDs, and the core ledger is closed with at least two distinct
analysis updates, all labeled by the MCP sidecar.

Child agents come from the supervisor's dispatch output (``conversation_id`` + ``agent``),
cross-checked against the child node's ``tool``/``title``/``session_name`` (its ``agent_name``
is None on Omnigent 0.16). Five delegations need not be five child conversations: ``sys_session_send`` may
reuse a same-agent child, and ``child_turns`` maps that child's k-th user turn (its text must be the k-th
dispatched message verbatim) to its k-th accepted delegation, or refuses. Persisted records are grouped into
SDK calls per turn by ``sdk_calls``: only
exact duplicates and the documented stale-FIFO call_id pair collapse; every collapse is listed
in ``collapsed_record_artifacts`` and the raw dispatch record count stays in the proof.
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
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "inspection_agent"
RUNTIME = ROOT / ".inspection-live-runtime"
AGENT_NAME = "inspection_supervisor"
ANALYST = "inspection_analyst"
EXPERIMENTER = "inspection_experimenter"
PORT = 6771
MCP_SERVER = "inspection"
ROLE_OF = {ANALYST: "analyst", EXPERIMENTER: "experimenter"}
ROLE_TOOLS = {ANALYST: {"analyze_and_plan", "read_result"}, EXPERIMENTER: {"review_site", "read_result"}}
DELEGATION_ORDER = [ANALYST, EXPERIMENTER, ANALYST, EXPERIMENTER, ANALYST]
DISPATCH_TOOLS = {"sys_session_send", "sys_session_create"}
DISCOVERY_TOOLS = {"ToolSearch", "sys_agent_list"}
SIDECAR_ACTOR_PREFIX = "mcp-sidecar:"
CLOSED_OK = {"finalized", "complete"}
EXPECTED_BUDGET = 120.0
EXPECTED_MAX_REVIEWS = 4
MIN_UPDATES = 2
MODEL = "claude-opus-5-5"
HARNESS = "claude-sdk"
TOOL_CAPS = {AGENT_NAME: 16, ANALYST: 6, EXPERIMENTER: 6}
PROOF_REL = Path("omnigent") / "session-proof.json"
SIDECAR_REL = Path("tools") / "mcp" / "inspection.yaml"
RESULT_ID_RE = re.compile(r"\bir_[0-9a-f]{16}\b")
PROMPT = (
    "Run the prepared paid inspection live loop exactly as your instructions define: "
    "inspection_analyst initial plan, inspection_experimenter reviews the recorded site, "
    "inspection_analyst next plan, inspection_experimenter reviews the recorded site, "
    "inspection_analyst finalizes. Exactly five delegations, then stop."
)
TERMINAL = {
    "response.completed": "completed",
    "response.failed": "failed",
    "response.incomplete": "incomplete",
    "response.cancelled": "cancelled",
}
PRESERVED_OUTCOMES = {"timeout", "incomplete", "cancelled"}
SECRET_KEYS = {"owner", "user_id", "user", "username", "email", "account", "account_id", "org_id", "organization_id",
               "authorization", "api_key", "token", "access_token", "refresh_token", "id_token", "cookie", "credential",
               "credentials", "password", "secret", "host_id", "hostname", "host_name", "machine", "machine_id",
               "workspace", "cwd", "home", "runner_id", "runner_name", "binding_token", "external_session_id"}
CORE_PROBE = ("import json, sys\n"
              "from inspection_live import InspectionLive\n"
              "print(json.dumps(InspectionLive(sys.argv[1]).status(), default=str))\n")

_ABS_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:(?:\\{1,2}|/)|\\\\\\\\|\\\\|/(?:Users|home|tmp|var|private|root|mnt)/)"
                       r"[^\s'\",\]\}\)]*")
_SECRETISH = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"
                        r"|(?i:bearer\s+[A-Za-z0-9._\-]{8,})")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_KEYED = re.compile(r"(?P<q>\\*\")(?P<k>" + "|".join(sorted(SECRET_KEYS, key=len, reverse=True))
                    + r")(?P=q)\s*:\s*(?P=q).*?(?P=q)", re.IGNORECASE)


class LiveRunError(RuntimeError):
    pass


class RecoveryRefused(LiveRunError):
    """A recovery precondition failed; nothing was written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------- sanitizing
def scrub_text(text: str, redact: frozenset[str] = frozenset()) -> str:
    """Remove runtime IDs, account fields embedded as text, secrets, e-mails and absolute paths."""
    for value in sorted(redact, key=len, reverse=True):
        if value and len(value) >= 6:
            text = text.replace(value, "<runtime-id>")
    for p in {str(ROOT), str(Path.home())}:
        for v in (p, p.replace("\\", "/"), p.replace("\\", "\\\\")):
            text = text.replace(v, "<path>")
    text = _KEYED.sub(lambda m: f"{m['q']}{m['k']}{m['q']}: {m['q']}<redacted>{m['q']}", text)
    text = _ABS_PATH.sub("<path>", text)
    text = _SECRETISH.sub("<redacted>", text)
    text = _EMAIL.sub("<email>", text)
    host = os.environ.get("COMPUTERNAME") or ""
    if len(host) >= 4:
        text = re.sub(rf"\b{re.escape(host)}\b", "<host>", text, flags=re.IGNORECASE)
    return text


def sanitize(obj: Any, redact: frozenset[str] = frozenset()) -> Any:
    """Recursively drop account/credential/runtime keys and scrub every string, including JSON inside strings."""
    if isinstance(obj, dict):
        return {k: sanitize(v, redact) for k, v in obj.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, redact) for v in obj]
    if isinstance(obj, str):
        if obj.lstrip().startswith(("{", "[")):
            try:
                embedded = json.loads(obj)
            except (ValueError, TypeError):
                pass
            else:
                if isinstance(embedded, (dict, list)):
                    return json.dumps(sanitize(embedded, redact), ensure_ascii=False)
        return scrub_text(obj, redact)
    return obj


# ---------------------------------------------------------------------- sidecars and core
def read_sidecar(path: Path) -> dict[str, Any]:
    """Parse the launcher-rendered sidecar YAML (fixed shape) without a YAML dependency."""
    if not path.is_file():
        raise LiveRunError(f"{path.parent.parent.parent.name} MCP sidecar missing; run launch_inspection.ps1 -Setup")
    text = path.read_text(encoding="utf-8-sig")

    def field(key: str) -> str | None:
        m = re.search(rf'^\s*{key}:\s*"([^"]*)"\s*$', text, re.MULTILINE)
        return m.group(1) if m else None

    tools = re.search(r"^tools:\s*\[([^\]]*)\]\s*$", text, re.MULTILINE)
    return {"name": (re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE) or [None, None])[1],
            "command": field("command"), "root": field("INSPECTION_LIVE_ROOT"), "role": field("INSPECTION_MCP_ROLE"),
            "tools": {t.strip() for t in tools.group(1).split(",") if t.strip()} if tools else set()}


def check_sidecars(live_root: Path, agent_dir: Path = AGENT_DIR) -> str:
    """Both sidecars name this live root, their own role and tools, and one interpreter; returns it."""
    pythons = set()
    for agent in (ANALYST, EXPERIMENTER):
        sc = read_sidecar(agent_dir / "agents" / agent / SIDECAR_REL)
        if sc["name"] != MCP_SERVER:
            raise LiveRunError(f"{agent} sidecar server name is {sc['name']!r}, not {MCP_SERVER!r}")
        if not sc["root"] or Path(sc["root"]).resolve() != live_root.resolve():
            raise LiveRunError(f"{agent} sidecar points at another live root; re-run -Setup and restart -Server")
        if sc["role"] != ROLE_OF[agent]:
            raise LiveRunError(f"{agent} sidecar INSPECTION_MCP_ROLE must be {ROLE_OF[agent]!r}")
        if sc["tools"] != ROLE_TOOLS[agent]:
            raise LiveRunError(f"{agent} sidecar tools {sorted(sc['tools'])} != {sorted(ROLE_TOOLS[agent])}")
        if not sc["command"] or not Path(sc["command"]).is_file():
            raise LiveRunError(f"{agent} sidecar interpreter is missing")
        pythons.add(str(Path(sc["command"]).resolve()))
    if len(pythons) != 1:
        raise LiveRunError("the two sidecars use different interpreters")
    return pythons.pop()


def core_status(python: str, live_root: Path, timeout: float = 120.0) -> dict[str, Any]:
    """Public core status via ``InspectionLive(root).status()`` in the sidecar interpreter."""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run([python, "-c", CORE_PROBE, str(live_root)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LiveRunError(f"core status probe failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        raise LiveRunError(f"core status probe failed: {scrub_text(tail[0])[:300]}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        st = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise LiveRunError("core status probe printed no JSON") from None
    if not isinstance(st, dict):
        raise LiveRunError("core status is not an object")
    return st


def core_summary(st: dict[str, Any] | None) -> dict[str, Any] | None:
    """Public, ID-carrying subset of the core status for the proof (no oracle or private fields exist there)."""
    if st is None:
        return None

    def pick(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        return {k: d.get(k) for k in keys if isinstance(d, dict) and k in d}

    return {
        **pick(st, ("kind", "data_label", "lot_id", "n_sites", "mode", "variant", "policy", "model_hash", "budget",
                    "max_reviews", "reviews_admitted", "reviews_executed", "blocked", "closed")),
        "pending_decision": pick(st.get("pending_decision"), ("decision_sequence", "selected_site_id", "actor")) or None,
        "decisions": [{**pick(d, ("decision_sequence", "selected_site_id", "actor", "remaining_budget",
                                  "evidence_result_ids")),
                       "candidate_site_ids": [c.get("site_id") for c in d.get("candidates") or [] if isinstance(c, dict)]}
                      for d in st.get("decisions") or [] if isinstance(d, dict)],
        "observations": [pick(o, ("result_id", "site_id", "decision_sequence", "status", "reported_kind", "attempts",
                                  "charged", "cumulative_spend")) for o in st.get("observations") or [] if isinstance(o, dict)],
        "updates": [pick(u, ("update_index", "actor", "observed_result_id", "observed_site_id", "decision_sequence",
                             "observed_status", "next_selected_site_id", "next_decision", "remaining_budget"))
                    for u in st.get("updates") or [] if isinstance(u, dict)],
    }


def check_fresh(st: dict[str, Any]) -> None:
    if st.get("kind") not in (None, "inspection_live"):
        raise LiveRunError(f"live root kind is {st.get('kind')!r}, not inspection_live")
    budget = st.get("budget") or {}
    if budget.get("limit") is not None and float(budget["limit"]) != EXPECTED_BUDGET:
        raise LiveRunError(f"live budget is {budget.get('limit')}, not {EXPECTED_BUDGET:g}; prepare a fresh root")
    if st.get("max_reviews") not in (None, EXPECTED_MAX_REVIEWS):
        raise LiveRunError(f"review cap is {st.get('max_reviews')}, not {EXPECTED_MAX_REVIEWS}; prepare a fresh root")
    if st.get("decisions") or st.get("observations") or st.get("updates") or st.get("reviews_admitted") \
            or st.get("closed") or st.get("blocked") or st.get("pending_decision"):
        raise LiveRunError("live root already has decisions, reviews or a closure; one session per freshly prepared root")


def judge_core(st: dict[str, Any] | None) -> list[str]:
    if st is None:
        return ["core status unavailable"]
    reasons = []
    closed = st.get("closed")
    if not closed:
        reasons.append("core ledger not closed")
    elif closed.get("status") not in CLOSED_OK:
        reasons.append(f"core ledger closed with status {closed.get('status')!r}")
    if st.get("blocked"):
        reasons.append(f"core session blocked: {(st['blocked'] or {}).get('reason')}")
    if st.get("pending_decision"):
        reasons.append("a recorded decision was never reviewed")
    observations = [o for o in st.get("observations") or [] if isinstance(o, dict)]
    executed = st.get("reviews_executed", len(observations))
    if executed < 2 or len(observations) < 2:
        reasons.append(f"{executed} executed reviews < 2")
    updates = [u for u in st.get("updates") or [] if isinstance(u, dict)]
    distinct = {u.get("observed_result_id") for u in updates if u.get("observed_result_id")}
    if len(distinct) < MIN_UPDATES:
        reasons.append(f"{len(distinct)} distinct analysis updates < {MIN_UPDATES}")
    missing = [o.get("result_id") for o in observations if o.get("result_id") not in distinct]
    if missing:
        reasons.append(f"paid results without an analysis update: {missing}")
    for label, rows in (("decision", st.get("decisions") or []), ("update", updates), ("closure", [closed] if closed else [])):
        for row in rows:
            actor = row.get("actor") if isinstance(row, dict) else None
            if not (isinstance(actor, str) and actor.startswith(SIDECAR_ACTOR_PREFIX)):
                reasons.append(f"core {label} actor {actor!r} is not an MCP sidecar label (direct calls are not orchestration)")
    return reasons


# ---------------------------------------------------------------------- SDK items -> orchestration proof
def _text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(b.get("text", json.dumps(b, default=str)) if isinstance(b, dict) else str(b) for b in output)
    return json.dumps(output, default=str)


def _json(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("content"), list) and "status" not in obj:
        inner = _text(obj["content"])
        return _json(inner) or obj
    return obj if isinstance(obj, dict) else None


def _args(call: dict[str, Any]) -> dict[str, Any]:
    a = call.get("arguments")
    if isinstance(a, dict):
        return a
    parsed = _json(a) if isinstance(a, str) else None
    return parsed or {}


def _tool_name(name: Any) -> tuple[str | None, str]:
    parts = str(name or "").split("__")
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


def tool_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every persisted function_call record with its own output: the next unconsumed output carrying its call_id.

    ``origin`` comes from Omnigent's own stamp: a record whose ``model`` equals its ``response_id`` is the
    adapter's mirror of an SDK tool_use/tool_result (true SDK ``tool_use_id``); any other record is the
    Omnigent dispatch bridge's execution record.
    """
    used: set[int] = set()
    records = []
    for i, it in enumerate(items):
        if it.get("type") != "function_call":
            continue
        j = next((k for k in range(i + 1, len(items)) if k not in used and items[k].get("type") == "function_call_output"
                  and items[k].get("call_id") == it.get("call_id")), None)
        if j is not None:
            used.add(j)
        model, resp = it.get("model"), it.get("response_id")
        records.append({"item_index": i, "output_index": j, "item_id": it.get("id"), "response_id": resp,
                        "name": it.get("name"),
                        "call_id": it.get("call_id"), "status": it.get("status"),
                        "origin": "sdk_mirror" if model and model == resp else "dispatch",
                        "arguments": _args(it), "has_output": j is not None,
                        "out": _text(items[j].get("output")) if j is not None else ""})
    return records


def function_calls(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Pair each function_call with its own output (positional, see ``tool_records``)."""
    calls = [it for it in items if it.get("type") == "function_call"]
    return [(it, r["out"]) for it, r in zip(calls, tool_records(items))]


def _norm_out(text: str) -> str:
    """Comparable form of one tool output; unwraps the SDK mirror's ``{"result": "<text>"}`` envelope."""
    parsed: Any
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text.strip()
    if isinstance(parsed, dict) and set(parsed) == {"result"} and isinstance(parsed["result"], str):
        return parsed["result"].strip()
    return json.dumps(parsed, sort_keys=True)


def sdk_calls(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group persisted records into SDK tool calls; returns ``(calls, collapsed_artifacts)``.

    Two records become ONE call only on positive evidence, never by tool name alone:

    * ``exact_duplicate``: same call_id, name, arguments and output.
    * ``stale_fifo_call_id`` (Omnigent 0.16 ``_ExecutorAdapter._stable_tool_executor``): the SDK-internal
      ``ToolSearch`` tool_use id is queued like an MCP id, so every later MCP dispatch of that response is
      stamped with the PREVIOUS SDK tool_use id, and the SDK's own mirror of the call is persisted too. The
      dispatch record collapses into the mirror only when, in the same response, its call_id is exactly the
      SDK call_id of the mirror immediately before that mirror, and name, arguments and output are equal.

    A dispatch record is never dropped: each one is its own call or the execution of exactly one mirrored SDK
    call, so a real repeated or extra call (its own SDK tool_use id) always stays a separate call.
    """
    calls: list[dict[str, Any]] = []
    collapsed: list[dict[str, Any]] = []
    paired: dict[int, int] = {}  # dispatch record index -> mirror record index
    by_resp: dict[Any, list[int]] = {}
    for i, r in enumerate(records):
        if r["origin"] == "sdk_mirror":
            by_resp.setdefault(r["response_id"], []).append(i)
    for resp, mirrors in by_resp.items():
        for k in range(1, len(mirrors)):
            m = records[mirrors[k]]
            if m["name"] in DISCOVERY_TOOLS or not m["has_output"]:
                continue
            prev_id = records[mirrors[k - 1]]["call_id"]
            for i, d in enumerate(records):
                if (i not in paired and d["origin"] == "dispatch" and d["response_id"] == resp and d["call_id"] == prev_id
                        and d["call_id"] != m["call_id"] and d["name"] == m["name"] and d["arguments"] == m["arguments"]
                        and d["has_output"] and _norm_out(d["out"]) == _norm_out(m["out"])):
                    paired[i] = mirrors[k]
                    collapsed.append({"rule": "stale_fifo_call_id", "sdk_call_id": m["call_id"], "name": m["name"],
                                      "response_id": resp, "mirror_item_id": m["item_id"],
                                      "dispatch_item_id": d["item_id"], "dispatch_stamped_call_id": d["call_id"],
                                      "stamped_call_id_belongs_to": records[mirrors[k - 1]]["name"]})
                    break
    owner = {m: d for d, m in paired.items()}
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for i, r in enumerate(records):
        if i in paired:
            continue
        rec_ids = [r["item_id"]] + ([records[owner[i]]["item_id"]] if i in owner else [])
        key = (r["call_id"], r["name"], json.dumps(r["arguments"], sort_keys=True), _norm_out(r["out"]))
        if r["call_id"] and key in seen:
            seen[key]["record_item_ids"] += rec_ids
            collapsed.append({"rule": "exact_duplicate", "sdk_call_id": r["call_id"], "name": r["name"],
                              "response_id": r["response_id"], "duplicate_item_id": r["item_id"]})
            continue
        call = {"sdk_call_id": r["call_id"], "name": r["name"], "response_id": r["response_id"],
                "arguments": r["arguments"], "status": r["status"], "has_output": r["has_output"], "out": r["out"],
                "record_item_ids": rec_ids, "executed_by_dispatch": i in owner or r["origin"] == "dispatch"}
        seen[key] = call
        calls.append(call)
    return calls, collapsed


def _status_of(text: str) -> str | None:
    parsed = _json(text)
    if parsed and isinstance(parsed.get("status"), str):
        return parsed["status"]
    m = re.search(r'"status"\s*:\s*"([A-Za-z_]+)"', text)
    return m.group(1) if m else None


def _message_text(item: dict[str, Any]) -> str:
    return "".join(str(b.get("text", "")) for b in item.get("content") or []
                   if isinstance(b, dict) and b.get("type") in ("input_text", "text", "output_text"))


def child_turns(cid: str, items: list[dict[str, Any]],
                dispatches: list[dict[str, Any]]) -> tuple[list[tuple[int | None, list[dict[str, Any]]]],
                                                            list[dict[str, Any]], list[str]]:
    """Group one child's SDK calls by delegated turn; returns ``([(delegation, calls)], collapsed, problems)``.

    ``sys_session_send`` may reuse a child conversation: every accepted delegation to it opens exactly one
    user turn whose text is that delegation's dispatched message (``args``) verbatim, in dispatch order. Turn k
    of the child belongs to its k-th accepted delegation only when the turn count equals the delegation count
    and every turn text matches. A tool record belongs to the turn it is persisted in; its output and all
    records of its SDK response must lie in that turn. Otherwise the mapping is refused for the whole child
    (its calls get delegation None) rather than guessed.
    """
    problems: list[str] = []
    starts = [i for i, it in enumerate(items) if it.get("type") == "message" and it.get("role") == "user"]
    records = tool_records(items)

    def turn_of(idx: int) -> int:
        return sum(1 for s in starts if s <= idx) - 1

    proved = True
    if dispatches and len(starts) != len(dispatches):
        proved = False
        if len(starts) > len(dispatches):
            problems.append(f"child session {cid} has {len(starts)} delegated turns but {len(dispatches)} accepted "
                            "delegations (an undelegated or extra turn)")
        for pos, d in enumerate(dispatches[len(starts):], len(starts)):
            problems.append(f"delegation {d['index']} reuses child session {cid} without a matching ordered turn "
                            f"({len(starts)} turns for {len(dispatches)} accepted delegations)" if pos else
                            f"delegation {d['index']} has no delegated turn in child session {cid}")
    for k, d in enumerate(dispatches[:len(starts)]):
        if not isinstance(d.get("_message"), str) or _message_text(items[starts[k]]) != d["_message"]:
            proved = False
            problems.append(f"child session {cid} turn {k + 1} is not the message dispatched by delegation "
                            f"{d['index']}")
    buckets: dict[int, list[dict[str, Any]]] = {}
    resp_turns: dict[Any, set[int]] = {}
    for r in records:
        t = turn_of(r["item_index"])
        buckets.setdefault(t, []).append(r)
        resp_turns.setdefault(r["response_id"], set()).add(t)
        if dispatches and t < 0:
            proved = False
            problems.append(f"child session {cid} call {r['name']} ({r['call_id']}) precedes every delegated turn")
        if r["output_index"] is not None and turn_of(r["output_index"]) != t:
            proved = False
            problems.append(f"child session {cid} call {r['name']} ({r['call_id']}) has its output in another turn")
    for resp, ts in resp_turns.items():
        if len(ts) > 1:
            proved = False
            problems.append(f"child session {cid} SDK response {resp} spans {len(ts)} delegated turns")
    turns, collapsed, seen_ids = [], [], set()
    for t in sorted(buckets):
        step = dispatches[t]["index"] if proved and 0 <= t < len(dispatches) else None
        calls, c_collapsed = sdk_calls(buckets[t])
        collapsed += [{**x, "child_session_id": cid, "delegation": step} for x in c_collapsed]
        ids = {c["sdk_call_id"] for c in calls if c["sdk_call_id"]}
        problems += [f"child session {cid} SDK call id {x} recurs in another turn" for x in sorted(ids & seen_ids)]
        seen_ids |= ids
        turns.append((step, calls))
    return turns, collapsed, problems


def child_identity(cid: str, node: dict[str, Any], dispatch: dict[str, Any] | None,
                   root_id: str | None) -> tuple[str | None, list[str]]:
    """Agent of one child session from the authoritative dispatch output, cross-checked with its own metadata.

    The dispatch output (``conversation_id``/``agent``/``title``) names the agent; the child node's ``tool``,
    ``title`` prefix (``<agent>:<title>``), ``session_name`` and ``agent_name`` (often None) must not conflict.
    ``dispatch`` is the child's FIRST accepted delegation: a reused child keeps the title of its creation.
    """
    problems = []
    parent = node.get("parent_id") or node.get("parent_session_id")
    if root_id is not None and parent != root_id:
        problems.append(f"child session {cid} is not a direct child of the root session (nested or foreign subtree)")
    if node.get("kind") not in (None, "sub_agent"):
        problems.append(f"child session {cid} has kind {node.get('kind')!r}, not sub_agent")
    title = node.get("title")
    declared = {"agent_name": node.get("agent_name"), "tool": node.get("tool"),
                "title": title.split(":", 1)[0] if isinstance(title, str) and ":" in title else None}
    declared = {k: v for k, v in declared.items() if v}
    if dispatch is None:
        problems.append(f"child session {cid} ({declared or 'no agent metadata'}) was not created by an accepted "
                        "supervisor delegation")
        return None, problems
    agent = dispatch["agent"]
    if agent not in ROLE_OF:
        problems.append(f"child session {cid} belongs to unknown agent {agent!r}")
    for k, v in declared.items():
        if v != agent:
            problems.append(f"child session {cid} {k} {v!r} conflicts with its dispatch agent {agent!r}")
    if dispatch.get("title") and node.get("session_name") and node["session_name"] != dispatch["title"]:
        problems.append(f"child session {cid} session_name conflicts with its dispatch title")
    return agent, problems


SUPERVISOR_TOOLS = DISPATCH_TOOLS | {"sys_read_inbox"}
STEP_FINALIZE = {1: False, 3: False, 5: True}


def orchestration_proof(supervisor_items: list[dict[str, Any]], children: dict[str, dict[str, Any]],
                        core: dict[str, Any] | None, root_id: str | None = None) -> dict[str, Any]:
    """Delegations and specialist tool calls observed in the SDK session items, cross-checked with the core."""
    reasons: list[str] = []
    root_records = tool_records(supervisor_items)
    root_calls, collapsed = sdk_calls(root_records)
    delegations = []
    for c in root_calls:
        if c["name"] not in DISPATCH_TOOLS:
            if c["name"] not in SUPERVISOR_TOOLS | DISCOVERY_TOOLS:
                reasons.append(f"supervisor called unexpected tool {c['name']}")
            continue
        a, o = c["arguments"], (_json(c["out"]) or {})
        conv = o.get("conversation_id") or o.get("task_id")
        err = o.get("error") or (None if o else (c["out"][:200] or "no output"))
        out_agent = o.get("agent")
        delegations.append({"tool": c["name"], "sdk_call_id": c["sdk_call_id"], "record_item_ids": c["record_item_ids"],
                            "agent": a.get("agent"), "dispatch_output_agent": out_agent, "title": o.get("title"),
                            "child_session_id": conv, "dispatch_status": o.get("status"),
                            "accepted": bool(conv) and not o.get("error") and o.get("kind") in (None, "sub_agent"),
                            "error": err if not conv else None, "reuses_child_of_delegation": None,
                            "_message": a.get("args")})
    raw_dispatch_records = sum(1 for r in root_records if r["name"] in DISPATCH_TOOLS)
    if len(delegations) != len(DELEGATION_ORDER):
        reasons.append(f"{len(delegations)} supervisor delegations, expected exactly {len(DELEGATION_ORDER)}")
    if [d["agent"] for d in delegations] != DELEGATION_ORDER:
        reasons.append(f"delegation order {[d['agent'] for d in delegations]} != {DELEGATION_ORDER}")
    by_child: dict[str, list[dict[str, Any]]] = {}  # child session -> its accepted delegations, in order
    for i, d in enumerate(delegations, 1):
        if not d["accepted"]:
            reasons.append(f"delegation {i} was not accepted")
            continue
        if d["dispatch_output_agent"] not in (None, d["agent"]):
            reasons.append(f"delegation {i} requested {d['agent']!r} but the dispatch output names "
                           f"{d['dispatch_output_agent']!r}")
        earlier = by_child.get(d["child_session_id"])
        if earlier:
            first = earlier[0]
            if (d["agent"], d["dispatch_output_agent"] or d["agent"]) != (first["agent"], first["agent"]):
                reasons.append(f"delegation {i} reuses child session {d['child_session_id']} of delegation "
                               f"{first['index']} for another agent ({d['agent']!r} != {first['agent']!r})")
                continue
            d["reuses_child_of_delegation"] = earlier[-1]["index"]
        by_child.setdefault(d["child_session_id"], []).append({**d, "index": i})
        if d["child_session_id"] not in children:
            reasons.append(f"delegation {i} ({d['agent']}) has no matching child session in the subtree")

    calls, child_rows = [], []
    for cid, child in children.items():
        mine = by_child.get(cid) or []
        agent, problems = child_identity(cid, child.get("summary") or {}, mine[0] if mine else None, root_id)
        reasons += problems
        turns, c_collapsed, problems = child_turns(cid, child.get("items") or [], mine)
        reasons += problems
        collapsed += c_collapsed
        child_rows.append({"child_session_id": cid, "agent": agent, "delegation": mine[0]["index"] if mine else None,
                           "delegations": [d["index"] for d in mine],
                           "turn_delegations_with_calls": [s for s, _ in turns]})
        for step, c in ((s, c) for s, cs in turns for c in cs):
            if c["name"] in DISCOVERY_TOOLS:
                continue
            server, tool = _tool_name(c["name"])
            out = c["out"]
            parsed = _json(out) or {}
            allowed = agent is not None and server == MCP_SERVER and tool in ROLE_TOOLS.get(agent, set())
            calls.append({"agent": agent, "child_session_id": cid, "delegation": step, "tool": tool, "server": server,
                          "sdk_call_id": c["sdk_call_id"], "record_item_ids": c["record_item_ids"],
                          "arguments": c["arguments"], "call_status": c["status"], "has_output": c["has_output"],
                          "output_status": _status_of(out), "result_ids": sorted(set(RESULT_ID_RE.findall(out))),
                          "allowed": allowed, "_parsed": parsed})
    for c in calls:
        if not c["allowed"]:
            reasons.append(f"{c['agent']} called unexpected tool {c['server']}__{c['tool']}")
        if not c["has_output"]:
            reasons.append(f"{c['agent']} call {c['tool']} ({c['sdk_call_id']}) has no tool result")
        if c["agent"] is not None and c["delegation"] is None:
            reasons.append(f"{c['agent']} call {c['tool']} ({c['sdk_call_id']}) is not mapped to an accepted delegation")

    # Step chain: exactly one primary call per delegation, IDs carried verbatim between steps.
    primary = {ANALYST: "analyze_and_plan", EXPERIMENTER: "review_site"}
    steps: dict[int, dict[str, Any]] = {}
    for i, agent in enumerate(DELEGATION_ORDER, 1):
        mine = [c for c in calls if c["delegation"] == i and c["agent"] == agent and c["tool"] == primary[agent]]
        if len(mine) != 1:
            reasons.append(f"delegation {i} ({agent}) has {len(mine)} {primary[agent]} calls, expected exactly 1")
            continue
        steps[i] = mine[0]
    decisions: dict[int, tuple[Any, Any]] = {}
    results: dict[int, tuple[Any, Any, Any]] = {}
    updates_seen: list[Any] = []
    for i in (1, 3, 5):
        c = steps.get(i)
        if c is None:
            continue
        p = c["_parsed"]
        if c["arguments"].get("finalize", False) is not STEP_FINALIZE[i]:
            reasons.append(f"delegation {i} analyze_and_plan finalize={c['arguments'].get('finalize')!r}, "
                           f"expected {STEP_FINALIZE[i]}")
        want = "closed" if STEP_FINALIZE[i] else "decision_recorded"
        if p.get("status") != want:
            reasons.append(f"delegation {i} analyze_and_plan returned status {p.get('status')!r}, expected {want!r}")
        dec = p.get("decision") if isinstance(p.get("decision"), dict) else {}
        if not STEP_FINALIZE[i]:
            decisions[i] = (dec.get("decision_sequence"), dec.get("selected_site_id"))
        updates_seen += [u.get("observed_result_id") for u in p.get("new_updates") or [] if isinstance(u, dict)]
    for i, prev in ((2, 1), (4, 3)):
        c = steps.get(i)
        if c is None:
            continue
        p = c["_parsed"]
        res = p.get("result") if isinstance(p.get("result"), dict) else {}
        a = c["arguments"]
        results[i] = (res.get("result_id"), res.get("site_id"), res.get("decision_sequence"))
        if p.get("status") != "observed" or not res.get("result_id"):
            reasons.append(f"delegation {i} review_site returned status {p.get('status')!r} without a paid result")
        if (res.get("site_id"), res.get("decision_sequence")) != (a.get("site_id"), a.get("decision_sequence")):
            reasons.append(f"delegation {i} review_site result site/sequence differ from its arguments")
        if prev in decisions and (a.get("decision_sequence"), a.get("site_id")) != decisions[prev]:
            reasons.append(f"delegation {i} reviewed {a.get('site_id')!r}/{a.get('decision_sequence')!r}, not the "
                           f"decision recorded in delegation {prev}")
    for i, upd_step in ((2, 3), (4, 5)):
        if i in results and upd_step in steps:
            upd = [u.get("observed_result_id") for u in steps[upd_step]["_parsed"].get("new_updates") or []
                   if isinstance(u, dict)]
            if results[i][0] not in upd:
                reasons.append(f"delegation {upd_step} did not return the analysis update for {results[i][0]}")
    if core is not None:
        core_dec = [(d.get("decision_sequence"), d.get("selected_site_id")) for d in core.get("decisions") or []]
        if core_dec != [decisions.get(1), decisions.get(3)]:
            reasons.append(f"core decisions {core_dec} differ from the analyst calls {[decisions.get(1), decisions.get(3)]}")
        core_obs = [(o.get("result_id"), o.get("site_id"), o.get("decision_sequence")) for o in core.get("observations") or []]
        if core_obs != [results.get(2), results.get(4)]:
            reasons.append(f"core paid results {[o[0] for o in core_obs]} differ from the experimenter review_site "
                           f"results {[(results.get(i) or (None,))[0] for i in (2, 4)]}")
        core_upd = [u.get("observed_result_id") for u in core.get("updates") or []]
        if core_upd != updates_seen:
            reasons.append(f"core analysis updates {core_upd} differ from the analyst calls {updates_seen}")
        if bool(core.get("closed")) != (steps.get(5) is not None and steps[5]["_parsed"].get("status") == "closed"):
            reasons.append("core closure does not match the analyst finalize call")
    for c in calls:
        del c["_parsed"]
    for d in delegations:
        del d["_message"]
    return {"delegations": delegations, "specialist_tool_calls": calls, "child_sessions": child_rows,
            "delegation_count": len(delegations), "raw_dispatch_records": raw_dispatch_records,
            "collapsed_record_artifacts": collapsed, "reasons": reasons}


def judge(outcome: str, core: dict[str, Any] | None, orch: dict[str, Any] | None) -> tuple[str, list[str]]:
    """``completed`` only with a completed settled workflow, real orchestration and a closed, updated core ledger."""
    reasons = []
    if outcome != "completed":
        reasons.append(f"workflow ended {outcome}")
    reasons += orch["reasons"] if orch is not None else ["no orchestration proof"]
    reasons += judge_core(core)
    if not reasons:
        return "completed", []
    return (outcome if outcome in PRESERVED_OUTCOMES else "failed"), reasons


# ---------------------------------------------------------------------- session follow-up
def _items_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    calls, texts = [], []
    for call, out in function_calls(items):
        name = call.get("name")
        calls.append({"name": name, "call_id": call.get("call_id"), "status": call.get("status"),
                      "arguments": str(call.get("arguments", ""))[:2000],
                      "output": ("<omitted: local tool/agent discovery inventory; raw record retained privately>"
                                 if name in DISCOVERY_TOOLS else out[:4000])})
    for it in items:
        if it.get("type") == "message" and it.get("role") == "assistant":
            for block in it.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    texts.append(str(block.get("text", "")))
    counts: dict[str, int] = {}
    for c in calls:
        counts[str(c["name"])] = counts.get(str(c["name"]), 0) + 1
    return {"tool_calls": calls, "tool_call_counts": counts, "assistant_text": "\n".join(texts)[-8000:]}


def _dump(ev: Any) -> dict[str, Any]:
    return ev if isinstance(ev, dict) else ev.model_dump(mode="json")


class Recorder:
    """Local raw event log plus every terminal supervisor response observed in one attempt."""

    def __init__(self, raw: Any, session_id: str) -> None:
        self.raw, self.session_id = raw, session_id
        self.responses: list[dict[str, Any]] = []
        self.children: dict[str, dict[str, Any]] = {}
        self.root_status_events: list[dict[str, Any]] = []
        self.stream_errors: list[str] = []
        self.polls = 0

    def record(self, ev: Any, source: str) -> dict[str, Any]:
        d = _dump(ev)
        self.raw.write(json.dumps({"driver_source": source, **d}, default=str) + "\n")
        self.raw.flush()
        etype = d.get("type")
        if etype == "session.child_session.updated":
            self.children.setdefault(str(d.get("child_session_id")), {}).update(d.get("child") or {})
        elif etype == "session.status" and d.get("conversation_id") in (None, self.session_id):
            self.root_status_events.append({"status": d.get("status"), "source": source, "observed_at": _now()})
        if etype in TERMINAL:
            r = d.get("response") or {}
            if not (r.get("id") and any(x["response_id"] == r.get("id") for x in self.responses)):
                self.responses.append({"response_id": r.get("id"), "event": etype, "status": TERMINAL[etype],
                                       "usage": r.get("usage"), "source": source, "observed_at": _now()})
        return d


async def send_initial(chat: Any, prompt: str, rec: Recorder, deadline: float) -> tuple[str | None, str | None]:
    """Post the single prompt and record its stream; ``(None, None)`` means: follow the workflow.

    ``SessionsChat.send`` returns at the FIRST terminal response, which for this asynchronous
    supervisor is usually "waiting for the analyst"; it is never the end of the workflow.
    """
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            async for ev in chat.send(prompt):
                rec.record(ev, "initial_send")
    except TimeoutError:
        return "timeout", "initial send exceeded the global timeout"
    except Exception as exc:  # noqa: BLE001 - recorded, never success
        return "failed", f"{type(exc).__name__}: {exc}"
    return None, None


async def follow_workflow(sessions: Any, session_id: str, rec: Recorder, *, deadline: float,
                          poll_interval: float, settle_grace: float) -> dict[str, Any]:
    """Follow child turns and wake turns without sending anything.

    Settled only when the root is idle AND no sub-agent is busy continuously for
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
            except Exception as exc:  # noqa: BLE001 - reattach; the poll decides the outcome
                rec.stream_errors.append(f"{type(exc).__name__}: {scrub_text(str(exc))[:200]}")
            await asyncio.sleep(poll_interval)

    task = asyncio.create_task(tail())
    settled_since: float | None = None
    last: dict[str, Any] = {}
    try:
        while True:
            snap = await sessions.get(session_id)
            busy = bool(await sessions.subtree_busy(session_id))
            rec.polls += 1
            last = {"root_status": snap.status, "subtree_busy": busy, "polled_at": _now()}
            if snap.status == "failed":
                return {"outcome": "failed", "error": f"root session failed: {snap.last_task_error}", "last_poll": last}
            now = time.monotonic()
            if snap.status != "idle" or busy:
                settled_since = None
            else:
                settled_since = now if settled_since is None else settled_since
                if now - settled_since >= settle_grace:
                    return {"outcome": "settled", "error": None, "last_poll": last}
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


async def drive(sessions: Any, session_id: str, rec: Recorder, *, deadline: float, poll_interval: float,
                settle_grace: float, chat: Any = None, prompt: str | None = None,
                interrupt_on_timeout: bool = False) -> dict[str, Any]:
    """Initial send (only when ``chat`` is given), then follow the workflow to its real end."""
    hard, error = None, None
    if chat is not None:
        hard, error = await send_initial(chat, prompt or "", rec, deadline)
        if hard is None and not rec.responses:
            rec.stream_errors.append("initial send stream ended without a terminal event")
    if hard is None:
        follow = await follow_workflow(sessions, session_id, rec, deadline=deadline, poll_interval=poll_interval,
                                       settle_grace=settle_grace)
    else:
        follow = {"outcome": hard, "error": error, "last_poll": None}
    if follow["outcome"] == "timeout" and interrupt_on_timeout:
        try:
            await sessions.interrupt(session_id)
        except Exception as exc:  # noqa: BLE001 - the timeout stays the outcome
            follow["error"] = f"{follow['error']}; interrupt failed: {type(exc).__name__}"
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
    """Full root session, its items and the whole sub-agent subtree with items."""
    snap = await sessions.get(session_id)
    items = await _all_items(sessions, session_id)
    tree = await sessions.child_sessions_tree(session_id)
    rows, raw_children = [], {}
    for node in tree:
        cid = str(node.get("id"))
        c_items = await _all_items(sessions, cid)
        c_snap = await sessions.get(cid)
        raw_children[cid] = {"summary": node, "items": c_items}
        rows.append({"session_id": cid, "parent_id": node.get("parent_id"), "agent_name": node.get("agent_name"),
                     "dispatch_tool": node.get("tool"), "title": node.get("title"), "kind": node.get("kind"),
                     "status": c_snap.status, "current_task_status": node.get("current_task_status"),
                     "last_total_tokens": c_snap.last_total_tokens, "llm_model": c_snap.llm_model,
                     "harness": c_snap.harness, "reasoning_effort": c_snap.reasoning_effort,
                     "last_task_error": c_snap.last_task_error, **_items_summary(c_items)})
    return {"snap": snap, "items": items, "child_rows": rows, "raw_children": raw_children}


async def drive_and_collect(sessions: Any, session_id: str, raw_dir: Path, args: argparse.Namespace,
                            core_probe: Callable[[], dict[str, Any]], *, chat: Any = None, prompt: str | None = None,
                            interrupt_on_timeout: bool = False,
                            prior_responses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with open(raw_dir / "stream-events.jsonl", "a", encoding="utf-8") as raw:
        rec = Recorder(raw, session_id)
        rec.responses.extend(prior_responses or [])
        result = await drive(sessions, session_id, rec, deadline=t0 + args.timeout, poll_interval=args.poll_interval,
                             settle_grace=args.settle_grace, chat=chat, prompt=prompt,
                             interrupt_on_timeout=interrupt_on_timeout)
    elapsed = time.monotonic() - t0
    col = await collect_snapshot(sessions, session_id)
    _write_json(raw_dir / "session-items.json", {"session": col["items"], "children": col["raw_children"],
                                                 "child_stream_updates": rec.children})
    core, core_error = None, None
    try:
        core = await asyncio.to_thread(core_probe)
        _write_json(raw_dir / "core-status.json", core)
    except LiveRunError as exc:
        core_error = str(exc)
    orch = orchestration_proof(col["items"], col["raw_children"], core, root_id=session_id)
    snap = col["snap"]
    last = rec.responses[-1] if rec.responses else {}
    return {
        "turn_outcome": result["outcome"], "error": result["error"], "elapsed_seconds": round(elapsed, 3),
        "finished_at": _now(), "response_id": last.get("response_id"), "response_status": last.get("status"),
        "usage": last.get("usage"), "responses": rec.responses,
        "usage_note": ("responses lists every terminal supervisor response the driver observed (initial send, later "
                       "wake turns, and any carried over from a prior attempt). The first supervisor response is never "
                       "counted as workflow completion. Sub-agent rows carry each child's last_total_tokens. "
                       "Subscription use is not converted to money."),
        "workflow_follow": {"outcome": result["follow"]["outcome"], "polls": rec.polls,
                            "poll_interval_seconds": args.poll_interval, "settle_grace_seconds": args.settle_grace,
                            "last_poll": result["follow"]["last_poll"],
                            "root_status_events": rec.root_status_events[-100:], "stream_errors": rec.stream_errors[-20:]},
        "session": {"status": snap.status, "llm_model": snap.llm_model, "harness": snap.harness,
                    "reasoning_effort": snap.reasoning_effort, "last_total_tokens": snap.last_total_tokens,
                    "last_task_error": snap.last_task_error},
        "supervisor": _items_summary(col["items"]),
        "sub_agents": col["child_rows"],
        "orchestration": orch,
        "core": core_summary(core),
        "core_error": core_error,
        "_core_raw": core,
    }


# ---------------------------------------------------------------------- one session
def _default_client() -> Any:
    from omnigent_client import OmnigentClient
    return OmnigentClient


def _default_chat(client: Any, session: Any) -> Any:
    from omnigent_client import SessionsChat
    return SessionsChat(namespace=client.sessions, files_uploader=None, files_getter=None, session=session)


async def run_session(args: argparse.Namespace, proof_path: Path, raw_dir: Path,
                      core_probe: Callable[[], dict[str, Any]], redact: set[str], *,
                      client_cls: Any = None, chat_factory: Any = None) -> dict[str, Any]:
    """Create exactly one session, save its ID, send exactly one prompt, follow it to its end."""
    client_cls = client_cls or _default_client()
    chat_factory = chat_factory or _default_chat
    proof: dict[str, Any] = {"agent": AGENT_NAME, "status": "starting", "started_at": _now()}
    async with client_cls(base_url=args.server) as client:
        http = client._http  # the SDK's own client (trusted-origin header); loopback only
        health = await http.get(f"{args.server}/health")
        if health.status_code != 200:
            raise LiveRunError(f"server health check failed ({health.status_code})")
        agents = (await http.get(f"{args.server}/v1/agents", params={"limit": 1000})).json().get("data", [])
        matches = [a for a in agents if isinstance(a, dict) and a.get("name") == AGENT_NAME]
        if len(matches) != 1:
            raise LiveRunError(f"expected exactly one registered {AGENT_NAME}, found {len(matches)}; restart -Server")
        hosts = (await http.get(f"{args.server}/v1/hosts")).json().get("hosts", [])
        online = [h for h in hosts if isinstance(h, dict) and h.get("status") == "online" and not h.get("sandbox_provider")]
        if args.host_id:
            online = [h for h in online if h.get("host_id") == args.host_id]
        if len(online) != 1:
            raise LiveRunError(f"expected exactly one online local host, found {len(online)}; pass --host-id")
        redact.update(str(h[k]) for h in hosts if isinstance(h, dict) for k in ("host_id", "runner_id", "name") if h.get(k))
        body = {"agent_id": matches[0]["id"], "host_id": online[0]["host_id"], "workspace": str(ROOT),
                "reasoning_effort": "medium", "title": "inspection live loop"}
        resp = await http.post(f"{args.server}/v1/sessions", json=body)
        if resp.status_code >= 300:
            raise LiveRunError(f"session create failed ({resp.status_code}): {scrub_text(resp.text[:500])}")
        session_id = str(resp.json()["id"])
        proof.update({"session_id": session_id, "agent_id": matches[0]["id"], "status": "running",
                      "session_created_at": _now(), "host_note": "one online local foreground host (id kept local)"})
        _write_json(proof_path, sanitize(proof, frozenset(redact)))  # saved BEFORE the prompt
        session = await client.sessions.get(session_id)
        if getattr(session, "runner_id", None):
            redact.add(str(session.runner_id))
        chat = chat_factory(client, session)
        proof.update(await drive_and_collect(client.sessions, session_id, raw_dir, args, core_probe, chat=chat,
                                             prompt=PROMPT, interrupt_on_timeout=True))
    return proof


# ---------------------------------------------------------------------- recovery
def check_recovery(prior: dict[str, Any], session_id: str, st: dict[str, Any]) -> None:
    """The root's prior proof must name this session and the core state must extend what it recorded."""
    if prior.get("session_id") != session_id:
        raise RecoveryRefused("prior proof in this live root names another session; recovery refused")
    if prior.get("agent") != AGENT_NAME:
        raise RecoveryRefused("prior proof is not for the inspection supervisor; recovery refused")
    pc = prior.get("core") or {}
    now = core_summary(st) or {}
    for key, id_key in (("decisions", "decision_sequence"), ("observations", "result_id"), ("updates", "observed_result_id")):
        before = [r.get(id_key) for r in pc.get(key) or []]
        after = [r.get(id_key) for r in now.get(key) or []]
        if after[:len(before)] != before:
            raise RecoveryRefused(f"core {key} do not extend the prior proof; recovery refused")
    if pc.get("lot_id") and now.get("lot_id") and pc["lot_id"] != now["lot_id"]:
        raise RecoveryRefused("core lot differs from the prior proof; recovery refused")


def prior_responses(prior: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**r, "from_prior_attempt": True} for r in prior.get("responses") or [] if isinstance(r, dict)]


async def run_recovery(sessions: Any, args: argparse.Namespace, proof_path: Path, raw_dir: Path, prior: dict[str, Any],
                       core_probe: Callable[[], dict[str, Any]], redact: set[str]) -> dict[str, Any]:
    """Re-attach to an existing session: no create, no prompt, no interrupt."""
    session_id = args.recover_session
    try:
        snap = await sessions.get(session_id)
    except Exception as exc:  # noqa: BLE001 - nothing written yet
        raise RecoveryRefused(f"session lookup failed: {type(exc).__name__}") from None
    if prior.get("agent_id") and snap.agent_id != prior["agent_id"]:
        raise RecoveryRefused("session is bound to another agent than the prior proof; recovery refused")
    if not prior.get("agent_id") and snap.agent_name != AGENT_NAME:
        raise RecoveryRefused(f"session agent is not {AGENT_NAME}; recovery refused")
    if getattr(snap, "runner_id", None):
        redact.add(str(snap.runner_id))
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(proof_path, raw_dir / "prior-session-proof.json")  # kept before overwrite
    proof = {k: prior[k] for k in ("agent", "started_at", "session_id", "agent_id", "session_created_at", "host_note")
             if k in prior}
    proof.update({"status": "recovering", "recovered_at": _now(), "attempts": list(prior.get("attempts") or [])})
    try:
        proof.update(await drive_and_collect(sessions, session_id, raw_dir, args, core_probe,
                                             prior_responses=prior_responses(prior)))
    except Exception as exc:  # noqa: BLE001 - a failed recovery attempt, never success
        proof.update({"turn_outcome": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": _now()})
    return proof


async def recover_session(args: argparse.Namespace, proof_path: Path, raw_dir: Path, prior: dict[str, Any],
                          core_probe: Callable[[], dict[str, Any]], redact: set[str], *, client_cls: Any = None) -> dict[str, Any]:
    client_cls = client_cls or _default_client()
    async with client_cls(base_url=args.server) as client:
        try:
            health = await client._http.get(f"{args.server}/health")
            if health.status_code != 200:
                raise LiveRunError(f"health {health.status_code}")
        except Exception as exc:  # noqa: BLE001 - nothing written yet
            raise RecoveryRefused(f"server not reachable: {type(exc).__name__}") from None
        return await run_recovery(client.sessions, args, proof_path, raw_dir, prior, core_probe, redact)


# ---------------------------------------------------------------------- bundle validation
def validate_bundle(agent_dir: Path = AGENT_DIR) -> dict[str, Any]:
    """Parse + validate the bundle with the installed Omnigent package (Omnigent tool interpreter)."""
    from omnigent.spec import parse, validate

    spec = parse(agent_dir)
    problems: list[str] = []
    report: dict[str, Any] = {}
    expected_mcp = {AGENT_NAME: set(), **ROLE_TOOLS}
    roots = set()
    for s in [spec, *spec.sub_agents]:
        res = validate(s)
        problems += [f"{s.name}: {e}" for e in getattr(res, "errors", [])]
        ex = s.executor
        mcp = {m.name: set(m.tools or []) for m in s.mcp_servers}
        envs = {m.name: dict(getattr(m, "env", None) or {}) for m in s.mcp_servers}
        report[s.name] = {"valid": res.valid, "model": ex.model, "harness": ex.config.get("harness"),
                          "reasoning_effort": ex.reasoning_effort, "mcp": {k: sorted(v) for k, v in mcp.items()},
                          "os_env": None if s.os_env is None else s.os_env.type}
        if ex.model != MODEL or ex.reasoning_effort != "medium" or ex.config.get("harness") != HARNESS:
            problems.append(f"{s.name}: executor must be {HARNESS}/{MODEL}/medium")
        if s.os_env is not None:
            problems.append(f"{s.name}: os_env must be absent (no shell/filesystem tools)")
        if s.name not in expected_mcp:
            problems.append(f"unexpected agent {s.name}")
            continue
        if {t for v in mcp.values() for t in v} != expected_mcp[s.name] or (mcp and set(mcp) != {MCP_SERVER}):
            problems.append(f"{s.name}: MCP surface {report[s.name]['mcp']} != {sorted(expected_mcp[s.name])}")
        if s.name in ROLE_OF:
            env = envs.get(MCP_SERVER, {})
            if env.get("INSPECTION_MCP_ROLE") != ROLE_OF[s.name]:
                problems.append(f"{s.name}: sidecar INSPECTION_MCP_ROLE must be {ROLE_OF[s.name]!r}")
            roots.add(env.get("INSPECTION_LIVE_ROOT"))
    if spec.name != AGENT_NAME:
        problems.append(f"root agent is {spec.name}, expected {AGENT_NAME}")
    if sorted(a.name for a in spec.sub_agents) != sorted([ANALYST, EXPERIMENTER]):
        problems.append("supervisor must have exactly the inspection_analyst and inspection_experimenter sub-agents")
    if len(roots) != 1 or None in roots:
        problems.append("both sidecars must name the same INSPECTION_LIVE_ROOT")
    for name, path in [(AGENT_NAME, agent_dir / "config.yaml"),
                       *[(a, agent_dir / "agents" / a / "config.yaml") for a in (ANALYST, EXPERIMENTER)]]:
        text = path.read_text(encoding="utf-8")
        if ("skills: none" not in text or f"limit: {TOOL_CAPS[name]}" not in text
                or re.search(r"^\s*os_env\s*:", text, re.MULTILINE)):
            problems.append(f"{name}: config must keep skills: none, no os_env and tool-call cap {TOOL_CAPS[name]}")
    return {"agents": report, "problems": problems}


# ---------------------------------------------------------------------- main
def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)


def _loopback_6771(url: str) -> bool:
    return re.fullmatch(rf"http://(127\.0\.0\.1|localhost|\[::1\]):{PORT}", url) is not None


def main(argv: list[str] | None = None, *, client_cls: Any = None, chat_factory: Any = None,
         core_probe: Callable[[], dict[str, Any]] | None = None, agent_dir: Path = AGENT_DIR,
         runtime: Path = RUNTIME) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default=f"http://127.0.0.1:{PORT}")
    ap.add_argument("--live-root", type=Path)
    ap.add_argument("--host-id", help="pick this online host when several are online")
    ap.add_argument("--timeout", type=float, default=1800.0, help="global seconds for the whole workflow")
    ap.add_argument("--poll-interval", type=float, default=5.0, help="seconds between GET status polls")
    ap.add_argument("--settle-grace", type=float, default=60.0,
                    help="seconds the root must stay idle with no busy sub-agent before the workflow counts as settled")
    ap.add_argument("--recover-session", metavar="SESSION_ID",
                    help="re-attach to the session named by this root's proof; creates nothing, sends nothing")
    ap.add_argument("--validate-bundle", action="store_true", help="only parse + validate the agent bundle")
    args = ap.parse_args(argv)
    if args.validate_bundle:
        report = validate_bundle(agent_dir)
        print(json.dumps(sanitize(report), indent=2, default=str))
        return 1 if report["problems"] else 0
    if args.live_root is None:
        ap.error("--live-root is required")
    args.server = args.server.rstrip("/")
    if not _loopback_6771(args.server):
        print(json.dumps({"ok": False, "status": "not_started",
                          "error": f"only the project-local loopback server on port {PORT} is allowed"}))
        return 2
    live_root = args.live_root.resolve()
    proof_path = live_root / PROOF_REL
    recovering = args.recover_session is not None
    redact: set[str] = set()
    prior: dict[str, Any] = {}
    try:
        python = check_sidecars(live_root, agent_dir)
        probe = core_probe or (lambda: core_status(python, live_root))
        st = probe()
        if recovering:
            if not proof_path.is_file():
                raise RecoveryRefused("this live root has no session proof; nothing to recover")
            prior = json.loads(proof_path.read_text(encoding="utf-8"))
            check_recovery(prior, args.recover_session, st)
        else:
            if proof_path.exists():
                raise LiveRunError("this live root already has a session proof; one session per prepared root "
                                   "(use --recover-session with that proof's session id to re-attach)")
            check_fresh(st)
    except LiveRunError as exc:
        print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(str(exc))}, indent=2))
        return 2
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}" + ("-recover" if recovering else "")
    raw_dir = runtime / "live-proof" / stamp
    try:
        if recovering:
            proof = asyncio.run(recover_session(args, proof_path, raw_dir, prior, probe, redact, client_cls=client_cls))
        else:
            proof = asyncio.run(run_session(args, proof_path, raw_dir, probe, redact,
                                            client_cls=client_cls, chat_factory=chat_factory))
    except RecoveryRefused as exc:
        print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(str(exc))}, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never turned into success
        if recovering:
            print(json.dumps({"ok": False, "status": "recovery_failed", "proof_written": False,
                              "error": scrub_text(f"{type(exc).__name__}: {exc}")}, indent=2))
            return 1
        proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.exists() else {"agent": AGENT_NAME}
        proof.update({"turn_outcome": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": _now()})
        if "session_id" not in proof:
            print(json.dumps({"ok": False, "status": "not_started", "error": scrub_text(proof["error"])}, indent=2))
            return 1
    core_raw = proof.pop("_core_raw", None)
    status, reasons = judge(proof.get("turn_outcome", "failed"), core_raw, proof.get("orchestration"))
    attempts = proof.get("attempts") if isinstance(proof.get("attempts"), list) else []
    attempts = attempts + [{"attempt": len(attempts) + 1, "mode": "recover" if recovering else "send", "status": status,
                            "turn_outcome": proof.get("turn_outcome"), "error": proof.get("error"),
                            "failure_reasons": reasons, "finished_at": proof.get("finished_at"),
                            "response_ids": [r.get("response_id") for r in proof.get("responses") or []
                                             if not r.get("from_prior_attempt")]}]
    proof.update({"status": status, "failure_reasons": reasons, "attempts": attempts, "raw_events_local_only": True,
                  "data_label": ("real Omnigent SDK session over the paid inspection live core; authored numeric synthetic "
                                 "live demonstration, separate from the v3 primary benchmark and classifier accuracy")})
    if raw_dir.exists():
        _write_json(raw_dir / "proof-unsanitized.json", proof)
    _write_json(proof_path, sanitize(proof, frozenset(redact)))
    core = proof.get("core") or {}
    print(json.dumps({"ok": status == "completed", "status": status, "session_id": proof.get("session_id"),
                      "mode": "recover" if recovering else "send", "attempt": len(attempts),
                      "failure_reasons": [scrub_text(r) for r in reasons], "proof": str(PROOF_REL),
                      "delegations": (proof.get("orchestration") or {}).get("delegation_count"),
                      "core": {"closed": core.get("closed"), "updates": len(core.get("updates") or [])}},
                     indent=2, default=str))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
