"""SP2 Plan 1 Task 6: the durable strategy signal record (spec amendment 6.1)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from tests.automation.test_controller_epoch import Clock
from tests.test_signal_proposer import _frame, _make_runtime
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import (
    SOURCE_EVENT_ID, SignalCursorAhead, SignalEntry, StrategySignalRecord,
)
from trader.objects import Action
from trader.trading.strategy import Signal

T0 = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def record(tmp_path, clock):
    return StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "mmr.duckdb")),
                                retention_days=7, now=clock)


def entry(minute=0, action="BUY", conid=265598, probability=0.7, strategy="orb"):
    return SignalEntry.create(strategy_name=strategy, conid=conid, action=action, probability=probability,
                              signal_time=T0 + dt.timedelta(minutes=minute))


def test_cursors_are_monotonic_and_reads_page(record):
    assert [record.append(entry(m)) for m in range(5)] == [1, 2, 3, 4, 5]
    page = record.read(after_cursor=1, limit=2)
    assert [s.cursor for s in page.signals] == [2, 3]
    assert (page.next_cursor, page.oldest_retained_cursor, page.gap) == (3, 1, False)
    view = page.signals[0].to_json()
    assert set(view) == {"cursor", "source_event_id", "strategy_name", "conid", "action", "probability",
                         "signal_time", "recorded_at"}
    assert SOURCE_EVENT_ID.fullmatch(view["source_event_id"]) and view["conid"] == 265598
    assert view["signal_time"] == (T0 + dt.timedelta(minutes=1)).isoformat()


def test_a_redelivered_signal_is_the_same_row(record):
    assert record.append(entry(0)) == 1
    assert record.append(entry(0)) == 1
    assert record.append(entry(0, action="SELL")) == 2
    assert len(record.read(0, 500).signals) == 2


def test_gap_only_after_real_pruning(record, clock):                        # Review Focus 4
    for m in range(3):
        record.append(entry(m))
    clock.now = T0 + dt.timedelta(days=8)
    record.append(entry(10_000))                                            # prunes cursors 1..3
    page = record.read(after_cursor=1, limit=10)
    assert page.gap is True and [s.cursor for s in page.signals] == [4]
    assert page.oldest_retained_cursor == 4
    assert record.read(after_cursor=3, limit=10).gap is False


def test_rolled_back_append_leaves_no_hole(record, tmp_path):              # Review Focus 4
    record.append(entry(0))
    db = DuckDBConnection.get_instance(str(tmp_path / "mmr.duckdb"))

    def append_then_fail(conn):
        record.append_in_tx(conn, entry(1))
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        db.transaction(append_then_fail)
    assert record.append(entry(2)) == 2
    assert record.read(after_cursor=0, limit=10).gap is False


def test_empty_record_reports_the_next_cursor(record):
    page = record.read(after_cursor=0, limit=10)
    assert (page.signals, page.next_cursor, page.oldest_retained_cursor, page.gap) == ((), 0, 1, False)


def test_cursor_ahead_of_the_record_is_refused(record):                     # Review Focus 4
    record.append(entry(0))
    with pytest.raises(SignalCursorAhead):
        record.read(after_cursor=2, limit=10)


@pytest.mark.parametrize("after,limit", [(-1, 10), (0, 0), (0, 501), (True, 10), (0, True), (0.0, 10)])
def test_read_arguments_are_strict(record, after, limit):
    with pytest.raises(ValueError):
        record.read(after_cursor=after, limit=limit)


@pytest.mark.parametrize("changes", [{"action": "NEUTRAL"}, {"conid": 0}, {"conid": True}, {"conid": "265598"},
                                     {"strategy": ""}])
def test_entries_are_strict(changes):
    kwargs = {"minute": 0, **changes}
    with pytest.raises(ValueError):
        entry(**kwargs)


def test_non_finite_probability_is_stored_as_null(record):
    record.append(entry(0, probability=math.nan))
    assert record.read(0, 1).signals[0].to_json()["probability"] is None


@pytest.mark.parametrize("days", [0, 366, True, 7.0])
def test_retention_days_are_checked(tmp_path, days):
    with pytest.raises(ValueError):
        StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "x.duckdb")), retention_days=days)


def test_dispatch_records_buy_and_sell_but_not_neutral(tmp_path, installed_strategy, clock):
    rt = _make_runtime(tmp_path)
    rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "s.duckdb")), now=clock)
    frame = _frame(last_time="2026-10-07 14:30")
    for action in (Action.BUY, Action.NEUTRAL, Action.SELL, Action.BUY):
        rt._dispatch_signal(installed_strategy, Signal(source_name="x", action=action, probability=0.6, risk=0.1),
                            conId=4391, frame=frame)
    signals = [s.to_json() for s in rt.signal_record.read(0, 10).signals]
    assert [(s["action"], s["conid"], s["strategy_name"]) for s in signals] == [
        ("BUY", 4391, installed_strategy.name), ("SELL", 4391, installed_strategy.name)]
    assert signals[0]["signal_time"] == "2026-10-07T14:30:00+00:00"
    assert len(rt.event_store.events) == 4                                   # trading_events unchanged
