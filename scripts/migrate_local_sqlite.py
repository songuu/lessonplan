"""Copy a stopped local SQLite app to an EMPTY local lesson_assistant PostgreSQL database.

Run Alembic first. The original SQLite file and file storage are retained.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import Base, Run  # noqa: E402


def parent_first(table, rows):
    """Self-referencing version history must be inserted before its children."""
    dependencies = [(fk.parent.name, fk.column.name) for fk in table.foreign_keys if fk.column.table is table]
    if not dependencies:
        return rows
    result, pending, seen = [], list(rows), {parent: set() for _, parent in dependencies}
    while pending:
        ready = [r for r in pending if all(r[child] is None or r[child] in seen[parent]
                                          for child, parent in dependencies)]
        if not ready:
            raise ValueError(f"Unresolved version ancestry in {table.name}")
        for row in ready:
            result.append(row)
            for _, parent in dependencies:
                seen[parent].add(row[parent])
        ready_ids = {id(row) for row in ready}
        pending = [r for r in pending if id(r) not in ready_ids]
    return result


async def copy_database(source, target):
    """Refuse active jobs/nonempty targets; copy and read back within one transaction."""
    counts = {}
    async with source.connect() as old, target.begin() as new:
        if await old.scalar(select(func.count()).select_from(Run).where(Run.status.in_(["queued", "running"]))):
            raise ValueError("Source has active generation runs; stop admission and let them finish first")
        for table in Base.metadata.sorted_tables:
            if await new.scalar(select(func.count()).select_from(table)):
                raise ValueError(f"Target is not empty: {table.name}")
        for table in Base.metadata.sorted_tables:
            query = select(table).order_by(*table.primary_key.columns)
            rows = [dict(r) for r in (await old.execute(query)).mappings()]
            ordered = parent_first(table, rows)
            for offset in range(0, len(ordered), 500):
                await new.execute(table.insert(), ordered[offset:offset + 500])
            copied = [dict(r) for r in (await new.execute(query)).mappings()]
            if copied != rows:
                raise ValueError(f"Readback mismatch: {table.name}; transaction rolled back")
            counts[table.name] = len(rows)
    return counts


def validate_target(value):
    url = make_url(value)
    # asyncpg query arguments can override the authority/database after URL validation.
    if (url.drivername != "postgresql+asyncpg" or url.host not in {"localhost", "127.0.0.1", "::1"}
            or url.database != "lesson_assistant" or url.query):
        raise ValueError("Target must be the local dedicated PostgreSQL lesson_assistant database")
    return url


async def main(source_path):
    source_path = source_path.resolve(strict=True)
    if not source_path.is_relative_to(ROOT):
        raise ValueError("Source SQLite file must belong to this workspace")
    url = validate_target(get_settings().database_url)
    backup = ROOT / "var/backups" / f"sqlite-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    # SQLite's backup API includes committed WAL pages, unlike copying the .db file alone.
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as old, sqlite3.connect(backup) as new:
        old.backup(new)
    source = create_async_engine(f"sqlite+aiosqlite:///{backup.as_posix()}")
    target = create_async_engine(url)
    try:
        counts = await copy_database(source, target)
        print(json.dumps({"backup": str(backup), "verified_table_counts": counts}, ensure_ascii=False))
    finally:
        await source.dispose()
        await target.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "lesson_assistant.db")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.source))
    except Exception as exc:
        # Driver exceptions can contain connection credentials or teacher records.
        print(f"Migration failed ({type(exc).__name__}); source and backup retained", file=sys.stderr)
        raise SystemExit(1) from None
