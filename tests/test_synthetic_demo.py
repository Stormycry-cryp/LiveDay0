"""Single directed real-PG loop through the public CLI; never pytest fixtures."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


class SyntheticDemoIntegrationTests(unittest.TestCase):
    def test_public_cli_isolated_reviewed_life_loop(self):
        # Supply a deliberately unusable synthetic DSN. The public launcher must
        # ignore it and create its own marked empty cluster; no old DB is read.
        env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
               'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null',
               'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1',
               'LIVEDAY0_DATABASE_URL': 'dbname=must_never_connect host=/nonexistent-synthetic-socket'}
        result = subprocess.run([sys.executable, '-B', '-m', 'liveday0', 'synthetic-demo', '--scripted'],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['stopped'])
        self.assertTrue(report['postmaster_pid_absent'])
        proof = report['initial_empty_isolation_proof']
        self.assertEqual(proof['public_tables'], 0)
        self.assertEqual(proof['listen'], '')
        self.assertEqual(proof['comment'], report['marker'])
        app = report['result']['application_isolation_proof']
        self.assertFalse(app['rolsuper'] or app['rolbypassrls'] or app['rolcreatedb'] or app['rolcreaterole'])
        steps = report['result']['steps']
        self.assertEqual([s['action'] for s in steps], [
            'reject', 'save', 'query-pending', 'correct', 'query-corrected',
            'delete-card', 'delete-source', 'delete-source', 'query-after-delete'])
        self.assertTrue(all(all(s['checks'].values()) for s in steps))
        reviews = report['result']['reviews']
        self.assertEqual([r['action'] for r in reviews], ['save-candidate', 'save-candidate', 'correct', 'delete', 'delete', 'delete'])
        self.assertEqual([r['accepted'] for r in reviews], [False, True, True, True, True, True])
        self.assertEqual(reviews[1]['payload']['proposal']['privacy_class'], 'ordinary')
        self.assertEqual(report['result']['real_model_calls'], 0)
        self.assertEqual(report['result']['consent'], 'scripted fixture')
        final = report['result']['final_state']
        self.assertEqual(final['retained_card_bodies'], 0)
        self.assertTrue(all(e['status'] == 'deleted' and e['content'] is None and e['object_ref'] is None
                            for e in final['evidence']))
        path = Path(report['root'])
        self.assertFalse((path / 'pgdata/postmaster.pid').exists())
        self.assertEqual(json.loads((path / 'manifest.json').read_text())['status'], 'passed')
        print('SYNTHETIC_DEMO_EVIDENCE=' + str(path / 'manifest.json'))


class SyntheticDemoStopEvidenceTests(unittest.TestCase):
    def stop_failure(self, error):
        # File-only simulation of the failure boundary. No PG command is run.
        sys.path.insert(0, str(REPO / 'src'))
        from liveday0 import synthetic_demo as demo
        with TemporaryDirectory(prefix='ld0-stop-test-') as directory:
            base = Path(directory).resolve(); root = base / 'fresh'; root.mkdir(mode=0o700)
            tools = base / 'bin'; tools.mkdir()
            for name in ('initdb', 'pg_ctl', 'psql'):
                (tools / name).touch()
            suffix = 'deadbeef'; marker = 'liveday0-synthetic-demo-' + suffix
            def fake_run(argv, **kwargs):
                name = Path(argv[0]).name
                if name == 'initdb':
                    (root / 'pgdata').mkdir()
                elif name == 'pg_ctl' and argv[-1] == 'start':
                    (root / 'pgdata/postmaster.pid').write_text('synthetic pid')
                elif name == 'pg_ctl' and argv[-1] == 'stop':
                    raise error
                elif name == 'psql' and str(argv[-1]).endswith('/proof.sql'):
                    return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
                        'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                        'marker': marker, 'db': 'ld0_synth_' + suffix, 'comment': marker, 'public_tables': 0}))
                elif 'liveday0.synthetic_demo' in argv:
                    return SimpleNamespace(returncode=0, stderr='', stdout='{"synthetic":true}')
                return SimpleNamespace(returncode=0, stderr='', stdout='[]')
            with patch.object(demo, 'mkdtemp', return_value=str(root)), \
                    patch.object(demo, 'uuid4', return_value=SimpleNamespace(hex=suffix * 4)), \
                    patch.object(demo.subprocess, 'run', side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, 'stop unconfirmed'):
                    demo.launch(scripted=True, postgres_bin=tools)
            record = json.loads((root / 'manifest.json').read_text())
            self.assertEqual(record['status'], 'stop_unconfirmed')
            self.assertFalse(record['stopped'])
            self.assertFalse(record['postmaster_pid_absent'])
            self.assertEqual(record['stop_error'], type(error).__name__)

    def test_stop_timeout_always_saves_unconfirmed_evidence(self):
        self.stop_failure(subprocess.TimeoutExpired('synthetic-pg-ctl', 30))

    def test_stop_oserror_always_saves_unconfirmed_evidence(self):
        self.stop_failure(OSError('synthetic stop failure'))


if __name__ == '__main__':
    unittest.main()
