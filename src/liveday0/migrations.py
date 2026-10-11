from __future__ import annotations

from pathlib import Path
from importlib.resources import files
import re

from liveday0.db import connect

MIGRATIONS_DIR = files("liveday0").joinpath("sql_migrations")
# Editable/source checkouts keep the single SQL source at the repository root.
# An installed distribution must use its own resources, never nearby SQL.
_source_root = Path(__file__).resolve().parents[2]
if (not MIGRATIONS_DIR.is_dir()
        and Path(__file__).resolve().parent == _source_root / "src" / "liveday0"
        and (_source_root / "pyproject.toml").is_file()):
    MIGRATIONS_DIR = _source_root / "migrations"


def _migration_sql() -> dict[int, dict[str, str]]:
    """Read a complete paired catalog before any database side effect."""
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError("migration SQL resources are missing; reinstall a complete LiveDay0 distribution")
    catalog: dict[int, dict[str, str]] = {}
    names: dict[int, str] = {}
    for path in MIGRATIONS_DIR.iterdir():
        if not path.name.endswith(".sql"):
            continue
        match = re.fullmatch(r"(\d{3})_(\w+)\.(up|down)\.sql", path.name)
        if not match or not path.is_file():
            raise RuntimeError("invalid migration SQL resource name")
        version, name, direction = int(match[1]), match[2], match[3]
        pair = catalog.setdefault(version, {})
        if direction in pair or (version in names and names[version] != name):
            raise RuntimeError("duplicate or mismatched migration SQL resources")
        names[version] = name
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise RuntimeError("empty migration SQL resource")
        pair[direction] = sql
    # Versions 001..005 belong to every current release. The floor also catches
    # removal of both files in the last pair; future additions must be gapless.
    if (not set(range(1, 6)).issubset(catalog)
            or set(catalog) != set(range(1, max(catalog, default=0) + 1))
            or any(set(pair) != {"up", "down"} for pair in catalog.values())):
        raise RuntimeError("incomplete migration SQL resources; reinstall a complete LiveDay0 distribution")
    return catalog


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
    catalog = _migration_sql()
    applied: list[int] = []
    with connect() as conn:
        _ensure_registry(conn)
        existing = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        if not existing.issubset(catalog):
            raise RuntimeError("database migration versions are newer than this distribution")
        for version in sorted(catalog):
            if version in existing:
                continue
            conn.execute(catalog[version]["up"])
            conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
            applied.append(version)
        conn.commit()
    return applied


def migrate_down(steps: int = 1) -> list[int]:
    reverted: list[int] = []
    if steps < 1:
        raise ValueError("steps must be at least 1")
    catalog = _migration_sql()
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
            if version not in catalog:
                raise RuntimeError("database migration versions are newer than this distribution")
            conn.execute(catalog[version]["down"])
            conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            reverted.append(version)
        conn.commit()
    return reverted


def migration_status() -> list[dict]:
    _migration_sql()
    with connect() as conn:
        _ensure_registry(conn)
        rows = conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()
        conn.commit()
    return rows
