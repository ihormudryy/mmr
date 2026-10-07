"""SP1 Plan 6 Task 6: `mmr flatten`, the abort path, over a fake SDK and over the served composed stack."""
from __future__ import annotations

import io

import pytest

from trader import mmr_cli
from trader.mmr_cli import build_parser


class FakeSdk:
    def __init__(self, account="DU111111"):
        self.account = account
        self.calls = []
        self.states = []

    def account_id(self):
        return self.account

    def flatten(self, reason, command_id=None):
        body = {"command_id": command_id or "flatten-0123456789abcdef", "reason": reason}
        self.calls.append(("liquidate_account", body))
        return {"command_id": body["command_id"], "state": "OUTCOME_UNKNOWN", "error_code": "CLOSE_PENDING",
                "outcome": {"close_root_id": "flatten-0123456789abcdef"}}

    def wait_flat(self, command_id, timeout=300.0, *, sleep=None, clock=None, poll=2.0):
        from trader.sdk import MMR
        return MMR.wait_flat(self, command_id, timeout, sleep=sleep or (lambda s: None), clock=clock, poll=poll)

    def flat_state(self, command_id):
        return dict(self.states.pop(0) if len(self.states) > 1 else self.states[0])


@pytest.fixture
def sdk():
    return FakeSdk()


@pytest.fixture
def cli(sdk, capsys, monkeypatch):
    def run(line, stdin="", target=None, clock=None):
        monkeypatch.setattr(mmr_cli, "_json_mode", False)
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        args = build_parser().parse_args(line.split())
        run.code = 0
        try:
            mmr_cli._handle_flatten(target or sdk, args, sleep=lambda s: None, clock=clock)
        except SystemExit as exc:
            run.code = exc.code
        return capsys.readouterr().out
    return run


def _state(command_state="RESOLVED", positions=(), working=()):
    return {"command": {"state": command_state, "error_code": None}, "positions": list(positions),
            "working_orders": list(working), "capture_error": None}


def test_flatten_needs_confirmation(cli, sdk):
    out = cli("flatten --reason abort", stdin="no\n")
    assert "Type FLATTEN" in out and sdk.calls == [] and cli.code == 1


def test_typing_flatten_confirms(cli, sdk):
    cli("flatten --reason abort", stdin="FLATTEN\n")
    assert [method for method, _ in sdk.calls] == ["liquidate_account"]


def test_flatten_sends_liquidate_account_with_a_fresh_command_id(cli, sdk):
    out = cli("flatten --reason abort --yes")
    (method, body), = sdk.calls
    assert method == "liquidate_account" and body["reason"] == "abort" and ":" not in body["command_id"]
    assert "flatten-0123456789abcdef" in out


def test_the_sdk_command_id_is_fresh_and_colon_free():
    from types import SimpleNamespace
    from trader.sdk import MMR
    sent = []
    sdk = object.__new__(MMR)
    sdk._typed_command_client = SimpleNamespace(call=lambda method, body, _t: sent.append(body) or {})
    sdk._typed_query_client = object()
    MMR.flatten(sdk, "abort")
    MMR.flatten(sdk, "abort")
    assert sent[0]["command_id"] != sent[1]["command_id"]
    assert all(b["command_id"].startswith("flatten-") and ":" not in b["command_id"] for b in sent)


def test_flatten_wait_reports_flat_only_on_broker_evidence(cli, sdk):
    sdk.states = [_state(working=[{"order_entity_id": "x"}])]                    # resolved, but an order works
    ticks = iter(range(0, 1000, 100))
    out = cli("flatten --reason abort --yes --wait", clock=lambda: next(ticks))
    assert "FLAT" not in out.replace("NOT FLAT", "") and "NOT FLAT" in out and cli.code == 1
    sdk.states = [_state("OUTCOME_UNKNOWN"), _state()]
    out = cli("flatten --reason abort --yes --wait")
    assert out.strip().splitlines()[-1] == "FLAT" and cli.code == 0


def test_flatten_refuses_a_live_account(cli):
    live = FakeSdk(account="U1234567")
    out = cli("flatten --reason abort --yes", target=live)
    assert "LIVE_REFUSED" in out and live.calls == [] and cli.code == 1


def test_flatten_over_the_served_stack_reaches_flat(tmp_path, loop_thread, monkeypatch, capsys):
    from tests.rpc_identity_fixtures import make_identities, write_keyset
    from tests.sp1_acceptance.test_cli_acceptance import _sdk
    from tests.sp1_fixtures import CONID, MSFT, served_stack

    keys_dir = tmp_path / "keys"
    keys = write_keyset(keys_dir)
    served = served_stack(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=keys))
    sdk = _sdk(served, keys_dir)
    try:
        served.sim.auto_fill()
        served.sim.quote(CONID, 229.9, 230.0)
        served.sim.quote(MSFT, 499.9, 500.0)
        served.sim.held.update({CONID: 3.0, MSFT: 1.0})
        served.sim.promote()
        monkeypatch.setattr(mmr_cli, "_json_mode", False)
        args = build_parser().parse_args("flatten --reason test --yes --wait".split())
        mmr_cli._handle_flatten(sdk, args, sleep=served.advance_and_promote)
        out = capsys.readouterr().out
        assert out.strip().splitlines()[-1] == "FLAT"
        assert {c: q for c, q in served.sim.held.items() if q} == {}
        assert [p[1] for p in served.sim.placed] == ["MKT", "MKT"]
    finally:
        for client in (sdk._typed_query_client, sdk._typed_command_client):
            if client is not None:
                client.close()
        served.close()


@pytest.fixture
def loop_thread():
    from tests.sp1_fixtures import LoopThread
    lt = LoopThread()
    yield lt
    lt.stop()
