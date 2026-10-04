"""Focused tests for scripts/serve_app.py (local app server and job controller).

Jobs here run small stand-in Python commands injected through the
JobManager's command_factory; the HTTP API itself never accepts commands.
No real falsify_lab.cli job is started by these tests.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import serve_app  # noqa: E402

SLEEP_CODE = "import time; print('started', flush=True); time.sleep(60)"


def py(code: str):
    return lambda kind, root: [sys.executable, "-c", code]


def wait_for(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


class ServerCase(unittest.TestCase):
    factory = staticmethod(py("print('{\"status\": \"ok\"}')"))
    core_available = True
    enabled = True
    snapshot_text = '{"schema_version": 1, "data_mode": "real", "observations": []}\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.snapshot_path = base / "snapshot.json"
        if self.snapshot_text is not None:
            self.snapshot_path.write_bytes(self.snapshot_text.encode("utf-8"))
        self.jobs = serve_app.JobManager(
            base / "state",
            {"demo-prepare": base / "demo", "reproduce": base / "reproduce"},
            command_factory=self.factory,
            core_check=lambda: self.core_available,
            enabled=self.enabled,
        )
        self.server = serve_app.AppServer(0, self.jobs, serve_app.SnapshotSource(self.snapshot_path))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.jobs.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.server.port}"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if method == "POST":
            hdrs.update({"Origin": self.origin, "Content-Type": "application/json"})
            hdrs["Content-Length"] = str(len(data or b""))
        hdrs.update(headers or {})
        hdrs = {k: v for k, v in hdrs.items() if v is not None}
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        if data:
            conn.send(data)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def json_request(self, *args, **kwargs):
        status, headers, payload = self.request(*args, **kwargs)
        return status, json.loads(payload.decode("utf-8")) if payload else None

    def current_job(self):
        return self.json_request("GET", "/api/job")[1]["job"]


class StaticAndSnapshotTests(ServerCase):
    def test_index_and_assets_served(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"./app.en.js", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for path in ("/app.js", "/app.en.js?v=translation-check", "/app.css", "/english-presentation.css?v=translation-check", "/index.html"):
            self.assertEqual(self.request("GET", path)[0], 200, path)

    def test_path_traversal_and_unknown_paths_denied(self):
        for path in (
            "/../pyproject.toml", "/..%2f..%2fpyproject.toml", "/%2e%2e/scripts/serve_app.py",
            "/web/app.js", "/data/../app.js", "/tests/fixtures/snapshot.fixture.json",
            "/scripts/serve_app.py", "/app.js/..", "//etc/passwd", "/C:/Windows/win.ini",
            "/api/snapshot/../job/../../RESEARCH_PROTOCOL.md", "/data/snapshot.json/..",
        ):
            status, _, body = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"[project]", body)

    def test_snapshot_view_and_download_identity(self):
        expected = self.snapshot_path.read_bytes()
        bodies = {}
        for path in ("/api/snapshot", "/data/snapshot.json", "/api/snapshot/download"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            bodies[path] = body
            self.assertEqual(headers["ETag"], '"' + hashlib.sha256(expected).hexdigest() + '"')
        self.assertTrue(all(b == expected for b in bodies.values()))
        _, headers, _ = self.request("GET", "/api/snapshot/download")
        self.assertIn("attachment", headers["Content-Disposition"])
        job_status = self.json_request("GET", "/api/job")[1]
        self.assertEqual(job_status["snapshot"]["sha256"], hashlib.sha256(expected).hexdigest())

    def test_wrong_host_header_rejected(self):
        status, body = self.json_request("GET", "/api/job", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 421)
        self.assertEqual(body["error"], "host_rejected")

    def test_job_status_shape(self):
        status, body = self.json_request("GET", "/api/job")
        self.assertEqual(status, 200)
        self.assertEqual(body["controller"], "falsify-lab-local")
        self.assertEqual(body["kinds"], ["demo-prepare", "reproduce"])
        self.assertIsNone(body["job"])
        self.assertTrue(body["core_available"])


class MissingSnapshotTests(ServerCase):
    snapshot_text = None

    def test_missing_snapshot_is_explicit_empty_state(self):
        for path in ("/api/snapshot", "/api/snapshot/download", "/data/snapshot.json"):
            status, body = self.json_request("GET", path)
            self.assertEqual(status, 404)
            self.assertEqual(body["error"], "snapshot_unavailable")
        self.assertFalse(self.json_request("GET", "/api/job")[1]["snapshot"]["available"])


class RequestValidationTests(ServerCase):
    factory = staticmethod(py(SLEEP_CODE))

    def test_invalid_kinds_and_bodies(self):
        cases = [
            ({"kind": "benchmark"}, 400, "invalid_kind"),
            ({"kind": "calibrate"}, 400, "invalid_kind"),
            ({"kind": "demo-prepare; rm -rf /"}, 400, "invalid_kind"),
            ({"kind": ["demo-prepare"]}, 400, "invalid_body"),
            ({"kind": "demo-prepare", "root": "C:/"}, 400, "invalid_body"),
            ({"command": "python -c 1"}, 400, "invalid_body"),
            ([], 400, "invalid_body"),
        ]
        for body, status, code in cases:
            got_status, got = self.json_request("POST", "/api/jobs", body=body)
            self.assertEqual((got_status, got["error"]), (status, code), body)
        status, got = self.json_request("POST", "/api/jobs", raw=b"{not json")
        self.assertEqual((status, got["error"]), (400, "invalid_json"))
        self.assertIsNone(self.current_job())

    def test_origin_and_content_checks(self):
        body = {"kind": "reproduce"}
        for headers, status, code in [
            ({"Origin": None}, 403, "origin_rejected"),
            ({"Origin": "http://evil.example"}, 403, "origin_rejected"),
            ({"Origin": "null"}, 403, "origin_rejected"),
            ({"Origin": f"http://localhost:{self.server.port}"}, 403, "origin_rejected"),  # Host is 127.0.0.1
            ({"Sec-Fetch-Site": "cross-site"}, 403, "origin_rejected"),
            ({"Content-Type": "text/plain"}, 415, "json_required"),
            ({"Content-Type": "application/x-www-form-urlencoded"}, 415, "json_required"),
        ]:
            got_status, got = self.json_request("POST", "/api/jobs", body=body, headers=headers)
            self.assertEqual((got_status, got["error"]), (status, code), headers)
        self.assertIsNone(self.current_job())

    def test_body_size_bounded(self):
        big = json.dumps({"kind": "x" * 5000}).encode()
        status, got = self.json_request("POST", "/api/jobs", raw=big)
        self.assertEqual((status, got["error"]), (413, "body_too_large"))
        status, got = self.json_request("POST", "/api/jobs", raw=b'{"kind":"reproduce"}',
                                        headers={"Content-Length": None})
        self.assertEqual(status, 411)

    def test_unknown_methods_and_routes(self):
        self.assertEqual(self.request("PUT", "/api/jobs")[0], 405)
        self.assertEqual(self.request("DELETE", "/api/job")[0], 405)
        self.assertEqual(self.json_request("POST", "/api/run", body={"kind": "reproduce"})[0], 404)
        self.assertEqual(self.json_request("POST", "/api/snapshot", body={})[0], 404)


class JobLifecycleTests(ServerCase):
    factory = staticmethod(py(SLEEP_CODE))

    def test_concurrent_admission_allows_exactly_one(self):
        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            kind = "demo-prepare" if i % 2 else "reproduce"
            results.append(self.json_request("POST", "/api/jobs", body={"kind": kind})[0])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(sorted(results), [202] + [409] * 7)
        job = wait_for(lambda: (j := self.current_job()) and j["state"] == "running" and j)
        self.assertEqual(self.json_request("POST", "/api/jobs", body={"kind": "reproduce"})[0], 409)
        self.assertTrue(job["pid"])

    def test_cancel_running_job_stops_owned_process(self):
        status, body = self.json_request("POST", "/api/jobs", body={"kind": "reproduce"})
        self.assertEqual(status, 202)
        job_id = body["job"]["job_id"]
        wait_for(lambda: self.current_job()["state"] == "running")
        proc = self.jobs._proc
        self.assertIsNotNone(proc)
        status, _ = self.json_request("POST", "/api/jobs/cancel", body={"job_id": "someone-else"})
        self.assertEqual(status, 409)
        status, _ = self.json_request("POST", "/api/jobs/cancel", body={"job_id": job_id})
        self.assertEqual(status, 202)
        job = wait_for(lambda: (j := self.current_job())["state"] == "cancelled" and j)
        self.assertEqual(job["termination_reason"], "cancelled_by_user")
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.jobs._proc)
        self.assertTrue((Path(job["log_dir"]) / "stdout.log").is_file())
        # A new job is admitted after cancellation.
        self.assertEqual(self.json_request("POST", "/api/jobs", body={"kind": "demo-prepare"})[0], 202)

    def test_cancel_without_job(self):
        status, body = self.json_request("POST", "/api/jobs/cancel", body={})
        self.assertEqual((status, body["error"]), (409, "no_active_job"))
        status, body = self.json_request("POST", "/api/jobs/cancel", body={"job_id": 3})
        self.assertEqual(status, 400)

    def test_fixed_argv_uses_kind_root(self):
        captured = []
        self.jobs.command_factory = lambda kind, root: captured.append((kind, root)) or [sys.executable, "-c", "pass"]
        self.json_request("POST", "/api/jobs", body={"kind": "reproduce"})
        self.jobs.wait(15)
        self.assertEqual(captured[0][0], "reproduce")
        self.assertEqual(captured[0][1], self.jobs.job_roots["reproduce"])
        self.assertNotEqual(self.jobs.job_roots["reproduce"], self.jobs.job_roots["demo-prepare"])


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, code=None, **kwargs):
        kwargs.setdefault("core_check", lambda: True)
        factory = kwargs.pop("command_factory", None) or py(code)
        return serve_app.JobManager(self.base / "state",
                                    {"demo-prepare": self.base / "demo", "reproduce": self.base / "reproduce"},
                                    command_factory=factory, **kwargs)

    def run_job(self, mgr, kind="demo-prepare"):
        status, payload = mgr.admit(kind)
        self.assertEqual(status, 202, payload)
        mgr.wait(30)
        return mgr.snapshot_status()["job"]

    def test_completed_with_summary(self):
        job = self.run_job(self.manager("print('log line'); print('{\"status\": \"ok\", \"path\": \"x\"}')"))
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["summary"], {"status": "ok", "path": "x"})
        self.assertEqual(job["returncode"], 0)

    def test_failure_preserves_artifacts_and_error(self):
        code = "import sys; print('partial'); sys.stderr.write('core exploded\\n'); sys.exit(3)"
        job = self.run_job(self.manager(code))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["returncode"], 3)
        self.assertEqual(job["error"], "core exploded")
        self.assertIn("core exploded", (Path(job["log_dir"]) / "stderr.log").read_text())
        self.assertIn("partial", (Path(job["log_dir"]) / "stdout.log").read_text())

    def test_incomplete_status_reported(self):
        code = "print('{\"status\": \"incomplete\", \"termination_reason\": \"attempt_cap\"}')"
        job = self.run_job(self.manager(code))
        self.assertEqual(job["state"], "incomplete")
        self.assertEqual(job["termination_reason"], "attempt_cap")

    def test_missing_core_module_is_failure_not_success(self):
        # The real default argv against an interpreter without falsify_lab.cli.
        mgr = self.manager(command_factory=lambda kind, root: [sys.executable, "-m", "falsify_lab_missing_cli", kind])
        job = self.run_job(mgr)
        self.assertEqual(job["state"], "failed")
        self.assertIn("falsify_lab_missing_cli", job["error"])

    def test_core_unavailable_rejected_before_start(self):
        mgr = serve_app.JobManager(self.base / "state", {"demo-prepare": self.base / "d", "reproduce": self.base / "r"},
                                   repo_root=self.base)  # no falsify_lab/cli.py here
        status, payload = mgr.admit("demo-prepare")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "core_unavailable")
        self.assertIsNone(mgr.snapshot_status()["job"])
        self.assertEqual(serve_app.default_command("reproduce", Path("R"))[1:],
                         ["-m", "falsify_lab.cli", "reproduce", "--root", "R"])

    def test_read_only_rejects_jobs(self):
        status, payload = self.manager("pass", enabled=False).admit("reproduce")
        self.assertEqual((status, payload["error"]), (403, "jobs_disabled"))

    def test_spawn_error_is_failed(self):
        mgr = self.manager(command_factory=lambda kind, root: [str(self.base / "no-such-binary.exe")])
        job = self.run_job(mgr)
        self.assertEqual((job["state"], job["termination_reason"]), ("failed", "spawn_failed"))

    def test_timeout_is_failed(self):
        mgr = self.manager(SLEEP_CODE, timeout_seconds=1)
        job = self.run_job(mgr)
        self.assertEqual((job["state"], job["termination_reason"]), ("failed", "controller_timeout"))

    def test_state_persisted_and_restart_marks_active_incomplete(self):
        mgr = self.manager("print('{\"status\": \"ok\"}')")
        job = self.run_job(mgr)
        reloaded = self.manager("pass")
        self.assertEqual(reloaded.snapshot_status()["job"]["job_id"], job["job_id"])
        # Simulate a controller that died while a job was running.
        state_file = self.base / "state" / "job-state.json"
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data["history"][-1]["state"] = "running"
        state_file.write_text(json.dumps(data), encoding="utf-8")
        restarted = self.manager("pass")
        last = restarted.snapshot_status()["job"]
        self.assertEqual(last["state"], "incomplete")
        self.assertEqual(last["termination_reason"], "controller_restarted_during_job")
        self.assertEqual(restarted.admit("reproduce")[0], 202)
        restarted.wait(15)

    def test_stale_finish_does_not_clear_newer_process(self):
        """Old queued job cancelled, new job admitted, old setup error lands late."""
        release_old = threading.Event()
        blocker = self.base / "blocked-root"
        blocker.write_text("a file, so mkdir of the demo root fails")

        class DelayedManager(serve_app.JobManager):
            def _run(inner, job):
                if job["kind"] == "demo-prepare":
                    release_old.wait(20)
                super()._run(job)

        mgr = DelayedManager(self.base / "state", {"demo-prepare": blocker / "demo", "reproduce": self.base / "rep"},
                             command_factory=py(SLEEP_CODE), core_check=lambda: True)
        status, old = mgr.admit("demo-prepare")
        self.assertEqual(status, 202)
        old_thread = mgr._thread
        status, cancelled = mgr.cancel()
        self.assertEqual((status, cancelled["job"]["state"]), (200, "cancelled"))
        status, new = mgr.admit("reproduce")
        self.assertEqual(status, 202)
        wait_for(lambda: mgr.snapshot_status()["job"]["state"] == "running")
        new_proc = mgr._proc
        self.assertIsNotNone(new_proc)

        release_old.set()          # old job now hits its setup failure
        old_thread.join(20)
        self.assertIs(mgr._proc, new_proc)
        self.assertIsNone(new_proc.poll())
        history = {j["job_id"]: j for j in json.loads((self.base / "state" / "job-state.json").read_text())["history"]}
        self.assertEqual(history[old["job"]["job_id"]]["state"], "cancelled")
        self.assertEqual(history[old["job"]["job_id"]]["termination_reason"], "cancelled_before_start")
        self.assertEqual(history[new["job"]["job_id"]]["state"], "running")

        self.assertEqual(mgr.cancel()[0], 202)
        mgr.wait(20)
        self.assertEqual(mgr.snapshot_status()["job"]["state"], "cancelled")
        self.assertIsNotNone(new_proc.poll())

    def test_parse_summary_variants(self):
        self.assertEqual(serve_app.parse_summary('{"a": 1}'), {"a": 1})
        self.assertEqual(serve_app.parse_summary('noise\n{"status": "ok"}\n'), {"status": "ok"})
        self.assertIsNone(serve_app.parse_summary("[1, 2]"))
        self.assertIsNone(serve_app.parse_summary(""))


class DefaultSnapshotTests(unittest.TestCase):
    def test_shipped_snapshot_is_empty_real(self):
        snap = json.loads((ROOT / "web" / "data" / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["data_mode"], "real")
        for key in ("observations", "evaluations", "decisions", "trace", "coverage"):
            self.assertEqual(snap[key], [], key)
        for key in ("run", "model", "benchmark", "cost", "generated_at"):
            self.assertIsNone(snap[key], key)

        def numbers(value):
            if isinstance(value, bool):
                return []
            if isinstance(value, (int, float)):
                return [value]
            if isinstance(value, dict):
                return [n for v in value.values() for n in numbers(v)]
            if isinstance(value, list):
                return [n for v in value for n in numbers(v)]
            return []

        self.assertEqual(numbers({k: v for k, v in snap.items() if k != "schema_version"}), [])


if __name__ == "__main__":
    unittest.main()
