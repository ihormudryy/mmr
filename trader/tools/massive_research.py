"""Massive-backed data boundary for Command Center research views."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trader.tools.idea_scanner import (
    LIQUID_US_FALLBACK_TICKERS,
    IdeaScanner,
    TwelveDataIdeaScanner,
    entitlement_fallback_notice,
    is_data_entitlement_error,
    list_presets,
)

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
    """Normalize Massive REST-client responses for research consumers.

    Optional ``td_client`` enables the same Stocks-Basic entitlement fallback
    the CLI uses (TwelveData quotes / movers) when Massive rejects snapshots.
    """

    def __init__(self, client: Any, td_client: Any | None = None):
        self._client = client
        self._td_client = td_client

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
        try:
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
        except Exception as exc:
            if not is_data_entitlement_error(exc) or self._td_client is None:
                raise
            notice = entitlement_fallback_notice("massive", str(exc))
            fb_source = source
            fb_tickers = tickers
            fb_universe = universe_symbols
            if source == "movers" or (not tickers and not universe_symbols):
                fb_source = "tickers"
                fb_tickers = list(LIQUID_US_FALLBACK_TICKERS)
                fb_universe = None
            frame = TwelveDataIdeaScanner(self._td_client).scan(
                preset=preset,
                source=fb_source,
                tickers=fb_tickers,
                universe_symbols=fb_universe,
                top_n=top_n,
                custom_filters=custom_filters,
                fundamentals=fundamentals,
                news=False,
                names=names,
            )
            frame.attrs["ideas_notice"] = notice
            return ResearchResult(
                _records(frame),
                f"Ideas: {preset}",
                provider="twelvedata",
                notice=notice,
            )

    def movers(
        self,
        *,
        market: str,
        direction: str,
        limit: int,
        detail: bool = False,
    ) -> ResearchResult:
        title = f"{market.title()} Movers ({direction})"
        try:
            snapshots = list(self._client.get_snapshot_direction(
                market_type=market, direction=direction,
            ))[:limit]
        except Exception as exc:
            if not is_data_entitlement_error(exc) or self._td_client is None:
                raise
            return self._movers_from_twelvedata(
                market=market, direction=direction, limit=limit, detail=detail,
                notice=entitlement_fallback_notice("massive", str(exc)),
            )
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
        return ResearchResult(json_clean(rows), title)

    def snapshot(self, symbol: str) -> ResearchResult:
        ticker = symbol.strip().upper()
        try:
            snapshot = self._client.get_snapshot_ticker(
                market_type="stocks", ticker=ticker,
            )
        except Exception as exc:
            if not is_data_entitlement_error(exc) or self._td_client is None:
                raise
            return self._snapshot_from_twelvedata(
                ticker,
                notice=entitlement_fallback_notice("massive", str(exc)),
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

    def _snapshot_from_twelvedata(self, ticker: str, *, notice: str) -> ResearchResult:
        payload = self._td_client.quote(symbol=ticker).as_json()

        def _num(key: str) -> Any:
            value = payload.get(key)
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        close = _num("close")
        data = {
            "ticker": str(payload.get("symbol") or ticker).upper(),
            "change": _num("change"),
            "change_pct": _num("percent_change"),
            "day": {
                "open": _num("open"),
                "high": _num("high"),
                "low": _num("low"),
                "close": close,
                "volume": _num("volume"),
                "vwap": None,
            },
            "previous_day": {"close": _num("previous_close"), "volume": None},
            "bid": None,
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "last": close,
            "last_size": None,
        }
        return ResearchResult(
            json_clean(data), f"Snapshot: {ticker}",
            provider="twelvedata", notice=notice,
        )

    def _movers_from_twelvedata(
        self,
        *,
        market: str,
        direction: str,
        limit: int,
        detail: bool,
        notice: str,
    ) -> ResearchResult:
        title = f"{market.title()} Movers ({direction})"
        rows: list[dict[str, Any]] = []
        try:
            payload = self._td_client.get_market_movers(
                market=market, direction=direction,
            ).as_json()
            entries = payload if isinstance(payload, list) else payload.get("values", [])
            for entry in entries[:limit]:
                rows.append({
                    "ticker": entry.get("symbol", "") or "",
                    "open": None,
                    "close": entry.get("last"),
                    "volume": entry.get("volume"),
                    "change": entry.get("change"),
                    "change_pct": entry.get("percent_change"),
                    "market": market,
                })
        except Exception as td_exc:
            if not is_data_entitlement_error(td_exc):
                raise
            notice = entitlement_fallback_notice("twelvedata", str(td_exc))
            for quote in self._td_batch_quote(list(LIQUID_US_FALLBACK_TICKERS)):
                rows.append({
                    "ticker": quote.get("symbol", "") or "",
                    "open": quote.get("open"),
                    "close": quote.get("close") or quote.get("last"),
                    "volume": quote.get("volume"),
                    "change": quote.get("change"),
                    "change_pct": quote.get("percent_change"),
                    "market": market,
                })
            rows.sort(
                key=lambda row: float(row.get("change_pct") or 0.0),
                reverse=(direction != "losers"),
            )
            rows = rows[:limit]
        if detail:
            rows = self._enrich_movers(rows)
        return ResearchResult(
            json_clean(rows), title, provider="twelvedata", notice=notice,
        )

    def _td_batch_quote(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Fetch TwelveData quotes for a symbol list (same shape as TD /quote)."""
        joined = ",".join(symbol for symbol in symbols if symbol)
        if not joined:
            return []
        payload = self._td_client.quote(symbol=joined).as_json()
        if isinstance(payload, dict) and "symbol" in payload:
            return [payload]
        if isinstance(payload, dict):
            return [
                item for item in payload.values()
                if isinstance(item, dict) and ("symbol" in item or "close" in item)
            ]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

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
