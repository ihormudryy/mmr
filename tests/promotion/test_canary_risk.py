"""P4 Task 6 — canary capital-safety incident response and drawdown."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.circuit_breaker_store import CircuitBreakerStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.promotion.canary_risk import (
    CANARY_RISK_MIGRATION_VERSIONS,
    CAPITAL_SAFETY_INCIDENT_KINDS,
    CanaryAttributionView,
    CanaryRiskController,
    CanaryRiskStore,
    CapitalSafetyIncident,
    apply_canary_risk_migration,
)
from trader.trading.circuit_breaker import BreakerSignal, CircuitBreaker

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
STRATEGY = "orb_breakout"
ACCOUNT = "DU9000001"
CONID = 265598


def _db(tmp_path: Path, name: str = "canary_risk.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_canary_risk_migration(migrator)
    from trader.data.circuit_breaker_store import apply_circuit_breaker_migration

    apply_circuit_breaker_migration(migrator)
    return db, journal


def _broker(
    *,
    net_liquidation: float = 1_000_000.0,
    daily_pnl: float = 0.0,
    positions: tuple[BrokerPositionRow, ...] = (),
    account_id: str = ACCOUNT,
) -> BrokerRiskSnapshot:
    return BrokerRiskSnapshot(
        generation_id=1,
        source_cursor=1,
        promoted_at=NOW,
        account_id=account_id,
        account_mode="live",
        net_liquidation=net_liquidation,
        daily_pnl=daily_pnl,
        positions=positions,
        working_orders=(),
    )


def _position(conid: int = CONID, qty: float = 100.0) -> BrokerPositionRow:
    return BrokerPositionRow(
        account_id=ACCOUNT,
        conid=conid,
        symbol="AAPL",
        sec_type="STK",
        exchange="NASDAQ",
        currency="USD",
        quantity=qty,
        average_cost=140.0,
        market_price=150.0,
        market_value=qty * 150.0,
        unrealized_pnl=None,
        realized_pnl=None,
        daily_pnl=None,
        deleted=False,
        revision=1,
        source_timestamp=NOW,
    )


def _attribution(*, net_pnl: float = 0.0, unresolved: int = 0, commission_total: float = 0.0) -> CanaryAttributionView:
    return CanaryAttributionView(
        net_pnl_after_cost=net_pnl,
        unresolved_trade_count=unresolved,
        commission_total=commission_total,
    )


def _controller(journal: DomainJournal) -> CanaryRiskController:
    store = CanaryRiskStore(journal, STRATEGY, ACCOUNT)
    breaker_store = CircuitBreakerStore(journal, ACCOUNT)
    breaker_store.seed(NOW)

    def session_key(ts: dt.datetime) -> str:
        return ts.astimezone(UTC).date().isoformat()

    breaker = CircuitBreaker(
        breaker_store,
        now=lambda: NOW,
        reset_ready=lambda: True,
        reconciliation_complete=lambda: True,
        session_key=session_key,
    )
    return CanaryRiskController(store=store, breaker=breaker, strategy_id=STRATEGY)


class TestMigration44:
    def test_migration_version_in_p4_range(self):
        assert all(40 <= v <= 49 for v in CANARY_RISK_MIGRATION_VERSIONS)


class TestHighWaterMark:
    def test_updates_from_broker_net_liquidation(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        snap = _broker(net_liquidation=1_050_000.0)
        state = ctrl.observe(snap, _attribution(), now=NOW, permitted_conids=(CONID,))
        assert state.high_water_mark == 1_050_000.0
        assert state.paused is False

    def test_persists_across_restart(self, tmp_path):
        db_path = tmp_path / "persist.duckdb"
        db1 = DuckDBConnection.get_instance(str(db_path))
        migrator1 = SchemaMigrator(db1)
        journal1 = DomainJournal(db1)
        journal1.migrate(migrator1)
        apply_canary_risk_migration(migrator1)
        from trader.data.circuit_breaker_store import apply_circuit_breaker_migration

        apply_circuit_breaker_migration(migrator1)
        ctrl1 = _controller(journal1)
        ctrl1.observe(_broker(net_liquidation=1_020_000.0), _attribution(), now=NOW, permitted_conids=(CONID,))

        db2 = DuckDBConnection.get_instance(str(db_path))
        journal2 = DomainJournal(db2)
        ctrl2 = _controller(journal2)
        state = ctrl2.observe(_broker(net_liquidation=990_000.0), _attribution(), now=NOW, permitted_conids=(CONID,))
        assert state.high_water_mark == 1_020_000.0


class TestDrawdownAndDailyLoss:
    def test_three_percent_drawdown_triggers_incident_and_pause(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        ctrl.observe(_broker(net_liquidation=1_000_000.0), _attribution(), now=NOW, permitted_conids=(CONID,))
        state = ctrl.observe(
            _broker(net_liquidation=965_000.0),
            _attribution(net_pnl=-35_000.0),
            now=NOW + dt.timedelta(minutes=5),
            permitted_conids=(CONID,),
        )
        assert state.drawdown_fraction >= 0.03
        assert state.paused is True
        assert state.suspend_required is True
        assert "TRIP_BREAKER" in state.actions
        assert "SUSPEND_ARTIFACT" in state.actions

    def test_half_percent_daily_loss_includes_commissions_and_unrealized(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        # daily_pnl from broker includes unrealized; attribution commissions add to loss
        state = ctrl.observe(
            _broker(net_liquidation=1_000_000.0, daily_pnl=-4_500.0),
            _attribution(net_pnl=-500.0, commission_total=500.0),
            now=NOW,
            permitted_conids=(CONID,),
        )
        assert state.daily_loss_fraction >= 0.005
        assert state.paused is True


class TestCapitalSafetyIncidents:
    @pytest.mark.parametrize(
        "kind",
        sorted(CAPITAL_SAFETY_INCIDENT_KINDS),
    )
    def test_each_incident_causes_one_record_and_global_pause(self, tmp_path, kind):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        result = ctrl.record_incident(
            CapitalSafetyIncident(kind=kind, detail=f"test {kind}", occurred_at=NOW),
            broker=_broker(),
            now=NOW,
        )
        assert result.paused is True
        assert result.incident_count == 1
        assert result.actions[0] == "PERSIST_INCIDENT"
        assert "TRIP_BREAKER" in result.actions
        assert "REJECT_NEW_EXPOSURE" in result.actions

    def test_duplicate_incident_id_is_idempotent(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        inc = CapitalSafetyIncident(kind="DUPLICATE_SUBMISSION", detail="dup", occurred_at=NOW, key="cmd-1")
        r1 = ctrl.record_incident(inc, broker=_broker(), now=NOW)
        r2 = ctrl.record_incident(inc, broker=_broker(), now=NOW + dt.timedelta(seconds=1))
        assert r1.incident_count == 1
        assert r2.incident_count == 1

    def test_unexplained_position_outside_allowlist(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        state = ctrl.observe(
            _broker(positions=(_position(conid=999999, qty=10.0),)),
            _attribution(),
            now=NOW,
            permitted_conids=(CONID,),
        )
        assert state.paused is True
        assert any(i.kind == "UNEXPLAINED_POSITION" for i in state.incidents)


class TestResponseOrdering:
    def test_atomic_action_order(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        result = ctrl.record_incident(
            CapitalSafetyIncident(kind="MISSING_PROTECTION", detail="no stop", occurred_at=NOW),
            broker=_broker(),
            now=NOW,
        )
        assert result.actions == (
            "PERSIST_INCIDENT",
            "TRIP_BREAKER",
            "REJECT_NEW_EXPOSURE",
            "CANCEL_ENTRIES",
            "REDUCE",
            "RECONCILE",
            "SUSPEND_ARTIFACT",
        )


class TestBreakerResetPolicy:
    def test_daily_loss_reset_blocked_same_session(self, tmp_path):
        _, journal = _db(tmp_path)
        store = CanaryRiskStore(journal, STRATEGY, ACCOUNT)
        breaker_store = CircuitBreakerStore(journal, ACCOUNT)
        breaker_store.seed(NOW)
        session_key = lambda ts: ts.date().isoformat()
        breaker = CircuitBreaker(
            breaker_store,
            now=lambda: NOW,
            reset_ready=lambda: True,
            reconciliation_complete=lambda: True,
            session_key=session_key,
        )
        ctrl = CanaryRiskController(store=store, breaker=breaker, strategy_id=STRATEGY)
        ctrl.observe(_broker(daily_pnl=-6_000.0), _attribution(), now=NOW, permitted_conids=(CONID,))
        from trader.trading.circuit_breaker import BreakerResetRefused

        with pytest.raises(BreakerResetRefused):
            breaker.reset("reset-1", "operator cleared", "op:alice")

    def test_drawdown_suspension_requires_fresh_promotion_not_breaker_reset_alone(self, tmp_path):
        _, journal = _db(tmp_path)
        ctrl = _controller(journal)
        ctrl.observe(_broker(net_liquidation=1_000_000.0), _attribution(), now=NOW, permitted_conids=(CONID,))
        state = ctrl.observe(
            _broker(net_liquidation=960_000.0),
            _attribution(),
            now=NOW + dt.timedelta(hours=1),
            permitted_conids=(CONID,),
        )
        assert state.suspend_required is True
        assert state.reactivation_requires_promotion_review is True
