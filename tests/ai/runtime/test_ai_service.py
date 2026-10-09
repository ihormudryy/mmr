"""SP2 Plan 5 Task 10: the entry point fails loudly and installs nothing it should not."""
import logging

import pytest

# Importing typed_rpc runs dictConfig, which replaces the root handlers (caplog's too). Do it before any test
# sets up caplog, so these tests do not depend on which test file imported it first.
import trader.messaging.typed_rpc  # noqa: F401
from tests.ai.fakes import config_text
from tests.rpc_identity_fixtures import write_keyset
from trader.ai_service import EXIT_REFUSED, ServiceSettings, build_engine, main, run_service


def settings(tmp_path, **controller):
    keys = tmp_path / "keys"
    write_keyset(keys)
    block = f"database_path: {tmp_path / 'ai' / 'ai.duckdb'}\n"
    path = tmp_path / "ai.yaml"
    path.write_text(config_text(extra_top_level=block))
    return ServiceSettings(config_path=str(path), keys_dir=str(keys), trader_address="tcp://127.0.0.1")


def test_build_engine_installs_the_paper_decision_engine(tmp_path):              # SP2 Plan 6 Task 6
    import datetime as dt

    from tests.ai.fakes import FakeClock, load_test_config
    from trader.ai.decision_engine import PaperDecisionEngine
    from trader.ai.replay import ReplayRecorder
    from trader.ai.store import AiStore
    from trader.ai_service import EngineDeps
    clock = FakeClock(dt.datetime(2026, 7, 17, 15, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    engine = build_engine(EngineDeps(load_test_config(tmp_path), None, None, clock, ReplayRecorder(store), store))
    assert isinstance(engine, PaperDecisionEngine)


def test_an_engine_factory_that_refuses_stops_the_service(tmp_path, caplog):
    from trader.ai_service import EngineNotInstalled

    def refuse(_deps):
        raise EngineNotInstalled("no decision engine is installed")
    caplog.set_level(logging.ERROR)
    code = run_service(settings(tmp_path), engine_factory=refuse, environ={"OPENROUTER_API_KEY": "test-only"})
    assert code == EXIT_REFUSED and "no decision engine is installed" in caplog.text
    assert "test-only" not in caplog.text


def test_missing_credentials_stop_the_service_before_any_trader_call(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    called = []
    code = run_service(settings(tmp_path), engine_factory=lambda deps: called.append(deps), environ={})
    assert code == EXIT_REFUSED and called == []


def test_a_missing_config_file_exits_loudly(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    monkeypatch.setenv("TRADER_TYPED_ADDRESS", "tcp://127.0.0.1")
    assert main(["--config", str(tmp_path / "missing.yaml")]) == EXIT_REFUSED
    assert "AI_CONFIG_NOT_FOUND" in caplog.text


def test_the_research_cycle_uses_the_cap_gated_gateway_and_is_built_only_when_enabled(tmp_path):  # SP2c Plan 4
    import datetime as dt
    from types import SimpleNamespace

    from tests.ai.fakes import FakeClock, load_test_config
    from tests.ai.research.rig import BLOCK
    from trader.ai.store import AiStore
    from trader.ai_service import build_research_cycle, build_session_slots

    clock = FakeClock(dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    clients, gateway = SimpleNamespace(lab=object(), research=object()), object()

    def build(config):
        return build_research_cycle(config, store=store, clock=clock, slots=build_session_slots(config),
                                    leadership=None, watch=None, clients=clients, gateway=gateway)
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    assert build(load_test_config(tmp_path / "off")) is None
    cycle = build(load_test_config(tmp_path / "on", extra_top_level=BLOCK))
    assert cycle._gateway is gateway and cycle._judge._gateway is gateway
    assert (cycle._lab, cycle._registry) == (clients.lab, clients.research)
