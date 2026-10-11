"""Bounded old-cue retrieval in a newly marked synthetic PostgreSQL cluster."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
import unittest
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')


class RecallSeedCandidateTests(unittest.TestCase):
    def test_old_cues_enter_bounded_scoped_candidates(self):
        root = Path(mkdtemp(prefix='ld0-oldcue-', dir='/tmp')).resolve()
        (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
        suffix = uuid4().hex[:8]
        owner, client, db = 'cue_owner_' + suffix, 'cue_client_' + suffix, 'ld0_cue_' + suffix
        marker = 'liveday0-oldcue-' + suffix
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
            (root / 'pgdata/oldcue-marker.txt').write_text(marker)
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
import json
from datetime import datetime,timezone,timedelta
from uuid import UUID,uuid4
from unittest.mock import patch
from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.types import EvidenceInput,SemanticInput,RecallOptions
with connect() as c: flags=c.execute('SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user').fetchone()
assert not any(flags.values())
s=MemoryService(uuid4());s.ensure_tenant();foreign=MemoryService(uuid4());foreign.ensure_tenant()
time=datetime(2000,1,1,tzinfo=timezone.utc)
def event(service,source,cue,day,*,lifecycle='active'):
    return service.observe(EvidenceInput('text','synthetic',source,occurred_at=time+timedelta(days=day),idempotency_key=source),semantics=[SemanticInput('event',{'goal_context':cue,'current_result':'synthetic lived result'},canonical_key=source,valid_at=time+timedelta(days=day),lifecycle=lifecycle)])['card_ids'][0]
old=event(s,'old_unique_trip','old_unique_trip',0)
vector=event(s,'ordinary unmatching source','vectorneedle',1)
closed=event(s,'closedcue','closedcue',2,lifecycle='closed')
foreign_card=event(foreign,'foreignsecret','foreignsecret',0)
for i in range(20):event(s,'sharedcue '+str(i),'sharedcue',3+i)
for i in range(60):recent=event(s,'ordinary source '+str(i),'recent_family_walk' if i==59 else 'routine',50+i)
compiler=s.recall_compiler
with __import__('liveday0.db',fromlist=['tenant_transaction']).tenant_transaction(s.tenant_id,mode='read') as c:
    assert old in compiler._fts_card_ids(c,'old_unique_trip',48)
    assert not compiler._fts_card_ids(c,'foreignsecret',48)
selected=[];original=compiler._source_map
def capture(c,ids):
    selected.append(list(ids));return original(c,ids)
def recalled(query,options=None):
    with patch.object(compiler,'_source_map',capture):context=s.recall(query,options=options)
    assert len(selected[-1])<=(options.candidate_limit if options else 48)
    s.release_snapshot(UUID(context['snapshot']['id']))
    return context
first=recalled('old_unique_trip')
assert str(old) in {c['id'] for c in first['layers']['events']},'old FTS match excluded from candidate envelope'
assert first['layers']['events'][0]['sources']
assert str(recent) in {c['id'] for c in recalled('recent_family_walk')['layers']['events']}
assert not recalled('unseen_life_cue')['historical_available']
assert not recalled('foreignsecret')['historical_available']
assert not recalled('closedcue')['historical_available']
# Isolate admission of existing vector-discovery IDs, not real pgvector quality.
with patch.object(compiler,'_vector_card_ids',return_value={vector}):
    assert str(vector) in {c['id'] for c in recalled('vectorneedle')['layers']['events']}
# An out-of-scope/inactive discovery ID still cannot bypass the canonical SQL gate.
with patch.object(compiler,'_fts_card_ids',return_value={old,foreign_card,closed}):
    result=recalled('old_unique_trip')
    assert {c['id'] for c in result['layers']['events']}=={str(old)}
# Overlapping cue/history candidates deduplicate by canonical ID and share one cap.
small=RecallOptions(candidate_limit=8)
shared=recalled('sharedcue',small)
assert shared['historical_available'] and len(selected[-1])==8 and len(set(selected[-1]))==8
assert all('sharedcue' in c['body']['goal_context'] for c in shared['layers']['events'])
print(json.dumps({'restricted_flags':flags,'old_fts_source_found_and_event_returned':True,'recent_preserved':True,'unrelated_empty':True,'foreign_and_inactive_excluded':True,'vector_seed_admission_only':True,'forced_candidates_cannot_bypass_scope':True,'canonical_candidates_deduplicated_and_capped':True,'candidate_counts':[len(x) for x in selected],'maximum_default_candidates':48,'explicit_small_cap':8,'schema_or_model_changes':False}))
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
        print('RECALL_SEED_EVIDENCE=' + str(root / 'manifest.json'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
