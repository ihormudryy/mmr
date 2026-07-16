import asyncio
import datetime as dt
import queue
import threading
import time

import pytest

from trader.domain.events import DomainEvent, ReadDomainEventsResult, SnapshotWithCursor
from web.command_center.bridge import BridgeLifecycle, DashboardEventBridge
from web.command_center.state import DashboardState

UTC = dt.timezone.utc


def _event(cursor: int, revision: int = 1, entity_id: str = "DU123:265598") -> DomainEvent:
    return DomainEvent(
        event_id=f"evt-{cursor}", source_cursor=cursor, entity_revision=revision,
        event_type="position.updated", entity_type="position", entity_id=entity_id,
        operation="upsert", account_id="DU123", source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None, payload={"quantity": cursor})


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeQueryClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)  # SnapshotWithCursor | Exception
        self.closed = False

    def call(self, method, body, response_model):
        if method == "snapshot_with_cursor":
            item = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            if isinstance(item, Exception):
                raise item
            return item
        if method == "get_quotes_snapshot":
            return {"quotes": {"265598": {"instrument_id": "265598", "last": 100.0}}}
        raise AssertionError(f"unexpected query method {method}")

    def close(self):
        self.closed = True


class ScriptedFeed:
    """Feed client returning scripted batches; blocks like a real long-poll."""

    def __init__(self):
        self.items: "queue.Queue[object]" = queue.Queue()
        self.calls: list[dict] = []
        self.closed = False

    def call(self, method, body, response_model):
        assert method == "read_domain_events"
        self.calls.append(dict(body))
        item = self.items.get(timeout=2)
        if isinstance(item, Exception):
            raise item
        events = tuple(e for e in item if e.source_cursor > body["after_cursor"])
        newest = max([e.source_cursor for e in events], default=body["after_cursor"])
        return ReadDomainEventsResult(events=events, newest_cursor=newest)

    def close(self):
        self.closed = True


class RecordingFanout:
    def __init__(self):
        self.published: list[dict] = []
        self.resyncs = 0

    def publish(self, envelope):
        self.published.append(envelope)

    def publish_quotes(self, batch):
        pass

    def broadcast_resync(self):
        self.resyncs += 1


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def _snapshot(cursor: int = 10) -> SnapshotWithCursor:
    return SnapshotWithCursor(
        source_cursor=cursor, broker_generation=1,
        entities={"position": [
            {"entity_id": "DU123:265598", "entity_revision": 3, "quantity": 5}]})


def _bridge(loop, query, feed, state=None, fanout=None, **kwargs):
    state = state or DashboardState()
    fanout = fanout or RecordingFanout()
    bridge = DashboardEventBridge(
        query, feed, state, fanout, loop,
        backoff_min=0.01, backoff_max=0.05, **kwargs)
    return bridge, state, fanout


class TestBaselineAndTail:
    def test_fenced_baseline_installs_before_tailing(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: state.has_baseline)
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            assert wait_until(lambda: feed.calls and feed.calls[0]["after_cursor"] == 10)
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_events_apply_and_cursor_advances(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put([_event(11, revision=4), _event(12, revision=5)])
            assert wait_until(lambda: len(fanout.published) == 2)
            assert wait_until(lambda: len(feed.calls) >= 2
                              and feed.calls[-1]["after_cursor"] == 12)
            assert state.positions["DU123:265598"]["quantity"] == 12
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_stale_revision_is_ignored_not_published(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put([_event(11, revision=2)])  # baseline already at revision 3
            assert wait_until(lambda: len(feed.calls) >= 2)
            assert fanout.published == []
            assert state.positions["DU123:265598"]["quantity"] == 5
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()


class TestFailureHandling:
    def test_snapshot_not_ready_keeps_synchronizing_without_partial_state(self, loop_thread):
        query = FakeQueryClient([RuntimeError("SNAPSHOT_NOT_READY"), _snapshot(10)])
        feed = ScriptedFeed()
        bridge, state, _ = _bridge(loop_thread, query, feed)
        bridge.start()
        try:
            assert wait_until(lambda: state.has_baseline)
            assert bridge.health()["reconnects"] >= 1
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_feed_failure_degrades_then_resyncs_with_new_stream_id(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            first_stream = state.stream_id
            feed.items.put(ConnectionError("journal gone"))
            assert wait_until(lambda: state.stream_id != first_stream)
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            assert fanout.resyncs >= 2  # initial baseline + post-failure resync
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_health_reports_safe_error_and_age(self, loop_thread):
        feed = ScriptedFeed()
        bridge, _, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put(ConnectionError("journal gone"))
            assert wait_until(
                lambda: bridge.health()["sources"]["journal"]["last_error"] is not None)
            health = bridge.health()
            assert "ConnectionError" in health["sources"]["journal"]["last_error"]
            assert set(health) >= {"lifecycle", "reconnects", "cursor", "sources"}
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_stop_joins_thread_and_closes_nothing_it_does_not_own(self, loop_thread):
        feed = ScriptedFeed()
        bridge, _, _ = _bridge(loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
        feed.items.put(ConnectionError("stop"))
        bridge.stop()
        assert not bridge.is_alive()
