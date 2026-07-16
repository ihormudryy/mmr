"""Canonical identity-helper tests. [M1-F1] Task 2.

Any separator or format drift here is silent data corruption: M1-R reduces
its read model by ``entity_id`` string equality, so a changed format re-keys
every downstream entity. Each test pins one canonical format.
"""
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


def test_position_key_includes_account_and_conid():
    assert position_entity_id("DU123", 265598) == "DU123:265598"


def test_position_key_accepts_string_conid():
    assert position_entity_id("DU123", "265598") == "DU123:265598"


def test_instrument_key_is_the_bare_conid_string():
    assert instrument_entity_id(265598) == "265598"
    assert instrument_entity_id("265598") == "265598"


def test_proposal_key_is_the_integer_rendered_as_a_string():
    assert proposal_entity_id(42) == "42"
    assert proposal_entity_id("42") == "42"


def test_strategy_key_is_the_strategy_name():
    assert strategy_entity_id("smi_crossover") == "smi_crossover"


def test_command_key_is_the_command_id():
    assert command_entity_id("cmd-1") == "cmd-1"


def test_reconciliation_run_key_is_the_run_id():
    assert reconciliation_run_entity_id("run-1") == "run-1"


def test_risk_namespaces_use_their_prefixes():
    assert risk_policy_entity_id("default") == "policy:default"
    assert risk_projection_entity_id("DU123") == "projection:DU123"
    assert risk_decision_entity_id("cmd-1") == "decision:cmd-1"


def test_risk_namespaces_are_disjoint():
    # Three separate revision streams must never collide on a shared key.
    keys = {
        risk_policy_entity_id("x"),
        risk_projection_entity_id("x"),
        risk_decision_entity_id("x"),
    }
    assert len(keys) == 3
