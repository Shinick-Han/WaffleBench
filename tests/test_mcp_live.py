"""Live MCP loop: information flow, bounded budget, idempotency, export (NON-SCIENTIFIC fixtures).

Every call here goes straight to ``LiveLoop`` with an explicitly non-scientific
actor label; none of it is an Omnigent session or scientific evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

from test_core_support import fixture_campaign, fresh_dir

from falsify_lab import benchmark as bm
from falsify_lab import mcp_server as ms
from falsify_lab.reporting import build_snapshot
from falsify_lab.simulator import FixtureSimulator
from falsify_lab.storage import BudgetExhausted, Campaign

ANALYST = "NON-SCIENTIFIC fixture test (analyst role)"
EXPERIMENTER = "NON-SCIENTIFIC fixture test (experimenter role)"
RID = ms.LIVE_RUN_ID


def prepared(name: str):
    c = fixture_campaign(name)
    bm.demo_prepare(c)
    return c, ms.LiveLoop(c.root, FixtureSimulator)


def ledger_len(c: Campaign) -> int:
    return len(c.events())


def loop_once(loop: ms.LiveLoop) -> dict:
    plan = loop.analyze_and_plan(False, ANALYST)
    d = plan["decision"]
    res = loop.simulate_pvt_point(d["selected_point_id"], d["decision_sequence"], EXPERIMENTER)
    assert res["status"] == "success", res
    return plan


class RootTests(unittest.TestCase):
    def test_unprepared_roots_are_refused(self):
        with self.assertRaisesRegex(ms.LiveLoopError, "prepared demo campaign"):
            ms.LiveLoop(None, FixtureSimulator).analyze_and_plan(False, ANALYST)
        missing = fresh_dir("mcp-missing-root")
        with self.assertRaisesRegex(ms.LiveLoopError, "prepared demo campaign"):
            ms.LiveLoop(missing, FixtureSimulator).analyze_and_plan(False, ANALYST)
        self.assertFalse(missing.exists())  # nothing created
        c = fixture_campaign("mcp-calibrated-only")
        bm.calibrate(c)
        n = ledger_len(c)
        with self.assertRaisesRegex(ms.LiveLoopError, "not prepared"):
            ms.LiveLoop(c.root, FixtureSimulator).analyze_and_plan(False, ANALYST)
        self.assertEqual(ledger_len(c), n)

    def test_role_gating_and_no_path_parameters(self):
        c, _ = prepared("mcp-roles")
        with mock.patch.dict(os.environ, {"FALSIFY_RUNS_DIR": str(c.root)}, clear=False):
            os.environ.pop("FALSIFY_MCP_ROLE", None)
            ms._LOOP = None
            self.assertIn("not available", ms._call("analyze_and_plan", lambda a: {}).get("error", ""))
            os.environ["FALSIFY_MCP_ROLE"] = "experimenter"
            self.assertIn("not available", ms._call("analyze_and_plan", lambda a: {}).get("error", ""))
            self.assertEqual(ms._call("simulate_pvt_point", lambda a: {"actor": a}), {"actor": "Omnigent experimenter"})
        ms._LOOP = None
        tools = asyncio.run(ms.build_server().list_tools())
        self.assertEqual({t.name for t in tools}, {"analyze_and_plan", "simulate_pvt_point", "read_result"})
        params = {p for t in tools for p in (t.inputSchema.get("properties") or {})}
        self.assertEqual(params, {"finalize", "point_id", "decision_sequence", "result_id"})


class LoopTests(unittest.TestCase):
    def test_two_loops_then_finalize_complete_and_export(self):
        c, loop = prepared("mcp-two-loops")
        plan1 = loop.analyze_and_plan(False, ANALYST)
        self.assertEqual(plan1["status"], "decision_recorded")
        self.assertIsNone(plan1["update"])  # preparation is not an adaptive update
        d1 = plan1["decision"]
        roles = {x["role"] for x in d1["candidates"]}
        self.assertGreaterEqual(len({x["point_id"] for x in d1["candidates"]}), 2)
        self.assertTrue({"exploitation", "exploration"} <= roles)
        self.assertTrue(all(x["cost_queries"] == 1 and "distance_bonus" in x["score_parts"] for x in d1["candidates"]))
        self.assertEqual(plan1["budget"]["limit"], 16)
        self.assertEqual(plan1["budget"]["remaining"], 4)
        self.assertTrue(set(d1["evidence_result_ids"]) <= set(c.revealed_result_ids(RID)))

        # Idempotent: the pending decision is returned again, nothing appended.
        n = ledger_len(c)
        again = loop.analyze_and_plan(False, ANALYST)
        self.assertEqual((again["status"], again["decision"]["decision_sequence"]), ("decision_pending", 1))
        self.assertEqual(ledger_len(c), n)

        # Denied without spending budget: held-out, other point, wrong decision, malformed.
        held = c.protocol.held_out[0].id
        other = next(p.id for p in c.protocol.search_pool if p.id != d1["selected_point_id"])
        for pid, seq in ((held, 1), (other, 1), (d1["selected_point_id"], 2), ("XX|9|9", 1), (d1["selected_point_id"], "1")):
            with self.assertRaisesRegex(ms.LiveLoopError, "denied"):
                loop.simulate_pvt_point(pid, seq, EXPERIMENTER)
        self.assertEqual(c.run_state(RID)["used"], 12)
        self.assertEqual(ledger_len(c), n)

        r1 = loop.simulate_pvt_point(d1["selected_point_id"], 1, EXPERIMENTER)
        self.assertEqual(r1["status"], "success")
        self.assertEqual(r1["observation"]["point_id"], d1["selected_point_id"])
        with self.assertRaisesRegex(ms.LiveLoopError, "no recorded decision"):
            loop.simulate_pvt_point(d1["selected_point_id"], 1, EXPERIMENTER)

        plan2 = loop.analyze_and_plan(False, ANALYST)
        u1 = plan2["update"]
        self.assertEqual(u1["observed_result_id"], r1["observation"]["result_id"])
        self.assertEqual((u1["update_index"], u1["decision_sequence"], u1["next_decision"]), (1, 1, "recorded"))
        self.assertEqual(u1["selection_changed"], u1["rank_before"][0] != u1["rank_after"][0])
        self.assertEqual(u1["next_selected_point_id"], plan2["decision"]["selected_point_id"])
        n = ledger_len(c)
        self.assertEqual(loop.analyze_and_plan(False, ANALYST)["status"], "decision_pending")
        self.assertEqual(ledger_len(c), n)

        r2 = loop.simulate_pvt_point(plan2["decision"]["selected_point_id"], 2, EXPERIMENTER)
        fin = loop.analyze_and_plan(True, ANALYST)
        self.assertEqual(fin["status"], "closed")
        self.assertEqual(fin["update"]["observed_result_id"], r2["observation"]["result_id"])
        self.assertEqual(fin["update"]["next_decision"], "not_recorded_finalized")
        self.assertFalse(fin["next_preview_unrecorded"]["recorded"])
        closed = fin["closed"]
        self.assertEqual((closed["status"], closed["termination_reason"]), ("complete", "live_finalized"))
        self.assertEqual((closed["logical_queries"], closed["remaining_budget"], closed["search_queries"]), (14, 2, 2))
        st = c.run_state(RID)
        self.assertEqual(len(st["decisions"]), 2)  # finalize records no executable decision
        n = ledger_len(c)
        self.assertEqual(loop.analyze_and_plan(True, ANALYST)["status"], "closed")
        self.assertEqual(ledger_len(c), n)
        with self.assertRaisesRegex(ms.LiveLoopError, "closed"):
            loop.simulate_pvt_point(fin["next_preview_unrecorded"]["selected_point_id"], 3, EXPERIMENTER)

        # read_result: revealed ids only.
        self.assertEqual(loop.read_result(r1["observation"]["result_id"])["point_id"], d1["selected_point_id"])
        other_run = bm.open_and_seed(c, "reproduce", bm.ADAPTIVE, 1002)
        for bad in ("res_0000000000000000", "../ledger", c.revealed_result_ids(other_run)[-1]):
            with self.assertRaisesRegex(ms.LiveLoopError, "denied"):
                loop.read_result(bad)

        snap = build_snapshot(c, RID)
        json.dumps(snap, allow_nan=False)
        self.assertEqual(snap["run"]["status"], "complete")
        self.assertEqual(snap["run"]["budget"], {"limit": 16, "used": 14, "remaining": 2})
        self.assertEqual(len(snap["updates"]), 2)
        for d in snap["decisions"]:
            pru = d["post_result_update"]
            self.assertEqual(pru["observed_result_id"], d["observed_result_id"])
            self.assertEqual(d["actor"], ANALYST)
            self.assertTrue(set(pru) >= {"observed_result_id", "rank_before", "rank_after", "selection_changed", "next_candidates"})
        stored = {e["payload"]["sequence"]: e["payload"] for e in c.events("decision")}
        self.assertEqual([d["rank_before"] for d in snap["decisions"]], [stored[1]["rank_before"], stored[2]["rank_before"]])
        revealed = set(c.revealed_result_ids(RID))
        live_trace = [t for t in snap["trace"] if t["action"] in ("analysis_update", "mcp_tool_call")]
        self.assertEqual(len([t for t in live_trace if t["action"] == "analysis_update"]), 2)
        for t in snap["trace"]:
            self.assertTrue(set(t["result_ids"]) <= revealed, t)
            self.assertFalse(t["actor"].startswith("Omnigent"))  # fixture calls are never labelled Omnigent
        search_results = [t for t in snap["trace"] if t["action"] == "query_result" and t["detail"].get("phase") == "search"]
        self.assertEqual([t["detail"]["decision_sequence"] for t in search_results], [1, 2])
        self.assertTrue(all(t["actor"] == EXPERIMENTER and t["detail"]["via_tool"] == "simulate_pvt_point" for t in search_results))
        calib = [t for t in snap["trace"] if t["action"] == "query_result" and t["detail"].get("phase") == "calibration"]
        self.assertTrue(all(t["actor"] == "deterministic runner" for t in calib))
        self.assertIsNone(snap["omnigent_session"])

    def test_max_four_then_fifth_rejected(self):
        c, loop = prepared("mcp-max-four")
        for _ in range(4):
            loop_once(loop)
        n_dec = len(c.run_state(RID)["decisions"])
        fifth = loop.analyze_and_plan(False, ANALYST)
        self.assertEqual(fifth["status"], "search_quota_reached")
        self.assertEqual(fifth["update"]["next_decision"], "not_recorded_search_quota_reached")
        self.assertEqual(len(c.run_state(RID)["decisions"]), n_dec)
        preview = bm.preview_decision(c, RID)
        with self.assertRaises(BudgetExhausted):
            c.record_decision(RID, preview)
        n = ledger_len(c)
        self.assertEqual(loop.analyze_and_plan(False, ANALYST)["status"], "search_quota_reached")
        self.assertEqual(ledger_len(c), n)  # update not duplicated
        fin = loop.analyze_and_plan(True, ANALYST)
        self.assertIsNone(fin["update"])
        self.assertEqual((fin["closed"]["status"], fin["closed"]["logical_queries"], fin["closed"]["remaining_budget"]), ("complete", 16, 0))
        self.assertEqual(fin["budget"]["adaptive_updates"], 4)

    def test_failed_selected_query_spends_and_stops(self):
        c, loop = prepared("mcp-failure")
        d = loop.analyze_and_plan(False, ANALYST)["decision"]
        failing = ms.LiveLoop(c.root, lambda: FixtureSimulator(fail={d["selected_point_id"]}))
        res = failing.simulate_pvt_point(d["selected_point_id"], 1, EXPERIMENTER)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((res["closed"]["status"], res["closed"]["termination_reason"]), ("incomplete", "query_failed"))
        st = c.run_state(RID)
        self.assertEqual(st["used"], 13)
        self.assertEqual(st["queries"][-1]["status"], "failed")
        self.assertTrue((c.root / "attempts").exists())  # failure evidence preserved
        after = loop.analyze_and_plan(False, ANALYST)
        self.assertEqual(after["status"], "closed")
        self.assertEqual(len(c.run_state(RID)["decisions"]), 1)  # never replaced

    def test_fewer_than_two_or_invalid_stop_is_incomplete(self):
        c, loop = prepared("mcp-early-finalize")
        loop_once(loop)
        fin = loop.analyze_and_plan(True, ANALYST)
        self.assertEqual(fin["closed"]["status"], "incomplete")
        self.assertEqual(fin["budget"]["adaptive_updates"], 1)
        c2, loop2 = prepared("mcp-finalize-pending")
        loop_once(loop2)
        loop_once(loop2)
        loop2.analyze_and_plan(False, ANALYST)  # decision 3 recorded, not executed
        self.assertEqual(loop2.analyze_and_plan(True, ANALYST)["closed"]["status"], "incomplete")
        c3, loop3 = prepared("mcp-manual-stop")
        loop_once(loop3)
        loop_once(loop3)
        self.assertEqual(c3.close_run(RID, "manual stop")["status"], "incomplete")


class BudgetIsolationTests(unittest.TestCase):
    def test_benchmark_budget_and_fixed_policy_unchanged(self):
        c = fixture_campaign("mcp-benchmark-unchanged")
        bm.calibrate(c)
        closed = bm.run_policy(c, bm.ADAPTIVE, 1001)
        self.assertEqual((closed["status"], closed["logical_queries"]), ("complete", 24))
        st = c.run_state(f"benchmark-{bm.ADAPTIVE}-1001")
        self.assertEqual((st["budget"], st["quotas"]), (24, {"calibration": 9, "initial": 3, "search": 12}))
        self.assertNotIn("search_min", st)
        rep = c.open_run("reproduce", "random", 1001)
        self.assertEqual(c.run_state(rep)["budget"], 24)
        # The live tools record the same frozen rule's choices as the benchmark runner.
        lc, loop = prepared("mcp-same-rule")
        live = [loop_once(loop)["decision"]["selected_point_id"] for _ in range(2)]
        self.assertEqual(live, [d["selected_point_id"] for d in st["decisions"][:2]])


if __name__ == "__main__":
    unittest.main()
