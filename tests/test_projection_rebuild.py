from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from uuid import uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, VersionConflict
from tests.helpers import evidence, event, flatten_context
from tests.test_trust_boundaries import ControlledTransactions, named


def card(service, label):
    return service.observe(evidence(label, key=f"source:{label}"), semantics=[
        event({"goal_context": "rebuild life", "current_result": label}, key=f"event:{label}")])


def setup_rebuild(service):
    a, b, c = [card(service, name) for name in ["erased-A-private", "surviving-B", "counter-C"]]
    pid = service.materialize_projection(projection_type="relationship", projection_key="erased-original-key",
        scope="erased-original-scope", body={"summary": "erased-original-body"},
        support_card_ids=[a["card_ids"][0], b["card_ids"][0]])
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,'counterevidence')",
                     (service.tenant_id, pid, c["card_ids"][0]))
    service.delete_evidence(a["evidence_id"])
    return pid, a, b, c


def assert_erased(service, pid):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        target = conn.execute("SELECT lifecycle,scope FROM projections WHERE id=%s", (pid,)).fetchone()
        assert target["lifecycle"] in {"invalidated", "deleted"} and target["scope"] == ""
        assert conn.execute("SELECT bool_and(body='{}'::jsonb) AS empty FROM projection_versions WHERE projection_id=%s", (pid,)).fetchone()["empty"]


def commit(service, prepared):
    return service.maintenance.commit_projection_rebuild(prepared,
        replacement_body={"summary": "surviving-B with counter-C uncertainty"}, replacement_scope="remaining-life")


def test_read_is_immutable_canonical_complete_and_excludes_erased_content(service):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    assert prepared == service.maintenance.read_projection_rebuild(pid)
    for erased in ["erased-A-private", "erased-original-key", "erased-original-scope", "erased-original-body"]:
        assert erased not in prepared.canonical_input
    deps = prepared.payload["dependencies"]
    assert {dep["card_id"] for dep in deps} == {str(x["card_ids"][0]) for x in [a, b, c]}
    dead = next(dep for dep in deps if dep["card_id"] == str(a["card_ids"][0]))
    assert not dead["usable"] and "body" not in dead
    assert dead["sources"][0]["status"] == "deleted"
    counter = next(dep for dep in deps if dep["card_id"] == str(c["card_ids"][0]))
    assert counter["role"] == "counterevidence" and counter["body"]["current_result"] == "counter-C"
    with pytest.raises(FrozenInstanceError): prepared.canonical_input = "changed"
    detached = prepared.payload; detached["dependencies"].clear()
    assert len(prepared.payload["dependencies"]) == 3
    # DB row insertion order and JSON key order are irrelevant to the input fingerprint.
    with tenant_transaction(service.tenant_id) as conn:
        rows = conn.execute("DELETE FROM projection_supports WHERE projection_id=%s RETURNING *", (pid,)).fetchall()
        for row in reversed(rows):
            conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,%s)",
                         (row["tenant_id"], row["projection_id"], row["card_id"], row["support_role"]))
        conn.execute("UPDATE semantic_card_versions SET body=jsonb_build_object('current_result','surviving-B','goal_context','rebuild life') WHERE card_id=%s",
                     (b["card_ids"][0],))
    assert prepared.fingerprint == service.maintenance.read_projection_rebuild(pid).fingerprint
    assert_erased(service, pid)


def test_version_bound_rebuild_restores_same_id_from_remaining_support_and_keeps_marker(service):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    result = commit(service, prepared)
    assert result == {"projection_id": pid, "version": 3, "lifecycle": "active"}
    recalled = service.recall("surviving-B")
    view = next(view for view in recalled["layers"]["relationship_context"] if str(view["id"]) == str(pid))
    assert view["body"]["support_versions"] == {str(b["card_ids"][0]): 1}
    assert view["body"]["counterevidence_versions"] == {str(c["card_ids"][0]): 1}
    assert "erased-A-private" not in flatten_context(recalled)
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT scope FROM projections WHERE id=%s", (pid,)).fetchone()["scope"] == "remaining-life"
        assert conn.execute("SELECT bool_and(body='{}'::jsonb) AS empty FROM projection_versions WHERE projection_id=%s AND version<3", (pid,)).fetchone()["empty"]
        assert conn.execute("SELECT 1 FROM deletion_markers WHERE object_kind='projection_content' AND object_id=%s", (pid,)).fetchone()
        assert conn.execute("SELECT state FROM maintenance_jobs WHERE target_id=%s AND job_type='projection_resynthesis' ORDER BY created_at DESC LIMIT 1", (pid,)).fetchone()["state"] == "succeeded"
        # Even after a successful rebuild this lineage cannot use a naked replacement.
        service._enqueue_job_conn(conn, job_type="projection_resynthesis", target_kind="projection", target_id=pid,
            coalesce_key=f"projection_resynthesis:{pid}", baseline_version=3, available_after_seconds=0)
    retry = service.maintenance.run_ready(limit=1, projection_outputs={pid: {"summary": "unbound old output"}})
    assert retry[0]["state"] == "retry"
    assert "unbound old output" not in flatten_context(service.recall("surviving-B"))


@pytest.mark.parametrize("change", [
    "support_version", "counter_version", "support_delete", "counter_delete", "source_version", "source_status",
    "add_support", "remove_support", "change_role", "add_counter", "remove_counter", "add_source", "remove_source",
    "source_role", "target_version", "unsafe_support", "unsafe_counter", "safe_pending_delta", "card_lifecycle",
])
def test_read_then_dependency_or_target_change_rejects_stale_output(service, change):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    bid, cid = b["card_ids"][0], c["card_ids"][0]
    if change in {"support_version", "counter_version"}:
        target = bid if change == "support_version" else cid
        service.correct_card(target, evidence("later correction", key="later"),
            {"goal_context": "rebuild life", "current_result": "corrected"}, expected_version=1)
    elif change in {"support_delete", "counter_delete"}:
        service.delete_evidence((b if change == "support_delete" else c)["evidence_id"])
    elif change in {"unsafe_support", "unsafe_counter", "safe_pending_delta"}:
        service.add_event_delta(cid if change == "unsafe_counter" else bid, evidence("later delta", key="delta"),
            {"current_result": "changed", "requires_restructure": change != "safe_pending_delta"}, idempotency_key="delta")
    else:
        d = card(service, "new-D") if change in {"add_support", "add_counter", "add_source"} else None
        with tenant_transaction(service.tenant_id) as conn:
            if change == "source_version": conn.execute("UPDATE evidence SET version=version+1 WHERE id=%s", (b["evidence_id"],))
            if change == "source_status": conn.execute("UPDATE evidence SET status='corrected' WHERE id=%s", (c["evidence_id"],))
            if change in {"add_support", "add_counter"}:
                conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,%s)",
                    (service.tenant_id, pid, d["card_ids"][0], "support" if change == "add_support" else "counterevidence"))
            if change == "remove_support": conn.execute("DELETE FROM projection_supports WHERE projection_id=%s AND card_id=%s", (pid, bid))
            if change == "remove_counter": conn.execute("DELETE FROM projection_supports WHERE projection_id=%s AND card_id=%s", (pid, cid))
            if change == "change_role": conn.execute("UPDATE projection_supports SET support_role='support' WHERE projection_id=%s AND card_id=%s", (pid, cid))
            if change == "add_source": conn.execute("INSERT INTO card_sources VALUES (%s,%s,%s,'counterevidence')", (service.tenant_id, bid, d["evidence_id"]))
            if change == "remove_source": conn.execute("DELETE FROM card_sources WHERE card_id=%s", (cid,))
            if change == "source_role": conn.execute("UPDATE card_sources SET source_role='counterevidence' WHERE card_id=%s", (bid,))
            if change == "card_lifecycle": conn.execute("UPDATE semantic_cards SET lifecycle='invalidated' WHERE id=%s", (cid,))
            if change == "target_version":
                conn.execute("UPDATE projections SET current_version=current_version+1 WHERE id=%s", (pid,))
                conn.execute("INSERT INTO projection_versions SELECT tenant_id,id,current_version,'{}'::jsonb,lifecycle,epistemic_state,now() FROM projections WHERE id=%s", (pid,))
    with pytest.raises(VersionConflict): commit(service, prepared)
    assert_erased(service, pid)


def test_rebuild_requires_erased_target_and_positive_support_and_own_tenant(service):
    item = card(service, "only-support")
    pid = service.materialize_projection(projection_type="relationship", projection_key="only-view", scope="one",
        body={"summary": "old"}, support_card_ids=item["card_ids"])
    with pytest.raises(VersionConflict): service.maintenance.read_projection_rebuild(pid)
    service.delete_evidence(item["evidence_id"])
    with pytest.raises(VersionConflict): service.maintenance.read_projection_rebuild(pid)
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    other = MemoryService(uuid4()); other.ensure_tenant()
    with pytest.raises(NotFound): other.maintenance.read_projection_rebuild(pid)
    with pytest.raises(NotFound): commit(other, prepared)
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE projection_supports SET support_role='counterevidence' WHERE projection_id=%s AND card_id=%s", (pid, b["card_ids"][0]))
    with pytest.raises(VersionConflict, match="no valid"): service.maintenance.read_projection_rebuild(pid)
    assert_erased(service, pid)


def test_reusing_prepared_after_success_cannot_overwrite_newer_projection(service):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    commit(service, prepared)
    with pytest.raises(VersionConflict):
        service.maintenance.commit_projection_rebuild(prepared, replacement_body={"summary": "stale"}, replacement_scope="stale")
    assert "stale" not in flatten_context(service.recall("surviving-B"))


@pytest.mark.parametrize("delete_first", [False, True])
def test_rebuild_commit_and_delete_use_actual_gate_order(service, monkeypatch, delete_first):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    control = ControlledTransactions(monkeypatch, "delete" if delete_first else "rebuild",
        "FOR UPDATE" if delete_first else "INSERT INTO projection_versions", after=delete_first)

    def rebuild():
        try:
            return commit(service, prepared)
        except VersionConflict:
            return "rejected"

    actions = {"delete": lambda: service.delete_evidence(b["evidence_id"]), "rebuild": rebuild}
    first, second = ("delete", "rebuild") if delete_first else ("rebuild", "delete")
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(named, first, actions[first])
        try:
            assert control.ready.wait(5)
            end = pool.submit(named, second, actions[second])
            control.wait_blocked(second, first)
        finally:
            control.release.set()
        result = {first: start.result(timeout=6), second: end.result(timeout=6)}
    if delete_first: assert result["rebuild"] == "rejected"
    else: assert result["rebuild"]["lifecycle"] == "active"
    assert_erased(service, pid)
    assert not service.recall("surviving-B")["layers"]["relationship_context"]
    control.record(f"rebuild-delete:{delete_first}")
