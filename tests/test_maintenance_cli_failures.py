"""One directed CLI failure-visibility journey in a new synthetic private PG.

Run directly with Python 3.13/psycopg; avoids pytest's shared DB fixtures.
"""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class MaintenanceCliFailureTests(unittest.TestCase):
    def test_sql_failure_remains_visible_after_new_process_and_empty_poll(self):
        root = Path(mkdtemp(prefix='ld0-job-cli-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'job_owner_' + suffix, 'job_client_' + suffix, 'ld0_job_' + suffix
        marker = 'liveday0-maintenance-cli-' + suffix
        env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
               'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null', 'PGSYSCONFDIR': str(root),
               'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        report = {'root': str(root), 'marker': marker, 'synthetic': True, 'model_calls': 0, 'stopped': False}
        counter = 0
        def command(argv, *, login=owner, check=True):
            nonlocal counter
            counter += 1
            result = subprocess.run([str(a) for a in argv], cwd=root,
                env={**env, 'LIVEDAY0_DATABASE_URL': f'dbname={db} user={login} host={root / "s"}'},
                text=True, capture_output=True, timeout=45)
            (root / 'logs' / f'{counter:02d}.stdout').write_text(result.stdout)
            (root / 'logs' / f'{counter:02d}.stderr').write_text(result.stderr)
            if check: self.assertEqual(result.returncode, 0, result.stderr)
            return result
        def sql(statement, database=db):
            return command([PG / 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                '-h', root / 's', '-U', owner, '-d', database, '-c', statement]).stdout.strip()
        def python(script, *, check=True):
            return command([sys.executable, '-B', '-c', script], login=client, check=check)
        started = False
        try:
            command([PG / 'initdb', '-D', root / 'pgdata', '-U', owner, '--no-locale',
                '--encoding=UTF8', '--auth-local=trust', '--auth-host=reject'])
            with (root / 'pgdata/postgresql.conf').open('a') as stream:
                stream.write(f"\nlisten_addresses=''\nunix_socket_directories='{root / 's'}'\nunix_socket_permissions=0700\ncluster_name='{marker}'\n")
            (root / 'pgdata/job-cli-marker.txt').write_text(marker)
            started = True
            command([PG / 'pg_ctl', '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
            sql(f'CREATE DATABASE {db};', 'postgres'); sql(f"COMMENT ON DATABASE {db} IS '{marker}';")
            proof = json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();"))
            self.assertEqual(proof, {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                'marker': marker, 'db': db, 'comment': marker, 'public_tables': 0})
            report['initial_empty_isolation_proof'] = proof
            command([sys.executable, '-B', '-m', 'liveday0', 'migrate', 'up'])
            sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {db} TO {client};')
            seed = json.loads(python('''
import json
from uuid import uuid4
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput
with connect() as c:
    flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
if any(flags.values()): raise AssertionError('restricted application role required')
s=MemoryService(uuid4());s.ensure_tenant()
e=lambda label:EvidenceInput('text','synthetic',label,idempotency_key=label)
card=s.observe(e('合成事件'),semantics=[SemanticInput('event',{'goal_context':'合成维护验收','current_result':'合成旧状态'},canonical_key='synthetic:event')])['card_ids'][0]
s.add_event_delta(card,e('合成变化'),{'current_result':'合成新状态'},idempotency_key='synthetic:delta')
pending=s.observe(e('合成尚未到期'))['evidence_id'];future=s.maintenance.enqueue_candidate_discovery(pending)
cancel=s.observe(e('合成删除任务'))['evidence_id'];cancelled=s.maintenance.enqueue_candidate_discovery(cancel);s.delete_evidence(cancel)
with tenant_transaction(s.tenant_id) as c:
    job=c.execute("SELECT id FROM maintenance_jobs WHERE tenant_id=%s AND target_id=%s AND job_type='event_rewrite'",(s.tenant_id,card)).fetchone()['id']
    c.execute('UPDATE maintenance_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s',(s.tenant_id,job))
    c.execute("UPDATE maintenance_jobs SET available_at=now()+interval '1 day',last_error='synthetic diagnostic canary' WHERE tenant_id=%s AND id=%s",(s.tenant_id,future))
other=MemoryService(uuid4());other.ensure_tenant();eid=other.observe(e('合成他租户'))['evidence_id'];foreign=other.maintenance.enqueue_candidate_discovery(eid)
with tenant_transaction(other.tenant_id) as c:
    c.execute("UPDATE maintenance_jobs SET state='dead',last_error='synthetic foreign canary' WHERE tenant_id=%s AND id=%s",(other.tenant_id,foreign))
print(json.dumps({'tenant':str(s.tenant_id),'card':str(card),'job':str(job),'future':str(future),'cancelled':str(cancelled),'foreign':str(foreign),'restricted_flags':flags}))
''').stdout)
            report['seed'] = seed
            tenant, job, card = seed['tenant'], seed['job'], seed['card']
            def inspect():
                return json.loads(python(f'''
import json
from uuid import UUID
from liveday0.db import tenant_transaction
with tenant_transaction(UUID({tenant!r}),mode='read') as c:
    version=c.execute('SELECT current_version FROM semantic_cards WHERE tenant_id=%s AND id=%s',(UUID({tenant!r}),UUID({card!r}))).fetchone()['current_version']
    versions=c.execute('SELECT count(*) AS n FROM semantic_card_versions WHERE tenant_id=%s AND card_id=%s',(UUID({tenant!r}),UUID({card!r}))).fetchone()['n']
    pending=c.execute("SELECT count(*) AS n FROM event_deltas WHERE tenant_id=%s AND event_id=%s AND state='pending'",(UUID({tenant!r}),UUID({card!r}))).fetchone()['n']
    row=c.execute('SELECT state,attempts,failure_count,last_error FROM maintenance_jobs WHERE tenant_id=%s AND id=%s',(UUID({tenant!r}),UUID({job!r}))).fetchone()
print(json.dumps({{'version':version,'versions':versions,'pending':pending,'job':row}}))
''').stdout)
            fault = f'''
from liveday0.cli import main
from liveday0.maintenance import MaintenanceEngine
original=MaintenanceEngine._rewrite_event
def fail_after_write(self,conn,job):
    original(self,conn,job)
    conn.execute('SELECT 1/0')
MaintenanceEngine._rewrite_event=fail_after_write
main(['run-jobs',{tenant!r},'--limit','1'])
'''
            retry = python(fault, check=False)
            self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
            self.assertEqual(json.loads(retry.stdout)[0]['state'], 'retry')
            first = inspect(); report['retry_after_partial_write'] = first
            self.assertEqual((first['version'], first['versions'], first['pending']), (1, 1, 1))
            self.assertEqual((first['job']['state'], first['job']['failure_count']), ('retry', 1))
            # Seed two prior failures rather than repeat already-covered exhaustion tests.
            python(f"from uuid import UUID;from liveday0.db import tenant_transaction\nwith tenant_transaction(UUID({tenant!r})) as c:c.execute('UPDATE maintenance_jobs SET failure_count=2,available_at=now() WHERE tenant_id=%s AND id=%s',(UUID({tenant!r}),UUID({job!r})))")
            dead = python(fault, check=False)
            self.assertEqual(dead.returncode, 1, dead.stdout + dead.stderr)
            self.assertEqual(json.loads(dead.stdout)[0]['state'], 'dead')
            before = inspect(); report['last_budget_failure_after_partial_write'] = before
            self.assertEqual((before['version'], before['versions'], before['pending']), (1, 1, 1))
            self.assertEqual((before['job']['state'], before['job']['failure_count']), ('dead', 3))
            empty = command([sys.executable, '-B', '-m', 'liveday0', 'run-jobs', tenant, '--limit', '1'], login=client)
            self.assertEqual(json.loads(empty.stdout), [])
            full = command([sys.executable, '-B', '-m', 'liveday0', 'run-jobs', tenant, '--status', '--limit', '100'], login=client)
            state = json.loads(full.stdout); report['new_process_status'] = state
            self.assertEqual({str(r['job_id']) for r in state['jobs']}, {job, seed['future']})
            self.assertFalse(state['has_more'])
            self.assertNotIn('canary', full.stdout)
            self.assertNotIn('last_error', full.stdout)
            self.assertEqual(next(r for r in state['jobs'] if r['job_id'] == job)['diagnostic_code'], 'execution_error')
            small = command([sys.executable, '-B', '-m', 'liveday0', 'run-jobs', tenant, '--status', '--limit', '1'], login=client)
            page = json.loads(small.stdout); self.assertEqual(len(page['jobs']), 1); self.assertTrue(page['has_more'])
            bad = command([sys.executable, '-B', '-m', 'liveday0', 'run-jobs', tenant, '--status', '--limit', '0'], login=client, check=False)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(inspect(), before)
            report['checks'] = {'retry_and_dead_exit_one': True, 'partial_writes_rolled_back': True,
                'new_process_failed_job_visible': True, 'empty_poll_not_recovery': True,
                'status_tenant_scoped_content_free_bounded': True, 'cancelled_job_excluded': True,
                'status_read_does_not_revive_or_change_budget': True}
            report['status'] = 'passed'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    report['stopped'] = True
                except Exception as error:
                    report['stop_error'] = type(error).__name__
            report['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        self.assertTrue(report['stopped'] and report['postmaster_pid_absent'])
        print('MAINTENANCE_CLI_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
