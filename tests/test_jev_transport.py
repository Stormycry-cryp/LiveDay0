"""仅通过无凭据 loopback 测试发送门与 HTTP；不调用真实模型。"""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread
import time
import unittest
from unittest.mock import patch

from liveday0.jev import (JevHTTP, JudgmentFailure, MODEL, HOST, PATH, MAX_REQUEST_BYTES,
                         MAX_RESPONSE_BYTES, MAX_RESPONSE_ID_LENGTH)


QUESTIONS = {'support': {'type': 'choice', 'instructions': 'Is this supported?',
                         'criteria': {'yes': 'Supported', 'no': 'Unsupported'}}}
RESPONSE = {'id': 'task_synthetic_transport', 'model': MODEL, 'answers': {'support': {'type': 'choice', 'choice': 'yes',
            'probabilities': {'yes': 1, 'no': 0}, 'confidence': 1}},
            'usage': {'input_tokens': 4, 'output_tokens': 5}}


@contextmanager
def fixture(raw=None, *, status=200, content_type='application/json', length=None, delay=0,
            content_encoding=None):
    seen = []
    raw = json.dumps(RESPONSE).encode() if raw is None else raw
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)) if length is None else length)
                if content_encoding:
                    self.send_header('Content-Encoding', content_encoding)
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class JevTransportTests(unittest.TestCase):
    def test_live_two_decimal_probabilities_preserve_original_values(self):
        response = deepcopy(RESPONSE)
        response['answers']['support']['probabilities'] = {'yes': 0.50, 'no': 0.49}
        with fixture(json.dumps(response).encode()) as (port, _):
            self.assertEqual(JevHTTP(fixture_port=port, authorize_send=lambda _: True).evaluate('合成', QUESTIONS), response)
        response['answers']['support']['probabilities'] = {'yes': 0.50, 'no': 0.40}
        with fixture(json.dumps(response).encode()) as (port, _), self.assertRaises(JudgmentFailure):
            JevHTTP(fixture_port=port, authorize_send=lambda _: True).evaluate('合成', QUESTIONS)

    def test_live_score_rounding_accepts_bounded_error_and_rejects_inconsistent_score(self):
        questions = {'relevance': {'type': 'score', 'instructions': 'How relevant?',
                                   'criteria': ['none', 'topic', 'helpful', 'necessary']}}
        answer = {'type': 'score', 'score': 0.08, 'confidence': 0.93,
                  'legend': {str(i): value for i, value in enumerate(questions['relevance']['criteria'])},
                  'probabilities': {'0': 0.93, '1': 0.07, '2': 0, '3': 0}}
        response = {**deepcopy(RESPONSE), 'answers': {'relevance': answer}}
        with fixture(json.dumps(response).encode()) as (port, _):
            self.assertEqual(JevHTTP(fixture_port=port, authorize_send=lambda _: True).evaluate('合成', questions), response)
        answer['score'] = 0.20
        with fixture(json.dumps(response).encode()) as (port, _), self.assertRaises(JudgmentFailure):
            JevHTTP(fixture_port=port, authorize_send=lambda _: True).evaluate('合成', questions)

    def test_exact_protocol_and_detached_send_authorization(self):
        state = {'text': '合成生活材料'}
        questions = deepcopy(QUESTIONS)
        approvals = []
        def approve(payload):
            approvals.append(deepcopy(payload))
            payload['state']['text'] = '回调改动不能改变发送内容'
            payload['questions']['support']['criteria']['yes'] = 'changed'
            return True
        with fixture(content_type='application/json; charset=utf-8') as (port, seen):
            with patch.dict(os.environ, {'HTTPS_PROXY': 'http://example.invalid:9'}):
                result = JevHTTP(fixture_port=port, authorize_send=approve, timeout=1).evaluate(state, questions)
        self.assertEqual(result, RESPONSE)
        self.assertEqual(len(seen), 1)
        path, headers, raw = seen[0]
        self.assertEqual(path, PATH)
        self.assertEqual(HOST, 'api.beatapi.io')
        self.assertEqual(MODEL, 'jev-1.14-free')
        self.assertNotIn('Authorization', headers)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(json.loads(raw), approvals[0])
        self.assertEqual(approvals[0], {'state': state, 'model': MODEL, 'questions': QUESTIONS})
        self.assertEqual(questions, QUESTIONS)
        self.assertEqual(state['text'], '合成生活材料')

    def test_gate_rejects_before_any_network(self):
        def fail(_):
            raise RuntimeError('PRIVATE_SYNTHETIC_DETAIL')
        for approve in [None, lambda _: False, lambda _: 1, lambda _: {'approved': True}, fail]:
            with self.subTest(approve=approve), fixture() as (port, seen):
                with self.assertRaises(JudgmentFailure) as caught:
                    JevHTTP(fixture_port=port, authorize_send=approve).evaluate('合成', QUESTIONS)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(seen, [])

    def test_sizes_and_schema_checked_before_authorization(self):
        seen = []
        transport = JevHTTP(fixture_port=12345, authorize_send=lambda payload: seen.append(payload) or True)
        for state, questions in [('x' * MAX_REQUEST_BYTES, QUESTIONS),
            ({'x': float('nan')}, QUESTIONS), ({1: 'implicit key conversion'}, QUESTIONS),
            ('合成', {'support': {**QUESTIONS['support'], 'extra': 'bad'}}),
            ('合成', {'support': {**QUESTIONS['support'], 'type': 'generated-code'}}),
            ('合成', {}), (True, QUESTIONS)]:
            with self.subTest(state_type=type(state)), self.assertRaises(JudgmentFailure):
                transport.evaluate(state, questions)
        self.assertEqual(seen, [])

    def test_credentials_are_explicit_and_cannot_enter_fixture(self):
        with patch.dict(os.environ, {'BEATAPI_API_KEY': 'environment-is-ignored',
                                    'TYPESAFE_API_KEY': 'environment-is-ignored'}):
            with self.assertRaises(ValueError):
                JevHTTP()
        for key in ['', 'a\nb', '空白', True, 'x' * 513]:
            with self.subTest(key_type=type(key)), self.assertRaises(ValueError):
                JevHTTP(api_key=key)
        with self.assertRaises(ValueError):
            JevHTTP(api_key='explicit-secret', fixture_port=12345)
        for port in [True, 0, 65536, '12345']:
            with self.assertRaises(ValueError):
                JevHTTP(fixture_port=port)
        for timeout in [True, 0, -1, 31, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                JevHTTP(fixture_port=12345, timeout=timeout)
        transport = JevHTTP(api_key='explicit-secret', authorize_send=lambda _: False)
        self.assertNotIn('explicit-secret', repr(transport))
        with self.assertRaises(JudgmentFailure):
            transport.evaluate('合成', QUESTIONS)

    def test_http_errors_are_safe_and_never_retried(self):
        cases = [dict(status=401), dict(status=302), dict(status=429), dict(status=529),
                 dict(content_type='text/html'), dict(content_encoding='gzip'),
                 dict(raw=b'{"private":"PRIVATE_SYNTHETIC_DETAIL"}'),
                 dict(raw=b'not json PRIVATE_SYNTHETIC_DETAIL'),
                 dict(raw=b'{"model":"a","model":"b"}'),
                 dict(raw=b'{"x":NaN}'), dict(raw=b'\xff'), dict(length='-1'),
                 dict(length='99999999999999999999999'),
                 dict(raw=b'x' * (MAX_RESPONSE_BYTES + 1))]
        for case in cases:
            with self.subTest(case_keys=list(case)), fixture(**case) as (port, seen):
                with self.assertRaises(JudgmentFailure) as caught:
                    JevHTTP(fixture_port=port, authorize_send=lambda _: True, timeout=0.5).evaluate('合成', QUESTIONS)
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(len(seen), 1)

    def test_bound_response_model_and_typed_usage(self):
        for change in [lambda r: r.update(model='jev-latest'),
                       lambda r: r.update(model='jev-1.14'),
                       lambda r: r.update(model='jev-1.14.0'),
                       lambda r: r.update(model='jev-1.13-free'),
                       lambda r: r.pop('id'),
                       lambda r: r.update(id=''),
                       lambda r: r.update(id=' \t\n'),
                       lambda r: r.update(id=True),
                       lambda r: r.update(id=123),
                       lambda r: r.update(id=None),
                       lambda r: r.update(id='x' * (MAX_RESPONSE_ID_LENGTH + 1)),
                       lambda r: r['usage'].update(output_tokens=True),
                       lambda r: r['answers']['support'].update(choice='no'),
                       lambda r: r['answers'].update(unknown={}),
                       lambda r: r.update(unknown='bad')]:
            response = deepcopy(RESPONSE); change(response)
            with fixture(json.dumps(response).encode()) as (port, seen):
                with self.assertRaises(JudgmentFailure):
                    JevHTTP(fixture_port=port, authorize_send=lambda _: True).evaluate('合成', QUESTIONS)
                self.assertEqual(len(seen), 1)

    def test_timeout_and_connection_failure_have_static_reasons(self):
        with fixture(delay=0.2) as (port, seen):
            with self.assertRaisesRegex(JudgmentFailure, '^timeout$'):
                JevHTTP(fixture_port=port, authorize_send=lambda _: True, timeout=0.03).evaluate('合成', QUESTIONS)
            self.assertEqual(len(seen), 1)
        # fixture 关闭后，已知端口上的连接被拒绝；无需访问外网。
        with self.assertRaisesRegex(JudgmentFailure, '^transport_error$'):
            JevHTTP(fixture_port=port, authorize_send=lambda _: True, timeout=0.1).evaluate('合成', QUESTIONS)


if __name__ == '__main__':
    unittest.main()
