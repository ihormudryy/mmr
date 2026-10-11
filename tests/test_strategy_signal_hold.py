"""Issue #80: held strategy signals of a disabled strategy, and a bounded hold when the record keeps failing."""
from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from tests.test_signal_proposer import _frame
from tests.test_strategy_signal_record import T0, ticking_runtime, recorded
from tests.automation.test_controller_epoch import Clock
from trader.data.event_store import EventType
from trader.data.strategy_signal_record import source_event_id_for
from trader.strategy.signal_hold import MAX_HELD_SIGNALS
from trader.trading.strategy import StrategyState

CONID = 4391
ALWAYS = 10 ** 6
WAIT = 10          # seconds; only a guard against a hang, never part of an assertion's timing


@pytest.fixture
def clock():
    return Clock(T0)


def holding_runtime(tmp_path, strategy, clock, *, fail_after_write):
    """A runtime whose record failed the 14:30 BUY of a propose-mode strategy, so that signal is held."""
    strategy.ctx.auto_execute = 'propose'
    rt, _ = ticking_runtime(tmp_path, clock, strategy, fail_after_write=fail_after_write)
    rt._schedule_persist_enabled = lambda name, enabled: None
    rt._announce_and_drain = lambda name: None
    rt._reconcile_sync = lambda: None
    rt.signal_proposer.expire_stale = lambda: None
    rt.signal_record.failures_left = ALWAYS
    rt._on_tick_for_strategy(strategy, CONID)
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1
    return rt, strategy


@pytest.fixture
def held(tmp_path, installed_strategy, clock):
    return holding_runtime(tmp_path, installed_strategy, clock, fail_after_write=False)


@pytest.fixture
def held_after_commit(tmp_path, installed_strategy, clock):
    """The append committed the 14:30 row and then raised, so the runtime holds a signal the record has."""
    return holding_runtime(tmp_path, installed_strategy, clock, fail_after_write=True)


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
        'source_event_id': source_event_id_for(strategy.name, CONID, 'BUY', T0.replace(minute=30)),
        'recorded': False}
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


def test_recovery_dispatches_the_newest_held_signal_after_one_gap_for_the_evicted_and_stale_gaps(held, caplog):
    rt, strategy = held
    events = rt.event_store.append
    rt.event_store.append = _raise_disk_full                              # both stores are down
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 3))         # 14:30..14:33 evicted, 14:34..14:41 held
    assert rt._signal_hold.held_count((CONID, strategy.name)) == MAX_HELD_SIGNALS
    rt.event_store.append = events
    rt.signal_record.failures_left = 0
    caplog.clear()
    seen = watch_side_effects(rt, strategy)
    rt._on_tick_for_strategy(strategy, CONID)                             # the latest bar is still 14:41
    evicted, *stale = gaps(rt)
    assert evicted.metadata == {'reason': 'HOLD_FULL', 'count': 4, 'first_signal_time': '2026-10-07T14:30:00+00:00',
                                'last_signal_time': '2026-10-07T14:33:00+00:00', 'recorded_count': 0}
    assert [(g.metadata['reason'], g.metadata['signal_time']) for g in stale] == [
        ('STALE', f'2026-10-07T14:{m:02d}:00+00:00') for m in range(34, 41)]
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:41:00+00:00']   # the newest signal is on the latest bar
    assert [kind for kind, _ in seen] == ['record', 'publish', 'proposal']
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 0
    assert errors(caplog) == []
    rt.current_frame = _frame(last_time='2026-10-07 14:42')                # the next signal is dispatched at once
    rt._on_tick_for_strategy(strategy, CONID)
    assert [t for _, t in recorded(rt)][-1] == '2026-10-07T14:42:00+00:00'


def test_a_record_that_keeps_failing_never_fills_the_hold_while_events_can_be_written(held):
    rt, strategy = held
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 1))
    assert [g.metadata['reason'] for g in gaps(rt)] == ['STALE'] * (MAX_HELD_SIGNALS + 1)
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1        # only the latest bar's signal waits
    assert nothing_acted_on(rt)


# -- a held signal superseded by a newer completed bar (issue #140) --

def test_a_held_signal_retried_after_a_newer_bar_completed_is_a_stale_gap(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    rt.current_frame = _frame(last_time='2026-10-07 14:31')
    rt._retry_held_signals()                                              # the reconcile retry
    assert nothing_acted_on(rt)
    [gap] = gaps(rt)
    assert gap.strategy_name == strategy.name and gap.conid == CONID and gap.action == ''
    assert gap.metadata == {
        'reason': 'STALE', 'action': 'BUY', 'signal_time': '2026-10-07T14:30:00+00:00',
        'source_event_id': source_event_id_for(strategy.name, CONID, 'BUY', T0.replace(minute=30)),
        'recorded': False, 'latest_bar_time': '2026-10-07T14:31:00+00:00'}
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 0


def test_the_first_tick_of_a_newer_bar_writes_the_held_signal_as_stale_and_dispatches_the_new_one(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    rt.current_frame = _frame(last_time='2026-10-07 14:31')
    rt._on_tick_for_strategy(strategy, CONID)                             # the retry runs before the new bar
    assert [g.metadata['signal_time'] for g in gaps(rt)] == ['2026-10-07T14:30:00+00:00']
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:31:00+00:00']
    assert len(rt.zmq_messagebus_client.written) == 1 and len(rt.signal_proposer.signals) == 1


def test_a_held_signal_retried_within_its_own_bar_is_dispatched(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    rt._retry_held_signals()
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:30:00+00:00']
    assert len(rt.zmq_messagebus_client.written) == 1 and len(rt.signal_proposer.signals) == 1
    assert gaps(rt) == []


def test_of_several_held_signals_only_the_latest_bar_is_dispatched_after_the_stale_gaps_in_order(held):
    rt, strategy = held
    events = rt.event_store.append
    rt.event_store.append = _raise_disk_full                              # the STALE gaps cannot be written yet
    tick_bars(rt, strategy, [31, 32])
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 3
    rt.event_store.append = events
    rt.signal_record.failures_left = 0
    seen = watch_side_effects(rt, strategy)
    rt._retry_held_signals()
    assert [(g.metadata['reason'], g.metadata['signal_time']) for g in gaps(rt)] == [
        ('STALE', '2026-10-07T14:30:00+00:00'), ('STALE', '2026-10-07T14:31:00+00:00')]
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:32:00+00:00']
    assert [kind for kind, _ in seen] == ['record', 'publish', 'proposal']


def test_a_stale_signal_stays_held_until_its_gap_can_be_written(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    rt.current_frame = _frame(last_time='2026-10-07 14:31')
    events, rt.event_store.append = rt.event_store.append, _raise_disk_full
    rt._retry_held_signals()
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1 and nothing_acted_on(rt)
    rt.event_store.append = events
    rt._retry_held_signals()
    assert nothing_acted_on(rt) and [g.metadata['reason'] for g in gaps(rt)] == ['STALE']
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 0


def _no_frame(conid, bar_size):
    return None


def _unreadable_frame(conid, bar_size):
    raise OSError('history store unreadable')


@pytest.mark.parametrize('latest_frame', [_no_frame, _unreadable_frame])
def test_a_held_signal_whose_latest_bar_cannot_be_read_stays_held(held, latest_frame):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    frame = rt._strategy_frame
    rt._strategy_frame = latest_frame
    rt._signal_hold.retry((CONID, strategy.name))                         # the tick path's retry: must not raise
    rt._retry_held_signals()
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1
    assert nothing_acted_on(rt) and gaps(rt) == []
    rt._strategy_frame = frame                                            # still the 14:30 bar
    rt._retry_held_signals()
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:30:00+00:00']


# -- a disable from the RPC thread against a dispatch on the loop (mmr-openai review of 0818eaf5, PR #139) --

class ObservedLock:
    """The hold's dispatch lock, reporting when another thread has to wait for it and pausing the loop before it."""

    def __init__(self, *, contended, pause_thread=None, paused=None, resume=None):
        self._lock = threading.RLock()
        self.contended, self.pause_thread, self.paused, self.resume = contended, pause_thread, paused, resume

    def __enter__(self):
        if self.pause_thread is not None and threading.current_thread() is self.pause_thread:
            self.pause_thread = None
            self.paused.set()
            assert self.resume.wait(WAIT)
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


def watch_side_effects(rt, strategy):
    """Each record, publish and proposal of the dispatch, with the strategy state at that moment."""
    seen = []
    record, publish, propose = rt._record_signal, rt.zmq_messagebus_client.write, rt.signal_proposer.on_signal
    rt._record_signal = lambda *a: (record(*a), seen.append(('record', strategy.state)))
    rt.zmq_messagebus_client.write = lambda *a: (publish(*a), seen.append(('publish', strategy.state)))
    rt.signal_proposer.on_signal = lambda *a: (propose(*a), seen.append(('proposal', strategy.state)))
    return seen


def disable_on_rpc_thread(rt, strategy, done):
    thread = threading.Thread(target=lambda: (rt.disable_strategy(strategy.name), done.set()), name='rpc')
    thread.start()
    return thread


def test_a_disable_during_the_held_record_write_waits_until_the_dispatch_is_done(held):
    """OpenAI's probe: pause in _record_signal on a retry, disable from the RPC thread, resume."""
    rt, strategy = held
    rt.signal_record.failures_left = 0
    seen = watch_side_effects(rt, strategy)
    in_write, resume, disable_progressed = threading.Event(), threading.Event(), threading.Event()
    rt._signal_hold._dispatch_lock = ObservedLock(contended=disable_progressed)
    record = rt._record_signal
    rt._record_signal = lambda *a: (in_write.set(), resume.wait(WAIT), record(*a))
    loop = threading.Thread(target=rt._retry_held_signals, name='loop')
    loop.start()
    assert in_write.wait(WAIT)
    rpc = disable_on_rpc_thread(rt, strategy, disable_progressed)
    assert disable_progressed.wait(WAIT)                    # the disable finished, or it waits for the dispatch
    resume.set()
    loop.join(WAIT)
    rpc.join(WAIT)
    running = StrategyState.RUNNING
    assert seen == [('record', running), ('publish', running), ('proposal', running)]   # all before the disable
    assert strategy.state == StrategyState.DISABLED and gaps(rt) == []


def test_a_disable_before_the_held_dispatch_takes_the_lock_makes_a_gap_and_no_dispatch(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    paused, resume, disabled = threading.Event(), threading.Event(), threading.Event()
    loop = threading.Thread(target=rt._retry_held_signals, name='loop')
    rt._signal_hold._dispatch_lock = ObservedLock(contended=threading.Event(), pause_thread=loop,
                                                  paused=paused, resume=resume)
    loop.start()
    assert paused.wait(WAIT)                                # the retry is about to check the strategy
    disable_on_rpc_thread(rt, strategy, disabled).join(WAIT)
    assert disabled.is_set()
    resume.set()
    loop.join(WAIT)
    assert nothing_acted_on(rt)
    assert [g.metadata['reason'] for g in gaps(rt)] == ['STRATEGY_DISABLED']


def test_a_disable_while_a_fresh_signal_is_computed_makes_a_gap_and_no_dispatch(tmp_path, installed_strategy, clock):
    installed_strategy.ctx.auto_execute = 'propose'
    rt, _ = ticking_runtime(tmp_path, clock, installed_strategy, fail_after_write=False)
    rt.signal_record.failures_left = 0
    rt._schedule_persist_enabled = lambda name, enabled: None
    rt._announce_and_drain = lambda name: None
    on_prices = installed_strategy.on_prices

    def disabled_mid_bar(frame):
        disable_on_rpc_thread(rt, installed_strategy, threading.Event()).join(WAIT)
        return on_prices(frame)
    installed_strategy.on_prices = disabled_mid_bar
    rt._on_tick_for_strategy(installed_strategy, CONID)
    assert nothing_acted_on(rt)
    assert [g.metadata['reason'] for g in gaps(rt)] == ['STRATEGY_DISABLED']


# -- the gap says whether the record already has the signal (mmr-openai review of 53255a17, PR #145) --

def published_or_proposed(rt):
    return rt.zmq_messagebus_client.written != [] or rt.signal_proposer.signals != []


def _supersede_with_a_newer_bar(rt, strategy):
    rt.current_frame = _frame(last_time='2026-10-07 14:31')


def _disable(rt, strategy):
    rt.disable_strategy(strategy.name)


@pytest.mark.parametrize('block, reason', [(_supersede_with_a_newer_bar, 'STALE'), (_disable, 'STRATEGY_DISABLED')])
def test_a_gap_for_a_signal_whose_append_committed_says_it_was_recorded(held_after_commit, block, reason):
    rt, strategy = held_after_commit
    rt.signal_record.failures_left = 0
    block(rt, strategy)
    rt._retry_held_signals()
    assert not published_or_proposed(rt)
    [gap] = gaps(rt)
    assert (gap.metadata['reason'], gap.metadata['recorded']) == (reason, True)
    assert recorded(rt) == [(1, '2026-10-07T14:30:00+00:00')]           # the AI intake judges this row by its age


def test_a_held_signal_stays_held_while_the_record_cannot_say_whether_it_has_it(held):
    rt, strategy = held
    rt.signal_record.failures_left = 0
    rt.current_frame = _frame(last_time='2026-10-07 14:31')
    lookup = rt.signal_record.recorded_source_event_ids
    rt.signal_record.recorded_source_event_ids = _raise_disk_full
    rt._signal_hold.retry((CONID, strategy.name))                         # the tick path's retry: must not raise
    rt._retry_held_signals()
    assert rt._signal_hold.held_count((CONID, strategy.name)) == 1
    assert nothing_acted_on(rt) and gaps(rt) == []
    rt.signal_record.recorded_source_event_ids = lookup
    rt._retry_held_signals()
    assert [(g.metadata['reason'], g.metadata['recorded']) for g in gaps(rt)] == [('STALE', False)]


def test_the_evicted_gap_counts_an_evicted_signal_whose_append_committed(held_after_commit):
    rt, strategy = held_after_commit
    events = rt.event_store.append
    rt.event_store.append = _raise_disk_full
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS))             # 14:30 (committed) is evicted
    rt.event_store.append = events
    rt.signal_record.failures_left = 0
    rt._retry_held_signals()
    evicted, *_ = gaps(rt)
    assert (evicted.metadata['reason'], evicted.metadata['count'], evicted.metadata['recorded_count']) == (
        'HOLD_FULL', 1, 1)


def _fail_hold_full_gaps(append):
    def append_unless_hold_full(event):
        if event.event_type == EventType.SIGNAL_GAP and event.metadata.get('reason') == 'HOLD_FULL':
            raise OSError('No space left on device')
        return append(event)
    return append_unless_hold_full


def test_nothing_held_or_newer_acts_until_the_evicted_gap_is_written(held):
    rt, strategy = held
    events = rt.event_store.append
    rt.event_store.append = _raise_disk_full                              # both stores are down
    tick_bars(rt, strategy, range(31, 31 + MAX_HELD_SIGNALS + 3))         # 14:30..14:33 evicted, 14:34..14:41 held
    rt.event_store.append = _fail_hold_full_gaps(events)                  # only the evicted gap still fails
    rt.signal_record.failures_left = 0
    seen = watch_side_effects(rt, strategy)
    rt._on_tick_for_strategy(strategy, CONID)
    rt._retry_held_signals()
    assert nothing_acted_on(rt) and gaps(rt) == [] and seen == []
    rt.current_frame = _frame(last_time='2026-10-07 14:42')                # a newer signal waits too, evicting 14:34
    rt._on_tick_for_strategy(strategy, CONID)
    assert nothing_acted_on(rt) and gaps(rt) == [] and seen == []
    rt.event_store.append = events
    rt._retry_held_signals()
    evicted, *stale = gaps(rt)
    assert evicted.metadata == {'reason': 'HOLD_FULL', 'count': 5, 'first_signal_time': '2026-10-07T14:30:00+00:00',
                                'last_signal_time': '2026-10-07T14:34:00+00:00', 'recorded_count': 0}
    assert [(g.metadata['reason'], g.metadata['signal_time']) for g in stale] == [
        ('STALE', f'2026-10-07T14:{m:02d}:00+00:00') for m in range(35, 42)]
    assert [t for _, t in recorded(rt)] == ['2026-10-07T14:42:00+00:00']
    assert [kind for kind, _ in seen] == ['record', 'publish', 'proposal']
