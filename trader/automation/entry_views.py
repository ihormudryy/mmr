"""What the protective saga, session_risk and build_bracket_plan read from an entry.

Two kinds of entry share that path: the one-strategy ``ExecutionIntent`` with
its ``VerifiedArtifact``, and the ai_paper ``AiPaperEntryOrder`` with its
``DeploymentBinding``. Both satisfy these protocols.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Protocol, Sequence


class EntryOrderView(Protocol):
    command_id: str
    conid: int
    side: str
    requested_quantity: Optional[Decimal]
    risk_fraction: Decimal
    entry_policy: Any
    stop_policy: Any
    target_policy: Any
    account_mode: str
    artifact_id: str


class AttestedNotionalView(Protocol):
    order_notional: Optional[float]


class EntryAuthorityView(Protocol):
    artifact_id: str
    allowlist: Sequence[str]
    max_gross_allocation: float
    attested_strategy: Optional[AttestedNotionalView]
