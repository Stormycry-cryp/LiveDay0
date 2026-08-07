from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from liveday0.config import database_url


def connect(*, autocommit: bool = False) -> Connection:
    return psycopg.connect(database_url(), autocommit=autocommit, row_factory=dict_row)


@contextmanager
def tenant_transaction(
    tenant_id: UUID,
    *,
    isolation_level: str = "READ COMMITTED",
) -> Iterator[Connection]:
    with connect() as conn:
        with conn.transaction():
            conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation_level}")
            conn.execute("SET LOCAL ROLE liveday0_app")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            yield conn
