"""Paper-only fallback: Alpaca's real-time IEX quote when IB has no live feed (issue #74).

IEX is one venue, so its book is thinner and wider than the national best bid
and offer. Alpaca's quote has no halt flag: ``session_state`` comes from the
XNYS calendar only, so a halt inside the regular session is not seen here.
Alpaca's forum says sizes are round lots; they are passed on unchanged, which
is a lower bound on the shares shown (a round lot is at least one share).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from typing import Any, Callable, Optional

import pandas as pd

from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data_providers.alpaca.us_listing import US_PRIMARY_EXCHANGES
from trader.trading.command_ports import _side_wants_ask, _usable_depth, _usable_price
from trader.trading.proposal_command_service import ExecutableQuote, QuoteAuthority
from trader.trading.quote_feeds import IEX_REALTIME_FEED, LIVE_FEED

logger = logging.getLogger(__name__)

LATEST_QUOTES_PATH = "/v2/stocks/quotes/latest"
CONTINUOUS = "continuous"
OUTSIDE_REGULAR_SESSION = "closed"
HALTED = "halted"

# IB spells a class share with one space ("BRK B"); Alpaca with a dot ("BRK.B").
_IB_STOCK_SYMBOL = re.compile(r"[A-Z0-9]+( [A-Z0-9]+)?")


def alpaca_symbol(conid: int, security: Any) -> Optional[str]:
    """Alpaca's spelling of the IB contract the trader resolved, or None for any other shape."""
    if security is None:
        return None
    if (getattr(security, "conId", None) != conid
            or getattr(security, "secType", None) != "STK"
            or getattr(security, "currency", None) != "USD"
            or getattr(security, "primaryExchange", None) not in US_PRIMARY_EXCHANGES):
        return None
    symbol = getattr(security, "symbol", None)
    if not isinstance(symbol, str) or not _IB_STOCK_SYMBOL.fullmatch(symbol):
        return None
    return symbol.replace(" ", ".")


def _aware_utc(value: Any) -> Optional[dt.datetime]:
    """Alpaca's RFC 3339 time (nanoseconds) as an aware UTC datetime; a zoneless time is refused."""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp) or stamp.tzinfo is None:
        return None
    return stamp.tz_convert("UTC").floor("us").to_pydatetime()


class AlpacaIexQuoteAuthority:
    """``QuoteAuthority`` over Alpaca's latest IEX quote. Any failure returns None."""

    def __init__(self, client: Any, *, resolve_security: Callable[[int], Any],
                 now: Callable[[], dt.datetime], calendar: Optional[XNYSCalendarPolicy] = None):
        self._client = client
        self._resolve_security = resolve_security
        self._now = now
        self._calendar = calendar or XNYSCalendarPolicy()
        # requests.Session is not thread-safe; quote reads run on worker threads.
        self._client_lock = threading.Lock()

    def executable_quote(self, conid: int, *, side: str) -> Optional[ExecutableQuote]:
        try:
            symbol = alpaca_symbol(conid, self._resolve_security(conid))
            if symbol is None:
                return None
            with self._client_lock:
                body = self._client.get_json(LATEST_QUOTES_PATH, {"symbols": symbol, "feed": "iex"})
            raw = ((body or {}).get("quotes") or {}).get(symbol)
            return self._to_quote(conid, side, raw)
        except Exception as exc:  # noqa: BLE001 — no quote; the text may carry request details
            logger.warning("alpaca iex quote unavailable for conid %s: %s", conid, type(exc).__name__)
            return None

    def _to_quote(self, conid: int, side: str, raw: Any) -> Optional[ExecutableQuote]:
        if not isinstance(raw, dict):
            return None
        timestamp = _aware_utc(raw.get("t"))
        bid = _usable_price(raw.get("bp"))
        ask = _usable_price(raw.get("ap"))
        if timestamp is None or bid is None or ask is None:
            return None
        return ExecutableQuote(
            conid=conid, side=side, price=ask if _side_wants_ask(side) else bid,
            market_timestamp=timestamp, feed_type=IEX_REALTIME_FEED,
            session_state=self._session_state(timestamp, self._now()),
            bid=bid, ask=ask,
            bid_size=_usable_depth(raw.get("bs")), ask_size=_usable_depth(raw.get("as")),
        )

    def _session_state(self, *moments: dt.datetime) -> str:
        for moment in moments:
            schedule = self._calendar.resolve(moment)
            if schedule is None or not schedule.open_utc <= moment < schedule.close_utc:
                return OUTSIDE_REGULAR_SESSION
        return CONTINUOUS


class FallbackQuoteAuthority:
    """IB first; the IEX quote only when IB has no live quote and reports no halt. Paper only."""

    def __init__(self, primary: QuoteAuthority, fallback: QuoteAuthority, *, account_mode: str):
        if account_mode != "paper":
            raise ValueError(f"the IEX quote fallback is paper only, not {account_mode!r}")
        self._primary = primary
        self._fallback = fallback

    def executable_quote(self, conid: int, *, side: str) -> Optional[ExecutableQuote]:
        quote = self._primary.executable_quote(conid, side=side)
        if quote is not None and (quote.feed_type == LIVE_FEED or quote.session_state == HALTED):
            # IEX has no halt flag, so an IB halt (on any feed) must never be replaced.
            return quote
        iex = self._fallback.executable_quote(conid, side=side)
        # Without an IEX quote, keep IB's answer: a delayed reference still serves
        # manual paper proposals, and the feed checks refuse it for automated entries.
        return quote if iex is None else iex
