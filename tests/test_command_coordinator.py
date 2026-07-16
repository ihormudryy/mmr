"""[M1-F3] Task 3 — command ledger, audit, and TradingCommandCoordinator."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import ProposalRepository, apply_proposal_authority_migration
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import RejectProposalRequest, build_production_registry
from trader.messaging.typed_rpc import HmacServiceAuthenticator
from trader.trading.command_coordinator import (
    CommandLedger,
    CommandRequest,
    CommandValidationError,
    IllegalCommandTransition,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
    canonical_request_hash,
)
from trader.trading.proposal_command_service import ExecutableQuote, ProposalCommandService

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


def _request(command_id="cmd-1", action="reject_proposal", body=None, nonce=None, account_id="DU111111"):
    return CommandRequest(
        command_id=command_id, action=action, account_id=account_id,
        target_type="proposal", target_id="7",
        expected_version=None, body=body or {"proposal_id": 7, "reason": "x"},
        source="dashboard", preflight_nonce=nonce)


class FakeNonceGate:
    """Test double for ``PreflightNonceGate``: each nonce is single-use."""

    def __init__(self):
        self._consumed: set[str] = set()
        self._fail_all = False

    def fail_all(self) -> None:
        self._fail_all = True

    def consume_in_tx(self, conn, nonce, request) -> bool:
        if self._fail_all or not nonce or nonce in self._consumed:
            return False
        self._consumed.add(nonce)
        return True


class FakeCommandAudit:
    """Test double for ``CommandAudit``: can be told to fail the next write."""

    def __init__(self):
        self._fail_next = False
        self.records: list[str] = []

    def fail_next(self) -> None:
        self._fail_next = True

    def record_in_tx(self, conn, request, **_kwargs) -> None:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("audit sink unavailable")
        self.records.append(request.command_id)


@pytest.fixture
def db(tmp_path):
    return DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))


@pytest.fixture
def journal(db):
    migrator = SchemaMigrator(db)
    j = DomainJournal(db)
    j.migrate(migrator)
    apply_command_ledger_migration(migrator)
    return j


@pytest.fixture
def ledger(journal):
    return CommandLedger(journal)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def coordinator(journal, ledger):
    coord = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=FakeCommandAudit(),
        nonces=FakeNonceGate(), now=lambda: NOW,
    )
    return coord


# ---------------------------------------------------------------------------
# Command-ledger ordering
# ---------------------------------------------------------------------------

def test_exact_retry_returns_recorded_state_before_nonce_validation(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=True)
    request = _request(action="noop", nonce="nonce-1")
    first = coordinator.execute(request)
    assert first.state == "RESOLVED"
    # The nonce is consumed. An exact retry must return the recorded outcome
    # WITHOUT re-validating the nonce (spec §9.1).
    coordinator._nonces.fail_all()
    retry = coordinator.execute(request)
    assert retry.state == "RESOLVED" and retry.outcome == first.outcome


def test_same_command_id_with_different_payload_is_a_conflict(coordinator):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop", body={"proposal_id": 7, "reason": "x"}))
    conflict = coordinator.execute(_request(action="noop", body={"proposal_id": 8, "reason": "x"}))
    assert conflict.error_code == "COMMAND_CONFLICT" and conflict.retryable is False


def test_ledger_row_exists_before_validation_runs(coordinator, ledger):
    seen = {}

    def handler(cmd):
        seen["row"] = ledger.get(cmd.command_id)
        raise CommandValidationError("RISK_REJECTED", "concentration too high")

    coordinator.register_action("noop", handler, requires_preflight=False)
    receipt = coordinator.execute(_request(action="noop"))
    assert seen["row"].state == "RECEIVED"        # insert precedes validation
    assert receipt.state == "REJECTED" and receipt.error_code == "RISK_REJECTED"


def test_audit_write_failure_fails_closed(coordinator):
    dispatched = []
    coordinator.register_action("noop", lambda cmd: dispatched.append(cmd) or {}, requires_preflight=False)
    coordinator._audit.fail_next()
    receipt = coordinator.execute(_request(action="noop"))
    assert receipt.error_code == "AUDIT_UNAVAILABLE" and receipt.state == "REJECTED"
    assert dispatched == []                        # nothing reached the handler
    assert coordinator._ledger.get("cmd-1") is None  # nothing committed at all


def test_nonce_failure_leaves_no_ledger_row(coordinator, ledger):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=True)
    receipt = coordinator.execute(_request(action="noop", nonce=None))
    assert receipt.error_code == "PREFLIGHT_REQUIRED" and receipt.state == "REJECTED"
    assert ledger.get("cmd-1") is None


def test_command_transitions_journal_command_updated(coordinator, journal):
    coordinator.register_action("noop", lambda cmd: {"ok": True}, requires_preflight=False)
    coordinator.execute(_request(action="noop"))
    kinds = [e.event_type for e in journal.read_after(0, 100)]
    assert kinds == ["command.updated", "command.updated"]   # RECEIVED, RESOLVED


def test_illegal_transition_raises(ledger, journal, now):
    with pytest.raises(IllegalCommandTransition):
        conn = journal.connect()
        ledger.transition_in_tx(conn, "does-not-exist", "RECEIVED", "RESOLVED")


def test_colon_bearing_command_id_is_rejected():
    with pytest.raises(ValueError, match="colon|:"):
        _request(command_id="bad:id")


def test_canonical_request_hash_ignores_source_and_nonce():
    a = _request(action="noop", nonce="n1")
    b = CommandRequest(
        command_id=a.command_id, action=a.action, account_id=a.account_id,
        target_type=a.target_type, target_id=a.target_id,
        expected_version=a.expected_version, body=a.body,
        source="cli", preflight_nonce="n2",
    )
    assert canonical_request_hash(a) == canonical_request_hash(b)


# ---------------------------------------------------------------------------
# Retention: purge terminal, never OUTCOME_UNKNOWN
# ---------------------------------------------------------------------------

def test_retention_purges_terminal_but_never_unknown(ledger, now):
    ledger.insert_for_test("old-resolved", state="RESOLVED", updated_at=now - dt.timedelta(days=31))
    ledger.insert_for_test("old-unknown", state="OUTCOME_UNKNOWN", updated_at=now - dt.timedelta(days=31))
    assert ledger.purge_expired(now) == 1
    assert ledger.get("old-resolved") is None
    assert ledger.get("old-unknown") is not None


def test_retention_keeps_recent_terminal_rows(ledger, now):
    ledger.insert_for_test("recent-resolved", state="RESOLVED", updated_at=now - dt.timedelta(days=1))
    assert ledger.purge_expired(now) == 0
    assert ledger.get("recent-resolved") is not None


# ---------------------------------------------------------------------------
# Production registry: no risk-limit admin / bypass on the command socket
# ---------------------------------------------------------------------------

class _FakeIB:
    def accountValues(self):
        return []

    def managedAccounts(self):
        return ["DU111111"]


class _FakeClient:
    def __init__(self):
        self.ib = _FakeIB()


class _FakeTrader:
    def __init__(self):
        self.ib_account = "DU111111"
        self.client = _FakeClient()

    def status(self) -> dict:
        return {"ib_connected": True, "ib_upstream_connected": True}


@pytest.fixture
def authority(tmp_path):
    db_ = DuckDBConnection.get_instance(str(tmp_path / "journal2.duckdb"))
    migrator = SchemaMigrator(db_)
    journal_ = DomainJournal(db_)
    journal_.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    repository = ProposalRepository(journal_)
    ledger_ = CommandLedger(journal_)

    quotes = SimpleNamespace(executable_quote=lambda conid, side: ExecutableQuote(
        conid=265598, side="ask", price=210.0, market_timestamp=NOW,
        feed_type="live", session_state="continuous",
    ) if conid == 265598 else None)
    universe = SimpleNamespace(resolve_conid=lambda conid: SimpleNamespace(
        conId=conid, symbol="AAPL", primaryExchange="NASDAQ", secType="STK",
        exchange="SMART", currency="USD",
    ) if conid == 265598 else None)
    risk_gate = SimpleNamespace(check_instrument=lambda **_kw: SimpleNamespace(approved=True, reason=""))

    proposal_service = ProposalCommandService(
        repository=repository, journal=journal_, risk_gate=risk_gate, quotes=quotes,
        universe=universe, account_id="DU111111", account_mode="paper", now=lambda: NOW,
    )
    coordinator = TradingCommandCoordinator(
        journal=journal_, ledger=ledger_, audit=FakeCommandAudit(), nonces=FakeNonceGate(),
        now=lambda: NOW,
    )
    return SimpleNamespace(
        journal=journal_, repository=repository, ledger=ledger_,
        proposal_service=proposal_service, coordinator=coordinator,
    )


@pytest.fixture
def production_registry(authority):
    authenticator = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    return build_production_registry(
        _FakeTrader(), authenticator,
        command_coordinator=authority.coordinator,
        proposal_service=authority.proposal_service,
        proposal_repository=authority.repository,
    )


def test_no_risk_limit_admin_or_bypass_on_the_command_socket(production_registry):
    for method in ("set_risk_limits", "place_order_simple", "place_expressive_order"):
        assert not production_registry.contains("command", method)
    with pytest.raises(ValidationError):
        RejectProposalRequest(command_id="c", proposal_id=7, reason="x", skip_risk_gate=True)


def test_command_authority_methods_are_registered(production_registry):
    assert production_registry.contains("command", "create_proposal")
    assert production_registry.contains("command", "reject_proposal")
    assert production_registry.contains("query", "get_command")
    assert production_registry.contains("query", "get_proposal")
    assert production_registry.contains("query", "list_proposals")


def test_create_and_reject_proposal_over_the_registry(production_registry):
    create_reg = production_registry.resolve("command", "create_proposal")
    from trader.messaging.production_api import CreateProposalRequest
    parsed = CreateProposalRequest(command_id="cmd-create-1", conid=265598, action="BUY", quantity=10)
    receipt = create_reg.handler(parsed)
    assert receipt["state"] == "RESOLVED"
    proposal_id = receipt["outcome"]["id"]

    reject_reg = production_registry.resolve("command", "reject_proposal")
    from trader.messaging.production_api import RejectProposalRequest as RPR
    parsed_reject = RPR(command_id="cmd-reject-1", proposal_id=proposal_id, reason="changed thesis")
    reject_receipt = reject_reg.handler(parsed_reject)
    assert reject_receipt["state"] == "RESOLVED"
    assert reject_receipt["outcome"]["status"] == "REJECTED"

    get_proposal_reg = production_registry.resolve("query", "get_proposal")
    from trader.messaging.production_api import GetProposalRequest
    fetched = get_proposal_reg.handler(GetProposalRequest(proposal_id=proposal_id))
    assert fetched["status"] == "REJECTED"

    get_command_reg = production_registry.resolve("query", "get_command")
    from trader.messaging.production_api import GetCommandRequest
    command_state = get_command_reg.handler(GetCommandRequest(command_id="cmd-create-1"))
    assert command_state["state"] == "RESOLVED"
