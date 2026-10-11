"""Finite synthetic CLI baseline. Default launch always creates its own empty PG."""
from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
from tempfile import mkdtemp
import time
from uuid import UUID, uuid4

REPO = Path(__file__).resolve().parents[2]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')
SCALES = (365, 3650)
OPERATIONS = ('retention_stats', 'retention_candidates', 'recall_recent', 'recall_old', 'recall_unrelated')
WARMUPS, SAMPLES = 1, 7


def check_worker(marker):
    from liveday0.db import connect
    with connect() as conn:
        row = conn.execute("SELECT current_setting('cluster_name') AS marker,current_setting('listen_addresses') AS listen,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit FROM pg_roles WHERE rolname=current_user").fetchone()
    assert marker.startswith('liveday0-bounded-scale-') and row['marker'] == marker
    assert row['listen'] == '' and not any(row[k] for k in ('rolsuper','rolbypassrls','rolcreatedb','rolcreaterole','rolinherit'))
    return row


def seed_worker(marker, n):
    from liveday0.core import MemoryService
    from liveday0.db import tenant_transaction
    check_worker(marker)
    assert n in SCALES
    tenant = UUID(int=n)
    MemoryService(tenant).ensure_tenant()
    with tenant_transaction(tenant) as conn:
        # Direct deterministic fixture loading, not a semantic-ingestion quality test.
        conn.execute("""INSERT INTO evidence(id,tenant_id,modality,source_kind,content,occurred_at)
          SELECT md5(%s||':e:'||i)::uuid,%s,'text','synthetic_scale',
            CASE WHEN i IN (1,2) THEN 'old_unique_trip' WHEN i IN (%s,%s) THEN 'recent_family_walk' ELSE 'ordinary_day_'||((i+1)/2) END,
            '2016-01-01'::timestamptz+make_interval(days=>(i+1)/2)
          FROM generate_series(1,%s) i""", (str(tenant),tenant,2*n-1,2*n,2*n))
        conn.execute("""INSERT INTO semantic_cards(id,tenant_id,canonical_key,card_type,valid_at)
          SELECT md5(%s||':c:'||i)::uuid,%s,'synthetic:day:'||i,'event',
            '2016-01-01'::timestamptz+make_interval(days=>i) FROM generate_series(1,%s) i""", (str(tenant),tenant,n))
        conn.execute("""INSERT INTO semantic_card_versions(tenant_id,card_id,version,body,lifecycle,epistemic_state,valid_at)
          SELECT tenant_id,id,1,jsonb_build_object('goal_context',
            CASE WHEN canonical_key='synthetic:day:1' THEN 'old_unique_trip' WHEN canonical_key=%s THEN 'recent_family_walk' ELSE 'ordinary_day' END,
            'current_result','synthetic lived moment','boundaries','synthetic only'),lifecycle,epistemic_state,valid_at
          FROM semantic_cards WHERE tenant_id=%s""", ('synthetic:day:'+str(n),tenant))
        conn.execute("""INSERT INTO card_sources(tenant_id,card_id,evidence_id)
          SELECT %s,md5(%s||':c:'||((i+1)/2))::uuid,md5(%s||':e:'||i)::uuid
          FROM generate_series(1,%s) i""", (tenant,str(tenant),str(tenant),2*n))
        conn.execute("""INSERT INTO maintenance_jobs(id,tenant_id,job_type,target_kind,target_id,coalesce_key,state,created_at,updated_at)
          SELECT md5(%s||':j:'||i)::uuid,%s,'event_rewrite','semantic_card',
            md5(%s||':c:'||((i+2)/3))::uuid,'event:'||((i+2)/3),'succeeded',
            '2020-01-01'::timestamptz+make_interval(secs=>i),'2020-01-01'::timestamptz+make_interval(secs=>i)
          FROM generate_series(1,%s) i""", (str(tenant),tenant,str(tenant),3*n))
        conn.execute("""INSERT INTO recall_snapshots(id,tenant_id,tenant_revision,state,context,created_at,invalidated_at)
          SELECT md5(%s||':s:'||i)::uuid,%s,0,CASE WHEN i%%5=0 THEN 'active' ELSE 'expired' END,
            CASE WHEN i%%5=0 THEN '{"synthetic":"active-cache"}'::jsonb ELSE '{}'::jsonb END,
            '2020-01-01'::timestamptz,CASE WHEN i%%5=0 THEN NULL ELSE '2020-01-02'::timestamptz END
          FROM generate_series(1,%s) i""", (str(tenant),tenant,2*n))
        counts = {t:conn.execute('SELECT count(*) AS n FROM '+t+' WHERE tenant_id=%s',(tenant,)).fetchone()['n']
                  for t in ('evidence','semantic_cards','semantic_card_versions','card_sources','maintenance_jobs','recall_snapshots')}
    print(json.dumps({'tenant':str(tenant),'counts':counts,'expected_old_id':str(UUID(hashlib.md5((str(tenant)+':c:1').encode()).hexdigest()))}))


def measure_worker(marker, tenant, operation):
    import liveday0.db as db
    from liveday0.cli import main as cli_main
    from liveday0.core import MemoryService
    check_worker(marker)
    assert operation in OPERATIONS
    original = db.connect
    queries = []
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, key): return getattr(self.conn, key)
        def __enter__(self): self.conn.__enter__(); return self
        def __exit__(self, *args): return self.conn.__exit__(*args)
        def execute(self, query, params=None, *args, **kwargs):
            begin = time.perf_counter()
            try:return self.conn.execute(query, params, *args, **kwargs)
            finally:
                queries.append({'sql':' '.join(query.split()) if isinstance(query,str) else str(query),
                                'ms':(time.perf_counter()-begin)*1000})
    db.connect = lambda **kwargs: Connection(original(**kwargs))
    args = ['retention',tenant,'--dry-run'] if operation.startswith('retention') else ['recall',tenant,{
        'recall_recent':'recent_family_walk','recall_old':'old_unique_trip','recall_unrelated':'unseen_life_cue'}[operation]]
    if operation == 'retention_candidates':args += ['--snapshot-days','30','--job-days','30']
    samples=[];sql_counts=[];outputs=[];failure=None
    startup_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for i in range(WARMUPS+SAMPLES):
        queries.clear();output=StringIO();begin=time.perf_counter()
        try:
            with redirect_stdout(output):cli_main(args)
            result=json.loads(output.getvalue())
        except Exception as error:
            last_queries = list(queries)
            failure={'type':type(error).__name__,'sqlstate':getattr(error,'sqlstate',None),'ms':(time.perf_counter()-begin)*1000}
            break  # Never repeatedly hammer a timed-out path.
        elapsed=(time.perf_counter()-begin)*1000
        if i==0:first_ms=elapsed
        else:samples.append(elapsed);sql_counts.append(len(queries))
        outputs[:] = [result]
        last_queries = list(queries)
        if operation.startswith('recall'):
            # Cleanup follows the measured call; do not include release in recall timing/query count.
            MemoryService(UUID(tenant)).release_snapshot(UUID(result['snapshot']['id']))
    # Retain only a small SQL timing summary, excluding release and never replaying writes.
    plans=[]
    if outputs or failure:
        # Captured queries contain positional parameters; identify hot SQL and retain elapsed/count,
        # rather than interpolating/replaying captured parameters or executing an unbounded EXPLAIN.
        groups={}
        for q in locals().get('last_queries', queries):
            g=groups.setdefault(q['sql'],{'sql':q['sql'],'count':0,'ms':0});g['count']+=1;g['ms']+=q['ms']
        plans=sorted(groups.values(),key=lambda q:q['ms'],reverse=True)[:5]
    peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    factor=1 if sys.platform=='darwin' else 1024
    result={'operation':operation,'warmups':WARMUPS,'requested_samples':SAMPLES,'completed_samples':len(samples),
            'first_call_ms':locals().get('first_ms'),'samples_ms':samples,
            'median_ms':statistics.median(samples) if samples else None,
            'p95_ms':sorted(samples)[min(len(samples)-1,__import__('math').ceil(.95*len(samples))-1)] if samples else None,
            'max_ms':max(samples) if samples else None,'sql_counts':sql_counts,'last_call_sql_top':plans,'failure':failure,
            'application_startup_peak_rss_mib':startup_peak*factor/2**20,'application_peak_rss_mib':peak*factor/2**20}
    if outputs:
        last=outputs[-1]
        if operation.startswith('recall'):
            result['event_ids']=[c['id'] for c in last['layers']['events']]
            result['event_count']=len(result['event_ids']);result['historical_available']=last['historical_available']
            result['reported_estimated_tokens']=last['budget']['estimated_tokens']
            result['returned_json_bytes']=len(json.dumps(last,ensure_ascii=False).encode())
        else:result['totals']={k:last[k]['total'] for k in ('jobs','snapshots')};result['eligible']={k:last[k]['eligible'] for k in ('jobs','snapshots')}
    print(json.dumps(result))


def launch(*, retention_only=False):
    root=Path(mkdtemp(prefix='ld0-scale-',dir='/tmp')).resolve()
    (root/'s').mkdir(mode=0o700);(root/'logs').mkdir(mode=0o700)
    suffix=uuid4().hex[:8];owner='scale_owner_'+suffix;client='scale_client_'+suffix;database='ld0_scale_'+suffix
    marker='liveday0-bounded-scale-'+suffix
    env={'PATH':'/usr/bin:/bin:/opt/homebrew/bin','LC_ALL':'C','TZ':'UTC','PGPASSFILE':'/dev/null','PGSERVICEFILE':'/dev/null',
         'PGSYSCONFDIR':str(root),'PYTHONPATH':str(REPO/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
    record={'root':str(root),'marker':marker,'synthetic':True,'model_calls':0,'stopped':False,'scales':[],
            'python':platform.python_version(),'os':platform.platform(),'machine':platform.machine(),
            'workload':{'retention_only':retention_only,'event_counts':SCALES,'sources_per_event':2,'jobs_per_event':3,'snapshots_per_event':2,
                        'warmups':WARMUPS,'samples':SAMPLES,'api':'actual CLI main in fresh per-operation process; application imports excluded',
                        'deadline':'retention SQL 2000ms; worker whole process 45s; no artificial recall SQL timeout'}}
    counter=0;started=False;postmaster=None
    def cluster_rss():
        if not postmaster:return None
        p=subprocess.run(['/bin/ps','-axo','pid=,ppid=,rss='],capture_output=True,text=True,env=env,timeout=5)
        if p.returncode:return None
        rows=[tuple(map(int,line.split())) for line in p.stdout.splitlines() if len(line.split())==3]
        if not any(pid==postmaster for pid,ppid,rss in rows):return None
        ids={postmaster}
        for _ in range(4):ids|={pid for pid,ppid,rss in rows if ppid in ids}
        return sum(rss for pid,ppid,rss in rows if pid in ids)/1024
    def command(args,*,login=owner,check=True,monitor=False):
        nonlocal counter
        counter+=1;prefix=root/'logs'/f'{counter:02d}'
        run_env={**env,'LIVEDAY0_DATABASE_URL':f'dbname={database} user={login} host={root/"s"}'}
        begin=time.monotonic();pg_peak=None;rss_errors=0
        with Path(str(prefix)+'.stdout').open('w') as out,Path(str(prefix)+'.stderr').open('w') as err:
            p=subprocess.Popen([str(a) for a in args],cwd=root,env=run_env,stdout=out,stderr=err)
            try:
                if monitor:
                    while p.poll() is None:
                        sample=cluster_rss()
                        if sample is None:rss_errors+=1
                        else:pg_peak=max(pg_peak or 0,sample)
                        if time.monotonic()-begin>45:raise subprocess.TimeoutExpired(args,45)
                        time.sleep(.05)
                p.wait(timeout=max(.1,45-(time.monotonic()-begin)))
            except BaseException:p.kill();p.wait();raise
        stdout=Path(str(prefix)+'.stdout').read_text();stderr=Path(str(prefix)+'.stderr').read_text()
        if check and p.returncode:raise RuntimeError(f'command {counter} failed: {stderr}')
        return stdout,{'peak_mib':pg_peak,'unreadable_samples':rss_errors}
    def sql(text,db=database):
        return command([PG/'psql','-X','-A','-t','-v','ON_ERROR_STOP=1','-h',root/'s','-U',owner,'-d',db,'-c',text])[0].strip()
    try:
        command([PG/'initdb','-D',root/'pgdata','-U',owner,'--no-locale','--encoding=UTF8','--auth-local=trust','--auth-host=reject'])
        with (root/'pgdata/postgresql.conf').open('a') as f:
            f.write(f"\nlisten_addresses=''\nunix_socket_directories='{root/'s'}'\nunix_socket_permissions=0700\ncluster_name='{marker}'\nautovacuum_naptime='1s'\n")
        (root/'pgdata/scale-marker.txt').write_text(marker);started=True
        command([PG/'pg_ctl','-D',root/'pgdata','-l',root/'logs/postgres.log','-w','-t','20','start'])
        postmaster=int((root/'pgdata/postmaster.pid').read_text().splitlines()[0])
        sql(f'CREATE DATABASE {database}','postgres');sql(f"COMMENT ON DATABASE {database} IS '{marker}'")
        proof=json.loads(sql("SELECT json_build_object('data',current_setting('data_directory'),'socket',current_setting('unix_socket_directories'),'listen',current_setting('listen_addresses'),'marker',current_setting('cluster_name'),'db',current_database(),'comment',shobj_description(oid,'pg_database'),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public')) FROM pg_database WHERE datname=current_database()"))
        assert proof=={'data':str(root/'pgdata'),'socket':str(root/'s'),'listen':'','marker':marker,'db':database,'comment':marker,'public_tables':0}
        record['initial_empty_isolation_proof']=proof;record['postgres']=sql('SHOW server_version')
        command([sys.executable,'-B','-m','liveday0','migrate','up'])
        sql(f'CREATE ROLE {client} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT; GRANT liveday0_app TO {client}; GRANT CONNECT ON DATABASE {database} TO {client}')
        for n in SCALES:
            data,_=command([sys.executable,'-B',Path(__file__).resolve(),'--seed',marker,str(n)],login=client)
            fixture=json.loads(data);measurements=[]
            for op in (OPERATIONS[:2] if retention_only else OPERATIONS):
                data,rss=command([sys.executable,'-B',Path(__file__).resolve(),'--measure',marker,fixture['tenant'],op],login=client,monitor=True)
                measured=json.loads(data);measured['postgres_cluster_aggregate_rss_peak_mib']=rss['peak_mib'];measured['rss_unreadable_samples']=rss['unreadable_samples'];measurements.append(measured)
            record['scales'].append({'fixture':fixture,'measurements':measurements})
            (root/'manifest.json').write_text(json.dumps(record,indent=2))
        record['status']='completed_with_entry_failures' if any(m['failure'] for s in record['scales'] for m in s['measurements']) else 'completed_without_entry_failures'
    finally:
        if started:
            try:command([PG/'pg_ctl','-D',root/'pgdata','-m','fast','-w','-t','20','stop']);record['stopped']=True
            except Exception as error:record['stop_error']=type(error).__name__
        record['postmaster_pid_absent']=not(root/'pgdata/postmaster.pid').exists()
        (root/'manifest.json').write_text(json.dumps(record,indent=2))
        print('BOUNDED_SCALE_EVIDENCE='+str(root/'manifest.json'))
    assert record['stopped'] and record['postmaster_pid_absent']


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--seed':seed_worker(sys.argv[2],int(sys.argv[3]))
    elif len(sys.argv)==5 and sys.argv[1]=='--measure':measure_worker(sys.argv[2],sys.argv[3],sys.argv[4])
    elif sys.argv[1:]==['--retention-only']:launch(retention_only=True)
    elif len(sys.argv)==1:launch()
    else:raise SystemExit('Default invocation only; internal worker args are fixed synthetic fixtures.')
