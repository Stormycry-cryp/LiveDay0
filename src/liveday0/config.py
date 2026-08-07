from __future__ import annotations

import os


def database_url() -> str:
    value = os.environ.get("LIVEDAY0_DATABASE_URL")
    if not value:
        raise RuntimeError("LIVEDAY0_DATABASE_URL is required")
    return value


def event_quiet_seconds() -> int:
    return int(os.environ.get("LIVEDAY0_EVENT_QUIET_SECONDS", "60"))


def event_delta_soft_limit() -> int:
    return int(os.environ.get("LIVEDAY0_EVENT_DELTA_SOFT_LIMIT", "8"))
