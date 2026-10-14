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
