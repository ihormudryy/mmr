import pytest

from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured
from trader.listeners.massive_history import MassiveHistoryWorker


def test_rest_history_worker_builds_from_config():
    from trader.mmr_cli import _rest_history_worker
    assert isinstance(_rest_history_worker('massive', {'massive_api_key': 'k'}), MassiveHistoryWorker)


def test_rest_history_worker_missing_key_names_env_var():
    from trader.mmr_cli import _rest_history_worker
    with pytest.raises(ProviderNotConfigured, match='TWELVEDATA_API_KEY'):
        _rest_history_worker('twelvedata', {'twelvedata_api_key': ''})


def test_rest_history_worker_rejects_unknown_source():
    from trader.mmr_cli import _rest_history_worker
    with pytest.raises(CapabilityNotSupported):
        _rest_history_worker('yahoo', {})


def test_download_parser_offers_registry_sources():
    from trader.data_providers.builtin import history_source_choices
    from trader.mmr_cli import build_parser
    parser = build_parser()
    args = parser.parse_args(['data', 'download', 'AAPL', '--source', 'ib'])
    assert args.source == 'ib'
    for source in history_source_choices():
        assert parser.parse_args(['data', 'download', 'AAPL', '--source', source]).source == source


def test_dead_polygon_modules_are_gone():
    import importlib.util
    for name in ('trader.listeners.polygon_listener', 'trader.listeners.polygon_reactive',
                 'trader.batch.polygon_batch', 'trader.batch.polygon_queuer'):
        assert importlib.util.find_spec(name) is None, name
