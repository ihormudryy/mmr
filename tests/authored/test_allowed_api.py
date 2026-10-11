"""SP3 Plan 1 Task 4: the allow-lists walk the shipped pandas/numpy (spec 7.2)."""
from __future__ import annotations

import importlib
import inspect

import pytest

from trader.authored.allowed_api import (
    ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
)

PROCESS_MODULES = {"os", "sys", "subprocess", "builtins"}


def test_imports_are_the_spec_seven_modules():
    assert ALLOWED_IMPORTS == frozenset(
        {"numpy", "pandas", "math", "statistics", "dataclasses", "typing",
         "trader.authored.api"})


def test_from_imports_exclude_the_data_modules():
    assert "numpy" not in ALLOWED_FROM_IMPORTS and "pandas" not in ALLOWED_FROM_IMPORTS
    assert ALLOWED_FROM_IMPORTS == frozenset({"typing", "dataclasses", "trader.authored.api"})


def test_deny_list_covers_the_spec_and_the_rulings():
    for name in ("os", "sys", "io", "subprocess", "signal", "kill", "getppid",
                 "system", "popen", "modules", "builtins", "ctypeslib", "pickle",
                 "ctypes", "dump", "dumps", "loads", "frombuffer"):
        assert name in DENIED_CHAIN_NAMES


@pytest.mark.parametrize("module_name", sorted(ALLOWED_MODULE_ATTRIBUTES))
def test_every_allowed_attribute_is_not_a_module_and_hides_no_process_module(module_name):
    module = importlib.import_module(module_name)
    for attribute in sorted(ALLOWED_MODULE_ATTRIBUTES[module_name]):
        assert hasattr(module, attribute), f"{module_name}.{attribute} is gone on this build"
        value = getattr(module, attribute)
        assert not inspect.ismodule(value), f"{module_name}.{attribute} is a module"
        for public in dir(value):
            if public.startswith("_"):
                continue
            try:
                nested = getattr(value, public)
            except Exception:
                continue
            assert not (inspect.ismodule(nested) and nested.__name__ in PROCESS_MODULES), (
                f"{module_name}.{attribute}.{public} exposes {PROCESS_MODULES}")


def test_statistics_submodules_are_never_listed():
    # statistics imports math, numbers, random, sys as attributes; none may be allowed.
    for submodule in ("random", "sys", "math", "numbers"):
        assert submodule not in ALLOWED_MODULE_ATTRIBUTES["statistics"]
