"""Focused tests for public_discovery.server with an injected fake runtime.

The runtime
is replaced by a script that copies the genuine recorded evidence/discovery-cycle ledger,
SDK capture and receipt into the isolated job and writes the real verified export
web/data/discovery-cycle.json (no model, no Claude, no Omnigent). Bad modes forge, stale,
tamper or leave outputs unverified.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from public_discovery import server as pd

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://shinick-han.github.io"

FAKE = r'''
import argparse, json, shutil, sys, time
from pathlib import Path
ROOT = Path(__ROOT__)
MODE = __MODE__
ap = argparse.ArgumentParser()
ap.add_argument("--job-root", required=True)
ap.add_argument("--output", required=True)
a = ap.parse_args()
job, out = Path(a.job_root), Path(a.output)
if MODE == "fail":
    sys.exit(3)
if MODE == "sleep":
    while not (job.parent.parent / "release").exists():
        time.sleep(0.05)
doc = json.loads((ROOT / "web/data/discovery-cycle.json").read_text(encoding="utf-8"))
cycle = job / "cycle"
if MODE != "stale":
    # Genuine recorded ledger + SDK capture + receipt, copied into this isolated job (no model).
    shutil.copytree(ROOT / "evidence/discovery-cycle", cycle, ignore=shutil.ignore_patterns(".lock"))
if MODE == "unverified":
    doc["verification"]["status"] = "failed"
if MODE == "sdk_incomplete":
    doc["sdk"]["status"] = "failed"
if MODE == "forged":
    doc["results"][1]["result_id"] = "res_ffffffffffffffff"
if MODE == "numbers_tampered":
    doc["results"][0]["values"]["invented_gain"] = 999
if MODE == "private_path":
    doc["label"] = "written to " + str(job)
if MODE == "unbound":
    doc["sources"][1]["sha256"] = "0" * 64
if MODE == "capture_tampered":
    cap = json.loads((cycle / "omnigent/sdk-records.json").read_text(encoding="utf-8"))
    cap["items"] = []
    (cycle / "omnigent/sdk-records.json").write_text(json.dumps(cap), encoding="utf-8")
if MODE == "ledger_tampered":
    with open(cycle / "events.jsonl", "ab") as fh:
        fh.write(b'{"seq": 99}\n')
if MODE == "partial":
    shutil.rmtree(cycle / "omnigent")
out.write_text(json.dumps(doc), encoding="utf-8")
'''


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    mode = "good"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.clock = Clock()

    def tearDown(self):
        (self.state / "release").parent.mkdir(parents=True, exist_ok=True)
        (self.state / "release").write_text("x")
        for m in getattr(self, "managers", []):
            m.join(30)
        self.tmp.cleanup()

    def script(self, mode: str) -> Path:
        path = self.base / f"fake_{mode}.py"
        path.write_text(FAKE.replace("__ROOT__", repr(str(ROOT))).replace("__MODE__", repr(mode)), encoding="utf-8")
        return path

    def manager(self, mode=None, **kw) -> pd.RunManager:
        kw.setdefault("cooldown_seconds", 0)
        cfg = pd.Config(state_dir=self.state, repo_root=ROOT, runtime_script=self.script(mode or self.mode),
                        timeout_seconds=kw.pop("timeout_seconds", 120), clock=self.clock,
                        rejected_session_ids=kw.pop("rejected_session_ids", ()), **kw)
        m = pd.RunManager(cfg)
        self.managers = getattr(self, "managers", []) + [m]
        return m

    @staticmethod
    def rid() -> str:
        return str(uuid.uuid4())

    def finished(self, m: pd.RunManager, run_id: str) -> dict:
        return wait_for(lambda: (s := m.public_status(run_id))["status"] in ("completed", "failed") and s)


class ParseTests(unittest.TestCase):
    def test_strict_body(self):
        good = str(uuid.uuid4())
        self.assertEqual(pd.parse_request(json.dumps({"request_id": good}).encode()), good)
        bad = [b"", b"[]", b"null", b"{}", b"not json",
               json.dumps({"request_id": good, "prompt": "x"}).encode(),
               json.dumps({"request_id": good.upper()}).encode(),
               json.dumps({"request_id": "../../etc"}).encode(),
               json.dumps({"request_id": 5}).encode(),
               ('{"request_id": "%s", "request_id": "%s"}' % (good, good)).encode(),
               b'{"request_id": NaN}',
               json.dumps({"request_id": good, "pad": "x" * 600}).encode()]
        for body in bad:
            self.assertIsNone(pd.parse_request(body), body[:60])


class RunTests(Base):
    def test_good_run_completes_and_serves_verified_bytes(self):
        m = self.manager()
        code, body = m.submit(self.rid())
        self.assertEqual(code, "accepted")
        st = self.finished(m, body["id"])
        self.assertEqual((st["status"], st["verification"], st["phase"]), ("completed", "passed", "completed"))
        self.assertEqual((st["plans"], st["experiments"], st["updates"]), (2, 2, 2))
        self.assertIsNone(st["error"])
        self.assertEqual(set(st) - {"latest_hypothesis"}, {"id", "status", "phase", "created_at", "updated_at",
                                                           "plans", "experiments", "updates", "verification", "error"})
        code, raw = m.result_bytes(body["id"])
        self.assertEqual(code, "ok")
        self.assertEqual(json.loads(raw)["verification"]["status"], "passed")
        dumped = json.dumps(st)
        self.assertNotIn(str(self.state), dumped)

    def test_tampered_result_after_completion_is_withheld(self):
        m = self.manager()
        _, body = m.submit(self.rid())
        self.finished(m, body["id"])
        out = self.state / "jobs" / body["id"] / pd.OUTPUT_NAME
        out.write_bytes(out.read_bytes().replace(b"WaffleBench", b"WaffleBenck", 1))
        self.assertEqual(m.result_bytes(body["id"])[0], "unavailable")

    def test_unverified_or_forged_outputs_fail_closed(self):
        for mode in ("fail", "stale", "unverified", "sdk_incomplete", "forged", "numbers_tampered", "private_path", "unbound",
                     "capture_tampered", "ledger_tampered", "partial"):
            with self.subTest(mode=mode):
                state = self.base / f"state_{mode}"
                cfg = pd.Config(state_dir=state, repo_root=ROOT, runtime_script=self.script(mode),
                                cooldown_seconds=0, clock=self.clock, rejected_session_ids=())
                m = pd.RunManager(cfg)
                _, body = m.submit(self.rid())
                st = self.finished(m, body["id"])
                self.assertEqual(st["status"], "failed")
                self.assertNotEqual(st["verification"], "passed")
                self.assertEqual(m.result_bytes(body["id"])[0], "not_ready")
                self.assertNotIn(str(state), json.dumps(st))
                self.assertIn(st["error"], pd.RUN_ERRORS.values())

    def test_frozen_published_session_is_rejected_as_stale(self):
        m = self.manager(rejected_session_ids=pd.FROZEN_SESSION_IDS)
        _, body = m.submit(self.rid())
        st = self.finished(m, body["id"])
        self.assertEqual((st["status"], st["verification"]), ("failed", "failed"))

    def test_serial_busy_and_idempotent_reuse(self):
        m = self.manager("sleep")
        first = self.rid()
        code, body = m.submit(first)
        self.assertEqual(code, "accepted")
        self.assertEqual(m.submit(self.rid())[0], "busy")
        code, again = m.submit(first)
        self.assertEqual((code, again["id"]), ("reused", body["id"]))
        st = wait_for(lambda: (s := m.public_status(body["id"]))["status"] == "running" and s)
        self.assertEqual(st["verification"], "pending")
        self.assertEqual(m.health()["active_run_id"], body["id"])
        self.assertFalse(m.health()["available"])
        (self.state / "release").write_text("x")
        self.assertEqual(self.finished(m, body["id"])["status"], "completed")

    def test_partial_progress_tolerates_missing_ledger_and_never_claims_verification(self):
        m = self.manager("sleep")
        _, body = m.submit(self.rid())
        st = wait_for(lambda: (s := m.public_status(body["id"]))["status"] == "running" and s)
        self.assertEqual((st["phase"], st["plans"], st["verification"]), ("starting", 0, "pending"))

    def test_cooldown_and_quota(self):
        m = self.manager(cooldown_seconds=180, max_runs=2)
        first = self.rid()
        _, body = m.submit(first)
        self.finished(m, body["id"])
        self.assertEqual(m.submit(self.rid())[0], "cooldown")
        self.assertEqual(m.submit(first)[0], "reused")
        self.clock.now += 181
        code, b2 = m.submit(self.rid())
        self.assertEqual(code, "accepted")
        self.finished(m, b2["id"])
        self.clock.now += 181
        self.assertEqual(m.submit(self.rid())[0], "quota")
        self.assertEqual(m.health()["remaining_runs"], 0)

    def test_restart_marks_active_failed_and_keeps_quota(self):
        self.state.mkdir(parents=True)
        runs = [{"id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()), "status": s, "created_epoch": 1.0,
                 "updated_epoch": 1.0, "error_code": None, "result_sha256": None, "counts": None, "exit_code": None}
                for s in ("running", "queued")]
        (self.state / "state.json").write_text(json.dumps({"version": 1, "runs": runs}))
        m = self.manager(max_runs=2)
        for r in runs:
            st = m.public_status(r["id"])
            self.assertEqual((st["status"], st["error"]), ("failed", pd.RUN_ERRORS["interrupted"]))
        self.assertEqual(m.submit(self.rid())[0], "quota")
        self.assertEqual(json.loads((self.state / "state.json").read_text())["runs"][0]["status"], "failed")

    def test_reservation_persisted_and_survives_restart(self):
        m = self.manager("sleep", max_runs=3)
        _, body = m.submit(self.rid())
        saved = json.loads((self.state / "state.json").read_text())["runs"]
        self.assertEqual(saved[0]["id"], body["id"])
        m2 = pd.RunManager(pd.Config(state_dir=self.base / "state", repo_root=ROOT, runtime_script=self.script("good"),
                                     max_runs=3, cooldown_seconds=0, clock=self.clock))
        self.assertEqual(m2.public_status(body["id"])["status"], "failed")
        self.assertEqual(m2.remaining(), 2)

    def test_corrupt_state_and_missing_runtime_disable(self):
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text("{broken")
        m = self.manager()
        self.assertEqual(m.submit(self.rid())[0], "disabled")
        self.assertEqual(m.health()["remaining_runs"], 0)
        cfg = pd.Config(state_dir=self.base / "s2", runtime_script=self.base / "missing.py", clock=self.clock)
        self.assertEqual(pd.RunManager(cfg).submit(self.rid())[0], "disabled")

    def test_timeout_fails_run(self):
        m = self.manager("sleep", timeout_seconds=1)
        _, body = m.submit(self.rid())
        st = self.finished(m, body["id"])
        self.assertEqual(st["error"], pd.RUN_ERRORS["timeout"])


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        cfg = pd.Config(state_dir=self.state, repo_root=ROOT, runtime_script=self.script("good"),
                        cooldown_seconds=0, clock=self.clock, rejected_session_ids=())
        self.server = pd.make_server(cfg, port=0)
        self.managers = [self.server.manager]
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def post(self, body, origin=ORIGIN, ctype="application/json"):
        headers = {"Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        return self.req("POST", "/api/runs", body, headers)

    def test_bind_is_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            pd.make_server(self.server.manager.config, port=0, host="0.0.0.0")
        self.assertEqual(pd.PORT, 8782)

    def test_health_and_cors(self):
        status, headers, data = self.req("GET", "/api/health", headers={"Origin": ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(set(json.loads(data)), {"available", "active_run_id", "remaining_runs", "scope", "message"})
        status, headers, _ = self.req("GET", "/api/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = self.req("OPTIONS", "/api/runs", headers={"Origin": "http://localhost:8768"})
        self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (204, "http://localhost:8768"))
        self.assertEqual(self.req("OPTIONS", "/api/runs", headers={"Origin": "http://localhost:9999"})[0], 403)

    def test_post_origin_and_malformed(self):
        body = json.dumps({"request_id": self.rid()}).encode()
        self.assertEqual(self.post(body, origin=None)[0], 403)
        self.assertEqual(self.post(body, origin="https://shinick-han.github.io.evil.example")[0], 403)
        self.assertEqual(self.post(body, origin="null")[0], 403)
        self.assertEqual(self.post(body, ctype="text/plain")[0], 400)
        for bad in (b"{}", b"[1]", json.dumps({"request_id": self.rid(), "model": "x"}).encode(),
                    json.dumps({"request_id": "x" * 600}).encode()):
            status, _, data = self.post(bad)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(data)["error"], "malformed")
        self.assertEqual(self.server.manager.runs, [])

    def test_paths_and_methods(self):
        rid = str(uuid.uuid4())
        for path in ("/", "/state.json", "/api/runs/../state.json", "/api/runs/%2e%2e/state.json",
                     f"/api/runs/{rid}/../../state.json", "/api/runs/" + "A" * 36, f"/api/runs/{rid}",
                     f"/api/runs/{rid}/result", "/jobs/x/public-result.json", "/api/runs/..%5c..%5cstate.json"):
            status, _, data = self.req("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(data), {"error": "not_found", "message": "Not found."})
        self.assertEqual(self.req("DELETE", "/api/health")[0], 405)

    def test_end_to_end_run(self):
        request_id = self.rid()
        body = json.dumps({"request_id": request_id}).encode()
        status, headers, data = self.post(body)
        self.assertEqual(status, 202)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ORIGIN)
        run = json.loads(data)
        self.assertEqual(set(run), {"id", "status"})
        status, _, data = self.post(body)
        self.assertEqual((status, json.loads(data)["id"]), (202, run["id"]))
        wait_for(lambda: json.loads(self.req("GET", f"/api/runs/{run['id']}")[2])["status"] == "completed")
        status, _, data = self.req("GET", f"/api/runs/{run['id']}/result")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["sdk"]["status"], "completed")
        self.assertNotIn(str(self.state).encode(), data)


if __name__ == "__main__":
    unittest.main()
