"""Quote feed labels, and which feeds count as executable evidence.

A live account accepts only IB's ``live`` feed. A paper account may also accept
Alpaca's real-time IEX quote, but only when ``automation.quote_fallback`` is
``alpaca_iex``. Every check that refuses ``FEED_NOT_LIVE`` takes the accepted
set explicitly from the command stack.
"""
from __future__ import annotations

LIVE_FEED = "live"
IEX_REALTIME_FEED = "iex_realtime"

LIVE_ONLY_FEEDS = frozenset({LIVE_FEED})
PAPER_IEX_FEEDS = frozenset({LIVE_FEED, IEX_REALTIME_FEED})

QUOTE_FALLBACK_OFF = ""
QUOTE_FALLBACK_ALPACA_IEX = "alpaca_iex"
QUOTE_FALLBACK_VALUES = (QUOTE_FALLBACK_OFF, QUOTE_FALLBACK_ALPACA_IEX)


def parse_quote_fallback(value: object) -> str:
    """The exact setting, or ValueError. Unset (None or empty) means off."""
    setting = QUOTE_FALLBACK_OFF if value is None else value
    if not isinstance(setting, str) or setting not in QUOTE_FALLBACK_VALUES:
        raise ValueError(
            f"automation.quote_fallback must be empty or {QUOTE_FALLBACK_ALPACA_IEX!r}, got {value!r}"
        )
    return setting


def accepted_feeds(account_mode: str, quote_fallback: str) -> frozenset[str]:
    if account_mode == "paper" and quote_fallback == QUOTE_FALLBACK_ALPACA_IEX:
        return PAPER_IEX_FEEDS
    return LIVE_ONLY_FEEDS


def require_live_feed_on_live_account(account_mode: str, feeds: frozenset[str]) -> None:
    if account_mode != "paper" and frozenset(feeds) != LIVE_ONLY_FEEDS:
        raise ValueError(f"a {account_mode} account accepts only the live feed, not {sorted(feeds)}")
