# Paper Evidence Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute the five missing `paper-v1` evidence values (liquidity envelope, vol-matched SPY benchmark, three regime values) from real market data, key "holdout opened once" on strategy + window, and refuse live automated entries larger than the attested order notional.

**Architecture:** One new pure module, `trader/research/market_context.py`, turns SPY daily closes into regime labels, a vol-matched benchmark and a liquidity envelope. `trader/research/evaluation.py` feeds the values into the existing `EligibilityEvidence` fields. The registry gains a strategy-wide holdout-overlap guard. `paper_v1.py` is never edited (its digest is the ruleset identity).

**Tech Stack:** Python 3.12, pandas, numpy, DuckDB, exchange_calendars, pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-paper-evidence-phase-b-design.md` — read it first; every constant and edge case below comes from it.

## Global Constraints

- Branch `feat/paper-evidence-phase-b`, worktree `/Users/mudryy/private/mmr/.worktrees/paper-evidence-phase-b`, based on master `45520990`.
- **Never edit `trader/research/rulesets/paper_v1.py`** — any change invalidates every attestation (the module digest is the ruleset identity).
- Constants (exact values, all in `trader/research/market_context.py`): `BENCHMARK_CONID = 756733`, `SPY_LOOKBACK_SESSIONS = 220`, `TREND_SMA_SESSIONS = 200`, `VOLATILITY_SESSIONS = 20`, `REGIME_MIN_SAMPLES = 30`, `REGIME_HOLD_SESSIONS = 3`, `TRANSITION_WINDOW_SESSIONS = 5`, `LIQUIDITY_ADV_SESSIONS = 20`, `LIQUIDITY_MAX_ADV_SHARE = 0.01`, `LIVE_NOTIONAL_TOLERANCE = 0.05`.
- Every missing evidence value carries a human-readable cause string ("fail loudly"); never a bare `None` without a cause in the report.
- Lookahead is forbidden: session `d`'s regime label uses only SPY closes strictly before `d`.
- Tests: `pytest <file> --timeout=60 -q`. Before each commit run the touched test files; before the final task run the full suite: `.venv-or-main-venv pytest tests/ --timeout=30 --timeout-method=thread -q --ignore=tests/test_ibrx_async.py` (use `/Users/mudryy/private/mmr/.venv/bin/python -m pytest` — the worktree has no venv; same commit base, editable install points at the main checkout, so run pytest with `PYTHONPATH=$PWD` from the worktree root to import the worktree's code: `cd <worktree> && PYTHONPATH=$PWD /Users/mudryy/private/mmr/.venv/bin/python -m pytest ...`).
- Commit messages: conventional commits, lowercase, imperative. End every commit with a blank line then `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Review Focus

1. **SPY history too short** (fewer than 220 XNYS sessions before `period.start`): must stop before any trial with the exact `mmr data download SPY --bar-size "1 day" --days <n>` command — test in Task 5.
2. **SELL intents and the notional cap**: an exit must never be refused by `ORDER_EXCEEDS_ATTESTED_NOTIONAL` — test in Task 8.
3. **Volatility exactly on a band edge** (1% or 2% daily): banding is deterministic (`< 0.01` low, `< 0.02` normal, else high) — test in Task 1.
4. **Holdout folds without parseable dates** (legacy imports, old test fixtures store `{"kind": "holdout", "test": "2025"}`): the overlap guard skips them instead of crashing — test in Task 6.
5. **A flat strategy in the holdout** (zero session-return volatility, or an empty equity curve): benchmark ratio is missing with a cause, rule fails closed, run completes — test in Task 4.

---

## File structure

- Create `trader/research/market_context.py` — constants, `Measured`, regime labels, regime evidence, transitions, liquidity, benchmark, time in market. Pure: pandas in, values out, no DB.
- Create `tests/research/test_market_context.py` — all pure-function tests.
- Modify `trader/research/evaluation_data.py` — `load_benchmark_closes`, SPY qualification, manifest file entry.
- Modify `trader/research/evaluation.py` — wire context into evidence, pre-run overlap refusal, family identity additions.
- Modify `trader/research/experiment_registry.py` — `opened_holdout_windows`, overlap guard inside `open_holdout`.
- Modify `trader/research/evaluation_report.py` — "Market context" section + missing-value causes.
- Modify `trader/automation/session_risk.py` — `ORDER_EXCEEDS_ATTESTED_NOTIONAL`.
- Modify `tests/research/evaluation_fixtures.py` — `write_trend_bars` also writes SPY daily bars (single choke point: every evaluation test gets SPY data for free).

### Shared interfaces (used across tasks)

```python
# trader/research/market_context.py
@dataclass(frozen=True)
class Measured:
    """A computed evidence value, or None plus the reason it is missing."""
    value: Optional[Any]
    cause: Optional[str] = None

class MarketContextError(Exception): ...

def regime_labels(spy_closes: pd.Series, session_dates: Sequence[dt.date]) -> pd.Series  # date -> label
def annotate_regimes(trips: Sequence[RoundTrip], labels: pd.Series) -> list[RoundTrip]
def confirmed_regimes(labels: pd.Series) -> list[tuple[dt.date, str]]
@dataclass(frozen=True)
class RegimeEvidence:
    positive_fraction: Measured
    worst_loss: Measured
    transitions_stable: Measured
    table: AttributionTable
    n_changes: int
    transition_group_pnl: float
    transition_group_trades: int
def regime_evidence(trips: Sequence[RoundTrip], labels: pd.Series) -> RegimeEvidence
@dataclass(frozen=True)
class LiquidityRow:
    conid: int
    floor_median: Optional[float]   # lowest 20-session rolling median dollar volume, prior-close known
    floor_date: Optional[dt.date]
    share: Optional[float]          # order_notional / floor_median
@dataclass(frozen=True)
class LiquidityEnvelope:
    within: Measured                # bool or None+cause
    capacity_estimate: Optional[float]
    rows: tuple[LiquidityRow, ...]
def liquidity_envelope(bars: Mapping[int, pd.DataFrame], *, order_notional: float,
                       before: dt.date) -> LiquidityEnvelope
@dataclass(frozen=True)
class BenchmarkEvidence:
    ratio: Measured
    scale: Optional[float]                    # k
    benchmark_return: Optional[float]         # of the SCALED curve
    benchmark_downside_deviation: Optional[float]
    benchmark_recovery_time: Optional[int]
    raw_spy_return: Optional[float]
    raw_spy_drawdown: Optional[float]
def vol_matched_benchmark(holdout_equity: pd.Series, spy_closes: pd.Series, *,
                          account_equity: float) -> BenchmarkEvidence
def time_in_market(trips: Sequence[RoundTrip], *, calendar_name: str,
                   start: dt.date, end: dt.date) -> Optional[float]
```

---

### Task 1: market_context constants, regime labels, annotation

**Files:**
- Create: `trader/research/market_context.py`
- Create: `tests/research/test_market_context.py`

**Interfaces:**
- Consumes: `trader.research.attribution.classify_regime`, `RoundTrip`.
- Produces: constants above, `Measured`, `MarketContextError`, `regime_labels`, `annotate_regimes`.

- [ ] **Step 1: Write the failing tests**

```python
"""Pure market-context maths for Phase B evidence (spec 2026-10-05)."""
import dataclasses
import datetime as dt

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from trader.research import market_context as mc
from trader.research.attribution import RoundTrip

XNYS = xcals.get_calendar('XNYS')


def spy_series(*, n_sessions: int, end: str = '2024-03-28', drift: float = 0.0,
               last_20_std: float = 0.0) -> pd.Series:
    """SPY closes over the last n_sessions XNYS sessions ending at `end`.
    Base price 400 with `drift` per session; when last_20_std > 0 the final 21
    closes alternate so their daily returns have that exact std."""
    sessions = XNYS.sessions_in_range('2015-01-01', end)[-n_sessions:]
    dates = [s.date() for s in sessions]
    prices = 400.0 * (1 + drift) ** np.arange(n_sessions)
    if last_20_std > 0:
        # Alternate +r/-r returns over the last 20 returns: std(ddof=1) ≈ r.
        r = last_20_std
        for i in range(n_sessions - 20, n_sessions):
            sign = 1 if (i % 2 == 0) else -1
            prices[i] = prices[i - 1] * (1 + sign * r)
    return pd.Series(prices, index=pd.Index(dates))


def trip(day: dt.date, pnl: float, conid: int = 1001) -> RoundTrip:
    opened = dt.datetime.combine(day, dt.time(15, 0), tzinfo=dt.timezone.utc)
    return RoundTrip(conid=conid, open_time=opened,
                     close_time=opened + dt.timedelta(hours=1), quantity=10,
                     entry_price=100.0, exit_price=100.0 + pnl / 10, pnl=pnl)


def test_labels_match_hand_computed_trend_and_volatility():
    spy = spy_series(n_sessions=260, drift=0.001)   # rising, tiny vol -> bull_low_vol
    day = spy.index[-1]
    labels = mc.regime_labels(spy, [day])
    prior = spy[spy.index < day]
    trend = prior.iloc[-1] / prior.iloc[-mc.TREND_SMA_SESSIONS:].mean() - 1
    assert trend > 0
    assert labels[day] == 'bull_low_vol'


def test_falling_high_vol_is_bear_high_vol():
    spy = spy_series(n_sessions=260, drift=-0.001, last_20_std=0.025)
    day = spy.index[-1]
    assert mc.regime_labels(spy, [day])[day] == 'bear_high_vol'


def test_volatility_band_edges_are_deterministic():
    # volatility_band: < 0.01 low, < 0.02 normal, else high (frozen taxonomy).
    from trader.research.attribution import volatility_band
    assert volatility_band(0.0099999) == 'low'
    assert volatility_band(0.01) == 'normal'
    assert volatility_band(0.02) == 'high'


def test_no_lookahead_future_bars_do_not_change_past_labels():
    spy = spy_series(n_sessions=300, drift=0.001)
    day = spy.index[250]
    before = mc.regime_labels(spy, [day])[day]
    crashed = spy.copy()
    crashed.iloc[251:] = crashed.iloc[251:] * 0.5   # future crash
    assert mc.regime_labels(crashed, [day])[day] == before


def test_insufficient_spy_history_raises_with_the_need():
    spy = spy_series(n_sessions=100)
    with pytest.raises(mc.MarketContextError, match='SPY sessions'):
        mc.regime_labels(spy, [spy.index[-1]])


def test_annotate_sets_the_entry_session_regime():
    spy = spy_series(n_sessions=260, drift=0.001)
    day = spy.index[-1]
    labels = mc.regime_labels(spy, [day])
    [annotated] = mc.annotate_regimes([trip(day, 5.0)], labels)
    assert annotated.regime == 'bull_low_vol'


def test_annotate_refuses_a_trip_on_an_unlabelled_session():
    spy = spy_series(n_sessions=260, drift=0.001)
    labels = mc.regime_labels(spy, [spy.index[-1]])
    with pytest.raises(mc.MarketContextError, match='no regime label'):
        mc.annotate_regimes([trip(spy.index[-5], 5.0)], labels)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/mudryy/private/mmr/.worktrees/paper-evidence-phase-b && PYTHONPATH=$PWD /Users/mudryy/private/mmr/.venv/bin/python -m pytest tests/research/test_market_context.py --timeout=60 -q`
Expected: FAIL — `trader.research.market_context` does not exist.

- [ ] **Step 3: Write the implementation**

```python
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

import pandas as pd

from trader.research.attribution import AttributionTable, RoundTrip, attribute, classify_regime

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


def _entry_date(rt: RoundTrip) -> dt.date:
    stamp = pd.Timestamp(rt.open_time)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize('UTC')
    return stamp.tz_convert('America/New_York').date()


def annotate_regimes(trips: Sequence[RoundTrip], labels: pd.Series) -> list[RoundTrip]:
    """Attach each round trip's entry-session regime label."""
    out = []
    for rt in trips:
        day = _entry_date(rt)
        if day not in labels.index:
            raise MarketContextError(f'no regime label for session {day} (round trip conid {rt.conid})')
        out.append(replace(rt, regime=str(labels[day])))
    return out
```

(Leave room in the module: Tasks 2–4 append to this file.)

- [ ] **Step 4: Run the tests to verify they pass**

Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/research/market_context.py tests/research/test_market_context.py
git commit -m "feat(research): frozen-lookback regime labels for phase B evidence"
```

---

### Task 2: regime evidence (fraction, worst loss, transition stability)

**Files:**
- Modify: `trader/research/market_context.py` (append)
- Test: `tests/research/test_market_context.py` (append)

**Interfaces:**
- Consumes: Task 1's `regime_labels`, `annotate_regimes`, `Measured`; `attribution.attribute`.
- Produces: `confirmed_regimes(labels) -> list[(date, label)]`, `RegimeEvidence`, `regime_evidence(trips, labels) -> RegimeEvidence`. `trips` must already be annotated.

- [ ] **Step 1: Write the failing tests**

```python
def labels_from(pairs) -> pd.Series:
    """pairs: [(date_str, label), ...] -> labels Series."""
    return pd.Series({dt.date.fromisoformat(d): lab for d, lab in pairs}, dtype='object')


def seq_labels(labs: list[str], start='2024-02-01') -> pd.Series:
    sessions = XNYS.sessions_in_range(start, '2024-12-31')[:len(labs)]
    return pd.Series(dict(zip((s.date() for s in sessions), labs)), dtype='object')


def test_a_flapping_label_is_not_a_regime_change():
    labels = seq_labels(['bull_low_vol'] * 5 + ['bear_low_vol'] + ['bull_low_vol'] * 6)
    assert [lab for _, lab in mc.confirmed_regimes(labels)] == ['bull_low_vol']


def test_a_label_held_three_sessions_is_a_change():
    labels = seq_labels(['bull_low_vol'] * 5 + ['bear_low_vol'] * 3 + ['bull_low_vol'] * 4)
    changed = mc.confirmed_regimes(labels)
    assert [lab for _, lab in changed] == ['bull_low_vol', 'bear_low_vol', 'bull_low_vol']
    # the bear regime starts at the FIRST of its three sessions
    assert changed[1][0] == labels.index[5]


def _annotated(labels, day_pnls):
    """day_pnls: [(session_index, pnl), ...] -> annotated trips."""
    return mc.annotate_regimes(
        [trip(labels.index[i], pnl, conid=1001 + n) for n, (i, pnl) in enumerate(day_pnls)], labels)


def test_transitions_stable_is_none_without_a_change():
    labels = seq_labels(['bull_low_vol'] * 10)
    evidence = mc.regime_evidence(_annotated(labels, [(2, 50.0)] * 40), labels)
    assert evidence.transitions_stable.value is None
    assert 'no regime change' in evidence.transitions_stable.cause


def test_transitions_stable_is_true_with_no_trades_in_windows():
    labels = seq_labels(['bull_low_vol'] * 10 + ['bear_low_vol'] * 10)
    # all trades in the first regime, none in sessions 10..14 (the window)
    evidence = mc.regime_evidence(_annotated(labels, [(2, 50.0)] * 40), labels)
    assert evidence.transitions_stable.value is True


def test_transitions_unstable_when_window_trades_give_back_over_ten_percent():
    labels = seq_labels(['bull_low_vol'] * 10 + ['bear_low_vol'] * 10)
    trips = _annotated(labels, [(2, 50.0)] * 40 + [(11, -300.0)])  # -300 vs total 1700
    evidence = mc.regime_evidence(trips, labels)
    assert evidence.transitions_stable.value is False
    assert evidence.transition_group_trades == 1


def test_positive_fraction_and_worst_loss_over_adequate_buckets():
    labels = seq_labels(['bull_low_vol'] * 10 + ['bear_low_vol'] * 10)
    trips = _annotated(labels, [(2, 50.0)] * 30 + [(16, -30.0)] * 30)  # bear window ends at 14
    evidence = mc.regime_evidence(trips, labels)
    assert evidence.positive_fraction.value == 0.5          # 1 of 2 adequate buckets positive
    total = 30 * 50.0 - 30 * 30.0                           # 600
    assert evidence.worst_loss.value == pytest.approx(-900.0 / total)


def test_regime_values_missing_without_an_adequate_bucket():
    labels = seq_labels(['bull_low_vol'] * 10)
    evidence = mc.regime_evidence(_annotated(labels, [(2, 50.0)] * 10), labels)  # 10 < 30
    assert evidence.positive_fraction.value is None
    assert str(mc.REGIME_MIN_SAMPLES) in evidence.positive_fraction.cause


def test_worst_loss_missing_when_total_pnl_is_not_positive():
    labels = seq_labels(['bull_low_vol'] * 10)
    evidence = mc.regime_evidence(_annotated(labels, [(2, -5.0)] * 40), labels)
    assert evidence.worst_loss.value is None
    assert 'not positive' in evidence.worst_loss.cause
```

- [ ] **Step 2: Run, expect FAIL** (`confirmed_regimes` undefined).

- [ ] **Step 3: Implementation (append to market_context.py)**

```python
# REGIME_LOSS_TOLERANCE lives in paper_v1 (ruleset identity); import, never copy.
from trader.research.rulesets.paper_v1 import REGIME_LOSS_TOLERANCE


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
```

- [ ] **Step 4: Run, expect PASS.** Also run `tests/research/test_attribution*.py` if present (shared module untouched, but verify): `PYTHONPATH=$PWD .../python -m pytest tests/research -k attribution --timeout=60 -q`.

- [ ] **Step 5: Commit** — `feat(research): regime fraction, worst-loss share and transition stability`

---

### Task 3: liquidity envelope and capacity estimate

**Files:**
- Modify: `trader/research/market_context.py` (append)
- Test: `tests/research/test_market_context.py` (append)

**Interfaces:**
- Produces: `LiquidityRow`, `LiquidityEnvelope`, `liquidity_envelope(bars, *, order_notional, before)`. `bars` is the evaluator's `dict[int, pd.DataFrame]` of 1-min (or 15-min) UTC-indexed OHLCV frames with `close` and `volume`.

- [ ] **Step 1: Write the failing tests**

```python
def intraday_frame(*, sessions: int, dollar_per_session: float, start='2024-02-01') -> pd.DataFrame:
    days = XNYS.sessions_in_range(start, '2024-12-31')[:sessions]
    stamps, price = [], 100.0
    for s in days:
        opens = XNYS.session_open(s)
        stamps.extend(pd.date_range(opens, periods=26, freq='15min'))
    index = pd.DatetimeIndex(stamps).tz_convert('UTC')
    volume = dollar_per_session / (26 * price)
    return pd.DataFrame({'close': price, 'volume': volume}, index=index)


def _before(frame) -> dt.date:
    return dt.date(2099, 1, 1)   # no holdout cut in these unit tests


def test_order_within_one_percent_of_the_floor_median_passes():
    bars = {1001: intraday_frame(sessions=30, dollar_per_session=1_000_000)}
    envelope = mc.liquidity_envelope(bars, order_notional=10_000, before=_before(bars))
    assert envelope.within.value is True
    assert envelope.capacity_estimate == pytest.approx(10_000)
    assert envelope.rows[0].floor_median == pytest.approx(1_000_000)


def test_order_over_one_percent_fails():
    bars = {1001: intraday_frame(sessions=30, dollar_per_session=1_000_000)}
    assert mc.liquidity_envelope(bars, order_notional=10_001, before=_before(bars)).within.value is False


def test_the_floor_is_known_as_of_the_previous_close():
    # 21 sessions of 1M then 20 of 100k: the rolling median that includes the
    # thin sessions only exists for windows ending before `before` minus one.
    frame = pd.concat([intraday_frame(sessions=21, dollar_per_session=1_000_000),
                       intraday_frame(sessions=20, dollar_per_session=100_000, start='2024-03-05')])
    envelope = mc.liquidity_envelope({1001: frame}, order_notional=1_500, before=_before(frame))
    assert envelope.within.value is False          # thin tail drags the floor down
    assert envelope.rows[0].floor_median < 1_000_000


def test_too_few_sessions_is_missing_with_a_cause():
    bars = {1001: intraday_frame(sessions=10, dollar_per_session=1_000_000)}
    envelope = mc.liquidity_envelope(bars, order_notional=100, before=_before(bars))
    assert envelope.within.value is None
    assert 'conid 1001' in envelope.within.cause


def test_sessions_on_or_after_the_holdout_are_ignored():
    frame = intraday_frame(sessions=40, dollar_per_session=1_000_000)
    cut = sorted({ts.date() for ts in frame.index.tz_convert('America/New_York')})[25]
    envelope = mc.liquidity_envelope({1001: frame}, order_notional=100, before=cut)
    assert envelope.within.value is True
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implementation (append)**

```python
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
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(research): liquidity envelope from prior-close dollar-volume medians`

---

### Task 4: vol-matched benchmark and time in market

**Files:**
- Modify: `trader/research/market_context.py` (append)
- Test: `tests/research/test_market_context.py` (append)

**Interfaces:**
- Consumes: `trader.research.validation.benchmark_metrics`, `_series_metrics` (import `benchmark_metrics` only; recompute raw SPY via `benchmark_metrics(spy_eq, None, ...)`).
- Produces: `BenchmarkEvidence`, `vol_matched_benchmark(holdout_equity, spy_closes, *, account_equity)`, `time_in_market(trips, *, calendar_name, start, end)`.

- [ ] **Step 1: Write the failing tests**

```python
def equity_curve(session_returns, start='2024-02-01', equity=100_000.0) -> pd.Series:
    days = XNYS.sessions_in_range(start, '2024-12-31')[:len(session_returns)]
    stamps = [XNYS.session_close(s) for s in days]
    values = equity * np.cumprod(1 + np.asarray(session_returns))
    return pd.Series(values, index=pd.DatetimeIndex(stamps).tz_convert('UTC'))


def spy_over(session_returns, start='2024-02-01') -> pd.Series:
    days = [s.date() for s in XNYS.sessions_in_range(start, '2024-12-31')[:len(session_returns)]]
    return pd.Series(400.0 * np.cumprod(1 + np.asarray(session_returns)), index=pd.Index(days))


def test_vol_matching_scales_spy_to_the_strategy_volatility():
    strat = equity_curve([0.01, -0.01, 0.01, -0.01, 0.01])
    spy = spy_over([0.02, -0.02, 0.02, -0.02, 0.02])
    out = mc.vol_matched_benchmark(strat, spy, account_equity=100_000.0)
    assert out.scale == pytest.approx(0.5, rel=1e-6)
    # scaled SPY moves ±1% like the strategy: drawdown ratio ≈ 1
    assert out.ratio.value == pytest.approx(1.0, rel=1e-6)
    assert out.raw_spy_drawdown == pytest.approx(-0.02, rel=1e-6)


def test_zero_strategy_volatility_is_missing():
    strat = equity_curve([0.0, 0.0, 0.0, 0.0])
    out = mc.vol_matched_benchmark(strat, spy_over([0.01, -0.01, 0.01, -0.01]),
                                   account_equity=100_000.0)
    assert out.ratio.value is None and 'zero volatility' in out.ratio.cause


def test_spy_without_a_drawdown_is_missing():
    strat = equity_curve([0.01, -0.01, 0.01, -0.01])
    out = mc.vol_matched_benchmark(strat, spy_over([0.01, 0.01, 0.01, 0.01]),
                                   account_equity=100_000.0)
    assert out.ratio.value is None and 'no drawdown' in out.ratio.cause


def test_fewer_than_two_joined_sessions_is_missing():
    strat = equity_curve([0.01])
    out = mc.vol_matched_benchmark(strat, spy_over([0.01, 0.02], start='2024-06-03'),
                                   account_equity=100_000.0)
    assert out.ratio.value is None and 'session' in out.ratio.cause


def test_time_in_market_merges_overlapping_trips_across_conids():
    day = XNYS.sessions_in_range('2024-02-01', '2024-02-10')[0]
    opens = XNYS.session_open(day)
    def rt(conid, start_min, end_min):
        return RoundTrip(conid=conid, open_time=(opens + pd.Timedelta(minutes=start_min)).to_pydatetime(),
                         close_time=(opens + pd.Timedelta(minutes=end_min)).to_pydatetime(),
                         quantity=1, entry_price=100, exit_price=100, pnl=0)
    share = mc.time_in_market([rt(1, 0, 60), rt(2, 30, 90)], calendar_name='XNYS',
                              start=day.date(), end=day.date())
    assert share == pytest.approx(90 / 390)


def test_time_in_market_is_none_without_trips():
    day = XNYS.sessions_in_range('2024-02-01', '2024-02-10')[0].date()
    assert mc.time_in_market([], calendar_name='XNYS', start=day, end=day) is None
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implementation (append)**

```python
import numpy as np
import exchange_calendars as xcals

from trader.research.validation import benchmark_metrics


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
    strat_eq = account_equity * (1 + joined['strat']).cumprod()
    bench_eq = account_equity * (1 + scale * joined['spy']).cumprod()
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
    spans = sorted((pd.Timestamp(rt.open_time), pd.Timestamp(rt.close_time)) for rt in trips)
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
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `feat(research): vol-matched SPY benchmark and time in market`

---

### Task 5: SPY loading, qualification, manifest and family identity

**Files:**
- Modify: `trader/research/evaluation_data.py`
- Modify: `trader/research/evaluation.py` (`_family` only, in this task)
- Modify: `tests/research/evaluation_fixtures.py`
- Test: `tests/research/test_evaluation_data.py` (create if absent), `tests/research/test_evaluation.py`

**Interfaces:**
- Produces: `evaluation_data.load_benchmark_closes(history_db: str, spec) -> pd.Series` (index: `dt.date`, values: float closes; covers ≥ `SPY_LOOKBACK_SESSIONS` sessions before `spec.period_start` through `spec.period_end`); `qualify_dataset(bars, spec, benchmark_closes=None)` appends a `DatasetFile(path='tick_data/756733/1 day', ...)` entry when given.
- `_family` gains in `validation_protocol`: `benchmark_conid`, `regime_definition`, `liquidity` (exact dicts below).

- [ ] **Step 1: Failing tests**

In `tests/research/evaluation_fixtures.py`, first add the fixture writer and call it from `write_trend_bars` (single choke point — every existing evaluation test then has SPY data):

```python
SPY_CONID = 756733

def write_benchmark_bars(duckdb_path: str, *, start='2023-01-03', end=PERIOD[1],
                         drift: float = 0.0002) -> None:
    """SPY daily closes over real XNYS sessions, enough lookback for regimes."""
    calendar = xcals.get_calendar('XNYS')
    sessions = calendar.sessions_in_range(start, end)
    index = pd.DatetimeIndex([pd.Timestamp(s.date(), tz='UTC') for s in sessions])
    price = 400.0 * (1 + drift) ** np.arange(len(index))
    frame = pd.DataFrame({'open': price, 'high': price, 'low': price, 'close': price,
                          'volume': 1_000_000.0, 'bar_size': '1 day'}, index=index)
    frame.index.name = 'date'
    DuckDBDataStore(duckdb_path).write(str(SPY_CONID), frame)
```

and at the end of `write_trend_bars(...)` add `write_benchmark_bars(duckdb_path, end=end)`.

New tests (put loader tests in `tests/research/test_evaluation_data.py`):

```python
import datetime as dt
import pytest

from tests.research.evaluation_fixtures import build_spec_file, write_benchmark_bars, write_universe
from trader.research.evaluation_data import EvaluationDataError, load_benchmark_closes


def _spec(tmp_path, tmp_duckdb_path):
    # same loading dance as tests/research/test_evaluation.py::_spec
    ...


def test_benchmark_closes_cover_the_lookback(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path)
    spec = _spec(tmp_path, tmp_duckdb_path)
    closes = load_benchmark_closes(tmp_duckdb_path, spec)
    before = [d for d in closes.index if d < spec.period_start]
    assert len(before) >= 220
    assert all(isinstance(d, dt.date) for d in closes.index)


def test_missing_spy_names_the_download_command(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)   # no benchmark bars
    spec = _spec(tmp_path, tmp_duckdb_path)
    with pytest.raises(EvaluationDataError, match=r'mmr data download SPY --bar-size "1 day"'):
        load_benchmark_closes(tmp_duckdb_path, spec)


def test_short_spy_history_names_the_download_command(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_benchmark_bars(tmp_duckdb_path, start='2024-01-02')   # no lookback
    spec = _spec(tmp_path, tmp_duckdb_path)
    with pytest.raises(EvaluationDataError, match='220'):
        load_benchmark_closes(tmp_duckdb_path, spec)
```

Family-identity tests (append to `tests/research/test_evaluation.py`, reusing its `workspace` fixture and `_family_id` helper):

```python
def test_new_spy_data_is_a_new_family(workspace):
    # manifest digest covers the SPY file: adding data changes the family
    _, db_path, _, _ = workspace
    write_trend_bars(db_path, drift=0.0006)
    first = ...   # run evaluate twice with extended SPY end dates and compare family ids
                  # (cheaper: call qualify_dataset directly with two different
                  #  benchmark_closes series and compare manifest digests)


def test_regime_definition_is_part_of_the_family(workspace):
    before = _family_id(workspace)
    import trader.research.market_context as mcx
    # _family must read the constants via market_context so monkeypatching shows up
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcx, 'TREND_SMA_SESSIONS', 150)
        assert _family_id(workspace) != before
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implementation**

`evaluation_data.py`:

```python
import exchange_calendars as xcals

from trader.research.market_context import BENCHMARK_CONID, SPY_LOOKBACK_SESSIONS


def load_benchmark_closes(history_db: str, spec) -> pd.Series:
    """SPY daily closes from SPY_LOOKBACK_SESSIONS sessions before the period
    start through the period end. Missing or short history stops the run with
    the exact download command."""
    calendar = xcals.get_calendar(spec.calendar)
    sessions_before = calendar.sessions_in_range('2000-01-01', str(spec.period_start))
    if len(sessions_before) <= SPY_LOOKBACK_SESSIONS:
        raise EvaluationDataError(f'period start {spec.period_start} is too early for the calendar')
    required_start = sessions_before[-(SPY_LOOKBACK_SESSIONS + 1)].date()
    days_needed = (dt.date.today() - required_start).days + 5
    download = f'mmr data download SPY --bar-size "1 day" --days {days_needed}'
    tickdata = TickStorage(history_db).get_tickdata(BarSize.parse_str('1 day'))
    raw = tickdata.read(BENCHMARK_CONID, date_range=DateRange(
        start=_utc_midnight(required_start), end=_day_end_utc(spec.period_end)))
    if raw is None or len(raw) == 0:
        raise EvaluationDataError(
            f'no SPY (conid {BENCHMARK_CONID}) daily bars in the history DB; run: {download}')
    frame = normalize_historical(raw).dropna(subset=['close'])
    closes = frame['close']
    closes.index = pd.Index([pd.Timestamp(ts).date() for ts in frame.index])
    closes = closes[~closes.index.duplicated(keep='last')].sort_index()
    before = closes[closes.index < spec.period_start]
    if len(before) < SPY_LOOKBACK_SESSIONS:
        raise EvaluationDataError(
            f'SPY daily history starts too late: need {SPY_LOOKBACK_SESSIONS} sessions before '
            f'{spec.period_start} (from {required_start}), have {len(before)}; run: {download}')
    return closes
```

(`_day_end_utc` = the existing `_utc_midnight(period_end) + 1 day`; add a tiny helper. Reuse the module's existing imports — `TickStorage`, `BarSize`, `DateRange`, `normalize_historical` are already there.)

`qualify_dataset(bars, spec, benchmark_closes: Optional[pd.Series] = None)`: when given, qualify the SPY frame separately — build a one-column daily frame and run `DatasetQualifier().qualify(DatasetQualificationRequest(bars={BENCHMARK_CONID: spy_frame}, bar_interval='1 day', calendar_name=spec.calendar, expected_start=<first close date>, expected_end=spec.period_end))`; failed required findings raise `EvaluationDataError` prefixed `benchmark dataset failed qualification: `. Append to `files`:

```python
DatasetFile(path=f'tick_data/{BENCHMARK_CONID}/1 day',
            sha256=hashlib.sha256(benchmark_closes.to_csv().encode('utf-8')).hexdigest(),
            rows=len(benchmark_closes))
```

**Note:** the SPY daily frame must be indexed by UTC-midnight timestamps of session dates for the qualifier's daily mode (`_expected_labels` labels daily bars at session-date midnight UTC). Build it from the raw frame before collapsing to the date-indexed closes series, or reconstruct: `pd.DataFrame({'open': v, 'high': v, 'low': v, 'close': v, 'volume': 0.0}, index=pd.DatetimeIndex([pd.Timestamp(d, tz='UTC') for d in closes.index]))` — if the qualifier rejects zero volume (check `_check_corrupt`), carry the real OHLCV frame through instead of the closes-only series: make `load_benchmark_closes` return `(closes, spy_frame)` and pass the frame to `qualify_dataset`. Decide by reading `_check_corrupt` first; keep whichever shape passes real data.

`evaluation.py::_family` — add to `validation_protocol` (import the constants from `market_context` as a module so tests can monkeypatch: `from trader.research import market_context as mcx`, then read `mcx.TREND_SMA_SESSIONS` etc. at call time):

```python
'benchmark_conid': mcx.BENCHMARK_CONID,
'regime_definition': {
    'taxonomy_digest': regime_taxonomy_digest(),
    'trend_sma_sessions': mcx.TREND_SMA_SESSIONS,
    'volatility_sessions': mcx.VOLATILITY_SESSIONS,
    'hold_sessions': mcx.REGIME_HOLD_SESSIONS,
    'transition_window_sessions': mcx.TRANSITION_WINDOW_SESSIONS,
    'min_samples': mcx.REGIME_MIN_SAMPLES,
    'labelling': 'entry_session'},
'liquidity': {'adv_sessions': mcx.LIQUIDITY_ADV_SESSIONS,
              'max_adv_share': mcx.LIQUIDITY_MAX_ADV_SHARE},
```

- [ ] **Step 4: Run the new tests AND the whole existing evaluation suite** (`tests/research/`, `tests/test_research_evaluate_cli.py`): the `_family` change breaks `test_a_comment_in_the_costs_config_keeps_the_family`-style identity expectations only if they hash protocol contents — they compare ids consistently, so they must still pass unchanged. Fix anything that assumed the old protocol keys.

- [ ] **Step 5: Commit** — `feat(research): SPY benchmark data is a sealed, identity-bearing evaluation input`

---

### Task 6: holdout keyed on strategy + window

**Files:**
- Modify: `trader/research/experiment_registry.py`
- Modify: `trader/research/evaluation.py` (pre-run refusal)
- Test: `tests/research/test_experiment_registry.py`, `tests/research/test_evaluation.py`

**Interfaces:**
- Produces: `ExperimentRegistry.opened_holdout_windows(strategy_path: str, class_name: str) -> list[dict]` with keys `artifact_id`, `family_id`, `start: dt.date`, `end: dt.date` (folds without parseable `start`/`end` are skipped); `open_holdout` raises `HoldoutAlreadyOpened` on overlap with another opened holdout of the same strategy+class; `evaluation._refuse_if_strategy_holdout_overlaps(registry, spec, plan)`.

- [ ] **Step 1: Failing tests** (`tests/research/test_experiment_registry.py` — follow its existing family/trial builder style; the essential cases:)

```python
def test_opened_holdout_windows_lists_only_parseable_opened_holdouts(registry, ...):
    # family A: holdout fold {'kind': 'holdout', 'start': '2025-01-02', 'end': '2025-05-30'}, opened
    # family B (same strategy/class): never opened -> not listed
    # family C (same strategy/class): opened, fold {'kind': 'holdout', 'test': '2025'} -> skipped
    # family D (other class): opened -> not listed
    windows = registry.opened_holdout_windows('strategies/orb.py', 'OpeningRangeBreakout')
    assert [w['start'] for w in windows] == [dt.date(2025, 1, 2)]


def test_open_holdout_refuses_an_overlapping_strategy_window(registry, ...):
    # family A opened over 2025-01-02..2025-05-30; family E same strategy/class,
    # holdout fold 2025-05-01..2025-08-29 (overlap), sealed artifact
    with pytest.raises(HoldoutAlreadyOpened, match='2025-05-30'):
        registry.open_holdout(artifact_e, opened_at=NOW, passed=True)


def test_open_holdout_allows_a_later_window(registry, ...):
    # family F same strategy/class, holdout 2025-06-02..2025-09-30 (after A) -> opens fine


def test_absolute_and_relative_strategy_paths_are_one_strategy(registry, ...):
    # family A stored with '/abs/repo/strategies/orb.py', query with 'strategies/orb.py'
```

And in `tests/research/test_evaluation.py`:

```python
@pytest.mark.timeout(240)
def test_same_strategy_overlapping_holdout_is_refused_before_any_trial(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace)
    evaluate(spec, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())
    other = _spec(workspace, params={'ENTRY_MINUTE': 600, 'EXIT_MINUTE': 690})  # new family
    registry = ExperimentRegistry(db)
    trials_before = len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay'))

    with pytest.raises(EvaluationError, match='already opened a holdout'):
        evaluate(other, research_db=db, paths=paths, now=_now, ruleset=_holdout_ruleset())

    # refused BEFORE any trial ran, and no second holdout row exists
    assert len(registry.strategy_trials('strategies/time_of_day.py', 'TimeOfDay')) == trials_before
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implementation**

Registry — a private query usable both from the public method and inside the `open_holdout` transaction:

```python
def _opened_windows_tx(self, conn, strategy_path: str, class_name: str) -> list[dict]:
    target = normalize_strategy_path(strategy_path)
    rows = conn.execute(
        "SELECT f.family_id, f.strategy_path, h.artifact_id, v.spec "
        "FROM experiment_families f "
        "JOIN strategy_artifacts a ON a.family_id = f.family_id "
        "JOIN holdout_access_log h ON h.artifact_id = a.artifact_id "
        "JOIN validation_folds v ON v.family_id = f.family_id "
        "WHERE f.class_name = ? AND v.kind = 'holdout'", [class_name]).fetchall()
    windows = []
    for family_id, path, artifact_id, spec_json in rows:
        if normalize_strategy_path(path) != target:
            continue
        fold = json.loads(spec_json)
        try:
            start = dt.date.fromisoformat(str(fold['start']))
            end = dt.date.fromisoformat(str(fold['end']))
        except (KeyError, ValueError):
            continue   # legacy/test fold without parseable dates
        windows.append({'artifact_id': artifact_id, 'family_id': family_id,
                        'start': start, 'end': end})
    return windows


def opened_holdout_windows(self, strategy_path: str, class_name: str) -> list[dict]:
    return self._db.transaction(
        lambda conn: self._opened_windows_tx(conn, strategy_path, class_name))
```

Inside `open_holdout`'s `_tx`, after the existing per-artifact write-once check and before the INSERT: look up the artifact's family (`strategy_path`, `class_name`) and its own holdout fold window; when parseable, compare against `self._opened_windows_tx(conn, ...)` entries from **other** artifacts; overlap (`start_a <= end_b and start_b <= end_a`) raises:

```python
raise HoldoutAlreadyOpened(
    f"strategy {path} class {class_name} already opened a holdout over "
    f"{w['start']}–{w['end']} (artifact {w['artifact_id']}); "
    f"the next holdout must start after {w['end']}")
```

(`import datetime as dt` at the top of the registry module.)

Evaluation — after `_refuse_if_holdout_opened(registry, family_id)` add:

```python
_refuse_if_strategy_holdout_overlaps(registry, spec, plan)
```

```python
def _refuse_if_strategy_holdout_overlaps(registry: ExperimentRegistry, spec: EvaluationSpec,
                                         plan: ValidationPlan) -> None:
    start = pd.Timestamp(plan.holdout.start).date()
    end = pd.Timestamp(plan.holdout.end).date()
    for w in registry.opened_holdout_windows(spec.strategy_path, spec.class_name):
        if start <= w['end'] and w['start'] <= end:
            raise EvaluationError(
                f"strategy {spec.strategy_path} class {spec.class_name} already opened a "
                f"holdout over {w['start']}–{w['end']} (artifact {w['artifact_id']}); "
                f"the next holdout must start after {w['end']}")
```

**Placement note:** the pre-check must run before `_run_point` is first called. Keeping it after `create_family` is fine (a family row is cheap and idempotent); the requirement is "before any trial".

- [ ] **Step 4: Run the registry + evaluation + automation fixture tests** — `tests/research/`, `tests/automation/test_paper_activation.py`, `tests/automation/test_bundle_finder.py` (their fixtures create same-strategy opened holdouts with `{'kind': 'holdout', 'test': ...}` folds, which the guard must skip, so they stay green).

- [ ] **Step 5: Commit** — `feat(research): one holdout per strategy and window, checked in the open transaction`

---

### Task 7: wire market context into the evaluation and the report

**Files:**
- Modify: `trader/research/evaluation.py`
- Modify: `trader/research/evaluation_report.py`
- Test: `tests/research/test_evaluation.py`, `tests/research/test_failed_holdout.py` (adjust), `tests/test_research_evaluate_cli.py` (adjust)

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: `EligibilityEvidence` now carries real values for `order_within_envelope`, `eligible_regime_positive_fraction`, `worst_eligible_regime_loss`, `regime_transitions_stable` (pre-holdout) and `benchmark_drawdown_ratio`, `benchmark_return`, `benchmark_downside_deviation`, `benchmark_recovery_time`, `strategy_time_in_market`, `capacity_estimate` (holdout stage). `write_evaluation_report(..., market_context: Optional[dict] = None, missing_causes: Optional[dict] = None)`.

- [ ] **Step 1: Failing tests** (adjust + add in `tests/research/test_evaluation.py`)

The synthetic fixture (steady drift, one regime, >30 trips per bucket, deep liquidity) now computes three of the four pre-holdout Phase B rules; only the transition rule stays missing (no regime change):

```python
# replace the old PHASE_B_PRE_HOLDOUT assertion in test_phase_a_stops_before_the_holdout:
    assert set(result.missing_rules) == {'regime_transition_stability'}
    report = json.loads(result.report_path.with_suffix('.json').read_text())
    assert report['evidence']['order_within_envelope'] is True
    assert report['evidence']['eligible_regime_positive_fraction'] == 1.0
    assert report['missing_causes']['regime_transition_stability'] == (
        'no regime change in the walk-forward sessions')
    assert report['market_context']['liquidity']['rows'][0]['floor_median'] > 0
    assert report['market_context']['regimes']['n_changes'] == 0


@pytest.mark.timeout(240)
def test_an_oversized_order_fails_the_liquidity_rule(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    spec = _spec(workspace, sizing={'order_notional': 10_000_000, 'account_equity': 100_000_000})
    result = evaluate(spec, research_db=db, paths=paths, now=_now)
    assert 'liquidity_capacity_envelope' in result.failed_rules


@pytest.mark.timeout(240)
def test_the_holdout_records_the_benchmark_evidence(workspace):
    _, db_path, db, paths = workspace
    write_trend_bars(db_path, drift=0.0006)
    result = evaluate(_spec(workspace), research_db=db, paths=paths, now=_now,
                      ruleset=_holdout_ruleset())
    report = json.loads(result.report_path.with_suffix('.json').read_text())
    ev = report['evidence']
    # synthetic SPY drifts up with ~zero vol -> the ratio is missing WITH a cause
    assert (ev['benchmark_drawdown_ratio'] is not None
            or 'benchmark_relative_drawdown' in report['missing_causes'])
    assert 0 <= ev['strategy_time_in_market'] <= 1
    assert report['market_context']['benchmark']['raw_spy_return'] is not None
```

(The fixture SPY has near-zero vol: `b_std` may be ~0 → cause `'SPY session returns have zero volatility'`. For a deterministic ratio, give `write_benchmark_bars` alternating ±0.5% returns instead of pure drift — do that in the fixture now: `price[i] = price[i-1] * (1 + (0.005 if i % 2 else -0.005))` after the first 200 sessions, keeping trend positive via a base drift. Then assert `ev['benchmark_drawdown_ratio']` is a float. The implementer picks whichever fixture shape makes the ratio deterministic and asserts exactly.)

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implementation**

In `evaluate(...)` after `load_bars`:

```python
benchmark_closes = load_benchmark_closes(paths.history_db, spec)   # EvaluationDataError if absent
manifest_digest = DatasetManifestRepository(research_db).seal(
    qualify_dataset(bars, spec, benchmark_closes=benchmark_closes), sealed_at=now())
```

After `main`/`neighbours`, build the context (wrap `MarketContextError` into `EvaluationError`):

```python
sessions = _period_session_dates(spec)     # XNYS sessions period_start..period_end, as dates
labels = mcx.regime_labels(benchmark_closes, sessions)
walk_trips = mcx.annotate_regimes(_round_trips(main.outcomes[1.0]), labels)
regimes = mcx.regime_evidence(walk_trips, labels.loc[[d for d in sessions if d < holdout_start]])
liquidity = mcx.liquidity_envelope(bars, order_notional=spec.order_notional, before=holdout_start)
```

where `holdout_start = pd.Timestamp(plan.holdout.start).date()`. Thread a `missing_causes: dict[str, str]` map: rule code → cause, filled from each `Measured.cause` (`liquidity_capacity_envelope`, `regime_positive_expectancy_fraction`, `regime_loss_tolerance`, `regime_transition_stability`, `benchmark_relative_drawdown`).

Extend `_walk_forward_evidence(...)` signature with `regimes: RegimeEvidence, liquidity: LiquidityEnvelope` and set:

```python
order_within_envelope=liquidity.within.value,
capacity_estimate=liquidity.capacity_estimate,
eligible_regime_positive_fraction=regimes.positive_fraction.value,
worst_eligible_regime_loss=regimes.worst_loss.value,
regime_transitions_stable=regimes.transitions_stable.value,
```

In the holdout branch, after the determinism replace:

```python
holdout_trips = build_round_trips(first.trades)
tim = mcx.time_in_market(holdout_trips, calendar_name=spec.calendar,
                         start=holdout_start, end=pd.Timestamp(plan.holdout.end).date())
bench = mcx.vol_matched_benchmark(first.equity_series(),
                                  benchmark_closes[benchmark_closes.index >= holdout_start],
                                  account_equity=spec.account_equity)
evidence = replace(evidence, benchmark_drawdown_ratio=bench.ratio.value,
                   benchmark_return=bench.benchmark_return,
                   benchmark_downside_deviation=bench.benchmark_downside_deviation,
                   benchmark_recovery_time=bench.benchmark_recovery_time,
                   strategy_time_in_market=tim)
if bench.ratio.cause:
    missing_causes['benchmark_relative_drawdown'] = bench.ratio.cause
```

Build `market_context` dict for the report (regime table rows via `dataclasses.asdict`-style dicts, `n_changes`, transition group pnl/trades, liquidity rows, benchmark numbers incl. `scale`, `raw_spy_return`, `raw_spy_drawdown`) and pass `market_context=...`, `missing_causes=...` through `_finish` into `write_evaluation_report`. Report changes: add both to the JSON top level (default `{}`), and two markdown sections:

```
## Market context
### Regimes            (| Regime | Trades | Net P&L | Share | Adequate |, then changes + transition-group line)
### Liquidity          (| Conid | Floor median $ volume | Date | Order share |)
### Benchmark          (scale k, ratio, scaled return/downside/recovery, raw SPY return/drawdown, time in market)
## Missing evidence causes   (| Rule | Cause |; omit when empty)
```

`_period_session_dates`:

```python
def _period_session_dates(spec) -> list[dt.date]:
    cal = xcals.get_calendar(spec.calendar)
    return [s.date() for s in cal.sessions_in_range(str(spec.period_start), str(spec.period_end))]
```

- [ ] **Step 4: Run the full research + CLI + automation-fixture suites**; fix the knock-on asserts (`tests/research/test_failed_holdout.py`, `tests/test_research_evaluate_cli.py` expect the old missing set). The fixture-driven suites (`test_attest_export`, `test_paper_activation`, `test_bundle_finder`) go through `evaluate_synthetic` and must stay green.

- [ ] **Step 5: Commit** — `feat(research): compute phase B evidence inside research evaluate`

---

### Task 8: live order size vs attested notional

**Files:**
- Modify: `trader/automation/session_risk.py:362-380` (the `is_entry and entry_price is not None` block)
- Test: `tests/automation/test_session_risk.py`

**Interfaces:**
- Consumes: `VerifiedArtifact.attested_strategy: Optional[AttestedStrategy]` (`order_notional: Optional[float]`), `market_context.LIVE_NOTIONAL_TOLERANCE`.
- Produces: reason code `ORDER_EXCEEDS_ATTESTED_NOTIONAL`.

- [ ] **Step 1: Failing tests** (append to `tests/automation/test_session_risk.py`; extend `make_artifact` so EXISTING tests keep passing)

In `make_artifact`, add to `base`:

```python
from trader.automation.strategy_binding import AttestedStrategy

        attested_strategy=AttestedStrategy(
            strategy_path="strategies/orb.py", class_name="OpeningRangeBreakout",
            source_digest="src-1", parameters={}, instruments=frozenset({str(CONID)}),
            bar_size="1 min", order_notional=1_000_000.0),
```

(1,000,000 keeps every existing quantity below the cap, so no legacy test changes behaviour.)

New tests:

```python
def _attested(notional):
    return make_artifact(attested_strategy=AttestedStrategy(
        strategy_path="strategies/orb.py", class_name="OpeningRangeBreakout",
        source_digest="src-1", parameters={}, instruments=frozenset({str(CONID)}),
        bar_size="1 min", order_notional=notional))


def test_an_entry_over_the_attested_notional_is_refused():
    decision = SessionRiskController().evaluate(
        make_intent(requested_quantity=Decimal("11")),            # $1,100 vs $1,000 * 1.05
        _attested(1_000.0), make_approval(quote=make_quote(price=100.0)),
        make_session(), make_allocation())
    assert "ORDER_EXCEEDS_ATTESTED_NOTIONAL" in decision.reason_codes


def test_an_entry_within_five_percent_of_the_notional_passes():
    decision = SessionRiskController().evaluate(
        make_intent(requested_quantity=Decimal("10")),            # $1,000 == attested
        _attested(1_000.0), make_approval(quote=make_quote(price=100.0)),
        make_session(), make_allocation())
    assert "ORDER_EXCEEDS_ATTESTED_NOTIONAL" not in decision.reason_codes


def test_a_missing_attested_notional_refuses_the_entry():
    decision = SessionRiskController().evaluate(
        make_intent(), _attested(None), make_approval(quote=make_quote(price=100.0)),
        make_session(), make_allocation())
    assert "ORDER_EXCEEDS_ATTESTED_NOTIONAL" in decision.reason_codes


def test_a_sell_is_never_checked_against_the_notional():
    decision = SessionRiskController().evaluate(
        make_intent(side="SELL", requested_quantity=Decimal("100000")),
        _attested(1_000.0),
        make_approval(quantity=100000.0, quote=make_quote(price=100.0)),
        make_session(), make_allocation())
    assert "ORDER_EXCEEDS_ATTESTED_NOTIONAL" not in decision.reason_codes
```

(Match the file's actual builder names — `make_session` / `make_approval` / `make_allocation` may take arguments; copy the call shapes used by the neighbouring `POSITION_PCT` tests. The SELL test needs a held position: reuse the existing sell-path builder.)

- [ ] **Step 2: Run, expect FAIL** (new tests only; legacy stay green).

- [ ] **Step 3: Implementation** — in `session_risk.py`, inside the `if is_entry and entry_price is not None and equity > 0 and qty > 0:` block, right after the `POSITION_PCT` append:

```python
from trader.research.market_context import LIVE_NOTIONAL_TOLERANCE   # module top

            # Attested-size ceiling: the research evidence priced THIS notional.
            attested = artifact.attested_strategy
            attested_notional = None if attested is None else attested.order_notional
            if attested_notional is None or order_notional > (
                    attested_notional * (1.0 + LIVE_NOTIONAL_TOLERANCE)):
                reasons.append("ORDER_EXCEEDS_ATTESTED_NOTIONAL")
```

(`order_notional = float(qty) * entry_price` already exists two lines above.)

- [ ] **Step 4: Run** `tests/automation/test_session_risk.py` and the automation suites that drive `SessionRiskController` end-to-end (`test_production_evidence.py`, `test_protective_order_saga.py`, `test_automated_command_boundary.py`); where their artifact builders lack `attested_strategy`, give them the same 1,000,000 default.

- [ ] **Step 5: Commit** — `feat(automation): refuse automated entries larger than the attested order notional`

---

### Task 9: docs, changelog and the full suite

**Files:**
- Modify: `CLAUDE.md:118` (the "Research evaluation (paper evidence)" paragraph)
- Modify: `docs/OPERATIONAL_STATE.md:50-56, 275`
- Modify: `docs/PAPER_AUTOMATION_SETUP.md:64-67` (+ a SPY download step in "Step-by-step")
- Modify: `CHANGELOG.md` (Unreleased)

- [ ] **Step 1: CLAUDE.md** — in the paragraph at line 118, replace the sentence `Phase A leaves liquidity, benchmark and regime evidence missing, so every run stops at stage `pre_holdout` (state `CANDIDATE`) and nothing is eligible yet.` with:

> Phase B computes the remaining evidence from SPY daily bars (conid 756733; `trader/research/market_context.py`): per-session regime labels (200-session trend, 20-session volatility, frozen taxonomy), a vol-matched SPY benchmark over the holdout, and a liquidity envelope (order notional ≤ 1% of the lowest prior-close 20-session median dollar volume). SPY bars are a required input (the error names the `mmr data download SPY` command), are sealed into the dataset manifest, and the regime/liquidity definitions are part of the family identity. "Holdout opened once" is keyed on (strategy file, class, overlapping window) — a code or lock-file change no longer buys a second look at the same dates. Live dispatch refuses an automated entry more than 5% above the attested order notional (`ORDER_EXCEEDS_ATTESTED_NOTIONAL`).

- [ ] **Step 2: OPERATIONAL_STATE.md** — update the lines saying "Phase A leaves the liquidity, benchmark and regime evidence missing, so no strategy can be eligible until Phase B": state Phase B is implemented, eligibility now depends on the data, and the soak remains blocked by the automated-exit fix only. Keep the exit blocker text as-is.

- [ ] **Step 3: PAPER_AUTOMATION_SETUP.md** — replace the Phase A note (lines 64-67) with the same message, and add to the step-by-step, before `research evaluate`: download SPY daily bars (`mmr data download SPY --bar-size "1 day" --days 1100` — the evaluate error names the exact number when the history is short).

- [ ] **Step 4: CHANGELOG.md** — under `## [Unreleased]` / `### Added`:

```markdown
- **Phase B evidence**: `research evaluate` now computes the liquidity envelope, the vol-matched SPY benchmark and the regime values from SPY daily bars, so a strategy can reach `PAPER_ELIGIBLE` on real data. "Holdout opened once" is keyed on strategy + window across families. Automated entries larger than 105% of the attested order notional are refused (`ORDER_EXCEEDS_ATTESTED_NOTIONAL`).
```

- [ ] **Step 5: Full suite**

Run: `cd /Users/mudryy/private/mmr/.worktrees/paper-evidence-phase-b && PYTHONPATH=$PWD /Users/mudryy/private/mmr/.venv/bin/python -m pytest tests/ --timeout=30 --timeout-method=thread -q --ignore=tests/test_ibrx_async.py`
Expected: 0 failures (skips allowed). Fix anything red before committing.

- [ ] **Step 6: Commit** — `docs: describe phase B evidence, the strategy-window holdout key and the notional ceiling`
