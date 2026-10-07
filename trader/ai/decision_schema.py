"""ai.duckdb tables of the decision engine (SP2 Plan 6, migrations 20-21; 22-29 stay free). Plain CREATE only."""
from __future__ import annotations

from trader.ai.schema import Migration

DECISION_MIGRATIONS: tuple[Migration, ...] = (
    Migration(20, "ai_rulings", ("""
        CREATE TABLE ai_rulings (
            ruling_id VARCHAR PRIMARY KEY, unit_key VARCHAR NOT NULL,
            step VARCHAR NOT NULL CHECK (step IN ('jev', 'entries', 'closes')),
            action_key VARCHAR,
            outcome VARCHAR NOT NULL CHECK (outcome IN ('TAKE', 'SKIP', 'REDUCE', 'PICKS', 'CLOSES', 'REFUSED')),
            code VARCHAR NOT NULL, quantity BIGINT, ceiling BIGINT, evidence_digest VARCHAR, detail VARCHAR,
            recorded_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(21, "ai_discovery_reads", ("""
        CREATE TABLE ai_discovery_reads (
            cycle_id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL CHECK (status IN ('OK', 'FAILED')),
            error_code VARCHAR, read_at VARCHAR, complete BOOLEAN NOT NULL, coverage_json VARCHAR,
            seen INTEGER NOT NULL, eligible INTEGER NOT NULL, dropped_json VARCHAR NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL)""",)),
)
