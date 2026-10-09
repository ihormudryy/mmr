"""SP2c Plan 1 Task 1: the operator-only ai_paper.backtest_judge block (spec 6.1)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader.automation.ai_paper_config import AiPaperConfigError, load_ai_paper_config
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.strategy_key import split_strategy_key

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


def judge(section):
    return load_ai_paper_config({"backtest_judge": section}, trading_mode="paper").backtest_judge


def test_defaults_are_the_owner_numbers():
    config = load_ai_paper_config(None, trading_mode="paper").backtest_judge
    assert config == BacktestJudgeConfig()
    assert (config.evaluations_per_day, config.family_cooldown_sessions, config.max_active_deploys,
            config.deploy_expiry_sessions, config.max_cohort_points, config.shadow_warmup_sessions,
            config.strategy_allowlist) == (10, 10, 3, 20, 3, 5, ())


def test_a_full_block_loads_and_the_allowlist_is_exact():
    config = judge({"evaluations_per_day": 4, "family_cooldown_sessions": 12, "max_active_deploys": 2,
                    "deploy_expiry_sessions": 30, "max_cohort_points": 5, "shadow_warmup_sessions": 0,
                    "strategy_allowlist": [KEY]})
    assert (config.evaluations_per_day, config.max_cohort_points, config.strategy_allowlist) == (4, 5, (KEY,))
    assert config.allows(KEY) and not config.allows("strategies/opening_range_breakout.py:Other")


@pytest.mark.parametrize("key,value", [
    ("evaluations_per_day", 0), ("evaluations_per_day", 101), ("evaluations_per_day", True),
    ("evaluations_per_day", "10"), ("family_cooldown_sessions", 1.5), ("max_cohort_points", 11),
    ("shadow_warmup_sessions", -1), ("max_active_deploys", None),
    ("family_cooldown_sessions", 121), ("deploy_expiry_sessions", 121),
])
def test_a_bad_number_is_refused_with_its_key_path(key, value):
    with pytest.raises(AiPaperConfigError, match=f"ai_paper.backtest_judge.{key}"):
        judge({key: value})


def test_the_longest_session_counts_are_accepted():
    config = judge({"family_cooldown_sessions": 120, "deploy_expiry_sessions": 120})
    assert (config.family_cooldown_sessions, config.deploy_expiry_sessions) == (120, 120)


@pytest.mark.parametrize("allowlist", [
    "strategies/x.py:Foo", ["strategies/x.py"], ["x.py:Foo"], ["strategies/x.py:Foo", "strategies/x.py:Foo"],
    ["strategies/../x.py:Foo"], [7],
])
def test_a_bad_allowlist_is_refused(allowlist):
    with pytest.raises(AiPaperConfigError, match="strategy_allowlist"):
        judge({"strategy_allowlist": allowlist})


def test_an_unknown_key_is_refused():
    with pytest.raises(AiPaperConfigError, match="ai_paper.backtest_judge.evaluations_per_week: unknown key"):
        judge({"evaluations_per_week": 3})


def test_an_environment_override_is_refused(monkeypatch):
    monkeypatch.setenv("AI_PAPER_BACKTEST_JUDGE_EVALUATIONS_PER_DAY", "99")
    with pytest.raises(AiPaperConfigError, match="environment override refused"):
        load_ai_paper_config({}, trading_mode="paper")


def test_the_shipped_trader_yaml_has_the_block_with_an_empty_allowlist():
    raw = yaml.safe_load((REPO_ROOT / "config_defaults" / "trader.yaml").read_text())
    assert raw["ai_paper"]["backtest_judge"]["strategy_allowlist"] == []
    assert load_ai_paper_config(raw["ai_paper"], trading_mode="paper").backtest_judge == BacktestJudgeConfig()


def test_a_strategy_key_splits_into_path_and_class():
    assert split_strategy_key(KEY) == ("strategies/opening_range_breakout.py", "OpeningRangeBreakout")
    with pytest.raises(ValueError):
        split_strategy_key("strategies/opening_range_breakout.py")
