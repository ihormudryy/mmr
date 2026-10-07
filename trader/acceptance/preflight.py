"""The clean-account gate (Plan 6 rulings 3 and 4). Pure: two readings in, a verdict out."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

MAX_EQUITY_AGE_SECONDS = 300.0
MAX_EQUITY_DRIFT = 0.001          # 0.1 % between two readings at least 30 s apart
READINGS_APART_SECONDS = 30.0


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    failures: tuple[str, ...]


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _parse_time(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _reading_failures(reading: Mapping[str, Any], now: dt.datetime) -> list[str]:
    if reading.get("capture_error"):
        return ["CAPTURE_UNAVAILABLE"]
    failures = []
    account_id = reading.get("account_id")
    if reading.get("account_mode") != "paper" or not isinstance(account_id, str) or not account_id.startswith("DU"):
        failures.append("NOT_PAPER")
    if reading.get("positions"):
        failures.append("POSITIONS_OPEN")
    if reading.get("working_orders"):
        failures.append("WORKING_ORDERS")
    if reading.get("unresolved_commands"):
        failures.append("UNRESOLVED_COMMANDS")
    if reading.get("open_liquidation_roots"):
        failures.append("LIQUIDATION_OPEN")
    if reading.get("exit_owner") is not None:
        failures.append("EXIT_OWNER_ACTIVE")
    if reading.get("breaker_tripped") is not False:
        failures.append("BREAKER_TRIPPED")
    nlv = reading.get("net_liquidation")
    if not _finite(nlv) or nlv <= 0:
        failures.append("EQUITY_INVALID")
    as_of = _parse_time(reading.get("nlv_as_of"))
    if as_of is None or (now - as_of).total_seconds() > MAX_EQUITY_AGE_SECONDS:
        failures.append("EQUITY_STALE")
    if not _finite(reading.get("daily_pnl")):
        failures.append("DAILY_PNL_UNKNOWN")
    if reading.get("base_currency") != "USD":
        rate = reading.get("usd_per_base")
        if not _finite(rate) or rate <= 0:
            failures.append("FX_UNAVAILABLE")
    return failures


def evaluate_preflight(first: Mapping[str, Any], second: Mapping[str, Any], *, now: dt.datetime) -> PreflightResult:
    """Both readings must be clean, and the account value must hold still between them."""
    failures: list[str] = []
    for reading in (first, second):
        for code in _reading_failures(reading, now):
            if code not in failures:
                failures.append(code)
    a, b = first.get("net_liquidation"), second.get("net_liquidation")
    if _finite(a) and _finite(b) and a > 0 and abs(b - a) / a > MAX_EQUITY_DRIFT:
        failures.append("EQUITY_UNSTABLE")
    return PreflightResult(passed=not failures, failures=tuple(failures))
