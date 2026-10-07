"""SP1 Plan 5 Task 8: GET /api/scoreboard on the command center, over a fake typed query client."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cc_fakes import NullBridge, NullQuotePlane
from trader.messaging.typed_rpc import TypedRpcRemoteError
from web.app import create_app
from web.command_center import CommandCenter, CommandCenterConfig
from web.command_center.session import DashboardCredentials

TOKEN = "scoreboard-test-token"
SECRET = b"s" * 64
REPORT = {"label": "PAPER", "account": {"sessions": 0}}


class FakeQuery:
    def __init__(self, result=REPORT, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def call(self, method, body, _type, timeout=None):
        self.calls.append((method, body, timeout))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def build(monkeypatch):
    class EmptyManageClient:
        def trader_query(self, method, body=None):
            return {"universes": []} if method == "list_universes" else {}

        def strategy_query(self, method, body=None):
            return {"strategies": []} if method == "list_strategies" else {}

    monkeypatch.setattr("web.app.get_manage_client", lambda: EmptyManageClient())

    def make(query, login=True):
        cc = CommandCenter(
            CommandCenterConfig(),
            credentials_loader=lambda: DashboardCredentials(token=TOKEN, session_secret=SECRET,
                                                            legacy_alias_used=False),
            query_client_factory=lambda: None, feed_client_factory=lambda: None,
            bridge_factory=lambda *a, **k: NullBridge(), quote_plane_factory=lambda loop, deliver: NullQuotePlane())
        cc._query_client = query
        client = TestClient(create_app(cc))
        if login:
            assert client.post("/session", data={"token": TOKEN}).status_code in (200, 303)
        return client
    return make


def test_requires_a_session(build):
    assert build(FakeQuery(), login=False).get("/api/scoreboard").status_code == 401


def test_returns_the_trader_report_as_is(build):
    query = FakeQuery()
    response = build(query).get("/api/scoreboard?experiment_id=exp-0123456789abcdef0123")
    assert response.status_code == 200 and response.json() == REPORT
    assert query.calls == [("get_scoreboard", {"experiment_id": "exp-0123456789abcdef0123"}, 8)]


def test_without_a_parameter_the_body_is_empty(build):
    query = FakeQuery()
    build(query).get("/api/scoreboard")
    assert query.calls[0][1] == {}


def test_trader_down_is_503_not_an_empty_report(build):
    response = build(None).get("/api/scoreboard")
    assert response.status_code == 503 and response.json()["error"]["code"] == "TRADER_UNAVAILABLE"


def test_permission_denied_is_403(build):
    response = build(FakeQuery(error=TypedRpcRemoteError("PERMISSION_DENIED", "no"))).get("/api/scoreboard")
    assert response.status_code == 403 and response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_other_remote_error_is_502_with_its_code(build):
    response = build(FakeQuery(error=TypedRpcRemoteError("INTERNAL_ERROR", "x"))).get("/api/scoreboard")
    assert response.status_code == 502 and response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_timeout_is_502_with_a_code(build):
    response = build(FakeQuery(error=TimeoutError("slow"))).get("/api/scoreboard")
    assert response.status_code == 502 and response.json()["error"]["code"] == "TRADER_TIMEOUT"


def test_unknown_experiment_in_the_body_is_404(build):
    query = FakeQuery(result={"label": "PAPER", "error_code": "EXPERIMENT_NOT_FOUND"})
    response = build(query).get("/api/scoreboard?experiment_id=exp-ffffffffffffffffffff")
    assert response.status_code == 404 and response.json()["error"]["code"] == "EXPERIMENT_NOT_FOUND"


@pytest.mark.parametrize("query_string", ["nope=1", "experiment_id=bad"])
def test_unknown_or_malformed_query_parameter_is_422(build, query_string):
    query = FakeQuery()
    assert build(query).get(f"/api/scoreboard?{query_string}").status_code == 422
    assert query.calls == []


def test_page_contains_the_scoreboard_tab_button_and_pane(build):
    html = build(FakeQuery()).get("/cc").text
    assert 'data-dash-tab="scoreboard"' in html and 'id="dash-scoreboard"' in html
    assert '/static/command_center_scoreboard.js' in html
