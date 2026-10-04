import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from trader.data_service import DataService


def _security(symbol='AAPL'):
    # Only the attributes pull_history touches; SecurityDefinition has ~25 required fields.
    return SimpleNamespace(symbol=symbol, exchange='SMART', conId=265598,
                           primaryExchange='NASDAQ', timeZoneId='US/Eastern')


class RecordingProvider:
    calls = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        RecordingProvider.calls.append(ticker)
        return pd.DataFrame()


def test_pull_history_reports_missing_key_without_downloading(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    result = asyncio.run(service.pull_history('massive', symbols=['AAPL']))
    assert result['enqueued'] == 0
    assert 'MASSIVE_API_KEY' in result['errors'][0]


def test_pull_history_reports_unknown_source(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    result = asyncio.run(service.pull_history('nope', symbols=['AAPL']))
    assert result['enqueued'] == 0
    assert "does not support history" in result['errors'][0]


def test_pull_history_routes_to_registry_provider(tmp_duckdb_path):
    RecordingProvider.calls = []
    service = DataService(massive_api_key='k', duckdb_path=tmp_duckdb_path)
    with patch.object(service, '_resolve_symbols', return_value=[_security()]), \
         patch('trader.data_providers.builtin._massive_history', return_value=RecordingProvider()):
        result = asyncio.run(service.pull_history('massive', symbols=['AAPL'], prev_days=3))
    assert RecordingProvider.calls and set(RecordingProvider.calls) == {'AAPL'}
    assert result['failed'] == 0
    assert result['enqueued'] >= 1
    assert result['completed'] >= 1


class ExplodingProvider:
    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        raise RuntimeError('boom')


def test_pull_history_turns_provider_failure_into_per_symbol_error(tmp_duckdb_path):
    service = DataService(massive_api_key='k', duckdb_path=tmp_duckdb_path)
    with patch.object(service, '_resolve_symbols', return_value=[_security()]), \
         patch('trader.data_providers.builtin._massive_history', return_value=ExplodingProvider()):
        result = asyncio.run(service.pull_history('massive', symbols=['AAPL'], prev_days=3))
    assert result['failed'] == 1
    assert 'boom' in result['errors'][0]


def test_legacy_aliases_delegate_to_pull_history(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    with patch.object(service, 'pull_history', return_value={'ok': 1}) as pull:
        asyncio.run(service.pull_massive(symbols=['A']))
        asyncio.run(service.pull_twelvedata(symbols=['B']))
    assert [c.args[0] for c in pull.call_args_list] == ['massive', 'twelvedata']
