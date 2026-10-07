"""SP2 Plan 1: controller epoch, reconcile read and signals over signed typed RPC."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.test_controller_epoch import Clock
from tests.test_ai_paper_rpc import _served, command, enter_body, publish, query, register
from trader.automation.ai_paper_config import AiPaperConfig
from trader.messaging.typed_rpc import TypedRpcRemoteError


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def served(tmp_path, monkeypatch, clock):
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), now=clock)
    yield stack
    stack.close()


def grant(served, holder="ctl-a", current=None, lease=60, principal="ai_supervisor"):
    return command(served, principal).call(
        "grant_ai_controller_epoch", {"holder_id": holder, "current_epoch": current, "lease_seconds": lease}, dict)


def code_of(fn, *args, **kwargs):
    with pytest.raises(TypedRpcRemoteError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_grant_renew_and_takeover(served, clock):
    first = grant(served)
    assert first["epoch"] == 1
    assert dt.datetime.fromisoformat(first["lease_expires_at"]) == NOW + dt.timedelta(seconds=60)
    clock.advance(20)
    assert grant(served, current=1)["epoch"] == 1
    assert code_of(grant, served, holder="ctl-b") == "CONTROLLER_EPOCH_HELD"
    clock.advance(61)
    assert grant(served, holder="ctl-b")["epoch"] == 2
    assert code_of(grant, served, current=9) == "CONTROLLER_EPOCH_UNKNOWN"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research", "strategy"])
def test_only_the_supervisor_may_grant(served, principal):
    assert code_of(grant, served, principal=principal) == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [
    {"holder_id": "ctl-a", "lease_seconds": 60},                                   # current_epoch key required
    {"holder_id": "ctl-a", "current_epoch": True, "lease_seconds": 60},
    {"holder_id": "ctl-a", "current_epoch": None, "lease_seconds": 60, "extra": 1}])
def test_grant_wire_is_strict(served, body):
    assert code_of(command(served, "ai_supervisor").call, "grant_ai_controller_epoch", body, dict) == \
        "VALIDATION_ERROR"


def armed(served):
    publish(served)
    digest = register(served)
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()
    return digest


def submit(served, body, epoch):
    return command(served, "ai_supervisor").call("submit_ai_paper_decision", body, dict, controller_epoch=epoch)


def test_missing_epoch_is_refused_without_a_ledger_row(served):
    body = enter_body(armed(served))
    assert code_of(submit, served, body, None) == "CONTROLLER_EPOCH_MISSING"
    assert served.coordinator.get_command("aip-dec-00000001") is None


def test_stale_holder_resend_is_refused_and_successor_replays(served, clock):       # Review Focus 1
    body = enter_body(armed(served))
    first = submit(served, body, grant(served)["epoch"])
    assert first["state"] == "SUBMITTED", first
    clock.advance(61)
    successor = grant(served, holder="ctl-b")["epoch"]
    assert code_of(submit, served, body, 1) == "CONTROLLER_EPOCH_STALE"
    replay = submit(served, body, successor)                                         # replay, not a new command
    assert (replay["state"], replay["command_id"]) == ("SUBMITTED", first["command_id"])
    assert len(served.orders.plans) == 1


def test_epoch_from_cli_on_submit_is_an_authentication_error(served):
    assert code_of(command(served, "cli").call, "submit_ai_paper_decision", enter_body(), dict,
                   controller_epoch=1) == "AUTHENTICATION_ERROR"


def read_decision(served, decision_id, epoch, principal="ai_supervisor"):
    return query(served, principal).call("get_ai_paper_decision", {"decision_id": decision_id}, dict,
                                         controller_epoch=epoch)


def test_reconcile_read_returns_receipt_and_decision_row(served):
    body = enter_body(armed(served))
    epoch = grant(served)["epoch"]
    first = submit(served, body, epoch)
    view = read_decision(served, "dec-00000001", epoch)
    assert view["found"] is True and view["receipt"]["state"] == first["state"] == "SUBMITTED"
    assert view["receipt"]["command_id"] == first["command_id"]
    assert (view["command_id"], view["decision_state"], view["controller_epoch"]) == \
        ("aip-dec-00000001", "SUBMITTED", epoch)
    unknown = read_decision(served, "dec-99999999", epoch)
    assert (unknown["found"], unknown["receipt"], unknown["decision_state"]) == (False, None, None)


def test_reconcile_read_needs_the_current_epoch(served, clock):
    epoch = grant(served)["epoch"]
    assert code_of(read_decision, served, "dec-00000001", None) == "CONTROLLER_EPOCH_MISSING"
    clock.advance(61)
    grant(served, holder="ctl-b")
    assert code_of(read_decision, served, "dec-00000001", epoch) == "CONTROLLER_EPOCH_STALE"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research"])
def test_reconcile_read_is_supervisor_only(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("get_ai_paper_decision", {"decision_id": "dec-00000001"}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [{"decision_id": "dec:0001"}, {"decision_id": 7}, {}, {"decision_id": "d" * 8,
                                                                                          "x": 1}])
def test_reconcile_read_wire_is_strict(served, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("get_ai_paper_decision", body, dict, controller_epoch=1)
    assert exc.value.code == "VALIDATION_ERROR"
