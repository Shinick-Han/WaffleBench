"""Public boundary and non-retention checks; no holdout data or model download."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import request, error

from scripts.serve_inspection_engine import EngineService
from scripts.serve_public_inspection_engine import PublicEngineService, RequestLimit, public_handler, validate_origin


class PublicTests(unittest.TestCase):
    def test_sliding_limits_and_expiration(self):
        current = [0.0]
        limit = RequestLimit(per_minute=2, per_day=3, clock=lambda: current[0])
        self.assertTrue(limit.admit()); self.assertTrue(limit.admit())
        self.assertFalse(limit.admit())
        current[0] = 60.0
        self.assertTrue(limit.admit())
        current[0] = 120.0
        self.assertFalse(limit.admit())
        current[0] = 86400.0
        self.assertTrue(limit.admit())

    def test_origin_schema(self):
        self.assertEqual(validate_origin('https://demo.example'), 'https://demo.example')
        for value in ('http://demo.example', 'https://demo.example/', 'https://demo.example:443',
                      'https://user@demo.example', 'https://DEMO.example', 'https://demo.example?q=x'):
            with self.assertRaises(ValueError):
                validate_origin(value)

    def test_cleanup_success_and_failure_without_touching_other_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            service = PublicEngineService.__new__(PublicEngineService)
            service.output = Path(folder)
            existing = service.output / 'unrelated.txt'; existing.write_text('keep')
            def fake_run(instance, payload):
                (instance.output / 'acquired-image.bin').write_bytes(b'private acquired image')
                if payload['fail']:
                    raise RuntimeError('fixture failure')
                return {'status': 'complete'}
            with patch.object(EngineService, 'run', fake_run):
                result = service.run({'fail': False})
                self.assertFalse(result['uploads_retained'])
                with self.assertRaises(RuntimeError):
                    service.run({'fail': True})
            self.assertEqual(list(service.output.iterdir()), [existing])
            self.assertEqual(existing.read_text(), 'keep')

    def test_public_origin_fixed_routes_and_rate_limit(self):
        class Fake:
            model_sha = 'b' * 64
            lock = threading.Lock()
            example = {'fixture': True}
            def run(self, payload):
                return {'status': 'complete', 'uploads_retained': False}
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / 'ui.html'; page.write_text('<html>fixture</html>')
            server = ThreadingHTTPServer(('127.0.0.1', 0), public_handler(Fake(), page, 0, 'https://demo.example'))
            server.RequestHandlerClass = public_handler(Fake(), page, server.server_port,
                                                         'https://demo.example', RequestLimit(per_minute=1))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            def post(origin=None, path='/api/analyze'):
                headers = {'Content-Type': 'application/json'}
                if origin is not None:
                    headers['Origin'] = origin
                return request.urlopen(request.Request(url+path, data=b'{}', headers=headers))
            try:
                self.assertFalse(json.load(request.urlopen(url+'/api/info'))['uploads_retained'])
                for origin in (None, 'https://external.example', 'https://demo.example.evil.test'):
                    with self.assertRaises(error.HTTPError) as caught:
                        post(origin)
                    self.assertEqual(caught.exception.code, 403)
                with self.assertRaises(error.HTTPError) as caught:
                    post('https://demo.example', '/api/command')
                self.assertEqual(caught.exception.code, 404)
                self.assertEqual(json.load(post('https://demo.example'))['status'], 'complete')
                with self.assertRaises(error.HTTPError) as caught:
                    post('https://demo.example')
                self.assertEqual(caught.exception.code, 429)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
