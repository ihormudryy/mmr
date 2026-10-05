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


def _thin_after(full_sessions: int, thin_sessions: int, *, thin_dollars: float) -> pd.DataFrame:
    thin_start = XNYS.sessions_in_range('2024-02-01', '2024-12-31')[full_sessions]
    return pd.concat([
        intraday_frame(sessions=full_sessions, dollar_per_session=1_000_000),
        intraday_frame(sessions=thin_sessions, dollar_per_session=thin_dollars,
                       start=str(thin_start.date()))])


def test_the_floor_is_known_as_of_the_previous_close():
    # 20 sessions of 1M then 11 of 100k. The only window that includes the last
    # thin session is the unshifted one (median 100k). Shifted, the last window
    # ends one session earlier: 10 thin + 10 full, median (100k + 1M) / 2.
    frame = _thin_after(20, 11, thin_dollars=100_000)
    envelope = mc.liquidity_envelope({1001: frame}, order_notional=5_000, before=_before(frame))
    assert envelope.rows[0].floor_median == pytest.approx(550_000)
    assert envelope.within.value is True           # 1% of 550k = 5,500; of 100k it would be 1,000


def test_too_few_sessions_is_missing_with_a_cause():
    bars = {1001: intraday_frame(sessions=10, dollar_per_session=1_000_000)}
    envelope = mc.liquidity_envelope(bars, order_notional=100, before=_before(bars))
    assert envelope.within.value is None
    assert 'conid 1001' in envelope.within.cause


def test_sessions_on_or_after_the_holdout_are_ignored():
    frame = _thin_after(25, 15, thin_dollars=100_000)
    sessions = sorted({ts.date() for ts in frame.index.tz_convert('America/New_York')})
    envelope = mc.liquidity_envelope({1001: frame}, order_notional=5_000, before=sessions[25])
    assert envelope.rows[0].floor_median == pytest.approx(1_000_000)
    assert envelope.within.value is True           # the thin tail is on/after the cut


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
    # peak after the first +2%, trough after +2%/-2%/+2%/-2%: 0.98 * 1.02 * 0.98 - 1
    assert out.raw_spy_drawdown == pytest.approx(0.98 * 1.02 * 0.98 - 1, rel=1e-6)


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


def test_a_first_session_loss_counts_as_strategy_drawdown():
    # The strategy's only loss is session 1 (-5%), then it only rises. Measured
    # from the account_equity starting level its drawdown is -5%, not 0.
    strat = equity_curve([-0.05, 0.01, 0.01, 0.01])
    spy = spy_over([0.0, -0.02, 0.02, -0.02, 0.02], start='2024-01-31')  # first return unused
    out = mc.vol_matched_benchmark(strat, spy, account_equity=100_000.0)
    assert out.ratio.value is not None and out.ratio.value > 0
    # scaled SPY: -2%s, +2%s, -2%s of the starting level; its trough is session 3
    s = out.scale
    bench_drawdown = (1 - 0.02 * s) * (1 + 0.02 * s) * (1 - 0.02 * s) - 1
    assert out.ratio.value == pytest.approx(0.05 / abs(bench_drawdown), rel=1e-6)


def test_time_in_market_treats_naive_round_trip_times_as_utc():
    day = XNYS.sessions_in_range('2024-02-01', '2024-02-10')[0]
    opens = XNYS.session_open(day)
    def naive_rt(start_min, end_min):
        def stamp(minutes):
            return (opens + pd.Timedelta(minutes=minutes)).tz_convert('UTC').tz_localize(None).to_pydatetime()
        return RoundTrip(conid=1, open_time=stamp(start_min), close_time=stamp(end_min),
                         quantity=1, entry_price=100, exit_price=100, pnl=0)
    share = mc.time_in_market([naive_rt(0, 39)], calendar_name='XNYS',
                              start=day.date(), end=day.date())
    assert share == pytest.approx(39 / 390)
