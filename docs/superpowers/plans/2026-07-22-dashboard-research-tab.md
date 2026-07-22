# Dashboard Research Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Phase 1 Command Center Research tab with Massive-backed Ideas, Movers, Snapshot, and News, plus safe entry into the existing proposal drawer.

**Architecture:** A reusable `MassiveResearch` provider normalizes vendor objects without constructing the IB-dependent `MMR` SDK. A Command Center `ResearchService` owns a dedicated four-thread executor, admission slots, timeout/error translation, and provider lifecycle; a thin router owns session authentication and query validation. The UI lives in a focused template partial, stylesheet, and JavaScript module, with one small proposal-prefill hook added to the existing command script.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pandas, Massive REST client, asyncio, `ThreadPoolExecutor`, Jinja2, browser JavaScript, Node's built-in test harness, pytest, pytest-asyncio, pytest-timeout.

## Global Constraints

- Phase 1 implements Ideas, Movers, Lookup Snapshot, and Lookup News only; Scan, Depth, Options, and Forex are inert entries marked “Later.”
- Massive calls run in the web process. Do not construct `MMR`, call `trader.mmr_cli`, spawn a subprocess, or use legacy RPC.
- All `/api/research/*` routes require the dashboard session and do not require command CSRF.
- Missing `massive_api_key` returns `503 MASSIVE_NOT_CONFIGURED`; upstream failure returns `502 RESEARCH_UPSTREAM_ERROR`; saturation returns `503 RESEARCH_BUSY`; timeout returns `504 RESEARCH_TIMEOUT`.
- Research uses exactly four dedicated workers. Snapshot times out after 10 seconds; Movers and News after 15 seconds; Ideas after 30 seconds.
- A timed-out worker retains its admission slot until its underlying future exits.
- Valid empty results return `200` with empty `data`; NaN and infinity serialize as JSON `null`.
- Failed refreshes preserve the last successful browser result.
- Research reads are independent of command flags. Propose is rendered only when `DASHBOARD_COMMANDS_ENABLED=true`; it never infers side, size, confidence, thesis, or reasoning.
- Proposal creation continues through the existing `/api/resolve` and `/api/commands/proposals` paths. `DASHBOARD_LIVE_COMMANDS_ENABLED` remains an approval-time concern.
- Preserve unrelated working-tree changes in `web/static/command_center.js`, `web/templates/command_center.html`, and `.DS_Store`. Stage only files named by each task.
- Run tests with the repository's configured 30-second timeout and `timeout_method = "thread"`; the final full-suite command repeats `--timeout-method=thread` explicitly.

---

## File map

- `trader/tools/massive_research.py` — provider-only Massive operations and JSON normalization.
- `web/command_center/research.py` — executor, admission, timeouts, stable errors, runtime provider construction.
- `web/command_center/routes_research.py` — authenticated GET routes and Pydantic query contracts.
- `web/templates/_research_tab.html` — Research shell and accessible controls.
- `web/static/command_center_research.css` — split-pane and responsive Research styling.
- `web/static/command_center_research.js` — per-tool state, requests, rendering, and selection.
- `web/static/research_test_harness.js` — minimal DOM/fetch VM used only by the Research Node tests.
- `web/static/command_center.js` — one reusable proposal-drawer prefill function.
- `web/templates/command_center.html` — sixth tab, partial include, and Research assets.
- `web/app.py` — injectable service, lifecycle composition, application state, and router installation.
- `tests/test_massive_research.py` — provider normalization and delegation tests.
- `tests/test_dashboard_research_service.py` — executor/admission/timeout/error tests.
- `tests/test_dashboard_research_api.py` — auth, validation, HTTP contract, page markers, and isolation test.
- `web/static/command_center_research.test.js` — dependency-free Research client tests.
- `tests/test_command_center_research_js.py` — Node test launcher.
- `web/static/command_center.test.js` — proposal reset/prefill regression test.

### Task 1: Reusable Massive research provider

**Files:**
- Create: `trader/tools/massive_research.py`
- Create: `tests/test_massive_research.py`

**Interfaces:**
- Consumes: `IdeaScanner`, `list_presets`, and a Massive REST-client-shaped object.
- Produces: `ResearchResult(data, title, provider="massive", notice=None)` and `MassiveResearch.presets()`, `.ideas(...)`, `.movers(...)`, `.snapshot(symbol)`, `.news(...)`.

- [ ] **Step 1: Write failing normalization and delegation tests**

```python
# tests/test_massive_research.py
from types import SimpleNamespace as NS

import pandas as pd

from trader.tools.massive_research import MassiveResearch, ResearchResult, json_clean


def test_json_clean_converts_non_finite_values_recursively():
    assert json_clean({"a": float("nan"), "b": [float("inf"), 3]}) == {
        "a": None, "b": [None, 3],
    }


def test_presets_are_cli_shaped_rows():
    result = MassiveResearch(object()).presets()
    assert isinstance(result, ResearchResult)
    assert result.title == "Idea Scanner Presets"
    assert {row["preset"] for row in result.data} >= {"momentum", "gap-up"}


def test_snapshot_normalizes_massive_stock_snapshot():
    client = NS(get_snapshot_ticker=lambda **kwargs: NS(
        ticker="AAPL", todays_change=2.5, todays_change_percent=1.25,
        day=NS(open=198.0, high=202.0, low=197.5, close=201.0,
               volume=1_000_000, vwap=200.0),
        prev_day=NS(close=198.5, volume=900_000),
        last_trade=NS(price=201.1, size=20, timestamp=1_753_188_000_000_000_000),
        last_quote=NS(bid=201.0, ask=201.2, bid_size=10, ask_size=12),
    ))
    result = MassiveResearch(client).snapshot("aapl")
    assert result.data["ticker"] == "AAPL"
    assert result.data["bid"] == 201.0
    assert result.data["day"]["volume"] == 1_000_000


def test_news_normalizes_polygon_and_benzinga_rows():
    article = NS(published_utc="2026-07-22T10:00:00Z", title="Earnings",
                 tickers=["AAPL"], insights=[NS(sentiment="positive")],
                 author="Reporter", article_url="https://example.test/a")
    client = NS(list_ticker_news=lambda **kwargs: [article])
    rows = MassiveResearch(client).news("AAPL", limit=10, source="polygon").data
    assert rows == [{
        "published": "2026-07-22T10:00:00Z", "title": "Earnings",
        "tickers": ["AAPL"], "sentiment": "positive", "author": "Reporter",
        "url": "https://example.test/a", "teaser": "",
    }]
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `.venv/bin/python -m pytest tests/test_massive_research.py -q`

Expected: collection fails with `ModuleNotFoundError: trader.tools.massive_research`.

- [ ] **Step 3: Implement the provider boundary**

```python
# trader/tools/massive_research.py
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from trader.tools.idea_scanner import IdeaScanner, list_presets


@dataclass(frozen=True)
class ResearchResult:
    data: list[dict[str, Any]] | dict[str, Any]
    title: str
    provider: str = "massive"
    notice: str | None = None


def json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_clean(value.item())
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return json_clean(frame.to_dict("records"))


def _attrs(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name, None) for name in names} if obj else {}


class MassiveResearch:
    def __init__(self, client: Any):
        self._client = client

    def presets(self) -> ResearchResult:
        return ResearchResult(_records(list_presets()), "Idea Scanner Presets", "local")

    def ideas(self, *, preset: str, source: str, tickers: list[str] | None,
              universe_symbols: list[str] | None, top_n: int,
              custom_filters: dict[str, Any] | None, fundamentals: bool,
              news: bool, names: bool) -> ResearchResult:
        frame = IdeaScanner(self._client).scan(
            preset=preset, source=source, tickers=tickers,
            universe_symbols=universe_symbols, top_n=top_n,
            custom_filters=custom_filters, fundamentals=fundamentals,
            news=news, names=names,
        )
        return ResearchResult(_records(frame), f"Ideas: {preset}", notice=frame.attrs.get("ideas_notice"))

    def movers(self, *, market: str, direction: str, limit: int,
               detail: bool = False) -> ResearchResult:
        snapshots = list(self._client.get_snapshot_direction(
            market_type=market, direction=direction))[:limit]
        rows = []
        for snap in snapshots:
            day = getattr(snap, "day", None)
            rows.append({
                "ticker": getattr(snap, "ticker", "") or "",
                "open": getattr(day, "open", None),
                "close": getattr(day, "close", None),
                "volume": getattr(day, "volume", None),
                "change": getattr(snap, "todays_change", None),
                "change_pct": getattr(snap, "todays_change_percent", None),
                "market": market,
            })
        if detail:
            rows = self._enrich_movers(rows)
        return ResearchResult(json_clean(rows), f"{market.title()} Movers ({direction})")

    def snapshot(self, symbol: str) -> ResearchResult:
        ticker = symbol.strip().upper()
        snap = self._client.get_snapshot_ticker(market_type="stocks", ticker=ticker)
        data = {
            "ticker": getattr(snap, "ticker", ticker) or ticker,
            "change": getattr(snap, "todays_change", None),
            "change_pct": getattr(snap, "todays_change_percent", None),
            "day": _attrs(getattr(snap, "day", None),
                          ("open", "high", "low", "close", "volume", "vwap")),
            "previous_day": _attrs(getattr(snap, "prev_day", None), ("close", "volume")),
            "bid": getattr(getattr(snap, "last_quote", None), "bid", None),
            "ask": getattr(getattr(snap, "last_quote", None), "ask", None),
            "bid_size": getattr(getattr(snap, "last_quote", None), "bid_size", None),
            "ask_size": getattr(getattr(snap, "last_quote", None), "ask_size", None),
            "last": getattr(getattr(snap, "last_trade", None), "price", None),
            "last_size": getattr(getattr(snap, "last_trade", None), "size", None),
        }
        return ResearchResult(json_clean(data), f"Snapshot: {ticker}")

    def news(self, ticker: str, *, limit: int, source: str) -> ResearchResult:
        symbol = ticker.strip().upper()
        if source == "benzinga":
            articles = self._client.list_benzinga_news(tickers=symbol, limit=limit)
        else:
            articles = self._client.list_ticker_news(ticker=symbol, limit=limit)
        rows = [self._news_row(article, source) for article in articles]
        return ResearchResult(json_clean(rows[:limit]), f"News: {symbol}")

    def _enrich_movers(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for row in rows:
            ticker = row["ticker"]
            try:
                details = self._client.get_ticker_details(ticker)
                row["details"] = _attrs(details, ("name", "market_cap", "description"))
            except Exception:
                row["details"] = {}
            try:
                ratios = list(self._client.list_financials_ratios(ticker=ticker, limit=1))
                ratio = ratios[0] if ratios else None
                row["ratios"] = {
                    label: getattr(ratio, field, None) for field, label in (
                        ("price_to_earnings", "pe"), ("debt_to_equity", "de"),
                        ("return_on_equity", "roe"), ("earnings_per_share", "eps"),
                        ("dividend_yield", "div_yield"),
                    ) if ratio is not None
                }
            except Exception:
                row["ratios"] = {}
            try:
                articles = list(self._client.list_ticker_news(ticker=ticker, limit=1))
                article = articles[0] if articles else None
                row["news"] = self._news_row(article, "polygon") if article else {}
            except Exception:
                row["news"] = {}
        return rows

    @staticmethod
    def _news_row(article: Any, source: str) -> dict[str, Any]:
        insights = getattr(article, "insights", None) or []
        sentiments = [getattr(item, "sentiment", "") for item in insights]
        sentiments = [value for value in sentiments if value]
        return {
            "published": (getattr(article, "published", None)
                          if source == "benzinga"
                          else getattr(article, "published_utc", None)) or "",
            "title": getattr(article, "title", "") or "",
            "tickers": list(getattr(article, "tickers", None) or []),
            "sentiment": ", ".join(sentiments),
            "author": getattr(article, "author", "") or "",
            "url": (getattr(article, "url", None)
                    if source == "benzinga"
                    else getattr(article, "article_url", None)) or "",
            "teaser": getattr(article, "teaser", "") or "",
        }
```

- [ ] **Step 4: Add provider edge-case tests and make them pass**

Append these concrete cases: `test_empty_provider_iterators_return_empty_data`,
`test_ideas_delegates_all_scan_arguments`, `test_movers_applies_limit_and_direction`,
`test_detail_subfetch_failure_keeps_base_row`, and
`test_benzinga_uses_published_url_and_teaser`. For the delegation assertion use:

```python
def test_ideas_delegates_all_scan_arguments(monkeypatch):
    captured = {}
    class Scanner:
        def __init__(self, client):
            captured["client"] = client
        def scan(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame([{"ticker": "AAPL", "score": 80.0}])
    monkeypatch.setattr("trader.tools.massive_research.IdeaScanner", Scanner)
    client = object()
    result = MassiveResearch(client).ideas(
        preset="momentum", source="tickers", tickers=["AAPL"],
        universe_symbols=None, top_n=5, custom_filters={"min_price": 10},
        fundamentals=True, news=True, names=True)
    assert result.data[0]["ticker"] == "AAPL"
    assert captured == {
        "client": client, "preset": "momentum", "source": "tickers",
        "tickers": ["AAPL"], "universe_symbols": None, "top_n": 5,
        "custom_filters": {"min_price": 10}, "fundamentals": True,
        "news": True, "names": True,
    }
```

Run:

`.venv/bin/python -m pytest tests/test_massive_research.py tests/test_idea_scanner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the provider unit**

```bash
git add trader/tools/massive_research.py tests/test_massive_research.py
git commit -m "feat(research): add Massive provider helpers"
```

### Task 2: Isolated Research service

**Files:**
- Create: `web/command_center/research.py`
- Create: `tests/test_dashboard_research_service.py`

**Interfaces:**
- Consumes: `MassiveResearch`, a zero-argument provider factory, and a clock.
- Produces: `ResearchService.run(tool, operation, log_params=None) -> dict`, `ResearchService.presets() -> dict`, `ResearchService.close()`, and `ResearchError(status, code, message, retryable)`.

- [ ] **Step 1: Write failing service tests**

```python
# tests/test_dashboard_research_service.py
import asyncio
import threading

import pytest

from trader.tools.massive_research import ResearchResult
from web.command_center.research import ResearchError, ResearchService


@pytest.mark.asyncio
async def test_success_envelope_contains_metadata():
    service = ResearchService(lambda: object(), workers=1,
                              clock=lambda: "2026-07-22T12:00:00Z")
    body = await service.run("news", lambda provider: ResearchResult(
        [{"title": "x"}], "News: AAPL"))
    assert body == {
        "data": [{"title": "x"}], "title": "News: AAPL",
        "meta": {"tool": "news", "provider": "massive",
                 "observed_at": "2026-07-22T12:00:00Z", "notice": None},
    }
    service.close()


@pytest.mark.asyncio
async def test_saturation_fails_fast_and_timeout_holds_slot_until_exit():
    started = threading.Event()
    release = threading.Event()
    service = ResearchService(lambda: object(), workers=1,
                              timeouts={"snapshot": 0.01})

    async def first():
        with pytest.raises(ResearchError) as caught:
            await service.run("snapshot", lambda provider: (
                started.set(), release.wait(), ResearchResult({}, "Snapshot"))[2])
        assert caught.value.code == "RESEARCH_TIMEOUT"

    task = asyncio.create_task(first())
    await asyncio.to_thread(started.wait, 1)
    await task
    with pytest.raises(ResearchError) as busy:
        await service.run("snapshot", lambda provider: ResearchResult({}, "Snapshot"))
    assert busy.value.code == "RESEARCH_BUSY"
    release.set()
    await asyncio.sleep(0.02)
    service.close()


@pytest.mark.asyncio
async def test_missing_provider_maps_to_configuration_error():
    service = ResearchService(lambda: None)
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: ResearchResult([], "News"))
    assert (caught.value.status, caught.value.code, caught.value.retryable) == (
        503, "MASSIVE_NOT_CONFIGURED", False)
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `.venv/bin/python -m pytest tests/test_dashboard_research_service.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement executor admission and stable errors**

```python
# web/command_center/research.py
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from trader.tools.massive_research import MassiveResearch, ResearchResult

logger = logging.getLogger("web.command_center.research")
DEFAULT_TIMEOUTS = {"snapshot": 10.0, "movers": 15.0, "news": 15.0, "ideas": 30.0}


@dataclass(frozen=True)
class ResearchError(Exception):
    status: int
    code: str
    message: str
    retryable: bool


class ResearchService:
    def __init__(self, provider_factory: Callable[[], MassiveResearch | None], *,
                 workers: int = 4, timeouts: dict[str, float] | None = None,
                 clock: Callable[[], str] | None = None):
        self._provider_factory = provider_factory
        self._provider: MassiveResearch | None = None
        self._provider_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="cc-research")
        self._slots = threading.BoundedSemaphore(workers)
        self._timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self._closed = False

    def _get_provider(self) -> MassiveResearch:
        with self._provider_lock:
            if self._provider is None:
                self._provider = self._provider_factory()
            if self._provider is None:
                raise ResearchError(503, "MASSIVE_NOT_CONFIGURED",
                                    "Massive API key is not configured.", False)
            return self._provider

    async def run(self, tool: str,
                  operation: Callable[[MassiveResearch], ResearchResult], *,
                  log_params: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        outcome = "rejected"
        if self._closed or not self._slots.acquire(blocking=False):
            logger.info("research tool=%s outcome=busy duration_ms=0 params=%r",
                        tool, log_params or {})
            raise ResearchError(503, "RESEARCH_BUSY",
                                "Research workers are busy; retry shortly.", True)
        future = self._executor.submit(lambda: operation(self._get_provider()))
        future.add_done_callback(lambda completed: self._slots.release())
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)), self._timeouts[tool])
            outcome = "ok"
        except asyncio.TimeoutError as exc:
            outcome = "timeout"
            raise ResearchError(504, "RESEARCH_TIMEOUT",
                                f"{tool.title()} did not complete within "
                                f"{self._timeouts[tool]:g} seconds.", True) from exc
        except ResearchError:
            outcome = "configuration_error"
            raise
        except Exception as exc:
            outcome = "upstream_error"
            logger.warning("research %s failed: %s", tool, type(exc).__name__)
            raise ResearchError(502, "RESEARCH_UPSTREAM_ERROR",
                                f"{tool.title()} provider request failed.", True) from exc
        finally:
            logger.info("research tool=%s outcome=%s duration_ms=%d params=%r",
                        tool, outcome, int((time.monotonic() - started) * 1000),
                        log_params or {})
        return {"data": result.data, "title": result.title, "meta": {
            "tool": tool, "provider": result.provider,
            "observed_at": self._clock(), "notice": result.notice,
        }}

    def presets(self) -> dict[str, Any]:
        result = MassiveResearch(object()).presets()
        return {"data": result.data, "title": result.title, "meta": {
            "tool": "presets", "provider": "local",
            "observed_at": self._clock(), "notice": None,
        }}

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
```

Add `build_research_service()` in the same module. It must lazily read
`Container.instance().config()["massive_api_key"]`, return `None` from the
provider factory when blank, and lazily import/create `massive.RESTClient` only
when a request needs it. Catch configuration-file errors as not configured so
`create_app()` and `/cc` still boot.

- [ ] **Step 4: Complete error, sanitization, and shutdown tests**

Use these exact assertions, with `timeouts` reduced to milliseconds so the test
never sleeps for production budgets:

```python
@pytest.mark.asyncio
async def test_provider_exception_is_sanitized():
    service = ResearchService(lambda: object(), workers=1)
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: (_ for _ in ()).throw(
            RuntimeError("secret vendor response")))
    assert caught.value.code == "RESEARCH_UPSTREAM_ERROR"
    assert "secret vendor response" not in caught.value.message
    service.close()


@pytest.mark.asyncio
async def test_log_contains_timing_and_sanitized_params(caplog):
    service = ResearchService(lambda: object(), workers=1)
    with caplog.at_level("INFO", logger="web.command_center.research"):
        await service.run("news", lambda provider: ResearchResult([], "News"),
                          log_params={"ticker": "AAPL", "limit": 10})
    message = caplog.records[-1].getMessage()
    assert "tool=news" in message and "outcome=ok" in message
    assert "ticker" in message and "AAPL" in message and "duration_ms=" in message
    assert "api_key" not in message
    service.close()


@pytest.mark.parametrize("tool,seconds", [
    ("snapshot", 10.0), ("movers", 15.0), ("news", 15.0), ("ideas", 30.0),
])
def test_default_timeout_budgets(tool, seconds):
    assert DEFAULT_TIMEOUTS[tool] == seconds


@pytest.mark.asyncio
async def test_close_rejects_new_work():
    service = ResearchService(lambda: object(), workers=1)
    service.close()
    with pytest.raises(ResearchError) as caught:
        await service.run("news", lambda provider: ResearchResult([], "News"))
    assert caught.value.code == "RESEARCH_BUSY"
```

Retain the saturation test from Step 1 as the exact-once slot-release coverage.
Run:

`.venv/bin/python -m pytest tests/test_dashboard_research_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the service unit**

```bash
git add web/command_center/research.py tests/test_dashboard_research_service.py
git commit -m "feat(research): isolate provider calls from dashboard work"
```

### Task 3: Authenticated Research API and application lifecycle

**Files:**
- Create: `web/command_center/routes_research.py`
- Create: `tests/test_dashboard_research_api.py`
- Modify: `web/app.py:51-64,1476-1595`

**Interfaces:**
- Consumes: `ResearchService`, `ResearchError`, `CommandCenter.require_session`.
- Produces: `create_research_router(cc, service) -> APIRouter` and `create_app(cc=None, research_service=None)`.

- [ ] **Step 1: Write failing authenticated route tests**

```python
# tests/test_dashboard_research_api.py
from fastapi.testclient import TestClient

from trader.tools.massive_research import ResearchResult
from web.command_center.research import ResearchService


def test_research_routes_require_session(app_with_research):
    client = TestClient(app_with_research)
    for path in ("presets", "ideas", "movers", "snapshot?symbol=AAPL", "news?ticker=AAPL"):
        assert client.get(f"/api/research/{path}").status_code == 401


def test_snapshot_success_is_cli_shaped(logged_in_research_client):
    response = logged_in_research_client.get("/api/research/snapshot?symbol=aapl")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["ticker"] == "AAPL"
    assert body["meta"]["tool"] == "snapshot"


def test_query_validation_happens_before_worker_admission(logged_in_research_client):
    assert logged_in_research_client.get("/api/research/ideas?num=0").status_code == 422
    assert logged_in_research_client.get(
        "/api/research/ideas?source=tickers").status_code == 422
    assert logged_in_research_client.get(
        "/api/research/ideas?location=STK.AU.ASX").status_code == 422
```

Build fixtures with the existing `CommandCenter`, `DashboardCredentials`, and
null bridge/quote fakes. Inject a `ResearchService` whose provider is a small
fake implementing all five provider methods; do not patch global config.

- [ ] **Step 2: Run route tests and verify 404 failures**

Run: `.venv/bin/python -m pytest tests/test_dashboard_research_api.py -q`

Expected: authenticated Research requests fail with `404 Not Found`.

- [ ] **Step 3: Add validated query models and routes**

```python
# web/command_center/routes_research.py
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from web.command_center.research import ResearchError, ResearchService


def create_research_router(cc, service: ResearchService) -> APIRouter:
    router = APIRouter(prefix="/api/research", tags=["research"])

    def require_session(request: Request) -> str:
        return cc.require_session(request)

    def reject_unknown(request: Request, allowed: set[str]) -> None:
        unknown = set(request.query_params.keys()) - allowed
        if unknown:
            key = sorted(unknown)[0]
            raise RequestValidationError([{"type": "extra_forbidden",
                "loc": ("query", key), "msg": "Extra inputs are not permitted",
                "input": request.query_params.get(key)}])

    def response(call):
        async def wrapped():
            try:
                return await call()
            except ResearchError as exc:
                return JSONResponse(status_code=exc.status, content={"error": {
                    "code": exc.code, "message": exc.message,
                    "retryable": exc.retryable,
                }})
        return wrapped

    @router.get("/presets")
    async def presets(request: Request, _session: str = Depends(require_session)):
        reject_unknown(request, set())
        return service.presets()

    @router.get("/snapshot")
    async def snapshot(request: Request,
                       symbol: str = Query(min_length=1, max_length=32,
                                           pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
                       _session: str = Depends(require_session)):
        reject_unknown(request, {"symbol"})
        return await response(lambda: service.run(
            "snapshot", lambda provider: provider.snapshot(symbol),
            log_params={"symbol": symbol.upper()}))()

    @router.get("/news")
    async def news(request: Request,
                   ticker: str = Query(min_length=1, max_length=32,
                                       pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
                   limit: int = Query(10, ge=1, le=50),
                   source: Literal["polygon", "benzinga"] = "polygon",
                   _session: str = Depends(require_session)):
        reject_unknown(request, {"ticker", "limit", "source"})
        return await response(lambda: service.run(
            "news", lambda provider: provider.news(ticker, limit=limit, source=source),
            log_params={"ticker": ticker.upper(), "limit": limit, "source": source}))()

    @router.get("/movers")
    async def movers(
        request: Request,
        market: Literal["stocks", "crypto", "indices", "options", "futures"] = "stocks",
        direction: Literal["gainers", "losers"] = "gainers",
        num: int = Query(20, ge=1, le=100), detail: bool = False,
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"market", "direction", "num", "detail"})
        return await response(lambda: service.run(
            "movers", lambda provider: provider.movers(
                market=market, direction=direction, limit=num, detail=detail),
            log_params={"market": market, "direction": direction,
                        "num": num, "detail": detail}))()

    @router.get("/ideas")
    async def ideas(
        request: Request,
        preset: str = Query("momentum", min_length=1, max_length=40),
        source: Literal["movers", "tickers", "universe"] = "movers",
        tickers: list[str] = Query(default=[]), universe: str = "",
        num: int = Query(15, ge=1, le=50),
        min_price: float | None = Query(None, gt=0),
        max_price: float | None = Query(None, gt=0),
        min_volume: int | None = Query(None, ge=0),
        min_change: float | None = None, max_change: float | None = None,
        fundamentals: bool = False, news: bool = False, names: bool = False,
        _session: str = Depends(require_session),
    ):
        allowed = {"preset", "source", "tickers", "universe", "num",
                   "min_price", "max_price", "min_volume", "min_change",
                   "max_change", "fundamentals", "news", "names"}
        reject_unknown(request, allowed)
        normalized = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
        if len(normalized) > 100:
            raise RequestValidationError([{"type": "too_long", "loc": ("query", "tickers"),
                "msg": "At most 100 tickers are allowed", "input": tickers}])
        if source == "tickers" and not normalized:
            raise RequestValidationError([{"type": "value_error", "loc": ("query", "tickers"),
                "msg": "tickers are required when source=tickers", "input": tickers}])
        if source == "universe" and not universe.strip():
            raise RequestValidationError([{"type": "value_error", "loc": ("query", "universe"),
                "msg": "universe is required when source=universe", "input": universe}])
        filters = {key: value for key, value in {
            "min_price": min_price, "max_price": max_price,
            "min_volume": min_volume, "min_change_pct": min_change,
            "max_change_pct": max_change,
        }.items() if value is not None}
        return await response(lambda: service.run("ideas", lambda provider: provider.ideas(
            preset=preset, source=source, tickers=normalized or None,
            universe_symbols=(load_universe_symbols(universe)
                              if source == "universe" else None), top_n=num,
            custom_filters=filters or None, fundamentals=fundamentals,
            news=news, names=names), log_params={
                "preset": preset, "source": source, "tickers": normalized,
                "universe": universe, "num": num, "filters": filters,
                "fundamentals": fundamentals, "news": news, "names": names,
            }))()
```

Import `RequestValidationError`. Define injected
`load_universe_symbols: Callable[[str], list[str]]` on the router factory;
the production default constructs `UniverseAccessor` from
`Container.instance().config()` and returns `security_definitions[*].symbol`.
Tests inject a pure dictionary-backed loader. Validate `max_price >= min_price`
with the same 422 shape before calling the loader or service.

- [ ] **Step 4: Wire the service into `create_app` and lifespan**

```python
# web/app.py additions
from web.command_center.research import ResearchService, build_research_service
from web.command_center.routes_research import create_research_router


def create_app(cc: CommandCenter | None = None,
               research_service: ResearchService | None = None) -> FastAPI:
    center = cc or CommandCenter(CommandCenterConfig.from_env(),
                                 commands_enabled=_COMMAND_FLAGS.commands_enabled)
    research = research_service or build_research_service()

    @contextlib.asynccontextmanager
    async def _app_lifespan(fastapi_app: FastAPI):
        try:
            async with center.lifespan(fastapi_app):
                async with _lifespan(fastapi_app):
                    yield
        finally:
            research.close()

    application = FastAPI(title="MMR Dashboard", lifespan=_app_lifespan)
    application.state.command_center = center
    application.state.research_service = research
    # existing middleware/session/read-router setup remains unchanged
    application.include_router(create_research_router(center, research))
```

Install the research router beside `create_read_router`, before legacy route
registration. Preserve the current command flags and lifecycle ordering.

- [ ] **Step 5: Complete API error-contract and lifecycle tests**

Add parametrized assertions for the four stable error envelopes, valid empty
data, every query bound/enum, repeated ticker normalization, session enforcement
on all routes, no CSRF requirement, and service closure at lifespan exit. Run:

`.venv/bin/python -m pytest tests/test_dashboard_research_api.py tests/test_dashboard_lifespan.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the API unit**

```bash
git add web/command_center/routes_research.py web/app.py tests/test_dashboard_research_api.py
git commit -m "feat(research): expose session-gated dashboard routes"
```

### Task 4: Research shell, semantics, and responsive styling

**Files:**
- Create: `web/templates/_research_tab.html`
- Create: `web/static/command_center_research.css`
- Modify: `web/templates/command_center.html`
- Modify: `tests/test_dashboard_research_api.py`

**Interfaces:**
- Consumes: `commands_enabled` template context and the existing `dash-tab`/`dash-pane` behavior.
- Produces: stable DOM IDs/data attributes consumed by Task 5.

- [ ] **Step 1: Add failing page-marker tests**

```python
def test_research_shell_is_the_sixth_tab(logged_in_research_client):
    html = logged_in_research_client.get("/cc").text
    assert 'data-dash-tab="research"' in html
    assert 'id="dash-research"' in html
    assert html.index('data-dash-tab="research"') > html.index('data-dash-tab="guide"')
    for tool in ("ideas", "movers", "lookup", "scan", "depth", "options", "forex"):
        assert f'data-research-tool="{tool}"' in html
    assert html.count('data-research-later="true"') == 4


def test_read_only_page_keeps_research_without_propose(logged_in_research_client):
    html = logged_in_research_client.get("/cc").text
    assert 'id="dash-research"' in html
    assert 'data-research-propose-enabled="false"' in html
```

- [ ] **Step 2: Run the marker tests and verify failure**

Run: `.venv/bin/python -m pytest tests/test_dashboard_research_api.py -q -k shell`

Expected: FAIL because the Research tab marker is absent.

- [ ] **Step 3: Add the partial and stylesheet**

```html
<!-- web/templates/_research_tab.html -->
<div class="research-layout" data-research-propose-enabled="{{ 'true' if commands_enabled else 'false' }}">
  <nav class="research-tools" aria-label="Research tools">
    <button type="button" class="research-tool active" data-research-tool="ideas">Ideas</button>
    <button type="button" class="research-tool" data-research-tool="movers">Movers</button>
    <button type="button" class="research-tool" data-research-tool="lookup">Lookup</button>
    {% for tool in ('scan', 'depth', 'options', 'forex') %}
    <button type="button" class="research-tool" data-research-tool="{{ tool }}"
            data-research-later="true" disabled>{{ tool|title }} <span>Later</span></button>
    {% endfor %}
  </nav>
  <section class="research-workspace" aria-live="polite">
    <div id="research-config-banner" class="research-banner" hidden></div>
    <div id="research-controls"></div>
    <p id="research-status" role="status"></p>
    <div id="research-results" tabindex="-1"></div>
  </section>
  <aside id="research-detail" class="research-detail" aria-label="Research detail">
    <p class="dim">Select a result to inspect it.</p>
  </aside>
</div>
```

In `command_center.html`, add the sixth nav button after Guide, include the
partial in `<div id="dash-research" class="dash-pane">`, link
`/static/command_center_research.css`, and load the Research script after
`command_center.js`. The CSS must use the existing custom properties, a
three-column `220px minmax(0, 1fr) minmax(280px, .75fr)` desktop grid, a sticky
detail pane below the global command band, visible `:focus-visible`, and a
single-column layout at `max-width: 960px`. Do not modify the active visual
redesign beyond these Research selectors.

- [ ] **Step 4: Run marker and existing page tests**

Run: `.venv/bin/python -m pytest tests/test_dashboard_research_api.py tests/test_web_dashboard.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the shell unit**

```bash
git add web/templates/_research_tab.html web/static/command_center_research.css web/templates/command_center.html tests/test_dashboard_research_api.py
git commit -m "feat(research): add command center research shell"
```

### Task 5: Research browser state, requests, and rendering

**Files:**
- Create: `web/static/command_center_research.js`
- Create: `web/static/command_center_research.test.js`
- Create: `tests/test_command_center_research_js.py`

**Interfaces:**
- Consumes: Task 4 DOM markers and the five `/api/research/*` response contracts.
- Produces: `globalThis.CCResearch` with `state`, `selectTool`, `run`, `selectRow`, and pure render helpers.

- [ ] **Step 1: Write failing dependency-free JavaScript tests**

```javascript
// web/static/command_center_research.test.js
'use strict';
const assert = require('node:assert/strict');
const { makeHarness } = require('./research_test_harness.js');

(async () => {
  const h = makeHarness();
  h.loadProductionScript();

  h.fetch.enqueue(200, {data: [{ticker: 'AAPL'}, {ticker: 'MSFT'}],
    title: 'Ideas: momentum', meta: {tool: 'ideas', provider: 'massive'}});
  await h.api.run('ideas', new URLSearchParams({preset: 'momentum'}));
  assert.equal(h.api.state.ideas.data.length, 2);
  assert.equal(h.elements.get('research-results').innerHTML.includes('AAPL'), true);

  h.fetch.enqueue(502, {error: {code: 'RESEARCH_UPSTREAM_ERROR',
    message: 'Ideas provider request failed.', retryable: true}});
  await h.api.run('ideas', new URLSearchParams({preset: 'momentum'}));
  assert.equal(h.api.state.ideas.data.length, 2, 'failed refresh preserves data');
  assert.match(h.elements.get('research-status').textContent, /provider request failed/i);

  h.fetch.enqueue(503, {error: {code: 'MASSIVE_NOT_CONFIGURED',
    message: 'Massive API key is not configured.', retryable: false}});
  await h.api.run('movers', new URLSearchParams());
  assert.equal(h.elements.get('research-config-banner').hidden, false);

  console.log('command_center_research.test.js: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
```

Create `research_test_harness.js` beside the test with the same dependency-free
VM/DOM technique as `command_center.test.js`; it supplies queued fetch
responses, elements, `URLSearchParams`, and `document.querySelectorAll`.

- [ ] **Step 2: Add the pytest launcher and verify failure**

```python
# tests/test_command_center_research_js.py
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SCRIPT = Path(__file__).parents[1] / "web/static/command_center_research.test.js"


@pytest.mark.skipif(NODE is None, reason="node runtime not available")
def test_command_center_research_client():
    result = subprocess.run([NODE, str(SCRIPT)], cwd=SCRIPT.parent,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
```

Run: `.venv/bin/python -m pytest tests/test_command_center_research_js.py -q`

Expected: FAIL because the production script/harness does not exist.

- [ ] **Step 3: Implement the focused client module**

```javascript
// web/static/command_center_research.js
'use strict';
(() => {
  const slot = () => ({data: null, meta: null, selected: null,
                       loading: false, error: null});
  const state = {presets: slot(), ideas: slot(), movers: slot(),
                 lookup: {snapshot: slot(), news: slot()}};
  let activeTool = 'ideas';

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);

  function errorMessage(body, status) {
    return body?.error?.message || `Research request failed (HTTP ${status})`;
  }

  async function request(target, tool, path, params) {
    target.loading = true;
    target.error = null;
    render();
    try {
      const response = await fetch(`/api/research/${path}?${params}`, {
        credentials: 'same-origin', headers: {Accept: 'application/json'},
      });
      const body = await response.json();
      if (!response.ok) {
        target.error = {message: errorMessage(body, response.status),
                        code: body?.error?.code || 'HTTP_ERROR'};
        return null;
      }
      target.data = body.data;
      target.meta = body.meta;
      target.selected = Array.isArray(body.data) ? body.data[0] || null : body.data;
      return body;
    } catch (error) {
      target.error = {message: String(error.message || error), code: 'NETWORK_ERROR'};
      return null;
    } finally {
      target.loading = false;
      render();
    }
  }

  async function run(tool, params) {
    if (tool === 'lookup') {
      const symbol = params.get('symbol');
      const [snapshot, news] = await Promise.all([
        request(state.lookup.snapshot, 'snapshot', 'snapshot', new URLSearchParams({symbol})),
        request(state.lookup.news, 'news', 'news', new URLSearchParams({ticker: symbol,
          limit: params.get('limit') || '10', source: params.get('source') || 'polygon'})),
      ]);
      return {snapshot, news};
    }
    return request(state[tool], tool, tool, params);
  }

  async function loadPresets() {
    return request(state.presets, 'presets', 'presets', new URLSearchParams());
  }

  function selectTool(tool) { activeTool = tool; renderControls(); render(); }
  function selectRow(index) {
    if (activeTool === 'lookup') return;
    state[activeTool].selected = state[activeTool].data[index];
    renderDetail();
  }

  function renderControls() {
    const root = document.getElementById('research-controls');
    const forms = {
      ideas: `<form data-research-form="ideas"><label>Preset <select name="preset" id="research-preset">${(state.presets.data || []).map(row => `<option value="${esc(row.preset)}">${esc(row.preset)}</option>`).join('')}</select></label><label>Source <select name="source"><option>movers</option><option>tickers</option><option>universe</option></select></label><label>Tickers <input name="tickers" placeholder="AAPL MSFT"></label><label>Universe <input name="universe"></label><label>Results <input name="num" type="number" min="1" max="50" value="15"></label><button type="submit">Run Ideas</button></form>`,
      movers: `<form data-research-form="movers"><label>Market <select name="market"><option>stocks</option><option>crypto</option><option>indices</option><option>options</option><option>futures</option></select></label><label>Direction <select name="direction"><option>gainers</option><option>losers</option></select></label><label>Results <input name="num" type="number" min="1" max="100" value="20"></label><label><input name="detail" type="checkbox"> Detail</label><button type="submit">Run Movers</button></form>`,
      lookup: `<form data-research-form="lookup"><label>Ticker <input name="symbol" required maxlength="32"></label><label>News source <select name="source"><option>polygon</option><option>benzinga</option></select></label><label>News limit <input name="limit" type="number" min="1" max="50" value="10"></label><button type="submit">Lookup</button></form>`,
    };
    root.innerHTML = forms[activeTool];
  }

  function statusFor(target) {
    if (target.loading) return 'Loading…';
    if (target.error) return target.error.message;
    if (Array.isArray(target.data) && target.data.length === 0) return 'No results.';
    return '';
  }

  function renderResults() {
    const root = document.getElementById('research-results');
    const status = document.getElementById('research-status');
    if (activeTool === 'lookup') {
      const snap = state.lookup.snapshot;
      const news = state.lookup.news;
      status.textContent = [statusFor(snap), statusFor(news)].filter(Boolean).join(' ');
      root.innerHTML = `<section data-lookup-part="snapshot">${snap.data ? esc(JSON.stringify(snap.data)) : ''}</section><section data-lookup-part="news">${Array.isArray(news.data) ? news.data.map(row => `<article>${esc(row.title)}</article>`).join('') : ''}</section>`;
      return;
    }
    const target = state[activeTool];
    status.textContent = statusFor(target);
    const rows = Array.isArray(target.data) ? target.data : [];
    root.innerHTML = rows.map((row, index) => `<button type="button" class="research-row" data-research-row="${index}"><strong>${esc(row.ticker || row.symbol)}</strong><span>${esc(row.change_pct ?? row.score ?? '')}</span></button>`).join('');
  }

  function renderDetail() {
    const root = document.getElementById('research-detail');
    const target = activeTool === 'lookup' ? state.lookup.snapshot : state[activeTool];
    if (!target.selected) {
      root.innerHTML = '<p class="dim">Select a result to inspect it.</p>';
      return;
    }
    const fields = Object.entries(target.selected).map(([key, value]) =>
      `<dt>${esc(key)}</dt><dd>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</dd>`).join('');
    root.innerHTML = `<dl>${fields}</dl><p class="dim">${esc(target.meta?.provider || '')} ${esc(target.meta?.observed_at || '')}</p>`;
  }
  function render() { renderResults(); renderDetail(); }

  globalThis.CCResearch = {state, run, selectTool, selectRow,
                           renderControls, renderResults, renderDetail};
  document.addEventListener('DOMContentLoaded', () => {
    loadPresets().finally(() => { renderControls(); render(); });
  });
})();
```

Complete initialization and event delegation with:

```javascript
  function paramsFromForm(form) {
    const params = new URLSearchParams();
    for (const [name, value] of new FormData(form).entries()) {
      if (name === 'tickers') {
        String(value).split(/[\s,]+/).filter(Boolean)
          .forEach(ticker => params.append('tickers', ticker.toUpperCase()));
      } else if (value !== '') {
        params.append(name, value);
      }
    }
    for (const checkbox of form.querySelectorAll('input[type="checkbox"]')) {
      params.set(checkbox.name, checkbox.checked ? 'true' : 'false');
    }
    return params;
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-research-tool]:not([disabled])').forEach(button =>
      button.addEventListener('click', () => selectTool(button.dataset.researchTool)));
    document.getElementById('research-controls').addEventListener('submit', event => {
      event.preventDefault();
      run(event.target.dataset.researchForm, paramsFromForm(event.target));
    });
    document.getElementById('research-results').addEventListener('click', event => {
      const row = event.target.closest('[data-research-row]');
      if (row) selectRow(Number(row.dataset.researchRow));
    });
    renderControls();
    render();
  });
```

Expand the Ideas form literal with inputs named `min_price`, `max_price`,
`min_volume`, `min_change`, and `max_change`, plus checkboxes named
`fundamentals`, `news`, and `names`. In `request()`, when
`target.error.code === 'MASSIVE_NOT_CONFIGURED'`, set the configuration banner's
text from the escaped error message and unhide it. Every provider value passed
to `innerHTML` must go through `esc`.

- [ ] **Step 4: Complete state and rendering tests**

Append named tests for `tool state retention`, `initialization fetches presets only`,
`valid empty response`, `network error`, `HTML escaping`, `row click and Enter`,
`Lookup snapshot succeeds while News fails`, `provider/time labels`, and
`disabled Later tool makes no request`. Each test asserts the corresponding
state slot plus its rendered DOM marker; Lookup assertions use
`state.lookup.snapshot` and `state.lookup.news` independently. Run:

`.venv/bin/python -m pytest tests/test_command_center_research_js.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the client unit**

```bash
git add web/static/command_center_research.js web/static/command_center_research.test.js web/static/research_test_harness.js tests/test_command_center_research_js.py
git commit -m "feat(research): render scanner workflows in the browser"
```

### Task 6: Safe proposal-drawer entry

**Files:**
- Modify: `web/static/command_center.js:1150-1225`
- Modify: `web/static/command_center.test.js`
- Modify: `web/static/command_center_research.js`
- Modify: `web/static/command_center_research.test.js`
- Modify: `tests/test_dashboard_research_api.py`

**Interfaces:**
- Consumes: existing `ccOpenProposalDrawer()`, `ccResolveSymbol()`, and command-gated drawer DOM.
- Produces: `globalThis.ccOpenResearchProposal({ticker, exchange, currency})`.

- [ ] **Step 1: Write failing proposal reset/prefill tests**

```javascript
// append in web/static/command_center.test.js
await test('research proposal resets stale intent and resolves only the instrument', async () => {
  const {context, elements, run} = makeContext();
  const form = elements.get('cc-proposal-form');
  form.reset = () => {
    form.action.value = 'BUY'; form.quantity.value = ''; form.amount.value = '';
    form.confidence.value = ''; form.thesis.value = ''; form.reasoning.value = '';
  };
  for (const name of ['resolve_symbol', 'resolve_exchange', 'resolve_currency',
                       'conid', 'action', 'quantity', 'amount', 'confidence',
                       'thesis', 'reasoning']) form[name] = element();
  form.thesis.value = 'stale thesis';
  run('globalThis.resolveCalls = 0; ccResolveSymbol = async () => { resolveCalls += 1; };');
  await run("ccOpenResearchProposal({ticker:'AAPL', exchange:'NASDAQ', currency:'USD'})");
  assert.equal(form.resolve_symbol.value, 'AAPL');
  assert.equal(form.resolve_exchange.value, 'NASDAQ');
  assert.equal(form.thesis.value, '');
  assert.equal(run('resolveCalls'), 1);
});
```

Add Research-client assertions that Propose is absent when the partial's
dataset is false, present only for equity/Ideas/Lookup data when true, and calls
`ccOpenResearchProposal` without posting a command.

- [ ] **Step 2: Run both Node suites and verify failure**

Run: `.venv/bin/python -m pytest tests/test_command_center_js.py tests/test_command_center_research_js.py -q`

Expected: FAIL because `ccOpenResearchProposal` is undefined.

- [ ] **Step 3: Implement the proposal prefill hook**

```javascript
// web/static/command_center.js beside ccOpenProposalDrawer
async function ccOpenResearchProposal(instrument) {
  const form = document.getElementById('cc-proposal-form');
  if (!form) return;
  form.reset();
  form.resolve_symbol.value = String(instrument.ticker || instrument.symbol || '')
    .trim().toUpperCase();
  form.resolve_exchange.value = String(instrument.exchange || '').trim();
  form.resolve_currency.value = String(instrument.currency || '').trim();
  form.conid.value = '';
  ccOpenProposalDrawer();
  await ccResolveSymbol();
}
globalThis.ccOpenResearchProposal = ccOpenResearchProposal;
```

In the Research module, read `data-research-propose-enabled`; render Propose
only for Ideas, stock Movers, and Lookup. Delegate clicks to the global hook.
Do not copy proposal submission code or prefill any intent field.

- [ ] **Step 4: Add server-rendered feature-flag assertions**

Use monkeypatching of `web.app._COMMAND_FLAGS` following existing command-page
tests. Assert Research API routes return the same data with commands on or off,
the Research dataset reflects the flag, and the drawer remains under the
existing command-only template gate.

Run: `.venv/bin/python -m pytest tests/test_command_center_js.py tests/test_command_center_research_js.py tests/test_dashboard_research_api.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the proposal bridge**

```bash
git add web/static/command_center.js web/static/command_center.test.js web/static/command_center_research.js web/static/command_center_research.test.js tests/test_dashboard_research_api.py
git commit -m "feat(research): open proposals from resolved equity results"
```

### Task 7: Isolation release gate and full verification

**Files:**
- Modify: `tests/test_dashboard_research_api.py`

**Interfaces:**
- Consumes: complete Phase 1 feature.
- Produces: a regression gate proving saturated Research work does not block Command Center reads/SSE.

- [ ] **Step 1: Write the saturated-pool isolation test**

Create a real-uvicorn async fixture using the `_free_port`, null bridge/quote,
credentials, login, and baseline-seeding helpers already defined in
`tests/test_dashboard_snapshot_api.py`; keep the fixture local to this test
module because those helpers are not public imports. Its provider is exactly:

```python
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
    service = ResearchService(lambda: provider, workers=4,
                              timeouts={"snapshot": 4.0})
    yield service, release, started
    release.set()
    service.close()
```

Build `research_server` with `create_app(cc, research_service=service)`, start
uvicorn on the loopback port, seed one account baseline, and yield its base URL
with `release` and `started`. Shut the server down and join its thread within
five seconds in the fixture finalizer.

```python
@pytest.mark.asyncio
async def test_saturated_research_pool_does_not_block_trading_sse(research_server):
    base_url, release, all_started = research_server
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
        await login(client)
        scans = [asyncio.create_task(client.get(
            "/api/research/snapshot", params={"symbol": f"T{i}"}))
                 for i in range(4)]
        await asyncio.to_thread(all_started.wait, 2)
        started = time.monotonic()
        async with client.stream("GET", "/api/events") as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line == "event: quotes.snapshot":
                    break
        assert time.monotonic() - started < 1.0
        release.set()
        await asyncio.gather(*scans)
```

- [ ] **Step 2: Run the isolation test and fix only genuine integration defects**

Run: `.venv/bin/python -m pytest tests/test_dashboard_research_api.py -q -k saturated`

Expected: PASS in under five seconds. If it fails, fix executor ownership,
lifespan wiring, or the test fixture; do not relax the one-second SSE assertion.

- [ ] **Step 3: Run focused Phase 1 verification**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_massive_research.py \
  tests/test_dashboard_research_service.py \
  tests/test_dashboard_research_api.py \
  tests/test_command_center_research_js.py \
  tests/test_command_center_js.py -q --timeout-method=thread
```

Expected: all pass, with only pre-existing environment-dependent skips.

- [ ] **Step 4: Run adjacent Command Center regression tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_dashboard_session.py \
  tests/test_dashboard_snapshot_api.py \
  tests/test_dashboard_sse.py \
  tests/test_dashboard_lifespan.py \
  tests/test_web_dashboard.py -q --timeout-method=thread
```

Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout-method=thread`

Expected: all tests pass; browser-extra tests may skip when Playwright is not installed.

- [ ] **Step 6: Check the final diff and commit the release gate**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Verify `.DS_Store` and unrelated pre-existing Command Center edits are not
staged. Then:

```bash
git add tests/test_dashboard_research_api.py
git commit -m "test(research): prove scanner isolation from trading SSE"
```

## Completion criteria

- `/cc#research` renders the approved split-pane shell and inert later entries.
- Every Phase 1 route is session-gated and returns the approved success/error envelopes.
- Missing Massive configuration degrades only Research, not `/cc` or ops probes.
- Slow or abandoned Massive work cannot consume the shared executor or block Trading SSE.
- Ideas, Movers, Snapshot, and News preserve existing field semantics and explicit operator execution.
- Propose resets the existing drawer, resolves exact conId, and never bypasses proposal controls.
- Focused, adjacent, and full test suites pass with thread-based timeout handling.
