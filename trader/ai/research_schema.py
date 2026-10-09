"""ai.duckdb tables of the research cycle (SP2c Plan 4, migrations 30-34; 35-39 stay free). Plain CREATE only."""
from __future__ import annotations

from trader.ai.schema import Migration

RESEARCH_MIGRATIONS: tuple[Migration, ...] = (
    Migration(30, "ai_research_cycles", ("""
        CREATE TABLE ai_research_cycles (
            cycle_id VARCHAR PRIMARY KEY, session_date VARCHAR NOT NULL, slot_start TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('RUNNING', 'DONE', 'SKIPPED', 'MISSED', 'FAILED')),
            reason VARCHAR, menu_json VARCHAR, dropped_json VARCHAR,
            started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ)""",)),
    Migration(31, "ai_research_candidates", ("""
        CREATE TABLE ai_research_candidates (
            candidate_id VARCHAR PRIMARY KEY, cycle_id VARCHAR NOT NULL,
            kind VARCHAR NOT NULL CHECK (kind IN ('INITIAL', 'RENEWAL')),
            strategy_key VARCHAR NOT NULL, prior_version_digest VARCHAR, thesis VARCHAR,
            body_json VARCHAR NOT NULL, body_sha256 VARCHAR NOT NULL, request_id VARCHAR,
            submit_reply_lost BOOLEAN NOT NULL DEFAULT FALSE,
            state VARCHAR NOT NULL CHECK (state IN ('NEW', 'SUBMITTED', 'EVALUATED', 'CLOSED')),
            case_digest VARCHAR, summary_json VARCHAR, end_code VARCHAR,
            accepted_at TIMESTAMPTZ, next_try_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(32, "ai_backtest_judgments", ("""
        CREATE TABLE ai_backtest_judgments (
            judgment_id VARCHAR PRIMARY KEY, candidate_id VARCHAR NOT NULL UNIQUE,
            case_digest VARCHAR NOT NULL UNIQUE,
            kind VARCHAR NOT NULL CHECK (kind IN ('INITIAL', 'RENEWAL')), prior_version_digest VARCHAR,
            menu_json VARCHAR NOT NULL,
            verdict VARCHAR CHECK (verdict IN ('DEPLOY', 'SHADOW', 'REJECT', 'NO_VERDICT')),
            code VARCHAR, body_json VARCHAR, body_sha256 VARCHAR,
            state VARCHAR NOT NULL CHECK (state IN ('JUDGING', 'DECIDED', 'RECORDED', 'REFUSED')),
            receipt_json VARCHAR, error_code VARCHAR, decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(33, "ai_research_registrations", ("""
        CREATE TABLE ai_research_registrations (
            judgment_id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK (kind IN ('INITIAL', 'RENEWAL')), prior_version_digest VARCHAR,
            strategy_key VARCHAR NOT NULL, bundle_digest VARCHAR, body_json VARCHAR, body_sha256 VARCHAR,
            state VARCHAR NOT NULL CHECK (state IN ('ATTESTING', 'REGISTERING', 'WAITING_CAP', 'REGISTERED',
                                                    'REFUSED')),
            base_digest VARCHAR, version_digest VARCHAR UNIQUE, expiry_session VARCHAR,
            line_state VARCHAR CHECK (line_state IN ('LIVE', 'RENEWING', 'ENDED')),
            error_code VARCHAR, attest_tries INTEGER NOT NULL DEFAULT 0, next_try_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(34, "ai_research_cooldowns", ("""
        CREATE TABLE ai_research_cooldowns (
            strategy_key VARCHAR PRIMARY KEY, until_session VARCHAR NOT NULL,
            source VARCHAR NOT NULL CHECK (source IN ('REJECT', 'CLAIM_REFUSED')),
            recorded_at TIMESTAMPTZ NOT NULL)""",)),
)
