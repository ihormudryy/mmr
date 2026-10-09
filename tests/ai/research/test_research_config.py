import pytest

from tests.ai.fakes import config_text, write_config
from trader.ai.config import AiConfigError, load_ai_config

UNIVERSE = "[265598, 272093, 1003, 1004, 1005, 1006, 1007, 1008]"


def load(tmp_path, block):
    return load_ai_config(str(write_config(tmp_path, config_text(extra_top_level=block))))


def research(**fields):
    body = {"enabled": "true", "strategy_keys": '["strategies/time_of_day.py:TimeOfDay"]',
            "universes": "{us_eight: " + UNIVERSE + "}", **fields}
    return "research:\n" + "".join(f"  {k}: {v}\n" for k, v in body.items())


def test_research_is_off_by_default_and_in_the_digest(tmp_path):
    plain = load(tmp_path, "")
    assert plain.research.enabled is False and plain.controller.research_command_port == 42107
    assert load(tmp_path, research()).digest() != plain.digest()


@pytest.mark.parametrize("fields,code", [
    ({"universes": "{tiny: [1, 2, 3]}"}, "RESEARCH_UNIVERSE_INVALID"),
    ({"universes": "{dup: [1, 1, 3, 4, 5, 6, 7, 8]}"}, "RESEARCH_UNIVERSE_INVALID"),
    ({"bar_sizes": '["1 hour"]'}, "RESEARCH_BAR_SIZE_TOO_LONG"),
    ({"bar_sizes": '["7 mins"]'}, "RESEARCH_BAR_SIZE_INVALID"),
    ({"strategy_keys": "[]"}, "RESEARCH_MENU_EMPTY"),
    ({"strategy_keys": '["strategies/x.py:A", "strategies/x.py:A"]'}, "RESEARCH_DUPLICATE_STRATEGY"),
])
def test_bad_research_blocks_are_refused_at_load(tmp_path, fields, code):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, research(**fields))
    assert exc.value.code == code


@pytest.mark.parametrize("key", ["strategies/../x.py:A", "strategies/x.py", "strategies/sub/x.py:A",
                                 "other/x.py:A"])
def test_strategy_keys_are_exact_top_level_files(tmp_path, key):
    with pytest.raises(AiConfigError):
        load(tmp_path, research(strategy_keys=f'["{key}"]'))


def test_shipped_template_research_block_matches_the_defaults():
    import yaml
    from pathlib import Path
    from trader.ai.config import ResearchCycleConfig

    shipped = yaml.safe_load(Path("config_defaults/ai.yaml").read_text())["research"]
    assert ResearchCycleConfig.model_validate(shipped) == ResearchCycleConfig()
