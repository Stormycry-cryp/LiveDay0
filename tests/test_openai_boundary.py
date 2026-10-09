"""Synthetic HTTP protocol fixtures, never a model quality or paid-call test."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Event, Thread
import time
from uuid import uuid4

import pytest

from liveday0 import extraction_contract as c
from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, InterpretationRevoked
from liveday0.local_extraction import DeterministicLocalAdapter, SYNTHETIC_EXAMPLES, validate_and_gate
from liveday0.local_host import LocalMemorySession
from liveday0.openai_extraction import OpenAIResponsesAdapter, ResponsesHTTP, output_schema
from liveday0.provider_budget import CallBudget, MODEL, MAX_INPUT, RESERVATION_USD, ProviderFailure
from tests.test_atomic_writes import snapshot


TEXT = next(iter(SYNTHETIC_EXAMPLES))
STAMP = '2026-10-09T00:00:00+00:00'


def request(**kwargs):
    return c.ExtractionRequest.build(str(uuid4()), '1', TEXT, STAMP, **kwargs)


def wire():
    # Derive only the expected HTTP body from the existing synthetic fixture.
    p = DeterministicLocalAdapter().extract(request()).to_dict()['proposals'][0]
    for k in ('schema_version', 'proposal_id', 'source', 'producer'):
        p.pop(k)
    p.update(span_start=0, span_end=len(TEXT), revision_intent=p.pop('revision')['intent'])
    semantic = p['semantic_input']
    semantic.pop('canonical_key'); semantic.pop('valid_at')
    for k in c._BODY_OPTIONAL[semantic['card_type']]:
        semantic['body'].setdefault(k, None)
    return {'proposals': [p]}


def response():
    return {'model': MODEL, 'status': 'completed', 'error': None, 'incomplete_details': None,
        'usage': {'input_tokens': 100, 'output_tokens': 100}, 'output': [{
            'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': json.dumps(wire(), ensure_ascii=False)}]}]}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    real = socket.socket.connect
    def guarded(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert sock.family == socket.AF_INET and address[0] == '127.0.0.1', 'external network forbidden'
        return real(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    resolve = socket.getaddrinfo
    def local_resolution(host, *args, **kwargs):
        assert host == '127.0.0.1', 'external DNS forbidden'
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', local_resolution)


@pytest.fixture
def http_fixture():
    class Fixture:
        status = 200
        data = None
        content_type = 'application/json'
        mode = 'normal'
        requests = None
    fixture = Fixture()
    fixture.data = json.dumps(response(), ensure_ascii=False).encode()
    fixture.requests = []
    received, release = Event(), Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            fixture.requests.append((self.path, dict(self.headers), json.loads(raw)))
            received.set()
            if fixture.mode == 'stall_headers':
                release.wait(1)
            try:
                self.send_response(fixture.status)
                self.send_header('Content-Type', fixture.content_type)
                self.send_header('Content-Length', str(len(fixture.data)))
                if fixture.status == 302:
                    self.send_header('Location', 'https://example.invalid/never-follow')
                self.end_headers()
                if fixture.mode == 'stall_body':
                    self.wfile.write(fixture.data[:1]); self.wfile.flush()
                    release.wait(1)
                    self.wfile.write(fixture.data[1:])
                else:
                    self.wfile.write(fixture.data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    fixture.port, fixture.received, fixture.release = server.server_port, received, release
    try:
        yield fixture
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(timeout=2)
        assert not thread.is_alive()


def adapter(fixture, *, budget=None, counter=lambda model, payload: 100, timeout=1):
    return OpenAIResponsesAdapter(transport=ResponsesHTTP(fixture_port=fixture.port, timeout=timeout),
        budget=budget or CallBudget(), token_counter=counter)


def command(**changes):
    return {'action': 'capture', 'text': TEXT, 'message_id': str(uuid4()), 'occurred_at': STAMP, **changes}


def test_http_contract_host_binding_and_no_credential(http_fixture):
    seen = []
    def counter(model, payload):
        seen.append((model, deepcopy(payload)))
        payload['input'] = []  # Counter cannot mutate the serialized request.
        return 100
    a = adapter(http_fixture, counter=counter)
    r = request(chunk_start=13)
    batch = a.extract(r); p = batch.proposals[0]
    path, headers, payload = http_fixture.requests[0]
    assert path == '/v1/responses' and 'Authorization' not in headers
    assert payload == seen[0][1] and seen[0][0] == MODEL
    assert payload['store'] is False and payload['stream'] is False and 'tools' not in payload
    assert payload['max_output_tokens'] == 4096 and payload['text']['format']['strict'] is True
    assert payload['text']['format']['schema'] == output_schema()
    assert r.source.source_id not in json.dumps(payload)
    assert p.source.source_id == r.source.source_id and p.source.span.start == 13
    assert p.source.span.end == 13 + len(TEXT) and p.source.span.digest == c.sha256_digest(TEXT)
    assert batch.request_fingerprint == r.fingerprint and p.semantic_input.valid_at == r.occurred_at
    assert p.producer.producer_id == 'openai-responses-fixture' and not a.production_ready
    assert a._budget.snapshot()['entries'] == [{'status': 'validated', 'input_tokens': 100, 'output_tokens': 100}]


@pytest.mark.parametrize('case', ['missing-counter', 'counter-raises', 'too-many-tokens', 'bool-tokens', 'secret'])
def test_preflight_does_not_connect_or_reserve(http_fixture, case):
    def bad(model, payload):
        raise RuntimeError('SYNTHETIC_PRIVATE_COUNTER_DETAIL')
    counter = {'missing-counter': None, 'counter-raises': bad, 'too-many-tokens': lambda m,p: MAX_INPUT+1,
               'bool-tokens': lambda m,p: True}.get(case, lambda m,p: 100)
    a = adapter(http_fixture, counter=counter)
    with pytest.raises(ProviderFailure) as caught:
        a.extract(request(sensitivity='secret' if case == 'secret' else 'ordinary'))
    assert 'PRIVATE' not in str(caught.value)
    assert a._budget.snapshot()['calls'] == 0 and http_fixture.requests == []


@pytest.mark.parametrize('approval', [None, lambda r: False, lambda r: 'yes', lambda r: True])
def test_remote_requires_exact_approval_and_preflight_without_connecting(approval, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'IGNORED_SYNTHETIC_KEY')
    with pytest.raises(ValueError, match='explicit'):
        ResponsesHTTP()
    transport = ResponsesHTTP(api_key='EXPLICIT_SYNTHETIC_NOT_A_REAL_KEY')
    budget = CallBudget()
    a = OpenAIResponsesAdapter(transport=transport, budget=budget, authorize_send=approval)
    with pytest.raises(ProviderFailure, match='outbound_approval_required|token_preflight_unavailable'):
        a.extract(request())
    assert budget.snapshot()['calls'] == 0
    with pytest.raises(ValueError, match='no credential'):
        ResponsesHTTP(api_key='NEVER', fixture_port=12345)


@pytest.mark.parametrize('case', [
    'redirect', 'rate-limit', 'server-error', 'wrong-content-type', 'oversize', 'malformed-json',
    'incomplete', 'refusal', 'tool-call', 'wrong-model', 'invalid-inner-json', 'duplicate-key',
    'unknown-key', 'unknown-null-key', 'missing-key', 'source-id-forgery', 'bad-span', 'bool-span',
    'nan-confidence', 'required-null', 'two-proposals', 'usage-missing', 'usage-over-limit',
])
def test_http_failures_never_write_or_retry(http_fixture, case):
    res = response(); w = wire(); p = w['proposals'][0]
    if case in {'redirect', 'rate-limit', 'server-error'}:
        http_fixture.status = {'redirect': 302, 'rate-limit': 429, 'server-error': 503}[case]
    elif case == 'wrong-content-type': http_fixture.content_type = 'text/html'
    elif case == 'incomplete': res['status'] = 'incomplete'
    elif case == 'refusal': res['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'SYNTHETIC_PRIVATE'}]
    elif case == 'tool-call': res['output'] = [{'type': 'function_call', 'name': 'delete'}]
    elif case == 'wrong-model': res['model'] = 'other'
    elif case == 'unknown-key': p['action'] = 'delete'
    elif case == 'unknown-null-key': p['semantic_input']['body']['tenant_id'] = None
    elif case == 'missing-key': p.pop('privacy_class')
    elif case == 'source-id-forgery': p['source_id'] = str(uuid4())
    elif case == 'bad-span': p['span_end'] += 1
    elif case == 'bool-span': p['span_start'] = False
    elif case == 'nan-confidence': p['confidence'] = float('nan')
    elif case == 'required-null': p['semantic_input']['body']['goal_context'] = None
    elif case == 'two-proposals': w['proposals'].append(deepcopy(p))
    elif case == 'usage-missing': res.pop('usage')
    elif case == 'usage-over-limit': res['usage']['input_tokens'] = MAX_INPUT + 1
    if case not in {'refusal', 'tool-call'}:
        res['output'][0]['content'][0]['text'] = json.dumps(w)
    if case == 'invalid-inner-json': res['output'][0]['content'][0]['text'] = '{SYNTHETIC_PRIVATE'
    if case == 'duplicate-key': res['output'][0]['content'][0]['text'] = '{"proposals":[],"proposals":[]}'
    http_fixture.data = json.dumps(res).encode()
    if case == 'oversize': http_fixture.data = b' ' * 64001
    if case == 'malformed-json': http_fixture.data = b'{SYNTHETIC_PRIVATE'
    budget = CallBudget(max_calls=1)
    session = LocalMemorySession(adapter=adapter(http_fixture, budget=budget))
    before = snapshot(MemoryService(session.tenant_id))
    with pytest.raises(ProviderFailure) as caught:
        session.handle(command())
    assert 'SYNTHETIC_PRIVATE' not in str(caught.value)
    assert snapshot(MemoryService(session.tenant_id)) == before
    assert len(http_fixture.requests) == 1
    assert budget.snapshot()['reserved_usd'] == str(RESERVATION_USD)
    with pytest.raises(ProviderFailure, match='budget_exhausted'):
        session.handle(command())
    assert len(http_fixture.requests) == 1


@pytest.mark.parametrize('mode', ['stall_headers', 'stall_body'])
def test_deadline_retains_reservation_and_no_writes(http_fixture, mode):
    http_fixture.mode = mode
    a = adapter(http_fixture, timeout=0.1)
    session = LocalMemorySession(adapter=a); before = snapshot(MemoryService(session.tenant_id))
    start = time.monotonic()
    with pytest.raises(ProviderFailure, match='timeout'):
        session.handle(command())
    assert time.monotonic() - start < 0.8
    assert snapshot(MemoryService(session.tenant_id)) == before and len(http_fixture.requests) == 1
    assert a._budget.snapshot()['calls'] == 1


def test_concurrent_adapter_calls_do_not_amplify(http_fixture):
    http_fixture.mode = 'stall_headers'
    a = adapter(http_fixture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(a.extract, request())
        assert http_fixture.received.wait(1)
        with pytest.raises(ProviderFailure, match='concurrent_call_rejected'):
            a.extract(request())
        http_fixture.release.set(); first.result(timeout=2)
    assert len(http_fixture.requests) == 1 and a._budget.snapshot()['calls'] == 1


def test_budget_reserves_atomically_and_never_refunds():
    budget = CallBudget(max_calls=12, max_usd=RESERVATION_USD * 2)
    def attempt(_):
        try: return budget.reserve()
        except ProviderFailure: return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sorted(x for x in results if x is not None) == [0, 1]
    budget.finish(0, 'timeout'); budget.finish(1, 'validated', {'input_tokens': 1, 'output_tokens': 1})
    assert budget.snapshot()['reserved_usd'] == str(RESERVATION_USD * 2)
    with pytest.raises(ProviderFailure, match='budget_exhausted'): budget.reserve()
    with pytest.raises(ValueError): CallBudget(max_calls=13)
    with pytest.raises(ValueError): CallBudget(max_usd=Decimal('NaN'))


@pytest.mark.parametrize('field,value,reason', [
    ('speech_mode', 'quotation', 'non_authoritative_speech_mode'),
    ('epistemic_state', 'inferred', 'not_asserted'),
    ('privacy_class', 'never_store', 'never_store'),
    ('persistence_intent', 'explicit_save', 'confirmation'),
])
def test_http_output_still_passes_existing_privacy_gate(http_fixture, field, value, reason):
    w = wire(); w['proposals'][0][field] = value
    if reason == 'confirmation': w['proposals'][0]['privacy_class'] = 'sensitive'
    res = response(); res['output'][0]['content'][0]['text'] = json.dumps(w)
    http_fixture.data = json.dumps(res).encode()
    session = LocalMemorySession(adapter=adapter(http_fixture)); before = snapshot(MemoryService(session.tenant_id))
    if reason == 'confirmation':
        with pytest.raises(AuthorizationRequired): session.handle(command())
    else:
        assert session.handle(command())['outcomes'][0]['reason'] == reason
    assert snapshot(MemoryService(session.tenant_id)) == before


def test_secret_gate_precedes_http_and_budget(http_fixture):
    a = adapter(http_fixture); session = LocalMemorySession(adapter=a)
    before = snapshot(MemoryService(session.tenant_id))
    assert session.handle(command(sensitivity='secret'))['outcomes'][0]['reason'] == 'secret_source'
    assert http_fixture.requests == [] and a._budget.snapshot()['calls'] == 0
    assert snapshot(MemoryService(session.tenant_id)) == before


def test_http_capture_interpret_delete_and_revocation(http_fixture):
    a = adapter(http_fixture)
    session = LocalMemorySession(adapter=a, confirm=lambda check: True)  # Synthetic consent only.
    receipt = session.handle(command())['outcomes'][0]
    assert receipt['status'] == 'stored' and len(http_fixture.requests) == 1
    result = session.handle({'action': 'interpret', 'evidence_id': str(receipt['evidence_id'])})
    assert result['created'] and len(http_fixture.requests) == 2
    with tenant_transaction(session.tenant_id, mode='read') as conn:
        evidence = conn.execute('SELECT content FROM evidence WHERE id=%s', (receipt['evidence_id'],)).fetchone()
    assert evidence['content'] == TEXT
    session.handle({'action': 'delete', 'kind': 'card', 'id': str(receipt['card_ids'][0])})
    before = snapshot(MemoryService(session.tenant_id))
    with pytest.raises(InterpretationRevoked):
        session.handle({'action': 'interpret', 'evidence_id': str(receipt['evidence_id'])})
    assert len(http_fixture.requests) == 2 and snapshot(MemoryService(session.tenant_id)) == before
