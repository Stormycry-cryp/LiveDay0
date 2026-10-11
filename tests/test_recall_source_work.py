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


class RecallSourceWorkTests(unittest.TestCase):
    def test_invalid_source_limit_before_database(self):
        sys.path.insert(0, str(REPO / 'src'))
        from liveday0.types import RecallOptions
        for value in (0, -1, True, 4097):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RecallOptions(source_reference_limit=value)

    def test_shared_source_reference_work_limit(self):
        root = Path(mkdtemp(prefix='ld0-source-work-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'src_owner_' + suffix, 'src_client_' + suffix, 'ld0_src_' + suffix
        marker = 'liveday0-source-work-' + suffix
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
            (root / 'pgdata/source-work-marker.txt').write_text(marker)
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
from pathlib import Path
from uuid import UUID,uuid4
from unittest.mock import patch
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from liveday0.exceptions import RecallLimitError
f=Path(__import__('tests').__file__).parent/'fixtures/recall_source_work_cases.json';fixture=json.loads(f.read_text())
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant();ids={};source_ids={}
for item in fixture['events']:
    ids[item['key']]=s.observe(EvidenceInput('text','synthetic',item['cue'],idempotency_key=item['key']),semantics=[SemanticInput('event',{'goal_context':item['cue'],'current_result':'固定合成结果','boundaries':'仅当次经历','uncertainty':'没有其他结论'},canonical_key=item['key'])])['card_ids'][0]
    with tenant_transaction(s.tenant_id,mode='read') as c:source_ids[item['key']]={str(r['evidence_id']) for r in c.execute('SELECT evidence_id FROM card_sources WHERE tenant_id=%s AND card_id=%s',(s.tenant_id,ids[item['key']]))}
    for n in range(item['sources']-1):
        eid=s.observe(EvidenceInput('text','synthetic','固定支持'+item['key']+str(n),idempotency_key=item['key']+':'+str(n)))['evidence_id']
        source_ids[item['key']].add(str(eid))
        with tenant_transaction(s.tenant_id) as c:c.execute('INSERT INTO card_sources(tenant_id,card_id,evidence_id) VALUES (%s,%s,%s)',(s.tenant_id,ids[item['key']],eid))
def life_digest():
    with tenant_transaction(s.tenant_id,mode='read') as c:
        return {table:sorted(json.dumps(dict(r),sort_keys=True,default=str) for r in c.execute('SELECT * FROM '+table)) for table in ['evidence','semantic_cards','semantic_card_versions','card_sources','event_deltas']}
def snapshot_count():
    with tenant_transaction(s.tenant_id,mode='read') as c:return c.execute('SELECT count(*) AS n FROM recall_snapshots WHERE tenant_id=%s',(s.tenant_id,)).fetchone()['n']
original_life=life_digest();configured=hasattr(RecallOptions,'source_reference_limit');rows_read=[]
original=s.recall_compiler._source_map
class Cursor:
    def __init__(self,value):self.value=value
    def fetchall(self):
        rows=self.value.fetchall();rows_read.append(len(rows));return rows
class Connection:
    def __init__(self,value):self.value=value
    def execute(self,*a,**kw):return Cursor(self.value.execute(*a,**kw))
def capture(conn,ids,**kw):return original(Connection(conn),ids,**kw)
checks=[]
for name,query,cap,expected_keys,should_deny in [('heavy_over','高连接散步事件',8,[],True),('heavy_exact','高连接散步事件',9,['heavy'],False),('shared_over','共同出行事件',7,[],True),('shared_exact','共同出行事件',8,['pair_a','pair_b'],False),('unrelated','完全无关的系统升级问题',1,[],False)]:
    issues=[];before=snapshot_count();start=len(rows_read)
    opts=RecallOptions(**({'source_reference_limit':cap} if configured else {}))
    context=None
    try:
        with patch.object(s.recall_compiler,'_source_map',capture):context=s.recall(query,options=opts)
    except RecallLimitError as error:
        if not should_deny or error.code!='recall_work_limit_exceeded' or error.reason!='source_reference_limit':issues.append('unexpected limit outcome')
        if snapshot_count()!=before:issues.append('over-limit snapshot published')
    else:
        if should_deny:issues.append('over-limit usable context returned')
        cards=list(context['layers']['events'])
        for h in context['expansion_handles']:
            if h['type']=='event':cards.append(s.expand_snapshot(UUID(context['snapshot']['id']),UUID(h['item_id'])))
        if not should_deny:
            if {c['id'] for c in cards}!={str(ids[k]) for k in expected_keys}:issues.append('wrong selected identities')
            for card in cards:
                item=next(i for i in fixture['events'] if str(ids[i['key']])==card['id'])
                if len(card['sources'])!=item['sources'] or {str(r['evidence_id']) for r in card['sources']}!=source_ids[item['key']] or not card['body']['uncertainty'] or not card['body']['boundaries']:issues.append('partial provenance/qualifier')
        s.release_snapshot(UUID(context['snapshot']['id']))
    if any(n>cap+1 for n in rows_read[start:]):issues.append('SQL source row transfer exceeded cap+1')
    if name=='unrelated' and rows_read[start:]:issues.append('unrelated sources loaded')
    checks.append({'id':name,'passed':not issues,'failures':issues,'cap':cap,'source_rows_read':rows_read[start:]})
cli=[sys.executable,'-B','-m','liveday0','recall',str(s.tenant_id),'高连接散步事件']
if configured:cli+=['--source-refs','8']
r=subprocess.run(cli,capture_output=True,text=True,timeout=5)
try:payload=json.loads(r.stdout)
except ValueError:payload={}
cli_pass=r.returncode==2 and payload.get('error')=='recall_work_limit_exceeded' and payload.get('reason')=='source_reference_limit' and payload.get('context_available') is False and len(r.stdout.encode())<=512
checks.append({'id':'actual_cli_static_limit','passed':cli_pass,'failures':[] if cli_pass else ['explicit shared source limit unavailable or not enforced']})
assert life_digest()==original_life
print(json.dumps({'restricted_flags':flags,'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'configured_limit_present':configured,'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'life_five_tables_unchanged':True,'source_rows_read':rows_read,'model_calls':0}))

"""))
            record['status'] = 'passed' if record['checks']['failed']==0 else 'missing_source_work_guard'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('SOURCE_WORK_EVIDENCE=' + str(root / 'manifest.json'))
        self.assertEqual(record['checks']['failed'], 0, json.dumps(record['checks']['cases'],ensure_ascii=False))


if __name__ == '__main__':
    unittest.main(verbosity=2)
