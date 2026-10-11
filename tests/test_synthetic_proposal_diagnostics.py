"""Focused synthetic mismatch → real host confirmation seam, with no database."""
from contextlib import redirect_stdout
from decimal import Decimal
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('semantic_fixture_helpers',
                                            Path(__file__).with_name('test_glm_semantic_scenarios.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
c, glm, local = helpers.c, helpers.glm, helpers.local


class RecordingService:
    """Record the host's handoff only; never pretend this is a real commit."""
    def __init__(self, tenant, **kwargs):
        self.observed = []
    def ensure_tenant(self):
        pass
    def observe(self, evidence, *, semantics):
        self.observed.append((evidence, semantics))
        return {'created': True, 'card_ids': ['synthetic-recording-stub']}


def load_confirmation_seam():
    previous = {k: v for k, v in sys.modules.items()
                if k == 'liveday0' or k.startswith('liveday0.')}
    for key in previous:
        del sys.modules[key]
    package = types.ModuleType('liveday0'); package.__path__ = [str(ROOT / 'src/liveday0')]
    core = types.ModuleType('liveday0.core'); core.MemoryService = RecordingService
    db = types.ModuleType('liveday0.db')
    def forbidden_transaction(*args, **kwargs):
        raise AssertionError('database path forbidden in this focused test')
    db.tenant_transaction = forbidden_transaction
    sys.modules.update({'liveday0': package, 'liveday0.core': core, 'liveday0.db': db,
        'liveday0.extraction_contract': c, 'liveday0.glm_extraction': glm,
        'liveday0.local_extraction': local, 'liveday0.provider_budget': helpers.failures})
    try:
        return tuple(importlib.import_module('liveday0.' + name) for name in
                     ('synthetic_proposal_diagnostics', 'local_host', 'local_console', 'exceptions'))
    finally:
        for key in list(sys.modules):
            if key == 'liveday0' or key.startswith('liveday0.'):
                del sys.modules[key]
        sys.modules.update(previous)


d, host, console, errors = load_confirmation_seam()


def expected_wire(text=d.PENDING_SOURCE):
    body = {'goal_context': '合成朋友爬山', 'development': '原定2026-10-10',
            'current_result': '改到2026-10-11'}
    if text == d.PENDING_SOURCE:
        body.update(unfinished_future='等待朋友确认', uncertainty='朋友尚未确认')
    return helpers.proposal(text, 'event', body, 'event')


def adapter(port):
    return d.CheckedSyntheticAdapter(glm.GLMCNAdapter(
        transport=glm.GLMHTTP(fixture_port=port, timeout=1),
        budget=glm.GLMCallBudget(max_cny=Decimal('1.25')), purpose='capture_new',
        authorize_send=lambda check: check))


class SyntheticProposalDiagnosticTests(unittest.TestCase):
    def blocked(self, mutate, field, code):
        wire = expected_wire(); mutate(wire['proposals'][0])
        confirmations = []
        with helpers.fixture(wire) as (port, seen):
            session = host.LocalMemorySession(adapter=adapter(port),
                confirm=lambda check: confirmations.append(check) or True)
            with self.assertRaises(d.SyntheticSemanticFailure) as caught:
                session.handle({'action': 'capture', 'text': d.PENDING_SOURCE,
                                'sensitivity': 'sensitive'})
        report = caught.exception.diagnostic
        self.assertEqual(report['code'], 'synthetic_semantic_mismatch')
        self.assertEqual(report['field_results'][field], code)
        self.assertEqual(len(seen), 1)
        self.assertEqual(confirmations, [])
        self.assertEqual(session._service.observed, [])
        rendered = json.dumps(report, ensure_ascii=False) + str(caught.exception)
        for value in ('PRIVATE_SENTINEL', d.PENDING_SOURCE, 'synthetic-person', '2026-10-12'):
            self.assertNotIn(value, rendered)

    def test_wrong_original_date_is_blocked_before_confirmation(self):
        self.blocked(lambda p: p['semantic_input']['body'].update(development='原定2026-10-09'),
                     'semantic_input.body.development', 'date_mismatch')

    def test_wrong_updated_date_is_blocked_before_confirmation(self):
        self.blocked(lambda p: p['semantic_input']['body'].update(current_result='改到2026-10-12'),
                     'semantic_input.body.current_result', 'date_mismatch')

    def test_wrong_user_identifier_is_blocked_without_repeating_role_gate(self):
        self.blocked(lambda p: p['subject'].update(identifier='PRIVATE_SENTINEL'),
                     'subject.identifier', 'subject_mismatch')

    def test_pending_changed_to_confirmed_is_blocked(self):
        self.blocked(lambda p: p['semantic_input']['body'].update(
            unfinished_future=None, uncertainty=None),
            'semantic_input.body.unfinished_future', 'confirmation_mismatch')

    def test_correct_pending_and_confirmed_results_reach_exact_confirmation(self):
        for text in (d.PENDING_SOURCE, d.CONFIRMED_SOURCE):
            with self.subTest(text=text), helpers.fixture(expected_wire(text)) as (port, seen):
                checks = []
                def approve(check):
                    checks.append(check)
                    self.assertEqual(session._service.observed, [])
                    self.assertEqual(check.action, 'save-sensitive')
                    self.assertEqual(check.payload['source']['content'], text)
                    self.assertEqual(check.payload['semantic']['body']['current_result'], '改到2026-10-11')
                    return True
                session = host.LocalMemorySession(adapter=adapter(port), confirm=approve)
                result = session.handle({'action': 'capture', 'text': text, 'sensitivity': 'sensitive'})
                self.assertEqual(len(checks), 1)
                self.assertEqual(len(session._service.observed), 1)
                self.assertEqual(result['outcomes'][0]['status'], 'stored')
                self.assertEqual(len(seen), 1)

    def test_correct_result_can_be_rejected_without_service_write(self):
        with helpers.fixture(expected_wire()) as (port, _):
            checks = []
            session = host.LocalMemorySession(adapter=adapter(port),
                confirm=lambda check: checks.append(check) or False)
            with self.assertRaises(errors.AuthorizationRequired):
                session.handle({'action': 'capture', 'text': d.PENDING_SOURCE, 'sensitivity': 'sensitive'})
        self.assertEqual(len(checks), 1)
        self.assertEqual(session._service.observed, [])

    def test_terminal_save_and_reject_use_existing_prompt(self):
        # Exercise the actual terminal helper without opening a human TTY.
        from unittest.mock import call, patch
        class Terminal(io.StringIO):
            def __init__(self, answer):
                super().__init__(); self.answer = answer
            def readline(self):
                return self.answer
            def close(self):
                pass
        approval = host.LocalApproval('save-sensitive', json.dumps({'synthetic': True}))
        for answer, accepted in (('SAVE\n', True), ('NO\n', False)):
            terminal = Terminal(answer)
            with patch('builtins.open', return_value=terminal) as opened:
                self.assertIs(console.terminal_confirmation(approval), accepted)
            self.assertEqual(opened.call_args_list, [call('/dev/tty', 'r'), call('/dev/tty', 'w')])
            self.assertIn('save-sensitive', terminal.getvalue())

    def test_unknown_synthetic_text_is_blocked_without_adapter_call(self):
        from unittest.mock import Mock
        delegate = Mock()
        request = c.ExtractionRequest.build('synthetic-unknown', 'v1', 'PRIVATE_SENTINEL', helpers.STAMP)
        with self.assertRaises(d.SyntheticSemanticFailure) as caught:
            d.CheckedSyntheticAdapter(delegate).extract(request)
        delegate.extract.assert_not_called()
        self.assertEqual(caught.exception.diagnostic['code'], 'unsupported_synthetic_source')


if __name__ == '__main__':
    unittest.main()
