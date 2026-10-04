"""Regression: /cc manage overlay must not wedge Starlette TestClient.

``asyncio.wait_for(asyncio.to_thread(manage_context))`` cancels the awaitable
on timeout but leaves the worker thread running; TestClient's portal then
``thread.join()``s forever. The page handler must abandon a slow manage fetch
without tying an asyncio Task to that thread.
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

import web.app as webapp
from cc_fakes import NullBridge, NullQuotePlane

TEST_TOKEN = 'test-dashboard-token'
TEST_SECRET = 's' * 64


def test_cc_manage_timeout_does_not_wedge_testclient(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def _slow_manage(flash=''):
        started.set()
        release.wait(timeout=60)
        return {
            'strategies': [],
            'available_strategies': [],
            'watchlists': [],
            'deployed_count': 0,
            'flash': flash,
            'flash_err': False,
            'csrf_token': webapp._CSRF_TOKEN,
            'errors': {},
        }

    import web.command_center.routes_read as routes_read
    monkeypatch.setattr(routes_read, 'MANAGE_PAGE_TIMEOUT_S', 0.25)
    monkeypatch.setattr(
        webapp, '_manage_page_context',
        lambda flash='': (_slow_manage(flash), {}))

    from web.command_center import CommandCenter, CommandCenterConfig
    from web.command_center.session import DashboardCredentials

    cc = CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TEST_TOKEN, session_secret=TEST_SECRET.encode(),
            legacy_alias_used=False),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *a, **k: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane(),
    )
    app = webapp.create_app(cc)
    client = TestClient(app)
    client.post('/session', data={'token': TEST_TOKEN})

    t0 = time.monotonic()
    r = client.get('/cc')
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    assert elapsed < 2.0, (
        f'/cc took {elapsed:.1f}s — portal likely wedged on manage thread')
    assert started.wait(timeout=1.0), 'manage overlay never started'
    # Request returned while the worker is still blocked — proof we abandoned.
    assert not release.is_set()
    release.set()
