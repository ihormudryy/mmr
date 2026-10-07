"""SP2 Plan 1: controller epoch, reconcile read and signals over signed typed RPC."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.test_controller_epoch import Clock
from tests.test_ai_paper_rpc import _served, command, query
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
