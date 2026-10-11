"""Read-only retention preview in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class RetentionPreviewTests(unittest.TestCase):
    def test_preview_bounds_samples_and_preserves_live_memory_and_scheduler_anchors(self):
        root = Path(mkdtemp(prefix='ld0-retention-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'ret_owner_' + suffix, 'ret_client_' + suffix, 'ld0_ret_' + suffix
        marker = 'liveday0-retention-preview-' + suffix
        env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
               'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null', 'PGSYSCONFDIR': str(root),
               'PYTHONPATH': str(REPO / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        record = {'root': str(root), 'marker': marker, 'synthetic': True, 'model_calls': 0, 'stopped': False}
        counter = 0
        def command(args, *, login=owner, check=True):
            nonlocal counter
            counter += 1
            result = subprocess.run([str(a) for a in args], cwd=root,
                env={**env, 'LIVEDAY0_DATABASE_URL': f'dbname={db} user={login} host={root / "s"}'},
                text=True, capture_output=True, timeout=45)
            (root / 'logs' / f'{counter:02d}.stdout').write_text(result.stdout)
            (root / 'logs' / f'{counter:02d}.stderr').write_text(result.stderr)
            if check: self.assertEqual(result.returncode, 0, result.stderr)
            return result
        def sql(text, database=db):
            return command([PG / 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                '-h', root / 's', '-U', owner, '-d', database, '-c', text]).stdout.strip()
        def python(text):
            return command([sys.executable, '-B', '-c', text], login=client).stdout
        started = False
        try:
            command([PG / 'initdb', '-D', root / 'pgdata', '-U', owner, '--no-locale',
                '--encoding=UTF8', '--auth-local=trust', '--auth-host=reject'])
            with (root / 'pgdata/postgresql.conf').open('a') as stream:
                stream.write(f"\nlisten_addresses=''\nunix_socket_directories='{root / 's'}'\nunix_socket_permissions=0700\ncluster_name='{marker}'\n")
            (root / 'pgdata/retention-marker.txt').write_text(marker)
            started = True
            command([PG / 'pg_ctl', '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
            sql(f'CREATE DATABASE {db};', 'postgres'); sql(f"COMMENT ON DATABASE {db} IS '{marker}';")
            proof = json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();"))
            self.assertEqual(proof, {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                'marker': marker, 'db': db, 'comment': marker, 'public_tables': 0})
            record['initial_empty_isolation_proof'] = proof
            command([sys.executable, '-B', '-m', 'liveday0', 'migrate', 'up'])
            sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {db} TO {client};')
            seed = json.loads(python('''
import json
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from psycopg.types.json import Jsonb
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
if any(flags.values()):raise AssertionError('restricted role required')
s=MemoryService(uuid4());s.ensure_tenant();other=MemoryService(uuid4());other.ensure_tenant()
e=lambda text:EvidenceInput('text','synthetic',text,idempotency_key=text)
card=s.observe(e('合成保留来源'),trace={'observation':'合成普通痕迹'},semantics=[SemanticInput('event',{'goal_context':'合成生活','current_result':'仍有效'},canonical_key='synthetic:retention')])['card_ids'][0]
s.add_event_delta(card,e('合成未吸收变化'),{'current_result':'待追平'},idempotency_key='synthetic:retention-delta')
deleted=s.observe(e('合成明确删除'))['evidence_id'];s.maintenance.enqueue_candidate_discovery(deleted);s.delete_evidence(deleted)
now=datetime.now(timezone.utc);old=now-timedelta(days=120);recent=now-timedelta(days=2)
eligible_snapshots=[];eligible_jobs=[];protected_jobs=[];foreign_ids=[]
with tenant_transaction(s.tenant_id) as c:
    for state,closed in [('active',None),('active',old),('invalidated',None),('expired',None),('invalidated',recent),('invalidated',old),('invalidated',old),('invalidated',old),('expired',old)]:
        sid=uuid4();c.execute("INSERT INTO recall_snapshots(id,tenant_id,tenant_revision,state,context,expansion_store,created_at,invalidated_at) VALUES (%s,%s,0,%s,%s,%s,%s,%s)",(sid,s.tenant_id,state,Jsonb({'synthetic':'PAYLOAD_CANARY'}),Jsonb({'synthetic':'PAYLOAD_CANARY'}),old,closed))
        if state!='active' and closed==old:eligible_snapshots.append(str(sid))
    def job(key,*,state='succeeded',age=120,created=None,failures=0,error=None,locked=None,wait=None,fp=None,kind='event_rewrite',jid=None):
        jid=jid or uuid4();created=created or old
        c.execute("INSERT INTO maintenance_jobs(id,tenant_id,job_type,target_kind,target_id,coalesce_key,state,failure_count,last_error,locked_at,wait_reason,wait_input_fingerprint,created_at,updated_at) VALUES (%s,%s,%s,'semantic_card',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",(jid,s.tenant_id,kind,card,key,state,failures,error,locked,wait,fp,created,now-timedelta(days=age)))
        return str(jid)
    eligible_jobs.append(job('superseded'));protected_jobs.append(job('superseded',created=recent))
    low,high=sorted((uuid4(),uuid4()));eligible_jobs.append(job('tie',jid=low));protected_jobs.append(job('tie',jid=high))
    protected_jobs.append(job('failed-success',failures=1));protected_jobs.append(job('failed-success',created=recent))
    protected_jobs.append(job('error-success',error='ERROR_CANARY'));protected_jobs.append(job('error-success',created=recent))
    protected_jobs.append(job('locked-success',locked=now));protected_jobs.append(job('locked-success',created=recent))
    protected_jobs.append(job('wait-success',wait='semantic_output_required'));protected_jobs.append(job('wait-success',created=recent))
    protected_jobs.append(job('fingerprint-success',fp='f'*64));protected_jobs.append(job('fingerprint-success',created=recent))
    protected_jobs.append(job('recent-success',age=2));protected_jobs.append(job('recent-success',created=now))
    protected_jobs.append(job('legacy-candidate',kind='candidate_discovery'));protected_jobs.append(job('legacy-candidate',state='pending',created=recent,kind='candidate_discovery'))
    for state in ('pending','running','retry','waiting','dead'):protected_jobs.append(job('state:'+state,state=state))
    protected_jobs.append(job('failed-dead',state='dead',failures=3,error='ERROR_CANARY'))
with tenant_transaction(other.tenant_id) as c:
    for _ in range(2):
        sid=uuid4();foreign_ids.append(str(sid));c.execute("INSERT INTO recall_snapshots(id,tenant_id,tenant_revision,state,context,created_at,invalidated_at) VALUES (%s,%s,0,'invalidated','{}',%s,%s)",(sid,other.tenant_id,old,old))
    for i in range(2):
        jid=uuid4();foreign_ids.append(str(jid));c.execute("INSERT INTO maintenance_jobs(id,tenant_id,job_type,target_kind,coalesce_key,state,created_at,updated_at) VALUES (%s,%s,'event_rewrite','semantic_card','foreign','succeeded',%s,%s)",(jid,other.tenant_id,old+timedelta(seconds=i),old))
print(json.dumps({'tenant':str(s.tenant_id),'foreign_tenant':str(other.tenant_id),'eligible_snapshots':eligible_snapshots,'eligible_jobs':eligible_jobs,'protected_jobs':protected_jobs,'foreign_ids':foreign_ids,'restricted_flags':flags}))
'''))
            record['seed'] = seed
            tenant = seed['tenant']
            def digest():
                return python(f'''
import hashlib,json
from uuid import UUID
from liveday0.db import tenant_transaction
result={{}}
for tenant in ({tenant!r},{seed['foreign_tenant']!r}):
    with tenant_transaction(UUID(tenant),mode='read') as c:
        for table in ('evidence','life_traces','semantic_cards','semantic_card_versions','event_deltas','card_sources','relations','projections','projection_versions','projection_supports','maintenance_jobs','recall_snapshots','deletion_markers','observation_receipts','interpretation_intents','source_interpretation_revocations','reobservation_intents'):
            rows=c.execute('SELECT * FROM '+table+' WHERE tenant_id=%s',(UUID(tenant),)).fetchall()
            result[tenant+':'+table]=sorted(json.dumps(r,sort_keys=True,default=str) for r in rows)
print(hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest())
''').strip()
            before = digest()
            def preview(*args):
                result = command([sys.executable, '-B', '-m', 'liveday0', 'retention', tenant, '--dry-run', *args], login=client)
                self.assertNotIn('CANARY', result.stdout)
                return json.loads(result.stdout)
            disabled = preview();record['unconfigured_preview'] = disabled
            self.assertEqual((disabled['snapshots']['eligible'], disabled['jobs']['eligible']), (0, 0))
            full = preview('--snapshot-days','30','--job-days','30','--sample-limit','100','--row-warning','1')
            record['configured_preview'] = full
            self.assertEqual({r['id'] for r in full['snapshots']['samples']}, set(seed['eligible_snapshots']))
            self.assertEqual({r['id'] for r in full['jobs']['samples']}, set(seed['eligible_jobs']))
            for kind in ('snapshots','jobs'):
                self.assertEqual(full[kind]['total'], full[kind]['protected'] + full[kind]['eligible'])
                self.assertTrue(full[kind]['row_warning_reached'])
                self.assertFalse(full[kind]['samples_truncated'])
            small = preview('--snapshot-days','30','--job-days','30','--sample-limit','1')
            self.assertTrue(small['snapshots']['samples_truncated'] and small['jobs']['samples_truncated'])
            self.assertEqual(len(small['snapshots']['samples']), 1);self.assertEqual(len(small['jobs']['samples']), 1)
            for args in (['--snapshot-days','0'],['--sample-limit','0'],['--apply']):
                result = command([sys.executable, '-B', '-m', 'liveday0', 'retention', tenant, '--dry-run', *args], login=client, check=False)
                self.assertNotEqual(result.returncode, 0)
            record['database_digest_before'] = before;record['database_digest_after'] = digest()
            self.assertEqual(record['database_digest_after'], before)
            record['checks'] = {'no_default_ttl':True,'exact_inactive_and_superseded_candidates':True,
                'active_and_unknown_time_snapshots_protected':True,'live_failed_latest_and_bookkeeping_jobs_protected':True,
                'latest_anchor_uuid_tie_break_matches_scheduler':True,'samples_bounded_tenant_scoped_no_body':True,
                'all_memory_journal_receipts_markers_and_operations_unchanged':True,'apply_flag_absent':True}
            record['status'] = 'passed'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('RETENTION_PREVIEW_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
