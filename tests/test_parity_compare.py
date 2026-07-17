"""[COMPAT] Task 3: field-level parity core (tolerances, keys, exit codes).

The classes/functions above `TestCollectLegacy` are the untouched parity
CORE (12 tests) -- see `scripts/parity_compare.py`'s module docstring.
Everything from `TestNormalizeIbStatus` on is new coverage for the rebuilt
collectors (`collect_legacy`/`collect_center`), added as part of the COMPAT
T3 collector rebuild.
"""
import json
import urllib.request

import pytest

import web.app as webapp
from scripts.parity_compare import (
    CollectionError, FieldDivergence, ParityReport, _center_cash_rows,
    _center_risk_rows, _merge_active_terminal, _round2, collect_center,
    collect_legacy, compare_keyed, compare_surfaces, normalize_center_proposal,
    normalize_ib_status, normalize_legacy_proposal, values_match)


class TestValuesMatch:
    def test_float_within_tolerance(self):
        assert values_match(100.0000004, 100.0000009, 'float')

    def test_float_beyond_tolerance_diverges(self):
        assert not values_match(100.0, 100.000002, 'float')

    def test_timestamps_compare_at_second_resolution(self):
        assert values_match('2026-07-15T13:42:17.201Z',
                            '2026-07-15T13:42:17.899+00:00', 'timestamp')
        assert not values_match('2026-07-15T13:42:17Z',
                                '2026-07-15T13:42:18Z', 'timestamp')

    def test_naive_timestamp_is_treated_as_utc(self):
        assert values_match('2026-07-15T13:42:17', '2026-07-15T13:42:17Z', 'timestamp')

    def test_none_on_one_side_diverges(self):
        assert not values_match(None, 0.0, 'float')
        assert values_match(None, None, 'float')

    def test_loose_map_mixes_numeric_and_text(self):
        assert values_match({'RANGE_MINUTES': 45, 'TZ': 'Australia/Sydney'},
                            {'RANGE_MINUTES': '45', 'TZ': 'Australia/Sydney'},
                            'loose_map')
        assert not values_match({'RANGE_MINUTES': 45}, {'RANGE_MINUTES': 30},
                                'loose_map')


class TestProposalNormalization:
    def test_executed_maps_to_order_submitted_on_both_sides(self):
        legacy = normalize_legacy_proposal(
            {'id': 7, 'storage_status': 'EXECUTED', 'display_status': 'ORDER_SUBMITTED',
             'symbol': 'AMD', 'action': 'BUY', 'quantity': 10, 'amount': None,
             'confidence': 0.7})
        center = normalize_center_proposal(
            {'id': 7, 'status': 'EXECUTED', 'symbol': 'AMD', 'action': 'BUY',
             'quantity': 10, 'amount': None, 'confidence': 0.7})
        assert legacy['display_status'] == center['display_status'] == 'ORDER_SUBMITTED'
        assert legacy['storage_status'] == center['storage_status'] == 'EXECUTED'


class TestCompareKeyed:
    FIELDS = {'quantity': 'float'}

    def test_presence_divergence_when_row_missing(self):
        divs = compare_keyed('positions', [{'key': 5437, 'quantity': 100.0}], [],
                             self.FIELDS, allow=[])
        assert divs == [FieldDivergence('positions', '5437', '<presence>',
                                        True, False, False)]

    def test_allow_pattern_marks_explained(self):
        divs = compare_keyed('positions',
                             [{'key': 5437, 'quantity': 100.0}],
                             [{'key': 5437, 'quantity': 99.0}],
                             self.FIELDS, allow=['positions:5437:quantity'])
        assert divs[0].explained is True


class TestReport:
    def _report(self, explained):
        return ParityReport(generated_at='2026-07-15T14:00:00Z', counts={'positions': 1},
                            divergences=[FieldDivergence('positions', '5437', 'quantity',
                                                         100.0, 99.0, explained)])

    def test_unexplained_divergence_exits_one(self):
        assert self._report(explained=False).exit_code() == 1

    def test_explained_divergence_exits_zero_but_is_reported(self):
        report = self._report(explained=True)
        assert report.exit_code() == 0
        assert json.loads(report.to_json())['divergences'][0]['explained'] is True


def test_compare_surfaces_covers_every_required_section():
    empty = {s: [] for s in ('account', 'cash', 'positions', 'proposals',
                             'strategies', 'risk', 'orders', 'fills')}
    report = compare_surfaces(empty, empty, allow=[])
    assert set(report.counts) == set(empty)
    assert report.exit_code() == 0


# =============================================================================
# Rebuilt collectors: collect_legacy / collect_center
# =============================================================================

class TestNormalizeIbStatus:
    """L1/L4: `trader.trading.order_tracker.normalize_ib_status` does not
    exist -- this is the small local replacement."""

    def test_maps_known_ib_statuses_to_canonical_upper(self):
        assert normalize_ib_status('Submitted') == 'SUBMITTED'
        assert normalize_ib_status('PreSubmitted') == 'PRESUBMITTED'
        assert normalize_ib_status('PendingSubmit') == 'PENDING_SUBMIT'
        assert normalize_ib_status('PendingCancel') == 'PENDING_CANCEL'
        assert normalize_ib_status('ApiPending') == 'API_PENDING'
        assert normalize_ib_status('Filled') == 'FILLED'
        assert normalize_ib_status('Cancelled') == 'CANCELLED'
        assert normalize_ib_status('ApiCancelled') == 'CANCELLED'
        assert normalize_ib_status('Inactive') == 'INACTIVE'

    def test_unknown_status_upper_cased_not_crashed(self):
        assert normalize_ib_status('SomeNewIBStatus') == 'SOMENEWIBSTATUS'

    def test_none_maps_to_empty_string(self):
        assert normalize_ib_status(None) == ''


def _install_fake_legacy(monkeypatch, *, account='DU123456', net_liq=100000.0,
                         currencies=None, positions=None, proposals=None,
                         strategies=None, status_override=None,
                         snapshot_override=None):
    """Monkeypatch `web.app`'s fetchers so `collect_legacy()` runs against
    fully controlled, SDK-shaped fake data instead of a real trader_service
    (mirrors the monkeypatch style already used in test_web_dashboard.py)."""
    monkeypatch.setattr(webapp, 'fetch_status',
                        lambda: status_override if status_override is not None
                        else {'connected': True, 'account': account})
    monkeypatch.setattr(webapp, 'fetch_snapshot',
                        lambda: snapshot_override if snapshot_override is not None
                        else {'net_liquidation': net_liq})
    monkeypatch.setattr(webapp, 'fetch_cash',
                        lambda: {'account': account,
                                 'currencies': currencies or {}})
    monkeypatch.setattr(webapp, 'fetch_positions', lambda: positions or [])
    monkeypatch.setattr(webapp, 'fetch_proposals', lambda: proposals or [])
    monkeypatch.setattr(webapp, 'fetch_strategies', lambda: strategies or [])


class TestCollectLegacy:
    def test_no_crash_and_right_shape_on_full_fake_data(self, monkeypatch):
        _install_fake_legacy(
            monkeypatch, account='DU123456', net_liq=100000.0,
            currencies={'USD': {'cash': 5000.0, 'exchange_rate': 1.0, 'base_value': 5000.0}},
            positions=[{'conId': 265598, 'symbol': 'AMD', 'position': 10,
                        'avgCost': 150.0, 'marketValue': 1550.0,
                        'unrealizedPNL': 50.0}],
            proposals=[{'id': 7, 'storage_status': 'PENDING',
                       'display_status': 'PENDING', 'symbol': 'AMD',
                       'action': 'BUY', 'size': '10 sh', 'confidence': 0.7}],
            strategies=[{'name': 'my_strat', 'enabled': True,
                        'params': {'EMA_PERIOD': 20}}],
        )

        result = collect_legacy()

        assert result['account'] == [{'key': 'account', 'account_id': 'DU123456',
                                      'mode': 'paper', 'net_liquidation': 100000.0}]
        assert result['cash'] == [{'key': 'USD', 'amount': 5000.0}]
        assert result['positions'] == [{'key': 265598, 'quantity': 10,
                                        'avg_cost': 150.0, 'market_value': 1550.0,
                                        'unrealized_pnl': 50.0}]
        assert len(result['proposals']) == 1
        assert result['proposals'][0]['storage_status'] == 'PENDING'
        assert result['proposals'][0]['symbol'] == 'AMD'
        assert result['strategies'] == [{'key': 'my_strat', 'enabled': True}]
        # Dropped sections: always empty, never crash (see module docstring).
        assert result['risk'] == []
        assert result['orders'] == []
        assert result['fills'] == []

    def test_unreachable_trader_service_raises_collection_error(self, monkeypatch):
        _install_fake_legacy(monkeypatch, status_override=None)
        monkeypatch.setattr(webapp, 'fetch_status', lambda: None)
        with pytest.raises(CollectionError):
            collect_legacy()

    def test_live_account_maps_to_live_mode(self, monkeypatch):
        _install_fake_legacy(monkeypatch, account='U26774889', net_liq=1.0)
        result = collect_legacy()
        assert result['account'][0]['mode'] == 'live'


def _fake_broker_account(*, account_id='DU123456', account_mode='paper',
                         net_liquidation=100000.004, balances=None):
    return {
        'account_id': account_id, 'account_mode': account_mode,
        'net_liquidation': net_liquidation, 'total_cash': None,
        'buying_power': None, 'available_funds': None,
        'maintenance_margin': None, 'balances': balances or {},
        'revision': 1, 'source_timestamp': '2026-07-15T00:00:00+00:00',
        'entity_id': account_id, 'entity_revision': 1,
    }


def _fake_broker_position(*, account_id='DU123456', conid=265598,
                          quantity=10.0, average_cost=150.0,
                          market_value=1550.0, unrealized_pnl=50.0):
    return {
        'account_id': account_id, 'conid': conid, 'symbol': 'AMD',
        'sec_type': 'STK', 'exchange': 'SMART', 'currency': 'USD',
        'quantity': quantity, 'average_cost': average_cost,
        'market_price': 155.0, 'market_value': market_value,
        'unrealized_pnl': unrealized_pnl, 'realized_pnl': 0.0,
        'daily_pnl': 5.0, 'deleted': False, 'revision': 1,
        'source_timestamp': '2026-07-15T00:00:00+00:00',
        'entity_id': f'{account_id}:{conid}', 'entity_revision': 1,
    }


def _fake_proposal_payload(*, proposal_id=7, account_id='DU123456',
                           status='PENDING', symbol='AMD', action='BUY',
                           confidence=0.7):
    return {
        'id': proposal_id, 'symbol': symbol, 'action': action,
        'quantity': None, 'amount': None, 'execution': {}, 'reasoning': '',
        'confidence': confidence, 'thesis': '', 'source': 'manual',
        'metadata': {}, 'status': status,
        'created_at': '2026-07-15T00:00:00+00:00',
        'updated_at': '2026-07-15T00:00:00+00:00', 'order_ids': [],
        'rejection_reason': '', 'sec_type': 'STK', 'account_id': account_id,
        'account_mode': 'paper', 'conid': 265598, 'reference_price': None,
        'reference_timestamp': None, 'reference_quote_side': None,
        'reference_feed_type': None, 'max_price_drift_bps': None,
        'expires_at': None, 'live_approval_eligible': False, 'revision': 1,
        'order_group_id': None,
    }


def _fake_strategy_row(*, strategy_name='my_strat', strategy_state='RUNNING'):
    return {
        'strategy_name': strategy_name, 'action': 'enable_strategy',
        'strategy_state': strategy_state, 'control_revision': 1,
        'state_revision': 1, 'error': None,
        'entity_id': strategy_name, 'entity_revision': 1,
    }


def _fake_snapshot_view(*, accounts=None, positions=None,
                        proposals_active=None, proposals_terminal=None,
                        strategies=None, risk=None, orders_active=None,
                        orders_terminal=None, fills=None):
    """A `DashboardState.snapshot_view()`-shaped dict -- top-level PLURAL
    keys, `proposals`/`orders` as `{active, terminal}`, `risk` as a dict
    keyed by entity_id. No `entities` key (C0)."""
    return {
        'schema_version': 1, 'stream_id': 'stream-1', 'sequence': 1,
        'generated_at': '2026-07-15T00:00:00+00:00', 'has_baseline': True,
        'last_event_at': None,
        'accounts': accounts if accounts is not None else [_fake_broker_account()],
        'positions': positions or [],
        'quotes': {},
        'proposals': {'active': proposals_active or [],
                      'terminal': proposals_terminal or []},
        'orders': {'active': orders_active or [], 'terminal': orders_terminal or []},
        'fills': fills or [],
        'strategies': strategies or [],
        'risk': risk or {},
        'reconciliation': [], 'trading_control': [], 'commands': [],
    }


class _FakeHttpResponse:
    def __init__(self, body: bytes, headers=None):
        self._body = body
        self._headers = headers or {}

    def read(self):
        return self._body

    @property
    def headers(self):
        return self._headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_center_http(monkeypatch, view: dict):
    """Monkeypatch `urllib.request.urlopen` so `login()` + the
    `/api/snapshot` GET in `collect_center()` run against a fully
    controlled, `snapshot_view()`-shaped fake instead of a real HTTP
    server."""
    def _fake_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
        if url.endswith('/session'):
            return _FakeHttpResponse(b'', headers={'Set-Cookie': 'session=fake; Path=/'})
        if url.endswith('/api/snapshot'):
            return _FakeHttpResponse(json.dumps(view).encode())
        raise AssertionError(f'unexpected URL requested: {url}')

    monkeypatch.setattr(urllib.request, 'urlopen', _fake_urlopen)


class TestCenterHelpers:
    """Direct unit coverage for the small pure helpers `collect_center`
    delegates to (C2/C5/C6)."""

    def test_merge_active_terminal_flattens_both(self):
        merged = _merge_active_terminal({'active': [{'id': 1}], 'terminal': [{'id': 2}]})
        assert merged == [{'id': 1}, {'id': 2}]

    def test_merge_active_terminal_handles_missing_keys(self):
        assert _merge_active_terminal({}) == []

    def test_round2_rounds_floats_only(self):
        assert _round2(100.004) == 100.0
        assert _round2(None) is None
        assert _round2('n/a') == 'n/a'

    def test_center_cash_rows_parses_total_cash_value_tag(self):
        account = _fake_broker_account(balances={
            'TotalCashValue:USD': '5000.0',
            'TotalCashValue:BASE': '5000.0',  # skipped -- pseudo-currency
            'NetLiquidation:USD': '100000.0',  # skipped -- wrong tag
        })
        assert _center_cash_rows(account) == [{'key': 'USD', 'amount': 5000.0}]

    def test_center_risk_rows_empty_when_no_projection_present(self):
        assert _center_risk_rows({}) == []

    def test_center_risk_rows_best_effort_extraction_when_present(self):
        risk = {'projection:DU123456': {'kind': 'projection',
                                        'risk_id': 'projection:DU123456',
                                        'warnings': ['concentration'],
                                        'limits': {'max_position_pct': 10}}}
        assert _center_risk_rows(risk) == [
            {'key': 'projection:DU123456', 'warnings': ['concentration'],
             'limits': {'max_position_pct': 10}}]


class TestCollectCenter:
    def test_no_crash_and_right_shape_on_full_fake_data(self, monkeypatch):
        view = _fake_snapshot_view(
            accounts=[_fake_broker_account(
                balances={'TotalCashValue:USD': '5000.0'})],
            positions=[_fake_broker_position()],
            proposals_active=[_fake_proposal_payload()],
            strategies=[_fake_strategy_row()],
        )
        _install_fake_center_http(monkeypatch, view)

        result = collect_center('http://fake-dashboard', 'token123')

        assert result['account'] == [{'key': 'account', 'account_id': 'DU123456',
                                      'mode': 'paper', 'net_liquidation': 100000.0}]
        assert result['cash'] == [{'key': 'USD', 'amount': 5000.0}]
        assert result['positions'] == [{'key': 265598, 'quantity': 10.0,
                                        'avg_cost': 150.0, 'market_value': 1550.0,
                                        'unrealized_pnl': 50.0}]
        assert len(result['proposals']) == 1
        assert result['proposals'][0]['storage_status'] == 'PENDING'
        assert result['strategies'] == [{'key': 'my_strat', 'enabled': True}]
        # Dropped sections: always empty, never crash (see module docstring).
        assert result['risk'] == []
        assert result['orders'] == []
        assert result['fills'] == []

    def test_merges_active_and_terminal_proposals(self, monkeypatch):
        view = _fake_snapshot_view(
            proposals_active=[_fake_proposal_payload(proposal_id=1, status='PENDING')],
            proposals_terminal=[_fake_proposal_payload(proposal_id=2, status='EXECUTED')],
        )
        _install_fake_center_http(monkeypatch, view)
        result = collect_center('http://fake-dashboard', 'token123')
        assert {p['key'] for p in result['proposals']} == {1, 2}

    def test_disabled_strategy_state_is_not_enabled(self, monkeypatch):
        view = _fake_snapshot_view(
            strategies=[_fake_strategy_row(strategy_name='s1', strategy_state='DISABLED')])
        _install_fake_center_http(monkeypatch, view)
        result = collect_center('http://fake-dashboard', 'token123')
        assert result['strategies'] == [{'key': 's1', 'enabled': False}]

    def test_no_accounts_no_crash(self, monkeypatch):
        view = _fake_snapshot_view(accounts=[])
        _install_fake_center_http(monkeypatch, view)
        result = collect_center('http://fake-dashboard', 'token123')
        assert result['account'] == []
        assert result['cash'] == []


class TestCollectorsAgreeOnMatchingData:
    """End-to-end: build legacy + center fakes describing the SAME
    real-world state and confirm `compare_surfaces` finds zero divergence
    on the sections that are genuinely compared, then perturb one field and
    confirm exactly the expected divergence appears (no crash, no
    systematic false-positive noise)."""

    def _matching_pair(self, monkeypatch):
        _install_fake_legacy(
            monkeypatch, account='DU123456', net_liq=100000.0,
            currencies={'USD': {'cash': 5000.0, 'exchange_rate': 1.0, 'base_value': 5000.0}},
            positions=[{'conId': 265598, 'symbol': 'AMD', 'position': 10,
                        'avgCost': 150.0, 'marketValue': 1550.0,
                        'unrealizedPNL': 50.0}],
            proposals=[{'id': 7, 'storage_status': 'PENDING',
                       'display_status': 'PENDING', 'symbol': 'AMD',
                       'action': 'BUY', 'size': '10 sh', 'confidence': 0.7}],
            strategies=[{'name': 'my_strat', 'enabled': True}],
        )
        view = _fake_snapshot_view(
            accounts=[_fake_broker_account(
                account_id='DU123456', net_liquidation=100000.0,
                balances={'TotalCashValue:USD': '5000.0'})],
            positions=[_fake_broker_position(quantity=10.0, average_cost=150.0,
                                             market_value=1550.0, unrealized_pnl=50.0)],
            proposals_active=[_fake_proposal_payload(status='PENDING', confidence=0.7)],
            strategies=[_fake_strategy_row(strategy_name='my_strat', strategy_state='RUNNING')],
        )
        _install_fake_center_http(monkeypatch, view)
        return collect_legacy(), collect_center('http://fake-dashboard', 'token123')

    def test_zero_divergence_on_matching_state(self, monkeypatch):
        legacy, center = self._matching_pair(monkeypatch)
        report = compare_surfaces(legacy, center, allow=[])
        compared = ('account', 'cash', 'positions', 'proposals', 'strategies')
        assert [d for d in report.divergences if d.section in compared] == []
        # Dropped sections never contribute noise either.
        assert [d for d in report.divergences if d.section in ('risk', 'orders', 'fills')] == []
        assert report.exit_code() == 0

    def test_right_divergence_on_mismatched_position_quantity(self, monkeypatch):
        legacy, center = self._matching_pair(monkeypatch)
        center['positions'][0]['quantity'] = 999.0
        report = compare_surfaces(legacy, center, allow=[])
        position_divs = [d for d in report.divergences if d.section == 'positions']
        assert len(position_divs) == 1
        assert position_divs[0].field == 'quantity'
        assert position_divs[0].legacy == 10
        assert position_divs[0].center == 999.0
        assert position_divs[0].explained is False
        assert report.exit_code() == 1

    def test_right_divergence_on_mismatched_account_mode(self, monkeypatch):
        legacy, center = self._matching_pair(monkeypatch)
        center['account'][0]['mode'] = 'live'
        report = compare_surfaces(legacy, center, allow=[])
        account_divs = [d for d in report.divergences if d.section == 'account']
        assert len(account_divs) == 1
        assert account_divs[0].field == 'mode'
        assert report.exit_code() == 1
