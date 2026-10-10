"""Strategy signals whose durable record write failed, held for a retry (PR #78, issue #80).

A held signal is retried, oldest first, on every tick of its strategy and on the
runtime's 30 s reconcile, so a disabled strategy's signal is settled too. A held
signal whose strategy was disabled or unloaded after it fired is never dispatched:
the signal record feeds the AI controller, which may enter on it. It is written
down as a ``SIGNAL_GAP`` event instead.

A held signal is also never dispatched once it is ``STALE``: a newer bar of its
(conId, bar size) has completed since the bar it was computed on. The runtime
evaluates a strategy once per completed bar, so that newer bar has already
superseded the signal (issue #140). The check reads the strategy's current frame,
not the wall clock: a bar is the strategy's own clock. When the latest bar cannot
be read, the signal stays held and is neither dispatched nor written down.

The hold is bounded per (conId, strategy). Once full, the oldest held signal is
evicted and counted, so the newest one (the only one that can still be current)
is kept. The count is written as one ``SIGNAL_GAP`` event by the next retry that
can write an event, before the held signals are settled. A failing record logs one
ERROR when the hold starts and one ``SIGNAL_HOLD_FULL`` ERROR when it first evicts
a signal, not one per tick.

Every dispatch, fresh or held, checks that its strategy may still act and then
writes the record and runs its side effects under one lock. A disable or unload
takes the same lock, so it lands either before the check (the signal becomes a
gap) or after the side effects (the signal was dispatched while enabled), never
between them (mmr-openai review of PR #139).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

import pandas as pd

from trader.data.strategy_signal_record import completed_bar_time, source_event_id_for

MAX_HELD_SIGNALS = 8

STRATEGY_DISABLED = 'STRATEGY_DISABLED'
STRATEGY_UNLOADED = 'STRATEGY_UNLOADED'
STALE = 'STALE'
HOLD_FULL = 'HOLD_FULL'


class SignalRecordWriteFailed(Exception):
    """The durable signal record write failed or its outcome is unknown.

    The signal is kept and retried with the same ``source_event_id``, which the
    record treats as the same row (SP2 Plan 1, PR #78 review).
    """


class SignalGapWriteFailed(Exception):
    """A ``SIGNAL_GAP`` event could not be written; it is tried again on the next retry."""


class LatestBarUnknown(Exception):
    """The strategy's latest completed bar could not be read, so a held signal stays held."""


_NOT_SETTLED = (SignalRecordWriteFailed, SignalGapWriteFailed, LatestBarUnknown)


@dataclass
class HeldSignal:
    strategy: Any
    signal: Any
    conid: int
    frame: pd.DataFrame
    disables_when_held: int
    # Once set the signal is never dispatched, only written down as a gap.
    block_reason: Optional[str] = None
    # The newer completed bar that made the signal STALE.
    superseded_by: Optional[dt.datetime] = None

    @property
    def signal_time(self) -> dt.datetime:
        return completed_bar_time(self.frame)


@dataclass
class _DroppedSignals:
    count: int
    first_signal_time: dt.datetime
    last_signal_time: dt.datetime


@dataclass
class _Hold:
    signals: List[HeldSignal] = field(default_factory=list)
    dropped: Optional[_DroppedSignals] = None
    full_alarm_raised: bool = False


class SignalHold:
    """Per (conId, strategy name) queue of signals waiting for the record.

    The queue is used from the event loop only; ``no_dispatch`` and ``note_disabled`` come from other threads.
    """

    def __init__(self, *, dispatch: Callable[[HeldSignal], None],
                 block_reason: Callable[[Any], Optional[str]],
                 latest_bar_time: Callable[[HeldSignal], Optional[dt.datetime]],
                 write_gap: Callable[[str, int, dict], None],
                 capacity: int = MAX_HELD_SIGNALS):
        self._dispatch = dispatch            # raises SignalRecordWriteFailed when the record write failed
        self._block_reason = block_reason    # why a strategy may no longer act, or None
        self._latest_bar_time = latest_bar_time   # newest completed bar of the signal's (conId, bar size), or None
        self._write_gap = write_gap
        self._capacity = capacity
        self._holds: Dict[tuple, _Hold] = {}
        # Bumped from the RPC thread on every disable; a held signal compares it with its own copy.
        self._disables: Dict[str, int] = {}
        # Reentrant: a dispatch may disable its own strategy (an AI source file changed after load).
        self._dispatch_lock = threading.RLock()

    @contextmanager
    def no_dispatch(self) -> Iterator[None]:
        """Hold while a strategy is disabled or unloaded, so no dispatch straddles the change."""
        with self._dispatch_lock:
            yield

    def note_disabled(self, strategy_name: str) -> None:
        with self._dispatch_lock:
            self._disables[strategy_name] = self._disables.get(strategy_name, 0) + 1

    def held_count(self, key: tuple) -> int:
        hold = self._holds.get(key)
        return len(hold.signals) if hold else 0

    def dispatch(self, key: tuple, strategy: Any, signal: Any, conid: int, frame: pd.DataFrame) -> None:
        """Dispatch a new signal now, or queue it behind the held ones so the record stays in signal order."""
        held = HeldSignal(strategy, signal, conid, frame, self._disables.get(strategy.name, 0))
        hold = self._holds.get(key)
        if hold is not None and hold.signals:
            self._enqueue(key, hold, held)
            return
        try:
            self._settle(held)
        except _NOT_SETTLED as ex:
            logging.error('signal of %s conId %s could not be settled; holding its signals (at most %d) and '
                          'retrying on each tick and reconcile: %s', key[1], key[0], self._capacity, ex)
            self._holds.setdefault(key, _Hold()).signals.append(held)

    def retry(self, key: tuple) -> None:
        hold = self._holds.get(key)
        if hold is None:
            return
        self._flush_dropped(key, hold)              # the evicted signals are older than every held one
        self._settle_in_order(key, hold)
        if not hold.signals and hold.dropped is None:
            del self._holds[key]
            logging.warning('signal hold for %s conId %s is clear', key[1], key[0])

    def retry_all(self) -> None:
        for key in list(self._holds):
            self.retry(key)

    def _enqueue(self, key: tuple, hold: _Hold, held: HeldSignal) -> None:
        """Queue behind the held signals; when full, evict the oldest, which a newer bar has already superseded.

        The evicted signals form one contiguous run before the held ones. They are not written here: the
        retry that ran just before on this tick could not write an event, or the hold would be empty.
        """
        if len(hold.signals) >= self._capacity:
            evicted = hold.signals.pop(0)
            hold.dropped = _add_dropped(hold.dropped, evicted.signal_time)
            if not hold.full_alarm_raised:
                hold.full_alarm_raised = True
                logging.error('SIGNAL_HOLD_FULL: %d signals of %s conId %s wait for the signal record; the oldest '
                              'are evicted and written as one SIGNAL_GAP event once any event can be written',
                              len(hold.signals) + 1, key[1], key[0])
        hold.signals.append(held)

    def _settle_in_order(self, key: tuple, hold: _Hold) -> None:
        while hold.signals:
            try:
                self._settle(hold.signals[0])
            except _NOT_SETTLED as ex:
                logging.debug('held signal of %s conId %s still not settled: %s', key[1], key[0], ex)
                return
            hold.signals.pop(0)

    def _settle(self, held: HeldSignal) -> None:
        with self._dispatch_lock:
            if held.block_reason is None:
                held.block_reason = self._why_blocked(held)
            if held.block_reason is None:
                self._dispatch(held)
                return
        self._write_blocked_gap(held)

    def _write_blocked_gap(self, held: HeldSignal) -> None:
        signal_time = held.signal_time
        action = str(held.signal.action)
        metadata = {'reason': held.block_reason, 'action': action, 'signal_time': signal_time.isoformat(),
                    'source_event_id': source_event_id_for(held.strategy.name, held.conid, action, signal_time)}
        if held.superseded_by is not None:
            metadata['latest_bar_time'] = held.superseded_by.isoformat()
        self._write(held.strategy.name, held.conid, metadata)
        logging.warning('%s signal of %s conId %s at %s was not dispatched (%s); written as SIGNAL_GAP',
                        action, held.strategy.name, held.conid, signal_time.isoformat(), held.block_reason)

    def _why_blocked(self, held: HeldSignal) -> Optional[str]:
        if self._disables.get(held.strategy.name, 0) != held.disables_when_held:
            return STRATEGY_DISABLED
        return self._block_reason(held.strategy) or self._staleness(held)

    def _staleness(self, held: HeldSignal) -> Optional[str]:
        latest = self._latest_bar_time(held)
        if latest is None:
            raise LatestBarUnknown(f'no completed bar for conId {held.conid} {held.strategy.bar_size}')
        if latest <= held.signal_time:
            return None
        held.superseded_by = latest
        return STALE

    def _flush_dropped(self, key: tuple, hold: _Hold) -> None:
        dropped = hold.dropped
        if dropped is None:
            return
        try:
            self._write(key[1], key[0], {
                'reason': HOLD_FULL, 'count': dropped.count,
                'first_signal_time': dropped.first_signal_time.isoformat(),
                'last_signal_time': dropped.last_signal_time.isoformat()})
        except SignalGapWriteFailed as ex:
            logging.debug('SIGNAL_GAP for %s conId %s not written yet: %s', key[1], key[0], ex)
            return
        hold.dropped = None
        logging.warning('%d signals of %s conId %s from %s to %s were evicted by a full hold; written as SIGNAL_GAP',
                        dropped.count, key[1], key[0], dropped.first_signal_time.isoformat(),
                        dropped.last_signal_time.isoformat())

    def _write(self, strategy_name: str, conid: int, metadata: dict) -> None:
        try:
            self._write_gap(strategy_name, conid, metadata)
        except Exception as ex:
            raise SignalGapWriteFailed(str(ex)) from ex


def _add_dropped(dropped: Optional[_DroppedSignals], signal_time: dt.datetime) -> _DroppedSignals:
    if dropped is None:
        return _DroppedSignals(1, signal_time, signal_time)
    return _DroppedSignals(dropped.count + 1, dropped.first_signal_time, signal_time)
