"""Journal tables of the SP2c backtest judge (migrations 110 and 111, and Plan 5's 125; 112-114 stay free)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

CLAIMS_MIGRATION_VERSION = 110
JUDGMENTS_MIGRATION_VERSION = 111
RENEWAL_JUDGMENTS_MIGRATION_VERSION = 125

_CLAIMS = """CREATE TABLE IF NOT EXISTS evaluation_claims (
    request_id VARCHAR PRIMARY KEY, body_json VARCHAR NOT NULL, strategy_key VARCHAR NOT NULL,
    ny_day DATE NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('QUEUED','RUNNING','DONE','FAILED')),
    principal VARCHAR NOT NULL, claimed_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)"""

_JUDGMENTS = """CREATE TABLE IF NOT EXISTS backtest_judgments (
    judgment_id VARCHAR PRIMARY KEY, case_digest VARCHAR NOT NULL UNIQUE, request_id VARCHAR,
    kind VARCHAR NOT NULL CHECK (kind IN ('INITIAL','RENEWAL')),
    verdict VARCHAR NOT NULL CHECK (verdict IN ('DEPLOY','SHADOW','REJECT','NO_VERDICT')),
    strategy_key VARCHAR NOT NULL, body_json VARCHAR NOT NULL, body_digest VARCHAR NOT NULL,
    binding_json VARCHAR NOT NULL, cooldown_until_session DATE, recorded_at TIMESTAMPTZ NOT NULL,
    record_digest VARCHAR NOT NULL,
    CHECK ((kind = 'INITIAL') = (request_id IS NOT NULL)),
    CHECK ((verdict = 'REJECT') = (cooldown_until_session IS NOT NULL)))"""

_RENEWAL_JUDGMENTS = """CREATE TABLE IF NOT EXISTS renewal_judgments (
    prior_version VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL UNIQUE,
    recorded_at TIMESTAMPTZ NOT NULL)"""


def apply_backtest_judge_migrations(migrator: Any) -> list[int]:
    applied = []
    if migrator.apply(CLAIMS_MIGRATION_VERSION, "sp2c_evaluation_claims", (_CLAIMS,)):
        applied.append(CLAIMS_MIGRATION_VERSION)
    if migrator.apply(JUDGMENTS_MIGRATION_VERSION, "sp2c_backtest_judgments", (_JUDGMENTS,)):
        applied.append(JUDGMENTS_MIGRATION_VERSION)
    if migrator.apply(RENEWAL_JUDGMENTS_MIGRATION_VERSION, "sp2c_renewal_judgments", (_RENEWAL_JUDGMENTS,)):
        applied.append(RENEWAL_JUDGMENTS_MIGRATION_VERSION)
    return applied


def cooling_until_in_tx(conn: Any, strategy_key: str, today: dt.date) -> Optional[dt.date]:
    """The last cooldown session of ``strategy_key`` if it still cools down on ``today``, else None."""
    row = conn.execute(
        "SELECT MAX(cooldown_until_session) FROM backtest_judgments WHERE strategy_key = ? AND verdict = 'REJECT'",
        [strategy_key]).fetchone()
    until = None if row is None else row[0]
    return until if until is not None and today <= until else None
