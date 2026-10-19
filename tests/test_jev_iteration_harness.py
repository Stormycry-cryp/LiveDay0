"""Protocol fixtures validate packaging, restoration, refusal and metric accounting only.

Run with unittest, not pytest: the repository pytest conftest always initializes PostgreSQL.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('jev_iteration_harness', ROOT / 'benchmarks/jev_memory_iteration/run.py')
h = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(h)


def choices_response(payload, bindings, cases):
    response = h.protocol_fixture(payload['questions'])
    response.update(id='synthetic-protocol-only', usage={'input_tokens': 321, 'output_tokens': 87})
    for case in cases:
        proposal = case['proposal']
        choices = {'disposition':'keep', 'speech_mode':'direct_statement', 'subject':'user',
                   'persistence':proposal['scope']['persistence'], 'category':proposal['semantic_category'],
                   'event_assignment':'new_event', 'source_support':'supported'}
        expected = case['expected']
        if expected['primary_route'] in ('trace_only','outside_scope'):
            choices['disposition'] = expected['primary_route']
        if expected['primary_route'] == 'existing_event':
            choices['event_assignment'] = 'candidate_0'
        if 'forbidden_write' in expected['groups']:
            choices['source_support'] = 'contradicted'
        for name, global_name in bindings[case['id']].items():
            answer = response['answers'][global_name]
            choice = (('same_event' if expected['primary_route'] == 'existing_event' else 'different_event')
                      if name.startswith('event_candidate_') else choices[name])
            answer['choice'] = choice
            answer['probabilities'] = {key:float(key == choice) for key in answer['probabilities']}
    return response


class IterationHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, cls.case_hash = h.load_cases('dev')
        cls.module, cls.origin, _ = h.load_judgments('baseline')
        cls.payload, cls.bindings = h.prepare(cls.cases, cls.module)

    def test_frozen_source_bound_fixtures_and_question_budget(self):
        self.assertEqual(len(self.cases), 8)
        challenge, challenge_hash = h.load_cases('challenge')
        self.assertEqual(len(challenge), 6)
        self.assertNotEqual(self.case_hash, challenge_hash)
        self.assertEqual(len(self.payload['questions']), 55)
        self.assertTrue(self.origin['origin'].startswith('git:' + h.BASELINE_COMMIT))
        self.assertEqual(set(self.payload['state']['cases']), {case['id'] for case in self.cases})

    def test_every_question_explicitly_restricts_state_to_its_case(self):
        for identifier, mapping in self.bindings.items():
            for global_name in mapping.values():
                instructions = self.payload['questions'][global_name]['instructions']
                self.assertIn('ONLY inspect state.cases.'+identifier, instructions)
                self.assertIn('RELATIVE to that local object', instructions)
                self.assertIn('opaque identifiers, never evidence', instructions)
        # Answers returned by collector never enter request state or the benchmark quality result.
        self.assertNotIn('protocol_collection_only', str(self.payload))
        self.assertNotIn('expected', str(self.payload))

    def test_restores_case_local_answers_and_preserves_actual_envelope(self):
        response = choices_response(self.payload, self.bindings, self.cases)
        original = deepcopy(response)
        rows = h.evaluate_response(self.cases, self.module, self.payload, self.bindings, response)
        self.assertEqual(response, original)
        self.assertTrue(all(row['passed'] for row in rows))
        self.assertTrue(all('usage' not in row for row in rows))
        result = h.metrics(rows)
        self.assertEqual(result['work_event']['total'],3)
        self.assertEqual(result['forbidden_write']['correct'],2)
        self.assertEqual(result['overall']['needs_review_rate'],2/8)
        self.assertEqual(result['existing_event']['accuracy'],1)

    def test_protocol_malformed_answer_is_rejected_not_semantic_failure(self):
        response = choices_response(self.payload, self.bindings, self.cases)
        response['answers'].pop(next(iter(response['answers'])))
        with self.assertRaises(h.JudgmentFailure):
            h.evaluate_response(self.cases, self.module, self.payload, self.bindings, response)

    def test_case_local_answer_cross_wiring_is_rejected(self):
        collector = h.Collector()
        self.module.MemoryJudgments(collector).judge_observation(*h.values(self.cases[0]))
        state, questions = collector.calls[0]
        evaluator = h.RestoredEvaluator(state,questions,h.protocol_fixture(questions)['answers'],h.protocol_fixture(questions))
        changed = deepcopy(state)
        changed['source_span_text'] = 'different fictional source'
        with self.assertRaises(h.JudgmentFailure):
            evaluator.evaluate(changed,questions)

    def test_baseline_argmax_ablation_reuses_same_response_and_questions(self):
        response = choices_response(self.payload, self.bindings, self.cases)
        for answer in response['answers'].values():
            answer['confidence'] = 0.2
        baseline = h.evaluate_response(self.cases,self.module,self.payload,self.bindings,response)
        module, _, _ = h.load_judgments('baseline','argmax')
        payload, bindings = h.prepare(self.cases,module)
        self.assertEqual(payload,self.payload)
        rows = h.evaluate_response(self.cases,module,payload,bindings,response)
        self.assertTrue(all(row['passed'] for row in rows))
        self.assertNotEqual(h.metrics(baseline)['overall']['accuracy'],h.metrics(rows)['overall']['accuracy'])

    def test_no_send_default_and_prepare_only_never_reads_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            def no_getenv(*args,**kwargs):
                raise AssertionError('prepare-only must not inspect credential environment')
            with patch.object(h.os,'getenv',side_effect=no_getenv):
                self.assertEqual(h.main(['--split','dev','--variant','baseline','--output',str(Path(directory)/'prepared'),'--prepare-only']),0)
            with patch.object(h.JevHTTP,'evaluate',side_effect=AssertionError('unauthorized network')):
                output=Path(directory)/'denied'
                self.assertEqual(h.main(['--split','dev','--variant','baseline','--output',str(output)]),2)
                self.assertIsNone(h.read_json(output/'report.json')['metrics'])

    def test_offline_replay_and_context_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            saved=Path(directory)/'saved'
            saved.mkdir()
            h.write_json(saved/'request.json',self.payload)
            h.write_json(saved/'response.json',choices_response(self.payload,self.bindings,self.cases))
            with patch.object(h.JevHTTP,'evaluate',side_effect=AssertionError('replay network')):
                output=Path(directory)/'replay'
                self.assertEqual(h.main(['--split','dev','--variant','baseline','--output',str(output),'--replay',str(saved)]),0)
                report=h.read_json(output/'report.json')
                self.assertEqual(report['network_calls'],0)
                self.assertEqual(report['mode'],'offline_replay')
                self.assertEqual(report['usage'],{'input_tokens':321,'output_tokens':87})
                changed=deepcopy(self.payload)
                changed['state']['cases']['c01']['source_span_text']='modified'
                h.write_json(saved/'request.json',changed)
                bad=Path(directory)/'bad'
                self.assertEqual(h.main(['--split','dev','--variant','baseline','--output',str(bad),'--replay',str(saved)]),2)
                self.assertIsNone(h.read_json(bad/'report.json')['metrics'])

    def test_live_attempt_ledger_enforces_minute_and_single_challenge(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(h,'ROOT',Path(directory)):
            with patch.object(h.time,'time',return_value=100):
                h.reserve_live_call('dev','baseline',0)
                with self.assertRaisesRegex(ValueError,'one_request_per_minute'):
                    h.reserve_live_call('dev','current',1)
            with patch.object(h.time,'time',return_value=161):
                h.reserve_live_call('challenge','current',1)
            with patch.object(h.time,'time',return_value=222):
                with self.assertRaisesRegex(ValueError,'challenge_live_attempt'):
                    h.reserve_live_call('challenge','current',2)
                with self.assertRaisesRegex(ValueError,'dev_variant_round'):
                    h.reserve_live_call('dev','baseline',0)


    def test_new_version_current_per_candidate_questions_restore_automatically(self):
        cases, frozen_hash = h.load_cases('challenge_v2')
        self.assertEqual(len(cases),6)
        module, _, _ = h.load_judgments('current')
        payload, bindings = h.prepare(cases,module)
        self.assertEqual(len(payload['questions']),6 * len(cases) + sum(len(case['candidates']) for case in cases))
        response = choices_response(payload,bindings,cases)
        rows = h.evaluate_response(cases,module,payload,bindings,response)
        self.assertTrue(all(row['passed'] for row in rows))
        for case in cases:
            local = bindings[case['id']]
            self.assertNotIn('event_assignment',local)
            self.assertEqual(sum(name.startswith('event_candidate_') for name in local),len(case['candidates']))

    def test_round_two_policy_preserves_consumed_v1_and_new_version_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(h,'ROOT',Path(directory)):
            h.write_json(Path(directory)/'freeze_manifest.json',{
                'live_phase':'round_2_only',
                'challenge_version_policy':{'challenge':{'consumed':True},'challenge_v2':{'consumed':False}}})
            with self.assertRaisesRegex(ValueError,'version_already_consumed'):
                h.reserve_live_call('challenge','current',2)
            with self.assertRaisesRegex(ValueError,'round_2_phase'):
                h.reserve_live_call('dev','current',1)
            with patch.object(h.time,'time',return_value=100):
                h.reserve_live_call('dev','current',2)
            with patch.object(h.time,'time',return_value=161):
                h.reserve_live_call('challenge_v2','current',2)
            with patch.object(h.time,'time',return_value=222):
                with self.assertRaisesRegex(ValueError,'challenge_live_attempt'):
                    h.reserve_live_call('challenge_v2','current',2)


if __name__ == '__main__':
    unittest.main()
