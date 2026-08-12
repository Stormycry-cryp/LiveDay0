from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from liveday0.core import MemoryService
from liveday0.types import EvidenceInput, RecallOptions, SemanticInput
from benchmarks.public_human_recall_v2.common import audit_records, load_jsonl
from benchmarks.public_human_recall_v2_benchmark import audit_cases, load_cases


ROOT = Path(__file__).resolve().parents[1]


def context_ids(context: dict) -> set[str]:
    ids = {
        str(item["id"])
        for layer in context["layers"].values()
        for item in layer
        if item.get("id")
    }
    ids.update(handle["item_id"] for handle in context["expansion_handles"])
    return ids


def test_v2_dataset_and_case_contracts() -> None:
    records = load_jsonl()
    cases = load_cases()
    assert audit_records(records)["passed"]
    assert audit_cases(records, cases)["passed"]
    assert len(records) >= 10_000
    assert len(cases) == 360


def test_v2_live_source_audit_was_frozen() -> None:
    audit = json.loads(
        (ROOT / "benchmarks/public_human_recall_v2/source_audit.json").read_text(encoding="utf-8")
    )
    assert audit["passed"]
    assert audit["collection"]["dataset_audit"]["passed"]
    stackexchange = next(item for item in audit["checks"] if item["family"] == "stackexchange_v1_frozen")
    assert not stackexchange["collection_enabled"]


def test_fts_hit_is_not_lost_behind_recent_fallback_cards() -> None:
    service = MemoryService(uuid4())
    service.ensure_tenant()
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    target = service.observe(
        EvidenceInput(
            modality="text", source_kind="test", content="rare continuity marigold promise",
            occurred_at=old, idempotency_key="target",
        ),
        semantics=[
            SemanticInput(
                "fact", {"proposition": "rare continuity marigold promise", "scope": "test"},
                canonical_key="target", valid_at=old,
            )
        ],
    )["card_ids"][0]
    for index in range(70):
        service.observe(
            EvidenceInput(
                modality="text", source_kind="test", content=f"unrelated recent noise {index}",
                occurred_at=old + timedelta(days=index + 1), idempotency_key=f"noise:{index}",
            ),
            semantics=[
                SemanticInput(
                    "fact", {"proposition": f"unrelated recent noise {index}", "scope": "test"},
                    canonical_key=f"noise:{index}", valid_at=old + timedelta(days=index + 1),
                )
            ],
        )
    context = service.recall(
        "rare continuity marigold promise",
        options=RecallOptions(candidate_limit=48, final_limit=12),
    )
    assert str(target) in context_ids(context)


def test_explicitly_excluded_applicability_is_not_recalled() -> None:
    service = MemoryService(uuid4())
    service.ensure_tenant()
    observed = service.observe(
        EvidenceInput(
            modality="text", source_kind="test", content="marigold illustrative example",
            idempotency_key="excluded",
        ),
        semantics=[
            SemanticInput(
                "fact",
                {
                    "proposition": "marigold illustrative example",
                    "scope": "not personal",
                    "applicability": "excluded_from_personal_continuity",
                },
                canonical_key="excluded",
            )
        ],
    )
    context = service.recall("marigold illustrative example")
    assert str(observed["card_ids"][0]) not in context_ids(context)
