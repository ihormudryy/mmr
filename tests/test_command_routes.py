"""[M1-C] Task 3 -- command router: proposal create + position close.

Source-vs-brief drift (see the module docstring in
``web/command_center/routes_commands.py`` and
``.superpowers/sdd/m1c-task-3-report.md`` for the full account): the plan
this test was drafted against assumed a ``DashboardSession``/
``require_session``/``session_csrf_token`` trio importable from
``web.command_center.session`` and a rich ``{"proposal": {...}}``/
``{"close": {...}}`` wire shape (symbol, order_type, limit_price, bracket,
stop-loss, trailing-stop, TIF). Neither exists in the landed [M1-R] session
module or the frozen [M1-F3] ``create_proposal`` wire contract
(``CreateProposalRequest``), which is a flat, MARKET-only
``{command_id, conid, action, quantity, amount, reasoning, confidence,
thesis, group, max_price_drift_bps, preflight_nonce}`` shape (conId, not
symbol). This test file exercises the routes against those REAL shapes.
"""
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.command_center.routes_commands as routes_commands
from trader.domain.commands import CommandReceipt
from web.command_center.flags import CommandFlags
from web.command_center.gateway import GatewayError, PreflightTicket
from web.command_center.routes_commands import install_command_routes, require_session

CMD_ID = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d7e8f"
SESSION = "epoch-abc.1752000000.deadbeefsig"
HEADERS = {"X-CSRF-Token": "test-csrf", "Origin": "http://testserver",
           "Host": "testserver"}


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.error: GatewayError | None = None
        self.receipt = CommandReceipt("", "corr-1", "RECEIVED", None, None, False)
        self.ticket = PreflightTicket(CMD_ID, "n-1", "2026-07-15T13:42:47Z", {})

    def execute(self, method, body):
        self.calls.append((method, body))
        if self.error is not None:
            raise self.error
        return replace(self.receipt, command_id=body["command_id"])

    def preflight(self, body):
        self.calls.append(("preflight_command", body))
        if self.error is not None:
            raise self.error
        return self.ticket

    def get_command(self, command_id):
        self.calls.append(("get_command", {"command_id": command_id}))
        return replace(self.receipt, command_id=command_id, state="SUBMITTED")


@pytest.fixture(autouse=True)
def _fixed_csrf(monkeypatch):
    monkeypatch.setattr(routes_commands, "session_csrf_token", lambda s: "test-csrf")


@pytest.fixture()
def gateway():
    return FakeGateway()


def make_client(gateway, flags=CommandFlags(True, False, None, None)):
    app = FastAPI()
    app.state.command_flags = flags
    app.state.command_gateway = gateway
    install_command_routes(app)
    app.dependency_overrides[require_session] = lambda: SESSION
    return TestClient(app)


def _proposal_body(**overrides):
    body = {"command_id": CMD_ID, "conid": 265598, "action": "BUY",
            "reasoning": "breakout"}
    body.update(overrides)
    return body


def test_create_returns_202_receipt_and_forwards_typed_body(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 202
    assert r.json() == {"command_id": CMD_ID, "correlation_id": "corr-1",
                        "state": "RECEIVED"}
    method, body = gateway.calls[0]
    assert method == "create_proposal"
    assert body["command_id"] == CMD_ID
    assert body["conid"] == 265598
    assert body["action"] == "BUY"
    assert body["reasoning"] == "breakout"


def test_empty_quantity_and_amount_means_auto_sizing(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 202
    body = gateway.calls[0][1]
    assert body["quantity"] is None and body["amount"] is None


@pytest.mark.parametrize("bad, match", [
    (dict(quantity=10, amount=5000.0), "quantity OR amount"),
    (dict(confidence=1.5), "confidence"),
    (dict(action="HOLD"), "action"),
    (dict(conid=-1), "conid"),
    (dict(quantity=-5), "quantity"),
    (dict(unknown_field=1), "unknown_field"),
])
def test_create_body_validation(gateway, bad, match):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(**bad),
                    headers=HEADERS)
    assert r.status_code == 422
    assert match.lower() in str(r.json()).lower()
    assert gateway.calls == []


def test_missing_quote_refusal_is_surfaced_verbatim(gateway):
    gateway.error = GatewayError("QUOTE_MISSING",
                                 "no fresh quote for AAPL; proposal refused",
                                 retryable=True, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 409
    assert r.json() == {"code": "QUOTE_MISSING",
                        "message": "no fresh quote for AAPL; proposal refused",
                        "retryable": True, "correlation_id": CMD_ID}


def test_pause_gate_error_is_surfaced_verbatim(gateway):
    gateway.error = GatewayError("TRADING_PAUSED",
                                 "new trading is paused for DU123",
                                 retryable=False, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 409
    assert r.json()["message"] == "new trading is paused for DU123"


def test_close_position_builds_reducing_payload(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/positions/DU123/265598/close",
                    json={"command_id": CMD_ID, "action": "SELL", "quantity": 40.0,
                          "reasoning": "trim"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "create_proposal"
    assert body == {"command_id": CMD_ID, "conid": 265598, "action": "SELL",
                    "quantity": 40.0, "reasoning": "trim"}
    assert "account" not in body  # not part of the frozen wire contract


def test_commands_disabled_returns_403_before_gateway(gateway):
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "COMMANDS_DISABLED"
    assert gateway.calls == []


def test_missing_csrf_header_is_rejected(gateway):
    client = make_client(gateway)
    bad_headers = dict(HEADERS)
    bad_headers.pop("X-CSRF-Token")
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=bad_headers)
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_REJECTED"
    assert gateway.calls == []


def test_cross_origin_mutation_is_rejected(gateway):
    client = make_client(gateway)
    bad_headers = dict(HEADERS, Origin="http://evil.example")
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=bad_headers)
    assert r.status_code == 403
    assert r.json()["code"] == "ORIGIN_REJECTED"
    assert gateway.calls == []


def test_csrf_token_endpoint_returns_session_bound_token(gateway, monkeypatch):
    monkeypatch.undo()  # use the real session_csrf_token, not the fixture's stub
    client = make_client(gateway)
    r = client.get("/api/commands/csrf-token", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["csrf_token"] == routes_commands.session_csrf_token(SESSION)


def test_get_command_returns_receipt_for_reconciliation(gateway):
    client = make_client(gateway)
    r = client.get(f"/api/commands/{CMD_ID}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["state"] == "SUBMITTED"


def test_get_command_disabled_gateway_returns_stable_error(gateway):
    """No `app.state.command_gateway` at all (commands disabled at startup,
    so `web/app.py` never constructs one) must not surface a raw
    AttributeError -- it degrades to the same stable COMMANDS_DISABLED
    envelope the mutation routes use."""
    app = FastAPI()
    app.state.command_flags = CommandFlags(False, False, None, None)
    install_command_routes(app)
    app.dependency_overrides[require_session] = lambda: SESSION
    client = TestClient(app)
    r = client.get(f"/api/commands/{CMD_ID}", headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "COMMANDS_DISABLED"
