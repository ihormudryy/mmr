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
from types import SimpleNamespace

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
LIVE_FLAGS = CommandFlags(True, True, "U1234567", 25000.0)


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


def make_client(gateway, flags=CommandFlags(True, False, None, None), session=SESSION):
    """``session`` defaults to the fixed authenticated ``SESSION`` string
    (unchanged behaviour for every pre-existing caller). [M1-C] Task 7's
    security-gate matrix (``tests/test_command_security_gate.py``) also
    needs an UNAUTHENTICATED client to prove every command route enforces
    session auth: ``session=None`` skips the ``require_session`` override
    entirely and leaves ``app.state.command_center`` unset (``None``), the
    same "no session manager wired" shape ``require_session`` in
    ``routes_commands.py`` already handles explicitly -- it fails closed
    with 401 ``SESSION_REQUIRED`` rather than raising an unhandled
    ``AttributeError`` off the bare ``SimpleNamespace`` stand-in below.
    """
    app = FastAPI()
    app.state.command_flags = flags
    if session is None:
        app.state.command_center = None
    else:
        # [M1-C] Task 3 fix (I-1/M-6): the gateway now lives on
        # `command_center.command_gateway`, not a bare `app.state.command_gateway`
        # -- a plain namespace stands in for the real `CommandCenter` here since
        # these tests only ever read that one attribute off it.
        app.state.command_center = SimpleNamespace(command_gateway=gateway)
    install_command_routes(app)
    if session is not None:
        app.dependency_overrides[require_session] = lambda: session
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


@pytest.mark.parametrize("bad_conid", [0, -1, -265598])
def test_close_position_rejects_non_positive_conid(gateway, bad_conid):
    """M-1: the `close_position` route's `conid` path param had no
    positive-integer bound, unlike `CreateProposalBody.conid = Field(gt=0)`
    -- a non-positive conid must be a 422 at this web layer (consistent
    with proposal-create) and must never reach the gateway."""
    client = make_client(gateway)
    r = client.post(f"/api/commands/positions/DU123/{bad_conid}/close",
                    json={"command_id": CMD_ID, "action": "SELL",
                          "quantity": 40.0, "reasoning": "trim"},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


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


def test_loopback_origin_aliases_accepted(gateway):
    """localhost vs 127.0.0.1 must not reject same-machine dashboard POSTs."""
    client = make_client(gateway)
    headers = dict(HEADERS)
    headers["Origin"] = "http://127.0.0.1:7424"
    headers["Host"] = "localhost:7424"
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=headers)
    assert r.status_code == 202, r.text
    assert gateway.calls


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
    """No `app.state.command_center` at all (commands disabled at startup,
    so `web/app.py` never builds a gateway) must not surface a raw
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


# ---------------------------------------------------------------------------
# [M1-C] Task 3 fix wave -- I-1 (eager gateway build regression), M-6
# (degraded gateway -> 503, not 403), M-4 (creds-missing -> 401, not 500),
# M-7 (colon-in-command_id rejected at the web layer).
# ---------------------------------------------------------------------------

def test_degraded_gateway_returns_503_not_403_for_create_proposal(gateway):
    """M-6: commands ARE enabled but the gateway never came up (e.g. the
    command center degraded to inert at startup -- I-1) -- this must be a
    distinct, retryable 503, not the 403 COMMANDS_DISABLED the feature-off
    path returns."""
    app = FastAPI()
    app.state.command_flags = CommandFlags(True, False, None, None)
    app.state.command_center = SimpleNamespace(command_gateway=None)
    install_command_routes(app)
    app.dependency_overrides[require_session] = lambda: SESSION
    client = TestClient(app)
    r = client.post("/api/commands/proposals", json=_proposal_body(), headers=HEADERS)
    assert r.status_code == 503
    assert r.json()["code"] == "COMMAND_GATEWAY_UNAVAILABLE"
    assert r.json()["retryable"] is True


def test_degraded_gateway_returns_503_for_get_command(gateway):
    app = FastAPI()
    app.state.command_flags = CommandFlags(True, False, None, None)
    app.state.command_center = SimpleNamespace(command_gateway=None)
    install_command_routes(app)
    app.dependency_overrides[require_session] = lambda: SESSION
    client = TestClient(app)
    r = client.get(f"/api/commands/{CMD_ID}", headers=HEADERS)
    assert r.status_code == 503
    assert r.json()["code"] == "COMMAND_GATEWAY_UNAVAILABLE"


def test_missing_credentials_on_require_session_is_401_not_500():
    """M-4: `require_session` must catch `CredentialConfigError` (raised by
    `CommandCenter.ensure_session_manager` on missing dashboard credentials,
    via the `require_session` property) and surface the stable 401
    SESSION_REQUIRED, not an unhandled 500."""
    from web.command_center.session import CredentialConfigError

    class _BrokenCenter:
        @property
        def require_session(self):
            raise CredentialConfigError("dashboard login token missing")

    app = FastAPI()
    app.state.command_flags = CommandFlags(True, False, None, None)
    app.state.command_center = _BrokenCenter()
    install_command_routes(app)
    client = TestClient(app)
    r = client.get(f"/api/commands/{CMD_ID}", headers=HEADERS)
    assert r.status_code == 401
    assert r.json()["code"] == "SESSION_REQUIRED"


@pytest.mark.parametrize("bad_id", [
    "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d:8f1",   # colon, still exactly 36 chars
    "not-a-uuid-shaped-value-at-all-36789",    # 36 chars, wrong shape
])
def test_create_proposal_rejects_malformed_command_id(gateway, bad_id):
    """M-7: a `:`-bearing (or otherwise malformed) command_id must be
    rejected at THIS web layer, not one hop later at the coordinator."""
    assert len(bad_id) == 36
    client = make_client(gateway)
    r = client.post("/api/commands/proposals",
                    json=_proposal_body(command_id=bad_id), headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_close_position_rejects_malformed_command_id(gateway):
    client = make_client(gateway)
    bad_id = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d:8f1"
    r = client.post("/api/commands/positions/DU123/265598/close",
                    json={"command_id": bad_id, "action": "SELL",
                          "quantity": 40.0, "reasoning": "trim"},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_get_command_rejects_colon_in_path_param(gateway):
    """M-7 applies to the path-param `command_id` too -- previously
    unvalidated entirely (not even length-checked)."""
    client = make_client(gateway)
    bad_id = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d:8f1"
    r = client.get(f"/api/commands/{bad_id}", headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


class TestGatewayLifespanWiring:
    """I-1: the command gateway must be built inside
    `CommandCenter._start_or_degrade` (the SAME degrade-tolerant try/except
    that already builds the bridge + quote plane), never eagerly at
    `create_app()` time -- a bad/missing service HMAC key must degrade the
    center to inert, not crash the whole web process (re-breaking the M1-R
    Task-5 ops-probe contract)."""

    @staticmethod
    def _center(monkeypatch, *, commands_enabled, command_gateway_factory):
        from cc_fakes import NullBridge, NullQuotePlane

        from web.command_center import CommandCenter, CommandCenterConfig
        from web.command_center.session import DashboardCredentials

        monkeypatch.setenv("MMR_DILL_STRICT", "1")
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("UVICORN_WORKERS", raising=False)
        return CommandCenter(
            CommandCenterConfig(),
            credentials_loader=lambda: DashboardCredentials(
                token="t", session_secret=b"s" * 32, legacy_alias_used=False),
            query_client_factory=lambda: None,
            feed_client_factory=lambda: None,
            bridge_factory=lambda *a, **k: NullBridge(),
            quote_plane_factory=lambda *a, **k: NullQuotePlane(),
            commands_enabled=commands_enabled,
            command_gateway_factory=command_gateway_factory,
        )

    @pytest.mark.asyncio
    async def test_gateway_build_failure_degrades_whole_center_not_abort(self, monkeypatch):
        def _boom():
            raise RuntimeError("missing/invalid service HMAC key")

        # `_start_or_degrade` calls `asyncio.get_running_loop()` (to hand the
        # loop to the bridge/quote-plane factories) -- exercised here from
        # inside a running loop, same as its one real caller, the async
        # `lifespan` context manager.
        center = self._center(monkeypatch, commands_enabled=True,
                              command_gateway_factory=_boom)
        started = center._start_or_degrade()
        assert started is False
        assert center.command_gateway is None
        # Same try/except as bridge + quote plane -- a gateway failure
        # degrades the WHOLE center, not just the gateway.
        assert center.bridge is None
        assert center.quote_plane is None

    @pytest.mark.asyncio
    async def test_gateway_build_success_is_stored_on_center(self, monkeypatch):
        fake_gateway = object()
        center = self._center(monkeypatch, commands_enabled=True,
                              command_gateway_factory=lambda: fake_gateway)
        started = center._start_or_degrade()
        assert started is True
        assert center.command_gateway is fake_gateway

    @pytest.mark.asyncio
    async def test_gateway_factory_never_invoked_when_commands_disabled(self, monkeypatch):
        calls = []

        def factory():
            calls.append(1)
            return object()

        center = self._center(monkeypatch, commands_enabled=False,
                              command_gateway_factory=factory)
        started = center._start_or_degrade()
        assert started is True
        assert center.command_gateway is None
        assert calls == []


def test_create_app_survives_commands_enabled_with_missing_hmac_key(monkeypatch):
    """I-1 regression test. `build_command_gateway()` used to run EAGERLY in
    `create_app()` (outside any try/except), so DASHBOARD_COMMANDS_ENABLED=
    true + a missing/bad service HMAC key raised straight out of
    `create_app()` -- taking the whole web process, including the
    unauthenticated `/healthz`/`/readyz`/`/api/health` probes, down with it.

    RED before the fix: `webapp.create_app()` itself raises
    `ServiceHmacKeyError`. GREEN after: gateway construction lives inside the
    degrade-tolerant `CommandCenter._start_or_degrade`, so a bad/missing key
    only degrades the command center -- the app still boots and the probes
    still serve.
    """
    import web.app as webapp

    monkeypatch.setattr(webapp, "_COMMAND_FLAGS",
                        CommandFlags(True, False, None, None))
    monkeypatch.delenv("MMR_SERVICE_HMAC_KEY_FILE", raising=False)

    app = webapp.create_app()  # must not raise merely from a missing HMAC key
    with TestClient(app) as client:  # lifespan startup must not raise either
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


# ---------------------------------------------------------------------------
# [M1-C] Task 4 -- approve, reject, preflight, and the live confirmation
# ceremony.
#
# Source-vs-brief drift: `session.py` ([M1-R], out of scope for this task)
# has no `DashboardSession` dataclass -- the session identity threaded
# through every route in this module (including these new ones) is the raw
# signed cookie `str` `require_session`/`require_command_auth` already
# return (see the module docstring's drift item 1). `session_fingerprint`
# is therefore `session_fingerprint(session: str) -> str`, added to
# `session.py` as a plain module-level function keyed by a process-local
# secret (the exact same pattern Task 3 already established for
# `session_csrf_token` in this file) rather than the brief's hypothetical
# `_session_secret()` accessor for a `DashboardSession.session_id`/`.epoch`
# pair that doesn't exist in the landed session module.
# ---------------------------------------------------------------------------

def test_paper_approve_is_single_post_with_expected_version(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "approve_proposal"
    assert body == {"command_id": CMD_ID, "proposal_id": 7,
                    "expected_version": 3, "preflight_nonce": None,
                    "session_fingerprint": body["session_fingerprint"]}
    assert body["session_fingerprint"]  # opaque, non-empty


def test_live_approve_forwards_nonce_with_same_command_id(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3,
                          "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["preflight_nonce"] == "n-1"
    assert gateway.calls[0][1]["command_id"] == CMD_ID


def test_reject_is_immediate_without_expected_version(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/proposals/7/reject",
                    json={"command_id": CMD_ID, "reason": "changed thesis"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "reject_proposal"
    assert body == {"command_id": CMD_ID, "proposal_id": 7,
                    "reason": "changed thesis"}


def test_preflight_requires_live_commands_enabled(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "approve_proposal",
                          "params": {"proposal_id": 7}, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_preflight_returns_ticket_bound_to_session(gateway):
    gateway.ticket = PreflightTicket(
        CMD_ID, "n-9", "2026-07-15T13:42:47Z",
        {"side": "BUY", "instrument": "AAPL", "quantity": 10,
         "notional": 2350.0, "order_type": "MARKET", "latest_price": 235.0,
         "drift_bps": 12.0, "warnings": ["quote is 4s old"],
         "account_id": "U1234567", "account_mode": "live"})
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "approve_proposal",
                          "params": {"proposal_id": 7}, "expected_version": 3},
                    headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["nonce"] == "n-9"
    assert r.json()["summary"]["drift_bps"] == 12.0
    sent = gateway.calls[0][1]
    assert sent["session_fingerprint"]
    assert sent["action"] == "approve_proposal"


def test_expired_preflight_maps_to_410(gateway):
    gateway.error = GatewayError("PREFLIGHT_EXPIRED", "nonce expired after 30s",
                                 retryable=True, correlation_id=CMD_ID)
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3,
                          "preflight_nonce": "n-old"},
                    headers=HEADERS)
    assert r.status_code == 410
    assert r.json()["retryable"] is True


def test_session_fingerprint_is_stable_and_session_bound(gateway):
    """Direct unit coverage of the derived value itself: same session ->
    same fingerprint (so the trader can bind a nonce across the preflight ->
    approve ceremony), different sessions -> different fingerprints, and the
    fingerprint never IS the raw session string it was derived from."""
    from web.command_center.session import session_fingerprint

    fp1 = session_fingerprint(SESSION)
    fp2 = session_fingerprint(SESSION)
    assert fp1 == fp2
    assert fp1 != SESSION
    assert fp1 != session_fingerprint("epoch-abc.1752000001.otherSig")


def test_live_targeted_approve_without_live_commands_enabled_is_403(gateway):
    """[M1-C] Task 4 (I-2, deferred from the T3 review): approve EXECUTES a
    real order, unlike create_proposal/close_position (gated on
    `commands_enabled` only). A `preflight_nonce` on the wire only ever
    appears once the browser has completed the live ceremony
    (`ccIsLive(proposal.account_mode)` decides whether to run it in
    command_center.js) -- so its presence here is the signal that this
    approval targets the live account. A live-targeted approval must be
    refused with the same 403 LIVE_COMMANDS_DISABLED code `/api/preflight`
    already uses when this dashboard isn't configured for live commands,
    and must never reach the gateway."""
    client = make_client(gateway)  # paper-only (default) flags
    r = client.post("/api/commands/proposals/7/approve",
                    json={"command_id": CMD_ID, "expected_version": 3,
                          "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_preflight_action_rejects_unknown_action(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "delete_everything",
                          "params": {}, "expected_version": None},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_approve_reject_preflight_require_csrf_and_origin(gateway):
    bad_headers = dict(HEADERS)
    bad_headers.pop("X-CSRF-Token")
    for method, url, body in [
        ("post", "/api/commands/proposals/7/approve",
         {"command_id": CMD_ID, "expected_version": 3}),
        ("post", "/api/commands/proposals/7/reject",
         {"command_id": CMD_ID, "reason": ""}),
        ("post", "/api/preflight",
         {"command_id": CMD_ID, "action": "approve_proposal",
          "params": {"proposal_id": 7}, "expected_version": 3}),
    ]:
        client = make_client(gateway, flags=LIVE_FLAGS)
        r = getattr(client, method)(url, json=body, headers=bad_headers)
        assert r.status_code == 403
        assert r.json()["code"] == "CSRF_REJECTED"
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# [M1-C] Task 5 -- order cancel and cancel-all with the classification
# ceremony (spec 9.7). The SERVER (F3 Task 6's `classify_cancel` +
# `CancelCommandService`) is the real authority for whether a cancel is
# risk-reducing (entry) or risk-increasing (protective/unclassifiable) -- this
# web layer forwards through the gateway and never re-derives that itself.
# ---------------------------------------------------------------------------

def test_cancel_single_order_forwards_entity_id(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/grp-9:entry/cancel",
                    json={"command_id": CMD_ID}, headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "cancel_order"
    assert body == {"command_id": CMD_ID, "order_entity_id": "grp-9:entry",
                    "preflight_nonce": None,
                    "session_fingerprint": body["session_fingerprint"]}


def test_protective_cancel_forwards_nonce(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/orders/grp-9:stop/cancel",
                    json={"command_id": CMD_ID, "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls[0][1]["preflight_nonce"] == "n-1"


def test_cancel_all_sends_every_order_under_one_command(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/cancel-all",
                    json={"command_id": CMD_ID,
                          "order_entity_ids": ["grp-9:entry", "grp-9:stop"]},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "cancel_orders"
    assert body["order_entity_ids"] == ["grp-9:entry", "grp-9:stop"]
    # one correlation id: the coordinator expands per-order commands under it
    assert body["command_id"] == CMD_ID


def test_cancel_all_requires_at_least_one_order(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/orders/cancel-all",
                    json={"command_id": CMD_ID, "order_entity_ids": []},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_terminal_order_cancel_reports_authoritative_state(gateway):
    gateway.error = GatewayError("COMMAND_REJECTED",
                                 "order grp-9:entry already FILLED; no-op",
                                 retryable=False, correlation_id=CMD_ID)
    client = make_client(gateway)
    r = client.post("/api/commands/orders/grp-9:entry/cancel",
                    json={"command_id": CMD_ID}, headers=HEADERS)
    assert r.status_code == 409
    assert "already FILLED" in r.json()["message"]


def test_live_targeted_cancel_without_live_commands_enabled_is_403(gateway):
    """Mirrors `test_live_targeted_approve_without_live_commands_enabled_is_403`
    above: a `preflight_nonce` on the wire only ever exists once the browser
    has completed the live ceremony, so its presence is this layer's signal
    that the cancel targets the live account -- refused before the gateway
    is ever reached when this dashboard isn't configured for live commands."""
    client = make_client(gateway)  # paper-only (default) flags
    r = client.post("/api/commands/orders/grp-9:stop/cancel",
                    json={"command_id": CMD_ID, "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_cancel_routes_reject_malformed_command_id(gateway):
    bad_id = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d:8f1"
    client = make_client(gateway)
    r = client.post("/api/commands/orders/grp-9:entry/cancel",
                    json={"command_id": bad_id}, headers=HEADERS)
    assert r.status_code == 422
    r = client.post("/api/commands/orders/cancel-all",
                    json={"command_id": bad_id, "order_entity_ids": ["grp-9:entry"]},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_cancel_routes_require_csrf_and_origin(gateway):
    bad_headers = dict(HEADERS)
    bad_headers.pop("X-CSRF-Token")
    for url, body in [
        ("/api/commands/orders/grp-9:entry/cancel", {"command_id": CMD_ID}),
        ("/api/commands/orders/cancel-all",
         {"command_id": CMD_ID, "order_entity_ids": ["grp-9:entry"]}),
    ]:
        client = make_client(gateway, flags=LIVE_FLAGS)
        r = client.post(url, json=body, headers=bad_headers)
        assert r.status_code == 403
        assert r.json()["code"] == "CSRF_REJECTED"
    assert gateway.calls == []


# ---------------------------------------------------------------------------
# [M1-C] Task 6 -- strategy control (enable/disable/params) and pause/resume.
#
# Source-vs-brief drift: the plan's literal route bodies forwarded
# `expected_version` + `preflight_nonce` + `session_fingerprint` on EVERY one
# of these four commands, mirroring approve_proposal's shape. The real
# [M1-F3] request models (`trader/messaging/production_api.py`) do not:
#
# - `EnableStrategyRequest` / `DisableStrategyRequest` /
#   `UpdateStrategyParamsRequest` are `extra="forbid"` with fields
#   `{command_id, strategy_name, expected_control_revision[, params]}` --
#   NOT `expected_version`, and NO `preflight_nonce` field at all (registered
#   `requires_preflight=False, saga=True` at the coordinator; the strategy
#   control trio doesn't use the coordinator's preflight-nonce mechanism).
# - `SetTradingPauseRequest` IS `extra="forbid"` with fields `{command_id,
#   paused, expected_version, reason, preflight_nonce}` -- `expected_version`
#   matches the plan here, but there is no `account_id` field (the
#   coordinator pins its own configured account) and no `session_fingerprint`
#   field either.
#
# Forwarding `session_fingerprint`/a mismatched CAS field name to any of
# these four would 422 downstream against the real models -- so unlike
# approve_proposal/cancel_order (an ALREADY-LANDED drift this task was told
# not to repeat), these bodies below carry ONLY the fields each real model
# declares. `preflight_nonce` is still accepted on the WEB-FACING body for
# enable/params (risk-increasing, per spec 9.3: re-enabling or reparameterizing
# lets new signals fire) as this layer's own live-ceremony gate signal
# (mirrors `_reject_live_targeted_without_flag` for approve/cancel) -- it is
# just never forwarded past this layer. Disable and pause=true are
# risk-REDUCING and immediate in both modes (mirrors reject_proposal/an
# entry-leg cancel): no live-gate is applied even if a nonce is supplied.
# ---------------------------------------------------------------------------

def test_strategy_enable_forwards_control_revision(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/enable",
                    json={"command_id": CMD_ID, "expected_version": 4},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "enable_strategy"
    # Matches EnableStrategyRequest field-for-field: expected_control_revision,
    # not expected_version; no preflight_nonce/session_fingerprint (the real
    # model declares neither and is extra="forbid").
    assert body == {"command_id": CMD_ID, "strategy_name": "smi_crossover",
                    "expected_control_revision": 4}


def test_strategy_params_forwards_typed_params(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": {"EMA_PERIOD": 15}},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "update_strategy_params"
    assert body == {"command_id": CMD_ID, "strategy_name": "smi_crossover",
                    "expected_control_revision": 4,
                    "params": {"EMA_PERIOD": 15}}


def test_strategy_params_rejects_non_object_params(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": [1, 2, 3]},
                    headers=HEADERS)
    assert r.status_code == 422           # params must be a JSON object
    assert gateway.calls == []


def test_strategy_params_rejects_empty_params(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": {}},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_strategy_disable_is_immediate_even_with_a_nonce(gateway):
    """Disable is risk-reducing (spec 9.3) -- immediate in both modes, no
    live-gate. A client-supplied preflight_nonce is silently dropped (never
    forwarded -- DisableStrategyRequest declares no such field) rather than
    gated on, unlike enable/params below."""
    client = make_client(gateway)  # paper-only (default) flags
    r = client.post("/api/commands/strategies/smi_crossover/disable",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "preflight_nonce": "n-7"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "disable_strategy"
    assert body == {"command_id": CMD_ID, "strategy_name": "smi_crossover",
                    "expected_control_revision": 4}


def test_live_targeted_strategy_enable_without_live_commands_enabled_is_403(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/commands/strategies/smi_crossover/enable",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "preflight_nonce": "n-7"},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_live_strategy_enable_forwards_without_the_nonce_field(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/strategies/smi_crossover/enable",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "preflight_nonce": "n-7"},
                    headers=HEADERS)
    assert r.status_code == 202
    body = gateway.calls[0][1]
    assert "preflight_nonce" not in body
    assert body["expected_control_revision"] == 4


def test_live_targeted_strategy_params_without_live_commands_enabled_is_403(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/commands/strategies/smi_crossover/params",
                    json={"command_id": CMD_ID, "expected_version": 4,
                          "params": {"EMA_PERIOD": 15}, "preflight_nonce": "n-7"},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_pause_sets_absolute_boolean_without_account(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/pause",
                    json={"command_id": CMD_ID, "reason": "operator hold"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "pause_trading"
    assert body == {"command_id": CMD_ID, "reason": "operator hold"}
    assert "account_id" not in body      # coordinator pins its configured account
    assert "session_fingerprint" not in body


def test_pause_rejects_preflight_field_instead_of_forwarding_it(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/commands/pause",
                    json={"command_id": CMD_ID, "reason": "operator hold",
                          "preflight_nonce": "n-9"},
                    headers=HEADERS)
    assert r.status_code == 422
    assert gateway.calls == []


def test_resume_forwards_exact_revision(gateway):
    client = make_client(gateway, flags=LIVE_FLAGS)
    r = client.post("/api/commands/resume",
                    json={"command_id": CMD_ID,
                          "expected_control_revision": 12, "reason": "resume",
                          "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "resume_trading"
    assert body["expected_control_revision"] == 12
    assert body["preflight_nonce"] == "n-1"
    assert body["session_fingerprint"]


def test_resume_without_revision_is_rejected(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/resume",
                    json={"command_id": CMD_ID, "reason": "resume"},
                    headers=HEADERS)
    assert r.status_code == 422           # resume is CAS: an exact revision is mandatory
    assert gateway.calls == []


def test_live_targeted_resume_without_live_commands_enabled_is_403(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/commands/resume",
                    json={"command_id": CMD_ID,
                          "expected_control_revision": 12, "reason": "resume",
                          "preflight_nonce": "n-1"},
                    headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "LIVE_COMMANDS_DISABLED"
    assert gateway.calls == []


def test_strategy_and_pause_routes_reject_malformed_command_id(gateway):
    bad_id = "0f9b2c1a-5b7e-4c1d-9e2f-3a4b5c6d:8f1"
    client = make_client(gateway)
    for url, body in [
        ("/api/commands/strategies/smi_crossover/enable",
         {"command_id": bad_id, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/disable",
         {"command_id": bad_id, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/params",
         {"command_id": bad_id, "expected_version": 4, "params": {"EMA_PERIOD": 15}}),
        ("/api/commands/pause",
         {"command_id": bad_id, "reason": "hold"}),
        ("/api/commands/resume",
         {"command_id": bad_id, "expected_control_revision": 1, "reason": "resume"}),
    ]:
        r = client.post(url, json=body, headers=HEADERS)
        assert r.status_code == 422
    assert gateway.calls == []


def test_strategy_and_pause_routes_require_csrf_and_origin(gateway):
    bad_headers = dict(HEADERS)
    bad_headers.pop("X-CSRF-Token")
    for url, body in [
        ("/api/commands/strategies/smi_crossover/enable",
         {"command_id": CMD_ID, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/disable",
         {"command_id": CMD_ID, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/params",
         {"command_id": CMD_ID, "expected_version": 4, "params": {"EMA_PERIOD": 15}}),
        ("/api/commands/pause",
         {"command_id": CMD_ID, "reason": "hold"}),
        ("/api/commands/resume",
         {"command_id": CMD_ID, "expected_control_revision": 1, "reason": "resume"}),
    ]:
        client = make_client(gateway, flags=LIVE_FLAGS)
        r = client.post(url, json=body, headers=bad_headers)
        assert r.status_code == 403
        assert r.json()["code"] == "CSRF_REJECTED"
    assert gateway.calls == []


def test_strategy_and_pause_routes_disabled_returns_403_before_gateway(gateway):
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    for url, body in [
        ("/api/commands/strategies/smi_crossover/enable",
         {"command_id": CMD_ID, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/disable",
         {"command_id": CMD_ID, "expected_version": 4}),
        ("/api/commands/strategies/smi_crossover/params",
         {"command_id": CMD_ID, "expected_version": 4, "params": {"EMA_PERIOD": 15}}),
        ("/api/commands/pause",
         {"command_id": CMD_ID, "reason": "hold"}),
        ("/api/commands/resume",
         {"command_id": CMD_ID, "expected_control_revision": 1, "reason": "resume"}),
    ]:
        r = client.post(url, json=body, headers=HEADERS)
        assert r.status_code == 403
        assert r.json()["code"] == "COMMANDS_DISABLED"
    assert gateway.calls == []


def test_activate_allocation_requires_preflight_nonce(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/allocation/activate",
                    json={"command_id": CMD_ID,
                          "attestation": {"strategy_id": "orb"},
                          "reason": "scale"},
                    headers=HEADERS)
    assert r.status_code == 428
    assert r.json()["code"] == "PREFLIGHT_REQUIRED"
    assert gateway.calls == []


def test_activate_allocation_forwards_with_nonce_on_paper(gateway):
    # Paper dashboards (live_commands=False) still need a nonce for
    # activate_allocation; the route must not treat nonce presence as a
    # live-only signal.
    client = make_client(gateway)
    attestation = {"strategy_id": "orb", "signature": "sig"}
    r = client.post("/api/commands/allocation/activate",
                    json={"command_id": CMD_ID, "attestation": attestation,
                          "reason": "scale 1", "preflight_nonce": "n-alloc"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "activate_allocation"
    assert body["command_id"] == CMD_ID
    assert body["attestation"] == attestation
    assert body["reason"] == "scale 1"
    assert body["preflight_nonce"] == "n-alloc"
    assert body["session_fingerprint"]  # opaque, non-empty


def test_suspend_allocation_is_single_post(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/allocation/suspend",
                    json={"command_id": CMD_ID, "reason": "drawdown"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls == [("suspend_allocation", {
        "command_id": CMD_ID, "reason": "drawdown",
    })]


def test_preflight_allows_activate_allocation_without_live_commands(gateway):
    client = make_client(gateway)  # paper-only flags
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID, "action": "activate_allocation",
                          "params": {"attestation": {"strategy_id": "orb"},
                                     "reason": "scale"}},
                    headers=HEADERS)
    assert r.status_code == 200
    assert gateway.calls[0][0] == "preflight_command"
    assert gateway.calls[0][1]["action"] == "activate_allocation"


def test_allocation_routes_disabled_returns_403_before_gateway(gateway):
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    for url, body in [
        ("/api/commands/allocation/activate",
         {"command_id": CMD_ID, "attestation": {"strategy_id": "orb"},
          "reason": "scale", "preflight_nonce": "n-1"}),
        ("/api/commands/allocation/suspend",
         {"command_id": CMD_ID, "reason": "drawdown"}),
    ]:
        r = client.post(url, json=body, headers=HEADERS)
        assert r.status_code == 403
        assert r.json()["code"] == "COMMANDS_DISABLED"
    assert gateway.calls == []


def test_activate_paper_automation_requires_preflight_nonce(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/paper-automation/activate",
                    json={"command_id": CMD_ID, "strategy_name": "orb",
                          "reason": "enable reviewed strategy"},
                    headers=HEADERS)
    assert r.status_code == 428
    assert r.json()["code"] == "PREFLIGHT_REQUIRED"
    assert gateway.calls == []


def test_activate_paper_automation_forwards_with_nonce(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/paper-automation/activate",
                    json={"command_id": CMD_ID, "strategy_name": "orb",
                          "reason": "enable reviewed strategy",
                          "preflight_nonce": "n-paper"},
                    headers=HEADERS)
    assert r.status_code == 202
    method, body = gateway.calls[0]
    assert method == "activate_paper_automation"
    assert body["command_id"] == CMD_ID
    assert body["strategy_name"] == "orb"
    assert body["reason"] == "enable reviewed strategy"
    assert body["preflight_nonce"] == "n-paper"
    # Nonce consume binds session fingerprint; omitting it made Activate
    # always fail with PREFLIGHT_REQUIRED after a successful preflight.
    assert body["session_fingerprint"]


def test_deactivate_paper_automation_is_immediate(gateway):
    client = make_client(gateway)
    r = client.post("/api/commands/paper-automation/deactivate",
                    json={"command_id": CMD_ID, "reason": "operator stop"},
                    headers=HEADERS)
    assert r.status_code == 202
    assert gateway.calls == [("deactivate_paper_automation", {
        "command_id": CMD_ID, "reason": "operator stop",
    })]


def test_preflight_allows_paper_automation_activation_without_live_commands(gateway):
    client = make_client(gateway)
    r = client.post("/api/preflight",
                    json={"command_id": CMD_ID,
                          "action": "activate_paper_automation",
                          "params": {"strategy_name": "orb",
                                     "reason": "enable reviewed strategy"}},
                    headers=HEADERS)
    assert r.status_code == 200
    assert gateway.calls[0][0] == "preflight_command"
    assert gateway.calls[0][1]["action"] == "activate_paper_automation"
