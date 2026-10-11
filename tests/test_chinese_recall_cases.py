"""Frozen Chinese life-query expectations in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class ChineseRecallCasesTests(unittest.TestCase):
    def test_lexical_controls_without_database(self):
        sys.path.insert(0, str(REPO / 'src'))
        from liveday0.recall import RecallCompiler
        score = RecallCompiler._relevance
        # Existing acceptance-scene predicates: birthday must not inherit sales boundaries.
        industry = {'proposition': '不参加陌生人强推销的行业局',
                    'scope': '陌生人强推销的行业聚会；不扩展到所有社交', 'applicability': '当前有效'}
        self.assertEqual(score('老朋友生日聚会', industry), 0)
        self.assertGreater(score('陌生人强推销 行业酒会', industry), 0)
        self.assertEqual(score('项目排期和代码', {'proposition': '用户对花生严重过敏，任何量都不接受'}), 0)
        self.assertEqual(score('杭州这周足球赛门票在哪买？', {'goal_context': '从北京搬到杭州'}), 0)
        self.assertEqual(score('我挪窝之后路上是不是省事了', {'goal_context': '从北京搬到杭州'}), 0)
        self.assertEqual(score('', {'goal_context': '从北京搬到杭州'}), 0)
        self.assertEqual(score('unseen_life_cue', {'goal_context': 'old_unique_trip'}), 0)
        self.assertEqual(score('old_unique_trip', {'goal_context': 'old_unique_trip'}), 6)

    def test_frozen_life_queries(self):
        root = Path(mkdtemp(prefix='ld0-chinese-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'zh_owner_' + suffix, 'zh_client_' + suffix, 'ld0_zh_' + suffix
        marker = 'liveday0-chinese-' + suffix
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
            (root / 'pgdata/chinese-marker.txt').write_text(marker)
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
from pathlib import Path
from datetime import datetime,timezone
from uuid import UUID,uuid4
from liveday0.core import MemoryService
from liveday0.db import connect,tenant_transaction
from liveday0.types import EvidenceInput,SemanticInput
fixture_path=Path(__import__('tests').__file__).parent/'fixtures/chinese_recall_cases.json'
fixture=json.loads(fixture_path.read_text()); fixture_hash=hashlib.sha256(fixture_path.read_bytes()).hexdigest()
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant(); ids={};requests={}
for i,item in enumerate(fixture['events']):
    stamp=datetime(2026,1,i+1,tzinfo=timezone.utc)
    request=EvidenceInput('text','synthetic',item['source'],occurred_at=stamp,idempotency_key='zh:'+item['key'])
    semantic=SemanticInput('event',item['body'],canonical_key='zh:'+item['key'],valid_at=stamp,lifecycle=item.get('lifecycle','active'),epistemic_state=item.get('epistemic_state','confirmed'))
    receipt=s.observe(request,semantics=[semantic]);ids[item['key']]=receipt['card_ids'][0];requests[item['key']]=(request,semantic,receipt)
mention=s.create_unbound_mention(EvidenceInput('text','synthetic','那次出门之后我就想再联系她了',occurred_at=datetime(2026,1,5,tzinfo=timezone.utc),idempotency_key='zh:ambiguous'),'那次出门',[{'card_id':ids['visit'],'reason':'探望母亲也是出门，但指代未确认','confidence':.5},{'card_id':ids['movie'],'reason':'观影也是出门，但指代未确认','confidence':.5}])
outcomes=[]
for case in fixture['cases']:
    failures=[]
    if case.get('replay_same_request_count'):
        request,semantic,original=requests['move']
        for _ in range(case['replay_same_request_count']):
            repeat=s.observe(request,semantics=[semantic])
            if repeat['evidence_id']!=original['evidence_id'] or repeat['card_ids']!=original['card_ids'] or repeat['created']:failures.append('replay identity changed')
    context=s.recall(case['query'])
    cards=list(context['layers']['events'])
    # Count whole omitted event references as candidates too; compare via pinned expansion.
    for handle in context['expansion_handles']:
        if handle['type']=='event':cards.append(s.expand_snapshot(UUID(context['snapshot']['id']),UUID(handle['item_id'])))
    returned={card['id'] for card in cards};expected={str(ids[key]) for key in case['expected_event_keys']}
    if returned!=expected:failures.append('returned event identities do not match frozen expectation')
    if len(cards)!=len(returned):failures.append('duplicate canonical card in compiled context')
    for card in cards:
        if not card.get('sources'):failures.append('returned event lacks source')
        if not card['body'].get('boundaries') or not card['body'].get('uncertainty'):failures.append('scope or uncertainty missing')
    if case.get('expected_sources') and cards and len(cards[0]['sources'])!=case['expected_sources']:failures.append('replay duplicated source')
    if case.get('required_state') and cards:
        card=next((c for c in cards if c['id']==str(ids['course'])),None)
        if card is None or card['epistemic_state']!=case['required_state'] or card['body']['uncertainty']!=case['required_uncertainty'] or card['body']['current_result']!='尚未付款':failures.append('provisional intent promoted or qualifier lost')
    if case.get('expected_unbound_candidate_keys'):
        matches=[m for m in context['layers']['ambiguous_mentions'] if m['id']==str(mention)]
        if len(matches)!=1 or matches[0]['epistemic_state']!='candidate' or {str(c['card_id']) for c in matches[0]['candidates']}!={str(ids[k]) for k in case['expected_unbound_candidate_keys']}:failures.append('ambiguity lost or candidates forced')
        with tenant_transaction(s.tenant_id,mode='read') as c:
            if c.execute('SELECT state FROM mentions WHERE tenant_id=%s AND id=%s',(s.tenant_id,mention)).fetchone()['state']!='unbound':failures.append('mention canonically bound')
    if case.get('unsupported_claim') and (context['layers']['explicit_facts'] or context['layers']['relationship_context'] or context['layers']['active_threads']):failures.append('unsupported interpretation introduced')
    s.release_snapshot(UUID(context['snapshot']['id']))
    outcomes.append({'id':case['id'],'kind':case['kind'],'passed':not failures,'expected_event_keys':case['expected_event_keys'],'actual_event_keys':[k for k,v in ids.items() if str(v) in returned],'failures':failures})
with tenant_transaction(s.tenant_id,mode='read') as c:
    assert c.execute('SELECT count(*) AS n FROM semantic_cards WHERE tenant_id=%s',(s.tenant_id,)).fetchone()['n']==4
    assert c.execute('SELECT count(*) AS n FROM evidence WHERE tenant_id=%s',(s.tenant_id,)).fetchone()['n']==5
    assert all(c.execute('SELECT body FROM semantic_card_versions WHERE tenant_id=%s AND card_id=%s ORDER BY version DESC LIMIT 1',(s.tenant_id,ids[item['key']])).fetchone()['body']==item['body'] for item in fixture['events'])
print(json.dumps({'restricted_flags':flags,'fixture_sha256':fixture_hash,'cases':outcomes,'passed':sum(c['passed'] for c in outcomes),'failed':sum(not c['passed'] for c in outcomes),'canonical_bodies_unchanged':True,'replays_one_identity_one_source':True,'snapshot_cycles_explicitly_released':True,'model_calls':0}))

"""))
            record['status'] = 'passed' if record['checks']['failed'] == 0 else 'frozen_expectation_failures'
        finally:
            if started:
                try:
                    command([PG / 'pg_ctl', '-D', root / 'pgdata', '-m', 'fast', '-w', '-t', '20', 'stop'])
                    record['stopped'] = True
                except Exception as error:record['stop_error'] = type(error).__name__
            record['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
            (root / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        self.assertTrue(record['stopped'] and record['postmaster_pid_absent'])
        print('CHINESE_RECALL_EVIDENCE=' + str(root / 'manifest.json'))
        self.assertEqual(record['checks']['failed'], 0, json.dumps(record['checks']['cases'],ensure_ascii=False))


if __name__ == '__main__':
    unittest.main(verbosity=2)
