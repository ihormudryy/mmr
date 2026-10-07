"""Fake Telegram transport and a token file. The token below is a made-up test value, not a real bot."""
import datetime as dt
import os
from dataclasses import dataclass

from trader.scoreboard.telegram_sender import PostResult

FAKE_TOKEN = "123456:TEST-not-a-real-token_abcdefgh"
CHAT = "123456789"


def token_file(tmp_path, content=FAKE_TOKEN, mode=0o600, name="telegram.token"):
    path = tmp_path / name
    path.write_text(content + "\n")
    os.chmod(path, mode)
    return path


@dataclass
class Call:
    url: str
    payload: dict


class FakePost:
    def __init__(self):
        self.calls = []
        self.results = []
        self.errors = []
        self.next_id = 100

    def fail_next(self, n, status=502):
        self.results.extend([PostResult(status, None, None)] * n)

    def raise_next(self, exc):
        self.errors.append(exc)

    def __call__(self, url, payload):
        self.calls.append(Call(url, dict(payload)))
        if self.errors:
            raise self.errors.pop(0)
        if self.results:
            return self.results.pop(0)
        self.next_id += 1
        return PostResult(200, self.next_id, None)


class Clock:
    def __init__(self, start=dt.datetime(2026, 10, 6, 21, 0, tzinfo=dt.timezone.utc)):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += dt.timedelta(seconds=seconds)
