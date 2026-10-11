"""Content-free diagnostics for the fixed five-field GLM synthetic trial.

Pure response inspection only: no transport, credentials, storage or retry.
This does not validate the product's full ProposalBatch contract.
"""
from __future__ import annotations

from datetime import date
import json
import re


MODEL = 'glm-5.3-flash'
MAX_RESPONSE_BYTES = 65536
SYNTHETIC_SOURCE = '合成试验：我原定2026年10月10日和朋友爬山，现已改到10月11日，正在等朋友确认。'
_EXPECTED = {
    'event_kind': 'plan_change',
    'original_date': '2026-10-10',
    'updated_date': '2026-10-11',
    'confirmation_status': 'pending',
    'source_quote': SYNTHETIC_SOURCE,
}
_ENUMS = {'event_kind': ('plan_change',), 'confirmation_status': ('pending', 'confirmed')}


class _DuplicateKey(Exception):
    pass


def _strict_json(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _DuplicateKey()
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError()

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


def _report(stage: str, code: str, **details) -> dict:
    return {
        'diagnostic_version': 1,
        'status': 'validated_fixed_fields' if code == 'ok' else 'failed',
        'stage': stage,
        'code': code,
        'retry_authorized': False,
        'raw_response_retained': False,
        **details,
    }


def diagnose_transport_failure(*, timed_out: bool) -> dict:
    """Caller classifies its exception; never accept exception text here."""
    if type(timed_out) is not bool:
        return _report('input', 'invalid_diagnostic_input')
    return _report('transport', 'timeout' if timed_out else 'transport_error')


def _usage(response: dict) -> dict:
    usage = response.get('usage')
    if type(usage) is not dict:
        return {'code': 'usage_missing'}
    names = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    counts = [usage.get(name) for name in names]
    if any(type(value) is not int or value <= 0 for value in counts):
        return {'code': 'invalid_usage'}
    if counts[2] != counts[0] + counts[1]:
        return {'code': 'invalid_usage'}
    if counts[0] > 1048576 or counts[1] > 131072:
        return {'code': 'usage_exceeds_capacity_estimate'}
    return {'code': 'ok', 'counts': dict(zip(names, counts))}


def _field_schema(fields: dict) -> dict:
    results = {}
    for name in _EXPECTED:
        if name not in fields:
            code = 'missing'
        elif type(fields[name]) is not str:
            code = 'wrong_type'
        elif name in _ENUMS and fields[name] not in _ENUMS[name]:
            code = 'invalid_enum'
        elif name in ('original_date', 'updated_date'):
            value = fields[name]
            code = 'schema_valid'
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
                code = 'invalid_date_format'
            else:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    code = 'invalid_calendar_date'
        else:
            code = 'schema_valid'
        results[name] = code
    return results


def diagnose_response(*, http_status: int, content_type: str, body: bytes) -> dict:
    """Return only static codes, fixed field names and bounded usage counts.

    HTTP/provider/protocol/JSON/schema failures precede semantic comparison.
    Usage is independent; it never hides a field or semantic failure.
    """
    if (type(http_status) is not int or not 100 <= http_status <= 599
            or type(content_type) is not str or type(body) is not bytes):
        return _report('input', 'invalid_diagnostic_input')
    if http_status != 200:
        code = ('authentication_or_permission' if http_status in (401, 403)
                else 'rate_limited' if http_status == 429
                else 'server_error' if http_status >= 500 else 'http_error')
        return _report('http', code, http_status=http_status)
    if len(body) > MAX_RESPONSE_BYTES:
        return _report('http', 'response_too_large')
    if content_type.split(';', 1)[0].strip().lower() != 'application/json':
        return _report('http', 'invalid_content_type')
    try:
        raw = body.decode('utf-8')
    except UnicodeError:
        return _report('outer_json', 'invalid_utf8')
    try:
        response = _strict_json(raw)
    except _DuplicateKey:
        return _report('outer_json', 'duplicate_json_key')
    except (ValueError, RecursionError):
        return _report('outer_json', 'invalid_json')
    if type(response) is not dict:
        return _report('protocol', 'invalid_response')
    if response.get('error') is not None:
        return _report('provider', 'provider_error')
    if response.get('model') != MODEL:
        return _report('protocol', 'model_mismatch')
    choices = response.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        return _report('protocol', 'invalid_choices')
    choice = choices[0]
    if choice.get('finish_reason') != 'stop':
        return _report('protocol', 'incomplete_response')
    message = choice.get('message')
    if type(message) is not dict or message.get('role') != 'assistant':
        return _report('protocol', 'invalid_message')
    if message.get('tool_calls') or message.get('function_call') or message.get('refusal'):
        return _report('protocol', 'unexpected_action_or_refusal')
    if type(message.get('content')) is not str:
        return _report('protocol', 'invalid_content')
    usage = _usage(response)
    try:
        fields = _strict_json(message['content'])
    except _DuplicateKey:
        return _report('inner_json', 'duplicate_json_key', usage=usage)
    except (ValueError, RecursionError):
        return _report('inner_json', 'invalid_json', usage=usage)
    if type(fields) is not dict:
        return _report('schema', 'fields_not_object', usage=usage)
    field_results = _field_schema(fields)
    extra_count = sum(name not in _EXPECTED for name in fields)
    details = {'field_results': field_results, 'extra_field_count': extra_count, 'usage': usage}
    if extra_count or any(code != 'schema_valid' for code in field_results.values()):
        return _report('schema', 'invalid_fixed_schema', **details)
    details['field_results'] = {
        name: 'match' if fields[name] == expected else 'value_mismatch'
        for name, expected in _EXPECTED.items()
    }
    if 'value_mismatch' in details['field_results'].values():
        return _report('semantic', 'synthetic_extraction_mismatch', **details)
    if usage['code'] != 'ok':
        return _report('usage', usage['code'], **details)
    return _report('complete', 'ok', **details)
