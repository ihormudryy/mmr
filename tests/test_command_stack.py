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
        ("command", "activate_paper_automation"),
        ("command", "deactivate_paper_automation"),
        ("query", "get_paper_automation_status"),
    }
    for role, method in expected:
        assert registry.contains(role, method), (role, method)


def test_enabled_stack_wires_paper_automation_service_and_preflight_policy(
    tmp_path, monkeypatch,
):
    from trader.trading.command_stack import build_command_stack

    home = tmp_path / "home"
    trader_yaml = tmp_path / "custom" / "trader.yaml"
    strategy_yaml = tmp_path / "custom" / "strategies.yaml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TRADER_CONFIG", str(trader_yaml))
    trader = _trader(tmp_path)
    trader.strategy_config_file = str(strategy_yaml)

    stack = build_command_stack(trader, _policy(), now=lambda: NOW)

    service = stack.paper_automation_service
    assert service._trader_yaml_path == trader_yaml
    assert service._strategy_yaml_path == strategy_yaml
    assert service._config_dir == home / ".config" / "mmr"
    assert service._share_dir == home / ".local" / "share" / "mmr"
    assert service._account_mode == "paper"
    assert service._command_authority_enabled is True

    registry = build_production_registry(
        trader,
        HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        command_stack=stack,
    )
    assert registry.contains("command", "activate_paper_automation")
    assert registry.contains("command", "deactivate_paper_automation")
    assert registry.contains("query", "get_paper_automation_status")
    assert stack.coordinator._actions["activate_paper_automation"].requires_preflight is True
    assert stack.coordinator._actions["deactivate_paper_automation"].requires_preflight is False


def test_paper_automation_action_maps_activation_error_code():
    from trader.automation.paper_activation import PaperAutomationActivationError
    from trader.messaging.production_api import _paper_automation_action
    from trader.trading.command_coordinator import CommandRequest, CommandValidationError

    class RefusingService:
        def activate(self, **_kwargs):
            raise PaperAutomationActivationError("NOT_PAPER", "paper account required")

    action = _paper_automation_action(RefusingService(), activate=True)
    request = CommandRequest(
        command_id="activate-paper-1",
        action="activate_paper_automation",
        account_id="DU111111",
        target_type="paper_automation",
        target_id="orb_gld",
        expected_version=None,
        body={"strategy_name": "orb_gld", "reason": "operator approved"},
        source="operator",
    )

    with pytest.raises(CommandValidationError) as exc:
        action(request)

    assert exc.value.code == "NOT_PAPER"


def test_paper_automation_action_maps_materials_error_code():
    from trader.automation.paper_materials import PaperMaterialsError
    from trader.messaging.production_api import _paper_automation_action
    from trader.trading.command_coordinator import CommandRequest, CommandValidationError

    class FailingService:
        def activate(self, **_kwargs):
            raise PaperMaterialsError("public key permissions too open")

    action = _paper_automation_action(FailingService(), activate=True)
    request = CommandRequest(
        command_id="activate-paper-2",
        action="activate_paper_automation",
        account_id="DU111111",
        target_type="paper_automation",
        target_id="orb_gld",
        expected_version=None,
        body={"strategy_name": "orb_gld", "reason": "operator approved"},
        source="operator",
    )

    with pytest.raises(CommandValidationError) as exc:
        action(request)

    assert exc.value.code == "PAPER_MATERIALS_ERROR"


def test_enabled_stack_attaches_recovery_components_to_trader(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)

    assert trader.command_ledger is stack.ledger
    assert trader.command_reconciler is stack.reconciler
    assert trader.automation_circuit_breaker is stack.circuit_breaker
    assert trader.semantic_readiness is stack.semantic_readiness
    assert stack.circuit_breaker.store.get().state == "CLEAR"
    assert stack.automated_intent_service is None


def _automation_key_ring(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from trader.research.signing import public_key_pem

    keys = tmp_path / "keys"
    keys.mkdir()
    priv = ed25519.Ed25519PrivateKey.generate()
    (keys / "verify.pem").write_bytes(public_key_pem(priv.public_key()))
    return str(keys)


def test_paper_automation_registers_execute_automated_intent(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    trader.automation_enabled = True
    trader.automation_live_enabled = False
    trader.automation_public_key_ring_path = _automation_key_ring(tmp_path)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    (tmp_path / "artifacts").mkdir()

    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack is not None
    assert stack.automated_intent_service is not None

    registry = build_production_registry(
        trader,
        HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=DomainSnapshotService(trader.domain_journal),
        feed_service=DomainFeedService(trader.domain_journal),
        command_stack=stack,
    )
    assert registry.contains("command", "execute_automated_intent")


def test_automation_disabled_does_not_register_automated_intent(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    trader.automation_enabled = False
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    registry = build_production_registry(
        trader,
        HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=DomainSnapshotService(trader.domain_journal),
        feed_service=DomainFeedService(trader.domain_journal),
        command_stack=stack,
    )
    assert stack.automated_intent_service is None
    assert not registry.contains("command", "execute_automated_intent")


def test_automation_live_enabled_refused_at_stack_build(tmp_path):
    from trader.trading.command_stack import (
        CommandStackConfigurationError,
        build_command_stack,
    )

    trader = _trader(tmp_path)
    trader.automation_enabled = True
    trader.automation_live_enabled = True
    trader.automation_public_key_ring_path = _automation_key_ring(tmp_path)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    (tmp_path / "artifacts").mkdir()

    with pytest.raises(CommandStackConfigurationError) as exc:
        build_command_stack(trader, _policy(), now=lambda: NOW)
    assert exc.value.code == "AUTOMATION_LIVE_REFUSED"
