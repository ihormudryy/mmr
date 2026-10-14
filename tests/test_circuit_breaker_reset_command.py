"""Issue #181: the operator clears a tripped breaker through an audited cli-only command.

``CircuitBreaker.reset`` holds every rule (reason, readiness, reconciliation, the next session for a daily-loss
trip); these tests cover the command around it.
"""
from __future__ import annotations

import datetime as dt

import pytest

from tests.test_circuit_breaker import ACCOUNT, T0, _breaker, breaker_parts  # noqa: F401 (fixture)
from trader.messaging.production_api import (
    BREAKER_RESET_ACTION, _circuit_breaker_status_handler, _reset_circuit_breaker_action,
)
from trader.trading.command_coordinator import CommandRequest, CommandValidationError
from trader.trading.circuit_breaker import BreakerSignal


def _request(*, principal="cli", body=None, command_id="reset-1"):
    return CommandRequest(
        command_id=command_id, action=BREAKER_RESET_ACTION, account_id=ACCOUNT, target_type="account",
        target_id=ACCOUNT, expected_version=None,
        body={"reason": "oct 7 flatten reached FLAT; reviewed"} if body is None else body,
        source=principal, principal=principal,
    )


def _tripped(store, clock, *, kind="LIQUIDATION_FAILED", **ready):
    breaker = _breaker(store, clock, **ready)
    breaker.record(BreakerSignal(kind, clock[0], "account owner claimed", "k1"))
    return breaker


def test_the_operator_clears_a_tripped_breaker_and_the_command_id_is_recorded(breaker_parts):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _tripped(store, clock)
    clock[0] = T0 + dt.timedelta(hours=1)

    outcome = _reset_circuit_breaker_action(breaker)(_request())

    assert outcome["state"] == "CLEAR"
    assert outcome["reset_command_id"] == "reset-1"
    assert store.get().state == "CLEAR"


@pytest.mark.parametrize("principal", ["dashboard", "strategy", "ai_supervisor", "ai_research", "research"])
def test_only_the_cli_principal_resets_even_behind_the_rpc_allow_list(breaker_parts, principal):
    _db, _journal, store = breaker_parts
    breaker = _tripped(store, [T0])
    with pytest.raises(CommandValidationError) as refused:
        _reset_circuit_breaker_action(breaker)(_request(principal=principal))
    assert refused.value.code == "PRINCIPAL_FORBIDDEN"
    assert store.get().state == "TRIPPED"


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 201},
                                  {"reason": "ok", "extra": 1}])
def test_a_request_without_exactly_one_reason_is_refused(breaker_parts, body):
    _db, _journal, store = breaker_parts
    breaker = _tripped(store, [T0])
    with pytest.raises(CommandValidationError) as refused:
        _reset_circuit_breaker_action(breaker)(_request(body=body))
    assert refused.value.code == "BREAKER_RESET_REQUEST_INVALID"
    assert store.get().state == "TRIPPED"


@pytest.mark.parametrize("ready,reconciled,message", [
    (False, True, "readiness"),
    (True, False, "reconciliation"),
])
def test_a_refused_reset_keeps_the_breaker_tripped_and_names_why(breaker_parts, ready, reconciled, message):
    _db, _journal, store = breaker_parts
    breaker = _tripped(store, [T0], ready=ready, reconciled=reconciled)
    with pytest.raises(CommandValidationError) as refused:
        _reset_circuit_breaker_action(breaker)(_request())
    assert refused.value.code == "BREAKER_RESET_REFUSED"
    assert message in refused.value.message
    assert store.get().state == "TRIPPED"


def test_a_daily_loss_trip_is_refused_in_the_same_session(breaker_parts):
    _db, _journal, store = breaker_parts
    clock = [T0]
    breaker = _tripped(store, clock, kind="DAILY_LOSS_BREACH")
    with pytest.raises(CommandValidationError) as refused:
        _reset_circuit_breaker_action(breaker)(_request())
    assert refused.value.code == "BREAKER_RESET_REFUSED"
    assert "next session" in refused.value.message


def test_the_status_read_shows_the_trip_reason(breaker_parts):
    _db, _journal, store = breaker_parts
    breaker = _tripped(store, [T0])
    status = _circuit_breaker_status_handler(breaker)(None)
    assert status["state"] == "TRIPPED"
    assert status["reason_code"] == "LIQUIDATION_FAILED"
    assert status["account_id"] == ACCOUNT


# -- through a real coordinator and ledger: the reset's own row is in flight while it runs ----------------

class _NoNonces:
    def consume_in_tx(self, conn, nonce, request):
        return True


@pytest.fixture()
def ledger_world(tmp_path):
    from trader.data.circuit_breaker_store import CircuitBreakerStore, apply_circuit_breaker_migration
    from trader.data.domain_journal import DomainJournal
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.trading.command_coordinator import (
        CommandAudit, CommandLedger, TradingCommandCoordinator, apply_command_ledger_migration,
    )
    from trader.trading.circuit_breaker import CircuitBreaker

    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_circuit_breaker_migration(migrator)
    ledger = CommandLedger(journal)
    store = CircuitBreakerStore(journal, ACCOUNT)
    store.seed(T0)
    now = T0 + dt.timedelta(hours=1)
    breaker = CircuitBreaker(
        store, now=lambda: now, reset_ready=lambda: True, session_key=lambda value: value.date().isoformat(),
        reconciliation_complete=lambda exclude: not ledger.unresolved_for_account(ACCOUNT, exclude_command_id=exclude),
    )
    breaker.record(BreakerSignal("LIQUIDATION_FAILED", T0, "account owner claimed", "k1"))
    coordinator = TradingCommandCoordinator(journal=journal, ledger=ledger, audit=CommandAudit(journal),
                                            nonces=_NoNonces(), now=lambda: now)
    coordinator.register_action(BREAKER_RESET_ACTION, _reset_circuit_breaker_action(breaker),
                                requires_preflight=False)
    return coordinator, ledger, store


def test_the_reset_is_not_blocked_by_its_own_ledger_row(ledger_world):
    coordinator, _ledger, store = ledger_world
    receipt = coordinator.execute(_request())
    assert (receipt.state, receipt.error_code) == ("RESOLVED", None)
    assert store.get().state == "CLEAR"
    assert store.get().reset_command_id == "reset-1"


def test_another_unresolved_command_still_refuses_the_reset(ledger_world):
    coordinator, ledger, store = ledger_world
    ledger.insert_for_test("pause-9", state="OUTCOME_UNKNOWN", updated_at=T0, created_at=T0, account_id=ACCOUNT,
                           action="pause_trading", target_type="account", target_id=ACCOUNT, outcome=None)
    receipt = coordinator.execute(_request())
    assert (receipt.state, receipt.error_code) == ("REJECTED", "BREAKER_RESET_REFUSED")
    assert store.get().state == "TRIPPED"
