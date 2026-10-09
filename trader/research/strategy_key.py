"""The strategy key (SP2c spec 3): ``strategies/<file>.py:<Class>``.

Cooldown, the trial count and holdout windows use it. It is not the registry's
``family_id``, which changes with every parameter set.
"""
from __future__ import annotations

import re

STRATEGY_KEY = re.compile(r"^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$")


def is_strategy_key(value: object) -> bool:
    return isinstance(value, str) and STRATEGY_KEY.fullmatch(value) is not None


def split_strategy_key(key: str) -> tuple[str, str]:
    if not is_strategy_key(key):
        raise ValueError(f"{key!r} is not strategies/<file>.py:<Class>")
    path, class_name = key.split(":", 1)
    return path, class_name
