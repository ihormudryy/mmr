"""ai.duckdb table of the nightly author (SP3 Plan 5, migration 40; 41-44 stay free). Plain CREATE only."""
from __future__ import annotations

from trader.ai.schema import Migration

AUTHOR_MIGRATIONS: tuple[Migration, ...] = (
    Migration(40, "author_attempts", ("""
        CREATE TABLE author_attempts (
            attempt_id VARCHAR PRIMARY KEY, session_date VARCHAR NOT NULL UNIQUE, slot_start TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('RUNNING', 'SUBMITTING', 'STORED', 'REFUSED', 'FAILED',
                                                    'SKIPPED', 'MISSED')),
            reason VARCHAR, author_model VARCHAR, calls INTEGER NOT NULL DEFAULT 0, violations_json VARCHAR,
            body_json VARCHAR, body_sha256 VARCHAR, digest VARCHAR, authored_key VARCHAR, tunables_json VARCHAR,
            receipt_json VARCHAR, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ)""",)),
)
