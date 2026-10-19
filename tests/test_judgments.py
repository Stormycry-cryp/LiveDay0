"""合成答案只验证协议与组合逻辑，不证明 Jev 的真实语义质量。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from liveday0 import extraction_contract as c
from liveday0.judgments import (MemoryJudgments, Classification, EventAssignment,
                               ObservationJudgment, MAX_CANDIDATES)
from liveday0.jev import JudgmentFailure, MODEL, MAX_RESPONSE_ID_LENGTH


STAMP = datetime(2026, 10, 19, 1, tzinfo=timezone.utc)
TEXT = '合成：今天与朋友散步后轻松了一些，还想周末再约。'


def observation(kind='event', category='event', **changes):
    request = c.ExtractionRequest.build('synthetic-source', 'v1', TEXT, STAMP)
    bodies = {'event': {'goal_context': '与朋友散步', 'current_result': '轻松了一些'},
              'fact': {'proposition': '喜欢散步', 'scope': '闲暇时'},
              'prospective': {'item': '周末再约散步', 'status': 'pending'}}
    proposal = c.SemanticProposal(
        proposal_id='synthetic-proposal', source=c.ProposalSource(request.source.source_id,
            request.source.source_version, request.content_digest, c.SourceSpan.from_text(0, TEXT)),
        speaker='user', subject=c.SubjectRef('user', 'synthetic-user'),
        speech_mode='direct_statement', semantic_category=category,
        scope=c.ProposalScope('situational', '本次散步'), confidence=0.9,
        epistemic_state='asserted', privacy_class='ordinary', persistence_intent='implicit_candidate',
        revision=c.RevisionRef('create', 'synthetic-key'), producer=c.ProducerMetadata('synthetic', 'fixture', '1.0.0', '1.0.0'),
        semantic_input=c.ProposedSemanticInput(kind, bodies[kind], 'active', 'confirmed', 'synthetic-key', STAMP))
    return request, replace(proposal, **changes)


def candidate(identifier='host-event-1', version=3, kind='event'):
    return {'id': identifier, 'version': version, 'type': kind,
            'body': {'goal_context': '与朋友散步', 'current_result': '已经出门'},
            'valid_at': STAMP.isoformat(), 'lifecycle': 'active', 'epistemic_state': 'confirmed'}


def envelope(questions, choices=None, scores=None, confidence=1.0):
    answers = {}
    for name, question in questions.items():
        if question['type'] == 'choice':
            chosen = (choices or {}).get(name, next(iter(question['criteria'])))
            answers[name] = {'type': 'choice', 'choice': chosen,
                             'probabilities': {key: float(key == chosen) for key in question['criteria']},
                             'confidence': confidence}
        else:
            score = (scores or {}).get(name, 0)
            answers[name] = {'type': 'score', 'score': score,
                'legend': {str(i): text for i, text in enumerate(question['criteria'])},
                'probabilities': {str(i): float(i == score) for i in range(len(question['criteria']))},
                'confidence': confidence}
    return {'id': 'task_synthetic_judgment', 'model': MODEL, 'answers': answers,
            'usage': {'input_tokens': 100, 'output_tokens': 30}}


class Evaluator:
    def __init__(self, choices=None, scores=None, confidence=1.0, mutate=None):
        self.choices, self.scores, self.confidence, self.mutate = choices, scores, confidence, mutate
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((deepcopy(state), deepcopy(questions)))
        response = envelope(questions, self.choices, self.scores, self.confidence)
        if self.mutate:
            self.mutate(response)
        return response


class JudgmentTests(unittest.TestCase):
    def test_batched_event_judgment_uses_one_frozen_state_and_evaluation(self):
        choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                   'persistence': 'situational', 'category': 'event', 'event_assignment': 'candidate_0',
                   'source_support': 'supported'}
        evaluator = Evaluator(choices)
        values = [candidate()]
        original = deepcopy(values)
        result = MemoryJudgments(evaluator).judge_observation(*observation(), values)
        self.assertEqual(result, ObservationJudgment(
            Classification('keep', 'direct_statement', 'user', 'situational', 'event'),
            EventAssignment('existing_event', 'host-event-1', 3), 'supported'))
        self.assertEqual(len(evaluator.calls), 1)
        state, questions = evaluator.calls[0]
        self.assertEqual(len(questions), 7)
        self.assertEqual(set(questions), set(choices))
        self.assertEqual(state['source_span_text'], TEXT)
        self.assertEqual(set(state['candidates']), {'candidate_0'})
        self.assertEqual(values, original)
        self.assertTrue(all('independent' in question['instructions'] for question in questions.values()))
        self.assertIn('This premise does not establish', questions['event_assignment']['instructions'])

    def test_batched_fact_and_future_have_six_questions_without_event_candidates(self):
        for kind, category in [('fact', 'explicit_fact'), ('prospective', 'commitment')]:
            choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                       'persistence': 'situational', 'category': category, 'source_support': 'supported'}
            evaluator = Evaluator(choices)
            result = MemoryJudgments(evaluator).judge_observation(*observation(kind, category), [candidate()])
            self.assertIsNone(result.assignment)
            self.assertEqual(result.classification.category, category)
            self.assertEqual(result.source_support, 'supported')
            self.assertEqual(len(evaluator.calls), 1)
            state, questions = evaluator.calls[0]
            self.assertEqual(len(questions), 6)
            self.assertNotIn('event_assignment', questions)
            self.assertNotIn('candidates', state)

    def test_unused_low_confidence_event_branch_does_not_change_non_event_route(self):
        def uncertain_assignment(response):
            response['answers']['event_assignment']['confidence'] = 0.1
        for disposition in ['trace_only', 'outside_scope', 'uncertain']:
            evaluator = Evaluator({'disposition': disposition, 'event_assignment': 'candidate_0',
                                   'source_support': 'supported'}, mutate=uncertain_assignment)
            result = MemoryJudgments(evaluator).judge_observation(*observation(), [candidate()])
            self.assertEqual(result.classification.disposition, disposition)
            self.assertIsNone(result.assignment)
            self.assertEqual(result.source_support, 'supported')
            self.assertEqual(len(evaluator.calls), 1)
        required = Evaluator({'disposition': 'keep', 'event_assignment': 'candidate_0',
                              'source_support': 'supported'}, mutate=uncertain_assignment)
        result = MemoryJudgments(required).judge_observation(*observation(), [candidate()])
        self.assertEqual(result.assignment, EventAssignment('uncertain'))

    def test_batch_and_individual_interfaces_share_questions_and_parsers(self):
        choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                   'persistence': 'situational', 'category': 'event', 'event_assignment': 'new_event',
                   'source_support': 'supported'}
        evaluator = Evaluator(choices)
        judgments = MemoryJudgments(evaluator)
        batch = judgments.judge_observation(*observation(), [candidate()])
        classification = judgments.classify_observation(*observation())
        assignment = judgments.assign_event(*observation(), [candidate()])
        support = judgments.check_source_support(*observation())
        self.assertEqual(batch, ObservationJudgment(classification, assignment, support))
        batch_questions = evaluator.calls[0][1]
        individual_questions = {}
        for _, questions in evaluator.calls[1:]:
            individual_questions.update(questions)
        self.assertEqual(batch_questions, individual_questions)

    def test_batch_still_rejects_malformed_unused_answers(self):
        def malformed_assignment(response):
            response['answers']['event_assignment']['choice'] = 'invented_identity'
        evaluator = Evaluator({'disposition': 'outside_scope'}, mutate=malformed_assignment)
        with self.assertRaises(JudgmentFailure):
            MemoryJudgments(evaluator).judge_observation(*observation(), [])
        self.assertEqual(len(evaluator.calls), 1)

    def test_classification_independent_dimensions_in_one_call(self):
        evaluator = Evaluator({'disposition': 'keep', 'speech_mode': 'direct_statement',
                               'subject': 'user', 'persistence': 'situational', 'category': 'event'})
        result = MemoryJudgments(evaluator).classify_observation(*observation())
        self.assertEqual(result, Classification('keep', 'direct_statement', 'user', 'situational', 'event'))
        self.assertEqual(len(evaluator.calls), 1)
        questions = evaluator.calls[0][1]
        self.assertEqual(set(questions), {'disposition', 'speech_mode', 'subject', 'persistence', 'category'})
        self.assertEqual(set(questions['speech_mode']['criteria']), set(c._SPEECH_MODES) | {'unknown'})
        self.assertEqual(set(questions['category']['criteria']), set(c._SEMANTIC_CATEGORIES) | {'unknown'})
        self.assertFalse(MemoryJudgments.confidence_policy_calibrated)

    def test_low_confidence_is_uncertainty_and_threshold_is_configurable(self):
        request, proposal = observation()
        low = MemoryJudgments(Evaluator(confidence=0.79))
        self.assertEqual(low.classify_observation(request, proposal),
                         Classification('uncertain', 'unknown', 'unknown', 'unknown', 'unknown'))
        self.assertEqual(low.check_source_support(request, proposal), 'uncertain')
        self.assertEqual(low.assign_event(request, proposal, [candidate()]), EventAssignment('uncertain'))
        self.assertEqual(low.rank_recall_candidates('散步', [candidate()]), [])
        configured = MemoryJudgments(Evaluator({'source_support': 'supported'}, confidence=0.79), 0.75)
        self.assertEqual(configured.check_source_support(request, proposal), 'supported')
        for bad in [True, None, '0.8', -0.01, 1.1, float('nan'), float('inf'), 10 ** 400]:
            with self.subTest(bad=bad), self.assertRaises((JudgmentFailure, ValueError)):
                MemoryJudgments(Evaluator(), bad)

    def test_event_selection_uses_only_code_aliases_and_original_version(self):
        evaluator = Evaluator({'event_assignment': 'candidate_1'})
        values = [candidate(), candidate('host-event-2', 9)]
        original = deepcopy(values)
        result = MemoryJudgments(evaluator).assign_event(*observation(), values)
        self.assertEqual(result, EventAssignment('existing_event', 'host-event-2', 9))
        self.assertEqual(values, original)
        state, questions = evaluator.calls[0]
        self.assertNotIn('host-event-2', str(state))
        self.assertEqual(set(state['candidates']), {'candidate_0', 'candidate_1'})
        self.assertIn('topical resemblance alone is insufficient', questions['event_assignment']['criteria']['candidate_0'])
        for kind in ['new_event', 'trace_only', 'outside_scope', 'uncertain']:
            with self.subTest(kind=kind):
                self.assertEqual(MemoryJudgments(Evaluator({'event_assignment': kind})).assign_event(
                    *observation(), []), EventAssignment(kind))

    def test_facts_and_prospectives_do_not_create_events(self):
        for kind, category in [('fact', 'explicit_fact'), ('prospective', 'commitment')]:
            evaluator = Evaluator()
            result = MemoryJudgments(evaluator).assign_event(*observation(kind, category), [])
            self.assertEqual(result, EventAssignment('outside_scope'))
            self.assertEqual(evaluator.calls, [])

    def test_ordinary_trace_and_scope_rejection_remain_distinct(self):
        for disposition in ['trace_only', 'outside_scope']:
            evaluator = Evaluator({'disposition': disposition})
            result = MemoryJudgments(evaluator).classify_observation(*observation())
            self.assertEqual(result.disposition, disposition)

    def test_source_support_closed_options(self):
        for choice in ['supported', 'contradicted', 'unsupported', 'uncertain']:
            result = MemoryJudgments(Evaluator({'source_support': choice})).check_source_support(*observation())
            self.assertEqual(result, choice)

    def test_recall_allows_multiple_empty_and_stable_ties_without_mutation(self):
        values = [candidate('a'), candidate('b'), candidate('c'), candidate('d')]
        before = deepcopy(values)
        evaluator = Evaluator(scores={'candidate_0': 2, 'candidate_1': 0, 'candidate_2': 3, 'candidate_3': 2})
        self.assertEqual(MemoryJudgments(evaluator).rank_recall_candidates('散步经历', values), ['c', 'a', 'd'])
        self.assertEqual(values, before)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertTrue(all(q['type'] == 'score' for q in evaluator.calls[0][1].values()))
        self.assertEqual(MemoryJudgments(Evaluator()).rank_recall_candidates('无关话题', values), [])
        empty = Evaluator()
        self.assertEqual(MemoryJudgments(empty).rank_recall_candidates('无关话题', []), [])
        self.assertEqual(empty.calls, [])

    def test_invalid_candidates_are_rejected_before_evaluation(self):
        mutations = [lambda x: x.update(version=True), lambda x: x.update(version=0),
            lambda x: x.update(id=''), lambda x: x.update(id=123), lambda x: x.update(extra='private'),
            lambda x: x.pop('body'), lambda x: x.update(type='thread'), lambda x: x.update(valid_at='2026-10-19'),
            lambda x: x.update(lifecycle=[]), lambda x: x.update(epistemic_state={}),
            lambda x: x.update(body={'x': float('nan')}), lambda x: x.update(body={'x': 'x' * 33000})]
        for change in mutations:
            value = candidate(); change(value)
            evaluator = Evaluator()
            with self.subTest(value=value), self.assertRaises(JudgmentFailure):
                MemoryJudgments(evaluator).rank_recall_candidates('散步', [value])
            self.assertEqual(evaluator.calls, [])
        for values in [[candidate(), candidate()], [candidate(str(i)) for i in range(MAX_CANDIDATES + 1)]]:
            with self.assertRaises(JudgmentFailure):
                MemoryJudgments(Evaluator()).assign_event(*observation(), values)
        with self.assertRaises(JudgmentFailure):
            MemoryJudgments(Evaluator()).assign_event(*observation(), [candidate(kind='fact')])

    def test_source_binding_and_secret_are_checked_before_evaluation(self):
        request, proposal = observation()
        evaluator = Evaluator()
        for changed in [replace(request, source=replace(request.source, sensitivity='secret')),
                        c.ExtractionRequest.build('other-source', 'v1', TEXT, STAMP)]:
            with self.assertRaises(JudgmentFailure):
                MemoryJudgments(evaluator).check_source_support(changed, proposal)
        self.assertEqual(evaluator.calls, [])

    def test_invalid_choice_envelopes_are_rejected(self):
        def mutate_answer(fn):
            return lambda response: fn(response['answers']['source_support'])
        cases = [lambda r: r.update(model='jev-latest'), lambda r: r.update(model='jev-1.14'),
            lambda r: r.update(model='jev-1.14.0'), lambda r: r.update(extra='bad'),
            lambda r: r.pop('id'), lambda r: r.update(id=''), lambda r: r.update(id=' \t\n'),
            lambda r: r.update(id=True), lambda r: r.update(id=123), lambda r: r.update(id=None),
            lambda r: r.update(id='x' * (MAX_RESPONSE_ID_LENGTH + 1)),
            lambda r: r['answers'].update(extra={}), lambda r: r['answers'].pop('source_support'),
            lambda r: r['usage'].update(input_tokens=True), lambda r: r['usage'].update(output_tokens=-1),
            mutate_answer(lambda a: a.update(extra='bad')), mutate_answer(lambda a: a.pop('confidence')),
            mutate_answer(lambda a: a.update(choice='generated-id')),
            mutate_answer(lambda a: a.update(type='score')), mutate_answer(lambda a: a.update(confidence=True)),
            mutate_answer(lambda a: a.update(confidence=float('nan'))),
            mutate_answer(lambda a: a['probabilities'].update(supported=0.5)),
            mutate_answer(lambda a: a['probabilities'].update(supported=True)),
            mutate_answer(lambda a: a['probabilities'].update(supported=float('inf'))),
            mutate_answer(lambda a: a['probabilities'].update(unknown=0)),
            mutate_answer(lambda a: a['probabilities'].pop('unsupported')),
            mutate_answer(lambda a: a.update(choice='unsupported'))]
        for mutate in cases:
            with self.subTest(mutate=mutate), self.assertRaises(JudgmentFailure):
                MemoryJudgments(Evaluator({'source_support': 'supported'}, mutate=mutate)).check_source_support(*observation())

    def test_score_response_is_bound_to_legend_and_distribution(self):
        for mutate in [lambda a: a.update(score=True), lambda a: a.update(score=3),
                       lambda a: a['legend'].update({'2': 'different rubric'}),
                       lambda a: a['probabilities'].update({'2': 0.5}),
                       lambda a: a['probabilities'].update({'4': 0})]:
            def change(response, mutate=mutate):
                mutate(response['answers']['candidate_0'])
            with self.subTest(mutate=mutate), self.assertRaises(JudgmentFailure):
                MemoryJudgments(Evaluator(scores={'candidate_0': 2}, mutate=change)).rank_recall_candidates('散步', [candidate()])
        def fractional(response):
            a = response['answers']['candidate_0']
            a['score'] = 2.5; a['probabilities'] = {'0': 0., '1': 0., '2': 0.5, '3': 0.5}
        self.assertEqual(MemoryJudgments(Evaluator(mutate=fractional)).rank_recall_candidates('散步', [candidate()]), ['host-event-1'])

    def test_evaluator_failure_does_not_leak_details(self):
        class Failed:
            def evaluate(self, *_):
                raise RuntimeError('PRIVATE_SYNTHETIC_MATERIAL')
        with self.assertRaises(JudgmentFailure) as caught:
            MemoryJudgments(Failed()).check_source_support(*observation())
        self.assertEqual(str(caught.exception), 'evaluator_failed')

    def test_request_total_size_rejected_before_call(self):
        evaluator = Evaluator()
        values = [candidate(str(i)) for i in range(16)]
        for value in values:
            value['body'] = {'x': 'x' * 30000}
        with self.assertRaises(JudgmentFailure):
            MemoryJudgments(evaluator).rank_recall_candidates('散步', values)
        self.assertEqual(evaluator.calls, [])


if __name__ == '__main__':
    unittest.main()
