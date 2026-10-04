import datetime as dt

import pandas as pd

from trader.data_providers.capabilities import Capability, HISTORY_COLUMNS, HistoryProvider
from trader.data_providers.errors import (
    CapabilityNotSupported,
    ProviderEntitlementError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
)


def test_not_configured_names_every_key_and_env_var():
    err = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                           ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')])
    message = str(err)
    assert 'alpaca is not configured' in message
    for name in ('alpaca_api_key_id', 'ALPACA_API_KEY_ID', 'alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY'):
        assert name in message
    assert err.provider == 'alpaca'


def test_capability_not_supported_lists_alternatives():
    err = CapabilityNotSupported('history', 'finnhub', ['alpaca', 'massive'])
    assert "'finnhub' does not support history" in str(err)
    assert 'alpaca, massive' in str(err)


def test_all_errors_share_base_class():
    for cls in (ProviderNotConfigured, CapabilityNotSupported, ProviderEntitlementError, ProviderRateLimited):
        assert issubclass(cls, ProviderError)


def test_capability_values_are_stable_strings():
    assert Capability.HISTORY.value == 'history'
    assert Capability('history') is Capability.HISTORY


def test_history_columns_match_existing_contract():
    assert HISTORY_COLUMNS == ('open', 'high', 'low', 'close', 'volume',
                               'average', 'bar_count', 'bar_size', 'what_to_show')


def test_history_provider_protocol_is_structural():
    class Fake:
        def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
            return pd.DataFrame()

    assert isinstance(Fake(), HistoryProvider)
    assert not isinstance(object(), HistoryProvider)
