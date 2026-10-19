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


def event_observation(text, goal, result):
    _, proposal = observation()
    request = c.ExtractionRequest.build('synthetic-source', 'v1', text, STAMP)
    return request, replace(proposal,
        source=c.ProposalSource(request.source.source_id, request.source.source_version,
            request.content_digest, c.SourceSpan.from_text(0, text)),
        scope=c.ProposalScope('situational', goal),
        semantic_input=replace(proposal.semantic_input,
            body={'goal_context': goal, 'current_result': result}))


def envelope(questions, choices=None, confidence=1.0):
    answers = {}
    for name, question in questions.items():
        assert question['type'] == 'choice'
        chosen = (choices or {}).get(name, next(iter(question['criteria'])))
        answers[name] = {'type': 'choice', 'choice': chosen,
                         'probabilities': {key: float(key == chosen) for key in question['criteria']},
                         'confidence': confidence}
    return {'id': 'task_synthetic_judgment', 'model': MODEL, 'answers': answers,
            'usage': {'input_tokens': 100, 'output_tokens': 30}}


class Evaluator:
    def __init__(self, choices=None, confidence=1.0, mutate=None):
        self.choices, self.confidence, self.mutate = choices, confidence, mutate
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((deepcopy(state), deepcopy(questions)))
        response = envelope(questions, self.choices, self.confidence)
        if self.mutate:
            self.mutate(response)
        return response


class JudgmentTests(unittest.TestCase):
    def test_batched_event_judgment_uses_one_frozen_state_and_evaluation(self):
        choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                   'persistence': 'situational', 'category': 'event', 'event_candidate_0': 'same_event',
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
        self.assertTrue(all('独立' in question['instructions'] for question in questions.values()))
        self.assertTrue(all('不可信' in question['instructions'] for question in questions.values()))
        self.assertEqual(set(state), {'source_span_text', 'observed_at', 'claim', 'candidates'})
        self.assertEqual(state['claim'], {'kind': 'event',
            'content': {'goal_context': '与朋友散步', 'current_result': '轻松了一些'},
            'applies_to': '本次散步', 'valid_at': STAMP.isoformat()})
        self.assertEqual(state['observed_at'], STAMP.isoformat())

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
            self.assertFalse(any(name.startswith('event_candidate_') for name in questions))
            self.assertNotIn('candidates', state)

    def test_low_confidence_assignment_is_used_only_for_keep_route(self):
        def uncertain_assignment(response):
            response['answers']['event_candidate_0']['confidence'] = 0.1
        for disposition in ['trace_only', 'outside_scope', 'uncertain']:
            evaluator = Evaluator({'disposition': disposition, 'event_candidate_0': 'same_event',
                                   'source_support': 'supported'}, mutate=uncertain_assignment)
            result = MemoryJudgments(evaluator).judge_observation(*observation(), [candidate()])
            self.assertEqual(result.classification.disposition, disposition)
            self.assertIsNone(result.assignment)
            self.assertEqual(result.source_support, 'supported')
            self.assertEqual(len(evaluator.calls), 1)
        required = Evaluator({'disposition': 'keep', 'event_candidate_0': 'same_event',
                              'source_support': 'supported'}, mutate=uncertain_assignment)
        result = MemoryJudgments(required).judge_observation(*observation(), [candidate()])
        self.assertEqual(result.assignment, EventAssignment('existing_event', 'host-event-1', 3))

    def test_batch_and_individual_interfaces_share_questions_and_parsers(self):
        choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                   'persistence': 'situational', 'category': 'event', 'event_candidate_0': 'different_event',
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
            response['answers']['event_candidate_0']['choice'] = 'invented_identity'
        evaluator = Evaluator({'disposition': 'outside_scope'}, mutate=malformed_assignment)
        with self.assertRaises(JudgmentFailure):
            MemoryJudgments(evaluator).judge_observation(*observation(), [candidate()])
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
        self.assertEqual(MemoryJudgments.selection_policy, 'maximum_probability')

    def test_low_confidence_keeps_highest_probability_choice_without_threshold(self):
        request, proposal = observation()
        choices = {'disposition': 'keep', 'speech_mode': 'direct_statement', 'subject': 'user',
                   'persistence': 'situational', 'category': 'event', 'source_support': 'supported',
                   'event_candidate_0': 'same_event', 'candidate_0': 'use'}
        low = MemoryJudgments(Evaluator(choices, confidence=0.01))
        self.assertEqual(low.classify_observation(request, proposal),
                         Classification('keep', 'direct_statement', 'user', 'situational', 'event'))
        self.assertEqual(low.check_source_support(request, proposal), 'supported')
        self.assertEqual(low.assign_event(request, proposal, [candidate()]),
                         EventAssignment('existing_event', 'host-event-1', 3))
        self.assertEqual(low.rank_recall_candidates('散步', [candidate()]), ['host-event-1'])

    def test_explicit_unknown_and_uncertain_are_not_converted_to_save_decisions(self):
        choices = {'disposition': 'uncertain', 'speech_mode': 'unknown', 'subject': 'unknown',
                   'persistence': 'unknown', 'category': 'unknown', 'source_support': 'uncertain',
                   'event_candidate_0': 'same_event'}
        result = MemoryJudgments(Evaluator(choices, confidence=0.01)).judge_observation(
            *observation(), [candidate()])
        self.assertEqual(result.classification,
                         Classification('uncertain', 'unknown', 'unknown', 'unknown', 'unknown'))
        self.assertEqual(result.source_support, 'uncertain')
        self.assertIsNone(result.assignment)
        self.assertEqual(MemoryJudgments(Evaluator({'event_candidate_0': 'uncertain'}, confidence=0.01))
                         .assign_event(*observation(), [candidate()]), EventAssignment('uncertain'))

    def test_event_selection_uses_only_code_aliases_and_original_version(self):
        evaluator = Evaluator({'event_candidate_0': 'different_event', 'event_candidate_1': 'same_event'})
        values = [candidate(), candidate('host-event-2', 9)]
        original = deepcopy(values)
        result = MemoryJudgments(evaluator).assign_event(*observation(), values)
        self.assertEqual(result, EventAssignment('existing_event', 'host-event-2', 9))
        self.assertEqual(values, original)
        state, questions = evaluator.calls[0]
        self.assertNotIn('host-event-2', str(state))
        self.assertEqual(set(state['candidates']), {'candidate_0', 'candidate_1'})
        self.assertEqual(set(questions), {'event_candidate_0', 'event_candidate_1'})
        for index, question in enumerate(questions.values()):
            self.assertEqual(set(question['criteria']), {'same_event', 'different_event', 'uncertain'})
            self.assertIn(f'candidates.candidate_{index}', question['instructions'])
            self.assertIn('主题', question['criteria']['different_event'])

    def test_pairwise_progression_from_unresolved_to_agreed_uses_same_event_answer(self):
        text = '合成：继续上次与同事讨论的发布计划，今天分歧已解决，双方谈妥按周五发布。'
        current = event_observation(text, '与同事确认周五发布计划', '分歧已解决，双方谈妥按周五发布')
        previous = candidate()
        previous['body'] = {'goal_context': '与同事确认周五发布计划', 'current_result': '双方还未谈妥'}
        evaluator = Evaluator({'event_candidate_0': 'same_event'}, confidence=0.01)
        assignment = MemoryJudgments(evaluator).assign_event(*current, [previous])
        self.assertEqual(assignment, EventAssignment('existing_event', 'host-event-1', 3))
        state, questions = evaluator.calls[0]
        self.assertEqual(state['source_span_text'], text)
        self.assertEqual(state['claim']['content']['current_result'], '分歧已解决，双方谈妥按周五发布')
        self.assertEqual(state['candidates']['candidate_0']['body']['current_result'], '双方还未谈妥')
        instructions = questions['event_candidate_0']['instructions']
        for meaning in ['source_span_text', 'claim.content', '继续', '后续', '目标对象', '解决分歧', '确认计划']:
            self.assertIn(meaning, instructions)

    def test_pairwise_different_experiences_in_one_project_create_new_event(self):
        current = event_observation('合成：今天处理同一项目的一次数据库故障，已经恢复。',
                                    '恢复项目数据库故障', '数据库已恢复')
        previous = candidate()
        previous['body'] = {'goal_context': '确认项目发布计划', 'current_result': '发布计划已确认'}
        evaluator = Evaluator({'event_candidate_0': 'different_event'})
        self.assertEqual(MemoryJudgments(evaluator).assign_event(*current, [previous]),
                         EventAssignment('new_event'))
        instructions = evaluator.calls[0][1]['event_candidate_0']['instructions']
        self.assertIn('仅话题、项目或人名相同', instructions)

    def test_pairwise_uncertain_without_a_same_match_remains_uncertain(self):
        for choices in [{'event_candidate_0': 'uncertain'},
                        {'event_candidate_0': 'different_event', 'event_candidate_1': 'uncertain'}]:
            values = [candidate(str(index)) for index in range(len(choices))]
            result = MemoryJudgments(Evaluator(choices)).assign_event(*observation(), values)
            self.assertEqual(result, EventAssignment('uncertain'))

    def test_pairwise_same_matches_choose_largest_probability_and_preserve_identity(self):
        values = [candidate('a', 4), candidate('b', 9), candidate('c', 12)]
        choices = {'event_candidate_0': 'same_event', 'event_candidate_1': 'same_event',
                   'event_candidate_2': 'uncertain'}

        def probabilities(response):
            for name, distribution in {
                'event_candidate_0': {'same_event': 0.6, 'different_event': 0.3, 'uncertain': 0.1},
                'event_candidate_1': {'same_event': 0.8, 'different_event': 0.1, 'uncertain': 0.1},
                'event_candidate_2': {'same_event': 0.05, 'different_event': 0.05, 'uncertain': 0.9},
            }.items():
                response['answers'][name]['probabilities'] = distribution

        evaluator = Evaluator(choices, confidence=0.0, mutate=probabilities)
        self.assertEqual(MemoryJudgments(evaluator).assign_event(*observation(), values),
                         EventAssignment('existing_event', 'b', 9))

    def test_pairwise_same_matches_with_exact_probability_tie_are_uncertain(self):
        evaluator = Evaluator({'event_candidate_0': 'same_event', 'event_candidate_1': 'same_event'})
        self.assertEqual(MemoryJudgments(evaluator).assign_event(
            *observation(), [candidate('a'), candidate('b')]), EventAssignment('uncertain'))

    def test_no_event_candidates_skip_event_model_questions(self):
        evaluator = Evaluator({'disposition': 'keep', 'source_support': 'supported'})
        judgments = MemoryJudgments(evaluator)
        self.assertEqual(judgments.assign_event(*observation(), []), EventAssignment('new_event'))
        self.assertEqual(evaluator.calls, [])
        result = judgments.judge_observation(*observation(), [])
        self.assertEqual(result.assignment, EventAssignment('new_event'))
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(evaluator.calls[0][1]), 6)
        self.assertFalse(any(name.startswith('event_candidate_') for name in evaluator.calls[0][1]))
        self.assertEqual(evaluator.calls[0][0]['candidates'], {})

    def test_batch_question_budget_accepts_48_and_58_but_rejects_59_candidates(self):
        for count in [48, 58]:
            evaluator = Evaluator({'disposition': 'keep'})
            values = [candidate(str(index)) for index in range(count)]
            result = MemoryJudgments(evaluator).judge_observation(*observation(), values)
            self.assertEqual(result.assignment, EventAssignment('uncertain'))
            self.assertEqual(len(evaluator.calls), 1)
            self.assertEqual(len(evaluator.calls[0][1]), 6 + count)
            self.assertEqual(len(evaluator.calls[0][0]['candidates']), count)
        evaluator = Evaluator()
        with self.assertRaisesRegex(JudgmentFailure, '^invalid_questions$'):
            MemoryJudgments(evaluator).judge_observation(
                *observation(), [candidate(str(index)) for index in range(59)])
        self.assertEqual(evaluator.calls, [])

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
        choices = {'candidate_0': 'use', 'candidate_1': 'skip', 'candidate_2': 'use', 'candidate_3': 'use'}

        def probabilities(response):
            for name, use_probability in [('candidate_0', 0.6), ('candidate_2', 0.8), ('candidate_3', 0.6)]:
                response['answers'][name]['probabilities'] = {
                    'use': use_probability, 'skip': 1 - use_probability, 'uncertain': 0.0}

        evaluator = Evaluator(choices, confidence=0.01, mutate=probabilities)
        self.assertEqual(MemoryJudgments(evaluator).rank_recall_candidates('散步经历', values), ['c', 'a', 'd'])
        self.assertEqual(values, before)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertTrue(all(q['type'] == 'choice' and set(q['criteria']) == {'use', 'skip', 'uncertain'}
                            for q in evaluator.calls[0][1].values()))
        ignored = {'candidate_0': 'skip', 'candidate_1': 'uncertain',
                   'candidate_2': 'skip', 'candidate_3': 'uncertain'}
        self.assertEqual(MemoryJudgments(Evaluator(ignored)).rank_recall_candidates('无关话题', values), [])
        empty = Evaluator()
        self.assertEqual(MemoryJudgments(empty).rank_recall_candidates('无关话题', []), [])
        self.assertEqual(empty.calls, [])

    def test_recall_uses_plurality_without_a_probability_or_confidence_threshold(self):
        def plurality(response):
            response['answers']['candidate_0']['probabilities'] = {'use': 0.34, 'skip': 0.33, 'uncertain': 0.33}
        evaluator = Evaluator({'candidate_0': 'use'}, confidence=0.0, mutate=plurality)
        self.assertEqual(MemoryJudgments(evaluator).rank_recall_candidates('散步', [candidate()]), ['host-event-1'])

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

    def test_observation_state_preserves_complete_claim_and_needed_context_without_metadata(self):
        request, proposal = observation()
        prefix, suffix = '这是我自己的经历。', '这个周末尚未到来。'
        content = prefix + TEXT + suffix
        request = c.ExtractionRequest.build('synthetic-source', 'v1', content, STAMP)
        proposal = replace(proposal, source=c.ProposalSource(request.source.source_id,
            request.source.source_version, request.content_digest, c.SourceSpan.from_text(len(prefix), TEXT)))
        evaluator = Evaluator({'source_support': 'supported'})
        MemoryJudgments(evaluator).check_source_support(request, proposal)
        state, _ = evaluator.calls[0]
        self.assertEqual(set(state), {'source_span_text', 'source_context', 'observed_at', 'claim'})
        self.assertEqual(state['source_span_text'], TEXT)
        self.assertEqual(state['source_context'], content)
        self.assertEqual(state['claim']['content'], proposal.semantic_input.body)
        self.assertEqual(state['claim']['applies_to'], proposal.scope.applies_to)
        self.assertEqual(state['claim']['kind'], proposal.semantic_input.card_type)
        self.assertEqual(state['claim']['valid_at'], STAMP.isoformat())
        self.assertEqual(state['observed_at'], STAMP.isoformat())
        for private in ['source_id', 'digest', 'producer', 'canonical_key', 'synthetic-source',
                        'synthetic-proposal', 'synthetic-key', 'synthetic-user', 'request', 'proposal']:
            with self.subTest(private=private):
                self.assertNotIn(private, str(state))

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

    def test_recall_choice_is_bound_to_known_candidates_and_distribution(self):
        for mutate in [lambda a: a.update(choice='invented_candidate'),
                       lambda a: a.update(choice='skip'),
                       lambda a: a['probabilities'].update(use=0.5),
                       lambda a: a['probabilities'].update(candidate_0=0),
                       lambda a: a['probabilities'].pop('uncertain')]:
            def change(response, mutate=mutate):
                mutate(response['answers']['candidate_0'])
            with self.subTest(mutate=mutate), self.assertRaises(JudgmentFailure):
                MemoryJudgments(Evaluator({'candidate_0': 'use'}, mutate=change)).rank_recall_candidates('散步', [candidate()])

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
