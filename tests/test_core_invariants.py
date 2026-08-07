from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import connect, tenant_transaction
from liveday0.exceptions import NotFound, SnapshotInvalidated, VersionConflict
from liveday0.migrations import migrate_down, migrate_up, migration_status
from liveday0.types import RecallOptions
from tests.helpers import evidence, event, fact, flatten_context


def test_observe_is_idempotent_and_does_not_duplicate_semantics(service):
    request = evidence("一次明确事实", key="idempotent-evidence")
    semantic = fact({"proposition": "明确事实", "scope": "当前测试"}, key="fact:idempotent")
    first = service.observe(request, semantics=[semantic])
    second = service.observe(request, semantics=[semantic])
    assert first["created"] is True
    assert second["created"] is False
    assert second["evidence_id"] == first["evidence_id"]
    assert second["card_ids"] == []
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 1


def test_concurrent_delta_retry_and_atomic_absorption(service):
    event_id = service.observe(
        evidence("事件开始", key="delta-event"),
        semantics=[
            event(
                {"goal_context": "连续事件", "current_result": "开始"},
                key="event:delta-concurrency",
            )
        ],
    )["card_ids"][0]

    def append_once():
        return service.add_event_delta(
            event_id,
            evidence("事件有了结果", key="delta-evidence"),
            {"current_result": "已取得结果", "unfinished_future": "等待确认"},
            idempotency_key="same-delta",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: append_once(), range(2)))
    assert sum(result["created"] for result in results) == 1
    effective = service.effective_event(event_id)
    assert effective["pending"] is True
    assert effective["body"]["current_result"] == "已取得结果"
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM event_deltas").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM maintenance_jobs").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT available_at > now() AS delayed FROM maintenance_jobs"
        ).fetchone()["delayed"] is True

    service.maintenance.make_pending_ready(job_type="event_rewrite")
    failed = service.maintenance.run_ready(limit=1, fail_job_types={"event_rewrite"})
    assert failed[0]["state"] == "retry"
    assert service.effective_event(event_id)["pending"] is True
    service.maintenance.make_retries_ready()
    succeeded = service.maintenance.run_ready(limit=1)
    assert succeeded[0]["state"] == "succeeded"
    final = service.effective_event(event_id)
    assert final["pending"] is False
    assert final["version"] == 2
    assert final["body"]["unfinished_future"] == "等待确认"


def test_expected_version_prevents_lost_correction(service):
    card_id = service.observe(
        evidence("初始理解", key="versioned-card"),
        semantics=[event({"goal_context": "版本测试", "current_result": "v1"}, key="event:versioned")],
    )["card_ids"][0]
    service.correct_card(
        card_id,
        evidence("第一次纠正", key="correction-v2"),
        {"goal_context": "版本测试", "current_result": "v2"},
        expected_version=1,
    )
    with pytest.raises(VersionConflict):
        service.correct_card(
            card_id,
            evidence("并发旧版本纠正", key="stale-correction"),
            {"goal_context": "版本测试", "current_result": "stale"},
            expected_version=1,
        )
    assert service.recall("版本测试")["layers"]["events"][0]["body"]["current_result"] == "v2"


def test_polluted_projection_needs_validated_bounded_replacement(service):
    card_id = service.observe(
        evidence("错误解释来源", key="projection-source"),
        semantics=[
            event(
                {"goal_context": "工作状态", "current_result": "已辞职"},
                key="event:projection-correction",
            )
        ],
    )["card_ids"][0]
    projection_id = service.materialize_projection(
        projection_type="current_state",
        projection_key="state:projection-correction",
        scope="employment",
        body={"state": "已辞职"},
        support_card_ids=[card_id],
    )
    service.correct_card(
        card_id,
        evidence("我没有辞职", key="projection-correction"),
        {"goal_context": "工作状态", "current_result": "仍在职；没有辞职"},
        expected_version=1,
    )
    retry = service.maintenance.run_ready(limit=1)
    assert retry[0]["state"] == "retry"
    assert not service.recall("工作状态 辞职")["layers"]["current_state"]
    service.maintenance.make_retries_ready()
    success = service.maintenance.run_ready(
        limit=1,
        projection_outputs={projection_id: {"state": "仍在职；没有辞职"}},
    )
    assert success[0]["state"] == "succeeded"
    context = service.recall("工作状态 辞职")
    assert context["layers"]["current_state"][0]["body"]["state"] == "仍在职；没有辞职"


def test_tenant_scope_is_enforced_by_service_and_rls(service):
    card_id = service.observe(
        evidence("A 租户私有内容", key="tenant-a"),
        semantics=[fact({"proposition": "A 租户私有内容", "scope": "A"}, key="fact:tenant-a")],
    )["card_ids"][0]
    snapshot = service.recall("A 租户私有内容")
    other = MemoryService(uuid4())
    other.ensure_tenant()
    with pytest.raises(NotFound):
        other.delete_card(card_id)
    with pytest.raises(NotFound):
        other.expand_snapshot(UUID(snapshot["snapshot"]["id"]), card_id)
    with tenant_transaction(other.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 0
    with connect() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE liveday0_app")
            assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 0


def test_snapshot_pins_versions_until_hard_invalidation(service):
    card_id = service.observe(
        evidence("快照事件 v1", key="snapshot-event"),
        semantics=[event({"goal_context": "快照", "current_result": "v1"}, key="event:snapshot")],
    )["card_ids"][0]
    first = service.recall("快照")
    first_id = UUID(first["snapshot"]["id"])
    service.add_event_delta(
        card_id,
        evidence("快照事件 v2", key="snapshot-delta"),
        {"current_result": "v2"},
        idempotency_key="snapshot-delta",
    )
    service.maintenance.make_pending_ready(job_type="event_rewrite")
    service.maintenance.run_ready(limit=1)
    pinned = service.expand_snapshot(first_id, card_id)
    assert pinned["version"] == 1
    assert pinned["body"]["current_result"] == "v1"
    fresh = service.recall("快照")
    assert fresh["layers"]["events"][0]["version"] == 2
    assert fresh["layers"]["events"][0]["body"]["current_result"] == "v2"
    service.correct_card(
        card_id,
        evidence("硬纠正", key="snapshot-hard-correction"),
        {"goal_context": "快照", "current_result": "corrected"},
        expected_version=2,
    )
    with pytest.raises(SnapshotInvalidated):
        service.expand_snapshot(first_id, card_id)


def test_soft_delta_limit_forces_coalesced_catchup_ready(service):
    card_id = service.observe(
        evidence("软上限事件", key="soft-limit-event"),
        semantics=[event({"goal_context": "软上限", "current_result": "初始"}, key="event:soft-limit")],
    )["card_ids"][0]
    for index in range(8):
        service.add_event_delta(
            card_id,
            evidence(f"软上限增量 {index}", key=f"soft-limit-evidence-{index}"),
            {"current_result": f"增量 {index}"},
            idempotency_key=f"soft-limit-delta-{index}",
        )
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM maintenance_jobs").fetchone()["n"] == 1
        assert conn.execute("SELECT available_at <= now() AS ready FROM maintenance_jobs").fetchone()["ready"] is True
    result = service.maintenance.run_ready(limit=1)
    assert result[0]["state"] == "succeeded"
    assert service.effective_event(card_id)["version"] == 2


def test_unsafe_overlay_is_caught_up_before_recall(service):
    card_id = service.observe(
        evidence("需要因果重排的事件", key="unsafe-event"),
        semantics=[
            event(
                {"goal_context": "因果重排", "current_result": "旧结果"},
                key="event:unsafe-overlay",
            )
        ],
    )["card_ids"][0]
    service.add_event_delta(
        card_id,
        evidence("纠正了因果关系", key="unsafe-delta-source"),
        {"current_result": "重排后的结果", "requires_restructure": True},
        idempotency_key="unsafe-delta",
    )
    context = service.recall("因果重排")
    assert context["layers"]["events"][0]["version"] == 2
    assert context["layers"]["events"][0]["body"]["current_result"] == "重排后的结果"
    assert context["layers"]["events"][0]["pending"] is False


def test_card_and_context_budgets_omit_whole_units_with_expansion(service):
    huge = "完整语义" * 500
    card_id = service.observe(
        evidence("超长事件", key="huge-event"),
        semantics=[
            event(
                {
                    "goal_context": huge,
                    "development": huge,
                    "current_result": huge,
                    "unfinished_future": huge,
                    "boundaries": huge,
                },
                key="event:huge",
            )
        ],
    )["card_ids"][0]
    context = service.recall(
        "超长事件 完整语义",
        options=RecallOptions(card_token_limit=40, context_token_limit=100),
    )
    assert not context["layers"]["events"]
    assert context["expansion_handles"] == [
        {
            "item_id": str(card_id),
            "type": "event",
            "reason": "whole_card_omitted_by_budget",
        }
    ]
    expanded = service.expand_snapshot(UUID(context["snapshot"]["id"]), card_id)
    assert expanded["body"]["current_result"] == huge
    assert "…" not in flatten_context(context)


def test_explicit_deletion_removes_content_from_sources_derivatives_and_cache(service):
    observed = service.observe(
        evidence("需要彻底删除的内容", key="delete-me"),
        trace={"observation": "敏感生活痕迹", "observation_boundary": "仅此来源"},
        semantics=[
            event(
                {"goal_context": "删除测试", "current_result": "敏感结论"},
                key="event:delete-me",
            )
        ],
    )
    card_id = observed["card_ids"][0]
    service.materialize_projection(
        projection_type="current_state",
        projection_key="state:delete-me",
        scope="deletion test",
        body={"state": "敏感派生内容"},
        support_card_ids=[card_id],
    )
    snapshot = service.recall("删除测试 敏感")
    service.delete_evidence(observed["evidence_id"])
    after = service.recall("删除测试 敏感")
    assert "敏感" not in flatten_context(after)
    with pytest.raises(SnapshotInvalidated):
        service.expand_snapshot(UUID(snapshot["snapshot"]["id"]), card_id)
    with tenant_transaction(service.tenant_id) as conn:
        source = conn.execute("SELECT * FROM evidence WHERE id=%s", (observed["evidence_id"],)).fetchone()
        assert source["status"] == "deleted"
        assert source["content"] is source["object_ref"] is None
        assert source["image_observation"] is source["sending_context"] is None
        assert source["idempotency_key"] is None
        deleted_card = conn.execute("SELECT canonical_key FROM semantic_cards WHERE id=%s", (card_id,)).fetchone()
        assert deleted_card["canonical_key"] == f"deleted:{card_id}"
        assert conn.execute(
            "SELECT bool_and(body = '{}'::jsonb) AS empty FROM semantic_card_versions WHERE card_id=%s",
            (card_id,),
        ).fetchone()["empty"] is True
        assert conn.execute("SELECT count(*) AS n FROM deletion_markers").fetchone()["n"] >= 2


def test_only_postgresql_and_relational_adjacency_are_used():
    with connect() as conn:
        extensions = {row["extname"] for row in conn.execute("SELECT extname FROM pg_extension")}
        assert "pgcrypto" in extensions
        assert conn.execute("SELECT to_regclass('public.relations') AS name").fetchone()["name"] == "relations"
        vector_available = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name='vector') AS value"
        ).fetchone()["value"]
        vector_installed = "vector" in extensions
        assert vector_installed is vector_available
        if vector_installed:
            assert conn.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM information_schema.columns
                  WHERE table_name='evidence' AND column_name='embedding'
                ) AS value
                """
            ).fetchone()["value"] is True


def test_migration_up_down_up_is_repeatable():
    assert [row["version"] for row in migration_status()] == [1]
    assert migrate_down(1) == [1]
    with connect() as conn:
        assert conn.execute("SELECT to_regclass('public.semantic_cards') AS name").fetchone()["name"] is None
    assert migrate_up() == [1]
    assert [row["version"] for row in migration_status()] == [1]
