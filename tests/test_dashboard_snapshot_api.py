import asyncio
import datetime as dt
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus

from cc_fakes import NullBridge as _NullBridge, NullQuotePlane as _NullQuotePlane
from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center import CommandCenter, CommandCenterConfig
from web.command_center.session import DashboardCredentials

UTC = dt.timezone.utc
SECRET = "s" * 64
TOKEN = "test-token"


@pytest.fixture
def cc(monkeypatch):
    monkeypatch.setenv("MMR_DILL_STRICT", "1")
    center = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=SECRET.encode(), legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: _NullBridge(),
        quote_plane_factory=lambda loop, deliver: _NullQuotePlane(),
    )
    return center


@pytest.fixture
def app(cc):
    from web.app import create_app
    return create_app(cc)


def _seed(cc, positions=1):
    cc.state.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
            "position": [{"entity_id": f"DU123:{i}", "entity_revision": 1,
                          "quantity": 10 * i, "currency": "USD"}
                         for i in range(1, positions + 1)],
            "account": [{"entity_id": "DU123", "entity_revision": 1,
                         "net_liquidation": 50_000.0, "mode": "paper"}],
        }),
        stream_id="stream-t")


async def _login(client):
    response = await client.post("/session", data={"token": TOKEN})
    assert response.status_code in (200, 303)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def client(app):
    """A real, locally-bound uvicorn server -- deliberately NOT
    ``httpx.ASGITransport``.

    ``ASGITransport.handle_async_request`` awaits the whole ASGI app call
    to completion before it ever returns a ``Response`` (it collects every
    body chunk into a list first, then hands back a static replay of
    them) -- fine for ordinary request/response routes, but incompatible
    with ``/api/events``'s deliberately-indefinite SSE connection:
    ``EventSourceResponse``'s disconnect detection polls ``receive()`` for
    ``http.disconnect``, and ``ASGITransport``'s fake ``receive()`` can only
    report that AFTER the response has already fully completed -- an
    unbreakable deadlock for a stream that, by design (spec §7), never
    completes on its own while a client is connected. A real server on a
    real loopback socket detects the client closing its side the normal
    way (the OS reports the dropped connection), so these tests can
    actually observe live streaming and a disconnect-triggered unregister.
    """
    # sse_starlette.sse.AppStatus.should_exit_event is a process-wide
    # singleton bound to whichever event loop first touches it. Each test
    # here spins up a brand-new uvicorn server -- and therefore a
    # brand-new event loop -- so a stale event left over from an earlier
    # test's (now-dead) loop must be cleared, or EventSourceResponse's
    # listen_for_exit_signal task raises "bound to a different event
    # loop" against the current one.
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "test uvicorn server failed to start within 5s"
    try:
        yield httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


class TestSnapshotApi:
    @pytest.mark.asyncio
    async def test_snapshot_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            assert (await c.get("/api/snapshot")).status_code == 401

    @pytest.mark.asyncio
    async def test_snapshot_503_before_baseline(self, client, cc):
        async with client as c:
            await _login(c)
            assert (await c.get("/api/snapshot")).status_code == 503

    @pytest.mark.asyncio
    async def test_snapshot_shape_and_polling_fallback(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            body = (await c.get("/api/snapshot")).json()
            for key in ("schema_version", "stream_id", "sequence", "generated_at",
                        "accounts", "positions", "quotes", "proposals", "orders",
                        "fills", "strategies", "risk", "health"):
                assert key in body
            assert body["accounts"][0]["net_liquidation"] == 50_000.0
            # polling fallback sees new events on the next poll
            envelope = cc.state.apply(DomainEvent(
                event_id="evt-9", source_cursor=9, entity_revision=2,
                event_type="position.updated", entity_type="position",
                entity_id="DU123:1", operation="upsert", account_id="DU123",
                source="trader_service",
                source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                correlation_id=None, payload={"quantity": 77}))
            cc.fanout.publish(envelope)
            second = (await c.get("/api/snapshot")).json()
            assert second["sequence"] == body["sequence"] + 1
            assert second["positions"][0]["quantity"] == 77


class TestEventsEndpoint:
    @pytest.mark.asyncio
    async def test_sse_handshake_replays_then_snapshots_quotes(self, client, cc):
        _seed(cc)
        envelope = cc.state.apply(DomainEvent(
            event_id="evt-1", source_cursor=1, entity_revision=2,
            event_type="position.updated", entity_type="position",
            entity_id="DU123:1", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            correlation_id=None, payload={"quantity": 5}))
        cc.fanout.publish(envelope)
        cc.fanout.publish_quotes({"1": {"instrument_id": "1", "last": 42.0}})
        async with client as c:
            await _login(c)
            frames = []
            async with c.stream("GET", "/api/events?after=stream-t:0") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    frames.append(line)
                    if line.startswith("event: quotes.snapshot"):
                        break
            joined = "\n".join(frames)
            assert "id: stream-t:1" in joined            # replayed domain event
            assert "event: quotes.snapshot" in joined
            snap_index = joined.index("event: quotes.snapshot")
            assert "id:" not in joined[snap_index:]       # control frame has no SSE id

    @pytest.mark.asyncio
    async def test_stale_last_event_id_gets_resync_required(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            frames = []
            async with c.stream("GET", "/api/events",
                                headers={"last-event-id": "dead-stream:5"}) as response:
                async for line in response.aiter_lines():
                    frames.append(line)
                    if line.startswith("event: resync_required"):
                        break
            assert any(line.startswith("event: resync_required") for line in frames)

    @pytest.mark.asyncio
    async def test_generator_unregisters_client_on_disconnect(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            async with c.stream("GET", "/api/events") as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: quotes.snapshot"):
                        break
            await asyncio.sleep(0.05)  # let the finally block run
        assert cc.fanout.client_count() == 0


class TestCommandCenterPage:
    @pytest.mark.asyncio
    async def test_page_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            response = await c.get("/cc")
            assert response.status_code == 303
            assert response.headers["location"] == "/cc/login"

    @pytest.mark.asyncio
    async def test_page_renders_layout_a_regions(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            for element_id in ("status-bar", "mode-badge", "account-id",
                               "dependency-chips", "last-event-time",
                               "account-cards", "positions-panel", "action-rail",
                               "orders-panel", "fills-panel", "strategies-panel",
                               "risk-panel", "degraded-banner", "drawer"):
                assert f'id="{element_id}"' in html
            assert '/static/command_center.js' in html
            assert 'data-degraded-after-ms="15000"' in html
            assert 'data-poll-interval-ms="5000"' in html

    @pytest.mark.asyncio
    async def test_risk_panel_defaults_to_unavailable_not_green(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            assert 'data-state="unavailable"' in html
            assert 'Risk unavailable' in html
