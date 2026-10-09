"""Stable encoding for internal read contracts and explicit-intent fingerprints."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID


def _encode(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")
    raise TypeError(f"unsupported contract value: {type(value).__name__}")


def canonical_json(value) -> str:
    return json.dumps(value, default=_encode, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(canonical_input: str) -> str:
    return hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()


def source_identity_digest(tenant_id: UUID, key: str) -> str:
    return fingerprint(canonical_json(["liveday0:evidence-idempotency:v1", tenant_id, key]))
