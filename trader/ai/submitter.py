"""Persist-before-send submission of ai paper decisions (SP2 spec 5.3, 9; Plan 5 Rulings 10-11).

The exact body and id are written before any byte leaves. A proven pre-send
failure keeps the command unsent until it expires. A possible send becomes
UNKNOWN and is reconciled by the same id under the current epoch; a permitted
retry resends the stored bytes, never a regenerated command. No logical
duplicate can exist, because the trader deduplicates by command id and body.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.ai.engine import ProposedDecision
from trader.ai.ids import canonical_json, command_id_for, derive_decision_id
from trader.ai.rpc_clients import EPOCH_REFUSALS, RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.store import to_utc

logger = logging.getLogger(__name__)

SOURCE_KINDS = ("entry_signal", "exit_signal", "entry_cycle", "position_cycle")
OPEN_STATES = ("PENDING", "SENDING", "UNKNOWN", "ACCEPTED")
UNSETTLED_STATES = ("PENDING", "SENDING", "UNKNOWN")
TRADER_FINAL_STATES = frozenset({"RESOLVED", "REJECTED"})
REFUSED_BEFORE_ANY_HANDLER = frozenset({"AUTHENTICATION_ERROR", "REPLAY_ERROR"})
BROKEN_REQUEST = frozenset({"VALIDATION_ERROR", "PERMISSION_DENIED", "METHOD_NOT_ALLOWED"})
SEND_MARGIN = dt.timedelta(seconds=5)
MAX_TTL = dt.timedelta(minutes=15)                 # the trader's MAX_EXPIRY_AHEAD
WAIT = "WAIT"
_COLUMNS = ("decision_id, command_id, source_kind, source_id, action_key, action, body_json, body_sha256, "
            "expires_at, created_epoch, state, attempts, last_epoch, last_sent_at, next_try_at, receipt_state, "
            "close_root_id, error_code")


@dataclass(frozen=True)
class Submission:
    decision_id: str
    command_id: str
    source_kind: str
    source_id: str
    action_key: str
    action: str
    body_json: str
    body_sha256: str
    expires_at: dt.datetime
    created_epoch: int
    state: str
    attempts: int
    last_epoch: Optional[int]
    last_sent_at: Optional[dt.datetime]
    next_try_at: dt.datetime
    receipt_state: Optional[str]
    close_root_id: Optional[str]
    error_code: Optional[str]


def _submission(row: tuple) -> Submission:
    values = list(row)
    for index in (8, 13, 14):
        values[index] = None if values[index] is None else to_utc(values[index])
    return Submission(*values)


class SubmissionConflict(Exception):
    """The same decision id was persisted with a different body: a bug, never resolved silently."""


def build_body(decision: ProposedDecision, *, decision_id: str, expires_at: dt.datetime) -> dict:
    return {"decision_id": decision_id, "deployment_digest": decision.deployment_digest,
            "decider": decision.decider, "action": decision.action, "conid": decision.conid,
            "side": decision.side, "stop_price": decision.stop_price, "target_price": decision.target_price,
            "quantity": decision.quantity, "policy_revision": decision.policy_revision,
            "evidence_digest": decision.evidence_digest,
            "deployment_version": decision.deployment_version, "source_digest": decision.source_digest,
            "expires_at": expires_at.astimezone(dt.timezone.utc).isoformat()}


class Submitter:
    def __init__(self, *, store: Any, supervisor: Any, leadership: Any, clock: Any, slots: Any,
                 experiment_state: Callable[[], Optional[str]], not_found_settle_seconds: int = 120):
        self._store, self._supervisor, self._leadership = store, supervisor, leadership
        self._clock, self._slots, self._experiment_state = clock, slots, experiment_state
        self._settle = dt.timedelta(seconds=not_found_settle_seconds)
        self._lock = asyncio.Lock()                # one send or reconcile step at a time in this process

    # -- persistence ---------------------------------------------------------------------------------
    def insert_in_tx(self, conn: Any, *, source_kind: str, source_id: str, decision: ProposedDecision,
                     expires_at: dt.datetime, epoch: int, now: dt.datetime) -> str:
        """Persist the exact body and id; the caller's transaction also records why (spec 5.3)."""
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"unknown source kind {source_kind!r}")
        if type(epoch) is not int or epoch < 1:
            raise ValueError("a decision is persisted under a held epoch")
        if not now < expires_at <= now + MAX_TTL:
            raise ValueError("expires_at must lie within 15 minutes ahead")
        decision_id = derive_decision_id(source_id, decision.action_key)
        body_json = canonical_json(build_body(decision, decision_id=decision_id, expires_at=expires_at))
        digest = hashlib.sha256(body_json.encode()).hexdigest()
        existing = conn.execute("SELECT body_sha256 FROM ai_submissions WHERE decision_id = ?",
                                [decision_id]).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise SubmissionConflict(decision_id)
            return decision_id
        conn.execute(
            "INSERT INTO ai_submissions (decision_id, command_id, source_kind, source_id, action_key, action, "
            "body_json, body_sha256, expires_at, created_epoch, state, attempts, next_try_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)",
            [decision_id, command_id_for(decision_id), source_kind, source_id, decision.action_key, decision.action,
             body_json, digest, expires_at, epoch, now, now, now])
        return decision_id

    async def get(self, decision_id: str) -> Optional[Submission]:
        row = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_submissions WHERE decision_id = ?",
                                       [decision_id], fetch="one")
        return None if row is None else _submission(row)

    async def unsettled_count(self) -> int:
        row = await self._store.aquery(
            "SELECT COUNT(*) FROM ai_submissions WHERE state IN ('PENDING', 'SENDING', 'UNKNOWN')", fetch="one")
        return int(row[0])

    async def _rows(self, where: str, params: list) -> list[Submission]:
        rows = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_submissions WHERE {where} ORDER BY created_at",
                                        params)
        return [_submission(row) for row in rows]

    async def _set(self, decision_id: str, **fields: Any) -> None:
        fields["updated_at"] = self._clock.now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._store.atransaction(lambda conn: conn.execute(
            f"UPDATE ai_submissions SET {assignments} WHERE decision_id = ?", [*fields.values(), decision_id]))

    # -- startup ------------------------------------------------------------------------------------
    async def recover(self) -> int:
        """A SENDING row may have reached the trader before the crash: it is UNKNOWN now."""
        now = self._clock.now()

        def work(conn: Any) -> int:
            count = conn.execute("SELECT COUNT(*) FROM ai_submissions WHERE state = 'SENDING'").fetchone()[0]
            conn.execute("UPDATE ai_submissions SET state = 'UNKNOWN', error_code = 'PROCESS_RESTARTED', "
                         "updated_at = ? WHERE state = 'SENDING'", [now])
            return int(count)
        return await self._store.atransaction(work)

    # -- sending --------------------------------------------------------------------------------------
    async def send_due(self) -> None:
        async with self._lock:
            for row in await self._rows("state = 'PENDING' AND next_try_at <= ?", [self._clock.now()]):
                await self._try_send(row, resend=False)

    def _gate(self, row: Submission, now: dt.datetime, *, resend: bool) -> Optional[str]:
        """None to send now, WAIT, or the code that abandons a never-sent command."""
        if now >= row.expires_at - SEND_MARGIN:
            return WAIT if resend else "EXPIRED_UNSENT"
        state = self._experiment_state()
        if row.action == "ENTER":
            if not self._slots.entry_window_open(now):
                return WAIT if resend else "OUTSIDE_ENTRY_WINDOW"
            return None if state == "ARMED" else WAIT
        if state == "STOPPED":
            return WAIT if resend else "EXPERIMENT_STOPPED"
        return None if state is not None else WAIT

    async def _try_send(self, row: Submission, *, resend: bool) -> None:
        now = self._clock.now()
        verdict = self._gate(row, now, resend=resend)
        if verdict == WAIT:
            return
        if verdict is not None:
            await self._set(row.decision_id, state="ABANDONED", error_code=verdict)
            return
        epoch = self._leadership.current_epoch()          # re-checked right before the send (spec 5.3)
        if epoch is None:
            return
        await self._set(row.decision_id, state="SENDING", attempts=row.attempts + 1, last_epoch=epoch,
                        last_sent_at=now)
        not_sent_state = "UNKNOWN" if resend else "PENDING"   # an earlier send of a resend may have landed
        try:
            reply = await self._supervisor.call("submit_ai_paper_decision", json.loads(row.body_json), epoch=epoch)
        except RpcNotSent as exc:
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code,
                            next_try_at=now + _backoff(row.attempts + 1))
            return
        except RpcOutcomeUnknown as exc:
            await self._set(row.decision_id, state="UNKNOWN", error_code=exc.code)
            return
        except RpcRefused as exc:
            await self._refused(row, exc, not_sent_state, now)
            return
        await self._apply_receipt(row.decision_id, reply, None)

    async def _refused(self, row: Submission, exc: RpcRefused, not_sent_state: str, now: dt.datetime) -> None:
        if exc.code in EPOCH_REFUSALS:                     # Plan 1 Ruling 1a: refused before any ledger row
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code)
            await self._leadership.on_stale(exc.code)
        elif exc.code in REFUSED_BEFORE_ANY_HANDLER:
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code,
                            next_try_at=now + _backoff(row.attempts + 1))
        elif exc.code in BROKEN_REQUEST:
            logger.error("ai decision %s refused as a broken request: %s", row.decision_id, exc.code)
            await self._set(row.decision_id, state="UNKNOWN" if not_sent_state == "UNKNOWN" else "FAILED",
                            error_code=exc.code)
        else:                                              # the trader may have started on it
            await self._set(row.decision_id, state="UNKNOWN", error_code=exc.code)

    async def _apply_receipt(self, decision_id: str, receipt: Any, close_root_id: Optional[str]) -> None:
        state = receipt.get("state") if isinstance(receipt, dict) else None
        if not isinstance(state, str):
            logger.error("ai decision %s: receipt without a state; kept UNKNOWN", decision_id)
            await self._set(decision_id, state="UNKNOWN", error_code="RECEIPT_MALFORMED")
            return
        error_code = receipt.get("error_code")
        await self._set(decision_id, state="FINAL" if state in TRADER_FINAL_STATES else "ACCEPTED",
                        receipt_json=canonical_json(receipt), receipt_state=state, error_code=error_code,
                        close_root_id=close_root_id)
        if error_code in EPOCH_REFUSALS:                   # takeover during admission (Plan 1 Ruling 4): lost
            await self._leadership.on_stale(error_code)

    # -- reconciliation -------------------------------------------------------------------------------
    async def reconcile_once(self) -> None:
        """Runs whatever the budget and the model do: it needs only the trader and the epoch."""
        async with self._lock:
            for row in await self._rows("state IN ('UNKNOWN', 'ACCEPTED')", []):
                epoch = self._leadership.current_epoch()
                if epoch is None:
                    return
                try:
                    view = await self._supervisor.call("get_ai_paper_decision", {"decision_id": row.decision_id},
                                                       epoch=epoch)
                except RpcRefused as exc:
                    if exc.code in EPOCH_REFUSALS:
                        await self._leadership.on_stale(exc.code)
                        return
                    logger.error("reconcile read of %s refused: %s", row.decision_id, exc.code)
                    continue
                except (RpcNotSent, RpcOutcomeUnknown):
                    return                                 # trader away: the next pass tries again
                if view.get("found") is True:
                    await self._apply_receipt(row.decision_id, view.get("receipt"), view.get("close_root_id"))
                elif row.state == "UNKNOWN":
                    await self._unknown_not_found(row)
                else:
                    logger.error("accepted ai decision %s has no trader row", row.decision_id)

    async def _unknown_not_found(self, row: Submission) -> None:
        now = self._clock.now()
        if row.last_sent_at is not None and now < row.last_sent_at + self._settle:
            return                                         # a late request may still be handled (30 s skew)
        if now < row.expires_at - SEND_MARGIN:
            await self._try_send(row, resend=True)         # same id, same stored bytes
        else:
            await self._set(row.decision_id, state="NOT_ADMITTED", error_code="NOT_FOUND_AFTER_EXPIRY")


def _backoff(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(2 ** attempts, 30))
