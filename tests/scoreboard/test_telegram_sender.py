import logging

import httpx
import pytest

from tests.scoreboard.telegram_fakes import CHAT, FAKE_TOKEN, Clock, FakePost, token_file
from trader.scoreboard.telegram_config import TelegramConfig
from trader.scoreboard.telegram_outbox import TelegramOutbox
from trader.scoreboard.telegram_sender import PostResult, TelegramSender, build_telegram, http_post

CONFIG = TelegramConfig(chat_id=CHAT, token=FAKE_TOKEN)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def outbox(store, db, clock):
    box = TelegramOutbox(db, now=clock)
    box.enqueue("daily_summary:exp-1:2026-10-06", "daily_summary", "PAPER — FLAT")
    return box


@pytest.fixture
def post():
    return FakePost()


@pytest.fixture
def sender(outbox, post):
    return TelegramSender(outbox, CONFIG, post=post)


def last_error(db):
    return db.execute("SELECT last_error FROM telegram_outbox", fetch="one")[0]


def test_sends_once_per_event_id_and_marks_sent(sender, post):
    assert sender.drain() == 1 and sender.drain() == 0
    assert len(post.calls) == 1 and post.calls[0].url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


def test_chat_id_is_always_the_configured_one(sender, post):
    sender.drain()
    assert post.calls[0].payload["chat_id"] == CONFIG.chat_id and set(post.calls[0].payload) == {"chat_id", "text"}


def test_outage_delays_then_delivers_without_duplicates(sender, post, clock, outbox):
    post.fail_next(2)
    assert sender.drain() == 0
    clock.advance(30)
    assert sender.drain() == 0
    assert sender.drain() == 0                      # backed off: not due yet, no call
    clock.advance(60)
    assert sender.drain() == 1
    assert len(post.calls) == 3 and outbox.row("daily_summary:exp-1:2026-10-06").status == "SENT"


def test_http_429_honours_retry_after(sender, post, clock, outbox):
    post.results.append(PostResult(429, None, 120.0))
    sender.drain()
    clock.advance(119)
    assert sender.drain() == 0 and len(post.calls) == 1
    clock.advance(1)
    assert sender.drain() == 1


def test_http_4xx_keeps_the_row_pending_and_logs_an_error(sender, post, caplog, outbox):
    post.results.append(PostResult(401, None, None))
    with caplog.at_level(logging.ERROR):
        assert sender.drain() == 0
    assert outbox.row("daily_summary:exp-1:2026-10-06").status == "PENDING" and "401" in caplog.text


def test_transport_error_text_never_contains_the_token(sender, post, caplog, db):
    post.raise_next(httpx.ConnectError(f"failed for https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"))
    with caplog.at_level(logging.DEBUG):
        sender.drain()
    assert FAKE_TOKEN not in caplog.text and FAKE_TOKEN not in last_error(db)
    assert "<token>" in last_error(db)


def test_one_failing_row_does_not_block_the_next(sender, post, outbox):
    outbox.enqueue("e2", "k", "second")
    post.raise_next(RuntimeError("boom"))
    assert sender.drain() == 1
    assert outbox.row("e2").status == "SENT" and outbox.row("daily_summary:exp-1:2026-10-06").status == "PENDING"


def test_rows_marked_pending_after_a_crash_are_resent_once_with_their_event_id_in_the_text(sender, post, outbox):
    sender.drain()                                                 # sent, but pretend mark_sent was lost
    outbox.db.execute("UPDATE telegram_outbox SET status = 'PENDING', sent_at = NULL")
    sender.drain()
    sender.drain()
    assert len(post.calls) == 2 and post.calls[0].payload["text"] == post.calls[1].payload["text"]
    assert post.calls[1].payload["text"].endswith("event daily_summary:exp-1:2026-10-06")


def test_disabled_gate_means_no_sender_and_no_http(db, store):
    assert build_telegram(None, db, now=Clock()) == (None, None)
    assert build_telegram({"enabled": False}, db, now=Clock()) == (None, None)


def test_enabled_gate_builds_an_outbox_and_a_sender(db, store, tmp_path):
    section = {"enabled": True, "chat_id": 5, "token_secret_file": str(token_file(tmp_path))}
    outbox, sender = build_telegram(section, db, now=Clock())
    assert isinstance(outbox, TelegramOutbox) and isinstance(sender, TelegramSender)


def test_http_post_uses_no_redirects_and_a_timeout(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"message_id": 7}}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return Response()
    monkeypatch.setattr(httpx, "post", fake_post)
    result = http_post("https://example.invalid/x", {"chat_id": "1", "text": "t"})
    assert result == PostResult(200, 7, None)
    assert seen["follow_redirects"] is False and seen["timeout"] == 10.0 and seen["json"]["text"] == "t"


def test_http_post_reads_retry_after_and_hides_transport_detail(monkeypatch):
    class Response:
        status_code = 429

        @staticmethod
        def json():
            return {"ok": False, "parameters": {"retry_after": 33}}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: Response())
    assert http_post("u", {}) == PostResult(429, None, 33.0)

    def boom(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(Exception) as exc:
        http_post(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage", {})
    assert FAKE_TOKEN not in str(exc.value)
