"""trading_filters.yaml for ai_paper entries (owner answer 4, Plan 3 R6).

The filter is applied to the instrument the trader resolves from the conid,
never to fields of the decision. The exchange is the exact listing exchange
(``primaryExchange``); ``SMART`` is a route and never stands in for it.
Reductions never consult this filter.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from trader.trading.trading_filter import TradingFilter

INSTRUMENT_UNRESOLVED = "INSTRUMENT_UNRESOLVED"
TRADING_FILTER_DENIED = "TRADING_FILTER_DENIED"
TRADING_FILTER_UNAVAILABLE = "TRADING_FILTER_UNAVAILABLE"


class MtimeCachedFilterLoader:
    """Re-reads trading_filters.yaml only when its modification time changes."""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._cached: Optional[tuple[Optional[int], TradingFilter]] = None

    def __call__(self) -> TradingFilter:
        mtime = self._mtime()
        with self._lock:
            if self._cached is not None and self._cached[0] == mtime and mtime is not None:
                return self._cached[1]
            loaded = TradingFilter.load(self._path)
            self._cached = (mtime, loaded)
            return loaded

    def _mtime(self) -> Optional[int]:
        if self._path is not None:
            path = Path(self._path)
        else:
            from trader.trading.trading_filter import _default_path
            path = _default_path()
        try:
            return os.stat(path).st_mtime_ns
        except FileNotFoundError:
            return None


class AiEntryFilter:
    def __init__(self, *, universe: Any, load_filter: Optional[Callable[[], TradingFilter]] = None):
        self._universe = universe
        self._load_filter = load_filter or MtimeCachedFilterLoader()
        self.last_reason = ""

    def refusal(self, conid: int, price: float) -> Optional[str]:
        row = self._resolved(conid)
        if row is None:
            return INSTRUMENT_UNRESOLVED
        try:
            trading_filter = self._load_filter()
        except Exception as exc:
            self.last_reason = f"trading filter unavailable: {type(exc).__name__}"
            return TRADING_FILTER_UNAVAILABLE
        allowed, reason = trading_filter.is_allowed(
            symbol=row.symbol, exchange=row.primaryExchange, sec_type=row.secType, price=price)
        if not allowed:
            self.last_reason = reason
            return TRADING_FILTER_DENIED
        self.last_reason = ""
        return None

    def _resolved(self, conid: int) -> Optional[Any]:
        """The one instrument with exactly this conId and a listing exchange, or None."""
        try:
            rows = list(self._universe.resolve_symbol(conid))
        except Exception as exc:
            self.last_reason = f"instrument lookup failed: {type(exc).__name__}"
            return None
        if len(rows) != 1 or type(rows[0].conId) is not int or rows[0].conId != conid:
            self.last_reason = f"conid {conid} does not resolve to exactly one instrument"
            return None
        row = rows[0]
        if not isinstance(row.primaryExchange, str) or not row.primaryExchange.strip():
            # TradingFilter skips every exchange rule on a blank exchange: that would read as "allowed".
            self.last_reason = f"conid {conid} has no listing exchange"
            return None
        if not isinstance(row.symbol, str) or not row.symbol.strip():
            self.last_reason = f"conid {conid} has no symbol"
            return None
        return row
