"""P2 Task 5 -- leakage-safe validation protocol (design §8.2 / §8.3 / §8.4).

Folds (property-based over generated date ranges), cost stress (higher cost can
never improve net P&L), attribution over the frozen regime taxonomy with
adequate-sample flagging + extreme-event partition, and deterministic replay via
``trace_signature``. Fully offline: synthetic pandas/DuckDB storage + the real
XNYS calendar, no IB, no network.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import replace

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from trader.data.data_access import TickStorage
from trader.data.duckdb_store import DuckDBDataStore
from trader.data.universe import UniverseAccessor
from trader.objects import Action, BarSize
from trader.simulation.backtester import trace_signature
from trader.trading.strategy import Signal, Strategy, StrategyContext, StrategyState

from trader.research.attribution import (
    FROZEN_REGIMES,
    REGIME_LABELS,
    RoundTrip,
    attribute,
    build_round_trips,
    classify_regime,
    extreme_event_partition,
    regime_taxonomy_digest,
    volatility_band,
)
from trader.research.statistics import BootstrapCI
from trader.research.validation import (
    BenchmarkComparison,
    CostModel,
    Fold,
    ValidationError,
    ValidationPlan,
    ValidationResult,
    Window,
    benchmark_metrics,
    cost_stress,
    generate_walk_forward,
    run_window,
    validate_plan,
)

UTC = dt.timezone.utc
CAL = xcals.get_calendar("XNYS")
ALL_SESSIONS = list(CAL.sessions_in_range("2016-01-01", "2019-12-31"))


# --------------------------------------------------------------------------- #
# Walk-forward folds -- property-based
# --------------------------------------------------------------------------- #
@st.composite
def _plan_inputs(draw):
    n_folds = draw(st.integers(2, 5))
    embargo = draw(st.integers(1, 3))
    holdout = draw(st.integers(2, 10))
    n_seg = n_folds + 1
    base = draw(st.integers(embargo + 1, 25))
    remainder = draw(st.integers(0, n_seg - 1))
    length = base * n_seg + remainder + holdout
    max_start = len(ALL_SESSIONS) - length
    assume(max_start >= 0)
    start = draw(st.integers(0, max_start))
    sessions = ALL_SESSIONS[start:start + length]
    return sessions, n_folds, embargo, holdout


class TestWalkForwardProperties:
    @settings(deadline=None, max_examples=150)
    @given(_plan_inputs())
    def test_chronological_embargoed_holdout_last(self, inp):
        sessions, n_folds, embargo, holdout = inp
        plan = generate_walk_forward(list(sessions), n_folds=n_folds,
                                     embargo=embargo, holdout=holdout)
        validate_plan(plan)  # a generated plan must be structurally valid
        assert len(plan.folds) == n_folds
        idx_of = {pd.Timestamp(ts): i for i, ts in enumerate(sessions)}

        prev_test_end = None
        for i, f in enumerate(plan.folds):
            assert f.index == i
            # strictly chronological within a fold
            assert f.train_start <= f.train_end < f.test_start <= f.test_end
            # exactly `embargo` sessions between train_end and test_start
            gap = idx_of[pd.Timestamp(f.test_start)] - idx_of[pd.Timestamp(f.train_end)] - 1
            assert gap == embargo
            # forward-advancing, non-overlapping test windows
            if prev_test_end is not None:
                assert prev_test_end < f.test_start
            prev_test_end = f.test_end

        # single final holdout strictly after every fold
        assert plan.holdout.start > max(f.test_end for f in plan.folds)
        assert pd.Timestamp(plan.holdout.end) == pd.Timestamp(sessions[-1])

    @settings(deadline=None, max_examples=50)
    @given(_plan_inputs())
    def test_train_expands_and_holdout_untouched(self, inp):
        sessions, n_folds, embargo, holdout = inp
        plan = generate_walk_forward(list(sessions), n_folds=n_folds,
                                     embargo=embargo, holdout=holdout)
        # expanding training: every fold trains from the very first session
        assert all(pd.Timestamp(f.train_start) == pd.Timestamp(sessions[0])
                   for f in plan.folds)
        # no fold test window intrudes into the holdout block
        for f in plan.folds:
            assert f.test_end < plan.holdout.start


class TestWalkForwardGenerationGuards:
    def test_insufficient_sessions_for_embargo_raises(self):
        with pytest.raises(ValueError):
            generate_walk_forward(list(ALL_SESSIONS[:10]), n_folds=4, embargo=3,
                                  holdout=2)

    def test_holdout_consuming_all_sessions_raises(self):
        with pytest.raises(ValueError):
            generate_walk_forward(list(ALL_SESSIONS[:20]), n_folds=2, embargo=1,
                                  holdout=20)

    def test_range_tuple_lands_on_real_sessions(self):
        plan = generate_walk_forward(("2018-01-01", "2018-12-31"), n_folds=3,
                                     embargo=2, holdout=10)
        # every boundary is an actual XNYS session
        session_set = {pd.Timestamp(s) for s in CAL.sessions_in_range("2018-01-01", "2018-12-31")}
        for f in plan.folds:
            for b in (f.train_start, f.train_end, f.test_start, f.test_end):
                assert pd.Timestamp(b) in session_set


# --------------------------------------------------------------------------- #
# validate_plan rejects shuffles / overlap / leakage / holdout-before-fold
# --------------------------------------------------------------------------- #
class TestValidatePlanRejections:
    def _valid(self):
        return generate_walk_forward(list(ALL_SESSIONS[:150]), n_folds=3,
                                     embargo=2, holdout=10)

    def test_accepts_valid_plan(self):
        validate_plan(self._valid())  # no raise

    def test_rejects_shuffled_folds(self):
        plan = self._valid()
        shuffled = ValidationPlan(training=plan.training,
                                  folds=tuple(reversed(plan.folds)),
                                  embargo=plan.embargo, holdout=plan.holdout)
        with pytest.raises(ValidationError):
            validate_plan(shuffled)

    def test_rejects_train_test_leak(self):
        plan = self._valid()
        f = plan.folds[0]
        leak = Fold(index=0, train_start=f.train_start, train_end=f.test_start,
                    test_start=f.test_start, test_end=f.test_end)  # train_end == test_start
        bad = ValidationPlan(training=plan.training,
                             folds=(leak,) + plan.folds[1:],
                             embargo=plan.embargo, holdout=plan.holdout)
        with pytest.raises(ValidationError):
            validate_plan(bad)

    def test_rejects_overlapping_test_windows(self):
        s = ALL_SESSIONS
        f0 = Fold(0, s[0], s[3], s[5], s[10])
        f1 = Fold(1, s[0], s[6], s[8], s[15])  # test_start s[8] < f0.test_end s[10]
        bad = ValidationPlan(training=Window(s[0], s[4]), folds=(f0, f1),
                             embargo=2, holdout=Window(s[20], s[25]))
        with pytest.raises(ValidationError):
            validate_plan(bad)

    def test_rejects_holdout_before_a_fold(self):
        plan = self._valid()
        early = Window(start=plan.folds[0].test_start, end=plan.holdout.end)
        bad = ValidationPlan(training=plan.training, folds=plan.folds,
                             embargo=plan.embargo, holdout=early)
        with pytest.raises(ValidationError):
            validate_plan(bad)

    def test_rejects_empty_folds(self):
        with pytest.raises(ValidationError):
            validate_plan(ValidationPlan(Window(1, 2), (), 1, Window(3, 4)))


# --------------------------------------------------------------------------- #
# Deterministic backtester adapter: cost stress + replay signature
# --------------------------------------------------------------------------- #
class OneRoundTrip(Strategy):
    """Buys once (2nd bar) and sells once (7th bar), fixed 10 shares. Cost-
    independent signals so higher cost only subtracts from net P&L."""

    BUY_AT = 2
    SELL_AT = 7

    def on_prices(self, prices):
        n = len(prices)
        conid = self.conids[0] if self.conids else 0
        if n == self.BUY_AT:
            return Signal(source_name="rt", action=Action.BUY, probability=0.9,
                          risk=0.1, conid=conid, quantity=10)
        if n == self.SELL_AT:
            return Signal(source_name="rt", action=Action.SELL, probability=0.9,
                          risk=0.1, conid=conid, quantity=10)
        return None


def _write_uptrend_bars(duckdb_path, conid=4391, n=12, start=100.0, step=2.0):
    store = DuckDBDataStore(duckdb_path)
    dates = pd.date_range("2024-01-02 09:30", periods=n, freq="1min", tz="UTC")
    close = np.array([start + step * i for i in range(n)], dtype=float)
    df = pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": [10_000.0] * n,
    }, index=dates)
    df.index.name = "date"
    store.write(str(conid), df)


def _install(strategy, duckdb_path, conids=(4391,)):
    storage = TickStorage(duckdb_path=duckdb_path)
    ua = UniverseAccessor.__new__(UniverseAccessor)
    ua.duckdb_path = duckdb_path
    ua.universe_library = "Universes"
    ctx = StrategyContext(
        name=strategy.__class__.__name__, bar_size=BarSize.Mins1,
        conids=list(conids), universe=None, historical_days_prior=0,
        paper_only=False, storage=storage, universe_accessor=ua,
        logger=logging.getLogger("test"))
    strategy.install(ctx)
    strategy.state = StrategyState.RUNNING
    return strategy


def _window():
    return Window(start=dt.datetime(2024, 1, 2, 9, 0, tzinfo=UTC),
                  end=dt.datetime(2024, 1, 2, 11, 0, tzinfo=UTC))


class TestCostStress:
    def test_higher_cost_never_improves_net_pnl(self, tmp_duckdb_path):
        _write_uptrend_bars(tmp_duckdb_path)
        storage = TickStorage(duckdb_path=tmp_duckdb_path)
        base = CostModel(slippage_bps=5.0, commission_per_share=0.01)

        def run_fn(m):
            return run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path),
                              [4391], storage=storage, window=_window(),
                              cost=base.scaled(m))

        res = cost_stress(run_fn, multipliers=(1.0, 1.5, 2.0))
        vals = [res[1.0], res[1.5], res[2.0]]
        assert res[1.0] > 0.0                              # profitable at baseline
        assert all(b <= a for a, b in zip(vals, vals[1:]))  # monotone non-increasing

    def test_cost_stress_uses_all_multipliers(self, tmp_duckdb_path):
        _write_uptrend_bars(tmp_duckdb_path)
        storage = TickStorage(duckdb_path=tmp_duckdb_path)

        def run_fn(m):
            return run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path),
                              [4391], storage=storage, window=_window(),
                              cost=CostModel(5.0, 0.01).scaled(m))

        res = cost_stress(run_fn, multipliers=(1.0, 2.0, 3.0))
        assert set(res.keys()) == {1.0, 2.0, 3.0}


class TestTraceSignature:
    def test_identical_runs_identical_signature(self, tmp_duckdb_path):
        _write_uptrend_bars(tmp_duckdb_path)
        storage = TickStorage(duckdb_path=tmp_duckdb_path)
        cost = CostModel(5.0, 0.01)
        r1 = run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path), [4391],
                        storage=storage, window=_window(), cost=cost)
        r2 = run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path), [4391],
                        storage=storage, window=_window(), cost=cost)
        assert trace_signature(r1) == trace_signature(r2)
        assert len(trace_signature(r1)) == 64

    def test_signature_changes_when_a_trade_differs(self, tmp_duckdb_path):
        _write_uptrend_bars(tmp_duckdb_path)
        storage = TickStorage(duckdb_path=tmp_duckdb_path)
        r1 = run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path), [4391],
                        storage=storage, window=_window(), cost=CostModel(5.0, 0.01))
        # different slippage -> different fill prices -> different trades
        r3 = run_window(lambda: _install(OneRoundTrip(), tmp_duckdb_path), [4391],
                        storage=storage, window=_window(), cost=CostModel(80.0, 0.01))
        assert r1.total_trades == r3.total_trades  # same signals, same trade count
        assert trace_signature(r1) != trace_signature(r3)


# --------------------------------------------------------------------------- #
# Benchmark comparison
# --------------------------------------------------------------------------- #
class TestBenchmarkMetrics:
    def test_both_series_reported_with_time_in_market(self):
        strat = pd.Series([100_000, 100_500, 100_200, 101_000, 100_800], dtype=float)
        bench = pd.Series([100_000, 100_300, 100_100, 100_050, 100_400], dtype=float)
        bc = benchmark_metrics(strat, bench, periods_per_year=252,
                               strategy_time_in_market=0.5)
        assert isinstance(bc, BenchmarkComparison)
        assert bc.benchmark is not None
        assert bc.strategy.time_in_market == 0.5
        assert bc.benchmark.time_in_market == 1.0
        assert bc.strategy.max_drawdown <= 0.0
        assert isinstance(bc.strategy.recovery_time, int)
        assert bc.drawdown_ratio is not None

    def test_none_benchmark_is_explicit_none(self):
        strat = pd.Series([100_000, 100_500, 100_200], dtype=float)
        bc = benchmark_metrics(strat, None, periods_per_year=252)
        assert bc.benchmark is None
        assert bc.drawdown_ratio is None
        assert bc.strategy.time_in_market is None  # not supplied -> not assumed


# --------------------------------------------------------------------------- #
# Attribution: frozen taxonomy, round trips, adequate-sample flagging, extremes
# --------------------------------------------------------------------------- #
class TestFrozenRegimeTaxonomy:
    def test_six_ordered_regimes(self):
        assert len(FROZEN_REGIMES) == 6
        assert REGIME_LABELS == (
            "bull_low_vol", "bull_normal_vol", "bull_high_vol",
            "bear_low_vol", "bear_normal_vol", "bear_high_vol")

    def test_taxonomy_digest_stable(self):
        assert regime_taxonomy_digest() == regime_taxonomy_digest()
        assert len(regime_taxonomy_digest()) == 64

    def test_classify_regime(self):
        assert classify_regime({"trend": 0.5, "volatility": 0.005}) == "bull_low_vol"
        assert classify_regime({"trend": 1.0, "volatility": 0.015}) == "bull_normal_vol"
        assert classify_regime({"trend": -0.5, "volatility": 0.03}) == "bear_high_vol"

    def test_volatility_band_thresholds(self):
        assert volatility_band(0.009) == "low"
        assert volatility_band(0.01) == "normal"
        assert volatility_band(0.019) == "normal"
        assert volatility_band(0.02) == "high"


def _rt_trades():
    return [
        {"timestamp": dt.datetime(2024, 1, 10), "conid": 1, "action": "BUY",
         "quantity": 10, "price": 100.0, "commission": 0.0},
        {"timestamp": dt.datetime(2024, 1, 20), "conid": 1, "action": "SELL",
         "quantity": 10, "price": 110.0, "commission": 0.0},
        {"timestamp": dt.datetime(2024, 2, 10), "conid": 2, "action": "BUY",
         "quantity": 10, "price": 50.0, "commission": 0.0},
        {"timestamp": dt.datetime(2024, 2, 20), "conid": 2, "action": "SELL",
         "quantity": 10, "price": 45.0, "commission": 0.0},
    ]


class TestBuildRoundTrips:
    def test_basic_pnl(self):
        rts = build_round_trips(_rt_trades())
        assert len(rts) == 2
        pnls = sorted(rt.pnl for rt in rts)
        assert pnls == pytest.approx([-50.0, 100.0])

    def test_subtracts_both_commissions(self):
        trades = [
            {"timestamp": dt.datetime(2024, 1, 1), "conid": 1, "action": "BUY",
             "quantity": 10, "price": 100.0, "commission": 1.0},
            {"timestamp": dt.datetime(2024, 1, 2), "conid": 1, "action": "SELL",
             "quantity": 10, "price": 110.0, "commission": 2.0},
        ]
        rt = build_round_trips(trades)[0]
        assert rt.pnl == pytest.approx(97.0)  # 100 gross - 1 buy - 2 sell

    def test_fifo_partial_close(self):
        trades = [
            {"timestamp": dt.datetime(2024, 1, 1), "conid": 1, "action": "BUY",
             "quantity": 10, "price": 100.0, "commission": 0.0},
            {"timestamp": dt.datetime(2024, 1, 2), "conid": 1, "action": "BUY",
             "quantity": 10, "price": 120.0, "commission": 0.0},
            {"timestamp": dt.datetime(2024, 1, 3), "conid": 1, "action": "SELL",
             "quantity": 15, "price": 130.0, "commission": 0.0},
        ]
        rts = build_round_trips(trades)
        assert len(rts) == 2
        assert rts[0].pnl == pytest.approx(300.0)  # first lot 10@100
        assert rts[1].pnl == pytest.approx(50.0)   # second lot 5@120

    def test_unmatched_buy_ignored(self):
        trades = [
            {"timestamp": dt.datetime(2024, 1, 1), "conid": 1, "action": "BUY",
             "quantity": 10, "price": 100.0, "commission": 0.0},
        ]
        assert build_round_trips(trades) == []


class TestAttribution:
    def test_month_attribution(self):
        tbl = attribute(build_round_trips(_rt_trades()), by="month", min_samples=1)
        assert tbl.bucket("2024-01").pnl == pytest.approx(100.0)
        assert tbl.bucket("2024-02").pnl == pytest.approx(-50.0)

    def test_instrument_attribution(self):
        tbl = attribute(build_round_trips(_rt_trades()), by="instrument", min_samples=1)
        assert tbl.bucket(1).pnl == pytest.approx(100.0)
        assert tbl.bucket(2).pnl == pytest.approx(-50.0)

    def test_insufficient_bucket_flagged_not_assumed_positive(self):
        rts = [RoundTrip(conid=1, open_time=dt.datetime(2024, 1, 1),
                         close_time=dt.datetime(2024, 1, 2), quantity=10,
                         entry_price=100.0, exit_price=110.0, pnl=100.0)]
        tbl = attribute(rts, by="instrument", min_samples=5)
        b = tbl.bucket(1)
        assert b.n_trades == 1
        assert b.adequate is False            # positive P&L but NOT assumed adequate
        assert tbl.adequate_buckets == ()
        assert tbl.positive_fraction_of_adequate is None  # explicitly "no evidence"

    def test_by_regime_requires_annotation(self):
        rts = build_round_trips(_rt_trades())  # regime=None
        with pytest.raises(ValueError):
            attribute(rts, by="regime")

    def test_by_regime_with_annotation(self):
        def annotate(rt):
            return replace(rt, regime="bull_low_vol", volatility_bucket="low")

        rts = build_round_trips(_rt_trades(), annotate=annotate)
        reg = attribute(rts, by="regime", min_samples=1)
        assert reg.bucket("bull_low_vol") is not None
        vol = attribute(rts, by="volatility", min_samples=1)
        assert vol.bucket("low") is not None

    def test_unknown_axis_raises(self):
        with pytest.raises(ValueError):
            attribute(build_round_trips(_rt_trades()), by="sector")


class TestExtremePartition:
    def test_flag_based_partition_reports_separately(self):
        rts = [
            RoundTrip(1, dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2), 10,
                      100.0, 110.0, 100.0, extreme=False),
            RoundTrip(2, dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2), 10,
                      100.0, 50.0, -500.0, extreme=True),
        ]
        normal, extreme = extreme_event_partition(rts)
        assert [rt.conid for rt in normal] == [1]
        assert [rt.conid for rt in extreme] == [2]
        # extreme trips are kept in the record, not dropped
        assert len(normal) + len(extreme) == len(rts)

    def test_predicate_partition(self):
        rts = [
            RoundTrip(1, dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2), 10,
                      100.0, 110.0, 100.0),
            RoundTrip(2, dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2), 10,
                      100.0, 60.0, -400.0),
        ]
        normal, extreme = extreme_event_partition(rts, is_extreme=lambda rt: rt.pnl < -100)
        assert [rt.conid for rt in extreme] == [2]
        assert [rt.conid for rt in normal] == [1]


# --------------------------------------------------------------------------- #
# ValidationResult digest
# --------------------------------------------------------------------------- #
def _make_result(trace="trace-abc"):
    month = attribute(build_round_trips(_rt_trades()), by="month", min_samples=1)
    return ValidationResult(
        trace_signature=trace,
        cost_stress={1.0: 100.0, 1.5: 90.0, 2.0: 80.0},
        mean_pnl_ci=BootstrapCI(low=1.0, point=2.0, high=3.0),
        sharpe_ci=None,
        deflated_sharpe=0.97,
        selection_adjusted_confidence=0.96,
        profit_factor=float("inf"),   # must not break the digest
        n_round_trips=200,
        n_instruments=8,
        fold_net_pnls=(1.0, 2.0, 3.0),
        month_attribution=month,
        instrument_attribution=None,
        regime_attribution=None,
        benchmark=benchmark_metrics(
            pd.Series([100_000.0, 100_500.0, 100_200.0]), None, periods_per_year=252),
        capacity_estimate=None)


class TestValidationResultDigest:
    def test_digest_stable_and_hex(self):
        a, b = _make_result(), _make_result()
        assert a.digest == b.digest
        assert len(a.digest) == 64

    def test_digest_sensitive_to_change(self):
        assert _make_result("trace-abc").digest != _make_result("trace-xyz").digest

    def test_infinite_profit_factor_does_not_break_digest(self):
        # inf must be neutralized before canonical JSON (which rejects non-finite)
        assert isinstance(_make_result().digest, str)
