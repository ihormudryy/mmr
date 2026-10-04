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


def test_alpaca_is_the_default_history_source():
    from trader.data_providers.alpaca.history import AlpacaHistoryProvider
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    assert registry.default_source(Capability.HISTORY) == 'alpaca'
    assert isinstance(registry.get(Capability.HISTORY), AlpacaHistoryProvider)


def test_data_providers_override_beats_default_data_source():
    registry = ProviderRegistry.from_config({'data_providers': {'history': 'massive'},
                                             'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.HISTORY) == 'massive'


def test_download_parser_default_is_none_so_registry_decides():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['data', 'download', 'AAPL']).source is None


def test_alpaca_missing_secret_names_env_var():
    import pytest
    from trader.data_providers.errors import ProviderNotConfigured
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k'})
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_SECRET_KEY'):
        registry.get(Capability.HISTORY, 'alpaca')


def test_alpaca_keys_load_from_env(monkeypatch, tmp_path):
    from trader.config import MMRConfig
    cfg = tmp_path / 'trader.yaml'
    cfg.write_text('duckdb_path: data/mmr.duckdb\n')
    monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-id')
    monkeypatch.setenv('ALPACA_API_SECRET_KEY', 'env-secret')
    config = MMRConfig.from_yaml(str(cfg))
    assert config.alpaca.api_key_id == 'env-id'
    assert config.alpaca.secret_key == 'env-secret'


def test_alpaca_env_overrides_yaml_like_massive(monkeypatch, tmp_path):
    from trader.config import MMRConfig
    cfg = tmp_path / 'trader.yaml'
    cfg.write_text("alpaca_api_key_id: yaml-id\nalpaca_api_secret_key: yaml-secret\n")
    monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-id')
    monkeypatch.delenv('ALPACA_API_SECRET_KEY', raising=False)
    config = MMRConfig.from_yaml(str(cfg))
    assert config.alpaca.api_key_id == 'env-id'
    assert config.alpaca.secret_key == 'yaml-secret'


def test_alpaca_asset_directory_needs_both_keys():
    from trader.data_providers.builtin import alpaca_asset_directory
    assert alpaca_asset_directory({}) is None
    assert alpaca_asset_directory({'alpaca_api_key_id': 'id', 'alpaca_api_secret_key': ' '}) is None
    assert alpaca_asset_directory({'alpaca_api_key_id': 'id', 'alpaca_api_secret_key': 'secret'}) is not None
