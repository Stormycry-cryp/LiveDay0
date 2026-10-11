"""Frozen offline evaluation preparation; no credential or network execution mode."""
from pathlib import Path
import argparse
import hashlib
import importlib
import json
import re
import socket
import sys
import types
from decimal import Decimal

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


class Stopped(Exception):
    pass


def fail(code):
    raise Stopped(code)


def forbidden_network(*args, **kwargs):
    fail('network_disabled')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, maximum=1_048_576):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        fail('input_unavailable_or_too_large')
    raw = path.read_bytes()
    if len(raw) > maximum:
        fail('input_too_large')
    return raw


def strict(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                fail('duplicate_json_key')
            out[key] = value
        return out
    def bad_constant(value):
        fail('invalid_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)


def rows(path):
    return [strict(line) for line in read(path).splitlines() if line.strip()]


def write(path, value, lines=False):
    path = Path(path)
    # Exclusive output never overwrites evidence or a previous failed attempt.
    with path.open('x') as stream:
        if lines:
            for row in value:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        else:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def pure_modules():
    # Same namespace technique as existing semantic scenarios; no package
    # __init__, persistence, dotenv, environment or credentials are imported.
    package = types.ModuleType('liveday0')
    package.__path__ = [str(REPO / 'src/liveday0')]
    sys.modules['liveday0'] = package
    return tuple(importlib.import_module('liveday0.' + name) for name in
                 ('extraction_contract', 'glm_extraction', 'local_extraction', 'provider_budget'))


c, glm, local, failures = pure_modules()
# Load stdlib SSL's socket subclasses before replacing the socket constructor.
# Module imports define adapters only; no transport is constructed or called.
socket.socket = socket.create_connection = socket.getaddrinfo = forbidden_network


def frozen():
    manifest = strict(read(ROOT / 'manifest.json'))
    for name, expected in manifest['files'].items():
        path = REPO / name
        if digest(read(path)) != expected:
            fail('frozen_input_changed')
    data = rows(ROOT / 'development.jsonl') + rows(ROOT / 'heldout.jsonl')
    if len(data) != 12 or len({r['id'] for r in data}) != 12:
        fail('case_set_invalid')
    for split in ('development', 'heldout'):
        part = [r for r in data if r['split'] == split]
        if len(part) != 6 or {r['dimension'] for r in part} != set(manifest['dimensions']):
            fail('split_coverage_invalid')
    for r in data:
        if r['task'] not in {'extraction', 'correction', 'abstention'} or not r['content']:
            fail('case_task_invalid')
        request(r)  # Existing source/timestamp/length contract, no extraction.
        oracle = r['oracle']
        if type(oracle['empty_output_allowed']) is not bool:
            fail('empty_rule_invalid')
        for group in ('required', 'forbidden'):
            values = oracle[group]
            if not 1 <= len(values) <= 5 or len({x['id'] for x in values}) != len(values):
                fail('rubric_invalid')
            if any(x['axis'] != 'extraction' or not x['claim'] for x in values):
                fail('rubric_axis_invalid')
        if any(x['applicability'] not in {'any_output', 'returned_content'} for x in oracle['required']):
            fail('rubric_applicability_invalid')
        if any(x['severity'] not in {'critical', 'major'} for x in oracle['forbidden']):
            fail('rubric_severity_invalid')
        if r['task'] == 'correction' and (not oracle['correction_target'] or
                oracle['correction_target'] not in {x['label'] for x in r['baseline_anchors']}):
            fail('correction_target_invalid')
    return data, digest(read(ROOT / 'manifest.json'))


def request(case):
    return c.ExtractionRequest.build('quality-v1:' + case['id'], 'v1', case['content'],
                                     case['occurred_at'], max_proposals=1)


class NeverTransport:
    fixture = True
    def post(self, body):
        fail('transport_disabled')


def payload(case):
    found = []
    budget = glm.GLMCallBudget(max_cny=Decimal('1.25'))
    def collect(check):
        found.append(check.payload)
        return None  # Deliberate approval refusal BEFORE reserve/transport.
    adapter = glm.GLMCNAdapter(transport=NeverTransport(), budget=budget,
                               purpose='capture_new', authorize_send=collect)
    try:
        adapter.extract(request(case))
    except failures.ProviderFailure as exc:
        if str(exc) != 'outbound_approval_required':
            fail('payload_preparation_failed')
    if len(found) != 1 or budget.snapshot()['calls'] != 0:
        fail('unexpected_attempt_reservation')
    return found[0]


def indexed(values):
    if not isinstance(values, list) or any(type(v) is not dict or 'case_id' not in v for v in values):
        fail('row_format_invalid')
    out = {v['case_id']: v for v in values}
    if len(out) != len(values):
        fail('duplicate_case')
    return out


def import_captures(cases, path, mh):
    captures = indexed(rows(path))
    if not captures or list(captures) != [r['id'] for r in cases[:len(captures)]]:
        fail('capture_set_not_sequential_prefix')
    results = []
    class Replay:
        fixture = True
        def __init__(self, response): self.response = response
        def post(self, body): return self.response
    for case in cases:
        if case['id'] not in captures:
            results.append({'case_id':case['id'], 'structural_valid':False,
                            'empty_output':None, 'not_run':True})
            continue
        item = captures[case['id']]
        if set(item) != {'case_id', 'request_sha256', 'response'} or item['request_sha256'] != digest(payload(case)):
            fail('capture_payload_mismatch')
        if len(json.dumps(item['response'], ensure_ascii=False).encode()) > 64_000:
            fail('capture_response_too_large')
        adapter = glm.GLMCNAdapter(transport=Replay(item['response']),
            budget=glm.GLMCallBudget(max_cny=Decimal('1.25')), purpose='capture_new',
            authorize_send=lambda check: check)
        result = {'case_id': case['id'], 'structural_valid': False, 'empty_output': None}
        try:
            gated = local.validate_and_gate(request(case), adapter)
            result.update(structural_valid=True, empty_output=not gated,
                gate_reasons=[x.reason for x in gated],
                proposals=[x.proposal.to_dict() for x in gated])
        except (failures.ProviderFailure, c.ContractViolation, ValueError):
            result['failure'] = 'invalid_provider_contract_source_or_usage'
        results.append(result)
    return {'manifest_sha256': mh, 'split': cases[0]['split'], 'capture_sha256': digest(read(path)),
            'origin': 'operator_supplied_unverified_until_human_review',
            'real_calls_by_this_tool': 0, 'semantic_quality': 'unjudged', 'cases': results,
            'capture_count':len(captures), 'protocol_stop_not_observed':any(
                not r['structural_valid'] and not r.get('not_run')
                for r in results[:max(0,len(captures)-1)])}


def score(cases, report, review, mh, report_hash):
    results = indexed(report['cases']); judgments = indexed(review)
    expected = {r['id'] for r in cases}
    if set(results) != expected or set(judgments) != expected or report['manifest_sha256'] != mh:
        fail('review_case_set_or_manifest_mismatch')
    counts = {axis: {'pass': 0, 'fail': 0, 'not_run': 0} for axis in
              ('extraction', 'recall', 'correction', 'abstention')}
    complete, case_results = True, []
    for case in cases:
        r, j = results[case['id']], judgments[case['id']]
        if j['capture_report_sha256'] != report_hash:
            fail('review_report_hash_mismatch')
        ok = r['structural_valid'] and j['provenance_verified'] is True
        judged = j['provenance_verified'] is True and not r.get('not_run')
        missing = []
        for group in ('required', 'forbidden'):
            values = case['oracle'][group]
            if set(j[group]) != {x['id'] for x in values}:
                fail('review_criterion_set_mismatch')
            for value in values:
                verdict = j[group][value['id']]
                optional_empty = (group == 'required' and case['oracle']['empty_output_allowed']
                    and r['empty_output'] is True and value['applicability'] == 'returned_content')
                if verdict not in {'pass', 'fail', 'unjudged', 'not_applicable'}:
                    fail('review_verdict_invalid')
                if verdict == 'not_applicable' and not optional_empty:
                    fail('review_invalid_not_applicable')
                if verdict == 'unjudged' or not j['notes'].get(value['id']):
                    missing.append(value['id']); judged = False
                ok = ok and verdict in ({'pass', 'not_applicable'} if optional_empty else {'pass'})
        if r['empty_output'] and not case['oracle']['empty_output_allowed']:
            ok = False
        complete = complete and judged
        status = ('pass' if ok else 'fail') if judged else 'not_run'
        counts['extraction'][status] += 1
        case_results.append({'case_id':case['id'], 'extraction_status':status,
                             'extraction_pass':bool(ok and judged), 'unjudged':missing})
        for axis in ('recall', 'correction', 'abstention'):
            a = j[axis]; status = a['status']
            if status not in {'pass', 'fail', 'not_run'}:
                fail('axis_status_invalid')
            if status != 'not_run' and not re.fullmatch(r'sha256:[0-9a-f]{64}', a.get('evidence') or ''):
                fail('separate_axis_evidence_required')
            counts[axis][status] += 1
    return {'manifest_sha256': mh, 'split': cases[0]['split'], 'manual_judgments_complete': complete,
        'extraction_phase_pass': complete and counts['extraction']['pass'] == len(cases)
            and report.get('protocol_stop_not_observed') is False,
        'axes': counts, 'cases': case_results, 'empty_output_count': sum(r['empty_output'] is True for r in results.values()),
        'semantic_authority': 'human judgments, no automated entailment claim',
        'real_calls_by_this_tool': 0, 'production_quality_pass': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'export', 'ingest', 'template', 'score'])
    parser.add_argument('--split', choices=['development', 'heldout'], default='development')
    parser.add_argument('--out'); parser.add_argument('--captures'); parser.add_argument('--report')
    parser.add_argument('--review'); parser.add_argument('--development-score')
    args = parser.parse_args()
    data, mh = frozen(); cases = [r for r in data if r['split'] == args.split]
    if args.command == 'check':
        sizes = [len(payload(r)) for r in data]
        print(json.dumps({'status': 'prepared_not_executed', 'cases': len(data),
            'split_sizes': {'development': 6, 'heldout': 6}, 'request_bytes_min': min(sizes),
            'request_bytes_max': max(sizes), 'request_limit': 12, 'model_calls': 0,
            'network': 'disabled', 'credentials': 'no_input_or_lookup', 'database': 'not_loaded'}))
        return
    if not args.out: fail('output_required')
    if args.command == 'export':
        if args.split == 'heldout':
            if not args.development_score: fail('complete_development_review_required')
            dev = strict(read(args.development_score))
            if (dev.get('manifest_sha256') != mh or dev.get('split') != 'development'
                or dev.get('extraction_phase_pass') is not True or dev.get('manual_judgments_complete') is not True
                or dev.get('axes',{}).get('extraction') != {'pass':6,'fail':0,'not_run':0}
                or {x.get('case_id') for x in dev.get('cases',[])} != {r['id'] for r in data if r['split']=='development'}):
                fail('development_review_not_passed')
        write(args.out, [{'case_id':r['id'], 'split':r['split'],
            'request_sha256':digest(payload(r)), 'source_fingerprint':request(r).fingerprint,
            'payload':strict(payload(r))} for r in cases], lines=True)
    elif args.command == 'ingest':
        if not args.captures: fail('captures_required')
        write(args.out, import_captures(cases, args.captures, mh))
    else:
        if not args.report: fail('capture_report_required')
        report = strict(read(args.report)); report_hash = digest(read(args.report))
        if report.get('manifest_sha256') != mh or report.get('split') != args.split:
            fail('report_scope_mismatch')
        if args.command == 'template':
            write(args.out, [{'case_id':r['id'], 'capture_report_sha256':report_hash,
                'provenance_verified':False, 'required':{x['id']:'unjudged' for x in r['oracle']['required']},
                'forbidden':{x['id']:'unjudged' for x in r['oracle']['forbidden']}, 'notes':{},
                **{axis:{'status':'not_run','evidence':None} for axis in ('recall','correction','abstention')}}
                for r in cases], lines=True)
        else:
            if not args.review: fail('review_required')
            write(args.out, score(cases, report, rows(args.review), mh, report_hash))
    print(json.dumps({'status':'offline_artifact_written','model_calls':0,'network':'disabled'}))


if __name__ == '__main__':
    try:
        main()
    except Stopped as exc:
        print(json.dumps({'status':'stopped','code':exc.args[0],'model_calls':0}), file=sys.stderr)
        sys.exit(2)
    except Exception:
        print(json.dumps({'status':'stopped','code':'invalid_input_or_local_error','model_calls':0}), file=sys.stderr)
        sys.exit(2)
