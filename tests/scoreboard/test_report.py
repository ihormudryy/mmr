import datetime as dt
import itertools
import json
from dataclasses import replace

import pytest

from tests.scoreboard.common import EXP_ID, NOW, UTC
from tests.scoreboard.ledger_world import CAL, D, D_NEXT, END, EXP, END_FLAT
from trader.scoreboard.report import ReportInputs, build_report

START = EXP.started_at


def session(day, start, end, **changes):
    row = {"experiment_id": EXP_ID, "session_date": day, "session_end_state": "FLAT", "start_nlv_usd": start,
           "end_nlv_usd": end, "realized_pnl_usd": 1.0, "commissions_usd": 2.0, "commission_json": '{"e1": 2.0}',
           "peak_gross_exposure_usd": 1000.0, "trade_count": 1, "open_positions": 0, "start_source": "prev_end",
           "missing_sessions_before": 0}
    row.update(changes)
    return row


def trip(net, **changes):
    row = {"status": "CLOSED", "net_pnl_usd": net, "fees_usd": 1.0, "fees_complete": True,
           "notional_traded_usd": 2000.0, "decider": "jev", "strategy_version": "sv", "style": "intraday_long"}
    row.update(changes)
    return row


def empty_inputs(**changes):
    values = dict(experiment=EXP, rows=[], adjustments=[], trips=[], spy_closes={}, spy_version=None,
                  spy_provider="history_duckdb", ai_costs=[], sim_decisions=[], sim_outcomes=[], incidents=[], warnings=[], outbox=None,
                  calendar=CAL)
    values.update(changes)
    return ReportInputs(**values)


def full_inputs(**changes):
    rows = [session(D, 100_000.0, 101_000.0, start_source="experiment_start"),
            session(D_NEXT, 101_000.0, 100_500.0)]
    spy = {dt.date(2026, 10, 5): 500.0, D: 505.0, D_NEXT: 510.0}
    values = dict(rows=rows, trips=[trip(10.0), trip(-5.0)], spy_closes=spy, spy_version=1)
    values.update(changes)
    return empty_inputs(**values)


def test_report_with_no_sessions_is_all_unknown():
    r = build_report(empty_inputs())
    assert r["account"]["sessions"] == 0 and r["account"]["return_pct"] is None
    assert r["account"]["sharpe_daily"] is None and r["account"]["eod_drawdown_pct"] is None
    assert r["trips"]["closed"] == 0 and r["trips"]["win_rate"] is None and r["trips"]["profit_factor"] is None
    assert (r["benchmarks"]["ai_cost_usd"], r["benchmarks"]["ai_calls"], r["benchmarks"]["ai_costs_status"]) == (
        None, None, "UNAVAILABLE")
    assert r["benchmarks"]["books"] == [] and r["benchmarks"]["ai_cost"]["status"] == "NONE"
    assert r["label"] == "PAPER"
    assert r["benchmarks"]["spy"]["return_pct"] is None and r["benchmarks"]["vs_spy_pp"] is None
    assert "proof of live edge" in r["disclaimer"]


def test_report_without_an_experiment_is_explicit():
    r = build_report(empty_inputs(experiment=None))
    assert r["experiment"] is None and r["label"] == "PAPER" and r["account"]["sessions"] == 0


def test_report_json_has_no_nan_or_infinity():
    json.dumps(build_report(full_inputs()), allow_nan=False)


def test_two_sessions_give_return_drawdown_and_vs_spy():
    r = build_report(full_inputs())
    assert r["account"]["return_pct"] == pytest.approx(0.5)
    assert r["account"]["eod_drawdown_pct"] == pytest.approx(100 * 500 / 101_000, abs=1e-6)
    assert r["benchmarks"]["spy"]["return_pct"] == pytest.approx(2.0)        # 510 / 500 (base = Oct 5)
    assert r["benchmarks"]["vs_spy_pp"] == pytest.approx(0.5 - 2.0)
    assert r["account"]["pnl_usd"] == pytest.approx(500.0)
    assert [s["date"] for s in r["sessions"]] == [D.isoformat(), D_NEXT.isoformat()]
    assert r["sessions"][0]["return_pct"] == pytest.approx(1.0)


def test_missing_spy_bar_for_the_last_session_makes_spy_return_and_vs_spy_unknown():
    r = build_report(full_inputs(spy_closes={dt.date(2026, 10, 5): 500.0, D: 505.0}))
    assert r["benchmarks"]["spy"]["return_pct"] is None and r["benchmarks"]["vs_spy_pp"] is None


def test_missing_spy_base_close_is_unknown():
    r = build_report(full_inputs(spy_closes={D: 505.0, D_NEXT: 510.0}))
    assert r["benchmarks"]["spy"]["return_pct"] is None


@pytest.mark.parametrize("start,base", [(dt.date(2026, 10, 7), dt.date(2026, 10, 6)),     # Wednesday -> Tuesday
                                        (dt.date(2026, 10, 12), dt.date(2026, 10, 9)),    # Monday -> Friday
                                        (dt.date(2026, 11, 27), dt.date(2026, 11, 25))])  # after Thanksgiving
def test_spy_base_is_the_last_completed_close_before_the_start_date(start, base):
    started_at = dt.datetime.combine(start, dt.time(14, 0), tzinfo=UTC)
    r = build_report(empty_inputs(experiment=replace(EXP, started_at=started_at)))
    assert r["benchmarks"]["spy"]["base_date"] == base.isoformat()


def test_labels_for_spy_and_eod_drawdown_are_present():
    r = build_report(full_inputs())
    assert r["benchmarks"]["spy"]["label"] == "SPY price only; dividends excluded"
    assert r["account"]["eod_drawdown_label"] == "drawdown from end-of-day equity; intraday lows may be missed"
    assert "kill" not in json.dumps(r["account"]).lower() and "max_drawdown" not in json.dumps(r)


def test_session_with_unknown_nlv_is_counted_and_excluded_from_drawdown():
    rows = [session(D, 100_000.0, None, session_end_state="UNKNOWN"), session(D_NEXT, None, 99_000.0)]
    r = build_report(full_inputs(rows=rows))
    assert r["account"]["unknown_nlv_sessions"] == 1 and r["account"]["sessions"] == 2
    assert r["account"]["eod_drawdown_pct"] == pytest.approx(1.0)
    assert r["sessions"][0]["end_nlv_usd"] is None and r["sessions"][0]["end_state"] == "UNKNOWN"


def test_last_session_with_unknown_nlv_leaves_the_return_at_the_last_known_date():
    rows = [session(D, 100_000.0, 101_000.0), session(D_NEXT, 101_000.0, None)]
    r = build_report(full_inputs(rows=rows))
    assert r["account"]["return_pct"] == pytest.approx(1.0) and r["account"]["end_date"] == D.isoformat()
    assert r["benchmarks"]["spy"]["last_date"] == D.isoformat()
    assert r["benchmarks"]["spy"]["return_pct"] == pytest.approx(1.0)


def test_row_commissions_include_adjustments_and_stay_unknown_until_complete():
    rows = [session(D, 1.0, 1.0, commissions_usd=None, commission_json='{"e1": null, "e2": 1.0}')]
    unknown = build_report(full_inputs(rows=rows))
    assert unknown["sessions"][0]["commissions_usd"] is None
    adjustment = {"session_date": D, "exec_id": "e1", "amount_usd": 0.5}
    known = build_report(full_inputs(rows=rows, adjustments=[adjustment]))
    assert known["sessions"][0]["commissions_usd"] == pytest.approx(1.5)


_COST_IDS = itertools.count(1)


def _cost(cost):
    return {"record_id": f"c{next(_COST_IDS)}", "cost_usd": cost,
            "cost_status": "unknown" if cost is None else "confirmed", "corrects_record_id": None,
            "correction_seq": 0}


def test_unknown_ai_cost_makes_pnl_minus_cost_unknown():
    b = build_report(full_inputs(ai_costs=[_cost(0.5), _cost(None)]))["benchmarks"]
    assert (b["ai_calls"], b["ai_cost_usd"], b["pnl_minus_ai_cost_usd"], b["ai_costs_status"]) == (
        2, None, None, "AVAILABLE")


def test_no_ai_cost_rows_is_unavailable_not_zero():
    b = build_report(full_inputs())["benchmarks"]
    assert (b["ai_cost_usd"], b["ai_calls"], b["pnl_minus_ai_cost_usd"], b["ai_costs_status"]) == (
        None, None, None, "UNAVAILABLE")


def test_pnl_minus_ai_cost_uses_account_pnl_in_usd():
    b = build_report(full_inputs(ai_costs=[_cost(0.5), _cost(1.5)]))["benchmarks"]
    assert b["ai_cost_usd"] == pytest.approx(2.0) and b["pnl_minus_ai_cost_usd"] == pytest.approx(498.0)


def test_fill_outside_session_is_listed_as_a_warning():
    warning = {"code": "FILL_OUTSIDE_SESSION", "detail": "exec x on 2026-10-10"}
    assert build_report(full_inputs(warnings=[warning]))["warnings"] == [warning]


def test_books_are_listed_separately_with_the_simulated_label():
    decisions = [{"record_id": "a", "baseline_id": "follow_signal.v1", "cohort": "strategy_signal"},
                 {"record_id": "b", "baseline_id": "no_trade.v1", "cohort": "self_found"}]
    outcomes = [{"record_id": "a", "status": "COMPLETE", "pnl_usd": 3.0, "reason": None, "trades": 1},
                {"record_id": "b", "status": "COMPLETE", "pnl_usd": 0.0, "reason": None, "trades": 0}]
    b = build_report(full_inputs(sim_decisions=decisions, sim_outcomes=outcomes))["benchmarks"]
    assert "simulated" not in b
    assert [(x["baseline_id"], x["label"], x["pnl_usd"]) for x in b["books"]] == [
        ("follow_signal.v1", "simulated", 3.0), ("no_trade.v1", "simulated", 0.0)]


def test_estimated_cost_is_labelled_and_counted_in_pnl_minus_cost():
    estimated = {**_cost(1.5), "record_id": "e", "cost_status": "estimated"}
    b = build_report(full_inputs(ai_costs=[_cost(0.5), estimated]))["benchmarks"]
    assert b["ai_cost"]["status"] == "ESTIMATED" and b["ai_cost_usd"] == pytest.approx(2.0)
    assert b["pnl_minus_ai_cost_usd"] == pytest.approx(498.0)


def test_splits_cover_trip_metrics_by_three_keys():
    splits = build_report(full_inputs(trips=[trip(1.0), trip(2.0, decider=None)]))["splits"]
    assert set(splits) == {"strategy_version", "decider", "style"}
    assert set(splits["decider"]) == {"jev", "unattributed"}


def test_outbox_line_is_disabled_without_telegram():
    assert build_report(full_inputs())["outbox"] == {"enabled": False, "pending": None, "last_sent_at": None}
    line = build_report(full_inputs(outbox={"pending": 2, "last_sent_at": NOW}))["outbox"]
    assert line == {"enabled": True, "pending": 2, "last_sent_at": NOW.isoformat()}


# -- the service ---------------------------------------------------------------

def test_refresh_then_report_shows_a_new_fill(world, scoreboard):
    world.round_trip()
    assert scoreboard.report()["trips"]["closed"] == 0          # report() only reads
    scoreboard.refresh()
    report = scoreboard.report()
    assert report["trips"]["closed"] == 1 and len(world.store.fetch("round_trips", {})) == 1


def test_report_for_a_named_experiment_ignores_other_experiments(world, scoreboard):
    world.round_trip()
    scoreboard.refresh()
    world.ledger().record_session_end(END_FLAT)
    other = scoreboard.report("exp-ffffffffffffffffffff")
    assert other["error_code"] == "EXPERIMENT_NOT_FOUND"
    assert scoreboard.report(EXP_ID)["account"]["sessions"] == 1


def test_benchmark_refresh_runs_to_the_last_completed_session(world, scoreboard):
    world.clock[0] = dt.datetime(2026, 10, 7, 15, 0, tzinfo=UTC)             # Wednesday, before the close
    assert scoreboard.last_completed_session(world.clock[0]) == D
    assert scoreboard.last_completed_session(dt.datetime(2026, 10, 7, 20, 20, tzinfo=UTC)) == D_NEXT


def test_service_lists_fills_outside_a_session(world, scoreboard):
    world.fill("sat", "BUY", 1, 100, 0.0, dt.datetime(2026, 10, 10, 15, 0, tzinfo=UTC))   # Saturday
    codes = [w["code"] for w in scoreboard.report()["warnings"]]
    assert codes == ["FILL_OUTSIDE_SESSION"]


def test_round_trips_stop_at_the_experiment_stop(world, scoreboard):
    world.round_trip()
    world.fill("later", "BUY", 1, 100, 0.0, dt.datetime(2026, 10, 8, 15, 0, tzinfo=UTC))
    world.experiments.record = replace(EXP, state="STOPPED", stopped_at=END)
    scoreboard.refresh()
    assert len(world.store.fetch("round_trips", {})) == 1
