import argparse
import json
from unittest.mock import patch

from types import SimpleNamespace


class _StubContainer:
    def __init__(self, config):
        self._config = config

    def config(self):
        return self._config


def _download_args(source='alpaca', symbols=('AAPL',)):
    return argparse.Namespace(symbols=list(symbols), source=source, bar_size='1 day',
                              days=5, force=False)


def _patch_container(config):
    return patch('trader.container.Container.instance', return_value=_StubContainer(config))


def test_download_reports_failed_summary_when_provider_not_configured(tmp_path):
    from trader.mmr_cli import _handle_data_download
    with _patch_container({'duckdb_path': str(tmp_path / 'x.duckdb')}):
        summary = _handle_data_download(_download_args(symbols=('AAPL', 'MSFT')))
    assert {key: summary[key] for key in ('completed', 'skipped_up_to_date', 'failed', 'rows_written')} == {
        'completed': 0, 'skipped_up_to_date': 0, 'failed': 2, 'rows_written': 0}
    assert 'ALPACA_API_KEY_ID' in summary['error']


def test_download_reports_failed_summary_when_duckdb_path_missing():
    from trader.mmr_cli import _handle_data_download
    with _patch_container({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'}):
        summary = _handle_data_download(_download_args())
    assert summary['failed'] == 1
    assert summary['completed'] == 0


def test_download_json_mode_prints_failure_shape(tmp_path, capsys):
    import trader.mmr_cli as mmr_cli
    with _patch_container({'duckdb_path': str(tmp_path / 'x.duckdb')}), \
            patch.object(mmr_cli, '_json_mode', True):
        mmr_cli._handle_data_download(_download_args())
    payload = json.loads(capsys.readouterr().out)
    assert payload['success'] is False
    assert 'ALPACA_API_KEY_ID' in payload['message']
    assert payload['failed'] == 1
    assert set(payload) >= {'completed', 'skipped_up_to_date', 'failed', 'rows_written'}


def test_refresh_marks_job_failed_when_provider_not_configured(tmp_path, capsys):
    import trader.mmr_cli as mmr_cli
    config = {'duckdb_path': str(tmp_path / 'x.duckdb')}
    universe = SimpleNamespace(security_definitions=[
        SimpleNamespace(conId=265598, symbol='AAPL', exchange='SMART', primaryExchange='NASDAQ')])
    refresh_yaml = {'jobs': {'us_daily': {'universe': 'us_test', 'source': 'alpaca',
                                          'bar_size': '1 day', 'days': 5}}}
    args = argparse.Namespace(jobs=['us_daily'], refresh_all=False)
    with _patch_container(config), \
            patch.object(mmr_cli, '_load_data_refresh_yaml', return_value=refresh_yaml), \
            patch('trader.data.universe.UniverseAccessor.get', return_value=universe), \
            patch.object(mmr_cli, '_json_mode', True):
        mmr_cli._handle_data_refresh(args)
    output = capsys.readouterr().out
    payload = json.loads(output[output.rindex('\n{') + 1:])
    assert payload['success'] is False
    assert payload['failed'] == 1
    assert 'ALPACA_API_KEY_ID' in payload['results'][0]['error']
