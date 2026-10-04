"""Jev adapter checks with an injected fake transport. No external network calls, no real key.

Redirect checks use a loopback-only HTTP server with proxies disabled.
"""
import copy
import http.server
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from inspection_v2 import jev

FAKE_KEY = 'test-key-not-a-secret-7f3a91'
ENV = {jev.API_KEY_ENV: FAKE_KEY}


def note(note_id='n1', text='Stage motion blurred this acquisition; edges cannot be assessed.'):
    return {'id': note_id, 'note': text, 'source': 'review_station_log', 'observed_at': '2026-10-01T08:30:00+00:00'}


def good_response(route='repeat_image', noul=0.9):
    probs = {option: 0.0 for option in jev.ROUTE_CRITERIA}
    probs[route] = 0.97
    probs[jev.INSUFFICIENT if route != jev.INSUFFICIENT else 'sem_review'] = 0.03
    return {'model': jev.MODEL,
            'answers': {'route': {'type': 'choice', 'choice': route, 'confidence': 0.95, 'probabilities': probs},
                        'acquisition_invalid': {'type': 'noul', 'noul': noul}},
            'usage': {'input_tokens': 560, 'output_tokens': 80}}


class FakeTransport:
    """Records every call; replies from a queue of (status, body) or raises queued exceptions."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, body, headers, timeout_s):
        self.calls.append({'url': url, 'body': body, 'headers': dict(headers), 'timeout_s': timeout_s})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        status, payload = reply
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def client(transport, **kwargs):
    kwargs.setdefault('enabled', True)
    kwargs.setdefault('environ', ENV)
    return jev.JevClient(transport=transport, **kwargs)


class NoteWhitelist(unittest.TestCase):
    def test_accepts_exact_whitelist(self):
        record = jev.note_record(note())
        self.assertEqual(record.id, 'n1')

    def test_rejects_oracle_or_extra_fields(self):
        for extra in ({'doi': True}, {'kind': 'bridge'}, {'review_kind': 'particle'}, {'features': [1, 2]}):
            with self.assertRaises(ValueError):
                jev.note_record({**note(), **extra})
        with self.assertRaises(ValueError):
            jev.note_record({k: v for k, v in note().items() if k != 'source'})
        with self.assertRaises(TypeError):
            jev.note_record([('id', 'x')])

    def test_rejects_generated_sources_bad_timestamps_and_empty_text(self):
        for source in ('synthetic_wafers', 'Simulator', 'oracle_dump', 'mock'):
            with self.assertRaises(ValueError):
                jev.note_record({**note(), 'source': source})
        for stamp in ('yesterday', '2026-10-01T08:30:00'):
            with self.assertRaises(ValueError):
                jev.note_record({**note(), 'observed_at': stamp})
        with self.assertRaises(ValueError):
            jev.note_record({**note(), 'note': '   '})
        with self.assertRaises(ValueError):
            jev.note_record({**note(), 'note': 'x' * (jev.MAX_NOTE_CHARS + 1)})

    def test_payload_carries_only_note_text(self):
        transport = FakeTransport((200, good_response()))
        client(transport).judge(note())
        sent = json.loads(transport.calls[0]['body'])
        self.assertEqual(set(sent), {'model', 'state', 'questions'})
        self.assertEqual(sent['state'], {'inspection_note': note()['note']})
        self.assertEqual(sent['model'], 'jev-1.13.0')
        self.assertEqual(set(sent['questions']), {'route', 'acquisition_invalid'})


class Availability(unittest.TestCase):
    def test_absent_genuine_notes_is_unavailable(self):
        self.assertEqual(jev.integration_status([])['status'], 'unavailable')
        self.assertEqual(jev.integration_status(None)['reason'], 'no_genuine_notes')
        self.assertEqual(jev.SIMULATOR_INTEGRATION['status'], 'unavailable')
        self.assertEqual(jev.integration_status([note()])['status'], 'available')
        with self.assertRaises(ValueError):
            jev.integration_status([{**note(), 'doi': 1}])

    def test_offline_default_never_calls_transport(self):
        transport = FakeTransport((200, good_response()))
        result = jev.JevClient(transport=transport, environ=ENV).judge(note())
        self.assertEqual((result['status'], result['reason']), ('unavailable', 'offline'))
        self.assertIsNone(result['route'])
        self.assertFalse(result['actionable'])
        self.assertEqual(transport.calls, [])

    def test_missing_key_is_unavailable_without_call(self):
        transport = FakeTransport((200, good_response()))
        result = client(transport, environ={}).judge(note())
        self.assertEqual((result['status'], result['reason']), ('unavailable', 'missing_api_key'))
        self.assertEqual(transport.calls, [])

    def test_model_is_pinned(self):
        with self.assertRaises(ValueError):
            jev.JevClient(model='jev-latest')

    def test_rubric_requires_insufficient_information(self):
        questions = copy.deepcopy(jev.DEFAULT_QUESTIONS)
        del questions['route']['criteria'][jev.INSUFFICIENT]
        with self.assertRaises(ValueError):
            jev.JevClient(questions=questions)
        with self.assertRaises(ValueError):
            jev.validate_questions({'q': {'type': 'score', 'instructions': 'x', 'criteria': ['a', 'b']}})


class SecretHandling(unittest.TestCase):
    def test_key_only_in_authorization_header(self):
        transport = FakeTransport((200, good_response()))
        with tempfile.TemporaryDirectory() as tmp:
            c = client(transport, cache_dir=tmp)
            result = c.judge(note())
            self.assertEqual(result['status'], 'ok')
            call = transport.calls[0]
            self.assertEqual(call['headers']['Authorization'], 'Bearer ' + FAKE_KEY)
            self.assertNotIn(FAKE_KEY.encode(), call['body'])
            self.assertNotIn(FAKE_KEY, json.dumps(result))
            self.assertNotIn(FAKE_KEY, repr(c))
            self.assertNotIn(FAKE_KEY, json.dumps(vars(c), default=str))
            for path in Path(tmp).iterdir():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding='utf-8'))

    def test_key_absent_from_failure_results(self):
        for reply in ((500, b''), (200, b'not json'), urllib.error.URLError('boom ' + FAKE_KEY)):
            result = client(FakeTransport(reply)).judge(note())
            self.assertEqual(result['status'], 'abstain')
            self.assertNotIn(FAKE_KEY, json.dumps(result))


class Cache(unittest.TestCase):
    def test_memory_cache_hit_avoids_second_call_and_bills_no_tokens(self):
        transport = FakeTransport((200, good_response()))
        c = client(transport)
        first, second = c.judge_batch([note('a'), note('b')])  # identical text, different ids
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(first['cache_hit'])
        self.assertTrue(second['cache_hit'])
        self.assertEqual(second['note_id'], 'b')
        self.assertEqual(second['usage'], {'input_tokens': 0, 'output_tokens': 0})
        self.assertEqual(first['cache_key'], second['cache_key'])

    def test_key_changes_with_model_rubric_and_input(self):
        state = {'inspection_note': 'x'}
        rubric = jev.rubric_hash(jev.DEFAULT_QUESTIONS)
        other_questions = copy.deepcopy(jev.DEFAULT_QUESTIONS)
        other_questions['route']['instructions'] += ' Prefer SEM when unsure.'
        other_rubric = jev.rubric_hash(other_questions)
        self.assertNotEqual(rubric, other_rubric)
        base = jev.cache_key(jev.MODEL, rubric, state)
        self.assertNotEqual(base, jev.cache_key(jev.MODEL, other_rubric, state))
        self.assertNotEqual(base, jev.cache_key('jev-1.14.0', rubric, state))
        self.assertNotEqual(base, jev.cache_key(jev.MODEL, rubric, {'inspection_note': 'y'}))
        self.assertEqual(base, jev.cache_key(jev.MODEL, rubric, dict(state)))
        reordered = dict(reversed(list(jev.DEFAULT_QUESTIONS.items())))
        self.assertEqual(rubric, jev.rubric_hash(reordered))

    def test_disk_cache_shared_only_for_same_rubric(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FakeTransport((200, good_response()))
            client(first, cache_dir=tmp).judge(note())
            replay = FakeTransport((500, b''))
            result = client(replay, cache_dir=tmp).judge(note())
            self.assertEqual((result['status'], result['cache_hit']), ('ok', True))
            self.assertEqual(replay.calls, [])
            other_questions = copy.deepcopy(jev.DEFAULT_QUESTIONS)
            other_questions['acquisition_invalid']['instructions'] = 'Is the image explicitly unusable?'
            changed = FakeTransport((200, good_response()))
            client(changed, cache_dir=tmp, questions=other_questions).judge(note())
            self.assertEqual(len(changed.calls), 1)

    def test_corrupt_disk_cache_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            client(FakeTransport((200, good_response())), cache_dir=tmp).judge(note())
            for path in Path(tmp).glob('jev-*.json'):
                path.write_text('{"schema": 1, "broken": true}', encoding='utf-8')
            transport = FakeTransport((200, good_response()))
            result = client(transport, cache_dir=tmp).judge(note())
            self.assertFalse(result['cache_hit'])
            self.assertEqual(len(transport.calls), 1)

    def test_failures_are_not_cached(self):
        transport = FakeTransport((503, b''), (200, good_response()))
        c = client(transport)
        self.assertEqual(c.judge(note())['status'], 'abstain')
        self.assertEqual(c.judge(note())['status'], 'ok')
        self.assertEqual(len(transport.calls), 2)

    def test_cache_dir_must_exist(self):
        with self.assertRaises(ValueError):
            jev.JevClient(cache_dir=Path(tempfile.gettempdir()) / 'jev-does-not-exist-91c2')


class Failures(unittest.TestCase):
    def assert_abstain(self, reply, reason):
        result = client(FakeTransport(reply)).judge(note())
        self.assertEqual((result['status'], result['reason']), ('abstain', reason))
        self.assertIsNone(result['route'])
        self.assertIsNone(result['answers'])
        self.assertFalse(result['actionable'])
        return result

    def test_transport_failures_abstain(self):
        self.assert_abstain(TimeoutError(), 'timeout')
        self.assert_abstain(socket.timeout(), 'timeout')
        self.assert_abstain(urllib.error.URLError(TimeoutError()), 'timeout')
        self.assert_abstain(urllib.error.URLError('dns'), 'transport_error')
        self.assert_abstain(ConnectionResetError(), 'transport_error')

    def test_http_errors_abstain_and_auth_halts(self):
        self.assert_abstain((500, b''), 'http_500')
        transport = FakeTransport((401, b''), (200, good_response()))
        c = client(transport)
        self.assertEqual(c.judge(note('a', 'first'))['reason'], 'http_401')
        self.assertEqual(c.judge(note('b', 'second'))['reason'], 'halted_after_http_401')
        self.assertEqual(len(transport.calls), 1)

    def test_request_budget(self):
        transport = FakeTransport((200, good_response()))
        c = client(transport, max_requests=1)
        c.judge(note('a', 'first'))
        self.assertEqual(c.judge(note('b', 'second'))['reason'], 'request_budget_exhausted')
        self.assertEqual(len(transport.calls), 1)

    def test_malformed_bodies(self):
        self.assert_abstain((200, b'not json'), 'malformed_response')
        self.assert_abstain((200, b'\xff\xfe'), 'malformed_response')
        self.assert_abstain((200, b'[]'), 'schema_error')

    def test_schema_and_probability_violations(self):
        def mutated(fn):
            response = good_response()
            fn(response)
            return response

        route = lambda r: r['answers']['route']  # noqa: E731
        cases = {
            'wrong model': lambda r: r.update(model='jev-latest'),
            'missing answer': lambda r: r['answers'].pop('acquisition_invalid'),
            'extra answer': lambda r: r['answers'].update(extra={'type': 'noul', 'noul': 0.1}),
            'type mismatch': lambda r: route(r).update(type='noul'),
            'unknown option': lambda r: route(r)['probabilities'].update(scrap=0.0),
            'choice not offered': lambda r: route(r).update(choice='scrap'),
            'choice not argmax': lambda r: route(r).update(choice=jev.INSUFFICIENT),
            'sum not one': lambda r: route(r)['probabilities'].update(sem_review=0.5),
            'negative prob': lambda r: route(r)['probabilities'].update(sem_review=-0.01, no_further_review=0.01),
            'nan prob': lambda r: route(r)['probabilities'].update(sem_review=float('nan')),
            'bool prob': lambda r: route(r)['probabilities'].update(sem_review=True),
            'confidence range': lambda r: route(r).update(confidence=1.5),
            'noul inf': lambda r: r['answers']['acquisition_invalid'].update(noul=float('inf')),
            'noul string': lambda r: r['answers']['acquisition_invalid'].update(noul='0.4'),
            'usage negative': lambda r: r['usage'].update(input_tokens=-1),
            'usage float': lambda r: r['usage'].update(output_tokens=1.5),
            'usage missing': lambda r: r.pop('usage'),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                body = json.dumps(mutated(fn)).encode()  # json emits NaN/Infinity literals
                self.assert_abstain((200, body), 'schema_error')

    def test_insufficient_information_is_ok_but_not_actionable(self):
        result = client(FakeTransport((200, good_response(jev.INSUFFICIENT)))).judge(note())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['route'], jev.INSUFFICIENT)
        self.assertFalse(result['actionable'])


class ShadowAudit(unittest.TestCase):
    def test_audit_counts_coverage_errors_tokens_latency(self):
        transport = FakeTransport((200, good_response('repeat_image')), (200, good_response('sem_review')),
                                  (200, good_response(jev.INSUFFICIENT)), (500, b''))
        ticks = iter([0.0, 0.1, 1.0, 1.2, 2.0, 2.3, 3.0, 3.4])
        c = client(transport, clock=lambda: next(ticks))
        results = c.judge_batch([note('a', 'one'), note('b', 'two'), note('c', 'three'), note('d', 'four'),
                                 note('e', 'one')])
        labels = {'a': 'repeat_image', 'b': 'electrical_review', 'c': 'sem_review', 'd': 'sem_review',
                  'e': 'repeat_image'}
        audit = jev.shadow_audit(results + [{'note_id': 'zz', 'status': 'unavailable'}], labels)
        self.assertEqual(audit['status'], 'complete')
        self.assertFalse(audit['affects_decisions'])
        self.assertEqual(audit['labelled'], 5)
        self.assertEqual(audit['unlabelled_judgements'], 1)
        self.assertEqual(audit['answered'], 4)          # a, b, c, e (cache hit)
        self.assertEqual(audit['covered'], 3)           # c is insufficient information
        self.assertAlmostEqual(audit['coverage'], 3 / 5)
        self.assertEqual(audit['errors'], 1)            # b
        self.assertEqual(audit['abstention_reasons'], {'http_500': 1})
        self.assertEqual(audit['cache_hits'], 1)
        self.assertEqual(audit['input_tokens'], 3 * 560)
        self.assertEqual(audit['output_tokens'], 3 * 80)
        self.assertAlmostEqual(audit['latency_ms_p50'], 250.0)
        self.assertAlmostEqual(audit['latency_ms_p95_nearest_rank'], 400.0)
        self.assertIsNotNone(audit['brier_answered'])
        self.assertEqual(audit['confusion']['electrical_review'], {'sem_review': 1})
        top = [row for row in audit['risk_coverage'] if row['confidence_threshold'] == 0.9][0]
        self.assertEqual((top['kept'], top['risk']), (3, 1 / 3))

    def test_audit_without_notes_or_answers(self):
        self.assertEqual(jev.shadow_audit([], {})['status'], 'unavailable')
        offline = jev.JevClient(environ=ENV).judge_batch([note('a')])
        audit = jev.shadow_audit(offline, {'a': 'sem_review'})
        self.assertEqual(audit['status'], 'no_answers')
        self.assertEqual(audit['coverage'], 0.0)
        self.assertIsNone(audit['accuracy_answered'])

    def test_audit_rejects_labels_outside_rubric(self):
        with self.assertRaises(ValueError):
            jev.shadow_audit([], {'a': 'scrap_wafer'})




class LoopbackServer:
    """127.0.0.1 HTTP server recording every request; /redirect answers `code` toward /capture."""

    def __init__(self, code):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                self.rfile.read(length)
                outer.requests.append({'path': self.path, 'headers': dict(self.headers)})
                if self.path == '/redirect':
                    self.send_response(code)
                    self.send_header('Location', outer.url('/capture'))
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    payload = json.dumps(good_response()).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            do_GET = do_POST  # urllib's default handler rewrites a redirected POST to GET

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, path):
        return f'http://127.0.0.1:{self.server.server_address[1]}{path}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def loopback_opener():
    """The production redirect policy without environment proxies, so loopback traffic stays local."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), jev._RefuseRedirects)


class EndpointPinning(unittest.TestCase):
    def test_rejects_any_nonofficial_endpoint_at_construction(self):
        for endpoint in ('https://evil.example/v1/systemone', 'http://api.typesafe.ai/v1/systemone',
                         jev.ENDPOINT + '/', jev.ENDPOINT + '?x=1', 'https://api.typesafe.ai.evil.example/v1/systemone',
                         'https://api.typesafe.ai/v2/systemone', ''):
            transport = FakeTransport((200, good_response()))
            with self.assertRaises(ValueError):
                client(transport, endpoint=endpoint)
            self.assertEqual(transport.calls, [])

    def test_official_endpoint_is_the_only_target(self):
        transport = FakeTransport((200, good_response()))
        self.assertEqual(client(transport, endpoint=jev.ENDPOINT).judge(note())['status'], 'ok')
        self.assertEqual([call['url'] for call in transport.calls], [jev.ENDPOINT])

    def test_default_transport_refuses_other_urls_before_any_io(self):
        self.assertIs(jev.JevClient().transport, jev.urllib_transport)
        with self.assertRaises(ValueError):
            jev.urllib_transport('https://evil.example/v1/systemone', b'{}',
                                 {'Authorization': 'Bearer ' + FAKE_KEY}, 1.0)

    def test_default_opener_replaces_the_following_redirect_handler(self):
        redirect_handlers = [h for h in jev._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h) for h in redirect_handlers], [jev._RefuseRedirects])


class RedirectRefusal(unittest.TestCase):
    def test_redirect_is_not_followed_and_credential_never_forwarded(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), LoopbackServer(code) as server:
                status, body = jev._post(loopback_opener(), server.url('/redirect'), b'{}',
                                         {'Authorization': 'Bearer ' + FAKE_KEY}, 5.0)
                self.assertEqual((status, body), (code, b''))
                self.assertEqual([r['path'] for r in server.requests], ['/redirect'])

    def test_client_abstains_and_halts_on_redirect(self):
        with LoopbackServer(307) as server:
            urls = []

            def loopback_transport(url, body, headers, timeout_s):
                urls.append(url)
                return jev._post(loopback_opener(), server.url('/redirect'), body, headers, timeout_s)

            c = client(loopback_transport)
            first = c.judge(note('a', 'first'))
            second = c.judge(note('b', 'second'))
        self.assertEqual((first['status'], first['reason']), ('abstain', jev.REDIRECT_REFUSED))
        self.assertIsNone(first['route'])
        self.assertEqual((second['status'], second['reason']), ('abstain', 'halted_after_' + jev.REDIRECT_REFUSED))
        self.assertEqual(urls, [jev.ENDPOINT])
        self.assertEqual([r['path'] for r in server.requests], ['/redirect'])
        self.assertNotIn(FAKE_KEY, json.dumps([first, second]))

    def test_control_stdlib_default_would_forward_the_credential(self):
        # Guards the redirect test against passing vacuously: urllib's default handler follows the
        # 302 and re-sends Authorization to /capture, which this harness detects.
        with LoopbackServer(302) as server:
            default = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            status, _ = jev._post(default, server.url('/redirect'), b'{}', {'Authorization': 'Bearer ' + FAKE_KEY}, 5.0)
        self.assertEqual(status, 200)
        self.assertEqual([r['path'] for r in server.requests], ['/redirect', '/capture'])
        self.assertEqual(server.requests[1]['headers'].get('Authorization'), 'Bearer ' + FAKE_KEY)

    def test_fake_redirect_status_abstains(self):
        transport = FakeTransport((302, b''), (200, good_response()))
        c = client(transport)
        self.assertEqual(c.judge(note('a', 'first'))['reason'], jev.REDIRECT_REFUSED)
        self.assertEqual(c.judge(note('b', 'second'))['reason'], 'halted_after_' + jev.REDIRECT_REFUSED)
        self.assertEqual(len(transport.calls), 1)


if __name__ == '__main__':
    unittest.main()
