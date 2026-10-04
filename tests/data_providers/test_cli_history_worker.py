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
                 'trader.batch'):
        assert importlib.util.find_spec(name) is None, name


def test_us_universe_auto_source_is_alpaca():
    from types import SimpleNamespace
    from trader.mmr_cli import _auto_source_for_universe
    us = [SimpleNamespace(exchange='SMART', primaryExchange='NASDAQ')] * 3
    asx = [SimpleNamespace(exchange='ASX', primaryExchange='ASX')] * 3
    assert _auto_source_for_universe(us) == 'alpaca'
    assert _auto_source_for_universe(asx) == 'ib'
    assert _auto_source_for_universe([]) == 'ib'


def test_history_alpaca_subcommand_parses():
    from trader.mmr_cli import build_parser
    args = build_parser().parse_args(['history', 'alpaca', '--symbol', 'AAPL'])
    assert args.symbol == 'AAPL'


def test_data_refresh_template_uses_alpaca_for_us_jobs():
    from pathlib import Path
    import yaml
    jobs = yaml.safe_load(Path('config_defaults/data_refresh.yaml').read_text())['jobs']
    assert jobs['us_top20_daily']['source'] == 'alpaca'
    assert jobs['us_top20_1min']['source'] == 'alpaca'


@pytest.mark.parametrize('config, expected', [
    ({'default_data_source': 'ib'}, 'ib'),
    ({'data_providers': {'history': 'ib'}}, 'ib'),
    ({'data_providers': {'history': 'ib'}, 'default_data_source': 'massive'}, 'ib'),
    ({'data_providers': {'history': 'massive'}, 'default_data_source': 'ib'}, 'massive'),
    ({'default_data_source': 'massive'}, 'massive'),
    ({}, 'alpaca'),
])
def test_default_history_source_keeps_ib_and_honours_overrides(monkeypatch, config, expected):
    from trader.mmr_cli import _default_history_source
    monkeypatch.delenv('MMR_DEFAULT_DATA_SOURCE', raising=False)
    assert _default_history_source(config) == expected


@pytest.mark.parametrize('env_value, config, expected', [
    ('ib', {}, 'ib'),
    ('massive', {'default_data_source': 'twelvedata'}, 'massive'),
    ('ib', {'data_providers': {'history': 'massive'}}, 'massive'),
])
def test_default_history_source_honours_mmr_default_data_source_env(monkeypatch, env_value, config, expected):
    from trader.mmr_cli import _default_history_source
    monkeypatch.setenv('MMR_DEFAULT_DATA_SOURCE', env_value)
    assert _default_history_source(config) == expected
