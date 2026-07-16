"""Canonical entity-identity helpers. [M1-F1] Task 2 — FROZEN formats.

Every producer keys its entities through these helpers and M1-R reduces its
read model by ``entity_id`` string equality, so the *output format* of each
helper is a frozen contract: any separator or format drift silently re-keys
downstream entities and corrupts the read model. The formats are fixed by the
plan-index cross-plan interface freeze.

Scope: this module owns identity for instruments, positions, proposals,
strategies, risk namespaces, commands, and reconciliation runs. Order and
fill identity helpers (``order_group_leg_entity_id``,
``external_order_entity_id``, ``fill_entity_id``) are owned by [M1-F2]
broker-producers and defined there — this module deliberately does NOT define
them to avoid a conflicting duplicate. For reference, the canonical formats
those helpers emit: an order key is the immutable local order id
(``permId``/``clientId``/``parentId`` are non-rekeying aliases, never the
key), and a fill keys on ``account:execId`` (e.g. ``"DU123:exec-1"``).
"""
from __future__ import annotations


def instrument_entity_id(conid: int | str) -> str:
    """An instrument is keyed by its bare IB conId, e.g. ``"265598"``."""
    return str(conid)


def position_entity_id(account_id: str, conid: int | str) -> str:
    """A position is keyed ``account:conId``, e.g. ``"DU123:265598"``.

    Both parts are required: the same conId held in two accounts is two
    distinct positions with independent revision streams.
    """
    return f"{account_id}:{conid}"


def proposal_entity_id(proposal_id: int | str) -> str:
    """A proposal is keyed by its integer id rendered as a string, e.g. ``"42"``."""
    return str(proposal_id)


def strategy_entity_id(strategy_name: str) -> str:
    """A strategy is keyed by its unique strategy name."""
    return strategy_name


def command_entity_id(command_id: str) -> str:
    """A command is keyed by its command id."""
    return command_id


def reconciliation_run_entity_id(run_id: str) -> str:
    """A reconciliation run is keyed by its run id."""
    return run_id


def risk_policy_entity_id(policy_id: str) -> str:
    """Risk policy namespace: ``policy:<policy_id>``."""
    return f"policy:{policy_id}"


def risk_projection_entity_id(account_id: str) -> str:
    """Risk projection namespace: ``projection:<account_id>``."""
    return f"projection:{account_id}"


def risk_decision_entity_id(command_id: str) -> str:
    """Per-command risk decision namespace (write-once): ``decision:<command_id>``."""
    return f"decision:{command_id}"
