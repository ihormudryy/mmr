from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from trader.automation.intent_ids import derive_intent_id, derive_command_id

@dataclass(frozen=True)
class EntryPolicy:
    order_type: Literal["LIMIT", "MARKETABLE_LIMIT"]
    limit_offset_bps: Decimal
    tif: Literal["DAY"]

@dataclass(frozen=True)
class StopPolicy:
    stop_price: Decimal
    order_type: Literal["STP", "STP_LMT"]

@dataclass(frozen=True)
class TargetPolicy:
    target_price: Decimal
    order_type: Literal["LMT"]

@dataclass(frozen=True)
class TimeExitPolicy:
    max_hold_bars: int | None
    close_by: datetime

    def __post_init__(self):
        if self.close_by.tzinfo is None or self.close_by.utcoffset() is None:
            raise ValueError("close_by must be timezone-aware")
        if self.max_hold_bars is not None and self.max_hold_bars <= 0:
            raise ValueError("max_hold_bars must be strictly positive if provided")

@dataclass(frozen=True)
class ExecutionIntent:
    artifact_id: str
    session_id: str
    bar_id: str
    signal_id: str
    intent_id: str
    command_id: str
    account_mode: Literal["paper", "live"]
    conid: int
    side: Literal["BUY", "SELL"]
    requested_quantity: Decimal | None
    risk_fraction: Decimal
    entry_policy: EntryPolicy
    stop_policy: StopPolicy
    target_policy: TargetPolicy | None
    time_exit_policy: TimeExitPolicy
    artifact_digest: str
    eligibility_attestation_digest: str
    signal_timestamp: datetime
    completed_bar_timestamp: datetime

    def __post_init__(self):
        if self.signal_timestamp.tzinfo is None or self.signal_timestamp.utcoffset() is None:
            raise ValueError("signal_timestamp must be timezone-aware")
        if self.completed_bar_timestamp.tzinfo is None or self.completed_bar_timestamp.utcoffset() is None:
            raise ValueError("completed_bar_timestamp must be timezone-aware")
        if self.completed_bar_timestamp > self.signal_timestamp:
            raise ValueError("completed_bar_timestamp must be <= signal_timestamp")
        if self.conid <= 0:
            raise ValueError("conid must be positive")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not (Decimal("0") < self.risk_fraction <= Decimal("1")):
            raise ValueError("risk_fraction must be in (0, 1.0]")
        if self.account_mode not in ("paper", "live"):
            raise ValueError("account_mode must be 'paper' or 'live'")
        if self.stop_policy.stop_price <= Decimal("0"):
            raise ValueError("stop_price must be strictly positive")
        if self.target_policy is not None and self.target_policy.target_price <= Decimal("0"):
            raise ValueError("target_price must be strictly positive")
        if self.requested_quantity is not None and self.requested_quantity <= Decimal("0"):
            raise ValueError("requested_quantity must be strictly positive if provided")
            
        fields = asdict(self)
        expected_intent = derive_intent_id(fields)
        if self.intent_id != expected_intent:
            raise ValueError(f"intent_id {self.intent_id} does not match derived {expected_intent}")
        expected_command = derive_command_id(expected_intent)
        if self.command_id != expected_command:
            raise ValueError(f"command_id {self.command_id} does not match derived {expected_command}")
