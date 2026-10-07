"""ai.duckdb schema owned by Plan 4: migrations 1-5 (6-9 stay free). Plain CREATE only."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


FOUNDATION_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "ai_model_attempts", ("""
        CREATE TABLE ai_model_attempts (
            attempt_key VARCHAR PRIMARY KEY,
            request_key VARCHAR NOT NULL,
            attempt_no INTEGER NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            reservation_id VARCHAR NOT NULL,
            request_json VARCHAR NOT NULL, request_sha256 VARCHAR NOT NULL,
            response_text VARCHAR, finish_reason VARCHAR, provider_request_id VARCHAR,
            input_tokens BIGINT, output_tokens BIGINT,
            error_code VARCHAR, error_detail VARCHAR,
            started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
            UNIQUE (request_key, attempt_no))""",)),
    Migration(2, "ai_budget_state", ("""
        CREATE TABLE ai_budget_state (
            id INTEGER PRIMARY KEY,
            effective_cap_micros BIGINT NOT NULL,
            pending_cap_micros BIGINT,
            pending_window_date VARCHAR,
            updated_at TIMESTAMPTZ NOT NULL)""", """
        CREATE TABLE ai_budget_cap_events (
            occurred_at TIMESTAMPTZ NOT NULL, kind VARCHAR NOT NULL,
            requested_micros BIGINT NOT NULL, effective_micros BIGINT NOT NULL,
            pending_micros BIGINT, pending_window_date VARCHAR)""")),
    Migration(3, "ai_budget_reservations", ("""
        CREATE TABLE ai_budget_reservations (
            reservation_id VARCHAR PRIMARY KEY,
            window_date VARCHAR NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            reserved_micros BIGINT NOT NULL, actual_micros BIGINT,
            input_tokens BIGINT, output_tokens BIGINT,
            release_reason VARCHAR,
            created_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ)""",)),
    Migration(4, "ai_cost_events", ("CREATE SEQUENCE ai_cost_event_seq START 1", """
        CREATE TABLE ai_cost_events (
            event_seq BIGINT PRIMARY KEY DEFAULT nextval('ai_cost_event_seq'),
            event_id VARCHAR NOT NULL UNIQUE,
            attempt_key VARCHAR NOT NULL,
            role VARCHAR NOT NULL, backend VARCHAR NOT NULL, model VARCHAR NOT NULL,
            kind VARCHAR NOT NULL, cost_micros BIGINT NOT NULL,
            input_tokens BIGINT, output_tokens BIGINT,
            occurred_at TIMESTAMPTZ NOT NULL)""")),
    Migration(5, "ai_replay_evidence", ("""
        CREATE TABLE ai_replay_evidence (
            decision_key VARCHAR NOT NULL, kind VARCHAR NOT NULL, name VARCHAR NOT NULL,
            args_sha256 VARCHAR NOT NULL, ordinal INTEGER NOT NULL,
            payload_json VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (decision_key, kind, name, args_sha256, ordinal))""",)),
)
