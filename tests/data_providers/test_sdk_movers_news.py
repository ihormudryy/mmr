from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import Capability, make_news_item
from trader.data_providers.registry import ProviderRegistry


def _frame():
    return pd.DataFrame([
        {'ticker': 'HPAIW', 'name': '', 'close': 3.0, 'volume': float('nan'), 'change': 1.0, 'change_pct': 99.0,
         'provider': 'alpaca', 'note': ''},
        {'ticker': 'PENNY', 'name': '', 'close': 0.2, 'volume': float('nan'), 'change': 0.1, 'change_pct': 50.0,
         'provider': 'alpaca', 'note': ''},
        {'ticker': 'AAPL', 'name': '', 'close': 300.0, 'volume': float('nan'), 'change': 3.0, 'change_pct': 1.0,
         'provider': 'alpaca', 'note': ''},
    ])


def _mmr(frame, news_items=(), assets=None):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    movers_provider, news_provider = MagicMock(), MagicMock()
    movers_provider.movers.return_value = frame
    news_provider.news.return_value = list(news_items)
    mmr._provider = MagicMock(side_effect=lambda cap, source=None:
                              movers_provider if cap == Capability.MOVERS else news_provider)
    mmr._provider_default = MagicMock(return_value='alpaca')
    mmr._alpaca_assets = MagicMock(return_value=assets)
    return mmr


class _Assets:
    def load(self):
        return self

    def is_derivative_unit(self, symbol):
        return symbol.endswith('W')

    def name(self, symbol):
        return {'AAPL': 'Apple Inc.'}.get(symbol, '')

    def exchange(self, symbol):
        return 'NASDAQ'


def test_stock_movers_are_filtered():
    out = _mmr(_frame(), assets=_Assets()).movers('stocks', 'gainers')
    assert out['ticker'].tolist() == ['AAPL'] and out.loc[0, 'name'] == 'Apple Inc.'


def test_asset_list_failure_only_turns_instrument_filter_off():
    from trader.data_providers.errors import ProviderEntitlementError
    mmr = _mmr(_frame())
    mmr._alpaca_assets = MagicMock(side_effect=ProviderEntitlementError('alpaca rejected the API key'))
    out = mmr.movers('stocks', 'gainers', source='massive')
    assert out['ticker'].tolist() == ['HPAIW', 'AAPL']
    assert all('warrant filter off' in note for note in out['note'])


def test_crypto_movers_are_not_filtered():
    out = _mmr(_frame(), assets=_Assets()).movers('crypto', 'gainers')
    assert len(out) == 3


def test_movers_default_ignores_default_data_source():
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.MOVERS) == 'alpaca'
    assert registry.default_source(Capability.NEWS) == 'alpaca'


def test_movers_detail_from_capabilities():
    news = [make_news_item(title='Apple up', sentiment='')]
    detail = _mmr(_frame(), news_items=news, assets=_Assets()).movers_detail('stocks', 'gainers', num=5)
    assert [d['ticker'] for d in detail] == ['AAPL']
    row = detail[0]
    assert row['details'] == {'name': 'Apple Inc.', 'exchange': 'NASDAQ', 'description': ''}
    assert row['news'] == {'headline': 'Apple up', 'sentiment': ''}
    assert row['ratios'] == {} and row['close'] == 300.0 and row['open'] is None


def test_cli_min_price_flag():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['movers', '--min-price', '0']).min_price == 0.0
    assert build_parser().parse_args(['movers']).min_price == 1.0
