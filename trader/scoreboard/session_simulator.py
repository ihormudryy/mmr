"""Writes one sealed outcome per simulated decision once the session's bars are ready (SP2 Plan 2, ruling 13)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from trader.scoreboard.bar_sources import BarSource, BarSourceError
from trader.scoreboard.simulator import FLATTEN_BAR_WINDOW, MINUTE, SimInput, SimResult, simulate_long_bracket
from trader.scoreboard.ports import NullCloseFills, StoreTripFacts
from trader.scoreboard.store import ScoreboardConflict, ScoreboardStore

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DATA_READY_ET = dt.time(20, 16)          # the Alpaca provider's own completed-session rule
GRACE = dt.timedelta(hours=2)
RETRY_AFTER = dt.timedelta(minutes=5)
PENDING_SQL = (
    "SELECT d.record_id, d.experiment_id, d.baseline_id, d.cohort, d.session_date, d.conid, d.quantity, "
    "d.reference_price, d.stop_price, d.target_price, d.decided_at, d.opportunity_id, d.linked_round_trip_id, "
    "d.recorded_at FROM simulated_decisions d "
    "LEFT JOIN simulated_outcomes o ON o.record_id = d.record_id WHERE o.record_id IS NULL "
    "ORDER BY d.session_date, d.decided_at, d.recorded_at, d.record_id")
MATCHED_ENTRY = "matched_entry_bracket_exit.v1"
NO_SHARES = SimResult("COMPLETE", "CLOSE_REMOVED_NO_SHARES", "NONE", None, None, 0.0, 0, None)
UNPROVEN = SimResult("INCOMPLETE", "close_fill_unproven", "NONE", None, None, None, None, None)


class SessionSimulator:
    def __init__(self, *, store: ScoreboardStore, calendar: Any, sources: Sequence[BarSource],
                 now: Callable[[], dt.datetime], grace: dt.timedelta = GRACE,
                 retry_after: dt.timedelta = RETRY_AFTER, close_fills: Any = None, trips: Any = None):
        self._store, self._calendar, self._sources, self._now = store, calendar, list(sources), now
        self._close_fills = close_fills or NullCloseFills()     # Ruling 21: proven shares per model close
        self._trips = trips or StoreTripFacts(store)
        self._grace, self._retry_after = grace, retry_after
        self._last_try: dict[str, dt.datetime] = {}

    @staticmethod
    def data_ready_at(session_date: dt.date) -> dt.datetime:
        return dt.datetime.combine(session_date, DATA_READY_ET, tzinfo=ET).astimezone(dt.timezone.utc)

    def run_due(self) -> int:
        now = self._now()
        written = 0
        cache: dict[tuple[str, int, dt.date], list] = {}
        for record in self._store.db.execute(PENDING_SQL, fetch="all"):
            row = dict(zip(("record_id", "experiment_id", "baseline_id", "cohort", "session_date", "conid",
                            "quantity", "reference_price", "stop_price", "target_price", "decided_at",
                            "opportunity_id", "linked_round_trip_id", "recorded_at"), record))
            ready = self.data_ready_at(row["session_date"])
            if now < ready:
                continue
            tried = self._last_try.get(row["record_id"])
            if tried is not None and now - tried < self._retry_after:
                continue
            self._last_try[row["record_id"]] = now
            if row["baseline_id"] == MATCHED_ENTRY:
                verdict = self._matched_quantity(row)
                if verdict is None:            # an earlier close of the trip is still open: keep the order
                    continue
                if isinstance(verdict, SimResult):
                    if verdict is UNPROVEN and now < ready + self._grace:
                        continue               # the close's fills may still be attributed
                    written += self._write(row, verdict, "none", now)
                    continue
                row["quantity"] = verdict
            result, source = self._simulate(row, cache)
            if result.status == "INCOMPLETE" and now < ready + self._grace:
                continue                       # data may still arrive; the next attempt is allowed
            written += self._write(row, result, source, now)
        return written

    def _matched_quantity(self, row: dict) -> Any:
        """Ruling 21: min(requested, shares the close is proven to have removed), clipped per trip to the proven
        entry fill. Returns the quantity, a final SimResult (UNPROVEN or NO_SHARES), or None to wait."""
        trip_id = row["linked_round_trip_id"]
        earlier = self._store.db.execute(
            "SELECT d.record_id, o.status, o.quantity FROM simulated_decisions d LEFT JOIN simulated_outcomes o "
            "ON o.record_id = d.record_id WHERE d.experiment_id = ? AND d.baseline_id = ? "
            "AND d.linked_round_trip_id = ? AND (d.recorded_at < ? OR (d.recorded_at = ? AND d.record_id < ?))",
            [row["experiment_id"], MATCHED_ENTRY, trip_id, row["recorded_at"], row["recorded_at"], row["record_id"]],
            fetch="all")
        if any(status is None for _, status, _ in earlier):
            return None
        trip = self._trips.by_id(row["experiment_id"], trip_id)
        fill = self._close_fills.removed(trip_id, row["opportunity_id"])
        if trip is None or not fill.proven:
            return UNPROVEN
        if fill.shares == 0:
            return NO_SHARES
        counted = sum(int(quantity or 0) for _, status, quantity in earlier if status == "COMPLETE")
        quantity = min(int(row["quantity"]), int(fill.shares), int(trip.entry_qty) - counted)
        return quantity if quantity >= 1 else UNPROVEN    # proven shares but nothing left: contradictory facts

    def _simulate(self, row: dict, cache: dict) -> tuple[SimResult, str]:
        schedule = self._calendar.resolve(dt.datetime.combine(row["session_date"], dt.time(12), tzinfo=ET))
        if schedule is None:
            return SimResult("INCOMPLETE", "NOT_A_SESSION", "NONE", None, None, None, None, None), "none"
        trade = SimInput(row["conid"], int(row["quantity"]), row["reference_price"], row["stop_price"],
                         row["target_price"], row["decided_at"], schedule.flatten_start_utc)
        start = row["decided_at"].astimezone(dt.timezone.utc).replace(second=0, microsecond=0) + MINUTE
        end = schedule.flatten_start_utc + FLATTEN_BAR_WINDOW
        if not self._sources:
            return SimResult("INCOMPLETE", "NO_BAR_SOURCE", "NONE", None, None, None, None, None), "none"
        reasons: list[str] = []
        for source in self._sources:
            key = (source.name, row["conid"], row["session_date"])
            try:
                if key not in cache:
                    cache[key] = source.bars(row["conid"], start, end)
                bars = cache[key]
            except BarSourceError as exc:
                reasons.append(f"{source.name}:{exc}")
                continue
            result = simulate_long_bracket(trade, bars) if bars else SimResult(
                "INCOMPLETE", "NO_BARS", "NONE", None, None, None, None, None)
            if result.status == "COMPLETE":
                return result, source.name
            reasons.append(f"{source.name}:{result.reason}")
        return SimResult("INCOMPLETE", "; ".join(reasons), "NONE", None, None, None, None, None), "none"

    def _write(self, row: dict, result: SimResult, source: str, now: dt.datetime) -> int:
        outcome = {
            "record_id": row["record_id"], "experiment_id": row["experiment_id"], "baseline_id": row["baseline_id"],
            "cohort": row["cohort"], "session_date": row["session_date"], "status": result.status,
            "reason": result.reason, "exit_kind": result.exit_kind, "exit_at": result.exit_at,
            "exit_price": result.exit_price, "pnl_usd": result.pnl_usd, "trades": result.trades,
            "quantity": int(row["quantity"]) if result.status == "COMPLETE" and result.trades else None,
            "bar_source": source, "bars_digest": result.bars_digest, "computed_at": now}
        try:
            self._store.insert_sealed("simulated_outcomes", outcome)
        except ScoreboardConflict:
            return 0                           # another tick wrote it first; sealed rows are final
        self._last_try.pop(row["record_id"], None)
        if result.status == "INCOMPLETE":
            logger.warning("simulated decision %s is INCOMPLETE: %s", row["record_id"], result.reason)
        return 1
