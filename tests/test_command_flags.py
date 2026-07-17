"""[M1-C] Task 1: dashboard command feature flags + fail-closed startup
validation.

Both DASHBOARD_COMMANDS_ENABLED and DASHBOARD_LIVE_COMMANDS_ENABLED default
false. Enabling live commands has no permissive fallback: it requires paper
commands enabled, an exact (non-empty, non-wildcard) account id, and a
finite positive max order notional -- any inconsistency raises
CommandFlagsError so a bad config kills the process at startup rather than
silently running in a partially-enabled state.
"""
import pytest

from web.command_center.flags import CommandFlags, CommandFlagsError, load_command_flags


def test_defaults_are_disabled():
    flags = load_command_flags({})
    assert flags == CommandFlags(False, False, None, None)


def test_live_requires_paper_commands_enabled():
    with pytest.raises(CommandFlagsError, match="DASHBOARD_COMMANDS_ENABLED"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "false",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
        })


@pytest.mark.parametrize("account", ["", "  ", "U*", "DU%", "live"])
def test_live_requires_exact_account_id(account):
    with pytest.raises(CommandFlagsError, match="exact DASHBOARD_LIVE_ACCOUNT_ID"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": account,
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
        })


@pytest.mark.parametrize("notional", ["", "0", "-1", "nan", "inf", "lots"])
def test_live_requires_positive_finite_notional(notional):
    with pytest.raises(CommandFlagsError, match="DASHBOARD_LIVE_MAX_ORDER_NOTIONAL"):
        load_command_flags({
            "DASHBOARD_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
            "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
            "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": notional,
        })


def test_valid_live_configuration():
    flags = load_command_flags({
        "DASHBOARD_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_COMMANDS_ENABLED": "true",
        "DASHBOARD_LIVE_ACCOUNT_ID": "U1234567",
        "DASHBOARD_LIVE_MAX_ORDER_NOTIONAL": "25000",
    })
    assert flags == CommandFlags(True, True, "U1234567", 25000.0)


def test_malformed_boolean_fails_loudly():
    with pytest.raises(CommandFlagsError, match="boolean"):
        load_command_flags({"DASHBOARD_COMMANDS_ENABLED": "enabled"})
