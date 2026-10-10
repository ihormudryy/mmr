"""Issue #146: a signal's age counts from its bar's close, not from the bar's start label.

Every path into the live strategy frame labels a sub-daily bar by its start: IB history (formatDate=2) and
Alpaca ``t`` are bar starts, and ``resample_ticks_to_bars`` uses the pandas default (left label). A bar is
handed to the strategy only once a tick lands in the next bar, so a signal is recorded at least one bar
after its ``signal_time``.
"""
import datetime as dt
import logging

import numpy as np
import pandas as pd
import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, et
from tests.ai.runtime.test_controller import rig_at
from tests.test_signal_proposer import _make_runtime
from tests.test_strategy_runtime import _make_ticker
from trader.ai.engine import SignalOpportunity
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.signal_intake import BAR_SIZE_UNKNOWN, STALE, SignalIntake
from trader.ai.store import AiStore
from trader.bar_size import BarSize
from trader.data.duckdb_store import DuckDBConnection
from trader.data.market_data import normalize_ticker
from trader.data.strategy_signal_record import StrategySignalRecord
from trader.objects import Action
from trader.trading.strategy import Signal, Strategy, StrategyContext

MAX_AGE = dt.timedelta(seconds=300)


class _AlwaysBuys(Strategy):
    def on_prices(self, prices):
        return Signal(source_name=self.name, action=Action.BUY, probability=0.9, risk=0.1)


def _stored_bars(first_start: dt.datetime, count: int, bar_size: BarSize) -> pd.DataFrame:
    """History as DuckDB holds it: one row per bar, indexed by the bar's start."""
    index = pd.date_range(first_start, periods=count, freq=BarSize.to_pandas_freq(bar_size), name="date")
    price = np.linspace(230.0, 231.0, count)
    return pd.DataFrame({"open": price, "high": price, "low": price, "close": price, "volume": 100.0,
                         "vwap": price, "bar_count": 1.0, "bid": price, "ask": price, "last": price,
                         "last_size": 1.0}, index=index)


def _strategy_runtime(tmp_path, bar_size: BarSize, record_clock: list):
    rt = _make_runtime(tmp_path)
    rt._hist_bars, rt._tick_retention_days = {}, 1
    rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "mmr.duckdb")),
                                            now=lambda: record_clock[0])
    strategy = _AlwaysBuys()
    strategy.install(StrategyContext(name="orb15", bar_size=bar_size, conids=[AAPL], universe=None,
                                     historical_days_prior=1, paper_only=False, storage=None,
                                     universe_accessor=None, logger=logging.getLogger("test"),
                                     auto_execute="propose"))
    strategy.history_depth = rt._history_depth_for(strategy, 1)
    strategy.enable()
    rt.strategy_implementations.append(strategy)
    rt.strategies[AAPL] = [strategy]
    return rt


def _tick(rt, at: dt.datetime, record_clock: list) -> None:
    record_clock[0] = at
    rt.on_ticker_next(_make_ticker(conid=AAPL, time=at))


@pytest.mark.asyncio
async def test_a_15_minute_signal_from_the_live_frame_is_judged_not_stale(tmp_path):
    record_clock = [et(10, 45, 5)]
    rt = _strategy_runtime(tmp_path, BarSize.Mins15, record_clock)
    rt._hist_bars[(AAPL, BarSize.Mins15)] = _stored_bars(et(9, 30), 5, BarSize.Mins15)    # 09:30 .. 10:30
    rt._hist_bar_days[(AAPL, BarSize.Mins15)] = 365
    for at in (et(10, 45, 5), et(10, 52), et(10, 59, 59), et(11, 0, 1)):
        _tick(rt, at, record_clock)

    signals = [s.to_json() for s in rt.signal_record.read(0, 10).signals]
    assert [(s["signal_time"], s["recorded_at"]) for s in signals] == [
        (et(10, 30).isoformat(), et(10, 45, 5).isoformat()),     # the last stored bar, closed at 10:45
        (et(10, 45).isoformat(), et(11, 0, 1).isoformat())]      # the live bar 10:45-11:00, labelled by its start
    assert {s["bar_size"] for s in signals} == {"15 mins"}

    rig = await rig_at(tmp_path / "ai", et(11, 0, 30))
    rig.trader.signals.record = signals
    await rig.signals_then_drain()
    old, just_closed = signals
    assert rig.opportunity(old) == ("MISSED", "STALE")           # closed 15 min 30 s ago
    assert rig.opportunity(just_closed) == ("DECIDED", None)     # closed 30 s ago: Jev judges it


def _opportunity(signal_time: dt.datetime, bar_size) -> SignalOpportunity:
    return SignalOpportunity("sig-" + "1" * 32, 1, "orb", AAPL, "BUY", 0.6, signal_time, signal_time,
                             bar_size=bar_size)


@pytest.fixture
def intake(tmp_path):
    clock = FakeClock(et(11, 0, 30))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    return SignalIntake(store=store, supervisor=None, clock=clock, max_age_seconds=300)


BAR_SIZES = [("1 min", dt.timedelta(minutes=1)), ("5 mins", dt.timedelta(minutes=5)),
             ("15 mins", dt.timedelta(minutes=15)), ("1 hour", dt.timedelta(hours=1))]


@pytest.mark.parametrize("bar_size,length", BAR_SIZES)
def test_a_signal_on_a_just_completed_bar_is_fresh(intake, bar_size, length):
    now = et(11, 0, 1)
    assert intake.stale_reason(_opportunity(et(11, 0) - length, bar_size), now) is None


@pytest.mark.parametrize("bar_size,length", BAR_SIZES)
def test_a_signal_older_than_its_bar_close_plus_the_max_age_is_stale(intake, bar_size, length):
    bar_start = et(11, 0) - length
    close = bar_start + length
    assert intake.stale_reason(_opportunity(bar_start, bar_size), close + MAX_AGE) is None
    assert intake.stale_reason(_opportunity(bar_start, bar_size), close + MAX_AGE + dt.timedelta(seconds=1)) == STALE


@pytest.mark.parametrize("bar_size", [None, "1 day", "1 week", "1 month", "7 mins"])
def test_a_signal_whose_bar_close_is_unknown_is_never_fresh(intake, bar_size):
    assert intake.stale_reason(_opportunity(et(11, 0), bar_size), et(11, 0)) == BAR_SIZE_UNKNOWN


@pytest.mark.asyncio
async def test_expiry_names_an_unknown_bar_size_apart_from_an_old_signal(tmp_path, caplog):
    rig = await rig_at(tmp_path, et(11, 0, 30))
    unknown = rig.trader.signals.add(at=et(10, 59), bar_size=None)
    old = rig.trader.signals.add(at=et(10, 50))
    with caplog.at_level(logging.ERROR):
        await rig.signals_then_drain()
    assert rig.opportunity(unknown) == ("MISSED", BAR_SIZE_UNKNOWN)
    assert rig.opportunity(old) == ("MISSED", STALE)
    assert any(BAR_SIZE_UNKNOWN in r.getMessage() and unknown["source_event_id"] in r.getMessage()
               for r in caplog.records)
