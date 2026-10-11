"""One fixed, reviewed synthetic life loop in a newly created local cluster.

No arbitrary database URL, existing-cluster cleanup, credentials or model calls.
The bootstrap owner is used only for creation/migrations/isolation inspection;
all memory operations run in a child under a non-superuser application login.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from liveday0 import extraction_contract as c
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired
from liveday0.local_console import terminal_confirmation
from liveday0.local_host import LocalMemorySession
from liveday0.synthetic_proposal_diagnostics import (
    PENDING_SOURCE, CONFIRMED_SOURCE, CheckedSyntheticAdapter,
)


class FixedPlanAdapter:
    production_ready = False

    def extract(self, request):
        # Exact synthetic fixtures only; this does not extract arbitrary prose.
        if request.content not in (PENDING_SOURCE, CONFIRMED_SOURCE):
            return c.ProposalBatch(request.fingerprint, ())
        body = {'goal_context': '合成朋友爬山', 'development': '原定2026-10-10',
                'current_result': '改到2026-10-11'}
        if request.content == PENDING_SOURCE:
            body.update(unfinished_future='等待朋友确认', uncertainty='朋友尚未确认')
        key = f'synthetic-plan:{request.source.source_id}'
        p = c.SemanticProposal('synthetic-plan',
            c.ProposalSource(request.source.source_id, request.source.source_version,
                request.content_digest, c.SourceSpan.from_text(request.chunk_start, request.content)),
            'user', c.SubjectRef('user', 'synthetic-person'), 'direct_statement', 'event',
            c.ProposalScope('durable', 'synthetic life'), 1.0, 'asserted', 'ordinary',
            'implicit_candidate', c.RevisionRef('create', key),
            c.ProducerMetadata('synthetic-plan', 'golden-fixture', '1.0.0', '1.0.0'),
            c.ProposedSemanticInput('event', body, 'active', 'confirmed', key, request.occurred_at))
        return c.ProposalBatch(request.fingerprint, (p,))


def _state(session):
    with tenant_transaction(session.tenant_id, mode='read') as conn:
        evidence = conn.execute('SELECT id,status,content,object_ref,interpretation_revoked FROM evidence WHERE tenant_id=%s ORDER BY id',
                                (session.tenant_id,)).fetchall()
        cards = conn.execute('SELECT id,lifecycle,current_version FROM semantic_cards WHERE tenant_id=%s ORDER BY id',
                             (session.tenant_id,)).fetchall()
        stale = conn.execute("SELECT count(*) AS n FROM recall_snapshots WHERE tenant_id=%s AND state='active'",
                             (session.tenant_id,)).fetchone()['n']
        bodies = conn.execute("SELECT count(*) AS n FROM semantic_card_versions WHERE tenant_id=%s AND body<>'{}'::jsonb",
                              (session.tenant_id,)).fetchone()['n']
    return {'evidence': evidence, 'cards': cards, 'active_snapshots': stale, 'retained_card_bodies': bodies}


def run_loop(*, scripted):
    choices = iter((False, True, True, True, True, True))
    reviews = []
    steps = []
    def confirm(approval):
        accepted = next(choices) if scripted else terminal_confirmation(approval)
        reviews.append({'action': approval.action, 'fingerprint': approval.fingerprint,
                        'accepted': accepted, 'payload': approval.payload})
        return accepted
    session = LocalMemorySession(adapter=CheckedSyntheticAdapter(FixedPlanAdapter()),
                                 confirm=confirm, review_candidates=True)
    before = _state(session)
    if before['evidence'] or before['cards']:
        raise RuntimeError('synthetic memory scope must start empty')
    def record(action, result, **checks):
        if not all(checks.values()):
            raise RuntimeError(f'synthetic {action} verification failed')
        steps.append({'action': action, 'result': result, 'checks': checks})
    try:
        session.handle({'action': 'capture', 'text': PENDING_SOURCE})
    except AuthorizationRequired:
        record('reject', {'status': 'rejected'}, no_evidence_or_card=_state(session) == before)
    else:
        raise RuntimeError('First review must reject; this isolated run is stopped and retained')
    saved = session.handle({'action': 'capture', 'text': PENDING_SOURCE})
    receipt = saved['outcomes'][0]; card = str(receipt['card_ids'][0]); original = str(receipt['evidence_id'])
    record('save', saved, stored=receipt['created'] is True,
           version_one=_state(session)['cards'][0]['current_version'] == 1)
    pending = session.handle({'action': 'query', 'query': '合成朋友爬山'})
    record('query-pending', pending, pending_present='等待朋友确认' in json.dumps(pending, ensure_ascii=False))
    corrected = session.handle({'action': 'correct', 'card_id': card, 'expected_version': 1,
                                'text': CONFIRMED_SOURCE})
    record('correct', corrected, version_two=corrected['outcomes'][0]['version'] == 2,
           old_snapshots_invalidated=_state(session)['active_snapshots'] == 0)
    current = session.handle({'action': 'query', 'query': '合成朋友爬山'})
    rendered = json.dumps(current, ensure_ascii=False)
    record('query-corrected', current, updated_date_present='改到2026-10-11' in rendered,
           old_pending_absent='等待朋友确认' not in rendered and '朋友尚未确认' not in rendered)
    with tenant_transaction(session.tenant_id, mode='read') as conn:
        sources = [str(row['evidence_id']) for row in conn.execute(
            'SELECT DISTINCT evidence_id FROM card_sources WHERE tenant_id=%s AND card_id=%s ORDER BY evidence_id',
            (session.tenant_id, receipt['card_ids'][0]))]
    if original not in sources or len(sources) != 2:
        raise RuntimeError('expected exactly the two synthetic sources')
    deleted = session.handle({'action': 'delete', 'kind': 'card', 'id': card})
    after_card = _state(session)
    record('delete-card', deleted, card_deleted=after_card['cards'][0]['lifecycle'] == 'deleted',
           sources_revoked=all(e['interpretation_revoked'] for e in after_card['evidence']),
           all_card_version_bodies_erased=after_card['retained_card_bodies'] == 0,
           old_snapshots_invalidated=after_card['active_snapshots'] == 0)
    for eid in sources:
        erased = session.handle({'action': 'delete', 'kind': 'evidence', 'id': eid})
        row = next(e for e in _state(session)['evidence'] if str(e['id']) == eid)
        record('delete-source', erased, source_erased=row['status'] == 'deleted'
               and row['content'] is None and row['object_ref'] is None)
    final = session.handle({'action': 'query', 'query': '合成朋友爬山'})
    rendered = json.dumps(final, ensure_ascii=False)
    record('query-after-delete', final, deleted_content_absent='改到2026-10-11' not in rendered
           and '等待朋友确认' not in rendered)
    return {'synthetic': True, 'real_model_calls': 0, 'consent': 'scripted fixture' if scripted else 'terminal decisions',
            'steps': steps, 'reviews': reviews, 'final_state': _state(session)}


def launch(*, scripted=False, postgres_bin=Path('/opt/homebrew/opt/postgresql@17/bin')):
    if not scripted and not sys.stdin.isatty():
        raise ValueError('interactive synthetic-demo requires a human terminal; --scripted uses fixture choices')
    tools = {name: Path(postgres_bin) / name for name in ('initdb', 'pg_ctl', 'psql')}
    if any(not p.is_file() for p in tools.values()):
        raise ValueError('PostgreSQL 17 tools are required; no install or existing database fallback')
    # Always create a unique directory. Never accept an existing PGDATA or DSN.
    root = Path(mkdtemp(prefix='ld0-synth-', dir='/tmp')).resolve()
    (root / 's').mkdir(mode=0o700); (root / 'logs').mkdir(mode=0o700)
    suffix = uuid4().hex[:8]; owner = 'demo_owner_' + suffix; client = 'demo_client_' + suffix
    database = 'ld0_synth_' + suffix; marker = 'liveday0-synthetic-demo-' + suffix
    repo = Path(__file__).resolve().parents[2]
    env = {'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
           'PGPASSFILE': '/dev/null', 'PGSERVICEFILE': '/dev/null', 'PGSYSCONFDIR': str(root),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(repo / 'src')}
    manifest = {'root': str(root), 'marker': marker, 'database': database,
                'owner': owner, 'client': client, 'stopped': False, 'status': 'initializing'}
    def run(name, argv, *, child_env=env):
        result = subprocess.run([str(x) for x in argv], env=child_env, cwd=repo,
                                text=True, capture_output=True,
                                timeout=None if name == 'loop' and not scripted else 60)
        (root / 'logs' / (name + '.stdout')).write_text(result.stdout)
        (root / 'logs' / (name + '.stderr')).write_text(result.stderr)
        if result.returncode:
            raise RuntimeError(f'{name} failed; see isolated logs in {root}')
        return result.stdout
    def sql(name, statement, db=database):
        file = root / (name + '.sql'); file.write_text(statement)
        return run(name, [tools['psql'], '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                          '-h', root / 's', '-p', '5432', '-U', owner, '-d', db, '-f', file])
    started = False
    try:
        run('initdb', [tools['initdb'], '-D', root / 'pgdata', '-U', owner,
                      '--no-locale', '--encoding=UTF8', '--auth-local=trust', '--auth-host=reject'])
        with (root / 'pgdata/postgresql.conf').open('a') as stream:
            stream.write(f"\nlisten_addresses=''\nunix_socket_directories='{root / 's'}'\nunix_socket_permissions=0700\ncluster_name='{marker}'\nport=5432\n")
        (root / 'pgdata/synthetic-demo-marker.txt').write_text(marker)
        started = True
        run('start', [tools['pg_ctl'], '-D', root / 'pgdata', '-l', root / 'logs/postgres.log', '-w', '-t', '20', 'start'])
        sql('create', f"CREATE DATABASE {database}; COMMENT ON DATABASE {database} IS '{marker}';", db='postgres')
        raw = sql('proof', "SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database();")
        proof = json.loads(raw)
        if proof != {'data': str(root / 'pgdata'), 'socket': str(root / 's'), 'listen': '',
                     'marker': marker, 'db': database, 'comment': marker, 'public_tables': 0}:
            raise RuntimeError('fresh empty synthetic isolation proof failed')
        manifest['initial_empty_isolation_proof'] = proof
        owner_dsn = f'dbname={database} user={owner} host={root / "s"} port=5432'
        run('migrate', [sys.executable, '-B', '-m', 'liveday0', 'migrate', 'up'],
            child_env={**env, 'LIVEDAY0_DATABASE_URL': owner_dsn})
        sql('client', f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {database} TO {client};')
        manifest['status'] = 'ready'
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        client_dsn = f'dbname={database} user={client} host={root / "s"} port=5432'
        args = [sys.executable, '-B', '-m', 'liveday0.synthetic_demo', '--worker', root / 'manifest.json']
        if scripted:
            args.append('--scripted')
        else:
            print('仅固定合成样例：第一次候选请拒绝，第二次输入 SAVE 保存；随后确认更正、删除卡片及两份来源。',
                  file=sys.stderr, flush=True)
            print(f'新建隔离目录：{root}；退出时停止并保留日志。', file=sys.stderr, flush=True)
        result = json.loads(run('loop', args, child_env={**env, 'LIVEDAY0_DATABASE_URL': client_dsn}))
        manifest['result'] = result
        manifest['status'] = 'passed'
    except BaseException:
        manifest['status'] = 'failed_or_interrupted'
        raise
    finally:
        if started:
            try:
                stopped = subprocess.run([str(tools['pg_ctl']), '-D', str(root / 'pgdata'), '-m', 'fast',
                                          '-w', '-t', '20', 'stop'], env=env, text=True, capture_output=True, timeout=30)
                manifest['stopped'] = stopped.returncode == 0
                (root / 'logs/stop.stdout').write_text(stopped.stdout)
                (root / 'logs/stop.stderr').write_text(stopped.stderr)
            except (OSError, subprocess.TimeoutExpired) as error:
                manifest['stop_error'] = type(error).__name__
        manifest['postmaster_pid_absent'] = not (root / 'pgdata/postmaster.pid').exists()
        if started and (not manifest['stopped'] or not manifest['postmaster_pid_absent']):
            manifest['status'] = 'stop_unconfirmed'
        (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, default=str, indent=2))
    if not manifest['stopped'] or not manifest['postmaster_pid_absent']:
        raise RuntimeError(f'isolated instance stop unconfirmed; inspect {root}')
    return manifest


def worker(path, *, scripted):
    manifest = json.loads(Path(path).read_text()); root = Path(manifest['root'])
    marker = (root / 'pgdata/synthetic-demo-marker.txt').read_text()
    if manifest['status'] != 'ready' or marker != manifest['marker']:
        raise RuntimeError('synthetic worker marker mismatch')
    dsn = f'dbname={manifest["database"]} user={manifest["client"]} host={root / "s"} port=5432'
    os.environ['LIVEDAY0_DATABASE_URL'] = dsn
    # This is the child's only source of DB location; check it before memory ops.
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        proof = conn.execute("SELECT current_database() AS db,current_user AS login,shobj_description(d.oid,'pg_database') AS marker,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles r CROSS JOIN pg_database d WHERE r.rolname=current_user AND d.datname=current_database()").fetchone()
        proof['socket'] = conn.info.host
        if proof != {'db': manifest['database'], 'login': manifest['client'],
                     'socket': str(root / 's'), 'marker': marker, 'rolsuper': False,
                     'rolbypassrls': False, 'rolcreatedb': False, 'rolcreaterole': False}:
            raise RuntimeError('synthetic worker application isolation proof failed')
    result = run_loop(scripted=scripted); result['application_isolation_proof'] = proof
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--scripted', action='store_true')
    args = parser.parse_args()
    worker(args.worker, scripted=args.scripted)
