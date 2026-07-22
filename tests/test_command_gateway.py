import threading

import pytest

from trader.domain.commands import CommandReceipt
from trader.messaging.typed_rpc import TypedRpcRemoteError
from web.command_center.gateway import (
    DashboardCommandGateway,
    GatewayError,
    PreflightTicket,
)


class FakeTypedClient:
    def __init__(self):
        self.calls = []
        self.response = {
            "command_id": "c-1", "correlation_id": "c-1", "state": "RECEIVED",
            "outcome": None, "error_code": None, "retryable": False,
        }
        self.raise_exc: Exception | None = None
        self.closed = False

    def call(self, method, body, response_model):
        self.calls.append((method, body))
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.response, command_id=body.get("command_id", "c-1"))

    def close(self):
        self.closed = True


@pytest.fixture()
def fake():
    return FakeTypedClient()


@pytest.fixture()
def gateway(fake):
    return DashboardCommandGateway(client_factory=lambda: fake, timeout_s=0.5)


def test_execute_returns_receipt(gateway, fake):
    receipt = gateway.execute("approve_proposal", {"command_id": "cmd-9", "proposal_id": 7})
    assert isinstance(receipt, CommandReceipt)
    assert receipt.command_id == "cmd-9"
    assert receipt.state == "RECEIVED"
    assert fake.calls == [("approve_proposal", {"command_id": "cmd-9", "proposal_id": 7})]


def test_rejected_receipt_raises_gateway_error(gateway, fake):
    fake.response = {
        "command_id": "cmd-9", "correlation_id": "cmd-9", "state": "REJECTED",
        "outcome": {"message": "new trading is paused for DU123"},
        "error_code": "TRADING_PAUSED", "retryable": False,
    }
    with pytest.raises(GatewayError) as exc:
        gateway.execute("create_proposal", {"command_id": "cmd-9"})
    assert exc.value.code == "TRADING_PAUSED"
    assert exc.value.message == "new trading is paused for DU123"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-9"


def test_rejected_without_outcome_message_uses_error_code(gateway, fake):
    fake.response = {
        "command_id": "cmd-9", "correlation_id": "cmd-9", "state": "REJECTED",
        "outcome": None, "error_code": "QUOTE_UNAVAILABLE", "retryable": False,
    }
    with pytest.raises(GatewayError) as exc:
        gateway.execute("create_proposal", {"command_id": "cmd-9"})
    assert exc.value.code == "QUOTE_UNAVAILABLE"
    assert exc.value.message == "QUOTE_UNAVAILABLE"


def test_remote_error_maps_to_stable_contract(gateway, fake):
    fake.raise_exc = TypedRpcRemoteError(code="VERSION_CONFLICT", message="revision 4 expected 3")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("approve_proposal", {"command_id": "cmd-9", "expected_version": 3})
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-9"


def test_timeout_maps_to_outcome_unknown_and_resets_client(fake):
    made = []

    def factory():
        made.append(FakeTypedClient() if made else fake)
        return made[-1]

    gateway = DashboardCommandGateway(client_factory=factory, timeout_s=0.5)
    fake.raise_exc = TimeoutError("no reply in 0.5s")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("cancel_order", {"command_id": "cmd-2", "order_entity_id": "o-1"})
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert exc.value.retryable is False
    assert exc.value.correlation_id == "cmd-2"
    assert fake.closed  # stale DEALER identity discarded
    # next call builds a fresh client
    gateway.get_command("cmd-2")
    assert len(made) == 2


def test_connection_error_is_retryable(gateway, fake):
    fake.raise_exc = ConnectionError("command socket down")
    with pytest.raises(GatewayError) as exc:
        gateway.execute("reject_proposal", {"command_id": "cmd-3", "proposal_id": 7})
    assert exc.value.code == "COMMAND_CHANNEL_DOWN"
    assert exc.value.retryable is True


def test_preflight_parses_ticket(gateway, fake):
    fake.response = {
        "command_id": "cmd-4", "nonce": "n-1", "expires_at": "2026-07-15T13:42:47Z",
        "summary": {"side": "BUY", "instrument": "AAPL", "drift_bps": 12.0,
                    "warnings": [], "account_id": "U1234567", "account_mode": "live",
                    "quantity": 10, "notional": 2350.0, "order_type": "MARKET",
                    "latest_price": 235.0},
    }
    ticket = gateway.preflight({"command_id": "cmd-4", "action": "approve_proposal",
                                "params": {"proposal_id": 7}, "expected_version": 3,
                                "session_fingerprint": "fp"})
    assert isinstance(ticket, PreflightTicket)
    assert ticket.nonce == "n-1"
    assert ticket.summary["account_mode"] == "live"
    assert fake.calls[0][0] == "preflight_command"


def test_gateway_serializes_calls_with_its_own_lock(gateway):
    # The command serialization lock now lives on the gateway's dedicated
    # command-only TraderLink (never shared with the read/feed links).
    assert isinstance(gateway._link._lock, type(threading.Lock()))
