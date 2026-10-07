"""SPY closes the scoreboard used, immutable and versioned (spec 5.2, ruling 14).

A stored close is never changed by a later refresh; a correction is a new
version. A missing bar is simply absent: it is never forward-filled.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable, Mapping, Optional

from trader.research.market_context import BENCHMARK_CONID
from trader.scoreboard.store import ScoreboardStore

SYMBOL = "SPY"
PROVIDER = "history_duckdb"
BAR_SIZE = "1 day"
DOWNLOAD = 'mmr data download SPY --bar-size "1 day"'


class BenchmarkSourceError(RuntimeError):
    """The history DB has no usable SPY daily bars."""


def read_spy_closes(history_db_path: str, start: dt.date, end: dt.date) -> dict[dt.date, float]:
    """SPY daily closes by UTC bar date in ``[start, end]``, from the local history DB."""
    from trader.data.data_access import TickStorage
    from trader.data.market_data import normalize_historical
    from trader.data.store import DateRange
    from trader.objects import BarSize

    tickdata = TickStorage(history_db_path).get_tickdata(BarSize.parse_str(BAR_SIZE))
    begin = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    finish = dt.datetime.combine(end, dt.time.min, tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    raw = tickdata.read(BENCHMARK_CONID, date_range=DateRange(start=begin, end=finish))
    if raw is None or len(raw) == 0:
        raise BenchmarkSourceError(
            f"no SPY (conid {BENCHMARK_CONID}) daily bars from {start} to {end} in the history DB; run: {DOWNLOAD}")
    frame = normalize_historical(raw).dropna(subset=["close"])
    index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
    closes: dict[dt.date, float] = {}
    for stamp, close in zip(index, frame["close"].astype(float)):
        day = stamp.date()
        if not start <= day <= end:
            continue
        if day in closes:
            raise BenchmarkSourceError(f"SPY has more than one daily bar for {day}; re-download: {DOWNLOAD}")
        closes[day] = float(close)
    return closes


class BenchmarkBook:
    def __init__(self, store: ScoreboardStore, source: Callable[[dt.date, dt.date], Mapping[dt.date, float]],
                 calendar: Any, now: Callable[[], dt.datetime]):
        self.store = store
        self._source = source
        self._calendar = calendar
        self._now = now

    def current_version(self) -> Optional[int]:
        versions = self.store.fetch("benchmark_versions", {})
        return None if not versions else max(int(v["version"]) for v in versions)

    def closes(self, version: Optional[int] = None) -> dict[dt.date, float]:
        version = self.current_version() if version is None else version
        if version is None:
            return {}
        return {row["bar_date"]: row["close"] for row in self.store.fetch("benchmark_prices", {"version": version})}

    def provider(self) -> str:
        return PROVIDER

    def _price_row(self, version: int, day: dt.date, close: float, fetched_at: dt.datetime) -> dict:
        return {"version": version, "bar_date": day, "symbol": SYMBOL, "conid": BENCHMARK_CONID,
                "close": float(close), "provider": PROVIDER, "bar_size": BAR_SIZE, "fetched_at": fetched_at}

    def refresh(self, start: dt.date, end: dt.date) -> int:
        """Store the missing session closes in ``[start, end]``; returns how many were stored."""
        sessions = self._calendar.sessions_in_range(start, end)
        if not sessions:
            return 0
        version = self.current_version()
        stored = {} if version is None else self.closes(version)
        source = self._source(start, end)
        for day in sessions:
            if day in stored and day in source and abs(float(source[day]) - stored[day]) > 1e-9:
                self.store.record_incident(
                    "BENCHMARK_SOURCE_DIFFERS", f"{version}:{day.isoformat()}",
                    f"SPY close for {day} is {source[day]} in the history DB, {stored[day]} in benchmark "
                    f"version {version}; the stored close is kept (correct_benchmark makes a new version)")
        missing = [day for day in sessions if day not in stored and day in source]
        if not missing:
            return 0
        now = self._now()
        items = []
        if version is None:
            version = 1
            items.append(("benchmark_versions", {"version": 1, "reason": "initial", "created_at": now}))
        items += [("benchmark_prices", self._price_row(version, day, source[day], now)) for day in missing]
        self.store.insert_sealed_many(items)
        return len(missing)

    def correct_benchmark(self, bar_date: dt.date, close: float, reason: str) -> int:
        """A new version: the current rows with ``bar_date`` replaced. Returns the new version."""
        version = self.current_version()
        current = {} if version is None else self.closes(version)
        if bar_date not in current:
            raise ValueError(f"no stored SPY close for {bar_date} to correct")
        if not isinstance(close, (int, float)) or isinstance(close, bool) or not math.isfinite(close) or close <= 0:
            raise ValueError(f"a corrected close must be a positive number, got {close!r}")
        if not reason:
            raise ValueError("a correction needs a reason")
        now = self._now()
        new_version = version + 1
        items = [("benchmark_versions", {"version": new_version, "reason": reason, "created_at": now})]
        for day, value in sorted(current.items()):
            items.append(("benchmark_prices",
                          self._price_row(new_version, day, close if day == bar_date else value, now)))
        self.store.insert_sealed_many(items)
        return new_version
