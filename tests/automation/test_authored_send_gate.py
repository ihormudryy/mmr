"""Review r1 #4: an authored retirement or switch-off after the final gate and before the send stops the entry."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_world import World
from trader.automation.authored_wire import RetireAuthoredStrategyRequest


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, real_liquidation=True)


def retire(store, digest):
    return store.retire(RetireAuthoredStrategyRequest.model_validate(
        {"digest": digest, "code": "SANDBOX_ERROR", "detail": "live fault"}))


def enter(world, armed, version):
    return world.submit(deployment_digest=armed.base, deployment_version=version, source_digest=armed.digest)


def test_a_deployed_authored_version_still_enters(world, tmp_path):
    armed = world.arm_authored(tmp_path / "store")
    assert enter(world, armed, armed.version).state == "SUBMITTED" and len(world.dispatch.plans) == 1


def test_a_retirement_after_the_final_gate_and_before_the_send_stops_the_entry(world, tmp_path):
    armed = world.arm_authored(tmp_path / "store")
    real = world.guard.revalidate

    def gate_then_retire(*args, **kwargs):
        permit = real(*args, **kwargs)                  # the final gate passed: the record was DEPLOYED
        assert retire(armed.store, armed.digest)["record"]["state"] == "RETIRED"
        return permit
    world.guard.revalidate = gate_then_retire
    receipt = enter(world, armed, armed.version)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE")
    assert world.dispatch.plans == []                     # no broker send


def test_a_retirement_before_the_send_stops_an_entry_on_a_renewed_version(world, tmp_path):   # review r2 #3
    armed = world.arm_authored(tmp_path / "store")
    renewed = armed.renew()                              # a new version on the same base; the record still holds V1
    real = world.guard.revalidate

    def gate_then_retire(*args, **kwargs):
        permit = real(*args, **kwargs)
        assert retire(armed.store, armed.digest)["record"]["state"] == "RETIRED"
        return permit
    world.guard.revalidate = gate_then_retire
    receipt = enter(world, armed, renewed)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE")
    assert world.dispatch.plans == []                     # the gate resolved the record from the renewed version


def test_a_switch_off_after_the_final_gate_and_before_the_send_stops_the_entry(world, tmp_path):
    armed = world.arm_authored(tmp_path / "store")
    real = world.guard.revalidate

    def gate_then_switch_off(*args, **kwargs):
        permit = real(*args, **kwargs)
        armed.store._config = replace(armed.store.config, enabled=False)   # a restart with the switch off
        return permit
    world.guard.revalidate = gate_then_switch_off
    receipt = enter(world, armed, armed.version)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "AUTHORED_STRATEGIES_DISABLED")
    assert world.dispatch.plans == []


def test_a_catalogue_version_is_not_touched_by_the_authored_gate(world, tmp_path):
    armed = world.arm_authored(tmp_path / "store")
    armed.store._config = replace(armed.store.config, enabled=False)
    assert world.submit().state == "SUBMITTED"            # the world's own catalogue version still enters


def test_a_retirement_waits_for_a_submitting_row_on_the_journal_lock(world, tmp_path):
    """The fence is real only if a retirement cannot commit beside the SUBMITTING transaction: DuckDB snapshots
    would let both commit having missed each other. With the journal wired, the retirement waits for the row
    and then finds the entry in flight; it never commits unseen while the send gate reads."""
    import threading

    armed = world.arm_authored(tmp_path / "store")
    replies = []
    retirement = threading.Thread(target=lambda: replies.append(retire(armed.store, armed.digest)))

    def retire_during_submitting(conn, request):
        retirement.start()
        retirement.join(timeout=0.5)
        assert retirement.is_alive()                     # held by the journal's write lock, not committed
    world.on_intent_check(retire_during_submitting)
    receipt = enter(world, armed, armed.version)
    retirement.join(timeout=10)
    assert receipt.state == "SUBMITTED" and len(world.dispatch.plans) == 1     # in flight before the retirement
    assert replies[0]["record"]["state"] == "RETIRED"
