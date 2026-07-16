"""Domain contracts and canonical identities. [M1-F1]

This package holds the FROZEN, dependency-free domain layer: immutable
event/mutation dataclasses (``events``) and canonical entity-identity helpers
(``identity``). Everything here is consumed as a frozen contract by [M1-F2],
[M1-F3], and [M1-R]; see each module's docstring and the plan-index
cross-plan interface freeze before changing anything.
"""
from trader.domain.events import (
    DomainEvent,
    DomainMutation,
    EntityKey,
    JSONValue,
    Operation,
    ReadDomainEventsResult,
    SnapshotWithCursor,
)
from trader.domain.identity import (
    command_entity_id,
    instrument_entity_id,
    position_entity_id,
    proposal_entity_id,
    reconciliation_run_entity_id,
    risk_decision_entity_id,
    risk_policy_entity_id,
    risk_projection_entity_id,
    strategy_entity_id,
)

__all__ = [
    "DomainEvent",
    "DomainMutation",
    "EntityKey",
    "JSONValue",
    "Operation",
    "ReadDomainEventsResult",
    "SnapshotWithCursor",
    "command_entity_id",
    "instrument_entity_id",
    "position_entity_id",
    "proposal_entity_id",
    "reconciliation_run_entity_id",
    "risk_decision_entity_id",
    "risk_policy_entity_id",
    "risk_projection_entity_id",
    "strategy_entity_id",
]
