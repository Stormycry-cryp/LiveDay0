from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import psycopg
import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import DeletedSource, NotFound, VersionConflict
from liveday0.migrations import migrate_down, migrate_up, migration_status
from liveday0.types import EvidenceInput, SemanticInput
from tests.test_trust_boundaries import ControlledTransactions, named


STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def source(key="source", **changes):
    return EvidenceInput("text", "synthetic", "synthetic source", occurred_at=STAMP,
                         idempotency_key=key, **changes)


def proposal(**changes):
    return SemanticInput("event", {"goal_context": "synthetic", "current_result": "before"},
                         valid_at=STAMP, **changes)


def seed(service):
    return service.observe(source("seed"), semantics=[proposal()])["card_ids"][0]


def snapshot(service):
    tables = ["tenants", "evidence", "life_traces", "semantic_cards", "semantic_card_versions",
              "card_sources", "event_deltas", "mentions", "mention_candidates", "relations",
              "projections", "projection_versions", "projection_supports", "maintenance_jobs",
              "recall_snapshots", "deletion_markers", "reobservation_intents"]
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return {table: sorted(json.dumps(dict(r), sort_keys=True, default=str)
                              for r in conn.execute("SELECT * FROM " + table)) for table in tables}


def conflict(action, message):
    with pytest.raises(VersionConflict, match=message) as caught:
        action()
    assert type(caught.value).__name__ == "IdempotencyConflict"


@pytest.mark.parametrize("field,value", [
    ("modality", "image"), ("source_kind", "synthetic-import"), ("content", "different"),
    ("object_ref", "synthetic://changed"), ("occurred_at", STAMP + timedelta(microseconds=1)),
    ("image_observation", "visual"), ("sending_context", "context"), ("model_interpretation", "guess"),
    ("embedding", (0.0,) * 8),
])
def test_source_every_field_conflicts_without_side_effects(service, field, value):
    req = source(); service.observe(req); before = snapshot(service)
    conflict(lambda: service.observe(replace(req, **{field: value})), "different frozen request")
    assert snapshot(service) == before


@pytest.mark.parametrize("change", ["trace-extra", "trace-none", "trace-empty", "trace-boundary",
    "trace-accessibility", "trace-observation", "body", "type", "lifecycle", "epistemic", "key",
    "valid_at", "order", "bool-number", "int-float", "none-empty"])
def test_trace_and_entire_ordered_semantic_request_are_frozen(service, change):
    req = source(); trace = {"observation": "life trace", "accessibility": 0.2, "extra": None}
    items = [replace(proposal(), body={**proposal().body, "value": 1, "optional": None}),
             SemanticInput("fact", {"proposition": "fact", "scope": "test"}, valid_at=STAMP)]
    service.observe(req, trace=trace, semantics=items); before = snapshot(service)
    trace = deepcopy(trace); items = deepcopy(items)
    if change == "trace-extra": trace["extra"] = "metadata"
    elif change == "trace-none": trace = None
    elif change == "trace-empty": trace = {}
    elif change == "trace-boundary": trace["observation_boundary"] = "changed"
    elif change == "trace-accessibility": trace["accessibility"] = 0.3
    elif change == "trace-observation": trace["observation"] = "changed"
    elif change == "body": items[0].body["current_result"] = "changed"
    elif change == "type": items[0] = SemanticInput("fact", {"proposition":"changed","scope":"test"}, valid_at=STAMP)
    elif change == "lifecycle": items[0] = replace(items[0], lifecycle="provisional")
    elif change == "epistemic": items[0] = replace(items[0], epistemic_state="candidate")
    elif change == "key": items[0] = replace(items[0], canonical_key="changed")
    elif change == "valid_at": items[0] = replace(items[0], valid_at=STAMP + timedelta(microseconds=1))
    elif change == "order": items.reverse()
    elif change == "bool-number": items[0].body["value"] = True
    elif change == "int-float": items[0].body["value"] = 1.0
    elif change == "none-empty": items[0].body["optional"] = ""
    conflict(lambda: service.observe(req, trace=trace, semantics=items), "different frozen request")
    assert snapshot(service) == before


def test_equivalent_utc_and_dictionary_order_replay_original_fingerprint_after_correction(service):
    req = source(""); item = proposal(); first = service.observe(req, semantics=[item])
    cid = first["card_ids"][0]
    service.correct_card(cid, source("correction"), {"goal_context":"synthetic","current_result":"after"}, expected_version=1)
    before = snapshot(service)
    offset = timezone(timedelta(hours=8))
    retry = service.observe(replace(req, occurred_at=STAMP.astimezone(offset)), semantics=[
        replace(item, valid_at=STAMP.astimezone(offset), body=dict(reversed(list(item.body.items()))))])
    assert retry["created"] is False and retry["evidence_id"] == first["evidence_id"]
    assert snapshot(service) == before
    conflict(lambda: service.observe(req, semantics=[replace(item, body={"goal_context":"synthetic","current_result":"after"})]), "different frozen request")


def test_key_scope_anonymous_and_deleted_precedence(service):
    req = source(); item = service.observe(req)
    other = MemoryService(uuid4()); other.ensure_tenant()
    assert other.observe(replace(req, content="another tenant"))["created"]
    assert service.observe(replace(req, idempotency_key="other"))["created"]
    a = service.observe(source(None)); b = service.observe(source(None)); assert a["evidence_id"] != b["evidence_id"]
    service.delete_evidence(item["evidence_id"])
    with pytest.raises(DeletedSource): service.observe(replace(req, content="changed"))
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        row = conn.execute("SELECT request_fingerprint,idempotency_key FROM evidence WHERE id=%s", (item["evidence_id"],)).fetchone()
        assert row == {"request_fingerprint":None, "idempotency_key":None}


@pytest.mark.parametrize("kind", ["source", "delta"])
def test_legacy_keys_conflict_and_migration_does_not_backfill(service, kind):
    # Prepare rows on old schema through SQL, without fabricating a frozen request.
    assert migrate_down(1) == [4]
    try:
        with tenant_transaction(service.tenant_id) as conn:
            eid = conn.execute("""INSERT INTO evidence(tenant_id,modality,source_kind,content,occurred_at,idempotency_key)
                VALUES (%s,'text','synthetic','synthetic source',%s,'source') RETURNING id""", (service.tenant_id,STAMP)).fetchone()["id"]
        assert migrate_up() == [4]
        if kind == "source":
            before = snapshot(service); conflict(lambda: service.observe(source()), "legacy source")
        else:
            cid = seed(service); req = source("delta-source"); service.observe(req)
            with tenant_transaction(service.tenant_id) as conn:
                sid = conn.execute("SELECT id FROM evidence WHERE idempotency_key='delta-source'").fetchone()["id"]
                conn.execute("""INSERT INTO event_deltas(tenant_id,event_id,evidence_id,delta,idempotency_key)
                    VALUES (%s,%s,%s,'{"current_result":"delta"}','delta')""",(service.tenant_id,cid,sid))
            before = snapshot(service)
            conflict(lambda: service.add_event_delta(cid,req,{"current_result":"delta"},idempotency_key="delta"), "legacy delta")
        assert snapshot(service) == before
    finally: migrate_up()


@pytest.mark.parametrize("case", ["delta-missing", "delta-fact", "delta-deleted", "delta-foreign",
    "correct-version", "correct-body", "correct-deleted", "correct-foreign", "close-version",
    "mention-missing", "mention-foreign", "mention-deleted", "mention-sql"])
def test_composite_failure_leaves_all_tables_unchanged(service, case):
    cid = seed(service); target = cid
    if case.endswith("missing"): target = uuid4()
    if case.endswith("fact"):
        target = service.observe(source("fact"), semantics=[SemanticInput("fact", {"proposition":"fact","scope":"test"}, valid_at=STAMP)])["card_ids"][0]
    if case.endswith("deleted"): service.delete_card(cid)
    if case.endswith("foreign"):
        other = MemoryService(uuid4()); other.ensure_tenant(); target = seed(other)
    before = snapshot(service); req = source("must-rollback")
    with pytest.raises((ValueError,NotFound,VersionConflict,psycopg.Error)):
        if case.startswith("delta"):
            service.add_event_delta(target,req,{"current_result":"delta"},idempotency_key="delta")
        elif case.startswith("correct") or case.startswith("close"):
            fn = service.close_card if case.startswith("close") else service.correct_card
            body = {} if case.endswith("body") else {"goal_context":"synthetic","current_result":"changed"}
            fn(target,req,body,expected_version=0 if case.endswith("version") else 1)
        else:
            service.create_unbound_mention(req,"someone",[{"card_id":cid,"reason":"first valid"},
                {"card_id":target,"reason":"second", "confidence":2.0 if case.endswith("sql") else 0.5}])
    assert snapshot(service) == before


@pytest.mark.parametrize("kind", ["delta", "correct", "close", "mention"])
def test_actual_sql_failure_after_target_write_rolls_back_source_and_all_effects(service, monkeypatch, kind):
    cid = seed(service); before = snapshot(service)
    original = service._bump_revision; calls = []
    def fail(conn):
        calls.append(1)
        if len(calls) == 2: conn.execute("SELECT 1/0")
        return original(conn)
    monkeypatch.setattr(service,"_bump_revision",fail)
    with pytest.raises(psycopg.errors.DivisionByZero):
        if kind == "delta": service.add_event_delta(cid,source(),{"current_result":"changed"},idempotency_key="delta")
        elif kind == "mention": service.create_unbound_mention(source(),"someone",[{"card_id":cid,"reason":"candidate"}])
        else: getattr(service, "correct_card" if kind == "correct" else "close_card")(cid,source(),
            {"goal_context":"synthetic","current_result":"changed"},expected_version=1)
    assert calls == [1,1] and snapshot(service) == before


def test_previously_committed_source_survives_failed_composite_and_success_is_atomic(service):
    cid = seed(service); req = source(); item = service.observe(req); before = snapshot(service)
    with pytest.raises(VersionConflict):
        service.correct_card(cid,req,{"goal_context":"synthetic","current_result":"after"},expected_version=0)
    assert snapshot(service) == before
    assert service.correct_card(cid,req,{"goal_context":"synthetic","current_result":"after"},expected_version=1)["version"] == 2
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT count(*) AS n FROM evidence WHERE id=%s",(item["evidence_id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT source_role FROM card_sources WHERE evidence_id=%s",(item["evidence_id"],)).fetchone()["source_role"] == "correction"
    before = snapshot(service)
    with pytest.raises(VersionConflict): service.correct_card(cid,req,{"goal_context":"synthetic","current_result":"after"},expected_version=1)
    assert snapshot(service) == before


@pytest.mark.parametrize("change", ["source", "anonymous", "payload", "int-float", "bool-number"])
def test_delta_key_conflict_rolls_back_any_new_source(service, change):
    cid = seed(service); req = source(); body = {"current_result":"delta", "value":1}
    service.add_event_delta(cid,req,body,idempotency_key="delta"); before = snapshot(service)
    if change == "source": req = source("another")
    elif change == "anonymous": req = source(None)
    elif change == "payload": body["current_result"] = "changed"
    elif change == "int-float": body["value"] = 1.0
    else: body["value"] = True
    conflict(lambda: service.add_event_delta(cid,req,body,idempotency_key="delta"), "different source or payload")
    assert snapshot(service) == before


def test_delta_replay_has_stable_id_no_new_jobs_or_revision_after_absorption_or_correction(service):
    cid = seed(service); req = source(); body = {"current_result":"delta"}
    first = service.add_event_delta(cid,req,body,idempotency_key="delta")
    for stage in ["pending","absorbed","invalidated"]:
        if stage == "absorbed":
            service.maintenance.make_pending_ready(); service.maintenance.run_ready(limit=1)
        if stage == "invalidated":
            service.correct_card(cid,source("correct"),{"goal_context":"synthetic","current_result":"corrected"},expected_version=2)
        before = snapshot(service)
        assert service.add_event_delta(cid,req,body,idempotency_key="delta") == {"delta_id":first["delta_id"],"created":False}
        assert snapshot(service) == before


@pytest.mark.parametrize("different", [False,True])
def test_concurrent_same_key_serializes_exact_request_or_conflict(service, monkeypatch, different):
    control = ControlledTransactions(monkeypatch,"first","INSERT INTO evidence")
    req = source(); second_req = replace(req,content="different") if different else req
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(named,"first",lambda:service.observe(req,semantics=[proposal()]))
        try:
            assert control.ready.wait(3)
            second = pool.submit(named,"second",lambda:service.observe(second_req,semantics=[proposal()]))
            control.wait_blocked("second","first")
        finally: control.release.set()
        a = first.result(timeout=8)
        if different: conflict(lambda:second.result(timeout=8),"different frozen request")
        else:
            b = second.result(timeout=8); assert b["evidence_id"] == a["evidence_id"] and not b["created"]
    control.record("atomic-source-conflict" if different else "atomic-source-replay")
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        assert conn.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 1


@pytest.mark.parametrize("delete_first", [False,True])
def test_correction_source_delete_and_composite_commit_are_ordered(service,monkeypatch,delete_first):
    cid = seed(service); req = source("correct"); sid = service.observe(req)["evidence_id"]
    control = ControlledTransactions(monkeypatch,"delete" if delete_first else "correct","FOR UPDATE",after=True)
    write = lambda:service.correct_card(cid,req,{"goal_context":"synthetic","current_result":"must erase"},expected_version=1)
    erase = lambda:MemoryService(service.tenant_id).delete_evidence(sid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(named,"delete" if delete_first else "correct",erase if delete_first else write)
        try:
            assert control.ready.wait(3)
            second = pool.submit(named,"correct" if delete_first else "delete",write if delete_first else erase)
            control.wait_blocked("correct" if delete_first else "delete","delete" if delete_first else "correct")
        finally: control.release.set()
        first.result(timeout=8)
        if delete_first:
            with pytest.raises(DeletedSource): second.result(timeout=8)
        else: second.result(timeout=8)
    control.record("atomic-correction-delete-first" if delete_first else "atomic-correction-first")
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        row = conn.execute("SELECT status,request_fingerprint FROM evidence WHERE id=%s",(sid,)).fetchone()
        assert row == {"status":"deleted","request_fingerprint":None}
        if not delete_first:
            assert conn.execute("SELECT lifecycle FROM semantic_cards WHERE id=%s",(cid,)).fetchone()["lifecycle"] == "deleted"
        assert not conn.execute("SELECT 1 FROM semantic_card_versions WHERE body::text LIKE '%must erase%'").fetchone()


@pytest.mark.parametrize("kind", ["observe", "delta", "correct", "mention"])
def test_nested_inputs_are_detached_before_tenant_wait(service,monkeypatch,kind):
    cid = seed(service); control = ControlledTransactions(monkeypatch,"writer","FOR UPDATE",after=True)
    body = {"goal_context":"synthetic","current_result":"frozen"}; trace = {"observation":"frozen"}
    candidates = [{"card_id":cid,"reason":"frozen"}]
    def write():
        if kind == "observe": return service.observe(source(),trace=trace,semantics=[replace(proposal(),body=body)])
        if kind == "delta": return service.add_event_delta(cid,source(),body,idempotency_key="delta")
        if kind == "correct": return service.correct_card(cid,source(),body,expected_version=1)
        return service.create_unbound_mention(source(),"someone",candidates)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(named,"writer",write)
        try:
            assert control.ready.wait(3); body["current_result"]="mutated"; trace["observation"]="mutated"; candidates[0]["reason"]="mutated"
        finally: control.release.set()
        future.result(timeout=8)
    assert "mutated" not in json.dumps(snapshot(service))
    assert "frozen" in json.dumps(snapshot(service))


def test_deleting_card_erases_observation_and_delta_content_fingerprints(service):
    cid = seed(service); req = source(); service.add_event_delta(cid,req,{"current_result":"delta"},idempotency_key="delta")
    service.delete_card(cid)
    with tenant_transaction(service.tenant_id,mode="read") as conn:
        assert not conn.execute("SELECT 1 FROM evidence WHERE request_fingerprint IS NOT NULL").fetchone()
        assert not conn.execute("SELECT 1 FROM event_deltas WHERE request_fingerprint IS NOT NULL").fetchone()
    conflict(lambda: service.observe(req),"legacy source")


@pytest.mark.parametrize("table", ["evidence","event_deltas"])
def test_downgrade_refuses_to_discard_frozen_requests(service,table):
    cid = seed(service); service.add_event_delta(cid,source(),{"current_result":"delta"},idempotency_key="delta")
    if table == "event_deltas":
        with tenant_transaction(service.tenant_id) as conn: conn.execute("UPDATE evidence SET request_fingerprint=NULL")
    before = snapshot(service)
    with pytest.raises(psycopg.errors.RaiseException,match="frozen request fingerprints"): migrate_down(1)
    assert [r["version"] for r in migration_status()] == [1,2,3,4]
    assert snapshot(service) == before


@pytest.mark.parametrize("target", ["evidence", "semantic"])
def test_naive_times_are_rejected_without_side_effects(service,target):
    before = snapshot(service); req = source(); item = proposal()
    if target == "evidence": req = replace(req,occurred_at=STAMP.replace(tzinfo=None))
    else: item = replace(item,valid_at=STAMP.replace(tzinfo=None))
    with pytest.raises(ValueError,match="timezone-aware"): service.observe(req,semantics=[item])
    assert snapshot(service) == before
