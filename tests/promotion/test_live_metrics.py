"""P4 Task 3 -- live statistical + concentration evidence gate.

Contract:
* ``LiveMetrics.evaluate(EvidenceWindow) -> MetricDecision`` is a pure
  function of an ``EvidenceWindow`` (same "no forged window" discipline as
  ``PaperGate`` -- the caller hands it a freshly projected window).
* Only round trips carrying ``resolved: True`` (authoritative P3 attribution)
  feed the metrics below; every other round trip in the window is reported
  separately as ``unresolved_round_trips``, never silently dropped or
  averaged in as if it were zero.
* Daily Sharpe and Sortino are corroborative only -- they never appear in
  ``blockers`` no matter how negative (or absent) they are.
* Every OTHER metric (net expectancy, prediction envelope, average/tail
  slippage, drawdown, best-trade removal, trade/day/instrument/regime profit
  concentration) is safety evidence: missing data required to compute it
  blocks exactly like a negative/breached value would -- "missing or
  negative safety evidence always wins".
* Trade profit concentration may not exceed 35%; day profit concentration
  may not exceed 40% -- each independently blocks.
* Window-level safety signals (breaker trip, cost breach, drawdown breach,
  staleness) carry through as blockers too, exactly like ``PaperGate``.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trader.promotion.evidence_store import EvidenceWindow

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _rt(
    idx: int,
    *,
    pnl: float = 10.0,
    instrument: str = "1000",
    session: str = "s0",
    regime: str = "trend",
    slippage_bps: float | None = 2.0,
    within_envelope: bool | None = True,
    resolved: bool = True,
) -> dict:
    record = {
        "round_trip_id": f"rt-{idx}",
        "instrument_id": instrument,
        "session_id": session,
        "regime_id": regime,
        "pnl_after_cost": pnl,
        "resolved": resolved,
    }
    if slippage_bps is not None:
        record["slippage_bps"] = slippage_bps
    if within_envelope is not None:
        record["within_prediction_envelope"] = within_envelope
    return record


def _clean_records(n: int = 20) -> tuple[dict, ...]:
    """``n`` resolved round trips spread evenly across 5 distinct
    instruments/regimes/days with small, mildly-varying (never zero
    variance) positive pnl -- variance is needed so daily Sharpe/Sortino
    are actually defined in the baseline, while every concentration ratio
    (trade/day/instrument/regime) stays comfortably under its floor and the
    overall average pnl is exactly 10.0 for easy assertions."""
    instruments = [str(1000 + i) for i in range(5)]
    regimes = ["trend", "range", "volatile", "squeeze", "reversal"]
    sessions = [f"s{i}" for i in range(5)]
    records = []
    for i in range(n):
        k = i % 5
        pnl = 9.0 + 0.5 * k  # per-day totals 36/38/40/42/44 -> overall mean 10.0
        records.append(_rt(
            i,
            pnl=pnl,
            instrument=instruments[k],
            session=sessions[k],
            regime=regimes[k],
            slippage_bps=2.0,
            within_envelope=True,
        ))
    return tuple(records)


def _window(
    *,
    round_trip_records: tuple[dict, ...] | None = None,
    breaker_trips: tuple = (),
    cost_breaches: tuple = (),
    drawdown_breaches: tuple = (),
    stale: bool = False,
    as_of: dt.datetime = NOW,
    window_reset_at: dt.datetime | None = None,
) -> EvidenceWindow:
    if round_trip_records is None:
        round_trip_records = _clean_records()
    return EvidenceWindow(
        strategy_id=STRATEGY,
        as_of=as_of,
        window_reset_at=window_reset_at,
        first_event_at=NOW - dt.timedelta(days=30),
        last_event_at=NOW,
        calendar_days=tuple(f"2026-06-{i+1:02d}" for i in range(5)),
        session_ids=tuple(f"s{i}" for i in range(5)),
        round_trip_ids=tuple(r["round_trip_id"] for r in round_trip_records),
        instrument_ids=tuple(sorted({
            r["instrument_id"] for r in round_trip_records if r.get("instrument_id") is not None
        })),
        corrections=(),
        breaker_trips=breaker_trips,
        cost_breaches=cost_breaches,
        drawdown_breaches=drawdown_breaches,
        stale=stale,
        event_count=len(round_trip_records),
        round_trip_records=round_trip_records,
    )


# ---------------------------------------------------------------------------
# Baseline happy path
# ---------------------------------------------------------------------------

def test_all_clean_evidence_passes():
    from trader.promotion.live_metrics import LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert decision.passed is True
    assert decision.blockers == ()
    assert decision.metrics["net_expectancy"] == pytest.approx(10.0)


def test_decision_to_payload_is_json_serializable_and_deterministic():
    import json

    from trader.promotion.live_metrics import LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    payload_a = decision.to_payload()
    payload_b = LiveMetrics().evaluate(_window()).to_payload()
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b, sort_keys=True)


# ---------------------------------------------------------------------------
# Net expectancy
# ---------------------------------------------------------------------------

def test_negative_net_expectancy_blocks():
    from trader.promotion.live_metrics import BLOCK_NEGATIVE_NET_EXPECTANCY, LiveMetrics

    records = tuple(_rt(i, pnl=-5.0, session=f"s{i % 5}", instrument=str(1000 + i % 5)) for i in range(20))
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert decision.passed is False
    assert BLOCK_NEGATIVE_NET_EXPECTANCY in decision.blockers
    assert decision.metrics["net_expectancy"] < 0


def test_zero_net_expectancy_blocks():
    from trader.promotion.live_metrics import BLOCK_NEGATIVE_NET_EXPECTANCY, LiveMetrics

    records = tuple(_rt(i, pnl=0.0, session=f"s{i % 5}", instrument=str(1000 + i % 5)) for i in range(20))
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert BLOCK_NEGATIVE_NET_EXPECTANCY in decision.blockers


def test_no_resolved_round_trips_blocks_with_missing_evidence():
    from trader.promotion.live_metrics import BLOCK_MISSING_EVIDENCE, LiveMetrics

    records = tuple(_rt(i, resolved=False) for i in range(5))
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert decision.passed is False
    assert BLOCK_MISSING_EVIDENCE in decision.blockers
    assert len(decision.unresolved_round_trips) == 5


# ---------------------------------------------------------------------------
# Unresolved rows reported separately, never averaged in
# ---------------------------------------------------------------------------

def test_unresolved_round_trips_excluded_from_metrics_and_reported_separately():
    from trader.promotion.live_metrics import LiveMetrics

    resolved = list(_clean_records(20))
    unresolved = [_rt(100, pnl=-100000.0, resolved=False), _rt(101, pnl=999999.0, resolved=False)]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(resolved + unresolved)))
    assert decision.passed is True
    assert decision.metrics["net_expectancy"] == pytest.approx(10.0)
    assert len(decision.unresolved_round_trips) == 2
    assert {r["round_trip_id"] for r in decision.unresolved_round_trips} == {"rt-100", "rt-101"}


# ---------------------------------------------------------------------------
# Sharpe / Sortino: corroborative only
# ---------------------------------------------------------------------------

def test_sharpe_and_sortino_are_computed_from_daily_pnl():
    from trader.promotion.live_metrics import LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert decision.metrics["daily_sharpe"] is not None


def test_negative_sharpe_and_sortino_never_block():
    """Daily Sharpe/Sortino sign necessarily tracks total P&L sign (the sum
    invariant is the same whether you average per-day or per-trade), so a
    deeply negative Sharpe implies negative net expectancy too -- that
    already blocks on its own economic merits. What must hold regardless is
    that Sharpe/Sortino themselves never contribute a blocker reason."""
    from trader.promotion.live_metrics import LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    day_totals = [-50.0, -50.0, -50.0, -50.0, 10.0]
    records = []
    for day_idx, day_total in enumerate(day_totals):
        per_trade = day_total / 4
        for k in range(4):
            i = day_idx * 4 + k
            records.append(_rt(
                i, pnl=per_trade, session=f"s{day_idx}", instrument=instruments[i % 5],
            ))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.metrics["daily_sharpe"] is not None
    assert decision.metrics["daily_sharpe"] < 0
    for blocker in decision.blockers:
        assert "sharpe" not in blocker
        assert "sortino" not in blocker


def test_missing_daily_pnl_data_still_produces_none_sharpe_without_blocking():
    """Only one session in the window -- Sharpe/Sortino are undefined (no
    variance to measure). That alone must never contribute a blocker; a
    single day is separately and correctly caught by day-profit
    concentration (100% of profit on one day), which is asserted here too
    so the None-Sharpe case is not mistaken for a free pass."""
    from trader.promotion.live_metrics import BLOCK_DAY_PROFIT_CONCENTRATION, LiveMetrics

    records = tuple(_rt(i, session="s0", instrument=str(1000 + i % 5)) for i in range(20))
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert decision.metrics["daily_sharpe"] is None
    assert BLOCK_DAY_PROFIT_CONCENTRATION in decision.blockers
    for blocker in decision.blockers:
        assert "sharpe" not in blocker
        assert "sortino" not in blocker


# ---------------------------------------------------------------------------
# Prediction envelope
# ---------------------------------------------------------------------------

def test_prediction_envelope_breach_blocks():
    from trader.promotion.live_metrics import BLOCK_PREDICTION_ENVELOPE, LiveMetrics

    records = list(_clean_records(20))
    for i in range(6):  # 30% miss rate, above the 20% default threshold
        records[i] = {**records[i], "within_prediction_envelope": False}
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_PREDICTION_ENVELOPE in decision.blockers


def test_missing_prediction_envelope_field_counts_as_a_miss():
    from trader.promotion.live_metrics import BLOCK_PREDICTION_ENVELOPE, LiveMetrics

    records = list(_clean_records(20))
    for i in range(6):
        del records[i]["within_prediction_envelope"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert BLOCK_PREDICTION_ENVELOPE in decision.blockers


def test_low_envelope_miss_rate_does_not_block():
    from trader.promotion.live_metrics import BLOCK_PREDICTION_ENVELOPE, LiveMetrics

    records = list(_clean_records(20))
    records[0] = {**records[0], "within_prediction_envelope": False}  # 5% miss rate
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert BLOCK_PREDICTION_ENVELOPE not in decision.blockers


# ---------------------------------------------------------------------------
# Slippage: average + tail
# ---------------------------------------------------------------------------

def test_average_slippage_breach_blocks():
    from trader.promotion.live_metrics import BLOCK_AVERAGE_SLIPPAGE, LiveMetrics

    records = tuple(
        _rt(i, slippage_bps=100.0, session=f"s{i % 5}", instrument=str(1000 + i % 5))
        for i in range(20)
    )
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert decision.passed is False
    assert BLOCK_AVERAGE_SLIPPAGE in decision.blockers


def test_tail_slippage_breach_blocks_even_with_low_average():
    """'Tail' slippage is the worst single observed value -- a lone bad fill
    can still blow through the tail floor while the mean (dominated by 19
    clean fills) stays comfortably under the average floor."""
    from trader.promotion.live_metrics import BLOCK_AVERAGE_SLIPPAGE, BLOCK_TAIL_SLIPPAGE, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = []
    for i in range(20):
        slip = 100.0 if i == 0 else 1.0
        records.append(_rt(i, slippage_bps=slip, session=f"s{i % 5}", instrument=instruments[i % 5]))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_TAIL_SLIPPAGE in decision.blockers
    assert BLOCK_AVERAGE_SLIPPAGE not in decision.blockers


def test_incomplete_slippage_evidence_blocks():
    from trader.promotion.live_metrics import BLOCK_INCOMPLETE_SLIPPAGE_EVIDENCE, LiveMetrics

    records = list(_clean_records(20))
    del records[0]["slippage_bps"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_INCOMPLETE_SLIPPAGE_EVIDENCE in decision.blockers


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def test_drawdown_breach_blocks():
    from trader.promotion.live_metrics import BLOCK_DRAWDOWN, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = []
    # Big run-up then a deep pullback -- classic large-drawdown shape --
    # followed by enough small recoveries to keep net expectancy positive
    # and each individual trade's profit share under 35%.
    for i in range(10):
        records.append(_rt(i, pnl=20.0, session=f"s{i % 5}", instrument=instruments[i % 5]))
    records.append(_rt(10, pnl=-150.0, session="s0", instrument=instruments[0]))
    for i in range(11, 30):
        records.append(_rt(i, pnl=20.0, session=f"s{i % 5}", instrument=instruments[i % 5]))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_DRAWDOWN in decision.blockers


def test_shallow_drawdown_does_not_block():
    from trader.promotion.live_metrics import BLOCK_DRAWDOWN, LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert BLOCK_DRAWDOWN not in decision.blockers


# ---------------------------------------------------------------------------
# Best-trade removal ("anti-luck")
# ---------------------------------------------------------------------------

def test_best_trade_removal_blocks_when_reliant_on_a_single_trade():
    from trader.promotion.live_metrics import BLOCK_BEST_TRADE_RELIANCE, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = [_rt(0, pnl=1000.0, session="s0", instrument=instruments[0])]
    for i in range(1, 20):
        records.append(_rt(i, pnl=-1.0, session=f"s{i % 5}", instrument=instruments[i % 5]))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_BEST_TRADE_RELIANCE in decision.blockers


def test_best_trade_removal_does_not_block_when_broadly_profitable():
    from trader.promotion.live_metrics import BLOCK_BEST_TRADE_RELIANCE, LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert BLOCK_BEST_TRADE_RELIANCE not in decision.blockers


def test_best_trade_removal_blocks_with_fewer_than_two_resolved_trades():
    from trader.promotion.live_metrics import BLOCK_BEST_TRADE_RELIANCE, LiveMetrics

    records = (_rt(0, pnl=10.0),)
    decision = LiveMetrics().evaluate(_window(round_trip_records=records))
    assert BLOCK_BEST_TRADE_RELIANCE in decision.blockers


# ---------------------------------------------------------------------------
# Trade profit concentration (35%)
# ---------------------------------------------------------------------------

def test_trade_profit_concentration_above_35pct_blocks():
    from trader.promotion.live_metrics import BLOCK_TRADE_PROFIT_CONCENTRATION, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = [_rt(0, pnl=500.0, session="s0", instrument=instruments[0])]
    for i in range(1, 20):
        records.append(_rt(i, pnl=40.0, session=f"s{i % 5}", instrument=instruments[i % 5]))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_TRADE_PROFIT_CONCENTRATION in decision.blockers


def test_evenly_distributed_trade_profit_does_not_block():
    from trader.promotion.live_metrics import BLOCK_TRADE_PROFIT_CONCENTRATION, LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert BLOCK_TRADE_PROFIT_CONCENTRATION not in decision.blockers


# ---------------------------------------------------------------------------
# Day profit concentration (40%)
# ---------------------------------------------------------------------------

def test_day_profit_concentration_above_40pct_blocks():
    from trader.promotion.live_metrics import BLOCK_DAY_PROFIT_CONCENTRATION, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = []
    for i in range(10):
        records.append(_rt(i, pnl=100.0, session="s0", instrument=instruments[i % 5]))
    for i in range(10, 30):
        records.append(_rt(i, pnl=5.0, session=f"s{1 + i % 4}", instrument=instruments[i % 5]))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_DAY_PROFIT_CONCENTRATION in decision.blockers


def test_evenly_distributed_day_profit_does_not_block():
    from trader.promotion.live_metrics import BLOCK_DAY_PROFIT_CONCENTRATION, LiveMetrics

    decision = LiveMetrics().evaluate(_window())
    assert BLOCK_DAY_PROFIT_CONCENTRATION not in decision.blockers


def test_missing_session_id_blocks_day_concentration_and_sharpe():
    from trader.promotion.live_metrics import BLOCK_INCOMPLETE_SESSION_EVIDENCE, LiveMetrics

    records = list(_clean_records(20))
    del records[0]["session_id"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_INCOMPLETE_SESSION_EVIDENCE in decision.blockers
    assert decision.metrics["daily_sharpe"] is None


# ---------------------------------------------------------------------------
# Instrument / regime profit concentration
# ---------------------------------------------------------------------------

def test_instrument_profit_concentration_blocks():
    from trader.promotion.live_metrics import BLOCK_INSTRUMENT_PROFIT_CONCENTRATION, LiveMetrics

    regimes = [f"r{i}" for i in range(5)]
    records = []
    for i in range(10):
        records.append(_rt(
            i, pnl=100.0, session=f"s{i % 5}", instrument="9999", regime=regimes[i % 5],
        ))
    for i in range(10, 30):
        records.append(_rt(
            i, pnl=5.0, session=f"s{i % 5}", instrument=str(1000 + i % 5), regime=regimes[i % 5],
        ))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_INSTRUMENT_PROFIT_CONCENTRATION in decision.blockers


def test_regime_profit_concentration_blocks():
    from trader.promotion.live_metrics import BLOCK_REGIME_PROFIT_CONCENTRATION, LiveMetrics

    instruments = [str(1000 + i) for i in range(5)]
    records = []
    for i in range(10):
        records.append(_rt(i, pnl=100.0, session=f"s{i % 5}", instrument=instruments[i % 5], regime="squeeze"))
    for i in range(10, 30):
        records.append(_rt(i, pnl=5.0, session=f"s{i % 5}", instrument=instruments[i % 5], regime="trend"))
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_REGIME_PROFIT_CONCENTRATION in decision.blockers


def test_missing_regime_id_blocks_regime_concentration_only():
    from trader.promotion.live_metrics import (
        BLOCK_INCOMPLETE_REGIME_EVIDENCE,
        BLOCK_INCOMPLETE_SESSION_EVIDENCE,
        LiveMetrics,
    )

    records = list(_clean_records(20))
    del records[0]["regime_id"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert BLOCK_INCOMPLETE_REGIME_EVIDENCE in decision.blockers
    assert BLOCK_INCOMPLETE_SESSION_EVIDENCE not in decision.blockers


def test_missing_instrument_id_blocks_instrument_concentration_only():
    from trader.promotion.live_metrics import (
        BLOCK_INCOMPLETE_INSTRUMENT_EVIDENCE,
        BLOCK_INCOMPLETE_REGIME_EVIDENCE,
        BLOCK_INCOMPLETE_SESSION_EVIDENCE,
        LiveMetrics,
    )

    records = list(_clean_records(20))
    del records[0]["instrument_id"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert BLOCK_INCOMPLETE_INSTRUMENT_EVIDENCE in decision.blockers
    assert BLOCK_INCOMPLETE_REGIME_EVIDENCE not in decision.blockers
    assert BLOCK_INCOMPLETE_SESSION_EVIDENCE not in decision.blockers


def test_missing_pnl_after_cost_blocks_net_expectancy_as_missing():
    from trader.promotion.live_metrics import BLOCK_MISSING_NET_EXPECTANCY_EVIDENCE, LiveMetrics

    records = list(_clean_records(20))
    del records[0]["pnl_after_cost"]
    decision = LiveMetrics().evaluate(_window(round_trip_records=tuple(records)))
    assert decision.passed is False
    assert BLOCK_MISSING_NET_EXPECTANCY_EVIDENCE in decision.blockers
    assert decision.metrics["net_expectancy"] is None


def test_evenly_distributed_instrument_and_regime_profit_does_not_block():
    from trader.promotion.live_metrics import (
        BLOCK_INSTRUMENT_PROFIT_CONCENTRATION,
        BLOCK_REGIME_PROFIT_CONCENTRATION,
        LiveMetrics,
    )

    decision = LiveMetrics().evaluate(_window())
    assert BLOCK_INSTRUMENT_PROFIT_CONCENTRATION not in decision.blockers
    assert BLOCK_REGIME_PROFIT_CONCENTRATION not in decision.blockers


# ---------------------------------------------------------------------------
# Window-level safety signals carry through (fail-closed, never averaged away)
# ---------------------------------------------------------------------------

def test_breaker_trip_blocks_regardless_of_clean_metrics():
    from trader.promotion.live_metrics import LiveMetrics
    from trader.promotion.paper_gate import BLOCK_BREAKER_TRIP

    decision = LiveMetrics().evaluate(_window(breaker_trips=({"incident_id": "inc-1"},)))
    assert decision.passed is False
    assert BLOCK_BREAKER_TRIP in decision.blockers


def test_cost_breach_blocks():
    from trader.promotion.live_metrics import LiveMetrics
    from trader.promotion.paper_gate import BLOCK_STRESSED_COST_BREACH

    decision = LiveMetrics().evaluate(_window(cost_breaches=({"metric": "x"},)))
    assert BLOCK_STRESSED_COST_BREACH in decision.blockers


def test_drawdown_breach_event_blocks():
    from trader.promotion.live_metrics import LiveMetrics
    from trader.promotion.paper_gate import BLOCK_DRAWDOWN_BREACH

    decision = LiveMetrics().evaluate(_window(drawdown_breaches=({"drawdown_pct": 5.0},)))
    assert BLOCK_DRAWDOWN_BREACH in decision.blockers


def test_stale_window_blocks():
    from trader.promotion.live_metrics import LiveMetrics
    from trader.promotion.paper_gate import BLOCK_STALE_EVIDENCE

    decision = LiveMetrics().evaluate(_window(stale=True))
    assert BLOCK_STALE_EVIDENCE in decision.blockers
