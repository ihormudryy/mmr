"""SP3 Plan 1 Task 4: the allow-lists walk the shipped pandas/numpy (spec 7.2)."""
from __future__ import annotations

import importlib
import inspect

import pytest

from trader.authored.allowed_api import (
    ALLOWED_AST_NODES, ALLOWED_BUILTINS, ALLOWED_FROM_IMPORT_NAMES, ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS,
    ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
    DENIED_CHAIN_PREFIXES,
)


def test_imports_are_the_spec_seven_modules():
    assert ALLOWED_IMPORTS == frozenset(
        {"numpy", "pandas", "math", "statistics", "dataclasses", "typing",
         "trader.authored.api"})


def test_from_imports_exclude_the_data_modules():
    assert "numpy" not in ALLOWED_FROM_IMPORTS and "pandas" not in ALLOWED_FROM_IMPORTS
    assert ALLOWED_FROM_IMPORTS == frozenset({"typing", "dataclasses", "trader.authored.api"})


def test_every_from_import_module_has_a_name_list():
    assert set(ALLOWED_FROM_IMPORT_NAMES) == set(ALLOWED_FROM_IMPORTS)


@pytest.mark.parametrize("module_name, name", sorted(
    (module_name, name)
    for module_name, names in ALLOWED_FROM_IMPORT_NAMES.items() for name in names))
def test_every_from_import_name_resolves_and_is_not_a_module(module_name, name):
    value = getattr(importlib.import_module(module_name), name)
    assert not inspect.ismodule(value), f"from {module_name} import {name} binds a module"


def test_every_allowed_ast_node_is_a_real_node_type():
    import ast
    for name in ALLOWED_AST_NODES:
        assert isinstance(getattr(ast, name, None), type) and issubclass(getattr(ast, name), ast.AST), name


def test_the_ast_allow_list_leaves_out_control_flow_a_strategy_never_needs():
    refused = {"Try", "TryStar", "ExceptHandler", "Raise", "With", "AsyncWith", "Match",
               "Global", "Nonlocal", "Yield", "YieldFrom", "Await", "AsyncFunctionDef",
               "AsyncFor", "TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple"}
    assert not refused & ALLOWED_AST_NODES


SPEC_7_2_DENIED_NAMES = {
    "os", "sys", "io", "common", "core", "compat", "api", "lib", "util", "testing",
    "builtins", "modules", "subprocess", "signal", "kill", "getppid", "system",
    "popen", "plot", "style", "sql"}
RULING_6_DENIED_NAMES = {"ctypes", "dump", "dumps", "loads", "frombuffer"}


def test_deny_list_covers_every_spec_name_and_ruling():
    assert SPEC_7_2_DENIED_NAMES | RULING_6_DENIED_NAMES <= DENIED_CHAIN_NAMES
    assert {"ctypeslib", "pickle", "mro", "boxplot", "hist"} <= DENIED_CHAIN_NAMES


def test_pandas_string_dispatch_methods_are_denied():
    # They accept a method name, and a name built at runtime passes the literal rule.
    assert {"apply", "agg", "aggregate", "transform", "applymap", "pipe"} <= DENIED_CHAIN_NAMES


def test_builtins_are_a_small_allow_list_of_real_builtins():
    import builtins
    assert ALLOWED_BUILTINS == frozenset({
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "isinstance",
        "len", "list", "max", "min", "range", "reversed", "round", "set", "sorted", "str",
        "sum", "tuple", "zip", "True", "False", "None"})
    assert ALLOWED_BUILTINS <= set(dir(builtins))


def test_prefixes_close_the_introspection_families():
    assert {"func_", "gi_", "cr_", "ag_", "f_", "tb_", "co_", "im_"} <= set(DENIED_CHAIN_PREFIXES)


def _is_denied(name: str) -> bool:
    return name in DENIED_CHAIN_NAMES or name.startswith(DENIED_CHAIN_PREFIXES)


def _walk_public_attributes(path: str, value: object, depth_left: int):
    """Yield (path, name, attribute) for public attributes, never descending through a denied name."""
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attribute = getattr(value, name)
        except Exception:
            continue
        yield f"{path}.{name}", name, attribute
        if depth_left > 1 and not _is_denied(name):
            yield from _walk_public_attributes(f"{path}.{name}", attribute, depth_left - 1)


@pytest.mark.parametrize("module_name", sorted(ALLOWED_MODULE_ATTRIBUTES))
def test_every_allowed_attribute_is_not_a_module_and_reaches_no_undenied_module(module_name):
    module = importlib.import_module(module_name)
    for attribute in sorted(ALLOWED_MODULE_ATTRIBUTES[module_name]):
        assert hasattr(module, attribute), f"{module_name}.{attribute} is gone on this build"
        value = getattr(module, attribute)
        root = f"{module_name}.{attribute}"
        assert not inspect.ismodule(value), f"{root} is a module"
        for path, name, nested in _walk_public_attributes(root, value, depth_left=2):
            if inspect.ismodule(nested):
                assert _is_denied(name), f"{path} reaches module {nested.__name__} by an undenied name"
            if name.startswith(DENIED_CHAIN_PREFIXES):
                assert _is_denied(name)  # a prefixed name is denied by the prefix rule itself


def test_statistics_submodules_are_never_listed():
    # statistics imports math, numbers, random, sys as attributes; none may be allowed.
    for submodule in ("random", "sys", "math", "numbers"):
        assert submodule not in ALLOWED_MODULE_ATTRIBUTES["statistics"]


def test_walk_really_reaches_the_cython_introspection_names():
    reached = {name for _, name, _ in _walk_public_attributes("pd.Timedelta", importlib.import_module("pandas").Timedelta, 2)}
    assert {"func_globals", "func_code"} <= reached
    assert all(_is_denied(name) for name in ("func_globals", "func_code"))
