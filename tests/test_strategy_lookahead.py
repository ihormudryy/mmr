"""Walk-forward lookahead audit for every precompute-based strategy.

Runs ``assert_no_lookahead`` over realistic multi-day 1-min data in BOTH
session shapes — RTH-only and premarket+RTH+after-hours — with truncations
deep enough to cut into a day's opening window mid-formation. The extended-
hours variant is what caught the original violations: VwapReclaim's
``open_vwap`` and OpeningDriveFade's ``day_open``/``drive_low`` were
``transform('first'/'min')`` broadcasts that put a value on bars BEFORE the
bar it came from existed (future data at past indices). Both now use causal
carry-forward (ffill / running cummin) instead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.simulation.lookahead_check import assert_no_lookahead

from strategies.gap_reversion import GapReversion
from strategies.keltner_breakout import KeltnerBreakout
from strategies.late_day_momentum import LateDayMomentum
from strategies.opening_drive_fade import OpeningDriveFade
from strategies.opening_range_breakout import OpeningRangeBreakout
from strategies.vwap_reclaim import VwapReclaim
from strategies.vwap_reversion import VwapReversion

# Deep truncations: cut into the last day's afternoon, midday, opening-range
# window, and pre-market — where day-scoped transforms leak.
HIDES = (1, 2, 5, 10, 30, 60, 120, 240, 390, 500, 700, 900)

STRATEGIES = [
    OpeningRangeBreakout,
    VwapReclaim,
    GapReversion,
    LateDayMomentum,
    OpeningDriveFade,
    VwapReversion,
    KeltnerBreakout,
]


def _synth(days: int = 5, seed: int = 7, premarket: bool = True) -> pd.DataFrame:
    """1-min OHLCV, naive-UTC index (mirrors UTC-keyed DuckDB history)."""
    rng = np.random.default_rng(seed)
    frames = []
    base = 100.0
    for d in range(days):
        day = pd.Timestamp('2026-03-02') + pd.Timedelta(days=d)  # Mon..Fri
        start, end = ('04:00', '19:59') if premarket else ('09:30', '15:59')
        idx_et = pd.date_range(f'{day.date()} {start}', f'{day.date()} {end}',
                               freq='1min', tz='America/New_York')
        idx = idx_et.tz_convert('UTC').tz_localize(None)
        n = len(idx)
        rets = rng.normal(0, 0.0006, n)
        if d == 2:
            rets[:30] -= 0.001  # a down-drive day for the fade strategies
        close = base * np.exp(np.cumsum(rets))
        base = close[-1] * (1 + rng.normal(0, 0.004))  # overnight gap
        high = close * (1 + np.abs(rng.normal(0, 0.0005, n)))
        low = close * (1 - np.abs(rng.normal(0, 0.0005, n)))
        open_ = np.concatenate([[close[0]], close[:-1]])
        vol = rng.integers(500, 5000, n).astype(float)
        et_min = idx_et.hour * 60 + idx_et.minute
        vol[(et_min < 570) | (et_min >= 960)] *= 0.05  # quiet extended hours
        frames.append(pd.DataFrame(
            {'open': open_, 'high': high, 'low': low, 'close': close,
             'volume': vol}, index=idx))
    df = pd.concat(frames)
    df.index.name = 'date'
    return df


@pytest.fixture(scope='module')
def prices_extended() -> pd.DataFrame:
    return _synth(premarket=True)


@pytest.fixture(scope='module')
def prices_rth() -> pd.DataFrame:
    return _synth(premarket=False)


@pytest.mark.parametrize('strategy_cls', STRATEGIES,
                         ids=lambda c: c.__name__)
def test_no_lookahead_with_extended_hours(strategy_cls, prices_extended):
    assert_no_lookahead(strategy_cls(), prices_extended,
                        hide_bars_sequence=HIDES)


@pytest.mark.parametrize('strategy_cls', STRATEGIES,
                         ids=lambda c: c.__name__)
def test_no_lookahead_rth_only(strategy_cls, prices_rth):
    assert_no_lookahead(strategy_cls(), prices_rth,
                        hide_bars_sequence=HIDES)


def test_no_lookahead_vectorbt_strategies(prices_rth):
    """SMICrossOver / VbtMacdBB import vectorbt (slow numba JIT on first
    run) — keep them in one test so the JIT cost is paid once and the
    parametrized fast tests above stay fast."""
    from strategies.smi_crossover import SMICrossOver
    from strategies.vbt_macd_bb import VbtMacdBB
    for cls in (SMICrossOver, VbtMacdBB):
        assert_no_lookahead(cls(), prices_rth, hide_bars_sequence=HIDES)
