"""Ledger, admission, cache and access checks (NON-SCIENTIFIC fixture simulator)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import unittest

from test_core_support import ROOT, fixture_campaign

from falsify_lab import benchmark as bm
from falsify_lab.protocol import Point, load_protocol
from falsify_lab.simulator import FixtureSimulator
from falsify_lab.storage import (
    AccessDenied,
    BudgetExhausted,
    CacheIntegrityError,
    Campaign,
    CapReached,
    LedgerCorrupt,
    StorageError,
)

PROTO = load_protocol()
SEARCH = [p for p in PROTO.search_pool]


def _subprocess(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class LedgerTests(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        c = fixture_campaign("ledger-tamper")
        bm.calibrate(c)
        n = c.verify_ledger()
        self.assertGreater(n, 20)
        lines = c.events_path.read_text(encoding="utf-8").splitlines(keepends=True)
        ev = json.loads(lines[3])
        ev["payload"]["point_id"] = "TT|1.500|0.0"
        lines[3] = json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n"
        c.events_path.write_text("".join(lines), encoding="utf-8", newline="\n")
        with self.assertRaises(LedgerCorrupt):
            Campaign(c.root, FixtureSimulator())

    def test_fresh_root_required_and_no_mixing(self):
        c = fixture_campaign("fresh-root")
        with self.assertRaises(StorageError):
            Campaign.create(c.root, "fixture", FixtureSimulator())
        with self.assertRaises(StorageError):
            Campaign(c.root, FixtureSimulator(variant="other"))  # incompatible simulator fingerprint
        with self.assertRaises(StorageError):
            Campaign.create(c.root.parent / (c.root.name + "-p"), "primary", FixtureSimulator())


class AdmissionTests(unittest.TestCase):
    def test_same_instance_threads_respect_attempt_cap(self):
        c = fixture_campaign("thread-admission", FixtureSimulator(sleep_s=0.05), caps={"attempts_max": 5})
        outcomes: list[str] = []
        guard = threading.Lock()

        def work(pt: Point) -> None:
            try:
                c.simulate(pt, "basic", purpose="preflight")
                r = "ok"
            except CapReached:
                r = "cap"
            with guard:
                outcomes.append(r)

        threads = [threading.Thread(target=work, args=(pt,)) for pt in SEARCH[:12]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 5)
        self.assertEqual(outcomes.count("cap"), 7)
        ids = [e["payload"]["attempt_id"] for e in c.events("attempt_admitted")]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)
        c.verify_ledger()

    def test_cross_process_attempt_admission_is_atomic(self):
        c = fixture_campaign("process-admission", caps={"attempts_max": 6})
        procs = []
        for k in range(3):
            pts = [p.id for p in SEARCH[k * 5:(k + 1) * 5]]
            procs.append(_subprocess(f"""
                import sys; sys.path.insert(0, '.')
                from falsify_lab.storage import Campaign, CapReached
                from falsify_lab.simulator import FixtureSimulator
                from falsify_lab.protocol import Point
                c = Campaign({str(c.root)!r}, FixtureSimulator(sleep_s=0.02))
                ok = 0
                for pid in {pts!r}:
                    try:
                        c.simulate(Point.parse(pid), 'basic', purpose='preflight'); ok += 1
                    except CapReached:
                        pass
                print(ok)
            """))
        oks = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            oks.append(int(out.strip()))
        self.assertEqual(sum(oks), 6)
        self.assertEqual(len(c.events("attempt_admitted")), 6)
        self.assertEqual(c.verify_ledger(), len(c.events()))

    def test_two_process_cache_publication_is_consistent(self):
        c = fixture_campaign("process-cache", FixtureSimulator())
        pid = SEARCH[0].id
        reader = _subprocess(f"""
            import sys, time; sys.path.insert(0, '.')
            from falsify_lab.storage import Campaign
            from falsify_lab.simulator import FixtureSimulator
            from falsify_lab.protocol import Point
            c = Campaign({str(c.root)!r}, FixtureSimulator())
            seen = 0; end = time.time() + 3
            while time.time() < end:
                if c.lookup_cached(Point.parse({pid!r}), 'basic', restricted=False) is not None:
                    seen += 1
            print(seen)
        """)
        writers = [_subprocess(f"""
            import sys; sys.path.insert(0, '.')
            from falsify_lab.storage import Campaign
            from falsify_lab.simulator import FixtureSimulator
            from falsify_lab.protocol import Point
            c = Campaign({str(c.root)!r}, FixtureSimulator(sleep_s=0.3))
            out = c.simulate(Point.parse({pid!r}), 'basic', purpose='calibration')
            print(out.record['sim_id'], out.record['tpd_s'])
        """) for _ in range(2)]
        results = []
        for w in writers:
            out, err = w.communicate(timeout=120)
            self.assertEqual(w.returncode, 0, err)
            results.append(out.split())
        out, err = reader.communicate(timeout=120)
        self.assertEqual(reader.returncode, 0, err)  # no CacheIntegrityError ever observed
        self.assertGreater(int(out.strip()), 0)
        self.assertEqual(results[0], results[1])
        statuses = sorted(e["payload"]["status"] for e in c.events("attempt_finished"))
        self.assertIn("success", statuses)
        self.assertEqual(statuses.count("success"), 1)
        c.verify_ledger()


class RunBudgetTests(unittest.TestCase):
    def test_failed_query_consumes_budget_is_retained_and_not_replaced(self):
        rep = PROTO.replicate(1001)
        bad = rep.initial[1].id
        c = fixture_campaign("failed-query", FixtureSimulator(fail={bad}))
        bm.calibrate(c)
        closed = bm.run_policy(c, "adaptive_idw_plus_distance", 1001)
        self.assertEqual(closed["status"], "incomplete")
        st = c.run_state("benchmark-adaptive_idw_plus_distance-1001")
        self.assertEqual(st["used"], 24)
        failed = [q for q in st["queries"] if q["status"] == "failed"]
        self.assertEqual([q["point_id"] for q in failed], [bad])
        self.assertEqual(sum(q["phase"] == "search" for q in st["queries"]), 12)
        self.assertNotIn(bad, [q["point_id"] for q in st["queries"] if q["phase"] == "search"])
        fin = [e["payload"] for e in c.events("attempt_finished") if e["payload"]["status"] == "failed"]
        self.assertEqual(len(fin), 1)
        self.assertTrue((c.root / "attempts" / fin[0]["attempt_id"] / "failure.txt").exists())
        self.assertEqual(c.cost()["failures"], 1)
        with self.assertRaises(StorageError):
            c.query(st["run_id"], PROTO.training[0].id, "calibration")  # closed run

    def test_cache_and_repeat_do_not_grant_fresh_selection(self):
        c = fixture_campaign("cache-repeat")
        bm.calibrate(c)
        bm.run_policy(c, "random", 1002)
        attempts = len(c.events("attempt_admitted"))
        rid = bm.open_and_seed(c, "benchmark", "space_filling", 1002)
        self.assertEqual(len(c.events("attempt_admitted")), attempts)  # all 12 served from cache
        st = c.run_state(rid)
        self.assertTrue(all(q["cache_hit"] for q in st["queries"]))
        self.assertEqual(st["used"], 12)
        repeated = PROTO.replicate(1002).initial[0]
        cand = {"point_id": repeated.id, "pvt": repeated.pvt, "role": "exploitation", "score": 0.0,
                "idw_predicted_abs_error": None, "min_normalized_distance": 0.0, "cost_queries": 1}
        dec = c.record_decision(rid, {"evidence_result_ids": [], "candidates": [cand], "selected_point_id": repeated.id,
                                      "rank_before": [], "rank_after": [], "selection_changed": False})
        res = c.query(rid, repeated.id, "search", dec["sequence"])
        self.assertTrue(res["repeat"] and res["cache_hit"])
        st = c.run_state(rid)
        self.assertEqual(st["used"], 13)
        self.assertEqual(st["remaining"], 11)
        from falsify_lab.reporting import run_summary

        s = run_summary(c, rid)
        self.assertEqual(len(set(s["selection_point_ids"])), 3)  # repeat adds no distinct point
        for _ in range(11):
            bm.step(c, rid)
        with self.assertRaises(BudgetExhausted):
            c.query(rid, SEARCH[-1].id, "search", 99)

    def test_cache_integrity_is_enforced(self):
        c = fixture_campaign("cache-integrity")
        bm.calibrate(c)
        cal = c.run_state("calibration")["queries"][0]
        path = c.root / "evidence" / "sims" / f"{cal['sim_id']}.json"
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["tpd_s"] *= 1.5
        path.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        with self.assertRaises(CacheIntegrityError):
            c.lookup_cached(PROTO.training[0], "basic", restricted=False)
        # Evidence copied from another campaign is never silently reused.
        other = fixture_campaign("cache-foreign")
        pt = SEARCH[5]
        out = other.simulate(pt, "basic", purpose="calibration")
        foreign = other.root / "evidence" / "sims" / f"{out.record['sim_id']}.json"
        target = c.root / "evidence" / "sims" / foreign.name
        target.write_bytes(foreign.read_bytes())
        with self.assertRaises(CacheIntegrityError):
            c.lookup_cached(pt, "basic", restricted=False)
        # A different numerical setting is a different fingerprint, so no reuse.
        self.assertIsNone(c.lookup_cached(PROTO.training[1], "tight", restricted=False))


class AccessTests(unittest.TestCase):
    def test_heldout_cross_run_and_posthoc_denials(self):
        c = fixture_campaign("access")
        bm.preflight(c)
        bm.calibrate(c)
        ra = bm.open_and_seed(c, "benchmark", "adaptive_idw_plus_distance", 1003)
        rb = bm.open_and_seed(c, "benchmark", "random", 1003)
        held = PROTO.held_out[0]
        cand = {"point_id": held.id, "pvt": held.pvt, "role": "exploitation", "score": 1.0,
                "idw_predicted_abs_error": None, "min_normalized_distance": 0.0, "cost_queries": 1}
        dec = c.record_decision(ra, {"evidence_result_ids": [], "candidates": [cand], "selected_point_id": held.id,
                                     "rank_before": [], "rank_after": [], "selection_changed": False})
        used = c.run_state(ra)["used"]
        with self.assertRaises(AccessDenied):
            c.query(ra, held.id, "search", dec["sequence"])
        with self.assertRaises(AccessDenied):
            c.query(ra, PROTO.prior_excluded[0].id, "search", dec["sequence"])
        with self.assertRaises(AccessDenied):
            c.query(ra, "XX|9.900|500.0", "search", dec["sequence"])
        self.assertEqual(c.run_state(ra)["used"], used)  # denied requests consume nothing
        rid_b = c.revealed_result_ids(rb)[-1]
        with self.assertRaises(AccessDenied):
            c.get_observation(ra, rid_b)
        self.assertEqual(c.get_observation(rb, rid_b)["result_id"], rid_b)
        # Preflight evidence (incl. the held-out FS|1.800|85.0) is restricted, never revealed to runs.
        held_pre = next(e["payload"] for e in c.events("preflight_result") if e["payload"]["point_id"] == "FS|1.800|85.0" and e["payload"]["setting"] == "basic")
        self.assertFalse((c.root / "evidence" / "sims" / f"{held_pre['sim_id']}.json").exists())
        self.assertTrue((c.root / "restricted" / "sims" / f"{held_pre['sim_id']}.json").exists())
        self.assertIsNone(c.lookup_cached(Point.parse("FS|1.800|85.0"), "basic", restricted=False))
        purposes = {e["payload"]["attempt_id"]: e["payload"]["purpose"] for e in c.events("attempt_admitted")}
        for q in c.run_state(ra)["queries"]:
            self.assertTrue((c.root / "evidence" / "sims" / f"{q['sim_id']}.json").exists())
            if q.get("attempt_id"):
                self.assertNotEqual(purposes[q["attempt_id"]], "preflight")
        with self.assertRaises(AccessDenied):
            c.begin_posthoc()  # runs still open
        with self.assertRaises(AccessDenied):
            c.restricted_observe("posthoc", held, "basic")
        with self.assertRaises(StorageError):
            c.open_run("benchmark", "adaptive_idw_plus_distance", 1003)  # no silent resume


if __name__ == "__main__":
    unittest.main()
