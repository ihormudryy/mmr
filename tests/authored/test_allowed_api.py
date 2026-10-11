"""SP3 Plan 1 Task 4: the allow-lists walk the shipped pandas/numpy (spec 7.2)."""
from __future__ import annotations

import importlib
import inspect
import sys
import types

import pytest

from trader.authored.allowed_api import (
    ALLOWED_AST_NODES, ALLOWED_BUILTINS, ALLOWED_FROM_IMPORT_NAMES, ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS,
    ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
    DENIED_CHAIN_PREFIXES,
)


API_MODULE = "trader.authored.api"


def test_imports_and_from_imports_together_are_the_spec_seven_modules():
    assert ALLOWED_IMPORTS | ALLOWED_FROM_IMPORTS == frozenset(
        {"numpy", "pandas", "math", "statistics", "dataclasses", "typing", API_MODULE})


def test_typing_and_dataclasses_are_from_import_only():
    # Plain `import typing` would expose typing.operator, typing.sys, ... (Plan 1 ruling 11).
    assert not {"typing", "dataclasses"} & ALLOWED_IMPORTS


def test_every_plain_importable_module_has_an_attribute_allow_list():
    # trader.authored.api is never a plain import: the checker refuses every dotted import.
    assert set(ALLOWED_IMPORTS) - {API_MODULE} == set(ALLOWED_MODULE_ATTRIBUTES)


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


def test_abc_register_is_denied():
    # AuthoredStrategy is an ABC: AuthoredStrategy.register(cls) would make any class a "subclass".
    assert "register" in DENIED_CHAIN_NAMES


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


def _is_module_globals(value: object) -> bool:
    return isinstance(value, dict) and any(
        value is vars(module) for module in list(sys.modules.values()) if module is not None)


def _reaches_the_interpreter(value: object) -> bool:
    """A module, a code object, a frame, a traceback or a module's globals dict."""
    return (inspect.ismodule(value)
            or isinstance(value, (types.CodeType, types.FrameType, types.TracebackType))
            or _is_module_globals(value))


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


def _assert_reaches_nothing_undenied(root: str, value: object) -> None:
    assert not inspect.ismodule(value), f"{root} is a module"
    for path, name, nested in _walk_public_attributes(root, value, depth_left=2):
        if _reaches_the_interpreter(nested):
            assert _is_denied(name), f"{path} reaches {type(nested).__name__} by an undenied name"


@pytest.mark.parametrize("module_name", sorted(ALLOWED_MODULE_ATTRIBUTES))
def test_every_allowed_attribute_is_not_a_module_and_reaches_no_undenied_module(module_name):
    module = importlib.import_module(module_name)
    for attribute in sorted(ALLOWED_MODULE_ATTRIBUTES[module_name]):
        assert hasattr(module, attribute), f"{module_name}.{attribute} is gone on this build"
        _assert_reaches_nothing_undenied(f"{module_name}.{attribute}", getattr(module, attribute))


@pytest.mark.parametrize("module_name", sorted(ALLOWED_FROM_IMPORT_NAMES))
def test_every_from_import_name_reaches_no_undenied_module(module_name):
    module = importlib.import_module(module_name)
    for name in sorted(ALLOWED_FROM_IMPORT_NAMES[module_name]):
        _assert_reaches_nothing_undenied(f"{module_name}.{name}", getattr(module, name))


def test_the_walk_flags_what_reaches_the_interpreter():
    # The walk's predicate is not the deny-list itself: it recognises these by type.
    function_code = (lambda: None).__code__
    assert _reaches_the_interpreter(function_code)
    assert _reaches_the_interpreter(sys._getframe())
    assert _reaches_the_interpreter(vars(inspect))
    assert not _reaches_the_interpreter({"a": 1})


def test_statistics_submodules_are_never_listed():
    # statistics imports math, numbers, random, sys as attributes; none may be allowed.
    for submodule in ("random", "sys", "math", "numbers"):
        assert submodule not in ALLOWED_MODULE_ATTRIBUTES["statistics"]


def test_walk_really_reaches_the_cython_introspection_names():
    timedelta = importlib.import_module("pandas").Timedelta
    reached = {name: nested for _, name, nested in _walk_public_attributes("pd.Timedelta", timedelta, 2)}
    assert {"func_globals", "func_code"} <= set(reached)
    assert _reaches_the_interpreter(reached["func_globals"]) and _reaches_the_interpreter(reached["func_code"])
    assert all(_is_denied(name) for name in ("func_globals", "func_code"))
