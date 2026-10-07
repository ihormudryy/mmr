from datetime import datetime, timezone

import pytest

from tests.ai.fakes import FakeClock
from trader.ai.store import AiStore


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc))  # 11:00 New York


@pytest.fixture
def store(tmp_path, clock) -> AiStore:
    created = AiStore(tmp_path / "ai.duckdb", clock=clock)
    created.migrate()
    return created


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect or name lookup fails the test."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access during an offline test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
