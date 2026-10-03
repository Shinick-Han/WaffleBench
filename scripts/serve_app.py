"""Local Falsify Lab app server and bounded job controller.

Serves the fixed assets in web/ and a small JSON API on 127.0.0.1 only:

  GET  /api/snapshot            the configured snapshot file, byte for byte
  GET  /api/snapshot/download   the same bytes as an attachment
  GET  /api/job                 controller status and the current/last job
  POST /api/jobs                {"kind": "demo-prepare" | "reproduce"}
  POST /api/jobs/cancel         {} or {"job_id": "..."}

A job always runs the fixed argv
  sys.executable -m falsify_lab.cli <kind> --root <fixed directory for kind>
from the repository root. Requests never carry commands or paths. One job is
admitted at a time; state is persisted and logs are kept on failure.

Usage:
  python scripts/serve_app.py [--port 8765] [--snapshot PATH]
                              [--demo-root DIR] [--reproduce-root DIR]
                              [--state-dir DIR] [--read-only]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
BIND_HOST = "127.0.0.1"
CONTROLLER_ID = "falsify-lab-local"

JOB_KINDS = ("demo-prepare", "reproduce")
ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("completed", "incomplete", "failed", "cancelled")
INCOMPLETE_STATUSES = {"incomplete", "partial", "budget_exhausted", "cap_reached"}

MAX_BODY_BYTES = 1024
JOB_TIMEOUT_SECONDS = 45 * 60
TAIL_BYTES = 8192
HISTORY_LIMIT = 20

# URL path -> file under web/. Nothing else is served from disk.
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SNAPSHOT_PATHS = ("/api/snapshot", "/data/snapshot.json")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

CommandFactory = Callable[[str, Path], list]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_command(kind: str, root: Path) -> list:
    return [sys.executable, "-m", "falsify_lab.cli", kind, "--root", str(root)]


def core_cli_available(repo_root: Path) -> bool:
    pkg = repo_root / "falsify_lab"
    return (pkg / "cli.py").is_file() or (pkg / "cli" / "__main__.py").is_file()


def read_tail(path: Path, limit: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_summary(stdout_text: str):
    """The CLI prints a JSON summary; take the whole text or the last JSON line."""
    text = stdout_text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader holds the file briefly
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


class JobManager:
    """Owns at most one child process and the persisted job history."""

    def __init__(
        self,
        state_dir: Path,
        job_roots: dict,
        repo_root: Path = REPO_ROOT,
        command_factory: CommandFactory = default_command,
        core_check: Callable[[], bool] | None = None,
        enabled: bool = True,
        timeout_seconds: float = JOB_TIMEOUT_SECONDS,
    ):
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "job-state.json"
        self.job_roots = {k: Path(v) for k, v in job_roots.items()}
        self.repo_root = Path(repo_root)
        self.command_factory = command_factory
        self.core_check = core_check or (lambda: core_cli_available(self.repo_root))
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._current: dict | None = None
        self._history: list = []
        self._counter = 0
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        history = data.get("history") if isinstance(data, dict) else None
        if not isinstance(history, list):
            return
        changed = False
        for job in history:
            if isinstance(job, dict) and job.get("state") in ACTIVE_STATES:
                # The previous controller died with this job active. We no
                # longer own the process, so we never report it as finished.
                job["state"] = "incomplete"
                job["termination_reason"] = "controller_restarted_during_job"
                job["error"] = (
                    "The controller stopped while this job was active; its outcome "
                    "was not observed. Inspect the job logs and root directory."
                )
                job["finished_at"] = job.get("finished_at") or utc_now()
                changed = True
        self._history = [j for j in history if isinstance(j, dict)][-HISTORY_LIMIT:]
        if changed:
            self._persist()

    def _persist(self) -> None:
        atomic_write_json(
            self.state_file,
            {"controller": CONTROLLER_ID, "updated_at": utc_now(), "history": self._history},
        )

    def _update(self, job: dict, **fields) -> None:
        with self._lock:
            job.update(fields)
            self._persist()

    # ---- public API --------------------------------------------------

    def snapshot_status(self) -> dict:
        with self._lock:
            last = self._history[-1] if self._history else None
            return {
                "jobs_enabled": self.enabled,
                "core_available": bool(self.core_check()),
                "kinds": list(JOB_KINDS),
                "job": dict(last) if last else None,
                "active": bool(last and last.get("state") in ACTIVE_STATES),
            }

    def admit(self, kind: str):
        """Atomically admit one job. Returns (http_status, payload)."""
        if kind not in JOB_KINDS:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid_kind", "allowed": list(JOB_KINDS)}
        with self._lock:
            if not self.enabled:
                return HTTPStatus.FORBIDDEN, {
                    "error": "jobs_disabled",
                    "message": "This server was started with --read-only.",
                }
            if self._current is not None and self._current.get("state") in ACTIVE_STATES:
                return HTTPStatus.CONFLICT, {"error": "job_active", "job": dict(self._current)}
            if not self.core_check():
                return HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "core_unavailable",
                    "message": "falsify_lab.cli is not installed in this checkout; no job was started.",
                }
            self._counter += 1
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-{self._counter}"
            root = self.job_roots[kind]
            job_dir = self.state_dir / "jobs" / job_id
            argv = [str(a) for a in self.command_factory(kind, root)]
            job = {
                "job_id": job_id,
                "kind": kind,
                "state": "queued",
                "argv": argv,
                "root": str(root),
                "log_dir": str(job_dir),
                "created_at": utc_now(),
                "started_at": None,
                "finished_at": None,
                "pid": None,
                "returncode": None,
                "termination_reason": None,
                "error": None,
                "summary": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "cancel_requested": False,
            }
            self._current = job
            self._history.append(job)
            self._history = self._history[-HISTORY_LIMIT:]
            self._persist()
            self._thread = threading.Thread(target=self._run, args=(job,), name=f"job-{job_id}", daemon=True)
            self._thread.start()
            return HTTPStatus.ACCEPTED, {"job": dict(job)}

    def cancel(self, job_id: str | None = None):
        with self._lock:
            job = self._current
            if job is None or job.get("state") not in ACTIVE_STATES:
                return HTTPStatus.CONFLICT, {"error": "no_active_job"}
            if job_id is not None and job_id != job["job_id"]:
                return HTTPStatus.CONFLICT, {"error": "job_mismatch", "job": dict(job)}
            job["cancel_requested"] = True
            if job["state"] == "queued" and self._proc is None:
                self._update(job, state="cancelled", termination_reason="cancelled_before_start",
                             finished_at=utc_now())
                return HTTPStatus.OK, {"job": dict(job)}
            proc = self._proc
            self._persist()
        if proc is not None:
            self._terminate(proc)
        return HTTPStatus.ACCEPTED, {"job": dict(job)}

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self) -> None:
        with self._lock:
            active = self._current is not None and self._current.get("state") in ACTIVE_STATES
        if active:
            self.cancel()
            self.wait(15)

    # ---- process handling --------------------------------------------

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Stop the child we started and its descendants, nothing else."""
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                if proc.poll() is None:
                    subprocess.run(
                        [str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
                    )
            else:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _run(self, job: dict) -> None:
        job_dir = Path(job["log_dir"])
        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            Path(job["root"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._finish(job, "failed", reason="setup_failed", error=f"Could not create job directories: {exc}")
            return

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            with self._lock:
                if job.get("cancel_requested") or job["state"] != "queued":
                    return
                try:
                    proc = subprocess.Popen(
                        job["argv"], cwd=str(self.repo_root), stdin=subprocess.DEVNULL,
                        stdout=out, stderr=err, env=env, **kwargs,
                    )
                except OSError as exc:
                    self._finish(job, "failed", reason="spawn_failed", error=f"Could not start the core command: {exc}")
                    return
                self._proc = proc
                self._update(job, state="running", pid=proc.pid, started_at=utc_now())
            timed_out = False
            try:
                returncode = proc.wait(self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(proc)
                returncode = proc.wait()

        stdout_tail = read_tail(stdout_path)
        stderr_tail = read_tail(stderr_path)
        summary = parse_summary(read_tail(stdout_path, 256 * 1024))
        status = str(summary.get("status", "")).lower() if summary else ""
        fields = {"returncode": returncode, "summary": summary,
                  "stdout_tail": stdout_tail, "stderr_tail": stderr_tail}

        if job.get("cancel_requested"):
            self._finish(job, "cancelled", reason="cancelled_by_user", **fields)
        elif timed_out:
            self._finish(job, "failed", reason="controller_timeout",
                         error=f"Job exceeded the {int(self.timeout_seconds)} s controller limit and was stopped.",
                         **fields)
        elif status in INCOMPLETE_STATUSES:
            self._finish(job, "incomplete", reason=(summary or {}).get("termination_reason") or status, **fields)
        elif returncode == 0:
            self._finish(job, "completed", reason=(summary or {}).get("termination_reason") or "exit_0", **fields)
        else:
            last_line = next((ln for ln in reversed(stderr_tail.strip().splitlines()) if ln.strip()), "")
            self._finish(job, "failed", reason=f"exit_{returncode}",
                         error=last_line or f"Core command exited with status {returncode}.", **fields)

    def _finish(self, job: dict, state: str, reason: str | None = None, error: str | None = None, **fields) -> None:
        with self._lock:
            if job.get("state") in TERMINAL_STATES:
                # Already settled (e.g. cancelled before start); keep that receipt.
                return
            if job is self._current:
                # Only the current job owns self._proc; a late finish from an
                # older job must not clear a newer job's process handle.
                self._proc = None
            self._update(job, state=state, termination_reason=reason, error=error,
                         finished_at=utc_now(), **fields)


class SnapshotSource:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self):
        try:
            return self.path.read_bytes()
        except OSError:
            return None

    def status(self) -> dict:
        data = self.read()
        if data is None:
            return {"available": False, "sha256": None, "bytes": None}
        return {"available": True, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, jobs: JobManager, snapshot: SnapshotSource,
                 web_dir: Path = WEB_DIR, verbose: bool = False):
        self.jobs = jobs
        self.snapshot = snapshot
        self.web_dir = Path(web_dir)
        self.verbose = verbose
        super().__init__((BIND_HOST, port), AppHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def allowed_hosts(self) -> set:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}


class AppHandler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "FalsifyLabLocal/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    # ---- responses ---------------------------------------------------

    def _send(self, status, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, code: str, message: str = "") -> None:
        self._json(status, {"error": code, "message": message})

    # ---- request guards ----------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts()

    def _path(self) -> str:
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def _post_guard(self):
        """Strict same-origin JSON POST with a bounded body. Returns dict or None."""
        host = (self.headers.get("Host") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin or origin != f"http://{host}":
            self._error(HTTPStatus.FORBIDDEN, "origin_rejected", "POST requires a same-origin Origin header.")
            return None
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site.lower() != "same-origin":
            self._error(HTTPStatus.FORBIDDEN, "origin_rejected", "Cross-site request rejected.")
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required")
            return None
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.LENGTH_REQUIRED, "length_required")
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.strip().isdigit():
            self._error(HTTPStatus.LENGTH_REQUIRED, "length_required")
            return None
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", f"Limit is {MAX_BODY_BYTES} bytes.")
            self.close_connection = True
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json")
            return None
        if not isinstance(body, dict):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_body", "Body must be a JSON object.")
            return None
        return body

    # ---- routes ------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "host_rejected")
            return
        path = self._path()
        if path in STATIC_ASSETS:
            name, ctype = STATIC_ASSETS[path]
            try:
                body = (self.server.web_dir / name).read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            self._send(HTTPStatus.OK, body, ctype)
        elif path in SNAPSHOT_PATHS or path == "/api/snapshot/download":
            data = self.server.snapshot.read()
            if data is None:
                self._error(HTTPStatus.NOT_FOUND, "snapshot_unavailable",
                            "No snapshot has been exported for the configured source yet.")
                return
            extra = {"ETag": '"' + hashlib.sha256(data).hexdigest() + '"'}
            if path == "/api/snapshot/download":
                extra["Content-Disposition"] = 'attachment; filename="snapshot.json"'
            self._send(HTTPStatus.OK, data, "application/json; charset=utf-8", extra)
        elif path == "/api/job":
            payload = {"controller": CONTROLLER_ID, "mode": "local", "snapshot": self.server.snapshot.status()}
            payload.update(self.server.jobs.snapshot_status())
            self._json(HTTPStatus.OK, payload)
        else:
            self._error(HTTPStatus.NOT_FOUND, "not_found")

    def do_POST(self):
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "host_rejected")
            return
        path = self._path()
        if path not in ("/api/jobs", "/api/jobs/cancel"):
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        body = self._post_guard()
        if body is None:
            return
        if path == "/api/jobs":
            if set(body) != {"kind"} or not isinstance(body.get("kind"), str):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_body", 'Expected exactly {"kind": "<kind>"}.')
                return
            status, payload = self.server.jobs.admit(body["kind"])
        else:
            if not set(body) <= {"job_id"} or ("job_id" in body and not isinstance(body["job_id"], str)):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_body", 'Expected {} or {"job_id": "<id>"}.')
                return
            status, payload = self.server.jobs.cancel(body.get("job_id"))
        self._json(status, payload)

    def _method_not_allowed(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed


def build_server(args) -> AppServer:
    work_root = REPO_ROOT / "runs" / "app"
    demo_root = Path(args.demo_root).resolve() if args.demo_root else work_root / "demo"
    reproduce_root = Path(args.reproduce_root).resolve() if args.reproduce_root else work_root / "reproduce"
    if demo_root == reproduce_root:
        raise SystemExit("--demo-root and --reproduce-root must differ (reproduce is isolated).")
    state_dir = Path(args.state_dir).resolve() if args.state_dir else work_root / "controller"
    snapshot_path = Path(args.snapshot).resolve() if args.snapshot else demo_root / "snapshot.json"
    jobs = JobManager(state_dir, {"demo-prepare": demo_root, "reproduce": reproduce_root},
                      enabled=not args.read_only)
    return AppServer(args.port, jobs, SnapshotSource(snapshot_path), verbose=args.verbose)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Falsify Lab app on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="TCP port on 127.0.0.1 (0 picks a free port)")
    parser.add_argument("--snapshot", help="snapshot.json to display (default: <demo-root>/snapshot.json)")
    parser.add_argument("--demo-root", help="fixed --root for demo-prepare (default: runs/app/demo)")
    parser.add_argument("--reproduce-root", help="fixed --root for reproduce (default: runs/app/reproduce)")
    parser.add_argument("--state-dir", help="controller state and job logs (default: runs/app/controller)")
    parser.add_argument("--read-only", action="store_true", help="disable job start; view snapshot only")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)

    server = build_server(args)
    print(json.dumps({
        "url": f"http://{BIND_HOST}:{server.port}/",
        "snapshot": str(server.snapshot.path),
        "jobs_enabled": server.jobs.enabled,
        "core_available": server.jobs.core_check(),
        "job_roots": {k: str(v) for k, v in server.jobs.job_roots.items()},
    }), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
