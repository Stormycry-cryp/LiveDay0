"""Explicit pinned-cycle release in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class ReleaseSnapshotTests(unittest.TestCase):
    def test_release_is_scoped_atomic_and_ordered_without_life_expiry(self):
        root = Path(mkdtemp(prefix='ld0-release-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'rel_owner_' + suffix, 'rel_client_' + suffix, 'ld0_rel_' + suffix
        marker = 'liveday0-release-snapshot-' + suffix
        env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
               'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null', 'PGSYSCONFDIR': str(root),
               'PYTHONPATH': str(REPO / 'src') + ':' + str(REPO), 'PYTHONDONTWRITEBYTECODE': '1'}
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
            (root / 'pgdata/release-marker.txt').write_text(marker)
            started = True
            command([PG / 'pg_ctl', '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
            sql(f'CREATE DATABASE {db};', 'postgres'); sql(f"COMMENT ON DATABASE {db} IS '{marker}';")
            proof = json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();"))
            self.assertEqual(proof, {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                'marker': marker, 'db': db, 'comment': marker, 'public_tables': 0})
            record['initial_empty_isolation_proof'] = proof
            command([sys.executable, '-B', '-m', 'liveday0', 'migrate', 'up'])
            sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {db} TO {client};')
            record['checks'] = json.loads(python(r"""
import hashlib,json,subprocess,sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID,uuid4
import pytest
import liveday0.recall as recall
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.exceptions import NotFound,SnapshotInvalidated
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from tests.test_trust_boundaries import ControlledTransactions,named
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant();foreign=MemoryService(uuid4());foreign.ensure_tenant()
card=s.observe(EvidenceInput('text','synthetic','synthetic completed-cycle memory',idempotency_key='release-source'),semantics=[SemanticInput('event',{'goal_context':'synthetic cycle','current_result':'still valid'},canonical_key='release-event')])['card_ids'][0]
def snapshot():
    return UUID(s.recall('synthetic completed-cycle memory')['snapshot']['id'])
def row(sid):
    with tenant_transaction(s.tenant_id,mode='read') as c:
        return c.execute('SELECT * FROM recall_snapshots WHERE tenant_id=%s AND id=%s',(s.tenant_id,sid)).fetchone()
def digest():
    result={}
    with tenant_transaction(s.tenant_id,mode='read') as c:
        result['tenant']=c.execute('SELECT * FROM tenants WHERE id=%s',(s.tenant_id,)).fetchall()
        for table in ('evidence','life_traces','semantic_cards','semantic_card_versions','event_deltas','card_sources','relations','projections','projection_versions','projection_supports','maintenance_jobs','deletion_markers','observation_receipts','interpretation_intents','source_interpretation_revocations','reobservation_intents'):
            result[table]=sorted(json.dumps(r,sort_keys=True,default=str) for r in c.execute('SELECT * FROM '+table+' WHERE tenant_id=%s',(s.tenant_id,)).fetchall())
    return hashlib.sha256(json.dumps(result,sort_keys=True,default=str).encode()).hexdigest()
first,other=snapshot(),snapshot()
assert s.expand_snapshot(first,card)['id']==str(card)
before=digest()
# Actual CLI process uses the already clean, explicitly constructed test environment.
cli=subprocess.run([sys.executable,'-B','-m','liveday0','release-snapshot',str(s.tenant_id),str(first)],capture_output=True,text=True,timeout=15)
assert cli.returncode==0,cli.stderr
assert json.loads(cli.stdout)=={'snapshot_id':str(first),'state':'expired','released':True}
closed=row(first)
assert closed['invalidated_at'] is not None
assert all(not closed[k] for k in ('context','expansion_store','referenced_ids','degraded_reasons'))
assert s.release_snapshot(first)['released'] is False and row(first)==closed
try:s.expand_snapshot(first,card)
except SnapshotInvalidated:pass
else:raise AssertionError('released expansion permitted')
assert s.expand_snapshot(other,card)['id']==str(card)
for service,sid in ((foreign,other),(s,uuid4())):
    try:service.release_snapshot(sid)
    except NotFound:pass
    else:raise AssertionError('foreign/missing snapshot accepted')
# A previously hard-invalidated cycle keeps its original state/time.
invalid=snapshot()
with tenant_transaction(s.tenant_id) as c:
    c.execute("UPDATE recall_snapshots SET state='invalidated',invalidated_at=now(),context='{}',expansion_store='{}',referenced_ids='{}',degraded_reasons='{}' WHERE tenant_id=%s AND id=%s",(s.tenant_id,invalid))
invalid_before=row(invalid)
assert s.release_snapshot(invalid)['state']=='invalidated' and row(invalid)==invalid_before
# Fail after the release SQL, before commit: no partial erasure survives.
failed=snapshot();failed_before=row(failed)
original=recall.tenant_transaction
@contextmanager
def fail_after_sql(*args,**kwargs):
    with original(*args,**kwargs) as c:
        yield c
        c.execute('SELECT 1/0')
with pytest.MonkeyPatch.context() as patch:
    patch.setattr(recall,'tenant_transaction',fail_after_sql)
    try:s.release_snapshot(failed)
    except Exception as error:
        from psycopg.errors import DivisionByZero
        assert isinstance(error,DivisionByZero)
    else:raise AssertionError('injected fault not reached')
assert row(failed)==failed_before and s.expand_snapshot(failed,card)['id']==str(card)
# Reuse the established gate instrumentation; prove each ordering with PG blockers.
locks=[]
for release_first in (False,True):
    sid=snapshot();first_action='release' if release_first else 'expand';second_action='expand' if release_first else 'release'
    def expand():
        try:return s.expand_snapshot(sid,card)
        except SnapshotInvalidated:return 'closed'
    actions={'release':lambda:s.release_snapshot(sid),'expand':expand}
    with pytest.MonkeyPatch.context() as patch:
        control=ControlledTransactions(patch,first_action,'SELECT state',after=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(named,first_action,actions[first_action])
            try:
                assert control.ready.wait(5)
                b=pool.submit(named,second_action,actions[second_action])
                control.wait_blocked(second_action,first_action)
            finally:control.release.set()
            results={first_action:a.result(timeout=10),second_action:b.result(timeout=10)}
        assert results['release']['released'] is True
        assert results['expand']=='closed' if release_first else results['expand']['id']==str(card)
        locks.extend(control.observations)
assert len(locks)==2
fresh=snapshot()
assert s.expand_snapshot(fresh,card)['body']['current_result']=='still valid'
assert digest()==before
print(json.dumps({'restricted_flags':flags,'actual_cli_release':True,'closed_copy_empty':True,'repeat_and_invalidated_noop':True,'other_active_and_foreign_scope':True,'sql_failure_rolls_back':True,'expand_release_pg_blockers':locks,'life_memory_and_revision_digest_before':before,'life_memory_and_revision_digest_after':digest(),'new_recall_preserves_memory':True}))
"""))
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
        print('RELEASE_SNAPSHOT_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
