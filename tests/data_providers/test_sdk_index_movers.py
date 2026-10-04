import io
import json
from argparse import Namespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from rich.console import Console

from trader.data_providers.capabilities import Capability

NAN = float('nan')


def _index_frame():
    return pd.DataFrame([{'ticker': 'SPY', 'name': 'S&P 500 (ETF proxy)', 'close': 0.5, 'volume': NAN,
                          'change': 0.0, 'change_pct': 1.0, 'provider': 'etf_proxy',
                          'note': 'ETF proxy for S&P 500; IEX prices'}])


def _mmr():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    movers_provider, news_provider = MagicMock(), MagicMock()
    movers_provider.movers.return_value = _index_frame()
    news_provider.news.return_value = []
    mmr._provider = MagicMock(side_effect=lambda cap, source=None:
                              news_provider if cap == Capability.NEWS else movers_provider)
    mmr._provider_default = MagicMock(return_value='etf_proxy')
    mmr._alpaca_assets = MagicMock(return_value=None)
    return mmr, movers_provider


def test_indices_route_to_index_movers_capability():
    mmr, provider = _mmr()
    out = mmr.movers(market='indices', direction='gainers')
    mmr._provider.assert_called_once_with(Capability.MOVERS_INDICES, None)
    provider.movers.assert_called_once_with('indices', 'gainers')
    assert out['ticker'].tolist() == ['SPY']   # close 0.5 < min_price: the stock filter must not run
    mmr._alpaca_assets.assert_not_called()


def test_stocks_still_route_to_movers():
    mmr, provider = _mmr()
    provider.movers.return_value = _index_frame().assign(close=50.0)
    mmr.movers(market='stocks', direction='gainers', source='alpaca')
    mmr._provider.assert_called_once_with(Capability.MOVERS, 'alpaca')


def test_movers_detail_default_uses_market_capability():
    mmr, _ = _mmr()
    detail = mmr.movers_detail(market='indices', direction='gainers', num=5)
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_INDICES)
    assert detail[0]['ticker'] == 'SPY' and detail[0]['details']['name'] == 'S&P 500 (ETF proxy)'
    assert detail[0]['provider'] == 'etf_proxy' and detail[0]['note'] == 'ETF proxy for S&P 500; IEX prices'


def test_cli_movers_sources_and_default(capsys):
    from trader.mmr_cli import _handle_movers, build_parser
    parser = build_parser()
    assert parser.parse_args(['movers', '--market', 'indices', '--source', 'etf_proxy']).source == 'etf_proxy'
    assert parser.parse_args(['movers', '--source', 'alpaca']).source == 'alpaca'
    mmr = MagicMock()
    mmr._provider_default.return_value = 'etf_proxy'
    mmr.movers.return_value = _index_frame()
    _handle_movers(mmr, Namespace(losers=False, market='indices', source=None, detail=False, num=20,
                                  min_price=1.0))
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_INDICES)
    mmr.movers.assert_called_once_with(market='indices', direction='gainers', source='etf_proxy', min_price=1.0)
    assert 'etf_proxy' in capsys.readouterr().out


def test_movers_massive_override_keeps_massive_for_indices_and_forex():
    from trader.data_providers.registry import ProviderRegistry
    registry = ProviderRegistry.from_config({'data_providers': {'movers': 'massive'}})
    assert registry.default_source(Capability.MOVERS_INDICES) == 'massive'
    assert registry.default_source(Capability.MOVERS_FOREX) == 'massive'


def test_movers_alpaca_override_keeps_builtin_index_and_forex_movers():
    from trader.data_providers.registry import ProviderRegistry
    registry = ProviderRegistry.from_config({'data_providers': {'movers': 'alpaca'}})
    assert registry.default_source(Capability.MOVERS_INDICES) == 'etf_proxy'
    assert registry.default_source(Capability.MOVERS_FOREX) == 'computed_fx'


def test_explicit_market_override_wins_over_movers_override():
    from trader.data_providers.registry import ProviderRegistry
    registry = ProviderRegistry.from_config(
        {'data_providers': {'movers': 'massive', 'movers_indices': 'etf_proxy', 'movers_forex': 'computed_fx'}})
    assert registry.default_source(Capability.MOVERS_INDICES) == 'etf_proxy'
    assert registry.default_source(Capability.MOVERS_FOREX) == 'computed_fx'



@pytest.fixture
def cli_console(monkeypatch):
    from trader import mmr_cli
    buffer = io.StringIO()
    monkeypatch.setattr(mmr_cli, 'console', Console(file=buffer, width=200, color_system=None))
    return buffer


def _detail_args(source, market='indices'):
    return Namespace(losers=False, market=market, source=source, detail=True, num=5, min_price=1.0)


def _failed_etf_detail():
    return [{'ticker': 'XLRE', 'open': None, 'close': NAN, 'volume': None, 'change': NAN, 'change_pct': NAN,
             'details': {'name': 'S&P 500 Real Estate sector (ETF proxy)', 'exchange': '', 'description': ''},
             'ratios': {}, 'news': {}, 'provider': 'etf_proxy',
             'note': 'ETF proxy for S&P 500 Real Estate sector; IEX prices; alpaca has no snapshot for XLRE'}]


def test_detail_card_shows_dash_for_missing_change_and_keeps_label(cli_console):
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers_detail.return_value = _failed_etf_detail()
    _handle_movers(mmr, _detail_args('etf_proxy'))
    out = cli_console.getvalue()
    assert 'nan' not in out.lower()
    assert '—' in out and 'etf_proxy' in out and 'alpaca has no snapshot for XLRE' in out


def test_index_movers_table_uses_fx_precision(cli_console):
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers.return_value = _index_frame().assign(change=0.0013)
    _handle_movers(mmr, Namespace(losers=False, market='indices', source='etf_proxy', detail=False, num=20,
                                  min_price=1.0))
    assert '0.00130' in cli_console.getvalue()


def test_stock_movers_table_keeps_two_decimals(cli_console):
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers.return_value = _index_frame().assign(change=0.0013, close=50.0)
    _handle_movers(mmr, Namespace(losers=False, market='stocks', source='alpaca', detail=False, num=20,
                                  min_price=1.0))
    out = cli_console.getvalue()
    assert '0.00130' not in out and '50.00' in out


def test_twelvedata_credits_notice_waits_for_source_validation(cli_console, capsys):
    from trader.data_providers.errors import CapabilityNotSupported
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr._provider.side_effect = CapabilityNotSupported('movers_indices', 'twelvedata', ['etf_proxy', 'massive'])
    _handle_movers(mmr, _detail_args('twelvedata'))
    out = cli_console.getvalue() + capsys.readouterr().out
    assert 'credits' not in out and 'does not support' in out
    mmr.movers_detail.assert_not_called()


def test_twelvedata_credits_notice_after_validation(cli_console):
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers_detail.return_value = []
    _handle_movers(mmr, _detail_args('twelvedata', market='stocks'))
    assert 'credits' in cli_console.getvalue()


def test_movers_detail_json_is_pure_json(cli_console, capsys, monkeypatch):
    from trader import mmr_cli
    monkeypatch.setattr(mmr_cli, '_json_mode', True)
    mmr = MagicMock()
    mmr.movers_detail.return_value = _failed_etf_detail()
    mmr_cli._handle_movers(mmr, _detail_args('twelvedata', market='stocks'))
    out = capsys.readouterr().out
    payload = json.loads(out, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    assert payload['data'][0]['change'] is None and payload['data'][0]['provider'] == 'etf_proxy'
    assert cli_console.getvalue() == ''
