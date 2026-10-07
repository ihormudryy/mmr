"""The trader-granted controller epoch and its lease (SP2 spec 5.1, amendment 6.2).

One row per granted epoch; the newest row is the current epoch. A grant runs
inside ``DomainJournal.mutate_batch_work``, the same serialized write
transaction the ai_paper decision claim uses, so a grant and a claim never
interleave. "Current" means equal to the newest epoch: only a successor's
grant makes an epoch stale, an expired lease alone does not (Plan 1 Ruling 2).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

CONTROLLER_EPOCH_MIGRATION_VERSION = 90
EPOCH_MISSING = "CONTROLLER_EPOCH_MISSING"
EPOCH_STALE = "CONTROLLER_EPOCH_STALE"
EPOCH_HELD = "CONTROLLER_EPOCH_HELD"
EPOCH_UNKNOWN = "CONTROLLER_EPOCH_UNKNOWN"
MIN_LEASE_SECONDS = 10
MAX_LEASE_SECONDS = 600
MAX_CONTROLLER_EPOCH = 2**53
HOLDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def apply_controller_epoch_migration(migrator: Any) -> bool:
    return migrator.apply(CONTROLLER_EPOCH_MIGRATION_VERSION, "sp2_ai_controller_epochs", (
        """CREATE TABLE IF NOT EXISTS ai_controller_epochs (
            epoch BIGINT PRIMARY KEY, holder_id VARCHAR NOT NULL, granted_at TIMESTAMPTZ NOT NULL,
            renewed_at TIMESTAMPTZ NOT NULL, lease_expires_at TIMESTAMPTZ NOT NULL)""",
    ))


class EpochRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EpochGrant:
    epoch: int
    holder_id: str
    lease_expires_at: dt.datetime
    renewed: bool


@dataclass(frozen=True)
class _Latest:
    epoch: int
    holder_id: str
    lease_expires_at: dt.datetime


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("controller epoch times must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def is_epoch_number(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_CONTROLLER_EPOCH


def _check_grant_input(holder_id: object, current_epoch: object, lease_seconds: object) -> None:
    if not isinstance(holder_id, str) or not HOLDER_ID.fullmatch(holder_id):
        raise ValueError("holder_id must match ^[a-z0-9][a-z0-9_.-]{0,63}$")
    if current_epoch is not None and not is_epoch_number(current_epoch):
        raise ValueError("current_epoch must be null or an integer >= 1")
    if type(lease_seconds) is not int or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be an integer in {MIN_LEASE_SECONDS}..{MAX_LEASE_SECONDS}")


def _latest_in_tx(conn: Any) -> Optional[_Latest]:
    row = conn.execute(
        "SELECT epoch, holder_id, lease_expires_at FROM ai_controller_epochs ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _Latest(int(row[0]), row[1], _as_utc(row[2]))


class ControllerEpochs:
    def __init__(self, *, journal: Any, now: Callable[[], dt.datetime]):
        self._journal = journal
        self._now = now

    def grant(self, *, holder_id: str, current_epoch: Optional[int], lease_seconds: int) -> EpochGrant:
        _check_grant_input(holder_id, current_epoch, lease_seconds)
        now = _as_utc(self._now())
        return self._journal.mutate_batch_work(
            self._journal.connect(),
            lambda conn, _append: self._grant_in_tx(conn, holder_id, current_epoch, lease_seconds, now))

    def _grant_in_tx(self, conn: Any, holder_id: str, current_epoch: Optional[int], lease_seconds: int,
                     now: dt.datetime) -> EpochGrant:
        latest = _latest_in_tx(conn)
        if current_epoch is not None and (latest is None or current_epoch > latest.epoch):
            raise EpochRefused(EPOCH_UNKNOWN, f"epoch {current_epoch} was never granted by this trader")
        expires = now + dt.timedelta(seconds=lease_seconds)
        if latest is not None and latest.lease_expires_at > now:
            if latest.holder_id != holder_id or current_epoch != latest.epoch:
                raise EpochRefused(EPOCH_HELD, f"epoch {latest.epoch} is leased until "
                                               f"{latest.lease_expires_at.isoformat()}")
            conn.execute("UPDATE ai_controller_epochs SET renewed_at = ?, lease_expires_at = ? WHERE epoch = ?",
                         [now, expires, latest.epoch])
            return EpochGrant(latest.epoch, holder_id, expires, renewed=True)
        epoch = 1 if latest is None else latest.epoch + 1
        conn.execute("INSERT INTO ai_controller_epochs VALUES (?, ?, ?, ?, ?)",
                     [epoch, holder_id, now, now, expires])
        return EpochGrant(epoch, holder_id, expires, renewed=False)

    def require_current(self, epoch: Optional[int]) -> None:
        """Read-only check outside a transaction (RPC handlers, Plan 1 Ruling 1a)."""
        self.require_current_in_tx(self._journal.connect(), epoch)

    def require_current_in_tx(self, conn: Any, epoch: Optional[int]) -> None:
        if epoch is None:
            raise EpochRefused(EPOCH_MISSING, "a controller epoch is required")
        latest = _latest_in_tx(conn)
        if latest is None or epoch != latest.epoch:
            current = None if latest is None else latest.epoch
            raise EpochRefused(EPOCH_STALE, f"epoch {epoch} is not the current epoch {current}")
