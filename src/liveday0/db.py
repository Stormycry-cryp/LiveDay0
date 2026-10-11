from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Literal
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from liveday0.config import database_url
from liveday0.exceptions import NotFound, RecallDeadlineExceeded, RecallDeadlineUnavailable
from liveday0.recall_limits import current_deadline


def connect(*, autocommit: bool = False) -> Connection:
    deadline = current_deadline()
    if deadline is None:
        return psycopg.connect(database_url(), autocommit=autocommit, row_factory=dict_row)
    # Public libpq polling primitives allow a fractional, shared connection budget.
    # Avoid synchronous DNS and hidden multi-host retries on the bounded recall path.
    from ipaddress import ip_address
    from psycopg import generators, waiting
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(database_url())
    address = params.get('hostaddr') or params.get('host', '')
    if not address or ',' in address or ',' in params.get('host', '') or ',' in params.get('port', ''):
        raise RecallDeadlineUnavailable('explicit_single_database_address_required')
    if not address.startswith('/'):
        try: ip_address(address)
        except ValueError:
            raise RecallDeadlineUnavailable('resolved_database_address_required') from None
        params['hostaddr'] = address
    try:
        pgconn = waiting.wait_conn(generators.connect(make_conninfo(**params),
            timeout=deadline.remaining_ms('connect') / 1000), interval=0.01)
    except psycopg.errors.ConnectionTimeout as error:
        if error.pgconn is not None:
            error.pgconn.finish()
        raise RecallDeadlineExceeded('connect') from None
    conn = psycopg.Connection(pgconn, row_factory=dict_row)
    conn.autocommit = autocommit
    try: deadline.check('connect')
    except RecallDeadlineExceeded:
        conn.close()
        raise
    return _DeadlineConnection(conn, deadline)


class _DeadlineConnection:
    """Keep the existing connection/transaction protocol; only budget SQL calls."""
    def __init__(self, conn, deadline):
        self.conn, self.deadline = conn, deadline

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, *args):
        return self.conn.__exit__(*args)

    def execute(self, query, params=None, *args, **kwargs):
        remaining = self.deadline.remaining_ms('sql')
        try:
            # SET (not SELECT set_config) preserves SET TRANSACTION ordering.
            self.conn.execute(f'SET statement_timeout={remaining}')
            self.deadline.check('sql')
            result = self.conn.execute(query, params, *args, **kwargs)
        except psycopg.errors.QueryCanceled:
            raise RecallDeadlineExceeded('sql') from None
        self.deadline.check('sql')
        return result


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
