"""Issue #80: held strategy signals of a disabled strategy, and a bounded hold when the record keeps failing."""
from __future__ import annotations

import asyncio
import logging

import pytest

from tests.test_signal_proposer import _frame
from tests.test_strategy_signal_record import T0, ticking_runtime, recorded
from tests.automation.test_controller_epoch import Clock
from trader.data.event_store import EventType
from trader.data.strategy_signal_record import source_event_id_for
from trader.strategy.signal_hold import MAX_HELD_SIGNALS

CONID = 4391
ALWAYS = 10 ** 6


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def held(tmp_path, installed_strategy, clock):
    """A runtime whose record failed the 14:30 BUY of a propose-mode strategy, so that signal is held."""
    installed_strategy.ctx.auto_execute = 'propose'
    rt, _ = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=False)
    rt._schedule_persist_enabled = lambda name, enabled: None
    rt._announce_and_drain = lambda name: None
    rt._reconcile_sync = lambda: None
    rt.signal_proposer.expire_stale = lambda: None
    rt.signal_record.failures_left = ALWAYS
    rt._on_tick_for_strategy(installed_strategy, CONID)
    assert rt._signal_hold.held_count((CONID, installed_strategy.name)) == 1
    return rt, installed_strategy


def gaps(rt):
    return [e for e in rt.event_store.events if e.event_type == EventType.SIGNAL_GAP]


def nothing_acted_on(rt):
    return recorded(rt) == [] and rt.zmq_messagebus_client.written == [] and rt.signal_proposer.signals == []


def errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_held_signal_of_a_disabled_strategy_is_settled_on_reconcile_as_a_gap(held):
    rt, strategy = held
    rt.disable_strategy(strategy.name)
    rt.signal_record.failures_left = 0                                    # the record is writable again
    asyncio.run(rt._reconcile())                                          # no tick reaches a disabled strategy
    assert nothing_acted_on(rt)
    [gap] = gaps(rt)
    assert gap.strategy_name == strategy.name and gap.conid == CONID and gap.action == ''
    assert gap.metadata == {
        'reason': 'STRATEGY_DISABLED', 'action': 'BUY', 'signal_time': '2026-10-07T14:30:00+00:00',
        'source_event_id': source_event_id_for(strategy.name, CONID, 'BUY', T0.replace(minute=30))}
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 0


def test_a_disable_and_re_enable_between_retries_still_never_dispatches_the_held_signal(held):
    rt, strategy = held
    rt.disable_strategy(strategy.name)
    rt.enable_strategy(strategy.name)
    rt.signal_record.failures_left = 0
    rt._on_tick_for_strategy(strategy, CONID)                             # same bar: only the retry runs
    assert nothing_acted_on(rt)
    assert [g.metadata['reason'] for g in gaps(rt)] == ['STRATEGY_DISABLED']


def test_a_held_signal_of_an_unloaded_strategy_is_settled_as_a_gap(held):
    rt, strategy = held
    rt.unload_strategy(strategy.name)
    rt.signal_record.failures_left = 0
    rt._retry_held_signals()
    assert nothing_acted_on(rt)
    assert [g.metadata['reason'] for g in gaps(rt)] == ['STRATEGY_UNLOADED']


def test_a_disabled_strategy_keeps_its_signal_held_until_the_gap_can_be_written(held):
    rt, strategy = held
    rt.disable_strategy(strategy.name)
    events, rt.event_store.append = rt.event_store.append, _raise_disk_full
    rt._retry_held_signals()
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1
    rt.event_store.append = events
    rt._retry_held_signals()
    assert nothing_acted_on(rt) and [g.metadata['reason'] for g in gaps(rt)] == ['STRATEGY_DISABLED']


def _raise_disk_full(event):
    raise OSError('No space left on device')


def tick_bars(rt, strategy, minutes, ticks_per_bar=3):
    for minute in minutes:
        rt.current_frame = _frame(last_time=f'2026-10-07 14:{minute:02d}')
        for _ in range(ticks_per_bar):
            rt._on_tick_for_strategy(strategy, CONID)


def test_a_record_that_keeps_failing_holds_at_most_the_cap_and_raises_one_incident(held, caplog):
    rt, strategy = held
    rt.event_store.append = _raise_disk_full                              # nothing can be written at all
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 3))
        for _ in range(5):
            rt._retry_held_signals()
    assert rt._signal_hold.held_count((CONID, strategy.name)) == MAX_HELD_SIGNALS
    [incident] = errors(caplog)                                           # the hold-start ERROR came before
    assert incident.getMessage().startswith('SIGNAL_HOLD_FULL')
    assert nothing_acted_on(rt)


def test_recovery_writes_the_held_signals_in_order_and_one_gap_for_the_dropped(held, caplog):
    rt, strategy = held
    events = rt.event_store.append
    rt.event_store.append = _raise_disk_full
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 3))         # 14:30..14:37 held, 14:38..14:41 dropped
    rt.event_store.append = events
    rt.signal_record.failures_left = 0
    caplog.clear()
    rt._on_tick_for_strategy(strategy, CONID)
    assert [t for _, t in recorded(rt)] == [f'2026-10-07T14:{m:02d}:00+00:00' for m in range(30, 30 + MAX_HELD_SIGNALS)]
    [gap] = gaps(rt)
    assert gap.metadata == {'reason': 'HOLD_FULL', 'count': 4, 'first_signal_time': '2026-10-07T14:38:00+00:00',
                            'last_signal_time': '2026-10-07T14:41:00+00:00'}
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 0
    assert errors(caplog) == []
    assert len(rt.signal_proposer.signals) == MAX_HELD_SIGNALS            # an enabled strategy's held signals act
    rt.current_frame = _frame(last_time='2026-10-07 14:42')                # the next signal is dispatched at once
    rt._on_tick_for_strategy(strategy, CONID)
    assert recorded(rt)[-1][1] == '2026-10-07T14:42:00+00:00'


def test_a_full_hold_writes_each_dropped_signal_as_a_gap_while_events_can_be_written(held):
    rt, strategy = held
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 1))
    assert [g.metadata['count'] for g in gaps(rt)] == [1, 1]
    assert rt._signal_hold.held_count((CONID, strategy.name)) == MAX_HELD_SIGNALS
