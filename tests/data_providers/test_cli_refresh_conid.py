"""A refresh job keeps the universe's conId end to end (PR #117 review, issue #111)."""
import argparse
import datetime as dt
import json
from unittest.mock import patch

import pandas as pd
import pytest

from tests.test_execution_costs import _definition
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.data_providers.alpaca.history import _to_frame
from trader.objects import BarSize

DOWNLOADS_CONID, RESEARCH_CONID = 1001, 2002


class _StubContainer:
    def __init__(self, config):
        self._config = config

    def config(self):
        return self._config


class _FakeHistory:
    """Alpaca-shaped frames: one regular 15-min bar per requested calendar day."""

    def __init__(self):
        self.tickers = []

    def get_history(self, ticker, bar_size, start_date, end_date):
        self.tickers.append(ticker)
        day = start_date.date() if isinstance(start_date, dt.datetime) else start_date
        stamp = pd.Timestamp(dt.datetime.combine(day, dt.time(14, 30)), tz='UTC')
        return _to_frame([{'t': stamp.isoformat(), 'o': 1.0, 'h': 1.0, 'l': 1.0, 'c': 1.0, 'v': 10}],
                         bar_size, 'US/Eastern')


def _no_trader(*args, **kwargs):
    raise ConnectionError('no trader in this test')


@pytest.fixture
def same_ticker_twice(tmp_path):
    db = str(tmp_path / 'mmr.duckdb')
    universes = UniverseAccessor(db, 'Universes')
    universes.insert('downloads', _definition(DOWNLOADS_CONID, 'SAME', 'NASDAQ'))
    universes.insert('research', _definition(RESEARCH_CONID, 'SAME', 'NASDAQ'))
    return db


def _stored_rows(db, conid):
    return len(TickStorage(db).get_tickdata(BarSize.parse_str('15 mins')).read(conid))


def _run(db, history, call):
    import trader.mmr_cli as mmr_cli
    with patch('trader.container.Container.instance', return_value=_StubContainer({'duckdb_path': db})), \
            patch.object(mmr_cli, '_rest_history_worker', return_value=history), \
            patch('trader.sdk.MMR', side_effect=_no_trader), \
            patch.object(mmr_cli, '_json_mode', True):
        return call(mmr_cli)


def test_a_refresh_job_writes_the_universe_conid_not_a_reresolved_ticker(same_ticker_twice, capsys):
    db, history = same_ticker_twice, _FakeHistory()
    jobs = {'jobs': {'research_15mins': {'universe': 'research', 'source': 'alpaca',
                                         'bar_size': '15 mins', 'days': 5}}}

    def refresh(mmr_cli):
        with patch.object(mmr_cli, '_load_data_refresh_yaml', return_value=jobs):
            mmr_cli._handle_data_refresh(argparse.Namespace(jobs=['research_15mins'], refresh_all=False))
    _run(db, history, refresh)
    output = capsys.readouterr().out
    payload = json.loads(output[output.rindex('\n{') + 1:])
    assert payload['success'] is True and history.tickers
    assert _stored_rows(db, RESEARCH_CONID) > 0
    assert _stored_rows(db, DOWNLOADS_CONID) == 0


def test_a_download_of_an_ambiguous_ticker_fails_loudly_and_writes_nothing(same_ticker_twice):
    db, history = same_ticker_twice, _FakeHistory()
    args = argparse.Namespace(symbols=['SAME'], source='alpaca', bar_size='15 mins', days=5, force=False)
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert history.tickers == []
    assert _stored_rows(db, RESEARCH_CONID) == 0 and _stored_rows(db, DOWNLOADS_CONID) == 0


def test_a_bound_definition_without_a_conid_fails_instead_of_writing_under_the_ticker(tmp_path):
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    args = argparse.Namespace(symbols=['NOID'], source='alpaca', bar_size='15 mins', days=5, force=False,
                              security_definitions=[_definition(0, 'NOID', 'NASDAQ')])
    summary = _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert history.tickers == [] and _stored_rows(db, 'NOID') == 0
