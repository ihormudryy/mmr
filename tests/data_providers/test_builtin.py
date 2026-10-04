from trader.data_providers.builtin import BUILTIN_DEFAULTS, builtin_specs, history_source_choices
from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry
from trader.listeners.massive_history import MassiveHistoryWorker
from trader.listeners.twelvedata_history import TwelveDataHistoryWorker


def test_history_sources_include_rest_providers_and_ib():
    choices = history_source_choices()
    assert {'massive', 'twelvedata', 'ib'} <= set(choices)
    assert choices[-1] == 'ib'


def test_every_builtin_default_points_at_a_registered_source():
    names = {spec.name for spec in builtin_specs()}
    assert set(BUILTIN_DEFAULTS.values()) <= names


def test_from_config_builds_existing_workers():
    registry = ProviderRegistry.from_config({'massive_api_key': 'm', 'twelvedata_api_key': 't'})
    assert isinstance(registry.get(Capability.HISTORY, 'massive'), MassiveHistoryWorker)
    assert isinstance(registry.get(Capability.HISTORY, 'twelvedata'), TwelveDataHistoryWorker)
