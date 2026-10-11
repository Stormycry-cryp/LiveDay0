"""One fixed evaluation, one persistent ledger. Real mode is manual-only.

Run directly with -I -S -B. No environment lookup or retries.
An explicit private credential file is read only by the human execute path.
The Agent may exercise fixture_capture only; --execute is for a future human.
"""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import fcntl
import getpass
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import types
import warnings
from threading import Lock

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
BASE_SHA = 'd246c704dc56abed4dc9164d23850069cb2e12733d77f6dd83fe7967ef9dd217'
TASK = Path('/Users/chenyunzhe/Documents/Codex/2026-10-09/task/implementation-phase2/semantic-quality-preflight-v1')
REAL_STATE = TASK / 'capture-v1-new-attempts'
ORDER = tuple('d%02d' % i for i in range(1, 7)) + tuple('h%02d' % i for i in range(1, 7))
MODEL = 'glm-5.3-flash'
ENDPOINT = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
RESERVE = Decimal('1.2058624')


class Stop(Exception):
    pass


def fail(code):
    raise Stop(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()


def read(path, maximum=1_048_576, lines=False):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        import stat
        if not stat.S_ISREG(st.st_mode) or st.st_size > maximum:
            fail('bounded_regular_file_required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            fail('input_too_large')
        def pairs(items):
            out = {}
            for k, v in items:
                if k in out:
                    fail('duplicate_json_key')
                out[k] = v
            return out
        def bad_constant(value):
            fail('nonfinite_json')
        def parse(value):
            return json.loads(value, object_pairs_hook=pairs, parse_constant=bad_constant)
        return raw, ([parse(line) for line in raw.splitlines() if line.strip()] if lines else parse(raw))
    finally:
        os.close(fd)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_credential(path):
    """Human-enabled file only; never call this on a user's file from the Agent."""
    directory = fd = None
    try:
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or len(path.parts) < 3:
            raise ValueError
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 4096):
            raise ValueError
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        def pairs(items):
            result = {}
            for k, v in items:
                if k in result:
                    raise ValueError
                result[k] = v
            return result
        def nonfinite(value):
            raise ValueError
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
        if not isinstance(value, dict) or set(value) != {'enabled', 'api_key'} or value['enabled'] is not True:
            raise ValueError
        key = value['api_key']
        if (not isinstance(key, str) or not 1 <= len(key) <= 512
                or any(not 33 <= ord(char) <= 126 for char in key)):
            raise ValueError
        return key
    except Exception:
        # No parser error, path, supplied content or secret appears in output.
        fail('credential_file_unavailable_or_invalid')
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def credential_reader_for(path):
    # Selection is lazy: preview and failed authorization never read the file.
    return getpass.getpass if path is None else lambda prompt: private_credential(path)


def exclusive(path, value):
    exclusive_raw(path, encode(value))


def exclusive_raw(path, raw):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def frozen():
    raw, base = read(ROOT / 'manifest.json')
    if sha(raw) != BASE_SHA:
        fail('base_manifest_changed')
    release_raw, release = read(ROOT / 'capture-manifest.json')
    for p, expected in {**base['files'], **release['files']}.items():
        # Release paths are deliberately repo-relative, never arbitrary input.
        target = REPO / p
        if not target.resolve().is_relative_to(REPO) or sha(target.read_bytes()) != expected or target.is_symlink():
            fail('frozen_dependency_changed')
    if release['base_manifest_sha256'] != BASE_SHA or release['model'] != MODEL or release['endpoint'] != ENDPOINT:
        fail('release_scope_changed')
    package = types.ModuleType('liveday0')
    package.__path__ = [str(REPO / 'src/liveday0')]
    sys.modules['liveday0'] = package
    c, g, local = (importlib.import_module('liveday0.' + name) for name in
                   ('extraction_contract', 'glm_extraction', 'local_extraction'))
    cases = []
    for split in ('development', 'heldout'):
        raw = (ROOT / (split + '.jsonl')).read_bytes()
        cases.extend(json.loads(line) for line in raw.splitlines() if line.strip())
    if tuple(x['id'] for x in cases) != ORDER or g.RESERVATION_CNY != RESERVE:
        fail('case_order_or_reservation_changed')
    return sha(release_raw), cases, c, g, local


def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        fail('utc_timestamp_required')
    return dt


def authorization(release_sha, review_path, grant_path, price_path):
    """Trusted-operator artifacts, not cryptographic authorization credentials."""
    _, review = read(review_path, 4096)
    grant_raw, grant = read(grant_path, 4096)
    _, price = read(price_path, 4096)
    scope = {'capture_manifest_sha256': release_sha, 'model': MODEL, 'endpoint': ENDPOINT}
    if review != {**scope, 'scope': 'code_review_only'}:
        fail('code_review_required')
    if (set(grant) != set(scope) | {'scope', 'case_ids', 'retain_fictional_captures', 'expires_utc'}
            or any(grant.get(k) != v for k, v in scope.items())
            or grant['scope'] != 'new_fictional_evaluation_only'
            or type(grant['case_ids']) is not list or grant['case_ids'] != list(ORDER)
            or grant['retain_fictional_captures'] is not True):
        fail('new_execution_authorization_required')
    now = datetime.now(timezone.utc)
    if timestamp(grant['expires_utc']) <= now:
        fail('grant_expired')
    if any(price.get(k) != v for k, v in scope.items()):
        fail('preflight_scope_mismatch')
    price_age = (now - timestamp(price['read_utc'])).total_seconds()
    if (price.get('source') != 'https://docs.bigmodel.cn/cn/guide/start/pricing'
            or price.get('input_cny_per_million') != '0.8' or price.get('output_cny_per_million') != '2.8'
            or price.get('cache_storage_free') is not True or price.get('operator_verified') is not True
            or not 0 <= price_age <= 86400):
        fail('current_price_unverifiable_or_changed')
    return sha(grant_raw)


def call_accounting(glm):
    """Reuse frozen usage validation; one-send accounting has no CNY ceiling."""
    class CaptureAccounting(glm.GLMCallBudget):
        def __init__(self):
            self._entry, self._lock = None, Lock()

        def reserve(self):
            with self._lock:
                if self._entry is not None:
                    raise glm.ProviderFailure('duplicate_call')
                self._entry = {'status':'reserved', 'input_tokens':None, 'output_tokens':None}

        def snapshot(self):
            value = super().snapshot()
            value['estimated_cny'] = value.pop('reserved_cny')
            return value
    return CaptureAccounting()


def stopped(state, code):
    if not (state / 'STOP.json').exists():
        exclusive(state / 'STOP.json', {'code': code, 'retry_authorized': False})
    fail(code)


class Ledger:
    def __init__(self, state, release_sha, grant_sha, fixture):
        self.state, self.release, self.fixture = Path(state), release_sha, fixture
        expected = {'capture_manifest_sha256': release_sha, 'grant_sha256': grant_sha,
                    'mode': 'fixture' if fixture else 'real', 'case_ids': list(ORDER)}
        witness = self.state.with_name(self.state.name + '-started.json')
        if not self.state.exists() and witness.exists():
            fail('deleted_state_not_a_new_run')
        try:
            self.state.mkdir(mode=0o700)
        except FileExistsError:
            if self.state.is_symlink() or not self.state.is_dir() or self.state.stat().st_mode & 0o077:
                fail('private_state_required')
        else:
            # Independent content-free witness prevents missing whole state
            # being silently re-created as a fresh paid opportunity.
            exclusive(witness, expected)
            exclusive(self.state / 'plan.json', expected)
            sync_dir(self.state.parent)
        self.lock = os.open(self.state / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        locked = False
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            if read(self.state / 'plan.json')[1] != expected:
                fail('state_scope_mismatch')
            if read(witness)[1] != expected:
                fail('state_witness_unverifiable')
            if (self.state / 'STOP.json').exists():
                fail('run_permanently_stopped')
            self.attempts = sorted(self.state.glob('attempt-*'))
            reservations = sorted(self.state.glob('reserved-*.json'))
            if len(self.attempts) > len(ORDER) or len(reservations) != len(self.attempts):
                stopped(self.state, 'ledger_unverifiable')
            for i, path in enumerate(self.attempts):
                if path.name != 'attempt-%02d' % (i + 1) or path.is_symlink():
                    stopped(self.state, 'ledger_unverifiable')
                _, a = read(path / 'attempt.json')
                if read(self.state / ('reserved-%02d.json' % (i+1)))[1] != a:
                    stopped(self.state, 'ledger_unverifiable')
                if a.get('case_id') != ORDER[i] or a.get('estimated_cny') != str(RESERVE) or a.get('capture_manifest_sha256') != release_sha:
                    stopped(self.state, 'ledger_unverifiable')
                if not (path / 'result.json').is_file():
                    stopped(self.state, 'interrupted_or_unknown')
                _, result = read(path / 'result.json')
                if result.get('status') != 'captured':
                    stopped(self.state, 'previous_attempt_failed')
                row_raw, _ = read(path / 'capture.json', 65_536)
                if result.get('capture_sha256') != sha(row_raw):
                    stopped(self.state, 'capture_changed')
        except BaseException:
            if locked and not (self.state / 'STOP.json').exists():
                exclusive(self.state / 'STOP.json', {'code':'ledger_unverifiable', 'retry_authorized':False})
            os.close(self.lock)
            raise

    def close(self):
        os.close(self.lock)

    def captures(self, split):
        return [read(p / 'capture.json', 65_536)[1] for p in self.attempts
                if read(p / 'attempt.json')[1]['split'] == split]

    def claim(self, case, payload_sha):
        if (self.state / 'STOP.json').exists():
            fail('run_permanently_stopped')
        n = len(self.attempts)
        if n == len(ORDER):
            fail('evaluation_cases_complete')
        path = self.state / ('attempt-%02d' % (n + 1))
        path.mkdir(mode=0o700)  # O_EXCL directory claim, never recycle a case.
        sync_dir(self.state)
        value = {'capture_manifest_sha256': self.release,
            'case_id': case['id'], 'split': case['split'], 'request_sha256': payload_sha,
            'estimated_cny': str(RESERVE), 'status': 'reserved', 'fixture': self.fixture}
        exclusive(path / 'attempt.json', value)
        exclusive(self.state / ('reserved-%02d.json' % (n+1)), value)
        return path


def human_gate(ledger, split, report_path, review_path):
    captured = ledger.captures(split)
    if not captured:
        return
    if not report_path or not review_path:
        fail('previous_case_human_review_required')
    report_raw, report = read(report_path)
    review_raw, _ = read(review_path, lines=True)
    expected_raw = b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in captured)
    if (report.get('capture_sha256') != sha(expected_raw) or report.get('manifest_sha256') != BASE_SHA
            or report.get('split') != split or report.get('capture_count') != len(captured)
            or report.get('protocol_stop_not_observed') is not False):
        stopped(ledger.state, 'review_capture_binding_invalid')
    gate = ledger.state / ('gate-%02d-%s' % (len(ledger.attempts), sha(report_raw + review_raw)))
    if gate.exists():
        # A verified prefix is immutable; edited replacement review is rejected.
        if read(gate / 'binding.json')[1] != {'report_sha256': sha(report_raw), 'review_sha256': sha(review_raw)}:
            stopped(ledger.state, 'human_review_changed')
        if (gate / 'accepted.json').is_file():
            return
    else:
        gate.mkdir(mode=0o700)
        exclusive(gate / 'binding.json', {'report_sha256': sha(report_raw), 'review_sha256': sha(review_raw)})
        exclusive_raw(gate / 'report.json', report_raw)
        exclusive_raw(gate / 'review.jsonl', review_raw)
        exclusive_raw(gate / 'captures.jsonl', expected_raw)
    if not (gate / 'derived-report.json').exists():
        derive = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'prepare.py'),
            'ingest', '--split', split, '--captures', str(gate / 'captures.jsonl'),
            '--out', str(gate / 'derived-report.json')], env={}, umask=0o077,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if derive.returncode != 0:
            stopped(ledger.state, 'capture_revalidation_failed')
    if read(gate / 'derived-report.json')[1] != report:
        stopped(ledger.state, 'capture_report_not_derived_from_ledger')
    # Run only the pinned offline scorer in a separate interpreter, whose socket
    # guard cannot alter the manual caller. No inherited environment or secrets.
    if not (gate / 'score.json').exists():
        process = subprocess.run([sys.executable, '-I', '-S', '-B', str(ROOT / 'prepare.py'),
        'score', '--split', split, '--report', str(gate / 'report.json'),
        '--review', str(gate / 'review.jsonl'), '--out', str(gate / 'score.json')],
        env={}, umask=0o077, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if process.returncode != 0:
            stopped(ledger.state, 'human_review_invalid')
    _, score = read(gate / 'score.json')
    prefix = score['cases'][:len(captured)]
    if any(x['extraction_status'] == 'fail' for x in prefix):
        stopped(ledger.state, 'human_semantic_failure')
    if (len(prefix) != len(captured) or any(x['extraction_pass'] is not True for x in prefix)
            or (len(captured) == 6 and score['extraction_phase_pass'] is not True)):
        fail('human_review_incomplete_waiting')
    exclusive(gate / 'accepted.json', {'status': 'human_prefix_pass', 'fixture': ledger.fixture})


def capture_next(state, release_sha, cases, c, glm, local, *, grant_sha,
                 fixture_port=None, report=None, review=None, credential_reader=None, timeout=15,
                 pre_send_check=None):
    fixture = fixture_port is not None
    if fixture and (type(fixture_port) is not int or not 1 <= fixture_port <= 65535
                    or Path(state).resolve().is_relative_to(TASK.resolve())):
        fail('fixture_state_or_port_invalid')
    ledger = Ledger(state, release_sha, grant_sha, fixture)
    attempt = None
    try:
        n = len(ledger.attempts)
        if n == len(ORDER):
            fail('evaluation_cases_complete')
        if n:
            human_gate(ledger, 'development' if n <= 6 else 'heldout', report, review)
        case = cases[n]
        request = c.ExtractionRequest.build('quality-v1:' + case['id'], 'v1', case['content'],
                                             case['occurred_at'], max_proposals=1)
        # Key input belongs exclusively to a future human terminal, after all
        # code/grant/price/human gates. The CLI carries only a path,
        # never the key; file reading requires explicit human opt-in.
        if fixture:
            transport = glm.GLMHTTP(fixture_port=fixture_port, timeout=timeout)
        else:
            if credential_reader is None or not sys.stdin.isatty():
                fail('human_terminal_required')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = credential_reader('New authorized BigModel key (hidden; never saved): ')
            transport = glm.GLMHTTP(api_key=key, timeout=timeout)
            del key
        captured = []
        class Recording:
            fixture = transport.fixture
            def post(self, body):
                if (ledger.state / 'STOP.json').exists():
                    fail('stopped_before_transport')
                value = transport.post(body)
                captured.append(value)
                return value
        def authorize(check):
            nonlocal attempt
            if not fixture:
                if pre_send_check is None:
                    fail('fresh_pre_send_checks_required')
                pre_send_check()
            if check.model != MODEL or check.endpoint != ENDPOINT or check.reservation_cny != RESERVE:
                stopped(ledger.state, 'send_scope_changed')
            attempt = ledger.claim(case, check.payload_sha256)
            return check
        accounting = call_accounting(glm)
        adapter = glm.GLMCNAdapter(transport=Recording(), budget=accounting, purpose='capture_new', authorize_send=authorize)
        local.validate_and_gate(request, adapter)
        # Retain only the complete fields consumed by the validator. Never
        # store response headers, reasoning, unknown fields or exceptions.
        raw = captured[0]
        choice = raw['choices'][0]
        row = {'case_id':case['id'], 'request_sha256':read(attempt / 'attempt.json')[1]['request_sha256'],
               'response':{'model':raw['model'], 'usage':{k:raw['usage'][k] for k in
                   ('prompt_tokens','completion_tokens','total_tokens')}, 'choices':[{
                   'finish_reason':choice['finish_reason'], 'message':{'role':'assistant', 'content':choice['message']['content']}}]}}
        exclusive(attempt / 'capture.json', row)
        exclusive(attempt / 'result.json', {'status':'captured', 'capture_sha256':sha(encode(row)),
            'usage_accounting':accounting.snapshot(), 'semantic_quality':'unjudged', 'fixture':fixture, 'retry_authorized':False})
        return {'status':'captured_needs_human_review', 'case_id':case['id'], 'fixture':fixture,
                'attempts':n+1, 'estimated_cny':str(RESERVE*(n+1)), 'provider_bill_verified':False}
    except BaseException:
        if attempt is not None:
            # Even KeyboardInterrupt, validator/usage failure and unknown local
            # result permanently consume the durable attempt and stop this run.
            if not (attempt / 'result.json').exists():
                exclusive(attempt / 'result.json', {'status':'failed_or_unknown', 'retry_authorized':False})
            if not (ledger.state / 'STOP.json').exists():
                exclusive(ledger.state / 'STOP.json', {'code':'attempt_failed_or_unknown', 'retry_authorized':False})
        raise
    finally:
        ledger.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['preview', 'captures', 'finalize', 'stop', 'execute'])
    p.add_argument('--split', choices=['development','heldout'], default='development')
    p.add_argument('--out'); p.add_argument('--report'); p.add_argument('--review')
    p.add_argument('--code-review'); p.add_argument('--grant'); p.add_argument('--price')
    p.add_argument('--credentials-file', help='Explicit private JSON path; human execute only, no fallback')
    args = p.parse_args()
    if args.credentials_file is not None and args.command != 'execute':
        fail('credentials_file_execute_only')
    try:
        release, cases, c, glm, local = frozen()
    except BaseException:
        if args.command == 'execute':
            seal_existing_real_state()
        raise
    if args.command == 'preview':
        print(json.dumps({'capture_manifest_sha256':release, 'base_manifest_sha256':BASE_SHA,
            'model':MODEL, 'endpoint':ENDPOINT, 'case_ids':ORDER, 'evaluation_case_count':len(ORDER),
            'estimated_per_case_cny':str(RESERVE), 'estimated_all_cases_cny':str(RESERVE*len(ORDER)),
            'user_request_cap':None, 'user_cny_cap':None, 'codex_week_stop_percent':None,
            'real_calls':0, 'execution_authorized':False}))
        return
    if args.command == 'stop':
        if not REAL_STATE.is_dir() or REAL_STATE.is_symlink():
            fail('no_real_state')
        stopped(REAL_STATE, 'human_stop')
    if args.command == 'finalize':
        _, plan = read(REAL_STATE / 'plan.json')
        ledger = Ledger(REAL_STATE,release,plan['grant_sha256'],False)
        try:
            if len(ledger.attempts) != len(ORDER):
                fail('all12_captured_required')
            human_gate(ledger,'heldout',args.report,args.review)
            exclusive(REAL_STATE / 'COMPLETE.json', {'status':'human_extraction_preflight_complete',
                'capture_manifest_sha256':release,'production_quality_pass':False,
                'other_axes':'not_run_without_separate_evidence'})
        finally:
            ledger.close()
        print(json.dumps({'status':'human_extraction_preflight_complete','real_calls':0,'production_quality_pass':False}))
        return
    if args.command == 'captures':
        if not args.out or not REAL_STATE.is_dir():
            fail('real_state_and_output_required')
        # Export is read-only even after STOP; retain the successful prefix.
        rows = []
        for attempt in sorted(REAL_STATE.glob('attempt-*')):
            if read(attempt / 'attempt.json')[1]['split'] == args.split and (attempt / 'capture.json').is_file():
                rows.append(read(attempt / 'capture.json', 65_536)[1])
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b''.join((json.dumps(row, ensure_ascii=False)+'\n').encode() for row in rows))
            stream.flush(); os.fsync(stream.fileno())
        sync_dir(Path(args.out).resolve().parent)
        print(json.dumps({'status':'captures_exported','cases':len(rows),'real_calls':0}))
        return
    try:
        if any(x is None for x in (args.code_review,args.grant,args.price)):
            fail('new_review_grant_price_required')
        grant_sha = authorization(release,args.code_review,args.grant,args.price)
        def fresh_pre_send():
            if frozen()[0] != release or authorization(release,args.code_review,args.grant,args.price) != grant_sha:
                stopped(REAL_STATE, 'pre_send_scope_changed')
        print(json.dumps(capture_next(REAL_STATE,release,cases,c,glm,local,grant_sha=grant_sha,
            report=args.report,review=args.review,credential_reader=credential_reader_for(args.credentials_file),
            pre_send_check=fresh_pre_send)))
    except BaseException as exc:
        waiting = isinstance(exc, Stop) and exc.args[0] in {
            'previous_case_human_review_required', 'human_review_incomplete_waiting', 'evaluation_cases_complete'}
        if not waiting:
            seal_existing_real_state()
        raise


def seal_existing_real_state(state=REAL_STATE):
    """Known preflight failure must not resume using the same real grant."""
    state = Path(state)
    if state.is_symlink() or not state.is_dir() or state.stat().st_mode & 0o077:
        return
    fd = os.open(state / 'lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (state / 'STOP.json').exists():
            exclusive(state / 'STOP.json', {'code':'manual_execution_preflight_failed', 'retry_authorized':False})
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        main()
    except Stop as exc:
        print(json.dumps({'status':'stopped','code':exc.args[0],'retry_authorized':False}),file=sys.stderr)
        sys.exit(2)
    except BaseException:
        print(json.dumps({'status':'stopped','code':'local_error_or_interrupt','retry_authorized':False}),file=sys.stderr)
        sys.exit(2)
