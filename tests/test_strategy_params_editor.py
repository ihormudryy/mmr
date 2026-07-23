"""Params drawer editor: YAML/live params + class tunables for /cc."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_resolve_strategy_params_editor_merges_yaml_params_and_class_tunables(
    tmp_path, monkeypatch,
):
    from web import app as web_app

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "demo.py").write_text(
        "from trader.trading.strategy import Strategy\n"
        "class DemoStrat(Strategy):\n"
        "    EMA_PERIOD = 20\n"
        "    BAND_MULT = 2.5\n"
        "    def on_prices(self, prices): return None\n"
    )
    config = tmp_path / "strategy_runtime.yaml"
    config.write_text(yaml.safe_dump({
        "strategies": [{
            "name": "demo_live",
            "module": "strategies.demo",
            "class_name": "DemoStrat",
            "bar_size": "1 min",
            "params": {"EMA_PERIOD": 15},
        }],
    }))
    monkeypatch.setattr(web_app, "_STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(web_app, "_STRATEGY_CONFIG_PATH", config)

    class _EmptyManage:
        def strategy_query(self, method, body=None):
            raise ConnectionError("strategy down")

        def trader_query(self, method, body=None):
            return {}

    monkeypatch.setattr(web_app, "get_manage_client", lambda: _EmptyManage())

    payload = web_app.resolve_strategy_params_editor("demo_live")
    assert payload is not None
    assert payload["strategy_name"] == "demo_live"
    assert payload["class_name"] == "DemoStrat"
    assert payload["params"] == {"EMA_PERIOD": 15}
    assert payload["tunables"]["EMA_PERIOD"] == 20
    assert payload["tunables"]["BAND_MULT"] == 2.5


def test_resolve_strategy_params_editor_prefers_live_list_strategies(
    tmp_path, monkeypatch,
):
    from web import app as web_app

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "demo.py").write_text(
        "from trader.trading.strategy import Strategy\n"
        "class DemoStrat(Strategy):\n"
        "    EMA_PERIOD = 20\n"
        "    def on_prices(self, prices): return None\n"
    )
    config = tmp_path / "strategy_runtime.yaml"
    config.write_text(yaml.safe_dump({
        "strategies": [{
            "name": "demo_live",
            "class_name": "DemoStrat",
            "params": {"EMA_PERIOD": 10},
        }],
    }))
    monkeypatch.setattr(web_app, "_STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(web_app, "_STRATEGY_CONFIG_PATH", config)

    class _LiveManage:
        def strategy_query(self, method, body=None):
            assert method == "list_strategies"
            return {"strategies": [{
                "name": "demo_live",
                "class_name": "DemoStrat",
                "state": "RUNNING",
                "params": {"EMA_PERIOD": 42},
            }]}

        def trader_query(self, method, body=None):
            return {}

    monkeypatch.setattr(web_app, "get_manage_client", lambda: _LiveManage())

    payload = web_app.resolve_strategy_params_editor("demo_live")
    assert payload["params"] == {"EMA_PERIOD": 42}


def test_resolve_strategy_params_editor_unknown_name_returns_none(
    tmp_path, monkeypatch,
):
    from web import app as web_app

    config = tmp_path / "strategy_runtime.yaml"
    config.write_text("strategies: []\n")
    monkeypatch.setattr(web_app, "_STRATEGY_CONFIG_PATH", config)
    monkeypatch.setattr(web_app, "_STRATEGIES_DIR", tmp_path / "strategies")
    (tmp_path / "strategies").mkdir()

    class _EmptyManage:
        def strategy_query(self, method, body=None):
            return {"strategies": []}

        def trader_query(self, method, body=None):
            return {}

    monkeypatch.setattr(web_app, "get_manage_client", lambda: _EmptyManage())
    assert web_app.resolve_strategy_params_editor("missing") is None


def test_state_payload_includes_params(tmp_path):
    from trader.strategy.strategy_runtime import StrategyRuntime
    from trader.trading.strategy import StrategyState

    class _Strat:
        def __init__(self):
            self.name = "alpha"
            self.state = StrategyState.RUNNING
            self.bar_size = None
            self.class_name = "Alpha"
            self.conids = []
            self.universe = None
            self.last_error = None

        @property
        def params(self):
            return {"EMA_PERIOD": 12}

    # Minimal runtime shell — only needs get_strategy / _state_payload.
    rt = StrategyRuntime.__new__(StrategyRuntime)
    rt.strategy_implementations = [_Strat()]
    payload = StrategyRuntime._state_payload(rt, "alpha", control_revision=3)
    assert payload["params"] == {"EMA_PERIOD": 12}
    assert payload["strategy_state"] == "RUNNING"
