"""Version 2 of the machine-bound fixed trial entry; real attempt exhausted.

Run this file directly with Python -I -S -B to avoid the persistence package.
The local fixture exercises the response path without credentials or approval.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Event, Timer
import types


ENTRY_VERSION = 2
ROOT = Path('/Users/chenyunzhe/Documents/Codex/2026-10-09/task/implementation-phase2/provider-boundary')
STATE = ROOT / 'glm-cn-single-attempt'
REVIEW_GATE = ROOT / 'glm-cn-v2-reviewed.json'
LEGACY_SHA = '60d7e299f65bedf295a4fd12126157955403f42450fa2f5df4408a006339da1b'
DIAGNOSTICS_SHA = 'b05a05fb49294eb875678b04b24bee26a2bb004452b6d3bd2f99d0d49f0df665'
PAYLOAD_SHA = '3e21f31d34937859e0563e5cc5ea2f496f5da633165d3ef162c9e8720a0a8f15'
_HISTORICAL_STATE = {
    'attempt.json': '3a96d3f3a3282552a4495d0dfe5597d4516aa1f887ac943d175b41fae8c17521',
    'result.json': '41e3daf7fb286b0dd53fd7495304ab5c52e45a60884bf6843dcd925cc155f12f',
}


class EntryFailure(Exception):
    """Program-owned codes only; no provider or exception text."""


def _load_pinned(path: Path, digest: str, name: str):
    try:
        raw = path.read_bytes()
        if path.is_symlink() or hashlib.sha256(raw).hexdigest() != digest:
            raise EntryFailure('dependency_changed')
        module = types.ModuleType(name)
        module.__file__ = str(path)
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        return module
    except EntryFailure:
        raise
    except Exception:
        raise EntryFailure('dependency_unavailable') from None


def _dependencies():
    legacy = _load_pinned(ROOT / 'glm_cn_once.py', LEGACY_SHA, 'glm_fixed_legacy')
    diagnostics = _load_pinned(Path(__file__).with_name('synthetic_trial_diagnostics.py'),
                               DIAGNOSTICS_SHA, 'glm_fixed_diagnostics')
    return legacy, diagnostics


def _review_manifest() -> dict:
    return {'entry_version': ENTRY_VERSION,
            'entry_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'legacy_sha256': LEGACY_SHA, 'diagnostics_sha256': DIAGNOSTICS_SHA,
            'payload_sha256': PAYLOAD_SHA, 'approval_scope': 'code_review_only'}


def _verify_review_gate(gate: Path = REVIEW_GATE):
    try:
        if gate.is_symlink() or not gate.is_file():
            raise EntryFailure('source_review_not_approved')
        if gate.stat().st_size > 4096:
            raise EntryFailure('source_review_not_approved')
        raw = gate.read_text()
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise EntryFailure('source_review_not_approved')
                result[key] = value
            return result
        if json.loads(raw, object_pairs_hook=pairs) != _review_manifest():
            raise EntryFailure('source_review_not_approved')
    except EntryFailure:
        raise
    except Exception:
        raise EntryFailure('source_review_not_approved') from None


def _require_real_authorization():
    # This historical authorization was consumed. Missing/mutated files are
    # unverifiable, never a fresh attempt. A code-review gate cannot renew it.
    try:
        if STATE.is_symlink() or not STATE.is_dir() or STATE.stat().st_mode & 0o077:
            raise EntryFailure('historical_state_unverifiable')
        for name, digest in _HISTORICAL_STATE.items():
            path = STATE / name
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > 4096
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                raise EntryFailure('historical_state_unverifiable')
    except EntryFailure:
        raise
    except Exception:
        raise EntryFailure('historical_state_unverifiable') from None
    raise EntryFailure('attempt_already_consumed')


def _local_failure(code: str) -> dict:
    # All call sites supply literal codes; never convert an exception to text.
    return {'diagnostic_version': 1, 'status': 'failed', 'stage': 'entry',
            'code': code, 'retry_authorized': False, 'raw_response_retained': False}


def _inspect_response(legacy, diagnostics, contract, response) -> dict:
    status = response.status
    content_type = response.getheader('Content-Type', '')
    # HTTP and content-type failures need no body read or provider text.
    body = (response.read(diagnostics.MAX_RESPONSE_BYTES + 1)
            if status == 200 and content_type.split(';', 1)[0].strip().lower() == 'application/json'
            else b'')
    report = diagnostics.diagnose_response(http_status=status, content_type=content_type, body=body)
    if report['code'] == 'ok':
        try:
            # Preserve the pinned full validator; discard its content-bearing
            # return value. Source and fields cannot enter the saved report.
            legacy.validate_response(json.loads(body), contract, diagnostics.SYNTHETIC_SOURCE)
        except Exception:
            return _local_failure('invalid_full_contract')
    return report


def _post_fixture(legacy, diagnostics, contract, body: bytes, *, port: int, timeout: float) -> dict:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=min(timeout, 15))
    expired, timer = Event(), None
    try:
        conn.connect()
        sock = conn.sock
        sock.settimeout(timeout)
        def abort():
            expired.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        conn.request('POST', legacy.ENDPOINT, body,
                     {'Content-Type': 'application/json', 'Accept': 'application/json'})
        report = _inspect_response(legacy, diagnostics, contract, conn.getresponse())
        return diagnostics.diagnose_transport_failure(timed_out=True) if expired.is_set() else report
    except (OSError, http.client.HTTPException) as exc:
        return diagnostics.diagnose_transport_failure(
            timed_out=expired.is_set() or isinstance(exc, TimeoutError))
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        conn.close()


def _fixture_attempt(legacy, diagnostics, contract, *, port: int, state: Path,
                     timeout: float = 1.0) -> dict:
    """Test-only loopback entry. No key, external host or real-state override."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise EntryFailure('invalid_fixture_port')
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise EntryFailure('invalid_timeout')
    if not isinstance(state, Path) or state.resolve().is_relative_to(ROOT.resolve()):
        raise EntryFailure('fixture_state_must_be_separate')
    body = legacy.load_payload()
    # The pinned legacy claim reserves before transport and enforces O_EXCL.
    try:
        legacy.claim_attempt(state)
    except legacy.TrialError:
        raise EntryFailure('fixture_attempt_unavailable') from None
    report = _local_failure('interrupted_or_unknown')
    try:
        report = _post_fixture(legacy, diagnostics, contract, body, port=port, timeout=timeout)
    except Exception:
        report = _local_failure('local_error')
    finally:
        result = {'entry_version': ENTRY_VERSION, 'status': report['status'],
                  'diagnostic': report, 'full_contract_validated': report['code'] == 'ok',
                  'reserved_budget_cny': legacy.BUDGET_CNY, 'retry_authorized': False,
                  'provider_bill_verified': False, 'product_database_written': False}
        fd = os.open(state / 'result.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    return result


def main(argv=None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        fixture = (len(args) == 2 and args[0] == '--local-fixture'
                   and type(args[1]) is str and args[1].isascii() and args[1].isdecimal())
        if args not in (['--preview'], ['--execute']) and not fixture:
            raise EntryFailure('choose_preview_or_execute')
        legacy, diagnostics = _dependencies()
        legacy.load_payload()
        if fixture:
            port = int(args[1])
            if not 1 <= port <= 65535:
                raise EntryFailure('invalid_fixture_port')
            contract = legacy.load_contract()
            with TemporaryDirectory(prefix='glm-v2-fixture-') as directory:
                result = _fixture_attempt(legacy, diagnostics, contract, port=port,
                                          state=Path(directory) / 'attempt')
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result['full_contract_validated'] else 2
        if args == ['--preview']:
            print(json.dumps({'entry_version': ENTRY_VERSION, 'payload_sha256': PAYLOAD_SHA,
                              'model': legacy.MODEL, 'reserved_budget_cny': legacy.BUDGET_CNY,
                              'execution_authorized': False, 'historical_attempt_consumed': True,
                              'external_call_executed': False, 'retry_authorized': False}))
            return 0
        _verify_review_gate()
        _require_real_authorization()
        # No credential input or real transport exists in v2. A new grant
        # requires a separately reviewed entry; this consumed grant stays closed.
        raise EntryFailure('new_execution_authorization_required')
    except EntryFailure as exc:
        print(json.dumps({'entry_version': ENTRY_VERSION, 'status': 'stopped',
                          'code': exc.args[0], 'external_call_executed': False,
                          'retry_authorized': False}), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('trial_cancelled_no_retry', file=sys.stderr)
        return 2
    except Exception:
        print('entry_local_error_no_retry', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
