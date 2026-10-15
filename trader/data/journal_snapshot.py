"""A consistent copy of the command journal, written by the process that holds it open (#189).

trader_service keeps the journal open for its whole life, so no other process can attach it for a backup.
The trader copies it into ``<journal>.snapshot.duckdb``; the name ends in ``.duckdb``, so the scheduler's
in-DB backup and ``./docker.sh -B`` both pick it up. The copy is written to a ``.partial`` file first and
renamed, so a reader never sees a half-written snapshot.

Restore: stop the stack, copy the snapshot over the journal file and remove the journal's ``.wal``.
"""
from __future__ import annotations

import os

from trader.data.duckdb_store import DuckDBConnection

SNAPSHOT_SUFFIX = '.snapshot.duckdb'


def _sql_literal(text: str) -> str:
    return text.replace("'", "''")


def snapshot_path(journal_path: str) -> str:
    return f'{journal_path}{SNAPSHOT_SUFFIX}'


def write_journal_snapshot(journal_path: str) -> int:
    """Copy the journal to its snapshot file; returns the snapshot size in bytes."""
    target = snapshot_path(journal_path)
    partial = f'{target}.partial'
    for leftover in (partial, f'{partial}.wal'):
        if os.path.exists(leftover):
            os.remove(leftover)

    def copy(conn) -> None:
        source = conn.execute('SELECT current_database()').fetchone()[0]
        conn.execute(f"ATTACH '{_sql_literal(partial)}' AS journal_snapshot")   # ATTACH takes no parameters
        try:
            conn.execute(f'COPY FROM DATABASE "{source}" TO journal_snapshot')
        finally:
            conn.execute('DETACH journal_snapshot')

    DuckDBConnection.get_instance(journal_path).execute_atomic(copy)
    os.replace(partial, target)
    return os.path.getsize(target)
