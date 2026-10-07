"""1-minute bar sources for the baseline simulator: local history first, then the trader's Alpaca provider."""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Callable, Optional, Protocol

import pandas as pd

from trader.objects import WhatToShow
from trader.scoreboard.simulator import Bar

logger = logging.getLogger(__name__)


class BarSourceError(RuntimeError):
    """A source failed. The message is a short code, never a URL, header or key."""


class BarSource(Protocol):
    name: str

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]: ...


BAD_BAR = "BAD_BAR"
ONE_MINUTE = "1 min"
TRADES = WhatToShow.TRADES


def _price(value: Any) -> float:
    price = float(value)
    if math.isnan(price):
        raise ValueError("a null price")
    return price


def frame_to_bars(frame: Optional[pd.DataFrame]) -> list[Bar]:
    """UTC bars; a frame that cannot be read as numbers is ``BarSourceError("BAD_BAR")``, never a crash."""
    if frame is None or len(frame) == 0:
        return []
    try:
        index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
        return [Bar(stamp.to_pydatetime(), _price(o), _price(h), _price(l), _price(c))
                for stamp, o, h, l, c in zip(index, frame["open"], frame["high"], frame["low"], frame["close"])]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("unreadable 1-minute bar frame: %s", type(exc).__name__)
        raise BarSourceError(BAD_BAR) from None


class LocalHistoryBars:
    name = "history_duckdb"
    what_to_show = TRADES          # only rows proven to be 1-minute TRADES bars are used

    def __init__(self, history_db_path: str):
        self._path = history_db_path

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        if not self._path:
            raise BarSourceError("NO_HISTORY_DB")
        from trader.data.data_access import TickStorage
        from trader.data.store import DateRange
        from trader.objects import BarSize
        try:
            tickdata = TickStorage(self._path).get_tickdata(BarSize.Mins1)
            raw = tickdata.read(conid, date_range=DateRange(start=start, end=end))
        except Exception as exc:
            logger.warning("local 1-minute history unreadable for conid %s: %s", conid, type(exc).__name__)
            raise BarSourceError(f"HISTORY_READ_{type(exc).__name__}") from exc
        return [b for b in frame_to_bars(_proven_trade_minutes(raw)) if start <= b.start < end]


def _proven_trade_minutes(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """TickStorage also returns rows with a NULL bar size, and MIDPOINT/BID/ASK rows: only rows proven to be
    1-minute TRADES bars may price a simulated fill."""
    if raw is None or not len(raw):
        return raw
    missing = sorted({"bar_size", "what_to_show"} - set(raw.columns))
    if missing:
        raise BarSourceError("HISTORY_NO_" + missing[0].upper())
    proven = raw["bar_size"].eq(ONE_MINUTE).fillna(False) & raw["what_to_show"].eq(int(TRADES)).fillna(False)
    return raw.loc[proven.astype(bool)]


class AlpacaBars:
    name = "alpaca"
    what_to_show = TRADES          # the Alpaca /bars endpoint aggregates trades

    def __init__(self, provider: Any, security_for: Callable[[int], Any]):
        self._provider = provider
        self._security_for = security_for

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        from trader.objects import BarSize
        security = self._security_for(conid)
        if security is None:
            raise BarSourceError("SYMBOL_UNRESOLVED")
        if security.secType != "STK" or security.currency != "USD":
            raise BarSourceError("UNSUPPORTED_INSTRUMENT")
        try:
            frame = self._provider.get_history(security.symbol, BarSize.Mins1, start, end)
        except Exception as exc:
            logger.warning("alpaca 1-minute history failed for conid %s: %s", conid, type(exc).__name__)
            raise BarSourceError(f"ALPACA_{type(exc).__name__}") from exc
        return [b for b in frame_to_bars(frame) if start <= b.start < end]


class _LazyAlpacaHistory:
    """Builds the Alpaca history provider on the first read, so trader startup builds no Alpaca client
    (a live account's command stack never builds one at start, #76)."""

    def __init__(self, keys: dict):
        self._keys = keys
        self._provider: Any = None

    def get_history(self, ticker: str, bar_size: Any, start: dt.datetime, end: dt.datetime) -> Any:
        if self._provider is None:
            from trader.data_providers import Capability, ProviderRegistry
            self._provider = ProviderRegistry.from_config(self._keys).get(Capability.HISTORY, "alpaca")
        return self._provider.get_history(ticker, bar_size, start, end)


def default_bar_sources(trader: Any) -> list[BarSource]:
    sources: list[BarSource] = [LocalHistoryBars(getattr(trader, "history_duckdb_path", "") or "")]
    keys = {"alpaca_api_key_id": getattr(trader, "alpaca_api_key_id", "") or "",
            "alpaca_api_secret_key": getattr(trader, "alpaca_api_secret_key", "") or ""}
    if not all(value.strip() for value in keys.values()):
        logger.warning("no Alpaca history for baseline simulation (keys not set); local bars only")
        return sources

    def security_for(conid: int):
        rows = trader.universe_accessor.resolve_symbol(conid, first_only=True)
        return rows[0] if rows else None
    sources.append(AlpacaBars(_LazyAlpacaHistory(keys), security_for))
    return sources
