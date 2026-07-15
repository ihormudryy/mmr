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

    # --- read fetchers (minimal shapes; sections degrade gracefully) ---
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
    monkeypatch.setattr(webapp, '_mmr', sdk)
    monkeypatch.setattr(webapp, 'scan_strategies', lambda *a, **k: list(_SCANNED))
    return sdk


@pytest.fixture
def client(stub):
    return TestClient(webapp.app)


def _csrf():
    return webapp._CSRF_TOKEN


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
        html = client.get('/').text
        assert 'Opening Range Breakout' in html   # humanized class name
        assert 'orb_googl' in html                # config name still visible
        assert 'sweep run 309' in html            # description present (hover)

    def test_toggle_buttons_match_state(self, client):
        html = client.get('/').text
        assert '/strategies/orb_googl/disable' in html   # RUNNING → Disable
        assert '/strategies/vwap_cat/enable' in html     # DISABLED → Enable

    def test_unfold_param_editor_rendered(self, client):
        html = client.get('/').text
        assert '/strategies/orb_googl/params' in html
        assert 'param_RANGE_MINUTES' in html
        assert 'new_key' in html

    def test_available_strategies_listed(self, client):
        html = client.get('/').text
        assert 'Momentum' in html
        assert 'momentum.py' in html
        # deployed class is marked as such, not repeated as available-only
        assert 'deployed' in html.lower()

    def test_tooltips_present(self, client):
        html = client.get('/').text
        assert html.count('class="info"') >= 8    # ⓘ across sections/metrics
        assert 'class="tip"' in html

    def test_bar_and_conids_columns_have_tooltips(self, client):
        html = client.get('/').text
        assert 'completed bar of this size' in html      # Bar column tip
        assert 'IB contract IDs' in html                 # ConIds column tip

    def test_tooltips_use_viewport_positioning(self, client):
        """Tips must escape section overflow:hidden — fixed positioning with
        viewport clamping, computed on hover by positionTip()."""
        html = client.get('/').text
        assert 'function positionTip' in html
        assert 'position: fixed' in html


class TestTabs:
    def test_three_tabs_with_overview_default(self, client):
        html = client.get('/').text
        for name in ('overview', 'strategies', 'risk'):
            assert f'data-tab="{name}"' in html
            assert f'id="tab-{name}"' in html
        # overview is the default-active pane
        assert 'id="tab-overview" class="tabpane active"' in html

    def test_sections_live_in_the_right_panes(self, client):
        html = client.get('/').text
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
        html = client.get('/').text
        assert 'location.hash' in html
