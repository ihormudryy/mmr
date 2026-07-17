import asyncio
import datetime as dt
import queue
import threading
import time

import pytest

from trader.domain.events import DomainEvent, ReadDomainEventsResult, SnapshotWithCursor
from trader.domain.feed_service import domain_event_to_wire
from trader.messaging.typed_rpc import TypedRpcRemoteError
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
            # Real server returns a wire dict (response_model=dict); the bridge
            # reconstructs via snapshot_with_cursor_from_wire. Serialize here so
            # this test exercises the actual wire contract, not a bypass.
            return {"source_cursor": item.source_cursor,
                    "broker_generation": item.broker_generation,
                    "entities": item.entities}
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
        return {"events": [domain_event_to_wire(e) for e in events],
                "newest_cursor": newest}

    def close(self):
        self.closed = True


class BlockingFeed:
    """Long-poll fake that always blocks for `delay` seconds regardless of
    wait_ms -- simulates a poll genuinely in flight when stop() is called."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls = 0
        self.closed = False

    def call(self, method, body, response_model):
        assert method == "read_domain_events"
        self.calls += 1
        time.sleep(self.delay)
        return {"events": [], "newest_cursor": body["after_cursor"]}

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

    def test_missing_get_quotes_snapshot_does_not_block_baseline(self, loop_thread):
        # get_quotes_snapshot is an optional quote pre-seed. A trader query
        # surface that doesn't register it (METHOD_NOT_ALLOWED) must NOT block
        # the fenced baseline -- readiness is gated on snapshot_with_cursor, and
        # live quotes stream via the QuotePlane. Only METHOD_NOT_ALLOWED is
        # tolerated; other remote errors still degrade (covered elsewhere).
        class QuotelessQuery:
            def __init__(self):
                self.closed = False

            def call(self, method, body, response_model):
                if method == "snapshot_with_cursor":
                    s = _snapshot(10)
                    return {"source_cursor": s.source_cursor,
                            "broker_generation": s.broker_generation,
                            "entities": s.entities}
                if method == "get_quotes_snapshot":
                    raise TypedRpcRemoteError(
                        "METHOD_NOT_ALLOWED",
                        "method 'get_quotes_snapshot' is not registered on the 'query' socket")
                raise AssertionError(f"unexpected query method {method}")

            def close(self):
                self.closed = True

        feed = ScriptedFeed()
        bridge, state, _ = _bridge(loop_thread, QuotelessQuery(), feed)
        bridge.start()
        try:
            assert wait_until(lambda: state.has_baseline)
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            assert state.quotes == {}  # no pre-seed installed, baseline still live
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
            # The failure happened inside _resync_baseline (a snapshot-query
            # error) -- it must be attributed to the 'snapshot' source, not
            # hardcoded to 'journal'. reconnects is never reset by a later
            # success, so this is safe to check even after recovery.
            assert bridge.health()["sources"]["snapshot"]["reconnects"] >= 1
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
        query = FakeQueryClient([_snapshot(10)])
        bridge, _, _ = _bridge(loop_thread, query, feed)
        bridge.start()
        assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
        feed.items.put(ConnectionError("stop"))
        bridge.stop()
        assert not bridge.is_alive()
        assert not feed.closed
        assert not query.closed


class TestTailApplyIntegrity:
    """FIX #1: the loop-side apply must be confirmed before the bridge
    advances its read cursor, and two in-flight batches must never alias."""

    def test_apply_error_does_not_skip_events_and_forces_resync(self, loop_thread):
        class ExplodingState(DashboardState):
            def __init__(self):
                super().__init__()
                self.raise_on_cursor = None

            def apply(self, event):
                if event.source_cursor == self.raise_on_cursor:
                    raise RuntimeError("apply boom")
                return super().apply(event)

        feed = ScriptedFeed()
        state = ExplodingState()
        bridge, _, fanout = _bridge(
            loop_thread, FakeQueryClient([_snapshot(10)]), feed, state=state)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            state.raise_on_cursor = 12
            feed.items.put([_event(11, revision=4), _event(12, revision=5)])
            # The loop-side apply blows up on cursor 12 -- the bridge must
            # not silently treat the batch as consumed (a read-model gap),
            # must record the failure, and must force a resync rather than
            # quietly staying LIVE.
            assert wait_until(
                lambda: bridge.health()["sources"]["journal"]["last_error"] is not None)
            assert "apply boom" in bridge.health()["sources"]["journal"]["last_error"]
            assert wait_until(lambda: fanout.resyncs >= 2)
            # Recovery must resume tailing from the re-installed baseline
            # cursor (10), not from 12 -- proof the failed batch was never
            # marked consumed.
            assert wait_until(
                lambda: len(feed.calls) >= 2 and feed.calls[1]["after_cursor"] == 10)
        finally:
            state.raise_on_cursor = None
            feed.items.put(ConnectionError("stop"))
            bridge.stop()

    def test_apply_callback_binds_its_own_batch_no_aliasing(self, loop_thread):
        feed = ScriptedFeed()
        bridge, _, fanout = _bridge(
            loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        # Distinct entity_ids so batch2's higher revisions can't shadow
        # batch1's as merely-stale under DashboardState's per-entity
        # revision check -- any cross-talk here must come from aliasing.
        batch1 = (_event(11, revision=4, entity_id="DU123:111"),)
        batch2 = (
            _event(12, revision=5, entity_id="DU123:222"),
            _event(13, revision=6, entity_id="DU123:222"),
        )
        applied1, applied2 = threading.Event(), threading.Event()
        errors1: list = []
        errors2: list = []
        cb1 = bridge._make_apply_callback(batch1, applied1, errors1)
        cb2 = bridge._make_apply_callback(batch2, applied2, errors2)

        # Invoke out of creation order. If the callbacks aliased a shared
        # events/applied cell (the pre-fix bug), cb1 would end up operating
        # on batch2's events and/or signalling applied2 instead of applied1.
        cb2()
        cb1()

        assert applied1.is_set() and applied2.is_set()
        assert errors1 == [] and errors2 == []
        published_cursors = [e["source_cursor"] for e in fanout.published]
        assert published_cursors == [12, 13, 11]

    def test_two_sequential_batches_do_not_cross_contaminate(self, loop_thread):
        feed = ScriptedFeed()
        bridge, state, fanout = _bridge(
            loop_thread, FakeQueryClient([_snapshot(10)]), feed)
        bridge.start()
        try:
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
            feed.items.put([_event(11, revision=4)])
            assert wait_until(lambda: len(fanout.published) == 1)
            assert wait_until(lambda: bridge.health()["cursor"] == 11)
            feed.items.put([_event(12, revision=5), _event(13, revision=6)])
            assert wait_until(lambda: len(fanout.published) == 3)
            assert wait_until(lambda: bridge.health()["cursor"] == 13)
            assert [e["source_cursor"] for e in fanout.published] == [11, 12, 13]
            assert state.positions["DU123:265598"]["quantity"] == 13
        finally:
            feed.items.put(ConnectionError("stop"))
            bridge.stop()


class TestColdOutageDisconnect:
    """FIX #3: DISCONNECTED must be reachable even if the backend has never
    once succeeded (last_success stays None forever)."""

    def test_disconnected_after_cold_outage_never_had_a_success(self, loop_thread):
        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        class AlwaysFailQuery:
            def __init__(self):
                self.closed = False

            def call(self, method, body, response_model):
                raise ConnectionError("backend unreachable")

            def close(self):
                self.closed = True

        feed = ScriptedFeed()
        bridge, _, _ = _bridge(
            loop_thread, AlwaysFailQuery(), feed,
            monotonic=fake_monotonic, disconnected_after=5.0)
        bridge.start()
        try:
            assert wait_until(
                lambda: bridge.health()["sources"]["snapshot"]["reconnects"] >= 1)
            assert bridge.lifecycle is BridgeLifecycle.DEGRADED
            clock["t"] += 10.0
            assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.DISCONNECTED)
        finally:
            bridge.stop()


class TestStopReliability:
    """FIX #2: stop() must reliably join an in-flight long-poll, and every
    loop.call_soon_threadsafe must be guarded against a closed/None loop."""

    def test_stop_joins_thread_through_an_in_flight_long_poll(self, loop_thread):
        feed = BlockingFeed(delay=0.3)
        bridge, _, _ = _bridge(
            loop_thread, FakeQueryClient([_snapshot(10)]), feed, wait_ms=300)
        bridge.start()
        assert wait_until(lambda: bridge.lifecycle is BridgeLifecycle.LIVE)
        assert wait_until(lambda: feed.calls >= 1)
        # A poll is in flight (blocked inside feed.call) right now; the
        # caller's timeout is deliberately shorter than the poll itself.
        bridge.stop(timeout=0.05)
        assert not bridge.is_alive()

    def test_schedule_guards_closed_loop(self):
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        bridge, _, _ = _bridge(
            closed_loop, FakeQueryClient([_snapshot(10)]), ScriptedFeed())
        with pytest.raises(RuntimeError):
            bridge._schedule(lambda: None)


import msgpack
import zmq

from web.command_center.quotes import QuotePlane, clamp_hz, normalize_ticker


class _CollectingLoop:
    """Minimal loop stand-in: records deliveries synchronously."""

    def __init__(self):
        self.batches: list[dict] = []

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class TestQuoteNormalization:
    def test_dict_ticker_normalizes(self):
        quote = normalize_ticker({
            "conId": 265598, "bid": 199.4, "ask": 199.6, "last": 199.5,
            "time": "2026-07-15T14:30:00+00:00", "feed": "live"})
        assert quote == {
            "instrument_id": "265598", "bid": 199.4, "ask": 199.6, "last": 199.5,
            "market_timestamp": "2026-07-15T14:30:00+00:00", "feed_type": "live"}

    def test_unknown_shape_returns_none(self):
        assert normalize_ticker({"nothing": "useful"}) is None
        assert normalize_ticker(b"garbage") is None

    def test_hz_clamped_to_two_to_five(self):
        assert clamp_hz(0.5) == 2.0
        assert clamp_hz(4.0) == 4.0
        assert clamp_hz(60.0) == 5.0


class TestQuoteConflation:
    def _plane(self, hz=4.0):
        loop = _CollectingLoop()
        plane = QuotePlane("tcp://127.0.0.1", 0, loop,
                           lambda batch: loop.batches.append(batch), hz=hz,
                           decode=msgpack.unpackb)
        return plane, loop

    def test_conflation_keeps_latest_value_only(self):
        plane, loop = self._plane()
        for last in (100.0, 101.0, 102.5):
            plane.ingest({"conId": 265598, "bid": last - 0.1, "ask": last + 0.1,
                          "last": last})
        assert plane.flush_due(now=10.0)
        assert len(loop.batches) == 1
        assert loop.batches[0]["265598"]["last"] == 102.5

    def test_flush_respects_rate(self):
        plane, loop = self._plane(hz=4.0)  # interval 0.25s
        plane.ingest({"conId": 1, "last": 1.0})
        assert plane.flush_due(now=10.0)
        plane.ingest({"conId": 1, "last": 2.0})
        assert not plane.flush_due(now=10.1)   # too soon
        assert plane.flush_due(now=10.26)
        assert [b["1"]["last"] for b in loop.batches] == [1.0, 2.0]

    def test_malformed_payloads_dropped_and_counted(self):
        plane, loop = self._plane()
        plane.ingest({"useless": True})
        plane.ingest(12345)
        assert plane.dropped == 2
        assert not plane.flush_due(now=10.0)
        assert loop.batches == []


class TestQuoteSocketRoundTrip:
    def test_subscriber_thread_receives_published_quote(self):
        ctx = zmq.Context()
        pub = ctx.socket(zmq.PUB)
        port = pub.bind_to_random_port("tcp://127.0.0.1")
        loop = _CollectingLoop()
        received = threading.Event()

        def deliver(batch):
            loop.batches.append(batch)
            received.set()

        plane = QuotePlane("tcp://127.0.0.1", port, loop, deliver,
                           hz=5.0, decode=msgpack.unpackb)
        plane.start()
        try:
            deadline = time.monotonic() + 3
            payload = msgpack.packb({"conId": 4815747, "last": 172.4})
            while not received.is_set() and time.monotonic() < deadline:
                pub.send_multipart([b"", payload])  # re-send until SUB connects
                time.sleep(0.05)
            assert received.is_set()
            assert loop.batches[-1]["4815747"]["last"] == 172.4
        finally:
            plane.stop()
            pub.close(0)
            ctx.term()


class TestQuoteSecuritySeam:
    """The default decoder is the dill gate — it must refuse without opt-in
    and must parse MMR_DILL_STRICT exactly as clientserver.DILL_STRICT_MODE
    does ('1'/'true'/'yes', case-insensitive), so a value the rest of the
    system honors can't silently kill the quote plane here."""

    def test_default_decode_refuses_without_opt_in(self, monkeypatch):
        from web.command_center.quotes import _default_decode
        monkeypatch.delenv("MMR_DILL_STRICT", raising=False)
        with pytest.raises(RuntimeError, match="MMR_DILL_STRICT"):
            _default_decode(b"\x81\xa5conId\xcd\x01\x02")
        monkeypatch.setenv("MMR_DILL_STRICT", "0")
        with pytest.raises(RuntimeError, match="MMR_DILL_STRICT"):
            _default_decode(b"\x81\xa5conId\xcd\x01\x02")

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
    def test_default_decode_accepts_every_strict_spelling(self, monkeypatch, value):
        from web.command_center.quotes import _default_decode
        monkeypatch.setenv("MMR_DILL_STRICT", value)
        decoded = _default_decode(msgpack.packb({"conId": 265598, "last": 1.0}))
        assert decoded == {"conId": 265598, "last": 1.0}


class TestQuoteLoopGuard:
    def _plane(self, loop):
        return QuotePlane("tcp://127.0.0.1", 1, loop, lambda batch: None,
                          decode=msgpack.unpackb)

    def test_closed_loop_drops_batch_without_raising(self):
        class ClosedLoop:
            def is_closed(self):
                return True

            def call_soon_threadsafe(self, fn, *args):  # pragma: no cover
                raise AssertionError("must not be called on a closed loop")

        plane = self._plane(ClosedLoop())
        plane.ingest({"conId": 265598, "last": 1.0})
        assert plane.flush_due(now=1e9) is False

    def test_loop_raising_runtimeerror_drops_batch_without_raising(self):
        class RejectingLoop:
            def call_soon_threadsafe(self, fn, *args):
                raise RuntimeError("Event loop is closed")

        plane = self._plane(RejectingLoop())
        plane.ingest({"conId": 265598, "last": 1.0})
        assert plane.flush_due(now=1e9) is False

    def test_none_loop_drops_batch_without_raising(self):
        plane = self._plane(None)
        plane.ingest({"conId": 265598, "last": 1.0})
        assert plane.flush_due(now=1e9) is False


class TestQuotePlaneLifecycle:
    def test_stop_terminates_thread_and_plane_is_restartable(self):
        loop = _CollectingLoop()
        plane = QuotePlane("tcp://127.0.0.1", 1, loop, lambda b: None,
                           decode=msgpack.unpackb)
        plane.start()
        assert plane._thread is not None and plane._thread.is_alive()
        plane.stop()
        assert not plane._thread.is_alive(), "stop() must join the thread"
        # Restart: without _stop.clear() the new thread exits immediately
        # and the plane silently delivers nothing while appearing started.
        plane.start()
        try:
            assert wait_until(lambda: plane._thread.is_alive(), timeout=2.0)
            assert not plane._stop.is_set()
        finally:
            plane.stop()
            assert not plane._thread.is_alive()


class TestQuoteMultiInstrument:
    def test_different_instruments_coexist_in_one_batch(self):
        batches: list[dict] = []
        loop = _CollectingLoop()
        plane = QuotePlane("tcp://127.0.0.1", 1, loop, batches.append,
                           decode=msgpack.unpackb)
        plane.ingest({"conId": 265598, "last": 210.0})
        plane.ingest({"conId": 4815747, "last": 172.0})
        plane.ingest({"conId": 265598, "last": 211.0})  # conflates 265598 only
        assert plane.flush_due(now=1e9) is True
        assert batches == [{
            "265598": {"instrument_id": "265598", "bid": None, "ask": None,
                        "last": 211.0, "market_timestamp": None, "feed_type": None},
            "4815747": {"instrument_id": "4815747", "bid": None, "ask": None,
                         "last": 172.0, "market_timestamp": None, "feed_type": None},
        }]
