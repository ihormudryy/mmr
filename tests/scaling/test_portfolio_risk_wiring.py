"""P5 Task 7 — portfolio risk authority store (migration 53)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.portfolio_risk_authority_store import (
    PORTFOLIO_RISK_AUTHORITY_MIGRATION_VERSIONS,
    PortfolioRiskAuthorityStore,
    apply_portfolio_risk_authority_migrations,
)
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
ACCOUNT = "DU9000001"


def _db(tmp_path: Path):
    db = DuckDBConnection.get_instance(str(tmp_path / "portfolio_auth.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_portfolio_risk_authority_migrations(migrator)
    return db, journal, migrator


def test_migration_53_idempotent(tmp_path):
    _, _, migrator = _db(tmp_path)
    assert PORTFOLIO_RISK_AUTHORITY_MIGRATION_VERSIONS == (53,)
    assert apply_portfolio_risk_authority_migrations(migrator) is False


def test_portfolio_authority_active_lifecycle(tmp_path):
    db, journal, _ = _db(tmp_path)
    store = PortfolioRiskAuthorityStore(journal=journal, db=db, now=lambda: NOW)
    digest = "portfolio-auth-1"
    store.record_issued(
        authority_digest=digest,
        account_id=ACCOUNT,
        max_strategies=2,
        max_combined_gross=0.15,
        daily_loss_limit=0.005,
        operator="operator:alice",
        reason="admit second strategy",
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(days=30),
    )
    store.record_activated(digest, command_id="cmd-1")
    active = store.active_for(ACCOUNT, now=NOW)
    assert active is not None
    assert active.max_strategies == 2
    store.record_utilization(
        account_id=ACCOUNT,
        authority_digest=digest,
        combined_gross=0.08,
        daily_loss_pct=0.001,
        strategy_count=2,
        now=NOW,
    )
    store.record_revoked(digest, reason="rotate")
    assert store.active_for(ACCOUNT, now=NOW) is None
