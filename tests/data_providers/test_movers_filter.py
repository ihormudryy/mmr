import datetime as dt
import json
from pathlib import Path

import pandas as pd

from trader.data_providers.alpaca.assets import AlpacaAssetDirectory
from trader.data_providers.movers_filter import filter_stock_movers

SAMPLE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_assets_sample.json').read_text())


class _Client:
    def get_json(self, path, params):
        return SAMPLE


ASSETS = AlpacaAssetDirectory(_Client(), cache_path=None,
                              now=lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc))


def _frame(rows):
    return pd.DataFrame([{'ticker': t, 'name': '', 'close': c, 'volume': float('nan'), 'change': 1.0,
                          'change_pct': p, 'provider': 'alpaca', 'note': ''} for t, c, p in rows])


def test_drops_sub_dollar_and_unknown_price():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0), ('PENNY', 0.5, 90.0), ('NOPRICE', float('nan'), 80.0)]),
                              min_price=1.0, assets=None)
    assert out['ticker'].tolist() == ['AAPL']


def test_drops_warrants_rights_units():
    out = filter_stock_movers(_frame([('HPAIW', 3.0, 99.0), ('GLLRF', 2.0, 50.0), ('KVAUF', 10.0, 40.0),
                                      ('AAPL', 300.0, 5.0)]), min_price=1.0, assets=ASSETS)
    assert out['ticker'].tolist() == ['AAPL']


def test_keeps_unit_corporation():
    out = filter_stock_movers(_frame([('UNTC', 30.0, 7.0)]), min_price=1.0, assets=ASSETS)
    assert out['ticker'].tolist() == ['UNTC']


def test_fills_names_from_assets():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0)]), min_price=1.0, assets=ASSETS)
    assert out.loc[0, 'name'] == 'Apple Inc. Common Stock'


def test_without_assets_notes_that_instrument_filter_is_off():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0)]), min_price=1.0, assets=None)
    assert 'warrant filter off' in out.loc[0, 'note']


def test_min_price_zero_keeps_pennies():
    out = filter_stock_movers(_frame([('PENNY', 0.5, 90.0)]), min_price=0.0, assets=None)
    assert out['ticker'].tolist() == ['PENNY']


def test_empty_frame_is_handled():
    out = filter_stock_movers(_frame([]).reindex(columns=_frame([('A', 1.0, 1.0)]).columns), min_price=1.0, assets=ASSETS)
    assert out.empty
