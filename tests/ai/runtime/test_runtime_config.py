"""SP2 Plan 5 Task 1: the controller section of ai.yaml."""
from pathlib import Path

import pytest
import yaml

from tests.ai.fakes import config_text, write_config
from trader.ai.config import AiConfigError, ControllerConfig, load_ai_config

TEMPLATE = Path(__file__).resolve().parents[3] / "config_defaults" / "ai.yaml"


def load(directory: Path, block: str = ""):
    directory.mkdir(parents=True, exist_ok=True)
    return load_ai_config(str(write_config(directory, config_text(extra_top_level=block))))


def test_defaults_follow_the_index(tmp_path):
    c = load(tmp_path).controller
    assert (c.lease_seconds, c.renew_seconds, c.entry_slot_minutes, c.position_slot_minutes) == (60, 20, 15, 15)
    assert (c.trader_query_port, c.trader_command_port, c.decision_ttl_seconds) == (42101, 42102, 300)
    assert (c.slot_start_grace_seconds, c.signal_max_age_seconds, c.not_found_settle_seconds) == (120, 300, 120)


def test_values_are_read_and_change_the_digest(tmp_path):
    plain = load(tmp_path / "a")
    custom = load(tmp_path / "b", "controller: {entry_slot_minutes: 30, signal_poll_seconds: 0.5}")
    assert (custom.controller.entry_slot_minutes, custom.controller.signal_poll_seconds) == (30, 0.5)
    assert plain.digest() != custom.digest()


@pytest.mark.parametrize("block,code", [
    ("controller: {renew_seconds: 30}", "CONTROLLER_RENEW_TOO_SLOW"),
    ("controller: {entry_slot_minutes: 5, slot_start_grace_seconds: 300}", "CONTROLLER_GRACE_TOO_LONG"),
    ("controller: {decision_ttl_seconds: 901}", "AI_CONFIG_INVALID"),
    ("controller: {lease_seconds: true}", "AI_CONFIG_INVALID"),
    ("controller: {not_found_settle_seconds: 30}", "AI_CONFIG_INVALID"),
    ("controller: {unknown_key: 1}", "AI_CONFIG_INVALID"),
])
def test_bad_controller_config_fails_loudly(tmp_path, block, code):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, block)
    assert exc.value.code == code


def test_shipped_template_lists_the_controller_defaults():
    shipped = yaml.safe_load(TEMPLATE.read_text())["controller"]
    assert shipped == ControllerConfig().model_dump()
