"""#215: the pycron IB health check runs its lookup on ib_async's own loop and reports a working IB as working."""
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
