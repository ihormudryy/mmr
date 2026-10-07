"""SP2 Plan 3 Task 8: the discretionary scope rule at dispatch, and the discretionary label."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.automation.ai_paper_fixtures import CONID, NOW, quote
from tests.automation.discretionary_world import discretionary_world
from trader.automation.ai_paper_decision import AI_PAPER_ACTION
from trader.automation.ai_paper_evidence import planned_entry_limit
from trader.scoreboard.ports import DecisionStoreAttribution
from trader.trading.quote_feeds import LIVE_ONLY_FEEDS, PAPER_IEX_FEEDS


def test_price_falling_below_the_floor_at_dispatch_is_refused_with_its_part(tmp_path):    # review focus 2
    world = discretionary_world(tmp_path, rule={"min_price": 99.97})
    world.quotes.set(bid=99.98, ask=100.0)
    world.on_before_guard(lambda: world.quotes.set(bid=99.96, ask=100.0))     # no drift: the ask is unchanged
    receipt = world.submit(deployment_digest=world.ddigest)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "OUT_OF_DISCRETIONARY_SCOPE")
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("price", "dispatch")
    assert receipt.outcome["detail"]["check_id"] == f"dispatch:{receipt.command_id}"
    assert world.dispatch.plans == []
    assert world.decisions.row("dec-00000001").error_code == "OUT_OF_DISCRETIONARY_SCOPE"


def test_an_ask_rise_that_breaks_the_cap_is_refused_at_dispatch(tmp_path):
    assert planned_entry_limit(100.20, 100.15, Decimal("10")) == 100.30    # 399 x 100.30 = 40,019.70
    world = discretionary_world(tmp_path, rule={"max_order_share_of_dollar_volume": 0.0004})   # $40,000
    world.on_before_guard(lambda: world.quotes.set(bid=100.15, ask=100.20))   # 20 bps: inside the drift limit
    receipt = world.submit(deployment_digest=world.ddigest)
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("liquidity", "dispatch")
    assert world.dispatch.plans == []


def test_admission_evidence_older_than_sixty_seconds_is_stale_at_dispatch(tmp_path):
    world = discretionary_world(tmp_path)
    world.evidence.after = lambda: world.clock.advance(seconds=61)          # between admission and the saga
    receipt = world.submit(deployment_digest=world.ddigest)
    assert receipt.outcome["detail"]["part"] == "evidence_stale"
    assert world.dispatch.plans == []


def test_dispatch_makes_no_ib_or_history_call(tmp_path):
    world = discretionary_world(tmp_path)
    world.on_before_guard(lambda: (world.contracts.fail_with("must not be called"),
                                   world.volumes.fail_with("must not be called")))
    assert world.submit(deployment_digest=world.ddigest).state == "SUBMITTED"
    assert (world.contracts.calls, world.volumes.calls) == (1, 1)
    assert world.scope.checks.detail("aip-dec-00000001", "dispatch")["part"] is None


def test_a_discretionary_request_without_scope_evidence_fails_closed(tmp_path):
    world = discretionary_world(tmp_path)
    request = SimpleNamespace(action=AI_PAPER_ACTION, command_id="aip-x",
                              body={"deployment_digest": world.ddigest})
    approval = SimpleNamespace(discretionary_scope=None, conid=CONID, quantity=10.0)
    assert world.scope_gate(request, approval, quote(), NOW) == "OUT_OF_DISCRETIONARY_SCOPE"
    assert world.scope.checks.detail("aip-x", "dispatch")["part"] == "evidence_stale"


def test_scope_evidence_for_another_deployment_fails_closed(tmp_path):
    world = discretionary_world(tmp_path)
    request = SimpleNamespace(action=AI_PAPER_ACTION, command_id="aip-y", body={"deployment_digest": world.digest})
    evidence = SimpleNamespace(deployment_digest=world.ddigest)
    approval = SimpleNamespace(discretionary_scope=evidence, conid=CONID, quantity=10.0)
    assert world.scope_gate(request, approval, quote(), NOW) == "OUT_OF_DISCRETIONARY_SCOPE"
    assert world.scope.checks.detail("aip-y", "dispatch")["part"] == "evidence_stale"


def test_a_strategy_request_passes_the_gate_untouched(tmp_path):
    world = discretionary_world(tmp_path)
    request = SimpleNamespace(action=AI_PAPER_ACTION, command_id="aip-y", body={"deployment_digest": world.digest})
    assert world.scope_gate(request, SimpleNamespace(discretionary_scope=None), quote(), NOW) is None
    assert world.scope.checks.detail("aip-y", "dispatch") is None


def test_a_strategy_entry_writes_no_check(tmp_path):
    world = discretionary_world(tmp_path)
    receipt = world.submit()
    assert receipt.state == "SUBMITTED"
    assert world.db.execute("SELECT count(*) FROM discretionary_scope_checks", fetch="one") == (0,)


def test_the_trip_link_says_discretionary(tmp_path):
    world = discretionary_world(tmp_path)
    world.submit(deployment_digest=world.ddigest)
    (link,) = world.decisions.links_for_order_ref("mmr:og-aip-dec-00000001")
    assert (link.strategy_version, link.deployment_kind) == ("discretionary", "discretionary")
    assert DecisionStoreAttribution(world.decisions).links_for_order_ref(
        "mmr:og-aip-dec-00000001").strategy_version == "discretionary"


def test_a_strategy_trip_link_keeps_its_strategy_digest(tmp_path):
    world = discretionary_world(tmp_path)
    world.submit()
    (link,) = world.decisions.links_for_order_ref("mmr:og-aip-dec-00000001")
    assert (link.strategy_version, link.deployment_kind) == ("sha256:" + "a" * 64, "strategy")


@pytest.mark.parametrize("feeds", [PAPER_IEX_FEEDS, LIVE_ONLY_FEEDS])
def test_an_iex_quote_follows_the_accepted_set(tmp_path, feeds):            # owner #74
    world = discretionary_world(tmp_path, accepted_feeds=feeds)
    world.quotes.set(feed_type="iex_realtime")
    receipt = world.submit(deployment_digest=world.ddigest)
    if feeds == PAPER_IEX_FEEDS:
        assert receipt.state == "SUBMITTED", receipt
        for phase in ("admission", "dispatch"):
            (raw,) = world.db.execute("SELECT evidence_json FROM discretionary_scope_checks "
                                      "WHERE command_id = ? AND phase = ?", [receipt.command_id, phase], fetch="one")
            assert json.loads(raw)["quote"]["feed_type"] == "iex_realtime"
    else:
        assert receipt.error_code == "OUT_OF_DISCRETIONARY_SCOPE"
        assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("evidence_stale",
                                                                                           "admission")
        assert world.dispatch.plans == []
