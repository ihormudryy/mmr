"""A REST history provider looks bars up by ticker, so a non-US instrument must never be fetched through it
(issues #127 alpaca, #137 massive and twelvedata)."""
import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.data_providers.test_cli_refresh_conid import _FakeHistory, _no_trader, _run, _StubContainer, _stored_rows
from tests.test_execution_costs import _definition
from trader.data.universe import UniverseAccessor
from trader.data_providers.us_listing import NonUsInstrumentError, require_us_listing
from trader.data_service import DataService

US_CONID, ASX_CONID, UNKNOWN_CONID = 3001, 3002, 3003
REST_SOURCES = ['alpaca', 'massive', 'twelvedata']
HISTORY_BUILDERS = {'alpaca': '_alpaca_history', 'massive': '_massive_history', 'twelvedata': '_twelvedata_history'}
DATA_SERVICE_KEYS = {'alpaca': {'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'},
                     'massive': {'massive_api_key': 'k'},
                     'twelvedata': {'twelvedata_api_key': 'k'}}


def _refusal_code(source):
    return f'{source.upper()}_NON_US_INSTRUMENT'


@pytest.fixture
def mixed_universe(tmp_path):
    db = str(tmp_path / 'mmr.duckdb')
    universes = UniverseAccessor(db, 'Universes')
    universes.insert('mixed', _definition(US_CONID, 'SAME', 'NASDAQ'))
    universes.insert('mixed', _definition(ASX_CONID, 'SAME', 'ASX'))
    universes.insert('mixed', _definition(UNKNOWN_CONID, 'NOEX', ''))
    return db


def _refresh_payload(db, history, capsys, source):
    jobs = {'jobs': {'mixed': {'universe': 'mixed', 'source': source, 'bar_size': '15 mins', 'days': 5}}}

    def refresh(mmr_cli):
        with patch.object(mmr_cli, '_load_data_refresh_yaml', return_value=jobs):
            mmr_cli._handle_data_refresh(argparse.Namespace(jobs=['mixed'], refresh_all=False))
    _run(db, history, refresh)
    output = capsys.readouterr().out
    return json.loads(output[output.rindex('\n{') + 1:])


@pytest.mark.parametrize('source', REST_SOURCES)
def test_a_refresh_refuses_non_us_and_unknown_entries_but_still_refreshes_the_us_one(mixed_universe, capsys, source):
    history = _FakeHistory()
    payload = _refresh_payload(mixed_universe, history, capsys, source)
    job = payload['results'][0]
    assert payload['success'] is False and job['success'] is False
    assert job['failed_symbols'] == 2
    assert _refusal_code(source) in job['error']
    assert f'conId {ASX_CONID}' in job['error'] and 'ASX' in job['error']
    assert f'conId {UNKNOWN_CONID}' in job['error'] and 'unknown' in job['error']
    assert history.tickers == ['SAME']
    assert _stored_rows(mixed_universe, US_CONID) > 0
    assert _stored_rows(mixed_universe, ASX_CONID) == 0
    assert _stored_rows(mixed_universe, UNKNOWN_CONID) == 0


@pytest.mark.parametrize('source', REST_SOURCES)
def test_a_download_of_a_non_us_ticker_through_a_rest_source_is_refused(tmp_path, source):
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    UniverseAccessor(db, 'Universes').insert('asx', _definition(ASX_CONID, 'BHP', 'ASX'))
    args = argparse.Namespace(symbols=['BHP'], source=source, bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert _refusal_code(source) in summary['refused_targets'][0]
    assert history.tickers == [] and _stored_rows(db, ASX_CONID) == 0


@pytest.mark.parametrize('source', REST_SOURCES)
@pytest.mark.parametrize('primary_exchange', ['NASDAQ', 'nyse', 'ARCA', 'AMEX', 'BATS', 'ISLAND'])
def test_a_us_listing_is_accepted(primary_exchange, source):
    require_us_listing(source, _definition(1, 'AAA', primary_exchange))


@pytest.mark.parametrize('source', REST_SOURCES)
@pytest.mark.parametrize('primary_exchange', ['ASX', 'TSE', 'SEHK', 'SMART', '', '  '])
def test_anything_else_is_refused_and_smart_is_a_route_not_a_listing(primary_exchange, source):
    with pytest.raises(NonUsInstrumentError, match=_refusal_code(source)):
        require_us_listing(source, _definition(1, 'AAA', primary_exchange))


def test_a_source_nobody_vetted_is_refused_too():
    with pytest.raises(NonUsInstrumentError, match='NEWPROVIDER_NON_US_INSTRUMENT'):
        require_us_listing('newprovider', _definition(1, 'AAA', 'ASX'))


class _RecordingProvider:
    def __init__(self):
        self.tickers = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        self.tickers.append(ticker)
        raise AssertionError('the provider must not be called')


@pytest.mark.parametrize('source', REST_SOURCES)
def test_the_data_service_does_not_fetch_a_non_us_security_from_a_rest_source(tmp_duckdb_path, source):
    provider = _RecordingProvider()
    service = DataService(**DATA_SERVICE_KEYS[source], duckdb_path=tmp_duckdb_path)
    asx = SimpleNamespace(symbol='BHP', exchange='SMART', conId=ASX_CONID, primaryExchange='ASX',
                          timeZoneId='Australia/Sydney')
    with patch.object(service, '_resolve_symbols', return_value=[asx]), \
            patch(f'trader.data_providers.builtin.{HISTORY_BUILDERS[source]}', return_value=provider):
        result = asyncio.run(service.pull_history(source, symbols=['BHP'], prev_days=3))
    assert provider.tickers == []
    assert (result['enqueued'], result['completed'], result['failed']) == (0, 0, 1)
    assert _refusal_code(source) in result['errors'][0]


@pytest.mark.parametrize('source', REST_SOURCES)
def test_a_non_us_security_already_covered_is_still_refused(tmp_duckdb_path, source):
    """The refusal comes before the coverage check, so a covered range never reads as success (mmr-openai)."""
    provider = _RecordingProvider()
    service = DataService(**DATA_SERVICE_KEYS[source], duckdb_path=tmp_duckdb_path)
    asx = SimpleNamespace(symbol='BHP', exchange='SMART', conId=ASX_CONID, primaryExchange='ASX',
                          timeZoneId='Australia/Sydney')
    with patch.object(service, '_resolve_symbols', return_value=[asx]), \
            patch('trader.data_service._try_get_exchange_calendar', return_value=object()), \
            patch('trader.data.data_access.TickData.missing', return_value=[]), \
            patch(f'trader.data_providers.builtin.{HISTORY_BUILDERS[source]}', return_value=provider):
        result = asyncio.run(service.pull_history(source, symbols=['BHP'], prev_days=3))
    assert provider.tickers == []
    assert (result['completed'], result['failed']) == (0, 1)
    assert _refusal_code(source) in result['errors'][0]


@pytest.mark.parametrize('source', REST_SOURCES)
def test_a_download_named_by_conid_fetches_the_resolved_ticker(tmp_path, source):
    """``data download 4391`` resolves the conId; the provider must be asked for that instrument's ticker, not '4391'."""
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    UniverseAccessor(db, 'Universes').insert('us', _definition(US_CONID, 'AAPL', 'NASDAQ'))
    args = argparse.Namespace(symbols=[str(US_CONID)], source=source, bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert summary['failed'] == 0
    assert history.tickers and set(history.tickers) == {'AAPL'}
    assert _stored_rows(db, US_CONID) > 0


@pytest.mark.parametrize('source', REST_SOURCES)
def test_an_unresolved_numeric_target_never_stores_bars_a_later_conid_would_read(tmp_path, source):
    """``data download 3002`` before 3002 resolves must not leave rows that conId 3002 later reads (mmr-openai)."""
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    args = argparse.Namespace(symbols=[str(ASX_CONID)], source=source, bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert 'did not resolve' in summary['refused_targets'][0]
    assert history.tickers == []

    UniverseAccessor(db, 'Universes').insert('asx', _definition(ASX_CONID, 'BHP', 'ASX'))   # bound afterwards
    assert _stored_rows(db, ASX_CONID) == 0


@pytest.mark.parametrize('source', REST_SOURCES)
def test_an_unresolved_ticker_is_still_fetched_under_its_own_name(tmp_path, source):
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    args = argparse.Namespace(symbols=['ZZZZ'], source=source, bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert summary['failed'] == 0 and set(history.tickers) == {'ZZZZ'}


class _EmptyProvider:
    def __init__(self):
        self.tickers = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        import pandas as pd
        self.tickers.append(ticker)
        return pd.DataFrame()


@pytest.mark.parametrize('source', REST_SOURCES)
def test_the_data_service_fetches_a_us_security_from_a_rest_source(tmp_duckdb_path, source):
    provider = _EmptyProvider()
    service = DataService(**DATA_SERVICE_KEYS[source], duckdb_path=tmp_duckdb_path)
    nasdaq = SimpleNamespace(symbol='AAPL', exchange='SMART', conId=US_CONID, primaryExchange='NASDAQ',
                             timeZoneId='US/Eastern')
    with patch.object(service, '_resolve_symbols', return_value=[nasdaq]), \
            patch(f'trader.data_providers.builtin.{HISTORY_BUILDERS[source]}', return_value=provider):
        result = asyncio.run(service.pull_history(source, symbols=['AAPL'], prev_days=3))
    assert provider.tickers and set(provider.tickers) == {'AAPL'}
    assert result['failed'] == 0 and result['errors'] == []
