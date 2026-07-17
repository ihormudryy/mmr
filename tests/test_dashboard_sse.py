import asyncio
import datetime as dt
import json

import pytest

from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center.sse import FIFO_LIMIT, SseFanout
from web.command_center.state import DashboardState

UTC = dt.timezone.utc


def _event(cursor: int, revision: int) -> DomainEvent:
    return DomainEvent(
        event_id=f"evt-{cursor}", source_cursor=cursor, entity_revision=revision,
        event_type="position.updated", entity_type="position",
        entity_id="DU123:265598", operation="upsert", account_id="DU123",
        source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None, payload={"quantity": cursor})


@pytest.fixture
def state():
    s = DashboardState()
    s.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={}),
        stream_id="stream-a")
    return s


@pytest.fixture
def fanout(state):
    return SseFanout(state)


def _apply_and_publish(state, fanout, event):
    envelope = state.apply(event)
    if envelope is not None:
        fanout.publish(envelope)
    return envelope


class TestHandshake:
    def test_replay_cutover_is_exact_no_gap_no_duplicate(self, state, fanout):
        for i in range(1, 4):
            _apply_and_publish(state, fanout, _event(i, i))
        client, replay, quotes = fanout.register("stream-a:1")
        _apply_and_publish(state, fanout, _event(4, 4))
        replay_seqs = [env["sequence"] for env in replay]
        live_seqs = [env["sequence"] for env in client.fifo]
        assert replay_seqs == [2, 3]
        assert live_seqs == [4]

    def test_register_captures_current_quote_map(self, state, fanout):
        fanout.publish_quotes({"265598": {"instrument_id": "265598", "last": 9.9}})
        _, _, quotes = fanout.register(None)
        assert quotes["265598"]["last"] == 9.9

    def test_stale_stream_or_aged_cursor_returns_none_replay(self, state, fanout):
        _apply_and_publish(state, fanout, _event(1, 1))
        _, replay, _ = fanout.register("stream-OLD:1")
        assert replay is None


class TestFifoBounds:
    def test_fifo_overflow_clears_client_and_marks_resync(self, state, fanout):
        client, _, _ = fanout.register(None)
        for i in range(1, FIFO_LIMIT + 2):
            _apply_and_publish(state, fanout, _event(i, i))
        assert client.resync is True
        assert len(client.fifo) == 0

    def test_quotes_never_consume_fifo_capacity(self, state, fanout):
        client, _, _ = fanout.register(None)
        for i in range(5000):
            fanout.publish_quotes({str(i): {"instrument_id": str(i), "last": 1.0}})
        assert client.resync is False
        assert len(client.fifo) == 0
        assert len(client.quote_map) == 5000

    def test_slow_client_never_blocks_others(self, state, fanout):
        slow, _, _ = fanout.register(None)
        fast, _, _ = fanout.register(None)
        for i in range(1, FIFO_LIMIT + 2):
            _apply_and_publish(state, fanout, _event(i, i))
        assert slow.resync is True
        assert fast.resync is True  # both overflowed independently
        fresh, replay, _ = fanout.register(f"stream-a:{state.sequence}")
        assert replay == [] and fresh.resync is False


class TestResyncBroadcast:
    def test_broadcast_resync_flags_every_client(self, state, fanout):
        a, _, _ = fanout.register(None)
        b, _, _ = fanout.register(None)
        fanout.broadcast_resync()
        assert a.resync and b.resync

    def test_unregister_removes_client(self, state, fanout):
        client, _, _ = fanout.register(None)
        fanout.unregister(client)
        assert fanout.client_count() == 0
        _apply_and_publish(state, fanout, _event(1, 1))
        assert len(client.fifo) == 0
