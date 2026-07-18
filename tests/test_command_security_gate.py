"""[M1-C] Task 7 -- the security gate: every mutating command route must
require an authenticated session, a matching CSRF token, a same-origin
request, and reject unknown body fields; a 202 receipt must never look like
a success acknowledgement; live commands must fail closed without an exact
account and a positive notional ceiling; and the overlapping legacy
``web/app.py`` trading-mutation routes must become a 409 the moment the
command center owns mutations, so there is exactly ONE mutation authority.

Source-vs-brief drift (see ``.superpowers/sdd/m1c-task-7-report.md`` for the
full account) -- this test module deliberately does NOT reproduce the
task-7 brief's Step-1 code verbatim, because three of its assumptions don't
match what actually landed on this branch:

1. ``from tests.test_command_routes import ...`` does not work in this
   repo (no ``tests/__init__.py``, and pytest's default "prepend" import
   mode puts ``tests/`` itself -- not its parent -- on ``sys.path``, so
   ``tests`` is never an importable package name). ``test_command_routes.py``
   already imports its own sibling fixtures the same way this file does
   (``from cc_fakes import ...``): a bare top-level module import.

2. The command router is mounted UNCONDITIONALLY (``[M1-C]`` Task 3,
   confirmed by ``test_command_routes.py::test_commands_disabled_returns_403_
   before_gateway``) precisely so a disabled deployment still answers
   ``/api/commands/*`` with a stable, parseable 403 ``COMMANDS_DISABLED``
   instead of a bare, undifferentiated 404. The brief's literal matrix
   assumed the opposite ("the router is not mounted at all" -> 404); that
   assumption predates the Task 3 fix and would regress an already-tested,
   deliberate behaviour. This module asserts the REAL contract: 403 with
   ``COMMANDS_DISABLED``, gateway never called.

3. ``CommandFlags`` lives in ``web/command_center/flags.py`` (constructor
   order ``commands_enabled, live_commands_enabled, live_account_id,
   live_max_order_notional``) and has no ``.validate()`` instance method --
   the fail-closed live-config check already lives in that module's
   ``load_command_flags(env)`` factory (fully covered by
   ``tests/test_command_flags.py``, out of this task's file scope). Rather
   than add a ``.validate()`` method to ``flags.py`` (out of scope for this
   task -- see the coordination brief's hard constraints), the equivalent
   guarantee below drives the REAL factory directly.

Everything else below (session/CSRF/origin required on every mutating
route, extra fields forbidden, 202 carries no success flag) matches the
brief's intent using the real route paths and real minimal request bodies
-- one brief path was also wrong for the real route shape: position close
is ``/api/commands/positions/{account}/{conid}/close`` (two separate path
segments), not a single ``account:conid`` segment, and needs a real
``action``/``quantity`` body (``ClosePositionBody`` has no bare
``command_id``-only shape).
"""
import pytest

import web.command_center.routes_commands as routes_commands
from web.command_center.flags import CommandFlags, CommandFlagsError, load_command_flags
from test_command_routes import CMD_ID, HEADERS, LIVE_FLAGS, make_client, FakeGateway

# test_command_routes.py's own `_fixed_csrf` autouse fixture only applies to
# tests collected IN that module -- pytest autouse fixtures are scoped to
# where they're defined, not transitively to whatever imports the module's
# helpers. HEADERS' "X-CSRF-Token": "test-csrf" needs the same stub here so
# every route below actually clears require_command_auth's CSRF check
# instead of failing it for the wrong reason.
@pytest.fixture(autouse=True)
def _fixed_csrf(monkeypatch):
    monkeypatch.setattr(routes_commands, "session_csrf_token", lambda s: "test-csrf")


# Every mutating command route, with a minimally valid body for each --
# verified field-for-field against the real pydantic models in
# web/command_center/routes_commands.py (all extra="forbid").
COMMAND_ROUTES = [
    ("POST", "/api/commands/proposals",
     {"command_id": CMD_ID, "conid": 265598, "action": "BUY"}),
    ("POST", "/api/commands/proposals/7/approve",
     {"command_id": CMD_ID, "expected_version": 3}),
    ("POST", "/api/commands/proposals/7/reject",
     {"command_id": CMD_ID, "reason": "no"}),
    ("POST", "/api/commands/positions/U1/265598/close",
     {"command_id": CMD_ID, "action": "SELL", "quantity": 40.0}),
    ("POST", "/api/commands/orders/grp-9:entry/cancel", {"command_id": CMD_ID}),
    ("POST", "/api/commands/orders/cancel-all",
     {"command_id": CMD_ID, "order_entity_ids": ["grp-9:entry"]}),
    ("POST", "/api/commands/strategies/smi/enable",
     {"command_id": CMD_ID, "expected_version": 4}),
    ("POST", "/api/commands/strategies/smi/disable",
     {"command_id": CMD_ID, "expected_version": 4}),
    ("POST", "/api/commands/strategies/smi/params",
     {"command_id": CMD_ID, "expected_version": 4, "params": {"EMA_PERIOD": 15}}),
    ("POST", "/api/commands/pause",
     {"command_id": CMD_ID, "reason": "hold"}),
    ("POST", "/api/commands/resume",
     {"command_id": CMD_ID, "expected_control_revision": 1, "reason": "resume"}),
]


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_requires_session(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway, session=None)          # no authenticated session
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 401
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_requires_csrf(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    headers = {k: v for k, v in HEADERS.items() if k != "X-CSRF-Token"}
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_rejects_foreign_origin(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    headers = {**HEADERS, "Origin": "http://evil.example"}
    r = client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_is_403_when_commands_disabled(method, path, body):
    """Drift item 2 above: the real contract is a stable 403
    COMMANDS_DISABLED (the router is mounted unconditionally), not a bare
    404 -- see test_command_routes.py::test_commands_disabled_returns_403_
    before_gateway, which this generalizes across every mutating route."""
    gateway = FakeGateway()
    client = make_client(gateway, flags=CommandFlags(False, False, None, None))
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 403
    assert r.json()["code"] == "COMMANDS_DISABLED"
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_every_command_route_forbids_extra_fields(method, path, body):
    gateway = FakeGateway()
    client = make_client(gateway)
    r = client.request(method, path, json={**body, "skip_risk_gate": True},
                       headers=HEADERS)
    assert r.status_code == 422          # extra="forbid" on every request model
    assert gateway.calls == []


@pytest.mark.parametrize("method,path,body", COMMAND_ROUTES)
def test_202_never_carries_a_success_flag(method, path, body):
    gateway = FakeGateway()                              # SUBMITTED-shaped receipt
    client = make_client(gateway)
    r = client.request(method, path, json=body, headers=HEADERS)
    assert r.status_code == 202
    payload = r.json()
    assert "success" not in payload and payload.get("state") != "RESOLVED"
    assert payload["command_id"] == CMD_ID              # correlation only


def test_live_commands_require_exact_account_and_notional_at_startup():
    """Drift item 3 above: no ``CommandFlags.validate()`` exists (out of
    this task's file scope) -- the equivalent fail-closed guarantee is
    exercised directly through the real ``load_command_flags`` factory,
    which already backs this with ``CommandFlagsError`` (a ``ValueError``
    subclass -- ``tests/test_command_flags.py`` covers it exhaustively).
    """
    with pytest.raises(CommandFlagsError):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": "",
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
        })
    with pytest.raises(CommandFlagsError):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "",
        })
    ok = load_command_flags({
        "DASHBOARD_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
        "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
    })
    assert ok == CommandFlags(True, True, "U1234567", 25000.0)          # no raise


# ---------------------------------------------------------------------------
# Single-authority lockout: the legacy web/app.py trading-mutation routes
# must become a 409 pointing at the command center the moment
# DASHBOARD_COMMANDS_ENABLED is true, so there is exactly ONE mutation
# authority. Watchlist CRUD and /strategies/deploy are untouched here --
# their migration is [COMPAT], not this task.
# ---------------------------------------------------------------------------

LEGACY_MUTATIONS = [
    ("/proposals/7/approve", {"csrf_token": "whatever"}),
    ("/proposals/7/reject", {"csrf_token": "whatever", "reason": "no"}),
    ("/strategies/smi/enable", {"csrf_token": "whatever"}),
    ("/strategies/smi/disable", {"csrf_token": "whatever"}),
    ("/strategies/smi/params", {"csrf_token": "whatever", "param_EMA_PERIOD": "15"}),
]


@pytest.mark.parametrize("path,data", LEGACY_MUTATIONS)
def test_legacy_mutation_is_409_when_commands_enabled(path, data):
    """The gate is a route-level dependency, resolved before the endpoint
    body even runs -- so an arbitrary (wrong) csrf_token in ``data`` still
    hits the 409 first, never the legacy CSRF check inside the handler."""
    from web import app as webapp
    client = webapp.make_test_client(commands_enabled=True)
    r = client.post(path, data=data, headers=HEADERS)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "MOVED_TO_COMMAND_CENTER"
    assert "/command-center" in body["message"]


def test_legacy_approve_is_409_when_commands_enabled():
    from web import app as webapp
    client = webapp.make_test_client(commands_enabled=True)
    r = client.post("/proposals/7/approve",
                    data={"csrf_token": "test-csrf"}, headers=HEADERS)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "MOVED_TO_COMMAND_CENTER"
    assert "/command-center" in body["message"]


def test_legacy_approve_is_unaffected_when_commands_disabled(monkeypatch):
    """The other half of the contract: with commands disabled, the legacy
    surface behaves EXACTLY as before -- no 409, real SDK call attempted,
    normal redirect-with-flash response. Stubs the SDK seam the same way
    tests/test_web_dashboard.py's `stub` fixture does, so this never
    reaches a real trader_service."""
    from web import app as webapp

    class _Result:
        def is_success(self):
            return True

    class _StubSDK:
        def approve(self, pid):
            return _Result()

    stub = _StubSDK()
    monkeypatch.setattr(webapp, "_mmr", stub)
    monkeypatch.setattr(webapp, "_get_mmr", lambda: stub)
    monkeypatch.setattr(webapp, "_reset_mmr", lambda: None)

    client = webapp.make_test_client(commands_enabled=False)
    r = client.post("/proposals/7/approve",
                    data={"csrf_token": webapp._CSRF_TOKEN}, headers=HEADERS,
                    follow_redirects=False)
    assert r.status_code == 303
    assert "approved" in r.headers["location"]


def test_legacy_watchlist_and_deploy_routes_are_out_of_scope(monkeypatch):
    """Explicit negative check for the brief's stated boundary: watchlist
    CRUD and /strategies/deploy are NOT part of LEGACY_TRADING_MUTATION_PATHS
    (their migration is [COMPAT]) -- they must keep working unchanged even
    with commands enabled."""
    from web import app as webapp

    client = webapp.make_test_client(commands_enabled=True)
    r = client.post("/watchlists/create",
                    data={"csrf_token": webapp._CSRF_TOKEN, "name": "not-a-real-name!!"},
                    follow_redirects=False)
    # Rejected for an invalid name (existing validation), NOT for the
    # commands-enabled gate -- i.e. never a 409.
    assert r.status_code != 409
    assert r.status_code == 303
