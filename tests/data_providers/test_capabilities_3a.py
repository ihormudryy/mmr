import math

import pandas as pd
import pytest

from trader.data_providers.capabilities import (
    MOVER_COLUMNS, NEWS_FIELDS, QUOTE_FIELDS, Capability, MoversProvider, NewsProvider,
    QuoteProvider, make_news_item, make_quote,
)
from trader.data_providers.errors import CapabilityNotSupported
from trader.data_providers.registry import ProviderRegistry, ProviderSpec


def test_new_capability_values():
    assert [c.value for c in (Capability.QUOTES, Capability.MOVERS, Capability.NEWS)] == ['quotes', 'movers', 'news']


def test_make_quote_fills_every_field():
    quote = make_quote('aapl', last=1.5, feed='iex')
    assert tuple(quote) == QUOTE_FIELDS
    assert quote['symbol'] == 'AAPL'
    assert quote['last'] == 1.5
    assert math.isnan(quote['bid']) and math.isnan(quote['change_pct'])
    assert quote['error'] == '' and quote['feed'] == 'iex' and quote['name'] == ''


def test_make_quote_rejects_unknown_field():
    with pytest.raises(TypeError, match='unknown quote field'):
        make_quote('AAPL', lastt=1.0)


def test_make_news_item_defaults():
    item = make_news_item(title='t', tickers=['AAPL'])
    assert tuple(item) == NEWS_FIELDS
    assert item['sentiment'] == '' and item['insights'] == [] and item['tickers'] == ['AAPL']


def test_mover_columns():
    assert MOVER_COLUMNS == ('ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note')


def test_protocols_are_structural():
    class Q:
        def quotes(self, symbols):
            return []

    class M:
        markets = frozenset({'stocks'})

        def movers(self, market, direction):
            return pd.DataFrame()

    class N:
        def news(self, ticker, limit):
            return []

    assert isinstance(Q(), QuoteProvider) and isinstance(M(), MoversProvider) and isinstance(N(), NewsProvider)


def _registry(inherits, **config):
    specs = [ProviderSpec('a', (), {Capability.HISTORY: lambda c: 'a', Capability.MOVERS: lambda c: 'a'}),
             ProviderSpec('b', (), {Capability.HISTORY: lambda c: 'b', Capability.MOVERS: lambda c: 'b'})]
    return ProviderRegistry(config, specs, {Capability.HISTORY: 'a', Capability.MOVERS: 'a'},
                            inherits_global_default=inherits)


def test_only_listed_capabilities_inherit_default_data_source():
    registry = _registry({Capability.HISTORY}, default_data_source='b')
    assert registry.default_source(Capability.HISTORY) == 'b'
    assert registry.default_source(Capability.MOVERS) == 'a'


def test_data_providers_override_applies_even_without_inheritance():
    registry = _registry({Capability.HISTORY}, data_providers={'movers': 'b'})
    assert registry.default_source(Capability.MOVERS) == 'b'


def test_missing_builtin_default_raises_provider_error():
    registry = ProviderRegistry({}, [ProviderSpec('a', (), {Capability.NEWS: lambda c: 'a'})], {})
    with pytest.raises(CapabilityNotSupported, match='news'):
        registry.default_source(Capability.NEWS)


def test_builtin_inheritance_set_and_choices():
    from trader.data_providers.builtin import INHERITS_DEFAULT_DATA_SOURCE, source_choices
    assert INHERITS_DEFAULT_DATA_SOURCE == frozenset({Capability.HISTORY})
    assert source_choices(Capability.HISTORY) == ['alpaca', 'massive', 'twelvedata']


def test_sdk_provider_helper_uses_container_config():
    from unittest.mock import MagicMock
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._container = MagicMock()
    mmr._container.config.return_value = {'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'}
    assert mmr._provider_default(Capability.HISTORY) == 'alpaca'
    assert type(mmr._provider(Capability.HISTORY)).__name__ == 'AlpacaHistoryProvider'
