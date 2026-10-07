"""What the scoreboard reads from other plans, as small ports (Plan 5 A1-A3).

Plan 4's ``ExperimentStore`` and ``KillSessionEnd`` satisfy these structurally.
Plan 3's decision store is adapted by ``DecisionStoreAttribution``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
SESSION_END_STATES = ("FLAT", "KILLED", "FAILED_SAFE")
ENTRY_ACTION = "ENTER"


def session_date_et(moment: dt.datetime) -> dt.date:
    """The XNYS session date of a moment: its America/New_York calendar date."""
    if not isinstance(moment, dt.datetime) or moment.tzinfo is None:
        raise ValueError(f"a timezone-aware datetime is required, got {moment!r}")
    return moment.astimezone(ET).date()


@dataclass(frozen=True)
class ExperimentRecord:
    """The fields Plan 5 reads; Plan 4's record is a superset with the same names."""
    experiment_id: str
    account_id: str
    started_at: dt.datetime
    start_net_liquidation: Optional[float]
    base_currency: Optional[str]
    start_usd_per_base: Optional[float]
    state: str
    killed_at: Optional[dt.datetime]
    stopped_at: Optional[dt.datetime] = None


class ExperimentReader(Protocol):
    def latest(self) -> Optional[Any]: ...
    def get(self, experiment_id: str) -> Optional[Any]: ...


class NullExperimentReader:
    """No experiment stack on this trader: the scoreboard idles."""

    def latest(self) -> None:
        return None

    def get(self, experiment_id: str) -> None:
        return None


@dataclass(frozen=True)
class AttributionLinks:
    decision_id: str
    decider: str
    strategy_version: str
    policy_revision: str
    style: str
    digest: str


class AttributionLookup(Protocol):
    def links_for_order_ref(self, ref: str) -> Optional[AttributionLinks]: ...


class NullAttributionLookup:
    def links_for_order_ref(self, ref: str) -> None:
        return None


def _text_or_dash(value: Any) -> str:
    return "-" if value is None else str(value)


def links_digest(decision_id: str, decider: str, strategy_version: str, policy_revision: str,
                 style: str, deployment_digest: Optional[str]) -> str:
    """A content digest over every attribution field, so an edit to any of them is visible."""
    payload = json.dumps([decision_id, decider, strategy_version, policy_revision, style, deployment_digest],
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class DecisionStoreAttribution:
    """Adapter over Plan 3's ``AiPaperDecisionStore.links_for_order_ref`` (A2).

    Exactly one ENTER link gives the trip's attribution. No link, only close
    links, or more than one ENTER link is ``None``: an ambiguous link is not guessed.
    """

    def __init__(self, decision_store: Any):
        self._store = decision_store

    def links_for_order_ref(self, ref: str) -> Optional[AttributionLinks]:
        entries = [link for link in self._store.links_for_order_ref(ref) if link.action == ENTRY_ACTION]
        if len(entries) != 1:
            return None
        link = entries[0]
        fields = (str(link.decision_id), str(link.decider), _text_or_dash(link.strategy_version),
                  _text_or_dash(link.policy_revision), _text_or_dash(link.style))
        return AttributionLinks(*fields, digest=links_digest(*fields, link.digest))


@dataclass(frozen=True)
class FxEvidence:
    base_currency: str
    usd_per_base: Optional[float]
    source: str
    as_of: dt.datetime


class FxEvidencePort(Protocol):
    def evidence(self) -> FxEvidence: ...


@dataclass(frozen=True)
class SessionEnd:
    account_id: str
    session_date: dt.date
    state: str
    ended_at: dt.datetime
