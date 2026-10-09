"""One source of truth for where the research server is: RESEARCH_TYPED_ADDRESS plus the controller config ports."""
import asyncio

import pytest

import trader.messaging.typed_rpc  # noqa: F401  (runs dictConfig before any caplog)
from tests.ai.runtime.test_ai_service import settings as service_settings
from tests.rpc_identity_fixtures import write_keyset
from trader.ai.rpc_clients import AiRpcClients
from trader.ai_service import DEFAULT_TRADER_ADDRESS, ServiceSettings, main, serve


def test_the_default_research_address_is_the_host_loopback():
    assert DEFAULT_TRADER_ADDRESS == "tcp://127.0.0.1"
    assert ServiceSettings().research_address == "tcp://127.0.0.1"


@pytest.mark.parametrize("env,expected", [({}, "tcp://127.0.0.1"), ({"RESEARCH_TYPED_ADDRESS": "tcp://research"},
                                                                    "tcp://research")])
def test_main_reads_the_research_address_from_the_environment(monkeypatch, tmp_path, env, expected):
    monkeypatch.delenv("RESEARCH_TYPED_ADDRESS", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    seen = []
    monkeypatch.setattr("trader.ai_service.run_service", lambda settings: seen.append(settings) or 0)
    assert main(["--config", str(tmp_path / "ai.yaml")]) == 0
    assert seen[0].research_address == expected


def test_serve_connects_the_research_server_at_its_own_address_and_the_config_ports(tmp_path, monkeypatch):
    connected = []

    def connect(**kwargs):
        connected.append(kwargs)
        raise RuntimeError("stop after connect")
    monkeypatch.setattr(AiRpcClients, "connect", staticmethod(connect))
    settings = service_settings(tmp_path)
    research = ServiceSettings(settings.config_path, settings.keys_dir, "tcp://trader", "tcp://research")
    with pytest.raises(RuntimeError, match="stop after connect"):
        asyncio.run(serve(research, engine_factory=lambda deps: None, stop=asyncio.Event(),
                          environ={"OPENROUTER_API_KEY": "test-only"}))
    (kwargs,) = connected
    assert (kwargs["address"], kwargs["research_address"]) == ("tcp://trader", "tcp://research")
    assert (kwargs["query_port"], kwargs["command_port"]) == (42101, 42102)
    assert (kwargs["research_query_port"], kwargs["research_command_port"]) == (42106, 42107)


def test_connect_signs_as_ai_research_to_the_research_ports_and_to_the_trader_ports(tmp_path):
    keys = tmp_path / "keys"
    write_keyset(keys)
    clients = AiRpcClients.connect(
        keys_dir=str(keys), address="tcp://127.0.0.1", query_port=42101, command_port=42102,
        research_address="tcp://127.0.0.1", research_query_port=42106, research_command_port=42107, timeout=5.0)
    try:
        lab, trader = clients.lab._routes, clients.research._routes
        reached = {"submit_evaluation": lab["submit_evaluation"], "get_evaluation": lab["get_evaluation"],
                   "attest_from_judgment": lab["attest_from_judgment"],
                   "record_backtest_judgment": trader["record_backtest_judgment"],
                   "get_backtest_judgment": trader["get_backtest_judgment"]}
        assert {name: (c.server, c.address, c.identity.principal) for name, c in reached.items()} == {
            "submit_evaluation": ("research", "tcp://127.0.0.1:42107", "ai_research"),
            "get_evaluation": ("research", "tcp://127.0.0.1:42106", "ai_research"),
            "attest_from_judgment": ("research", "tcp://127.0.0.1:42107", "ai_research"),
            "record_backtest_judgment": ("trader", "tcp://127.0.0.1:42102", "ai_research"),
            "get_backtest_judgment": ("trader", "tcp://127.0.0.1:42101", "ai_research")}
    finally:
        clients.close()
