"""ai.duckdb tables owned by SP2 Plan 5: migrations 10-17 (18-19 stay free). Plain CREATE only."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from trader.ai.decision_schema import DECISION_MIGRATIONS
from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.ai.store import AiStore

RUNTIME_MIGRATIONS: tuple[Migration, ...] = (
    Migration(10, "ai_held_epochs", ("""
        CREATE TABLE ai_held_epochs (
            epoch BIGINT PRIMARY KEY, holder_id VARCHAR NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL, renewed_at TIMESTAMPTZ NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL, lost_at TIMESTAMPTZ, lost_reason VARCHAR)""",)),
    Migration(11, "ai_submissions", ("""
        CREATE TABLE ai_submissions (
            decision_id VARCHAR PRIMARY KEY, command_id VARCHAR NOT NULL UNIQUE,
            source_kind VARCHAR NOT NULL CHECK (source_kind IN
                ('entry_signal', 'exit_signal', 'entry_cycle', 'position_cycle')),
            source_id VARCHAR NOT NULL, action_key VARCHAR NOT NULL,
            action VARCHAR NOT NULL CHECK (action IN ('ENTER', 'CLOSE', 'PARTIAL_CLOSE')),
            body_json VARCHAR NOT NULL, body_sha256 VARCHAR NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            created_epoch BIGINT NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('PENDING', 'SENDING', 'UNKNOWN', 'ACCEPTED', 'FINAL',
                                                    'ABANDONED', 'NOT_ADMITTED', 'FAILED')),
            attempts INTEGER NOT NULL, last_epoch BIGINT, last_sent_at TIMESTAMPTZ,
            next_try_at TIMESTAMPTZ NOT NULL, receipt_json VARCHAR, receipt_state VARCHAR,
            close_root_id VARCHAR, error_code VARCHAR,
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE (source_id, action_key))""",)),
    Migration(12, "ai_outbox", ("CREATE SEQUENCE ai_outbox_seq START 1", """
        CREATE TABLE ai_outbox (
            record_id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK (kind IN ('cost', 'simulated')),
            source_ref VARCHAR NOT NULL, attempt_key VARCHAR, body_json VARCHAR NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('WAITING', 'PENDING', 'DELIVERED', 'DEAD', 'DROPPED')),
            wait_for_decision_id VARCHAR, attempts INTEGER NOT NULL, next_try_at TIMESTAMPTZ NOT NULL,
            last_code VARCHAR, delivered_status VARCHAR,
            created_seq BIGINT NOT NULL DEFAULT nextval('ai_outbox_seq'),
            created_at TIMESTAMPTZ NOT NULL, delivered_at TIMESTAMPTZ)""")),
    Migration(13, "ai_cursors", ("""
        CREATE TABLE ai_cursors (
            name VARCHAR PRIMARY KEY, value BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            generation VARCHAR)""",)),
    Migration(14, "ai_opportunities", ("""
        CREATE TABLE ai_opportunities (
            opportunity_id VARCHAR PRIMARY KEY, signal_cursor BIGINT NOT NULL, strategy_name VARCHAR NOT NULL,
            conid BIGINT NOT NULL, action VARCHAR NOT NULL CHECK (action IN ('BUY', 'SELL')),
            probability DOUBLE, signal_time TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('NEW', 'IN_PROGRESS', 'DECIDED', 'MISSED', 'FAILED')),
            reason VARCHAR, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(15, "ai_coverage_gaps", ("""
        CREATE TABLE ai_coverage_gaps (
            gap_id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK (kind IN ('RETENTION', 'CURSOR_AHEAD', 'GENERATION_CHANGED')),
            after_cursor BIGINT NOT NULL, resumed_cursor BIGINT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(16, "ai_call_contexts", ("""
        CREATE TABLE ai_call_contexts (
            context_key VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
            served_kind VARCHAR NOT NULL CHECK (served_kind IN ('decision', 'cycle', 'signal', 'research')),
            served_id VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(17, "ai_cycles", ("""
        CREATE TABLE ai_cycles (
            cycle_id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL CHECK (kind IN ('entry', 'position')),
            session_date VARCHAR NOT NULL, slot_start TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('RUNNING', 'DONE', 'MISSED', 'SKIPPED', 'TIMED_OUT',
                                                    'FAILED')),
            reason VARCHAR, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ)""",)),
)

ALL_MIGRATIONS: tuple[Migration, ...] = FOUNDATION_MIGRATIONS + RUNTIME_MIGRATIONS + DECISION_MIGRATIONS


def cursor_value_in_tx(conn: Any, name: str) -> int:
    row = conn.execute("SELECT value FROM ai_cursors WHERE name = ?", [name]).fetchone()
    return 0 if row is None else int(row[0])


def cursor_generation_in_tx(conn: Any, name: str) -> Optional[str]:
    """The source generation the cursor belongs to (the trader's signal record), or None if not known yet."""
    row = conn.execute("SELECT generation FROM ai_cursors WHERE name = ?", [name]).fetchone()
    return None if row is None else row[0]


def set_cursor_in_tx(conn: Any, name: str, value: int, now: datetime, generation: Optional[str] = None) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"cursor {name} must be a non-negative integer")
    conn.execute("INSERT INTO ai_cursors VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE "
                 "SET value = excluded.value, updated_at = excluded.updated_at, generation = excluded.generation",
                 [name, value, now, generation])


async def read_cursor(store: AiStore, name: str) -> int:
    return await store.atransaction(lambda conn: cursor_value_in_tx(conn, name))
