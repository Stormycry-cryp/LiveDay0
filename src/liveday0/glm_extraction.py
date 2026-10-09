"""Explicit domestic GLM boundary. Offline fixtures only; default CLI unchanged."""
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import http.client
import math
import socket
from threading import Event, Lock, Timer

from liveday0 import extraction_contract as c
from liveday0.extraction_wire import INSTRUCTIONS, output_schema, strict_json, bind
from liveday0.provider_budget import ProviderFailure
from liveday0.serialization import canonical_json


MODEL = 'glm-5.3-flash'
HOST = 'open.bigmodel.cn'
PATH = '/api/paas/v4/chat/completions'
MAX_INPUT = 1_048_576
MAX_OUTPUT = 131_072
REQUEST_OUTPUT = 4096
# Official domestic list prices checked 2026-10-09; no cache discount assumed.
RESERVATION_CNY = (MAX_INPUT * Decimal('0.8') + MAX_OUTPUT * Decimal('2.8')) / 1_000_000


class GLMCallBudget:
    """One process-local, non-refundable attempt. Not a provider billing cap."""
    def __init__(self, *, max_cny):
        if (not isinstance(max_cny, Decimal) or not max_cny.is_finite()
                or not 0 < max_cny <= Decimal('1.25')):
            raise ValueError('explicit CNY Decimal budget must be in (0, 1.25]')
        self._limit, self._entry, self._lock = max_cny, None, Lock()

    def reserve(self):
        with self._lock:
            if self._entry is not None or self._limit < RESERVATION_CNY:
                raise ProviderFailure('budget_exhausted')
            self._entry = {'status': 'reserved', 'input_tokens': None, 'output_tokens': None}

    def finish(self, status, usage=None):
        with self._lock:
            if self._entry is None or self._entry['status'] != 'reserved':
                raise ProviderFailure('reservation_already_finished')
            self._entry['status'] = status
            if usage is not None:
                counts = [usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')] if type(usage) is dict else []
                if (len(counts) != 3 or any(type(n) is not int or n <= 0 for n in counts)
                        or counts[0] > MAX_INPUT or counts[1] > MAX_OUTPUT
                        or counts[2] != counts[0] + counts[1]):
                    self._entry['status'] = 'usage_outside_preflight'
                    raise ProviderFailure('usage_outside_preflight')
                self._entry.update(input_tokens=counts[0], output_tokens=counts[1])

    def snapshot(self):
        with self._lock:
            return {'currency': 'CNY', 'calls': int(self._entry is not None),
                'reserved_cny': str(RESERVATION_CNY if self._entry is not None else Decimal(0)),
                'entry': deepcopy(self._entry)}


@dataclass(frozen=True)
class SendApproval:
    """Ephemeral trusted-host check; never log/repr the complete source payload.

    The authorizer must return this exact value, not a model-authored flag or a
    boolean. Approval is for outbound processing only, not persistence/recovery.
    """
    payload: bytes = field(repr=False)
    source_fingerprint: str
    purpose: str
    model: str = MODEL
    endpoint: str = 'https://' + HOST + PATH
    currency: str = 'CNY'
    reservation_cny: Decimal = RESERVATION_CNY

    @property
    def payload_sha256(self):
        return hashlib.sha256(self.payload).hexdigest()


class GLMHTTP:
    """Fixed HTTPS or explicit credential-free loopback; no redirects/proxies.

    The deadline bounds connected I/O. OS DNS lookup may take longer. No retry
    occurs after connect, timeout, malformed output, or an unknown result.
    """
    def __init__(self, *, api_key=None, fixture_port=None, timeout=15.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be in (0, 30] seconds')
        if fixture_port is not None:
            if type(fixture_port) is not int or not 1 <= fixture_port <= 65535 or api_key is not None:
                raise ValueError('loopback requires a port and no credential')
        elif (type(api_key) is not str or not 1 <= len(api_key) <= 512
                or any(ord(x) < 33 or ord(x) > 126 for x in api_key)):
            raise ValueError('an explicit API credential is required')
        self.fixture = fixture_port is not None
        self._port, self._key, self._timeout = fixture_port, api_key, timeout

    def post(self, body):
        conn = None
        expired, timer = Event(), None
        try:
            conn = (http.client.HTTPConnection('127.0.0.1', self._port, timeout=self._timeout)
                    if self.fixture else http.client.HTTPSConnection(HOST, timeout=self._timeout))
            conn.connect()
            connected_socket = conn.sock
            def abort():
                expired.set()
                try:
                    connected_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = Timer(self._timeout, abort)
            timer.daemon = True
            timer.start()
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
            if not self.fixture:
                headers['Authorization'] = 'Bearer ' + self._key
            conn.request('POST', PATH, body, headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ProviderFailure('http_error')
            if response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ProviderFailure('invalid_content_type')
            raw = response.read(64_001)
            if expired.is_set():
                raise ProviderFailure('timeout')
            if len(raw) > 64_000:
                raise ProviderFailure('response_too_large')
            return strict_json(raw)
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderFailure('timeout' if expired.is_set() or isinstance(exc, TimeoutError) else 'transport_error') from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if conn is not None:
                conn.close()


class GLMCNAdapter:
    production_ready = False

    def __init__(self, *, transport: GLMHTTP, budget: GLMCallBudget, purpose,
                 authorize_send=None, source_guard=None):
        if purpose not in {'capture_new', 'interpret_saved', 'restore_saved'}:
            raise ValueError('explicit host purpose required')
        self._transport, self._budget, self._purpose = transport, budget, purpose
        self._authorize, self._guard = authorize_send, source_guard
        self._call_lock = Lock()

    def _check_source(self, request):
        # Saved-source callbacks re-read the same tenant/version/epoch/status in
        # a short transaction, compare with the prepared source, then close it.
        if self._guard is None and self._purpose == 'capture_new':
            return
        try:
            valid = self._guard is not None and self._guard(deepcopy(request)) is True
        except Exception:
            raise ProviderFailure('source_check_failed') from None
        if not valid:
            raise ProviderFailure('source_check_required_or_stale')

    def extract(self, request):
        request = deepcopy(request)
        if request.source.sensitivity == 'secret':
            raise ProviderFailure('secret_source')
        if not self._call_lock.acquire(blocking=False):
            raise ProviderFailure('concurrent_call_rejected')
        try:
            return self._extract(request)
        finally:
            self._call_lock.release()

    def _extract(self, request):
        payload = {'model': MODEL, 'stream': False, 'max_tokens': REQUEST_OUTPUT,
            'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low',
            'response_format': {'type': 'json_object'}, 'messages': [
                {'role': 'system', 'content': INSTRUCTIONS + '\nJSON Schema:\n' + canonical_json(output_schema())},
                {'role': 'user', 'content': canonical_json({'content': request.content,
                    'occurred_at': request.occurred_at, 'source_kind': request.source.source_kind,
                    'authority': request.source.authority, 'sensitivity': request.source.sensitivity})}]}
        body = canonical_json(payload).encode('utf-8')
        if len(body) > 80_000:
            raise ProviderFailure('request_too_large')
        check = SendApproval(body, request.fingerprint, self._purpose)
        try:
            approved = self._authorize(check) if self._authorize is not None else None
        except Exception:
            raise ProviderFailure('outbound_approval_failed') from None
        if type(approved) is not SendApproval or approved != check:
            raise ProviderFailure('outbound_approval_required')
        self._check_source(request)
        self._budget.reserve()
        status, usage = 'invalid_response', None
        try:
            response = self._transport.post(body)
            if type(response) is not dict:
                raise ProviderFailure('invalid_response')
            usage = response.get('usage')
            if response.get('error') is not None:
                raise ProviderFailure('provider_error')
            if response.get('model') != MODEL:
                raise ProviderFailure('model_mismatch')
            choices = response.get('choices')
            if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                raise ProviderFailure('unexpected_output')
            choice = choices[0]
            if choice.get('finish_reason') != 'stop':
                raise ProviderFailure('incomplete_or_failed')
            message = choice.get('message')
            if (type(message) is not dict or message.get('role') != 'assistant'
                    or message.get('tool_calls') or message.get('function_call') or message.get('refusal')
                    or type(message.get('content')) is not str):
                raise ProviderFailure('unexpected_output')
            if usage is None:
                raise ProviderFailure('usage_missing')
            producer = c.ProducerMetadata('glm-cn-fixture' if self._transport.fixture else 'glm-cn',
                MODEL, '1.0.0', '1.0.0')
            batch = bind(request, strict_json(message['content']), producer)
            batch = c.ProposalBatch.from_dict(batch.to_dict())
            self._check_source(request)
            status = 'validated'
            return batch
        except c.ContractViolation:
            status = 'invalid_contract'
            raise ProviderFailure(status) from None
        except ProviderFailure as exc:
            status = str(exc)
            raise
        finally:
            self._budget.finish(status, usage)
