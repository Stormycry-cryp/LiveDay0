"""Finite ordinary-trace to later explicit event life journey in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class OrdinaryTraceJourneyTests(unittest.TestCase):
    def test_life_journey(self):
        self._run_isolated(JOURNEY_CASES)

    def _run_isolated(self, script):
        root = Path(mkdtemp(prefix='ld0-ordinary-journey-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'src_owner_' + suffix, 'src_client_' + suffix, 'ld0_src_' + suffix
        marker = 'liveday0-ordinary-journey-' + suffix
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
            (root / 'pgdata/ordinary-journey-marker.txt').write_text(marker)
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
            record['status'] = 'passed' if record['checks']['failed']==0 else 'missing_trace_context_pairing'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('ORDINARY_JOURNEY_EVIDENCE=' + str(root / 'manifest.json'))
        self.assertEqual(record['checks']['failed'], 0, json.dumps(record['checks']['cases'],ensure_ascii=False))


JOURNEY_CASES = r"""
import hashlib,json
from pathlib import Path
from datetime import datetime
from uuid import UUID,uuid4
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
from liveday0.exceptions import DeletedSource,InterpretationRevoked,SnapshotInvalidated
from tests.test_atomic_writes import snapshot
f=Path(__import__('tests').__file__).parent/'fixtures/ordinary_trace_life_journey.json';fixture=json.loads(f.read_text())
with connect() as c:flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant();checks=[];receipts={};inputs={}
def source(item):return EvidenceInput('text','synthetic',item['text'],occurred_at=datetime.fromisoformat(item['at']),idempotency_key=item['key'])
def check(name,issues):checks.append({'id':name,'passed':not issues,'failures':issues})
def trace_state():
    with tenant_transaction(s.tenant_id,mode='read') as c:return {str(r['id']):dict(r) for r in c.execute('SELECT * FROM life_traces')}
def original_receipt(eid):
    with tenant_transaction(s.tenant_id,mode='read') as c:return dict(c.execute('SELECT * FROM observation_receipts WHERE evidence_id=%s',(eid,)).fetchone())
def expanded(ctx):
    items=[item for cards in ctx['layers'].values() for item in cards]
    for h in ctx['expansion_handles']:items.append(s.expand_snapshot(UUID(ctx['snapshot']['id']),UUID(h['item_id'])))
    return items
def recall(options=None):return s.recall(fixture['query'],options=options)
def pairing_issues(ctx,keys):
    summaries=[c for c in expanded(ctx) if c['type']=='evidence_summary'];issues=[]
    if len(summaries)!=1: return ['missing complete ordinary context']
    card=summaries[0];records=card.get('traces',[])
    expected={str(receipts[k]['trace_id']):next(i for i in fixture['ordinary'] if i['key']==k) for k in keys}
    if card['epistemic_state']!='observed_only' or card['count']!=len(keys) or set(card['source_refs'])!={str(receipts[k]['evidence_id']) for k in keys} or {r.get('id') for r in records}!=set(expected):issues.append('unpaired trace provenance or missing identities')
    for record in records:
        item=expected.get(record.get('id'))
        if item is None:issues.append('unexpected trace');continue
        eid=receipts[item['key']]['evidence_id']
        try:stamp=datetime.fromisoformat(record['occurred_at'])
        except (KeyError,ValueError,TypeError):stamp=None
        if record.get('evidence_id')!=str(eid) or stamp!=datetime.fromisoformat(item['at']) or record.get('observation')!=item['observation'] or record.get('observation_boundary')!=item['boundary'] or record.get('type')!='life_trace' or record.get('epistemic_state')!='observed_only' or record.get('lifecycle')!='active':issues.append('trace source/time/boundary pairing incomplete')
    if 'observations' in card or 'observation_boundaries' in card:issues.append('parallel unpaired content retained')
    return issues
for item in fixture['ordinary']:
    req=source(item);inputs[item['key']]=req;receipts[item['key']]=s.observe(req,trace={'observation':item['observation'],'observation_boundary':item['boundary']})
early_traces=trace_state();early_receipts={k:original_receipt(v['evidence_id']) for k,v in receipts.items()};issues=[]
with tenant_transaction(s.tenant_id,mode='read') as c:
    if c.execute('SELECT count(*) AS n FROM semantic_cards').fetchone()['n'] or c.execute('SELECT count(*) AS n FROM projections').fetchone()['n']:issues.append('premature event/thread formed')
if expanded(recall()):issues.append('unlinked traces forced into history')
check('early_ordinary_only',issues)
late=fixture['explicit'];late_receipt=s.observe(source(late));prepared=s.read_evidence_interpretation(late_receipt['evidence_id']);intent=uuid4();sem=SemanticInput('event',late['body'],canonical_key='synthetic-park-decision',valid_at=datetime.fromisoformat(late['at']))
created=s.commit_evidence_interpretation(prepared,intent_id=intent,semantics=[sem],provenance={'producer_id':'offline-journey','model_id':'fixed-reviewed-output'});cid=created['card_ids'][0];issues=[]
if not created['created'] or len(created['card_ids'])!=1 or trace_state()!=early_traces:issues.append('event creation moved or copied traces')
for k,r in early_receipts.items():
    if original_receipt(receipts[k]['evidence_id'])!=r:issues.append('original receipt rewritten')
ctx=recall();ev=[x for x in expanded(ctx) if x['type']=='event']
if len(ev)!=1 or ev[0]['id']!=str(cid) or ev[0]['version']!=1 or ev[0]['body']!=late['body'] or {r['evidence_id'] for r in ev[0]['sources']}!={str(late_receipt['evidence_id'])} or datetime.fromisoformat(ev[0]['valid_at'])!=datetime.fromisoformat(late['at']):issues.append('decision continuity/provenance incomplete')
check('explicit_decision_one_event',issues)
before=snapshot(s);replay=s.commit_evidence_interpretation(prepared,intent_id=intent,semantics=[sem],provenance={'producer_id':'offline-journey','model_id':'fixed-reviewed-output'});issues=[]
if replay!={**created,'created':False} or snapshot(s)!=before:issues.append('interpretation replay duplicated or mutated source')
check('exact_replay_twenty_tables_unchanged',issues)
for receipt in receipts.values():s.add_relation(from_kind='life_trace',from_id=receipt['trace_id'],to_kind='semantic_card',to_id=cid,family='context_involvement',relation_type='earlier_context',annotation='ordinary background only; no habit inference',source_evidence_id=late_receipt['evidence_id'])
linked=recall();check('paired_context_recall',pairing_issues(linked,list(receipts)))
tiny=recall(RecallOptions(card_token_limit=1));issues=pairing_issues(tiny,list(receipts))
if tiny['layers']['evidence_summary'] or len([h for h in tiny['expansion_handles'] if h['type']=='evidence_summary'])!=1:issues.append('tiny budget emitted partial trace card')
check('whole_summary_pinned_expansion',issues)
fixed=fixture['correction'];result=s.correct_card(cid,source(fixed),fixed['body'],expected_version=1);corrected=recall();issues=pairing_issues(corrected,list(receipts));ev=[x for x in expanded(corrected) if x['type']=='event']
with tenant_transaction(s.tenant_id,mode='read') as c:correct_eid=c.execute('SELECT id FROM evidence WHERE idempotency_key=%s',(fixed['key'],)).fetchone()['id']
if len(ev)!=1 or ev[0]['id']!=str(cid) or ev[0]['version']!=2 or ev[0]['epistemic_state']!='corrected' or ev[0]['body']!=fixed['body'] or {r['evidence_id'] for r in ev[0]['sources']}!={str(late_receipt['evidence_id']),str(correct_eid)} or trace_state()!=early_traces:issues.append('correction lost event identity/current qualifiers/ordinary context')
try:s.expand_snapshot(UUID(linked['snapshot']['id']),cid);issues.append('corrected old snapshot usable')
except SnapshotInvalidated:pass
check('correction_continuity',issues)
first=fixture['ordinary'][0]['key'];second=fixture['ordinary'][1]['key'];s.delete_evidence(receipts[first]['evidence_id']);after_source=recall();issues=pairing_issues(after_source,[second]);ev=[x for x in expanded(after_source) if x['type']=='event']
if len(ev)!=1 or ev[0]['version']!=2 or ev[0]['body']!=fixed['body'] or trace_state()[str(receipts[first]['trace_id'])]['observation']!='' or trace_state()[str(receipts[second]['trace_id'])]!=early_traces[str(receipts[second]['trace_id'])]:issues.append('source deletion damaged unaffected event/trace or retained deleted content')
try:s.observe(inputs[first],trace={'observation':fixture['ordinary'][0]['observation'],'observation_boundary':fixture['ordinary'][0]['boundary']});issues.append('deleted ordinary source replayed')
except DeletedSource:pass
try:s.expand_snapshot(UUID(corrected['snapshot']['id']),cid);issues.append('deleted old snapshot usable')
except SnapshotInvalidated:pass
check('delete_first_ordinary_source',issues)
s.delete_card(cid);issues=[]
if expanded(recall()):issues.append('deleted event/background forced into history')
before=snapshot(s)
try:s.commit_evidence_interpretation(prepared,intent_id=intent,semantics=[sem],provenance={'producer_id':'offline-journey','model_id':'fixed-reviewed-output'});issues.append('deleted event regenerated')
except InterpretationRevoked:pass
if snapshot(s)!=before or trace_state()[str(receipts[second]['trace_id'])]!=early_traces[str(receipts[second]['trace_id'])]:issues.append('blocked replay wrote data or deleted unrequested trace')
try:s.expand_snapshot(UUID(after_source['snapshot']['id']),cid);issues.append('deleted event snapshot usable')
except SnapshotInvalidated:pass
check('delete_event_blocks_regeneration',issues)
print(json.dumps({'fixture_sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'restricted_flags':flags,'cases':checks,'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'semantic_judgments':'fixed reviewed output and explicitly scripted links; no extractor or model inference tested','model_calls':0}))

"""

if __name__ == "__main__":
    unittest.main(verbosity=2)
