import datetime as dt

import pytest

from tests.scoreboard.ingest_world import (STARTED, FakeDecisions, FakeTrips, close_fact, enter_fact, make_ingest,
                                           matched_body, sim)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.bar_sources import BarSourceError
from trader.scoreboard.ports import CloseFill, TripFact
from trader.scoreboard.session_simulator import GRACE, SessionSimulator
from trader.scoreboard.simulator import Bar

UTC = dt.timezone.utc
SESSION = dt.date(2026, 10, 6)


def minute_bars(day, hhmm_list, open_=100.0):
    out = []
    for hhmm in hhmm_list:
        hour, minute = divmod(hhmm, 100)
        out.append(Bar(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC), open_, open_ + 0.3,
                       open_ - 0.3, open_))
    return out


QUIET = [1431, 1500, 1530, 1600, 1630, 1700, 1730, 1800, 1830, 1900, 1930, 1945]


class FakeSource:
    def __init__(self, name, bars=(), error=None):
        self.name, self.bars_, self.error, self.calls = name, list(bars), error, 0

    def bars(self, conid, start, end):
        self.calls += 1
        if self.error:
            raise BarSourceError(self.error)
        return [b for b in self.bars_ if start <= b.start < end]


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def ready_clock(extra=dt.timedelta(minutes=1)):
    return Clock(SessionSimulator.data_ready_at(SESSION) + extra)


def simulator(store, sources, clock):
    return SessionSimulator(store=store, calendar=XNYSCalendarPolicy(), sources=sources, now=clock)


@pytest.fixture
def ingest(store):
    return make_ingest(store)


def outcome(store):
    return store.fetch("simulated_outcomes", {})[0]


def test_nothing_runs_before_the_bars_are_ready(store, ingest):
    sim(ingest)
    clock = Clock(SessionSimulator.data_ready_at(SESSION) - dt.timedelta(minutes=1))
    source = FakeSource("alpaca", minute_bars(SESSION, QUIET))
    assert simulator(store, [source], clock).run_due() == 0 and source.calls == 0


def test_a_complete_outcome_is_sealed_with_its_source(store, ingest):
    sim(ingest)
    local = FakeSource("history_duckdb")
    alpaca = FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))
    assert simulator(store, [local, alpaca], ready_clock()).run_due() == 1
    row = outcome(store)
    assert (row["status"], row["exit_kind"], row["bar_source"], row["pnl_usd"]) == ("COMPLETE", "FLATTEN", "alpaca", 10.0)
    assert store.verify_seals() == []


def test_the_first_complete_source_wins_and_the_second_is_not_called(store, ingest):
    sim(ingest)
    local, alpaca = FakeSource("history_duckdb", minute_bars(SESSION, QUIET)), FakeSource("alpaca")
    simulator(store, [local, alpaca], ready_clock()).run_due()
    assert outcome(store)["bar_source"] == "history_duckdb" and alpaca.calls == 0


def test_missing_bars_wait_for_the_grace_period_then_end_incomplete(store, ingest):
    sim(ingest)
    clock = ready_clock()
    runner = SessionSimulator(store=store, calendar=XNYSCalendarPolicy(), sources=[FakeSource("alpaca")],
                              now=clock, retry_after=dt.timedelta(0))
    assert runner.run_due() == 0 and store.fetch("simulated_outcomes", {}) == []
    clock.at += GRACE
    assert runner.run_due() == 1
    row = outcome(store)
    assert row["status"] == "INCOMPLETE" and row["pnl_usd"] is None and "alpaca:NO_BARS" in row["reason"]


def test_a_source_error_is_a_reason_and_never_a_secret(store, ingest):
    sim(ingest)
    clock = ready_clock(GRACE)
    runner = simulator(store, [FakeSource("history_duckdb", error="NO_HISTORY_DB"),
                               FakeSource("alpaca", error="ALPACA_ProviderError")], clock)
    runner.run_due()
    assert outcome(store)["reason"] == "history_duckdb:NO_HISTORY_DB; alpaca:ALPACA_ProviderError"


def test_no_source_at_all_is_a_stated_reason(store, ingest):
    sim(ingest)
    simulator(store, [], ready_clock(GRACE)).run_due()
    assert outcome(store)["reason"] == "NO_BAR_SOURCE"


def test_a_second_run_writes_nothing_more(store, ingest):
    sim(ingest)
    runner = simulator(store, [FakeSource("alpaca", minute_bars(SESSION, QUIET))], ready_clock())
    assert runner.run_due() == 1 and runner.run_due() == 0 and len(store.fetch("simulated_outcomes", {})) == 1


def test_the_stop_is_used_when_bars_show_it(store, ingest):
    sim(ingest)
    bars = minute_bars(SESSION, QUIET)
    bars[2] = Bar(bars[2].start, 99.0, 99.2, 97.0, 97.5)
    simulator(store, [FakeSource("alpaca", bars)], ready_clock()).run_due()
    assert (outcome(store)["exit_kind"], outcome(store)["exit_price"]) == ("STOP", 98.0)


def test_early_close_uses_the_early_flatten_start(store):
    day = dt.date(2026, 11, 27)                      # the day after Thanksgiving: close 13:00 ET, flatten 12:45 ET
    ingest = make_ingest(store)
    sim(ingest, record_id="sim-0000005", opportunity_id="sig-5", decided_at="2026-11-27T15:00:00+00:00")
    early = [1501, 1530, 1600, 1630, 1700, 1730, 1745]              # UTC; 17:45 UTC is 12:45 ET
    clock = Clock(SessionSimulator.data_ready_at(day) + dt.timedelta(minutes=1))
    simulator(store, [FakeSource("alpaca", minute_bars(day, early, 102.0))], clock).run_due()
    row = outcome(store)
    assert (row["status"], row["exit_kind"], row["exit_price"]) == ("COMPLETE", "FLATTEN", 102.0)
    assert row["exit_at"] == dt.datetime(2026, 11, 27, 17, 45, tzinfo=UTC)


# -- matched entry (Ruling 21) -------------------------------------------------------------------------------

class FakeCloseFills:
    def __init__(self, by_close):
        self.by_close = by_close

    def removed(self, round_trip_id, close_decision_id):
        return self.by_close.get(close_decision_id, CloseFill(False, None))


def _trips(entry_qty):
    return FakeTrips(**{"dec-00000001": TripFact("rt-1", 265598, STARTED + dt.timedelta(days=1), entry_qty)})


def seed_matched(store, *closes, entry_qty=10.0, wrong_trip_first=False):
    """One matched_body per (close decision id, requested quantity), ingested through the real AiIngest."""
    facts = [close_fact(decision_id, action="CLOSE" if index else "PARTIAL_CLOSE")
             for index, (decision_id, _requested) in enumerate(closes)]
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(), *facts), trips=_trips(entry_qty))
    for index, (decision_id, requested) in enumerate(closes):
        body = matched_body(record_id=f"sim-00000{20 + index}", opportunity_id=decision_id, quantity=requested)
        if wrong_trip_first and index == 1:
            refused = sim(ingest, {**body, "linked_round_trip_id": "rt-9"})
            assert refused["code"] == "MATCHED_ENTRY_TRIP_MISMATCH"
            body = {**body, "linked_round_trip_id": None}
        assert sim(ingest, body)["status"] == "INSERTED"
    store.entry_qty = entry_qty


def run(store, close_fills, extra=dt.timedelta(minutes=1)):
    runner = SessionSimulator(store=store, calendar=XNYSCalendarPolicy(),
                              sources=[FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))],
                              now=Clock(SessionSimulator.data_ready_at(SESSION) + extra),
                              close_fills=close_fills, trips=_trips(getattr(store, "entry_qty", 10.0)))
    return runner.run_due()

def test_matched_records_simulate_only_proven_close_shares(store):            # second PR #75 review
    seed_matched(store, ("dec-00000031", 5))                                    # PARTIAL_CLOSE asked for 5
    run(store, FakeCloseFills({"dec-00000031": CloseFill(True, 3)}))            # the broker shows 3 removed
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["quantity"], outcome["pnl_usd"]) == ("COMPLETE", 3, 3.0)


def test_an_unproven_close_fill_is_incomplete_after_the_grace(store):
    seed_matched(store, ("dec-00000031", 5))
    run(store, FakeCloseFills({"dec-00000031": CloseFill(False, None)}), extra=dt.timedelta(minutes=1))
    assert store.fetch("simulated_outcomes", {}) == []                          # inside the grace: wait
    run(store, FakeCloseFills({"dec-00000031": CloseFill(False, None)}), extra=GRACE)
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["reason"]) == ("INCOMPLETE", "close_fill_unproven")


def test_two_records_for_one_entry_never_both_count_the_whole_entry(store):
    # One 10-share ENTER; a refused CLOSE asked for 10, then a real CLOSE asked for 10 again. The second record
    # was sent with a changed trip id (refused) and then with none (the trader derived rt-1).
    seed_matched(store, ("dec-00000031", 10), ("dec-00000032", 10), wrong_trip_first=True)
    run(store, FakeCloseFills({"dec-00000031": CloseFill(True, 0), "dec-00000032": CloseFill(True, 10)}))
    by_close = {r["record_id"]: r for r in store.fetch("simulated_outcomes", {})}
    quantities = [o["quantity"] or 0 for o in by_close.values()]
    assert sorted(quantities) == [0, 10] and sum(quantities) == 10              # the entry, counted once
    assert {o["reason"] for o in by_close.values()} >= {"CLOSE_REMOVED_NO_SHARES"}


def test_the_per_trip_sum_is_clipped_to_the_proven_entry_fill(store):
    seed_matched(store, ("dec-00000031", 6), ("dec-00000032", 6), entry_qty=8.0)   # the ENTER filled only 8
    run(store, FakeCloseFills({"dec-00000031": CloseFill(True, 6), "dec-00000032": CloseFill(True, 6)}))
    assert sorted(o["quantity"] for o in store.fetch("simulated_outcomes", {})) == [2, 6]


def _local_session(tmp_path, what_to_show=1):
    """A complete 1-minute session in a local history file; bar 3 touches the stop (a -$20 STOP if used)."""
    import pandas as pd

    from trader.data.data_access import TickStorage
    from trader.objects import BarSize
    bars = minute_bars(SESSION, QUIET)
    bars[2] = Bar(bars[2].start, 99.0, 99.2, 97.0, 97.5)
    frame = pd.DataFrame({"open": [b.open for b in bars], "high": [b.high for b in bars],
                          "low": [b.low for b in bars], "close": [b.close for b in bars], "volume": 1000.0,
                          "bar_size": "1 min", "what_to_show": what_to_show},
                         index=pd.DatetimeIndex([b.start for b in bars]).rename("date"))
    path = str(tmp_path / "history.duckdb")
    TickStorage(path).get_tickdata(BarSize.Mins1).write(265598, frame)
    return path


def _null_column(path, column):
    import duckdb
    with duckdb.connect(path) as conn:
        conn.execute(f"UPDATE tick_data SET {column} = NULL")


def test_a_trade_price_local_session_completes(store, ingest, tmp_path):             # review 4210486721
    from trader.scoreboard.bar_sources import LocalHistoryBars
    sim(ingest)
    simulator(store, [LocalHistoryBars(_local_session(tmp_path))], ready_clock()).run_due()
    row = outcome(store)
    assert (row["status"], row["exit_kind"], row["pnl_usd"], row["bar_source"]) == (
        "COMPLETE", "STOP", -20.0, "history_duckdb")


@pytest.mark.parametrize("damage", ["midpoint", "null_what_to_show", "null_bar_size"])
def test_unproven_local_rows_never_complete_a_book(store, ingest, tmp_path, damage):  # reviews 4210486721, 4210055215
    from trader.scoreboard.bar_sources import LocalHistoryBars
    path = _local_session(tmp_path, what_to_show=2 if damage == "midpoint" else 1)
    if damage != "midpoint":
        _null_column(path, damage.removeprefix("null_"))
    sim(ingest)
    simulator(store, [LocalHistoryBars(path)], ready_clock(GRACE)).run_due()
    row = outcome(store)
    assert (row["status"], row["reason"], row["pnl_usd"]) == ("INCOMPLETE", "history_duckdb:NO_BARS", None)


def test_a_malformed_provider_frame_seals_bad_bar_after_the_grace(store, ingest):     # review 4210055549
    import pandas as pd
    from trader.scoreboard.bar_sources import AlpacaBars

    class Provider:
        calls = 0

        def get_history(self, ticker, bar_size, start, end):
            type(self).calls += 1
            index = pd.DatetimeIndex([b.start for b in minute_bars(SESSION, QUIET)]).rename("date")
            return pd.DataFrame({"open": "x", "high": 1.0, "low": 1.0, "close": 1.0}, index=index)
    source = AlpacaBars(Provider(), lambda conid: type("S", (), {"symbol": "AAPL", "secType": "STK",
                                                                  "currency": "USD"})())
    sim(ingest)
    clock = ready_clock()
    runner = SessionSimulator(store=store, calendar=XNYSCalendarPolicy(), sources=[source], now=clock,
                              retry_after=dt.timedelta(0))
    assert runner.run_due() == 0                                                 # inside the grace: no crash, wait
    clock.at += GRACE
    assert runner.run_due() == 1 and runner.run_due() == 0                      # sealed once; never retried again
    row = outcome(store)
    assert (row["status"], row["reason"]) == ("INCOMPLETE", "alpaca:BAD_BAR") and Provider.calls == 2
