"""What the scoreboard reads from other plans, as small ports (Plan 5 A1-A3).

Plan 4's ``ExperimentStore`` and ``KillSessionEnd`` satisfy these structurally.
Plan 3's decision store is adapted by ``DecisionStoreAttribution``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo

from trader.trading.order_correlation import decode_order_ref, liquidation_child_kind, liquidation_child_root

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


@dataclass(frozen=True)
class DecisionFact:
    decision_id: str
    account_id: str
    experiment_id: Optional[str]
    conid: Optional[int]
    action: Optional[str]
    received_at: dt.datetime
    entry_quantity: Optional[int] = None       # the size SP1 gave a placed ENTER (its SUBMITTED receipt)
    state: Optional[str] = None                # the decision row state (CloseFills needs a final close)


class DecisionFacts(Protocol):
    def get(self, decision_id: str) -> Optional[DecisionFact]: ...


class NullDecisionFacts:
    """No ai_paper stack on this trader: every decision link is unknown."""

    def get(self, decision_id: str) -> None:
        return None


class DecisionStoreFacts:
    """Adapter over ``AiPaperDecisionStore.row``; a row without a decision id is not a decision.

    With the SP1 command ledger, the command's ledger state is the decision's state: the close reconciler
    resolves the ledger row and leaves the decision row at OUTCOME_UNKNOWN. A placed ENTER's size is the
    ``quantity`` of its SUBMITTED receipt.
    """

    def __init__(self, decision_store: Any, ledger: Any = None):
        self._store, self._ledger = decision_store, ledger

    def get(self, decision_id: str) -> Optional[DecisionFact]:
        row = self._store.row(decision_id)
        if row is None or row.decision_id is None:
            return None
        receipt = None if self._ledger is None else self._ledger.get(row.command_id)
        state = row.state if receipt is None else receipt.state
        # Plan 3 adds experiment_id to migration 56; before it lands every link is "another experiment".
        return DecisionFact(row.decision_id, row.account_id, getattr(row, "experiment_id", None), row.conid,
                            row.action, row.received_at, _entry_quantity(row, receipt), state)


def _entry_quantity(row: Any, receipt: Any) -> Optional[int]:
    if row.action != "ENTER" or receipt is None:
        return None
    quantity = (getattr(receipt, "outcome", None) or {}).get("quantity")
    return quantity if type(quantity) is int and quantity >= 1 else None


@dataclass(frozen=True)
class SizedBaseline:
    quantity: int
    inputs: Mapping[str, Any]          # what bound the size; stored as sizing_json


class SizingUnavailable(Exception):
    """The trader cannot size this baseline as it would size a real ENTER (Ruling 19). ``reason`` is the
    outcome's incomplete reason: sizing_unavailable, or quote_unavailable / quote_not_executable."""

    def __init__(self, code: str, inputs: Mapping[str, Any], *, reason: str = "sizing_unavailable"):
        super().__init__(code)
        self.code, self.inputs, self.reason = code, dict(inputs), reason


class BaselineSizer(Protocol):
    def size(self, *, account_id: str, deployment_digest: str, conid: int, reference_price: float,
             stop_price: float) -> SizedBaseline: ...


@dataclass(frozen=True)
class TripFact:
    round_trip_id: str
    conid: int
    opened_at: dt.datetime
    entry_qty: float                   # the broker-proven entry fill (round_trips.entry_qty)


class TripFacts(Protocol):
    def opened_by(self, experiment_id: str, entry_decision_id: str) -> Optional[TripFact]: ...

    def by_id(self, experiment_id: str, round_trip_id: str) -> Optional[TripFact]: ...


class StoreTripFacts:
    """Ruling 21: the trip an ENTER opened, from this journal's round_trips; one row or None."""

    def __init__(self, store: Any):
        self._store = store

    def opened_by(self, experiment_id: str, entry_decision_id: str) -> Optional[TripFact]:
        rows = self._store.fetch("round_trips", {"experiment_id": experiment_id, "decision_id": entry_decision_id})
        if len(rows) != 1:
            return None
        return self._fact(rows[0])

    def by_id(self, experiment_id: str, round_trip_id: str) -> Optional[TripFact]:
        rows = self._store.fetch("round_trips", {"experiment_id": experiment_id, "round_trip_id": round_trip_id})
        return self._fact(rows[0]) if len(rows) == 1 else None

    @staticmethod
    def _fact(row: Mapping[str, Any]) -> TripFact:
        return TripFact(row["round_trip_id"], int(row["conid"]), row["opened_at"], float(row["entry_qty"]))


@dataclass(frozen=True)
class CloseFill:
    proven: bool
    shares: Optional[int]              # broker-proven shares the close removed; None when not proven


class CloseFills(Protocol):
    def removed(self, round_trip_id: str, close_decision_id: str) -> CloseFill: ...


class NullCloseFills:
    def removed(self, round_trip_id: str, close_decision_id: str) -> CloseFill:
        return CloseFill(False, None)


class DecisionStoreCloseLinks:
    """Ruling 21: the model close a reduce order belongs to. Only reduce children remove shares for the close;
    a re-protect stop or target that fills later is a protective exit, and a cancel removes nothing."""

    def __init__(self, decision_store: Any):
        self._store = decision_store

    def close_decision_for_order_ref(self, ref: str) -> Optional[str]:
        group = decode_order_ref(ref)
        if group is None or liquidation_child_kind(group) != "reduce" or liquidation_child_root(group) is None:
            return None
        from trader.automation.ai_paper_decision import REDUCTIONS   # lazy: keeps this port module light
        closes = [link for link in self._store.links_for_order_ref(ref) if link.action in REDUCTIONS]
        # A refused close may carry another owner's root (EXIT_IN_PROGRESS): it never owns that root's orders.
        owners = [link for link in closes if self._store.row(link.decision_id).state != "REJECTED"]
        return owners[0].decision_id if len(owners) == 1 else None
