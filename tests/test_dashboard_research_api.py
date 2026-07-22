from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

from cc_fakes import NullBridge, NullQuotePlane
from trader.domain.events import SnapshotWithCursor
from trader.tools.massive_research import ResearchResult
from web.app import create_app
from web.command_center import CommandCenter, CommandCenterConfig
from web.command_center.flags import CommandFlags
from web.command_center.research import ResearchError, ResearchService
from web.command_center.routes_research import create_research_router
from web.command_center.session import (
    DashboardCredentials,
    SessionSecurityMiddleware,
    create_session_router,
)


TOKEN = "research-test-token"
SECRET = b"r" * 64


class FakeResearchProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.threads: list[tuple[str, str]] = []

    def snapshot(self, symbol: str) -> ResearchResult:
        self.calls.append(("snapshot", {"symbol": symbol}))
        self.threads.append(("snapshot", threading.current_thread().name))
        return ResearchResult({"ticker": symbol.strip().upper()}, "Snapshot")

    def news(self, ticker: str, *, limit: int, source: str) -> ResearchResult:
        self.calls.append((
            "news", {"ticker": ticker, "limit": limit, "source": source}))
        self.threads.append(("news", threading.current_thread().name))
        return ResearchResult([], "News")

    def movers(
        self, *, market: str, direction: str, limit: int, detail: bool = False,
    ) -> ResearchResult:
        self.calls.append(("movers", {
            "market": market, "direction": direction, "limit": limit,
            "detail": detail,
        }))
        self.threads.append(("movers", threading.current_thread().name))
        return ResearchResult([], "Movers")

    def ideas(self, **kwargs: Any) -> ResearchResult:
        self.calls.append(("ideas", kwargs))
        self.threads.append(("ideas", threading.current_thread().name))
        return ResearchResult([], "Ideas")

    def presets(self) -> ResearchResult:
        return ResearchResult([], "Presets", provider="local")


class RecordingResearchService(ResearchService):
    def __init__(self, provider: FakeResearchProvider) -> None:
        super().__init__(
            lambda: provider,
            clock=lambda: "2026-07-22T12:00:00Z",
        )
        self.admissions = 0
        self.close_calls = 0

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.admissions += 1
        return await super().run(*args, **kwargs)

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class ErrorResearchService(ResearchService):
    def __init__(self, error: ResearchError) -> None:
        super().__init__(lambda: None)
        self.error = error

    async def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise self.error


@pytest.fixture
def research_provider() -> FakeResearchProvider:
    return FakeResearchProvider()


@pytest.fixture
def research_service(
    research_provider: FakeResearchProvider,
) -> RecordingResearchService:
    service = RecordingResearchService(research_provider)
    yield service
    service.close()


@pytest.fixture
def research_cc(monkeypatch) -> CommandCenter:
    class EmptyManageClient:
        def trader_query(self, method, body=None):
            if method == "list_universes":
                return {"universes": []}
            return {}

        def strategy_query(self, method, body=None):
            if method == "list_strategies":
                return {"strategies": []}
            return {}

    monkeypatch.setattr(
        "web.app.get_manage_client",
        lambda: EmptyManageClient(),
    )
    return CommandCenter(
        CommandCenterConfig(),
        credentials_loader=lambda: DashboardCredentials(
            token=TOKEN,
            session_secret=SECRET,
            legacy_alias_used=False,
        ),
        query_client_factory=lambda: None,
        feed_client_factory=lambda: None,
        bridge_factory=lambda *args, **kwargs: NullBridge(),
        quote_plane_factory=lambda loop, deliver: NullQuotePlane(),
    )


@pytest.fixture
def app_with_research(
    research_cc: CommandCenter,
    research_service: ResearchService,
):
    return create_app(research_cc, research_service)


@pytest.fixture
def logged_in_research_client(app_with_research) -> TestClient:
    client = TestClient(app_with_research)
    response = client.post("/session", data={"token": TOKEN})
    assert response.status_code in (200, 303)
    return client


def _login(client: TestClient) -> None:
    response = client.post("/session", data={"token": TOKEN})
    assert response.status_code in (200, 303)


def _router_app(
    cc: CommandCenter,
    service: ResearchService,
    universe_loader,
) -> FastAPI:
    app = FastAPI()

    def manager_provider():
        return cc.ensure_session_manager()

    app.add_middleware(
        SessionSecurityMiddleware,
        manager_provider=manager_provider,
    )
    app.include_router(create_session_router(
        cc.ensure_session_manager,
        cc.limiter,
        cookie_secure=cc.config.cookie_secure,
    ))
    app.include_router(create_research_router(
        cc,
        service,
        load_universe_symbols=universe_loader,
    ))
    return app


def test_research_routes_require_session(app_with_research):
    client = TestClient(app_with_research)
    for path in (
        "presets",
        "ideas",
        "movers",
        "snapshot?symbol=AAPL",
        "news?ticker=AAPL",
    ):
        assert client.get(f"/api/research/{path}").status_code == 401


def test_research_shell_is_the_sixth_tab(logged_in_research_client):
    html = logged_in_research_client.get("/cc").text
    assert 'data-dash-tab="research"' in html
    assert 'id="dash-research"' in html
    assert html.index('data-dash-tab="research"') > html.index(
        'data-dash-tab="guide"')
    for tool in (
        "ideas", "movers", "lookup", "scan", "depth", "options", "forex",
    ):
        assert f'data-research-tool="{tool}"' in html
    assert html.count('data-research-later="true"') == 4


def test_read_only_page_keeps_research_without_propose(logged_in_research_client):
    html = logged_in_research_client.get("/cc").text
    assert 'id="dash-research"' in html
    assert 'data-research-propose-enabled="false"' in html


def test_research_reads_and_proposal_shell_follow_command_flag(
    monkeypatch,
    research_cc,
):
    import web.app as webapp

    monkeypatch.setattr(
        webapp,
        "_manage_page_context",
        lambda flash="": ({}, None),
    )
    payloads: dict[bool, list[dict[str, Any]]] = {}
    paths = [
        "/api/research/presets",
        "/api/research/ideas",
        "/api/research/movers",
        "/api/research/snapshot?symbol=AAPL",
        "/api/research/news?ticker=AAPL",
    ]
    for enabled in (False, True):
        monkeypatch.setattr(
            webapp,
            "_COMMAND_FLAGS",
            CommandFlags(enabled, False, None, None),
        )
        provider = FakeResearchProvider()
        service = RecordingResearchService(provider)
        client = None
        try:
            client = TestClient(webapp.create_app(research_cc, service))
            _login(client)
            page = client.get("/cc")
            assert page.status_code == 200
            assert (
                f'data-research-propose-enabled="{str(enabled).lower()}"'
                in page.text
            )
            assert ('id="cc-proposal-form"' in page.text) is enabled

            responses = [client.get(path) for path in paths]
            assert [response.status_code for response in responses] == [200] * 5
            payloads[enabled] = [response.json() for response in responses]
        finally:
            if client is not None:
                client.close()
            service.close()

    assert payloads[False] == payloads[True]


def test_snapshot_success_is_cli_shaped(logged_in_research_client):
    response = logged_in_research_client.get("/api/research/snapshot?symbol=aapl")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["ticker"] == "AAPL"
    assert body["meta"]["tool"] == "snapshot"


def test_query_validation_happens_before_worker_admission(
    logged_in_research_client,
    research_service,
):
    assert logged_in_research_client.get(
        "/api/research/ideas?num=0").status_code == 422
    assert logged_in_research_client.get(
        "/api/research/ideas?source=tickers").status_code == 422
    assert logged_in_research_client.get(
        "/api/research/ideas?location=STK.AU.ASX").status_code == 422
    assert research_service.admissions == 0


@pytest.mark.parametrize("path", [
    "/api/research/presets?extra=1",
    "/api/research/snapshot?symbol=AAPL&extra=1",
    "/api/research/news?ticker=AAPL&extra=1",
    "/api/research/movers?extra=1",
    "/api/research/ideas?extra=1",
])
def test_unknown_query_parameters_are_rejected(
    logged_in_research_client,
    path,
):
    response = logged_in_research_client.get(path)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(("path", "field"), [
    ("/api/research/snapshot", "symbol"),
    ("/api/research/snapshot?symbol=1AAPL", "symbol"),
    (f"/api/research/snapshot?symbol={'A' * 33}", "symbol"),
    ("/api/research/news", "ticker"),
    ("/api/research/news?ticker=", "ticker"),
    ("/api/research/news?ticker=1AAPL", "ticker"),
    (f"/api/research/news?ticker={'A' * 33}", "ticker"),
    ("/api/research/news?ticker=AAPL&limit=0", "limit"),
    ("/api/research/news?ticker=AAPL&limit=51", "limit"),
    ("/api/research/news?ticker=AAPL&source=other", "source"),
    ("/api/research/movers?market=forex", "market"),
    ("/api/research/movers?direction=sideways", "direction"),
    ("/api/research/movers?num=0", "num"),
    ("/api/research/movers?num=101", "num"),
    ("/api/research/movers?detail=perhaps", "detail"),
    ("/api/research/ideas?preset=", "preset"),
    (f"/api/research/ideas?preset={'x' * 41}", "preset"),
    ("/api/research/ideas?source=manual", "source"),
    ("/api/research/ideas?num=0", "num"),
    ("/api/research/ideas?num=51", "num"),
    ("/api/research/ideas?min_price=0", "min_price"),
    ("/api/research/ideas?max_price=0", "max_price"),
    ("/api/research/ideas?min_volume=-1", "min_volume"),
    ("/api/research/ideas?fundamentals=perhaps", "fundamentals"),
    ("/api/research/ideas?news=perhaps", "news"),
    ("/api/research/ideas?names=perhaps", "names"),
])
def test_query_bounds_and_enums_are_rejected_before_admission(
    logged_in_research_client,
    research_service,
    path,
    field,
):
    response = logged_in_research_client.get(path)
    assert response.status_code == 422
    assert any(error["loc"][-1] == field for error in response.json()["detail"])
    assert research_service.admissions == 0


def test_ideas_cross_field_validation_precedes_admission(
    logged_in_research_client,
    research_service,
):
    paths = [
        "/api/research/ideas?source=tickers",
        "/api/research/ideas?source=universe",
        "/api/research/ideas?min_price=20&max_price=10",
    ]
    for path in paths:
        assert logged_in_research_client.get(path).status_code == 422
    assert research_service.admissions == 0


def test_ideas_rejects_more_than_100_normalized_tickers_before_admission(
    logged_in_research_client,
    research_service,
):
    params = [("source", "tickers")]
    params.extend(("tickers", f"TICKER{index}") for index in range(101))
    response = logged_in_research_client.get("/api/research/ideas", params=params)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "tickers"]
    assert research_service.admissions == 0


def test_repeated_tickers_are_trimmed_uppercased_and_deduplicated(
    logged_in_research_client,
    research_provider,
):
    response = logged_in_research_client.get(
        "/api/research/ideas",
        params=[
            ("source", "tickers"),
            ("tickers", " aapl "),
            ("tickers", "AAPL"),
            ("tickers", " ms-ft "),
            ("tickers", ""),
        ],
    )
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert research_provider.calls[-1] == (
        "ideas",
        {
            "preset": "momentum",
            "source": "tickers",
            "tickers": ["AAPL", "MS-FT"],
            "universe_symbols": None,
            "top_n": 15,
            "custom_filters": None,
            "fundamentals": False,
            "news": False,
            "names": False,
        },
    )


def test_ideas_maps_filters_to_provider_names(
    logged_in_research_client,
    research_provider,
):
    response = logged_in_research_client.get(
        "/api/research/ideas",
        params={
            "min_price": "1.5",
            "max_price": "20",
            "min_volume": "0",
            "min_change": "-2.5",
            "max_change": "8.5",
            "fundamentals": "true",
            "news": "true",
            "names": "true",
        },
    )
    assert response.status_code == 200
    call = research_provider.calls[-1]
    assert call[0] == "ideas"
    assert call[1]["custom_filters"] == {
        "min_price": 1.5,
        "max_price": 20.0,
        "min_volume": 0,
        "min_change_pct": -2.5,
        "max_change_pct": 8.5,
    }
    assert call[1]["fundamentals"] is True
    assert call[1]["news"] is True
    assert call[1]["names"] is True


@pytest.mark.parametrize(("path", "tool"), [
    ("/api/research/news?ticker=AAPL", "news"),
    ("/api/research/movers", "movers"),
    ("/api/research/ideas", "ideas"),
])
def test_valid_empty_provider_data_keeps_cli_envelope(
    logged_in_research_client,
    path,
    tool,
):
    response = logged_in_research_client.get(path)
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["tool"] == tool


def test_presets_are_available_through_the_authenticated_route(
    logged_in_research_client,
):
    response = logged_in_research_client.get("/api/research/presets")
    assert response.status_code == 200
    assert response.json()["meta"]["tool"] == "presets"
    assert response.json()["meta"]["provider"] == "local"


def test_research_gets_do_not_require_a_csrf_token(logged_in_research_client):
    response = logged_in_research_client.get("/api/research/snapshot?symbol=AAPL")
    assert response.status_code == 200


@pytest.mark.parametrize(("error", "expected"), [
    (
        ResearchError(
            503,
            "MASSIVE_NOT_CONFIGURED",
            "Massive API key is not configured.",
            False,
        ),
        {
            "code": "MASSIVE_NOT_CONFIGURED",
            "message": "Massive API key is not configured.",
            "retryable": False,
        },
    ),
    (
        ResearchError(
            503,
            "RESEARCH_BUSY",
            "Research workers are busy; retry shortly.",
            True,
        ),
        {
            "code": "RESEARCH_BUSY",
            "message": "Research workers are busy; retry shortly.",
            "retryable": True,
        },
    ),
    (
        ResearchError(
            504,
            "RESEARCH_TIMEOUT",
            "Snapshot did not complete within 10 seconds.",
            True,
        ),
        {
            "code": "RESEARCH_TIMEOUT",
            "message": "Snapshot did not complete within 10 seconds.",
            "retryable": True,
        },
    ),
    (
        ResearchError(
            502,
            "RESEARCH_UPSTREAM_ERROR",
            "Snapshot provider request failed.",
            True,
        ),
        {
            "code": "RESEARCH_UPSTREAM_ERROR",
            "message": "Snapshot provider request failed.",
            "retryable": True,
        },
    ),
])
def test_research_errors_use_stable_http_envelopes(research_cc, error, expected):
    service = ErrorResearchService(error)
    try:
        client = TestClient(create_app(research_cc, service))
        _login(client)
        response = client.get("/api/research/snapshot?symbol=AAPL")
        assert response.status_code == error.status
        assert response.json() == {"error": expected}
    finally:
        service.close()


def test_universe_loading_runs_inside_the_dedicated_research_worker(
    research_cc,
    research_provider,
):
    loader_calls: list[tuple[str, str]] = []

    def load_universe(name: str) -> list[str]:
        loader_calls.append((name, threading.current_thread().name))
        return {"STK.US.LIQUID": ["AAPL", "MSFT"]}[name]

    service = RecordingResearchService(research_provider)
    try:
        client = TestClient(_router_app(research_cc, service, load_universe))
        _login(client)
        response = client.get(
            "/api/research/ideas?source=universe&universe=STK.US.LIQUID")
        assert response.status_code == 200
        assert loader_calls[0][0] == "STK.US.LIQUID"
        assert loader_calls[0][1].startswith("cc-research")
        assert research_provider.threads[-1][1] == loader_calls[0][1]
        assert research_provider.calls[-1][1]["universe_symbols"] == [
            "AAPL", "MSFT"]
    finally:
        service.close()


def test_empty_or_unknown_universe_is_rejected_before_provider_scan(
    research_cc,
    research_provider,
):
    loader_threads: list[str] = []

    def load_empty_universe(name: str) -> list[str]:
        loader_threads.append(threading.current_thread().name)
        return []

    service = RecordingResearchService(research_provider)
    try:
        client = TestClient(_router_app(
            research_cc,
            service,
            load_empty_universe,
        ))
        _login(client)
        response = client.get(
            "/api/research/ideas?source=universe&universe=UNKNOWN")
        assert response.status_code == 502
        assert response.json() == {"error": {
            "code": "RESEARCH_UPSTREAM_ERROR",
            "message": "Ideas provider request failed.",
            "retryable": True,
        }}
        assert loader_threads[0].startswith("cc-research")
        assert research_provider.calls == []
    finally:
        service.close()


def test_invalid_universe_request_never_calls_loader_or_service(
    research_cc,
    research_provider,
):
    loader_calls: list[str] = []
    service = RecordingResearchService(research_provider)
    try:
        client = TestClient(_router_app(
            research_cc,
            service,
            lambda name: loader_calls.append(name) or [],
        ))
        _login(client)
        response = client.get("/api/research/ideas?source=universe")
        assert response.status_code == 422
        assert loader_calls == []
        assert service.admissions == 0
    finally:
        service.close()


def test_create_app_stores_and_closes_injected_research_service(
    research_cc,
    research_provider,
):
    service = RecordingResearchService(research_provider)
    app = create_app(research_cc, service)
    assert app.state.research_service is service
    with TestClient(app):
        assert service.close_calls == 0
    assert service.close_calls == 1


class BlockingResearchProvider:
    def __init__(self, started: threading.Barrier, release: threading.Event):
        self.started = started
        self.release = release

    def snapshot(self, symbol: str) -> ResearchResult:
        self.started.wait(timeout=2)
        self.release.wait(timeout=3)
        return ResearchResult({"ticker": symbol}, f"Snapshot: {symbol}")


@pytest.fixture
def blocking_research_service():
    release = threading.Event()
    started = threading.Barrier(5)
    provider = BlockingResearchProvider(started, release)
    service = ResearchService(
        lambda: provider,
        workers=4,
        timeouts={"snapshot": 4.0},
    )
    try:
        yield service, release, started
    finally:
        release.set()
        service.close()


def _research_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _seed_research_baseline(cc: CommandCenter) -> None:
    cc.state.install_baseline(
        SnapshotWithCursor(
            source_cursor=0,
            broker_generation=1,
            entities={
                "account": [{
                    "entity_id": "DU123",
                    "entity_revision": 1,
                    "net_liquidation": 50_000.0,
                    "mode": "paper",
                }],
            },
        ),
        stream_id="stream-research-isolation",
    )


async def _login_async(client: httpx.AsyncClient) -> None:
    response = await client.post("/session", data={"token": TOKEN})
    assert response.status_code in (200, 303)


@pytest.fixture
def research_server(
    research_cc: CommandCenter,
    blocking_research_service,
):
    service, release, started = blocking_research_service
    _seed_research_baseline(research_cc)
    application = create_app(research_cc, research_service=service)

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    port = _research_free_port()
    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5.0)
        pytest.fail("test uvicorn server failed to start within 5s")

    try:
        yield f"http://127.0.0.1:{port}", release, started
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "test uvicorn server failed to stop within 5s"


@pytest.mark.asyncio
async def test_saturated_research_pool_does_not_block_trading_sse(
    research_server,
):
    base_url, release, all_started = research_server
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
        await _login_async(client)
        scans = [
            asyncio.create_task(client.get(
                "/api/research/snapshot",
                params={"symbol": f"T{i}"},
            ))
            for i in range(4)
        ]
        try:
            await asyncio.to_thread(all_started.wait, 2)
            started = time.monotonic()
            async with client.stream("GET", "/api/events") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line == "event: quotes.snapshot":
                        break
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            scan_results = await asyncio.gather(*scans, return_exceptions=True)
        assert all(
            isinstance(result, httpx.Response) and result.status_code == 200
            for result in scan_results
        )
