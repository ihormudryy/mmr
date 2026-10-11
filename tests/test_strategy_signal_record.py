"""SP2 Plan 1 Task 6: the durable strategy signal record (spec amendment 6.1)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from tests.automation.test_controller_epoch import Clock
from tests.test_signal_proposer import _frame, _make_runtime
from trader.data.duckdb_store import DuckDBConnection
from trader.data.event_store import EventType
from trader.data.strategy_signal_record import (
    RECORD_GENERATION, SOURCE_EVENT_ID, SignalCursorAhead, SignalEntry, StrategySignalRecord,
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
                         "signal_time", "recorded_at", "deployment_digest", "deployment_version",
                         "source_digest"}
    assert (view["deployment_digest"], view["deployment_version"], view["source_digest"]) == (None, None, None)
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


def test_the_record_says_which_signals_it_already_has(record):
    held, absent = entry(1), entry(2)
    record.append(held)
    assert record.recorded_source_event_ids([held.source_event_id, absent.source_event_id]) == {held.source_event_id}
    assert record.recorded_source_event_ids([]) == frozenset()


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


class FlakyRecord:
    """A real record whose first append fails, before or after the row is written."""

    def __init__(self, inner, *, fail_after_write):
        self.inner, self.fail_after_write, self.failures_left = inner, fail_after_write, 1

    def append(self, entry):
        if self.failures_left and not self.fail_after_write:
            self.failures_left -= 1
            raise OSError("duckdb file is locked")
        cursor = self.inner.append(entry)
        if self.failures_left:
            self.failures_left -= 1
            raise OSError("connection dropped after commit")
        return cursor

    def read(self, after_cursor, limit):
        return self.inner.read(after_cursor, limit)

    def recorded_source_event_ids(self, source_event_ids):
        return self.inner.recorded_source_event_ids(source_event_ids)


def ticking_runtime(tmp_path, clock, strategy, *, fail_after_write):
    rt = _make_runtime(tmp_path)
    real = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "s.duckdb")), now=clock)
    rt.signal_record = FlakyRecord(real, fail_after_write=fail_after_write)
    strategy.enable()
    rt.strategy_implementations.append(strategy)
    rt.current_frame = _frame(last_time="2026-10-07 14:30")
    rt._strategy_frame = lambda conId, bar_size: rt.current_frame
    calls = []
    real_on_prices = strategy.on_prices
    strategy.on_prices = lambda frame: calls.append(frame.index[-1]) or real_on_prices(frame)
    return rt, calls


def recorded(rt):
    return [(s["cursor"], s["signal_time"]) for s in (x.to_json() for x in rt.signal_record.read(0, 10).signals)]


def test_a_failed_append_is_retried_before_the_next_bar(tmp_path, installed_strategy, clock):   # PR #78 thread
    rt, on_prices_calls = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=False)
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # bar 14:30: append fails
    assert recorded(rt) == [] and rt.zmq_messagebus_client.written == []
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # same bar: retried, not re-evaluated
    assert recorded(rt) == [(1, "2026-10-07T14:30:00+00:00")]
    rt.current_frame = _frame(last_time="2026-10-07 14:31")
    rt._on_tick_for_strategy(installed_strategy, 4391)
    assert recorded(rt) == [(1, "2026-10-07T14:30:00+00:00"), (2, "2026-10-07T14:31:00+00:00")]
    assert len(on_prices_calls) == 2                                         # each bar seen once
    assert len(rt.event_store.events) == 2 and len(rt.zmq_messagebus_client.written) == 2


def test_a_new_bar_supersedes_a_signal_held_by_a_failed_append(tmp_path, installed_strategy, clock):   # issue #140
    rt, _ = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=False)
    rt.signal_record.failures_left = 2
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # 14:30 fails
    rt.current_frame = _frame(last_time="2026-10-07 14:31")
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # 14:30 is STALE; 14:31 fails
    assert recorded(rt) == []
    rt._on_tick_for_strategy(installed_strategy, 4391)
    assert recorded(rt) == [(1, "2026-10-07T14:31:00+00:00")]
    assert len(rt.zmq_messagebus_client.written) == 1
    assert [e.metadata["reason"] for e in rt.event_store.events if e.event_type == EventType.SIGNAL_GAP] == ["STALE"]


def test_an_append_that_wrote_and_then_failed_is_not_duplicated(tmp_path, installed_strategy, clock):
    rt, _ = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=True)
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # row written, call raised
    rt._on_tick_for_strategy(installed_strategy, 4391)                      # retry finds the same row
    rt._on_tick_for_strategy(installed_strategy, 4391)
    assert recorded(rt) == [(1, "2026-10-07T14:30:00+00:00")]
    assert len(rt.event_store.events) == 1 and len(rt.zmq_messagebus_client.written) == 1


def test_a_failure_after_the_record_is_not_retried(tmp_path, installed_strategy, clock):
    rt, _ = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=False)
    rt.signal_record.failures_left = 0

    def bus_down(topic, payload):
        raise ConnectionError("bus down")
    rt.zmq_messagebus_client.write = bus_down
    rt._on_tick_for_strategy(installed_strategy, 4391)
    rt._on_tick_for_strategy(installed_strategy, 4391)
    assert recorded(rt) == [(1, "2026-10-07T14:30:00+00:00")]
    assert len(rt.event_store.events) == 1                                   # the signal is not replayed


def test_each_record_has_a_durable_generation(tmp_path, clock):              # PR #84 thread 4210304622
    path = str(tmp_path / "gen.duckdb")
    first = StrategySignalRecord(DuckDBConnection.get_instance(path), now=clock)
    generation = first.read(after_cursor=0, limit=1).record_generation
    assert RECORD_GENERATION.fullmatch(generation)
    first.append(entry(0))
    reopened = StrategySignalRecord(DuckDBConnection.get_instance(path), now=clock)
    assert reopened.read(after_cursor=0, limit=1).record_generation == generation      # a restart keeps it


def test_a_replaced_record_with_the_same_high_water_has_a_new_generation(tmp_path, clock):
    old = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "old.duckdb")), now=clock)
    new = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "new.duckdb")), now=clock)
    for record, strategy in ((old, "orb"), (new, "momentum")):
        for minute in range(5):
            record.append(entry(minute, strategy=strategy))
    old_page, new_page = old.read(after_cursor=5, limit=10), new.read(after_cursor=5, limit=10)
    assert (old_page.next_cursor, new_page.next_cursor) == (5, 5)              # equal high water
    assert old_page.record_generation != new_page.record_generation


BASE, VERSION, SOURCE = ("sha256:" + c * 64 for c in "bde")


def bound_entry(**overrides):
    fields = dict(strategy_name="aidv-0123456789abcdef", conid=265598, action="BUY", probability=0.5,
                  signal_time=T0, deployment_digest=BASE, deployment_version=VERSION, source_digest=SOURCE)
    return SignalEntry.create(**{**fields, **overrides})


def test_the_deployment_binding_round_trips(record):
    record.append(bound_entry())
    (signal,) = record.read(0, 10).signals
    assert (signal.entry.deployment_digest, signal.entry.deployment_version, signal.entry.source_digest) == (
        BASE, VERSION, SOURCE)
    assert signal.to_json()["deployment_version"] == VERSION


@pytest.mark.parametrize("missing", ["deployment_digest", "deployment_version", "source_digest"])
def test_a_partial_binding_is_refused(missing):
    with pytest.raises(ValueError, match="all set"):
        bound_entry(**{missing: None})


@pytest.mark.parametrize("field", ["deployment_digest", "deployment_version", "source_digest"])
def test_a_binding_that_is_not_a_digest_is_refused(field):
    with pytest.raises(ValueError, match="all set"):
        bound_entry(**{field: "sha256:abc"})
