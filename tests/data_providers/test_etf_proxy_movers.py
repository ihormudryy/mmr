import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trader.data_providers.alpaca.quotes import AlpacaQuotes
from trader.data_providers.capabilities import MOVER_COLUMNS, Capability, make_quote
from trader.data_providers.computed_movers import INDEX_PROXY_ETFS, EtfProxyMovers
from trader.data_providers.errors import CapabilityNotSupported, ProviderError, ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_snapshots_index_etfs.json').read_text())
ALPACA = {'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def _frame(direction='gainers'):
    client = FakeClient(FIXTURE)
    return EtfProxyMovers(AlpacaQuotes(client)).movers('indices', direction), client


def test_etf_list_is_explicit():
    assert dict(INDEX_PROXY_ETFS) == {
        'SPY': 'S&P 500', 'QQQ': 'Nasdaq-100', 'DIA': 'Dow Jones Industrial Average', 'IWM': 'Russell 2000',
        'XLK': 'S&P 500 Technology sector', 'XLF': 'S&P 500 Financials sector', 'XLE': 'S&P 500 Energy sector',
        'XLV': 'S&P 500 Health Care sector', 'XLI': 'S&P 500 Industrials sector',
        'XLY': 'S&P 500 Consumer Discretionary sector', 'XLP': 'S&P 500 Consumer Staples sector',
        'XLU': 'S&P 500 Utilities sector', 'XLB': 'S&P 500 Materials sector',
        'XLRE': 'S&P 500 Real Estate sector', 'XLC': 'S&P 500 Communication Services sector',
    }


def test_ranks_etfs_from_one_alpaca_iex_request():
    frame, client = _frame()
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == '/v2/stocks/snapshots' and params['feed'] == 'iex'
    assert params['symbols'].split(',') == [etf for etf, _ in INDEX_PROXY_ETFS]
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist()[:3] == ['QQQ', 'SPY', 'XLRE']
    assert len(frame) == len(INDEX_PROXY_ETFS)


def test_every_row_says_etf_proxy():
    frame, _ = _frame()
    assert set(frame['provider']) == {'etf_proxy'}
    assert frame['note'].str.startswith('ETF proxy for ').all()
    assert frame['name'].str.endswith('(ETF proxy)').all()
    qqq = frame.set_index('ticker').loc['QQQ']
    assert qqq['name'] == 'Nasdaq-100 (ETF proxy)'
    assert qqq['note'] == 'ETF proxy for Nasdaq-100; IEX prices as of 2026-10-02T20:54:33Z'
    assert qqq['close'] == 749.2 and qqq['change_pct'] == pytest.approx(0.9676, abs=1e-4)
    assert math.isnan(qqq['volume'])


def test_failed_etf_quote_keeps_labelled_row():
    frame, _ = _frame()
    dia = frame.set_index('ticker').loc['DIA']
    assert math.isnan(dia['change_pct']) and math.isnan(dia['close'])
    assert dia['note'] == 'ETF proxy for Dow Jones Industrial Average; IEX prices; alpaca has no snapshot for DIA'


def test_losers_put_biggest_drop_first():
    quotes = MagicMock()
    quotes.quotes.return_value = [
        make_quote(etf, last=1.0, change=0.0, change_pct=float(i), time='2026-10-02T20:00:00Z', feed='iex')
        for i, (etf, _) in enumerate(INDEX_PROXY_ETFS)]
    frame = EtfProxyMovers(quotes).movers('indices', 'losers')
    assert frame['ticker'].iloc[0] == 'SPY' and frame['ticker'].iloc[-1] == 'XLC'


def test_all_quotes_failing_raises():
    quotes = MagicMock()
    quotes.quotes.return_value = [make_quote(etf, feed='iex', error=f'alpaca has no snapshot for {etf}')
                                  for etf, _ in INDEX_PROXY_ETFS]
    with pytest.raises(ProviderError, match='no ETF quote'):
        EtfProxyMovers(quotes).movers('indices', 'gainers')


def test_rejects_other_markets():
    with pytest.raises(CapabilityNotSupported, match='stocks movers'):
        EtfProxyMovers(MagicMock()).movers('stocks', 'gainers')


def test_registry_index_movers_default_and_sources():
    registry = ProviderRegistry.from_config(dict(ALPACA, massive_api_key='m'))
    assert registry.default_source(Capability.MOVERS_INDICES) == 'etf_proxy'
    assert isinstance(registry.get(Capability.MOVERS_INDICES), EtfProxyMovers)
    assert type(registry.get(Capability.MOVERS_INDICES, 'massive')).__name__ == 'MassiveMovers'


def test_etf_proxy_without_alpaca_keys_names_env_var():
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_KEY_ID'):
        ProviderRegistry.from_config({}).get(Capability.MOVERS_INDICES)


def test_wrong_source_for_indices_names_supported():
    with pytest.raises(CapabilityNotSupported) as info:
        ProviderRegistry.from_config(ALPACA).get(Capability.MOVERS_INDICES, 'alpaca')
    assert info.value.supported == ['etf_proxy', 'massive']
