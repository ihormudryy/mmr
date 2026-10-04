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


def _alpaca_movers(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.movers import AlpacaMovers
    return AlpacaMovers(_alpaca_client(config))


def _alpaca_trading_client(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL
    from trader.data_providers.alpaca.client import AlpacaClient
    return AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'],
                        base_url=ALPACA_PAPER_TRADING_URL)


def _alpaca_options(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.options import AlpacaOptions
    return AlpacaOptions(_alpaca_client(config), _alpaca_trading_client(config))


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


def _massive_forex(config: Mapping[str, Any]):
    from trader.data_providers.massive.forex import MassiveForex
    return MassiveForex(_massive_rest_client(config))


def _twelvedata_forex(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.forex import TwelveDataForex
    return TwelveDataForex(TDClient(apikey=config['twelvedata_api_key']))


def _frankfurter_forex(config: Mapping[str, Any]):
    from trader.data_providers.frankfurter import FrankfurterClient, FrankfurterForex
    return FrankfurterForex(FrankfurterClient())


_ALPACA_KEYS = (('alpaca_api_key_id', 'ALPACA_API_KEY_ID'), ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY'))


def _etf_proxy_movers(config: Mapping[str, Any]):
    from trader.data_providers.computed_movers import EtfProxyMovers
    return EtfProxyMovers(_alpaca_quotes(config))


def _computed_fx_movers(config: Mapping[str, Any]):
    from trader.data_providers.computed_movers import ComputedFxMovers
    return ComputedFxMovers(_frankfurter_forex(config))


def _massive_options(config: Mapping[str, Any]):
    from trader.data_providers.massive.options import MassiveOptions
    return MassiveOptions(_massive_rest_client(config))


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


def _massive_ideas(config: Mapping[str, Any]):
    from trader.data_providers.massive.scan import MassiveScanSource
    return MassiveScanSource(_massive_rest_client(config))


def _twelvedata_ideas(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    return TwelveDataScanSource(TDClient(apikey=config['twelvedata_api_key']))


def _alpaca_ideas(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.scan import AlpacaScanSource
    return AlpacaScanSource(
        _alpaca_client(config),
        assets=alpaca_asset_directory(config),
        history=_alpaca_history(config),
        news=_alpaca_news(config),
    )


def alpaca_asset_directory(config: Mapping[str, Any]):
    """The cached Alpaca asset list, or None when Alpaca keys are not configured."""
    if not all(str(config.get(key) or '').strip() for key in ('alpaca_api_key_id', 'alpaca_api_secret_key')):
        return None
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL, AlpacaAssetDirectory
    from trader.data_providers.alpaca.client import AlpacaClient
    client = AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'],
                          base_url=ALPACA_PAPER_TRADING_URL)
    return AlpacaAssetDirectory(client)


def builtin_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec('alpaca', _ALPACA_KEYS,
                     {Capability.HISTORY: _alpaca_history,
                      Capability.QUOTES: _alpaca_quotes,
                      Capability.MOVERS: _alpaca_movers,
                      Capability.NEWS: _alpaca_news,
                      Capability.IDEAS: _alpaca_ideas,
                      Capability.OPTIONS: _alpaca_options}),
        ProviderSpec('massive', (('massive_api_key', 'MASSIVE_API_KEY'),),
                     {Capability.HISTORY: _massive_history,
                      Capability.MOVERS: _massive_movers,
                      Capability.MOVERS_FOREX: _massive_movers,
                      Capability.MOVERS_INDICES: _massive_movers,
                      Capability.FOREX: _massive_forex,
                      Capability.IDEAS: _massive_ideas,
                      Capability.OPTIONS: _massive_options}),
        ProviderSpec('twelvedata', (('twelvedata_api_key', 'TWELVEDATA_API_KEY'),),
                     {Capability.HISTORY: _twelvedata_history,
                      Capability.QUOTES: _twelvedata_quotes,
                      Capability.MOVERS: _twelvedata_movers,
                      Capability.FOREX: _twelvedata_forex,
                      Capability.IDEAS: _twelvedata_ideas}),
        ProviderSpec('frankfurter', (), {Capability.FOREX: _frankfurter_forex}),
        ProviderSpec('etf_proxy', _ALPACA_KEYS, {Capability.MOVERS_INDICES: _etf_proxy_movers}),
        ProviderSpec('computed_fx', (), {Capability.MOVERS_FOREX: _computed_fx_movers}),
        ProviderSpec('polygon', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _polygon_news}),
        ProviderSpec('benzinga', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _benzinga_news}),
    ]


BUILTIN_DEFAULTS: dict[Capability, str] = {
    Capability.HISTORY: 'alpaca',
    Capability.QUOTES: 'alpaca',
    Capability.MOVERS: 'alpaca',
    Capability.NEWS: 'alpaca',
    Capability.IDEAS: 'alpaca',
    Capability.FOREX: 'frankfurter',
    Capability.MOVERS_FOREX: 'computed_fx',
    Capability.MOVERS_INDICES: 'etf_proxy',
    Capability.OPTIONS: 'alpaca',
}

# `data_providers.movers: massive` keeps Massive for index and forex movers too; a movers
# source that does not serve them (alpaca) leaves the builtin default.
FALLBACK_CAPABILITIES: dict[Capability, Capability] = {
    Capability.MOVERS_FOREX: Capability.MOVERS,
    Capability.MOVERS_INDICES: Capability.MOVERS,
}

# IB history is contract-based and async, so it keeps its own code path and is
# offered only as a CLI source name, not as a registry provider.
IB_HISTORY_SOURCE = 'ib'

# IB forex is contract-based (IDEALPRO CASH via trader_service), so like IB history it is a
# CLI source name for `forex snapshot` / `forex quote`, not a registry provider.
IB_FOREX_SOURCE = 'ib'


INHERITS_DEFAULT_DATA_SOURCE = frozenset({Capability.HISTORY})


def source_choices(capability: Capability) -> list[str]:
    return sorted(spec.name for spec in builtin_specs() if capability in spec.builders)


def history_source_choices() -> list[str]:
    return source_choices(Capability.HISTORY) + [IB_HISTORY_SOURCE]


def forex_quote_source_choices() -> list[str]:
    return [IB_FOREX_SOURCE] + source_choices(Capability.FOREX)
