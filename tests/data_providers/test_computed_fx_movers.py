import datetime as dt
import json
import math
from pathlib import Path

import pytest

from trader.data_providers.capabilities import MOVER_COLUMNS, Capability
from trader.data_providers.computed_movers import MAJOR_FX_PAIRS, ComputedFxMovers
from trader.data_providers.errors import CapabilityNotSupported, ProviderError
from trader.data_providers.frankfurter import FrankfurterForex
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'frankfurter_ecb_majors.json').read_text())


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.rows


def _movers(rows=FIXTURE):
    client = FakeClient(rows)
    return ComputedFxMovers(FrankfurterForex(client, today=lambda: dt.date(2026, 10, 4))), client


def test_major_pairs_list():
    assert [base + quote for base, quote in MAJOR_FX_PAIRS] == [
        'EURUSD', 'USDJPY', 'GBPUSD', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY']


def test_gainers_frame_in_one_request():
    movers, client = _movers()
    frame = movers.movers('forex', 'gainers')
    assert len(client.calls) == 1
    assert client.calls[0][1]['quotes'] == 'AUD,CAD,CHF,GBP,JPY,NZD,USD'
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist() == ['USDCAD', 'NZDUSD', 'AUDUSD', 'USDJPY', 'GBPUSD',
                                        'EURGBP', 'GBPJPY', 'EURUSD', 'EURJPY', 'USDCHF']
    usdjpy = frame.set_index('ticker').loc['USDJPY']
    assert usdjpy['name'] == 'USD/JPY' and usdjpy['close'] == pytest.approx(176.99 / 1.1225)
    assert usdjpy['change_pct'] == pytest.approx(-0.1956, abs=1e-4)
    assert math.isnan(usdjpy['volume'])
    assert set(frame['provider']) == {'computed_fx'}
    assert set(frame['note']) == {'ECB daily rates 2026-10-02 vs 2026-10-01 (daily, not live)'}
    assert frame.attrs['as_of'] == '2026-10-02'
    assert frame.set_index('ticker').loc['EURUSD', 'change'] == -0.0073


def test_losers_put_biggest_drop_first():
    frame = _movers()[0].movers('forex', 'losers')
    assert frame['ticker'].iloc[0] == 'USDCHF'
    assert frame['change_pct'].iloc[0] == pytest.approx(-1.0349, abs=1e-4)


def test_other_market_rejected():
    with pytest.raises(CapabilityNotSupported, match='stocks movers'):
        _movers()[0].movers('stocks', 'gainers')


def test_single_date_raises():
    rows = [r for r in FIXTURE if r['date'] == '2026-10-02']
    with pytest.raises(ProviderError, match='need 2'):
        _movers(rows)[0].movers('forex', 'gainers')


def test_registry_default_is_computed_fx_and_needs_no_key():
    from trader.data_providers.builtin import source_choices
    registry = ProviderRegistry.from_config({'default_data_source': 'massive'})
    assert registry.default_source(Capability.MOVERS_FOREX) == 'computed_fx'
    assert isinstance(registry.get(Capability.MOVERS_FOREX), ComputedFxMovers)
    assert source_choices(Capability.MOVERS_FOREX) == ['computed_fx', 'massive']


def test_cli_accepts_computed_fx_source():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['forex', 'movers', '--source', 'computed_fx']).source == 'computed_fx'
