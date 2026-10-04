"""Backtest jobs for `research evaluate`: one strategy run over one window.

Jobs are plain picklable values so they can run in a process pool; every
worker rebuilds its storage, costs and rules from ``RunEnvironment``.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import BarSize
from trader.research.evaluation_data import EvaluationDataError
from trader.simulation.backtester import Backtester, BacktestConfig, trace_signature
from trader.simulation.execution_costs import build_realistic_costs, load_execution_costs_config
from trader.simulation.live_rules import PaperAutomationRules


@dataclass(frozen=True)
class RunEnvironment:
    history_db: str
    universe_db: str
    universe_library: str
    execution_costs_path: str
    strategy_file: str
    class_name: str
    conids: tuple[int, ...]
    bar_size: str
    order_notional: float
    account_equity: float
    max_gross_allocation: float


@dataclass(frozen=True)
class WindowJob:
    point_key: str
    params: Mapping[str, Any]
    window_kind: str
    window_index: int
    start: dt.datetime
    end: dt.datetime
    cost_multiplier: float
    replay: int = 0


@dataclass(frozen=True)
class WindowOutcome:
    job: WindowJob
    trades: tuple[Mapping[str, Any], ...]
    equity: tuple[tuple[dt.datetime, float], ...]
    net_pnl: float
    max_drawdown: float
    trace_signature: str
    live_rule_blocks: Mapping[str, int]

    def equity_series(self) -> pd.Series:
        if not self.equity:
            return pd.Series(dtype=float)
        stamps, values = zip(*self.equity)
        return pd.Series(values, index=pd.DatetimeIndex(stamps))


def default_workers() -> int:
    return max(1, min(16, (os.cpu_count() or 2) - 1))


def _require_every_conid_loaded(env: RunEnvironment, job: WindowJob,
                                loaded_conids: Sequence[int]) -> None:
    missing = [conid for conid in env.conids if conid not in loaded_conids]
    if missing:
        raise EvaluationDataError(
            f'no {env.bar_size} bars for conids {missing} in window '
            f'{job.start:%Y-%m-%d}..{job.end:%Y-%m-%d} ({job.window_kind} {job.window_index})')


def run_window_job(env: RunEnvironment, job: WindowJob) -> WindowOutcome:
    storage = TickStorage(env.history_db)
    accessor = UniverseAccessor(env.universe_db, env.universe_library)
    costs = build_realistic_costs(
        env.conids, accessor, load_execution_costs_config(env.execution_costs_path)
    ).scaled(job.cost_multiplier)
    config = BacktestConfig(
        start_date=job.start, end_date=job.end, initial_capital=env.account_equity,
        bar_size=BarSize.parse_str(env.bar_size), cost_model=costs,
        order_notional=env.order_notional,
        live_rules=PaperAutomationRules(max_gross_allocation=env.max_gross_allocation))
    result = Backtester(storage, config).run_from_module(
        env.strategy_file, env.class_name, list(env.conids),
        universe_accessor=accessor, params=dict(job.params))
    _require_every_conid_loaded(env, job, result.loaded_conids)
    curve = result.equity_curve
    return WindowOutcome(
        job=job,
        trades=tuple({'timestamp': t.timestamp, 'conid': int(t.conid), 'action': str(t.action),
                      'quantity': float(t.quantity), 'price': float(t.price),
                      'commission': float(t.commission)} for t in result.trades),
        equity=tuple((pd.Timestamp(ts).to_pydatetime(), float(value)) for ts, value in curve.items()),
        net_pnl=float(curve.iloc[-1]) - env.account_equity if len(curve) else 0.0,
        max_drawdown=float(result.max_drawdown),
        trace_signature=trace_signature(result),
        live_rule_blocks=dict(result.live_rule_blocks))


def _apply_logging_switch(disabled_level: int) -> None:
    logging.disable(disabled_level)


def worker_pool(max_workers: int, mp_context: Any = None) -> ProcessPoolExecutor:
    """A spawned worker starts with logging on and its imports print INFO lines to
    stdout, which would break `--json` output; it inherits the parent's switch."""
    return ProcessPoolExecutor(max_workers=max_workers, mp_context=mp_context,
                               initializer=_apply_logging_switch,
                               initargs=(logging.root.manager.disable,))


def run_jobs(env: RunEnvironment, jobs: Sequence[WindowJob],
             max_workers: int = 1) -> list[WindowOutcome]:
    if max_workers <= 1 or len(jobs) <= 1:
        return [run_window_job(env, job) for job in jobs]
    with worker_pool(min(max_workers, len(jobs))) as pool:
        return list(pool.map(run_window_job, [env] * len(jobs), jobs))
