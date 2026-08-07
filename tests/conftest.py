from __future__ import annotations

from uuid import uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.migrations import migrate_up


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    migrate_up()


@pytest.fixture(autouse=True)
def clean_database(migrated_database):
    with connect() as conn:
        conn.execute(
            """
            TRUNCATE TABLE
              deletion_markers, recall_snapshots, maintenance_jobs,
              projection_supports, projection_versions, projections,
              relations, mention_candidates, mentions, event_deltas,
              card_sources, semantic_card_versions, semantic_cards,
              life_traces, evidence, tenants
            CASCADE
            """
        )
        conn.commit()


@pytest.fixture
def service() -> MemoryService:
    value = MemoryService(uuid4())
    value.ensure_tenant()
    return value
