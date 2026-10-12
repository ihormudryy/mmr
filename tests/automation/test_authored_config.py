"""SP3 Plan 3 Task 2: the owner block ai_paper.authored_strategies (spec 6, 10 Config; rulings 3-4)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader.automation.ai_paper_config import AiPaperConfigError, load_ai_paper_config
from trader.automation.authored_config import AuthoredStrategiesConfig

ROOT = Path(__file__).resolve().parents[2]


def load(block):
    return load_ai_paper_config({"enabled": True, "authored_strategies": block}, trading_mode="paper")


def test_the_switch_is_off_by_default():
    config = load_ai_paper_config({}, trading_mode="paper").authored_strategies
    assert config == AuthoredStrategiesConfig(enabled=False, max_active=3, bar_sizes=("1 min", "5 mins", "15 mins"))


def test_the_block_loads():
    config = load({"enabled": True, "max_active": 2, "bar_sizes": ["5 mins"]}).authored_strategies
    assert (config.enabled, config.max_active, config.bar_sizes) == (True, 2, ("5 mins",))


@pytest.mark.parametrize("block, key", [
    ({"enabled": "yes"}, "authored_strategies.enabled"),
    ({"max_active": 0}, "authored_strategies.max_active"),
    ({"max_active": 4}, "authored_strategies.max_active"),       # C2: no more than the 3 live sandbox sessions
    ({"max_active": True}, "authored_strategies.max_active"),
    ({"bar_sizes": []}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": "5 mins"}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["1 hour"]}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["fortnight"]}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["5 mins", "5 mins"]}, "authored_strategies.bar_sizes"),
    ({"color": "red"}, "authored_strategies.color"),
    ("on", "authored_strategies"),
])
def test_a_bad_block_fails_load_and_names_the_key(block, key):
    with pytest.raises(AiPaperConfigError, match=key.replace(".", r"\.")):
        load(block)


def test_the_switch_needs_ai_paper_enabled():                                          # ruling 4
    with pytest.raises(AiPaperConfigError, match=r"requires ai_paper\.enabled"):
        load_ai_paper_config({"enabled": False, "authored_strategies": {"enabled": True}}, trading_mode="paper")


def test_an_environment_override_still_fails_load(monkeypatch):                        # spec 10 Config
    monkeypatch.setenv("AI_PAPER_AUTHORED_STRATEGIES_ENABLED", "true")
    with pytest.raises(AiPaperConfigError, match="environment override refused"):
        load({"enabled": False})


def test_the_shipped_template_loads_with_the_switch_off():
    raw = yaml.safe_load((ROOT / "config_defaults" / "trader.yaml").read_text())
    assert load_ai_paper_config(raw["ai_paper"], trading_mode="paper").authored_strategies == \
        AuthoredStrategiesConfig()
