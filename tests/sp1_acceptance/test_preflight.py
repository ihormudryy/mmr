"""Plan 6 Task 2: the clean-account gate (pure) and its trader reads on the served stack."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from tests.sp1_fixtures import CONID
from trader.acceptance.preflight import evaluate_preflight
from trader.messaging.principals import TRADER_ACL

NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)


def iso(value):
    return value.isoformat()


CLEAN = {"account_id": "DU111111", "account_mode": "paper", "generation_id": 7, "net_liquidation": 1_000_000.0,
         "nlv_as_of": iso(NOW - dt.timedelta(seconds=40)), "daily_pnl": 0.0, "base_currency": "USD", "usd_per_base": 1.0,
         "positions": [], "working_orders": [], "unresolved_commands": [], "open_liquidation_roots": [],
         "exit_owner": None, "breaker_tripped": False, "experiment_state": None}


def test_clean_account_passes():
    assert evaluate_preflight(CLEAN, {**CLEAN, "net_liquidation": 1_000_200.0}, now=NOW).passed


@pytest.mark.parametrize("change,code", [
    ({"account_mode": "live"}, "NOT_PAPER"), ({"account_id": "U123"}, "NOT_PAPER"),
    ({"capture_error": "GENERATION_STAGING"}, "CAPTURE_UNAVAILABLE"),
    ({"positions": [{"conid": 265598, "quantity": 1.0}]}, "POSITIONS_OPEN"),
    ({"working_orders": [{"order_entity_id": "x", "is_external": True}]}, "WORKING_ORDERS"),
    ({"unresolved_commands": ["aip-old"]}, "UNRESOLVED_COMMANDS"), ({"open_liquidation_roots": ["r"]}, "LIQUIDATION_OPEN"),
    ({"exit_owner": "r"}, "EXIT_OWNER_ACTIVE"), ({"breaker_tripped": True}, "BREAKER_TRIPPED"),
    ({"net_liquidation": math.nan}, "EQUITY_INVALID"), ({"net_liquidation": 0.0}, "EQUITY_INVALID"),
    ({"nlv_as_of": iso(NOW - dt.timedelta(seconds=301))}, "EQUITY_STALE"), ({"daily_pnl": None}, "DAILY_PNL_UNKNOWN"),
    ({"base_currency": "EUR", "usd_per_base": None}, "FX_UNAVAILABLE")])
def test_preflight_stops_on_each_unclean_condition(change, code):                     # Review Focus 2
    result = evaluate_preflight({**CLEAN, **change}, {**CLEAN, **change}, now=NOW)
    assert not result.passed and code in result.failures


def test_a_non_usd_base_with_a_rate_passes():
    reading = {**CLEAN, "base_currency": "EUR", "usd_per_base": 1.1}
    assert evaluate_preflight(reading, reading, now=NOW).passed


def test_unstable_equity_stops():
    assert "EQUITY_UNSTABLE" in evaluate_preflight(CLEAN, {**CLEAN, "net_liquidation": 1_002_000.0}, now=NOW).failures


def test_a_problem_in_either_reading_stops():
    assert "BREAKER_TRIPPED" in evaluate_preflight(CLEAN, {**CLEAN, "breaker_tripped": True}, now=NOW).failures


def test_query_on_a_clean_served_stack(served):
    served.sim.promote()
    reading = served.call("ai_supervisor", "get_acceptance_preflight", {})
    assert (reading["account_id"], reading["account_mode"], reading["base_currency"]) == ("DU111111", "paper", "USD")
    assert reading["positions"] == [] and reading["working_orders"] == [] and reading["unresolved_commands"] == []
    assert reading["breaker_tripped"] is False and reading["experiment_state"] == "ARMED"
    assert evaluate_preflight(reading, reading, now=served.now()).passed


def test_query_reports_an_unresolved_command_and_a_working_order(served):
    served.sim.held[CONID] = 3.0
    served.sim.promote()
    close = {"decision_id": "dec-close-0001", "deployment_digest": None, "decider": "jev", "action": "CLOSE",
             "conid": CONID, "side": "SELL", "stop_price": None, "target_price": None, "quantity": None,
             "policy_revision": None, "evidence_digest": "sha256:" + "c" * 64,
             "expires_at": (served.now() + dt.timedelta(minutes=5)).isoformat()}
    out = served.call("ai_supervisor", "submit_ai_paper_decision", close)
    assert out["state"] == "OUTCOME_UNKNOWN", out
    served.sim.promote()
    reading = served.call("cli", "get_acceptance_preflight", {})
    assert "aip-dec-close-0001" in reading["unresolved_commands"]
    assert [o["conid"] for o in reading["working_orders"]] == [CONID]
    assert reading["positions"] == [{"conid": CONID, "quantity": 3.0}]
    result = evaluate_preflight(reading, reading, now=served.now())
    assert {"POSITIONS_OPEN", "WORKING_ORDERS", "UNRESOLVED_COMMANDS"} <= set(result.failures)


def test_query_returns_a_capture_error_instead_of_raising(served):
    served.sim.stage_generation()
    assert served.call("ai_supervisor", "get_acceptance_preflight", {}) == {"capture_error": "GENERATION_STAGING"}


def test_the_evidence_read_carries_oca_fields_and_is_generation_fenced(served):        # ruling 14
    from types import SimpleNamespace
    stop = SimpleNamespace(permId=7001, orderId=7001, action="SELL", totalQuantity=3.0, ocaGroup="oca-og-x",
                           ocaType=2, parentId=0, orderType="STP", lmtPrice=None, auxPrice=225.0, displaySize=0)
    served.sim.add_order("og-x:stop", "og-x", "stop", "SELL", "STP", 3, order=stop)
    served.sim.promote()
    reply = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": CONID})
    rows = reply["orders"]
    assert {"oca_group", "oca_type", "remaining_quantity", "leg", "status_events", "perm_id"} <= set(rows[0])
    assert (rows[0]["oca_group"], rows[0]["oca_type"], rows[0]["perm_id"]) == ("oca-og-x", 2, 7001)
    assert reply["promoted"] is True and reply["source"] == "synthetic" and reply["events_gapless"] is True
    assert [e["cursor"] for e in rows[0]["status_events"]] == [1]
    served.sim.stage_generation()
    assert served.call("ai_supervisor", "get_broker_order_evidence", {})["capture_error"] == "GENERATION_STAGING"


def test_status_events_are_gapless_per_generation_and_belong_to_the_promoted_one(served):
    served.sim.add_order("og-y:stop", "og-y", "stop", "SELL", "STP", 3)
    served.sim.add_order("og-y:take_profit", "og-y", "take_profit", "SELL", "LMT", 3)
    first = served.sim.promote()
    served.sim.set_status("og-y:take_profit", "Submitted", filled=1.0)
    served.sim.set_status("og-y:stop", "Submitted", total=2.0)
    second = served.sim.promote()
    reply = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": CONID})
    assert reply["generation_id"] == second != first
    events = {o["order_entity_id"]: o["status_events"] for o in reply["orders"]}
    assert [(e["cursor"], e["filled_quantity"]) for e in events["og-y:take_profit"]] == [(1, 1.0)]
    assert [(e["cursor"], e["remaining_quantity"]) for e in events["og-y:stop"]] == [(2, 2.0)]


def test_query_acls_are_exact():
    for method in ("get_acceptance_preflight", "get_broker_order_evidence"):
        assert TRADER_ACL[("query", method)] == frozenset({"cli", "dashboard", "ai_supervisor"})


@pytest.mark.parametrize("principal", ["ai_research", "strategy"])
def test_the_reads_are_denied_to_research_and_strategy(served, principal):
    from trader.messaging.typed_rpc import TypedRpcRemoteError
    for method in ("get_acceptance_preflight", "get_broker_order_evidence"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.client(principal, "query").call(method, {}, dict)
        assert exc.value.code == "PERMISSION_DENIED"
