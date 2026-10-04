"""Public judge runtime: one fresh isolated Omnigent diagnostic cycle per job, fail closed.

Launched by the public API as
``python scripts/public_discovery_runtime.py --job-root <jobs>/<uuid> --output <jobs>/<uuid>/public-result.json``.
It prepares a new ledger under ``<job-root>/cycle`` from the fixed v3 evidence input, copies the
discovery bundle into the job (originals are never mutated), starts its own Omnigent server on
127.0.0.1:6775 plus a headless host, runs the existing verified driver
(``scripts/run_discovery_cycle.py``) once, re-verifies the SDK proof, and only then writes
``public-result.json`` atomically from ``cycle/export.json``. It stops only processes it started.
Raw logs stay in ``<job-root>/private``. No retries, no prompt input, nonzero exit on any failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = '127.0.0.1'
PORT = 6775
URL = f'http://{HOST}:{PORT}'
PROTECTED_PORTS = (6767, 6771, 6773)
OMNI_DIR = Path(os.environ.get('APPDATA', '')) / 'uv' / 'tools' / 'omnigent' / 'Scripts'
OMNI_PYTHON = OMNI_DIR / 'python.exe'
OMNI_EXE = OMNI_DIR / 'omni.exe'
INPUT_DIR = Path('evidence') / 'inspection-improvements-v3'
MODEL, EFFORT, HARNESS = 'claude-opus-5-5', 'medium', 'claude-sdk'
AGENTS = {'discovery_supervisor', 'discovery_analyst', 'discovery_experimenter'}
CONFIGS = ('config.yaml', 'agents/discovery_analyst/config.yaml', 'agents/discovery_experimenter/config.yaml')
MCP = (('', 'supervisor', 'read_context, finalize'),
       ('discovery_analyst', 'analyst', 'read_context, record_plan, record_update'),
       ('discovery_experimenter', 'experimenter', 'run_experiment'))
OVERALL_SECONDS = 1060      # API kills the runtime at 1100; leave room for cleanup
CLEANUP_RESERVE = 40
POST_DRIVE_RESERVE = 90     # snapshot collection plus the driver's 60 s verify subprocess
DRIVER_DEADLINE = 900
MIN_DRIVE = 300
VALIDATE_SECONDS = 120
HEALTH_SECONDS = 45
HOST_SECONDS = 60
LOG_LIMIT = 2 * 1024 * 1024
REMOVED_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY')
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
_PATH_LEAK = re.compile(r'[A-Za-z]:(\\\\|/)')
VALIDATE_CODE = r'''
import json, sys, yaml
from pathlib import Path
from omnigent.spec import parse, validate
d = Path(sys.argv[1]); s = parse(d); rows = []
for a in [s, *s.sub_agents]:
    v = validate(a); rows.append({'name': a.name, 'valid': bool(v.valid), 'errors': [str(e) for e in v.errors]})
ex = []
for c in sys.argv[2:]:
    y = yaml.safe_load((d / c).read_text(encoding='utf-8'))
    e = y.get('executor') or {}
    ex.append({'name': y.get('name'), 'model': e.get('model'), 'reasoning_effort': e.get('reasoning_effort'),
               'harness': (e.get('config') or {}).get('harness')})
print(json.dumps({'agents': rows, 'executors': ex}))
'''


class Failure(RuntimeError):
    """Fail-closed stop with a public-safe code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


class SystemOps:
    """Real process/network operations; tests substitute a fake."""

    def port_in_use(self, port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        try:
            with socket.socket() as s:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind((HOST, port))
        except OSError:
            return True
        return False

    def spawn(self, argv, *, env, cwd, stdout, stderr):
        with open(stdout, 'ab') as out, open(stderr, 'ab') as err:
            return subprocess.Popen([str(a) for a in argv], env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, creationflags=HIDDEN)

    def wait(self, proc, timeout: float) -> int:
        return proc.wait(timeout=max(timeout, 0.1))

    def alive(self, proc) -> bool:
        return proc.poll() is None

    def kill_tree(self, proc) -> None:
        if proc.poll() is None:  # our own live handle, so the PID cannot have been reused
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=30, creationflags=HIDDEN)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def http_json(self, url: str):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.loads(r.read(1_000_000))
        except Exception:  # noqa: BLE001 - not ready yet
            return None

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def scoped_env(base: dict, data_dir: Path) -> dict:
    """Original launcher flags: private data dir, no connectors/telemetry/update check, no API keys."""
    env = {k: v for k, v in base.items() if k.upper() not in REMOVED_ENV}
    env.update(OMNIGENT_DATA_DIR=str(data_dir), OMNIGENT_CONFIG_HOME=str(data_dir), OMNIGENT_NO_UPDATE_CHECK='1',
               OMNIGENT_HOST_NO_OPEN='1', OMNIGENT_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1', PYTHONUTF8='1',
               PYTHONIOENCODING='utf-8', ENABLE_CLAUDEAI_MCP_SERVERS='false',
               OMNIGENT_RUNNER_ENV_PASSTHROUGH='ENABLE_CLAUDEAI_MCP_SERVERS')
    npm = os.path.join(base.get('APPDATA', ''), 'npm').rstrip('\\').lower()
    env['PATH'] = ';'.join(p for p in base.get('PATH', '').split(';') if p and p.rstrip('\\').lower() != npm)
    return env


def check_job_args(job_root: Path, output: Path) -> tuple[Path, Path]:
    job_root, output = job_root.resolve(), output.resolve()
    try:
        canonical = str(uuid.UUID(job_root.name)) == job_root.name.lower()
    except ValueError:
        canonical = False
    if not canonical:
        raise Failure('invalid_job')
    if output != job_root / 'public-result.json' or output.exists() or (job_root / 'cycle').exists():
        raise Failure('invalid_job')
    return job_root, output


def copy_bundle(root: Path, dest: Path, cycle: Path, app_python: Path) -> None:
    src = root / 'discovery_agent'
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns('discovery.yaml', '__pycache__', '.gitignore'))
    for rel in CONFIGS:
        if _sha(dest / rel) != _sha(src / rel):
            raise Failure('bundle_invalid')
    py, project, target = (str(p).replace('\\', '/') for p in (app_python, root, cycle))
    for agent, role, tools in MCP:
        d = (dest / 'agents' / agent if agent else dest) / 'tools' / 'mcp'
        d.mkdir(parents=True, exist_ok=True)
        text = (f'name: discovery\ntransport: stdio\ncommand: "{py}"\nargs: ["-m", "discovery_cycle.mcp_server"]\n'
                f'env:\n  DISCOVERY_CYCLE_ROOT: "{target}"\n  DISCOVERY_CYCLE_ROLE: "{role}"\n'
                f'  PYTHONPATH: "{project}"\n  PYTHONUTF8: "1"\ntimeout: 120\ntools: [{tools}]\n\n')
        (d / 'discovery.yaml').write_text(text, encoding='utf-8')


def check_validation(text: str) -> None:
    try:
        rows = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Failure('bundle_invalid') from None
    agents, ex = rows.get('agents', []), rows.get('executors', [])
    ok = ({a['name'] for a in agents} == AGENTS and len(agents) == 3 and all(a['valid'] for a in agents)
          and {e['name'] for e in ex} == AGENTS and len(ex) == 3
          and all((e['model'], e['reasoning_effort'], e['harness']) == (MODEL, EFFORT, HARNESS) for e in ex))
    if not ok:
        raise Failure('bundle_invalid')


def check_and_read_export(cycle: Path, root: Path, job_root: Path) -> bytes:
    """Independent re-check of the driver's verification before anything becomes public."""
    try:
        from scripts import run_discovery_cycle as driver
        receipt = json.loads((cycle / 'verification.json').read_text(encoding='utf-8'))
        proof = json.loads((cycle / 'omnigent' / 'session-proof.json').read_text(encoding='utf-8'))
        capture = json.loads((cycle / 'omnigent' / 'sdk-records.json').read_text(encoding='utf-8'))
        raw = (cycle / 'export.json').read_bytes()
        export = json.loads(raw)
        fresh = driver.verify(cycle, capture)
    except Exception:  # noqa: BLE001 - any missing or unreadable proof fails closed
        raise Failure('verification_failed') from None
    sid = capture.get('session_id')
    checks = receipt.get('checks') or []
    ok = (receipt.get('status') == 'passed' and receipt.get('failure_reasons') == [] and checks
          and all(c.get('passed') is True for c in checks)
          and fresh['status'] == 'passed' and fresh['failure_reasons'] == []
          and [c['name'] for c in fresh['checks']] == [c['name'] for c in checks]
          and all(fresh[k] == receipt.get(k) for k in ('head_hash', 'input_sha256', 'events_sha256'))
          and proof.get('status') == 'completed' and proof.get('verification_status') == 'passed'
          and proof.get('session_id') == sid and isinstance(sid, str) and sid
          and export.get('schema_version') == 1
          and export.get('sdk') == {'session_id': sid, 'status': 'completed', 'model': MODEL, 'harness': HARNESS}
          and export.get('verification', {}).get('status') == 'passed'
          and export['verification'].get('checks') == checks
          and len(export.get('plans', [])) == len(export.get('results', [])) == len(export.get('updates', [])) == 2)
    if not ok:
        raise Failure('verification_failed')
    text = raw.decode('utf-8')
    if _PATH_LEAK.search(text) or any(s in text for s in {str(root), str(job_root), str(Path.home())}):
        raise Failure('export_invalid')
    if export != driver.export(cycle, capture, receipt):
        raise Failure('verification_failed')
    # Public records name their own logical run namespace instead of incorrectly
    # pointing at the older published session's ledger. No private path is exposed.
    public = driver.export(cycle, capture, receipt, public_run_id=job_root.name)
    return (json.dumps(public, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _bound_log(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_LIMIT:
            with path.open('rb') as fh:
                fh.seek(-LOG_LIMIT, os.SEEK_END)
                tail = fh.read()
            path.write_bytes(b'[truncated]\n' + tail)
    except OSError:
        pass


def execute(job_root: Path, output: Path, *, ops=None, root: Path = ROOT, omni_python: Path = OMNI_PYTHON,
            omni_exe: Path = OMNI_EXE, app_python: Path | None = None, base_env: dict | None = None) -> int:
    from inspection_live.session import LiveError, exclusive_lock
    ops = ops or SystemOps()
    app_python = app_python or root / '.venv' / 'Scripts' / 'python.exe'
    base_env = dict(os.environ if base_env is None else base_env)
    assert PORT not in PROTECTED_PORTS
    job_root, output = check_job_args(Path(job_root), Path(output))
    cycle, private = job_root / 'cycle', job_root / 'private'
    logs, raw_dir = private / 'logs', private / 'raw'
    # Omnigent artifact filenames add about 150 characters. A nested job/private
    # prefix exceeds Windows MAX_PATH in a normal checkout, so keep a distinct
    # full UUID namespace under a short private runtime directory.
    data_dir = root / '.public-discovery-runtime' / 'o' / uuid.UUID(job_root.name).hex
    bundle = private / 'bundle' / 'discovery_agent'
    (root / '.discovery-live-runtime').mkdir(parents=True, exist_ok=True)
    start = ops.monotonic()
    summary = {'job_id': job_root.name, 'status': 'failed', 'error': None, 'started_at': _now(), 'steps': [],
               'returncodes': {}, 'stopped_own_processes': [], 'port_released': None}
    owned: list[tuple[str, object]] = []

    def step(name):
        summary['steps'].append({'step': name, 'at_seconds': round(ops.monotonic() - start, 3)})

    def remaining():
        return OVERALL_SECONDS - (ops.monotonic() - start)

    def run_logged(name, argv, env):
        proc = ops.spawn(argv, env=env, cwd=root, stdout=logs / f'{name}.out.log', stderr=logs / f'{name}.err.log')
        owned.append((name, proc))
        return proc

    try:
        with exclusive_lock(root / '.discovery-live-runtime' / 'public-runtime.lock', timeout=0):
            try:
                step('preflight')
                if ops.port_in_use(PORT):
                    raise Failure('port_occupied')
                if not all(Path(p).is_file() for p in (omni_python, omni_exe, app_python)):
                    raise Failure('runtime_unavailable')
                for d in (logs, raw_dir, data_dir):
                    d.mkdir(parents=True, exist_ok=True)
                env = scoped_env(base_env, data_dir)

                step('prepare')
                try:
                    from discovery_cycle import prepare
                    prepare(cycle, root / INPUT_DIR)
                except Exception:  # noqa: BLE001
                    raise Failure('prepare_failed') from None
                copy_bundle(root, bundle, cycle, app_python)

                step('validate_bundle')
                proc = run_logged('validate', [omni_python, '-c', VALIDATE_CODE, bundle, *CONFIGS], env)
                try:
                    summary['returncodes']['validate'] = ops.wait(proc, VALIDATE_SECONDS)
                except subprocess.TimeoutExpired:
                    raise Failure('bundle_invalid') from None
                if summary['returncodes']['validate'] != 0:
                    raise Failure('bundle_invalid')
                check_validation((logs / 'validate.out.log').read_text(encoding='utf-8', errors='replace'))

                step('server')
                if ops.port_in_use(PORT):
                    raise Failure('port_occupied')
                server = run_logged('server', [omni_exe, 'server', '--host', HOST, '--port', str(PORT),
                                               '--agent', bundle], env)
                until = ops.monotonic() + HEALTH_SECONDS
                while not ('"ok"' in json.dumps(ops.http_json(URL + '/health')) and ops.alive(server)):
                    if not ops.alive(server) or ops.monotonic() > until:
                        raise Failure('server_unavailable')
                    ops.sleep(0.5)

                step('host')
                host = run_logged('host', [omni_exe, 'host', '--server', URL, '--no-open', '--non-interactive'], env)
                until = ops.monotonic() + HOST_SECONDS
                while True:
                    hosts = (ops.http_json(URL + '/v1/hosts') or {}).get('hosts') or []
                    online = [h for h in hosts if h.get('status') == 'online' and not h.get('sandbox_provider')]
                    if len(online) == 1 and ops.alive(host) and ops.alive(server):
                        break
                    if not ops.alive(host) or not ops.alive(server) or ops.monotonic() > until:
                        raise Failure('host_unavailable')
                    ops.sleep(1)

                step('driver')
                drive = min(DRIVER_DEADLINE, remaining() - POST_DRIVE_RESERVE - CLEANUP_RESERVE)
                if drive < MIN_DRIVE:
                    raise Failure('setup_timeout')
                driver = run_logged('driver', [omni_python, root / 'scripts' / 'run_discovery_cycle.py', '--root',
                                               cycle, '--server', URL, '--timeout', str(int(drive)),
                                               '--proof-dir', raw_dir], env)
                try:
                    summary['returncodes']['driver'] = ops.wait(driver, remaining() - CLEANUP_RESERVE)
                except subprocess.TimeoutExpired:
                    raise Failure('driver_timeout') from None
                if summary['returncodes']['driver'] != 0:
                    raise Failure('driver_failed')

                step('verify')
                data = check_and_read_export(cycle, root, job_root)
                _atomic(output, data)
                summary['status'] = 'completed'
                step('published_result')
            finally:
                for name, proc in reversed(owned):
                    try:
                        if ops.alive(proc):
                            ops.kill_tree(proc)
                            summary['stopped_own_processes'].append(name)
                    except Exception:  # noqa: BLE001 - keep cleaning the other handles
                        summary['stopped_own_processes'].append(name + ':error')
                if any(n == 'server' for n, _ in owned):
                    summary['port_released'] = not ops.port_in_use(PORT)
                for log in logs.glob('*.log'):
                    _bound_log(log)
    except LiveError:
        summary['error'] = 'busy'
    except Failure as exc:
        summary['error'] = exc.code
    except Exception:  # noqa: BLE001 - never leak details publicly
        summary['error'] = 'internal_error'
    summary['finished_at'] = _now()
    if private.exists():
        _atomic(private / 'runtime-summary.json', json.dumps(summary, indent=2).encode())
    print(json.dumps({'status': summary['status'], 'error': summary['error']}))
    return 0 if summary['status'] == 'completed' else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return execute(args.job_root, args.output)
    except Failure as exc:
        print(json.dumps({'status': 'failed', 'error': exc.code}))
        return 2


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
