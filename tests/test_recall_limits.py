"""Complete-wire budget and actual deadline in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class RecallLimitTests(unittest.TestCase):
    def test_unresolved_database_address_is_rejected_before_io(self):
        sys.path.insert(0, str(REPO / 'src'))
        from unittest.mock import patch
        from liveday0.db import connect
        from liveday0.exceptions import RecallDeadlineUnavailable
        from liveday0.recall_limits import deadline_scope, current_deadline
        with patch('liveday0.db.database_url', return_value='dbname=synthetic host=example.invalid'):
            with deadline_scope(40):
                with self.assertRaises(RecallDeadlineUnavailable):
                    connect()
        self.assertIsNone(current_deadline())

    def test_complete_wire_limits_and_shared_deadline(self):
        root = Path(mkdtemp(prefix='ld0-limits-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'lim_owner_' + suffix, 'lim_client_' + suffix, 'ld0_lim_' + suffix
        marker = 'liveday0-recall-limits-' + suffix
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
            (root / 'pgdata/limits-marker.txt').write_text(marker)
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
import json,socket,threading,time,subprocess,sys,os
from pathlib import Path
from uuid import UUID,uuid4
from unittest.mock import patch
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from liveday0.exceptions import RecallBudgetExceeded,RecallDeadlineExceeded
from liveday0.recall_limits import wire_bytes,recall_json,current_deadline
from liveday0.config import database_url
from tests.test_core_invariants import test_card_and_context_budgets_omit_whole_units_with_expansion
s=MemoryService(uuid4());s.ensure_tenant()
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
event=s.observe(EvidenceInput('text','synthetic','bounded_life source',idempotency_key='bounded-event'),semantics=[SemanticInput('event',{'goal_context':'bounded_life','current_result':'synthetic result','boundaries':'fixture only','uncertainty':'fixture qualified','detail':'BODY_CANARY '+('合成'*12000)},canonical_key='bounded-life')])['card_ids'][0]
normal=s.observe(EvidenceInput('text','synthetic','bounded_life recent',idempotency_key='bounded-normal'),semantics=[SemanticInput('event',{'goal_context':'bounded_life','current_result':'normal'},canonical_key='bounded-normal')])['card_ids'][0]
for i in range(40):
    source=s.observe(EvidenceInput('text','synthetic','support '+str(i),idempotency_key='support '+str(i)))['evidence_id']
    with tenant_transaction(s.tenant_id) as c:c.execute('INSERT INTO card_sources(tenant_id,card_id,evidence_id) VALUES (%s,%s,%s)',(s.tenant_id,event,source))
def snapshot_count():
    with tenant_transaction(s.tenant_id,mode='read') as c:return c.execute('SELECT count(*) AS n FROM recall_snapshots WHERE tenant_id=%s',(s.tenant_id,)).fetchone()['n']
context=s.recall('bounded_life',options=RecallOptions(context_token_limit=350,context_byte_limit=4096))
assert wire_bytes(context)==context['budget']['payload_bytes']<=4096
assert context['budget']['estimated_tokens']==(len(recall_json(context))+4)//4<=350
assert context['budget']['status']=='reduced' and context['degraded']
assert {h['item_id'] for h in context['expansion_handles']}=={str(event)}
assert str(event) not in {c['id'] for c in context['layers']['events']}
sid=UUID(context['snapshot']['id'])
for options in (RecallOptions(context_byte_limit=1),RecallOptions(context_token_limit=1)):
    before=snapshot_count()
    try:s.recall('bounded_life',options=options)
    except RecallBudgetExceeded as error:assert error.reason=='complete_reference_envelope_limit'
    else:raise AssertionError('tiny reference envelope accepted')
    assert snapshot_count()==before and current_deadline() is None
for limit in (128,65536):
    try:s.expand_snapshot(sid,event,options=RecallOptions(expansion_byte_limit=limit))
    except RecallBudgetExceeded as error:assert error.reason=='complete_expansion_byte_limit'
    else:raise AssertionError('oversized expansion accepted')
full=s.expand_snapshot(sid,event,options=RecallOptions(expansion_byte_limit=131072))
assert len(full['sources'])==41 and len({r['evidence_id'] for r in full['sources']})==41
assert full['body']['boundaries']=='fixture only' and full['body']['uncertainty']=='fixture qualified'
assert 'BODY_CANARY' in full['body']['detail'] and wire_bytes(full)<=131072
cli=subprocess.run([sys.executable,'-B','-m','liveday0','recall',str(s.tenant_id),'bounded_life'],capture_output=True,text=True,timeout=5)
assert cli.returncode==0,cli.stderr
parsed=json.loads(cli.stdout)
assert len(cli.stdout.encode())==parsed['budget']['payload_bytes']<=16384
failure=subprocess.run([sys.executable,'-B','-m','liveday0','expand-snapshot',str(s.tenant_id),str(sid),str(event),'--max-bytes','128'],capture_output=True,text=True,timeout=5)
assert failure.returncode==2 and json.loads(failure.stdout)['error']=='recall_budget_exceeded'
assert 'BODY_CANARY' not in failure.stdout and len(failure.stdout.encode())<=512
# A single real pg_sleep and two sequential sleeps use one remaining budget.
elapsed={}
for name,sleeps,budget in [('slow_sql',[.2],40),('cumulative_sql',[.025,.025],40)]:
    def slow(c,q,n):
        for delay in sleeps:c.execute('SELECT pg_sleep(%s)',(delay,))
        return set()
    before=snapshot_count();begin=time.monotonic()
    with patch.object(s.recall_compiler,'_fts_card_ids',slow):
        try:s.recall('bounded_life',options=RecallOptions(timeout_ms=budget))
        except RecallDeadlineExceeded:pass
        else:raise AssertionError('actual SQL deadline missed')
    elapsed[name]=(time.monotonic()-begin)*1000
    assert elapsed[name]<500 and snapshot_count()==before and current_deadline() is None
# The deadline is installed before acquiring the tenant gate, including catch-up.
with tenant_transaction(s.tenant_id) as holder:
    begin=time.monotonic()
    try:s.recall('bounded_life',options=RecallOptions(timeout_ms=40))
    except RecallDeadlineExceeded:pass
    else:raise AssertionError('tenant gate waited indefinitely')
    elapsed['tenant_gate']=(time.monotonic()-begin)*1000
    assert elapsed['tenant_gate']<500
    denied=subprocess.run([sys.executable,'-B','-m','liveday0','recall',str(s.tenant_id),'bounded_life','--timeout-ms','40'],capture_output=True,text=True,timeout=5)
    assert denied.returncode==2 and json.loads(denied.stdout)['error']=='recall_deadline_exceeded'
# Python work cooperates at checkpoints: no late context, not a hard realtime claim.
before=snapshot_count();original=s.recall_compiler._relevance
with patch.object(s.recall_compiler,'_relevance',side_effect=lambda *args:(time.sleep(.07),original(*args))[1]):
    begin=time.monotonic()
    try:s.recall('bounded_life',options=RecallOptions(timeout_ms=30))
    except RecallDeadlineExceeded:pass
    else:raise AssertionError('late CPU context returned')
    elapsed['python_checkpoint']=(time.monotonic()-begin)*1000
assert snapshot_count()==before and current_deadline() is None
# Bounded nonblocking connection polling, no DNS or TCP: own synthetic socket stalls auth.
root=Path.cwd();fake=root/'fake';fake.mkdir(mode=0o700);server=socket.socket(socket.AF_UNIX);server.bind(str(fake/'.s.PGSQL.5432'));server.listen(1);server.settimeout(1)
stop=threading.Event()
def stall():
    try:
        client,_=server.accept()
        with client:stop.wait(1)
    except (OSError,TimeoutError):pass
thread=threading.Thread(target=stall,daemon=True);thread.start()
try:
    with patch.dict(os.environ,{'LIVEDAY0_DATABASE_URL':f'dbname=synthetic user=synthetic host={fake}'}):
        begin=time.monotonic()
        try:s.recall('bounded_life',options=RecallOptions(timeout_ms=80))
        except RecallDeadlineExceeded as error:assert error.reason=='connect'
        else:raise AssertionError('stalled connection did not stop')
        elapsed['connection']=(time.monotonic()-begin)*1000
        assert elapsed['connection']<500
finally:stop.set();server.close();thread.join(2)
assert not thread.is_alive() and current_deadline() is None
# A timed-out catch-up must not consume the durable synthesis failure budget.
s.add_event_delta(normal,EvidenceInput('text','synthetic','unsafe delta',idempotency_key='unsafe delta'),{'current_result':'changed','requires_restructure':True},idempotency_key='unsafe delta')
with tenant_transaction(s.tenant_id,mode='read') as c:
    prior=c.execute("SELECT id,state,attempts,failure_count FROM maintenance_jobs WHERE tenant_id=%s AND target_id=%s AND job_type='event_rewrite'",(s.tenant_id,normal)).fetchone()
    version=c.execute('SELECT current_version FROM semantic_cards WHERE tenant_id=%s AND id=%s',(s.tenant_id,normal)).fetchone()['current_version']
def timeout_rewrite(c,j):c.execute('SELECT pg_sleep(.2)');return 'not reached'
with patch.object(s.maintenance,'_rewrite_event',timeout_rewrite):
    try:s.recall('bounded_life',options=RecallOptions(timeout_ms=40))
    except RecallDeadlineExceeded:pass
    else:raise AssertionError('catch-up swallowed deadline')
with tenant_transaction(s.tenant_id,mode='read') as c:
    assert c.execute('SELECT id,state,attempts,failure_count FROM maintenance_jobs WHERE tenant_id=%s AND id=%s',(s.tenant_id,prior['id'])).fetchone()==prior
    assert c.execute('SELECT current_version FROM semantic_cards WHERE tenant_id=%s AND id=%s',(s.tenant_id,normal)).fetchone()['current_version']==version
    assert c.execute("SELECT count(*) AS n FROM event_deltas WHERE tenant_id=%s AND event_id=%s AND state='pending'",(s.tenant_id,normal)).fetchone()['n']==1
# Only the affected existing semantic-card budget method; no global pytest fixtures.
legacy=MemoryService(uuid4());legacy.ensure_tenant();test_card_and_context_budgets_omit_whole_units_with_expansion(legacy)
print(json.dumps({'restricted_flags':flags,'exact_complete_wire_bytes_and_estimate':True,'all_omitted_handles_callable':True,'whole_provenance_41_sources_and_qualifiers':True,'tiny_envelope_no_snapshot':True,'oversized_expansion_rejected_whole':True,'actual_cli_bound_and_static_error_exit2':True,'deadline_observed_ms':elapsed,'sql_cumulative_and_gate_budget':True,'cooperative_python_no_late_context':True,'connection_polling_no_dns_tcp':True,'catchup_deadline_no_failure_budget_spent':True,'deadline_context_reset':True,'affected_existing_budget_method_passed':True}))
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
        print('RECALL_LIMIT_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
