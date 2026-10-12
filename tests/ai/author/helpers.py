"""Shared text for the author tests. ai.yaml with an author role is config_text plus three insertions."""
from tests.ai.fakes import config_text, write_config
from trader.ai.config import load_ai_config

UNIVERSES = "{b_big: [11, 12, 13, 14, 15, 16, 17, 18], a_small: [1, 2, 3, 4, 5, 6, 7, 8]}"
RESEARCH_BLOCK = ('research:\n  enabled: true\n  strategy_keys: ["strategies/time_of_day.py:TimeOfDay"]\n'
                  f"  universes: {UNIVERSES}\n  bar_sizes: [\"5 mins\", \"15 mins\"]\n")
AUTHOR_PRICE = '    "vendor/author-1": {input_usd_per_million: 2.0, output_usd_per_million: 8.0}\n'


def author_text(*, model: str = "vendor/author-1", role_extra: str = "", research: str = RESEARCH_BLOCK,
                price: bool = True, authoring: str = "", backend: str = "openrouter") -> str:
    text = config_text(extra_top_level=research + authoring)
    role = f'  author: {{backend: {backend}, model: "{model}", max_output_tokens: 8000{role_extra}}}\n'
    text = text.replace("pricing:\n  openrouter:\n", "pricing:\n  openrouter:\n" + (AUTHOR_PRICE if price else ""))
    return text.replace("roles:\n", "roles:\n" + role)


def load(tmp_path, text: str):
    return load_ai_config(str(write_config(tmp_path, text)))
