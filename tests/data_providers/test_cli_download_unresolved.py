"""Issue #178: a ticker that resolves to no conId is refused, not stored under the ticker.

Research and the strategies read bars by conId, so bars stored under "AAPL" are never read. In split Docker the
scheduler holds no RPC key and cannot ask the trader, so every runbook download was stored that way.
"""
import argparse

from tests.data_providers.test_cli_refresh_conid import _FakeHistory, _run, _stored_rows
from tests.compose_rpc_helpers import load_compose, volumes


def _download(tmp_path, **extra):
    db, history = str(tmp_path / 'mmr.duckdb'), _FakeHistory()
    args = argparse.Namespace(symbols=['AAPL'], source='alpaca', bar_size='15 mins', days=5, force=False, **extra)
    return db, history, _run(db, history, lambda mmr_cli: mmr_cli._handle_data_download(args))


def test_an_unresolved_ticker_is_refused_and_nothing_is_fetched_or_stored(tmp_path):
    db, history, summary = _download(tmp_path)
    assert (summary['failed'], summary['completed']) == (1, 0)
    assert history.tickers == [] and _stored_rows(db, 'AAPL') == 0
    assert 'universe add downloads AAPL --currency USD' in summary['refused_targets'][0]


def test_ticker_key_stores_an_unresolved_ticker_on_purpose(tmp_path):
    db, history, summary = _download(tmp_path, ticker_key=True)
    assert (summary['failed'], summary['completed']) == (0, 1)
    assert history.tickers == ['AAPL'] and _stored_rows(db, 'AAPL') > 0


def test_the_cli_service_mounts_the_data_volume_so_it_can_resolve_and_store():
    cli = load_compose()['services']['cli']
    assert {'source': 'mmr_db_data', 'target': '/home/trader/.local/share/mmr/data'}.items() <= next(
        mount for mount in volumes(cli) if mount['source'] == 'mmr_db_data').items()
