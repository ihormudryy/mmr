"""SP2 Plan 1: controller epoch, reconcile read and signals over signed typed RPC."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.test_controller_epoch import Clock
from tests.test_ai_paper_rpc import _served, command, enter_body, publish, query, register
from trader.automation.ai_paper_config import AiPaperConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import SignalEntry, StrategySignalRecord
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


def strategy_writes(served, minutes, *, now=NOW):
    """The strategy service's side: its own record object on the same file."""
    record = StrategySignalRecord(DuckDBConnection.get_instance(served.stack.ai_paper.signals_path),
                                  now=lambda: now)
    for minute in minutes:
        record.append(SignalEntry.create(strategy_name="orb", conid=265598, action="BUY", probability=0.6,
                                         signal_time=NOW + dt.timedelta(minutes=minute)))


def read_signals(served, after, limit, epoch, principal="ai_supervisor"):
    return query(served, principal).call("read_ai_signals", {"after_cursor": after, "limit": limit}, dict,
                                         controller_epoch=epoch)


def test_signals_page_through_rpc(served):
    epoch = grant(served)["epoch"]
    strategy_writes(served, range(3))
    page = read_signals(served, 0, 2, epoch)
    assert [s["cursor"] for s in page["signals"]] == [1, 2]
    assert (page["next_cursor"], page["oldest_retained_cursor"], page["gap"]) == (2, 1, False)
    assert page["signals"][0]["action"] == "BUY" and page["signals"][0]["conid"] == 265598


def test_signal_gap_and_reset_are_reported(served):
    epoch = grant(served)["epoch"]
    strategy_writes(served, range(2))
    strategy_writes(served, [9_999], now=NOW + dt.timedelta(days=8))
    assert read_signals(served, 0, 10, epoch)["gap"] is True
    assert code_of(read_signals, served, 50, 10, epoch) == "SIGNAL_CURSOR_AHEAD"


def test_signal_read_needs_the_current_epoch(served, clock):
    epoch = grant(served)["epoch"]
    assert code_of(read_signals, served, 0, 10, None) == "CONTROLLER_EPOCH_MISSING"
    clock.advance(61)
    grant(served, holder="ctl-b")
    assert code_of(read_signals, served, 0, 10, epoch) == "CONTROLLER_EPOCH_STALE"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research", "strategy"])
def test_signal_read_is_supervisor_only(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("read_ai_signals", {"after_cursor": 0, "limit": 1}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [{"after_cursor": 0, "limit": 501}, {"after_cursor": True, "limit": 1},
                                  {"after_cursor": 0, "limit": 1, "x": 1}])
def test_signal_read_wire_is_strict(served, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("read_ai_signals", body, dict, controller_epoch=1)
    assert exc.value.code == "VALIDATION_ERROR"
