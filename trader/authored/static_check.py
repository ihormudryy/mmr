"""Static checker for authored strategies (spec 7.2). Pure AST walk, no
execution. The second layer of SP3's defence; Plan 2's process boundary is the
isolation. Every failure is one StaticViolation whose rule names the broken
rule; the caller maps it to AUTHORED_STATIC_REJECTED.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from trader.authored.allowed_api import (
    ALLOWED_FROM_IMPORTS, ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES, DENIED_CHAIN_NAMES,
    DENIED_CHAIN_PREFIXES,
)

MAX_SOURCE_BYTES = 20_480
BASE_CLASS = "AuthoredStrategy"
_DATA_MODULES = frozenset(ALLOWED_MODULE_ATTRIBUTES)

BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "input", "breakpoint",
    "memoryview", "format"})
BANNED_ATTRIBUTES = frozenset({"format", "format_map"})
_IO_PREFIX = re.compile(r"^(read_|to_)")
_IO_NAMES = frozenset({"load", "save", "savez", "fromfile", "tofile", "eval",
                       "query", "ctypeslib", "pickle"})


@dataclass(frozen=True)
class StaticViolation:
    rule: str
    line: int
    detail: str


class AuthoredStaticRejected(Exception):
    def __init__(self, violations: tuple[StaticViolation, ...]):
        self.violations = violations
        super().__init__(f"{len(violations)} static violation(s)")


def _is_io_attribute(name: str) -> bool:
    if name == "to_numpy":
        return False
    return bool(_IO_PREFIX.match(name)) or name in _IO_NAMES


def _has_denied_prefix(name: str) -> bool:
    return name.startswith(DENIED_CHAIN_PREFIXES)


def _bad_string(text: str) -> bool:
    if "__" in text:
        return True
    if text in DENIED_CHAIN_NAMES or text in BANNED_NAMES or _has_denied_prefix(text):
        return True
    return _is_io_attribute(text)


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[StaticViolation] = []
        self.module_aliases: dict[str, str] = {}   # alias -> real module name

    def add(self, rule: str, node: ast.AST, detail: str) -> None:
        self.violations.append(StaticViolation(rule, getattr(node, "lineno", 0), detail))

    # --- imports -------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in ALLOWED_IMPORTS or "." in alias.name:
                self.add("import_not_allowed", node, f"import {alias.name}")
                continue
            if alias.name in _DATA_MODULES:
                self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if any(a.name == "*" for a in node.names):
            self.add("import_star", node, f"from {module} import *")
        elif module not in ALLOWED_FROM_IMPORTS:
            self.add("from_import_not_allowed", node, f"from {module} import ...")
        self.generic_visit(node)

    # --- names, attributes, strings -----------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        self._check_identifier(node, node.id)
        if node.id in self.module_aliases and not self._is_chain_root(node):
            self.add("module_alias_misuse", node, f"module alias {node.id} used outside a chain root")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_identifier(node, node.attr)
        if _has_denied_prefix(node.attr):
            self.add("banned_name", node, f"denied chain prefix in attribute {node.attr}")
        if node.attr in BANNED_ATTRIBUTES:
            self.add("banned_name", node, f"attribute {node.attr}")
        if _is_io_attribute(node.attr):
            self.add("io_attribute", node, f"attribute {node.attr}")
        root = self._chain_root(node)
        if isinstance(root, ast.Name) and root.id in self.module_aliases:
            self._check_module_chain(node, root.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
        if isinstance(text, str) and _bad_string(text):
            self.add("string_literal", node, f"string literal {node.value!r}")
        self.generic_visit(node)

    def _check_identifier(self, node: ast.AST, name: str) -> None:
        if name.startswith("_"):
            self.add("dunder_name", node, f"name {name}")
        elif name in BANNED_NAMES:
            self.add("banned_name", node, f"name {name}")
        elif name in DENIED_CHAIN_NAMES:
            self.add("banned_name", node, f"denied chain name {name}")

    def _check_module_chain(self, node: ast.Attribute, alias: str) -> None:
        # The attribute directly on the alias is the "first attribute".
        if isinstance(node.value, ast.Name) and node.value.id == alias:
            module = self.module_aliases[alias]
            if node.attr not in ALLOWED_MODULE_ATTRIBUTES[module]:
                self.add("module_attribute_not_allowed", node, f"{alias}.{node.attr}")

    @staticmethod
    def _chain_root(node: ast.AST) -> ast.AST:
        while isinstance(node, ast.Attribute):
            node = node.value
        return node

    @staticmethod
    def _is_chain_root(name_node: ast.Name) -> bool:
        parent = getattr(name_node, "_parent", None)
        return isinstance(parent, ast.Attribute) and parent.value is name_node

    # --- banned statements --------------------------------------------
    def visit_Global(self, node: ast.Global) -> None:
        self.add("banned_statement", node, "global")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.add("banned_statement", node, "nonlocal")

    def visit_Try(self, node: ast.Try) -> None:
        self.add("banned_statement", node, "try/except")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.add("banned_statement", node, "async def")

    def visit_Await(self, node: ast.Await) -> None:
        self.add("banned_statement", node, "await")

    def visit_Yield(self, node: ast.Yield) -> None:
        self.add("banned_statement", node, "yield")

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.add("banned_statement", node, "yield from")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # kw_defaults is a list with a None per keyword-only arg that has no default,
        # so test for a real default, not just a non-empty list.
        if node.args.defaults or any(d is not None for d in node.args.kw_defaults):
            self.add("banned_statement", node, "lambda with defaults")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.decorator_list:
            self.add("banned_statement", node, f"decorator on {node.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.decorator_list:
            self.add("banned_statement", node, f"decorator on {node.name}")
        self.generic_visit(node)


def _link_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]


def _check_module_shape(tree: ast.Module, checker: _Checker) -> None:
    classes = 0
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # a module docstring is allowed
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(statement, ast.ClassDef):
            classes += 1
            bases = {b.id for b in statement.bases if isinstance(b, ast.Name)}
            if BASE_CLASS not in bases:
                checker.add("class_shape", statement,
                            f"class {statement.name} does not subclass {BASE_CLASS}")
            continue
        checker.add("module_statement", statement,
                    f"module-level {type(statement).__name__}")
    if classes != 1:
        checker.add("class_shape", tree, f"expected exactly one class, found {classes}")


def _unparseable(line: int, detail: str) -> list[StaticViolation]:
    return [StaticViolation("unparseable", line, detail)]


def check_source(source: str) -> list[StaticViolation]:
    if not isinstance(source, str):
        return _unparseable(0, f"source is {type(source).__name__}, not str")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeEncodeError:
        return _unparseable(0, "source is not valid UTF-8 text")
    if size > MAX_SOURCE_BYTES:
        return [StaticViolation("oversize", 0, f"over {MAX_SOURCE_BYTES} bytes")]
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return _unparseable(error.lineno or 0, str(error.msg))
    except (ValueError, MemoryError, RecursionError) as error:
        return _unparseable(0, f"too complex to parse: {type(error).__name__}")
    _link_parents(tree)
    checker = _Checker()
    _check_module_shape(tree, checker)
    try:
        checker.visit(tree)
    except RecursionError:
        return _unparseable(0, "too deeply nested to check")
    return sorted(checker.violations, key=lambda v: (v.line, v.rule, v.detail))


def require_clean(source: str) -> str:
    violations = check_source(source)
    if violations:
        raise AuthoredStaticRejected(tuple(violations))
    tree = ast.parse(source)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            return statement.name
    raise AuthoredStaticRejected(
        (StaticViolation("class_shape", 0, f"no {BASE_CLASS} subclass"),))
