"""Plan 3 Task 3: the ai_paper owner ceiling block."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from trader.automation.ai_paper_config import AiPaperConfig, AiPaperConfigError, load_ai_paper_config
from trader.automation.risk_limits import PAPER_LIMITS, STEADY_LIMITS
from trader.config import MMRConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(section, mode="paper", **kw):
    return load_ai_paper_config(section, trading_mode=mode, **kw)


def test_missing_section_and_missing_keys_take_paper_limits():
    assert load(None).limits_ceiling == PAPER_LIMITS
    assert load({"limits_ceiling": {}}).limits_ceiling == PAPER_LIMITS
    assert load({"limits_ceiling": {"max_positions": 2}}).limits_ceiling == replace(PAPER_LIMITS, max_positions=2)


def test_defaults():
    config = load(None)
    assert (config.enabled, config.styles, config.experiment_kill_drawdown_pct, config.experiment_kill_basis,
            config.broker_outage_pause_seconds, config.acceptance_probe) == (
        False, ("intraday_long",), None, "start", 300, False)


@pytest.mark.parametrize("gross", [0.06, 0.10, 0.15])
def test_gross_may_rise_to_fifteen_percent(gross):
    assert load({"limits_ceiling": {"gross_fraction": gross}}).limits_ceiling.gross_fraction == gross


@pytest.mark.parametrize("key,value", [
    ("gross_fraction", 0.1501), ("position_fraction", 0.051), ("trade_risk_fraction", 0.0021),
    ("daily_loss_fraction", 0.0051), ("max_positions", 4), ("drawdown_fraction", 0.031),
    ("max_pending_entry_orders", 4)])
def test_any_other_field_above_today_fails_load(key, value):
    with pytest.raises(AiPaperConfigError, match=f"limits_ceiling.{key}"):
        load({"limits_ceiling": {key: value}})


@pytest.mark.parametrize("value", [0, -0.01, ".nan", ".inf", '"0.05"', True, None, [0.05]])
def test_wrong_type_non_finite_or_non_positive_fails_load(value):
    text = value if isinstance(value, str) else json.dumps(value)
    raw = yaml.safe_load(f"limits_ceiling: {{position_fraction: {text}}}")
    with pytest.raises(AiPaperConfigError, match="limits_ceiling.position_fraction"):
        load(raw)


def test_structural_problem_fails_load():
    with pytest.raises(AiPaperConfigError, match="POSITION_ABOVE_GROSS"):
        load({"limits_ceiling": {"gross_fraction": 0.03}})


def test_unknown_ceiling_key_and_unknown_section_key_fail_load():
    with pytest.raises(AiPaperConfigError, match="limits_ceiling.gross"):
        load({"limits_ceiling": {"gross": 0.05}})
    with pytest.raises(AiPaperConfigError, match="ai_paper.ceiling"):
        load({"ceiling": {}})


@pytest.mark.parametrize("section", [[], "on", 1])
def test_section_must_be_a_mapping(section):
    with pytest.raises(AiPaperConfigError, match="ai_paper"):
        load(section)


def test_telegram_is_left_for_plan_five_and_kept_raw():
    cfg = load({"telegram": {"enabled": False}})
    assert cfg.raw_section["telegram"] == {"enabled": False}
    with pytest.raises(TypeError):
        cfg.raw_section["x"] = 1


@pytest.mark.parametrize("styles", [[], ["swing_long"], ["intraday_long", "intraday_long"], "intraday_long", [1]])
def test_styles_must_be_supported_unique_and_non_empty(styles):
    with pytest.raises(AiPaperConfigError, match="styles"):
        load({"styles": styles})


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_enabled_must_be_a_bool(value):
    with pytest.raises(AiPaperConfigError, match="enabled"):
        load({"enabled": value})


def test_enabled_requires_paper_trading_mode():
    with pytest.raises(AiPaperConfigError, match="paper"):
        load({"enabled": True}, mode="live")


@pytest.mark.parametrize("pct", [0, 100, -5, ".nan", '"20"', True])
def test_kill_line_must_be_a_percent_strictly_between_0_and_100(pct):
    text = pct if isinstance(pct, str) else json.dumps(pct)
    raw = yaml.safe_load(f"experiment_kill_drawdown_pct: {text}")
    with pytest.raises(AiPaperConfigError, match="experiment_kill_drawdown_pct"):
        load(raw)


def test_kill_basis_is_start_or_peak():
    assert load({"experiment_kill_basis": "start"}).experiment_kill_basis == "start"
    assert load({"experiment_kill_basis": "peak"}).experiment_kill_basis == "peak"
    with pytest.raises(AiPaperConfigError, match="experiment_kill_basis"):
        load({"experiment_kill_basis": "high"})


RAISED = replace(STEADY_LIMITS, drawdown_fraction=0.10)   # test-only code maximum (spec 6)


@pytest.mark.parametrize("kill", [None, 12.0])
def test_drawdown_guard_fails_with_kill_off_or_looser(kill):
    with pytest.raises(AiPaperConfigError, match="experiment_kill_drawdown_pct"):
        load({"limits_ceiling": {"drawdown_fraction": 0.05}, "experiment_kill_drawdown_pct": kill},
             code_maximum=RAISED)


def test_drawdown_guard_loads_with_a_tighter_kill_line():
    assert load({"limits_ceiling": {"drawdown_fraction": 0.05}, "experiment_kill_drawdown_pct": 4.0},
                code_maximum=RAISED).experiment_kill_drawdown_pct == 4.0


@pytest.mark.parametrize("kill", [None, 20.0])
def test_todays_legal_combinations(kill):                  # spec 5.4: 3% with kill off or 20%
    load({"limits_ceiling": {"drawdown_fraction": 0.03}, "experiment_kill_drawdown_pct": kill})


@pytest.mark.parametrize("name", ["AI_PAPER_ENABLED", "MMR_AI_PAPER_LIMITS_CEILING",
                                  "AI_PAPER_MODEL_BUDGET_USD_PER_DAY"])
def test_prefixed_environment_override_is_refused(monkeypatch, name):          # R22, owner answer
    monkeypatch.setenv(name, "1")
    with pytest.raises(AiPaperConfigError, match=f"environment override refused: {name}"):
        load({"enabled": True})


@pytest.mark.parametrize("name", ["GROSS_FRACTION", "EXPERIMENT_KILL_DRAWDOWN_PCT", "BROKER_OUTAGE_PAUSE_SECONDS"])
def test_bare_environment_name_is_ignored_loudly(monkeypatch, caplog, name):
    monkeypatch.delenv(name, raising=False)
    expected = load({"enabled": True})
    monkeypatch.setenv(name, "0.15")
    with caplog.at_level(logging.WARNING):
        assert load({"enabled": True}) == expected                               # unchanged
    assert name in caplog.text and "ignored" in caplog.text


@pytest.mark.parametrize("value", [59, 3601, 300.0, True, "300", None])
def test_outage_pause_seconds_is_a_bounded_integer(value):                     # Plan 4 K23
    with pytest.raises(AiPaperConfigError, match="broker_outage_pause_seconds"):
        load({"broker_outage_pause_seconds": value})


def test_model_budget_defaults_to_2000():                                        # SP2 Plan 2 Ruling 20
    assert load(None).model_budget_usd_per_day == 2000.0
    assert load({"model_budget_usd_per_day": 15}).model_budget_usd_per_day == 15.0
    assert load({"model_budget_usd_per_day": 0}).model_budget_usd_per_day == 0.0


@pytest.mark.parametrize("value", [True, False, "2000", -1, -0.01, float("nan"), float("inf"), None])
def test_model_budget_refuses_bool_text_negative_and_nan(value):
    with pytest.raises(AiPaperConfigError, match="model_budget_usd_per_day: must be a finite number >= 0"):
        load({"model_budget_usd_per_day": value})


def test_acceptance_probe_defaults_off_and_loads_true_in_paper():                 # Plan 6 ruling 23
    assert load(None).acceptance_probe is False and load({}).acceptance_probe is False
    assert load({"acceptance_probe": True}).acceptance_probe is True


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_acceptance_probe_must_be_a_bool(value):
    with pytest.raises(AiPaperConfigError, match="acceptance_probe"):
        load({"acceptance_probe": value})


def test_acceptance_probe_is_refused_outside_paper_mode():
    with pytest.raises(AiPaperConfigError, match="acceptance_probe.*paper"):
        load({"acceptance_probe": True}, mode="live")


def test_acceptance_probe_true_does_not_fail_mmr_config_load(tmp_path):          # the key must not stop trader_service
    path = tmp_path / "trader.yaml"
    path.write_text("trading_mode: paper\nai_paper:\n  acceptance_probe: true\n")
    assert MMRConfig.from_yaml(str(path)).ai_paper.acceptance_probe is True


def test_mmr_config_without_the_block_gets_the_defaults(tmp_path):
    path = tmp_path / "trader.yaml"
    path.write_text("trading_mode: paper\n")
    assert MMRConfig.from_yaml(str(path)).ai_paper == AiPaperConfig()


def test_bad_block_fails_mmr_config_load(tmp_path):
    path = tmp_path / "trader.yaml"
    path.write_text("trading_mode: paper\nai_paper:\n  limits_ceiling: {max_positions: 9}\n")
    with pytest.raises(AiPaperConfigError):
        MMRConfig.from_yaml(str(path))


def test_template_block_loads():
    raw = yaml.safe_load((REPO_ROOT / "config_defaults" / "trader.yaml").read_text())
    assert load(raw["ai_paper"]) == AiPaperConfig(raw_section=MappingProxyType(raw["ai_paper"]))
