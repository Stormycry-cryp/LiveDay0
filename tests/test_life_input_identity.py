"""Distinct near-duplicate lived events through actual reviewed host in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class LifeInputIdentityTests(unittest.TestCase):
    def test_distinct_events_and_localized_correction(self):
        root = Path(mkdtemp(prefix='ld0-life-input-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'input_owner_' + suffix, 'input_client_' + suffix, 'ld0_input_' + suffix
        marker = 'liveday0-life-input-' + suffix
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
            (root / 'pgdata/input-marker.txt').write_text(marker)
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
import hashlib,json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import UUID,uuid4
from liveday0 import extraction_contract as c
from liveday0.local_host import LocalMemorySession
from liveday0.db import connect,tenant_transaction
from liveday0.exceptions import AuthorizationRequired
from tests.test_atomic_writes import snapshot
f=Path(__import__('tests').__file__).parent/'fixtures/life_input_identity_cases.json'
fixture=json.loads(f.read_text());items={i['id']:i for i in fixture['cases']}
with connect() as conn:flags=conn.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
class FixedCandidateAdapter:
    production_ready=False
    def extract(self,r):
        item=next(i for i in fixture['cases'] if i['source']==r.content)
        key=fixture['shared_model_key']
        p=c.SemanticProposal('fixed-offline-candidate',c.ProposalSource(r.source.source_id,r.source.source_version,r.content_digest,c.SourceSpan.from_text(r.chunk_start,r.content)),'user',c.SubjectRef('user','current-local-user'),'direct_statement','event',c.ProposalScope('durable','synthetic one visit'),1.0,'asserted','ordinary','implicit_candidate',c.RevisionRef('create',key),c.ProducerMetadata('offline-identity','manual-fixtures','1.0.0','1.0.0'),c.ProposedSemanticInput('event',deepcopy(item['body']),'active','confirmed',key,r.occurred_at))
        return c.ProposalBatch(r.fingerprint,(p,))
approvals=[]
def confirm(check):
    payload=check.payload
    # Scripted human decisions are externally fixed; no product truth validator.
    item=next(i for i in fixture['cases'] if i['source']==payload['source']['content'])
    assert payload.get('semantic',payload.get('replacement'))['body']==item['body']
    assert check.action==('correct' if item.get('action')=='correct' else 'save-candidate')
    if check.action=='save-candidate':assert payload['proposal']['subject']['kind']=='user'
    approvals.append({'id':item['id'],'action':check.action,'decision':item['decision']})
    return item['decision']=='approve'
s=LocalMemorySession(adapter=FixedCandidateAdapter(),confirm=confirm,review_candidates=True)
receipts={};commands={}
def command(item):return {'action':item.get('action','capture'),'text':item['source'],'message_id':str(uuid4()),'occurred_at':item['date']}
for name in ['mother_visit_oct1','my_visit_oct2','mother_visit_oct3']:
    commands[name]=command(items[name]);receipts[name]=s.handle(commands[name])['outcomes'][0]
assert len({str(r['card_ids'][0]) for r in receipts.values()})==3
assert len({str(r['evidence_id']) for r in receipts.values()})==3
before=snapshot(s._service)
replay=s.handle(commands['mother_visit_oct1'])['outcomes'][0]
assert replay['card_ids']==receipts['mother_visit_oct1']['card_ids'] and not replay['created'] and snapshot(s._service)==before
rejected=command(items['confused_role']);before=snapshot(s._service)
try:s.handle(rejected)
except AuthorizationRequired:pass
else:raise AssertionError('scripted rejection did not stop wrong-role candidate')
assert snapshot(s._service)==before

def card(name):
    cid=receipts[name]['card_ids'][0]
    with tenant_transaction(s.tenant_id,mode='read') as conn:
        row=conn.execute('SELECT c.id,c.current_version,c.valid_at,c.canonical_key,v.body FROM semantic_cards c JOIN semantic_card_versions v ON v.tenant_id=c.tenant_id AND v.card_id=c.id AND v.version=c.current_version WHERE c.tenant_id=%s AND c.id=%s',(s.tenant_id,cid)).fetchone()
        row['sources']=conn.execute('SELECT cs.evidence_id,e.content,e.occurred_at FROM card_sources cs JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id WHERE cs.tenant_id=%s AND cs.card_id=%s ORDER BY e.id',(s.tenant_id,cid)).fetchall()
        return row
initial={name:card(name) for name in receipts}
for name,row in initial.items():
    assert row['body']==items[name]['body'] and row['valid_at']==datetime.fromisoformat(items[name]['date']) and len(row['sources'])==1
    assert row['sources'][0]['content']==items[name]['source'] and row['sources'][0]['occurred_at']==datetime.fromisoformat(items[name]['date']) and row['canonical_key']!=fixture['shared_model_key']
before_context=s.handle({'action':'query','query':'复诊'})['context']
assert {c['id'] for c in before_context['layers']['events']}=={str(r['card_ids'][0]) for r in receipts.values()}
fix=command(items['correct_oct2']);fix.update(card_id=str(receipts['my_visit_oct2']['card_ids'][0]),expected_version=1)
s.handle(fix)
assert card('mother_visit_oct1')==initial['mother_visit_oct1'] and card('mother_visit_oct3')==initial['mother_visit_oct3']
corrected=card('my_visit_oct2');assert corrected['current_version']==2 and corrected['body']==items['correct_oct2']['body']
assert corrected['id']==initial['my_visit_oct2']['id'] and len(corrected['sources'])==2
with tenant_transaction(s.tenant_id,mode='read') as conn:
    old=conn.execute('SELECT state,context FROM recall_snapshots WHERE tenant_id=%s AND id=%s',(s.tenant_id,UUID(before_context['snapshot']['id']))).fetchone()
    assert old['state']=='invalidated'
context=s.handle({'action':'query','query':'复诊'})['context']
assert {c['id'] for c in context['layers']['events']}=={str(r['card_ids'][0]) for r in receipts.values()}
for returned in context['layers']['events']:
    name=next(k for k,r in receipts.items() if str(r['card_ids'][0])==returned['id'])
    expected=items['correct_oct2']['body'] if name=='my_visit_oct2' else items[name]['body']
    assert returned['body']==expected and returned['sources'] and returned['body']['boundaries'] and returned['body']['uncertainty']
s._service.release_snapshot(UUID(context['snapshot']['id']))
assert [a['id'] for a in approvals]==['mother_visit_oct1','my_visit_oct2','mother_visit_oct3','confused_role','correct_oct2']
print(json.dumps({'restricted_flags':flags,'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'three_model_key_collisions_stay_three_distinct_host_events':True,'person_roles_and_dates_preserved':True,'same_source_replay_zero_extra_write':True,'wrong_role_candidate_reaches_complete_review':True,'scripted_rejection_20_tables_unchanged':True,'explicit_correction_only_oct2':True,'previous_context_invalidated_new_sources_preserved':True,'approvals':approvals,'model_calls':0,'truth_detection':'scripted human review, not generic automatic semantic validation'}))

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
        print('LIFE_INPUT_IDENTITY_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
