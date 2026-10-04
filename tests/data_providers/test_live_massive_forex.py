"""Real Massive calls. Run with: MMR_LIVE_TESTS=1 MASSIVE_API_KEY=... pytest -m live

Needs a Massive plan with forex snapshots; a plan without it fails with ProviderEntitlementError.
"""

import math
import os

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv('MMR_LIVE_TESTS') != '1' or not os.getenv('MASSIVE_API_KEY'),
        reason='live test: set MMR_LIVE_TESTS=1 and MASSIVE_API_KEY',
    ),
]


def test_live_eurusd_rate_has_finite_bid_and_ask():
    registry = ProviderRegistry.from_config({'massive_api_key': os.environ['MASSIVE_API_KEY']})
    rate = registry.get(Capability.FOREX, 'massive').rate('EUR', 'USD')
    assert math.isfinite(rate['bid']) and math.isfinite(rate['ask'])
    assert 0 < rate['bid'] <= rate['ask']
