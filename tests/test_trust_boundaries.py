from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

import pytest

import liveday0.db as db
from liveday0.core import MemoryService
from liveday0.exceptions import NotFound, SnapshotInvalidated
from liveday0.types import RecallOptions
from tests.helpers import evidence, event, flatten_context


def seed(service, label="gate memory"):
    return service.observe(evidence(label, key=str(uuid4())), semantics=[
        event({"goal_context": label, "current_result": "private_old_body"}, key=str(uuid4()))
    ])


class ControlledTransactions:
    """Pause real SQL calls and prove blocking through PostgreSQL, not timing."""

    def __init__(self, monkeypatch, pause_thread, pause_query, *, after=False):
        self.original = db.connect
        self.pause_thread = pause_thread
        self.pause_query = pause_query
        self.after = after
        self.ready = threading.Event()
        self.release = threading.Event()
        self.pids = {}
        self.gates = {}
        self.observations = []
        self.paused = False
        owner = self

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def __enter__(self):
                self.conn.__enter__()
                return self

            def __exit__(self, *args):
                return self.conn.__exit__(*args)

            def execute(self, query, params=None, *args, **kwargs):
                name = threading.current_thread().name
                text = query if isinstance(query, str) else ""
                if "SELECT id FROM tenants" in text:
                    owner.pids[name] = self.conn.info.backend_pid
                    owner.gates[name] = text
                pause = name == owner.pause_thread and owner.pause_query in text and not owner.paused
                if pause and not owner.after:
                    owner.pause()
                result = self.conn.execute(query, params, *args, **kwargs)
                if pause and owner.after:
                    owner.pause()
                return result

        def connect(**kwargs):
            conn = self.original(**kwargs)
            # Every worker's database wait is bounded even if a test assertion fails.
            conn.execute("SET statement_timeout='8000ms'")
            conn.commit()
            return Connection(conn)

        monkeypatch.setattr(db, "connect", connect)

    def pause(self):
        self.paused = True
        self.ready.set()
        if not self.release.wait(6):
            raise RuntimeError("test SQL barrier timed out")

    def wait_blocked(self, waiter, holder):
        deadline = time.monotonic() + 5
        with self.original(autocommit=True) as conn:
            while time.monotonic() < deadline:
                waiting_pid = self.pids.get(waiter)
                holding_pid = self.pids.get(holder)
                if waiting_pid and holding_pid:
                    blockers = conn.execute("SELECT pg_blocking_pids(%s) AS pids", (waiting_pid,)).fetchone()["pids"]
                    if holding_pid in blockers:
                        self.observations.append({"waiter": waiter, "holder": holder,
                            "waiting_pid": waiting_pid, "holding_pid": holding_pid,
                            "blockers": blockers, "waiter_gate": self.gates[waiter], "holder_gate": self.gates[holder]})
                        return
                threading.Event().wait(0.01)
        raise AssertionError(f"expected database lock wait {waiter} -> {holder}")

    def record(self, case):
        target = os.environ.get("_PHASE1_LOCK_OBSERVATIONS")
        if target:
            with Path(target).open("a") as out:
                out.write(json.dumps({"case": case, "locks": self.observations}) + "\n")


def named(name, fn):
    threading.current_thread().name = name
    return fn()


def compile_context(service):
    return service.recall_compiler.compile("gate memory", current_evidence_ids=[], options=RecallOptions())


@pytest.mark.parametrize("delete_first", [False, True])
def test_snapshot_publication_and_delete_are_ordered(service, monkeypatch, delete_first):
    item = seed(service)
    control = ControlledTransactions(monkeypatch,
        "delete" if delete_first else "compile",
        "FOR UPDATE" if delete_first else "INSERT INTO recall_snapshots", after=delete_first)
    actions = {"delete": lambda: service.delete_evidence(item["evidence_id"]), "compile": lambda: compile_context(service)}
    first, second = ("delete", "compile") if delete_first else ("compile", "delete")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(named, first, actions[first])
        try:
            assert control.ready.wait(5)
            b = pool.submit(named, second, actions[second])
            control.wait_blocked(second, first)
        finally:
            control.release.set()
        results = {first: a.result(timeout=6), second: b.result(timeout=6)}
    context = results["compile"]
    if delete_first:
        assert "private_old_body" not in flatten_context(context)
    else:
        assert "private_old_body" in flatten_context(context)
        with pytest.raises(SnapshotInvalidated):
            service.expand_snapshot(UUID(context["snapshot"]["id"]), item["card_ids"][0])
    with db.tenant_transaction(service.tenant_id, mode="read") as conn:
        assert "private_old_body" not in json.dumps(conn.execute("SELECT context,expansion_store FROM recall_snapshots").fetchall())
    control.record(f"snapshot-delete:{delete_first}")


@pytest.mark.parametrize("delete_first", [False, True])
def test_expand_and_delete_are_ordered(service, monkeypatch, delete_first):
    item = seed(service)
    snapshot = service.recall("gate memory")
    sid, cid = UUID(snapshot["snapshot"]["id"]), item["card_ids"][0]
    control = ControlledTransactions(monkeypatch, "delete" if delete_first else "expand",
        "FOR UPDATE" if delete_first else "SELECT state, expansion_store", after=True)
    actions = {"delete": lambda: service.delete_evidence(item["evidence_id"]), "expand": lambda: service.expand_snapshot(sid, cid)}
    first, second = ("delete", "expand") if delete_first else ("expand", "delete")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(named, first, actions[first])
        try:
            assert control.ready.wait(5)
            b = pool.submit(named, second, actions[second])
            control.wait_blocked(second, first)
        finally:
            control.release.set()
        if delete_first:
            a.result(timeout=6)
            with pytest.raises(SnapshotInvalidated):
                b.result(timeout=6)
        else:
            assert a.result(timeout=6)["body"]["current_result"] == "private_old_body"
            b.result(timeout=6)
    with pytest.raises(SnapshotInvalidated):
        service.expand_snapshot(sid, cid)
    control.record(f"expand-delete:{delete_first}")


@pytest.mark.parametrize("delete_first", [False, True])
def test_projection_support_and_delete_are_ordered(service, monkeypatch, delete_first):
    item = seed(service)
    control = ControlledTransactions(monkeypatch, "delete" if delete_first else "projection",
        "FOR UPDATE" if delete_first else "INSERT INTO projections", after=delete_first)
    actions = {"delete": lambda: service.delete_evidence(item["evidence_id"]), "projection": lambda: service.materialize_projection(
        projection_type="relationship", projection_key="gate projection", scope="gate",
        body={"summary": "gate memory private_projection_body"}, support_card_ids=item["card_ids"])}
    first, second = ("delete", "projection") if delete_first else ("projection", "delete")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(named, first, actions[first])
        try:
            assert control.ready.wait(5)
            b = pool.submit(named, second, actions[second])
            control.wait_blocked(second, first)
        finally:
            control.release.set()
        a.result(timeout=6)
        if delete_first:
            with pytest.raises(ValueError):
                b.result(timeout=6)
        else:
            b.result(timeout=6)
    assert "private_projection_body" not in flatten_context(service.recall("gate memory"))
    with db.tenant_transaction(service.tenant_id, mode="read") as conn:
        assert "private_projection_body" not in json.dumps(conn.execute("SELECT body FROM projection_versions").fetchall())
    control.record(f"projection-delete:{delete_first}")


def test_different_tenant_can_write_while_first_tenant_is_locked(service, monkeypatch):
    item = seed(service)
    other = MemoryService(uuid4())
    other.ensure_tenant()
    control = ControlledTransactions(monkeypatch, "delete", "FOR UPDATE", after=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(named, "delete", lambda: service.delete_evidence(item["evidence_id"]))
        try:
            assert control.ready.wait(5)
            b = pool.submit(named, "other", lambda: seed(other))
            assert b.result(timeout=3)["created"]
            assert not a.done(), "first tenant must still be held at the barrier"
        finally:
            control.release.set()
        a.result(timeout=6)
    control.record("different-tenant-progress")


def test_missing_tenant_fails_and_bootstrap_is_idempotent():
    service = MemoryService(uuid4())
    with pytest.raises(NotFound):
        service.observe(evidence("no implicit tenant"))
    with pytest.raises(NotFound):
        compile_context(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: service.ensure_tenant(), range(2))) == [service.tenant_id] * 2
    assert seed(service)["created"]


def test_unsafe_delta_invalidates_existing_snapshot_and_excludes_dependent_projection(service, monkeypatch):
    item = seed(service)
    pid = service.materialize_projection(projection_type="relationship", projection_key="unsafe-view", scope="gate",
        body={"summary": "gate memory unsafe_old_projection"}, support_card_ids=item["card_ids"])
    snapshot = service.recall("gate memory")
    service.add_event_delta(item["card_ids"][0], evidence("restructure", key="unsafe-source"),
        {"current_result": "new", "requires_restructure": True}, idempotency_key="unsafe")
    monkeypatch.setattr(service.maintenance, "catch_up_unsafe_overlays", lambda: [])
    context = service.recall("gate memory")
    assert context["degraded"] and "unsafe_events_not_caught_up" in context["degraded_reasons"]
    assert "private_old_body" not in flatten_context(context)
    assert "unsafe_old_projection" not in flatten_context(context)
    assert str(pid) not in flatten_context(context)
    with pytest.raises(SnapshotInvalidated):
        service.expand_snapshot(UUID(snapshot["snapshot"]["id"]), item["card_ids"][0])


def test_deleted_projection_content_requires_version_bound_rebuild(service):
    a, b = seed(service, "deleted family"), seed(service, "remaining family")
    pid = service.materialize_projection(projection_type="relationship", projection_key="partial-family", scope="private scope",
        body={"summary": "private polluted summary"}, support_card_ids=[*a["card_ids"], *b["card_ids"]])
    service.delete_evidence(a["evidence_id"])
    result = service.maintenance.run_ready(limit=1, projection_outputs={pid: {"summary": "private polluted summary"}})
    assert result[0]["state"] == "waiting"
    with db.tenant_transaction(service.tenant_id, mode="read") as conn:
        row = conn.execute("SELECT lifecycle,scope FROM projections WHERE id=%s", (pid,)).fetchone()
        assert row == {"lifecycle": "invalidated", "scope": ""}
        assert conn.execute("SELECT bool_and(body='{}'::jsonb) AS empty FROM projection_versions WHERE projection_id=%s", (pid,)).fetchone()["empty"]
    assert "private polluted summary" not in flatten_context(service.recall("family"))


def test_deletion_version_is_idempotent_and_never_usable(service):
    item = seed(service)
    cid = item["card_ids"][0]
    service.delete_card(cid)
    unaffected = seed(service, "remaining memory")
    snapshot = service.recall("remaining memory")
    with db.tenant_transaction(service.tenant_id, mode="read") as conn:
        revision = conn.execute("SELECT revision FROM tenants WHERE id=%s", (service.tenant_id,)).fetchone()["revision"]
    service.delete_card(cid)
    with db.tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT revision FROM tenants WHERE id=%s", (service.tenant_id,)).fetchone()["revision"] == revision
        assert conn.execute("SELECT current_version FROM semantic_cards WHERE id=%s", (cid,)).fetchone()["current_version"] == 2
        assert conn.execute("SELECT bool_and(body='{}'::jsonb) AS empty FROM semantic_card_versions WHERE card_id=%s", (cid,)).fetchone()["empty"]
    with pytest.raises(ValueError):
        service.effective_event(cid)
    assert service.expand_snapshot(UUID(snapshot["snapshot"]["id"]), unaffected["card_ids"][0])["version"] == 1


def test_unsafe_flag_cannot_have_ambiguous_json_type(service):
    item = seed(service)
    with pytest.raises(ValueError, match="boolean"):
        service.add_event_delta(item["card_ids"][0], evidence("invalid delta source"),
            {"current_result": "unsafe", "requires_restructure": "true"}, idempotency_key="ambiguous")
    assert service.effective_event(item["card_ids"][0])["pending"] is False


def test_unsafe_support_cannot_publish_projection_that_reappears_after_catchup(service):
    item = seed(service, "unsafe projection gate")
    cid = item["card_ids"][0]
    service.add_event_delta(cid, evidence("new causal result", key="projection-unsafe-source"),
        {"current_result": "safe_new_body", "requires_restructure": True}, idempotency_key="projection-unsafe")
    projection_id = None
    rejected = False
    try:
        projection_id = service.materialize_projection(projection_type="relationship", projection_key="unsafe-new-view",
            scope="gate", body={"summary": "unsafe projection gate stale_after_catchup"}, support_card_ids=[cid])
    except ValueError as exc:
        assert "unsafe" in str(exc)
        rejected = True
    service.maintenance.make_pending_ready(job_type="event_rewrite")
    service.maintenance.run_ready(limit=1)
    effective = service.effective_event(cid)
    context = service.recall("unsafe projection gate")
    proof = {"rejected_before_insert": rejected, "created_projection_id": str(projection_id) if projection_id else None,
        "event_version": effective["version"], "event_pending": effective["pending"],
        "old_projection_visible_after_catchup": "stale_after_catchup" in flatten_context(context)}
    assert effective["version"] == 2 and not effective["pending"], proof
    assert effective["body"]["current_result"] == "safe_new_body", proof
    assert rejected and not proof["old_projection_visible_after_catchup"], proof
