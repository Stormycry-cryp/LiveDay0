from __future__ import annotations

from datetime import datetime, timezone

from liveday0.types import EvidenceInput, SemanticInput


def evidence(
    content: str,
    *,
    key: str | None = None,
    modality: str = "text",
    object_ref: str | None = None,
    image_observation: str | None = None,
) -> EvidenceInput:
    return EvidenceInput(
        modality=modality,
        source_kind="user_message" if modality == "text" else "user_image",
        content=content,
        object_ref=object_ref,
        occurred_at=datetime.now(timezone.utc),
        image_observation=image_observation,
        idempotency_key=key,
    )


def event(body: dict, *, key: str | None = None, state: str = "confirmed") -> SemanticInput:
    return SemanticInput("event", body, epistemic_state=state, canonical_key=key)


def fact(body: dict, *, key: str | None = None) -> SemanticInput:
    return SemanticInput("fact", body, canonical_key=key)


def future(body: dict, *, key: str | None = None) -> SemanticInput:
    return SemanticInput("prospective", body, canonical_key=key)


def flatten_context(context: dict) -> str:
    import json

    return json.dumps(context, ensure_ascii=False)
