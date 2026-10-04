"""Fake-subprocess tests for the public fresh-job runtime; no Omnigent or Claude process is started."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from inspection_live.session import exclusive_lock
from scripts import public_discovery_runtime as rt
from scripts import run_discovery_cycle as driver

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / 'evidence/discovery-cycle'
GOOD_VALIDATION = {'agents': [{'name': n, 'valid': True, 'errors': []} for n in sorted(rt.AGENTS)],
                   'executors': [{'name': n, 'model': 'claude-opus-5-5', 'reasoning_effort': 'medium',
                                  'harness': 'claude-sdk'} for n in sorted(rt.AGENTS)]}


def _rm(path):
    def onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=onexc)


def fake_driver_success(cycle, root, tamper=None):
    """Stand in for the real driver: replace the prepared ledger by the published genuine cycle, then --verify."""
    _rm(cycle)
    shutil.copytree(EVIDENCE, cycle, ignore=shutil.ignore_patterns('.lock'))
    capture = json.loads((cycle / 'omnigent/sdk-records.json').read_text(encoding='utf-8'))
    receipt = driver.verify(cycle, capture)
    driver.write(cycle / 'verification.json', receipt)
    proof = json.loads((cycle / 'omnigent/session-proof.json').read_text(encoding='utf-8'))
    proof.update(status='completed', verification_status=receipt['status'])
    driver.write(cycle / 'omnigent/session-proof.json', proof)
    export = driver.export(cycle, capture, receipt)
    if tamper == 'path':
        export['note'] = 'C:\\Users\\someone\\secret'
    driver.write(cycle / 'export.json', export)
    if tamper == 'capture':
        capture['outcome'] = 'failed'
        driver.write(cycle / 'omnigent/sdk-records.json', capture)
    return 0


class FakeProc:
    def __init__(self, kind, argv, rc=0, hang=False):
        self.kind, self.argv, self.rc, self.hang, self.live = kind, argv, rc, hang, True


class FakeOps:
    def __init__(self, test, *, occupied=False, healthy=True, server_dies=False, validation=GOOD_VALIDATION,
                 driver=None, driver_rc=0, driver_hang=False, host_online=True):
        self.t, self.occupied, self.healthy, self.server_dies = test, occupied, healthy, server_dies
        self.validation, self.driver, self.driver_rc, self.driver_hang = validation, driver, driver_rc, driver_hang
        self.host_online = host_online
        self.spawned, self.killed, self.clock = [], [], 0.0

    def port_in_use(self, port):
        assert port == 6775
        if self.occupied:
            return True
        return any(p.kind == 'server' and p.live for p in self.spawned)

    def spawn(self, argv, *, env, cwd, stdout, stderr):
        argv = [str(a) for a in argv]
        kind = ('validate' if '-c' in argv else 'server' if 'server' in argv[1:2] else 'host' if 'host' in argv[1:2]
                else 'driver')
        proc = FakeProc(kind, argv)
        proc.env = env
        if kind == 'validate':
            Path(stdout).write_text(json.dumps(self.validation) + '\n', encoding='utf-8')
        if kind == 'server' and self.server_dies:
            proc.live = False
        if kind == 'driver':
            proc.hang = self.driver_hang
            proc.rc = self.driver_rc
            if self.driver and not self.driver_hang:
                cycle = Path(argv[argv.index('--root') + 1])
                proc.rc = self.driver(cycle, self.t.root) if self.driver_rc == 0 else self.driver_rc
        self.spawned.append(proc)
        return proc

    def wait(self, proc, timeout):
        if proc.hang:
            self.clock += timeout
            raise subprocess.TimeoutExpired(proc.argv, timeout)
        proc.live = False
        return proc.rc

    def alive(self, proc):
        return proc.live

    def kill_tree(self, proc):
        self.killed.append(proc.kind)
        proc.live = False

    def http_json(self, url):
        if url.endswith('/health'):
            return {'status': 'ok'} if self.healthy else None
        hosts = [p for p in self.spawned if p.kind == 'host' and p.live]
        return {'hosts': [{'host_id': 'h1', 'status': 'online'}] if hosts and self.host_online else []}

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def kinds(self):
        return [p.kind for p in self.spawned]


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.root = base / 'app'
        (self.root / rt.INPUT_DIR).mkdir(parents=True)
        shutil.copy(REPO / rt.INPUT_DIR / 'quality-diagnostics.json', self.root / rt.INPUT_DIR)
        shutil.copytree(REPO / 'discovery_agent', self.root / 'discovery_agent',
                        ignore=shutil.ignore_patterns('discovery.yaml'))
        self.source_hashes = self._hashes(self.root / 'discovery_agent')
        self.bins = base / 'bin'
        self.bins.mkdir()
        for name in ('omni-python.exe', 'omni.exe', 'app-python.exe'):
            (self.bins / name).write_bytes(b'')
        self.job = base / 'jobs' / str(uuid.uuid4())
        self.job.mkdir(parents=True)
        self.output = self.job / 'public-result.json'
        self.env = {'PATH': 'C:\\Windows;C:\\AppData\\npm\\;C:\\tools', 'APPDATA': 'C:\\AppData',
                    'ANTHROPIC_API_KEY': 'sk-ant-test-secret', 'OPENAI_API_KEY': 'x', 'USERPROFILE': 'C:\\u'}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _hashes(d):
        return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(d.rglob('*')) if p.is_file()}

    def run_rt(self, ops, job=None, output=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = rt.execute(job or self.job, output or self.output, ops=ops, root=self.root,
                            omni_python=self.bins / 'omni-python.exe', omni_exe=self.bins / 'omni.exe',
                            app_python=self.bins / 'app-python.exe', base_env=self.env)
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def summary(self):
        return json.loads((self.job / 'private/runtime-summary.json').read_text(encoding='utf-8'))

    @unittest.skipUnless((EVIDENCE / 'omnigent/sdk-records.json').is_file(), 'published cycle not available')
    def test_success_publishes_exact_export_and_stops_only_own_processes(self):
        ops = FakeOps(self, driver=fake_driver_success)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out), (0, {'status': 'completed', 'error': None}))
        self.assertEqual(self.output.read_bytes(), (self.job / 'cycle/export.json').read_bytes())
        self.assertEqual(ops.kinds(), ['validate', 'server', 'host', 'driver'])
        self.assertEqual(sorted(ops.killed), ['host', 'server'])
        server, host, drv = ops.spawned[1:]
        self.assertEqual(server.argv[1:6], ['server', '--host', '127.0.0.1', '--port', '6775'])
        self.assertEqual(Path(server.argv[7]), self.job.resolve() / 'private/bundle/discovery_agent')
        self.assertIn('http://127.0.0.1:6775', host.argv)
        self.assertEqual(drv.argv[0], str(self.bins / 'omni-python.exe'))
        self.assertEqual(drv.argv[drv.argv.index('--server') + 1], 'http://127.0.0.1:6775')
        self.assertEqual(Path(drv.argv[drv.argv.index('--root') + 1]), self.job.resolve() / 'cycle')
        self.assertLessEqual(float(drv.argv[drv.argv.index('--timeout') + 1]), 900)
        env = drv.env
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['ENABLE_CLAUDEAI_MCP_SERVERS'], 'false')
        self.assertEqual(env['OMNIGENT_DISABLE_TELEMETRY'], '1')
        self.assertEqual(Path(env['OMNIGENT_DATA_DIR']), self.job.resolve() / 'private/omnigent')
        self.assertNotIn('npm', env['PATH'])
        mcp = (self.job / 'private/bundle/discovery_agent/agents/discovery_analyst/tools/mcp/discovery.yaml').read_text()
        self.assertIn(str(self.job.resolve() / 'cycle').replace('\\', '/'), mcp)
        self.assertIn('DISCOVERY_CYCLE_ROLE: "analyst"', mcp)
        self.assertEqual(self._hashes(self.root / 'discovery_agent'), self.source_hashes)
        self.assertEqual(Path(drv.argv[drv.argv.index('--proof-dir') + 1]), self.job.resolve() / 'private/raw')
        self.assertFalse((self.root / '.discovery-live-runtime/proof').exists())
        summary = self.summary()
        self.assertTrue(summary['port_released'])
        self.assertNotIn('sk-ant', json.dumps(summary))

    def test_occupied_port_starts_nothing(self):
        ops = FakeOps(self, occupied=True)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'port_occupied'))
        self.assertEqual((ops.spawned, ops.killed), ([], []))
        self.assertFalse(self.output.exists())
        self.assertFalse((self.job / 'cycle').exists())

    def test_driver_timeout_fails_closed_and_cleans_up(self):
        ops = FakeOps(self, driver_hang=True)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'driver_timeout'))
        self.assertEqual(sorted(ops.killed), ['driver', 'host', 'server'])
        self.assertFalse(self.output.exists())

    def test_driver_failure_has_no_output(self):
        ops = FakeOps(self, driver=fake_driver_success, driver_rc=1)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'driver_failed'))
        self.assertEqual(ops.kinds().count('driver'), 1)  # no automatic retry
        self.assertFalse(self.output.exists())

    def test_missing_export_is_not_verified(self):
        ops = FakeOps(self, driver=lambda cycle, root: 0)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'verification_failed'))
        self.assertFalse(self.output.exists())

    @unittest.skipUnless((EVIDENCE / 'omnigent/sdk-records.json').is_file(), 'published cycle not available')
    def test_independent_reverification_rejects_altered_capture(self):
        ops = FakeOps(self, driver=lambda c, r: fake_driver_success(c, r, tamper='capture'))
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'verification_failed'))
        self.assertFalse(self.output.exists())

    @unittest.skipUnless((EVIDENCE / 'omnigent/sdk-records.json').is_file(), 'published cycle not available')
    def test_local_path_in_export_is_refused(self):
        ops = FakeOps(self, driver=lambda c, r: fake_driver_success(c, r, tamper='path'))
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'export_invalid'))
        self.assertFalse(self.output.exists())

    def test_wrong_model_bundle_never_starts_server(self):
        bad = json.loads(json.dumps(GOOD_VALIDATION))
        bad['executors'][0]['model'] = 'claude-sonnet-5-5'
        ops = FakeOps(self, validation=bad)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'bundle_invalid'))
        self.assertEqual(ops.kinds(), ['validate'])

    def test_unhealthy_server_is_stopped_before_host(self):
        ops = FakeOps(self, healthy=False)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'server_unavailable'))
        self.assertEqual(ops.kinds(), ['validate', 'server'])
        self.assertEqual(ops.killed, ['server'])

    def test_server_that_exits_is_not_trusted_even_if_port_answers(self):
        ops = FakeOps(self, server_dies=True)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'server_unavailable'))
        self.assertNotIn('host', ops.kinds())

    def test_host_never_online(self):
        ops = FakeOps(self, host_online=False)
        rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'host_unavailable'))
        self.assertEqual(sorted(ops.killed), ['host', 'server'])
        self.assertNotIn('driver', ops.kinds())

    def test_concurrent_job_is_busy(self):
        lock = self.root / '.discovery-live-runtime/public-runtime.lock'
        lock.parent.mkdir(parents=True)
        with exclusive_lock(lock):
            ops = FakeOps(self)
            rc, out = self.run_rt(ops)
        self.assertEqual((rc, out['error']), (1, 'busy'))
        self.assertEqual(ops.spawned, [])

    def test_job_arguments_are_strict(self):
        with self.assertRaises(rt.Failure):
            rt.check_job_args(self.job.parent / 'not-a-uuid', self.job.parent / 'not-a-uuid/public-result.json')
        with self.assertRaises(rt.Failure):
            rt.check_job_args(self.job, self.job / 'other.json')
        self.output.write_text('{}')
        with self.assertRaises(rt.Failure):
            rt.check_job_args(self.job, self.output)

    def test_shared_legacy_raw_proof_is_left_untouched(self):
        proof = self.root / '.discovery-live-runtime/proof'
        proof.mkdir(parents=True)
        (proof / 'cycle-sdk-raw.json').write_text('{"legacy": 1}')
        ops = FakeOps(self, driver_rc=1)
        self.run_rt(ops)
        self.assertEqual((proof / 'cycle-sdk-raw.json').read_text(), '{"legacy": 1}')
        self.assertEqual([p.name for p in proof.iterdir()], ['cycle-sdk-raw.json'])


class DriverPortTest(unittest.TestCase):
    def test_driver_accepts_only_6773_and_6775(self):
        for port in ('6767', '6771', '8782'):
            with mock.patch('sys.argv', ['x', '--root', 'r', '--server', f'http://127.0.0.1:{port}']), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                driver.main()
        for port in ('6773', '6775'):
            with mock.patch('sys.argv', ['x', '--root', 'missing-root', '--server', f'http://127.0.0.1:{port}', '--verify']), \
                    self.assertRaises(FileNotFoundError):
                driver.main()

    def test_proof_dir_defaults_to_legacy_location(self):
        with mock.patch('sys.argv', ['x', '--root', 'r']), mock.patch.object(driver.asyncio, 'run',
                                                                          side_effect=lambda c: c.close() or 0) as run:
            driver.main()
        self.assertEqual(run.call_count, 1)
        source = Path(driver.__file__).read_text(encoding='utf-8')
        self.assertIn("args.proof_dir or ROOT/'.discovery-live-runtime/proof'", source)


if __name__ == '__main__':
    unittest.main()
