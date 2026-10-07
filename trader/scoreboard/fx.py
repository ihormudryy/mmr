"""FX evidence for USD reporting, from the trader's own IB account values (spec 5.2)."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable, Mapping

from trader.scoreboard.ports import FxEvidence


class FxEvidenceError(RuntimeError):
    """The account's base currency is not readable."""


class IbFxEvidence:
    """``get_cash`` is ``TraderServiceApi.get_account_cash_by_currency``.

    IB reports ``exchange_rate`` as base units per one unit of the currency, so
    USD per base unit is ``1 / currencies["USD"]["exchange_rate"]``.
    """

    def __init__(self, get_cash: Callable[[], Mapping[str, Any]], now: Callable[[], dt.datetime]):
        self._get_cash = get_cash
        self._now = now

    def evidence(self) -> FxEvidence:
        cash = self._get_cash()
        base = cash.get("base_currency") if isinstance(cash, Mapping) else None
        if type(base) is not str or len(base) != 3 or not base.isalpha() or not base.isupper():
            raise FxEvidenceError(f"base currency {base!r} is not a currency code")
        now = self._now()
        if base == "USD":
            return FxEvidence("USD", 1.0, "base_is_usd", now)
        currencies = cash.get("currencies")
        usd = currencies.get("USD") if isinstance(currencies, Mapping) else None
        rate = usd.get("exchange_rate") if isinstance(usd, Mapping) else None
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
            return FxEvidence(base, None, "ib_account_values", now)
        return FxEvidence(base, 1.0 / float(rate), "ib_account_values", now)
