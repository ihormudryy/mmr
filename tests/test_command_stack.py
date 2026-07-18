from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.feed_service import DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService
from trader.messaging.production_api import build_production_registry
from trader.messaging.typed_rpc import HmacServiceAuthenticator
from trader.trading.command_policy import CommandAuthorityPolicy


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 0, tzinfo=UTC)


class _Portfolio:
    def get_positions(self):
        return []

    def get_portfolio_items(self):
        return []


class _Book:
    def get_open_order_count(self):
        return 0


class _IB:
    def accountValues(self):
        return [SimpleNamespace(
            tag="NetLiquidation", currency="USD", account="DU111111", value="100000",
        )]

    def managedAccounts(self):
        return ["DU111111"]

    def openTrades(self):
        return []


class _Client:
    def __init__(self):
        self.ib = _IB()

    async def get_snapshot(self, contract, delayed):  # pragma: no cover - registration only
        raise AssertionError("quote read is not part of composition")


class _Universe:
    def resolve_symbol(self, conid, **_kwargs):
        if conid != 265598:
            return []
        return [SimpleNamespace(
            conId=265598, symbol="AAPL", secType="STK", exchange="SMART",
            primaryExchange="NASDAQ", currency="USD",
        )]


class _RiskGate:
    def check_instrument(self, **_kwargs):
        return SimpleNamespace(approved=True, reason="")

    def evaluate(self, **_kwargs):
        return SimpleNamespace(approved=True, reason="")

    def check_leverage(self, *_args, **_kwargs):
        return SimpleNamespace(approved=True, reason="")


def _trader(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    broker_store = BrokerStateStore(db)
    broker_store.migrate(migrator)
    return SimpleNamespace(
        journal_db=db,
        domain_journal=journal,
        broker_state_store=broker_store,
        broker_ingest=SimpleNamespace(is_ready=lambda: True),
        risk_gate=_RiskGate(),
        universe_accessor=_Universe(),
        portfolio=_Portfolio(),
        book=_Book(),
        client=_Client(),
        ib_account="DU111111",
        paper_trading=True,
        _main_loop=None,
        get_pnl=lambda: [],
        is_ib_connected=lambda: True,
    )


def _policy():
    return CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0)


@pytest.mark.parametrize(
    ("attribute", "code"),
    [
        ("domain_journal", "MISSING_DOMAIN_JOURNAL"),
        ("journal_db", "MISSING_JOURNAL_DB"),
        ("broker_state_store", "MISSING_BROKER_STATE"),
        ("broker_ingest", "MISSING_BROKER_INGEST"),
        ("risk_gate", "MISSING_RISK_GATE"),
        ("universe_accessor", "MISSING_UNIVERSE"),
        ("portfolio", "MISSING_POSITIONS"),
        ("book", "MISSING_ORDER_BOOK"),
        ("client", "MISSING_BROKER_CLIENT"),
    ],
)
def test_enabled_stack_refuses_a_missing_production_adapter(tmp_path, attribute, code):
    from trader.trading.command_stack import (
        CommandStackConfigurationError,
        build_command_stack,
    )

    trader = _trader(tmp_path)
    setattr(trader, attribute, None)

    with pytest.raises(CommandStackConfigurationError) as exc:
        build_command_stack(trader, _policy(), now=lambda: NOW)

    assert exc.value.code == code


def test_disabled_stack_is_dormant_even_when_ports_are_absent():
    from trader.trading.command_stack import build_command_stack

    assert build_command_stack(
        SimpleNamespace(), CommandAuthorityPolicy(enabled=False), now=lambda: NOW,
    ) is None


def test_live_authority_builds_with_dispatch_guard_and_hard_notional(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    trader.ib_account = "U111111"
    trader.paper_trading = False
    policy = CommandAuthorityPolicy(
        enabled=True,
        live_enabled=True,
        live_account_id="U111111",
        max_order_notional=25_000.0,
    )

    stack = build_command_stack(trader, policy, now=lambda: NOW)

    assert stack is not None
    assert stack.approval_service._dispatch_guard is not None
    assert stack.approval_service._dispatch_guard._policy.max_order_notional == 25_000.0


def test_one_registry_contains_reads_feed_ingest_and_landed_commands(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    snapshot = DomainSnapshotService(trader.domain_journal)
    feed = DomainFeedService(trader.domain_journal)
    registry = build_production_registry(
        trader,
        HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=snapshot,
        feed_service=feed,
        command_stack=stack,
    )

    expected = {
        ("query", "snapshot_with_cursor"),
        ("feed", "read_domain_events"),
        ("command", "record_state_acknowledged"),
        ("command", "preflight_command"),
        ("command", "approve_proposal"),
        ("command", "cancel_order"),
        ("command", "pause_trading"),
        ("command", "resume_trading"),
    }
    for role, method in expected:
        assert registry.contains(role, method), (role, method)


def test_enabled_stack_attaches_recovery_components_to_trader(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)

    assert trader.command_ledger is stack.ledger
    assert trader.command_reconciler is stack.reconciler
    assert trader.automation_circuit_breaker is stack.circuit_breaker
    assert trader.semantic_readiness is stack.semantic_readiness
    assert stack.circuit_breaker.store.get().state == "CLEAR"
