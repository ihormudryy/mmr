"""Durable domain-event journal with atomic materialized writes.

[M1-F1] Task 3 — the linchpin: every downstream broker/command producer
(``[M1-F2]``, ``[M1-F3]``) and the fenced snapshot / long-poll feed
(Tasks 4-5 of this plan) build on ``DomainJournal.mutate``.

Connection topology (binding pre-flight resolution, BLOCKER-1)
----------------------------------------------------------------
The journal, its snapshot checkpoints, and every materialized-state table
live in a DEDICATED DuckDB file (``journal_duckdb_path``, see
``trader/config.py`` / ``config_defaults/trader.yaml``) — never the shared
``mmr.duckdb``. A persistent connection held open on the shared file would
hold its cross-process lock and make every CLI ``connect()`` fail
(verified against DuckDB 1.4.4). Because ``journal_duckdb_path`` is opened
by ``trader_service`` alone, nothing else ever contends for it.

Within one process, ``DomainJournal`` holds exactly ONE persistent
``duckdb.connect()`` instance for its dedicated file (``self._shared_conn``)
and hands callers a fresh ``.cursor()`` per unit of work via
``connect()``. This was verified empirically (DuckDB 1.4.4, same process):
cursors off one ``duckdb.connect()`` instance see correctly isolated MVCC
snapshots (a cursor does NOT see another cursor's uncommitted, still-open
transaction), and a second, wholly independent ``duckdb.connect()`` call to
the SAME path from the SAME process (e.g. ``SchemaMigrator``'s
``DuckDBConnection``-based transient open/close calls used by
``migrate()``) coexists safely with this persistent connection — DuckDB
routes same-process connections to the same path to one shared in-process
database instance. There is no cross-process contention here because the
entire point of the dedicated file is that no other process opens it.

``mutate()`` owns its own transaction lifecycle end-to-end — an explicit
``BEGIN TRANSACTION`` / ``COMMIT`` / ``ROLLBACK`` issued directly on the
supplied ``conn``. This is a SEPARATE path from
``DuckDBConnection.transaction()``/``execute_atomic()`` (Task 1), which
continue to serve the ordinary trader-DB (``mmr.duckdb``) path only.
Callers must supply a ``conn`` that is NOT already inside an open
transaction — DuckDB rejects a nested ``BEGIN``.

Revision source (binding, RA-6)
--------------------------------
``mutate()`` reads the entity's current ``entity_revision`` from the
durable ``domain_materialized_entities`` ledger that THIS module owns and
maintains on every call — never ``MAX(entity_revision)`` over the journal,
which is compaction-trimmed (Task 6) and would go stale/NULL, causing a
``UNIQUE(entity_type, entity_id, entity_revision)`` collision. The
caller-supplied ``write_materialized(conn, entity_revision)`` callback is
free to ALSO persist its own richer, entity-specific table (e.g. a future
``broker_positions`` row with real query-able columns) stamped with that
exact same revision — that table is entirely decoupled from, and
irrelevant to, this module's own revision bookkeeping ledger.

Idempotency (binding, RA-7)
----------------------------
Idempotent retry is a ``SELECT`` by ``event_id`` inside the write
transaction: present → compare canonical fields → return the existing
``DomainEvent`` unchanged when every field matches, or raise
``EventIdentityConflict`` (without inserting) when any field differs;
absent → insert. A ``UNIQUE(entity_type, entity_id, entity_revision)``
violation is a hard fail-loud error and is never retried.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

import duckdb

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.events import DomainEvent, DomainMutation

WriteMaterialized = Callable[[duckdb.DuckDBPyConnection, int], None]


class EventIdentityConflict(Exception):
    """A retried ``event_id`` was presented with different canonical
    fields than the event already recorded under that id.

    This is a producer bug (an event_id was reused for two logically
    distinct events) and is never retried — the caller must be told
    loudly rather than silently coerced onto the earlier event.
    """

    def __init__(self, event_id: str, message: Optional[str] = None):
        self.event_id = event_id
        super().__init__(
            message or f"event_id {event_id!r} already recorded with different canonical fields"
        )


# Journal row columns, in a fixed order reused by every SELECT/INSERT so
# `dict(zip(_JOURNAL_COLUMNS, row))` reconstructs a row unambiguously.
_JOURNAL_COLUMNS = (
    "source_cursor", "event_id", "entity_revision", "event_type",
    "entity_type", "entity_id", "operation", "account_id", "source",
    "source_timestamp", "received_timestamp", "correlation_id", "payload",
)

_JOURNAL_DDL = (
    "CREATE SEQUENCE IF NOT EXISTS domain_event_cursor_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS domain_event_journal (
        source_cursor BIGINT PRIMARY KEY DEFAULT nextval('domain_event_cursor_seq'),
        event_id VARCHAR NOT NULL UNIQUE,
        entity_revision BIGINT NOT NULL,
        event_type VARCHAR NOT NULL,
        entity_type VARCHAR NOT NULL,
        entity_id VARCHAR NOT NULL,
        operation VARCHAR NOT NULL CHECK (operation IN ('upsert', 'delete')),
        account_id VARCHAR,
        source VARCHAR NOT NULL,
        source_timestamp TIMESTAMPTZ NOT NULL,
        received_timestamp TIMESTAMPTZ NOT NULL,
        correlation_id VARCHAR,
        payload JSON,
        UNIQUE(entity_type, entity_id, entity_revision)
    )
    """,
)

# Snapshot checkpoints (Task 4 reads broker_generation from here, defaulting
# to 0 when absent per BLOCKER-2; Task 6 writes rows here on compaction).
# The table is created now so the schema is stable across F1 tasks; nothing
# in Task 3 writes to it yet.
_CHECKPOINTS_DDL = (
    "CREATE SEQUENCE IF NOT EXISTS domain_snapshot_checkpoint_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS domain_snapshot_checkpoints (
        checkpoint_id BIGINT PRIMARY KEY DEFAULT nextval('domain_snapshot_checkpoint_seq'),
        created_at TIMESTAMPTZ NOT NULL,
        newest_cursor BIGINT NOT NULL,
        oldest_retained_cursor BIGINT NOT NULL,
        broker_generation BIGINT NOT NULL DEFAULT 0
    )
    """,
)

# The generic materialized-entity ledger (RA-6). One row per (entity_type,
# entity_id) carrying its current entity_revision — this is "the durable
# materialized row" mutate() reads from, valid for ANY entity type without
# per-type schema knowledge. Callers may ALSO maintain their own richer,
# entity-specific tables via write_materialized; those are independent of
# this bookkeeping ledger.
_MATERIALIZED_DDL = (
    """
    CREATE TABLE IF NOT EXISTS domain_materialized_entities (
        entity_type VARCHAR NOT NULL,
        entity_id VARCHAR NOT NULL,
        account_id VARCHAR,
        entity_revision BIGINT NOT NULL,
        deleted BOOLEAN NOT NULL DEFAULT FALSE,
        payload JSON,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (entity_type, entity_id)
    )
    """,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # DuckDB's Python driver returns TIMESTAMPTZ values normalized to the
    # local system timezone, not UTC (verified against 1.4.4) -- the
    # instant is correct, only the displayed tz differs. Normalize so every
    # comparison/consumer sees UTC consistently (RA-10).
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_payload(payload: Optional[dict]) -> Optional[str]:
    # Canonical JSON (sorted keys) so payload equality is compared by
    # content, not by incidental key order or re-parsed float formatting
    # (RA-10).
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, default=str)


class DomainJournal:
    """The durable, cursor-ordered domain-event journal.

    Constructor takes only ``db`` (a ``DuckDBConnection`` pointing at the
    dedicated ``journal_duckdb_path`` file) — no extra required args, so
    ``DomainJournal(db)`` is stable for every downstream fixture.
    """

    def __init__(self, db: DuckDBConnection):
        self.db = db
        # One persistent, this-process-shared connection to the dedicated
        # file (BLOCKER-1). Writers obtain their own cursor via connect()
        # rather than sharing this object directly across threads.
        self._shared_conn = duckdb.connect(db.db_path)
        # Defensive, in-process serialization of mutate()'s critical
        # section. Not required for correctness under the intended
        # single-writer-thread topology (see [M1-F2] Architecture: all
        # broker writes funnel through one dedicated writer thread), but
        # cheap insurance against two Python threads independently racing
        # DuckDB's single-active-write-transaction model.
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Connection access
    # ------------------------------------------------------------------ #

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Return a fresh cursor off the shared persistent connection.

        Each call is an independent logical session (its own transaction
        context) against the same in-process database instance -- this is
        the "writers use .cursor() off that instance" half of BLOCKER-1's
        resolution. The eventual Task 5 long-poll reader uses the same
        entry point.
        """
        return self._shared_conn.cursor()

    def close(self) -> None:
        """Close the shared persistent connection. Not required for
        correctness in tests (each test uses its own tmp file), provided
        for hygiene in long-lived processes."""
        self._shared_conn.close()

    # ------------------------------------------------------------------ #
    # Schema bootstrap
    # ------------------------------------------------------------------ #

    def migrate(self, migrator: SchemaMigrator) -> None:
        """Create schema_migrations (via the migrator), domain_event_journal,
        domain_snapshot_checkpoints, and domain_materialized_entities.
        Versions 1-3, well inside this plan's owned range 1-9. Idempotent —
        safe to call on every service startup.
        """
        migrator.apply(1, "domain_event_journal", _JOURNAL_DDL)
        migrator.apply(2, "domain_snapshot_checkpoints", _CHECKPOINTS_DDL)
        migrator.apply(3, "domain_materialized_entities", _MATERIALIZED_DDL)

    # ------------------------------------------------------------------ #
    # Atomic append
    # ------------------------------------------------------------------ #

    def mutate(
        self,
        conn: duckdb.DuckDBPyConnection,
        mutation: DomainMutation,
        write_materialized: WriteMaterialized,
        *,
        event_id: Optional[str] = None,
    ) -> DomainEvent:
        """Atomically read the entity's current revision, invoke
        ``write_materialized(conn, next_revision)``, upsert this module's
        own materialized ledger, and insert the journal row -- all inside
        one explicit transaction on ``conn``.

        ``conn`` must not already be inside an open transaction (this
        method issues its own BEGIN/COMMIT/ROLLBACK).

        ``event_id`` is optional and keyword-only so every existing
        3-positional-argument call site (``journal.mutate(conn, mutation,
        write)``) is unaffected; omitting it auto-generates one (no
        idempotent-retry semantics requested). Producers that need
        idempotent retries (the common case -- e.g. replaying an IB
        callback after a crash) pass a stable, source-derived id.
        """
        eid = event_id if event_id is not None else str(uuid4())
        with self._write_lock:
            conn.execute("BEGIN TRANSACTION")
            try:
                existing = self._select_journal_row(conn, eid)
                if existing is not None:
                    if not self._matches(existing, mutation):
                        raise EventIdentityConflict(eid)
                    conn.execute("COMMIT")
                    return self._row_to_event(existing)

                current_revision = self._read_current_revision(
                    conn, mutation.entity_type, mutation.entity_id
                )
                next_revision = current_revision + 1

                # Caller's own materialized write. Runs INSIDE this
                # transaction -- if it raises, everything below (and this
                # callback's own writes) rolls back together (atomicity,
                # binding item 4).
                write_materialized(conn, next_revision)

                received_ts = _utcnow()
                self._upsert_materialized(conn, mutation, next_revision, received_ts)
                event = self._insert_journal_row(conn, eid, mutation, next_revision, received_ts)

                conn.execute("COMMIT")
                return event
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except BaseException:
                    # RA-11-style guard: a rollback failure (e.g. a failed
                    # BEGIN left no open transaction) must never mask the
                    # real exception being re-raised below.
                    pass
                raise

    # ------------------------------------------------------------------ #
    # Test-support / read APIs
    # ------------------------------------------------------------------ #

    def read_after(self, after_cursor: int, limit: int) -> list[DomainEvent]:
        """Return up to ``limit`` journal events with
        ``source_cursor > after_cursor``, ordered by cursor. Test-support
        API for Task 3; Task 5 builds the long-poll feed on top of this
        query shape.
        """
        conn = self.connect()
        rows = conn.execute(
            f"SELECT {', '.join(_JOURNAL_COLUMNS)} FROM domain_event_journal "
            "WHERE source_cursor > ? ORDER BY source_cursor LIMIT ?",
            [after_cursor, limit],
        ).fetchall()
        return [self._row_to_event(dict(zip(_JOURNAL_COLUMNS, row))) for row in rows]

    def get_entity(self, entity_type: str, entity_id: str) -> Optional[dict[str, Any]]:
        """Return the current materialized-ledger row for
        ``(entity_type, entity_id)``, or ``None`` if it has never been
        written or has been tombstoned (deleted). Test-support API.
        """
        conn = self.connect()
        row = conn.execute(
            "SELECT entity_type, entity_id, account_id, entity_revision, "
            "deleted, payload, updated_at FROM domain_materialized_entities "
            "WHERE entity_type = ? AND entity_id = ?",
            [entity_type, entity_id],
        ).fetchone()
        if row is None:
            return None
        cols = ("entity_type", "entity_id", "account_id", "entity_revision", "deleted", "payload", "updated_at")
        data = dict(zip(cols, row))
        if data["deleted"]:
            return None
        data["payload"] = json.loads(data["payload"]) if data["payload"] is not None else None
        data["updated_at"] = _as_utc(data["updated_at"])
        return data

    # ------------------------------------------------------------------ #
    # Internal helpers (all operate on the caller-supplied conn — never
    # open a second connection mid-transaction)
    # ------------------------------------------------------------------ #

    def _read_current_revision(self, conn, entity_type: str, entity_id: str) -> int:
        # Deliberately NOT filtered by `deleted` -- a delete tombstone
        # still occupies the entity's revision stream, so the next
        # mutation (upsert or delete) must continue from it, never reset.
        row = conn.execute(
            "SELECT entity_revision FROM domain_materialized_entities "
            "WHERE entity_type = ? AND entity_id = ?",
            [entity_type, entity_id],
        ).fetchone()
        return row[0] if row is not None else 0

    def _upsert_materialized(
        self,
        conn,
        mutation: DomainMutation,
        revision: int,
        updated_at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO domain_materialized_entities
                (entity_type, entity_id, account_id, entity_revision, deleted, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                account_id = EXCLUDED.account_id,
                entity_revision = EXCLUDED.entity_revision,
                deleted = EXCLUDED.deleted,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """,
            [
                mutation.entity_type,
                mutation.entity_id,
                mutation.account_id,
                revision,
                mutation.operation == "delete",
                mutation.payload,
                updated_at,
            ],
        )

    def _insert_journal_row(
        self,
        conn,
        event_id: str,
        mutation: DomainMutation,
        revision: int,
        received_ts: datetime,
    ) -> DomainEvent:
        row = conn.execute(
            """
            INSERT INTO domain_event_journal
                (event_id, entity_revision, event_type, entity_type, entity_id,
                 operation, account_id, source, source_timestamp, received_timestamp,
                 correlation_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING source_cursor, event_id, entity_revision, event_type, entity_type,
                      entity_id, operation, account_id, source, source_timestamp,
                      received_timestamp, correlation_id, payload
            """,
            [
                event_id, revision, mutation.event_type, mutation.entity_type,
                mutation.entity_id, mutation.operation, mutation.account_id,
                mutation.source, mutation.source_timestamp, received_ts,
                mutation.correlation_id, mutation.payload,
            ],
        ).fetchone()
        return self._row_to_event(dict(zip(_JOURNAL_COLUMNS, row)))

    def _select_journal_row(self, conn, event_id: str) -> Optional[dict[str, Any]]:
        row = conn.execute(
            f"SELECT {', '.join(_JOURNAL_COLUMNS)} FROM domain_event_journal WHERE event_id = ?",
            [event_id],
        ).fetchone()
        if row is None:
            return None
        return dict(zip(_JOURNAL_COLUMNS, row))

    def _matches(self, existing: dict[str, Any], mutation: DomainMutation) -> bool:
        if existing["event_type"] != mutation.event_type:
            return False
        if existing["entity_type"] != mutation.entity_type:
            return False
        if existing["entity_id"] != mutation.entity_id:
            return False
        if existing["operation"] != mutation.operation:
            return False
        if existing["account_id"] != mutation.account_id:
            return False
        if existing["source"] != mutation.source:
            return False
        if _as_utc(existing["source_timestamp"]) != _as_utc(mutation.source_timestamp):
            return False
        if existing["correlation_id"] != mutation.correlation_id:
            return False
        existing_payload = existing["payload"]
        existing_payload_dict = json.loads(existing_payload) if existing_payload is not None else None
        if _canonical_payload(existing_payload_dict) != _canonical_payload(mutation.payload):
            return False
        return True

    def _row_to_event(self, row: dict[str, Any]) -> DomainEvent:
        payload = row["payload"]
        payload_dict = json.loads(payload) if payload is not None else None
        return DomainEvent(
            event_id=row["event_id"],
            source_cursor=row["source_cursor"],
            entity_revision=row["entity_revision"],
            event_type=row["event_type"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            operation=row["operation"],
            account_id=row["account_id"],
            source=row["source"],
            source_timestamp=_as_utc(row["source_timestamp"]),
            correlation_id=row["correlation_id"],
            payload=payload_dict,
        )
