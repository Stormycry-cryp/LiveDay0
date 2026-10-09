"""One explicit Responses adapter; no SDK, environment lookup, retries or tools.

Only local HTTP fixtures have been validated. Remote use additionally needs a
trusted outbound approval and a verified token preflight, neither supplied by
the default CLI. Source integrity is deterministic; semantic grounding is not.
"""
from copy import deepcopy
import http.client
import math
import socket
from threading import Event, Lock, Timer

from liveday0 import extraction_contract as c
from liveday0.extraction_wire import INSTRUCTIONS, output_schema, strict_json as _strict_json, bind
from liveday0.provider_budget import CallBudget, MODEL, MAX_INPUT, MAX_OUTPUT, ProviderFailure
from liveday0.serialization import canonical_json


def _bind(request, wire, fixture):
    # Preserve the existing private entry point and OpenAI producer/error contract.
    return bind(request, wire, c.ProducerMetadata(
        'openai-responses-fixture' if fixture else 'openai-responses',
        MODEL, '1.0.0', '1.0.0'))


class ResponsesHTTP:
    """Fixed HTTPS destination, or explicit credential-free IPv4 loopback fixture.

    timeout bounds connected I/O, including slow headers/body. OS DNS resolution
    may take longer; no HTTP payload is sent until connect completes. No proxy
    environment variables or redirect handling are used by http.client.
    """
    def __init__(self, *, api_key=None, fixture_port=None, timeout=15.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be in (0, 30] seconds')
        if fixture_port is not None:
            if type(fixture_port) is not int or not 1 <= fixture_port <= 65535 or api_key is not None:
                raise ValueError('loopback requires a port and no credential')
        elif not isinstance(api_key, str) or not api_key or any(ord(x) < 33 or ord(x) > 126 for x in api_key):
            raise ValueError('an explicit API credential is required')
        self.fixture = fixture_port is not None
        self._port, self._key, self._timeout = fixture_port, api_key, timeout

    def post(self, body):
        conn = (http.client.HTTPConnection('127.0.0.1', self._port, timeout=self._timeout)
                if self.fixture else http.client.HTTPSConnection('api.openai.com', timeout=self._timeout))
        expired = Event()
        timer = None
        try:
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
            conn.request('POST', '/v1/responses', body, headers)
            response = conn.getresponse()
            if response.status != 200:
                raise ProviderFailure('http_error')
            if response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ProviderFailure('invalid_content_type')
            data = response.read(64_001)
            if expired.is_set():
                raise ProviderFailure('timeout')
            if len(data) > 64_000:
                raise ProviderFailure('response_too_large')
            return _strict_json(data)
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderFailure('timeout' if expired.is_set() or isinstance(exc, TimeoutError) else 'transport_error') from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            conn.close()


class OpenAIResponsesAdapter:
    production_ready = False

    def __init__(self, *, transport: ResponsesHTTP, budget: CallBudget,
                 token_counter=None, authorize_send=None):
        # The counter is a trusted host dependency: (MODEL, full_payload) -> int.
        # It must cover instructions, schema and protocol framing, not just text.
        # No verified remote counter is bundled; absent preflight means no send.
        self._transport, self._budget = transport, budget
        self._counter, self._authorize = token_counter, authorize_send
        self._call_lock = Lock()

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
        if not self._transport.fixture:
            try:
                approved = self._authorize is not None and self._authorize(deepcopy(request)) is True
            except Exception:
                raise ProviderFailure('outbound_approval_failed') from None
            if not approved:
                raise ProviderFailure('outbound_approval_required')
        payload = {'model': MODEL, 'store': False, 'stream': False, 'max_output_tokens': MAX_OUTPUT,
            'instructions': INSTRUCTIONS,
            'input': [{'role': 'user', 'content': canonical_json({'content': request.content,
                'occurred_at': request.occurred_at, 'source_kind': request.source.source_kind,
                'authority': request.source.authority, 'sensitivity': request.source.sensitivity})}],
            'text': {'format': {'type': 'json_schema', 'name': 'life_memory_proposal',
                'strict': True, 'schema': output_schema()}}}
        body = canonical_json(payload).encode('utf-8')
        if len(body) > 80_000:
            raise ProviderFailure('request_too_large')
        if self._counter is None:
            raise ProviderFailure('token_preflight_unavailable')
        try:
            tokens = self._counter(MODEL, deepcopy(payload))
        except Exception:
            raise ProviderFailure('token_preflight_failed') from None
        if type(tokens) is not int or not 0 < tokens <= MAX_INPUT:
            raise ProviderFailure('input_token_limit')
        reservation = self._budget.reserve()
        status, usage = 'invalid_response', None
        try:
            response = self._transport.post(body)
            if type(response) is not dict:
                raise ProviderFailure('invalid_response')
            usage = response.get('usage')
            if response.get('status') != 'completed' or response.get('error') is not None or response.get('incomplete_details') is not None:
                raise ProviderFailure('incomplete_or_failed')
            if response.get('model') != MODEL:
                raise ProviderFailure('model_mismatch')
            output = response.get('output')
            if type(output) is not list or len(output) != 1:
                raise ProviderFailure('unexpected_output')
            message = output[0]
            if type(message) is not dict or message.get('type') != 'message' or message.get('role') != 'assistant' or message.get('status') != 'completed':
                raise ProviderFailure('unexpected_output')
            content = message.get('content')
            if type(content) is not list or len(content) != 1 or type(content[0]) is not dict:
                raise ProviderFailure('unexpected_output')
            part = content[0]
            if part.get('type') == 'refusal':
                raise ProviderFailure('model_refusal')
            if part.get('type') != 'output_text' or type(part.get('text')) is not str:
                raise ProviderFailure('unexpected_output')
            if usage is None:
                raise ProviderFailure('usage_missing')
            batch = _bind(request, _strict_json(part['text']), self._transport.fixture)
            status = 'validated'
            return batch
        except c.ContractViolation:
            status = 'invalid_contract'
            raise ProviderFailure(status) from None
        except ProviderFailure as exc:
            status = str(exc)
            raise
        finally:
            # Every reserved attempt, including timeout/refusal/malformed output,
            # retains its full reservation. No automatic repair or retry.
            self._budget.finish(reservation, status, usage)
