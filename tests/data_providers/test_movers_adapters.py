from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.data_providers.capabilities import MOVER_COLUMNS, Capability
from trader.data_providers.errors import CapabilityNotSupported
from trader.data_providers.massive.movers import MassiveMovers
from trader.data_providers.twelvedata.movers import TwelveDataMovers


def assert_movers_frame(frame: pd.DataFrame, direction: str) -> None:
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    expected = frame['change_pct'].sort_values(ascending=(direction == 'losers')).tolist()
    assert frame['change_pct'].tolist() == expected


def _snap(ticker, change_pct, close=10.0):
    return SimpleNamespace(ticker=ticker, day=SimpleNamespace(close=close, volume=1000.0),
                           todays_change=1.0, todays_change_percent=change_pct)


def test_massive_movers_shape_and_sort():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [_snap('A', 5.0), _snap('B', 9.0)]
    frame = MassiveMovers(client).movers('stocks', 'gainers')
    client.get_snapshot_direction.assert_called_once_with(market_type='stocks', direction='gainers')
    assert_movers_frame(frame, 'gainers')
    assert frame['ticker'].tolist() == ['B', 'A']
    assert set(frame['provider']) == {'massive'}


def test_massive_movers_tolerates_missing_day():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [SimpleNamespace(ticker='A', day=None, todays_change=None,
                                                                  todays_change_percent=-3.0)]
    frame = MassiveMovers(client).movers('crypto', 'losers')
    assert frame.loc[0, 'ticker'] == 'A' and pd.isna(frame.loc[0, 'close'])


class _Payload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


def test_twelvedata_movers_shape_and_sort():
    client = MagicMock()
    client.get_market_movers.return_value = _Payload({'values': [
        {'symbol': 'A', 'name': 'Alpha', 'exchange': 'NYSE', 'last': '10', 'volume': '5', 'change': '1',
         'percent_change': '-2'},
        {'symbol': 'B', 'name': 'Beta', 'exchange': 'NYSE', 'last': '20', 'volume': '6', 'change': '2',
         'percent_change': '-7'},
    ]})
    frame = TwelveDataMovers(client).movers('stocks', 'losers')
    assert_movers_frame(frame, 'losers')
    assert frame['ticker'].tolist() == ['B', 'A'] and frame.loc[0, 'name'] == 'Beta'
    assert frame.loc[0, 'close'] == 20.0


def test_twelvedata_rejects_unsupported_market():
    with pytest.raises(CapabilityNotSupported, match='crypto movers'):
        TwelveDataMovers(MagicMock()).movers('crypto', 'gainers')


def test_sdk_movers_uses_registry_default():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.movers.return_value = pd.DataFrame([{'ticker': 'A', 'name': '', 'close': 5.0, 'volume': 1.0,
                                                   'change': 1.0, 'change_pct': 3.0, 'provider': 'x', 'note': ''}])
    mmr._provider = MagicMock(return_value=provider)
    mmr._alpaca_assets = MagicMock(return_value=None)  # used from Task 8 on; harmless before
    frame = mmr.movers(market='stocks', direction='gainers')
    mmr._provider.assert_called_once_with(Capability.MOVERS, None)
    assert frame['ticker'].tolist() == ['A']


def test_cli_movers_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_movers(mmr, Namespace(losers=False, market='stocks', source=None, detail=False, num=20,
                                  min_price=1.0))
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_movers_source_default_is_none():
    from trader.mmr_cli import build_parser
    args = build_parser().parse_args(['movers'])
    assert args.source is None
