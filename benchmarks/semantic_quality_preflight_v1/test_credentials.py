"""Credential-reader boundaries, dummy bytes only; no execute, HTTP or user files."""
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('credential_capture', ROOT / 'capture.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
DUMMY = 'OFFLINE_DUMMY_NOT_A_PROVIDER_KEY'


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = Path(tempfile.mkdtemp(prefix='ld0-credential-offline-')).resolve()
        cls.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        cls.dns = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden'))
        cls.network.start(); cls.dns.start()

    @classmethod
    def tearDownClass(cls):
        cls.network.stop(); cls.dns.stop()
        print(json.dumps({'evidence_root': str(cls.evidence), 'real_model_calls': 0,
                          'user_credentials_read': False, 'database': 'not_loaded'}))

    def setUp(self):
        self.root = self.evidence / self._testMethodName
        self.root.mkdir(mode=0o700)
        self.path = self.root / 'dummy.json'

    def write(self, value=None, raw=None, name=None, mode=0o600):
        path = self.root / (name or 'dummy.json')
        if raw is None:
            raw = json.dumps(value if value is not None else {'enabled': True, 'api_key': DUMMY}).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        return path

    def reject(self, path):
        with self.assertRaises(entry.Stop) as caught:
            entry.private_credential(path)
        self.assertEqual(caught.exception.args, ('credential_file_unavailable_or_invalid',))

    def test_explicit_enabled_file_and_lazy_default(self):
        self.write()
        reader = entry.credential_reader_for(self.path)
        self.assertEqual(reader('ignored'), DUMMY)
        self.assertIs(entry.credential_reader_for(None), entry.getpass.getpass)
        with patch.object(entry, 'private_credential', side_effect=AssertionError('eager read')):
            self.assertTrue(callable(entry.credential_reader_for(self.path)))

    def test_closed_json_and_key_bounds(self):
        values = [None, [], {}, {'enabled': False, 'api_key': DUMMY}, {'enabled': 1, 'api_key': DUMMY},
                  {'enabled': 'true', 'api_key': DUMMY}, {'enabled': True, 'api_key': DUMMY, 'extra': 1}]
        values += [{'enabled': True, 'api_key': key} for key in
                   (None, 123, '', 'a'*513, ' a', 'a b', 'a\n', '\u4e2d', '\x7f')]
        for i, value in enumerate(values):
            with self.subTest(i=i):
                self.reject(self.write(raw=json.dumps(value).encode(), name=f'bad-{i}.json'))
        for n in (1, 512):
            path = self.write({'enabled': True, 'api_key': 'a'*n}, name=f'valid-{n}.json')
            self.assertEqual(len(entry.private_credential(path)), n)

    def test_malformed_duplicate_and_nonfinite_static_errors(self):
        for i, raw in enumerate((b'{"api_key":"OFFLINE', b'\xff', b'{"enabled":true,"enabled":true,"api_key":"a"}',
                                 b'{"enabled":true,"api_key":NaN}', b'{"enabled":true,"api_key":Infinity}', b'{} {}')):
            self.reject(self.write(raw=raw, name=f'bad-{i}.json'))

    def test_private_permissions_and_owner(self):
        self.write()
        for mode in (0o644, 0o400, 0o660, 0o1600):
            os.chmod(self.path, mode)
            self.reject(self.path)
        os.chmod(self.path, 0o600)
        os.chmod(self.root, 0o750)
        self.reject(self.path)
        os.chmod(self.root, 0o700)
        with patch.object(entry.os, 'geteuid', return_value=os.geteuid()+1):
            self.reject(self.path)
        original = os.fstat
        def wrong_file_owner(fd):
            value = original(fd)
            if stat.S_ISREG(value.st_mode):
                return types.SimpleNamespace(st_mode=value.st_mode, st_uid=value.st_uid+1,
                                             st_nlink=value.st_nlink, st_size=value.st_size)
            return value
        with patch.object(entry.os, 'fstat', side_effect=wrong_file_owner):
            self.reject(self.path)

    def test_links_nonregular_missing_relative_and_parent_traversal(self):
        original = self.write(name='original.json')
        self.path.symlink_to(original)
        self.reject(self.path)
        linkdir = self.evidence / 'linked-parent'
        linkdir.symlink_to(self.root, target_is_directory=True)
        self.reject(linkdir / 'original.json')
        hardlink = self.root / 'hardlink.json'
        os.link(original, hardlink)
        self.reject(hardlink)
        fifo = self.root / 'fifo.json'
        os.mkfifo(fifo, 0o600)
        self.reject(fifo)
        self.reject(self.root)
        self.reject(self.root / 'missing.json')
        self.reject(Path('relative.json'))
        self.reject(self.root / '..' / self.root.name / 'original.json')

    def test_size_bound_including_growth_after_stat(self):
        self.write(raw=b' '*4097)
        self.reject(self.path)
        original = os.fstat
        def earlier_size(fd):
            value = original(fd)
            if stat.S_ISREG(value.st_mode):
                return types.SimpleNamespace(st_mode=value.st_mode, st_uid=value.st_uid,
                                             st_nlink=value.st_nlink, st_size=10)
            return value
        with patch.object(entry.os, 'fstat', side_effect=earlier_size):
            self.reject(self.path)

    def test_nonexecute_and_failed_authorization_never_read(self):
        forbidden = AssertionError('credential read forbidden')
        for command in ('preview', 'captures', 'stop', 'finalize'):
            with self.subTest(command=command), patch.object(sys, 'argv', ['capture.py', command, '--credentials-file', str(self.path)]), \
                    patch.object(entry, 'private_credential', side_effect=forbidden), \
                    patch.object(entry, 'frozen', side_effect=AssertionError('unexpected preflight')):
                with self.assertRaises(entry.Stop):
                    entry.main()
        with patch.object(sys, 'argv', ['capture.py', 'execute', '--credentials-file', str(self.path),
                                       '--code-review', 'review', '--grant', 'grant', '--price', 'price']), \
                patch.object(entry, 'frozen', return_value=('release', [], None, None, None)), \
                patch.object(entry, 'authorization', side_effect=entry.Stop('blocked_preflight')), \
                patch.object(entry, 'seal_existing_real_state') as seal, \
                patch.object(entry, 'private_credential', side_effect=forbidden), \
                patch.object(entry, 'capture_next', side_effect=AssertionError('execute body forbidden')):
            with self.assertRaises(entry.Stop):
                entry.main()
            seal.assert_called_once_with()

    def test_human_and_tty_gates_before_reader_or_transport(self):
        ledger = types.SimpleNamespace(attempts=[], close=lambda: None)
        contract = types.SimpleNamespace(ExtractionRequest=types.SimpleNamespace(build=lambda *a, **kw: None))
        glm = types.SimpleNamespace(GLMHTTP=lambda **kw: (_ for _ in ()).throw(AssertionError('transport forbidden')))
        cases = [{'id': 'd01', 'content': 'fictional', 'occurred_at': '2026-01-01T00:00:00Z'}]
        with patch.object(entry, 'Ledger', return_value=ledger), patch.object(sys.stdin, 'isatty', return_value=False), \
                patch.object(entry, 'private_credential', side_effect=AssertionError('reader forbidden')):
            with self.assertRaises(entry.Stop) as caught:
                entry.capture_next(self.root, 'r', cases, contract, glm, None, grant_sha='g',
                                   credential_reader=entry.credential_reader_for(self.path))
            self.assertEqual(caught.exception.args, ('human_terminal_required',))
        ledger.attempts = [object()]
        with patch.object(entry, 'Ledger', return_value=ledger), \
                patch.object(entry, 'human_gate', side_effect=entry.Stop('human_gate_waiting')), \
                patch.object(entry, 'private_credential', side_effect=AssertionError('reader forbidden')):
            with self.assertRaises(entry.Stop) as caught:
                entry.capture_next(self.root, 'r', cases, contract, glm, None, grant_sha='g',
                                   credential_reader=entry.credential_reader_for(self.path))
            self.assertEqual(caught.exception.args, ('human_gate_waiting',))


if __name__ == '__main__':
    unittest.main(verbosity=2)
