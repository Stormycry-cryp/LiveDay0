from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

CardType = Literal["event", "fact", "prospective"]
ProjectionType = Literal["current_state", "life_thread", "relationship"]
Modality = Literal["text", "image", "object"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EvidenceInput:
    modality: Modality
    source_kind: str
    content: str | None = None
    object_ref: str | None = None
    occurred_at: datetime = field(default_factory=utcnow)
    image_observation: str | None = None
    sending_context: str | None = None
    model_interpretation: str | None = None
    embedding: tuple[float, ...] | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SemanticInput:
    card_type: CardType
    body: dict[str, Any]
    lifecycle: str = "active"
    epistemic_state: str = "confirmed"
    canonical_key: str | None = None
    valid_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class RecallOptions:
    card_token_limit: int = 180
    context_token_limit: int = 1600
    candidate_limit: int = 48
    relation_limit: int = 24
    final_limit: int = 12
    per_relation_family_limit: int = 6
    timeout_ms: int = 800
    simulate_vector_timeout: bool = False
    query_embedding: tuple[float, ...] | None = None
    as_of: datetime | None = None
