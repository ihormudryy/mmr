"""Issue #119: history depth follows a strategy's declared warm-up (MIN_BARS), and the runtime
holds the strategy back (HISTORY_BELOW_WARMUP) until its frame has that many bars."""
import asyncio
import datetime as dt
import logging
import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trader.bar_size import BarSize
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import Action
from trader.strategy.ai_deployment_source import AI_HISTORY_TIMEOUT_S, ai_history_timeout_s
from trader.strategy.history_depth import (
    HISTORY_BELOW_WARMUP,
    InvalidWarmupDeclaration,
    declared_warmup_bars,
    history_depth,
    ib_request_span,
)
from trader.strategy.strategy_runtime import SignalRecordWriteFailed, StrategyRuntime, _live_strategy_rows
from trader.trading.strategy import Signal, Strategy, StrategyContext

CONID = 265598


# --- depth from the declared warm-up ---------------------------------------------------------------

@pytest.mark.parametrize('bar_size, warmup_bars, expected_days', [
    (BarSize.Mins1, 300, 6),      # 300 bars fit one 300-minute session: 1 session -> 2 + 4 days
    (BarSize.Mins1, 1000, 10),    # 4 sessions -> 6 + 4
    (BarSize.Mins5, 120, 7),      # 60 bars a session -> 2 sessions -> 3 + 4
    (BarSize.Hours1, 300, 94),    # 5 bars a session -> 60 sessions -> 90 + 4
    (BarSize.Days1, 40, 64),      # 40 sessions -> 60 + 4
    (BarSize.Weeks1, 10, 79),     # 50 sessions -> 75 + 4
])
def test_the_depth_covers_the_declared_warm_up(bar_size, warmup_bars, expected_days):
    depth = history_depth(bar_size, warmup_bars, configured_days=1)
    assert (depth.days, depth.warmup_days, depth.capped) == (expected_days, expected_days, False)


def test_a_deeper_configured_depth_wins_and_no_warm_up_keeps_it():
    assert history_depth(BarSize.Days1, 40, configured_days=365).days == 365
    undeclared = history_depth(BarSize.Mins1, 0, configured_days=5)
    assert (undeclared.days, undeclared.warmup_bars, undeclared.warmup_days) == (5, 0, 0)


@pytest.mark.parametrize('bar_size, warmup_bars, cap_days', [
    (BarSize.Secs5, 10_000_000, 180),     # IB serves bars of 30 s or less for six months only
    (BarSize.Mins1, 1_000_000, 365),
    (BarSize.Days1, 5_000, 3650),
])
def test_the_warm_up_part_is_capped_per_bar_size(bar_size, warmup_bars, cap_days):
    depth = history_depth(bar_size, warmup_bars, configured_days=1)
    assert (depth.days, depth.warmup_days, depth.capped, depth.cap_days) == (cap_days, cap_days, True, cap_days)


class _Declares:
    def __init__(self, value):
        self.MIN_BARS = value


def test_a_missing_declaration_is_zero_and_a_bad_one_is_refused():
    assert declared_warmup_bars(object()) == 0
    assert declared_warmup_bars(_Declares(40)) == 40
    for bad in (None, 0, -5, 40.0, '40', True):
        with pytest.raises(InvalidWarmupDeclaration):
            declared_warmup_bars(_Declares(bad))


def test_the_ib_request_span_matches_by_bar_size_string():
    assert ib_request_span(BarSize.Days1) == ib_request_span('1 day') == ('10 Y', 3650)
    assert ib_request_span(BarSize.Mins1) == ('1 W', 7)
    assert ib_request_span(BarSize.Mins30) == ('86400 S', 1)


def test_the_ai_backfill_budget_grows_with_the_ib_requests():
    assert ai_history_timeout_s(BarSize.Mins1, 5, 1) == AI_HISTORY_TIMEOUT_S
    assert ai_history_timeout_s(BarSize.Mins30, 0, 20) == AI_HISTORY_TIMEOUT_S   # no MIN_BARS: the old budget
    assert ai_history_timeout_s(BarSize.Mins1, 70, 2) == 10 * 2 * 60
    assert ai_history_timeout_s(BarSize.Days1, 3650, 1) == AI_HISTORY_TIMEOUT_S


# --- the runtime gate --------------------------------------------------------------------------------

def _bars(count: int) -> pd.DataFrame:
    index = pd.date_range('2026-10-05 13:30', periods=count, freq='1min', tz='UTC', name='date')
    price = np.linspace(100.0, 101.0, count)
    return pd.DataFrame({'open': price, 'high': price, 'low': price, 'close': price, 'volume': 100.0,
                         'vwap': price, 'bar_count': 1.0, 'bid': price, 'ask': price, 'last': price,
                         'last_size': 1.0}, index=index)


class _AlwaysBuys(Strategy):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def on_prices(self, prices):
        self.calls += 1
        return Signal(source_name=self.name, action=Action.BUY, probability=0.9, risk=0.1)


class _NeedsFive(_AlwaysBuys):
    MIN_BARS = 5


class _ExitRecorder:
    def __init__(self):
        self.checked = []

    def check_exits(self, strategy_name, conid, frame):
        self.checked.append(len(frame))


def _runtime() -> StrategyRuntime:
    rt = StrategyRuntime.__new__(StrategyRuntime)  # skip __init__
    rt.strategy_implementations, rt.strategies, rt.streams = [], {}, {}
    rt._hist_bars, rt._hist_bar_days = {}, {}
    rt._last_dispatched_bar, rt._warmup_shortfalls, rt._pending_signals = {}, {}, {}
    rt.signal_proposer = _ExitRecorder()
    rt.dispatched = []
    rt._dispatch_once = lambda strategy, signal, conId, frame: rt.dispatched.append(signal) or True
    return rt


def _install(rt: StrategyRuntime, strategy: Strategy) -> Strategy:
    strategy.install(StrategyContext(name=type(strategy).__name__, bar_size=BarSize.Mins1, conids=[CONID],
                                     universe=None, historical_days_prior=1, paper_only=False, storage=None,
                                     universe_accessor=None, logger=logging.getLogger('test'),
                                     auto_execute='propose'))
    strategy.history_depth = rt._history_depth_for(strategy, 1)
    strategy.enable()
    rt.strategy_implementations.append(strategy)
    rt.strategies.setdefault(CONID, []).append(strategy)
    return strategy


def _feed(rt: StrategyRuntime, strategy: Strategy, bar_count: int) -> None:
    rt._hist_bars[(CONID, BarSize.Mins1)] = _bars(bar_count)
    rt._hist_bar_days[(CONID, BarSize.Mins1)] = 365
    rt._on_tick_for_strategy(strategy, CONID)


def test_below_the_warm_up_on_prices_is_not_called_and_the_code_is_named(caplog):
    rt = _runtime()
    strategy = _install(rt, _NeedsFive())
    with caplog.at_level(logging.WARNING):
        _feed(rt, strategy, 3)
        _feed(rt, strategy, 4)
    assert strategy.calls == 0 and rt.dispatched == []
    assert rt.history_code(strategy) == HISTORY_BELOW_WARMUP
    assert len([r for r in caplog.records if HISTORY_BELOW_WARMUP in r.getMessage()]) == 1
    assert rt.signal_proposer.checked == [3, 4]          # exits still run while held back
    row = next(r for r in _live_strategy_rows(rt) if r['name'] == strategy.name)
    assert row['history_code'] == HISTORY_BELOW_WARMUP
    assert (row['historical_days_prior'], row['history_days']) == (1, 6)


def test_once_the_warm_up_is_met_the_strategy_signals_and_the_code_clears():
    rt = _runtime()
    strategy = _install(rt, _NeedsFive())
    _feed(rt, strategy, 4)
    _feed(rt, strategy, 5)
    assert strategy.calls == 1 and len(rt.dispatched) == 1
    assert rt.history_code(strategy) is None


def test_a_strategy_without_min_bars_is_dispatched_as_before():
    rt = _runtime()
    strategy = _install(rt, _AlwaysBuys())
    _feed(rt, strategy, 2)
    assert strategy.calls == 1 and len(rt.dispatched) == 1
    assert rt.history_code(strategy) is None


def test_unloading_forgets_the_shortfall():
    rt = _runtime()
    strategy = _install(rt, _NeedsFive())
    _feed(rt, strategy, 3)
    assert rt._warmup_shortfalls == {(CONID, strategy.name): (3, 5)}
    rt.unload_strategy(strategy.name)
    assert rt._warmup_shortfalls == {}


def test_the_frame_is_reread_deep_enough_for_the_warm_up(monkeypatch):
    rt = _runtime()
    rt._tick_retention_days = 2
    reads = []
    monkeypatch.setattr(rt, '_read_hist_bars', lambda conId, bar_size, days=0: reads.append(days) or _bars(3))
    plain = _install(rt, _AlwaysBuys())
    rt._strategy_frame(CONID, BarSize.Mins1)
    deep = _install(rt, _NeedsFive())
    deep.history_depth = history_depth(BarSize.Mins1, 1000, 1)   # needs 10 days
    rt._strategy_frame(CONID, BarSize.Mins1)
    rt._strategy_frame(CONID, BarSize.Mins1)
    assert reads == [0, 10]                 # primed once as before, re-read once for the deeper warm-up
    assert plain.history_depth.warmup_days == 0


# --- load and fetch ----------------------------------------------------------------------------------

def _load_runtime(tmp_path, tmp_duckdb_path) -> StrategyRuntime:
    rt = object.__new__(StrategyRuntime)
    rt.strategy_implementations, rt.strategies, rt.streams = [], {}, {}
    rt.strategies_directory = str(tmp_path / 'strategies')
    rt.strategy_config_file = str(tmp_path / 'strategy_runtime.yaml')
    rt.duckdb_path = tmp_duckdb_path
    rt._hist_bars, rt._hist_bar_days = {}, {}
    rt.storage = TickStorage(duckdb_path=tmp_duckdb_path)
    rt.universe_accessor = UniverseAccessor.__new__(UniverseAccessor)
    os.makedirs(rt.strategies_directory, exist_ok=True)
    return rt


def _write(rt: StrategyRuntime, min_bars_line: str) -> str:
    path = os.path.join(rt.strategies_directory, 'warm.py')
    with open(path, 'w') as f:
        f.write(f"""
from trader.trading.strategy import Strategy

class Warm(Strategy):
{min_bars_line}
    def on_prices(self, prices):
        return None
""")
    return path


def _load(rt: StrategyRuntime, path: str, params=None):
    return rt.load_strategy(name='warm', bar_size_str='1 day', conids=[CONID], universe=None,
                            historical_days_prior=5, module=path, class_name='Warm', description='',
                            params=params)


def test_load_derives_the_depth_from_min_bars_and_its_override(tmp_path, tmp_duckdb_path):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    instance = _load(rt, _write(rt, '    MIN_BARS = 40'))
    assert (instance.history_depth.days, StrategyRuntime.history_days(instance)) == (64, 64)
    assert instance.historical_days_prior == 5              # the configured value is not rewritten

    rt2 = _load_runtime(tmp_path / 'b', tmp_duckdb_path)
    overridden = _load(rt2, _write(rt2, '    MIN_BARS = 40'), params={'MIN_BARS': 100})
    assert overridden.history_depth.warmup_bars == 100 and overridden.history_depth.days == 154


def test_load_refuses_a_bad_min_bars(tmp_path, tmp_duckdb_path, caplog):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    with caplog.at_level(logging.ERROR):
        assert _load(rt, _write(rt, "    MIN_BARS = 'forty'")) is None
    assert rt.strategy_implementations == []
    assert any('MIN_BARS' in r.getMessage() for r in caplog.records)


def test_an_undeclared_strategy_keeps_its_configured_depth(tmp_path, tmp_duckdb_path):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    instance = _load(rt, _write(rt, '    pass'))
    assert StrategyRuntime.history_days(instance) == 5


def test_the_startup_fetch_asks_for_the_derived_depth(tmp_path, tmp_duckdb_path):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    instance = _load(rt, _write(rt, '    MIN_BARS = 40'))
    asked = []

    async def fetch(security, bar_size, historical_days, strategy_name):
        asked.append(historical_days)
        return True

    rt._fetch_history_with_resume = fetch
    rt._trader_gateway = type('Gateway', (), {'resolve_instrument': staticmethod(lambda conid: object())})()
    assert asyncio.run(rt._fetch_strategy_history(instance))
    assert asked == [64]


def test_load_refuses_min_bars_none_so_a_one_bar_frame_never_signals(tmp_path, tmp_duckdb_path):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    assert _load(rt, _write(rt, '    MIN_BARS = None')) is None
    assert rt.strategy_implementations == []


def test_a_param_override_of_a_none_min_bars_is_the_declaration(tmp_path, tmp_duckdb_path):
    rt = _load_runtime(tmp_path, tmp_duckdb_path)
    instance = _load(rt, _write(rt, '    MIN_BARS = None'), params={'MIN_BARS': 40})
    assert instance is not None and instance.history_depth.warmup_bars == 40


# --- a backfill makes its new bars visible -----------------------------------------------------------

def _stored_bars(count: int) -> pd.DataFrame:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(second=0, microsecond=0)
    index = pd.date_range(start, periods=count, freq='1min', name='date').tz_convert('America/New_York')
    return pd.DataFrame({'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0 + np.arange(count) / 10,
                         'volume': 1000.0, 'average': 100.0, 'bar_count': 10, 'bar_size': '1 min',
                         'what_to_show': 1}, index=index)


def test_bars_stored_by_a_backfill_after_priming_reach_the_frame(tmp_duckdb_path):
    rt = _runtime()
    rt._tick_retention_days = 2
    rt.history_duckdb_path = tmp_duckdb_path
    tick_data = TickStorage(duckdb_path=tmp_duckdb_path).get_tickdata(bar_size=BarSize.Mins1)
    all_bars = _stored_bars(5)
    tick_data.write(CONID, all_bars.iloc[:3])
    assert len(rt._strategy_frame(CONID, BarSize.Mins1)) == 3

    async def backfill(security, bar_size, historical_days, strategy_name):
        tick_data.write(CONID, all_bars.iloc[3:])
        return True

    rt._fetch_history_with_resume = backfill
    rt._trader_gateway = SimpleNamespace(resolve_instrument=lambda conid: object())
    strategy = SimpleNamespace(name='s', conids=[CONID], universe=None, bar_size=BarSize.Mins1,
                               historical_days_prior=1)
    assert asyncio.run(rt._fetch_strategy_history(strategy))
    assert len(rt._strategy_frame(CONID, BarSize.Mins1)) == 5


# --- a held signal across a hot-swap (case 2 of the mmr-openai review of 2b3f0cda) ---------------------

_SWAPPED = """
from trader.trading.strategy import Strategy, Signal
from trader.objects import Action

class AlwaysBuys(Strategy):
    MIN_BARS = 2

    def on_prices(self, prices):
        return Signal(source_name=self.name, action=Action.BUY, probability=0.9, risk=0.1)
"""


class _GapEvents:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


@pytest.mark.xfail(reason='settled by #139 held-signal hold', strict=True)
def test_a_signal_held_before_a_hot_swap_is_not_sent_by_the_replacement_below_its_warm_up(tmp_path):
    import yaml
    (tmp_path / 'always_buys.py').write_text(_SWAPPED)
    entry = {'name': 'swapped', 'module': 'always_buys.py', 'class_name': 'AlwaysBuys', 'bar_size': '1 min',
             'historical_days_prior': 1, 'conids': [CONID]}
    (tmp_path / 'strategy_runtime.yaml').write_text(yaml.safe_dump({'strategies': [entry]}))
    rt = _runtime()
    del rt._dispatch_once
    rt.strategies_directory = str(tmp_path)
    rt.strategy_config_file = str(tmp_path / 'strategy_runtime.yaml')
    rt.storage = rt.universe_accessor = rt._trader_gateway = None
    rt.paper_trading, rt._config_mtime = True, 0.0
    rt.event_store = _GapEvents()
    if hasattr(StrategyRuntime, '_new_signal_hold'):       # the held-signal hold of PR #139
        rt._signal_hold = rt._new_signal_hold()
    sends, failures = [], [SignalRecordWriteFailed('record down')]

    def dispatch_signal(strategy, signal, conId, frame):
        if failures:
            raise failures.pop()
        sends.append((strategy, signal))

    rt._dispatch_signal = dispatch_signal
    old = rt.load_strategy(name='swapped', bar_size_str='1 min', conids=[CONID], universe=None,
                           historical_days_prior=1, module='always_buys.py', class_name='AlwaysBuys',
                           description='')
    old.enable()
    rt.strategies[CONID] = [old]
    _feed(rt, old, 2)                                       # BUY at MIN_BARS 2; its record write fails: held

    rt.update_strategy_params('swapped', {'MIN_BARS': '5'})
    new = rt.get_strategy('swapped')
    assert new is not old                                    # the hot-swap replaces the object
    new.enable()
    _feed(rt, new, 4)                                        # retries the held BUY, then the gate holds new

    assert sends == []
    assert rt.history_code(new) == HISTORY_BELOW_WARMUP
