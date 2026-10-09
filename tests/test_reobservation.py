from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import psycopg
import pytest

from liveday0.core import MemoryService
from liveday0.db import connect, tenant_transaction
from liveday0.exceptions import DeletedSource, NotFound, VersionConflict
from liveday0.migrations import migrate_down, migrate_up, migration_status
from tests.helpers import evidence, fact
from tests.test_trust_boundaries import ControlledTransactions, named


def forgotten(service, key="old-key"):
    value = service.observe(evidence("forgotten private content", key=key), semantics=[
        fact({"proposition": "forgotten private fact", "scope": "old life"}, key=f"fact:{key}")])
    service.delete_evidence(value["evidence_id"])
    return value


def request():
    return {"evidence": evidence("explicitly save this again", key="new-key"),
            "semantics": [fact({"proposition": "new explicit fact", "scope": "new life"}, key="new-fact")],
            "trace": {"observation": "new trace", "observation_boundary": "only this source"},
            "intent_id": uuid4()}


def state(service):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return {"revision": conn.execute("SELECT revision FROM tenants WHERE id=%s", (service.tenant_id,)).fetchone()["revision"],
                **{table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                   for table in ["evidence", "semantic_cards", "life_traces", "reobservation_intents"]}}


@pytest.mark.parametrize("changed_kind", [False, True])
def test_deleted_stable_source_cannot_replay_even_with_different_content_or_kind(service, changed_kind):
    original = evidence("erase this", key="private-source-key")
    item = service.observe(original)
    service.delete_evidence(item["evidence_id"])
    before = state(service)
    retry = replace(original, content="different text", source_kind="different-importer" if changed_kind else original.source_kind)
    with pytest.raises(DeletedSource):
        service.observe(retry, semantics=[fact({"proposition": "must not appear", "scope": "test"})])
    assert state(service) == before
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        marker = conn.execute("SELECT * FROM deletion_markers WHERE object_kind='evidence'").fetchone()
        assert len(marker["source_identity_digest"]) == 64
        assert "private-source-key" not in json.dumps(marker, default=str)
        assert "erase this" not in json.dumps(marker, default=str)
        assert conn.execute("SELECT content,idempotency_key FROM evidence").fetchone() == {"content": None, "idempotency_key": None}


def test_new_sources_and_unkeyed_one_time_input_remain_possible(service):
    old = forgotten(service)
    independent = service.observe(evidence("forgotten private content", key="independent-new-source"))
    unkeyed = service.observe(evidence("one-time input"))
    service.delete_evidence(unkeyed["evidence_id"])
    another = service.observe(evidence("one-time input"))
    assert independent["created"] and another["created"]
    assert independent["evidence_id"] != old["evidence_id"]
    other = MemoryService(uuid4()); other.ensure_tenant()
    assert other.observe(evidence("other tenant", key="old-key"))["created"]
    with pytest.raises(ValueError, match="string"):
        service.observe(replace(evidence("bad key"), idempotency_key=123))


def test_explicit_reobservation_creates_new_ids_and_retry_returns_original_ids(service):
    old = forgotten(service); req = request()
    first = service.reobserve_deleted(old["evidence_id"], **req)
    # A later use of the evidence must not change the intent's original result IDs.
    other = service.observe(evidence("another card"), semantics=[fact({"proposition": "other", "scope": "test"})])
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("INSERT INTO card_sources(tenant_id,card_id,evidence_id) VALUES (%s,%s,%s)",
                     (service.tenant_id, other["card_ids"][0], first["evidence_id"]))
    again = service.reobserve_deleted(old["evidence_id"], **req)
    assert first["created"] and again == {**first, "created": False}
    assert first["evidence_id"] != old["evidence_id"]
    assert set(first["card_ids"]).isdisjoint(old["card_ids"])
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT status,content FROM evidence WHERE id=%s", (old["evidence_id"],)).fetchone() == {"status": "deleted", "content": None}
        assert conn.execute("SELECT count(*) AS n FROM reobservation_intents").fetchone()["n"] == 1


@pytest.mark.parametrize("change", ["content", "key", "source", "occurred_at", "valid_at", "body", "trace"])
def test_reusing_intent_for_changed_frozen_request_conflicts(service, change):
    old = forgotten(service); another = forgotten(service, "another-old-key"); req = request()
    service.reobserve_deleted(old["evidence_id"], **req)
    before = state(service)
    changed = dict(req); source_id = old["evidence_id"]
    if change == "content": changed["evidence"] = replace(req["evidence"], content="different")
    if change == "key": changed["evidence"] = replace(req["evidence"], idempotency_key="different")
    if change == "source": source_id = another["evidence_id"]
    if change == "occurred_at": changed["evidence"] = replace(req["evidence"], occurred_at=req["evidence"].occurred_at + timedelta(seconds=1))
    if change == "valid_at": changed["semantics"] = [replace(req["semantics"][0], valid_at=req["semantics"][0].valid_at + timedelta(seconds=1))]
    if change == "body": changed["semantics"] = [replace(req["semantics"][0], body={"proposition": "different", "scope": "new life"})]
    if change == "trace": changed["trace"] = {**req["trace"], "observation": "different"}
    with pytest.raises(VersionConflict): service.reobserve_deleted(source_id, **changed)
    assert state(service) == before


def test_reobservation_is_atomic_on_real_database_constraint_failure(service):
    old = forgotten(service); req = request()
    taken = service.observe(evidence("existing"), semantics=req["semantics"])
    before = state(service)
    with pytest.raises(psycopg.errors.UniqueViolation):
        service.reobserve_deleted(old["evidence_id"], **req)
    assert state(service) == before
    service.delete_card(taken["card_ids"][0])
    assert service.reobserve_deleted(old["evidence_id"], **req)["created"]


def test_concurrent_same_intent_has_one_creation_and_identical_ids(service, monkeypatch):
    old = forgotten(service); req = request()
    control = ControlledTransactions(monkeypatch, "first", "INSERT INTO reobservation_intents")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(named, "first", lambda: service.reobserve_deleted(old["evidence_id"], **req))
        try:
            assert control.ready.wait(5)
            b = pool.submit(named, "second", lambda: service.reobserve_deleted(old["evidence_id"], **req))
            control.wait_blocked("second", "first")
        finally:
            control.release.set()
        first, second = a.result(timeout=6), b.result(timeout=6)
    assert first["created"] and second == {**first, "created": False}
    assert state(service)["reobservation_intents"] == 1
    control.record("reobservation-same-intent")


def test_nested_request_is_detached_before_waiting_for_write_gate(service, monkeypatch):
    old = forgotten(service); req = request()
    control = ControlledTransactions(monkeypatch, "save", "FOR UPDATE", after=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(named, "save", lambda: service.reobserve_deleted(old["evidence_id"], **req))
        try:
            assert control.ready.wait(5)
            req["semantics"][0].body["proposition"] = "late caller mutation"
            req["trace"]["observation"] = "late caller mutation"
        finally:
            control.release.set()
        result = job.result(timeout=6)
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        body = conn.execute("SELECT body FROM semantic_card_versions WHERE card_id=%s", (result["card_ids"][0],)).fetchone()["body"]
        assert body["proposition"] == "new explicit fact"
        assert conn.execute("SELECT observation FROM life_traces WHERE id=%s", (result["trace_id"],)).fetchone()["observation"] == "new trace"
    with pytest.raises(VersionConflict): service.reobserve_deleted(old["evidence_id"], **req)


def test_deleting_new_source_erases_intent_fingerprint_and_prevents_old_intent_revival(service):
    old = forgotten(service); req = request()
    new = service.reobserve_deleted(old["evidence_id"], **req)
    service.delete_evidence(new["evidence_id"])
    before = state(service)
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        row = conn.execute("SELECT state,request_fingerprint FROM reobservation_intents").fetchone()
        assert row == {"state": "deleted", "request_fingerprint": None}
    with pytest.raises(DeletedSource): service.reobserve_deleted(old["evidence_id"], **req)
    with pytest.raises(DeletedSource): service.observe(req["evidence"])
    assert state(service) == before
    fresh = {**req, "intent_id": uuid4(), "evidence": replace(req["evidence"], idempotency_key="third-key")}
    third = service.reobserve_deleted(new["evidence_id"], **fresh)
    assert third["created"] and third["evidence_id"] not in {old["evidence_id"], new["evidence_id"]}


def test_invalid_old_source_and_reused_or_absent_new_key_rejected(service):
    active = service.observe(evidence("still active", key="active-key")); req = request()
    with pytest.raises(VersionConflict): service.reobserve_deleted(active["evidence_id"], **req)
    old = forgotten(service)
    for key, error in [(None, ValueError), ("", ValueError), ("active-key", VersionConflict), ("old-key", DeletedSource)]:
        before = state(service)
        with pytest.raises(error):
            service.reobserve_deleted(old["evidence_id"], **{**req, "evidence": replace(req["evidence"], idempotency_key=key)})
        assert state(service) == before
    other = MemoryService(uuid4()); other.ensure_tenant()
    with pytest.raises(NotFound): other.reobserve_deleted(old["evidence_id"], **req)


def test_intent_rls_and_composite_foreign_keys_reject_cross_tenant_sources(service):
    old = forgotten(service); req = request()
    saved = service.reobserve_deleted(old["evidence_id"], **req)
    other = MemoryService(uuid4()); other.ensure_tenant()
    other_old = forgotten(other)
    other_new = other.observe(evidence("other new", key="other-new"))
    own_unused = service.observe(evidence("own unused", key="own-unused"))
    with tenant_transaction(other.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT count(*) AS n FROM reobservation_intents").fetchone()["n"] == 0
        assert conn.execute("SELECT * FROM deletion_markers WHERE object_id=%s", (old["evidence_id"],)).fetchall() == []
        flags = conn.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='reobservation_intents'::regclass").fetchone()
        assert flags == {"relrowsecurity": True, "relforcerowsecurity": True}
    insert = "INSERT INTO reobservation_intents VALUES (%s,%s,%s,%s,%s,'active')"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_transaction(service.tenant_id) as conn:
            conn.execute(insert, (other.tenant_id, uuid4(), other_old["evidence_id"], other_new["evidence_id"], "a" * 64))
    for old_id, new_id in [(other_old["evidence_id"], own_unused["evidence_id"]), (old["evidence_id"], other_new["evidence_id"])]:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with tenant_transaction(service.tenant_id) as conn:
                conn.execute(insert, (service.tenant_id, uuid4(), old_id, new_id, "b" * 64))


def test_incremental_migration_down_preserves_001_data_and_up_cannot_recover_erased_old_keys(service):
    old = forgotten(service)
    service.reobserve_deleted(old["evidence_id"], **request())
    assert migrate_down(2) == [3, 2]
    try:
        assert [row["version"] for row in migration_status()] == [1]
        with connect() as conn:
            assert conn.execute("SELECT to_regclass('reobservation_intents') AS t").fetchone()["t"] is None
            assert conn.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"] == 2
    finally:
        assert migrate_up() == [2, 3]
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        marker = conn.execute("SELECT source_identity_digest FROM deletion_markers WHERE object_kind='evidence'").fetchone()
        assert marker["source_identity_digest"] is None  # No deleted key/body is recovered to invent a backfill.
