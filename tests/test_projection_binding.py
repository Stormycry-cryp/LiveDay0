from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from uuid import uuid4

import psycopg
import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, VersionConflict
from tests.helpers import evidence, event, make_projection
from tests.test_atomic_writes import snapshot
from tests.test_trust_boundaries import ControlledTransactions, named


def seed(service, label="support"):
    return service.observe(evidence(label, key=label), semantics=[
        event({"goal_context":"synthetic binding", "current_result":label}, key=label)])


def refs(service):
    return seed(service), seed(service,"counter")


def read_new(service, a, b, key="view"):
    return service.maintenance.read_projection_creation(projection_type="relationship", projection_key=key,
        scope="declared scope", support_card_ids=a["card_ids"], counter_card_ids=b["card_ids"])


def setup(service, mode):
    a,b=refs(service); prepared=read_new(service,a,b)
    if mode=="update":
        service.materialize_projection(prepared=prepared,body={"summary":"previous"})
        prepared=service.maintenance.read_projection_update(prepared.projection_id)
    return a,b,prepared


def commit(service, prepared, body=None):
    return service.maintenance.commit_projection(prepared,replacement_body=body or {"summary":"bound body"})


def test_legacy_materialize_is_rejected_without_writes(service):
    a=seed(service); before=snapshot(service)
    with pytest.raises(VersionConflict,match="read_projection_creation"):
        service.materialize_projection(projection_type="relationship",projection_key="legacy",scope="old",
            support_card_ids=a["card_ids"],body={"summary":"unbound old body"})
    assert snapshot(service)==before


def test_raw_worker_output_is_rejected_before_claim_or_wake(service):
    a=seed(service)
    metadata=dict(projection_type="relationship",projection_key="legacy",scope="old",support_card_ids=a["card_ids"])
    # Only fixture setup spans the old/new API, so this regression runs on the old HEAD too.
    if hasattr(service.maintenance,"read_projection_creation"):
        pid=make_projection(service,body={"summary":"original"},**metadata)
    else:
        pid=service.materialize_projection(body={"summary":"original"},**metadata)
    service.correct_card(a["card_ids"][0],evidence("changed"),
        {"goal_context":"synthetic binding","current_result":"new"},expected_version=1)
    service.maintenance.run_ready(limit=1);before=snapshot(service)
    with pytest.raises(ValueError,match="unbound"):
        service.maintenance.run_ready(projection_outputs={pid:{"summary":"stale synthesized body"}})
    assert snapshot(service)==before


@pytest.mark.parametrize("mode",["create","update"])
@pytest.mark.parametrize("change",["support-correction","counter-correction","support-delete","counter-delete",
    "source-version","source-status","new-source","source-role","safe-pending","counter-pending","unsafe-pending"])
def test_stale_source_or_canonical_read_is_rejected_without_side_effects(service,mode,change):
    a,b,p=setup(service,mode);cid=a["card_ids"][0];other=b["card_ids"][0]
    if change.endswith("correction"):
        target=other if change.startswith("counter") else cid
        service.correct_card(target,evidence("correction"),{"goal_context":"binding","current_result":"changed"},expected_version=1)
    elif change.endswith("delete"):
        service.delete_evidence(b["evidence_id"] if change.startswith("counter") else a["evidence_id"])
    elif "pending" in change:
        target=other if change.startswith("counter") else cid
        service.add_event_delta(target,evidence("pending"),
            {"current_result":"pending result","requires_restructure":change.startswith("unsafe")},idempotency_key="delta")
    else:
        new_id=service.observe(evidence("new linked source"))["evidence_id"] if change=="new-source" else None
        with tenant_transaction(service.tenant_id) as conn:
            if change=="source-version":conn.execute("UPDATE evidence SET version=version+1 WHERE id=%s",(a["evidence_id"],))
            elif change=="source-status":conn.execute("UPDATE evidence SET status='corrected' WHERE id=%s",(a["evidence_id"],))
            elif change=="source-role":conn.execute("UPDATE card_sources SET source_role='counterevidence' WHERE evidence_id=%s",(a["evidence_id"],))
            else:conn.execute("INSERT INTO card_sources VALUES (%s,%s,%s,'support')",(service.tenant_id,cid,new_id))
    before=snapshot(service)
    with pytest.raises((VersionConflict,NotFound)):commit(service,p)
    assert snapshot(service)==before


@pytest.mark.parametrize("change",["add-counter","remove-counter","change-role","add-support","remove-support",
    "target-version","target-scope","target-key","target-lifecycle","target-epistemic"])
def test_complete_target_and_dependency_set_is_compared(service,change):
    a,b,p=setup(service,"update");third=seed(service,"third");pid=p.projection_id
    with tenant_transaction(service.tenant_id) as conn:
        if change.startswith("add-"):
            role="support" if change=="add-support" else "counterevidence"
            conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,%s)",(service.tenant_id,pid,third["card_ids"][0],role))
        elif change.startswith("remove-"):
            role="support" if change=="remove-support" else "counterevidence"
            conn.execute("DELETE FROM projection_supports WHERE projection_id=%s AND support_role=%s",(pid,role))
        elif change=="change-role":
            conn.execute("UPDATE projection_supports SET support_role='support' WHERE projection_id=%s AND card_id=%s",(pid,b["card_ids"][0]))
        else:
            statements={"target-version":"current_version=current_version+1","target-scope":"scope='changed'",
                "target-key":"projection_key='changed'","target-lifecycle":"lifecycle='dormant'","target-epistemic":"epistemic_state='candidate'"}
            conn.execute("UPDATE projections SET "+statements[change]+" WHERE id=%s",(pid,))
    before=snapshot(service)
    with pytest.raises(VersionConflict):commit(service,p)
    assert snapshot(service)==before


@pytest.mark.parametrize("mode",["create","update"])
def test_synthesis_is_outside_lock_and_unrelated_changes_do_not_conflict(service,mode):
    a,b,p=setup(service,mode);other=MemoryService(uuid4());other.ensure_tenant()
    # These writes complete between the read and commit, with no held model/synthesis lock.
    seed(other,"another tenant");unrelated=seed(service,"unrelated")
    service.add_event_delta(unrelated["card_ids"][0],evidence("unrelated pending"),{"current_result":"unrelated"},idempotency_key="other")
    out=commit(service,p)
    assert out["version"]==p.target_version+1 and out["lifecycle"]=="active"
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        body=conn.execute("SELECT body FROM projection_versions WHERE projection_id=%s AND version=%s",(p.projection_id,out["version"])).fetchone()["body"]
        assert body["support_versions"]=={str(a["card_ids"][0]):1}
        assert body["counterevidence_versions"]=={str(b["card_ids"][0]):1}
        assert body["projection_input_fingerprint"]==p.fingerprint


def test_creation_key_race_and_duplicate_prepared_commit_are_side_effect_free(service):
    a,b=refs(service);p=read_new(service,a,b);competitor=read_new(service,a,b)
    commit(service,competitor);before=snapshot(service)
    for old in [p,competitor]:
        with pytest.raises(VersionConflict):commit(service,old)
        assert snapshot(service)==before


@pytest.mark.parametrize("role",["support","counterevidence"])
def test_pending_blocks_read_before_synthesis_and_catchup_allows_fresh_input(service,role):
    a,b,p=setup(service,"update");target=a if role=="support" else b
    service.add_event_delta(target["card_ids"][0],evidence("pending"),{"current_result":"latest"},idempotency_key="pending")
    for read in [lambda:read_new(service,a,b,key="new"),lambda:service.maintenance.read_projection_update(p.projection_id)]:
        before=snapshot(service)
        with pytest.raises(VersionConflict,match="pending"):read()
        assert snapshot(service)==before
    service.maintenance.make_pending_ready(job_type="event_rewrite");service.maintenance.run_ready(limit=5)
    fresh=service.maintenance.read_projection_update(p.projection_id);commit(service,fresh)
    assert next(x for x in fresh.payload["dependencies"] if x["role"]==role)["version"]==2


def test_worker_waits_without_copying_body_and_bound_commit_preserves_failure_budget(service,monkeypatch):
    a,b,p=setup(service,"update");pid=p.projection_id
    with tenant_transaction(service.tenant_id) as conn:
        service._enqueue_job_conn(conn,job_type="projection_resynthesis",target_kind="projection",target_id=pid,
            coalesce_key=f"projection_resynthesis:{pid}",baseline_version=1,available_after_seconds=0)
    original=service.maintenance._resynthesize_projection
    monkeypatch.setattr(service.maintenance,"_resynthesize_projection",lambda conn,*args:conn.execute("SELECT 1/0"))
    assert service.maintenance.run_ready(limit=1)[0]["state"]=="retry"
    monkeypatch.setattr(service.maintenance,"_resynthesize_projection",original)
    service.maintenance.make_retries_ready();assert service.maintenance.run_ready(limit=1)[0]["state"]=="waiting"
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        before=conn.execute("SELECT * FROM maintenance_jobs WHERE target_id=%s",(pid,)).fetchone()
        assert conn.execute("SELECT current_version FROM projections WHERE id=%s",(pid,)).fetchone()["current_version"]==1
    assert service.maintenance.run_ready(limit=10)==[]
    commit(service,service.maintenance.read_projection_update(pid))
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        after=conn.execute("SELECT * FROM maintenance_jobs WHERE target_id=%s",(pid,)).fetchone()
        assert after["id"]==before["id"] and after["state"]=="succeeded"
        assert after["failure_count"]==before["failure_count"]==1 and after["attempts"]==before["attempts"]


def test_dead_budget_cannot_be_bypassed_by_ordinary_bound_commit(service):
    a,b,p=setup(service,"update");pid=p.projection_id
    with tenant_transaction(service.tenant_id) as conn:
        service._enqueue_job_conn(conn,job_type="projection_resynthesis",target_kind="projection",target_id=pid,
            coalesce_key=f"projection_resynthesis:{pid}",baseline_version=1,available_after_seconds=0)
    for _ in range(3):
        service.maintenance.make_retries_ready();service.maintenance.run_ready(limit=1,fail_job_types={"projection_resynthesis"})
    before=snapshot(service)
    with pytest.raises(VersionConflict,match="terminal"):commit(service,p)
    assert snapshot(service)==before


def test_dormant_update_remains_dormant(service):
    a,b,p=setup(service,"update")
    with tenant_transaction(service.tenant_id) as conn:conn.execute("UPDATE projections SET lifecycle='dormant' WHERE id=%s",(p.projection_id,))
    fresh=service.maintenance.read_projection_update(p.projection_id)
    assert commit(service,fresh)["lifecycle"]=="dormant"


@pytest.mark.parametrize("mode",["create","update"])
@pytest.mark.parametrize("delete_first",[False,True])
def test_projection_commit_and_source_delete_are_ordered(service,monkeypatch,mode,delete_first):
    a,b,p=setup(service,mode)
    control=ControlledTransactions(monkeypatch,"delete" if delete_first else "commit","FOR UPDATE",after=True)
    write=lambda:commit(service,p,{"summary":"private bound output"})
    erase=lambda:service.delete_evidence(a["evidence_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(named,"delete" if delete_first else "commit",erase if delete_first else write)
        try:
            assert control.ready.wait(3)
            second=pool.submit(named,"commit" if delete_first else "delete",write if delete_first else erase)
            control.wait_blocked("commit" if delete_first else "delete","delete" if delete_first else "commit")
        finally:control.release.set()
        first.result(timeout=8)
        if delete_first:
            with pytest.raises(VersionConflict):second.result(timeout=8)
        else:second.result(timeout=8)
    control.record(f"projection-binding:{mode}:delete-first={delete_first}")
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        assert not conn.execute("SELECT 1 FROM projection_versions WHERE body::text LIKE '%private bound output%'").fetchone()


def test_mutable_payload_access_and_foreign_prepared_cannot_change_contract(service):
    a,b,p=setup(service,"create");payload=p.payload;payload["dependencies"].clear()
    other=MemoryService(uuid4());other.ensure_tenant();before=snapshot(other)
    with pytest.raises(NotFound):commit(other,p)
    assert snapshot(other)==before and len(p.payload["dependencies"])==2
    commit(service,p)


@pytest.mark.parametrize("mode",["create","update"])
def test_commit_sql_failure_rolls_back_every_side_effect(service,monkeypatch,mode):
    a,b,p=setup(service,mode);before=snapshot(service)
    control=ControlledTransactions(monkeypatch,"unused","never")
    original=control.original
    class FailedConnection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def __enter__(self):self.conn.__enter__();return self
        def __exit__(self,*args):return self.conn.__exit__(*args)
        def execute(self,sql,params=None,*args,**kwargs):
            if "UPDATE tenants SET revision" in sql:self.conn.execute("SELECT 1/0")
            return self.conn.execute(sql,params,*args,**kwargs)
    import liveday0.db as db
    monkeypatch.setattr(db,"connect",lambda **kwargs:FailedConnection(original(**kwargs)))
    with pytest.raises(psycopg.errors.DivisionByZero):commit(service,p)
    assert snapshot(service)==before


@pytest.mark.parametrize("mode",["create","update"])
def test_two_concurrent_publications_cannot_stamp_stale_body(service,monkeypatch,mode):
    a,b,p=setup(service,mode)
    control=ControlledTransactions(monkeypatch,"first","FOR UPDATE",after=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(named,"first",lambda:commit(service,p,{"summary":"winning output"}))
        try:
            assert control.ready.wait(3)
            second=pool.submit(named,"second",lambda:commit(service,p,{"summary":"stale losing output"}))
            control.wait_blocked("second","first")
        finally:control.release.set()
        first.result(timeout=8)
        before=snapshot(service)
        with pytest.raises(VersionConflict):second.result(timeout=8)
        assert snapshot(service)==before
    control.record(f"projection-two-publications:{mode}")
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        assert not conn.execute("SELECT 1 FROM projection_versions WHERE body::text LIKE '%stale losing output%'").fetchone()


@pytest.mark.parametrize("mode",["create","update"])
def test_output_body_is_detached_before_commit_wait(service,monkeypatch,mode):
    a,b,p=setup(service,mode);body={"summary":{"text":"frozen output"}}
    control=ControlledTransactions(monkeypatch,"commit","FOR UPDATE",after=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(named,"commit",lambda:commit(service,p,body))
        try:
            assert control.ready.wait(3);body["summary"]["text"]="changed during wait"
        finally:control.release.set()
        result=future.result(timeout=8)
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        saved=conn.execute("SELECT body FROM projection_versions WHERE projection_id=%s AND version=%s",(p.projection_id,result["version"])).fetchone()["body"]
        assert saved["summary"]=={"text":"frozen output"}


@pytest.mark.parametrize("change,expected", [
    ("deleted", VersionConflict), ("missing", NotFound), ("erasure-marker", VersionConflict),
])
def test_target_disappears_or_is_erased_after_update_read_without_source_change(service, change, expected):
    a, b, prepared = setup(service, "update")
    pid = prepared.projection_id
    initial = snapshot(service)
    with tenant_transaction(service.tenant_id) as conn:
        service._enqueue_job_conn(conn, job_type="projection_resynthesis", target_kind="projection",
            target_id=pid, coalesce_key=f"projection_resynthesis:{pid}", baseline_version=1,
            available_after_seconds=0)
        conn.execute("""UPDATE maintenance_jobs SET state='waiting', attempts=2, failure_count=1,
            wait_reason='semantic_output_required' WHERE tenant_id=%s AND target_id=%s""",
            (service.tenant_id, pid))
        if change == "deleted":
            conn.execute("UPDATE projections SET lifecycle='deleted' WHERE tenant_id=%s AND id=%s",
                (service.tenant_id, pid))
        elif change == "missing":
            # Isolated legacy/repair fixture: remove dependent rows without disabling FKs.
            conn.execute("DELETE FROM projection_versions WHERE tenant_id=%s AND projection_id=%s",
                (service.tenant_id, pid))
            conn.execute("DELETE FROM projection_supports WHERE tenant_id=%s AND projection_id=%s",
                (service.tenant_id, pid))
            conn.execute("""DELETE FROM relations WHERE tenant_id=%s AND
                ((from_kind='projection' AND from_id=%s) OR (to_kind='projection' AND to_id=%s))""",
                (service.tenant_id, pid, pid))
            conn.execute("DELETE FROM projections WHERE tenant_id=%s AND id=%s", (service.tenant_id, pid))
        else:
            # Change only the erasure marker: target, sources and canonical versions stay identical.
            conn.execute("""INSERT INTO deletion_markers(tenant_id,object_kind,object_id,reason_code)
                VALUES (%s,'projection_content',%s,'synthetic_target_boundary')""", (service.tenant_id, pid))
    before = snapshot(service)
    assert len(before) == 17
    for table in ["evidence", "card_sources", "semantic_cards", "semantic_card_versions", "event_deltas"]:
        assert before[table] == initial[table]
    if change == "erasure-marker":
        for table in ["projections", "projection_versions", "projection_supports"]:
            assert before[table] == initial[table]
    with pytest.raises(expected):
        commit(service, prepared, {"summary": "forbidden target revival"})
    assert snapshot(service) == before  # Includes the waiting job and its failure budget.
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        target = conn.execute("SELECT lifecycle,current_version FROM projections WHERE tenant_id=%s AND id=%s",
            (service.tenant_id, pid)).fetchone()
        if change == "missing":
            assert target is None
        else:
            assert target == {"lifecycle": "deleted" if change == "deleted" else "active", "current_version": 1}
        assert not conn.execute("SELECT 1 FROM projection_versions WHERE body::text LIKE '%forbidden target revival%'").fetchone()
