#!/usr/bin/env python3
"""Finite source-bound Jev evaluation. Never imports historical benchmark material."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
if str(REPO / 'src') not in sys.path:
    sys.path.insert(0, str(REPO / 'src'))
from liveday0 import extraction_contract as contract
from liveday0.jev import JudgmentFailure, JevHTTP, MODEL, _json_bytes, validate_questions, validate_response

BASELINE_COMMIT = '87183ec31ef467afea45f24a463c14d5c61da00f'
JUDGMENTS_PATH = 'src/liveday0/judgments.py'
LIMITATIONS = [
    '有限开发或新挑战虚构样例，不能推断总体准确率。',
    '完整语义提案是固定夹具，不评估生成式提取质量。',
    '多 case batch 与单 case 生产上下文存在差异；每题只可访问其指定局部对象。',
    'route 是按生产 capture 判断顺序的只读预测；最终仍需独立单 case 实际 capture 验证。',
    '协议 collector 的合法答案只用于收集问题，不是质量答案；baseline 保留历史阈值，current 采用最高概率选项。',
    '最多两轮 dev 修改；每个独立 challenge 版本只允许一次真实调用，后续版本不能替换此前结果；脚本不自动循环、重试或付费回退。',
]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    from liveday0.jev import _strict_json
    return _strict_json(Path(path).read_bytes())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load_cases(split):
    raw = (ROOT / f'{split}.json').read_bytes()
    manifest = read_json(ROOT / 'freeze_manifest.json')
    if digest(raw) != manifest['sha256'][split]:
        raise ValueError('frozen_cases_changed')
    data = json.loads(raw)
    if data['split'] != split or len(data['cases']) != (8 if split == 'dev' else 6):
        raise ValueError('invalid_case_split')
    for case in data['cases']:
        request = contract.ExtractionRequest.from_dict(case['request'])
        proposal = contract.SemanticProposal.from_dict(case['proposal'])
        from uuid import UUID
        UUID(request.source.source_id)
        UUID(proposal.proposal_id)
        batch = contract.ProposalBatch(request.fingerprint, (proposal,))
        if any(not result.valid for result in contract.ProposalValidator.validate(request, batch)):
            raise ValueError('fixture_source_binding_invalid')
    return data['cases'], digest(raw)


def load_judgments(variant, policy_override=None):
    if variant == 'baseline':
        raw = subprocess.check_output(['git', 'show', f'{BASELINE_COMMIT}:{JUDGMENTS_PATH}'], cwd=REPO)
        origin = f'git:{BASELINE_COMMIT}:{JUDGMENTS_PATH}'
    else:
        raw = (REPO / JUDGMENTS_PATH).read_bytes()
        origin = str(REPO / JUDGMENTS_PATH)
    # A distinct module prevents baseline/current import cache contamination.
    name = '_jev_iteration_judgments_' + digest(raw)
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(raw, origin, 'exec'), module.__dict__)
    if policy_override == 'argmax':
        def selected(self, answer, fallback='uncertain'):
            return max(answer['probabilities'], key=answer['probabilities'].__getitem__)
        module.MemoryJudgments._selected = selected
    return module, {'origin': origin, 'sha256': digest(raw)}, raw


def protocol_fixture(questions):
    """Valid dummy protocol only; deliberately does not use case expectations."""
    answers = {}
    for name, question in questions.items():
        if question['type'] != 'choice':
            raise ValueError('unexpected_question_kind')
        chosen = next(iter(question['criteria']))
        answers[name] = {'type': 'choice', 'choice': chosen, 'confidence': 1.0,
                         'probabilities': {key: float(key == chosen) for key in question['criteria']}}
    return {'id': 'protocol_collection_only', 'model': MODEL, 'answers': answers,
            'usage': {'input_tokens': 0, 'output_tokens': 0}}


class Collector:
    def __init__(self):
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((deepcopy(state), deepcopy(questions)))
        return protocol_fixture(questions)


def values(case):
    return (contract.ExtractionRequest.from_dict(case['request']),
            contract.SemanticProposal.from_dict(case['proposal']), deepcopy(case['candidates']))


def prepare(cases, module):
    state, questions, bindings = {'cases': {}}, {}, {}
    for case in cases:
        collector = Collector()
        module.MemoryJudgments(collector).judge_observation(*values(case))
        if len(collector.calls) != 1:
            raise ValueError('judge_observation_must_be_single_batch')
        local_state, local_questions = collector.calls[0]
        identifier = case['id']
        state['cases'][identifier] = local_state
        bindings[identifier] = {}
        for name, question in local_questions.items():
            global_name = identifier + '__' + name
            scoped = deepcopy(question)
            scoped['instructions'] = (
                f'ONLY inspect state.cases.{identifier}. Ignore all other cases. '
                'All paths in the following question, including source_span_text, source_context, '
                'observed_at, claim, candidates, request and proposal, are RELATIVE to that local object. '
                'The question key and case key are opaque identifiers, never evidence. '
                + question['instructions'])
            questions[global_name] = scoped
            bindings[identifier][name] = global_name
    payload = {'model': MODEL, 'state': state, 'questions': questions}
    validate_questions(questions)
    _json_bytes(payload)
    if len(questions) > 56:
        raise ValueError('question_budget_exceeded')
    return payload, bindings


class RestoredEvaluator:
    def __init__(self, state, questions, answers, envelope):
        self.state, self.questions, self.answers, self.envelope = state, questions, answers, envelope
        self.calls = 0

    def evaluate(self, state, questions):
        self.calls += 1
        if self.calls != 1 or state != self.state or questions != self.questions:
            raise JudgmentFailure('replay_context_mismatch')
        # Preserve original model/id/usage. Token totals are counted only on the batch report.
        return {**{key: deepcopy(self.envelope[key]) for key in ('id', 'model', 'usage')},
                'answers': deepcopy(self.answers)}


def predict_route(judgment, proposal, candidates):
    """Read-only mirror of capture's semantic checks, without storage/authorization claims."""
    classification = judgment.classification
    disposition = classification.disposition
    if disposition == 'outside_scope':
        return 'outside_scope', None, None
    if disposition == 'uncertain':
        return 'needs_review', 'classification_uncertain', None
    if disposition not in {'keep', 'trace_only'}:
        return 'needs_review', 'invalid_classification', None
    if (classification.speech_mode != 'direct_statement' or classification.speech_mode != proposal.speech_mode
            or classification.subject != 'user' or classification.persistence not in {'situational', 'durable'}):
        return 'needs_review', 'classification_mismatch', None
    if disposition == 'keep' and (classification.persistence != proposal.scope.persistence
                                  or classification.category != proposal.semantic_category):
        return 'needs_review', 'classification_mismatch', None
    route, target = disposition, None
    if disposition == 'keep' and proposal.semantic_input.card_type == 'event':
        assignment = judgment.assignment
        if assignment is None:
            return 'needs_review', 'invalid_event_assignment', None
        route = assignment.kind
        if route == 'existing_event':
            target = next((candidate for candidate in candidates if candidate['id'] == assignment.candidate_id), None)
            if target is None or type(assignment.candidate_version) is not int or target['version'] != assignment.candidate_version:
                return 'needs_review', 'invalid_event_assignment', None
        elif route not in {'new_event', 'trace_only', 'outside_scope', 'uncertain'}:
            return 'needs_review', 'invalid_event_assignment', None
        elif assignment.candidate_id is not None or assignment.candidate_version is not None:
            return 'needs_review', 'invalid_event_assignment', None
    if route == 'outside_scope':
        return route, None, None
    if route == 'uncertain':
        return 'needs_review', 'assignment_uncertain', None
    if judgment.source_support != 'supported':
        return 'needs_review', 'source_not_supported', None
    return route, None, target['id'] if target else None


def evaluate_response(cases, module, payload, bindings, response):
    response = validate_response(response, payload['questions'])
    rows = []
    for case in cases:
        identifier = case['id']
        local_answers = {name: response['answers'][global_name] for name, global_name in bindings[identifier].items()}
        collector = Collector()
        module.MemoryJudgments(collector).judge_observation(*values(case))
        local_state, local_questions = collector.calls[0]
        evaluator = RestoredEvaluator(local_state, local_questions, local_answers, response)
        judgment = module.MemoryJudgments(evaluator).judge_observation(*values(case))
        request, proposal, candidates = values(case)
        route, reason, target = predict_route(judgment, proposal, candidates)
        expected = case['expected']
        route_correct = route == expected['primary_route'] and target == expected['candidate_id']
        support_correct = judgment.source_support in expected['source_support_allowed']
        failures = []
        if not route_correct:
            failures.append('primary_route_or_target_mismatch')
        if not support_correct:
            failures.append('source_support_assertion_failed')
        if 'forbidden_write' in expected['groups'] and (route != 'needs_review' or judgment.source_support == 'supported'):
            failures.append('unsafe_proposal_not_refused')
        rows.append({'id': identifier, 'raw_answers': local_answers, 'judgment': asdict(judgment),
                     'predicted_route': route, 'route_reason': reason, 'candidate_id': target,
                     'expected': deepcopy(expected), 'route_correct': route_correct,
                     'source_support_correct': support_correct, 'passed': not failures, 'failure_reasons': failures})
    return rows


def metrics(rows):
    result = {}
    groups = ('positive_event', 'work_event', 'trace', 'outside', 'forbidden_write', 'existing_event', 'new_event')
    for group in groups:
        selected = [row for row in rows if group in row['expected']['groups']]
        correct = sum(row['passed'] if group == 'forbidden_write' else row['route_correct'] for row in selected)
        result[group] = {'correct': correct, 'total': len(selected),
                         'accuracy': correct / len(selected) if selected else None,
                         'needs_review_rate': sum(row['predicted_route'] == 'needs_review' for row in selected) / len(selected) if selected else None}
    result['overall'] = {'correct': sum(row['passed'] for row in rows), 'total': len(rows),
                         'accuracy': sum(row['passed'] for row in rows) / len(rows) if rows else None,
                         'needs_review_rate': sum(row['predicted_route'] == 'needs_review' for row in rows) / len(rows) if rows else None}
    return result


def reserve_live_call(split, variant, round_number):
    """One account-wide local ledger; failures still consume a reserved live attempt."""
    if (variant == 'baseline' and round_number != 0) or (variant == 'current' and round_number not in (1, 2)):
        raise ValueError('baseline_requires_round_0_current_requires_round_1_or_2')
    manifest_path = ROOT / 'freeze_manifest.json'
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    challenge_policy = manifest.get('challenge_version_policy', {})
    if challenge_policy.get(split, {}).get('consumed'):
        raise ValueError('challenge_version_already_consumed')
    if manifest.get('live_phase') == 'round_2_only' and (variant != 'current' or round_number != 2 or split not in {'dev', 'challenge_v2'}):
        raise ValueError('round_2_phase_requires_current_dev_or_challenge_v2_round_2')
    ledger_path = ROOT / 'live_run_ledger.json'
    with (ROOT / '.live_run.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = read_json(ledger_path) if ledger_path.exists() else {'attempts': []}
        now = time.time()
        attempts = ledger['attempts']
        if attempts and now - attempts[-1]['time'] < 60:
            raise ValueError('free_account_one_request_per_minute_wait_before_next_explicit_call')
        if split in {'challenge', 'challenge_v2'} and any(item['split'] == split for item in attempts):
            raise ValueError('challenge_live_attempt_already_reserved')
        if split == 'dev' and any(item['split'] == split and item['variant'] == variant and item['round'] == round_number for item in attempts):
            raise ValueError('dev_variant_round_already_reserved')
        attempts.append({'time': now, 'split': split, 'variant': variant, 'round': round_number})
        write_json(ledger_path, ledger)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=('dev', 'challenge', 'challenge_v2'), required=True)
    parser.add_argument('--variant', choices=('baseline', 'current'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--policy-override', choices=('argmax',), help='Offline baseline response reuse only; no new inference.')
    parser.add_argument('--allow-synthetic-send', action='store_true')
    parser.add_argument('--dev-round', type=int, choices=(0, 1, 2), default=None)
    parser.add_argument('--replay', type=Path, help='Saved report directory with request.json and response.json; no new call.')
    args = parser.parse_args(argv)
    if args.prepare_only and args.replay:
        parser.error('--prepare-only and --replay are exclusive')
    if args.policy_override and (not args.replay or args.variant != 'baseline'):
        parser.error('--policy-override requires --variant baseline --replay <saved-directory>')
    cases, fixture_hash = load_cases(args.split)
    module, source, source_raw = load_judgments(args.variant, args.policy_override)
    payload, bindings = prepare(cases, module)
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {'split': args.split, 'variant': args.variant, 'fixture_sha256': fixture_hash,
                'policy_override': args.policy_override,
                'comparison_boundary': 'baseline uses old inputs and confidence threshold 0.8; current uses revised inputs and probability argmax. Combined effects cannot be attributed to prompts alone.',
                'judgments_source': source, 'request_sha256': digest(_json_bytes(payload)),
                'question_count': len(payload['questions']), 'limitations': LIMITATIONS,
                'created_at': datetime.now(timezone.utc).isoformat()}
    write_json(args.output / 'request.json', payload)
    write_json(args.output / 'metadata.json', metadata)
    (args.output / 'judgments_source.py').write_bytes(source_raw)
    if args.prepare_only:
        write_json(args.output / 'report.json', {**metadata, 'mode': 'prepare_only', 'network_calls': 0,
            'semantic_results': None, 'usage': None, 'metrics': None})
        print(json.dumps({'mode': 'prepare_only', 'question_count':len(payload['questions']), 'output':str(args.output)}))
        return 0
    response = None
    try:
        if args.replay:
            saved = read_json(args.replay / 'request.json')
            if saved != payload:
                raise ValueError('replay_request_context_mismatch')
            response = read_json(args.replay / 'response.json')
            mode, network_calls = 'offline_replay', 0
        else:
            if not args.allow_synthetic_send:
                raise ValueError('explicit_allow_synthetic_send_required')
            # The sole credential source. Do not load dotenv, files, shell profiles, or provider fallbacks.
            key = os.environ.get('BEATAPI_API_KEY')
            client = JevHTTP(api_key=key, authorize_send=lambda _: True, timeout=30)
            round_number = args.dev_round if args.dev_round is not None else (0 if args.variant == 'baseline' else 1)
            reserve_live_call(args.split, args.variant, round_number)
            mode, network_calls = 'live_single_batch', 1
            response = client.evaluate(payload['state'], payload['questions'])
        write_json(args.output / 'response.json', response)
        rows = evaluate_response(cases, module, payload, bindings, response)
        report = {**metadata, 'mode': mode, 'network_calls': network_calls,
                  'response_id': response['id'], 'model': response['model'],
                  'usage': response['usage'], 'usage_accounting': ('Original whole-batch usage counted exactly once; no per-case token estimates.' if network_calls else 'Reused original whole-batch metadata; zero new inference or billed tokens in this replay.'),
                  'cases': rows, 'metrics': metrics(rows)}
        write_json(args.output / 'report.json', report)
        print(json.dumps({'mode': mode, 'output': str(args.output), 'metrics': report['metrics']}, ensure_ascii=False))
        return 0
    except (JudgmentFailure, ValueError) as exc:
        # Fixed error codes only: never serialize credentials, HTTP bodies or exception reprs.
        reason = str(exc) if isinstance(exc, JudgmentFailure) or str(exc).isidentifier() else 'evaluation_setup_failed'
        write_json(args.output / 'report.json', {**metadata, 'mode': 'failed_no_semantic_result',
             'failure_reason': reason, 'semantic_results': None, 'metrics': None, 'usage': None,
             'network_calls': locals().get('network_calls', 0)})
        print(json.dumps({'mode': 'failed_no_semantic_result', 'reason': reason, 'output':str(args.output)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
