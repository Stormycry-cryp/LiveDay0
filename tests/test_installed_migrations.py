"""Directed installed-wheel acceptance; no pytest database fixtures or demo rerun.

Run with Python 3.13 and existing psycopg, uv, and PostgreSQL 17 tools.
All subprocesses use an explicit credential-free environment and new paths.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import mkdtemp
import unittest
from uuid import uuid4
import zipfile

REPO = Path(__file__).resolve().parents[1]
WHEEL = REPO / 'dist/liveday0-0.1.0-py3-none-any.whl'
SDIST = REPO / 'dist/liveday0-0.1.0.tar.gz'
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class InstalledMigrationTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        (cls.root / 'manifest.json').write_text(json.dumps(cls.record, ensure_ascii=False, indent=2))

    @classmethod
    def setUpClass(cls):
        cls.root = Path(mkdtemp(prefix='ld0-install-', dir='/tmp')).resolve()
        cls.site = cls.root / 'site'
        cls.env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
                   'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null',
                   'PGSYSCONFDIR': str(cls.root), 'UV_CACHE_DIR': str(cls.root / 'uv-cache'),
                   'PYTHONDONTWRITEBYTECODE': '1'}
        uv = shutil.which('uv')
        if not uv:
            raise RuntimeError('existing uv is required')
        cls.run_command([uv, '--no-config', 'pip', 'install', '--offline', '--no-deps',
                         '--python', sys.executable, '--target', str(cls.site), str(WHEEL)])
        cls.record = {'root': str(cls.root), 'wheel_sha256': hashlib.sha256(WHEEL.read_bytes()).hexdigest(),
                      'sdist_sha256': hashlib.sha256(SDIST.read_bytes()).hexdigest(),
                      'synthetic': True, 'model_calls': 0, 'stopped': False}

    @classmethod
    def run_command(cls, argv, *, extra_env=None):
        result = subprocess.run([str(a) for a in argv], cwd=cls.root, env={**cls.env, **(extra_env or {})},
                                text=True, capture_output=True, timeout=45)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    @classmethod
    def python(cls, code, *, extra_env=None):
        # -I ignores ambient Python paths; load only this installed wheel plus
        # the selected interpreter's already installed psycopg dependency.
        return cls.run_command([sys.executable, '-I', '-B', '-c',
            'import sys;sys.path.insert(0,' + repr(str(cls.site)) + ');' + code], extra_env=extra_env)

    def test_artifacts_contain_exact_single_source_sql(self):
        expected = {p.name: p.read_bytes() for p in (REPO / 'migrations').glob('*.sql')}
        with zipfile.ZipFile(WHEEL) as z:
            actual = {Path(n).name: z.read(n) for n in z.namelist() if n.endswith('.sql')}
        self.assertEqual(actual, expected)
        with tarfile.open(SDIST) as archive:
            actual_source = {Path(m.name).name: archive.extractfile(m).read()
                             for m in archive.getmembers() if m.isfile() and m.name.endswith('.sql')}
        self.assertEqual(actual_source, expected)
        result = self.python('import liveday0.migrations as m;print(m.__file__);print(sorted(m._migration_sql()))')
        self.assertIn(str(self.site / 'liveday0/migrations.py'), result)
        self.assertIn('[1, 2, 3, 4, 5]', result)
        self.record['exact_sql_artifacts_and_installed_import'] = True

    def test_missing_or_incomplete_resources_fail_before_connect(self):
        script = '''
from pathlib import Path
from shutil import copytree
from unittest.mock import patch
import liveday0.migrations as m
base = Path.cwd() / 'damaged'
base.mkdir()
for case in ('missing', 'last-pair', 'one-direction', 'duplicate', 'empty', 'gap'):
    target = base / case
    if case != 'missing':
        copytree(m.MIGRATIONS_DIR, target)
        if case == 'last-pair':
            for p in target.glob('005_*.sql'): p.unlink()
        elif case == 'one-direction': next(target.glob('005_*.down.sql')).unlink()
        elif case == 'duplicate': (target / '005_other.up.sql').write_text('SELECT 1;')
        elif case == 'empty': next(target.glob('005_*.up.sql')).write_text('')
        elif case == 'gap':
            for direction in ('up', 'down'): (target / ('007_gap.' + direction + '.sql')).write_text('SELECT 1;')
    with patch.object(m, 'MIGRATIONS_DIR', target), patch.object(m, 'connect') as connect:
        for action in (m.migrate_up, m.migrate_down, m.migration_status):
            try: action()
            except RuntimeError: pass
            else: raise AssertionError('damaged SQL reported success: ' + case)
        connect.assert_not_called()
print('six damaged catalogs rejected before database connection')
'''
        self.assertIn('six damaged catalogs', self.python(script))
        self.record['damaged_resources_no_database_connection'] = True

    def test_installed_cli_migrates_only_fresh_marked_empty_cluster(self):
        root = self.root
        (root / 's').mkdir(mode=0o700)
        (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'install_owner_' + suffix, 'install_client_' + suffix, 'ld0_install_' + suffix
        marker = 'liveday0-installed-migration-' + suffix
        self.record.update(marker=marker, database=db)
        def sql(statement, database=db):
            return self.run_command([PG / 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                '-h', root / 's', '-U', owner, '-d', database, '-c', statement]).strip()
        def migrate(action):
            return json.loads(self.python('from liveday0.cli import main;main(["migrate",' + repr(action) + '])',
                extra_env={'LIVEDAY0_DATABASE_URL': f'dbname={db} user={owner} host={root / "s"}'}))
        started = False
        try:
            self.run_command([PG / 'initdb', '-D', root / 'pgdata', '-U', owner, '--no-locale',
                '--encoding=UTF8', '--auth-local=trust', '--auth-host=reject'])
            with (root / 'pgdata/postgresql.conf').open('a') as stream:
                stream.write(f"\nlisten_addresses=''\nunix_socket_directories='{root / 's'}'\nunix_socket_permissions=0700\ncluster_name='{marker}'\n")
            (root / 'pgdata/installed-migration-marker.txt').write_text(marker)
            started = True
            self.run_command([PG / 'pg_ctl', '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
            sql(f'CREATE DATABASE {db};', 'postgres')
            sql(f"COMMENT ON DATABASE {db} IS '{marker}';")
            proof = json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();"))
            self.assertEqual(proof, {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                'marker': marker, 'db': db, 'comment': marker, 'public_tables': 0})
            self.record['initial_empty_isolation_proof'] = proof
            self.assertEqual(migrate('up'), [1, 2, 3, 4, 5])
            self.assertEqual(migrate('up'), [])
            self.assertEqual(migrate('down'), [5])
            self.assertEqual([s['version'] for s in migrate('status')], [1, 2, 3, 4])
            self.assertEqual(migrate('up'), [5])
            self.assertEqual([s['version'] for s in migrate('status')], [1, 2, 3, 4, 5])
            sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {db} TO {client};')
            result = self.python('''
import json
from uuid import uuid4
from datetime import datetime, timezone
from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.types import EvidenceInput
with connect() as conn:
    flags = conn.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
if any(flags.values()): raise AssertionError('application role must be restricted')
s = MemoryService(uuid4()); s.ensure_tenant()
receipt = s.observe(EvidenceInput(modality='text',source_kind='synthetic',content='合成：安装迁移验收',occurred_at=datetime.now(timezone.utc),idempotency_key='installed-synthetic'))
if not receipt['created']: raise AssertionError('expected newly created synthetic evidence')
print(json.dumps({'restricted_role':flags,'synthetic_observe_created':receipt['created']}))
''', extra_env={'LIVEDAY0_DATABASE_URL': f'dbname={db} user={client} host={root / "s"}'})
            self.record['application_check'] = json.loads(result)
            self.record['migration_sequence'] = ['up:1..5', 'up:empty', 'down:5', 'status:1..4', 'up:5', 'status:1..5']
        finally:
            if started:
                try:
                    self.run_command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    self.record['stopped'] = True
                except Exception as error:
                    self.record['stop_error'] = type(error).__name__
            self.record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(self.record, ensure_ascii=False, indent=2))
        self.assertTrue(self.record['stopped'] and self.record['postmaster_pid_absent'])
        print('INSTALLED_MIGRATION_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, default=WHEEL)
    parser.add_argument('--sdist', type=Path, default=SDIST)
    args, test_args = parser.parse_known_args()
    WHEEL, SDIST = args.wheel.resolve(), args.sdist.resolve()
    unittest.main(argv=[sys.argv[0], *test_args], verbosity=2)
