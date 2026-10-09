"""Synthetic core contracts; no extractor, model call, or real human authentication."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from uuid import uuid4

import psycopg
import pytest

from liveday0.core import MemoryService
from liveday0.db import connect, tenant_transaction
from liveday0.exceptions import (AuthorizationRequired, DeletedSource, IdempotencyConflict,
    InterpretationRevoked, NotFound, ReceiptUnavailable, VersionConflict)
from liveday0.migrations import migrate_down, migrate_up, migration_status
from liveday0.serialization import canonical_json, fingerprint
from tests.helpers import make_projection
from tests.test_atomic_writes import source, proposal, snapshot
from tests.test_trust_boundaries import ControlledTransactions, named


def row(service, table, **where):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return conn.execute('SELECT * FROM '+table+' WHERE '+
            ' AND '.join(k+'=%s' for k in where),tuple(where.values())).fetchone()


def commit(service, prepared, intent=None, **kw):
    return service.commit_evidence_interpretation(prepared,intent_id=intent or uuid4(),**kw)


class SyntheticHost:
    """Read-only allowlist of exact requests; deliberately not a real auth adapter."""
    def __init__(self):
        self.reads=set(); self.commits=set(); self.checks=[]

    def __call__(self, check):
        self.checks.append(check)
        if check.phase == 'read':
            return (check.intent_id,check.payload['evidence_id']) in self.reads
        return (check.intent_id,check.fingerprint) in self.commits

    def approve(self, intent, prepared, *, semantics, trace=None, provenance=None):
        from dataclasses import asdict
        request={'contract':'liveday0:interpretation-intent:v1','intent_id':intent,
            'input':prepared.payload,'output':{'trace':trace,
            'semantics':[asdict(s) for s in semantics],'provenance':provenance or {}}}
        self.commits.add((intent,fingerprint(canonical_json(request))))


def revoked(service):
    req=source(); result=service.observe(req,semantics=[proposal(),proposal()])
    service.delete_card(result['card_ids'][0])
    return req,result


def recovery(service):
    req,result=revoked(service); host=SyntheticHost()
    trusted=MemoryService(service.tenant_id,explicit_save_authorizer=host); intent=uuid4()
    host.reads.add((intent,str(result['evidence_id'])))
    prepared=trusted.read_explicit_reinterpretation(result['evidence_id'],explicit_save_intent_id=intent)
    items=[proposal()]; host.approve(intent,prepared,semantics=items)
    return req,result,host,trusted,intent,prepared,items


@pytest.mark.parametrize('shape',['evidence','trace','cards','all'])
def test_exact_observe_replays_original_complete_receipt(service,shape):
    req=source(); trace={'observation':'quiet evening'} if shape in {'trace','all'} else None
    items=[proposal(),proposal()] if shape in {'cards','all'} else []
    first=service.observe(req,trace=trace,semantics=items)
    if items:
        # Mutable edges are not the original receipt.
        with tenant_transaction(service.tenant_id) as conn:
            conn.execute('DELETE FROM card_sources WHERE evidence_id=%s',(first['evidence_id'],))
    before=snapshot(service); second=service.observe(req,trace=trace,semantics=items)
    assert second=={**first,'created':False}; assert snapshot(service)==before
    assert row(service,'observation_receipts',evidence_id=first['evidence_id'])['card_ids']==first['card_ids']


def test_later_interpretation_does_not_change_original_receipt(service):
    req=source(); observed=service.observe(req); prepared=service.read_evidence_interpretation(observed['evidence_id'])
    intent=uuid4(); items=[proposal(),proposal()]
    result=commit(service,prepared,intent,trace={'observation':'quiet'},semantics=items,provenance={'model_id':'synthetic-v1'})
    before=snapshot(service)
    assert commit(service,prepared,intent,trace={'observation':'quiet'},semantics=items,provenance={'model_id':'synthetic-v1'})=={**result,'created':False}
    assert service.observe(req)=={**observed,'created':False}; assert snapshot(service)==before
    assert len(before['evidence'])==1 and len(result['card_ids'])==2


@pytest.mark.parametrize('change',['body','order','provenance','trace','source'])
def test_intent_payload_conflict_has_no_side_effects(service,change):
    observed=service.observe(source()); prepared=service.read_evidence_interpretation(observed['evidence_id'])
    items=[proposal(),replace(proposal(),body={'goal_context':'other','current_result':'other'})]
    intent=uuid4(); trace={'observation':'quiet'}; prov={'producer_id':'synthetic'}
    commit(service,prepared,intent,trace=trace,semantics=items,provenance=prov)
    if change=='body': items[0].body['current_result']='changed'
    elif change=='order': items.reverse()
    elif change=='provenance': prov={'producer_id':'changed'}
    elif change=='trace': trace={'observation':'changed'}
    else: prepared=service.read_evidence_interpretation(service.observe(source('other'))['evidence_id'])
    before=snapshot(service)
    assert len(before)==20
    with pytest.raises(IdempotencyConflict): commit(service,prepared,intent,trace=trace,semantics=items,provenance=prov)
    assert snapshot(service)==before


@pytest.mark.parametrize('case',['empty','oversized','many','trace-collision','key-collision','duplicate-key','model-flag','provenance-shape'])
def test_invalid_interpretations_leave_no_partial_receipt(service,case):
    observed=service.observe(source(),trace={'observation':'original'} if case=='trace-collision' else None,
        semantics=[proposal(canonical_key='taken')] if case=='key-collision' else [])
    prepared=service.read_evidence_interpretation(observed['evidence_id']); kw={'semantics':[proposal()]}
    if case=='empty': kw={}
    elif case=='oversized': kw={'trace':{'observation':'x'*64001}}
    elif case=='many': kw={'semantics':[proposal() for _ in range(17)]}
    elif case=='trace-collision': kw={'trace':{'observation':'replacement'}}
    elif case=='key-collision': kw={'semantics':[proposal(canonical_key='taken')]}
    elif case=='duplicate-key': kw={'semantics':[proposal(canonical_key='dup')]*2}
    elif case=='model-flag': kw['provenance']={'explicit_save':True}
    else: kw['provenance']=[]
    before=snapshot(service)
    with pytest.raises((ValueError,VersionConflict)): commit(service,prepared,**kw)
    assert snapshot(service)==before


@pytest.mark.parametrize('field,value',[('version',2),('content','changed'),('status','deleted')])
def test_actual_source_read_is_checked_again(service,field,value):
    observed=service.observe(source()); prepared=service.read_evidence_interpretation(observed['evidence_id'])
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute('UPDATE evidence SET '+field+'=%s WHERE id=%s',(value,observed['evidence_id']))
    before=snapshot(service)
    with pytest.raises((VersionConflict,DeletedSource)): commit(service,prepared,semantics=[proposal()])
    assert snapshot(service)==before


def test_legacy_missing_receipt_is_not_reconstructed(service):
    req=source(); result=service.observe(req)
    with tenant_transaction(service.tenant_id) as conn: conn.execute('DELETE FROM observation_receipts')
    before=snapshot(service)
    with pytest.raises(ReceiptUnavailable): service.observe(req)
    assert snapshot(service)==before
    # An explicitly new interpretation is allowed on intact legacy evidence.
    assert commit(service,service.read_evidence_interpretation(result['evidence_id']),semantics=[proposal()])['created']


def test_distinct_deletions_advance_epoch_once_and_keep_other_cards(service):
    req=source(model_interpretation='old private inference'); result=service.observe(req,semantics=[proposal(),proposal()])
    prepared=service.read_evidence_interpretation(result['evidence_id']); a,b=result['card_ids']
    other=MemoryService(uuid4()); other.ensure_tenant(); other.observe(source())
    untouched=snapshot(other); service.delete_card(a)
    e=row(service,'evidence',id=result['evidence_id'])
    assert e['interpretation_revoked'] and e['interpretation_epoch']==1 and e['version']==1
    assert e['content']==req.content and e['status']=='active'
    assert e['model_interpretation'] is None and e['request_fingerprint'] is None
    assert row(service,'semantic_cards',id=b)['lifecycle']=='active'
    assert row(service,'semantic_card_versions',card_id=b,version=1)['body']==proposal().body
    # Valid canonical support remains usable, as does a fresh correction source.
    make_projection(service,projection_type='relationship',projection_key='valid',scope='test',
        body={'summary':'valid B'},support_card_ids=[b])
    for action in [lambda: service.observe(req,semantics=[proposal(),proposal()]),
                   lambda: service.read_evidence_interpretation(result['evidence_id']),
                   lambda: commit(service,prepared,semantics=[proposal()])]:
        before=snapshot(service)
        with pytest.raises(InterpretationRevoked): action()
        assert snapshot(service)==before
    before=snapshot(service); service.delete_card(a); assert snapshot(service)==before
    service.correct_card(b,source('fresh-correction'),proposal().body,expected_version=1)
    service.delete_card(b)
    assert row(service,'evidence',id=result['evidence_id'])['interpretation_epoch']==2
    assert snapshot(other)==untouched


@pytest.mark.parametrize('kind',['observe','interpret'])
def test_trace_only_receipts_are_erased_by_source_delete(service,kind):
    req=source(); result=service.observe(req,trace={'observation':'private trace'} if kind=='observe' else None)
    intent=uuid4(); prepared=service.read_evidence_interpretation(result['evidence_id'])
    if kind=='interpret':
        result=commit(service,prepared,intent,trace={'observation':'private trace'},provenance={'model_id':'private-meaning'})
    service.delete_evidence(result['evidence_id']); before=snapshot(service)
    assert row(service,'life_traces',id=result['trace_id'])['observation']==''
    assert row(service,'observation_receipts',evidence_id=result['evidence_id'])['state']=='deleted'
    if kind=='interpret':
        record=row(service,'interpretation_intents',intent_id=intent)
        assert record['state']=='deleted' and record['request_fingerprint'] is None and record['provenance']=={}
        with pytest.raises(InterpretationRevoked): commit(service,prepared,intent,trace={'observation':'private trace'})
    with pytest.raises(DeletedSource): service.observe(req)
    service.delete_evidence(result['evidence_id']); assert snapshot(service)==before


@pytest.mark.parametrize('kind',['observe','interpret'])
def test_original_receipt_keeps_deletion_provenance_after_edges_change(service,kind):
    observed=service.observe(source(),semantics=[proposal()] if kind=='observe' else [])
    if kind=='interpret': observed=commit(service,service.read_evidence_interpretation(observed['evidence_id']),semantics=[proposal()])
    with tenant_transaction(service.tenant_id) as conn: conn.execute('DELETE FROM card_sources')
    service.delete_card(observed['card_ids'][0])
    assert row(service,'evidence',id=observed['evidence_id'])['interpretation_revoked']


@pytest.mark.parametrize('path',['delta','correct','close','mention','bind','relation-source','relation-evidence','relation-trace','discovery'])
def test_common_revoked_source_gate_covers_composite_writes(service,path):
    req=source(); observed=service.observe(req)
    prepared=service.read_evidence_interpretation(observed['evidence_id'])
    interpreted=commit(service,prepared,trace={'observation':'trace'},semantics=[proposal(),proposal()])
    cards=interpreted['card_ids']; a,b=cards
    mention=service.create_unbound_mention(req,'someone',[{'card_id':b,'reason':'test'}])
    service.delete_card(a); before=snapshot(service)
    relation=dict(from_kind='semantic_card',from_id=b,to_kind='semantic_card',to_id=b,
        family='context',relation_type='test')
    with pytest.raises(InterpretationRevoked):
        if path=='delta': service.add_event_delta(b,req,{'current_result':'new'},idempotency_key='d')
        elif path=='correct': service.correct_card(b,req,proposal().body,expected_version=1)
        elif path=='close': service.close_card(b,req,proposal().body,expected_version=1)
        elif path=='mention': service.create_unbound_mention(req,'someone',[])
        elif path=='bind': service.bind_mention(mention,b)
        elif path=='discovery': service.maintenance.enqueue_candidate_discovery(observed['evidence_id'])
        elif path=='relation-source': service.add_relation(**relation,source_evidence_id=observed['evidence_id'])
        else:
            relation.update(from_kind='evidence' if path=='relation-evidence' else 'life_trace',
                from_id=observed['evidence_id'] if path=='relation-evidence' else interpreted['trace_id'])
            service.add_relation(**relation)
    assert snapshot(service)==before


def test_explicit_save_requires_exact_host_auth_and_never_reopens_source(service):
    req,old,host,trusted,intent,prepared,items=recovery(service)
    with pytest.raises(AuthorizationRequired): service.read_explicit_reinterpretation(old['evidence_id'],explicit_save_intent_id=intent)
    with pytest.raises(AuthorizationRequired): service.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    with pytest.raises(AuthorizationRequired): trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=[proposal(canonical_key='unapproved')])
    first=trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    before=snapshot(service)
    assert trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)=={**first,'created':False}
    assert snapshot(service)==before and first['card_ids'][0] not in old['card_ids']
    assert row(service,'semantic_cards',id=old['card_ids'][0])['lifecycle']=='deleted'
    assert row(service,'evidence',id=old['evidence_id'])['interpretation_revoked']
    with pytest.raises(InterpretationRevoked): service.read_evidence_interpretation(old['evidence_id'])
    changed=[proposal(canonical_key='changed')];host.approve(intent,prepared,semantics=changed)
    with pytest.raises(IdempotencyConflict): trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=changed)
    service.delete_card(first['card_ids'][0]); before=snapshot(service)
    assert row(service,'evidence',id=old['evidence_id'])['interpretation_epoch']==2
    record=row(service,'interpretation_intents',intent_id=intent)
    assert record['state']=='revoked' and record['request_fingerprint'] is None and record['provenance']=={}
    with pytest.raises(InterpretationRevoked): trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    assert snapshot(service)==before


@pytest.mark.parametrize('mode',['ordinary','explicit'])
def test_sql_failure_after_receipt_rolls_back_and_intent_remains_retryable(service,monkeypatch,mode):
    if mode=='explicit':
        _,_,host,trusted,intent,prepared,items=recovery(service)
        action=lambda:trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    else:
        trusted=service; intent=uuid4(); prepared=service.read_evidence_interpretation(service.observe(source())['evidence_id'])
        action=lambda:commit(service,prepared,intent,trace={'observation':'trace'},semantics=[proposal()])
    before=snapshot(service); original=trusted._bump_revision
    monkeypatch.setattr(trusted,'_bump_revision',lambda conn:conn.execute('SELECT 1/0'))
    with pytest.raises(psycopg.errors.DivisionByZero): action()
    assert snapshot(service)==before and row(service,'interpretation_intents',intent_id=intent) is None
    monkeypatch.setattr(trusted,'_bump_revision',original)
    assert action()['created']


@pytest.mark.parametrize('mode',['ordinary','explicit'])
@pytest.mark.parametrize('delete_first',[True,False])
def test_interpretation_and_delete_have_real_database_order(service,monkeypatch,mode,delete_first):
    if mode=='explicit':
        _,old,host,trusted,intent,prepared,items=recovery(service)
        action=lambda:trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    else:
        old=service.observe(source(),semantics=[proposal(),proposal()]); prepared=service.read_evidence_interpretation(old['evidence_id'])
        action=lambda:commit(service,prepared,semantics=[proposal()])
    control=ControlledTransactions(monkeypatch,'delete' if delete_first else 'interpret',
        'FOR UPDATE' if delete_first else 'INSERT INTO interpretation_intents',after=delete_first)
    actions={'delete':lambda:service.delete_card(old['card_ids'][1]),'interpret':action}
    first,second=('delete','interpret') if delete_first else ('interpret','delete')
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(named,first,actions[first])
        try:
            assert control.ready.wait(5); b=pool.submit(named,second,actions[second]); control.wait_blocked(second,first)
        finally: control.release.set()
        a.result(timeout=8)
        if delete_first:
            with pytest.raises(VersionConflict): b.result(timeout=8)
        else:
            b.result(timeout=8)
            with tenant_transaction(service.tenant_id,mode='read') as conn:
                assert not conn.execute("SELECT 1 FROM interpretation_intents WHERE state='active'").fetchone()
    control.record(f'interpretation-delete:{mode}:{delete_first}')


@pytest.mark.parametrize('kind',['observe','ordinary','explicit'])
def test_concurrent_exact_requests_return_one_receipt(service,kind):
    req=source(); items=[proposal(),proposal()]
    if kind=='observe': action=lambda:service.observe(req,semantics=items)
    elif kind=='ordinary':
        prepared=service.read_evidence_interpretation(service.observe(req)['evidence_id']); intent=uuid4()
        action=lambda:commit(service,prepared,intent,semantics=items)
    else:
        _,_,host,trusted,intent,prepared,items=recovery(service)
        action=lambda:trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:action(),range(2)))
    assert sorted(r['created'] for r in results)==[False,True]
    assert results[0]['card_ids']==results[1]['card_ids']


def test_foreign_read_commit_and_new_tables_are_tenant_scoped(service):
    own=service.observe(source()); prepared=service.read_evidence_interpretation(own['evidence_id'])
    commit(service,prepared,semantics=[proposal()]); other=MemoryService(uuid4());other.ensure_tenant()
    with pytest.raises(NotFound): other.read_evidence_interpretation(own['evidence_id'])
    with pytest.raises(NotFound): commit(other,prepared,semantics=[proposal()])
    service.delete_card(row(service,'interpretation_intents',evidence_id=own['evidence_id'])['card_ids'][0])
    with tenant_transaction(other.tenant_id,mode='read') as conn:
        for table in ['observation_receipts','interpretation_intents','source_interpretation_revocations']:
            assert conn.execute('SELECT * FROM '+table).fetchall()==[]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_transaction(other.tenant_id) as conn:
            conn.execute("INSERT INTO source_interpretation_revocations VALUES (%s,%s,'evidence',%s)",
                (service.tenant_id,own['evidence_id'],uuid4()))


@pytest.mark.parametrize('kind',['observe','interpret','revoked','foreign'])
def test_005_down_guard_is_global_and_atomic(service,kind):
    target=service
    if kind=='foreign': target=MemoryService(uuid4());target.ensure_tenant()
    result=target.observe(source(),semantics=[proposal()])
    if kind=='interpret': commit(target,target.read_evidence_interpretation(result['evidence_id']),semantics=[proposal()])
    if kind=='revoked': target.delete_card(result['card_ids'][0])
    before=snapshot(target)
    with pytest.raises(psycopg.errors.RaiseException,match='005 downgrade requires'): migrate_down(1)
    assert [r['version'] for r in migration_status()]==[1,2,3,4,5]
    assert snapshot(target)==before


def test_005_reconciles_provable_old_deletions_without_inventing_receipts(service):
    assert migrate_down(1)==[5]
    try:
        # Pre-005 fixture made through SQL because new service requires new schema.
        with tenant_transaction(service.tenant_id) as conn:
            eid=conn.execute("""INSERT INTO evidence(tenant_id,modality,source_kind,content,occurred_at,request_fingerprint)
                VALUES (%s,'text','synthetic','raw kept',now(),%s) RETURNING id""",(service.tenant_id,'a'*64)).fetchone()['id']
            cid=conn.execute("""INSERT INTO semantic_cards(tenant_id,card_type,canonical_key,lifecycle,valid_at)
                VALUES (%s,'event','deleted synthetic','deleted',now()) RETURNING id""",(service.tenant_id,)).fetchone()['id']
            conn.execute("INSERT INTO card_sources(tenant_id,card_id,evidence_id,source_role) VALUES (%s,%s,%s,'support')",(service.tenant_id,cid,eid))
        assert migrate_up()==[5]
        e=row(service,'evidence',id=eid)
        assert e['interpretation_revoked'] and e['interpretation_epoch']==1 and e['content']=='raw kept'
        assert e['request_fingerprint'] is None and row(service,'observation_receipts',evidence_id=eid) is None
        with pytest.raises(InterpretationRevoked): service.read_evidence_interpretation(eid)
    finally: migrate_up()


def test_reobservation_digest_is_revoked_by_card_delete_and_retry_cleans_legacy_residue(service):
    from tests.test_reobservation import forgotten, request
    old=forgotten(service); req=request(); result=service.reobserve_deleted(old['evidence_id'],**req)
    service.delete_card(result['card_ids'][0])
    e=row(service,'evidence',id=result['evidence_id']); assert e['status']=='active' and e['interpretation_epoch']==1
    receipt=row(service,'reobservation_intents',intent_id=req['intent_id'])
    assert receipt['state']=='revoked' and receipt['request_fingerprint'] is None
    before=snapshot(service)
    with pytest.raises(InterpretationRevoked): service.reobserve_deleted(old['evidence_id'],**req)
    assert snapshot(service)==before
    # Simulate provable old residual metadata, not a new user authorization.
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE evidence SET request_fingerprint=%s,model_interpretation='old private inference' WHERE id=%s",('a'*64,result['evidence_id']))
        conn.execute("UPDATE reobservation_intents SET state='active',request_fingerprint=%s WHERE intent_id=%s",('b'*64,req['intent_id']))
    service.delete_card(result['card_ids'][0])
    assert snapshot(service)==before


@pytest.mark.parametrize('kind',['observe','interpret'])
def test_trace_only_receipt_revoked_without_erasing_unrequested_trace(service,kind):
    req=source(); observed=service.observe(req,trace={'observation':'kept trace'} if kind=='observe' else None)
    prepared=service.read_evidence_interpretation(observed['evidence_id']); intent=uuid4()
    result=observed if kind=='observe' else commit(service,prepared,intent,trace={'observation':'kept trace'},provenance={'model_id':'private-inference'})
    card=commit(service,prepared,semantics=[proposal()])['card_ids'][0]; service.delete_card(card)
    assert row(service,'life_traces',id=result['trace_id'])['observation']=='kept trace'
    assert row(service,'observation_receipts',evidence_id=observed['evidence_id'])['state']=='revoked'
    before=snapshot(service)
    with pytest.raises(InterpretationRevoked):
        if kind=='observe': service.observe(req,trace={'observation':'kept trace'})
        else: commit(service,prepared,intent,trace={'observation':'kept trace'},provenance={'model_id':'private-inference'})
    assert snapshot(service)==before
    if kind=='interpret':
        r=row(service,'interpretation_intents',intent_id=intent)
        assert r['provenance']=={} and r['request_fingerprint'] is None


def test_first_observe_receipt_sql_failure_is_atomic(service,monkeypatch):
    before=snapshot(service); original=service._bump_revision
    monkeypatch.setattr(service,'_bump_revision',lambda conn:conn.execute('SELECT 1/0'))
    with pytest.raises(psycopg.errors.DivisionByZero): service.observe(source(),trace={'observation':'trace'},semantics=[proposal()])
    assert snapshot(service)==before
    monkeypatch.setattr(service,'_bump_revision',original)
    assert service.observe(source(),trace={'observation':'trace'},semantics=[proposal()])['created']


def test_host_checks_frozen_output_before_lock_without_consuming_it(service,monkeypatch):
    _,old,host,trusted,intent,prepared,items=recovery(service)
    control=ControlledTransactions(monkeypatch,'writer','FOR UPDATE',after=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(named,'writer',lambda:trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items))
        try:
            assert control.ready.wait(4)
            assert host.checks[-1].phase=='commit'
            detached=host.checks[-1].payload; detached['output']['semantics'][0]['body']['current_result']='mutated'
            items[0].body['current_result']='mutated'
            # The host already returned before the writer takes the tenant gate.
        finally: control.release.set()
        result=task.result(timeout=8)
    assert row(service,'semantic_card_versions',card_id=result['card_ids'][0],version=1)['body']['current_result']=='before'
    assert host.checks[-1].payload['output']['semantics'][0]['body']['current_result']=='before'


def test_deleted_source_never_allows_explicit_reinterpretation(service):
    _,old,host,trusted,intent,prepared,items=recovery(service)
    service.delete_evidence(old['evidence_id']); before=snapshot(service)
    with pytest.raises(DeletedSource): trusted.read_explicit_reinterpretation(old['evidence_id'],explicit_save_intent_id=intent)
    with pytest.raises(DeletedSource): trusted.commit_explicit_reinterpretation(prepared,explicit_save_intent_id=intent,semantics=items)
    assert snapshot(service)==before


@pytest.mark.parametrize('table',['observation_receipts','interpretation_intents'])
def test_new_receipts_reject_cross_tenant_writes(service,table):
    result=service.observe(source()); other=MemoryService(uuid4());other.ensure_tenant(); before=snapshot(service)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_transaction(other.tenant_id) as conn:
            if table=='observation_receipts':
                conn.execute("INSERT INTO observation_receipts(tenant_id,evidence_id,state) VALUES (%s,%s,'active')",(service.tenant_id,result['evidence_id']))
            else:
                conn.execute("""INSERT INTO interpretation_intents(tenant_id,intent_id,evidence_id,mode,source_version,source_epoch,request_fingerprint,state)
                    VALUES (%s,%s,%s,'ordinary',1,0,%s,'active')""",(service.tenant_id,uuid4(),result['evidence_id'],'a'*64))
    assert snapshot(service)==before
