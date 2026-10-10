"""Durable intake of the trader's strategy signal record (SP2 spec 5.5, amendment 6.1; Plan 5 Ruling 9).

New opportunities and the consumer cursor commit in one transaction, so a crash
never skips a signal; the source_event_id makes a redelivery a no-op. A pruned
range or a reset record is written down as a coverage gap. The missing signals
are never reconstructed or claimed as identified.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from typing import Any, Optional

from trader.ai.engine import SignalOpportunity, parse_aware
from trader.ai.rpc_clients import RpcRefused
from trader.ai.runtime_schema import cursor_generation_in_tx, cursor_value_in_tx, set_cursor_in_tx
from trader.ai.store import to_utc
from trader.bar_size import BarSize

logger = logging.getLogger(__name__)

SIGNAL_CURSOR = "signals"
SOURCE_EVENT_ID = re.compile(r"^sig-[0-9a-f]{32}$")
RECORD_GENERATION = re.compile(r"^gen-[0-9a-f]{32}$")
STALE = "STALE"
BAR_SIZE_UNKNOWN = "STALE_BAR_SIZE_UNKNOWN"
_STORED = ("opportunity_id, signal_cursor, strategy_name, conid, action, probability, signal_time, recorded_at, "
           "deployment_digest, deployment_version, source_digest, bar_size")
_COLUMNS = f"{_STORED}, state"


class SignalIntakeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SignalIntakeError("PAGE_MALFORMED", f"{name} must be a non-negative integer")
    return value


def _parse_signal(raw: Any) -> SignalOpportunity:
    if not isinstance(raw, dict):
        raise SignalIntakeError("SIGNAL_MALFORMED", "a signal must be an object")
    source = raw.get("source_event_id")
    if not isinstance(source, str) or not SOURCE_EVENT_ID.fullmatch(source):
        raise SignalIntakeError("SIGNAL_MALFORMED", "source_event_id")
    if raw.get("action") not in ("BUY", "SELL"):
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: action")
    conid, probability = raw.get("conid"), raw.get("probability")
    if type(conid) is not int or conid <= 0:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: conid")
    if probability is not None and (type(probability) is not float or not math.isfinite(probability)):
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: probability")
    if not isinstance(raw.get("strategy_name"), str) or not raw["strategy_name"]:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: strategy_name")
    try:
        signal_time = parse_aware(raw.get("signal_time"), "signal_time")
        recorded_at = parse_aware(raw.get("recorded_at"), "recorded_at")
    except ValueError as exc:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: {exc}") from None
    bar_size = raw.get("bar_size")
    if bar_size is not None and bar_size not in BarSize.bar_sizes():
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: bar_size")
    binding = {name: raw.get(name) for name in ("deployment_digest", "deployment_version", "source_digest")}
    try:
        return SignalOpportunity(source, _nonnegative_int(raw.get("cursor"), "cursor"), raw["strategy_name"], conid,
                                 raw["action"], probability, signal_time, recorded_at, **binding, bar_size=bar_size)
    except ValueError as exc:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: {exc}") from None


def _opportunity(row: tuple) -> tuple[SignalOpportunity, str]:
    values = list(row)
    return SignalOpportunity(values[0], int(values[1]), values[2], int(values[3]), values[4], values[5],
                             to_utc(values[6]), to_utc(values[7]), values[8], values[9], values[10],
                             values[11]), values[12]


class SignalIntake:
    def __init__(self, *, store: Any, supervisor: Any, clock: Any, page_limit: int = 100, max_age_seconds: int = 300):
        self._store, self._supervisor, self._clock = store, supervisor, clock
        self._limit = page_limit
        self._max_age = dt.timedelta(seconds=max_age_seconds)

    async def poll(self) -> list[str]:
        """One page from the trader. Returns the ids of new opportunities, in cursor order."""
        cursor, known_generation = await self._store.atransaction(lambda conn: (
            cursor_value_in_tx(conn, SIGNAL_CURSOR), cursor_generation_in_tx(conn, SIGNAL_CURSOR)))
        try:
            page = await self._supervisor.call("read_ai_signals", {"after_cursor": cursor, "limit": self._limit})
        except RpcRefused as exc:
            if exc.code != "SIGNAL_CURSOR_AHEAD":
                raise
            await self._record_reset(cursor)
            return []
        if not isinstance(page, dict) or type(page.get("gap")) is not bool or not isinstance(page.get("signals"), list):
            raise SignalIntakeError("PAGE_MALFORMED", "read_ai_signals reply has the wrong shape")
        signals = [_parse_signal(raw) for raw in page["signals"]]        # any bad signal: nothing is committed
        next_cursor = _nonnegative_int(page.get("next_cursor"), "next_cursor")
        oldest = _nonnegative_int(page.get("oldest_retained_cursor"), "oldest_retained_cursor")
        if next_cursor < cursor:
            raise SignalIntakeError("PAGE_MALFORMED", "next_cursor moved backwards")
        generation = page.get("record_generation")
        if not isinstance(generation, str) or not RECORD_GENERATION.fullmatch(generation):
            raise SignalIntakeError("PAGE_MALFORMED", "record_generation must look like gen-<32 hex>")
        if known_generation is not None and generation != known_generation:
            await self._record_replacement(cursor, generation)
            return []
        now = self._clock.now()

        def work(conn: Any) -> list[str]:
            if page["gap"]:
                conn.execute("INSERT INTO ai_coverage_gaps VALUES (?, 'RETENTION', ?, ?, ?) "
                             "ON CONFLICT (gap_id) DO NOTHING", [f"gap-r-{cursor}-{oldest}", cursor, oldest, now])
            new = []
            for s in signals:
                if conn.execute("SELECT 1 FROM ai_opportunities WHERE opportunity_id = ?",
                                [s.opportunity_id]).fetchone():
                    continue                                              # redelivered: not a new opportunity
                conn.execute(f"INSERT INTO ai_opportunities ({_STORED}, state, reason, created_at, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', NULL, ?, ?)",
                             [s.opportunity_id, s.signal_cursor, s.strategy_name, s.conid, s.action, s.probability,
                              s.signal_time, s.recorded_at, s.deployment_digest, s.deployment_version,
                              s.source_digest, s.bar_size, now, now])
                new.append(s.opportunity_id)
            set_cursor_in_tx(conn, SIGNAL_CURSOR, next_cursor, now, generation)
            return new
        new = await self._store.atransaction(work)
        if page["gap"]:
            logger.warning("signal coverage gap: signals after cursor %s and before %s were removed by retention",
                           cursor, oldest)
        return new

    async def _record_replacement(self, cursor: int, generation: str) -> None:
        """Another record answers, even at the same cursor (PR #84 thread 4210304622): what the old one held
        after ``cursor`` is unknown. Write the gap and read the new record from its beginning."""
        now = self._clock.now()
        logger.error("the trader's signal record was replaced (cursor %s); restarting from 0", cursor)

        def work(conn: Any) -> None:
            conn.execute("INSERT INTO ai_coverage_gaps VALUES (?, 'GENERATION_CHANGED', ?, 0, ?) "
                         "ON CONFLICT (gap_id) DO NOTHING", [f"gap-g-{cursor}-{generation}", cursor, now])
            set_cursor_in_tx(conn, SIGNAL_CURSOR, 0, now, generation)
        await self._store.atransaction(work)

    async def _record_reset(self, cursor: int) -> None:
        now = self._clock.now()
        logger.error("the trader's signal record is behind cursor %s (reset); restarting from 0", cursor)

        def work(conn: Any) -> None:
            conn.execute("INSERT INTO ai_coverage_gaps VALUES (?, 'CURSOR_AHEAD', ?, 0, ?) "
                         "ON CONFLICT (gap_id) DO NOTHING", [f"gap-a-{cursor}-{now:%Y%m%dT%H%M%S}", cursor, now])
            set_cursor_in_tx(conn, SIGNAL_CURSOR, 0, now)
        await self._store.atransaction(work)

    def stale_reason(self, opportunity: SignalOpportunity, now: dt.datetime) -> Optional[str]:
        """None while fresh. signal_time is the start of the signal's bar, so the age counts from the bar's close
        (issue #146). A signal whose bar close is unknown is never fresh."""
        try:
            bar_length = BarSize.intraday_length(opportunity.bar_size)
        except ValueError:
            logger.error("%s: signal %s from %s has bar size %r, so its bar close is unknown; it is not judged",
                         BAR_SIZE_UNKNOWN, opportunity.opportunity_id, opportunity.strategy_name,
                         opportunity.bar_size)
            return BAR_SIZE_UNKNOWN
        return None if now - (opportunity.signal_time + bar_length) <= self._max_age else STALE

    async def expire_stale(self) -> list[str]:
        now = self._clock.now()
        stale = []
        for opportunity, state in await self.open_opportunities():
            reason = self.stale_reason(opportunity, now) if state == "NEW" else None
            if reason is not None:
                await self.mark(opportunity.opportunity_id, "MISSED", reason)
                stale.append(opportunity.opportunity_id)
        return stale

    async def open_opportunities(self) -> list[tuple[SignalOpportunity, str]]:
        rows = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_opportunities "
                                        "WHERE state IN ('NEW', 'IN_PROGRESS') ORDER BY signal_cursor")
        return [_opportunity(row) for row in rows]

    async def mark(self, opportunity_id: str, state: str, reason: Any) -> None:
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_opportunities SET state = ?, reason = ?, updated_at = ? WHERE opportunity_id = ?",
            [state, reason, now, opportunity_id]))

    async def wait(self, opportunity_id: str, until: dt.datetime, reason: str) -> None:
        """Not decidable yet: stays IN_PROGRESS and is judged again until ``until`` (an exit waiting for its entry)."""
        now = self._clock.now()

        def work(conn: Any) -> None:
            conn.execute("INSERT INTO ai_exit_waits (opportunity_id, wait_until, reason, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?) ON CONFLICT (opportunity_id) DO UPDATE "
                         "SET wait_until = excluded.wait_until, reason = excluded.reason, "
                         "updated_at = excluded.updated_at", [opportunity_id, until, reason, now, now])
            conn.execute("UPDATE ai_opportunities SET state = 'IN_PROGRESS', reason = ?, updated_at = ? "
                         "WHERE opportunity_id = ?", [reason, now, opportunity_id])
        await self._store.atransaction(work)

    async def reopen(self, opportunity_id: str, refused_decision_id: str, until: dt.datetime, reason: str) -> None:
        """A decided exit whose CLOSE the trader refused because nothing was held yet waits again (once per
        refused decision; PR #86 4212667433)."""
        now = self._clock.now()

        def work(conn: Any) -> None:
            conn.execute("INSERT INTO ai_exit_waits (opportunity_id, wait_until, reason, created_at, updated_at, "
                         "reopened_for) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (opportunity_id) DO UPDATE "
                         "SET wait_until = excluded.wait_until, reason = excluded.reason, "
                         "updated_at = excluded.updated_at, reopened_for = excluded.reopened_for",
                         [opportunity_id, until, reason, now, now, refused_decision_id])
            conn.execute("UPDATE ai_opportunities SET state = 'IN_PROGRESS', reason = ?, updated_at = ? "
                         "WHERE opportunity_id = ?", [reason, now, opportunity_id])
        await self._store.atransaction(work)

    async def waits(self) -> dict[str, tuple[dt.datetime, Optional[dt.datetime]]]:
        """Open exit waits: opportunity id -> (wait_until, last incident alert or None)."""
        rows = await self._store.aquery("SELECT w.opportunity_id, w.wait_until, w.last_alert_at FROM ai_exit_waits w "
                                        "JOIN ai_opportunities o ON o.opportunity_id = w.opportunity_id "
                                        "WHERE o.state IN ('NEW', 'IN_PROGRESS')")
        return {row[0]: (to_utc(row[1]), None if row[2] is None else to_utc(row[2])) for row in rows}

    async def record_wait_alert(self, opportunity_id: str) -> None:
        """An incident on a wait past its backstop; the wait itself stays open (PR #86 4212341131)."""
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_exit_waits SET alerts = alerts + 1, last_alert_at = ? WHERE opportunity_id = ?",
            [now, opportunity_id]))

    def finish_in_tx(self, conn: Any, opportunity_id: str, ok: bool, reason: Any) -> None:
        conn.execute("UPDATE ai_opportunities SET state = ?, reason = ?, updated_at = ? WHERE opportunity_id = ?",
                     ["DECIDED" if ok else "FAILED", reason, self._clock.now(), opportunity_id])
