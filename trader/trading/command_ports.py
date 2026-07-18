"""IB-backed read adapters for the command authority.

Design: docs/superpowers/specs/2026-07-18-command-plane-activation-design.md
(Phase 5 ports / sequence step 2b).

Thin, READ-ONLY wrappers that implement the command-authority port protocols
(``PositionAuthority``, ``BrokerAuthority``) over live trader state. No mutation,
no order placement — ``what_if_margin`` runs whatIfOrder, which by definition
places nothing.

Loop-safety (design C1): the command handlers run OFF the trader's asyncio loop
(``asyncio.to_thread(coordinator.execute, ...)``), so these adapters run on a
worker thread. In-memory reads (portfolio / book / cached account values / PnL)
are plain snapshot reads and safe from that thread. The one async read
(``check_order_margin`` -> whatIfOrder) is marshalled back onto the trader loop
by the injected ``run_coro`` (production: ``run_coroutine_threadsafe(coro,
loop).result(timeout)``) so it never touches ib_async off-loop.

The ``QuoteAuthority`` adapter carries the IB ``Ticker``'s REAL market timestamp
(``ticker.time``), never a re-stamp to "now" — so a snapshot taken while the
market is closed reports the old close time and is correctly detected as stale
downstream, rather than reintroducing the "stale quote looks fresh" hazard just
fixed on the read path.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from ib_async import Order

from trader.trading.proposal_command_service import ExecutableQuote

logger = logging.getLogger(__name__)

# IB marketDataType -> feed label (reqMarketDataType / Ticker.marketDataType).
_MARKET_DATA_TYPE = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


def scoped_net_liquidation(
    account_values: Iterable[Any], active_account: Optional[str]) -> float:
    """Pick the NetLiquidation for ``active_account`` from IB's account-value
    rows. A multi-account login returns a NetLiquidation row per managed
    account plus a ``BASE`` summary; picking blind could size risk against the
    wrong (aggregate) account, so match the account and skip ``BASE``. Returns
    0.0 when there is no matching row. (De-duplicates the inline logic in
    ``trading_runtime.place_expressive_order``.)"""
    for v in account_values:
        if getattr(v, "tag", None) != "NetLiquidation" or getattr(v, "currency", None) == "BASE":
            continue
        account = getattr(v, "account", None)
        if active_account and account and account != active_account:
            continue
        return float(v.value)
    return 0.0


class TraderPositionAuthority:
    """``PositionAuthority`` over the trader's in-memory ``Portfolio``."""

    def __init__(self, trader):
        self._trader = trader

    def reducible_quantity(self, account_id: str, conid: int) -> float:
        """The signed held quantity for (account, conid), 0.0 if flat. The sign
        is preserved (IB's convention: long > 0, short < 0) so the caller can
        classify a SELL as a reducing close vs an exposure-increasing short."""
        for pos in self._trader.portfolio.get_positions():
            if pos.account == account_id and getattr(pos.contract, "conId", None) == conid:
                return float(pos.position)
        return 0.0


class TraderBrokerAuthority:
    """``BrokerAuthority`` over live trader/account state.

    ``run_coro`` marshals a coroutine onto the trader loop and returns its
    result (production: ``run_coroutine_threadsafe(coro, loop).result(timeout)``).
    ``resolve_contract`` maps a conId to an IB ``Contract`` for the whatIfOrder
    probe (production: the trader's universe/portfolio resolution)."""

    def __init__(self, trader, *, run_coro: Callable[[Any], Any],
                 resolve_contract: Callable[[int], Optional[Any]],
                 timeout: float = 5.0):
        self._trader = trader
        self._run_coro = run_coro
        self._resolve_contract = resolve_contract
        self._timeout = timeout

    def _active_account(self) -> Optional[str]:
        return self._trader.ib_account or (
            (self._trader.client.ib.managedAccounts() or [None])[0])

    def is_ready(self) -> bool:
        ingest = getattr(self._trader, "broker_ingest", None)
        if ingest is not None:
            return bool(ingest.is_ready())
        return bool(self._trader.is_ib_connected())

    def net_liquidation(self) -> float:
        return scoped_net_liquidation(
            self._trader.client.ib.accountValues(), self._active_account())

    def daily_pnl(self) -> float:
        total = 0.0
        for p in (self._trader.get_pnl() or []):
            total += float(getattr(p, "dailyPnL", 0.0) or 0.0)
        return total

    def open_order_count(self) -> int:
        return int(self._trader.book.get_open_order_count())

    def position_value(self, conid: int) -> float:
        for item in self._trader.portfolio.get_portfolio_items():
            if getattr(item.contract, "conId", None) == conid:
                return float(item.marketValue)
        return 0.0

    def what_if_margin(self, conid: int, side: str, quantity: float) -> Optional[dict]:
        """whatIfOrder margin impact for a MKT probe of (side, quantity), or
        None when unavailable — leverage is a secondary check the mode-aware
        consumer gates, so a None here never fails the capture. whatIfOrder
        places nothing."""
        try:
            contract = self._resolve_contract(conid)
            if contract is None:
                return None
            probe = Order(action=side, orderType="MKT", totalQuantity=abs(float(quantity)))
            return self._run_coro(self._trader.check_order_margin(contract, probe))
        except Exception as exc:  # noqa: BLE001 — best-effort; leverage gated by consumer
            logger.warning("what_if_margin unavailable for conid %s: %s", conid, exc)
            return None


def _feed_type(ticker) -> str:
    return _MARKET_DATA_TYPE.get(getattr(ticker, "marketDataType", None), "unknown")


def _session_state(ticker) -> str:
    halted = getattr(ticker, "halted", 0) or 0
    return "halted" if halted > 0 else "continuous"


def _usable_price(value) -> Optional[float]:
    """A tradable price, or None. Rejects None, NaN, and non-positive values —
    an empty/absent bid or ask must not become an executable quote."""
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price != price or price <= 0:  # NaN or non-positive
        return None
    return price


class TraderQuoteAuthority:
    """``QuoteAuthority`` backed by a fresh IB snapshot (reqMktData ``Ticker``).

    Executable price = the BUY ask / SELL bid (the price you'd cross the spread
    at). The quote carries the Ticker's REAL ``time`` so staleness is honest.
    Returns None -- and the capture then fails closed -- when the contract can't
    be resolved, the side has no usable price, the quote has no market
    timestamp, or the snapshot fails."""

    def __init__(self, trader, *, run_coro: Callable[[Any], Any],
                 resolve_contract: Callable[[int], Optional[Any]],
                 delayed: bool = False):
        self._trader = trader
        self._run_coro = run_coro
        self._resolve_contract = resolve_contract
        self._delayed = delayed

    def executable_quote(self, conid: int, *, side: str) -> Optional[ExecutableQuote]:
        try:
            contract = self._resolve_contract(conid)
            if contract is None:
                return None
            ticker = self._run_coro(
                self._trader.client.get_snapshot(contract, self._delayed))
            if ticker is None:
                return None
            raw = (getattr(ticker, "ask", None) if side.upper() == "BUY"
                   else getattr(ticker, "bid", None))
            price = _usable_price(raw)
            if price is None:
                return None
            market_timestamp = getattr(ticker, "time", None)
            if market_timestamp is None:
                return None  # no real market time -> can't age it -> not tradable
            return ExecutableQuote(
                conid=conid, side=side, price=price,
                market_timestamp=market_timestamp,
                feed_type=_feed_type(ticker),
                session_state=_session_state(ticker),
            )
        except Exception as exc:  # noqa: BLE001 — no usable quote -> capture fails closed
            logger.warning("executable_quote unavailable for conid %s: %s", conid, exc)
            return None
