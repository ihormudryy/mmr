"""Trader-owned command-authority policy (design C2/C4, Phase 1 / sequence step 1).

The dashboard's DASHBOARD_COMMANDS_ENABLED flag is a UI kill switch; the trader
owns the enforcement policy and validates it fail-closed at startup. These tests
pin the parsing, the contradiction checks, and the capability-matrix derivation
that later phases build on.
"""
from __future__ import annotations

import pytest

from trader.trading.command_policy import (
    ALL_PORTS,
    COMMAND_PORT_REQUIREMENTS,
    KNOWN_STUB_PORTS,
    CommandAuthorityPolicy,
    CommandPolicyError,
    available_commands,
    validate_command_policy,
)

LIVE_ACCT = "U1234567"


class TestPolicyParsing:
    def test_missing_block_is_disabled_default(self):
        p = CommandAuthorityPolicy.from_config(None)
        assert p.enabled is False and p.live_enabled is False
        assert p.live_account_id is None and p.max_order_notional is None
        assert p.max_drift_bps == 50.0

    def test_parses_full_block(self):
        p = CommandAuthorityPolicy.from_config({
            "enabled": True, "live_enabled": True, "live_account_id": LIVE_ACCT,
            "max_order_notional": 25000, "max_drift_bps": 40,
        })
        assert p.enabled is True and p.live_enabled is True
        assert p.live_account_id == LIVE_ACCT
        assert p.max_order_notional == 25000.0 and p.max_drift_bps == 40.0

    def test_unknown_key_is_rejected(self):
        with pytest.raises(CommandPolicyError, match="unknown command_authority"):
            CommandAuthorityPolicy.from_config({"enabledd": True})

    def test_non_bool_enabled_is_rejected(self):
        with pytest.raises(CommandPolicyError, match="enabled"):
            CommandAuthorityPolicy.from_config({"enabled": "maybe"})

    def test_string_bool_forms_accepted(self):
        assert CommandAuthorityPolicy.from_config({"enabled": "true"}).enabled is True
        assert CommandAuthorityPolicy.from_config({"enabled": "off"}).enabled is False

    def test_non_numeric_notional_is_rejected(self):
        with pytest.raises(CommandPolicyError, match="max_order_notional"):
            CommandAuthorityPolicy.from_config({"max_order_notional": "lots"})


class TestPolicyValidation:
    def test_disabled_skips_all_checks(self):
        # A disabled authority is the master-off switch; even a nonsensical
        # live config passes because nothing is ever registered.
        p = CommandAuthorityPolicy.from_config({
            "enabled": False, "live_enabled": True, "max_drift_bps": -1})
        validate_command_policy(p, trader_account_id="DUpaper", paper_trading=True)

    def test_enabled_requires_positive_finite_drift(self):
        for bad in (0, -5, float("inf"), float("nan")):
            p = CommandAuthorityPolicy(enabled=True, max_drift_bps=bad)
            with pytest.raises(CommandPolicyError, match="max_drift_bps"):
                validate_command_policy(p, trader_account_id="DU", paper_trading=True)

    def test_enabled_rejects_nonpositive_notional(self):
        p = CommandAuthorityPolicy(enabled=True, max_order_notional=0.0)
        with pytest.raises(CommandPolicyError, match="max_order_notional"):
            validate_command_policy(p, trader_account_id="DU", paper_trading=True)

    def test_enabled_paper_without_live_is_fine(self):
        p = CommandAuthorityPolicy(enabled=True)
        validate_command_policy(p, trader_account_id="DUpaper", paper_trading=True)

    def test_enabled_live_account_without_live_enabled_is_fine(self):
        # Commands on, but risk-increasing ones gated off on a live account.
        p = CommandAuthorityPolicy(enabled=True, live_enabled=False)
        validate_command_policy(p, trader_account_id=LIVE_ACCT, paper_trading=False)

    def test_live_enabled_on_paper_trader_is_rejected(self):
        p = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id=LIVE_ACCT,
            max_order_notional=25000)
        with pytest.raises(CommandPolicyError, match="paper"):
            validate_command_policy(p, trader_account_id="DUpaper", paper_trading=True)

    def test_live_enabled_requires_account_id(self):
        p = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id=None,
            max_order_notional=25000)
        with pytest.raises(CommandPolicyError, match="live_account_id"):
            validate_command_policy(p, trader_account_id=LIVE_ACCT, paper_trading=False)

    def test_live_account_must_match_trader_account(self):
        p = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id="Uother",
            max_order_notional=25000)
        with pytest.raises(CommandPolicyError, match="does not match"):
            validate_command_policy(p, trader_account_id=LIVE_ACCT, paper_trading=False)

    def test_live_enabled_requires_notional_cap(self):
        p = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id=LIVE_ACCT,
            max_order_notional=None)
        with pytest.raises(CommandPolicyError, match="max_order_notional"):
            validate_command_policy(p, trader_account_id=LIVE_ACCT, paper_trading=False)

    def test_valid_live_policy_passes(self):
        p = CommandAuthorityPolicy(
            enabled=True, live_enabled=True, live_account_id=LIVE_ACCT,
            max_order_notional=25000, max_drift_bps=50)
        validate_command_policy(p, trader_account_id=LIVE_ACCT, paper_trading=False)


class TestCapabilityMatrix:
    def test_alert_adapter_is_the_last_stub(self):
        # After step 2c (cancel/state) + step 5 (nonce gate), the critical-alert
        # adapter is the only port still unbuilt -> ONLY approve_proposal (which
        # requires it) is withheld; every other command is available.
        ready = ALL_PORTS - KNOWN_STUB_PORTS
        avail = set(available_commands(ready))
        assert "approve_proposal" not in avail
        assert set(COMMAND_PORT_REQUIREMENTS) - {"approve_proposal"} <= avail

    def test_full_port_set_yields_every_command(self):
        assert set(available_commands(ALL_PORTS)) == set(COMMAND_PORT_REQUIREMENTS)

    def test_missing_strategy_port_excludes_strategy_commands(self):
        avail = set(available_commands(ALL_PORTS - {"strategy"}))
        assert avail.isdisjoint(
            {"enable_strategy", "disable_strategy", "update_strategy_params"})
        assert "create_proposal" in avail

    def test_empty_ports_yields_only_zero_dependency_commands(self):
        # reject_proposal needs no port; nothing else survives an empty port set.
        assert set(available_commands(set())) == {"reject_proposal"}

    def test_result_is_sorted_and_a_subset(self):
        avail = available_commands(ALL_PORTS)
        assert list(avail) == sorted(avail)
        assert set(avail) <= set(COMMAND_PORT_REQUIREMENTS)
