from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction
from liveday0.exceptions import VersionConflict
from tests.helpers import evidence
from tests.test_projection_rebuild import assert_erased, card, setup_rebuild


def dependency_identity(service, item):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return {
            "version": conn.execute("SELECT current_version FROM semantic_cards WHERE id=%s",
                                    (item["card_ids"][0],)).fetchone()["current_version"],
            "sources": conn.execute(
                """SELECT cs.evidence_id,cs.source_role,e.version,e.status
                FROM card_sources cs JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id
                WHERE cs.tenant_id=%s AND cs.card_id=%s ORDER BY cs.evidence_id,cs.source_role""",
                (service.tenant_id, item["card_ids"][0]),
            ).fetchall(),
        }


@pytest.mark.parametrize("role", ["support", "counterevidence"])
@pytest.mark.parametrize("timing", ["before_read", "after_read"])
def test_safe_pending_requires_catchup_even_when_source_set_is_unchanged(service, role, timing):
    pid, a, b, c = setup_rebuild(service)
    item, label = (b, "surviving-B") if role == "support" else (c, "counter-C")
    prepared = service.maintenance.read_projection_rebuild(pid) if timing == "after_read" else None
    before = dependency_identity(service, item)
    changed_result = f"new-{role}-result"
    service.add_event_delta(item["card_ids"][0], evidence(label, key=f"source:{label}"),
        {"current_result": changed_result}, idempotency_key="reuse-existing-source")
    assert dependency_identity(service, item) == before  # No new source edge or canonical version.
    effective = service.effective_event(item["card_ids"][0])
    assert effective["version"] == 1 and effective["pending"]
    assert effective["body"]["current_result"] == changed_result
    rejected = False
    proof = {"role": role, "timing": timing, "canonical_and_sources_unchanged": True,
             "effective_pending": effective["pending"], "effective_result": effective["body"]["current_result"]}
    try:
        if timing == "before_read":
            stale = service.maintenance.read_projection_rebuild(pid)
            proof["prepared_result"] = next(dep["body"]["current_result"] for dep in stale.payload["dependencies"]
                                            if dep["card_id"] == str(item["card_ids"][0]))
        else:
            proof["stale_commit"] = service.maintenance.commit_projection_rebuild(prepared,
                replacement_body={"summary": "old canonical output"}, replacement_scope="old-read")
    except VersionConflict:
        rejected = True
    assert rejected, proof
    assert_erased(service, pid)

    service.maintenance.make_pending_ready(job_type="event_rewrite")
    service.maintenance.run_ready(limit=8)
    effective = service.effective_event(item["card_ids"][0])
    assert effective["version"] == 2 and not effective["pending"]
    if prepared is not None:
        with pytest.raises(VersionConflict):
            service.maintenance.commit_projection_rebuild(prepared,
                replacement_body={"summary": "old canonical output"}, replacement_scope="old-read")
    fresh = service.maintenance.read_projection_rebuild(pid)
    dependency = next(dep for dep in fresh.payload["dependencies"] if dep["card_id"] == str(item["card_ids"][0]))
    assert dependency["version"] == 2 and dependency["body"]["current_result"] == changed_result
    outcome = service.maintenance.commit_projection_rebuild(fresh,
        replacement_body={"summary": dependency["body"]["current_result"]}, replacement_scope="fresh-read")
    assert outcome == {"projection_id": pid, "version": 3, "lifecycle": "active"}


@pytest.mark.parametrize("delta_state", ["pending", "invalidated"])
@pytest.mark.parametrize("unsafe", [False, True])
def test_erased_dependency_residual_delta_cannot_block_or_enter_rebuild(service, delta_state, unsafe):
    pid, a, b, c = setup_rebuild(service)
    prepared = service.maintenance.read_projection_rebuild(pid)
    # Simulate a pre-upgrade residual row; ordinary service writes reject deleted targets.
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute(
            """INSERT INTO event_deltas(tenant_id,event_id,evidence_id,delta,state,idempotency_key)
            VALUES (%s,%s,%s,%s,%s,'legacy-unused')""",
            (service.tenant_id, a["card_ids"][0], a["evidence_id"],
             Jsonb({"current_result": "erased-residual-must-not-leak", "requires_restructure": unsafe}), delta_state),
        )
    fresh = service.maintenance.read_projection_rebuild(pid)
    assert fresh == prepared
    assert "erased-residual-must-not-leak" not in fresh.canonical_input
    result = service.maintenance.commit_projection_rebuild(prepared,
        replacement_body={"summary": "surviving-B"}, replacement_scope="remaining-only")
    assert result["lifecycle"] == "active"


def test_unrelated_pending_event_does_not_conflict_with_prepared_rebuild(service):
    pid, a, b, c = setup_rebuild(service)
    other = card(service, "unrelated-event")
    prepared = service.maintenance.read_projection_rebuild(pid)
    service.add_event_delta(other["card_ids"][0], evidence("unrelated-event", key="source:unrelated-event"),
        {"current_result": "unrelated change"}, idempotency_key="unrelated-delta")
    assert service.effective_event(other["card_ids"][0])["pending"]
    assert service.maintenance.read_projection_rebuild(pid) == prepared
    assert service.maintenance.commit_projection_rebuild(prepared,
        replacement_body={"summary": "surviving-B"}, replacement_scope="remaining-only")["lifecycle"] == "active"
