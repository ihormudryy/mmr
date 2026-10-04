import math
from types import SimpleNamespace as NS

import pytest
from massive.exceptions import AuthError, BadResponse
from urllib3.exceptions import MaxRetryError, ProtocolError

from trader.data_providers.capabilities import OPTION_FIELDS, Capability, OptionsProvider
from trader.data_providers.errors import ProviderEntitlementError, ProviderError, ProviderRateLimited
from trader.data_providers.massive.options import MassiveOptions
from trader.data_providers.option_symbols import parse_option_symbol
from trader.data_providers.registry import ProviderRegistry

NOT_AUTHORIZED = BadResponse('{"status":"NOT_AUTHORIZED","request_id":"x",'
                             '"message":"You are not entitled to this data. Please upgrade your plan"}')


def _snap(strike, contract_type='call', iv=0.3, bid=2.0, ask=2.5, delta=0.5, ticker=None):
    right = 'C' if contract_type == 'call' else 'P'
    return NS(
        details=NS(ticker=ticker or f'O:AAPL260320{right}{int(strike * 1000):08d}', contract_type=contract_type,
                   strike_price=strike, expiration_date='2026-03-20'),
        last_quote=NS(bid=bid, ask=ask), last_trade=NS(price=(bid + ask) / 2), day=NS(volume=500.0),
        greeks=NS(delta=delta, gamma=0.02, theta=-0.05, vega=0.15),
        open_interest=1000.0, implied_volatility=iv, break_even_price=strike + bid,
        underlying_asset=NS(price=150.0),
    )


class FakeClient:
    def __init__(self, snaps=(), contracts=(), snapshot=None, error=None):
        self.snaps, self.contracts, self.snapshot, self.error = list(snaps), list(contracts), snapshot, error
        self.calls = []

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def list_snapshot_options_chain(self, underlying_asset, params):
        self.calls.append(('chain', underlying_asset, params))
        self._maybe_fail()
        return iter(self.snaps)

    def list_options_contracts(self, **kwargs):
        self.calls.append(('contracts', kwargs))
        self._maybe_fail()
        return iter(self.contracts)

    def get_snapshot_option(self, underlying_asset, option_contract):
        self.calls.append(('snapshot', underlying_asset, option_contract))
        self._maybe_fail()
        return self.snapshot


def test_chain_rows_have_shared_shape_and_percent_iv():
    client = FakeClient([_snap(250.0, delta=0.5), _snap(245.0, 'put', delta=-0.4), _snap(245.0, delta=0.6)])
    rows = MassiveOptions(client).chain('aapl', '2026-03-20')
    assert client.calls == [('chain', 'AAPL', {'expiration_date': '2026-03-20'})]
    assert [r['ticker'] for r in rows] == ['AAPL260320C00245000', 'AAPL260320C00250000', 'AAPL260320P00245000']
    first = rows[0]
    assert tuple(first) == OPTION_FIELDS
    assert first['iv'] == pytest.approx(30.0)
    assert (first['bid'], first['ask'], first['mid'], first['last']) == (2.0, 2.5, 2.25, 2.25)
    assert (first['volume'], first['open_interest'], first['break_even']) == (500.0, 1000.0, 247.0)
    assert first['delta'] == 0.6 and first['underlying_price'] == 150.0 and first['underlying'] == 'AAPL'
    assert first['provider'] == 'massive' and first['feed'] == 'opra'
    assert first['quote_time'] == '' and first['last_time'] == ''


def test_chain_filters_by_type():
    client = FakeClient([_snap(245.0), _snap(250.0), _snap(245.0, 'put')])
    rows = MassiveOptions(client).chain('AAPL', '2026-03-20', contract_type='call')
    assert [r['type'] for r in rows] == ['call', 'call']


def test_chain_filters_by_strike_range():
    client = FakeClient([_snap(240.0), _snap(250.0), _snap(260.0)])
    rows = MassiveOptions(client).chain('AAPL', '2026-03-20', strike_min=245.0, strike_max=255.0)
    assert [r['strike'] for r in rows] == [250.0]


def test_missing_fields_are_nan_not_zero():
    bare = NS(details=NS(ticker='O:AAPL260320P00250000', contract_type='put', strike_price=250.0,
                         expiration_date='2026-03-20'),
              last_quote=None, last_trade=None, day=None, greeks=None, open_interest=None,
              implied_volatility=None, break_even_price=None, underlying_asset=None)
    (row,) = MassiveOptions(FakeClient([bare])).chain('AAPL', '2026-03-20')
    for column in ('bid', 'ask', 'mid', 'last', 'volume', 'open_interest', 'iv', 'delta', 'gamma',
                   'theta', 'vega', 'break_even', 'underlying_price'):
        assert math.isnan(row[column]), column


def test_rows_without_details_are_skipped():
    no_details = NS(details=None)
    assert MassiveOptions(FakeClient([no_details, _snap(250.0)])).chain('AAPL', '2026-03-20')[0]['strike'] == 250.0


def test_unparseable_provider_ticker_raises():
    with pytest.raises(ProviderError, match='massive returned an option symbol'):
        MassiveOptions(FakeClient([_snap(250.0, ticker='O:WEIRD')])).chain('AAPL', '2026-03-20')


def test_contract_snapshot():
    snapshot = NS(break_even_price=253.0, implied_volatility=0.35, open_interest=5000,
                  last_quote=NS(bid=3.0, ask=3.5), last_trade=NS(price=3.25),
                  greeks=NS(delta=0.45, gamma=0.02, theta=-0.08, vega=0.20),
                  underlying_asset=NS(price=248.0, ticker='AAPL'), day=NS(volume=1200))
    client = FakeClient(snapshot=snapshot)
    row = MassiveOptions(client).contract(parse_option_symbol('O:AAPL260320C00250000'))
    assert client.calls == [('snapshot', 'AAPL', 'O:AAPL260320C00250000')]
    assert (row['ticker'], row['type'], row['strike'], row['expiration']) == (
        'AAPL260320C00250000', 'call', 250.0, '2026-03-20')
    assert row['iv'] == pytest.approx(35.0) and row['delta'] == 0.45
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 248.0 and row['mid'] == 3.25


def test_expirations_unique_sorted():
    contracts = [NS(expiration_date='2026-11-20'), NS(expiration_date='2026-10-16'),
                 NS(expiration_date='2026-11-20'), NS(expiration_date=None)]
    client = FakeClient(contracts=contracts)
    assert MassiveOptions(client).expirations('aapl') == ['2026-10-16', '2026-11-20']
    assert client.calls == [('contracts', dict(underlying_ticker='AAPL', expired=False, limit=1000,
                                               sort='expiration_date', order='asc'))]


@pytest.mark.parametrize('call', [
    lambda options: options.chain('AAPL', '2026-03-20'),
    lambda options: options.expirations('AAPL'),
    lambda options: options.contract(parse_option_symbol('O:AAPL260320C00250000')),
])
def test_not_authorized_maps_to_entitlement_error(call):
    with pytest.raises(ProviderEntitlementError) as raised:
        call(MassiveOptions(FakeClient(error=NOT_AUTHORIZED)))
    assert 'NOT_AUTHORIZED' in str(raised.value) and '--source alpaca' in str(raised.value)


def test_other_bad_response_is_a_plain_provider_error():
    with pytest.raises(ProviderError) as raised:
        MassiveOptions(FakeClient(error=BadResponse('{"status":"ERROR","message":"boom"}'))).chain('AAPL', '2026-03-20')
    assert not isinstance(raised.value, ProviderEntitlementError)


def test_registry_builds_massive_options():
    from trader.data_providers.builtin import source_choices
    provider = ProviderRegistry.from_config({'massive_api_key': 'k'}).get(Capability.OPTIONS, 'massive')
    assert isinstance(provider, MassiveOptions) and isinstance(provider, OptionsProvider)
    assert 'massive' in source_choices(Capability.OPTIONS)


CALLS = [
    lambda options: options.chain('AAPL', '2026-03-20'),
    lambda options: options.expirations('AAPL'),
    lambda options: options.contract(parse_option_symbol('O:AAPL260320C00250000')),
]


@pytest.mark.parametrize('call', CALLS)
def test_auth_error_maps_to_entitlement_error(call):
    error = AuthError('Must specify env var MASSIVE_API_KEY or pass api_key in constructor')
    with pytest.raises(ProviderEntitlementError) as raised:
        call(MassiveOptions(FakeClient(error=error)))
    assert 'MASSIVE_API_KEY' in str(raised.value) and '--source alpaca' in str(raised.value)
    assert raised.value.__cause__ is error


@pytest.mark.parametrize('call', CALLS)
def test_retries_exhausted_on_429_is_rate_limited(call):
    error = MaxRetryError(None, '/v3/snapshot/options/AAPL', reason=Exception('too many 429 error responses'))
    with pytest.raises(ProviderRateLimited) as raised:
        call(MassiveOptions(FakeClient(error=error)))
    assert raised.value.__cause__ is error


@pytest.mark.parametrize('call', CALLS)
def test_network_failure_is_a_plain_provider_error(call):
    error = ProtocolError('Connection aborted.')
    with pytest.raises(ProviderError) as raised:
        call(MassiveOptions(FakeClient(error=error)))
    assert not isinstance(raised.value, (ProviderEntitlementError, ProviderRateLimited))
    assert raised.value.__cause__ is error


def test_non_massive_exceptions_are_not_swallowed():
    with pytest.raises(KeyError):
        MassiveOptions(FakeClient(error=KeyError('bug'))).chain('AAPL', '2026-03-20')


def test_connection_failure_on_a_url_containing_429_is_not_rate_limited():
    error = MaxRetryError(None, '/v3/snapshot/options/AAPL/O:AAPL260320C00429000',
                          reason=ProtocolError('Connection aborted.'))
    with pytest.raises(ProviderError) as raised:
        MassiveOptions(FakeClient(error=error)).contract(parse_option_symbol('O:AAPL260320C00429000'))
    assert not isinstance(raised.value, ProviderRateLimited)


def test_rate_limit_test_uses_the_real_exhausted_retry_reason():
    from urllib3.exceptions import ResponseError
    error = MaxRetryError(None, '/v3/snapshot/options/AAPL', reason=ResponseError('too many 429 error responses'))
    with pytest.raises(ProviderRateLimited):
        MassiveOptions(FakeClient(error=error)).chain('AAPL', '2026-03-20')


@pytest.mark.parametrize('spelling', ['CALL', ' Call ', 'call'])
def test_chain_accepts_contract_type_in_any_case(spelling):
    client = FakeClient([_snap(245.0), _snap(245.0, 'put')])
    rows = MassiveOptions(client).chain('AAPL', '2026-03-20', contract_type=spelling)
    assert [r['type'] for r in rows] == ['call']


@pytest.mark.parametrize('bad', ['calls', '', 'C', 'ca\u0131l'])
def test_chain_rejects_unknown_contract_type_before_any_request(bad):
    client = FakeClient([_snap(245.0)])
    with pytest.raises(ValueError, match='contract_type'):
        MassiveOptions(client).chain('AAPL', '2026-03-20', contract_type=bad)
    assert client.calls == []
