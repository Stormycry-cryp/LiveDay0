from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import uuid4
from pathlib import Path

import psycopg
import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import VersionConflict
from liveday0.migrations import migrate_down, migrate_up, migration_status
from tests.helpers import evidence, publish_update
from tests.test_maintenance_reliability import append_delta, focus_event, make_view, new_event, read_job, view_state
from tests.test_projection_rebuild import setup_rebuild


def record(name, payload):
    path = os.environ.get("_MAINT_WAIT_OBSERVATIONS")
    if path:
        with Path(path).open("a") as out:
            out.write(json.dumps({"case": name, **payload}, default=str) + "\n")


def waiting_view(service):
    cid = new_event(service)
    pid = make_view(service, cid)
    service.correct_card(cid, evidence("corrected work", key="correction"),
                         {"goal_context": "work", "current_result": "corrected"}, expected_version=1)
    return cid, pid


def notify(service, cid):
    with tenant_transaction(service.tenant_id) as conn:
        service.maintenance._invalidate_dependents(conn, cid)


def test_output_wait_survives_idle_polls_and_late_output_without_new_input(service):
    cid, pid = waiting_view(service)
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "waiting"
    before = read_job(service, pid, "projection_resynthesis")
    assert before["wait_reason"] == "semantic_output_required" and before["failure_count"] == 0
    for _ in range(5):
        assert service.maintenance.run_ready(limit=20) == []
        notify(service, cid)
    after = read_job(service, pid, "projection_resynthesis")
    assert (after["id"], after["state"], after["attempts"], after["failure_count"]) == (
        before["id"], "waiting", before["attempts"], 0)
    assert after["wait_input_fingerprint"] == before["wait_input_fingerprint"]
    with pytest.raises(ValueError):
        service.maintenance.run_ready(projection_outputs={pid: ["invalid output"]})
    assert read_job(service, pid, "projection_resynthesis")["state"] == "waiting"
    assert publish_update(service, pid, {"summary": "later output"})["lifecycle"] == "active"
    final = read_job(service, pid, "projection_resynthesis")
    assert final["id"] == before["id"] and final["failure_count"] == 0
    assert final["wait_reason"] is None and final["wait_input_fingerprint"] is None
    assert view_state(service, pid)["lifecycle"] == "active"


def test_dependency_catchup_wakes_same_job_without_another_observation(service):
    cid = new_event(service); pid = make_view(service, cid)
    append_delta(service, cid)
    focus_event(service, pid)  # Only the dependent projection is due first.
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "waiting"
    before = read_job(service, pid, "projection_resynthesis")
    assert before["wait_reason"] == "dependency_pending"
    with pytest.raises(VersionConflict, match="pending"):
        publish_update(service, pid, {"summary": "too early"})
    service.maintenance.make_pending_ready(job_type="event_rewrite")
    result = service.maintenance.run_ready(limit=2)
    assert [row["state"] for row in result] == ["succeeded", "waiting"]
    after = read_job(service, pid, "projection_resynthesis")
    assert after["id"] == before["id"] and after["wait_reason"] == "semantic_output_required"
    assert after["failure_count"] == 0 and after["wait_input_fingerprint"] != before["wait_input_fingerprint"]
    assert publish_update(service, pid, {"summary": "caught up"})["lifecycle"] == "active"


def test_version_bound_wait_survives_and_late_bound_commit_completes_it(service):
    pid, *_ = setup_rebuild(service)
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "waiting"
    before = read_job(service, pid, "projection_resynthesis")
    assert before["wait_reason"] == "version_bound_rebuild_required"
    for _ in range(4):
        with pytest.raises(ValueError, match="unbound"):
            service.maintenance.run_ready(limit=20, projection_outputs={pid: {"summary": "unbound"}})
    assert read_job(service, pid, "projection_resynthesis")["attempts"] == before["attempts"]
    prepared = service.maintenance.read_projection_rebuild(pid)
    service.maintenance.commit_projection_rebuild(prepared, replacement_body={"summary": "remaining"}, replacement_scope="remaining")
    final = read_job(service, pid, "projection_resynthesis")
    assert final["state"] == "succeeded" and final["failure_count"] == 0
    assert final["wait_reason"] is None and final["wait_input_fingerprint"] is None


def test_notifications_and_waits_never_reset_real_failure_budget(service, monkeypatch):
    cid, pid = waiting_view(service)
    worker = service.maintenance; original = worker._resynthesize_projection
    monkeypatch.setattr(worker, "_resynthesize_projection", lambda conn, *args: conn.execute("SELECT 1/0"))
    assert worker.run_ready(limit=1)[0]["state"] == "retry"
    monkeypatch.setattr(worker, "_resynthesize_projection", original)
    worker.make_retries_ready()
    assert worker.run_ready(limit=1)[0]["state"] == "waiting"
    before = read_job(service, pid, "projection_resynthesis")
    for _ in range(5): notify(service, cid)
    assert worker.run_ready(limit=5) == []
    after = read_job(service, pid, "projection_resynthesis")
    assert after["attempts"] == before["attempts"] and after["failure_count"] == 1
    assert "22012" in after["last_error"]
    append_delta(service, cid, label="new dependency")
    assert read_job(service, pid, "projection_resynthesis")["failure_count"] == 1
    worker.make_pending_ready(job_type="event_rewrite"); worker.run_ready(limit=5)
    assert read_job(service, pid, "projection_resynthesis")["failure_count"] == 1
    monkeypatch.setattr(worker, "_resynthesize_projection", lambda conn, *args: conn.execute("SELECT 1/0"))
    assert worker.resume_waiting(before["id"])
    assert worker.run_ready(limit=1)[0]["state"] == "retry"
    worker.make_retries_ready()
    assert worker.run_ready(limit=1)[0]["state"] == "dead"
    dead = read_job(service, pid, "projection_resynthesis")
    assert dead["failure_count"] == 3
    notify(service, cid)
    append_delta(service, cid, label="change after dead")
    worker.make_pending_ready(job_type="event_rewrite"); worker.run_ready(limit=5)
    with pytest.raises(ValueError, match="unbound"):
        worker.run_ready(limit=10, projection_outputs={pid: {"summary": "another output"}})
    assert worker.run_ready(limit=10) == []
    assert not worker.resume_waiting(dead["id"])
    assert read_job(service, pid, "projection_resynthesis")["id"] == dead["id"]
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT count(*) AS n FROM maintenance_jobs WHERE target_id=%s", (pid,)).fetchone()["n"] == 1


def test_dead_event_is_not_recreated_by_more_deltas(service, monkeypatch):
    cid = new_event(service); append_delta(service, cid); focus_event(service, cid)
    worker = service.maintenance
    monkeypatch.setattr(worker, "_rewrite_event", lambda conn, job: conn.execute("SELECT 1/0"))
    for state in ["retry", "retry", "dead"]:
        assert worker.run_ready(limit=1)[0]["state"] == state
        worker.make_retries_ready()
    dead = read_job(service, cid)
    append_delta(service, cid, label="more input")
    assert worker.run_ready(limit=5) == []
    assert read_job(service, cid)["id"] == dead["id"]
    assert read_job(service, cid)["failure_count"] == 3


def test_dead_projection_cannot_bypass_budget_through_bound_commit(service, monkeypatch):
    pid, _, _, counter = setup_rebuild(service); worker = service.maintenance
    prepared = worker.read_projection_rebuild(pid)
    monkeypatch.setattr(worker, "_resynthesize_projection", lambda conn, *args: conn.execute("SELECT 1/0"))
    for state in ["retry", "retry", "dead"]:
        assert worker.run_ready(limit=1)[0]["state"] == state
        worker.make_retries_ready()
    with pytest.raises(VersionConflict, match="terminal maintenance"):
        worker.commit_projection_rebuild(prepared, replacement_body={"summary": "output"}, replacement_scope="remaining")
    assert view_state(service, pid)["lifecycle"] == "invalidated"
    dead = read_job(service, pid, "projection_resynthesis")
    service.delete_evidence(counter["evidence_id"])
    assert read_job(service, pid, "projection_resynthesis")["id"] == dead["id"]
    assert worker.run_ready(limit=5) == []


def test_trusted_resume_is_tenant_scoped_and_preserves_budget(service, monkeypatch):
    _, pid = waiting_view(service); worker = service.maintenance
    assert worker.run_ready(limit=1, fail_job_types={"projection_resynthesis"})[0]["state"] == "retry"
    worker.make_retries_ready(); assert worker.run_ready(limit=1)[0]["state"] == "waiting"
    job = read_job(service, pid, "projection_resynthesis")
    other = MemoryService(uuid4()); other.ensure_tenant()
    assert not other.maintenance.resume_waiting(job["id"])
    assert worker.resume_waiting(job["id"])
    assert worker.run_ready(limit=1)[0]["state"] == "waiting"
    assert read_job(service, pid, "projection_resynthesis")["failure_count"] == 1


def test_baseline_refresh_reads_current_canonical_without_failure(service):
    cid = new_event(service); append_delta(service, cid)
    service.correct_card(cid, evidence("new baseline", key="baseline"),
                         {"goal_context": "work", "current_result": "corrected"}, expected_version=1)
    append_delta(service, cid, label="after correction"); focus_event(service, cid)
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "succeeded"
    assert service.effective_event(cid)["version"] == 3
    assert service.effective_event(cid)["body"]["current_result"] == "after correction"
    job = read_job(service, cid)
    assert job["baseline_version"] == 2 and job["failure_count"] == 0


def child(service, mode, pid=None):
    code = '''import json,os,sys
from uuid import UUID
from liveday0.core import MemoryService
s=MemoryService(UUID(sys.argv[1]));mode=sys.argv[2]
if mode=='fail':
    s.maintenance.make_retries_ready()
    s.maintenance._rewrite_event=lambda conn,job: conn.execute('SELECT 1/0')
if mode=='output':
    prepared=s.maintenance.read_projection_update(UUID(sys.argv[3]))
    result=[s.maintenance.commit_projection(prepared,replacement_body={'summary':'late process output'})]
else: result=s.maintenance.run_ready(limit=1)
print(json.dumps({'pid':os.getpid(),'result':result},default=str))
'''
    result = subprocess.run([sys.executable,"-c",code,str(service.tenant_id),mode,str(pid or "")],
                            env=dict(os.environ),text=True,capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_normal_process_restarts_continue_waiting_and_real_failure_budget(service):
    _, pid = waiting_view(service)
    a = child(service, "poll")
    assert a["result"][0]["state"] == "waiting"
    before = read_job(service, pid, "projection_resynthesis")
    b = child(service, "poll"); assert b["result"] == []
    assert read_job(service, pid, "projection_resynthesis")["attempts"] == before["attempts"]
    c = child(service, "output", pid); assert c["result"][0]["lifecycle"] == "active"
    record("process-wait-output", {"processes": [a,b,c], "waiting_attempts": before["attempts"],
        "final_job": read_job(service,pid,"projection_resynthesis")})
    cid = new_event(service,"process failures"); append_delta(service,cid); focus_event(service,cid)
    processes = [a["pid"], b["pid"], c["pid"]]
    for failures, state in [(1,"retry"),(2,"retry"),(3,"dead")]:
        value = child(service,"fail"); processes.append(value["pid"])
        assert value["result"][0]["state"] == state
        assert read_job(service,cid)["failure_count"] == failures
        record("process-failure-budget", {"process": value, "job": read_job(service,cid)})
    assert child(service,"poll")["result"] == []
    assert len(set(processes)) == len(processes)


def test_upgrade_preserves_old_history_and_parks_recognized_wait(service):
    cid = new_event(service); pid = make_view(service,cid)
    # Recreate a pre-003 fixture without retaining post-004 source receipts.
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE evidence SET request_fingerprint=NULL")
        conn.execute("UPDATE event_deltas SET request_fingerprint=NULL")
    assert migrate_down(2) == [4, 3]
    try:
        with tenant_transaction(service.tenant_id) as conn:
            conn.execute("UPDATE projections SET lifecycle='invalidated' WHERE id=%s",(pid,))
            conn.execute("""INSERT INTO maintenance_jobs(tenant_id,job_type,target_kind,target_id,coalesce_key,
              state,attempts,last_error) VALUES (%s,'projection_resynthesis','projection',%s,%s,'retry',7,
              'bounded semantic replacement required')""",(service.tenant_id,pid,f"projection_resynthesis:{pid}"))
        assert migrate_up() == [3,4]
        row = read_job(service,pid,"projection_resynthesis")
        assert row["state"] == "waiting" and row["wait_reason"] == "semantic_output_required"
        assert row["attempts"] == 7 and row["failure_count"] == 0
        assert row["last_error"] == "bounded semantic replacement required"
        assert service.maintenance.run_ready(limit=10) == []
        assert publish_update(service,pid,{"summary":"late"})["lifecycle"] == "active"
    finally:
        migrate_up()


@pytest.mark.parametrize("kind", ["waiting","failure"])
def test_down_migration_refuses_to_discard_wait_or_budget(service,kind):
    _, pid = waiting_view(service)
    failures = {"projection_resynthesis"} if kind == "failure" else ()
    service.maintenance.run_ready(limit=1,fail_job_types=failures)
    before = read_job(service,pid,"projection_resynthesis")
    # Pass 004's separately tested guard to exercise the existing 003 guard.
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE evidence SET request_fingerprint=NULL")
        conn.execute("UPDATE event_deltas SET request_fingerprint=NULL")
    try:
        with pytest.raises(psycopg.errors.RaiseException,match="explicit reconciliation"):
            migrate_down(2)
        assert [r["version"] for r in migration_status()] == [1,2,3,4]
        assert read_job(service,pid,"projection_resynthesis") == before
    finally:
        migrate_up()
