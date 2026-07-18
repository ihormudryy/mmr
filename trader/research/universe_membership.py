"""Point-in-time universe membership (P2 Task 3, part A).

Survivorship bias is the most common way a backtest lies: reconstruct a universe
from *today's* members and every delisted loser silently disappears, inflating
every metric. This module records membership as dated, non-overlapping intervals
per instrument so an experiment can ask "who was a member on <date>?" and freeze
the exact answer into an order-independent digest the evidence chain signs over.

This module is OFFLINE-ONLY: it imports nothing operational and never opens the
journal/history/universe databases. The read adapter consumes a
``UniverseAccessor`` passed in by the caller (dependency-injected); it never
constructs or mutates one.

Boundary semantics (load-bearing, enforced + tested):
- ``effective_from`` is INCLUSIVE, ``effective_to`` is EXCLUSIVE;
- ``effective_to is None`` -> still a member (open-ended);
- ``delisted_at is None`` -> not delisted; ``delisted_at <= as_of`` -> NOT a
  member (the delisting date itself is already out);
- overlapping intervals for the SAME conid are a data bug and fail loudly;
- a blank ``source`` (missing provenance) is refused at construction.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Optional

from trader.research.canonical import sha256_digest

MEMBERSHIP_DIGEST_PREFIX = "universe_membership"


def _require_pure_date(name: str, value: Optional[dt.date], *, allow_none: bool) -> None:
    """A date field must be a plain ``datetime.date`` -- never a ``datetime``.

    ``datetime`` is a subclass of ``date``, so a stray timestamp would slip
    through ``isinstance`` and canonicalize to a different string, silently
    forking the frozen digest. Refuse it loudly.
    """
    if value is None:
        if allow_none:
            return
        raise ValueError(f"PointInTimeMembership.{name} is required (got None)")
    if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
        raise TypeError(
            f"PointInTimeMembership.{name} must be a datetime.date, "
            f"not {type(value).__name__}")


@dataclass(frozen=True)
class PointInTimeMembership:
    """One dated membership interval for a single instrument.

    The interval is ``[effective_from, effective_to)`` (inclusive/exclusive).
    ``effective_to=None`` means the instrument is still a member. ``delisted_at``
    is an independent hard cutoff: on or after it the instrument is never a
    member, regardless of ``effective_to``.
    """

    conid: int
    effective_from: dt.date
    effective_to: Optional[dt.date]
    symbol: str
    delisted_at: Optional[dt.date]
    source: str

    def __post_init__(self) -> None:
        # bool is an int subclass; a boolean conid is a bug, not a "1".
        if not isinstance(self.conid, int) or isinstance(self.conid, bool):
            raise TypeError(
                f"PointInTimeMembership.conid must be an int, "
                f"not {type(self.conid).__name__}")
        _require_pure_date("effective_from", self.effective_from, allow_none=False)
        _require_pure_date("effective_to", self.effective_to, allow_none=True)
        _require_pure_date("delisted_at", self.delisted_at, allow_none=True)
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("PointInTimeMembership.symbol must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError(
                "PointInTimeMembership.source must be non-empty (provenance is required)")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                f"PointInTimeMembership.effective_to {self.effective_to} must be after "
                f"effective_from {self.effective_from}")

    def is_active_at(self, as_of: dt.date) -> bool:
        """True iff this interval is a member on ``as_of`` (see module boundary
        semantics)."""
        if self.effective_from > as_of:
            return False
        if self.effective_to is not None and as_of >= self.effective_to:
            return False
        if self.delisted_at is not None and as_of >= self.delisted_at:
            return False
        return True


def validate_memberships(
    memberships: Iterable[PointInTimeMembership],
) -> tuple[PointInTimeMembership, ...]:
    """Reject overlapping intervals for the same conid; return a materialized tuple.

    Two intervals for one conid overlap when one starts strictly before the other
    ends (``effective_to=None`` is treated as +infinity). Contiguous intervals
    (``next.effective_from == prev.effective_to``) do NOT overlap -- exclusive end
    meets inclusive start cleanly. Different conids never overlap each other.
    """
    rows = tuple(memberships)
    by_conid: dict[int, list[PointInTimeMembership]] = {}
    for m in rows:
        by_conid.setdefault(m.conid, []).append(m)
    for conid, group in by_conid.items():
        ordered = sorted(group, key=lambda r: r.effective_from)
        for prev, cur in zip(ordered, ordered[1:]):
            prev_end = prev.effective_to if prev.effective_to is not None else dt.date.max
            if cur.effective_from < prev_end:
                raise ValueError(
                    f"overlapping membership intervals for conid {conid}: "
                    f"[{prev.effective_from}, {prev.effective_to}) and "
                    f"[{cur.effective_from}, {cur.effective_to})")
    return rows


def members_as_of(
    memberships: Iterable[PointInTimeMembership],
    as_of_date: dt.date,
) -> tuple[PointInTimeMembership, ...]:
    """Members effective at ``as_of_date``, sorted by conid for determinism.

    Validates first (fails loudly on overlapping intervals) so a query can never
    silently return an ambiguous point-in-time snapshot.
    """
    rows = validate_memberships(memberships)
    active = [m for m in rows if m.is_active_at(as_of_date)]
    active.sort(key=lambda m: m.conid)
    return tuple(active)


def _digest_sort_key(m: PointInTimeMembership):
    # Total, deterministic order over a VALID set (dates are pure ordinals; None
    # sorts first via a -1 sentinel). Order-independence of the digest depends on
    # this being a stable total order, not on input order.
    return (
        m.conid,
        m.effective_from.toordinal(),
        m.effective_to.toordinal() if m.effective_to is not None else -1,
        m.symbol,
        m.delisted_at.toordinal() if m.delisted_at is not None else -1,
        m.source,
    )


def _membership_body(memberships: Iterable[PointInTimeMembership]) -> list[dict]:
    return [
        {
            "conid": m.conid,
            "effective_from": m.effective_from,
            "effective_to": m.effective_to,
            "symbol": m.symbol,
            "delisted_at": m.delisted_at,
            "source": m.source,
        }
        for m in sorted(memberships, key=_digest_sort_key)
    ]


def membership_digest(memberships: Iterable[PointInTimeMembership]) -> str:
    """Order-independent, deterministic SHA-256 digest of a membership set.

    Freezes exactly which conids/symbols were members (with their intervals and
    provenance) so an experiment can attest to its universe. Reordering the input
    yields the same digest; changing any field of any row changes it. Validates
    first -- an overlapping (ambiguous) set has no well-defined membership and is
    refused rather than digested.
    """
    rows = validate_memberships(memberships)
    return sha256_digest(MEMBERSHIP_DIGEST_PREFIX, _membership_body(rows))


def read_current_universe_membership(
    accessor,
    universe_name: str,
    *,
    as_of: dt.date,
    source: str,
) -> tuple[PointInTimeMembership, ...]:
    """Snapshot a CURRENT universe into open-ended point-in-time memberships.

    Reads ``accessor.get(universe_name).security_definitions`` (each exposing
    ``.conId`` + ``.symbol``) and records each as a membership starting at
    ``as_of`` with ``effective_to=None`` and ``delisted_at=None`` under the given
    ``source``. This is a read-only adapter: it never mutates the accessor. A
    blank ``source`` or a duplicate conid in the universe fails loudly.

    NOTE: the current universe has no history, so this captures only "as of now".
    It is the migration seed for a proper dated membership table, not a substitute
    for one -- it cannot recover who *left* the universe before ``as_of``.
    """
    universe = accessor.get(universe_name)
    memberships = tuple(
        sorted(
            (
                PointInTimeMembership(
                    conid=int(sd.conId),
                    effective_from=as_of,
                    effective_to=None,
                    symbol=sd.symbol,
                    delisted_at=None,
                    source=source,
                )
                for sd in universe.security_definitions
            ),
            key=lambda m: m.conid,
        )
    )
    return validate_memberships(memberships)
