"""SP2 Plan 1 Task 4: the SP1 acceptance port holds its own controller epoch (Ruling 16)."""
from __future__ import annotations

from trader.acceptance.ports import RpcAcceptancePort
from trader.messaging.typed_rpc import TypedRpcRemoteError


class FakeClient:
    def __init__(self, held_times=0):
        self.calls, self.held_times = [], held_times

    def call(self, method, body, kind, **options):
        self.calls.append((method, body, options))
        if method == "grant_ai_controller_epoch":
            if self.held_times:
                self.held_times -= 1
                raise TypedRpcRemoteError("CONTROLLER_EPOCH_HELD", "held")
            return {"epoch": 4, "lease_expires_at": "2026-10-07T14:01:00+00:00"}
        return {"state": "SUBMITTED"}


def test_submit_carries_a_granted_epoch_and_waits_while_held():
    command, sleeps = FakeClient(held_times=2), []
    port = RpcAcceptancePort(command, FakeClient(), sleep=sleeps.append)
    assert port.supervisor("submit_ai_paper_decision", {"decision_id": "d"}) == {"state": "SUBMITTED"}
    grants = [c for c in command.calls if c[0] == "grant_ai_controller_epoch"]
    assert len(grants) == 3 and sleeps == [5.0, 5.0]
    assert grants[0][1]["holder_id"].startswith("acceptance-") and grants[0][1]["lease_seconds"] == 60
    assert command.calls[-1] == ("submit_ai_paper_decision", {"decision_id": "d"}, {"controller_epoch": 4})
    port.supervisor("submit_ai_paper_decision", {"decision_id": "e"})
    assert [c for c in command.calls if c[0] == "grant_ai_controller_epoch"][-1][1]["current_epoch"] == 4


def test_other_supervisor_calls_carry_no_epoch():
    command = FakeClient()
    port = RpcAcceptancePort(command, FakeClient())
    port.supervisor("pause_experiment", {"x": 1})
    assert command.calls == [("pause_experiment", {"x": 1}, {})]
