import datetime as dt

from trader.ai.schedule import ET, SessionSlots


def et(day, hour, minute=0, month=10):
    return dt.datetime(2026, month, day, hour, minute, tzinfo=ET)


def test_the_slot_starts_after_the_close_and_ends_at_new_york_midnight():
    slots = SessionSlots()
    slot = slots.author_slot(et(8, 17, 5))                                   # Thursday
    assert (slot.attempt_id, slot.start, slot.ends_at) == ("aut-20261008", et(8, 17), et(9, 0))
    assert slots.author_due(slot, et(8, 23, 59)) and not slots.author_due(slot, et(9, 0))


def test_before_the_slot_the_previous_session_is_the_newest_and_not_due():
    slots = SessionSlots()
    slot = slots.author_slot(et(8, 16, 30))
    assert slot.attempt_id == "aut-20261007" and not slots.author_due(slot, et(8, 16, 30))


def test_a_weekend_adds_no_slot():
    slots = SessionSlots()
    slot = slots.author_slot(et(11, 12))                                     # Sunday
    assert slot.attempt_id == "aut-20261009" and not slots.author_due(slot, et(11, 12))


def test_a_holiday_adds_no_slot():
    slots = SessionSlots()
    slot = slots.author_slot(et(26, 20, month=11))                           # Thanksgiving 2026
    assert slot.attempt_id == "aut-20261125" and not slots.author_due(slot, et(26, 20, month=11))


def test_a_half_day_starts_after_the_early_close():
    slots = SessionSlots()
    slot = slots.author_slot(et(27, 14, 30, month=11))                       # day after Thanksgiving, close 13:00
    assert (slot.attempt_id, slot.start) == ("aut-20261127", et(27, 14, month=11))
    assert slots.author_due(slot, et(27, 14, 30, month=11))
    assert slots.author_slot(et(27, 13, 59, month=11)).attempt_id == "aut-20261125"


def test_the_delay_comes_from_authoring():
    assert SessionSlots(author_after_close_minutes=0).author_slot(et(8, 16)).start == et(8, 16)
