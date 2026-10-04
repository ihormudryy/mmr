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


def builtin_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec('alpaca', (('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')),
                     {Capability.HISTORY: _alpaca_history,
                      Capability.QUOTES: _alpaca_quotes}),
        ProviderSpec('massive', (('massive_api_key', 'MASSIVE_API_KEY'),),
                     {Capability.HISTORY: _massive_history}),
        ProviderSpec('twelvedata', (('twelvedata_api_key', 'TWELVEDATA_API_KEY'),),
                     {Capability.HISTORY: _twelvedata_history,
                      Capability.QUOTES: _twelvedata_quotes}),
    ]


BUILTIN_DEFAULTS: dict[Capability, str] = {
    Capability.HISTORY: 'alpaca',
    Capability.QUOTES: 'alpaca',
}

# IB history is contract-based and async, so it keeps its own code path and is
# offered only as a CLI source name, not as a registry provider.
IB_HISTORY_SOURCE = 'ib'


INHERITS_DEFAULT_DATA_SOURCE = frozenset({Capability.HISTORY, Capability.QUOTES})


def source_choices(capability: Capability) -> list[str]:
    return sorted(spec.name for spec in builtin_specs() if capability in spec.builders)


def history_source_choices() -> list[str]:
    return source_choices(Capability.HISTORY) + [IB_HISTORY_SOURCE]
