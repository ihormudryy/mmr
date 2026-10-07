import datetime as dt

import pytest

from tests.scoreboard.common import ACCOUNT, EXP_ID
from tests.scoreboard.ledger_world import D, END, END_FLAT, KILL_AT_SAME_ET_DATE, with_experiment
from tests.scoreboard.telegram_fakes import Clock
from trader.scoreboard.ports import SessionEnd
from trader.scoreboard.summary_text import DailySummaryProducer, format_daily_summary, summary_event_id
from trader.scoreboard.telegram_outbox import MAX_TEXT, TelegramOutbox


def report(end_state="FLAT", **account):
    return {
        "label": "PAPER", "disclaimer": "Paper trading. Nothing here is proof of live edge.",
        "experiment": {"id": EXP_ID, "state": "ARMED", "started_at": "2026-10-06T13:35:00+00:00",
                       "base_currency": "USD"},
        "account": {"return_pct": None, "pnl_usd": None, "eod_drawdown_pct": None,
                    "eod_drawdown_label": "drawdown from end-of-day equity; intraday lows may be missed",
                    "sessions": 1, **account},
        "benchmarks": {"spy": {"return_pct": None, "label": "SPY price only; dividends excluded"},
                       "vs_spy_pp": None, "ai_costs_status": "UNAVAILABLE", "ai_cost_usd": None,
                       "pnl_minus_ai_cost_usd": None,
                       "ai_cost": {"status": "NONE", "total_usd": None, "unknown_calls": 0}, "books": []},
        "trips": {"closed": 0, "open": 0},
        "sessions": [{"date": D.isoformat(), "end_state": end_state, "return_pct": None, "end_nlv_usd": None,
                      "realized_pnl_usd": 0.0, "commissions_usd": None, "trade_count": 0, "open_positions": None}],
        "incidents": [], "warnings": [],
    }


def test_summary_has_paper_label_end_state_and_unknowns_as_dash():
    text = format_daily_summary(report(), D)
    assert "PAPER" in text and "return -" in text and "fees -" in text
    assert "None" not in text and "nan" not in text.lower() and "proof of live edge" in text
    assert "realized $0.00" in text


def test_summary_labels_estimated_cost_and_counts_incomplete_books():
    data = report()
    data["benchmarks"]["ai_costs_status"] = "AVAILABLE"
    data["benchmarks"]["ai_cost"] = {"status": "ESTIMATED", "total_usd": 2.0, "unknown_calls": 0}
    data["benchmarks"]["books"] = [{"status": "COMPLETE"}, {"status": "INCOMPLETE"}]
    text = format_daily_summary(data, D)
    assert "$2.00 (estimated; 0 unknown call(s))" in text and "Baselines: 2 books, 1 not complete" in text


@pytest.mark.parametrize("state", ["FLAT", "KILLED", "FAILED_SAFE"])
def test_summary_first_line_is_the_end_state_for_all_three(state):
    assert format_daily_summary(report(state), D).splitlines()[0] == f"PAPER — {state}"


def test_unknown_row_sends_no_summary():
    with pytest.raises(ValueError):
        format_daily_summary(report("UNKNOWN"), D)


def test_a_date_without_a_session_row_is_refused():
    with pytest.raises(ValueError):
        format_daily_summary(report(), dt.date(2026, 10, 7))


def test_summary_carries_the_spy_label_and_never_says_exactly_once():
    text = format_daily_summary(report(), D)
    assert "SPY price only; dividends excluded" in text and "intraday lows may be missed" in text
    assert "exactly once" not in text.lower() and "exactly-once" not in text.lower()


def test_summary_is_plain_text_and_ends_with_the_event_id():
    text = format_daily_summary(report(), D)
    assert text.endswith(f"event {summary_event_id(EXP_ID, D)}")
    assert "<b>" not in text and "*" not in text


def test_summary_over_4000_chars_is_cut_with_a_notice():
    big = report()
    big["incidents"] = [{"kind": "SESSION_MISSING", "key": f"k{i}", "detail": "x" * 200} for i in range(100)]
    text = format_daily_summary(big, D)
    assert len(text) <= MAX_TEXT and "[cut:" in text and text.endswith(f"event {summary_event_id(EXP_ID, D)}")


@pytest.fixture
def producer(world, scoreboard, db):
    outbox = TelegramOutbox(db, now=Clock())
    return DailySummaryProducer(scoreboard, outbox, now=lambda: world.clock[0]), outbox


def test_producer_enqueues_one_message_per_session_row(world, producer):
    producer_, outbox = producer
    world.ledger().record_session_end(END_FLAT)
    assert producer_.on_session_row(EXP_ID, D) is True and producer_.on_session_row(EXP_ID, D) is False
    rows = outbox.due()
    assert [r.event_id for r in rows] == [summary_event_id(EXP_ID, D)]
    assert rows[0].text.startswith("PAPER — FLAT")


def test_producer_for_a_session_without_a_row_enqueues_nothing(world, producer):
    producer_, outbox = producer
    assert producer_.on_session_row(EXP_ID, D) is False and outbox.due() == []


def test_producer_writes_the_killed_state_first(world, producer):
    producer_, outbox = producer
    with_experiment(world, state="KILLED", killed_at=KILL_AT_SAME_ET_DATE)
    world.ledger().record_session_end(SessionEnd(ACCOUNT, D, "KILLED", END))
    producer_.on_session_row(EXP_ID, D)
    assert outbox.due()[0].text.startswith("PAPER — KILLED")


def test_catch_up_enqueues_the_newest_session_once(world, producer):
    producer_, outbox = producer
    world.ledger(on_row_written=None).record_session_end(END_FLAT)     # the hook never ran (crash)
    assert producer_.catch_up() is True and producer_.catch_up() is False
    assert [r.event_id for r in outbox.due()] == [summary_event_id(EXP_ID, D)]


def test_catch_up_never_sends_an_old_session(world, producer):
    producer_, outbox = producer
    world.ledger(on_row_written=None).record_session_end(END_FLAT)
    world.clock[0] = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.timezone.utc)      # two sessions later
    assert producer_.catch_up() is False and outbox.due() == []
