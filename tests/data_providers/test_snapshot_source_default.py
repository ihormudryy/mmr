from argparse import Namespace
from unittest.mock import MagicMock

import pytest

from trader.sdk import MMR

QUOTE_CHOICES = ['ib', 'alpaca', 'twelvedata']


@pytest.fixture
def trader_config(tmp_path, monkeypatch):
    monkeypatch.delenv('MMR_DEFAULT_DATA_SOURCE', raising=False)

    def write(yaml_text: str):
        path = tmp_path / 'trader.yaml'
        path.write_text(yaml_text)
        monkeypatch.setenv('TRADER_CONFIG', str(path))

    return write


@pytest.mark.parametrize('yaml_text, env_value, expected', [
    ('', None, 'ib'),
    ('default_data_source: twelvedata\n', None, 'twelvedata'),
    ('default_data_source: massive\n', None, 'ib'),
    ('data_providers: {quotes: alpaca}\ndefault_data_source: twelvedata\n', None, 'alpaca'),
    ('data_providers: {quotes: massive}\ndefault_data_source: twelvedata\n', None, 'twelvedata'),
    ('default_data_source: twelvedata\n', 'ib', 'ib'),
    ('', 'alpaca', 'alpaca'),
])
def test_snapshot_source_default_rule(trader_config, monkeypatch, yaml_text, env_value, expected):
    from trader.mmr_cli import _snapshot_source_default
    trader_config(yaml_text)
    if env_value:
        monkeypatch.setenv('MMR_DEFAULT_DATA_SOURCE', env_value)
    assert _snapshot_source_default(QUOTE_CHOICES) == expected


def test_snapshot_source_default_is_ib_without_config_file(tmp_path, monkeypatch):
    from trader.mmr_cli import _snapshot_source_default
    monkeypatch.delenv('MMR_DEFAULT_DATA_SOURCE', raising=False)
    monkeypatch.setenv('TRADER_CONFIG', str(tmp_path / 'missing.yaml'))
    assert _snapshot_source_default(QUOTE_CHOICES) == 'ib'


def test_parsers_use_snapshot_source_default(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('data_providers: {quotes: alpaca}\n')
    parser = build_parser()
    assert parser.parse_args(['snapshot', 'AAPL']).source == 'alpaca'
    assert parser.parse_args(['snapshot-batch', 'AAPL']).source == 'alpaca'


def test_parsers_default_to_ib_with_empty_config(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('')
    parser = build_parser()
    assert parser.parse_args(['snapshot', 'BHP', '--exchange', 'ASX']).source == 'ib'


@pytest.mark.parametrize('hints', [{'exchange': 'ASX'}, {'currency': 'AUD'}])
def test_snapshot_rejects_exchange_hints_for_rest_sources(hints):
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock()
    with pytest.raises(ValueError, match='need --source ib; alpaca covers US listings only'):
        mmr.snapshot('BHP', source='alpaca', **hints)
    mmr._provider.assert_not_called()


@pytest.mark.parametrize('hints', [{'exchange': 'ASX'}, {'currency': 'AUD'}])
def test_snapshot_batch_rejects_exchange_hints_for_rest_sources(hints):
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock()
    with pytest.raises(ValueError, match='need --source ib; twelvedata covers US listings only'):
        mmr.snapshot_batch(['BHP'], source='twelvedata', **hints)
    mmr._provider.assert_not_called()


@pytest.mark.parametrize('command, method', [('snapshot', 'snapshot'), ('snapshot-batch', 'snapshot_batch')])
def test_cli_snapshot_prints_hint_error(capsys, command, method):
    from trader.mmr_cli import _handle_snapshot
    mmr = MagicMock()
    getattr(mmr, method).side_effect = ValueError('--exchange/--currency need --source ib; alpaca covers US listings only')
    _handle_snapshot(mmr, Namespace(symbol='BHP', symbols=['BHP'], delayed=False, exchange='ASX',
                                    currency='', source='alpaca'), command)
    assert 'need --source ib' in capsys.readouterr().out
