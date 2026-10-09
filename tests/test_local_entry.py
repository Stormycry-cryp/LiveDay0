"""Trusted local control plane plus untrusted proposal data; synthetic only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, InterpretationRevoked, NotFound, VersionConflict
from liveday0.extraction_contract import ContractViolation, ExtractionRequest, SourceSpan
from liveday0.local_console import run_synthetic_demo
from liveday0.local_extraction import DeterministicLocalAdapter, SYNTHETIC_EXAMPLES, saved_request
from liveday0.local_host import LocalMemorySession, local_tenant_identity
from tests.test_atomic_writes import snapshot

TEXTS=list(SYNTHETIC_EXAMPLES)
STAMP='2026-10-09T00:00:00+00:00'


def command(text=TEXTS[0],**changes):
    return {'action':'capture','text':text,'message_id':str(uuid4()),'occurred_at':STAMP,**changes}


def state(session): return snapshot(MemoryService(session.tenant_id))


def first(session,**changes): return session.handle(command(**changes))['outcomes'][0]


class MutatedAdapter:
    production_ready=False
    def __init__(self,mutate): self.mutate=mutate
    def extract(self,request):
        value=DeterministicLocalAdapter().extract(request).to_dict()
        self.mutate(value,request)
        return value


@pytest.mark.parametrize('field',['tenant','tenant_id','user_id','explicit_save','approved'])
def test_request_cannot_supply_identity_or_authorization(field):
    session=LocalMemorySession();before=state(session)
    with pytest.raises(ValueError,match='fields'): session.handle(command(**{field:str(uuid4())}))
    assert state(session)==before


def test_identity_comes_from_os_not_request_environment(monkeypatch):
    before=local_tenant_identity()
    monkeypatch.setenv('USER','model-self-reported-user');monkeypatch.setenv('HOME','/forged-home')
    assert local_tenant_identity()==before
    with pytest.raises(TypeError): LocalMemorySession(tenant_id=uuid4())


def test_new_session_preserves_complete_source_receipt():
    session=LocalMemorySession();req=command();a=session.handle(req)['outcomes'][0];before=state(session)
    again=LocalMemorySession().handle(req)['outcomes'][0]
    assert again['card_ids']==a['card_ids'] and again['evidence_id']==a['evidence_id'] and not again['created']
    assert state(session)==before
    with pytest.raises(VersionConflict): session.handle({**req,'text':TEXTS[1]})
    assert state(session)==before


@pytest.mark.parametrize('change',['unknown','too-long','malformed-id','missing-time'])
def test_invalid_or_unknown_text_does_not_persist(change):
    session=LocalMemorySession();req=command();before=state(session)
    if change=='unknown':
        result=session.handle({**req,'text':'不在合成固定样例中的文本'})
        assert result['outcomes'][0]['reason']=='unsupported_synthetic_text'
    else:
        if change=='too-long': req['text']='x'*12001
        elif change=='malformed-id': req['message_id']='not-an-opaque-uuid'
        else: req.pop('occurred_at')
        with pytest.raises(ValueError): session.handle(req)
    assert state(session)==before


@pytest.mark.parametrize('path,value,reason',[
    ('speech_mode','quotation','non_authoritative_speech_mode'),
    ('speech_mode','hypothetical','non_authoritative_speech_mode'),
    ('speech_mode','reported_statement','non_authoritative_speech_mode'),
    ('speaker','assistant','untrusted_subject_or_speaker'),
    ('epistemic_state','inferred','not_asserted'),
    ('privacy_class','never_store','never_store'),
])
def test_declared_non_authoritative_or_forbidden_proposals_are_rejected(path,value,reason):
    session=LocalMemorySession(adapter=MutatedAdapter(lambda b,r:b['proposals'][0].__setitem__(path,value)))
    before=state(session);result=session.handle(command())
    assert result['outcomes'][0]['reason']==reason and state(session)==before


@pytest.mark.parametrize('case',['secret','sensitive-model-flag','sensitive-proposal'])
def test_model_explicit_save_never_substitutes_for_human_consent(case):
    def mutate(b,r):
        p=b['proposals'][0];p['persistence_intent']='explicit_save'
        if case=='sensitive-proposal':p['privacy_class']='sensitive'
    session=LocalMemorySession(adapter=MutatedAdapter(mutate));before=state(session)
    if case=='secret':
        assert session.handle(command(sensitivity='secret'))['outcomes'][0]['reason']=='secret_source'
    else:
        with pytest.raises(AuthorizationRequired): session.handle(command(sensitivity='sensitive' if case=='sensitive-model-flag' else 'ordinary'))
    assert state(session)==before


@pytest.mark.parametrize('case',['tenant-field','span','digest','duplicate-span','revision','forget'])
def test_closed_schema_and_reference_validator_precede_storage(case):
    def mutate(b,r):
        p=b['proposals'][0]
        if case=='tenant-field':p['tenant_id']=str(uuid4())
        elif case=='span':p['source']['span']['end']+=1
        elif case=='digest':p['source']['span']['digest']='sha256:'+'0'*64
        elif case=='duplicate-span':
            other=deepcopy(p);other['proposal_id']='other';b['proposals'].append(other)
        elif case=='revision':p['revision']['intent']='revise'
        else:p['revision']['intent']='forget';p['persistence_intent']='explicit_forget';p['semantic_input']=None
    session=LocalMemorySession(adapter=MutatedAdapter(mutate));before=state(session)
    if case=='tenant-field':
        with pytest.raises(ContractViolation):session.handle(command())
    else:
        result=session.handle(command())
        assert all(x['status']=='rejected' for x in result['outcomes'])
    assert state(session)==before


def test_never_store_overlap_blocks_otherwise_eligible_span():
    def mutate(b,r):
        forbidden=deepcopy(b['proposals'][0]);forbidden['proposal_id']='never'
        forbidden['privacy_class']='never_store';forbidden['source']['span']=SourceSpan.from_text(0,r.content[:5]).to_dict()
        b['proposals'].append(forbidden)
    session=LocalMemorySession(adapter=MutatedAdapter(mutate));before=state(session)
    result=session.handle(command())
    assert [r['reason'] for r in result['outcomes']]==['overlaps_never_store','never_store']
    assert state(session)==before


def test_only_accepted_span_and_stable_locator_persist_not_surrounding_digests():
    prefix='UNAPPROVED_SYNTHETIC_SURROUNDINGS:';text=prefix+TEXTS[0]
    class SpanAdapter:
        def extract(self,request):
            inner=ExtractionRequest.build(request.source.source_id,'1',TEXTS[0],request.occurred_at,chunk_start=len(prefix))
            batch=DeterministicLocalAdapter().extract(inner).to_dict()
            batch['request_fingerprint']=request.fingerprint
            batch['proposals'][0]['source']['content_digest']=request.content_digest
            return batch
    session=LocalMemorySession(adapter=SpanAdapter());req=command(text=text);result=session.handle(req)['outcomes'][0]
    service=MemoryService(session.tenant_id)
    with tenant_transaction(session.tenant_id,mode='read') as conn:
        e=conn.execute('SELECT * FROM evidence WHERE id=%s',(result['evidence_id'],)).fetchone()
    locator=json.loads(e['object_ref']);assert set(locator)=={'schema','message_id','version','start','end','sensitivity'}
    assert e['content']==TEXTS[0] and locator['start']==len(prefix)
    original=ExtractionRequest.build(req['message_id'],'1',text,STAMP)
    durable=json.dumps(state(session))
    assert prefix not in durable and original.content_digest not in durable and original.fingerprint not in durable
    prepared=service.read_evidence_interpretation(result['evidence_id'])
    restored=saved_request(prepared)
    assert restored.content==TEXTS[0] and restored.chunk_start==len(prefix)


def test_correct_delete_require_exact_human_control_and_keep_unrelated_memory():
    checks=[]
    session=LocalMemorySession(confirm=lambda check:checks.append(check) or True)
    item=first(session);unrelated=first(session,text=TEXTS[2]);before=state(session)
    unapproved=LocalMemorySession()
    with pytest.raises(AuthorizationRequired): unapproved.handle({'action':'delete','kind':'card','id':str(item['card_ids'][0])})
    with pytest.raises(AuthorizationRequired): unapproved.handle({'action':'correct','card_id':str(item['card_ids'][0]),'expected_version':1,'text':TEXTS[1]})
    assert state(session)==before
    result=session.handle({'action':'correct','card_id':str(item['card_ids'][0]),'expected_version':1,'text':TEXTS[1]})
    assert result['outcomes'][0]['version']==2 and checks[-1].action=='correct'
    context=json.dumps(session.handle({'action':'query','query':'合成搬家'}),ensure_ascii=False)
    assert '搬家完成' in context and '等待朋友帮忙' not in context
    session.handle({'action':'delete','kind':'card','id':str(item['card_ids'][0])})
    assert checks[-1].action=='delete'
    with tenant_transaction(session.tenant_id,mode='read') as conn:
        assert conn.execute('SELECT lifecycle FROM semantic_cards WHERE id=%s',(unrelated['card_ids'][0],)).fetchone()['lifecycle']=='active'


def test_interpretation_and_explicit_restore_use_host_only_consent_and_erase_provenance():
    checks=[];session=LocalMemorySession(confirm=lambda c:checks.append(c) or True)
    item=first(session);intent=str(uuid4())
    interpreted=session.handle({'action':'interpret','evidence_id':str(item['evidence_id']),'intent_id':intent})
    assert interpreted['evidence_id']==item['evidence_id']
    session.handle({'action':'delete','kind':'card','id':str(item['card_ids'][0])})
    before=state(session)
    with pytest.raises(InterpretationRevoked): session.handle({'action':'interpret','evidence_id':str(item['evidence_id'])})
    with pytest.raises(AuthorizationRequired): LocalMemorySession().handle({'action':'restore','evidence_id':str(item['evidence_id'])})
    assert state(session)==before
    restored=session.handle({'action':'restore','evidence_id':str(item['evidence_id']),'intent_id':str(uuid4())})
    assert checks[-2].action=='read-revoked-source' and checks[-1].action=='restore-exact-output'
    assert checks[-1].payload['input']['source']['interpretation_revoked'] is True
    assert restored['card_ids'][0] not in item['card_ids']
    with pytest.raises(InterpretationRevoked): session.handle({'action':'interpret','evidence_id':str(item['evidence_id'])})
    session.handle({'action':'delete','kind':'evidence','id':str(item['evidence_id'])})
    with tenant_transaction(session.tenant_id,mode='read') as conn:
        assert not conn.execute("SELECT 1 FROM interpretation_intents WHERE provenance<>'{}' OR request_fingerprint IS NOT NULL").fetchone()


def test_foreign_targets_are_not_reachable_through_local_session():
    session=LocalMemorySession(confirm=lambda _:True);other=MemoryService(uuid4());other.ensure_tenant()
    from tests.test_atomic_writes import source, proposal
    item=other.observe(source(),semantics=[proposal()]);before=state(session);foreign=snapshot(other)
    with pytest.raises(NotFound):session.handle({'action':'delete','kind':'card','id':str(item['card_ids'][0])})
    with pytest.raises(NotFound):session.handle({'action':'interpret','evidence_id':str(item['evidence_id'])})
    assert state(session)==before and snapshot(other)==foreign


def test_fixed_synthetic_demo_covers_life_loop_and_declares_limitations():
    result=run_synthetic_demo()
    assert result['synthetic'] and result['adapter_production_ready'] is False
    assert result['identity_source']=='current-os-login'
    assert 'not real human' in result['consent']
    assert {'capture','query','correct','delete','interpret','restore','automatic-after-restore'} <= {s['action'] for s in result['steps']}


def test_restore_authorizer_is_read_only_and_bound_to_exact_payload():
    from liveday0.types import ExplicitSaveAuthorization
    from liveday0.serialization import canonical_json
    calls=[];session=LocalMemorySession(confirm=lambda c:calls.append(c) or True)
    intent=uuid4();payload={'intent_id':intent,'input':{'source_epoch':1},'output':{'text':'synthetic'}}
    check=ExplicitSaveAuthorization(session.tenant_id,intent,'commit',canonical_json(payload))
    assert session._explicit_check(check) is False and calls==[]
    session._approve('restore-exact-output',payload)
    approved=set(session._approved)
    assert session._explicit_check(check) is True and len(calls)==1
    changed=ExplicitSaveAuthorization(session.tenant_id,intent,'commit',canonical_json({**payload,'output':{'text':'changed'}}))
    assert session._explicit_check(changed) is False
    assert session._approved==approved and len(calls)==1


def test_changed_span_cannot_allocate_new_identity_after_deletion():
    session=LocalMemorySession(confirm=lambda _:True);req=command()
    item=session.handle(req)['outcomes'][0]
    session.handle({'action':'delete','kind':'card','id':str(item['card_ids'][0])})
    before=state(session)
    with pytest.raises(InterpretationRevoked):session.handle({**req,'text':TEXTS[1]})
    assert state(session)==before


def test_multiple_accepted_spans_are_rejected_before_any_write():
    def mutate(b,r):
        first=b['proposals'][0];second=deepcopy(first);second['proposal_id']='second'
        first['source']['span']=SourceSpan.from_text(0,r.content[:6]).to_dict()
        second['source']['span']=SourceSpan.from_text(6,r.content[6:]).to_dict()
        b['proposals'].append(second)
    session=LocalMemorySession(adapter=MutatedAdapter(mutate));before=state(session)
    with pytest.raises(ValueError,match='one accepted span'):session.handle(command())
    assert state(session)==before


def test_approved_sensitive_capture_preserves_gate_during_later_interpretation():
    checks=[]
    session=LocalMemorySession(confirm=lambda c:checks.append(c) or True,
        adapter=MutatedAdapter(lambda b,r:b['proposals'][0].__setitem__('privacy_class','sensitive')))
    item=first(session)
    assert checks[-1].action=='save-sensitive'
    with tenant_transaction(session.tenant_id,mode='read') as conn:
        row=conn.execute('SELECT object_ref FROM evidence WHERE id=%s',(item['evidence_id'],)).fetchone()
    assert json.loads(row['object_ref'])['sensitivity']=='sensitive'
    before=state(session)
    # The new adapter labels its proposal ordinary; stored sensitivity still wins.
    with pytest.raises(AuthorizationRequired):
        LocalMemorySession().handle({'action':'interpret','evidence_id':str(item['evidence_id'])})
    assert state(session)==before
    approved=LocalMemorySession(confirm=lambda c:checks.append(c) or True)
    approved.handle({'action':'interpret','evidence_id':str(item['evidence_id'])})
    assert checks[-1].action=='interpret-sensitive'


def test_declared_secret_never_calls_adapter_or_persists():
    class CountingAdapter:
        calls=0
        def extract(self,request):
            self.calls+=1
            raise AssertionError('secret must be stopped before extraction')
    adapter=CountingAdapter();session=LocalMemorySession(adapter=adapter);before=state(session)
    result=session.handle(command(sensitivity='secret'))
    assert result['outcomes']==[{'status':'rejected','reason':'secret_source'}]
    assert adapter.calls==0 and state(session)==before
