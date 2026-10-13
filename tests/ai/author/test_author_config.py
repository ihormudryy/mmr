from pathlib import Path

import pytest
import yaml

from tests.ai.author.helpers import author_text, load
from tests.ai.fakes import config_text
from trader.ai.config import (AUTHOR_ROLE, OPTIONAL_ROLE_NAMES, REQUIRED_ROLE_NAMES, ROLE_NAMES, AiConfigError,
                              authored_universe, check_credentials)


def test_role_names_add_author_as_the_only_optional_role():
    assert REQUIRED_ROLE_NAMES == ("orchestrator", "jev") and OPTIONAL_ROLE_NAMES == ("author",)
    assert ROLE_NAMES == ("orchestrator", "jev", "author") and AUTHOR_ROLE == "author"


def test_no_author_role_loads_with_authoring_off(tmp_path):
    config = load(tmp_path, config_text())
    assert config.authoring_enabled is False and AUTHOR_ROLE not in config.roles
    assert (config.authoring.run_after_close_minutes, config.authoring.call_timeout_seconds) == (60, 300.0)


def test_an_empty_author_model_is_authoring_off_not_role_model_missing(tmp_path):     # review focus 2
    config = load(tmp_path, author_text(model="", price=False, research=""))
    assert config.authoring_enabled is False and AUTHOR_ROLE not in config.roles
    check_credentials(config, {"OPENROUTER_API_KEY": "sk-test"})                      # names only the active roles


def test_a_set_author_model_turns_authoring_on_with_its_own_timeout(tmp_path):
    config = load(tmp_path, author_text(authoring="authoring:\n  call_timeout_seconds: 240\n"))
    assert config.authoring_enabled is True
    assert config.role(AUTHOR_ROLE).call_timeout_seconds == 240.0                   # above the 60 s deadline
    assert config.budget.decision_deadline_seconds == 60.0
    assert config.role("orchestrator").call_timeout_seconds == 45.0


def test_the_author_timeout_is_not_set_in_the_role(tmp_path):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(role_extra=", call_timeout_seconds: 45"))
    assert exc.value.code == "AUTHOR_TIMEOUT_IN_ROLE"


def test_a_bad_author_model_id_is_refused_at_load(tmp_path):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(model="bad model"))
    assert exc.value.code == "AUTHOR_MODEL_INVALID"
    assert load(tmp_path, author_text(model="vendor/author-1")).authoring_enabled is True


def test_the_author_model_pattern_is_the_traders_jev_model_pattern():
    from trader.ai.config import TRADER_MODEL_ID
    from trader.automation.backtest_judge_wire import JEV_MODEL
    assert TRADER_MODEL_ID.pattern == JEV_MODEL.pattern


def test_a_set_author_model_needs_a_price_row(tmp_path):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(price=False))
    assert exc.value.code == "AUTHOR_PRICE_MISSING"


def test_authoring_needs_the_research_cycle(tmp_path):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(research=""))
    assert exc.value.code == "AUTHORING_NEEDS_RESEARCH"


def test_an_unsupported_author_backend_fails(tmp_path):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(backend="nope"))
    assert exc.value.code == "ROLE_BACKEND_UNSUPPORTED"


@pytest.mark.parametrize("block", ["authoring:\n  run_after_close_minutes: 421\n",
                                   "authoring:\n  run_after_close_minutes: -1\n",
                                   "authoring:\n  call_timeout_seconds: 0\n",
                                   "authoring:\n  call_timeout_seconds: 601\n",
                                   "authoring:\n  surprise: 1\n"])
def test_bad_authoring_blocks_fail_load(tmp_path, block):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, author_text(authoring=block))
    assert exc.value.code == "AI_CONFIG_INVALID"


def test_authoring_is_in_the_digest(tmp_path):
    base = load(tmp_path, author_text()).digest()
    assert load(tmp_path, author_text(authoring="authoring:\n  run_after_close_minutes: 90\n")).digest() != base


def test_the_authored_universe_is_the_first_by_name(tmp_path):                       # ruling 6
    config = load(tmp_path, author_text())
    assert authored_universe(config.research) == ("a_small", (1, 2, 3, 4, 5, 6, 7, 8))


def test_the_shipped_ai_yaml_has_authoring_off():
    raw = yaml.safe_load((Path(__file__).resolve().parents[3] / "config_defaults" / "ai.yaml").read_text())
    assert raw["roles"]["author"]["model"] == "" and "call_timeout_seconds" not in raw["roles"]["author"]
    assert raw["authoring"] == {"run_after_close_minutes": 60, "call_timeout_seconds": 300}
