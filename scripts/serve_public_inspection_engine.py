"""Post-submission live demo: fixed CPU model behind an exact-origin tunnel.

The loopback-only local runner and its evidence-retaining behavior are unchanged.
This public runner erases its temporary request files before returning a response.
"""
import argparse
from collections import deque
import copy
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.serve_inspection_engine import EngineService, make_handler


class RequestLimit:
    """Shared sliding-window admission limit, including invalid attempts."""
    def __init__(self, per_minute=12, per_day=1000, clock=time.monotonic):
        self.per_minute = per_minute
        self.per_day = per_day
        self.clock = clock
        self.attempts = deque()
        self.lock = threading.Lock()

    def admit(self):
        with self.lock:
            current = self.clock()
            while self.attempts and self.attempts[0] <= current - 86400:
                self.attempts.popleft()
            recent = sum(t > current - 60 for t in self.attempts)
            if recent >= self.per_minute or len(self.attempts) >= self.per_day:
                return False
            self.attempts.append(current)
            return True


class PublicEngineService(EngineService):
    def run(self, payload):
        # Each clone shares the fixed backend; the handler's existing job lock
        # protects inference. Its output directory is private to this request.
        with tempfile.TemporaryDirectory(prefix='request-', dir=self.output) as folder:
            request_service = copy.copy(self)
            request_service.output = Path(folder)
            result = EngineService.run(request_service, payload)
            result['uploads_retained'] = False
            result['deployment_mode'] = 'public_post_hackathon_development'
        return result


def validate_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment or
            parsed.port is not None):
        raise ValueError('public origin must be exactly https://HOST with no path or port')
    if value != 'https://' + parsed.hostname:
        raise ValueError('public origin must use the canonical lowercase hostname')
    return value


def public_handler(service, page, port, public_origin, limiter=None):
    public_origin = validate_origin(public_origin)
    limiter = limiter if limiter is not None else RequestLimit()
    local_host = f'127.0.0.1:{port}'
    base = make_handler(service, page, port)

    class Handler(base):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def guard(self, write_request=False):
            # cloudflared config sets httpHostHeader to this exact loopback host.
            if self.headers.get('Host') != local_host:
                self.send_json(403, {'error': 'Unexpected origin host'})
                return False
            origin = self.headers.get('Origin')
            if ((write_request and origin != public_origin) or
                    (origin is not None and origin != public_origin)):
                self.send_json(403, {'error': 'Use the live demo from its published origin'})
                return False
            if write_request and self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self.send_json(415, {'error': 'application/json required'})
                return False
            return True

        def do_GET(self):
            if urlsplit(self.path).path == '/api/info':
                if not self.guard():
                    return
                self.send_json(200, {
                    'model_sha256': service.model_sha,
                    'hardware_connected': False, 'commercial_validated': False,
                    'deployment_mode': 'public_post_hackathon_development',
                    'uploads_retained': False, 'max_pixels': 1024 * 1024,
                    'max_image_bytes': 8 << 20, 'max_candidates': 200,
                    'global_attempts_per_minute': limiter.per_minute,
                    'global_attempts_per_day': limiter.per_day,
                    'availability': 'Owner-operated CPU server; owner PC must remain online',
                })
            else:
                super().do_GET()

        def do_POST(self):
            if not self.guard(True):
                return
            if urlsplit(self.path).path != '/api/analyze':
                self.send_json(404, {'error': 'Unknown fixed endpoint'})
                return
            if not limiter.admit():
                self.send_json(429, {'error': 'Shared demo execution limit reached. Try again later.'})
                return
            super().do_POST()

        def send_json(self, code, data):
            if code == 409:
                data = {'error': 'Another live analysis is running. Please try again shortly.'}
            elif code == 500:
                data = {'error': 'Analysis failed. Temporary request files were removed.'}
            super().send_json(code, data)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model-root', required=True)
    p.add_argument('--out', required=True, help='Dedicated scratch directory, outside the repository')
    p.add_argument('--example-frame', required=True)
    p.add_argument('--example-image', required=True)
    p.add_argument('--public-origin', required=True)
    p.add_argument('--port', type=int, default=8878)
    a = p.parse_args()
    validate_origin(a.public_origin)
    if not 1024 <= a.port <= 65535:
        raise ValueError('port must be in 1024..65535')
    service = PublicEngineService(a.model_root, a.out, a.example_frame, a.example_image)
    page = Path(__file__).resolve().parents[1] / 'web' / 'post-hackathon-engine' / 'public-inspection.html'
    server = ThreadingHTTPServer(('127.0.0.1', a.port), public_handler(service, page, a.port, a.public_origin))
    print(f'Ready: {a.public_origin} | fixed model {service.model_sha}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
