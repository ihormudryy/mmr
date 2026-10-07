from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.rpc_identity_fixtures import make_identities

from trader.data.broker_state import BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.domain.feed_service import DomainFeedService
from trader.domain.snapshot_service import DomainSnapshotService
from trader.messaging.production_api import build_production_registry
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
        make_identities()["trader"],
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
    # Without an RPC identity on the stub trader, strategy-control stays
    # unregistered (dormant). Production boots with rpc_identity loaded.
    assert stack.strategy_control_service is None
    assert not registry.contains("command", "disable_strategy")


def test_strategy_control_registers_enable_disable_when_identity_present(tmp_path):
    """Regression: METHOD_NOT_ALLOWED on Disable from the Strategies panel."""
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    trader.rpc_identity = make_identities()["trader"]
    trader.strategy_typed_address = "tcp://127.0.0.1"
    trader.strategy_typed_command_port = 42104
    trader.strategy_typed_query_port = 42105

    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack.strategy_control_service is not None

    registry = build_production_registry(
        trader,
        trader.rpc_identity,
        command_stack=stack,
    )
    for method in ("enable_strategy", "disable_strategy", "update_strategy_params",
                   "record_state_acknowledged"):
        assert registry.contains("command", method), method
    assert "disable_strategy" in stack.coordinator._actions
    assert "enable_strategy" in stack.coordinator._actions


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
        make_identities()["trader"],
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
        make_identities()["trader"],
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
        make_identities()["trader"],
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


def test_frozen_command_stack_late_binds_automated_intent_service(tmp_path):
    """Paper hot-arm late-binds execute path onto a frozen CommandStack."""
    from dataclasses import FrozenInstanceError

    from trader.trading.command_stack import CommandStack, build_command_stack

    trader = _trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert isinstance(stack, CommandStack)
    assert stack.automated_intent_service is None
    with pytest.raises(FrozenInstanceError):
        stack.automated_intent_service = object()
    sentinel = object()
    object.__setattr__(stack, "automated_intent_service", sentinel)
    assert stack.automated_intent_service is sentinel
    object.__setattr__(stack, "automated_intent_service", None)
    assert stack.automated_intent_service is None


def test_automation_approval_uses_fenced_broker_and_executable_quote(tmp_path, monkeypatch):
    from decimal import Decimal

    from trader.data.broker_state import BrokerRiskSnapshot
    from trader.trading.command_ports import (
        TraderBrokerAuthority, TraderBrokerRiskSnapshotAuthority, TraderQuoteAuthority,
    )
    from trader.trading.command_stack import build_command_stack
    from trader.trading.proposal_command_service import ExecutableQuote

    trader = _trader(tmp_path)
    trader.automation_enabled = True
    trader.automation_live_enabled = False
    trader.automation_strategy_name = "qualified-paper-strategy"
    trader.automation_public_key_ring_path = _automation_key_ring(tmp_path)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    (tmp_path / "artifacts").mkdir()
    snapshot = BrokerRiskSnapshot(
        generation_id=7, source_cursor=35, promoted_at=NOW,
        account_id=trader.ib_account, account_mode="paper",
        net_liquidation=123_456.0, daily_pnl=-123.0, positions=(), working_orders=(),
    )
    quote = ExecutableQuote(
        conid=265598, side="BUY", price=160.01,
        market_timestamp=NOW - dt.timedelta(seconds=1),
        feed_type="live", session_state="continuous", bid=160.0, ask=160.01,
    )
    monkeypatch.setattr(TraderBrokerRiskSnapshotAuthority, "capture", lambda *_: snapshot)
    monkeypatch.setattr(TraderQuoteAuthority, "executable_quote", lambda *_, **__: quote)
    monkeypatch.setattr(TraderBrokerAuthority, "what_if_margin", lambda *_: None)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    intent = SimpleNamespace(
        conid=265598, side="BUY", requested_quantity=Decimal("10"),
        account_mode="paper", artifact_id="artifact-test-1",
    )

    approval = stack.automated_intent_service._approval_factory(
        intent=intent, command=SimpleNamespace(account_id=trader.ib_account),
    )

    assert approval.broker is snapshot
    assert approval.market.quote is quote
    assert approval.quantity == 10.0
    assert approval.reference_price == quote.price
    assert approval.market.quote.market_timestamp == NOW - dt.timedelta(seconds=1)


def test_hot_arm_and_disarm_use_real_command_stack_and_strategy_binding(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    assert stack is not None
    registry = build_production_registry(
        trader, make_identities()["trader"], command_stack=stack,
    )
    ports = stack.paper_hot_arm
    ports.attach_registry(registry)
    ports.trader_commit(
        strategy_name="qualified-paper-strategy", artifact_id="artifact-test-1",
        artifact_bundle_path=str(tmp_path / "bundle"),
        public_key_ring_path=_automation_key_ring(tmp_path),
    )

    assert registry.contains("command", "execute_automated_intent")
    assert stack.automated_intent_service is not None
    evidence = stack.automated_intent_service._approval_factory.__self__
    assert evidence._strategy_id == "qualified-paper-strategy"
    assert trader.automation_strategy_name == "qualified-paper-strategy"

    ports.trader_compensate()
    assert stack.automated_intent_service is None
    assert not registry.contains("command", "execute_automated_intent")
    assert trader.automation_enabled is False


def test_enabled_stack_applies_safe_close_migrations(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    build_command_stack(trader, _policy(), now=lambda: NOW)
    versions = {r[0] for r in trader.journal_db.execute("SELECT version FROM schema_migrations", fetch="all")}
    assert {35, 36} <= versions


# --- Plan 3 Task 9: ai_paper composition --------------------------------------

def _ai_stack(tmp_path, *, enabled=True, paper=True):
    from trader.automation.ai_paper_config import AiPaperConfig
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    if not paper:
        trader.ib_account, trader.paper_trading = "U111111", False
    trader.ai_paper_config = AiPaperConfig(enabled=enabled)
    policy = (_policy() if paper else CommandAuthorityPolicy(
        enabled=True, live_enabled=True, live_account_id="U111111", max_order_notional=25_000.0))
    return build_command_stack(trader, policy, now=lambda: NOW), trader


def test_ai_paper_on_a_live_account_refuses_to_build(tmp_path):
    from trader.trading.command_stack import CommandStackConfigurationError

    with pytest.raises(CommandStackConfigurationError) as exc:
        _ai_stack(tmp_path, paper=False)
    assert exc.value.code == "AI_PAPER_LIVE_REFUSED"


def test_ai_paper_stack_reads_real_experiments(tmp_path):          # SP1 Plan 4 Task 6
    from trader.automation.ai_paper_experiment import ExperimentStateReader

    stack, trader = _ai_stack(tmp_path)
    assert isinstance(stack.ai_paper.decisions._experiments, ExperimentStateReader)
    assert stack.experiments.reader is stack.ai_paper.decisions._experiments
    assert trader.experiment_store is stack.experiments.store
    assert trader.ai_paper_attribution is stack.ai_paper.decision_store
    assert stack.ai_paper.policy.ceiling == stack.ai_paper.config.limits_ceiling


def test_dispatch_guard_routes_current_limits_by_action(tmp_path):
    from trader.automation.ai_risk_policy import PolicyRefused
    from trader.automation.risk_limits import PAPER_LIMITS

    stack, _ = _ai_stack(tmp_path)
    guard = stack.dispatch_guard
    assert guard._current_limits(SimpleNamespace(action="execute_automated_intent")) == PAPER_LIMITS
    with pytest.raises(PolicyRefused, match="NO_EFFECTIVE_LIMITS"):      # nothing in force: refused, not defaulted
        guard._current_limits(SimpleNamespace(action="submit_ai_paper_decision"))


def test_dispatch_guard_gets_the_ai_gate_and_strict_margin_for_ai_paper_only(tmp_path):   # R25
    stack, _ = _ai_stack(tmp_path)
    assert stack.dispatch_guard._strict_margin_actions == frozenset({"submit_ai_paper_decision"})
    assert stack.dispatch_guard._ai_entry_gate(SimpleNamespace(action="approve_proposal"), None, None, NOW) is None


def test_ai_paper_disabled_keeps_the_old_guard_defaults(tmp_path):
    stack, _ = _ai_stack(tmp_path, enabled=False)
    assert stack.ai_paper is None
    assert stack.dispatch_guard._strict_margin_actions == frozenset()


def test_ai_paper_migrations_are_applied(tmp_path):
    stack, trader = _ai_stack(tmp_path, enabled=False)
    versions = {row[0] for row in trader.journal_db.execute("SELECT version FROM schema_migrations", fetch="all")}
    assert {54, 55, 56} <= versions


# --- Issue #74: paper-only Alpaca IEX quote fallback ---------------------------

IEX_FEEDS = frozenset({"live", "iex_realtime"})
LIVE_FEEDS = frozenset({"live"})


class _RecordingAlpacaClient:
    """Stands in for AlpacaClient: records construction and every request; no network."""

    built: list = []
    requests: list = []

    def __init__(self, key_id, secret_key, **kwargs):
        type(self).built.append((key_id, secret_key, kwargs))

    def get_json(self, path, params):
        type(self).requests.append((path, dict(params)))
        return {"quotes": {}}


@pytest.fixture
def alpaca(monkeypatch):
    import trader.data_providers.alpaca.client as client_module

    _RecordingAlpacaClient.built, _RecordingAlpacaClient.requests = [], []
    monkeypatch.setattr(client_module, "AlpacaClient", _RecordingAlpacaClient)
    return _RecordingAlpacaClient


def _fallback_trader(tmp_path, *, paper=True, setting="alpaca_iex", key_id="key-id", secret="key-secret"):
    trader = _trader(tmp_path)
    if not paper:
        trader.ib_account, trader.paper_trading = "U111111", False
    trader.automation_quote_fallback = setting
    trader.alpaca_api_key_id = key_id
    trader.alpaca_api_secret_key = secret
    return trader


def _live_policy():
    return CommandAuthorityPolicy(enabled=True, live_enabled=True, live_account_id="U111111",
                                  max_order_notional=25_000.0)


def _accepted_feeds_of(stack):
    return {"guard": stack.dispatch_guard._accepted_feeds,
            "liquidity": stack.session_risk._liquidity._accepted_feeds}


def test_paper_with_the_setting_wraps_quotes_and_accepts_the_iex_feed(tmp_path, alpaca):
    from trader.trading.command_stack import build_command_stack
    from trader.trading.paper_quote_fallback import FallbackQuoteAuthority

    stack = build_command_stack(_fallback_trader(tmp_path), _policy(), now=lambda: NOW)

    assert isinstance(stack.dispatch_guard._quotes, FallbackQuoteAuthority)
    assert stack.proposal_service._quotes is stack.dispatch_guard._quotes
    assert _accepted_feeds_of(stack) == {"guard": IEX_FEEDS, "liquidity": IEX_FEEDS}
    assert alpaca.built == [("key-id", "key-secret", {"timeout": 3.0})]


def test_paper_without_the_setting_keeps_ib_quotes_and_the_live_feed_only(tmp_path, alpaca):
    from trader.trading.command_ports import TraderQuoteAuthority
    from trader.trading.command_stack import build_command_stack

    stack = build_command_stack(_fallback_trader(tmp_path, setting=""), _policy(), now=lambda: NOW)

    assert isinstance(stack.dispatch_guard._quotes, TraderQuoteAuthority)
    assert _accepted_feeds_of(stack) == {"guard": LIVE_FEEDS, "liquidity": LIVE_FEEDS}
    assert alpaca.built == []


def test_a_live_account_never_builds_or_calls_alpaca_even_with_the_setting(tmp_path, alpaca, monkeypatch):
    from trader.trading.command_ports import TraderQuoteAuthority
    from trader.trading.command_stack import build_command_stack
    from trader.trading.proposal_command_service import ExecutableQuote

    delayed = ExecutableQuote(conid=265598, side="ask", price=1.0, market_timestamp=NOW,
                              feed_type="delayed", session_state="continuous", bid=1.0, ask=1.0)
    monkeypatch.setattr(TraderQuoteAuthority, "executable_quote", lambda *_, **__: delayed)
    stack = build_command_stack(_fallback_trader(tmp_path, paper=False), _live_policy(), now=lambda: NOW)

    assert stack.dispatch_guard._quotes.executable_quote(265598, side="ask") is delayed
    assert isinstance(stack.dispatch_guard._quotes, TraderQuoteAuthority)
    assert _accepted_feeds_of(stack) == {"guard": LIVE_FEEDS, "liquidity": LIVE_FEEDS}
    assert alpaca.built == [] and alpaca.requests == []


@pytest.mark.parametrize(("key_id", "secret"), [("", "key-secret"), ("key-id", ""), ("  ", "  ")])
def test_paper_with_the_setting_and_blank_keys_fails_loudly(tmp_path, alpaca, key_id, secret):
    from trader.trading.command_stack import CommandStackConfigurationError, build_command_stack

    with pytest.raises(CommandStackConfigurationError) as exc:
        build_command_stack(_fallback_trader(tmp_path, key_id=key_id, secret=secret), _policy(),
                            now=lambda: NOW)
    assert exc.value.code == "QUOTE_FALLBACK_KEYS_MISSING"
    assert "key-id" not in str(exc.value) and "key-secret" not in str(exc.value)


@pytest.mark.parametrize("setting", ["alpaca", "ALPACA_IEX", "iex"])
def test_an_unknown_setting_fails_loudly(tmp_path, alpaca, setting):
    from trader.trading.command_stack import CommandStackConfigurationError, build_command_stack

    with pytest.raises(CommandStackConfigurationError) as exc:
        build_command_stack(_fallback_trader(tmp_path, setting=setting), _policy(), now=lambda: NOW)
    assert exc.value.code == "QUOTE_FALLBACK_INVALID"


def test_the_automated_intent_evidence_gets_the_iex_set(tmp_path, alpaca):
    from trader.trading.command_stack import build_command_stack

    trader = _fallback_trader(tmp_path)
    trader.automation_enabled = True
    trader.automation_live_enabled = False
    trader.automation_strategy_name = "qualified-paper-strategy"
    trader.automation_public_key_ring_path = _automation_key_ring(tmp_path)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    (tmp_path / "artifacts").mkdir()

    stack = build_command_stack(trader, _policy(), now=lambda: NOW)

    evidence = stack.automated_intent_service._approval_factory.__self__
    assert evidence._accepted_feeds == IEX_FEEDS
    assert evidence._quotes is stack.dispatch_guard._quotes


def test_the_hot_armed_automated_intent_evidence_gets_the_iex_set(tmp_path, alpaca):
    from trader.trading.command_stack import build_command_stack

    trader = _fallback_trader(tmp_path)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)
    stack.paper_hot_arm.attach_registry(build_production_registry(
        trader, make_identities()["trader"], command_stack=stack))
    stack.paper_hot_arm.trader_commit(
        strategy_name="qualified-paper-strategy", artifact_id="artifact-test-1",
        artifact_bundle_path=str(tmp_path / "bundle"),
        public_key_ring_path=_automation_key_ring(tmp_path),
    )

    assert stack.automated_intent_service._approval_factory.__self__._accepted_feeds == IEX_FEEDS


def test_the_ai_paper_evidence_gets_the_iex_set(tmp_path, alpaca):
    from trader.automation.ai_paper_config import AiPaperConfig
    from trader.trading.command_stack import build_command_stack

    trader = _fallback_trader(tmp_path)
    trader.ai_paper_config = AiPaperConfig(enabled=True)
    stack = build_command_stack(trader, _policy(), now=lambda: NOW)

    assert stack.ai_paper.decisions._evidence._accepted_feeds == IEX_FEEDS
    assert stack.ai_paper.decisions._evidence._quotes is stack.dispatch_guard._quotes
