"""The strategy key (SP2c spec 3): ``strategies/<file>.py:<Class>``, and SP3's authored key.

Cooldown, the trial count and holdout windows use it. It is not the registry's
``family_id``, which changes with every parameter set. An authored strategy (SP3 spec 3)
is keyed ``authored:sha256:<hex>:<Class>``; inside SP2c its strategy path is
``authored:sha256:<hex>``, so ``<path>:<Class>`` is again its key (Plan 3 ruling 1).
The allowlist stays catalogue only: ``is_strategy_key`` never accepts an authored key.
"""
from __future__ import annotations

import re

from trader.authored.keys import is_authored_key, parse_authored_key

STRATEGY_KEY = re.compile(r"^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$")
AUTHORED_PATH_PREFIX = "authored:"
AUTHORED_PATH = re.compile(r"^authored:sha256:[0-9a-f]{64}$")


def is_strategy_key(value: object) -> bool:
    return isinstance(value, str) and STRATEGY_KEY.fullmatch(value) is not None


def split_strategy_key(key: str) -> tuple[str, str]:
    if not is_strategy_key(key):
        raise ValueError(f"{key!r} is not strategies/<file>.py:<Class>")
    path, class_name = key.split(":", 1)
    return path, class_name


def is_authored_path(value: object) -> bool:
    return isinstance(value, str) and AUTHORED_PATH.fullmatch(value) is not None


def authored_strategy_path(digest: str) -> str:
    path = AUTHORED_PATH_PREFIX + str(digest)
    if not is_authored_path(path):
        raise ValueError(f"{digest!r} is not sha256:<64 lower-case hex>")
    return path


def authored_digest_of_path(path: str) -> str:
    if not is_authored_path(path):
        raise ValueError(f"{path!r} is not authored:sha256:<64 hex>")
    return path[len(AUTHORED_PATH_PREFIX):]


def is_evaluation_key(value: object) -> bool:
    """A key SP2c may evaluate: a catalogue strategy key or an authored key."""
    return is_strategy_key(value) or (isinstance(value, str) and is_authored_key(value))


def split_evaluation_key(key: str) -> tuple[str, str]:
    """(strategy path, class name) of either kind of key."""
    if is_strategy_key(key):
        return split_strategy_key(key)
    if isinstance(key, str) and is_authored_key(key):
        digest, class_name = parse_authored_key(key)
        return authored_strategy_path(digest), class_name
    raise ValueError(f"{key!r} is neither strategies/<file>.py:<Class> nor authored:sha256:<hex>:<Class>")
