"""Massive-backed data boundary for Command Center research views."""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from trader.data_providers.massive.scan import MassiveScanSource
from trader.data_providers.twelvedata.scan import TwelveDataScanSource
from trader.tools.idea_scanner import (
    LIQUID_US_FALLBACK_TICKERS,
    IdeaScanner,
    entitlement_fallback_notice,
    is_data_entitlement_error,
    list_presets,
)

_MOVERS_DETAIL_WORKERS = 8
logger = logging.getLogger(__name__)


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


def _td_float(payload: dict, key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MassiveResearch:
    """Normalize Massive REST-client responses for research consumers.

    Optional ``td_client`` enables the same Stocks-Basic entitlement fallback
    the CLI uses (TwelveData quotes / movers) when Massive rejects snapshots.
    """

    def __init__(self, client: Any, td_client: Any | None = None, api_key: str = ""):
        self._client = client
        self._td_client = td_client
        self._api_key = api_key

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
            frame = IdeaScanner(MassiveScanSource(self._client)).scan(
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
                # Dashboard UI: never surface CLI entitlement notices (vendor
                # plan text / --tickers hints). Provider badge is enough.
                notice=None,
            )
        except Exception as exc:
            if not is_data_entitlement_error(exc) or self._td_client is None:
                raise
            logger.warning("%s", entitlement_fallback_notice("massive", str(exc)))
            fb_source = source
            fb_tickers = tickers
            fb_universe = universe_symbols
            if source == "movers" or (not tickers and not universe_symbols):
                fb_source = "tickers"
                fb_tickers = list(LIQUID_US_FALLBACK_TICKERS)
                fb_universe = None
            frame = IdeaScanner(TwelveDataScanSource(self._td_client)).scan(
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
            return ResearchResult(
                _records(frame),
                f"Ideas: {preset}",
                provider="twelvedata",
                notice=None,
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
            logger.warning("%s", entitlement_fallback_notice("massive", str(exc)))
            return self._movers_from_twelvedata(
                market=market, direction=direction, limit=limit, detail=detail,
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
            logger.warning("%s", entitlement_fallback_notice("massive", str(exc)))
            return self._snapshot_from_twelvedata(ticker)
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

    # ---- options ---------------------------------------------------------

    def options_expirations(self, symbol: str) -> ResearchResult:
        from trader.tools.chain import get_option_dates
        dates = get_option_dates(symbol, api_key=self._api_key)
        today = date.today()
        rows = [{
            "expiration": expiration,
            "dte": (date.fromisoformat(expiration) - today).days,
        } for expiration in dates]
        return ResearchResult(json_clean(rows), f"Options expirations: {symbol.upper()}")

    def options_chain(
        self,
        symbol: str,
        *,
        expiration: str | None,
        contract_type: str | None,
        strike_min: float | None,
        strike_max: float | None,
    ) -> ResearchResult:
        from trader.tools.chain import get_option_dates
        from trader.tools.options_data import chain_records
        exp = expiration
        if not exp:
            dates = get_option_dates(symbol, api_key=self._api_key)
            if not dates:
                return ResearchResult([], f"Options chain: {symbol.upper()}")
            exp = dates[0]
        rows = chain_records(
            self._client, symbol, expiration=exp, contract_type=contract_type,
            strike_min=strike_min, strike_max=strike_max,
        )
        return ResearchResult(json_clean(rows), f"Options chain: {symbol.upper()} {exp}")

    def options_snapshot(self, option_ticker: str) -> ResearchResult:
        from trader.tools.options_data import contract_snapshot
        data = contract_snapshot(self._client, option_ticker)
        return ResearchResult(json_clean(data), f"Option: {option_ticker}")

    def options_implied(
        self, symbol: str, *, expiration: str, risk_free_rate: float = 0.05,
    ) -> ResearchResult:
        from trader.tools.chain import implied_constant
        data = implied_constant(symbol, expiration, risk_free_rate, api_key=self._api_key)
        return ResearchResult(
            json_clean(data), f"Implied distribution: {symbol.upper()} {expiration}",
        )

    # ---- forex -------------------------------------------------------------

    def forex_snapshot(self, pair: str, *, source: str) -> ResearchResult:
        if source == "ib":
            raise ValueError("ib source is handled on the typed path, not the provider")
        ticker = pair.upper()
        if not ticker.startswith("C:"):
            ticker = f"C:{ticker}"
        if source == "twelvedata":
            raw = pair.upper().replace("C:", "").replace("/", "")
            td_symbol = f"{raw[:3]}/{raw[3:6]}"
            payload = self._td_client.quote(symbol=td_symbol).as_json()
            data = {
                "ticker": ticker,
                "last": _td_float(payload, "close"),
                "open": _td_float(payload, "open"),
                "high": _td_float(payload, "high"),
                "low": _td_float(payload, "low"),
                "close": _td_float(payload, "close"),
                "change": _td_float(payload, "change"),
                "change_pct": _td_float(payload, "percent_change"),
            }
            return ResearchResult(
                json_clean(data), f"Forex snapshot: {ticker}",
                provider="twelvedata", notice="TwelveData REST has no bid/ask.",
            )
        elif source == "massive":
            snap = self._client.get_snapshot_ticker(market_type="forex", ticker=ticker)
            data: dict[str, Any] = {"ticker": ticker}
            if snap.day:
                for field in ("open", "high", "low", "close", "volume", "vwap"):
                    data[field] = getattr(snap.day, field, None)
            if snap.last_quote:
                # Mirrors sdk.py's forex_snapshot massive branch: some Massive
                # forex quote payloads use a bare "P" price field instead of
                # separate bid/ask.
                data["bid"] = (
                    getattr(snap.last_quote, "bid", None)
                    or getattr(snap.last_quote, "P", None)
                )
                data["ask"] = (
                    getattr(snap.last_quote, "ask", None)
                    or getattr(snap.last_quote, "P", None)
                )
            data["change"] = getattr(snap, "todays_change", None)
            data["change_pct"] = getattr(snap, "todays_change_percent", None)
            return ResearchResult(json_clean(data), f"Forex snapshot: {ticker}")
        else:
            raise ValueError(f"unknown forex source: {source!r}")

    def forex_quote(self, from_ccy: str, to_ccy: str, *, source: str) -> ResearchResult:
        if source == "ib":
            raise ValueError("ib source is handled on the typed path, not the provider")
        pair = f"{from_ccy.upper()}/{to_ccy.upper()}"
        if source == "twelvedata":
            payload = self._td_client.exchange_rate(symbol=pair).as_json()
            data = {
                "pair": pair,
                "last": _td_float(payload, "rate"),
                "timestamp": payload.get("timestamp"),
            }
            return ResearchResult(
                json_clean(data), f"Forex quote: {pair}",
                provider="twelvedata", notice="TwelveData REST has no bid/ask.",
            )
        elif source == "massive":
            result = self._client.get_last_forex_quote(from_ccy.upper(), to_ccy.upper())
            data = {"pair": pair, "symbol": getattr(result, "symbol", pair)}
            last = getattr(result, "last", None)
            if last:
                data["bid"] = getattr(last, "bid", None)
                data["ask"] = getattr(last, "ask", None)
                data["exchange"] = getattr(last, "exchange", None)
                data["timestamp"] = getattr(last, "timestamp", None)
            return ResearchResult(json_clean(data), f"Forex quote: {pair}")
        else:
            raise ValueError(f"unknown forex source: {source!r}")

    def forex_movers(self, direction: str) -> ResearchResult:
        snaps = self._client.get_snapshot_direction(market_type="forex", direction=direction)
        rows = []
        for snap in snaps:
            row = {"ticker": getattr(snap, "ticker", "") or ""}
            day = getattr(snap, "day", None)
            if day:
                row["close"] = getattr(day, "close", None)
                row["volume"] = getattr(day, "volume", None)
            row["change"] = getattr(snap, "todays_change", None)
            row["change_pct"] = getattr(snap, "todays_change_percent", None)
            rows.append(row)
        return ResearchResult(json_clean(rows), f"Forex movers ({direction})")

    def forex_snapshot_all(self, tickers: list[str] | None) -> ResearchResult:
        ticker_arg = None
        if tickers:
            ticker_arg = [t if t.startswith("C:") else f"C:{t}" for t in tickers]
        snaps = self._client.get_snapshot_all(market_type="forex", tickers=ticker_arg)
        rows = []
        for snap in snaps:
            row = {"ticker": getattr(snap, "ticker", "") or ""}
            day = getattr(snap, "day", None)
            if day:
                for field in ("open", "high", "low", "close", "volume"):
                    row[field] = getattr(day, field, None)
            row["change"] = getattr(snap, "todays_change", None)
            row["change_pct"] = getattr(snap, "todays_change_percent", None)
            rows.append(row)
        return ResearchResult(json_clean(rows), "Forex snapshots")

    def forex_convert(self, from_ccy: str, to_ccy: str, amount: float) -> ResearchResult:
        # Attribute names (from_, to, initial_amount, converted, last.bid/ask)
        # mirror sdk.py's forex_convert massive branch exactly — the real
        # Massive SDK response has no "rate" field, so none is fabricated here.
        result = self._client.get_real_time_currency_conversion(
            from_ccy.upper(), to_ccy.upper(), amount=amount,
        )
        data = {
            "from": getattr(result, "from_", from_ccy.upper()),
            "to": getattr(result, "to", to_ccy.upper()),
            "amount": getattr(result, "initial_amount", float(amount)),
            "converted": getattr(result, "converted", None),
        }
        last = getattr(result, "last", None)
        if last:
            data["bid"] = getattr(last, "bid", None)
            data["ask"] = getattr(last, "ask", None)
        return ResearchResult(json_clean(data), f"Convert {from_ccy.upper()}→{to_ccy.upper()}")

    def _snapshot_from_twelvedata(self, ticker: str) -> ResearchResult:
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
            provider="twelvedata", notice=None,
        )

    def _movers_from_twelvedata(
        self,
        *,
        market: str,
        direction: str,
        limit: int,
        detail: bool,
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
            logger.warning(
                "%s", entitlement_fallback_notice("twelvedata", str(td_exc)),
            )
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
            json_clean(rows), title, provider="twelvedata", notice=None,
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
