"""Market context for Phase B evidence: regimes, benchmark, liquidity.

Pure functions over SPY daily closes and round trips. Every value that cannot
be computed comes back as ``Measured(None, cause)`` — the cause string lands in
the evaluation report, and the paper-v1 rule fails closed. All constants here
enter the family identity; changing one makes a new experiment family.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Sequence

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from trader.research.attribution import AttributionTable, RoundTrip, attribute, classify_regime
# REGIME_LOSS_TOLERANCE lives in paper_v1 (ruleset identity); import, never copy.
from trader.research.rulesets.paper_v1 import REGIME_LOSS_TOLERANCE
from trader.research.validation import benchmark_metrics

BENCHMARK_CONID = 756733           # SPY: the XNYS benchmark and regime index
SPY_LOOKBACK_SESSIONS = 220        # SPY sessions required before period.start
TREND_SMA_SESSIONS = 200           # trend = prev close / 200-session mean - 1
VOLATILITY_SESSIONS = 20           # std (ddof 1) of the last 20 daily returns
REGIME_MIN_SAMPLES = 30            # round trips for an "adequate" regime bucket
REGIME_HOLD_SESSIONS = 3           # a change counts when the label holds this long
TRANSITION_WINDOW_SESSIONS = 5     # first sessions of a new regime
LIQUIDITY_ADV_SESSIONS = 20        # rolling median window, dollar volume
LIQUIDITY_MAX_ADV_SHARE = 0.01     # order notional <= 1% of the lowest median
LIVE_NOTIONAL_TOLERANCE = 0.05     # live entry may exceed attested notional by 5%


class MarketContextError(Exception):
    """The market context cannot be computed; the message names the cause."""


@dataclass(frozen=True)
class Measured:
    """A computed evidence value, or None plus the reason it is missing."""

    value: Optional[Any]
    cause: Optional[str] = None


def regime_labels(spy_closes: pd.Series, session_dates: Sequence[dt.date]) -> pd.Series:
    """One frozen-taxonomy label per session, from SPY closes strictly before it."""
    closes = spy_closes.sort_index()
    need = TREND_SMA_SESSIONS + 1
    labels: dict[dt.date, str] = {}
    for day in session_dates:
        prior = closes[closes.index < day]
        if len(prior) < need:
            raise MarketContextError(
                f'need {need} SPY sessions before {day} for the regime trend, have {len(prior)}')
        trend = float(prior.iloc[-1] / prior.iloc[-TREND_SMA_SESSIONS:].mean() - 1)
        returns = prior.iloc[-(VOLATILITY_SESSIONS + 1):].pct_change().dropna()
        volatility = float(returns.std(ddof=1))
        labels[day] = classify_regime({'trend': trend, 'volatility': volatility})
    return pd.Series(labels, dtype='object')


def _utc_timestamp(moment: dt.datetime) -> pd.Timestamp:
    """A tz-aware timestamp; naive datetimes are taken to be UTC."""
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize('UTC')
    return stamp


def _entry_date(rt: RoundTrip) -> dt.date:
    return _utc_timestamp(rt.open_time).tz_convert('America/New_York').date()


def annotate_regimes(trips: Sequence[RoundTrip], labels: pd.Series) -> list[RoundTrip]:
    """Attach each round trip's entry-session regime label."""
    out = []
    for rt in trips:
        day = _entry_date(rt)
        if day not in labels.index:
            raise MarketContextError(f'no regime label for session {day} (round trip conid {rt.conid})')
        out.append(replace(rt, regime=str(labels[day])))
    return out


def confirmed_regimes(labels: pd.Series) -> list[tuple[dt.date, str]]:
    """(start date, label) of each confirmed regime, in session order.

    A label becomes the current regime when it holds REGIME_HOLD_SESSIONS
    sessions in a row; the regime starts at the first of those sessions.
    """
    dates = list(labels.index)
    values = [str(v) for v in labels]
    regimes: list[tuple[dt.date, str]] = []
    i = 0
    while i + REGIME_HOLD_SESSIONS <= len(values):
        candidate = values[i]
        held = all(values[i + j] == candidate for j in range(REGIME_HOLD_SESSIONS))
        if held and (not regimes or regimes[-1][1] != candidate):
            regimes.append((dates[i], candidate))
            i += REGIME_HOLD_SESSIONS
        else:
            i += 1
    return regimes


@dataclass(frozen=True)
class RegimeEvidence:
    positive_fraction: Measured
    worst_loss: Measured
    transitions_stable: Measured
    table: AttributionTable
    n_changes: int
    transition_group_pnl: float
    transition_group_trades: int


def _transition_window_dates(labels: pd.Series) -> tuple[int, frozenset]:
    dates = list(labels.index)
    changes = confirmed_regimes(labels)[1:]   # the first regime is not a change
    window: set = set()
    for start, _ in changes:
        at = dates.index(start)
        window.update(dates[at:at + TRANSITION_WINDOW_SESSIONS])
    return len(changes), frozenset(window)


def regime_evidence(trips: Sequence[RoundTrip], labels: pd.Series) -> RegimeEvidence:
    """The three paper-v1 regime values over annotated walk-forward round trips."""
    table = attribute(list(trips), by='regime', min_samples=REGIME_MIN_SAMPLES)
    total = table.total_pnl
    no_adequate = f'no regime bucket has {REGIME_MIN_SAMPLES} round trips'

    fraction = table.positive_fraction_of_adequate
    positive = Measured(fraction) if fraction is not None else Measured(None, no_adequate)

    if not table.adequate_buckets:
        worst = Measured(None, no_adequate)
    elif total <= 0:
        worst = Measured(None, 'total net P&L is not positive, so a loss share is undefined')
    else:
        worst = Measured(min(0.0, min(b.share for b in table.adequate_buckets)))

    n_changes, window = _transition_window_dates(labels)
    group = [rt for rt in trips if _entry_date(rt) in window]
    group_pnl = float(sum(rt.pnl for rt in group))
    if n_changes == 0:
        stable = Measured(None, 'no regime change in the walk-forward sessions')
    elif not group:
        stable = Measured(True)   # not active during changes
    elif total <= 0:
        stable = Measured(None, 'total net P&L is not positive, so a giveback share is undefined')
    else:
        stable = Measured(bool(group_pnl >= REGIME_LOSS_TOLERANCE * total))
    return RegimeEvidence(positive, worst, stable, table, n_changes, group_pnl, len(group))


@dataclass(frozen=True)
class LiquidityRow:
    conid: int
    floor_median: Optional[float]
    floor_date: Optional[dt.date]
    share: Optional[float]


@dataclass(frozen=True)
class LiquidityEnvelope:
    within: Measured
    capacity_estimate: Optional[float]
    rows: tuple[LiquidityRow, ...]


def liquidity_envelope(bars: Mapping[int, pd.DataFrame], *, order_notional: float,
                       before: dt.date) -> LiquidityEnvelope:
    """Order notional vs 1% of each conid's lowest prior-close 20-session median
    dollar volume, over sessions strictly before ``before`` (the holdout)."""
    rows: list[LiquidityRow] = []
    causes: list[str] = []
    floors: list[float] = []
    for conid in sorted(bars):
        frame = bars[conid]
        dollars = (frame['close'] * frame['volume'])
        by_session = dollars.groupby(
            pd.Index(frame.index.tz_convert('America/New_York').date)).sum().sort_index()
        by_session = by_session[by_session.index < before]
        medians = by_session.rolling(LIQUIDITY_ADV_SESSIONS).median().shift(1).dropna()
        if medians.empty:
            causes.append(f'conid {conid}: fewer than {LIQUIDITY_ADV_SESSIONS + 1} '
                          f'sessions before the holdout for the dollar-volume median')
            rows.append(LiquidityRow(conid, None, None, None))
            continue
        floor = float(medians.min())
        floors.append(floor)
        rows.append(LiquidityRow(conid, floor, medians.idxmin(),
                                 order_notional / floor if floor > 0 else None))
    if causes:
        return LiquidityEnvelope(Measured(None, '; '.join(causes)), None, tuple(rows))
    capacity = LIQUIDITY_MAX_ADV_SHARE * min(floors)
    return LiquidityEnvelope(Measured(bool(order_notional <= capacity)), capacity, tuple(rows))


@dataclass(frozen=True)
class BenchmarkEvidence:
    ratio: Measured
    scale: Optional[float] = None
    benchmark_return: Optional[float] = None
    benchmark_downside_deviation: Optional[float] = None
    benchmark_recovery_time: Optional[int] = None
    raw_spy_return: Optional[float] = None
    raw_spy_drawdown: Optional[float] = None


def _session_returns_by_date(equity: pd.Series, starting_equity: float) -> pd.Series:
    closes = equity.groupby(pd.Index(equity.index.date)).last()
    levels = np.concatenate([[starting_equity], closes.to_numpy(dtype=float)])
    return pd.Series(np.diff(levels) / levels[:-1], index=closes.index)


def vol_matched_benchmark(holdout_equity: pd.Series, spy_closes: pd.Series, *,
                          account_equity: float) -> BenchmarkEvidence:
    """Scale SPY's holdout session returns to the strategy's volatility, then
    compare session-close drawdowns (spec section 5)."""
    raw = benchmark_metrics(spy_closes.sort_index(), None, periods_per_year=252,
                            strategy_time_in_market=1.0)
    raw_return, raw_dd = raw.strategy.total_return, raw.strategy.max_drawdown
    if holdout_equity is None or len(holdout_equity) == 0:
        return BenchmarkEvidence(Measured(None, 'the holdout produced no equity curve'),
                                 raw_spy_return=raw_return, raw_spy_drawdown=raw_dd)
    strat = _session_returns_by_date(holdout_equity, account_equity)
    spy = spy_closes.sort_index().pct_change().dropna()
    joined = pd.DataFrame({'strat': strat, 'spy': spy}).dropna()
    if len(joined) < 2:
        return BenchmarkEvidence(
            Measured(None, f'only {len(joined)} joined holdout sessions; need at least 2'),
            raw_spy_return=raw_return, raw_spy_drawdown=raw_dd)
    s_std, b_std = joined['strat'].std(ddof=1), joined['spy'].std(ddof=1)
    if s_std == 0:
        return BenchmarkEvidence(Measured(None, 'strategy session returns have zero volatility'),
                                 raw_spy_return=raw_return, raw_spy_drawdown=raw_dd)
    if b_std == 0:
        return BenchmarkEvidence(Measured(None, 'SPY session returns have zero volatility'),
                                 raw_spy_return=raw_return, raw_spy_drawdown=raw_dd)
    scale = float(s_std / b_std)
    # Start from account_equity so a first-session loss counts as drawdown.
    strat_eq = account_equity * np.concatenate([[1.0], (1 + joined['strat']).cumprod()])
    bench_eq = account_equity * np.concatenate([[1.0], (1 + scale * joined['spy']).cumprod()])
    comp = benchmark_metrics(strat_eq, bench_eq, periods_per_year=252,
                             benchmark_time_in_market=1.0)
    bench = comp.benchmark
    if comp.drawdown_ratio is None:
        return BenchmarkEvidence(
            Measured(None, 'the scaled SPY curve has no drawdown in the holdout'), scale,
            bench.total_return, bench.downside_deviation, bench.recovery_time,
            raw_return, raw_dd)
    return BenchmarkEvidence(Measured(float(comp.drawdown_ratio)), scale,
                             bench.total_return, bench.downside_deviation,
                             bench.recovery_time, raw_return, raw_dd)


def time_in_market(trips: Sequence[RoundTrip], *, calendar_name: str,
                   start: dt.date, end: dt.date) -> Optional[float]:
    """Share of regular-session time in [start, end] with >= 1 round trip open."""
    if not trips:
        return None
    cal = xcals.get_calendar(calendar_name)
    sessions = cal.sessions_in_range(str(start), str(end))
    windows = [(cal.session_open(s), cal.session_close(s)) for s in sessions]
    total = sum((c - o).total_seconds() for o, c in windows)
    if total <= 0:
        return None
    spans = sorted((_utc_timestamp(rt.open_time), _utc_timestamp(rt.close_time))
                   for rt in trips)
    merged: list[list[pd.Timestamp]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    covered = 0.0
    for o, c in windows:
        for lo, hi in merged:
            covered += max(0.0, (min(hi, c) - max(lo, o)).total_seconds())
    return covered / total
