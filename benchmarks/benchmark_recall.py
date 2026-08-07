from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.types import EvidenceInput, RecallOptions, SemanticInput


def evidence(content: str, *, key: str) -> EvidenceInput:
    return EvidenceInput(
        modality="text",
        source_kind="synthetic_benchmark",
        content=content,
        occurred_at=datetime.now(timezone.utc),
        idempotency_key=key,
    )


def event(body: dict, *, key: str) -> SemanticInput:
    return SemanticInput("event", body, canonical_key=key)


def fact(body: dict, *, key: str) -> SemanticInput:
    return SemanticInput("fact", body, canonical_key=key)


def future(body: dict, *, key: str) -> SemanticInput:
    return SemanticInput("prospective", body, canonical_key=key)


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return ordered[index]


def main() -> None:
    tenant_id = uuid4()
    service = MemoryService(tenant_id)
    service.ensure_tenant()
    try:
        for index in range(120):
            topic = "上海生活变化" if index % 3 == 0 else f"普通生活片段{index % 12}"
            service.observe(
                evidence(f"{topic} 的来源记录 {index}", key=f"bench-event-{index}"),
                semantics=[
                    event(
                        {
                            "goal_context": topic,
                            "development": f"有来源的局部变化 {index}",
                            "current_result": f"当前结果 {index}",
                            "unfinished_future": "等待后续" if index % 4 == 0 else "无",
                            "boundaries": "仅限当前合成基准样本",
                        },
                        key=f"event:bench:{index}",
                    )
                ],
            )
        for index in range(20):
            service.observe(
                evidence(f"饮食明确事实 {index}", key=f"bench-fact-{index}"),
                semantics=[
                    fact(
                        {"proposition": f"饮食明确事实 {index}", "scope": "合成基准"},
                        key=f"fact:bench:{index}",
                    )
                ],
            )
        for index in range(20):
            service.observe(
                evidence(f"上海开放事项 {index}", key=f"bench-future-{index}"),
                semantics=[
                    future(
                        {"item": f"上海开放事项 {index}", "status": "open", "expected_window": "下月"},
                        key=f"future:bench:{index}",
                    )
                ],
            )

        cases = [
            RecallOptions(
                card_token_limit=120,
                context_token_limit=800,
                candidate_limit=24,
                relation_limit=12,
                final_limit=6,
            ),
            RecallOptions(),
            RecallOptions(
                card_token_limit=240,
                context_token_limit=2400,
                candidate_limit=96,
                relation_limit=48,
                final_limit=20,
            ),
        ]
        recall_results = []
        for options in cases:
            for _ in range(3):
                service.recall("上海生活变化 下月开放事项", options=options)
            elapsed_ms: list[float] = []
            last_context = None
            for _ in range(30):
                started = time.perf_counter()
                last_context = service.recall("上海生活变化 下月开放事项", options=options)
                elapsed_ms.append((time.perf_counter() - started) * 1000)
            assert last_context is not None
            cards = sum(len(items) for items in last_context["layers"].values())
            recall_results.append(
                {
                    "parameters": {
                        "card_tokens": options.card_token_limit,
                        "context_tokens": options.context_token_limit,
                        "candidate_relation_final": [
                            options.candidate_limit,
                            options.relation_limit,
                            options.final_limit,
                        ],
                    },
                    "latency_ms": {
                        "median": round(statistics.median(elapsed_ms), 2),
                        "p95": round(percentile(elapsed_ms, 0.95), 2),
                        "max": round(max(elapsed_ms), 2),
                    },
                    "cards_in_context": cards,
                    "estimated_tokens": last_context["budget"]["estimated_tokens"],
                    "omitted_handles": len(last_context["expansion_handles"]),
                    "under_800ms": max(elapsed_ms) < 800,
                }
            )

        overlay_results = []
        for count in (1, 4, 8, 16):
            event_id = service.observe(
                evidence(f"增量基准事件 {count}", key=f"overlay-event-{count}"),
                semantics=[
                    event(
                        {"goal_context": "增量合成基准", "current_result": "初始"},
                        key=f"event:overlay:{count}",
                    )
                ],
            )["card_ids"][0]
            for index in range(count):
                service.add_event_delta(
                    event_id,
                    evidence(f"增量 {count}-{index}", key=f"overlay-evidence-{count}-{index}"),
                    {"current_result": f"增量结果 {index}"},
                    idempotency_key=f"overlay-delta-{count}-{index}",
                )
            timings = []
            for _ in range(50):
                started = time.perf_counter()
                service.effective_event(event_id)
                timings.append((time.perf_counter() - started) * 1000)
            overlay_results.append(
                {
                    "pending_deltas": count,
                    "latency_ms": {
                        "median": round(statistics.median(timings), 2),
                        "p95": round(percentile(timings, 0.95), 2),
                    },
                }
            )

        with connect() as conn:
            postgres_version = conn.execute("SHOW server_version").fetchone()["server_version"]
            vector_available = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS value"
            ).fetchone()["value"]
        output = {
            "environment": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "postgresql": postgres_version,
                "pgvector_installed": vector_available,
            },
            "sample": {
                "tenant_count": 1,
                "canonical_cards": 160,
                "recall_runs_per_case": 30,
                "overlay_runs_per_case": 50,
            },
            "recall": recall_results,
            "pending_delta_overlay": overlay_results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM tenants WHERE id=%s", (tenant_id,))
            conn.commit()


if __name__ == "__main__":
    main()
