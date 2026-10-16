"""#215, #224: the pycron IB health check reports a working IB as working, also from a running event loop."""
from __future__ import annotations

import asyncio

from trader.tools import trader_check


class _Ib:
    def __init__(self):
        self.ran = []

    def run(self, awaitable):
        self.ran.append(awaitable)
        return asyncio.new_event_loop().run_until_complete(awaitable)


class _IbRx:
    """Stands in for IBAIORx: get_conid only completes on the loop `ib.run` drives."""

    def __init__(self, *args):
        self.ib = _Ib()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    async def get_conid(self, symbols):
        return ["AMD contract"]


def test_a_working_ib_passes_and_the_lookup_runs_on_ib_asyncs_loop(monkeypatch, tmp_path):
    fakes = []
    monkeypatch.setattr(trader_check, "IBAIORx", lambda *a: fakes.append(_IbRx()) or fakes[-1])
    assert trader_check.test_platform("ib-gateway", 4004, 100, str(tmp_path / "mmr.duckdb")) is True
    assert len(fakes[0].ib.ran) == 1


def test_an_ib_without_contract_details_fails(monkeypatch, tmp_path):
    class Empty(_IbRx):
        async def get_conid(self, symbols):
            return []
    monkeypatch.setattr(trader_check, "IBAIORx", Empty)
    assert trader_check.test_platform("ib-gateway", 4004, 100, str(tmp_path / "mmr.duckdb")) is False


class _Completed:
    def __init__(self, returncode):
        self.returncode = returncode


def _fail_in_process(*_args):
    raise AssertionError("the IB check must not run inside the caller's process")


def _record_child(calls, returncode=0):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _Completed(returncode)
    return run


def test_the_health_check_works_inside_a_running_event_loop(monkeypatch):
    """#224: pycron calls the check from its running loop, where IB's blocking connect() always failed."""
    calls = []
    monkeypatch.setattr(trader_check, "test_platform_config", _fail_in_process)
    monkeypatch.setattr(trader_check.subprocess, "run", _record_child(calls))

    async def from_pycron():
        return trader_check.health_check("~/.config/mmr/trader.yaml")

    assert asyncio.run(from_pycron()) is True
    command, kwargs = calls[0]
    assert command[1:] == ["-m", "trader.tools.trader_check", "--config", "~/.config/mmr/trader.yaml"]
    assert kwargs["timeout"] == trader_check.HEALTH_CHECK_TIMEOUT_SECONDS


def test_a_failing_child_check_fails_the_health_check(monkeypatch):
    calls = []
    monkeypatch.setattr(trader_check.subprocess, "run", _record_child(calls, returncode=1))
    assert trader_check.health_check("cfg.yaml") is False
    assert len(calls) == 1                                    # no second connect with the same client id


def test_a_child_check_that_hangs_fails_the_health_check(monkeypatch):
    def hangs(command, **kwargs):
        raise trader_check.subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(trader_check.subprocess, "run", hangs)
    assert trader_check.health_check("cfg.yaml") is False


def test_the_command_line_check_exits_non_zero_when_it_fails(monkeypatch):
    from click.testing import CliRunner
    monkeypatch.setattr(trader_check, "test_platform_config", lambda config: False)
    assert CliRunner().invoke(trader_check.main, ["--config", "cfg.yaml"]).exit_code == 1
    monkeypatch.setattr(trader_check, "test_platform_config", lambda config: True)
    assert CliRunner().invoke(trader_check.main, ["--config", "cfg.yaml"]).exit_code == 0
