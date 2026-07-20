"""Tests for the web dashboard's strategy controls and tooltips.

Drives web.app through FastAPI's TestClient with a stubbed SDK — no ZMQ, no
services. Covers: enable/disable/params routes (CSRF, redirect, SDK calls),
human-readable strategy rendering, the unfold param editor, the available-
strategies section, and tooltip markup.

Spec: docs/superpowers/specs/2026-07-15-dashboard-strategy-controls-design.md
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from cc_fakes import NullBridge, NullQuotePlane
from trader.common.reactivex import SuccessFail

TEST_TOKEN = "test-dashboard-token"
TEST_SECRET = "s" * 64


class _Ok:
    def is_success(self):
        return True


class _Fail:
    error = 'strategy_service unreachable'

    def is_success(self):
        return False


class StubSDK:
    def __init__(self):
        self.calls = []
        self.toggle_result = _Ok()
        self.params_result = _Ok()
        self.approve_result = SuccessFail.success()
        self.risk_error = None

    # --- read fetchers (minimal shapes; sections degrade gracefully) ---
    def account_cash(self):
        return None

    def portfolio_snapshot(self):
        return None

    def status(self):
        return None

    def risk_report(self):
        if self.risk_error is not None:
            raise self.risk_error
        return None

    def get_risk_limits(self):
        return None

    def portfolio(self):
        return pd.DataFrame()

    def proposals(self, limit=100):
        return pd.DataFrame()

    def strategies(self):
        return pd.DataFrame([
            {'name': 'orb_googl', 'state': 'RUNNING', 'bar_size': '1 min',
             'conids': [208813719], 'hist_days_prior': 90,
             'auto_execute': 'propose', 'class_name': 'OpeningRangeBreakout',
             'description': 'ORB 45/1.3 on GOOGL — sweep run 309',
             'params': {'RANGE_MINUTES': 45}},
            {'name': 'vwap_cat', 'state': 'DISABLED', 'bar_size': '1 min',
             'conids': [5437], 'hist_days_prior': 90,
             'auto_execute': False, 'class_name': 'VwapReclaim',
             'description': 'VWAP reclaim on CAT', 'params': {}},
        ])

    # --- mutations ---
    def approve(self, pid):
        self.calls.append(('approve', pid))
        return self.approve_result

    def enable_strategy(self, name):
        self.calls.append(('enable', name))
        return self.toggle_result

    def disable_strategy(self, name):
        self.calls.append(('disable', name))
        return self.toggle_result

    def update_strategy_params(self, name, params):
        self.calls.append(('params', name, params))
        return self.params_result


_SCANNED = [
    {'file': 'momentum.py', 'class': 'Momentum', 'mode': 'precompute',
     'tunables': {'LOOKBACK': 20}, 'docstring': 'Momentum strategy.',
     'docstring_full': 'Momentum strategy.\n\nLong explanation.'},
    {'file': 'opening_range_breakout.py', 'class': 'OpeningRangeBreakout',
     'mode': 'precompute', 'tunables': {'RANGE_MINUTES': 30},
     'docstring': 'ORB.', 'docstring_full': 'ORB.'},
]


@pytest.fixture
def stub(monkeypatch):
    sdk = StubSDK()
    # Patch at the _get_mmr seam, not the _mmr global: _call() reconnects on a
    # dropped connection via _reset_mmr() + _get_mmr(), and if _get_mmr fell
    # through to its real body it would construct a live MMR().connect() (real
    # ZMQ socket, 30s RPC timeout) the moment a test simulates a ConnectionError.
    # Returning the same stub from _get_mmr — and no-op'ing _reset_mmr — keeps a
    # simulated failure fully contained to the stub across _call's retry-once,
    # so no test can ever reach a real SDK.
    monkeypatch.setattr(webapp, '_mmr', sdk)
    monkeypatch.setattr(webapp, '_get_mmr', lambda: sdk)
    monkeypatch.setattr(webapp, '_reset_mmr', lambda: None)
    monkeypatch.setattr(webapp, 'scan_strategies', lambda *a, **k: list(_SCANNED))
    return sdk


@pytest.fixture
def stub_cc():
    """A CommandCenter wired with the same null bridge/quote-plane fakes
    used by tests/test_dashboard_snapshot_api.py, and a fixed test token --
    just enough to build create_app(...) and authenticate through
    /session, since these tests exercise the legacy SDK-backed dashboard
    routes (now behind the same session gate), not the read-model API."""
    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials
    return CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TEST_TOKEN, session_secret=TEST_SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane(),
    )


@pytest.fixture
def client(stub, stub_cc):
    from web.app import create_app
    app = create_app(stub_cc)
    test_client = TestClient(app)
    test_client.post("/session", data={"token": TEST_TOKEN})
    # [COMPAT] Task 1: the watchlist CRUD/upload routes now also require a
    # matching Origin header (`_check_origin`, reused verbatim from
    # web/command_center/routes_commands.py) -- a real browser's own-page
    # form POST always carries one, but httpx's TestClient (unlike a
    # browser) never adds it automatically. Set once, dashboard-wide, on
    # this client: harmless for every other route exercised below (none of
    # them check Origin), and lets the watchlist tests keep posting plain
    # form bodies unchanged.
    test_client.headers.update({"Origin": "http://testserver"})
    return test_client


def _csrf():
    return webapp._CSRF_TOKEN


# ---------------------------------------------------------------------------
# Root redirects to the command center. The legacy server-rendered dashboard
# (now at /legacy) fanned out 10 sequential, blocking SDK RPC fetchers; against
# the split-container trader (which serves only the typed sockets 42101/2/3,
# NOT the legacy full RPC on 42001) every send had "no route to server" and
# each fetcher stalled for the full RPC poll timeout — so GET `/` hung for
# minutes ("stuck loading the page"). `/` now redirects straight to the live
# command center `/cc` and touches no SDK.
# ---------------------------------------------------------------------------
class TestRootRedirectsToCommandCenter:
    def test_authenticated_root_redirects_to_command_center(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (303, 307, 308)
        assert r.headers["location"] == "/cc"

    def test_root_redirect_makes_no_sdk_calls(self, client, stub):
        def _boom(*_a, **_k):
            raise AssertionError("legacy SDK fetcher reached on `/`")

        for name in ("account_cash", "portfolio_snapshot", "status",
                     "risk_report", "get_risk_limits", "portfolio",
                     "strategies", "proposals"):
            setattr(stub, name, _boom)
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (303, 307, 308)


# ---------------------------------------------------------------------------
# [S0] Legacy dashboard failure rendering — a failed approval must never
# flash as "submitted", and a failed risk-report fetch must never render as
# a green "no active warnings" pass row (that infers OK from a missing
# report, which is exactly the false-safety bug this task closes).
# Spec: docs/superpowers/plans/2026-07-15-command-center-s0-safety.md (Task 5)
# ---------------------------------------------------------------------------
def test_failed_approval_never_flashes_submitted(client, stub):
    stub.approve_result = SuccessFail.fail(error="broker rejected")
    response = client.post(
        "/proposals/7/approve",
        data={"csrf_token": _csrf()},
        follow_redirects=False,
    )
    assert "broker%20rejected" in response.headers["location"]
    assert "submitted" not in response.headers["location"]


def test_risk_fetch_failure_is_unavailable_not_green(client, stub):
    stub.risk_error = ConnectionError("risk RPC down")
    html = client.get("/legacy").text
    assert "Risk unavailable" in html
    assert "No active risk warnings" not in html


class TestStrategyToggleRoutes:
    def test_enable_calls_sdk_and_redirects(self, client, stub):
        r = client.post('/strategies/vwap_cat/enable',
                        data={'csrf_token': _csrf()}, follow_redirects=False)
        assert r.status_code == 303
        assert ('enable', 'vwap_cat') in stub.calls

    def test_disable_calls_sdk_and_redirects(self, client, stub):
        r = client.post('/strategies/orb_googl/disable',
                        data={'csrf_token': _csrf()}, follow_redirects=False)
        assert r.status_code == 303
        assert ('disable', 'orb_googl') in stub.calls

    def test_bad_csrf_rejected(self, client, stub):
        r = client.post('/strategies/orb_googl/disable',
                        data={'csrf_token': 'wrong'}, follow_redirects=False)
        assert r.status_code == 403
        assert stub.calls == []

    def test_failure_flashes_error(self, client, stub):
        stub.toggle_result = _Fail()
        r = client.post('/strategies/orb_googl/enable',
                        data={'csrf_token': _csrf()}, follow_redirects=False)
        assert r.status_code == 303
        assert 'unreachable' in r.headers['location']


class TestParamsRoute:
    def test_param_fields_forwarded(self, client, stub):
        r = client.post('/strategies/orb_googl/params',
                        data={'csrf_token': _csrf(),
                              'param_RANGE_MINUTES': '60',
                              'param_VOLUME_MULT': '1.2'},
                        follow_redirects=False)
        assert r.status_code == 303
        assert ('params', 'orb_googl',
                {'RANGE_MINUTES': '60', 'VOLUME_MULT': '1.2'}) in stub.calls

    def test_new_key_value_pair_included(self, client, stub):
        client.post('/strategies/orb_googl/params',
                    data={'csrf_token': _csrf(), 'param_RANGE_MINUTES': '45',
                          'new_key': 'SESSION_TZ', 'new_value': 'Australia/Sydney'},
                    follow_redirects=False)
        call = next(c for c in stub.calls if c[0] == 'params')
        assert call[2]['SESSION_TZ'] == 'Australia/Sydney'

    def test_bad_csrf_rejected(self, client, stub):
        r = client.post('/strategies/orb_googl/params',
                        data={'csrf_token': 'nope', 'param_A': '1'},
                        follow_redirects=False)
        assert r.status_code == 403
        assert stub.calls == []


class TestDashboardRendering:
    def test_human_readable_names_and_description(self, client):
        html = client.get('/legacy').text
        assert 'Opening Range Breakout' in html   # humanized class name
        assert 'orb_googl' in html                # config name still visible
        assert 'sweep run 309' in html            # description present (hover)

    def test_toggle_buttons_match_state(self, client):
        html = client.get('/legacy').text
        assert '/strategies/orb_googl/disable' in html   # RUNNING → Disable
        assert '/strategies/vwap_cat/enable' in html     # DISABLED → Enable

    def test_unfold_param_editor_rendered(self, client):
        html = client.get('/legacy').text
        assert '/strategies/orb_googl/params' in html
        assert 'param_RANGE_MINUTES' in html
        assert 'new_key' in html

    def test_available_strategies_listed(self, client):
        html = client.get('/legacy').text
        assert 'Momentum' in html
        assert 'momentum.py' in html
        # deployed class is marked as such, not repeated as available-only
        assert 'deployed' in html.lower()

    def test_tooltips_present(self, client):
        html = client.get('/legacy').text
        assert html.count('class="info"') >= 8    # ⓘ across sections/metrics
        assert 'class="tip"' in html

    def test_bar_and_conids_columns_have_tooltips(self, client):
        html = client.get('/legacy').text
        assert 'completed bar of this size' in html      # Bar column tip
        assert 'IB contract IDs' in html                 # ConIds column tip

    def test_tooltips_use_viewport_positioning(self, client):
        """Tips must escape section overflow:hidden — fixed positioning with
        viewport clamping, computed on hover by positionTip()."""
        html = client.get('/legacy').text
        assert 'function positionTip' in html
        assert 'position: fixed' in html


class TestTabs:
    def test_three_tabs_with_overview_default(self, client):
        html = client.get('/legacy').text
        for name in ('overview', 'strategies', 'risk'):
            assert f'data-tab="{name}"' in html
            assert f'id="tab-{name}"' in html
        # overview is the default-active pane
        assert 'id="tab-overview" class="tabpane active"' in html

    def test_sections_live_in_the_right_panes(self, client):
        html = client.get('/legacy').text
        overview = html.index('id="tab-overview"')
        strategies = html.index('id="tab-strategies"')
        risk = html.index('id="tab-risk"')
        # Overview holds cash, positions, proposals (cash folded into first tab)
        assert overview < html.index('Cash by currency') < strategies
        assert overview < html.index('Positions &amp; P&amp;L') < strategies
        assert overview < html.index('>Proposals') < strategies
        # Strategies pane holds deployed + available tables
        assert strategies < html.index('/strategies/orb_googl/disable') < risk
        assert strategies < html.index('Available strategies') < risk
        # Risk pane holds the risk metrics
        assert html.index('Gross exposure') > risk

    def test_tab_state_survives_auto_refresh(self, client):
        """Tab selection is kept in location.hash, which location.reload()
        preserves — switching to Strategies must survive the 15s refresh."""
        html = client.get('/legacy').text
        assert 'location.hash' in html


# ---------------------------------------------------------------------------
# Watchlists (universes UI) + deploy-from-disk
# Spec: docs/superpowers/specs/2026-07-15-watchlists-and-ui-deploy-design.md
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _sd(symbol='AAPL', conid=265598):
    return SimpleNamespace(symbol=symbol, conId=conid, secType='STK',
                           exchange='SMART', primaryExchange='NASDAQ',
                           currency='USD')


class StubAccessor:
    def __init__(self):
        self.universes = {'mylist': [_sd('AAPL'), _sd('MSFT', 272093)]}
        self.calls = []

    def list_universes_count(self):
        return {n: len(d) for n, d in self.universes.items()}

    def get(self, name):
        defs = list(self.universes.get(name, []))
        ns = SimpleNamespace(name=name, security_definitions=defs)
        ns.find_symbol = lambda sym: next(
            (d for d in defs if d.symbol.upper() == sym.upper()), None)
        return ns

    def insert(self, name, sd):
        self.universes.setdefault(name, []).append(sd)
        self.calls.append(('insert', name, sd.symbol))

    def update(self, universe):
        self.universes[universe.name] = list(universe.security_definitions)
        self.calls.append(('update', universe.name))

    def delete(self, name):
        self.universes.pop(name, None)
        self.calls.append(('delete', name))

    def update_from_csv_str(self, name, csv_str):
        self.calls.append(('csv', name))
        return max(0, len(csv_str.strip().splitlines()) - 1)


_RESOLVABLE = {'AAPL': 265598, 'MSFT': 272093, 'GLD': 51529211}


@pytest.fixture
def accessor(monkeypatch):
    acc = StubAccessor()
    monkeypatch.setattr(webapp, '_get_accessor', lambda: acc)
    return acc


@pytest.fixture
def stub_resolving(stub, monkeypatch):
    def resolve(symbol, sec_type='STK', exchange='', currency='', universe=''):
        conid = _RESOLVABLE.get(str(symbol).upper())
        return [_sd(str(symbol).upper(), conid)] if conid else []
    stub.resolve = resolve
    stub.reload_strategies = lambda: _Ok()
    return stub


@pytest.fixture
def deploy_config(tmp_path, monkeypatch):
    import yaml
    cfg = tmp_path / 'strategy_runtime.yaml'
    cfg.write_text(yaml.safe_dump(
        {'strategies': [{'name': 'orb_googl', 'module': 'strategies/opening_range_breakout.py',
                         'class_name': 'OpeningRangeBreakout'}]}))
    monkeypatch.setattr(webapp, '_STRATEGY_CONFIG_PATH', cfg)
    return cfg


class TestWatchlistRoutes:
    def test_create(self, client, accessor, stub_resolving):
        r = client.post('/watchlists/create',
                        data={'csrf_token': _csrf(), 'name': 'My-Watch_1'},
                        follow_redirects=False)
        assert r.status_code == 303
        assert 'my-watch_1' in accessor.universes

    def test_create_bad_name_rejected(self, client, accessor, stub_resolving):
        client.post('/watchlists/create',
                    data={'csrf_token': _csrf(), 'name': '../evil'},
                    follow_redirects=False)
        assert '../evil' not in accessor.universes

    def test_add_symbols_resolves_and_inserts(self, client, accessor, stub_resolving):
        r = client.post('/watchlists/mylist/add',
                        data={'csrf_token': _csrf(), 'symbols': 'GLD, MSFT'},
                        follow_redirects=False)
        assert r.status_code == 303
        assert ('insert', 'mylist', 'GLD') in accessor.calls

    def test_add_unresolved_symbol_reported_not_inserted(self, client, accessor, stub_resolving):
        r = client.post('/watchlists/mylist/add',
                        data={'csrf_token': _csrf(), 'symbols': 'NOPE123'},
                        follow_redirects=False)
        assert 'NOPE123' in r.headers['location']
        assert not any(c[0] == 'insert' for c in accessor.calls)

    def test_upload_simple_csv_resolves_rows(self, client, accessor, stub_resolving):
        csv_bytes = b'symbol\nAAPL\nGLD\n'
        r = client.post('/watchlists/mylist/upload',
                        data={'csrf_token': _csrf()},
                        files={'file': ('w.csv', csv_bytes, 'text/csv')},
                        follow_redirects=False)
        assert r.status_code == 303
        assert ('insert', 'mylist', 'AAPL') in accessor.calls
        assert ('insert', 'mylist', 'GLD') in accessor.calls

    def test_upload_secdef_csv_uses_bulk_import(self, client, accessor, stub_resolving):
        csv_bytes = b'conId,symbol\n265598,AAPL\n'
        client.post('/watchlists/mylist/upload',
                    data={'csrf_token': _csrf()},
                    files={'file': ('w.csv', csv_bytes, 'text/csv')},
                    follow_redirects=False)
        assert ('csv', 'mylist') in accessor.calls

    def test_remove_symbol(self, client, accessor, stub_resolving):
        client.post('/watchlists/mylist/remove',
                    data={'csrf_token': _csrf(), 'symbol': 'AAPL'},
                    follow_redirects=False)
        assert all(d.symbol != 'AAPL' for d in accessor.universes['mylist'])

    def test_delete_watchlist(self, client, accessor, stub_resolving):
        client.post('/watchlists/mylist/delete',
                    data={'csrf_token': _csrf()}, follow_redirects=False)
        assert 'mylist' not in accessor.universes

    def test_watchlists_tab_rendered(self, client, accessor, stub_resolving):
        html = client.get('/manage').text
        assert 'data-tab="watchlists"' in html
        assert 'id="tab-watchlists"' in html

    def test_flash_redirects_to_manage(self, client, accessor, stub_resolving):
        r = client.post('/watchlists/create',
                        data={'csrf_token': _csrf(), 'name': 'flash_test'},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers['location'].startswith('/manage?flash=')
        assert 'flash_test' in accessor.universes


class TestDeployRoute:
    def _deploy(self, client, **extra):
        data = {'csrf_token': _csrf(), 'file': 'momentum.py', 'class': 'Momentum',
                'name': 'mom_test', 'bar_size': '1 min', 'days': '90',
                'symbols': 'AAPL'}
        data.update(extra)
        return client.post('/strategies/deploy', data=data, follow_redirects=False)

    def test_deploy_writes_yaml_reloads_and_enables(
            self, client, stub, accessor, stub_resolving, deploy_config):
        import yaml
        r = self._deploy(client)
        assert r.status_code == 303
        cfg = yaml.safe_load(deploy_config.read_text())
        entry = next(e for e in cfg['strategies'] if e['name'] == 'mom_test')
        assert entry['module'] == 'strategies/momentum.py'
        assert entry['class_name'] == 'Momentum'
        assert entry['conids'] == [265598]
        assert ('enable', 'mom_test') in stub.calls
        # resolved secdef registered so resolve_symbol(conId) works at load
        assert ('insert', 'strat_mom_test', 'AAPL') in accessor.calls

    def test_deploy_with_watchlist_target(self, client, stub, accessor,
                                          stub_resolving, deploy_config):
        import yaml
        self._deploy(client, symbols='', watchlist='mylist')
        cfg = yaml.safe_load(deploy_config.read_text())
        entry = next(e for e in cfg['strategies'] if e['name'] == 'mom_test')
        assert entry['universe'] == 'mylist'
        assert 'conids' not in entry

    def test_deploy_propose_mode(self, client, stub, accessor,
                                 stub_resolving, deploy_config):
        import yaml
        self._deploy(client, auto_propose='on')
        cfg = yaml.safe_load(deploy_config.read_text())
        entry = next(e for e in cfg['strategies'] if e['name'] == 'mom_test')
        assert entry['auto_execute'] == 'propose'

    def test_deploy_params_recorded(self, client, stub, accessor,
                                    stub_resolving, deploy_config):
        import yaml
        self._deploy(client, param_LOOKBACK='25')
        cfg = yaml.safe_load(deploy_config.read_text())
        entry = next(e for e in cfg['strategies'] if e['name'] == 'mom_test')
        assert entry['params'] == {'LOOKBACK': 25}

    def test_duplicate_name_rejected(self, client, stub, accessor,
                                     stub_resolving, deploy_config):
        import yaml
        r = self._deploy(client, name='orb_googl')
        assert 'already' in r.headers['location']
        cfg = yaml.safe_load(deploy_config.read_text())
        assert len([e for e in cfg['strategies'] if e['name'] == 'orb_googl']) == 1

    def test_unknown_class_rejected(self, client, stub, accessor,
                                    stub_resolving, deploy_config):
        import yaml
        self._deploy(client, **{'class': 'EvilClass', 'file': '../../etc/passwd'})
        cfg = yaml.safe_load(deploy_config.read_text())
        assert not any(e.get('name') == 'mom_test' for e in cfg['strategies'])

    def test_unresolved_symbol_aborts_deploy(self, client, stub, accessor,
                                             stub_resolving, deploy_config):
        import yaml
        r = self._deploy(client, symbols='AAPL NOPE123')
        assert 'NOPE123' in r.headers['location']
        cfg = yaml.safe_load(deploy_config.read_text())
        assert not any(e.get('name') == 'mom_test' for e in cfg['strategies'])

    def test_deploy_form_rendered_in_available_table(self, client, accessor, stub_resolving):
        html = client.get('/manage').text
        assert '/strategies/deploy' in html
        assert 'name="watchlist"' in html
        assert 'Command Center' in html
        assert '/strategies/' not in html or '/strategies/deploy' in html
        # Manage page is read-only for deployed strategies — no enable/disable forms
        assert '/strategies/orb_googl/enable' not in html


class TestManagePage:
    def test_manage_renders_without_heavy_fetchers(self, client, stub, monkeypatch):
        """ /manage must not fan out legacy overview fetchers (cash, risk, …). """
        def _boom():
            raise AssertionError('legacy fetcher must not run on /manage')

        for name in ('fetch_cash', 'fetch_snapshot', 'fetch_status', 'fetch_risk',
                     'fetch_risk_limits', 'fetch_positions', 'fetch_proposals'):
            monkeypatch.setattr(webapp, name, _boom)
        html = client.get('/manage').text
        assert 'Available strategies' in html
        assert 'Watchlists' in html
        assert 'Command Center' in html

    def test_manage_marks_deployed_classes(self, client):
        html = client.get('/manage').text
        assert 'deployed' in html.lower()

    def test_manage_default_tab_is_strategies(self, client):
        html = client.get('/manage').text
        assert 'id="tab-strategies"' in html
        assert "'strategies'" in html


class TestLegacyAccessTokenDoubleGate:
    """MINOR-3: the deprecated MMR_WEB_TOKEN alias sets both `_ACCESS_TOKEN`
    (this module's standalone token gate, checked by `_check_access`) and
    the command center's SessionManager token (since DASHBOARD_TOKEN is
    unset in that config). A cookie-authenticated browser never re-sends
    the raw token on ordinary page loads, so `_check_access` used to 401
    every legacy-page request in that configuration even with a fully
    valid session cookie -- a double gate. `_check_access` must also accept
    a valid dashboard session cookie."""

    @pytest.fixture
    def alias_client(self, stub, monkeypatch):
        from web.command_center import CommandCenter, CommandCenterConfig
        from web.command_center.session import DashboardCredentials
        from cc_fakes import NullBridge, NullQuotePlane
        # Simulate the deprecated-alias path: MMR_WEB_TOKEN populates
        # _ACCESS_TOKEN directly (normally read from os.environ at import
        # time) while ALSO being the token load_dashboard_credentials would
        # hand to the SessionManager.
        monkeypatch.setattr(webapp, '_ACCESS_TOKEN', TEST_TOKEN)
        cc = CommandCenter(
            CommandCenterConfig(),
            credentials_loader=lambda: DashboardCredentials(
                token=TEST_TOKEN, session_secret=TEST_SECRET.encode(),
                legacy_alias_used=True),
            query_client_factory=lambda: None,
            feed_client_factory=lambda: None,
            bridge_factory=lambda *a, **k: NullBridge(),
            quote_plane_factory=lambda loop, deliver: NullQuotePlane(),
        )
        return TestClient(webapp.create_app(cc))

    def test_cookie_session_satisfies_legacy_token_gate(self, alias_client):
        alias_client.post("/session", data={"token": TEST_TOKEN})
        assert alias_client.get("/legacy").status_code == 200

    def test_no_session_still_blocked(self, alias_client):
        response = alias_client.get("/legacy", follow_redirects=False)
        assert response.status_code in (303, 401)

    def test_canonical_config_unaffected(self, client):
        """The ordinary `client` fixture never sets `_ACCESS_TOKEN` (mirrors
        canonical DASHBOARD_TOKEN config, MMR_WEB_TOKEN unset) -- must keep
        working exactly as before."""
        assert client.get("/legacy").status_code == 200


class TestEntrypointWorkerGuard:
    """MINOR-2: an external multi-worker launch (WEB_CONCURRENCY /
    UVICORN_WORKERS set) must hard-fail at the `main()` CLI entrypoint,
    before uvicorn.run ever binds a socket -- distinct from the in-lifespan
    `CommandCenter._start_or_degrade` guard, which intentionally degrades
    rather than aborts so the ops probes keep serving."""

    def test_main_aborts_before_uvicorn_run_on_multi_worker_env(self, monkeypatch):
        import uvicorn

        def _must_not_run(*_a, **_k):
            raise AssertionError("uvicorn.run must not be reached")

        monkeypatch.setattr(uvicorn, "run", _must_not_run)
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with pytest.raises(RuntimeError, match="one worker"):
            webapp.main()

    def test_main_runs_uvicorn_when_unset(self, monkeypatch):
        import uvicorn
        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("UVICORN_WORKERS", raising=False)
        webapp.main()
        assert len(calls) == 1
        assert calls[0][1]["workers"] == 1
