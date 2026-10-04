"""The providers MMR ships with, and which one each capability uses by default."""

from typing import Any, Mapping

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderSpec


def _alpaca_client(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.client import AlpacaClient
    return AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'])


def _alpaca_history(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.history import AlpacaHistoryProvider
    return AlpacaHistoryProvider(_alpaca_client(config))


def _alpaca_quotes(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.quotes import AlpacaQuotes
    return AlpacaQuotes(_alpaca_client(config))


def _alpaca_news(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.news import AlpacaNews
    return AlpacaNews(_alpaca_client(config))


def _massive_history(config: Mapping[str, Any]):
    from trader.listeners.massive_history import MassiveHistoryWorker
    return MassiveHistoryWorker(massive_api_key=config['massive_api_key'])


def _twelvedata_history(config: Mapping[str, Any]):
    from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
    return TwelveDataHistoryWorker(twelvedata_api_key=config['twelvedata_api_key'])


def _twelvedata_quotes(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.quotes import TwelveDataQuotes
    return TwelveDataQuotes(TDClient(apikey=config['twelvedata_api_key']))


def _massive_rest_client(config: Mapping[str, Any]):
    from massive import RESTClient
    return RESTClient(api_key=config['massive_api_key'])


def _massive_movers(config: Mapping[str, Any]):
    from trader.data_providers.massive.movers import MassiveMovers
    return MassiveMovers(_massive_rest_client(config))


def _polygon_news(config: Mapping[str, Any]):
    from trader.data_providers.massive.news import MassiveTickerNews
    return MassiveTickerNews(_massive_rest_client(config))


def _benzinga_news(config: Mapping[str, Any]):
    from trader.data_providers.massive.news import MassiveBenzingaNews
    return MassiveBenzingaNews(_massive_rest_client(config))


def _twelvedata_movers(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.movers import TwelveDataMovers
    return TwelveDataMovers(TDClient(apikey=config['twelvedata_api_key']))


def builtin_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec('alpaca', (('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')),
                     {Capability.HISTORY: _alpaca_history,
                      Capability.QUOTES: _alpaca_quotes,
                      Capability.NEWS: _alpaca_news}),
        ProviderSpec('massive', (('massive_api_key', 'MASSIVE_API_KEY'),),
                     {Capability.HISTORY: _massive_history,
                      Capability.MOVERS: _massive_movers}),
        ProviderSpec('twelvedata', (('twelvedata_api_key', 'TWELVEDATA_API_KEY'),),
                     {Capability.HISTORY: _twelvedata_history,
                      Capability.QUOTES: _twelvedata_quotes,
                      Capability.MOVERS: _twelvedata_movers}),
        ProviderSpec('polygon', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _polygon_news}),
        ProviderSpec('benzinga', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _benzinga_news}),
    ]


BUILTIN_DEFAULTS: dict[Capability, str] = {
    Capability.HISTORY: 'alpaca',
    Capability.QUOTES: 'alpaca',
    Capability.MOVERS: 'massive',
    Capability.NEWS: 'polygon',
}

# IB history is contract-based and async, so it keeps its own code path and is
# offered only as a CLI source name, not as a registry provider.
IB_HISTORY_SOURCE = 'ib'


INHERITS_DEFAULT_DATA_SOURCE = frozenset({Capability.HISTORY, Capability.QUOTES})


def source_choices(capability: Capability) -> list[str]:
    return sorted(spec.name for spec in builtin_specs() if capability in spec.builders)


def history_source_choices() -> list[str]:
    return source_choices(Capability.HISTORY) + [IB_HISTORY_SOURCE]
