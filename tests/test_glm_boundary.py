"""Synthetic loopback protocol/contract tests, not real-service quality evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
import time
from uuid import uuid4

import pytest

from liveday0 import extraction_contract as c
from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, DeletedSource, InterpretationRevoked, VersionConflict
from liveday0.glm_extraction import GLMCNAdapter, GLMHTTP, GLMCallBudget, SendApproval, MODEL, HOST, PATH, RESERVATION_CNY
from liveday0.local_extraction import saved_request, validate_and_gate
from liveday0.local_host import LocalMemorySession
from liveday0.provider_budget import ProviderFailure
from tests.test_atomic_writes import snapshot
from tests.test_openai_boundary import http_fixture, no_external_network, request, wire, command, TEXT, STAMP


def response(w=None):
    return {'model': MODEL, 'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': json.dumps(w if w is not None else wire(), ensure_ascii=False),
        'reasoning_content': 'UNTRUSTED_SYNTHETIC_REASONING'}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}


def configure(fixture, w=None):
    fixture.data = json.dumps(response(w), ensure_ascii=False).encode()


def adapter(fixture, *, budget=None, approve=lambda check: check, purpose='capture_new', guard=None, timeout=1):
    return GLMCNAdapter(transport=GLMHTTP(fixture_port=fixture.port, timeout=timeout),
        budget=budget or GLMCallBudget(max_cny=Decimal('1.25')), purpose=purpose,
        authorize_send=approve, source_guard=guard)


@pytest.mark.parametrize('kind', ['event', 'fact', 'prospective', 'empty'])
def test_full_contract_types_and_host_binding(http_fixture, kind, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://example.invalid:9')
    w = wire(); p = w['proposals'][0]
    if kind == 'empty': w = {'proposals': []}
    elif kind != 'event':
        p['semantic_category'] = 'explicit_fact' if kind == 'fact' else 'commitment'
        body = {'proposition': '合成偏好', 'scope': '合成情况'} if kind == 'fact' else {'item': '合成约定', 'status': 'pending'}
        body.update({k: None for k in c._BODY_OPTIONAL[kind]})
        p['semantic_input'].update(card_type=kind, body=body)
    configure(http_fixture, w); checks = []
    def approve(check): checks.append(check); return check
    a = adapter(http_fixture, approve=approve)
    r = request(chunk_start=13); batch = a.extract(r)
    path, headers, payload = http_fixture.requests[0]
    check = checks[0]
    assert path == PATH and 'Authorization' not in headers
    assert payload == json.loads(check.payload) and payload['model'] == MODEL
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['max_tokens'] == 4096 and payload['reasoning_effort'] == 'low'
    assert payload['stream'] is False and 'tools' not in payload
    assert check.endpoint == 'https://' + HOST + PATH and check.currency == 'CNY'
    assert check.reservation_cny == RESERVATION_CNY and check.source_fingerprint == r.fingerprint
    assert check.purpose == 'capture_new' and len(check.payload_sha256) == 64
    assert TEXT not in repr(check) and r.source.source_id not in json.dumps(payload)
    assert batch.request_fingerprint == r.fingerprint and not a.production_ready
    assert all(v.valid for v in c.ProposalValidator.validate(r, batch))
    if kind != 'empty':
        p = batch.proposals[0]
        assert p.semantic_input.card_type == kind and p.semantic_input.valid_at == r.occurred_at
        assert p.source.source_id == r.source.source_id and p.source.span.start == 13
        assert p.source.span.end == 13 + len(TEXT) and p.source.span.digest == c.sha256_digest(TEXT)
        assert p.producer.producer_id == 'glm-cn-fixture' and p.producer.model_id == MODEL
    assert a._budget.snapshot()['entry']['status'] == 'validated'


@pytest.mark.parametrize('case', ['missing', 'boolean', 'payload', 'source', 'purpose', 'model', 'currency', 'cost', 'endpoint', 'raises', 'secret', 'guard', 'budget'])
def test_outbound_gate_rejects_before_http(http_fixture, case):
    def approve(check):
        if case == 'raises': raise RuntimeError('PRIVATE_SYNTHETIC_DETAIL')
        changes = {'payload': {'payload': b'{}'}, 'source': {'source_fingerprint': 'wrong'},
            'purpose': {'purpose': 'restore_saved'}, 'model': {'model': 'other'},
            'currency': {'currency': 'USD'}, 'cost': {'reservation_cny': Decimal('0.01')},
            'endpoint': {'endpoint': 'https://example.invalid'}}
        return True if case == 'boolean' else replace(check, **changes.get(case, {}))
    a = adapter(http_fixture, approve=None if case == 'missing' else approve,
        purpose='interpret_saved' if case == 'guard' else 'capture_new',
        budget=GLMCallBudget(max_cny=Decimal('0.01') if case == 'budget' else Decimal('1.25')))
    with pytest.raises(ProviderFailure) as caught:
        a.extract(request(sensitivity='secret' if case == 'secret' else 'ordinary'))
    assert 'PRIVATE' not in str(caught.value)
    assert http_fixture.requests == [] and a._budget.snapshot()['calls'] == 0


def test_sensitive_save_consent_is_not_outbound_consent(http_fixture):
    a = adapter(http_fixture, approve=None)
    session = LocalMemorySession(adapter=a, confirm=lambda check: True)
    before = snapshot(MemoryService(session.tenant_id))
    with pytest.raises(ProviderFailure, match='outbound_approval_required'):
        session.handle(command(sensitivity='sensitive'))
    assert http_fixture.requests == [] and snapshot(MemoryService(session.tenant_id)) == before
    configure(http_fixture)  # Model calls it ordinary; host sensitivity still wins.
    session = LocalMemorySession(adapter=adapter(http_fixture))
    with pytest.raises(AuthorizationRequired): session.handle(command(sensitivity='sensitive'))
    assert len(http_fixture.requests) == 1 and snapshot(MemoryService(session.tenant_id)) == before


@pytest.mark.parametrize('case', ['301', '302', '429', '503', 'content-type', 'oversize', 'bad-json',
    'provider-error', 'model', 'truncated', 'tools', 'refusal', 'inner-json', 'duplicate', 'unknown', 'missing',
    'enum', 'body-type', 'required-null', 'unknown-null', 'source-forgery', 'producer-forgery',
    'span', 'bool-span', 'confidence', 'count', 'usage-missing', 'usage-type', 'usage-total', 'usage-bool', 'usage-limit'])
def test_failure_retains_reservation_without_write_or_retry(http_fixture, case):
    res = response(); w = wire(); p = w['proposals'][0]
    if case.isdigit(): http_fixture.status = int(case)
    elif case == 'content-type': http_fixture.content_type = 'text/plain'
    elif case == 'provider-error': res['error'] = {'code': 'SYNTHETIC', 'message': 'PRIVATE_SYNTHETIC'}
    elif case == 'model': res['model'] = 'other'
    elif case == 'truncated': res['choices'][0]['finish_reason'] = 'length'
    elif case == 'tools': res['choices'][0]['message']['tool_calls'] = [{'name': 'delete'}]
    elif case == 'refusal': res['choices'][0]['message']['refusal'] = 'PRIVATE_SYNTHETIC'
    elif case == 'unknown': p['action'] = 'save'
    elif case == 'missing': p.pop('privacy_class')
    elif case == 'enum': p['speech_mode'] = 'made_up'
    elif case == 'body-type': p['semantic_input']['body']['goal_context'] = False
    elif case == 'required-null': p['semantic_input']['body']['goal_context'] = None
    elif case == 'unknown-null': p['semantic_input']['body']['tenant_id'] = None
    elif case == 'source-forgery': p['source'] = {'source_id': str(uuid4())}
    elif case == 'producer-forgery': p['producer'] = {'model_id': 'human'}
    elif case == 'span': p['span_end'] += 1
    elif case == 'bool-span': p['span_start'] = False
    elif case == 'confidence': p['confidence'] = float('nan')
    elif case == 'count': w['proposals'].append(deepcopy(p))
    elif case == 'usage-missing': res.pop('usage')
    elif case == 'usage-type': res['usage'] = []
    elif case == 'usage-total': res['usage']['total_tokens'] = 151
    elif case == 'usage-bool': res['usage']['prompt_tokens'] = True
    elif case == 'usage-limit': res['usage'] = {'prompt_tokens': 1048577, 'completion_tokens': 1, 'total_tokens': 1048578}
    res['choices'][0]['message']['content'] = json.dumps(w)
    if case == 'inner-json': res['choices'][0]['message']['content'] = '{PRIVATE_SYNTHETIC'
    if case == 'duplicate': res['choices'][0]['message']['content'] = '{"proposals":[],"proposals":[]}'
    http_fixture.data = json.dumps(res).encode()
    if case == 'oversize': http_fixture.data = b' ' * 64001
    if case == 'bad-json': http_fixture.data = b'{PRIVATE_SYNTHETIC'
    a = adapter(http_fixture); session = LocalMemorySession(adapter=a)
    before = snapshot(MemoryService(session.tenant_id))
    with pytest.raises(ProviderFailure) as caught: session.handle(command())
    if case == 'provider-error':
        assert str(caught.value) == 'provider_error'
        assert a._budget.snapshot()['entry']['status'] == 'provider_error'
    assert 'PRIVATE' not in str(caught.value)
    assert snapshot(MemoryService(session.tenant_id)) == before
    assert len(http_fixture.requests) == 1 and a._budget.snapshot()['reserved_cny'] == str(RESERVATION_CNY)
    with pytest.raises(ProviderFailure, match='budget_exhausted'): session.handle(command())
    assert len(http_fixture.requests) == 1


@pytest.mark.parametrize('mode', ['stall_headers', 'stall_body'])
def test_connected_deadline(http_fixture, mode):
    configure(http_fixture); http_fixture.mode = mode
    a = adapter(http_fixture, timeout=0.1); start = time.monotonic()
    with pytest.raises(ProviderFailure, match='timeout'): a.extract(request())
    assert time.monotonic() - start < 0.8 and len(http_fixture.requests) == 1
    assert a._budget.snapshot()['calls'] == 1


def test_shared_budget_and_adapter_are_single_attempt(http_fixture):
    configure(http_fixture); http_fixture.mode = 'stall_headers'
    budget = GLMCallBudget(max_cny=Decimal('1.25')); a = adapter(http_fixture, budget=budget)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(a.extract, request()); assert http_fixture.received.wait(1)
        with pytest.raises(ProviderFailure, match='concurrent_call_rejected'): a.extract(request())
        with pytest.raises(ProviderFailure, match='budget_exhausted'): adapter(http_fixture, budget=budget).extract(request())
        http_fixture.release.set(); pending.result(timeout=2)
    assert len(http_fixture.requests) == 1
    with pytest.raises(ProviderFailure): budget.finish('validated')
    for value in (Decimal('NaN'), Decimal('1.26'), 1.25):
        with pytest.raises(ValueError): GLMCallBudget(max_cny=value)


def test_transport_is_explicit_and_fixed(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY', 'IGNORED_SYNTHETIC')
    with pytest.raises(ValueError): GLMHTTP()
    with pytest.raises(ValueError): GLMHTTP(api_key='SYNTHETIC', fixture_port=1234)
    seen = []
    def reject_connection(host, **kwargs): seen.append(host); raise OSError('PRIVATE_SYNTHETIC')
    monkeypatch.setattr('http.client.HTTPSConnection', reject_connection)
    # Only construction is probed; no socket or credential is sent.
    with pytest.raises(ProviderFailure, match='transport_error'): GLMHTTP(api_key='SYNTHETIC_NOT_A_CREDENTIAL').post(b'{}')
    assert seen == [HOST]


@pytest.mark.parametrize('field,value,reason', [
    ('speech_mode', 'example', 'non_authoritative_speech_mode'),
    ('speech_mode', 'quotation', 'non_authoritative_speech_mode'),
    ('speaker', 'third_party', 'untrusted_subject_or_speaker'),
    ('scope', {'persistence': 'turn_only', 'applies_to': 'synthetic'}, 'turn_only'),
    ('epistemic_state', 'inferred', 'not_asserted'),
    ('privacy_class', 'never_store', 'never_store'),
    ('revision_intent', 'revise', 'revision_required'),
    ('revision_intent', 'forget', 'forget_required'),
])
def test_existing_gate_still_controls_persistence(http_fixture, field, value, reason):
    w = wire(); w['proposals'][0][field] = value
    if value == 'forget': w['proposals'][0]['semantic_input'] = None
    configure(http_fixture, w); session = LocalMemorySession(adapter=adapter(http_fixture))
    before = snapshot(MemoryService(session.tenant_id))
    assert session.handle(command())['outcomes'][0]['reason'] == reason
    assert snapshot(MemoryService(session.tenant_id)) == before


def seed_saved():
    session = LocalMemorySession(); receipt = session.handle(command())['outcomes'][0]
    service = MemoryService(session.tenant_id)
    prepared = service.read_evidence_interpretation(receipt['evidence_id'])
    return service, prepared, receipt


def source_guard(service, prepared):
    def check(r):
        return service.read_evidence_interpretation(prepared.evidence_id) == prepared and saved_request(prepared) == r
    return check


def invalidate(service, receipt, kind):
    if kind == 'deleted': service.delete_evidence(receipt['evidence_id'])
    elif kind == 'revoked': service.delete_card(receipt['card_ids'][0])
    else:
        with tenant_transaction(service.tenant_id) as conn:
            conn.execute('UPDATE evidence SET version=version+1 WHERE tenant_id=%s AND id=%s',
                (service.tenant_id, receipt['evidence_id']))


@pytest.mark.parametrize('kind', ['deleted', 'revoked', 'version', 'foreign'])
def test_saved_source_preflight_rejects_without_http(http_fixture, kind):
    service, prepared, receipt = seed_saved()
    if kind == 'foreign': service = MemoryService(uuid4()); service.ensure_tenant()
    else: invalidate(service, receipt, kind)
    a = adapter(http_fixture, purpose='interpret_saved', guard=source_guard(service, prepared))
    with pytest.raises(ProviderFailure, match='source_check'): a.extract(saved_request(prepared))
    assert http_fixture.requests == [] and a._budget.snapshot()['calls'] == 0


@pytest.mark.parametrize('kind', ['deleted', 'revoked', 'version'])
def test_deletion_during_http_is_not_blocked_or_persisted(http_fixture, kind):
    service, prepared, receipt = seed_saved(); configure(http_fixture)
    http_fixture.mode = 'stall_headers'
    a = adapter(http_fixture, purpose='interpret_saved', guard=source_guard(service, prepared), timeout=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(a.extract, saved_request(prepared)); assert http_fixture.received.wait(1)
        # A write must complete while network I/O is still waiting, not after it.
        mutation = pool.submit(invalidate, service, receipt, kind); mutation.result(timeout=0.8)
        assert not pending.done()
        before = snapshot(service); http_fixture.release.set()
        with pytest.raises(ProviderFailure, match='source_check'): pending.result(timeout=2)
    assert snapshot(service) == before and len(http_fixture.requests) == 1
    assert a._budget.snapshot()['calls'] == 1


@pytest.mark.parametrize('kind', ['valid', 'deleted', 'revoked', 'version'])
def test_core_commit_rechecks_after_adapter_returns(http_fixture, kind):
    service, prepared, receipt = seed_saved(); configure(http_fixture)
    a = adapter(http_fixture, purpose='interpret_saved', guard=source_guard(service, prepared))
    decisions = validate_and_gate(saved_request(prepared), a)
    semantics = [replace(d.proposal.semantic_input.to_semantic_input(), canonical_key=None) for d in decisions]
    if kind != 'valid': invalidate(service, receipt, kind)
    before = snapshot(service)
    def commit():
        return service.commit_evidence_interpretation(prepared, intent_id=uuid4(), semantics=semantics,
            provenance=decisions[0].proposal.producer.to_dict())
    if kind == 'valid': assert commit()['created']
    else:
        with pytest.raises((DeletedSource, InterpretationRevoked, VersionConflict)): commit()
        assert snapshot(service) == before


def test_successful_capture_and_secret_host_gate(http_fixture):
    configure(http_fixture); a = adapter(http_fixture); session = LocalMemorySession(adapter=a)
    assert session.handle(command(sensitivity='secret'))['outcomes'][0]['reason'] == 'secret_source'
    assert http_fixture.requests == [] and a._budget.snapshot()['calls'] == 0
    assert session.handle(command())['outcomes'][0]['status'] == 'stored'
