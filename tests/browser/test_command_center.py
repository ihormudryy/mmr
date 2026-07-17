"""Browser gate for the read-only command center (spec §13.2).

Runs only in the pinned Playwright/Chromium image (CI browser job) or when
MMR_BROWSER_TESTS=1 locally after `playwright install chromium`.

`playwright` is a `browser-test` EXTRA (not installed under `--extra test`,
the environment M1-R is developed/tested in) -- `pytest.importorskip` below
makes this whole module SKIP cleanly at collection time when it's absent, so
the default test suite never fails or errors because of this file.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MMR_BROWSER_TESTS") != "1",
    reason="browser gate runs in the pinned Playwright image (set MMR_BROWSER_TESTS=1)")

playwright_api = pytest.importorskip("playwright.sync_api")

TOKEN = "browser-token"

# `tests/browser/` has no `__init__.py`, so pytest's default "prepend" import
# mode inserts THIS file's own directory onto sys.path, not `tests/` -- a bare
# `from cc_fakes import ...` would miss it. `import tests.cc_fakes` doesn't
# work either: some third-party dependency ships its own top-level `tests`
# package into site-packages, and a regular (non-namespace) package found
# earlier on sys.path wins outright over the repo's `tests/` directory.
# Explicitly adding the repo's `tests/` dir to sys.path and importing
# `cc_fakes` as a flat module sidesteps both problems.
_TESTS_DIR = str(Path(__file__).resolve().parent.parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed(cc):
    from trader.domain.events import SnapshotWithCursor
    cc.state.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={
            "account": [{"entity_id": "DU111", "entity_revision": 1,
                         "mode": "paper", "net_liquidation": 250_000.0,
                         "currency": "USD"}],
            "position": [{"entity_id": "DU111:265598", "entity_revision": 1,
                          "conid": 265598, "symbol": "AAPL", "quantity": 100,
                          "avg_cost": 180.0, "currency": "USD",
                          "unrealized_pnl": 1_250.0}],
            "proposal": [{"entity_id": "42", "entity_revision": 1,
                          "status": "PENDING", "action": "BUY", "symbol": "AMD",
                          "quantity": 50, "confidence": 0.7,
                          "reasoning": "Breakout above resistance",
                          "sizing_result": {"reasoning": [
                              "base 2% of equity", "confidence scale 0.7",
                              "ATR volatility adjustment 0.8"]}}],
        }),
        stream_id="browser-stream")


@pytest.fixture(scope="module")
def server():
    os.environ.setdefault("MMR_DILL_STRICT", "1")
    os.environ["CC_DEGRADED_AFTER_MS"] = "1000"   # spec default 15000; shrunk for test speed
    os.environ["CC_POLL_INTERVAL_MS"] = "500"     # spec default 5000
    from fastapi.responses import JSONResponse
    from web.app import create_app
    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials
    from cc_fakes import NullBridge, NullQuotePlane

    cc = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN, session_secret=b"s" * 64, legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane())
    app = create_app(cc)
    broken = {"sse": False}

    @app.middleware("http")
    async def _breaker(request, call_next):
        if broken["sse"] and request.url.path == "/api/events":
            return JSONResponse({"detail": "broken"}, status_code=503)
        return await call_next(request)

    @app.post("/_test/break-sse")
    async def _break():
        broken["sse"] = True
        cc.fanout.broadcast_resync()   # kick live clients into reconnect
        return {"ok": True}

    @app.post("/_test/fix-sse")
    async def _fix():
        broken["sse"] = False
        return {"ok": True}

    import uvicorn
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not uv_server.started and time.time() < deadline:
        time.sleep(0.05)
    _seed(cc)  # before any client connects; the loop has no state readers yet
    yield f"http://127.0.0.1:{port}", cc
    uv_server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def page(server):
    base_url, _cc = server
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context().new_page()
        page.goto(f"{base_url}/cc")           # redirects to /cc/login
        page.fill("#token", TOKEN)
        page.click("button[type=submit]")
        page.wait_for_selector("#positions-body tr")
        yield page
        browser.close()


class TestLayoutA:
    def test_regions_render_with_positions_dominant(self, page):
        for selector in ("#status-bar", "#account-cards", "#positions-panel",
                         "#action-rail", "#orders-panel", "#fills-panel",
                         "#strategies-panel", "#risk-panel"):
            assert page.is_visible(selector), selector
        positions = page.locator("#positions-panel").bounding_box()
        rail = page.locator("#action-rail").bounding_box()
        assert positions["width"] > rail["width"]  # dominant workspace

    def test_status_bar_shows_mode_badge_and_exact_account(self, page):
        assert page.inner_text("#mode-badge") == "PAPER"
        assert "DU111" in page.inner_text("#account-id")

    def test_account_card_shows_net_liquidation_from_account_entity(self, page):
        assert "250,000" in page.inner_text("#account-cards")


class TestFreshnessAndAlerts:
    def test_position_row_shows_freshness_age(self, page):
        age = page.inner_text("#positions-body tr .age")
        assert age  # "no data" without quotes; the row carries the stale class
        assert "stale" in (page.get_attribute("#positions-body tr", "class") or "")

    def test_risk_unavailable_alert_is_not_color_only(self, page):
        text = page.inner_text("#risk-body")
        assert "Risk unavailable" in text and "⚠" in text
        assert page.get_attribute("#risk-body", "data-state") == "unavailable"


class TestDrawerKeyboardFocus:
    def test_enter_opens_drawer_focuses_close_escape_returns_focus(self, page):
        card = page.locator(".proposal-card[data-proposal]").first
        card.focus()
        page.keyboard.press("Enter")
        assert page.is_visible("#drawer")
        assert page.evaluate("document.activeElement.id") == "drawer-close"
        detail = page.inner_text("#drawer-content").lower()
        assert "sizing" in detail and "atr volatility" in detail  # full chain
        page.keyboard.press("Escape")
        assert page.is_hidden("#drawer")
        assert page.evaluate("!!document.activeElement.dataset.proposal")


class TestDegradedFallback:
    def test_sse_loss_shows_banner_polls_then_recovers(self, page, server):
        base_url, _cc = server
        page.request.post(f"{base_url}/_test/break-sse")
        page.wait_for_selector("#degraded-banner:not([hidden])", timeout=10_000)
        assert "degraded" in page.inner_text("#degraded-banner").lower()
        # polling keeps the page alive: snapshot data still renders
        assert page.is_visible("#positions-body tr")
        page.request.post(f"{base_url}/_test/fix-sse")
        # back to SSE only after a coherent snapshot; banner then clears
        page.wait_for_selector("#degraded-banner[hidden]", state="attached",
                               timeout=15_000)
