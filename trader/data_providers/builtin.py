"""The providers MMR ships with, and which one each capability uses by default."""

from typing import Any, Mapping

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderSpec


def _massive_history(config: Mapping[str, Any]):
    from trader.listeners.massive_history import MassiveHistoryWorker
    return MassiveHistoryWorker(massive_api_key=config['massive_api_key'])


def _twelvedata_history(config: Mapping[str, Any]):
    from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
    return TwelveDataHistoryWorker(twelvedata_api_key=config['twelvedata_api_key'])


def builtin_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec('massive', (('massive_api_key', 'MASSIVE_API_KEY'),),
                     {Capability.HISTORY: _massive_history}),
        ProviderSpec('twelvedata', (('twelvedata_api_key', 'TWELVEDATA_API_KEY'),),
                     {Capability.HISTORY: _twelvedata_history}),
    ]


BUILTIN_DEFAULTS: dict[Capability, str] = {
    Capability.HISTORY: 'twelvedata',
}

# IB history is contract-based and async, so it keeps its own code path and is
# offered only as a CLI source name, not as a registry provider.
IB_HISTORY_SOURCE = 'ib'


def history_source_choices() -> list[str]:
    rest_sources = sorted(spec.name for spec in builtin_specs() if Capability.HISTORY in spec.builders)
    return rest_sources + [IB_HISTORY_SOURCE]
