"""Evidence for the discretionary scope rule (SP2 Plan 3 rulings 3, 5 and 12).

Exact IB contract details by conid or by symbol, and the 20-session dollar volume
from the trader's local daily bars or, failing that, its Alpaca history adapter.
Alpaca bars are held in memory only: price history is never written here.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.calendar_policy import ET
from trader.automation.production_evidence import (
    TwentySessionVolume, latest_closed_sessions, local_twenty_sessions, twenty_sessions_from_frame,
)
from trader.objects import BarSize
from trader.trading.approval_context import ApprovalContextError

logger = logging.getLogger(__name__)

INSTRUMENTS_UNIVERSE = "_instruments"
CONTRACT_DETAILS_TIMEOUT_SECONDS = 5.0
ALPACA_DAILY_BARS = "alpaca_daily_bars"
_SYMBOL = re.compile(r"^[A-Z]{1,5}$")


class ScopeEvidenceUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ContractEvidence:
    conid: int
    symbol: str
    sec_type: str
    currency: str
    primary_exchange: str
    stock_type: str
    fetched_at: dt.datetime


@dataclass(frozen=True)
class SymbolResolution:
    symbol: str
    status: str
    conid: Optional[int]
    contract: Optional[ContractEvidence]
    reason: str = ""


def _matching_rows(rows: list, matches: Callable[[Any], bool]) -> list:
    return [d for d in rows
            if getattr(d, "contract", None) is not None and type(d.contract.conId) is int
            and d.contract.conId > 0 and matches(d.contract)]


class IbContractEvidenceSource:
    """Ruling 3: exact IB contract details; each definition found is remembered in the trader universe."""

    def __init__(self, *, request_details: Callable[[Any], list], remember: Callable[[Any], None],
                 now: Callable[[], dt.datetime]):
        self._request_details, self._remember, self._now = request_details, remember, now

    def by_conid(self, conid: int) -> ContractEvidence:
        from ib_async import Contract
        rows = _matching_rows(self._request(Contract(conId=conid)), lambda c: c.conId == conid)
        if len(rows) != 1:
            raise ScopeEvidenceUnavailable(f"IB returned {len(rows)} contract details for conid {conid}")
        return self._evidence(rows[0])

    def by_symbol(self, symbol: str) -> SymbolResolution:
        """Ruling 12: an exact symbol, one distinct conid; never a fuzzy match."""
        from ib_async import Contract
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            return SymbolResolution(symbol, "SYMBOL_FORM_UNSUPPORTED", None, None, "only ^[A-Z]{1,5}$ is looked up")
        rows = _matching_rows(self._request(Contract(symbol=symbol, secType="STK", exchange="SMART", currency="USD")),
                              lambda c: c.symbol == symbol)
        conids = {d.contract.conId for d in rows}
        if not conids:
            return SymbolResolution(symbol, "NOT_FOUND", None, None, "IB has no USD stock with this symbol")
        if len(conids) > 1:
            return SymbolResolution(symbol, "AMBIGUOUS", None, None, f"IB returned conids {sorted(conids)}")
        evidence = self._evidence(rows[0])
        return SymbolResolution(symbol, "RESOLVED", evidence.conid, evidence)

    def _request(self, contract: Any) -> list:
        try:
            return list(self._request_details(contract) or [])
        except Exception as ex:
            # Provider text is opaque and may carry credentials: the type name only.
            raise ScopeEvidenceUnavailable(f"IB contract details failed: {type(ex).__name__}") from None

    def _evidence(self, details: Any) -> ContractEvidence:
        try:
            self._remember(details)
        except Exception as ex:
            logger.warning("could not remember conid %s in %s: %s", details.contract.conId, INSTRUMENTS_UNIVERSE,
                           type(ex).__name__)
        c = details.contract
        return ContractEvidence(conid=int(c.conId), symbol=str(c.symbol or ""), sec_type=str(c.secType or ""),
                                currency=str(c.currency or ""), primary_exchange=str(c.primaryExchange or ""),
                                stock_type=str(getattr(details, "stockType", "") or ""), fetched_at=self._now())


class DollarVolumeSource:
    """Ruling 5: local daily bars, else Alpaca SIP daily bars held in memory (price history is not written)."""

    def __init__(self, *, history: Any, alpaca_history: Callable[[], Any], now: Callable[[], dt.datetime]):
        self._history, self._alpaca_history, self._now = history, alpaca_history, now
        self._cache: dict[int, TwentySessionVolume] = {}
        self._lock = threading.Lock()

    def cached(self, conid: int) -> Optional[TwentySessionVolume]:
        with self._lock:
            found = self._cache.get(conid)
        return found if found is not None and found.is_current(self._now()) else None

    def twenty_sessions(self, conid: int, symbol: str) -> TwentySessionVolume:
        found = self.cached(conid)
        if found is not None:
            return found
        now = self._now()
        problems: list[str] = []
        try:
            volume = local_twenty_sessions(self._history, conid, now)
        except ApprovalContextError as ex:
            problems.append(f"local: {ex.code}")
            volume = self._from_alpaca(conid, symbol, latest_closed_sessions(now), problems)
        with self._lock:
            self._cache[conid] = volume
        return volume

    def _from_alpaca(self, conid: int, symbol: str, expected: tuple[dt.date, ...],
                     problems: list[str]) -> TwentySessionVolume:
        try:
            frame = self._alpaca_history().get_history(
                symbol, BarSize.Days1, dt.datetime.combine(expected[0], dt.time(), ET),
                dt.datetime.combine(expected[-1], dt.time(), ET))
            return twenty_sessions_from_frame(frame, conid, expected, ALPACA_DAILY_BARS)
        except ApprovalContextError as ex:
            problems.append(f"alpaca: {ex.code}")
        except Exception as ex:
            problems.append(f"alpaca: {type(ex).__name__}")
        raise ScopeEvidenceUnavailable("20-session dollar volume unavailable (" + "; ".join(problems) + ")")
