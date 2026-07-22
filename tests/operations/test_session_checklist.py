"""Tests for P4 Task 7 session checklists."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.operations.session_checklist import (
    CHECK_PHASE_POST,
    CHECK_PHASE_PRE,
    SessionChecklist,
    SessionChecklistContext,
    SessionChecklistStore,
    apply_session_checklist_migration,
    checklist_digest,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
SESSION = "2026-07-20"
STRATEGY = "orb_breakout"
ARTIFACT = "artifact-deadbeef"
CONFIG = "config-cafebabe"
ACCOUNT = "DU9000001"


def _db(tmp_path: Path):
    db = DuckDBConnection.get_instance(str(tmp_path / "checklist.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_session_checklist_migration(migrator)
    return journal


def _ctx(**overrides) -> SessionChecklistContext:
    base = dict(
        session_key=SESSION,
        strategy_id=STRATEGY,
        artifact_digest=ARTIFACT,
        config_digest=CONFIG,
        account_id=ACCOUNT,
        account_mode="paper",
        gross_allocation=0.06,
        xnys_schedule_version="XNYS-2026",
        broker_generation=3,
        broker=None,
        prior_session_flat=True,
        prior_session_reconciled=True,
        eligibility_valid=True,
        breaker_clear=True,
        quotes_ready=True,
        risk_evidence_current=True,
    )
    base.update(overrides)
    return SessionChecklistContext(**base)


def _broker(*, flat: bool = True) -> BrokerRiskSnapshot:
    from trader.data.broker_state import BrokerPositionRow, BrokerOrderRow

    positions = () if flat else (
        BrokerPositionRow(
            account_id=ACCOUNT, conid=1, symbol="AAPL", sec_type="STK",
            exchange="NASDAQ", currency="USD", quantity=10.0,
            average_cost=100.0, market_price=101.0, market_value=1010.0,
            unrealized_pnl=10.0, realized_pnl=None, daily_pnl=None,
            deleted=False, revision=1, source_timestamp=NOW,
        ),
    )
    orders = () if flat else (
        BrokerOrderRow(
            order_entity_id="ord-1", account_id=ACCOUNT, conid=1, symbol="AAPL",
            order_group_id=None, leg=None, is_external=False, action="BUY",
            order_type="LMT", total_quantity=10.0, filled_quantity=0.0,
            avg_fill_price=None, limit_price=100.0, stop_price=None, tif="DAY",
            status="Submitted", deleted=False, revision=1, source_timestamp=NOW,
        ),
    )
    return BrokerRiskSnapshot(
        generation_id=3, source_cursor=10, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper",
        net_liquidation=1_000_000.0, daily_pnl=0.0,
        positions=positions, working_orders=orders,
    )


class TestPreSessionChecklist:
    def test_passes_when_all_preconditions_met(self, tmp_path):
        journal = _db(tmp_path)
        checklist = SessionChecklist(SessionChecklistStore(journal))
        result = checklist.run_pre(_ctx(broker=_broker()), now=NOW)
        assert result.passed is True
        assert result.phase == CHECK_PHASE_PRE

    @pytest.mark.parametrize("field", [
        "prior_session_flat",
        "prior_session_reconciled",
        "eligibility_valid",
        "breaker_clear",
        "quotes_ready",
    ])
    def test_each_failed_precondition_blocks(self, tmp_path, field):
        journal = _db(tmp_path)
        checklist = SessionChecklist(SessionChecklistStore(journal))
        result = checklist.run_pre(_ctx(**{field: False}), now=NOW)
        assert result.passed is False
        assert field in result.failed


class TestPostSessionChecklist:
    def test_post_requires_flat_replay_and_evidence(self, tmp_path):
        journal = _db(tmp_path)
        checklist = SessionChecklist(SessionChecklistStore(journal))
        ok = checklist.run_post(_ctx(
            broker=_broker(flat=True),
            replay_sealed=True,
            replay_passed=True,
            attribution_complete=True,
            evidence_updated=True,
        ), now=NOW)
        assert ok.passed is True

    def test_open_position_fails_post(self, tmp_path):
        journal = _db(tmp_path)
        checklist = SessionChecklist(SessionChecklistStore(journal))
        result = checklist.run_post(_ctx(
            broker=_broker(flat=False),
            replay_sealed=True,
            replay_passed=True,
            attribution_complete=True,
            evidence_updated=True,
        ), now=NOW)
        assert result.passed is False
        assert "broker_flat" in result.failed


class TestIdempotency:
    def test_same_digest_is_idempotent(self, tmp_path):
        journal = _db(tmp_path)
        checklist = SessionChecklist(SessionChecklistStore(journal))
        ctx = _ctx(broker=_broker())
        r1 = checklist.run_pre(ctx, now=NOW)
        r2 = checklist.run_pre(ctx, now=NOW + dt.timedelta(minutes=5))
        assert r1.result_digest == r2.result_digest
        assert r2.idempotent_replay is True

    def test_digest_changes_when_config_changes(self):
        d1 = checklist_digest(
            session_key=SESSION, strategy_id=STRATEGY,
            artifact_digest=ARTIFACT, config_digest=CONFIG, phase=CHECK_PHASE_PRE,
        )
        d2 = checklist_digest(
            session_key=SESSION, strategy_id=STRATEGY,
            artifact_digest=ARTIFACT, config_digest="other", phase=CHECK_PHASE_PRE,
        )
        assert d1 != d2
