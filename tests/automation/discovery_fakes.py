"""Fakes for the discovery read (SP2 Plan 3 Tasks 9-10): an Alpaca HTTP session by route and IB details.

No network: the real ``AlpacaClient`` sends through ``FakeSession``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from tests.automation.ai_paper_fixtures import CONID, OTHER, daily_frame

SPY = 756733
MOVERS_PATH = "/v1beta1/screener/stocks/movers"
ACTIVES_PATH = "/v1beta1/screener/stocks/most-actives"
NEWS_PATH = "/v1beta1/news"

MOVERS = {"gainers": [{"symbol": "AAPL", "price": 101.0, "change": 2.0, "percent_change": 2.0},
                      {"symbol": "BRK.B", "price": 400.0, "change": 4.0, "percent_change": 1.0}],
          "losers": [{"symbol": "PINKY", "price": 7.0, "change": -1.0, "percent_change": -12.5}],
          "last_updated": "2026-07-17T14:59:00Z"}
ACTIVES = {"most_actives": [{"symbol": "SPY", "volume": 9e7, "trade_count": 1},
                            {"symbol": "AAPL", "volume": 5e7, "trade_count": 1}],
           "last_updated": "2026-07-17T14:58:00Z"}
ARTICLE = {"id": 7, "headline": "Apple ships", "summary": "s", "created_at": "2026-07-17T14:00:00Z",
           "url": "https://example.test/a", "source": "benzinga", "symbols": ["AAPL"]}


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.headers: dict = {}

    def json(self):
        return self._payload


class FakeSession:
    """Answers by path. A route is a payload or an HTTP status; ``news`` routes by the ``symbols`` param."""

    def __init__(self, routes: dict, news: Optional[dict] = None):
        self.routes = routes
        self.news = news or {}
        self.raw_news = False           # True: a news route is the whole reply body, not the article list
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("data.alpaca.markets", 1)[-1]
        params = dict(params or {})
        self.requests.append((path, params))
        if path == NEWS_PATH and "symbols" in params:
            raw = self.raw_news and params["symbols"] in self.news
            route = self.news.get(params["symbols"], [])
            route = route if isinstance(route, int) or raw else {"news": route}
        else:
            route = self.routes.get(path, 404)
        return FakeResponse(route, {"message": "fake"}) if isinstance(route, int) else FakeResponse(200, route)


class NoWaitLimiter:
    def acquire(self):
        pass


def details(conid=CONID, symbol="AAPL", primary="NASDAQ", stock_type="COMMON", sec_type="STK"):
    return SimpleNamespace(contract=SimpleNamespace(conId=conid, symbol=symbol, secType=sec_type, currency="USD",
                                                    primaryExchange=primary), stockType=stock_type)


IB_BY_SYMBOL = {
    "AAPL": [details()],
    "MSFT": [details(conid=OTHER, symbol="MSFT")],
    "SPY": [details(conid=SPY, symbol="SPY", primary="ARCA", stock_type="ETF")],
    "PINKY": [details(conid=4444, symbol="PINKY", primary="PINK")],
    "DUAL": [details(conid=5551, symbol="DUAL"), details(conid=5552, symbol="DUAL", primary="NYSE")],
}


class FakeIbDetails:
    """``request_details(contract)``: the rows for ``contract.symbol`` (or for its conid); counts calls."""

    def __init__(self, rows_by_symbol: Optional[dict] = None):
        self.rows_by_symbol = IB_BY_SYMBOL if rows_by_symbol is None else rows_by_symbol
        self.calls = 0

    def __call__(self, contract) -> list:
        self.calls += 1
        if getattr(contract, "symbol", ""):
            return list(self.rows_by_symbol.get(contract.symbol, []))
        return [row for rows in self.rows_by_symbol.values() for row in rows if row.contract.conId == contract.conId]


class FakeAlpacaHistory:
    """``get_history`` returns twenty current daily bars ($100M median) and counts calls."""

    def __init__(self, build=daily_frame):
        self.build = build
        self.calls = 0

    def get_history(self, ticker, bar_size, start, end):
        self.calls += 1
        return self.build()
