import datetime as dt
import json
import logging

from tests.scoreboard.common import ACCOUNT, EXP_ID, UTC
from tests.scoreboard.fills import set_commission
from tests.scoreboard.ledger_world import (D, D_NEXT, END, END2, END_FLAT, IN_SESSION, KILL_AT_SAME_ET_DATE,
                                           START, controller_state, position, snap, with_experiment)
from trader.automation.kill_monitor import KillSessionEnd
from trader.data.broker_state import BrokerRiskSnapshotError
from trader.scoreboard.ports import FxEvidence, SessionEnd

AFTER_CLOSE = dt.datetime(2026, 10, 6, 20, 30, tzinfo=UTC)


def _kinds(world):
    return [i["kind"] for i in world.store.incidents()]


def _rows(world):
    return world.store.fetch("equity_daily", {})


def test_flat_session_writes_one_row_with_usd_values(world, ledger):
    world.round_trip()
    row = ledger.record_session_end(END_FLAT)
    assert (row["session_end_state"], row["realized_pnl_usd"], row["commissions_usd"],
            row["trade_count"], row["fill_count"], row["open_positions"]) == ("FLAT", 10.0, 2.0, 1, 2, 0)
    assert (row["start_source"], row["start_nlv_usd"], row["end_nlv_usd"]) == ("experiment_start", 100_000.0, 100_250.0)
    assert json.loads(row["commission_json"]) == {"e1": 1.0, "e2": 1.0}
    assert world.written == [(EXP_ID, D)]


def test_a_session_without_fills_has_zero_counts_and_known_zero_pnl(world, ledger):
    row = ledger.record_session_end(END_FLAT)
    assert (row["fill_count"], row["trade_count"], row["realized_pnl_usd"], row["commissions_usd"]) == (0, 0, 0.0, 0.0)


def test_second_call_is_a_no_op_and_never_edits(world, ledger):
    first = ledger.record_session_end(END_FLAT)
    world.broker.net_liquidation = 1.0
    assert ledger.record_session_end(END_FLAT) == first and len(_rows(world)) == 1
    assert world.written == [(EXP_ID, D)]


def test_next_session_starts_from_previous_end_value(world, ledger):
    ledger.record_session_end(END_FLAT)
    world.broker.net_liquidation = 100_500.0
    row = ledger.record_session_end(SessionEnd(ACCOUNT, D_NEXT, "FLAT", END2))
    assert (row["start_source"], row["start_nlv_usd"], row["missing_sessions_before"]) == ("prev_end", 100_250.0, 0)


def test_skipped_sessions_are_counted(world, ledger):
    friday, wednesday = dt.date(2026, 10, 9), dt.date(2026, 10, 14)
    with_experiment(world, started_at=dt.datetime(2026, 10, 9, 13, 35, tzinfo=UTC))
    ledger.record_session_end(SessionEnd(ACCOUNT, friday, "FLAT", END))
    row = ledger.record_session_end(SessionEnd(ACCOUNT, wednesday, "FLAT", END))
    assert row["missing_sessions_before"] == 2          # Mon 12th, Tue 13th


def test_the_first_row_counts_a_missed_start_session(world, ledger):
    assert ledger.record_session_end(SessionEnd(ACCOUNT, D_NEXT, "FLAT", END2))["missing_sessions_before"] == 1


def test_no_experiment_for_the_date_writes_nothing(world, ledger):
    world.experiments.record = None
    assert ledger.record_session_end(END_FLAT) is None and _rows(world) == []


def test_a_session_before_the_start_or_of_another_account_writes_nothing(world, ledger):
    assert ledger.record_session_end(SessionEnd(ACCOUNT, dt.date(2026, 10, 5), "FLAT", END)) is None
    assert ledger.record_session_end(SessionEnd("OTHER", D, "FLAT", END)) is None
    assert _rows(world) == []


def test_a_session_after_the_stop_writes_nothing(world, ledger):
    with_experiment(world, state="STOPPED", stopped_at=END)
    assert ledger.record_session_end(SessionEnd(ACCOUNT, D_NEXT, "FLAT", END2)) is None
    assert ledger.record_session_end(END_FLAT)["session_end_state"] == "FLAT"


def test_kill_on_that_date_overrides_flat(world, ledger):
    with_experiment(world, state="KILLED", killed_at=KILL_AT_SAME_ET_DATE)
    assert ledger.record_session_end(END_FLAT)["session_end_state"] == "KILLED"


def test_the_plan4_kill_notice_writes_a_killed_row(world, ledger):
    with_experiment(world, state="KILLED", killed_at=KILL_AT_SAME_ET_DATE)
    row = ledger.record_session_end(KillSessionEnd(account_id=ACCOUNT, session_date=D, state="KILLED", ended_at=END))
    assert row["session_end_state"] == "KILLED"


def test_incident_state_maps_to_failed_safe_with_open_positions_counted(world, ledger):
    world.broker.positions = [position(5)]
    world.clock[0] = END
    ledger.on_controller_terminal(controller_state("INCIDENT"))
    row = _rows(world)[0]
    assert (row["session_end_state"], row["open_positions"]) == ("FAILED_SAFE", 1)


def test_fill_after_utc_midnight_belongs_to_the_previous_et_session(world, ledger):
    world.fill("e1", "BUY", 1, 100, 0.0, dt.datetime(2026, 10, 9, 0, 30, tzinfo=UTC))   # Thu 20:30 ET
    row = ledger.record_session_end(SessionEnd(ACCOUNT, dt.date(2026, 10, 8), "FLAT", END))
    assert row["fill_count"] == 1


def test_non_usd_base_with_fx_writes_both_values_and_the_rate(world, ledger):
    with_experiment(world, base_currency="EUR", start_usd_per_base=1.05)
    world.fx.evidence_value = FxEvidence("EUR", 1.10, "ib_account_values", END)
    world.broker.net_liquidation = 90_000.0
    row = ledger.record_session_end(END_FLAT)
    assert (row["end_nlv_base"], row["fx_usd_per_base"], row["fx_source"]) == (90_000.0, 1.10, "ib_account_values")
    assert abs(row["end_nlv_usd"] - 99_000.0) < 1e-6 and abs(row["start_nlv_usd"] - 105_000.0) < 1e-6


def test_non_usd_base_without_fx_never_writes_usd_and_records_an_incident(world, ledger):
    with_experiment(world, base_currency="EUR", start_usd_per_base=1.05)
    world.fx.evidence_value = FxEvidence("EUR", None, "ib_account_values", END)
    row = ledger.record_session_end(END_FLAT)
    assert row["end_nlv_usd"] is None and row["end_nlv_base"] is not None and row["fx_usd_per_base"] is None
    assert _kinds(world) == ["FX_EVIDENCE_MISSING"]


def test_unreadable_fx_is_an_incident_not_a_crash(world, ledger):
    world.fx.raises = RuntimeError("ib down")
    row = ledger.record_session_end(END_FLAT)
    assert row["end_nlv_usd"] is None and "FX_EVIDENCE_MISSING" in _kinds(world)


def test_snapshot_unavailable_gives_unknown_end_value_and_an_incident(world, ledger):
    world.broker.raises = BrokerRiskSnapshotError("NO_PROMOTED_GENERATION", "none")
    row = ledger.record_session_end(END_FLAT)
    assert row["end_nlv_base"] is None and row["open_positions"] is None and row["end_nlv_usd"] is None
    assert "SNAPSHOT_UNAVAILABLE" in _kinds(world)


def test_peak_gross_is_the_maximum_observed_and_survives_a_restart(world, ledger):
    for gross in (1_000.0, 3_000.0, 2_000.0):
        ledger.observe_snapshot(snap(gross=gross))
    restarted = world.ledger()
    restarted.observe_snapshot(snap(gross=2_500.0))
    assert restarted.record_session_end(END_FLAT)["peak_gross_exposure_usd"] == 3_000.0


def test_peak_gross_is_unknown_if_any_observation_lacked_a_market_value(world, ledger):
    ledger.observe_snapshot(snap(gross=1_000.0))
    ledger.observe_snapshot(snap(market_value=None))
    ledger.observe_snapshot(snap(gross=5_000.0))
    assert ledger.record_session_end(END_FLAT)["peak_gross_exposure_usd"] is None


def test_a_non_usd_position_makes_the_peak_unknown(world, ledger):
    ledger.observe_snapshot(snap(gross=1_000.0, currency="EUR"))
    assert ledger.record_session_end(END_FLAT)["peak_gross_exposure_usd"] is None
    assert "NON_USD_POSITION" in _kinds(world)


def test_session_without_any_snapshot_observation_has_unknown_peak_not_zero(world, ledger):
    assert ledger.record_session_end(END_FLAT)["peak_gross_exposure_usd"] is None


def test_observations_outside_a_session_or_without_an_armed_experiment_are_ignored(world, ledger):
    world.clock[0] = dt.datetime(2026, 10, 10, 15, 0, tzinfo=UTC)        # Saturday
    ledger.observe_snapshot(snap(gross=1_000.0))
    world.clock[0] = IN_SESSION
    with_experiment(world, state="STOPPED", stopped_at=START)
    ledger.observe_snapshot(snap(gross=1_000.0))
    assert world.store.fetch("equity_session_peak", {}) == []


def test_late_commission_becomes_an_adjustment_and_the_row_is_untouched(world, ledger):
    world.round_trip(buy=("e1", 10, 100, None))
    row = ledger.record_session_end(END_FLAT)
    assert row["commissions_usd"] is None
    set_commission(world.db, ACCOUNT, "e1", 1.00)
    assert ledger.reconcile_commissions() == 1 and ledger.reconcile_commissions() == 0
    assert _rows(world)[0] == row
    assert world.store.fetch("equity_adjustments", {})[0]["amount_usd"] == 1.0


def test_revised_commission_adds_only_the_delta(world, ledger):
    world.round_trip()
    ledger.record_session_end(END_FLAT)
    set_commission(world.db, ACCOUNT, "e1", 1.30)
    assert ledger.reconcile_commissions() == 1
    set_commission(world.db, ACCOUNT, "e1", 1.10)
    assert ledger.reconcile_commissions() == 1
    amounts = sorted(round(a["amount_usd"], 6) for a in world.store.fetch("equity_adjustments", {}))
    assert amounts == [-0.2, 0.3] and world.store.verify_seals() == []


def test_fill_arriving_after_the_row_is_an_incident_not_an_adjustment(world, ledger):
    ledger.record_session_end(END_FLAT)
    world.fill("late", "SELL", 1, 100, 0.0, IN_SESSION)
    ledger.reconcile_commissions()
    assert "LATE_FILL_AFTER_SESSION_ROW" in _kinds(world)
    assert world.store.fetch("equity_adjustments", {}) == []


def test_recover_after_restart_between_flat_and_row_writes_it_once(world, ledger):
    world.controller_row("FLAT")
    world.clock[0] = AFTER_CLOSE
    assert ledger.recover(AFTER_CLOSE) == [D.isoformat()] and ledger.recover(AFTER_CLOSE) == []
    assert _rows(world)[0]["end_nlv_usd"] == 100_250.0


def test_recover_leaves_a_running_session_alone(world, ledger):
    world.controller_row("OPEN")
    assert ledger.recover(IN_SESSION) == [] and _rows(world) == []


def test_a_session_without_any_terminal_record_is_unknown_never_flat(world, ledger):   # ruling 6
    world.round_trip()
    now = dt.datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
    world.clock[0] = now
    assert ledger.recover(now) == [D.isoformat()]
    row = _rows(world)[0]
    assert row["session_end_state"] == "UNKNOWN"
    assert (row["end_nlv_base"], row["end_nlv_usd"], row["realized_pnl_usd"], row["commissions_usd"],
            row["start_nlv_usd"], row["peak_gross_exposure_usd"]) == (None,) * 6
    assert (row["fill_count"], row["trade_count"]) == (2, 1)
    assert "SESSION_MISSING" in _kinds(world)
    assert world.written == []                       # an UNKNOWN row sends no summary


def test_recover_of_a_past_terminal_session_writes_an_unknown_nlv_row_and_an_incident(world, ledger):
    world.controller_row("INCIDENT")
    now = dt.datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
    assert ledger.recover(now) == [D.isoformat()]
    row = _rows(world)[0]
    assert (row["session_end_state"], row["end_nlv_base"], row["fill_count"]) == ("FAILED_SAFE", None, 0)
    assert "SESSION_ROW_RECOVERED_WITHOUT_NLV" in _kinds(world)


def test_recover_ignores_sessions_before_the_experiment(world, ledger):
    with_experiment(world, started_at=dt.datetime(2026, 10, 7, 13, 35, tzinfo=UTC))
    world.controller_row("FLAT", D)
    assert ledger.recover(dt.datetime(2026, 10, 7, 15, 0, tzinfo=UTC)) == []


def test_recover_stops_at_the_experiment_stop(world, ledger):
    with_experiment(world, state="STOPPED", stopped_at=END)
    world.controller_row("FLAT", D)
    assert ledger.recover(dt.datetime(2026, 10, 9, 15, 0, tzinfo=UTC)) == [D.isoformat()]


def test_on_row_written_failure_does_not_lose_the_row(world, caplog):
    def boom(experiment_id, session_date):
        raise RuntimeError("outbox down")
    with caplog.at_level(logging.ERROR):
        row = world.ledger(on_row_written=boom).record_session_end(END_FLAT)
    assert row is not None and len(_rows(world)) == 1 and "outbox down" in caplog.text


def test_rows_stay_sealed(world, ledger):
    world.round_trip()
    ledger.record_session_end(END_FLAT)
    assert world.store.verify_seals() == []
