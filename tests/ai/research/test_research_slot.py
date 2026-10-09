import datetime as dt

from trader.ai.schedule import ET, SessionSlots


def et(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ET)


def test_the_slot_starts_after_the_close_and_ends_before_the_next_open():
    slots = SessionSlots()
    slot = slots.research_slot(et(8, 16, 31))                     # Thursday
    assert (slot.cycle_id, slot.start, slot.closes_at) == ("rcy-20261008", et(8, 16, 30), et(9, 9, 0))
    assert slots.research_due(slot, et(9, 3, 0)) and not slots.research_due(slot, et(9, 9, 0))


def test_a_friday_slot_runs_over_the_weekend():
    slots = SessionSlots()
    slot = slots.research_slot(et(11, 12))                         # Sunday
    assert slot.cycle_id == "rcy-20261009" and slot.closes_at == et(12, 9, 0)


def test_research_window_is_closed_during_the_session():
    slots = SessionSlots()
    for moment in (et(8, 9, 0), et(8, 9, 31), et(8, 11), et(8, 15, 59), et(8, 16, 29)):
        assert slots.research_window_open(moment) is False, moment
    assert slots.research_window_open(et(8, 16, 30)) and slots.research_window_open(et(9, 2))


def test_before_the_first_slot_of_today_the_previous_one_counts():
    slot = SessionSlots().research_slot(et(8, 10))
    assert slot.cycle_id == "rcy-20261007" and SessionSlots().research_due(slot, et(8, 10)) is False


def test_the_first_research_start_after_the_next_new_york_date_change():
    slots = SessionSlots()
    assert slots.research_start_after_next_midnight(et(8, 17)) == et(9, 16, 30)          # Thursday evening
    assert slots.research_start_after_next_midnight(et(9, 1)) == et(12, 16, 30)          # Friday 01:00: Monday
    assert slots.research_start_after_next_midnight(et(9, 17)) == et(12, 16, 30)         # Friday evening
