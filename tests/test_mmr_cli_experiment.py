"""SP1 Plan 4 Task 7: `mmr experiment ...` over a fake SDK."""
from __future__ import annotations

import json

import pytest

from trader import mmr_cli
from trader.mmr_cli import build_parser


DETECTION = "IB account updates, about 3 minutes; paper only"


def _experiment(state="ARMED", **changes):
    view = {"experiment_id": "exp-" + "a" * 20, "state": state, "started_at": "2026-07-17T15:00:00+00:00",
            "start_net_liquidation": 100_000.0, "base_currency": "USD", "kill_flat_state": None,
            "pause_cause": None}
    view.update(changes)
    return view


class FakeSdk:
    def __init__(self):
        self.experiment = _experiment()
        self.kill_line = {"active": {"pct": 20.0, "basis": "start"}, "configured": {"pct": 20.0, "basis": "start"},
                          "pending_restart": False, "detection": DETECTION}
        self.entry_block = None
        self.pause_receipt = {"state": "PAUSED"}
        self.calls = []

    def experiment_status(self):
        return {"experiment": self.experiment, "entry_block": self.entry_block, "last_evaluation": None,
                "kill_line": self.kill_line, "mode_conflict": None, "transitions": []}

    def _ok(self, name, *args, **kwargs):
        from trader.common.reactivex import SuccessFail
        self.calls.append((name, args, kwargs))
        return SuccessFail.success(obj={"state": "PAUSED"})

    def experiment_start(self, reason):
        return self._ok("start", reason)

    def experiment_pause(self, reason, experiment_id=None):
        from trader.common.reactivex import SuccessFail
        self.calls.append(("pause", (reason,), {"experiment_id": experiment_id}))
        return SuccessFail.success(obj=self.pause_receipt)

    def experiment_resume(self, reason, experiment_id=None):
        return self._ok("resume", reason, experiment_id=experiment_id)

    def experiment_stop(self, reason, experiment_id=None):
        return self._ok("stop", reason, experiment_id=experiment_id)


@pytest.fixture
def sdk():
    return FakeSdk()


@pytest.fixture
def cli(sdk, capsys, monkeypatch):
    def run(line, json_mode=False):
        monkeypatch.setattr(mmr_cli, "_json_mode", json_mode)
        args = build_parser().parse_args(line.split())
        mmr_cli._handle_experiment(sdk, args)
        return capsys.readouterr().out
    return run


def test_status_says_flatten_pending_until_flat(cli, sdk):
    sdk.experiment = _experiment("KILLED", kill_flat_state="PENDING")
    out = cli("experiment status")
    assert "flatten pending" in out and "PAPER" in out
    sdk.experiment = _experiment("KILLED", kill_flat_state="FLAT")
    out = cli("experiment status")
    assert "flatten pending" not in out and "FLAT on broker evidence" in out


def test_status_shows_active_and_pending_kill_lines(cli, sdk):                  # K9, owner answer
    sdk.kill_line = {"active": {"pct": 20.0, "basis": "start"}, "configured": {"pct": 15.0, "basis": "start"},
                     "pending_restart": True, "detection": DETECTION}
    out = cli("experiment status")
    assert "active kill line 20.0%" in out and "15.0% is NOT active until trader_service restarts" in out
    assert "about 3 minutes" in out


def test_status_shows_the_entry_block(cli, sdk):
    sdk.entry_block = "KILL_LINE_UNKNOWN"
    assert "entries refused: KILL_LINE_UNKNOWN" in cli("experiment status")


def test_status_without_an_experiment(cli, sdk):
    sdk.experiment = None
    assert "PAPER: no experiment" in cli("experiment status")


def test_resume_defaults_to_the_active_experiment_id(cli, sdk):
    cli("experiment resume --reason ok")
    assert sdk.calls == [("resume", ("ok",), {"experiment_id": None})]       # the SDK resolves the active id


def test_explicit_experiment_id_is_passed(cli, sdk):
    cli("experiment stop --experiment-id exp-bbbbbbbbbbbbbbbbbbbb --reason done")
    assert sdk.calls == [("stop", ("done",), {"experiment_id": "exp-bbbbbbbbbbbbbbbbbbbb"})]


def test_start_needs_a_reason():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["experiment", "start"])


def test_json_output_shape(cli, sdk):
    data = json.loads(cli("experiment status", json_mode=True))
    assert set(data) == {"data", "title"} and data["data"]["experiment"]["state"] == "ARMED"
    data = json.loads(cli("experiment pause --reason p", json_mode=True))
    assert data == {"data": {"state": "PAUSED"}, "title": "Experiment pause"}


def test_pause_names_the_entries_already_being_sent(cli, sdk):                       # issue #124
    sdk.pause_receipt = {"state": "PAUSED", "entries_in_flight": ["aip-dec-00000001", "aip-dec-00000002"]}
    out = " ".join(cli("experiment pause --reason p").split())
    assert ("2 entries were already being sent and may still reach the broker: "
            "aip-dec-00000001, aip-dec-00000002. Check mmr orders / mmr portfolio.") in out
    sdk.pause_receipt = {"state": "PAUSED", "entries_in_flight": ["aip-dec-00000001"]}
    assert "1 entry was already being sent" in " ".join(cli("experiment pause --reason p").split())


def test_pause_with_no_entry_in_flight_prints_no_warning(cli, sdk):
    sdk.pause_receipt = {"state": "PAUSED", "entries_in_flight": []}
    assert "already being sent" not in cli("experiment pause --reason p")


def test_json_pause_keeps_stdout_pure_and_carries_the_list(cli, sdk):
    sdk.pause_receipt = {"state": "PAUSED", "entries_in_flight": ["aip-dec-00000001"]}
    data = json.loads(cli("experiment pause --reason p", json_mode=True))
    assert data["data"]["entries_in_flight"] == ["aip-dec-00000001"]


def test_sdk_resolves_the_active_experiment_id(monkeypatch):
    from types import SimpleNamespace
    from trader.domain.commands import CommandReceipt
    from trader.sdk import MMR

    calls = []
    client = SimpleNamespace(call=lambda method, body, kind: calls.append((method, body)) or CommandReceipt(
        body["command_id"], body["command_id"], "RESOLVED", {"state": "PAUSED"}, None, False))
    mmr = MMR.__new__(MMR)
    monkeypatch.setattr(MMR, "_typed_command", property(lambda self: client))
    monkeypatch.setattr(MMR, "experiment_status", lambda self: {"experiment": {"experiment_id": "exp-" + "c" * 20}})
    result = mmr.experiment_pause("risk")
    assert result.is_success() and calls[0][0] == "pause_experiment"
    assert calls[0][1]["experiment_id"] == "exp-" + "c" * 20 and ":" not in calls[0][1]["command_id"]
