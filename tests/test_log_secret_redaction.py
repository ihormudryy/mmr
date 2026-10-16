"""Issue #204: the Telegram bot token is part of every Bot API URL; no log record may carry it."""
from __future__ import annotations

import io
import logging

import pytest

from trader.common import logging_helper
from trader.common.logging_helper import RedactTelegramToken, harden_secret_logging

TOKEN = "123456789:AAFakeSecretTokenValue_0123456789abcd"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture
def root_handler():
    """A root handler at DEBUG, as the services configure it; removed afterwards."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream, handler
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_the_http_client_loggers_never_log_request_urls(root_handler):
    stream, _handler = root_handler
    harden_secret_logging()
    logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', URL)    # what httpx logs per request
    logging.getLogger("httpcore.http11").debug("send_request_headers.started request=<Request [b'POST'] %s>", URL)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert TOKEN not in stream.getvalue()


def test_any_record_that_names_a_bot_url_is_redacted(root_handler):
    stream, _handler = root_handler
    harden_secret_logging()
    logging.getLogger("some.other.module").error("request to %s failed", URL)
    out = stream.getvalue()
    assert TOKEN not in out and "api.telegram.org/bot<redacted>/sendMessage" in out


def test_the_filter_keeps_other_records_unchanged():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "price %s", (101.5,), None)
    assert RedactTelegramToken().filter(record) is True
    assert record.getMessage() == "price 101.5"


def test_hardening_twice_adds_the_filter_once(root_handler):
    _stream, handler = root_handler
    harden_secret_logging()
    harden_secret_logging()
    assert sum(isinstance(f, RedactTelegramToken) for f in handler.filters) == 1


def test_setup_logging_hardens_every_service(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(logging_helper, "harden_secret_logging", lambda: called.append(True))
    logging_helper.setup_logging(default_path=str(tmp_path / "missing.yaml"), module_name="test-204-module")
    assert called == [True]
