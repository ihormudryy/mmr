import math
from unittest.mock import MagicMock

import pytest

from trader.data_providers.capabilities import Capability, make_quote
from trader.sdk import MMR


def _mmr_with_quotes(quotes):
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.quotes.return_value = quotes
    mmr._provider = MagicMock(return_value=provider)
    return mmr, provider


def test_snapshot_maps_quote_to_legacy_shape():
    mmr, _ = _mmr_with_quotes([make_quote('AAPL', last=2.0, bid=1.9, bid_size=3.0, exchange='NASDAQ',
                                          currency='USD', name='Apple', feed='iex', time='t')])
    snap = mmr.snapshot('AAPL', source='alpaca')
    mmr._provider.assert_called_once_with(Capability.QUOTES, 'alpaca')
    assert snap['symbol'] == 'AAPL' and snap['conId'] == '' and snap['last'] == 2.0
    assert snap['bid'] == 1.9 and snap['bidSize'] == 3.0
    assert math.isnan(snap['lastSize']) and math.isnan(snap['halted'])
    assert snap['feed'] == 'iex' and snap['name'] == 'Apple'


def test_snapshot_raises_on_error_quote():
    mmr, _ = _mmr_with_quotes([make_quote('ZZZZQ', error='alpaca has no snapshot for ZZZZQ')])
    with pytest.raises(ValueError, match='no snapshot for ZZZZQ'):
        mmr.snapshot('ZZZZQ', source='alpaca')


def test_snapshot_batch_keeps_error_rows():
    mmr, _ = _mmr_with_quotes([make_quote('AAPL', last=2.0), make_quote('ZZZZQ', error='nope')])
    rows = mmr.snapshot_batch(['AAPL', 'ZZZZQ'], source='twelvedata')
    assert [r['symbol'] for r in rows] == ['AAPL', 'ZZZZQ']
    assert rows[0]['last'] == 2.0 and rows[0]['error'] == ''
    assert rows[1]['error'] == 'nope' and math.isnan(rows[1]['last'])


def test_cli_snapshot_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_snapshot
    mmr = MagicMock()
    mmr.snapshot.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_snapshot(mmr, Namespace(symbol='AAPL', delayed=False, exchange='', currency='', source='alpaca'),
                     'snapshot')
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_snapshot_sources_come_from_registry():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['snapshot', 'AAPL', '--source', 'twelvedata']).source == 'twelvedata'
    assert parser.parse_args(['snapshot-batch', 'AAPL', '--source', 'ib']).source == 'ib'
