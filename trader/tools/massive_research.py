"""Massive-backed data boundary for Command Center research views."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trader.tools.idea_scanner import IdeaScanner, list_presets

_MOVERS_DETAIL_WORKERS = 8


@dataclass(frozen=True)
class ResearchResult:
    data: list[dict[str, Any]] | dict[str, Any]
    title: str
    provider: str = "massive"
    notice: str | None = None


def json_clean(value: Any) -> Any:
    """Convert provider/Pandas values into JSON-safe built-in values."""
    if isinstance(value, dict):
        return {str(key): json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_clean(item) for item in value]
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item") and not isinstance(value, (bytes, str)):
        try:
            return json_clean(value.item())
        except (ValueError, TypeError, AttributeError):
            return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return json_clean(frame.to_dict("records"))


def _attrs(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(obj, name, None) for name in names} if obj else {}


def _quote_price(quote: Any, side: str) -> Any:
    if quote is None:
        return None
    value = getattr(quote, side, None)
    if value is not None:
        return value
    return getattr(quote, f"{side}_price", None)


class MassiveResearch:
    """Normalize Massive REST-client responses for research consumers."""

    def __init__(self, client: Any):
        self._client = client

    def presets(self) -> ResearchResult:
        return ResearchResult(_records(list_presets()), "Idea Scanner Presets", "local")

    def ideas(
        self,
        *,
        preset: str,
        source: str,
        tickers: list[str] | None,
        universe_symbols: list[str] | None,
        top_n: int,
        custom_filters: dict[str, Any] | None,
        fundamentals: bool,
        news: bool,
        names: bool,
    ) -> ResearchResult:
        frame = IdeaScanner(self._client).scan(
            preset=preset,
            source=source,
            tickers=tickers,
            universe_symbols=universe_symbols,
            top_n=top_n,
            custom_filters=custom_filters,
            fundamentals=fundamentals,
            news=news,
            names=names,
        )
        return ResearchResult(
            _records(frame),
            f"Ideas: {preset}",
            notice=frame.attrs.get("ideas_notice"),
        )

    def movers(
        self,
        *,
        market: str,
        direction: str,
        limit: int,
        detail: bool = False,
    ) -> ResearchResult:
        snapshots = list(self._client.get_snapshot_direction(
            market_type=market, direction=direction,
        ))[:limit]
        rows = []
        for snapshot in snapshots:
            day = getattr(snapshot, "day", None)
            rows.append({
                "ticker": getattr(snapshot, "ticker", "") or "",
                "open": getattr(day, "open", None),
                "close": getattr(day, "close", None),
                "volume": getattr(day, "volume", None),
                "change": getattr(snapshot, "todays_change", None),
                "change_pct": getattr(snapshot, "todays_change_percent", None),
                "market": market,
            })
        if detail:
            rows = self._enrich_movers(rows)
        return ResearchResult(json_clean(rows), f"{market.title()} Movers ({direction})")

    def snapshot(self, symbol: str) -> ResearchResult:
        ticker = symbol.strip().upper()
        snapshot = self._client.get_snapshot_ticker(
            market_type="stocks", ticker=ticker,
        )
        data = {
            "ticker": getattr(snapshot, "ticker", ticker) or ticker,
            "change": getattr(snapshot, "todays_change", None),
            "change_pct": getattr(snapshot, "todays_change_percent", None),
            "day": _attrs(
                getattr(snapshot, "day", None),
                ("open", "high", "low", "close", "volume", "vwap"),
            ),
            "previous_day": _attrs(
                getattr(snapshot, "prev_day", None), ("close", "volume"),
            ),
            "bid": _quote_price(getattr(snapshot, "last_quote", None), "bid"),
            "ask": _quote_price(getattr(snapshot, "last_quote", None), "ask"),
            "bid_size": getattr(getattr(snapshot, "last_quote", None), "bid_size", None),
            "ask_size": getattr(getattr(snapshot, "last_quote", None), "ask_size", None),
            "last": getattr(getattr(snapshot, "last_trade", None), "price", None),
            "last_size": getattr(getattr(snapshot, "last_trade", None), "size", None),
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
        if not rows:
            return rows
        workers = min(_MOVERS_DETAIL_WORKERS, len(rows))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="cc-movers-detail",
        ) as pool:
            list(pool.map(self._enrich_mover_row, rows))
        return rows

    def _enrich_mover_row(self, row: dict[str, Any]) -> dict[str, Any]:
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
                label: getattr(ratio, field, None)
                for field, label in (
                    ("price_to_earnings", "pe"),
                    ("debt_to_equity", "de"),
                    ("return_on_equity", "roe"),
                    ("earnings_per_share", "eps"),
                    ("dividend_yield", "div_yield"),
                )
                if ratio is not None
            }
        except Exception:
            row["ratios"] = {}
        try:
            articles = list(self._client.list_ticker_news(ticker=ticker, limit=1))
            article = articles[0] if articles else None
            row["news"] = self._news_row(article, "polygon") if article else {}
        except Exception:
            row["news"] = {}
        return row

    @staticmethod
    def _news_row(article: Any, source: str) -> dict[str, Any]:
        insights = getattr(article, "insights", None) or []
        sentiments = [getattr(item, "sentiment", "") for item in insights]
        return {
            "published": (
                getattr(article, "published", None)
                if source == "benzinga"
                else getattr(article, "published_utc", None)
            ) or "",
            "title": getattr(article, "title", "") or "",
            "tickers": list(getattr(article, "tickers", None) or []),
            "sentiment": ", ".join(value for value in sentiments if value),
            "author": getattr(article, "author", "") or "",
            "url": (
                getattr(article, "url", None)
                if source == "benzinga"
                else getattr(article, "article_url", None)
            ) or "",
            "teaser": getattr(article, "teaser", "") or "",
        }
