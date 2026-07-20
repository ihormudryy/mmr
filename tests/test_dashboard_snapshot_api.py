import asyncio
import datetime as dt
import json
import re
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
from web.command_center.flags import CommandFlags
from web.command_center.session import DashboardCredentials

UTC = dt.timezone.utc
SECRET = "s" * 64
TOKEN = "test-token"

# [M1-C] UI-wiring pass: static, server-templated markers that only appear
# in command_center.html when `commands_enabled` is true (see
# routes_read.py's `/cc` handler and the template's `{% if commands_enabled
# %}` guards) -- the gated Actions table columns (positions/strategies), the
# "Cancel all" trigger, and the command drawer/dialog containers the
# client-side per-row buttons (Approve/Reject/Close/Cancel/Enable/Disable/
# Edit params -- rendered by command_center.js, not this server template)
# open. Shared by the enabled/disabled page-render tests below so the two
# assertions can never drift apart.
_CC_AFFORDANCE_MARKERS = (
    'data-cc-col="position-actions"',
    'data-cc-col="strategy-actions"',
    'id="cc-cancel-all-open"',
    'id="cc-open-proposal"',
    'id="cc-proposal-drawer"',
    'id="cc-close-drawer"',
    'id="cc-confirm-drawer"',
    'id="cc-cancel-all-dialog"',
    'id="cc-strategy-params-dialog"',
    'id="cc-pause-control"',
)


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

    @pytest.mark.asyncio
    async def test_snapshot_includes_paper_automation_status(self, client, cc):
        class QueryClient:
            def call(self, method, body, response_model, timeout=None):
                assert (method, body, response_model) == (
                    "get_paper_automation_status", {}, dict)
                assert timeout is not None
                return {"enabled": True, "strategy_name": "orb",
                        "restart_required": True}

        _seed(cc)
        cc._query_client = QueryClient()
        async with client as c:
            await _login(c)
            body = (await c.get("/api/snapshot")).json()
        assert body["paper_automation"] == {
            "enabled": True, "strategy_name": "orb", "restart_required": True,
        }

    @pytest.mark.asyncio
    async def test_snapshot_degrades_when_paper_automation_query_unavailable(
            self, client, cc):
        class UnavailableQueryClient:
            def call(self, method, body, response_model, timeout=None):
                raise ConnectionError("typed query socket unavailable")

        _seed(cc)
        cc._query_client = UnavailableQueryClient()
        async with client as c:
            await _login(c)
            response = await c.get("/api/snapshot")
        assert response.status_code == 200
        assert response.json()["paper_automation"] is None


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


class TestHealthEndpoints:
    """`/api/cc-health` — session-gated, NEW route (M1-R Task 7 addendum).

    The G0 `/healthz` / `/readyz` / `/api/health` routes in `web/app.py` stay
    exactly as they are (LB-drain readiness + their own `MMR_WEB_TOKEN` gate)
    and are pinned, unedited, by `tests/test_service_health.py`. This class
    covers the rich command-center dependency detail on the new path only.
    """

    @pytest.mark.asyncio
    async def test_cc_health_requires_session(self, client, cc):
        _seed(cc)
        async with client as c:
            assert (await c.get("/api/cc-health")).status_code == 401

    @pytest.mark.asyncio
    async def test_cc_health_ready_field_flips_with_baseline(self, client, cc):
        async with client as c:
            await _login(c)
            before = (await c.get("/api/cc-health")).json()
            assert before["ready"] is False
            _seed(cc)
            after = (await c.get("/api/cc-health")).json()
            assert after["ready"] is True

    @pytest.mark.asyncio
    async def test_cc_health_detail_and_redaction(self, client, cc):
        _seed(cc)
        # The `client` fixture runs uvicorn with `lifespan="off"` (see its
        # docstring), so `CommandCenter._start_or_degrade` -- and therefore
        # the `bridge_factory` that would normally install `cc.bridge` --
        # never runs. Assign the fake bridge directly, the same way `_seed`
        # bypasses the bridge's own `_resync_baseline` to drive `cc.state`.
        cc.bridge = _NullBridge()
        async with client as c:
            await _login(c)
            body = (await c.get("/api/cc-health")).json()
            assert set(body) >= {"lifecycle", "reconnects", "cursor", "stream_id",
                                 "sequence", "last_event_at", "transport_lag_ms",
                                 "sse_clients", "sources", "quote_plane",
                                 "replay_ring_events", "terminal_rows",
                                 "client_fifo_depth_max"}
            journal = body["sources"]["journal"]
            assert set(journal) == {"state", "last_success_age_seconds",
                                    "last_error", "reconnects"}
            assert set(body["quote_plane"]) == {"instruments", "feed_types", "dropped"}
            # COMPAT Task 4 soak-metric exporters: read-model counts backing
            # the previously-unmetered replay-ring/FIFO/terminal-rows soak
            # thresholds must be present and integer.
            for key in ("replay_ring_events", "terminal_rows", "client_fifo_depth_max"):
                assert isinstance(body[key], int)
            encoded = json.dumps(body).lower()
            assert TOKEN.lower() not in encoded
            assert SECRET.lower() not in encoded

    @pytest.mark.asyncio
    async def test_cc_health_read_model_counts_reflect_seeded_state(self, client, cc):
        """COMPAT Task 4: `replay_ring_events`/`terminal_rows` must track
        the live read model (not a stubbed/hardcoded value) so the soak
        runner's sampled maxima mean something."""
        _seed(cc)
        cc.bridge = _NullBridge()
        envelope = cc.state.apply(DomainEvent(
            event_id="evt-terminal", source_cursor=3, entity_revision=2,
            event_type="order.updated", entity_type="order",
            entity_id="ord-term-1", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime.now(UTC),
            correlation_id=None, payload={"status": "FILLED"}))
        cc.fanout.publish(envelope)
        async with client as c:
            await _login(c)
            body = (await c.get("/api/cc-health")).json()
            assert body["replay_ring_events"] == cc.state.ring_depth() >= 1
            assert body["terminal_rows"] == cc.state.terminal_row_count() >= 1
            assert body["client_fifo_depth_max"] == cc.fanout.max_fifo_depth()

    @pytest.mark.asyncio
    async def test_transport_lag_recorded_after_event(self, client, cc):
        _seed(cc)
        envelope = cc.state.apply(DomainEvent(
            event_id="evt-lag", source_cursor=2, entity_revision=3,
            event_type="position.updated", entity_type="position",
            entity_id="DU123:1", operation="upsert", account_id="DU123",
            source="trader_service",
            source_timestamp=dt.datetime.now(UTC) - dt.timedelta(milliseconds=120),
            correlation_id=None, payload={"quantity": 1}))
        cc.fanout.publish(envelope)
        async with client as c:
            await _login(c)
            lag = (await c.get("/api/cc-health")).json()["transport_lag_ms"]
            assert lag is not None and lag >= 100.0


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
            # [M1-C] UI-wiring pass: commands are OFF by default (no
            # DASHBOARD_COMMANDS_ENABLED in this test process's environment,
            # see web/app.py's module-level `_COMMAND_FLAGS`) -- the page
            # must render exactly as the pre-existing read-only [M1-R]
            # dashboard, with none of the command action affordances present.
            assert 'data-commands-enabled="false"' in html
            for cc_marker in _CC_AFFORDANCE_MARKERS:
                assert cc_marker not in html

    @pytest.mark.asyncio
    async def test_risk_panel_defaults_to_unavailable_not_green(self, client, cc):
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            assert 'data-state="unavailable"' in html
            assert 'Risk unavailable' in html

    @pytest.mark.asyncio
    async def test_page_renders_command_affordances_when_enabled(self, client, cc, app):
        """[M1-C] UI-wiring pass: with commands_enabled=True, the `/cc` page
        must render the command-affordance containers (the per-row Approve/
        Reject/Close/Cancel/Enable/Disable buttons are rendered client-side
        by command_center.js from the live snapshot -- see that file's new
        "[M1-C] UI-wiring pass" section -- so this server-rendered-HTML test
        can only observe the static markers: the gated Actions columns, the
        "Cancel all" trigger, and the drawer/dialog containers those client
        buttons open). A PENDING proposal / an active order / a strategy are
        seeded in the backing store regardless, so this exercises the same
        `/cc` code path a real commands-enabled deployment would serve.
        """
        app.state.command_flags = CommandFlags(True, False, None, None)
        cc.state.install_baseline(
            SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
                "position": [{"entity_id": "DU123:1", "entity_revision": 1,
                              "quantity": 10, "currency": "USD", "conid": 1}],
                "account": [{"entity_id": "DU123", "entity_revision": 1,
                             "net_liquidation": 50_000.0, "mode": "paper"}],
                "proposal": [{"entity_id": "1", "entity_revision": 1, "id": 1,
                              "status": "PENDING", "account_mode": "paper",
                              "symbol": "AAPL", "action": "BUY"}],
                "order": [{"entity_id": "ord-1", "entity_revision": 1,
                           "order_entity_id": "ord-1", "account_id": "DU123",
                           "conid": 1, "status": "SUBMITTED", "leg": "entry"}],
                "strategy": [{"entity_id": "my_strategy", "entity_revision": 1,
                              "strategy_name": "my_strategy",
                              "strategy_state": "RUNNING", "control_revision": 1}],
            }),
            stream_id="stream-t")
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            assert 'data-commands-enabled="true"' in html
            for cc_marker in _CC_AFFORDANCE_MARKERS:
                assert cc_marker in html

    @pytest.mark.asyncio
    async def test_command_affordances_have_no_inline_event_handlers(self, client, cc, app):
        """[M1-C] fix wave I-1: `/cc` is served with a strict CSP
        (`script-src 'self'`, no `'unsafe-inline'` -- see
        `web/command_center/session.py`'s `_STRICT_CSP`), so ANY inline
        `on*="..."` attribute is silently blocked by a real browser -- the
        element renders but the handler never fires. `ccCancelAll()` (the
        "Cancel all" button) and `ccOpenProposalDrawer()` (the "New
        proposal" button) were previously wired via inline `onclick=`, with
        no `addEventListener` fallback anywhere in command_center.js, so
        both buttons were dead on a real page load. This regression guard
        greps the ENTIRE rendered command-enabled page for any ` on\\w+=`
        attribute -- not just the two named handlers -- so any future
        CSP-incompatible handler (onsubmit, oninput, onchange, ...) trips
        this test too."""
        app.state.command_flags = CommandFlags(True, False, None, None)
        _seed(cc)
        async with client as c:
            await _login(c)
            html = (await c.get("/cc")).text
            assert 'data-commands-enabled="true"' in html
            assert re.search(r" on\w+=", html) is None, (
                "found an inline event-handler attribute in /cc's HTML -- "
                "this is silently blocked by the page's strict CSP "
                "(script-src 'self', no 'unsafe-inline'); bind it via "
                "addEventListener in command_center.js instead")
            # The two previously-dead command-initiation buttons must still
            # be present and addressable by id (used by command_center.js
            # to bind their click handlers post-fix).
            assert 'id="cc-cancel-all-open"' in html
            assert 'id="cc-open-proposal"' in html
