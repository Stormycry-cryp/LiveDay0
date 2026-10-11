"""Offline unit tests; runnable with unittest without the PostgreSQL fixtures."""
from copy import deepcopy
import json
import unittest

from liveday0.synthetic_trial_diagnostics import (
    MAX_RESPONSE_BYTES, SYNTHETIC_SOURCE, diagnose_response, diagnose_transport_failure,
)


def fixed_fields():
    return {
        'event_kind': 'plan_change', 'original_date': '2026-10-10',
        'updated_date': '2026-10-11', 'confirmation_status': 'pending',
        'source_quote': SYNTHETIC_SOURCE,
    }


def envelope(fields=None):
    return {
        'model': 'glm-5.3-flash',
        'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(fixed_fields() if fields is None else fields),
        }}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    }


def diagnose(response):
    return diagnose_response(http_status=200, content_type='application/json; charset=utf-8',
                             body=json.dumps(response).encode())


class SyntheticDiagnosticsTests(unittest.TestCase):
    def assert_failure(self, report, stage, code):
        self.assertEqual(report['status'], 'failed')
        self.assertEqual((report['stage'], report['code']), (stage, code))
        self.assertIs(report['retry_authorized'], False)
        self.assertIs(report['raw_response_retained'], False)

    def test_fixed_fields_pass_without_returning_values(self):
        report = diagnose(envelope())
        self.assertEqual(report['status'], 'validated_fixed_fields')
        self.assertEqual(report['code'], 'ok')
        self.assertEqual(report['field_results'], dict.fromkeys(fixed_fields(), 'match'))
        self.assertEqual(report['extra_field_count'], 0)
        self.assertEqual(report['usage']['counts']['total_tokens'], 150)
        self.assertNotIn(SYNTHETIC_SOURCE, json.dumps(report, ensure_ascii=False))
        self.assertNotIn('2026-10-10', json.dumps(report))

    def test_each_required_field_missing(self):
        for name in fixed_fields():
            with self.subTest(name=name):
                fields = fixed_fields()
                del fields[name]
                report = diagnose(envelope(fields))
                self.assert_failure(report, 'schema', 'invalid_fixed_schema')
                self.assertEqual(report['field_results'][name], 'missing')
                self.assertEqual(set(report['field_results'].values()), {'missing', 'schema_valid'})

    def test_each_field_rejects_non_strings(self):
        for name in fixed_fields():
            for value in (None, True, 3, [], {}):
                with self.subTest(name=name, value=value):
                    fields = fixed_fields()
                    fields[name] = value
                    report = diagnose(envelope(fields))
                    self.assert_failure(report, 'schema', 'invalid_fixed_schema')
                    self.assertEqual(report['field_results'][name], 'wrong_type')

    def test_valid_semantic_differences_identify_only_the_field(self):
        replacements = {'original_date': '2026-10-09', 'updated_date': '2026-10-12',
                        'confirmation_status': 'confirmed', 'source_quote': 'PRIVATE_SENTINEL'}
        for name, value in replacements.items():
            with self.subTest(name=name):
                fields = fixed_fields()
                fields[name] = value
                report = diagnose(envelope(fields))
                self.assert_failure(report, 'semantic', 'synthetic_extraction_mismatch')
                self.assertEqual(report['field_results'][name], 'value_mismatch')
                self.assertEqual(list(report['field_results'].values()).count('value_mismatch'), 1)
                self.assertNotIn(value, json.dumps(report))

    def test_enum_and_date_errors_are_schema_errors(self):
        cases = [('event_kind', 'completed_trip', 'invalid_enum'),
                 ('confirmation_status', 'unknown', 'invalid_enum'),
                 ('original_date', '20261010', 'invalid_date_format'),
                 ('updated_date', '2026-02-30', 'invalid_calendar_date'),
                 ('updated_date', '２０２６-１０-１１', 'invalid_date_format')]
        for name, value, code in cases:
            with self.subTest(name=name, value=value):
                fields = fixed_fields()
                fields[name] = value
                report = diagnose(envelope(fields))
                self.assert_failure(report, 'schema', 'invalid_fixed_schema')
                self.assertEqual(report['field_results'][name], code)

    def test_schema_error_prevents_semantic_comparison(self):
        fields = fixed_fields()
        fields['original_date'] = None
        fields['source_quote'] = 'PRIVATE_SENTINEL'
        report = diagnose(envelope(fields))
        self.assertEqual(report['field_results']['source_quote'], 'schema_valid')
        self.assertNotIn('value_mismatch', report['field_results'].values())

    def test_unknown_fields_are_counted_without_names_or_values(self):
        fields = fixed_fields()
        fields['PRIVATE_KEY_SENTINEL'] = {'nested': 'PRIVATE_VALUE_SENTINEL'}
        fields['ANOTHER_PRIVATE_KEY'] = 'SECOND_PRIVATE_VALUE'
        report = diagnose(envelope(fields))
        self.assert_failure(report, 'schema', 'invalid_fixed_schema')
        self.assertEqual(report['extra_field_count'], 2)
        serialized = json.dumps(report)
        for sentinel in ('PRIVATE_KEY_SENTINEL', 'PRIVATE_VALUE_SENTINEL',
                         'ANOTHER_PRIVATE_KEY', 'SECOND_PRIVATE_VALUE'):
            self.assertNotIn(sentinel, serialized)

    def test_http_errors_precede_body_parsing(self):
        for status, code in ((401, 'authentication_or_permission'),
                             (403, 'authentication_or_permission'), (429, 'rate_limited'),
                             (500, 'server_error'), (503, 'server_error'), (302, 'http_error')):
            with self.subTest(status=status):
                report = diagnose_response(http_status=status, content_type='text/plain',
                                           body=b'PRIVATE_SENTINEL')
                self.assert_failure(report, 'http', code)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))

    def test_response_byte_bound_and_content_type(self):
        report = diagnose_response(http_status=200, content_type='application/json',
                                   body=b' ' * (MAX_RESPONSE_BYTES + 1))
        self.assert_failure(report, 'http', 'response_too_large')
        report = diagnose_response(http_status=200, content_type='text/html', body=b'PRIVATE_SENTINEL')
        self.assert_failure(report, 'http', 'invalid_content_type')

    def test_outer_json_and_utf8_errors(self):
        for body, code in ((b'\xff', 'invalid_utf8'), (b'PRIVATE_SENTINEL', 'invalid_json'),
                           (b'{"a":1,"a":2}', 'duplicate_json_key'),
                           (b'{"a":NaN}', 'invalid_json'),
                           (b'[' * 2000, 'invalid_json')):
            with self.subTest(code=code):
                report = diagnose_response(http_status=200, content_type='application/json', body=body)
                self.assert_failure(report, 'outer_json', code)

    def test_non_object_outer_response(self):
        for value in ([], None, 3, 'PRIVATE_SENTINEL'):
            with self.subTest(value=value):
                self.assert_failure(diagnose(value), 'protocol', 'invalid_response')

    def test_http_200_provider_error_overrides_apparently_valid_choices(self):
        for value in ({'message': 'PRIVATE_SENTINEL'}, 'PRIVATE_SENTINEL', False, 0, []):
            with self.subTest(value=value):
                response = envelope()
                response['error'] = value
                report = diagnose(response)
                self.assert_failure(report, 'provider', 'provider_error')
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))
        response = envelope()
        response['error'] = None
        self.assertEqual(diagnose(response)['code'], 'ok')

    def test_model_choices_finish_and_message_errors(self):
        cases = []
        response = envelope(); response['model'] = 'PRIVATE_SENTINEL'
        cases.append((response, 'model_mismatch'))
        for value in (None, [], [None], [{}, {}]):
            response = envelope(); response['choices'] = value
            cases.append((response, 'invalid_choices'))
        response = envelope(); response['choices'][0]['finish_reason'] = 'length'
        cases.append((response, 'incomplete_response'))
        for value in (None, {'role': 'user'}):
            response = envelope(); response['choices'][0]['message'] = value
            cases.append((response, 'invalid_message'))
        for response, code in cases:
            with self.subTest(code=code):
                self.assert_failure(diagnose(response), 'protocol', code)

    def test_actions_refusals_and_non_string_content(self):
        for name in ('tool_calls', 'function_call', 'refusal'):
            response = envelope()
            response['choices'][0]['message'][name] = 'PRIVATE_SENTINEL'
            report = diagnose(response)
            self.assert_failure(report, 'protocol', 'unexpected_action_or_refusal')
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))
        response = envelope(); response['choices'][0]['message']['content'] = None
        self.assert_failure(diagnose(response), 'protocol', 'invalid_content')

    def test_inner_json_has_separate_error_stage(self):
        for content, code in (('PRIVATE_SENTINEL', 'invalid_json'),
                              ('{"source_quote":"a","source_quote":"b"}', 'duplicate_json_key'),
                              ('{"original_date":Infinity}', 'invalid_json')):
            with self.subTest(code=code):
                response = envelope(); response['choices'][0]['message']['content'] = content
                report = diagnose(response)
                self.assert_failure(report, 'inner_json', code)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))

    def test_inner_non_object_is_schema_failure(self):
        for value in ([], None, 'PRIVATE_SENTINEL', 7):
            response = envelope()
            response['choices'][0]['message']['content'] = json.dumps(value)
            self.assert_failure(diagnose(response), 'schema', 'fields_not_object')

    def test_usage_validation_is_independent_of_extraction_failure(self):
        fields = fixed_fields(); fields['confirmation_status'] = 'confirmed'
        response = envelope(fields); del response['usage']
        report = diagnose(response)
        self.assert_failure(report, 'semantic', 'synthetic_extraction_mismatch')
        self.assertEqual(report['usage'], {'code': 'usage_missing'})
        response = envelope(fields)
        response['usage']['prompt_tokens'] = 'PRIVATE_SENTINEL'
        report = diagnose(response)
        self.assert_failure(report, 'semantic', 'synthetic_extraction_mismatch')
        self.assertEqual(report['usage'], {'code': 'invalid_usage'})
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))

    def test_valid_fields_still_require_valid_usage(self):
        cases = [(None, 'usage_missing'),
                 ({'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2}, 'invalid_usage'),
                 ({'prompt_tokens': 0, 'completion_tokens': 1, 'total_tokens': 1}, 'invalid_usage'),
                 ({'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 3}, 'invalid_usage'),
                 ({'prompt_tokens': 1048577, 'completion_tokens': 1, 'total_tokens': 1048578},
                  'usage_exceeds_capacity_estimate'),
                 ({'prompt_tokens': 1, 'completion_tokens': 131073, 'total_tokens': 131074},
                  'usage_exceeds_capacity_estimate')]
        for usage, code in cases:
            with self.subTest(code=code):
                response = envelope(); response['usage'] = usage
                report = diagnose(response)
                self.assert_failure(report, 'usage', code)
                self.assertEqual(report['usage'], {'code': code})

    def test_transport_codes_accept_only_a_boolean(self):
        self.assert_failure(diagnose_transport_failure(timed_out=True), 'transport', 'timeout')
        self.assert_failure(diagnose_transport_failure(timed_out=False), 'transport', 'transport_error')
        self.assert_failure(diagnose_transport_failure(timed_out='PRIVATE_SENTINEL'),
                            'input', 'invalid_diagnostic_input')

    def test_invalid_diagnostic_inputs_are_static(self):
        base = {'http_status': 200, 'content_type': 'application/json', 'body': b'{}'}
        for name, value in (('http_status', True), ('http_status', 600),
                            ('content_type', None), ('body', 'PRIVATE_SENTINEL')):
            args = {**base, name: value}
            report = diagnose_response(**args)
            self.assert_failure(report, 'input', 'invalid_diagnostic_input')
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))

    def test_inspection_does_not_mutate_caller_data(self):
        response = envelope(); original = deepcopy(response)
        diagnose(response)
        self.assertEqual(response, original)


if __name__ == '__main__':
    unittest.main()
