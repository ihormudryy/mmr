"""SP2 Plan 1 Task 4: the epoch is checked atomically with the decision claim (spec 5.1)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.ai_paper_world import World
from trader.trading.command_coordinator import canonical_request_hash


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)                       # the World's experiment is ARMED by default


def takeover(world):
    world.clock.advance(seconds=61)
    return world.epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60).epoch


def test_the_epoch_is_not_part_of_the_command_identity(world):
    request = world.request(world.body())
    assert canonical_request_hash(request) == canonical_request_hash(replace(request, controller_epoch=9))


def test_a_current_epoch_is_admitted_and_recorded(world):
    receipt = world.submit()
    assert receipt.state == "SUBMITTED", receipt
    assert world.decisions.row("dec-00000001").controller_epoch == world.epoch


def test_a_missing_epoch_is_refused(world):
    receipt = world.coordinator.execute(replace(world.request(world.body()), controller_epoch=None))
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_MISSING", False)
    assert world.dispatch.plans == []


def test_a_stale_epoch_is_refused_at_the_claim(world):
    takeover(world)
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_STALE", False)
    assert world.dispatch.plans == []


def test_takeover_during_admission_is_refused_at_the_claim(world, monkeypatch):     # Review Focus 2
    real = world.evidence.prepare_entry

    def prepare_then_take_over(**kwargs):
        prepared = real(**kwargs)
        takeover(world)              # after _validate, before _claim
        return prepared
    monkeypatch.setattr(world.evidence, "prepare_entry", prepare_then_take_over)
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_STALE", False)
    assert world.dispatch.plans == []
    assert world.ledger.get(receipt.command_id).state == "REJECTED"
    assert world.decisions.row("dec-00000001").error_code == "CONTROLLER_EPOCH_STALE"


def test_an_admitted_command_survives_a_later_takeover(world):
    assert world.submit().state == "SUBMITTED"
    takeover(world)
    assert world.coordinator.get_command("aip-dec-00000001").state == "SUBMITTED"
    assert len(world.dispatch.plans) == 1


def test_a_reduction_needs_the_current_epoch_too(world):
    world.held(CONID, 300.0)
    takeover(world)
    close = world.body(action="CLOSE", side="SELL", deployment_digest=None, policy_revision=None,
                       stop_price=None, quantity=None)
    assert world.submit(close).error_code == "CONTROLLER_EPOCH_STALE"
