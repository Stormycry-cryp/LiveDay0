"""Machine-bound entry wiring tests using only a local credential-free server."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import types
import unittest
from unittest.mock import patch


_PATH = Path(__file__).resolve().parents[1] / 'src/liveday0/synthetic_trial_entry.py'
entry = types.ModuleType('synthetic_entry_under_test')
entry.__file__ = str(_PATH)
exec(compile(_PATH.read_bytes(), str(_PATH), 'exec'), entry.__dict__)


def response_body(content=None):
    fields = {'event_kind': 'plan_change', 'original_date': '2026-10-10',
              'updated_date': '2026-10-11', 'confirmation_status': 'pending',
              'source_quote': '合成试验：我原定2026年10月10日和朋友爬山，现已改到10月11日，正在等朋友确认。'}
    return {'model': 'glm-5.3-flash', 'id': 'PRIVATE_ID_SENTINEL',
            'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': json.dumps(fields) if content is None else content}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}


@contextmanager
def local_server(body, *, status=200, content_type='application/json', mode='normal', before=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            requests.append({'path': self.path, 'headers': dict(self.headers),
                             'body': self.rfile.read(int(self.headers.get('Content-Length', '0')))})
            if before is not None:
                before()
            if mode == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('X-Private-Identity', 'PRIVATE_HEADER_SENTINEL')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if mode == 'stall':
                # Handler outlives the short client timer; only fixture threads wait.
                import time
                time.sleep(0.12)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@unittest.skipUnless((entry.ROOT / 'glm_cn_once.py').is_file(),
                     'machine-bound frozen runner unavailable; portable diagnostics are separate')
class SyntheticEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy, cls.diagnostics = entry._dependencies()
        cls.contract = cls.legacy.load_contract()

    def assert_redacted(self, value):
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for sentinel in ('PRIVATE_SENTINEL', 'PRIVATE_ID_SENTINEL', 'PRIVATE_HEADER_SENTINEL',
                         'PRIVATE_KEY_SENTINEL', 'PRIVATE_VALUE_SENTINEL',
                         self.diagnostics.SYNTHETIC_SOURCE, 'synthetic-narrator',
                         'glm-cn-synthetic-once', str(entry.ROOT)):
            self.assertNotIn(sentinel, text)

    def attempt(self, port, state, **kwargs):
        return entry._fixture_attempt(self.legacy, self.diagnostics, self.contract,
                                      port=port, state=state, **kwargs)

    def test_success_uses_full_validator_and_saves_only_diagnostics(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / 'attempt'
            reserved_before_http = []
            body = json.dumps(response_body()).encode()
            with local_server(body, before=lambda: reserved_before_http.append((state/'attempt.json').exists())) as (port, requests):
                with patch.object(self.legacy, 'validate_response', wraps=self.legacy.validate_response) as validator:
                    result = self.attempt(port, state)
                self.assertEqual(validator.call_count, 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['path'], self.legacy.ENDPOINT)
                self.assertEqual(hashlib.sha256(requests[0]['body']).hexdigest(), entry.PAYLOAD_SHA)
                self.assertFalse(any(name.lower() == 'authorization' for name in requests[0]['headers']))
            self.assertEqual(reserved_before_http, [True])
            self.assertTrue(result['full_contract_validated'])
            self.assertEqual(result['diagnostic']['code'], 'ok')
            self.assertEqual(result, json.loads((state/'result.json').read_text()))
            self.assertEqual((state/'result.json').stat().st_mode & 0o777, 0o600)
            self.assert_redacted(result)

    def test_actual_http_errors_call_diagnostics_and_persist_redacted_result(self):
        cases = [(403, 'text/plain', b'PRIVATE_SENTINEL', 'http', 'authentication_or_permission'),
                 (200, 'text/html', b'PRIVATE_SENTINEL', 'http', 'invalid_content_type'),
                 (200, 'application/json', b'{"x":1,"x":2}', 'outer_json', 'duplicate_json_key'),
                 (200, 'application/json', b'PRIVATE_SENTINEL', 'outer_json', 'invalid_json')]
        provider_error = response_body(); provider_error['error'] = {'message': 'PRIVATE_SENTINEL'}
        cases.append((200, 'application/json', json.dumps(provider_error).encode(), 'provider', 'provider_error'))
        inner_duplicate = response_body('{"source_quote":"a","source_quote":"b"}')
        cases.append((200, 'application/json', json.dumps(inner_duplicate).encode(), 'inner_json', 'duplicate_json_key'))
        fields = json.loads(response_body()['choices'][0]['message']['content'])
        fields['PRIVATE_KEY_SENTINEL'] = 'PRIVATE_VALUE_SENTINEL'
        cases.append((200, 'application/json', json.dumps(response_body(json.dumps(fields))).encode(), 'schema', 'invalid_fixed_schema'))
        fields = json.loads(response_body()['choices'][0]['message']['content'])
        fields['confirmation_status'] = 'confirmed'
        response = response_body(json.dumps(fields)); response['usage'] = None
        cases.append((200, 'application/json', json.dumps(response).encode(), 'semantic', 'synthetic_extraction_mismatch'))
        for status, content_type, body, stage, code in cases:
            with self.subTest(stage=stage, code=code), TemporaryDirectory() as directory:
                state = Path(directory)/'attempt'
                with local_server(body, status=status, content_type=content_type) as (port, requests):
                    with patch.object(self.diagnostics, 'diagnose_response', wraps=self.diagnostics.diagnose_response) as diagnose:
                        result = self.attempt(port, state)
                    self.assertEqual(diagnose.call_count, 1)
                    self.assertEqual(len(requests), 1)
                    if status != 200 or content_type != 'application/json':
                        self.assertEqual(diagnose.call_args.kwargs['body'], b'')
                self.assertEqual((result['diagnostic']['stage'], result['diagnostic']['code']), (stage, code))
                self.assertFalse(result['full_contract_validated'])
                self.assertEqual(result, json.loads((state/'result.json').read_text()))
                self.assert_redacted(result)

    def test_body_bound_is_enforced_on_wire_path(self):
        with TemporaryDirectory() as directory, local_server(b'x'*65537) as (port, requests):
            result = self.attempt(port, Path(directory)/'attempt')
            self.assertEqual(result['diagnostic']['code'], 'response_too_large')
            self.assertEqual(len(requests), 1)
            self.assert_redacted(result)

    def test_transport_failure_and_timeout_use_new_diagnostics(self):
        for mode, code in (('disconnect', 'transport_error'), ('stall', 'timeout')):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                with local_server(json.dumps(response_body()).encode(), mode=mode) as (port, requests):
                    with patch.object(self.diagnostics, 'diagnose_transport_failure', wraps=self.diagnostics.diagnose_transport_failure) as diagnose:
                        result = self.attempt(port, Path(directory)/'attempt', timeout=0.03)
                    self.assertEqual(diagnose.call_count, 1)
                    self.assertEqual(len(requests), 1)
                self.assertEqual(result['diagnostic']['code'], code)
                self.assert_redacted(result)

    def test_failure_consumes_fixture_attempt_and_no_second_request(self):
        with TemporaryDirectory() as directory, local_server(b'PRIVATE_SENTINEL') as (port, requests):
            state = Path(directory)/'attempt'
            self.attempt(port, state)
            before = {name: (state/name).read_bytes() for name in ('attempt.json','result.json')}
            with self.assertRaisesRegex(entry.EntryFailure, 'fixture_attempt_unavailable'):
                self.attempt(port, state)
            self.assertEqual(len(requests), 1)
            self.assertEqual(before, {name: (state/name).read_bytes() for name in before})

    def test_full_contract_failure_does_not_echo_exception(self):
        with TemporaryDirectory() as directory, local_server(json.dumps(response_body()).encode()) as (port, _):
            with patch.object(self.legacy, 'validate_response', side_effect=ValueError('PRIVATE_SENTINEL')):
                result = self.attempt(port, Path(directory)/'attempt')
            self.assertEqual(result['diagnostic']['code'], 'invalid_full_contract')
            self.assertFalse(result['full_contract_validated'])
            self.assert_redacted(result)

    def test_cli_fixture_reaches_response_path_and_prints_redacted_failure(self):
        response = response_body(); response['error'] = 'PRIVATE_SENTINEL'
        with local_server(json.dumps(response).encode()) as (port, requests):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = entry.main(['--local-fixture', str(port)])
            self.assertEqual(code, 2)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['diagnostic']['code'], 'provider_error')
            self.assertEqual(len(requests), 1)
            self.assertEqual(stderr.getvalue(), '')
            self.assert_redacted(stdout.getvalue())

    def test_cli_preview_never_opens_connection_or_reads_credential(self):
        stdout = io.StringIO()
        with patch('http.client.HTTPConnection') as local, patch('http.client.HTTPSConnection') as real:
            with patch.object(self.legacy, 'read_credential') as credential, redirect_stdout(stdout):
                self.assertEqual(entry.main(['--preview']), 0)
            local.assert_not_called(); real.assert_not_called(); credential.assert_not_called()
        self.assertFalse(json.loads(stdout.getvalue())['execution_authorized'])
        self.assert_redacted(stdout.getvalue())

    def test_code_review_cannot_reopen_consumed_real_attempt(self):
        stderr = io.StringIO()
        with patch.object(entry, '_dependencies', return_value=(self.legacy, self.diagnostics)):
            with patch.object(entry, '_verify_review_gate', return_value=None):
                with patch('http.client.HTTPConnection') as local, patch('http.client.HTTPSConnection') as real:
                    with patch.object(self.legacy, 'read_credential') as credential, redirect_stderr(stderr):
                        self.assertEqual(entry.main(['--execute']), 2)
                    local.assert_not_called(); real.assert_not_called(); credential.assert_not_called()
        self.assertEqual(json.loads(stderr.getvalue())['code'], 'attempt_already_consumed')
        self.assert_redacted(stderr.getvalue())

    def test_missing_or_changed_history_never_grants_execution(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)/'state'; state.mkdir(mode=0o700)
            with patch.object(entry, 'STATE', state):
                with self.assertRaisesRegex(entry.EntryFailure, 'historical_state_unverifiable'):
                    entry._require_real_authorization()
                for name in entry._HISTORICAL_STATE:
                    (state/name).write_text('PRIVATE_SENTINEL')
                with self.assertRaisesRegex(entry.EntryFailure, 'historical_state_unverifiable'):
                    entry._require_real_authorization()

    def test_review_gate_checks_exact_dependency_manifest_and_duplicate_keys(self):
        with TemporaryDirectory() as directory:
            gate=Path(directory)/'gate.json'
            with self.assertRaisesRegex(entry.EntryFailure, 'source_review_not_approved'):
                entry._verify_review_gate(gate)
            gate.write_text(json.dumps(entry._review_manifest()))
            entry._verify_review_gate(gate)
            manifest=entry._review_manifest(); manifest['payload_sha256']='PRIVATE_SENTINEL'
            gate.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(entry.EntryFailure, 'source_review_not_approved'):
                entry._verify_review_gate(gate)
            gate.write_text('{"entry_version":2,"entry_version":2}')
            with self.assertRaisesRegex(entry.EntryFailure, 'source_review_not_approved'):
                entry._verify_review_gate(gate)

    def test_changed_dependency_is_rejected_without_executing_it(self):
        with TemporaryDirectory() as directory:
            source=Path(directory)/'source.py'; source.write_text("raise Exception('PRIVATE_SENTINEL')")
            with self.assertRaisesRegex(entry.EntryFailure, 'dependency_changed'):
                entry._load_pinned(source, '0'*64, 'untrusted_dependency')

    def test_fixture_arguments_cannot_select_historical_state_or_host(self):
        with patch('http.client.HTTPConnection') as connection:
            for port, state, timeout, code in ((True, Path('/tmp/fixture'), 1, 'invalid_fixture_port'),
                    (0, Path('/tmp/fixture'), 1, 'invalid_fixture_port'),
                    (1234, entry.STATE, 1, 'fixture_state_must_be_separate'),
                    (1234, entry.ROOT/'new-state', 1, 'fixture_state_must_be_separate'),
                    (1234, Path('/tmp/fixture'), 61, 'invalid_timeout')):
                with self.subTest(code=code), self.assertRaisesRegex(entry.EntryFailure, code):
                    self.attempt(port, state, timeout=timeout)
            connection.assert_not_called()

    def test_local_errors_are_static_in_cli(self):
        stderr=io.StringIO()
        with patch.object(entry, '_dependencies', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            with redirect_stderr(stderr):
                self.assertEqual(entry.main(['--preview']), 2)
        self.assertEqual(stderr.getvalue(), 'entry_local_error_no_retry\n')
        self.assert_redacted(stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
