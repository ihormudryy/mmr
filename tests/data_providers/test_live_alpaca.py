"""Real Alpaca calls. Run with: MMR_LIVE_TESTS=1 ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... pytest -m live"""

import datetime as dt
import os

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry
from trader.objects import BarSize

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv('MMR_LIVE_TESTS') != '1'
        or not os.getenv('ALPACA_API_KEY_ID') or not os.getenv('ALPACA_API_SECRET_KEY'),
        reason='live test: set MMR_LIVE_TESTS=1 and Alpaca keys',
    ),
]


def _provider():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }).get(Capability.HISTORY, 'alpaca')


def test_one_day_of_sip_minute_bars_with_extended_hours():
    df = _provider().get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert len(df) > 700                      # 04:00–20:00 ET session, ~960 bars
    assert df.index.min().hour == 4 and df.index.max().hour == 19


def test_nvda_split_is_adjusted_with_no_jump():
    # NVDA 10-for-1 split effective 2024-06-10. Split-adjusted closes stay ~$120.
    df = _provider().get_history('NVDA', BarSize.Days1, dt.datetime(2024, 6, 3), dt.datetime(2024, 6, 14))
    closes = df['close']
    assert closes.max() / closes.min() < 1.2
    assert closes.max() < 200
