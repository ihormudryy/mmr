"""SP2 Plan 3 Task 7: the discretionary scope rule at ai_paper entry admission."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.discretionary_world import discretionary_world
from trader.automation.ai_paper_evidence import planned_entry_limit


@pytest.fixture
def world(tmp_path):
    return discretionary_world(tmp_path)


def enter(world, **changes):
    return world.submit(deployment_digest=world.ddigest, **changes)


def test_an_in_scope_self_found_entry_is_submitted_and_labelled(world):
    receipt = enter(world)
    assert receipt.state == "SUBMITTED", receipt
    row = world.decisions.row("dec-00000001")
    assert (row.deployment_kind, row.strategy_digest, row.style) == ("discretionary", None, "intraday_long")
    assert world.scope.checks.detail(receipt.command_id, "admission")["part"] is None
    assert world.contracts.calls == 1 and world.volumes.calls == 1


@pytest.mark.parametrize("change,part", [
    (lambda w: w.contracts.set(CONID, primary_exchange="AMEX"), "exchange"),
    (lambda w: w.contracts.set(CONID, stock_type="WARRANT"), "instrument_type"),
    pytest.param(lambda w: w.contracts.set(CONID, stock_type=""), "instrument_type", id="blank-type"),
    (lambda w: w.quotes.set(bid=4.98, ask=5.0), "price"),
    (lambda w: w.volumes.set(volume=300_000.0), "dollar_volume"),
    (lambda w: w.filter_file.write(denylist=["AAPL"]), "trading_filter"),
    (lambda w: w.contracts.fail_with("IB contract details failed: TimeoutError"), "evidence_stale"),
    (lambda w: w.volumes.fail_with("no bars"), "evidence_stale"),
    (lambda w: setattr(w.quotes, "age", 6.0), "evidence_stale"),
])
def test_each_failed_part_is_refused_with_its_code(world, change, part):
    change(world)
    receipt = enter(world)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "OUT_OF_DISCRETIONARY_SCOPE")
    detail = receipt.outcome["detail"]
    assert (detail["part"], detail["phase"]) == (part, "admission")
    assert detail["check_id"] == f"admission:{receipt.command_id}"
    assert world.decisions.row("dec-00000001").error_code == "OUT_OF_DISCRETIONARY_SCOPE"
    assert world.dispatch.plans == []


def test_liquidity_boundary_is_exact(tmp_path):                     # review focus 3
    assert planned_entry_limit(100.0, 99.95, Decimal("10")) == 100.10      # the constants below rest on it
    world = discretionary_world(tmp_path)
    world.volumes.set(volume=600_000.0)                             # median $60M: cap $600,000 at 1%
    # 5,994 x 100.10 = 599,999.40 is inside the cap; other SP1 limits refuse that size.
    assert enter(world, quantity=5_994).error_code == "QUANTITY_ABOVE_MAXIMUM"
    receipt = enter(world, decision_id="dec-00000002", quantity=5_995)   # 600,099.50
    assert receipt.error_code == "OUT_OF_DISCRETIONARY_SCOPE"
    assert (receipt.outcome["detail"]["part"], receipt.outcome["detail"]["phase"]) == ("liquidity", "sizing")


def test_sizing_never_passes_the_cap(tmp_path):
    world = discretionary_world(tmp_path, rule={"max_order_share_of_dollar_volume": 0.0004})   # $40,000
    receipt = enter(world)
    assert receipt.state == "SUBMITTED"
    assert 0 < receipt.outcome["quantity"] * 100.10 <= 40_000.0


def test_a_strategy_deployment_is_unchanged(world):
    receipt = world.submit()                                        # world.digest: the SP1 strategy deployment
    assert receipt.state == "SUBMITTED" and world.contracts.calls == 0
    assert world.decisions.row("dec-00000001").deployment_kind == "strategy"


def test_without_a_wired_scope_service_a_discretionary_entry_fails_closed(tmp_path):
    world = discretionary_world(tmp_path)
    world.service.attach_scope(None)
    receipt = enter(world)
    assert (receipt.error_code, receipt.outcome["detail"]["part"]) == ("OUT_OF_DISCRETIONARY_SCOPE", "evidence_stale")
    assert world.dispatch.plans == []
