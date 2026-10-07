"""ai.duckdb access. DuckDB only through DuckDBConnection; blocking work runs off the event loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from trader.ai.clock import Clock
from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator


def to_utc(value: datetime) -> datetime:
    """DuckDB returns TIMESTAMPTZ in the process time zone. Normalise before comparing."""
    return value.astimezone(timezone.utc)


class AiStore:
    """One writer at a time: all instances of one path share DuckDBConnection's lock."""

    def __init__(self, path: str | Path, *, clock: Clock):
        self.path = str(Path(path).expanduser())
        self.db = DuckDBConnection.get_instance(self.path)
        self.clock = clock

    def migrate(self, migrations: Sequence[Migration] = FOUNDATION_MIGRATIONS) -> list[int]:
        """Apply each version once, in order. Plans 5 and 6 pass their own ranges (10-19, 20-29)."""
        versions = [m.version for m in migrations]
        if versions != sorted(set(versions)) or any(type(v) is not int or v < 1 for v in versions):
            raise ValueError("migration versions must be unique, ascending positive integers")
        migrator = SchemaMigrator(self.db)
        return [m.version for m in migrations if migrator.apply(m.version, m.name, m.statements)]

    def transaction(self, work: Callable[[Any], Any]) -> Any:
        return self.db.transaction(work)

    async def atransaction(self, work: Callable[[Any], Any]) -> Any:
        return await asyncio.to_thread(self.db.transaction, work)

    async def aquery(self, sql: str, params: Optional[list] = None, *, fetch: str = "all") -> Any:
        return await asyncio.to_thread(self.db.execute, sql, params, fetch)
