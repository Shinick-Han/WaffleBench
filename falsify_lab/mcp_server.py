"""stdio MCP server for the prepared live Omnigent loop (seed 1001).

``FALSIFY_RUNS_DIR`` names one demo campaign root already prepared by
``python -m falsify_lab.cli demo-prepare``. Every tool acts on the fixed run
``live-adaptive_idw_plus_distance-1001``; callers cannot pass a root, run id or
path, and the server offers no shell or filesystem tools.

``FALSIFY_MCP_ROLE`` (``analyst`` or ``experimenter``, written into each
specialist's sidecar by ``launch.ps1 -Setup``) selects the tools that role may
call and the ``Omnigent <role>`` actor label stored in the ledger. Without a
role the server refuses every tool.

Live loop (RESEARCH_PROTOCOL.md, "Omnigent 협업과 다음 판단")::

    analyst  analyze_and_plan()            -> records decision 1 (two candidates)
    experimenter simulate_pvt_point(p, 1)  -> spends one query on exactly that point
    analyst  analyze_and_plan()            -> one analysis_update for that result + decision 2
    experimenter simulate_pvt_point(p, 2)
    analyst  analyze_and_plan(finalize=True) -> update for result 2, unrecorded next preview, run closed

The selection is the frozen deterministic rule (``benchmark.preview_decision``);
the agents carry evidence between steps, they do not change the choice.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from falsify_lab import benchmark as bm  # noqa: E402
from falsify_lab.protocol import Point, ProtocolError  # noqa: E402
from falsify_lab.storage import (  # noqa: E402
    LIVE_FINALIZED,
    AccessDenied,
    Campaign,
    CapReached,
    StorageError,
)

LIVE_SEED = 1001
LIVE_RUN_ID = f"live-{bm.ADAPTIVE}-{LIVE_SEED}"
UPDATE_EVENT = "analysis_update"
TOOL_EVENT = "mcp_tool_call"
ROLE_TOOLS = {
    "analyst": ("analyze_and_plan", "read_result"),
    "experimenter": ("simulate_pvt_point", "read_result"),
}
RESULT_ID = re.compile(r"^res_[0-9a-f]{16}$")
_ABS_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var)/)[^\s'\"]*")


class LiveLoopError(Exception):
    """A refused tool call. Nothing was recorded and no budget was spent unless stated."""


class LiveLoop:
    """Tool logic over one prepared demo root. The MCP layer adds role and actor labels."""

    def __init__(self, root: Path | str | None, simulator_factory: Callable[[], Any]):
        self.root = Path(root).resolve() if root else None
        self._sim_factory = simulator_factory
        self._campaign: Campaign | None = None

    # ------------------------------------------------------------------ setup
    def _clean(self, msg: str) -> str:
        if self.root is not None:
            msg = msg.replace(str(self.root), "<demo-root>")
        return _ABS_PATH.sub("<path>", msg)

    def campaign(self) -> Campaign:
        if self._campaign is not None:
            return self._campaign
        if self.root is None or not (self.root / "ledger" / "events.jsonl").is_file():
            raise LiveLoopError("FALSIFY_RUNS_DIR does not name a prepared demo campaign; run demo-prepare first")
        try:
            c = Campaign(self.root, self._sim_factory())
        except StorageError as exc:
            raise LiveLoopError(f"demo campaign cannot be opened: {self._clean(str(exc))}") from exc
        if c.kind not in ("demo", "fixture"):
            raise LiveLoopError(f"campaign kind {c.kind!r} is not a demo campaign")
        if not any(e["payload"].get("run_id") == LIVE_RUN_ID for e in c.events("live_prepared")):
            raise LiveLoopError("demo campaign is not prepared (no live_prepared record); run demo-prepare first")
        st = c.run_state(LIVE_RUN_ID)
        b = c.protocol.budget
        if st["budget"] != int(b["live_demo_max"]) or st["quotas"].get("search") != int(c.protocol.manifest["live_demo"]["adaptive_updates_max"]):
            raise LiveLoopError("live run was prepared with a non-live budget; prepare a fresh demo root")
        self._campaign = c
        return c

    def _label(self, c: Campaign) -> str:
        return "NON-SCIENTIFIC FIXTURE DATA" if c.kind == "fixture" else "ngspice evidence, demo campaign (not the policy benchmark)"

    # ------------------------------------------------------------------ views
    @staticmethod
    def _pending_decision(st: dict[str, Any]) -> dict[str, Any] | None:
        executed = {q.get("decision_sequence") for q in st["queries"]}
        return next((d for d in st["decisions"] if d["sequence"] not in executed), None)

    @staticmethod
    def _updates(c: Campaign) -> list[dict[str, Any]]:
        return [e["payload"] for e in c.events(UPDATE_EVENT) if e["payload"].get("run_id") == LIVE_RUN_ID]

    @staticmethod
    def _budget(st: dict[str, Any], updates: list[dict[str, Any]]) -> dict[str, Any]:
        search = [q for q in st["queries"] if q["phase"] == "search"]
        return {
            "limit": st["budget"],
            "used": st["used"],
            "remaining": st["remaining"],
            "search_used": len(search),
            "search_successful": sum(q["status"] == "success" for q in search),
            "search_min": st.get("search_min"),
            "search_max": st["quotas"]["search"],
            "adaptive_updates": len(updates),
            "cost_per_candidate_test": 1,
        }

    def _candidates(self, c: Campaign, cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pp = c.protocol.policy_parameters
        weight, divisor = float(pp["exploration_weight"]), float(pp["distance_divisor"])
        out = []
        for cand in cands:
            bonus = weight * cand["min_normalized_distance"] / divisor
            out.append({
                **cand,
                "score_parts": {
                    "idw_predicted_abs_error": cand["idw_predicted_abs_error"],
                    "distance_bonus": bonus,
                    "rule": "score = idw_predicted_abs_error + 0.05 * min_normalized_distance / 2",
                },
            })
        return out

    def _decision_view(self, c: Campaign, d: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision_sequence": d["sequence"],
            "selected_point_id": d["selected_point_id"],
            "candidates": self._candidates(c, d["candidates"]),
            "evidence_result_ids": d["evidence_result_ids"],
            "remaining_budget_before_query": d["remaining_budget"],
            "score_rule": d.get("score_rule"),
            "actor": d.get("actor"),
        }

    @staticmethod
    def _observations(st: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for q in st["queries"]:
            if q["phase"] == "calibration":
                continue
            ev = q.get("evaluation") or {}
            rows.append({
                "result_id": q.get("result_id"), "point_id": q["point_id"], "phase": q["phase"], "query_index": q["query_index"],
                "decision_sequence": q.get("decision_sequence"), "status": q.get("status"),
                "abs_relative_error": ev.get("abs_relative_error"), "clear_counterexample": ev.get("clear_counterexample"),
                "secondary_counterexample": ev.get("secondary_counterexample"),
            })
        return rows

    def _state(self, c: Campaign, st: dict[str, Any], updates: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "run_id": LIVE_RUN_ID,
            "data_label": self._label(c),
            "budget": self._budget(st, updates),
            "calibration_result_ids": [q["result_id"] for q in st["queries"] if q["phase"] == "calibration"],
            "search_observations": self._observations(st),
            "updates": updates,
        }

    # ------------------------------------------------------------------ analyst
    def analyze_and_plan(self, finalize: bool, actor: str) -> dict[str, Any]:
        c = self.campaign()
        with c.lock():
            st = c.run_state(LIVE_RUN_ID)
            updates = self._updates(c)
            if st["closed"]:
                return {"status": "closed", "closed": st["closed"], **self._state(c, st, updates)}
            if any(q["status"] == "pending" for q in st["queries"]):
                raise LiveLoopError("a search query is still running; call again after the experimenter reports")
            last = st["queries"][-1]
            if last["phase"] == "search" and last["status"] != "success":
                closed = c.close_run(LIVE_RUN_ID, "query_failed")
                return {"status": "closed", "closed": closed, **self._state(c, c.run_state(LIVE_RUN_ID), updates)}
            pending = self._pending_decision(st)
            if pending is not None and not finalize:
                # Idempotent: the recorded decision is returned again, nothing is appended.
                return {"status": "decision_pending", "decision": self._decision_view(c, pending),
                        "latest_update": updates[-1] if updates else None, **self._state(c, st, updates)}
            search_used = sum(q["phase"] == "search" for q in st["queries"])
            quota_left = search_used < st["quotas"]["search"]
            t0 = time.perf_counter()
            preview = bm.preview_decision(c, LIVE_RUN_ID)
            policy_seconds = time.perf_counter() - t0
            will_record = pending is None and not finalize and quota_left
            if will_record and (preview is None or len({x["point_id"] for x in preview["candidates"]}) < 2):
                raise LiveLoopError("fewer than two distinct candidate tests remain; finalize instead")
            new_update = None
            observed = {u["observed_result_id"] for u in updates}
            if last["phase"] == "search" and last["result_id"] not in observed and preview is not None:
                # One update per actual search observation: same remaining set, ranked
                # without (rank_before) and with (rank_after) the newly observed result.
                new_update = {
                    "run_id": LIVE_RUN_ID,
                    "actor": actor,
                    "update_index": len(updates) + 1,
                    "observed_result_id": last["result_id"],
                    "observed_point_id": last["point_id"],
                    "query_index": last["query_index"],
                    "decision_sequence": last["decision_sequence"],
                    "observed_abs_relative_error": last["evaluation"]["abs_relative_error"],
                    "observed_clear_counterexample": last["evaluation"]["clear_counterexample"],
                    "remaining_candidates": len(bm.policy_inputs(c, st)["candidates"]),
                    "rank_before": preview["rank_before"],
                    "rank_after": preview["rank_after"],
                    "selection_changed": preview["selection_changed"],
                    "next_candidates": preview["candidates"],
                    "next_selected_point_id": preview["selected_point_id"],
                    "next_decision": "recorded" if will_record else ("not_recorded_finalized" if finalize else "not_recorded_search_quota_reached"),
                    "evidence_result_ids": preview["evidence_result_ids"],
                }
                c.append(UPDATE_EVENT, new_update)
                updates = [*updates, new_update]
            if finalize:
                closed = c.close_run(LIVE_RUN_ID, LIVE_FINALIZED)
                c.append(TOOL_EVENT, {"run_id": LIVE_RUN_ID, "actor": actor, "tool": "analyze_and_plan", "finalize": True,
                                      "status": closed["status"],
                                      "result_ids": [new_update["observed_result_id"]] if new_update else []})
                return {
                    "status": "closed", "closed": closed, "update": new_update,
                    "next_preview_unrecorded": None if preview is None else {
                        "selected_point_id": preview["selected_point_id"],
                        "candidates": self._candidates(c, preview["candidates"]),
                        "recorded": False,
                    },
                    **self._state(c, c.run_state(LIVE_RUN_ID), updates),
                }
            if not quota_left:
                return {"status": "search_quota_reached", "update": new_update,
                        "message": "all live search queries are used; call analyze_and_plan(finalize=true)",
                        **self._state(c, st, updates)}
            decision = {**preview, "policy_seconds": policy_seconds, "actor": actor, "via": "mcp analyze_and_plan"}
            rec = c.record_decision(LIVE_RUN_ID, decision)
            c.append(TOOL_EVENT, {"run_id": LIVE_RUN_ID, "actor": actor, "tool": "analyze_and_plan", "finalize": False,
                                  "status": "decision_recorded", "decision_sequence": rec["sequence"],
                                  "result_ids": [new_update["observed_result_id"]] if new_update else []})
            st = c.run_state(LIVE_RUN_ID)
            return {"status": "decision_recorded", "decision": self._decision_view(c, rec), "update": new_update,
                    **self._state(c, st, updates)}

    # ------------------------------------------------------------------ experimenter
    def simulate_pvt_point(self, point_id: str, decision_sequence: int, actor: str) -> dict[str, Any]:
        c = self.campaign()
        try:
            point = Point.parse(point_id)
        except ProtocolError as exc:
            raise LiveLoopError(f"denied: {exc}; no budget spent") from exc
        if isinstance(decision_sequence, bool) or not isinstance(decision_sequence, int):
            raise LiveLoopError("denied: decision_sequence must be an integer; no budget spent")
        with c.lock():
            st = c.run_state(LIVE_RUN_ID)
            if st["closed"]:
                raise LiveLoopError(f"denied: live run is closed ({st['closed']['status']}); no budget spent")
            pending = self._pending_decision(st)
            if pending is None:
                raise LiveLoopError("denied: no recorded decision awaits execution; ask the analyst first; no budget spent")
            if pending["sequence"] != decision_sequence or pending["selected_point_id"] != point.id:
                raise LiveLoopError(
                    f"denied: only the recorded selection {pending['selected_point_id']} (decision {pending['sequence']}) "
                    "may be simulated; no budget spent"
                )
            used_before = st["used"]
        try:
            res = c.query(LIVE_RUN_ID, point.id, "search", decision_sequence)
        except StorageError as exc:  # includes CapReached
            st = c.run_state(LIVE_RUN_ID)
            if st["used"] == used_before and not isinstance(exc, CapReached):
                raise LiveLoopError(f"denied: {self._clean(str(exc))}; no budget spent") from exc
            # Admitted (or the campaign cap stops everything): stop the live run, never replace.
            closed = c.close_run(LIVE_RUN_ID, getattr(exc, "reason", "query_error")) if st["closed"] is None else st["closed"]
            c.append(TOOL_EVENT, {"run_id": LIVE_RUN_ID, "actor": actor, "tool": "simulate_pvt_point", "status": "failed",
                                  "decision_sequence": decision_sequence, "result_ids": []})
            spent = st["used"] - used_before
            raise LiveLoopError(
                f"{type(exc).__name__}: {self._clean(str(exc))}; {spent} query spent; live run closed {closed['status']}"
            ) from exc
        c.append(TOOL_EVENT, {"run_id": LIVE_RUN_ID, "actor": actor, "tool": "simulate_pvt_point", "status": res["status"],
                              "decision_sequence": decision_sequence, "query_index": res["query_index"],
                              "result_ids": [res["result_id"]] if res.get("result_id") else []})
        if res["status"] != "success":
            # A failed selected query is spent, never replaced, and stops the live run.
            closed = c.close_run(LIVE_RUN_ID, "query_failed")
            st = c.run_state(LIVE_RUN_ID)
            return {"status": "failed", "error": self._clean(res.get("error", "")), "query_index": res["query_index"],
                    "point_id": point.id, "decision_sequence": decision_sequence, "closed": closed,
                    "budget": self._budget(st, self._updates(c)), "data_label": self._label(c)}
        obs = c.get_observation(LIVE_RUN_ID, res["result_id"])
        st = c.run_state(LIVE_RUN_ID)
        return {"status": "success", "decision_sequence": decision_sequence, "query_index": res["query_index"],
                "observation": obs, "budget": self._budget(st, self._updates(c)), "data_label": self._label(c)}

    # ------------------------------------------------------------------ shared
    def read_result(self, result_id: str) -> dict[str, Any]:
        c = self.campaign()
        if not isinstance(result_id, str) or not RESULT_ID.match(result_id):
            raise LiveLoopError("denied: malformed result_id")
        try:
            obs = c.get_observation(LIVE_RUN_ID, result_id)
        except AccessDenied as exc:
            raise LiveLoopError(f"denied: {exc}") from exc
        return {**obs, "data_label": self._label(c)}


# ---------------------------------------------------------------------- MCP surface
def _default_simulator() -> Any:
    from falsify_lab.simulator import NgspiceSimulator

    return NgspiceSimulator()


_LOOP: LiveLoop | None = None


def _loop() -> LiveLoop:
    global _LOOP
    if _LOOP is None:
        _LOOP = LiveLoop(os.environ.get("FALSIFY_RUNS_DIR") or None, _default_simulator)
    return _LOOP


def _call(tool: str, fn: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    role = os.environ.get("FALSIFY_MCP_ROLE", "")
    if tool not in ROLE_TOOLS.get(role, ()):
        return {"error": f"tool {tool} is not available to role {role or '<unset>'}"}
    try:
        return fn(f"Omnigent {role}")
    except LiveLoopError as exc:
        return {"error": str(exc)}
    except (StorageError, ProtocolError) as exc:
        return {"error": f"{type(exc).__name__}: {_loop()._clean(str(exc))}"}


def build_server() -> Any:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("falsify-lab")

    @mcp.tool()
    def analyze_and_plan(finalize: bool = False) -> dict[str, Any]:
        """Analyst: evaluate the stored observations of the prepared live run (seed 1001), record
        one analysis_update for the newest search result, and record the fixed rule's next decision
        with at least two candidate tests (exploitation/exploration), evidence result_ids, score
        parts and budget. Calling again before the experimenter runs returns the same pending
        decision. finalize=true records the last update, shows the next preview WITHOUT recording
        it, and closes the live run."""
        return _call("analyze_and_plan", lambda actor: _loop().analyze_and_plan(bool(finalize), actor))

    @mcp.tool()
    def simulate_pvt_point(point_id: str, decision_sequence: int) -> dict[str, Any]:
        """Experimenter: run ngspice for exactly the currently recorded selected point
        (point_id and decision_sequence copied from the analyst's decision). Any other point is
        denied without spending budget. A failed simulation spends the query and stops the run."""
        return _call("simulate_pvt_point", lambda actor: _loop().simulate_pvt_point(point_id, decision_sequence, actor))

    @mcp.tool()
    def read_result(result_id: str) -> dict[str, Any]:
        """Read one stored observation that was revealed in the live run (calibration, initial or
        search). Unrevealed, held-out, other-run or malformed ids are denied."""
        return _call("read_result", lambda actor: _loop().read_result(result_id))

    return mcp


if __name__ == "__main__":
    build_server().run()
