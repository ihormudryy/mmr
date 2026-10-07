"""Idempotent delivery of model costs and baseline records to the trader (SP2 spec 7; Plan 5 Rulings 12-14).

Cost events come from Plan 4's journal through a cursor; rows and the cursor
commit together. Every record has a stable id, so a retry after a lost
acknowledgement is a DUPLICATE on the trader, never a second row.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from trader.ai.engine import MATCHED_ENTRY_BASELINE, SimulatedBaseline
from trader.ai.ids import attempt_ref, canonical_json, cost_record_id, simulated_record_id
from trader.ai.journal import COST_CONFIRMED, COST_CORRECTION, COST_ESTIMATED_UNKNOWN, COST_NONE
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.runtime_schema import cursor_value_in_tx, set_cursor_in_tx

logger = logging.getLogger(__name__)

COST_CURSOR = "cost_events"
COST_STATUS = {COST_CONFIRMED: "confirmed", COST_ESTIMATED_UNKNOWN: "estimated", COST_NONE: "confirmed",
               COST_CORRECTION: "confirmed"}
METHOD_BY_KIND = {"cost": "record_ai_cost", "simulated": "record_simulated_decision"}
DELIVERED = frozenset({"INSERTED", "DUPLICATE"})
DEAD_CODES = frozenset({"VALIDATION_ERROR", "PERMISSION_DENIED", "METHOD_NOT_ALLOWED"})
ADMITTED_SUBMISSIONS = ("ACCEPTED", "FINAL")
NEVER_ADMITTED_SUBMISSIONS = ("ABANDONED", "NOT_ADMITTED", "FAILED")
MAX_BACKOFF_SECONDS = 300


def micros_to_usd(micros: int) -> float:
    """Plan 4 money is integer micro-USD; Plan 2 takes a float USD."""
    if type(micros) is not int or micros < 0:
        raise ValueError("cost must be a non-negative integer of micro-USD")
    return float(Decimal(micros) / Decimal(1_000_000))


@dataclass(frozen=True)
class CallContext:
    context_key: str
    experiment_id: str
    served_kind: str
    served_id: str


def register_context_in_tx(conn: Any, *, context_key: str, experiment_id: str, served_kind: str, served_id: str,
                           now: dt.datetime) -> None:
    """Written before any model call of this context, so every cost event can be attributed."""
    conn.execute("INSERT INTO ai_call_contexts VALUES (?, ?, ?, ?, ?) ON CONFLICT (context_key) DO NOTHING",
                 [context_key, experiment_id, served_kind, served_id, now])


def cost_body(event: Any, attempt: Any, context: CallContext) -> dict:
    status = COST_STATUS[event.kind]
    corrects = (cost_record_id(f"{event.attempt_key}:{COST_ESTIMATED_UNKNOWN}")
                if event.kind == COST_CORRECTION else None)
    return {"record_id": cost_record_id(event.event_id), "experiment_id": context.experiment_id,
            "role": event.role, "provider": event.backend, "model": event.model,
            "attempt_id": attempt_ref(event.attempt_key), "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens, "cost_usd": micros_to_usd(event.cost_micros),
            "cost_status": status, "called_at": attempt.started_at.isoformat(),
            "served_kind": context.served_kind, "served_id": context.served_id, "decision_id": None,
            "corrects_record_id": corrects}


def simulated_body(experiment_id: str, baseline: SimulatedBaseline) -> dict:
    return {"record_id": simulated_record_id(experiment_id, baseline.baseline_id, baseline.opportunity_id),
            "experiment_id": experiment_id, "baseline_id": baseline.baseline_id, "cohort": baseline.cohort,
            "opportunity_id": baseline.opportunity_id, "conid": baseline.conid, "side": baseline.side,
            "quantity": baseline.quantity, "reference_price": baseline.reference_price,
            "stop_price": baseline.stop_price, "target_price": baseline.target_price,
            "decided_at": baseline.decided_at.astimezone(dt.timezone.utc).isoformat(),
            "linked_decision_id": baseline.linked_decision_id,
            "linked_round_trip_id": baseline.linked_round_trip_id,
            "deployment_digest": baseline.deployment_digest, "incomplete_reason": baseline.incomplete_reason}


class ReportingOutbox:
    def __init__(self, *, store: Any, journal: Any, supervisor: Any, clock: Any):
        self._store, self._journal, self._supervisor, self._clock = store, journal, supervisor, clock

    # -- costs ------------------------------------------------------------------------------------------
    async def pump_costs(self, limit: int = 100) -> int:
        cursor = await self._store.atransaction(lambda conn: cursor_value_in_tx(conn, COST_CURSOR))
        # AiStore serializes every write, so event_seq order is commit order and the cursor skips nothing.
        events = await self._journal.acost_events_after(cursor, limit)
        if not events:
            return 0
        prepared = []
        for event in events:
            attempt = await self._journal.aget(event.attempt_key)
            context = None if attempt is None else await self._context(attempt.request_key.split("/", 1)[0])
            prepared.append((event, attempt, context))
        now = self._clock.now()

        def work(conn: Any) -> None:
            for event, attempt, context in prepared:
                if context is None:
                    logger.error("cost event %s has no call context; dead-lettered", event.event_id)
                    body, state, code = {"event_id": event.event_id}, "DEAD", "CONTEXT_MISSING"
                else:
                    body, state, code = cost_body(event, attempt, context), "PENDING", None
                conn.execute(
                    "INSERT INTO ai_outbox (record_id, kind, source_ref, attempt_key, body_json, state, attempts, "
                    "next_try_at, last_code, created_at) VALUES (?, 'cost', ?, ?, ?, ?, 0, ?, ?, ?) "
                    "ON CONFLICT (record_id) DO NOTHING",
                    [cost_record_id(event.event_id), event.event_id, event.attempt_key, canonical_json(body), state,
                     now, code, now])
            set_cursor_in_tx(conn, COST_CURSOR, events[-1].event_seq, now)
        await self._store.atransaction(work)
        return len(events)

    async def _context(self, context_key: str) -> Optional[CallContext]:
        row = await self._store.aquery("SELECT context_key, experiment_id, served_kind, served_id "
                                       "FROM ai_call_contexts WHERE context_key = ?", [context_key], fetch="one")
        return None if row is None else CallContext(*row)

    # -- baselines --------------------------------------------------------------------------------------
    def enqueue_simulated_in_tx(self, conn: Any, *, experiment_id: str, baseline: SimulatedBaseline,
                                wait_for_decision_id: Optional[str], now: dt.datetime) -> str:
        body = simulated_body(experiment_id, baseline)
        conn.execute(
            "INSERT INTO ai_outbox (record_id, kind, source_ref, body_json, state, wait_for_decision_id, attempts, "
            "next_try_at, created_at) VALUES (?, 'simulated', ?, ?, ?, ?, 0, ?, ?) ON CONFLICT (record_id) DO NOTHING",
            [body["record_id"], f"{baseline.baseline_id}|{baseline.opportunity_id}", canonical_json(body),
             "WAITING" if wait_for_decision_id else "PENDING", wait_for_decision_id, now, now])
        return body["record_id"]

    async def release_waiting(self) -> None:
        """A linked baseline goes out once its decision is settled: linked if the trader has it, else null."""
        def work(conn: Any) -> None:
            rows = conn.execute(
                "SELECT o.record_id, o.body_json, o.wait_for_decision_id, s.state FROM ai_outbox o "
                "LEFT JOIN ai_submissions s ON s.decision_id = o.wait_for_decision_id "
                "WHERE o.state = 'WAITING'").fetchall()
            for record_id, body_json, decision_id, submission_state in rows:
                body = json.loads(body_json)
                own_close = body["baseline_id"] == MATCHED_ENTRY_BASELINE      # waits on its close, keeps its ENTER
                if submission_state in ADMITTED_SUBMISSIONS:
                    link = body["linked_decision_id"] if own_close else decision_id
                elif submission_state is None or submission_state in NEVER_ADMITTED_SUBMISSIONS:
                    if own_close:
                        logger.info("matched-entry %s dropped: its close never reached the trader", record_id)
                        conn.execute("UPDATE ai_outbox SET state = 'DROPPED' WHERE record_id = ?", [record_id])
                        continue
                    link = None
                else:
                    continue
                body["linked_decision_id"] = link
                conn.execute("UPDATE ai_outbox SET body_json = ?, state = 'PENDING' WHERE record_id = ?",
                             [canonical_json(body), record_id])
        await self._store.atransaction(work)

    # -- delivery ---------------------------------------------------------------------------------------
    async def deliver_due(self, limit: int = 50) -> int:
        await self.release_waiting()
        rows = await self._store.aquery(
            "SELECT record_id, kind, body_json, attempts FROM ai_outbox WHERE state = 'PENDING' AND next_try_at <= ? "
            "ORDER BY created_seq LIMIT ?", [self._clock.now(), limit])
        delivered = 0
        for record_id, kind, body_json, attempts in rows:
            try:
                reply = await self._supervisor.call(METHOD_BY_KIND[kind], json.loads(body_json))
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                await self._retry(record_id, attempts, exc.code)
                break                                  # the trader is away: keep the order, try later
            except RpcRefused as exc:
                await (self._dead(record_id, exc.code) if exc.code in DEAD_CODES
                       else self._retry(record_id, attempts, exc.code))
                continue
            status = reply.get("status") if isinstance(reply, dict) else None
            if status in DELIVERED:
                await self._mark(record_id, state="DELIVERED", delivered_status=status,
                                 delivered_at=self._clock.now())
                delivered += 1
            elif status == "REFUSED" and reply.get("retryable") is True:
                await self._retry(record_id, attempts, reply.get("code") or "REFUSED")
            else:
                await self._dead(record_id, (reply or {}).get("code") or "REPLY_MALFORMED")
        return delivered

    async def counts(self) -> dict:
        rows = await self._store.aquery("SELECT state, COUNT(*) FROM ai_outbox GROUP BY state")
        found = {state: int(count) for state, count in rows}
        return {"waiting": found.get("WAITING", 0), "pending": found.get("PENDING", 0),
                "dropped": found.get("DROPPED", 0),
                "delivered": found.get("DELIVERED", 0), "dead": found.get("DEAD", 0)}

    async def _retry(self, record_id: str, attempts: int, code: str) -> None:
        delay = min(5 * 2 ** attempts, MAX_BACKOFF_SECONDS)
        await self._mark(record_id, attempts=attempts + 1, last_code=code,
                         next_try_at=self._clock.now() + dt.timedelta(seconds=delay))

    async def _dead(self, record_id: str, code: str) -> None:
        logger.error("outbox record %s dead-lettered: %s", record_id, code)
        await self._mark(record_id, state="DEAD", last_code=code)

    async def _mark(self, record_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._store.atransaction(lambda conn: conn.execute(
            f"UPDATE ai_outbox SET {assignments} WHERE record_id = ?", [*fields.values(), record_id]))
