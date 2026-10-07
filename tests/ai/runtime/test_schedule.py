"""SP2 Plan 5 Task 4: XNYS slots for entry and position cycles (spec 5.2, Rulings 6-8)."""
import datetime as dt

from tests.ai.runtime.fakes import et
from trader.ai.schedule import ENTRY, POSITION, SessionSlots

EARLY_CLOSE = dt.date(2026, 11, 27)          # the day after Thanksgiving: XNYS closes at 13:00
SATURDAY = dt.date(2026, 7, 18)


def test_entry_slots_run_from_after_stabilization_to_before_the_cutoff():
    slots = SessionSlots()
    assert slots.latest(ENTRY, et(9, 40)) is None                       # 09:30 is inside the stabilization
    first = slots.latest(ENTRY, et(9, 46))
    assert (first.cycle_id, first.start, first.deadline) == ("cyc-entry-20260717-0945", et(9, 45), et(10, 0))
    last = slots.latest(ENTRY, et(15, 29))
    assert (last.cycle_id, last.deadline) == ("cyc-entry-20260717-1515", et(15, 30))
    late = slots.latest(ENTRY, et(15, 40))
    assert late.cycle_id.endswith("1515") and not slots.is_due(late, et(15, 40))


def test_position_slots_continue_after_the_entry_cutoff():
    slots = SessionSlots()
    slot = slots.latest(POSITION, et(15, 31))
    assert (slot.cycle_id, slot.deadline) == ("cyc-position-20260717-1530", et(15, 45))
    assert slots.is_due(slot, et(15, 31)) and not slots.entry_window_open(et(15, 31))


def test_early_close_moves_both_windows():
    slots = SessionSlots()
    entry = slots.latest(ENTRY, et(12, 29, day=EARLY_CLOSE))
    position = slots.latest(POSITION, et(12, 31, day=EARLY_CLOSE))
    assert (entry.cycle_id, entry.deadline) == ("cyc-entry-20261127-1215", et(12, 30, day=EARLY_CLOSE))
    assert (position.cycle_id, position.deadline) == ("cyc-position-20261127-1230", et(12, 45, day=EARLY_CLOSE))


def test_no_slot_and_no_entry_window_on_a_closed_day():
    slots = SessionSlots()
    assert slots.latest(ENTRY, et(11, 0, day=SATURDAY)) is None
    assert slots.latest(POSITION, et(11, 0, day=SATURDAY)) is None
    assert not slots.entry_window_open(et(11, 0, day=SATURDAY))


def test_slot_ids_use_new_york_time_across_dst():
    slots = SessionSlots()
    before = slots.latest(ENTRY, et(9, 50, day=dt.date(2026, 3, 6)))      # EST
    after = slots.latest(ENTRY, et(9, 50, day=dt.date(2026, 3, 9)))       # EDT
    assert before.cycle_id.endswith("0945") and after.cycle_id.endswith("0945")
    assert before.start.hour - after.start.hour == 1                      # UTC hour moves, the id does not


def test_a_slot_is_due_only_inside_its_start_grace():
    slots = SessionSlots(grace_seconds=120)
    slot = slots.latest(ENTRY, et(11, 0))
    assert slots.is_due(slot, et(11, 1, 59)) and not slots.is_due(slot, et(11, 2, 0))


def test_a_shorter_interval_keeps_the_same_alignment():
    slot = SessionSlots(entry_minutes=5).latest(ENTRY, et(9, 36))
    assert slot.cycle_id.endswith("0935") and slot.deadline == et(9, 40)
