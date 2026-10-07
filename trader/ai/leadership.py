"""Controller leadership: one trader-granted epoch per process (SP2 spec 5.1, Plan 5 Rulings 4-5).

The trader decides who leads (Plan 1). This side only asks, persists what it
got before using it, and stops using it at once when the trader says another
holder leads, when a command is refused as stale, or when its own conservative
deadline passes without a renewal.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused

logger = logging.getLogger(__name__)

LEASE_SAFETY_SECONDS = 5.0
LOST_TO_ANOTHER = frozenset({"CONTROLLER_EPOCH_HELD", "CONTROLLER_EPOCH_UNKNOWN"})


def new_holder_id() -> str:
    """Fresh per process: a restart is a new holder and waits for the old lease (Plan 1 Ruling 3)."""
    return f"ai-{uuid.uuid4().hex[:12]}"


class NotLeader(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HeldEpoch:
    epoch: int
    lease_expires_at: dt.datetime          # the trader's clock; audit only
    local_deadline: float                  # our monotonic clock; the epoch is used only before it


def _parse_grant(reply: Any) -> tuple[int, dt.datetime]:
    epoch = reply.get("epoch") if isinstance(reply, dict) else None
    text = reply.get("lease_expires_at") if isinstance(reply, dict) else None
    if type(epoch) is not int or epoch < 1 or not isinstance(text, str):
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED")
    try:
        expires = dt.datetime.fromisoformat(text)
    except ValueError:
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED") from None
    if expires.utcoffset() is None:
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED")
    return epoch, expires.astimezone(dt.timezone.utc)


class Leadership:
    def __init__(self, *, supervisor: Any, store: Any, clock: Any, holder_id: str, lease_seconds: int = 60,
                 renew_seconds: float = 20, held_retry_seconds: float = 5.0):
        from trader.automation.controller_epoch import HOLDER_ID
        if not HOLDER_ID.fullmatch(holder_id):
            raise ValueError("holder_id must match the trader's HOLDER_ID pattern")
        self._supervisor, self._store, self._clock = supervisor, store, clock
        self._holder_id = holder_id
        self._lease_seconds, self._renew_seconds, self._held_retry = lease_seconds, renew_seconds, held_retry_seconds
        self._held: Optional[HeldEpoch] = None
        self._last_epoch: Optional[int] = None

    @property
    def holder_id(self) -> str:
        return self._holder_id

    @property
    def last_epoch(self) -> Optional[int]:
        return self._last_epoch

    def current_epoch(self) -> Optional[int]:
        """The epoch to send now, or None. Callers check it right before every epoch call."""
        held = self._held
        if held is None or self._clock.monotonic() >= held.local_deadline:
            return None
        return held.epoch

    async def grant_once(self) -> int:
        started = self._clock.monotonic()          # before the request: the trader's lease starts later
        body = {"holder_id": self._holder_id, "current_epoch": self._last_epoch,
                "lease_seconds": self._lease_seconds}
        try:
            reply = await self._supervisor.call("grant_ai_controller_epoch", body)
        except RpcRefused as exc:
            if exc.code not in LOST_TO_ANOTHER:
                raise                              # PERMISSION_DENIED and friends: a wrong key, fail loudly
            if exc.code == "CONTROLLER_EPOCH_UNKNOWN":
                self._last_epoch = None            # this trader never granted it (reset journal): ask fresh
            await self.mark_lost(exc.code)
            raise NotLeader(exc.code) from None
        epoch, expires = _parse_grant(reply)
        await self._persist(epoch, expires)
        self._last_epoch = epoch
        self._held = HeldEpoch(epoch, expires, started + self._lease_seconds - LEASE_SAFETY_SECONDS)
        return epoch

    async def _persist(self, epoch: int, expires: dt.datetime) -> None:
        now, previous, holder = self._clock.now(), self._last_epoch, self._holder_id

        def work(conn: Any) -> None:
            if previous is not None and previous != epoch:
                conn.execute("UPDATE ai_held_epochs SET lost_at = ?, lost_reason = 'SUPERSEDED' "
                             "WHERE epoch = ? AND lost_at IS NULL", [now, previous])
            if conn.execute("SELECT 1 FROM ai_held_epochs WHERE epoch = ?", [epoch]).fetchone():
                conn.execute("UPDATE ai_held_epochs SET holder_id = ?, renewed_at = ?, lease_expires_at = ?, "
                             "lost_at = NULL, lost_reason = NULL WHERE epoch = ?", [holder, now, expires, epoch])
            else:
                conn.execute("INSERT INTO ai_held_epochs VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                             [epoch, holder, now, now, expires])
        await self._store.atransaction(work)

    async def mark_lost(self, reason: str) -> None:
        held, self._held = self._held, None        # stop using it before anything else happens
        if held is None:
            return
        logger.warning("controller epoch %s lost: %s", held.epoch, reason)
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_held_epochs SET lost_at = ?, lost_reason = ? WHERE epoch = ? AND lost_at IS NULL",
            [now, reason, held.epoch]))

    async def on_stale(self, code: str) -> None:
        """The trader refused this process's epoch on a command or read."""
        await self.mark_lost(code)

    async def acquire(self, stop: Optional[asyncio.Event] = None) -> Optional[int]:
        """Ask until granted; a restarted process waits here up to one lease. None when stopped."""
        while stop is None or not stop.is_set():
            try:
                return await self.grant_once()
            except NotLeader:
                pass
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                logger.warning("controller epoch grant failed (%s); retrying", exc.code)
            await self._clock.sleep(self._held_retry)
        return None

    async def run_renewals(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            held = self._held
            if held is not None and self._clock.monotonic() >= held.local_deadline:
                await self.mark_lost("LEASE_EXPIRED_LOCALLY")
            await self._clock.sleep(self._renew_seconds if self.current_epoch() is not None else self._held_retry)
            try:
                await self.grant_once()
            except NotLeader:
                continue
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                logger.warning("controller epoch renewal failed (%s); the epoch stays until its local deadline",
                               exc.code)
