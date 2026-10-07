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


def test_without_an_engine_the_service_refuses_to_start(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    code = run_service(settings(tmp_path), engine_factory=build_engine, environ={"OPENROUTER_API_KEY": "test-only"})
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
