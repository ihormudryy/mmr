"""Wire-contract and time-exit tests for the signal→proposal bridge.

The original gap these tests pin down: ``SignalProposer.propose`` sends
``source`` / ``max_hold_bars`` / ``close_by_time`` (now also ``close_by_tz``)
on every body, but ``CreateProposalRequest`` was ``extra="forbid"`` WITHOUT
those fields — so against the real typed server every bridge proposal was
rejected with ``extra_forbidden``: the armed ``auto_execute: propose``
strategies could not create a single proposal. Only fake-client tests
existed, so nothing caught it. These tests drive the REAL request model and
the REAL exit-evaluation logic.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from trader.messaging.production_api import CreateProposalRequest
from trader.strategy.signal_proposer import SignalProposer
from trader.trading.proposal_command_service import ProposalCreateRequest


def _bridge_body(**overrides):
    """Exactly the body SignalProposer.propose() builds for a BUY signal."""
    body = {
        'command_id': 'strategy-3f2c', 'conid': 5437, 'action': 'BUY',
        'quantity': None, 'amount': None,
        'confidence': 0.64,
        'reasoning': 'Signal from strategy vwap_reclaim_cat (probability 0.64, risk 0.36)',
        'source': 'strategy:vwap_reclaim_cat',
        'max_hold_bars': 180,
        'close_by_time': '15:45:00',
        'close_by_tz': 'America/New_York',
    }
    body.update(overrides)
    return body


def test_bridge_body_validates_against_real_request_model():
    parsed = CreateProposalRequest(**_bridge_body())
    assert parsed.source == 'strategy:vwap_reclaim_cat'
    assert parsed.max_hold_bars == 180
    assert parsed.close_by_time == '15:45:00'
    assert parsed.close_by_tz == 'America/New_York'


def test_dashboard_body_without_bridge_fields_still_validates():
    body = {'command_id': 'c-1', 'conid': 265598, 'action': 'BUY',
            'quantity': 10.0, 'reasoning': 'manual'}
    parsed = CreateProposalRequest(**body)
    assert parsed.source == ''            # default: the dashboard
    assert parsed.max_hold_bars is None


def test_service_request_accepts_exit_fields():
    request = ProposalCreateRequest(
        conid=5437, action='BUY', max_hold_bars=180,
        close_by_time='15:45:00', close_by_tz='America/New_York')
    assert request.max_hold_bars == 180


# ---------------------------------------------------------------------------
# _exit_reason — the previously hard-coded-None seam, now evaluated against
# the metadata round-tripped on the proposal record.
# ---------------------------------------------------------------------------

@pytest.fixture()
def proposer():
    return SignalProposer(command_client=None, query_client=None,
                          paper_trading=True, account_id='DU111')


def _frame(end_utc: str, bars: int) -> pd.DataFrame:
    idx = pd.date_range(end=end_utc, periods=bars, freq='1min', tz='UTC')
    return pd.DataFrame({'close': 100.0}, index=idx)


def test_exit_reason_none_without_metadata(proposer):
    entry = {'id': 7, 'metadata': {}, 'updated_at': '2026-07-20T14:00:00+00:00'}
    assert proposer._exit_reason(entry, _frame('2026-07-20 15:00:00+00:00', 30)) is None


def test_exit_reason_max_hold_bars_counts_bars_since_execution(proposer):
    entry = {'id': 7, 'metadata': {'max_hold_bars': 10},
             'updated_at': '2026-07-20T14:00:00+00:00'}
    # 9 bars after execution: not yet.
    frame = _frame('2026-07-20 14:09:00+00:00', 60)
    assert proposer._exit_reason(entry, frame) is None
    # 10 bars after execution: triggered.
    frame = _frame('2026-07-20 14:10:00+00:00', 60)
    assert proposer._exit_reason(entry, frame) == 'max_hold_bars=10'


def test_exit_reason_close_by_time_compares_in_declared_tz(proposer):
    """15:45 ET == 19:45 UTC (July, EDT). A raw-UTC comparison would have
    triggered at 15:45 UTC — 11:45 ET, four hours early."""
    entry = {'id': 7, 'metadata': {'close_by_time': '15:45:00',
                                   'close_by_tz': 'America/New_York'},
             'updated_at': '2026-07-20T14:00:00+00:00'}
    # 15:45 UTC == 11:45 ET: must NOT trigger (the old failure mode).
    assert proposer._exit_reason(entry, _frame('2026-07-20 15:45:00+00:00', 30)) is None
    # 19:44 UTC == 15:44 ET: still before the target.
    assert proposer._exit_reason(entry, _frame('2026-07-20 19:44:00+00:00', 30)) is None
    # 19:45 UTC == 15:45 ET: triggered.
    reason = proposer._exit_reason(entry, _frame('2026-07-20 19:45:00+00:00', 30))
    assert reason == 'close_by_time=15:45:00 America/New_York'


def test_exit_reason_close_by_time_defaults_to_utc(proposer):
    entry = {'id': 7, 'metadata': {'close_by_time': '15:45:00'},
             'updated_at': '2026-07-20T14:00:00+00:00'}
    assert proposer._exit_reason(entry, _frame('2026-07-20 15:44:00+00:00', 30)) is None
    assert proposer._exit_reason(
        entry, _frame('2026-07-20 15:45:00+00:00', 30)) == 'close_by_time=15:45:00 UTC'


def test_exit_reason_naive_frame_index_treated_as_utc(proposer):
    entry = {'id': 7, 'metadata': {'close_by_time': '15:45:00',
                                   'close_by_tz': 'America/New_York'},
             'updated_at': '2026-07-20T14:00:00+00:00'}
    idx = pd.date_range(end='2026-07-20 19:45:00', periods=30, freq='1min')  # naive UTC
    frame = pd.DataFrame({'close': 100.0}, index=idx)
    assert proposer._exit_reason(entry, frame) is not None


def test_exit_reason_unparsable_metadata_is_safe(proposer):
    entry = {'id': 7, 'metadata': {'close_by_time': 'not-a-time',
                                   'max_hold_bars': 5},
             'updated_at': 'garbage'}
    assert proposer._exit_reason(entry, _frame('2026-07-20 19:45:00+00:00', 30)) is None


# ---------------------------------------------------------------------------
# Backtester close_by_time timezone semantics (the divergence twin of the
# live fix): an ET-intended flat time must not fire on the UTC clock.
# ---------------------------------------------------------------------------

def test_backtester_exit_condition_tz_comparison(tmp_path):
    """A Signal carrying ``close_by_time=15:45 ET`` replayed over UTC-keyed
    bars must exit at 15:45 ET (19:45 UTC in July), not at 15:45 on the raw
    UTC clock — the old comparison fired the "EOD flat" ~4-5 hours early
    (or instantly, for entries after 15:45 UTC)."""
    from trader.data.data_access import TickStorage
    from trader.data.duckdb_store import DuckDBDataStore
    from trader.data.universe import UniverseAccessor
    from trader.objects import Action, BarSize
    from trader.simulation.backtester import Backtester, BacktestConfig
    from trader.trading.strategy import Signal, Strategy, StrategyContext

    class FlatByEt(Strategy):
        """BUY early in the session, with an ET flat time."""
        def __init__(self):
            super().__init__()
            self._calls = 0

        def on_prices(self, prices):
            self._calls += 1
            if self._calls == 3:
                return Signal(
                    source_name='flat_by_et', action=Action.BUY,
                    probability=0.6, risk=0.4, quantity=10,
                    close_by_time=dt.time(15, 45),
                    close_by_tz='America/New_York',
                )
            return None

    duckdb_path = str(tmp_path / 'bt.duckdb')
    conid = 4391
    # One ET trading day of 1-min bars: 13:30–19:59 UTC == 09:30–15:59 ET (EDT).
    idx = pd.date_range('2026-07-20 13:30', '2026-07-20 19:59', freq='1min', tz='UTC')
    frame = pd.DataFrame({
        'open': 100.0, 'high': 100.1, 'low': 99.9, 'close': 100.0,
        'volume': 1000.0}, index=idx)
    frame.index.name = 'date'
    DuckDBDataStore(duckdb_path).write(str(conid), frame)

    strategy = FlatByEt()
    ctx = StrategyContext(
        name='flat_by_et', bar_size=BarSize.Mins1, conids=[conid],
        universe=None, historical_days_prior=0, paper_only=True,
        storage=TickStorage(duckdb_path=duckdb_path),
        universe_accessor=UniverseAccessor.__new__(UniverseAccessor),
        logger=__import__('logging'),
    )
    strategy.install(ctx)

    backtester = Backtester(
        storage=TickStorage(duckdb_path=duckdb_path),
        config=BacktestConfig(
            start_date=dt.datetime(2026, 7, 20, 13, 30, tzinfo=dt.timezone.utc),
            end_date=dt.datetime(2026, 7, 20, 20, 0, tzinfo=dt.timezone.utc),
        ),
    )
    result = backtester.run(strategy, [conid])

    exits = [t for t in result.trades if t.action == Action.SELL]
    assert exits, 'the ET flat time must synthesize an exit'
    exit_ts = pd.Timestamp(exits[0].timestamp)
    exit_et = (exit_ts.tz_localize('UTC') if exit_ts.tzinfo is None
               else exit_ts.tz_convert('UTC')).tz_convert('America/New_York')
    assert exit_et.time() >= dt.time(15, 45), (
        f'exit fired at {exit_et.time()} ET — the UTC-clock bug')
