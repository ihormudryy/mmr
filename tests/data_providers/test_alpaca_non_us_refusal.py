"""Alpaca looks bars up by ticker, so a non-US instrument must never be fetched through it (issue #127)."""
import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.data_providers.test_cli_refresh_conid import _FakeHistory, _no_trader, _run, _StubContainer, _stored_rows
from tests.test_execution_costs import _definition
from trader.data.universe import UniverseAccessor
from trader.data_providers.alpaca.us_listing import NonUsInstrumentError, require_us_listing
from trader.data_service import DataService

US_CONID, ASX_CONID, UNKNOWN_CONID = 3001, 3002, 3003
JOBS = {'jobs': {'mixed': {'universe': 'mixed', 'source': 'alpaca', 'bar_size': '15 mins', 'days': 5}}}


@pytest.fixture
def mixed_universe(tmp_path):
    db = str(tmp_path / 'mmr.duckdb')
    universes = UniverseAccessor(db, 'Universes')
    universes.insert('mixed', _definition(US_CONID, 'SAME', 'NASDAQ'))
    universes.insert('mixed', _definition(ASX_CONID, 'SAME', 'ASX'))
    universes.insert('mixed', _definition(UNKNOWN_CONID, 'NOEX', ''))
    return db


def _refresh_payload(db, history, capsys):
    def refresh(mmr_cli):
        with patch.object(mmr_cli, '_load_data_refresh_yaml', return_value=JOBS):
            mmr_cli._handle_data_refresh(argparse.Namespace(jobs=['mixed'], refresh_all=False))
    _run(db, history, refresh)
    output = capsys.readouterr().out
    return json.loads(output[output.rindex('\n{') + 1:])


def test_a_refresh_refuses_non_us_and_unknown_entries_but_still_refreshes_the_us_one(mixed_universe, capsys):
    history = _FakeHistory()
    payload = _refresh_payload(mixed_universe, history, capsys)
    job = payload['results'][0]
    assert payload['success'] is False and job['success'] is False
    assert job['failed_symbols'] == 2
    assert 'ALPACA_NON_US_INSTRUMENT' in job['error']
    assert f'conId {ASX_CONID}' in job['error'] and 'ASX' in job['error']
    assert f'conId {UNKNOWN_CONID}' in job['error'] and 'unknown' in job['error']
    assert history.tickers == ['SAME']
    assert _stored_rows(mixed_universe, US_CONID) > 0
    assert _stored_rows(mixed_universe, ASX_CONID) == 0
    assert _stored_rows(mixed_universe, UNKNOWN_CONID) == 0


def test_a_download_of_a_non_us_ticker_through_alpaca_is_refused(tmp_path):
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    UniverseAccessor(db, 'Universes').insert('asx', _definition(ASX_CONID, 'BHP', 'ASX'))
    args = argparse.Namespace(symbols=['BHP'], source='alpaca', bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert 'ALPACA_NON_US_INSTRUMENT' in summary['refused_targets'][0]
    assert history.tickers == [] and _stored_rows(db, ASX_CONID) == 0


@pytest.mark.parametrize('primary_exchange', ['NASDAQ', 'nyse', 'ARCA', 'AMEX', 'BATS', 'ISLAND'])
def test_a_us_listing_is_accepted(primary_exchange):
    require_us_listing(_definition(1, 'AAA', primary_exchange))


@pytest.mark.parametrize('primary_exchange', ['ASX', 'TSE', 'SEHK', 'SMART', '', '  '])
def test_anything_else_is_refused_and_smart_is_a_route_not_a_listing(primary_exchange):
    with pytest.raises(NonUsInstrumentError, match='ALPACA_NON_US_INSTRUMENT'):
        require_us_listing(_definition(1, 'AAA', primary_exchange))


class _RecordingProvider:
    def __init__(self):
        self.tickers = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        self.tickers.append(ticker)
        raise AssertionError('the provider must not be called')


def test_the_data_service_does_not_fetch_a_non_us_security_from_alpaca(tmp_duckdb_path):
    provider = _RecordingProvider()
    service = DataService(alpaca_api_key_id='k', alpaca_api_secret_key='s', duckdb_path=tmp_duckdb_path)
    asx = SimpleNamespace(symbol='BHP', exchange='SMART', conId=ASX_CONID, primaryExchange='ASX',
                          timeZoneId='Australia/Sydney')
    with patch.object(service, '_resolve_symbols', return_value=[asx]), \
            patch('trader.data_providers.builtin._alpaca_history', return_value=provider):
        result = asyncio.run(service.pull_history('alpaca', symbols=['BHP'], prev_days=3))
    assert provider.tickers == []
    assert result['completed'] == 0 and result['failed'] == result['enqueued'] >= 1
    assert 'ALPACA_NON_US_INSTRUMENT' in result['errors'][0]
