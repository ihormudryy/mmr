"""Real Frankfurter calls (no key). Run with: MMR_LIVE_TESTS=1 pytest -m live tests/data_providers/test_live_frankfurter.py"""

import datetime as dt
import os

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.frankfurter import ECB_NOTE
from trader.data_providers.registry import ProviderRegistry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv('MMR_LIVE_TESTS') != '1', reason='live test: set MMR_LIVE_TESTS=1'),
]


def _forex():
    return ProviderRegistry.from_config({}).get(Capability.FOREX, 'frankfurter')


def test_live_eurusd_is_an_ecb_business_day_fixing():
    rate = _forex().rate('EUR', 'USD')
    as_of = dt.date.fromisoformat(rate['as_of'])
    assert 0.5 < rate['last'] < 2.0 and rate['note'] == ECB_NOTE
    assert as_of <= dt.date.today() and as_of.weekday() < 5    # a weekend date would mean blended, not ECB


def test_live_unpublished_currency_is_loud():
    with pytest.raises(ValueError, match='COP'):
        _forex().rate('USD', 'COP')


def test_live_convert_and_all_rates():
    out = _forex().convert('EUR', 'USD', 1000.0)
    assert out['converted'] == pytest.approx(1000 * out['rate'])
    frame = _forex().rates('USD', None)
    assert len(frame) >= 25 and 'USD/EUR' in set(frame['pair'])


def test_live_computed_fx_movers():
    frame = ProviderRegistry.from_config({}).get(Capability.MOVERS_FOREX).movers('forex', 'gainers')
    assert len(frame) == 10 and frame['change_pct'].notna().all()
