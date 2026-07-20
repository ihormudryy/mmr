"""Versioned, idempotent DDL migrations for a dedicated DuckDB file.

``SchemaMigrator`` records every applied migration by integer version in a
``schema_migrations`` ledger so that re-running the same migration set (e.g.
on every service startup) is a safe no-op — statements are executed exactly
once per version, never replayed.

Version ranges are owned per downstream plan (binding, see the M1-F1
briefing / plan index): ``[M1-F1]`` (this plan) owns **1-9**, ``[M1-F2]``
owns **10-19**, ``[M1-F3]`` owns **20-29**. Crossing ranges collides two
plans' migrations against the same version number, causing one to be
silently skipped (already-applied) when it never actually ran, or to
double-apply DDL that assumes it is fresh. Callers must stay inside their
assigned range.

P1 command-plane safety continues the F3-owned range: migration 24 is the
durable automation breaker and incident ledger. P3 deterministic automation
owns **30-39** (protective order sagas begin at migration 30). P4 paper/live
canary promotion owns **40-49** (durable evidence windows and the promotion
stage machine begin at migration 40).

``SchemaMigrator`` is deliberately storage-agnostic about *which* file it
targets — it operates on whatever ``DuckDBConnection`` it is constructed
with. For M1-F1 that is the dedicated ``journal_duckdb_path`` file (see
``trader/data/domain_journal.py`` for why that file is separate from the
shared ``mmr.duckdb``), but the class itself has no opinion about that.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from trader.data.duckdb_store import DuckDBConnection

_LEDGER_DDL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name VARCHAR NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL
    )
"""


class SchemaMigrator:
    """Applies versioned DDL against ``db`` and records it in
    ``schema_migrations``.

    FROZEN CONTRACT: ``apply(version: int, name: str, statements: Sequence[str])``
    — downstream plans (``[M1-F2]`` at version 10, ``[M1-F3]`` at version 20)
    call this exact 3-positional-argument signature. A single-string-DDL
    variant, a nameless variant, or a reordered signature breaks both.
    """

    def __init__(self, db: DuckDBConnection):
        self.db = db

    def applied_versions(self) -> set[int]:
        """Return the set of migration versions already recorded."""
        self._ensure_ledger()
        rows = self.db.execute("SELECT version FROM schema_migrations", fetch="all")
        return {row[0] for row in rows}

    def apply(self, version: int, name: str, statements: Sequence[str]) -> bool:
        """Run ``statements`` under ``version`` exactly once.

        Idempotent: if ``version`` is already recorded in
        ``schema_migrations``, this is a no-op that does NOT re-execute
        ``statements`` (running a ``CREATE TABLE`` without ``IF NOT
        EXISTS`` twice would otherwise raise a ``CatalogException``, and
        more importantly a destructive statement must never replay).

        Returns ``True`` if this call newly applied the migration,
        ``False`` if it was already applied (and therefore skipped).
        """
        self._ensure_ledger()

        def _tx(conn):
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", [version]
            ).fetchone()
            if already is not None:
                return False
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                [version, name, datetime.now(timezone.utc)],
            )
            return True

        return self.db.transaction(_tx)

    def _ensure_ledger(self) -> None:
        # Cheap idempotent bootstrap so applied_versions()/apply() can be
        # called before any migration has ever run against this file.
        self.db.execute(_LEDGER_DDL)
