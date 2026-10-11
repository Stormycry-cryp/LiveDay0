"""Shared projection-support and trace input row gate in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class RecallDerivedInputTests(unittest.TestCase):
    def test_shared_derived_input_limit(self):
        self._run_isolated(DERIVED_CASES)

    def test_invalid_limit_and_nested_cycle_without_io(self):
        sys.path.insert(0, str(REPO / 'src'))
        from liveday0.types import RecallOptions
        from liveday0.exceptions import RecallWorkLimitExceeded, RecallMaintenanceRequired
        from liveday0.recall_limits import (deadline_scope, consume_derived_inputs,
            derived_input_fetch_limit, pending_delta_fetch_limit, current_deadline)
        for value in (0, -1, True, 4097):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RecallOptions(derived_input_limit=value)
        with self.assertRaises(RecallWorkLimitExceeded) as terminal:
            with deadline_scope(1000, derived_input_limit=5, pending_delta_limit=1) as cycle:
                consume_derived_inputs(2)
                with deadline_scope(2000, derived_input_limit=100):
                    self.assertEqual(derived_input_fetch_limit(), 4)
                    consume_derived_inputs(3)
                self.assertEqual(derived_input_fetch_limit(), 1)
                with self.assertRaises(RecallWorkLimitExceeded):
                    consume_derived_inputs(1)
                with self.assertRaises(RecallWorkLimitExceeded):
                    pending_delta_fetch_limit()
                with self.assertRaises(RecallWorkLimitExceeded):
                    with deadline_scope(1000, derived_input_limit=100):
                        self.fail('sealed cycle reentered')
                with self.assertRaises(RecallWorkLimitExceeded):
                    cycle.remaining_ms('sql')
        self.assertEqual(terminal.exception.code, 'recall_work_limit_exceeded')
        self.assertEqual(terminal.exception.reason, 'derived_input_limit')
        self.assertNotIsInstance(terminal.exception, RecallMaintenanceRequired)
        self.assertIsNone(current_deadline())
        with deadline_scope(1000, derived_input_limit=6):
            self.assertEqual(derived_input_fetch_limit(), 7)

    def test_affected_unsafe_projection_gate(self):
        self._run_isolated(DERIVED_CASES.split('initial=life_digest()')[0] + UNSAFE_GATE_CASE)

    def _run_isolated(self, script):
        root = Path(mkdtemp(prefix='ld0-derived-input-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'src_owner_' + suffix, 'src_client_' + suffix, 'ld0_src_' + suffix
        marker = 'liveday0-derived-input-' + suffix
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
            (root / 'pgdata/derived-input-marker.txt').write_text(marker)
            started = True
            command([PG / 'pg_ctl', '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
            sql(f'CREATE DATABASE {db};', 'postgres'); sql(f"COMMENT ON DATABASE {db} IS '{marker}';")
            proof = json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();"))
            self.assertEqual(proof, {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                'marker': marker, 'db': db, 'comment': marker, 'public_tables': 0})
            record['initial_empty_isolation_proof'] = proof
            command([sys.executable, '-B', '-m', 'liveday0', 'migrate', 'up'])
            sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {db} TO {client};')
            record['checks'] = json.loads(python(script))
            record['status'] = 'passed' if record['checks']['failed']==0 else 'missing_derived_input_guard'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('DERIVED_INPUT_EVIDENCE=' + str(root / 'manifest.json'))
        self.assertEqual(record['checks']['failed'], 0, json.dumps(record['checks']['cases'],ensure_ascii=False))


DERIVED_CASES = r"""
import hashlib,json,subprocess,sys
from pathlib import Path
from uuid import UUID,uuid4
from unittest.mock import patch
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from liveday0.exceptions import RecallLimitError,SnapshotInvalidated
from liveday0.recall_limits import current_deadline
from tests.helpers import make_projection
f=Path(__import__('tests').__file__).parent/'fixtures/recall_derived_input_cases.json';fixture=json.loads(f.read_text())
with connect() as c:flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant();ids={};traces={};source_ids={}
for item in fixture['events']:
    ids[item['key']]=s.observe(EvidenceInput('text','synthetic',item['cue'],idempotency_key=item['key']),semantics=[SemanticInput('event',{'goal_context':item['cue'],'current_result':item['result'],'boundaries':'仅合成当次','uncertainty':'不推出其他事实'},canonical_key=item['key'])])['card_ids'][0]
p=fixture['projection']
pid=make_projection(s,projection_type=p['type'],projection_key=p['key'],scope='synthetic only',support_card_ids=[ids[k] for k in p['support']],counter_card_ids=[ids[k] for k in p['counterevidence']],body={'summary':p['cue'],'boundaries':'仅合成三个事件的暂定视图','uncertainty':p['uncertainty']})
for item in fixture['traces']:
    receipt=s.observe(EvidenceInput('text','synthetic',item['observation'],idempotency_key=item['key']),trace={'observation':item['observation'],'observation_boundary':item['boundary']});traces[item['key']]=receipt['trace_id'];source_ids[item['key']]=receipt['evidence_id']
    with tenant_transaction(s.tenant_id) as c:
        for key in item['events']:c.execute("INSERT INTO relations(tenant_id,from_kind,from_id,to_kind,to_id,family,relation_type,lifecycle) VALUES (%s,'life_trace',%s,'semantic_card',%s,'evidence_support','supports_event','active')",(s.tenant_id,receipt['trace_id'],ids[key]))
def life_digest():
    with tenant_transaction(s.tenant_id,mode='read') as c:
        return {t:sorted(json.dumps(dict(r),sort_keys=True,default=str) for r in c.execute('SELECT * FROM '+t)) for t in ['evidence','life_traces','semantic_cards','semantic_card_versions','card_sources','event_deltas','relations','projections','projection_versions','projection_supports','maintenance_jobs']}
def snaps():
    with tenant_transaction(s.tenant_id,mode='read') as c:return c.execute('SELECT count(*) AS n FROM recall_snapshots').fetchone()['n']
reads=[];checks=[];configured=hasattr(RecallOptions,'derived_input_limit')
class Cursor:
    def __init__(self,value,kind,limit):self.value=value;self.kind=kind;self.limit=limit
    def fetchall(self):
        rows=self.value.fetchall()
        if self.kind=='old_support':n=sum(len(r['support_ids']) for r in rows)
        else:n=len(rows)
        reads.append({'kind':self.kind,'rows':n,'sql_limit':self.limit});return rows
    def __getattr__(self,k):return getattr(self.value,k)
class Conn:
    def __init__(self,value):self.value=value
    def execute(self,sql,params=None,*a,**kw):
        cursor=self.value.execute(sql,params,*a,**kw)
        if 'array_agg(ps.card_id)' in sql:return Cursor(cursor,'old_support',None)
        if 'SELECT projection_id,card_id,support_role' in sql:return Cursor(cursor,'support',params[-1])
        if 'SELECT DISTINCT lt.id' in sql:return Cursor(cursor,'trace',params[-1] if 'LIMIT %s' in sql else None)
        return cursor
    def __getattr__(self,k):return getattr(self.value,k)
orig_projection=s.recall_compiler._projection_cards;orig_trace=s.recall_compiler._trace_summary
def projection(c,*a,**kw):return orig_projection(Conn(c),*a,**kw)
def trace(c,*a,**kw):return orig_trace(Conn(c),*a,**kw)
def recall(cap,query='合成节律散步',deny=False):
    before=snaps();start=len(reads);issues=[];ctx=None
    try:
        with patch.object(s.recall_compiler,'_projection_cards',projection),patch.object(s.recall_compiler,'_trace_summary',trace):
            ctx=s.recall(query,options=RecallOptions(**({'derived_input_limit':cap} if configured else {})))
    except RecallLimitError as e:
        if not deny or e.code!='recall_work_limit_exceeded' or e.reason!='derived_input_limit':issues.append('wrong static input limit')
        if snaps()!=before:issues.append('rejected context snapshot committed')
    else:
        if deny:issues.append('over-limit usable context returned')
    remaining=cap
    for r in reads[start:]:
        if r['sql_limit']!=remaining+1 or r['rows']>remaining+1:issues.append('input SQL not remaining+1 bounded')
        remaining-=min(remaining,r['rows'])
    if current_deadline() is not None:issues.append('cycle leaked')
    return ctx,issues,reads[start:]
def items(ctx):
    result=[card for values in ctx['layers'].values() for card in values]
    for h in ctx['expansion_handles']:result.append(s.expand_snapshot(UUID(ctx['snapshot']['id']),UUID(h['item_id'])))
    return result
def check(name,issues,rows=()):checks.append({'id':name,'passed':not issues,'failures':issues,'input_reads':rows})
initial=life_digest()
for name,cap in [('support_over',2),('shared_exact_support_but_trace_over',3),('shared_over',5)]:
    ctx,issues,rows=recall(cap,deny=True)
    if name=='support_over' and any(r['kind']=='trace' for r in rows):issues.append('trace loaded after support overflow')
    if life_digest()!=initial:issues.append('rejection mutated eleven life tables')
    check(name,issues,rows)
ctx,issues,rows=recall(6)
if ctx:
    cs=items(ctx);projs=[c for c in cs if c['type']=='life_thread'];summ=[c for c in cs if c['type']=='evidence_summary']
    if len(projs)!=1 or set(projs[0]['support_refs'])!={str(i) for i in ids.values()} or projs[0]['body']['uncertainty']!=p['uncertainty'] or set(projs[0]['body']['counterevidence_versions'])!={str(ids['other'])}:issues.append('projection support/counterevidence incomplete')
    if len(summ)!=1 or summ[0]['count']!=3 or set(summ[0]['source_refs'])!={str(i) for i in source_ids.values()}:issues.append('trace identity/provenance incomplete or duplicated')
    elif {r['id'] for r in summ[0]['traces']}!={str(i) for i in traces.values()}:issues.append('trace identity incomplete or duplicated')
    else:
        records={r['id']:r for r in summ[0]['traces']}
        for item in fixture['traces']:
            record=records[str(traces[item['key']])]
            if record['evidence_id']!=str(source_ids[item['key']]) or not record['occurred_at'] or record['observation']!=item['observation'] or record['observation_boundary']!=item['boundary'] or record['type']!='life_trace' or record['epistemic_state']!='observed_only' or record['lifecycle']!='active':issues.append('trace source/time/qualifier pairing incomplete')
else:issues.append('larger finite allowance did not recover')
if life_digest()!=initial:issues.append('success mutated life inputs')
check('exact_full_recovery',issues,rows)
ctx,issues,rows=recall(1,'完全无关的设备升级问题')
if not ctx or items(ctx) or rows:issues.append('irrelevant history/fanout not empty')
check('unrelated_no_payload',issues,rows)
# Inject a real SQL failure after support payload reading and before publication.
before=snaps();issues=[]
def fault(c,*a,**kw):
    value=orig_projection(c,*a,**kw);c.execute('SELECT 1/0');return value
import psycopg
try:
    with patch.object(s.recall_compiler,'_projection_cards',fault):s.recall('合成节律散步',options=RecallOptions(**({'derived_input_limit':6} if configured else {})))
except psycopg.errors.DivisionByZero:pass
else:issues.append('real SQL failure not raised')
if snaps()!=before or life_digest()!=initial:issues.append('fault published snapshot or mutated inputs')
check('sql_failure_atomic',issues)
ctx,issues,rows=recall(6)
if not ctx or len([x for x in items(ctx) if x['type']=='evidence_summary'])!=1:issues.append('restored path failed recovery')
check('sql_failure_recovery',issues,rows)
argv=[sys.executable,'-B','-m','liveday0','recall',str(s.tenant_id),'合成节律散步']+(['--derived-inputs','5'] if configured else [])
r=subprocess.run(argv,capture_output=True,text=True,timeout=5)
try:out=json.loads(r.stdout)
except ValueError:out={}
issues=[]
if not(r.returncode==2 and out.get('error')=='recall_work_limit_exceeded' and out.get('reason')=='derived_input_limit' and out.get('context_available') is False and len(r.stdout.encode())<=512):issues.append('actual CLI static rejection unavailable')
if life_digest()!=initial:issues.append('CLI mutated input tables')
check('actual_cli_static',issues)
# Existing hard invalidation semantics after successful context; no resurrection.
old=s.recall('合成节律散步');sid=UUID(old['snapshot']['id'])
s.correct_card(ids['walk_a'],EvidenceInput('text','synthetic','明确纠正合成散步',idempotency_key='correct'),{'goal_context':'合成节律散步','current_result':'明确纠正','boundaries':'仅修正当次','uncertainty':'无外推'},expected_version=1)
s.delete_evidence(source_ids['trace_a'])
ctx,issues,rows=recall(2)
if ctx:
    cs=items(ctx);summary=[x for x in cs if x['type']=='evidence_summary']
    if any(x['id']==str(pid) for x in cs) or len(summary)!=1 or set(summary[0]['source_refs'])!={str(source_ids['trace_b']),str(source_ids['trace_c'])} or summary[0]['count']!=2:issues.append('invalid projection/deleted trace revived or remaining provenance incomplete')
try:s.expand_snapshot(sid,ids['walk_a']);issues.append('old invalid snapshot expanded')
except SnapshotInvalidated:pass
check('invalidated_deleted_inputs_excluded',issues,rows)
print(json.dumps({'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'restricted_flags':flags,'configured_limit_present':configured,'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'input_reads':reads,'eleven_life_tables_unchanged_before_explicit_correction_deletion':True,'model_calls':0}))

"""

UNSAFE_GATE_CASE = r"""
# Reuse the same fixed synthetic setup; exercise the changed unsafe eligibility SQL.
s.add_event_delta(ids['walk_a'],EvidenceInput('text','synthetic','合成结构修正',idempotency_key='unsafe'),{'current_result':'新状态','requires_restructure':True},idempotency_key='unsafe')
# Fault-inject a stale projection marked active, so its existing unsafe-support guard must still hold.
with tenant_transaction(s.tenant_id) as c:c.execute("UPDATE projections SET lifecycle='active' WHERE id=%s",(pid,))
before=life_digest()
with patch.object(s.maintenance,'catch_up_unsafe_overlays',lambda:[]):ctx,issues,rows=recall(2)
if not ctx:issues.append('unaffected bounded context unavailable')
else:
    cs=items(ctx)
    if any(x['id']==str(pid) or x['id']==str(ids['walk_a']) for x in cs) or 'unsafe_events_not_caught_up' not in ctx['degraded_reasons']:issues.append('unsafe canonical or dependent projection served')
    summary=[x for x in cs if x['type']=='evidence_summary']
    if len(summary)!=1 or set(summary[0]['source_refs'])!={str(source_ids['trace_a']),str(source_ids['trace_c'])}:issues.append('unaffected trace provenance incomplete')
if rows!=[{'kind':'trace','rows':2,'sql_limit':3}] or life_digest()!=before:issues.append('unsafe support payload loaded or life tables modified')
check('affected_unsafe_projection_gate',issues,rows)
print(json.dumps({'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'restricted_flags':flags,'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'model_calls':0}))

"""

if __name__ == "__main__":
    unittest.main(verbosity=2)
