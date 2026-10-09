from __future__ import annotations

from uuid import UUID

import pytest

from liveday0.db import tenant_transaction
from tests.helpers import evidence, event, flatten_context


def new_event(service, label="work"):
    return service.observe(evidence(label, key=f"source:{label}"), semantics=[
        event({"goal_context": label, "current_result": "old"}, key=f"event:{label}")
    ])["card_ids"][0]


def append_delta(service, card_id, *, label="change", unsafe=False):
    body = {"current_result": label}
    if unsafe:
        body["requires_restructure"] = True
    service.add_event_delta(card_id, evidence(label, key=f"source:{label}"), body,
                            idempotency_key=f"delta:{label}")


def read_job(service, card_id, kind="event_rewrite"):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return conn.execute(
            """SELECT *,extract(epoch FROM available_at-updated_at) AS delay
            FROM maintenance_jobs WHERE tenant_id=%s AND target_id=%s AND job_type=%s
            ORDER BY created_at DESC LIMIT 1""", (service.tenant_id, card_id, kind)
        ).fetchone()


def focus_event(service, card_id):
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE maintenance_jobs SET available_at=now()+interval '1 day' WHERE tenant_id=%s",
                     (service.tenant_id,))
        conn.execute("UPDATE maintenance_jobs SET available_at=now() WHERE tenant_id=%s AND target_id=%s",
                     (service.tenant_id, card_id))


def make_view(service, card_id, *, key="state", counter_id=None):
    pid = service.materialize_projection(projection_type="current_state", projection_key=key,
        scope="work", body={"summary": f"{key}-old-view"}, support_card_ids=[card_id])
    if counter_id:
        with tenant_transaction(service.tenant_id) as conn:
            conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,'counterevidence')",
                         (service.tenant_id, pid, counter_id))
    return pid


def view_state(service, projection_id):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return conn.execute("SELECT lifecycle,current_version FROM projections WHERE id=%s",
                            (projection_id,)).fetchone()


@pytest.mark.parametrize("error_sql,sqlstate", [
    ("SELECT 1/0", "22012"),
    ("UPDATE maintenance_jobs SET state='private-payload-must-not-be-logged'", "23514"),
])
def test_sql_failure_rolls_back_target_but_keeps_attempt_and_error(service, monkeypatch, error_sql, sqlstate):
    card_id = new_event(service)
    append_delta(service, card_id)
    focus_event(service, card_id)
    worker = service.maintenance
    original = worker._rewrite_event

    def fail_after_write(conn, job):
        original(conn, job)  # Fail after canonical version, absorption and revision writes.
        conn.execute(error_sql)

    monkeypatch.setattr(worker, "_rewrite_event", fail_after_write)
    outer_error = None
    try:
        result = worker.run_ready(limit=1)
    except Exception as exc:
        outer_error = type(exc).__name__
    persisted = read_job(service, card_id)
    assert (persisted["state"], persisted["attempts"]) == ("retry", 1), outer_error
    assert outer_error is None and result[0]["state"] == "retry"
    assert sqlstate in persisted["last_error"]
    assert "private-payload" not in persisted["last_error"]
    assert persisted["locked_at"] is None and persisted["delay"] == 1
    effective = service.effective_event(card_id)
    assert effective["version"] == 1 and effective["pending"]
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT count(*) AS n FROM semantic_card_versions WHERE card_id=%s",
                            (card_id,)).fetchone()["n"] == 1
    monkeypatch.setattr(worker, "_rewrite_event", original)
    worker.make_retries_ready()
    assert worker.run_ready(limit=1)[0]["state"] == "succeeded"
    assert read_job(service, card_id)["attempts"] == 2
    assert read_job(service, card_id)["last_error"] is None
    effective = service.effective_event(card_id)
    assert effective["version"] == 2 and not effective["pending"]
    assert effective["body"]["current_result"] == "change"


@pytest.mark.parametrize("role", ["support", "counterevidence"])
def test_normal_delta_invalidates_dependent_view_before_rewrite(service, role):
    support, counter = new_event(service), new_event(service, "counter")
    pid = make_view(service, support, counter_id=counter)
    unaffected = make_view(service, new_event(service, "unrelated"), key="unaffected")
    before = service.recall("work")
    assert "state-old-view" in flatten_context(before)
    append_delta(service, support if role == "support" else counter)
    assert view_state(service, pid)["lifecycle"] == "invalidated"
    assert view_state(service, unaffected)["lifecycle"] == "active"
    assert "state-old-view" not in flatten_context(service.recall("work"))
    # Ordinary updates preserve an already pinned cycle, matching the existing contract.
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT state FROM recall_snapshots WHERE id=%s",
                            (UUID(before["snapshot"]["id"]),)).fetchone()["state"] == "active"
    service.expand_snapshot(UUID(before["snapshot"]["id"]), support)


def test_unimplemented_candidate_discovery_is_terminal_not_success(service):
    source = service.observe(evidence("ordinary trace", key="trace"))
    job_id = service.maintenance.enqueue_candidate_discovery(source["evidence_id"])
    assert service.maintenance.run_ready(limit=1) == [
        {"job_id": job_id, "state": "dead", "outcome": "candidate_discovery_not_implemented"}
    ]
    job = read_job(service, source["evidence_id"], "candidate_discovery")
    assert job["attempts"] == 1 and job["last_error"] == "candidate_discovery_not_implemented"
    assert job["locked_at"] is None and service.maintenance.run_ready(limit=1) == []
    assert service.observe(evidence("new evidence still works", key="next"))["evidence_id"]


def test_real_sql_failure_has_finite_backoff_and_terminal_budget(service, monkeypatch):
    card_id = new_event(service)
    append_delta(service, card_id)
    focus_event(service, card_id)
    monkeypatch.setattr(service.maintenance, "_rewrite_event", lambda conn, job: conn.execute("SELECT 1/0"))
    for attempt, state in [(1, "retry"), (2, "retry"), (3, "dead")]:
        assert service.maintenance.run_ready(limit=1)[0]["state"] == state
        job = read_job(service, card_id)
        assert job["attempts"] == attempt and "22012" in job["last_error"]
        assert job["locked_at"] is None
        if state == "retry":
            assert job["delay"] == 2 ** (attempt - 1)
            service.maintenance.make_retries_ready()
    assert service.maintenance.run_ready(limit=10) == []
    assert service.maintenance.make_retries_ready() == 0
    assert service.effective_event(card_id)["pending"]
    assert service.observe(evidence("survives failed maintenance", key="survives"))["evidence_id"]


def test_legacy_exhausted_ready_job_does_not_execute_again(service, monkeypatch):
    card_id = new_event(service)
    append_delta(service, card_id)
    focus_event(service, card_id)
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE maintenance_jobs SET state='retry',attempts=9,failure_count=3,last_error='prior diagnostic'")
    calls = []
    monkeypatch.setattr(service.maintenance, "_rewrite_event", lambda *args: calls.append(True))
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "dead"
    assert calls == []
    job = read_job(service, card_id)
    assert job["attempts"] == 9 and job["last_error"] == "prior diagnostic"


def test_failed_job_does_not_abort_next_job_in_batch(service, monkeypatch):
    bad, good = new_event(service, "bad"), new_event(service, "good")
    for cid, label in [(bad, "bad-change"), (good, "good-change")]:
        append_delta(service, cid, label=label)
    service.maintenance.make_pending_ready(job_type="event_rewrite")
    original = service.maintenance._rewrite_event

    def rewrite(conn, job):
        result = original(conn, job)
        if job["target_id"] == bad:
            conn.execute("SELECT 1/0")
        return result

    monkeypatch.setattr(service.maintenance, "_rewrite_event", rewrite)
    assert {r["state"] for r in service.maintenance.run_ready(limit=2)} == {"retry", "succeeded"}
    assert service.effective_event(bad)["version"] == 1
    assert service.effective_event(good)["version"] == 2


def test_unsafe_catchup_and_new_deltas_preserve_retry_backoff(service, monkeypatch):
    card_id = new_event(service)
    append_delta(service, card_id, unsafe=True)
    focus_event(service, card_id)
    original = service.maintenance._rewrite_event
    monkeypatch.setattr(service.maintenance, "_rewrite_event", lambda conn, job: conn.execute("SELECT 1/0"))
    service.maintenance.run_ready(limit=1)
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE maintenance_jobs SET available_at=now()+interval '1 day'")
    before = read_job(service, card_id)
    append_delta(service, card_id, label="second-change", unsafe=True)
    service.maintenance.catch_up_unsafe_overlays()
    after = read_job(service, card_id)
    assert after["attempts"] == 1 and after["available_at"] == before["available_at"]
    monkeypatch.setattr(service.maintenance, "_rewrite_event", original)
    service.maintenance.make_retries_ready()
    assert service.maintenance.catch_up_unsafe_overlays()[0]["state"] == "succeeded"
    assert service.effective_event(card_id)["body"]["current_result"] == "second-change"


def test_rewrite_invalidates_view_created_during_safe_pending(service):
    card_id = new_event(service)
    append_delta(service, card_id)
    pid = make_view(service, card_id)
    focus_event(service, card_id)
    assert service.maintenance.run_ready(limit=1)[0]["state"] == "succeeded"
    assert view_state(service, pid)["lifecycle"] == "invalidated"
    assert "state-old-view" not in flatten_context(service.recall("work"))


@pytest.mark.parametrize("role", ["support", "counterevidence"])
def test_safe_pending_blocks_legacy_replacement_until_catchup(service, role):
    support, counter = new_event(service), new_event(service, "counter")
    pid = make_view(service, support, counter_id=counter)
    changed = support if role == "support" else counter
    append_delta(service, changed)
    with tenant_transaction(service.tenant_id) as conn:
        conn.execute("UPDATE maintenance_jobs SET available_at=now()+interval '1 day' WHERE target_id=%s", (changed,))
    service.maintenance.run_ready(limit=1, projection_outputs={pid: {"summary": "stale replacement"}})
    assert view_state(service, pid)["lifecycle"] == "invalidated"
    assert read_job(service, pid, "projection_resynthesis")["state"] == "waiting"
    focus_event(service, changed)
    service.maintenance.run_ready(limit=1)
    service.maintenance.make_retries_ready()
    result = service.maintenance.run_ready(limit=1, projection_outputs={pid: {"summary": "fresh replacement"}})
    assert result[0]["state"] == "succeeded"
    assert view_state(service, pid)["lifecycle"] == "active"
