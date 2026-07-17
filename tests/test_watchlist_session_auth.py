"""[COMPAT] Task 1: legacy watchlist CRUD + CSV upload under the
command-center session (spec 2026-07-15-realtime-trading-command-center-
design.md Sections 10/14.1).

Source-vs-brief drift (see ``.superpowers/sdd/compat-task-1-report.md`` for
the full account): the coordination brief
(``.superpowers/sdd/compat-task-1-brief.md``) assumed
``web.command_center.session`` exports a bare ``require_session`` /
``DashboardSession`` / ``verify_csrf`` trio importable directly. It doesn't
-- ``session.py`` only exports ``build_require_session(manager) ->
Callable[[Request], str]`` bound to a concrete ``SessionManager`` (the
identical gap ``web/command_center/routes_commands.py``'s own module
docstring already documents for the command routes). This module instead
reuses the REAL, already-working session/origin stack the command routes
enforce: ``require_session`` and ``_check_origin``, both imported verbatim
from ``web.command_center.routes_commands`` -- the exact objects
``web/app.py``'s watchlist routes now depend on too. CSRF verification
stays on the existing ``web.app._CSRF_TOKEN``/``_check_csrf`` pair (not the
per-session ``session_csrf_token``) because ``dashboard.html`` renders ONE
shared ``{{ csrf_token }}`` slot consumed by both these watchlist forms and
the not-yet-migrated trading-mutation/deploy forms -- see the long comment
above ``watchlist_create`` in ``web/app.py`` for the full reasoning.

Because ``SessionSecurityMiddleware`` ([M1-R], out of scope to touch) already
gates every non-exempt path -- including ``/watchlists/*`` -- unconditionally,
these tests exercise the REAL app (``web.app.create_app`` / the test-only
``web.app.make_test_client`` helper) rather than FastAPI
``dependency_overrides``: overriding just the ``require_session`` dependency
would leave the middleware's own independent cookie check in place and could
never actually reach an authenticated route body without a real signed
session cookie. This mirrors how ``tests/test_web_dashboard.py`` and
``tests/test_command_security_gate.py`` already authenticate against the
real app.
"""
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from cc_fakes import NullBridge, NullQuotePlane
from web.command_center import CommandCenter, CommandCenterConfig
from web.command_center.flags import CommandFlags
from web.command_center.session import DashboardCredentials

TEST_TOKEN = 'watchlist-compat-test-token'
TEST_SECRET = 'w' * 64
ORIGIN = 'http://testserver'


def _sd(symbol='BHP', conid=1111):
    return SimpleNamespace(symbol=symbol, conId=conid, secType='STK',
                           exchange='SMART', primaryExchange='ASX', currency='AUD')


class _StubAccessor:
    """UniverseAccessor stand-in -- same shape as
    tests/test_web_dashboard.py's StubAccessor."""

    def __init__(self):
        self.universes: dict[str, list] = {}

    def list_universes_count(self):
        return {n: len(d) for n, d in self.universes.items()}

    def get(self, name):
        defs = list(self.universes.get(name, []))
        ns = SimpleNamespace(name=name, security_definitions=defs)
        ns.find_symbol = lambda sym: next(
            (d for d in defs if d.symbol.upper() == sym.upper()), None)
        return ns

    def update(self, universe):
        self.universes[universe.name] = list(universe.security_definitions)

    def insert(self, name, sd):
        self.universes.setdefault(name, []).append(sd)

    def delete(self, name):
        self.universes.pop(name, None)

    def update_from_csv_str(self, name, csv_str):
        return max(0, len(csv_str.strip().splitlines()) - 1)


class _StubSDK:
    def account_cash(self):
        return None

    def portfolio_snapshot(self):
        return None

    def status(self):
        return None

    def risk_report(self):
        return None

    def get_risk_limits(self):
        return None

    def portfolio(self):
        return pd.DataFrame()

    def proposals(self, limit=100):
        return pd.DataFrame()

    def strategies(self):
        return pd.DataFrame()

    def resolve(self, symbol, sec_type='STK', exchange='', currency='', universe=''):
        return [_sd(str(symbol).upper())]


@pytest.fixture
def stub(monkeypatch):
    accessor = _StubAccessor()
    sdk = _StubSDK()
    monkeypatch.setattr(webapp, '_get_accessor', lambda: accessor)
    monkeypatch.setattr(webapp, '_mmr', sdk)
    monkeypatch.setattr(webapp, '_get_mmr', lambda: sdk)
    monkeypatch.setattr(webapp, '_reset_mmr', lambda: None)
    monkeypatch.setattr(webapp, 'scan_strategies', lambda *a, **k: [])
    return accessor


@pytest.fixture
def anon_client(stub):
    """The real, module-level app -- never logged in. No DASHBOARD_TOKEN/
    DASHBOARD_SESSION_SECRET is configured in the test process, so
    SessionSecurityMiddleware's manager_provider degrades to "no manager"
    (see web/app.py's `create_app` docstring) and treats every non-exempt
    request as unauthenticated -- exactly the same outcome a real deployment
    gives an anonymous caller with no cookie at all."""
    return TestClient(webapp.app)


@pytest.fixture
def client(stub):
    """A real, authenticated session against a throwaway CommandCenter --
    same construction tests/test_web_dashboard.py's `client`/`stub_cc`
    fixtures use, so watchlist routes are exercised through the REAL
    SessionSecurityMiddleware + require_session + _check_origin stack, not a
    bypassed one."""
    center = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TEST_TOKEN, session_secret=TEST_SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane(),
    )
    app = webapp.create_app(center)
    test_client = TestClient(app)
    test_client.post('/session', data={'token': TEST_TOKEN})
    test_client.headers.update({'Origin': ORIGIN})
    return test_client


def _csrf():
    return webapp._CSRF_TOKEN


WATCHLIST_MUTATIONS = [
    ('/watchlists/create', {'name': 'asx'}),
    ('/watchlists/asx/add', {'symbols': 'BHP'}),
    ('/watchlists/asx/remove', {'symbol': 'BHP'}),
    ('/watchlists/asx/delete', {}),
]


@pytest.mark.parametrize('path,data', WATCHLIST_MUTATIONS)
def test_watchlist_mutation_requires_session(anon_client, path, data):
    r = anon_client.post(path, data={**data, 'csrf_token': _csrf()}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers['location'] == '/cc/login'


@pytest.mark.parametrize('path,data', WATCHLIST_MUTATIONS)
def test_watchlist_mutation_rejects_missing_csrf(client, path, data):
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize('path,data', WATCHLIST_MUTATIONS)
def test_watchlist_mutation_rejects_wrong_csrf(client, path, data):
    r = client.post(path, data={**data, 'csrf_token': 'stale-or-forged'},
                    follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize('path,data', WATCHLIST_MUTATIONS)
def test_watchlist_mutation_rejects_cross_origin(client, path, data):
    r = client.post(path, data={**data, 'csrf_token': _csrf()},
                    headers={'Origin': 'http://evil.example'}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize('path,data', WATCHLIST_MUTATIONS)
def test_watchlist_mutation_succeeds_authenticated_same_origin(client, stub, path, data):
    r = client.post(path, data={**data, 'csrf_token': _csrf()}, follow_redirects=False)
    assert r.status_code == 303


def test_watchlist_create_actually_creates(client, stub):
    r = client.post('/watchlists/create', data={'name': 'asx', 'csrf_token': _csrf()},
                    follow_redirects=False)
    assert r.status_code == 303
    assert 'created' in r.headers['location']
    assert 'asx' in stub.universes


# ---------------------------------------------------------------------------
# CSV upload -- multipart, tested separately from the form-only mutations
# above since it needs a `files=` payload.
# ---------------------------------------------------------------------------

def test_csv_upload_requires_session(anon_client):
    r = anon_client.post('/watchlists/asx/upload',
                         files={'file': ('w.csv', b'symbol\nBHP\n', 'text/csv')},
                         data={'csrf_token': _csrf()}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers['location'] == '/cc/login'


def test_csv_upload_rejects_missing_csrf(client):
    r = client.post('/watchlists/asx/upload',
                    files={'file': ('w.csv', b'symbol\nBHP\n', 'text/csv')},
                    follow_redirects=False)
    assert r.status_code == 403


def test_csv_upload_rejects_cross_origin(client):
    r = client.post('/watchlists/asx/upload',
                    files={'file': ('w.csv', b'symbol\nBHP\n', 'text/csv')},
                    data={'csrf_token': _csrf()},
                    headers={'Origin': 'http://evil.example'}, follow_redirects=False)
    assert r.status_code == 403


def test_csv_upload_succeeds_authenticated_same_origin(client, stub):
    r = client.post('/watchlists/asx/upload',
                    files={'file': ('w.csv', b'symbol\nBHP\n', 'text/csv')},
                    data={'csrf_token': _csrf()}, follow_redirects=False)
    assert r.status_code == 303
    assert 'asx' in stub.universes
    assert any(d.symbol == 'BHP' for d in stub.universes['asx'])


# ---------------------------------------------------------------------------
# Single-authority overlap check: watchlist CRUD is explicitly OUT of
# LEGACY_TRADING_MUTATION_PATHS (web/app.py) and must keep working even when
# DASHBOARD_COMMANDS_ENABLED flips the 409 lockout on for the trading-
# mutation routes (tests/test_command_security_gate.py already covers the
# 409 side of that contract exhaustively; this only proves watchlist doesn't
# get caught in it).
# ---------------------------------------------------------------------------

def test_watchlist_create_survives_commands_enabled(stub):
    c = webapp.make_test_client(commands_enabled=True)
    c.headers.update({'Origin': ORIGIN})
    r = c.post('/watchlists/create', data={'name': 'keep', 'csrf_token': webapp._CSRF_TOKEN},
              follow_redirects=False)
    assert r.status_code == 303
    assert 'keep' in stub.universes


def test_trading_mutation_still_locked_out_when_commands_enabled():
    c = webapp.make_test_client(commands_enabled=True)
    r = c.post('/proposals/7/approve', data={'csrf_token': 'whatever'}, follow_redirects=False)
    assert r.status_code == 409
    assert r.json()['code'] == 'MOVED_TO_COMMAND_CENTER'


def test_per_process_csrf_secret_still_present():
    """The brief's original (stale) plan removed `_CSRF_TOKEN`/`_check_csrf`
    entirely -- not applicable here: this task's scope is watchlist CRUD/
    upload only (see module docstring), and `_CSRF_TOKEN`/`_check_csrf` are
    still load-bearing for the untouched trading-mutation and deploy
    routes. Their eventual removal is tracked by a later [COMPAT] task."""
    assert hasattr(webapp, '_CSRF_TOKEN')
    assert hasattr(webapp, '_check_csrf')
