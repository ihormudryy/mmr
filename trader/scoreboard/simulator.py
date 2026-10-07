"""Bracket outcome of one long position from 1-minute bars (SP2 Plan 2, ruling 10 and 11).

Pure: no I/O, no clock. Entry at the reference price; only bars after the minute of the decision are scanned.
One bar that touches both stop and target is a stop. Missing data is INCOMPLETE, never filled in.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

MINUTE = dt.timedelta(minutes=1)
MAX_BAR_GAP = dt.timedelta(minutes=30)
FLATTEN_BAR_WINDOW = dt.timedelta(minutes=10)


class Bar(NamedTuple):
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SimInput:
    conid: int
    quantity: int
    reference_price: float
    stop_price: float
    target_price: float
    decided_at: dt.datetime
    flatten_start_utc: dt.datetime


@dataclass(frozen=True)
class SimResult:
    status: str
    reason: Optional[str]
    exit_kind: str
    exit_at: Optional[dt.datetime]
    exit_price: Optional[float]
    pnl_usd: Optional[float]
    trades: Optional[int]
    bars_digest: Optional[str]


def _incomplete(reason: str) -> SimResult:
    return SimResult("INCOMPLETE", reason, "NONE", None, None, None, None, None)


def _floor_minute(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)


def _is_valid(bar: Bar) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close)
    return (all(isinstance(p, (int, float)) and math.isfinite(p) and p > 0 for p in prices)
            and bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close)
            and bar.start.tzinfo is not None)


def _digest(bars: Sequence[Bar]) -> str:
    rows = [[b.start.astimezone(dt.timezone.utc).isoformat(), repr(b.open), repr(b.high), repr(b.low),
             repr(b.close)] for b in bars]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _hit(trade: SimInput, bar: Bar) -> tuple[Optional[str], Optional[float]]:
    if bar.low <= trade.stop_price:
        return "STOP", min(trade.stop_price, bar.open)
    if bar.high >= trade.target_price:
        return "TARGET", trade.target_price
    return None, None


def _complete(trade: SimInput, kind: str, at: dt.datetime, price: float, used: Sequence[Bar]) -> SimResult:
    pnl = (price - trade.reference_price) * trade.quantity
    return SimResult("COMPLETE", None, kind, at, price, pnl, 1, _digest(used))


def simulate_long_bracket(trade: SimInput, bars: Sequence[Bar]) -> SimResult:
    first_start = _floor_minute(trade.decided_at) + MINUTE
    flatten = trade.flatten_start_utc
    used = sorted((b for b in bars if first_start <= b.start < flatten + FLATTEN_BAR_WINDOW),
                  key=lambda b: b.start)
    if not used:
        return _incomplete("NO_BARS")
    if len({b.start for b in used}) != len(used):
        return _incomplete("DUPLICATE_BAR")
    if not all(_is_valid(b) for b in used):
        return _incomplete("BAD_BAR")
    previous_start = _floor_minute(trade.decided_at)       # the entry minute; then each observed bar start
    for bar in (b for b in used if b.start < flatten):
        if bar.start - previous_start > MAX_BAR_GAP:
            return _incomplete("BAR_GAP")
        kind, price = _hit(trade, bar)
        if kind is not None:
            return _complete(trade, kind, bar.start, price, [b for b in used if b.start <= bar.start])
        previous_start = bar.start
    exit_bar = next((b for b in used if b.start >= flatten), None)
    if exit_bar is None:
        return _incomplete("NO_FLATTEN_BAR")
    if exit_bar.start - previous_start > MAX_BAR_GAP:
        return _incomplete("BAR_GAP")
    return _complete(trade, "FLATTEN", exit_bar.start, exit_bar.open, [b for b in used if b.start <= exit_bar.start])
