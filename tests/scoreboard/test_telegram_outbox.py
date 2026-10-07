import pytest

from tests.scoreboard.telegram_fakes import Clock
from trader.scoreboard.telegram_outbox import MAX_TEXT, TelegramOutbox


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def outbox(store, db, clock):
    return TelegramOutbox(db, now=clock)


def test_enqueue_is_idempotent_per_event_id(outbox):
    assert outbox.enqueue("e1", "k", "t") is True and outbox.enqueue("e1", "k", "other") is False
    assert len(outbox.due()) == 1


def test_every_text_ends_with_its_event_id(outbox):
    outbox.enqueue("kill_started:exp-1:1", "kill_started", "PAPER experiment KILLED")
    assert outbox.due()[0].text.endswith("event kill_started:exp-1:1")


def test_a_long_text_is_cut_with_a_notice_and_keeps_the_event_id(outbox):
    outbox.enqueue("e1", "k", "x" * 10_000)
    text = outbox.due()[0].text
    assert len(text) <= MAX_TEXT and "[cut:" in text and text.endswith("event e1")


def test_failed_send_backs_off_and_stays_pending(outbox, clock):
    outbox.enqueue("e1", "k", "t")
    waits = []
    for _ in range(9):
        outbox.mark_failed("e1", "boom")
        row = outbox.row("e1")
        waits.append(int((row.next_attempt_at - clock()).total_seconds()))
        assert outbox.due() == []
        clock.value = row.next_attempt_at
        assert [r.event_id for r in outbox.due()] == ["e1"]
    assert waits[:4] == [30, 60, 120, 240] and waits[-1] == 3600
    assert outbox.row("e1").status == "PENDING" and outbox.row("e1").attempts == 9


def test_retry_after_from_telegram_is_honoured(outbox, clock):
    outbox.enqueue("e1", "k", "t")
    outbox.mark_failed("e1", "429", retry_after=90)
    assert int((outbox.row("e1").next_attempt_at - clock()).total_seconds()) == 90


def test_sent_row_is_never_due_again(outbox, clock):
    outbox.enqueue("e1", "k", "t")
    outbox.mark_sent("e1", 42)
    clock.advance(10_000)
    assert outbox.due() == [] and outbox.row("e1").telegram_message_id == 42


def test_counts_report_pending_and_last_sent(outbox, clock):
    assert outbox.counts() == {"pending": 0, "last_sent_at": None}
    outbox.enqueue("e1", "k", "t")
    outbox.enqueue("e2", "k", "t")
    outbox.mark_sent("e1", 1)
    assert outbox.counts() == {"pending": 1, "last_sent_at": clock()}


def test_outbox_accepts_a_plan4_kill_alert(outbox):
    assert outbox.enqueue("kill_started:exp-0123456789abcdef0123:1", "kill_started", "t") is True
    assert outbox.enqueue("kill_started:exp-0123456789abcdef0123:1", "kill_started", "t") is False
