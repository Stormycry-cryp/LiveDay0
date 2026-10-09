from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from liveday0.config import database_url
from liveday0.exceptions import NotFound


def connect(*, autocommit: bool = False) -> Connection:
    return psycopg.connect(database_url(), autocommit=autocommit, row_factory=dict_row)


@contextmanager
def tenant_transaction(
    tenant_id: UUID,
    *,
    mode: Literal["write", "read", "bootstrap"] = "write",
) -> Iterator[Connection]:
    """Take the tenant gate before every business read/lock, through commit.

    Readers may only append their new recall snapshot. They must not upgrade
    the gate or call maintenance/nested service transactions. Writers always
    take this gate before locking jobs, sources, or targets. Bootstrap is only
    for creating the tenant itself. Migrations require offline exclusive use.
    """
    if mode not in {"write", "read", "bootstrap"}:
        raise ValueError("unknown tenant transaction mode")
    with connect() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute("SET LOCAL ROLE liveday0_app")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            if mode != "bootstrap":
                lock = "SHARE" if mode == "read" else "UPDATE"
                row = conn.execute(
                    f"SELECT id FROM tenants WHERE id=%s FOR {lock}", (tenant_id,)
                ).fetchone()
                if row is None:
                    raise NotFound("tenant not found")
            yield conn
