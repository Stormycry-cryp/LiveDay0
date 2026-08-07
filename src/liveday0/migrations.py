from __future__ import annotations

from pathlib import Path

from liveday0.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _ensure_registry(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version integer PRIMARY KEY,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def migrate_up() -> list[int]:
    applied: list[int] = []
    with connect() as conn:
        _ensure_registry(conn)
        existing = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.up.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in existing:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
            applied.append(version)
        conn.commit()
    return applied


def migrate_down(steps: int = 1) -> list[int]:
    reverted: list[int] = []
    if steps < 1:
        raise ValueError("steps must be at least 1")
    with connect() as conn:
        _ensure_registry(conn)
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT %s",
                (steps,),
            )
        ]
        for version in versions:
            matches = list(MIGRATIONS_DIR.glob(f"{version:03d}_*.down.sql"))
            if len(matches) != 1:
                raise RuntimeError(f"expected one down migration for {version}, found {len(matches)}")
            conn.execute(matches[0].read_text())
            conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            reverted.append(version)
        conn.commit()
    return reverted


def migration_status() -> list[dict]:
    with connect() as conn:
        _ensure_registry(conn)
        rows = conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()
        conn.commit()
    return rows
