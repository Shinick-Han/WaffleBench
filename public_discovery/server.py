"""Bounded public API that starts fixed, verified WaffleBench discovery diagnostics.

Endpoints (JSON only, nothing is ever served from disk by path):

  GET  /api/health               {available, active_run_id, remaining_runs, scope, message}
  POST /api/runs                 {"request_id": "<uuid>"}  -> 202 {id, status}
  GET  /api/runs/{id}            public status of one run
  GET  /api/runs/{id}/result     the verified export (same schema as web/data/discovery-cycle.json)

A run always executes the fixed argv

  sys.executable scripts/public_discovery_runtime.py --job-root <jobs>/<uuid> --output <jobs>/<uuid>/public-result.json

with no shell, no request-supplied input and a hard timeout. Runs are serial, limited to a
persisted lifetime quota with a start cooldown, and never retried automatically. A reservation
is persisted before the process is spawned; after a restart any queued/running reservation is
marked failed and still counts against the quota. A result is only exposed when the runtime
exited 0 and the output passes the fail-closed checks in ``verify_result`` (SDK completed,
verification passed, bound to the job's own finalized hash-chained ledger, fresh, no private
paths). Error bodies are fixed strings; internal paths, logs and tracebacks stay private.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BIND_HOST = "127.0.0.1"
PORT = 8782
ALLOWED_ORIGINS = ("https://shinick-han.github.io", "http://localhost:8768")

# The already-published recorded cycle; a public run that returns it is stale, not fresh.
FROZEN_SESSION_IDS = ("93ce9d9f48e9453d88898c13ebe8f83d",)

MAX_RUNS_LIMIT = 8
DEFAULT_COOLDOWN_SECONDS = 180.0
DEFAULT_TIMEOUT_SECONDS = 1100.0
MAX_BODY_BYTES = 512
MAX_CONNECTIONS = 16
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_LOG_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 4 * 1024 * 1024
MAX_HYPOTHESIS_CHARS = 500

STATE_NAME = "state.json"
JOBS_NAME = "jobs"
OUTPUT_NAME = "public-result.json"
LOG_NAME = "runtime.log"
CYCLE_NAME = "cycle"

ACTIVE_STATES = ("queued", "running")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
RUN_PATH_RE = re.compile(r"^/api/runs/([0-9a-f-]{36})(/result)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
PATHLIKE_RE = re.compile(r"(?i)(?:\b[a-z]:[\\/])|(?:\\\\)|(?:/(?:users|home|tmp|var|etc|mnt|private)/)")

SCOPE = ("Fixed two-experiment Omnigent diagnostics of already-existing authored synthetic evidence; "
         "not a new held-out gain, physical experiment or arbitrary prompt.")

# Public error codes -> (HTTP status, fixed message). Nothing else is ever returned as an error.
ERRORS = {
    "malformed": (400, "Request must be a JSON object with exactly one UUID field request_id."),
    "forbidden": (403, "Origin not allowed."),
    "not_found": (404, "Not found."),
    "method": (405, "Method not allowed."),
    "busy": (409, "Another diagnostic run is active."),
    "not_ready": (409, "The run has no verified result."),
    "quota": (429, "The public run quota is used."),
    "cooldown": (429, "Please wait before starting another run."),
    "disabled": (503, "Public runs are disabled."),
    "unavailable": (503, "The result is unavailable."),
    "internal": (500, "Internal error."),
}
# Private failure reasons -> safe public error text on a failed run.
RUN_ERRORS = {
    "spawn_failed": "The diagnostic runtime could not start.",
    "runtime_failed": "The diagnostic runtime did not complete.",
    "timeout": "The diagnostic runtime exceeded its time limit.",
    "verification_failed": "The output did not pass verification and is withheld.",
    "interrupted": "The run was interrupted by a service restart.",
    "internal": "Internal error.",
}
PHASES = {
    "prepared": "preparing", "plan_recorded": "planned", "experiment_admitted": "experimenting",
    "experiment_completed": "experiment_completed", "experiment_failed": "experiment_failed",
    "update_recorded": "updated", "finalized": "verifying",
}
RESULT_KEYS = ("schema_version", "project", "label", "question", "sdk", "verification", "sources", "plans",
               "results", "updates", "closed", "limits")


def _now_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def strict_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def parse_request(raw: bytes) -> str | None:
    """Return the request_id of a valid POST body, else None."""
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        doc = strict_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(doc, dict) or set(doc) != {"request_id"}:
        return None
    rid = doc["request_id"]
    return rid if isinstance(rid, str) and UUID_RE.match(rid) else None


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _private_markers(*paths: Path) -> list[str]:
    marks = set()
    for p in paths:
        s = str(p.resolve())
        marks.update({s.lower(), s.replace("\\", "/").lower()})
    return sorted(m for m in marks if len(m) > 3)


def verify_result(raw: bytes, job_dir: Path, started_epoch: float, private: list[str],
                  rejected_sessions: tuple[str, ...] = FROZEN_SESSION_IDS) -> bool:
    """Fail-closed check that ``raw`` is a genuine, fresh, verified export of this job's own ledger."""
    try:
        if not raw or len(raw) > MAX_RESULT_BYTES:
            return False
        doc = strict_json(raw)
        if not isinstance(doc, dict) or any(k not in doc for k in RESULT_KEYS):
            return False
        if doc["schema_version"] != 1 or doc["project"] != "WaffleBench":
            return False
        sdk, ver, closed = doc["sdk"], doc["verification"], doc["closed"]
        if not (isinstance(sdk, dict) and sdk.get("status") == "completed"
                and isinstance(sdk.get("session_id"), str) and sdk["session_id"]):
            return False
        checks = ver.get("checks") if isinstance(ver, dict) else None
        if ver.get("status") != "passed" or not isinstance(checks, list) or not checks:
            return False
        if not all(isinstance(c, dict) and c.get("passed") is True for c in checks):
            return False
        if not (isinstance(closed, dict) and closed.get("status") == "finalized"):
            return False
        plans, results, updates = doc["plans"], doc["results"], doc["updates"]
        for block in (plans, results, updates):
            if not isinstance(block, list) or len(block) != 2 or not all(isinstance(x, dict) for x in block):
                return False
        sources = doc["sources"]
        if not isinstance(sources, list) or not sources:
            return False
        for s in sources:
            path = s.get("path") if isinstance(s, dict) else None
            if not (isinstance(path, str) and isinstance(s.get("sha256"), str) and SHA_RE.match(s["sha256"])):
                return False
            if path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/") or PATHLIKE_RE.search(path):
                return False
        for text in _strings(doc):
            low = text.lower()
            if any(m in low for m in private):
                return False
        # Freshness: written by this job, after it started.
        if (job_dir / OUTPUT_NAME).stat().st_mtime < started_epoch - 1:
            return False
        # Bind to this job's own finalized, hash-chained ledger.
        from discovery_cycle.core import DiscoveryCycle  # verifies chain, head and input hash

        cycle = job_dir / CYCLE_NAME
        status = DiscoveryCycle(cycle).status()
        result_ids = [r.get("result_id") for r in results]
        if status.get("finalized") is not True or status.get("blocked") is not False or status.get("failed", 0):
            return False
        if status.get("result_ids") != result_ids or sorted(status.get("updated_result_ids") or []) != sorted(result_ids):
            return False
        if any(r.get("input_sha256") not in (None, status.get("input_sha256")) for r in results):
            return False
        ledger_sha = _sha((cycle / "events.jsonl").read_bytes())
        if ledger_sha not in {s["sha256"] for s in sources}:
            return False
        # Independently re-verify this job's own SDK capture against the ledger; never trust the
        # export's "completed"/"passed" text or the runtime's receipt on their own.
        from scripts.run_discovery_cycle import verify as verify_cycle, export as export_cycle

        capture = strict_json((cycle / "omnigent" / "sdk-records.json").read_bytes())
        proof = strict_json((cycle / "omnigent" / "session-proof.json").read_bytes())
        receipt = strict_json((cycle / "verification.json").read_bytes())
        public_id = doc.get("public_run_id")
        if public_id is not None and public_id != job_dir.name:
            return False
        if doc != export_cycle(cycle, capture, receipt, public_run_id=public_id):
            return False
        session = capture.get("session_id")
        if not (isinstance(session, str) and session and capture.get("outcome") == "completed"):
            return False
        if session in rejected_sessions or sdk["session_id"] != session or proof.get("session_id") != session:
            return False
        if proof.get("status") != "completed" or proof.get("verification_status") != "passed":
            return False
        fresh = verify_cycle(cycle, capture)
        if fresh.get("status") != "passed" or fresh.get("failure_reasons"):
            return False
        if fresh["checks"] != checks or receipt.get("status") != "passed" or receipt.get("checks") != fresh["checks"]:
            return False
        return fresh.get("events_sha256") == ledger_sha == receipt.get("events_sha256")
    except Exception:  # noqa: BLE001 - any surprise is a verification failure
        return False


def _safe_text(value: Any, private: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(CONTROL_RE.sub(" ", value).split())[:MAX_HYPOTHESIS_CHARS]
    low = text.lower()
    if not text or PATHLIKE_RE.search(text) or any(m in low for m in private):
        return None
    return text


def ledger_progress(job_dir: Path, private: list[str]) -> dict | None:
    """Untrusted partial progress from <job>/cycle/events.jsonl; None while absent or unreadable."""
    path = job_dir / CYCLE_NAME / "events.jsonl"
    try:
        if not path.is_file() or path.stat().st_size > MAX_LEDGER_BYTES:
            return None
        lines = path.read_bytes().splitlines()
    except OSError:
        return None
    counts = {"plans": 0, "experiments": 0, "updates": 0}
    last_type, hypothesis = None, None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            continue
        kind, data = ev["type"], ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if kind == "plan_recorded":
            counts["plans"] += 1
            hypothesis = _safe_text(data.get("hypothesis"), private)
        elif kind == "experiment_completed":
            counts["experiments"] += 1
        elif kind == "update_recorded":
            counts["updates"] += 1
        if kind in PHASES:
            last_type = kind
    return {**counts, "phase": PHASES.get(last_type, "starting"), "latest_hypothesis": hypothesis}


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(stream, dest: Path, cap: int) -> None:
    written = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if written < cap:
                part = chunk[: cap - written]
                fh.write(part)
                written += len(part)
    stream.close()


@dataclass
class Config:
    state_dir: Path
    repo_root: Path = REPO_ROOT
    runtime_script: Path | None = None
    max_runs: int = MAX_RUNS_LIMIT
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    enabled: bool = True
    allowed_origins: tuple[str, ...] = ALLOWED_ORIGINS
    max_connections: int = MAX_CONNECTIONS
    rejected_session_ids: tuple[str, ...] = FROZEN_SESSION_IDS
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.repo_root = Path(self.repo_root)
        if self.runtime_script is None:
            self.runtime_script = self.repo_root / "scripts" / "public_discovery_runtime.py"
        self.runtime_script = Path(self.runtime_script)
        if not 0 <= int(self.max_runs) <= MAX_RUNS_LIMIT:
            raise ValueError(f"max_runs must be between 0 and {MAX_RUNS_LIMIT}")
        self.max_runs = int(self.max_runs)


class RunManager:
    """Persisted serial run admission, execution and verification."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.state_dir = config.state_dir
        self.jobs_dir = self.state_dir / JOBS_NAME
        self.state_path = self.state_dir / STATE_NAME
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.private = _private_markers(self.state_dir, config.repo_root)
        self.runs: list[dict] = []
        self.broken = False
        self.threads: list[threading.Thread] = []
        self._load()

    # ------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            doc = strict_json(self.state_path.read_bytes())
            runs = doc["runs"]
            if doc.get("version") != 1 or not isinstance(runs, list):
                raise ValueError("bad state")
            for r in runs:
                if not (isinstance(r, dict) and UUID_RE.match(r.get("id", "")) and UUID_RE.match(r.get("request_id", ""))):
                    raise ValueError("bad run")
        except Exception:  # noqa: BLE001 - unreadable state fails closed: no new runs
            self.broken = True
            return
        self.runs = runs
        now = self.config.clock()
        changed = False
        for r in self.runs:
            if r["status"] in ACTIVE_STATES:
                r.update(status="failed", error_code="interrupted", updated_epoch=now)
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        tmp = self.state_path.with_name(f"state.{uuid.uuid4().hex}.tmp")
        data = json.dumps({"version": 1, "runs": self.runs}, sort_keys=True, indent=1).encode()
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            for attempt in range(21):
                try:
                    os.replace(tmp, self.state_path)
                    break
                except PermissionError:
                    if attempt == 20:
                        raise
                    # Windows can briefly hold a file during reads/AV scanning.
                    # This retries only an atomic state write, never a model run.
                    time.sleep(0.01)
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------ queries
    def _find(self, run_id: str) -> dict | None:
        return next((r for r in self.runs if r["id"] == run_id), None)

    def _active(self) -> dict | None:
        return next((r for r in self.runs if r["status"] in ACTIVE_STATES), None)

    def _disabled(self) -> bool:
        return self.broken or not self.config.enabled or not self.config.runtime_script.is_file()

    def _cooldown_left(self, now: float) -> float:
        if not self.runs:
            return 0.0
        last = max(r["created_epoch"] for r in self.runs)
        return max(0.0, self.config.cooldown_seconds - (now - last))

    def remaining(self) -> int:
        return 0 if self.broken else max(0, self.config.max_runs - len(self.runs))

    def health(self) -> dict:
        with self.lock:
            now = self.config.clock()
            active = self._active()
            remaining = self.remaining()
            if self._disabled():
                available, message = False, "Public runs are disabled."
            elif active:
                available, message = False, "A diagnostic run is in progress."
            elif remaining <= 0:
                available, message = False, "The public run quota is used."
            elif self._cooldown_left(now) > 0:
                available, message = False, "Cooling down before the next run."
            else:
                available, message = True, "Ready to start a fixed diagnostic run."
            return {"available": available, "active_run_id": active["id"] if active else None,
                    "remaining_runs": remaining, "scope": SCOPE, "message": message}

    def public_status(self, run_id: str) -> dict | None:
        with self.lock:
            run = self._find(run_id)
            run = dict(run) if run else None
        if run is None:
            return None
        status = run["status"]
        out = {"id": run["id"], "status": status, "phase": status,
               "created_at": _now_iso(run["created_epoch"]), "updated_at": _now_iso(run["updated_epoch"]),
               "plans": 0, "experiments": 0, "updates": 0, "latest_hypothesis": None,
               "verification": "pending", "error": None}
        if status == "completed":
            out.update(run.get("counts") or {}, verification="passed")
            return out
        if status == "failed":
            out.update(verification="failed" if run.get("error_code") == "verification_failed" else "not_run",
                       error=RUN_ERRORS.get(run.get("error_code"), RUN_ERRORS["internal"]))
        progress = ledger_progress(self.jobs_dir / run["id"], self.private)
        if progress:
            out.update({k: progress[k] for k in ("plans", "experiments", "updates", "latest_hypothesis")})
            if status == "running":
                out["phase"] = progress["phase"]
        elif status == "running":
            out["phase"] = "starting"
        return out

    def result_bytes(self, run_id: str) -> tuple[str, bytes | None]:
        with self.lock:
            run = self._find(run_id)
            run = dict(run) if run else None
        if run is None:
            return "not_found", None
        if run["status"] != "completed":
            return "not_ready", None
        try:
            raw = (self.jobs_dir / run["id"] / OUTPUT_NAME).read_bytes()[: MAX_RESULT_BYTES + 1]
        except OSError:
            return "unavailable", None
        if len(raw) > MAX_RESULT_BYTES or _sha(raw) != run.get("result_sha256"):
            return "unavailable", None
        return "ok", raw

    # ------------------------------------------------ admission
    def submit(self, request_id: str) -> tuple[str, dict | None]:
        """Admit a run. Returns (code, {id, status}); code is "accepted", "reused" or an ERRORS key."""
        with self.lock:
            existing = next((r for r in self.runs if r["request_id"] == request_id), None)
            if existing:
                return "reused", {"id": existing["id"], "status": existing["status"]}
            if self._disabled():
                return "disabled", None
            if self._active():
                return "busy", None
            if self.remaining() <= 0:
                return "quota", None
            now = self.config.clock()
            if self._cooldown_left(now) > 0:
                return "cooldown", None
            run = {"id": str(uuid.uuid4()), "request_id": request_id, "status": "queued", "created_epoch": now,
                   "updated_epoch": now, "error_code": None, "result_sha256": None, "counts": None,
                   "exit_code": None}
            self.runs.append(run)
            try:
                self._save()  # reservation is durable before anything is spawned
            except OSError:
                self.runs.pop()
                return "disabled", None
            thread = threading.Thread(target=self._execute, args=(run["id"],), daemon=True,
                                      name=f"public-run-{run['id'][:8]}")
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
            thread.start()
            return "accepted", {"id": run["id"], "status": run["status"]}

    # ------------------------------------------------ execution
    def _finish(self, run_id: str, status: str, **fields: Any) -> None:
        with self.lock:
            run = self._find(run_id)
            run.update(status=status, updated_epoch=self.config.clock(), **fields)
            self._save()

    def _execute(self, run_id: str) -> None:
        try:
            self._run(run_id)
        except Exception:  # noqa: BLE001 - never leave a run active on an unexpected error
            try:
                self._finish(run_id, "failed", error_code="internal")
            except Exception:  # noqa: BLE001
                pass

    def _run(self, run_id: str) -> None:
        if not UUID_RE.match(run_id):
            raise ValueError("unsafe run id")
        job_dir = self.jobs_dir / run_id
        job_dir.mkdir(parents=False, exist_ok=False)
        output = job_dir / OUTPUT_NAME
        argv = [sys.executable, str(self.config.runtime_script), "--job-root", str(job_dir), "--output", str(output)]
        started = time.time()
        with self.lock:
            self._find(run_id).update(status="running", updated_epoch=self.config.clock(), started_wall=started)
            self._save()
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, cwd=str(self.config.repo_root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **kwargs)
        except OSError:
            self._finish(run_id, "failed", error_code="spawn_failed")
            return
        drain = threading.Thread(target=_drain, args=(proc.stdout, job_dir / LOG_NAME, MAX_LOG_BYTES), daemon=True)
        drain.start()
        try:
            code = proc.wait(timeout=self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
            drain.join(timeout=10)
            self._finish(run_id, "failed", error_code="timeout")
            return
        drain.join(timeout=10)
        if code != 0:
            self._finish(run_id, "failed", error_code="runtime_failed", exit_code=code)
            return
        try:
            raw = output.read_bytes()[: MAX_RESULT_BYTES + 1]
        except OSError:
            raw = b""
        if not verify_result(raw, job_dir, started, self.private, self.config.rejected_session_ids):
            self._finish(run_id, "failed", error_code="verification_failed", exit_code=code)
            return
        doc = json.loads(raw)
        counts = {"plans": len(doc["plans"]), "experiments": len(doc["results"]), "updates": len(doc["updates"]),
                  "latest_hypothesis": _safe_text(doc["plans"][-1].get("hypothesis"), self.private)}
        self._finish(run_id, "completed", exit_code=code, result_sha256=_sha(raw), counts=counts)

    def join(self, timeout: float | None = None) -> None:
        for t in list(self.threads):
            t.join(timeout)


# ---------------------------------------------------------------- HTTP
class PublicServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], manager: RunManager):
        self.manager = manager
        self.slots = threading.BoundedSemaphore(manager.config.max_connections)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):  # no tracebacks on stderr
        pass


class Handler(BaseHTTPRequestHandler):
    server: PublicServer
    server_version = "WaffleBenchPublic"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - no unbounded access log
        pass

    # ------------------------------------------------ helpers
    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin in self.server.manager.config.allowed_origins else None

    def _send(self, status: int, body: bytes | dict, extra: dict | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Vary", "Origin")
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, code: str) -> None:
        status, message = ERRORS[code]
        self._send(status, {"error": code, "message": message})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        mapped = {400: "malformed", 404: "not_found", 405: "method", 501: "method"}.get(code, "malformed")
        try:
            self._error(mapped)
        except Exception:  # noqa: BLE001
            pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _guard(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - fixed safe error, no traceback
            self.close_connection = True
            try:
                self._error("internal")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------ methods
    def do_OPTIONS(self) -> None:
        def run():
            if not self._origin():
                return self._error("forbidden")
            self._send(204, b"", {
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600"})
        self._guard(run)

    def do_GET(self) -> None:
        self._guard(self._get)

    def do_POST(self) -> None:
        self._guard(self._post)

    def do_PUT(self) -> None:
        self._error("method")

    do_DELETE = do_PATCH = do_HEAD = do_PUT

    def _get(self) -> None:
        manager = self.server.manager
        path = self._path()
        if path == "/api/health":
            return self._send(200, manager.health())
        m = RUN_PATH_RE.match(path)
        if not m or not UUID_RE.match(m.group(1)):
            return self._error("not_found")
        run_id = m.group(1)
        if m.group(2):
            code, raw = manager.result_bytes(run_id)
            return self._send(200, raw) if code == "ok" else self._error(code)
        status = manager.public_status(run_id)
        return self._send(200, status) if status else self._error("not_found")

    def _post(self) -> None:
        if self._path() != "/api/runs":
            return self._error("not_found")
        if not self._origin():
            return self._error("forbidden")
        self.close_connection = True
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json" or self.headers.get("Transfer-Encoding"):
            return self._error("malformed")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= MAX_BODY_BYTES:
            return self._error("malformed")
        request_id = parse_request(self.rfile.read(int(length)))
        if request_id is None:
            return self._error("malformed")
        code, body = self.server.manager.submit(request_id)
        if code in ("accepted", "reused"):
            return self._send(202, body)
        return self._error(code)


def make_server(config: Config, port: int = PORT, host: str = BIND_HOST) -> PublicServer:
    """Build the server. Production binds 127.0.0.1:8782; tests may pass port 0 on loopback only."""
    if host != BIND_HOST:
        raise ValueError("the public API binds 127.0.0.1 only")
    return PublicServer((host, port), RunManager(config))
