"""Strict wire models for the two ingestion commands (SP2 Plan 2, Cross-plan additions).

A leaf module: nothing here touches the store. ``body_digest`` is over the client body only,
with times normalized to UTC, so a retry spelled differently is still the same record.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Annotated, Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RECORD_ID = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9_./:@+-]{1,128}$")
SERVED_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DECISION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEPLOYMENT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

TRADER_SIZED = frozenset({"follow_signal.v1", "fixed_rule.v1"})        # Ruling 19
MATCHED_ENTRY = "matched_entry_bracket_exit.v1"

_Count = Annotated[int, Field(ge=0, le=10_000_000_000)]
_Usd = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
_Quantity = Annotated[int, Field(ge=1, le=10_000_000)]
_Conid = Annotated[int, Field(gt=0)]
IncompleteReason = Literal["quote_unavailable", "feed_not_accepted", "quote_not_executable", "ranking_unavailable",
                           "budget_refused", "model_failed", "sizing_unavailable"]


def parse_utc(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("a time needs an offset, for example 2026-10-06T14:30:00+00:00")
    return moment.astimezone(dt.timezone.utc)


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def body_digest(payload: Mapping[str, Any]) -> str:
    """Over client fields only, with times normalized to UTC, so a retry spelled differently still matches."""
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _matches(pattern: re.Pattern, value: Optional[str], name: str) -> Optional[str]:
    if value is not None and not pattern.fullmatch(value):
        raise ValueError(f"{name} does not match {pattern.pattern}")
    return value


def _aware_time(value: str) -> str:
    parse_utc(value)
    return value


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def normalized(self, *time_fields: str) -> dict:
        payload = self.model_dump(mode="json")
        for name in time_fields:
            payload[name] = parse_utc(payload[name]).isoformat()
        return payload


class RecordAiCostRequest(_Base):
    record_id: str
    experiment_id: str
    role: Literal["orchestrator", "jev", "research"]
    provider: str
    model: str
    attempt_id: str
    input_tokens: Optional[_Count] = None
    output_tokens: Optional[_Count] = None
    cost_usd: Optional[_Usd] = None
    cost_status: Literal["confirmed", "estimated", "unknown"]
    called_at: str
    served_kind: Literal["decision", "cycle", "signal", "research"]
    served_id: str
    decision_id: Optional[str] = None
    corrects_record_id: Optional[str] = None

    @field_validator("record_id", "attempt_id", "corrects_record_id")
    @classmethod
    def _record_ids(cls, value, info):
        return _matches(RECORD_ID, value, info.field_name)

    @field_validator("experiment_id")
    @classmethod
    def _experiment_id(cls, value):
        return _matches(EXPERIMENT_ID, value, "experiment_id")

    @field_validator("provider", "model")
    @classmethod
    def _model_ids(cls, value, info):
        return _matches(MODEL_ID, value, info.field_name)

    @field_validator("served_id")
    @classmethod
    def _served_id(cls, value):
        return _matches(SERVED_ID, value, "served_id")

    @field_validator("decision_id")
    @classmethod
    def _decision_id(cls, value):
        return _matches(DECISION_ID, value, "decision_id")

    @field_validator("called_at")
    @classmethod
    def _called_at(cls, value):
        return _aware_time(value)

    @model_validator(mode="after")
    def _status_matches_cost(self):
        if (self.cost_status == "unknown") != (self.cost_usd is None):
            raise ValueError("cost_status 'unknown' needs a null cost_usd, and any other status needs a number")
        if self.corrects_record_id == self.record_id:
            raise ValueError("a record cannot correct itself")
        return self

    def digest(self) -> str:
        return body_digest(self.normalized("called_at"))


class RecordSimulatedDecisionRequest(_Base):
    record_id: str
    experiment_id: str
    baseline_id: str
    cohort: str
    opportunity_id: str
    conid: Optional[_Conid] = None
    side: Optional[Literal["BUY"]] = None
    quantity: Optional[_Quantity] = None
    reference_price: Optional[_Price] = None
    stop_price: Optional[_Price] = None
    target_price: Optional[_Price] = None
    decided_at: str
    linked_decision_id: Optional[str] = None
    linked_round_trip_id: Optional[str] = None
    deployment_digest: Optional[str] = None
    incomplete_reason: Optional[IncompleteReason] = None

    @field_validator("record_id")
    @classmethod
    def _record_id(cls, value):
        return _matches(RECORD_ID, value, "record_id")

    @field_validator("experiment_id")
    @classmethod
    def _experiment_id(cls, value):
        return _matches(EXPERIMENT_ID, value, "experiment_id")

    @field_validator("baseline_id", "cohort", "opportunity_id", "linked_round_trip_id")
    @classmethod
    def _served_ids(cls, value, info):
        return _matches(SERVED_ID, value, info.field_name)

    @field_validator("linked_decision_id")
    @classmethod
    def _decision_id(cls, value):
        return _matches(DECISION_ID, value, "linked_decision_id")

    @field_validator("deployment_digest")
    @classmethod
    def _deployment_digest(cls, value):
        return _matches(DEPLOYMENT_DIGEST, value, "deployment_digest")

    @field_validator("decided_at")
    @classmethod
    def _decided_at(cls, value):
        return _aware_time(value)

    @model_validator(mode="after")
    def _shape_by_baseline(self):
        prices = (self.reference_price, self.stop_price, self.target_price)
        if self.linked_round_trip_id is not None and self.baseline_id != MATCHED_ENTRY:
            raise ValueError("linked_round_trip_id belongs to the matched-entry baseline only")
        if self.baseline_id == "no_trade.v1":
            extra = (self.side, self.quantity, *prices, self.deployment_digest, self.incomplete_reason)
            if any(v is not None for v in extra):
                raise ValueError("a no_trade record carries no side, quantity, prices, deployment or incomplete reason")
            return self
        unranked = self.incomplete_reason == "ranking_unavailable"
        if unranked and self.baseline_id != "fixed_rule.v1":
            raise ValueError("ranking_unavailable belongs to the fixed rule only")
        if self.conid is None and not unranked:
            raise ValueError("a trading baseline names its conid (unless nothing could be ranked)")
        if self.incomplete_reason is not None:
            if any(v is not None for v in (self.side, self.quantity, *prices)):
                raise ValueError("an incomplete baseline carries no side, quantity or prices (never invented)")
            return self
        if self.side is None or any(v is None for v in prices):
            raise ValueError("a complete trading baseline needs side and all three prices")
        if not self.stop_price < self.reference_price < self.target_price:
            raise ValueError("stop_price < reference_price < target_price is required for a BUY")
        if self.baseline_id in TRADER_SIZED:
            if self.quantity is not None:
                raise ValueError("the trader sizes this baseline: quantity must be null")
            if self.deployment_digest is None:
                raise ValueError("a sized baseline names the deployment a real ENTER would use")
        elif self.baseline_id == MATCHED_ENTRY and (self.quantity is None or self.linked_decision_id is None):
            raise ValueError("the matched-entry baseline carries its close's quantity and its ENTER decision")
        return self

    def digest(self) -> str:
        return body_digest(self.normalized("decided_at"))
