"""Human confirmation is outside locks; deletion revalidates inside its write gate."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.exceptions import DeletedSource, NotFound, VersionConflict
from liveday0.local_host import LocalMemorySession
from liveday0.serialization import canonical_json
from tests.test_atomic_writes import snapshot, source, proposal
from tests.test_trust_boundaries import ControlledTransactions, named
from tests.test_local_entry import command


def mutation(service, item, kind):
    if kind=='card':
        return lambda:service.correct_card(item['card_ids'][0],source(str(uuid4())),
            {'goal_context':'synthetic','current_result':'newly corrected'},expected_version=1)
    prepared=service.read_evidence_interpretation(item['evidence_id'])
    return lambda:service.commit_evidence_interpretation(prepared,intent_id=uuid4(),semantics=[proposal()])


def target(item, kind):
    return item['card_ids'][0] if kind=='card' else item['evidence_id']


def delete(service, item, kind, prepared):
    fn=service.delete_card if kind=='card' else service.delete_evidence
    return fn(target(item,kind),expected_deletion=prepared)


@pytest.mark.parametrize('kind',['card','evidence'])
def test_confirmation_wait_allows_writer_and_changed_scope_requires_new_confirmation(kind,monkeypatch):
    ControlledTransactions(monkeypatch,"unused", "never-pause")  # Bound SQL waits even on failure.
    session=LocalMemorySession();service=MemoryService(session.tenant_id)
    item=session.handle(command())['outcomes'][0];change=mutation(service,item,kind)
    checks=[];after_change=[]
    def confirm(check):
        checks.append(check)
        if len(checks)==1:
            # A separate connection must finish while human confirmation is open.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(change).result(timeout=3)
            after_change.append(snapshot(service))
        return True
    session._confirm=confirm
    request={'action':'delete','kind':kind,'id':str(target(item,kind))}
    with pytest.raises(VersionConflict,match='confirm again'):session.handle(request)
    assert snapshot(service)==after_change[0]
    session.handle(request)
    assert len(checks)==2 and checks[0].fingerprint!=checks[1].fingerprint


@pytest.mark.parametrize('kind',['card','evidence'])
def test_target_version_and_affected_set_are_checked_beyond_tenant_revision(service,kind):
    item=service.observe(source(),semantics=[proposal()])
    prepared=service.read_deletion(kind,target(item,kind));old=prepared.payload
    mutation(service,item,kind)();fresh=service.read_deletion(kind,target(item,kind)).payload
    if kind=='card':assert old['target']['current_version']!=fresh['target']['current_version']
    else:
        assert old['target']==fresh['target']  # Source version did not change.
        assert len(fresh['affected']['cards'])==len(old['affected']['cards'])+1
    # Refresh only the coarse guard: the actual old target/dependencies still fail.
    old['tenant_revision']=fresh['tenant_revision']
    stale=replace(prepared,canonical_input=canonical_json(old));before=snapshot(service)
    with pytest.raises(VersionConflict,match='scope changed'):delete(service,item,kind,stale)
    assert snapshot(service)==before


@pytest.mark.parametrize('kind',['card','evidence'])
@pytest.mark.parametrize('delete_first',[False,True])
def test_delete_and_new_content_obey_real_database_lock_order(service,monkeypatch,kind,delete_first):
    item=service.observe(source(),semantics=[proposal()]);prepared=service.read_deletion(kind,target(item,kind))
    change=mutation(service,item,kind);snapshots={}
    def change_and_snapshot():
        result=change();snapshots['change']=snapshot(service);return result
    def delete_and_snapshot():
        delete(service,item,kind,prepared);snapshots['delete']=snapshot(service)
    actions={'change':change_and_snapshot,'delete':delete_and_snapshot}
    first,second=('delete','change') if delete_first else ('change','delete')
    control=ControlledTransactions(monkeypatch,first,'FOR UPDATE',after=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(named,first,actions[first])
        try:
            assert control.ready.wait(5)
            b=pool.submit(named,second,actions[second])
            control.wait_blocked(second,first)
        finally:control.release.set()
        a.result(timeout=6)
        if delete_first:
            expected=ValueError if kind=='card' else DeletedSource
            with pytest.raises(expected):b.result(timeout=6)
            assert snapshot(service)==snapshots['delete']
        else:
            with pytest.raises(VersionConflict,match='scope changed'):b.result(timeout=6)
            assert snapshot(service)==snapshots['change']
    control.record(f'deletion-confirmation:{kind}:delete-first={delete_first}')


def test_prepared_deletion_is_detached_and_cannot_change_tenant_or_target(service):
    item=service.observe(source(),semantics=[proposal()]);prepared=service.read_deletion('card',item['card_ids'][0])
    changed=prepared.payload;changed['target']['body']['current_result']='forged preview'
    assert prepared.payload['target']['body']['current_result']=='before'
    other=MemoryService(uuid4());other.ensure_tenant();before=snapshot(service);foreign=snapshot(other)
    with pytest.raises(NotFound):other.read_deletion('card',item['card_ids'][0])
    with pytest.raises(NotFound):other.delete_card(item['card_ids'][0],expected_deletion=prepared)
    with pytest.raises(VersionConflict,match='target changed'):service.delete_evidence(item['evidence_id'],expected_deletion=prepared)
    assert snapshot(service)==before and snapshot(other)==foreign
