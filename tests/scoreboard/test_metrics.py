import pytest

from trader.scoreboard.metrics import (daily_sharpe, eod_drawdown_pct, group_trip_metrics, session_returns,
                                       trip_metrics)


def row(start, end):
    return {"start_nlv_usd": start, "end_nlv_usd": end}


def trip(net, *, status="CLOSED", fees=0.0, notional=1000.0, decider="jev", **extra):
    return {"status": status, "net_pnl_usd": net, "fees_usd": fees, "fees_complete": fees is not None,
            "notional_traded_usd": notional, "decider": decider, "strategy_version": "sv", "style": "s", **extra}


def test_daily_sharpe_known_values():
    d, a, w = daily_sharpe([0.01, -0.01, 0.02, 0.0])
    assert d == pytest.approx(0.3873, abs=1e-4) and a == pytest.approx(d * 252 ** 0.5) and w == "SMALL_SAMPLE"


@pytest.mark.parametrize("returns", [[], [0.01], [0.01, 0.01]])
def test_sharpe_is_unknown_without_variance_or_sample(returns):
    assert daily_sharpe(returns)[:2] == (None, None)


def test_sharpe_warning_clears_at_sixty_sessions():
    assert daily_sharpe([0.01, -0.01] * 29 + [0.0])[2] == "SMALL_SAMPLE"
    assert daily_sharpe([0.01, -0.01] * 30)[2] is None


def test_returns_skip_unknown_values_instead_of_zeroing_them():
    assert session_returns([row(100, 101), row(None, 105), row(100, None), row(0, 5)]) == [
        pytest.approx(0.01), None, None, None]


def test_eod_drawdown_includes_the_start_point():
    assert eod_drawdown_pct([100, 110, 99, 105]) == pytest.approx(10.0)
    assert eod_drawdown_pct([110, 99]) == pytest.approx(10.0)
    assert eod_drawdown_pct([100, 101]) == 0.0


def test_eod_drawdown_needs_two_points():
    assert eod_drawdown_pct([100]) is None and eod_drawdown_pct([]) is None


def test_trip_metrics_win_rate_profit_factor_and_unresolved_fees():
    m = trip_metrics([trip(10.0), trip(5.0), trip(-5.0), trip(None, fees=None)], start_nlv_usd=10_000.0)
    assert m["win_rate"] == pytest.approx(2 / 3) and m["profit_factor"] == pytest.approx(3.0)
    assert (m["closed"], m["open"], m["unresolved_fee_trips"]) == (4, 0, 1)
    assert m["net_pnl_complete"] is False and m["net_pnl_usd"] is None and m["fees_usd"] is None
    assert m["turnover"] == pytest.approx(0.4)


def test_profit_factor_is_unknown_without_losses_and_with_no_trips():
    assert trip_metrics([trip(1.0)], start_nlv_usd=1.0)["profit_factor"] is None
    empty = trip_metrics([], start_nlv_usd=1.0)
    assert (empty["profit_factor"], empty["win_rate"], empty["closed"], empty["net_pnl_usd"]) == (None, None, 0, 0.0)


def test_open_trips_count_but_never_win_or_lose():
    m = trip_metrics([trip(None, status="OPEN", fees=1.0)], start_nlv_usd=1.0)
    assert (m["open"], m["closed"], m["win_rate"], m["fees_usd"]) == (1, 0, None, 1.0)


def test_turnover_is_unknown_without_a_start_value_but_zero_for_no_trades():
    assert trip_metrics([trip(1.0)], start_nlv_usd=None)["turnover"] is None
    assert trip_metrics([], start_nlv_usd=100.0)["turnover"] == 0.0


def test_groups_put_missing_attribution_under_unattributed():
    groups = group_trip_metrics([trip(1.0), trip(-1.0, decider=None)], "decider", start_nlv_usd=100.0)
    assert set(groups) == {"jev", "unattributed"} and groups["unattributed"]["closed"] == 1


def test_group_key_must_be_a_known_split():
    with pytest.raises(ValueError):
        group_trip_metrics([], "symbol", start_nlv_usd=1.0)
