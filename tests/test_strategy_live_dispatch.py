"""Live-dispatch parity tests for the Strategy base class and runtime helpers.

Pins three fixes:

1. **Base on_prices bridge** — a strategy implementing only the fast path
   (``precompute`` + ``on_bar``) used to emit NOTHING live, silently: the
   live runtime dispatches only ``on_prices``, whose base implementation
   returned None. Five shipped strategies (Keltner, GapReversion,
   LateDayMomentum, OpeningDriveFade, VwapReversion) were live-dead. The
   base ``on_prices`` now bridges to precompute+on_bar at the latest bar —
   recursion-safe (bridges only when BOTH hooks are overridden).

2. **Upper-case live params** — ``_apply_uppercase_params`` mirrors the
   backtester's ``apply_param_overrides``: a deployed
   ``params: {VOLUME_MULT: 1.0}`` must mean the same thing live as in the
   backtest that validated it. Before, it was accepted and silently ignored
   (the armed ORB deployments ran at the class default 1.5). Unknown
   upper-case keys refuse the load.

3. **dispatch_conid** — the runtime stamps which instrument each
   ``on_prices`` call is for, so multi-instrument strategies stop guessing
   identity from the shape of the data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.objects import Action
from trader.strategy.strategy_runtime import _apply_uppercase_params
from trader.trading.strategy import Signal, Strategy


def _frame(n=50, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range('2026-07-20 13:30', periods=n, freq='1min', tz='UTC')
    close = 100 + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame({
        'open': close, 'high': close + 0.1, 'low': close - 0.1,
        'close': close, 'volume': 1000.0}, index=idx)


class FastPathOnly(Strategy):
    """precompute + on_bar, no on_prices — the live-dead shape."""

    def precompute(self, prices):
        return {'signal_at_last': np.arange(len(prices))}

    def on_bar(self, prices, state, index):
        if index == len(prices) - 1:
            return Signal(source_name='fast', action=Action.BUY,
                          probability=0.6, risk=0.4)
        return None


class PrecomputeWithoutOnBar(Strategy):
    """Overrides precompute but NOT on_bar — the recursion hazard: on_bar's
    default falls back to on_prices, so the bridge must not engage."""

    def precompute(self, prices):
        return {'x': np.zeros(len(prices))}


class NeitherHook(Strategy):
    pass


def test_base_on_prices_bridges_fast_path_strategies():
    signal = FastPathOnly().on_prices(_frame())
    assert signal is not None and signal.action == Action.BUY


def test_bridge_disengages_without_on_bar_override():
    # Must return None (not recurse / not raise).
    assert PrecomputeWithoutOnBar().on_prices(_frame()) is None


def test_bridge_disengages_with_neither_hook():
    assert NeitherHook().on_prices(_frame()) is None


def test_all_shipped_fast_path_strategies_emit_via_on_prices():
    """The five previously live-dead strategies must at least DISPATCH
    through on_prices without error (signal or None, but the precompute
    runs)."""
    from strategies.gap_reversion import GapReversion
    from strategies.keltner_breakout import KeltnerBreakout
    from strategies.late_day_momentum import LateDayMomentum
    from strategies.opening_drive_fade import OpeningDriveFade
    from strategies.vwap_reversion import VwapReversion

    frame = _frame(400)
    for cls in (KeltnerBreakout, GapReversion, LateDayMomentum,
                OpeningDriveFade, VwapReversion):
        strategy = cls()
        # Not installed — name is None; just exercise the dispatch path.
        result = strategy.on_prices(frame)
        assert result is None or isinstance(result, Signal), cls.__name__


# ---------------------------------------------------------------------------
# _apply_uppercase_params
# ---------------------------------------------------------------------------

class Tunable(Strategy):
    RANGE_MINUTES = 30
    VOLUME_MULT = 1.5
    SESSION_TZ = 'America/New_York'
    PAPER_FLAG = True

    def on_prices(self, prices):
        return None


def test_uppercase_params_shadow_instance_not_class():
    a, b = Tunable(), Tunable()
    _apply_uppercase_params(a, {'VOLUME_MULT': 1.0, 'RANGE_MINUTES': 45})
    assert a.VOLUME_MULT == 1.0 and a.RANGE_MINUTES == 45
    assert b.VOLUME_MULT == 1.5 and b.RANGE_MINUTES == 30  # class untouched
    assert Tunable.VOLUME_MULT == 1.5


def test_uppercase_params_coerce_to_attr_type():
    s = Tunable()
    _apply_uppercase_params(s, {'VOLUME_MULT': '1.3', 'RANGE_MINUTES': '45',
                                'SESSION_TZ': 'Australia/Sydney',
                                'PAPER_FLAG': 'false'})
    assert s.VOLUME_MULT == 1.3 and isinstance(s.VOLUME_MULT, float)
    assert s.RANGE_MINUTES == 45 and isinstance(s.RANGE_MINUTES, int)
    assert s.SESSION_TZ == 'Australia/Sydney'
    assert s.PAPER_FLAG is False


def test_uppercase_param_typo_raises_naming_known_tunables():
    with pytest.raises(ValueError) as exc:
        _apply_uppercase_params(Tunable(), {'VOLUME_MULTT': 2.0})
    assert 'VOLUME_MULTT' in str(exc.value)
    assert 'VOLUME_MULT' in str(exc.value)  # known tunables listed


def test_lowercase_params_left_alone():
    s = Tunable()
    _apply_uppercase_params(s, {'roc_period': 10, 'session_tz': 'x'})
    assert not hasattr(s, 'roc_period')  # stays in context.params only


# ---------------------------------------------------------------------------
# dispatch_conid
# ---------------------------------------------------------------------------

def test_dispatch_conid_default_none_and_readable_when_stamped():
    s = Tunable()
    assert s.dispatch_conid is None
    s._dispatch_conid = 4391          # what the runtime dispatch loop does
    assert s.dispatch_conid == 4391


def test_pairs_strategy_uses_stamped_conid():
    from strategies.pairs_zscore import PairsZScore
    from trader.trading.strategy import StrategyContext
    from trader.objects import BarSize
    import logging

    pair = PairsZScore()
    ctx = StrategyContext(
        name='pairs', bar_size=BarSize.Mins1, conids=[111, 222],
        universe=None, historical_days_prior=0, paper_only=True,
        storage=None, universe_accessor=None, logger=logging)
    pair.install(ctx)

    idx = pd.date_range('2026-07-20 13:30', periods=80, freq='1min', tz='UTC')
    leg0 = pd.DataFrame({'close': np.linspace(100, 101, 80)}, index=idx)
    leg1 = pd.DataFrame({'close': np.linspace(50, 50.5, 80)}, index=idx)

    pair._dispatch_conid = 111
    assert pair.on_prices(leg0) is None      # first leg never signals
    assert 111 in pair._prices and 222 not in pair._prices

    pair._dispatch_conid = 222
    pair.on_prices(leg1)
    assert 222 in pair._prices               # identity from the stamp, not guessing
