"""Journal migrations 60-64: the scoreboard tables (Plan 5 owns 60-69); SP2 Plan 2 adds 95-96."""
from __future__ import annotations

from typing import Any

_CORE = (
    "CREATE SEQUENCE IF NOT EXISTS scoreboard_seal_seq START 1",
    """CREATE TABLE IF NOT EXISTS equity_daily (
        experiment_id VARCHAR NOT NULL, session_date DATE NOT NULL, account_id VARCHAR NOT NULL,
        session_end_state VARCHAR NOT NULL
            CHECK (session_end_state IN ('FLAT','KILLED','FAILED_SAFE','UNKNOWN')),
        base_currency VARCHAR, start_nlv_base DOUBLE, end_nlv_base DOUBLE,
        fx_usd_per_base DOUBLE, fx_source VARCHAR, fx_as_of TIMESTAMPTZ,
        start_nlv_usd DOUBLE, end_nlv_usd DOUBLE,
        start_source VARCHAR NOT NULL CHECK (start_source IN ('prev_end','experiment_start','unknown')),
        missing_sessions_before INTEGER,
        realized_pnl_usd DOUBLE, commissions_usd DOUBLE, commission_json VARCHAR NOT NULL,
        peak_gross_exposure_usd DOUBLE, trade_count INTEGER NOT NULL, fill_count INTEGER NOT NULL,
        open_positions INTEGER, fills_digest VARCHAR NOT NULL,
        ended_at TIMESTAMPTZ NOT NULL, written_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (experiment_id, session_date))""",
    """CREATE TABLE IF NOT EXISTS equity_adjustments (
        adjustment_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        session_date DATE NOT NULL, kind VARCHAR NOT NULL CHECK (kind IN ('COMMISSION')),
        exec_id VARCHAR NOT NULL, amount_usd DOUBLE NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS equity_session_peak (
        experiment_id VARCHAR NOT NULL, session_date DATE NOT NULL,
        peak_gross_usd DOUBLE, incomplete BOOLEAN NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (experiment_id, session_date))""",
    """CREATE TABLE IF NOT EXISTS scoreboard_seals (
        seal_id BIGINT PRIMARY KEY DEFAULT nextval('scoreboard_seal_seq'),
        table_name VARCHAR NOT NULL, row_key VARCHAR NOT NULL, row_digest VARCHAR NOT NULL,
        prev_chain VARCHAR NOT NULL, chain VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS scoreboard_incidents (
        kind VARCHAR NOT NULL, key VARCHAR NOT NULL, detail VARCHAR NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (kind, key))""",
)

_ROUND_TRIPS = (
    """CREATE TABLE IF NOT EXISTS round_trips (
        round_trip_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        account_id VARCHAR NOT NULL, conid BIGINT NOT NULL, symbol VARCHAR,
        direction VARCHAR NOT NULL, status VARCHAR NOT NULL CHECK (status IN ('OPEN','CLOSED')),
        opened_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ, opened_session DATE NOT NULL,
        closed_session DATE, entry_qty DOUBLE NOT NULL, exit_qty DOUBLE NOT NULL,
        entry_avg DOUBLE, exit_avg DOUBLE, gross_pnl_usd DOUBLE NOT NULL, fees_usd DOUBLE,
        net_pnl_usd DOUBLE, fees_complete BOOLEAN NOT NULL, notional_traded_usd DOUBLE NOT NULL,
        strategy_version VARCHAR, decider VARCHAR, policy_revision VARCHAR, style VARCHAR,
        decision_id VARCHAR, links_digest VARCHAR, exec_ids VARCHAR NOT NULL,
        fills_digest VARCHAR NOT NULL)""",
)

_BENCHMARK = (
    """CREATE TABLE IF NOT EXISTS benchmark_versions (
        version INTEGER PRIMARY KEY, reason VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS benchmark_prices (
        version INTEGER NOT NULL, bar_date DATE NOT NULL, symbol VARCHAR NOT NULL,
        conid BIGINT NOT NULL, close DOUBLE NOT NULL, provider VARCHAR NOT NULL,
        bar_size VARCHAR NOT NULL, fetched_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (version, bar_date))""",
)

_COSTS = (
    """CREATE TABLE IF NOT EXISTS ai_costs (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        role VARCHAR NOT NULL CHECK (role IN ('orchestrator','jev','research')),
        provider VARCHAR NOT NULL, model VARCHAR NOT NULL, attempt_id VARCHAR NOT NULL,
        input_tokens BIGINT, output_tokens BIGINT, cost_usd DOUBLE,
        cost_status VARCHAR NOT NULL CHECK (cost_status IN ('confirmed','estimated','unknown')),
        called_at TIMESTAMPTZ NOT NULL, served_kind VARCHAR NOT NULL, served_id VARCHAR NOT NULL,
        decision_id VARCHAR, corrects_record_id VARCHAR, correction_seq INTEGER NOT NULL,
        body_digest VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
        CHECK ((cost_status = 'unknown') = (cost_usd IS NULL)))""",
)

_SIMULATED = (
    """CREATE TABLE IF NOT EXISTS simulated_decisions (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        baseline_id VARCHAR NOT NULL, cohort VARCHAR NOT NULL, opportunity_id VARCHAR NOT NULL,
        conid BIGINT, side VARCHAR CHECK (side IS NULL OR side = 'BUY'), quantity BIGINT,
        quantity_source VARCHAR CHECK (quantity_source IS NULL OR quantity_source IN
            ('client','trader_sizing','linked_entry')),
        sizing_json VARCHAR, reference_price DOUBLE, stop_price DOUBLE, target_price DOUBLE,
        decided_at TIMESTAMPTZ NOT NULL, session_date DATE NOT NULL, linked_decision_id VARCHAR,
        linked_round_trip_id VARCHAR, deployment_digest VARCHAR, incomplete_reason VARCHAR,
        body_digest VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_simulated_decisions_book "
    "ON simulated_decisions(experiment_id, baseline_id, cohort)",
)

_OUTCOMES = (
    """CREATE TABLE IF NOT EXISTS simulated_outcomes (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        baseline_id VARCHAR NOT NULL, cohort VARCHAR NOT NULL, session_date DATE NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('COMPLETE','INCOMPLETE')), reason VARCHAR,
        exit_kind VARCHAR NOT NULL CHECK (exit_kind IN ('STOP','TARGET','FLATTEN','NONE')),
        exit_at TIMESTAMPTZ, exit_price DOUBLE, pnl_usd DOUBLE, trades INTEGER, quantity BIGINT,
        bar_source VARCHAR NOT NULL, bars_digest VARCHAR, computed_at TIMESTAMPTZ NOT NULL,
        CHECK ((status = 'COMPLETE') = (pnl_usd IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS idx_simulated_outcomes_book "
    "ON simulated_outcomes(experiment_id, baseline_id, cohort)",
)

_OUTBOX = (
    """CREATE TABLE IF NOT EXISTS telegram_outbox (
        event_id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL, text VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, status VARCHAR NOT NULL CHECK (status IN ('PENDING','SENT')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL,
        last_error VARCHAR, sent_at TIMESTAMPTZ, telegram_message_id BIGINT)""",
)

MIGRATIONS = (
    (60, "scoreboard_core", _CORE),
    (61, "scoreboard_round_trips", _ROUND_TRIPS),
    (62, "scoreboard_benchmark", _BENCHMARK),
    (63, "scoreboard_costs", _COSTS),
    (64, "scoreboard_outbox", _OUTBOX),
    (95, "sp2_simulated_decisions", _SIMULATED),
    (96, "sp2_simulated_outcomes", _OUTCOMES),
)


def apply_scoreboard_migrations(migrator: Any) -> bool:
    """True when any migration of this module was newly applied."""
    applied = [migrator.apply(version, name, statements) for version, name, statements in MIGRATIONS]
    return any(applied)
