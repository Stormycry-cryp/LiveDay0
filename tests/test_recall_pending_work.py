"""Bounded selected-source row transfer in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class RecallPendingWorkTests(unittest.TestCase):
    def test_shared_pending_work_limit(self):
        self._run_isolated(PENDING_CASES)

    def test_shared_catchup_overlay_and_metadata(self):
        self._run_isolated(PENDING_CASES.split("s,ids=seed([3]);before=state(s)")[0] + SUPPLEMENT_CASES)

    def test_invalid_limits_and_nested_scope_without_io(self):
        sys.path.insert(0, str(REPO / 'src'))
        from liveday0.types import RecallOptions
        from liveday0.recall_limits import deadline_scope, consume_pending_deltas, pending_delta_fetch_limit, current_deadline, catch_up_target_limit, consume_catch_up_targets
        from liveday0.exceptions import RecallMaintenanceRequired
        for key, maximum in [('pending_delta_limit', 4096), ('catch_up_target_limit', 32)]:
            for value in (0, -1, True, maximum + 1):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    RecallOptions(**{key: value})
        with deadline_scope(1000, pending_delta_limit=3, catch_up_target_limit=2) as cycle:
            end = cycle.end
            consume_pending_deltas(1)
            with deadline_scope(2000, pending_delta_limit=10, catch_up_target_limit=8):
                self.assertEqual(pending_delta_fetch_limit(), 3)
                self.assertLessEqual(cycle.end, end)
                self.assertEqual(cycle.catch_up_target_limit, 2)
                consume_pending_deltas(2)
        with self.assertRaises(RecallMaintenanceRequired):
            with deadline_scope(1000, pending_delta_limit=3):
                with self.assertRaises(RecallMaintenanceRequired):
                    consume_pending_deltas(4)
                with self.assertRaises(RecallMaintenanceRequired):
                    pending_delta_fetch_limit()
                with self.assertRaises(RecallMaintenanceRequired):
                    with deadline_scope(1000, pending_delta_limit=10):
                        self.fail('exhausted cycle reentered')
        self.assertIsNone(current_deadline())
        with self.assertRaises(RecallMaintenanceRequired):
            with deadline_scope(1000, pending_delta_limit=3, catch_up_target_limit=2):
                consume_catch_up_targets(1)
                with deadline_scope(1000, catch_up_target_limit=8):
                    self.assertEqual(catch_up_target_limit(), 1)
                    consume_catch_up_targets(1)
                self.assertEqual(catch_up_target_limit(), 0)
                with self.assertRaises(RecallMaintenanceRequired):
                    consume_catch_up_targets(1)
                with self.assertRaises(RecallMaintenanceRequired):
                    pending_delta_fetch_limit()
        self.assertIsNone(current_deadline())
        with deadline_scope(1000, pending_delta_limit=3):
            self.assertEqual(pending_delta_fetch_limit(), 4)

    def _run_isolated(self, script):
        root = Path(mkdtemp(prefix='ld0-pending-work-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'src_owner_' + suffix, 'src_client_' + suffix, 'ld0_src_' + suffix
        marker = 'liveday0-pending-work-' + suffix
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
            (root / 'pgdata/pending-work-marker.txt').write_text(marker)
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
            record['status'] = 'passed' if record['checks']['failed']==0 else 'missing_pending_work_guard'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('PENDING_WORK_EVIDENCE=' + str(root / 'manifest.json'))
        self.assertEqual(record['checks']['failed'], 0, json.dumps(record['checks']['cases'],ensure_ascii=False))


PENDING_CASES = r"""
import hashlib,json,subprocess,sys
from pathlib import Path
from uuid import UUID,uuid4
from unittest.mock import patch
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from liveday0.exceptions import RecallLimitError,SnapshotInvalidated
from liveday0.recall_limits import current_deadline
f=Path(__import__('tests').__file__).parent/'fixtures/recall_pending_work_cases.json'
with connect() as c:flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
configured=hasattr(RecallOptions,'pending_delta_limit');checks=[];row_reads=[]
def seed(counts,unsafe=False):
    s=MemoryService(uuid4());s.ensure_tenant();ids=[]
    for k,n in enumerate(counts):
        cid=s.observe(EvidenceInput('text','synthetic','合成散步经历',idempotency_key='base'+str(k)),semantics=[SemanticInput('event',{'goal_context':'合成散步经历','current_result':'初始','boundaries':'仅合成当次','uncertainty':'无额外结论'},canonical_key='event'+str(k))])['card_ids'][0];ids.append(cid)
        for j in range(n):s.add_event_delta(cid,EvidenceInput('text','synthetic','合成进展'+str(j),idempotency_key=f'e{k}d{j}'),{'current_result':'进展'+str(j),'requires_restructure':unsafe},idempotency_key=f'k{k}d{j}')
    return s,ids

def state(s):
    with tenant_transaction(s.tenant_id,mode='read') as c:
        return {'cards':[(str(r['id']),r['current_version'],r['lifecycle']) for r in c.execute('SELECT id,current_version,lifecycle FROM semantic_cards ORDER BY id')],
          'deltas':[(str(r['id']),r['state']) for r in c.execute('SELECT id,state FROM event_deltas ORDER BY id')],
          'jobs':[(str(r['id']),r['state'],r['attempts'],r['failure_count']) for r in c.execute('SELECT id,state,attempts,failure_count FROM maintenance_jobs ORDER BY id')],
          'snapshots':c.execute('SELECT count(*) AS n FROM recall_snapshots').fetchone()['n']}
class Cursor:
    def __init__(self,value,limit):self.value=value;self.limit=limit
    def fetchall(self):
        rows=self.value.fetchall();row_reads.append({'rows':len(rows),'sql_limit':self.limit});return rows
    def __getattr__(self,key):return getattr(self.value,key)
class Conn:
    def __init__(self,value):self.value=value
    def execute(self,sql,params=None,*a,**kw):
        cursor=self.value.execute(sql,params,*a,**kw)
        if 'FROM event_deltas' in sql and (('SELECT * FROM event_deltas' in sql) or ('SELECT evidence_id, delta' in sql)):
            return Cursor(cursor,params[-1] if 'LIMIT %s' in sql else None)
        return cursor
    def __getattr__(self,key):return getattr(self.value,key)
def recall(s,cap,targets=8,deny=False):
    before=state(s);start=len(row_reads);issues=[];ctx=None
    card_orig=s.recall_compiler._card_from_row;rewrite_orig=s.maintenance._rewrite_event
    def card(c,*a):return card_orig(Conn(c),*a)
    def rewrite(c,*a):return rewrite_orig(Conn(c),*a)
    try:
        with patch.object(s.recall_compiler,'_card_from_row',card),patch.object(s.maintenance,'_rewrite_event',rewrite):
            ctx=s.recall('合成散步经历',options=RecallOptions(**({'pending_delta_limit':cap,'catch_up_target_limit':targets} if configured else {})))
    except RecallLimitError as e:
        if not deny or e.code!='recall_maintenance_required' or not getattr(e,'retryable',False) or not getattr(e,'maintenance_required',False):issues.append('wrong limit result')
        if state(s)['snapshots']!=before['snapshots']:issues.append('overflow snapshot published')
    else:
        if deny:issues.append('overflow usable context returned')
    remaining=cap
    for r in row_reads[start:]:
        if r['sql_limit']!=remaining+1 or r['rows']>remaining+1:issues.append('pending SQL transfer not remaining+1 bounded')
        remaining-=min(remaining,r['rows'])
    if current_deadline() is not None:issues.append('scope leaked')
    return ctx,issues

def cards(s,ctx):
    result=list(ctx['layers']['events'])
    for h in ctx['expansion_handles']:
        if h['type']=='event':result.append(s.expand_snapshot(UUID(ctx['snapshot']['id']),UUID(h['item_id'])))
    return result

def check(name,issues):checks.append({'id':name,'passed':not issues,'failures':issues})
s,ids=seed([3]);before=state(s);ctx,issues=recall(s,2,deny=True)
if state(s)!=before:issues.append('safe overflow mutated journal/version/job/snapshot')
check('safe_over',issues)
ctx,issues=recall(s,3)
if ctx:
    cs=cards(s,ctx)
    if len(cs)!=1 or cs[0]['body'].get('current_result')!='进展2' or len(cs[0]['pending_source_ids'])!=3 or not cs[0]['pending'] or cs[0]['version']!=1 or len(cs[0]['sources'])!=4:issues.append('incomplete safe overlay')
check('safe_exact',issues)
s,ids=seed([2,2]);before=state(s);ctx,issues=recall(s,3,deny=True)
if state(s)!=before:issues.append('shared overflow mutated life/job/snapshot')
check('shared_over',issues)
ctx,issues=recall(s,4)
if ctx:
    cs=cards(s,ctx)
    if len(cs)!=2 or any(len(c['pending_source_ids'])!=2 or c['body'].get('current_result')!='进展1' for c in cs):issues.append('incomplete shared overlay')
check('shared_exact',issues)
s,ids=seed([3],True);before=state(s);ctx,issues=recall(s,2,deny=True)
if state(s)!=before:issues.append('unsafe overflow changed pending/version/failure budget')
check('unsafe_over',issues)
ctx,issues=recall(s,3)
if ctx:
    cs=cards(s,ctx)
    if len(cs)!=1 or cs[0]['version']!=2 or cs[0]['pending'] or cs[0]['body'].get('current_result')!='进展2' or len(cs[0]['sources'])!=4 or any(st!='absorbed' for _,st in state(s)['deltas']):issues.append('incomplete atomic rewrite')
check('unsafe_exact',issues)
s,ids=seed([1,1],True);before=state(s);ctx,issues=recall(s,2,targets=1,deny=True)
if state(s)!=before:issues.append('target overflow claimed or altered jobs')
check('targets_over',issues)
ctx,issues=recall(s,2,targets=2)
if ctx and (len(cards(s,ctx))!=2 or any(c['version']!=2 for c in cards(s,ctx))):issues.append('two target catchup incomplete')
check('targets_exact',issues)
# An unrelated due candidate and safe rewrite must remain untouched during read-before-use.
s,ids=seed([1],True);extra=s.observe(EvidenceInput('text','synthetic','合成无关资料',idempotency_key='unrelated'))['evidence_id'];s.maintenance.enqueue_candidate_discovery(extra)
safe=s.observe(EvidenceInput('text','synthetic','合成无关散步',idempotency_key='safe'),semantics=[SemanticInput('event',{'goal_context':'不同经历','current_result':'原状态'},canonical_key='safe')])['card_ids'][0]
s.add_event_delta(safe,EvidenceInput('text','synthetic','无关进展',idempotency_key='safe-d'),{'current_result':'新状态'},idempotency_key='safe-d');s.maintenance.make_pending_ready()
with tenant_transaction(s.tenant_id,mode='read') as c:before_other=[dict(r) for r in c.execute('SELECT * FROM maintenance_jobs WHERE target_id<>%s ORDER BY id',(ids[0],))]
ctx,issues=recall(s,1)
with tenant_transaction(s.tenant_id,mode='read') as c:after_other=[dict(r) for r in c.execute('SELECT * FROM maintenance_jobs WHERE target_id<>%s ORDER BY id',(ids[0],))]
if before_other!=after_other:issues.append('unselected due maintenance executed')
check('no_opportunistic_jobs',issues)
s,ids=seed([3],True);orig=s.maintenance._rewrite_event
called=[]
def fault(c,j):
    called.append(True);orig(c,j);c.execute('SELECT 1/0')
with patch.object(s.maintenance,'_rewrite_event',fault):ctx=s.recall('合成散步经历',options=RecallOptions(**({'pending_delta_limit':3} if configured else {})))
st=state(s);issues=[]
if not called or st['cards'][0][1]!=1 or any(v!='pending' for _,v in st['deltas']) or st['jobs'][0][1:]!=('retry',1,1) or cards(s,ctx):issues.append('real SQL failure rollback/degraded context failed')
check('sql_failure_preserves_pending',issues)
s.maintenance.make_retries_ready();ctx,issues=recall(s,3)
if not ctx or cards(s,ctx)[0]['version']!=2 or any(v!='absorbed' for _,v in state(s)['deltas']) or state(s)['jobs'][0][3]!=1:issues.append('finite retry recovery failed')
check('sql_retry_recovers',issues)
for action in ('correction','deletion'):
    s,ids=seed([0]);old=s.recall('合成散步经历');sid=UUID(old['snapshot']['id'])
    for j in range(3):s.add_event_delta(ids[0],EvidenceInput('text','synthetic','合成待改进',idempotency_key='d'+str(j)),{'current_result':'待更正','requires_restructure':True},idempotency_key='d'+str(j))
    ctx,issues=recall(s,2,deny=True)
    # Baseline lacks the cap and has already rewritten: record it, then use its actual version for continuation.
    version=state(s)['cards'][0][1]
    if action=='correction':s.correct_card(ids[0],EvidenceInput('text','synthetic','合成明确更正',idempotency_key='fix'),{'goal_context':'合成散步经历','current_result':'明确更正','boundaries':'仅合成更正','uncertainty':'无额外推断'},expected_version=version)
    else:s.delete_card(ids[0])
    ctx,postissues=recall(s,1);issues+=postissues
    cs=cards(s,ctx) if ctx else []
    if action=='correction' and (len(cs)!=1 or cs[0]['body'].get('current_result')!='明确更正' or any(v=='pending' for _,v in state(s)['deltas'])):issues.append('correction failed to invalidate pending')
    if action=='deletion':
        s.maintenance.run_ready(limit=1)
        if cs or state(s)['cards'][0][2]!='deleted':issues.append('deletion revived old target')
    try:s.expand_snapshot(sid,ids[0]);issues.append('invalid old snapshot usable')
    except SnapshotInvalidated:pass
    check(action+'_after_overflow',issues)
s,ids=seed([3],True)
argv=[sys.executable,'-B','-m','liveday0','recall',str(s.tenant_id),'合成散步经历']+(['--pending-deltas','2'] if configured else [])
r=subprocess.run(argv,capture_output=True,text=True,timeout=5)
try:out=json.loads(r.stdout)
except ValueError:out={}
issues=[]
if not (r.returncode==2 and out.get('error')=='recall_maintenance_required' and out.get('reason')=='pending_delta_limit' and out.get('retryable') is True and out.get('maintenance_required') is True and out.get('context_available') is False and len(r.stdout.encode())<=512):issues.append('static retryable CLI unavailable')
if any(v!='pending' for _,v in state(s)['deltas']):issues.append('CLI overflow lost pending')
check('actual_cli_static_retryable',issues)
print(json.dumps({'restricted_flags':flags,'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'configured_limit_present':configured,'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'row_reads':row_reads,'model_calls':0}))

"""

SUPPLEMENT_CASES = r"""
from contextlib import contextmanager
import liveday0.maintenance as maintenance_module
metadata=[];real_tx=maintenance_module.tenant_transaction
class MetaCursor:
    def __init__(self,value,cap):self.value=value;self.cap=cap
    def fetchall(self):
        rows=self.value.fetchall();metadata.append({'rows':len(rows),'limit':self.cap});return rows
class MetaConn:
    def __init__(self,value):self.value=value
    def execute(self,sql,params=None,*a,**kw):
        value=self.value.execute(sql,params,*a,**kw)
        return MetaCursor(value,params[-1]) if 'SELECT j.id,j.target_id' in sql else value
    def __getattr__(self,k):return getattr(self.value,k)
@contextmanager
def metadata_tx(*a,**kw):
    with real_tx(*a,**kw) as conn:yield MetaConn(conn)
s,ids=seed([1,1],True);before=state(s)
with patch.object(maintenance_module,'tenant_transaction',metadata_tx):ctx,issues=recall(s,2,targets=1,deny=True)
if metadata!=[{'rows':2,'limit':2}] or state(s)!=before:issues.append('target sentinel metadata transfer or no-claim failed')
check('actual_target_metadata_sentinel',issues)
s,ids=seed([1,2]);
# Make only the first event unsafe; preserve real journal and job coalescing.
from psycopg.types.json import Jsonb
with tenant_transaction(s.tenant_id) as c:c.execute("UPDATE event_deltas SET delta=delta||%s WHERE event_id=%s AND state='pending'",(Jsonb({'requires_restructure':True}),ids[0]))
before=state(s);ctx,issues=recall(s,2,deny=True)
after=state(s)
if after['snapshots']!=before['snapshots'] or next(v for i,v,_ in after['cards'] if i==str(ids[0]))!=2:issues.append('earlier lawful catchup commitment not represented')
with tenant_transaction(s.tenant_id,mode='read') as c:
    counts={r['event_id']:r['n'] for r in c.execute("SELECT event_id,count(*) AS n FROM event_deltas WHERE state='pending' GROUP BY event_id")}
if counts!={ids[1]:2}:issues.append('shared rewrite-overlay overflow lost pending')
check('shared_rewrite_overlay_budget',issues)
ctx,issues=recall(s,3)
if not ctx or len(cards(s,ctx))!=2 or next(c for c in cards(s,ctx) if c['id']==str(ids[1]))['body'].get('current_result')!='进展1':issues.append('fresh bounded overlay retry failed')
check('shared_budget_new_cycle_recovers',issues)
print(json.dumps({'restricted_flags':flags,'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'row_reads':row_reads,'metadata_reads':metadata,'model_calls':0}))

"""

if __name__ == "__main__":
    unittest.main(verbosity=2)
