"""Static checker for authored strategies (spec 7.2). Pure AST walk, no
execution. The second layer of SP3's defence; Plan 2's process boundary is the
isolation. Every failure is one StaticViolation whose rule names the broken
rule; the caller maps it to AUTHORED_STATIC_REJECTED.
"""
from __future__ import annotations

import ast
import builtins
import threading
import warnings
from dataclasses import dataclass

from trader.authored.allowed_api import (
    ALLOWED_AST_NODES, ALLOWED_BUILTINS, ALLOWED_FROM_IMPORT_NAMES, ALLOWED_FROM_IMPORTS,
    ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES, BANNED_ATTRIBUTES, BANNED_NAMES,
    DENIED_CHAIN_NAMES, DENIED_CHAIN_PREFIXES, IO_ATTRIBUTE_PREFIXES, IO_EXEMPT_NAMES, IO_NAMES,
    PROTECTED_NAMES,
)
from trader.authored.keys import is_valid_class_name

MAX_SOURCE_BYTES = 20_480
BASE_CLASS = "AuthoredStrategy"
API_MODULE = "trader.authored.api"
_BUILTIN_NAMES = frozenset(dir(builtins))
_LITERAL_EVAL_ERRORS = (ValueError, TypeError, SyntaxError, MemoryError, RecursionError)


@dataclass(frozen=True)
class StaticViolation:
    rule: str
    line: int
    detail: str


class AuthoredStaticRejected(Exception):
    def __init__(self, violations: tuple[StaticViolation, ...]):
        self.violations = violations
        super().__init__(f"{len(violations)} static violation(s)")


def _is_plain_importable(module: str) -> bool:
    # A module without an attribute allow-list would leave its attributes unchecked.
    return module in ALLOWED_IMPORTS and module in ALLOWED_MODULE_ATTRIBUTES and "." not in module


def _is_io_attribute(name: str) -> bool:
    if name in IO_EXEMPT_NAMES:
        return False
    return name.startswith(IO_ATTRIBUTE_PREFIXES) or name in IO_NAMES


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
            self._check_alias(node, alias)
            self._check_binding(node, alias.asname or alias.name.split(".")[0])
            if not _is_plain_importable(alias.name):
                self.add("import_not_allowed", node, f"import {alias.name}")
                continue
            self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if any(a.name == "*" for a in node.names):
            self.add("import_star", node, f"from {module} import *")
        elif node.level > 0:
            self.add("from_import_not_allowed", node, f"relative import from {module}")
        elif module not in ALLOWED_FROM_IMPORTS:
            self.add("from_import_not_allowed", node, f"from {module} import ...")
        else:
            for alias in node.names:
                self._check_alias(node, alias)
                self._check_api_alias(node, module, alias)
                if alias.name not in ALLOWED_FROM_IMPORT_NAMES.get(module, frozenset()):
                    self.add("from_import_not_allowed", node,
                             f"name {alias.name} not importable from {module}")
        self.generic_visit(node)

    def _check_alias(self, node: ast.AST, alias: ast.alias) -> None:
        for name in alias.name.split(".") + ([alias.asname] if alias.asname else []):
            self._check_identifier(node, name)

    def _check_api_alias(self, node: ast.AST, module: str, alias: ast.alias) -> None:
        bound_name = alias.asname or alias.name
        imports_itself = module == API_MODULE and bound_name == alias.name
        if not imports_itself:
            self._check_not_protected(node, bound_name)
        self._check_not_builtin(node, bound_name)

    def _check_binding(self, node: ast.AST, bound_name: str) -> None:
        self._check_not_protected(node, bound_name)
        self._check_not_builtin(node, bound_name)

    def _check_not_protected(self, node: ast.AST, bound_name: str) -> None:
        if bound_name in PROTECTED_NAMES:
            self.add("class_shape", node, f"{bound_name} re-bound")

    def _check_not_builtin(self, node: ast.AST, bound_name: str) -> None:
        if bound_name in _BUILTIN_NAMES:
            self.add("banned_builtin", node, f"builtin {bound_name} re-bound")

    # --- names, attributes, strings -----------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        self._check_identifier(node, node.id)
        if not isinstance(node.ctx, ast.Load):
            self._check_binding(node, node.id)
        elif node.id in _BUILTIN_NAMES and node.id not in ALLOWED_BUILTINS:
            self.add("banned_builtin", node, f"builtin {node.id}")
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
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add("keeps_state", node, f"assignment to attribute {node.attr}")
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
    def visit_Match(self, node: ast.Match) -> None:
        # Class patterns read attributes by keyword names that the AST holds as
        # plain strings, out of reach of every attribute rule.
        self.add("match_statement", node, "match statement")

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

    def visit_arg(self, node: ast.arg) -> None:
        self._check_identifier(node, node.arg)
        self._check_binding(node, node.arg)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_identifier(node, node.name)
        self._check_binding(node, node.name)
        self._check_no_decorator_or_type_params(node)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None and not _is_immutable_literal(default):
                self.add("keeps_state", default, f"default of {node.name} must be an immutable literal")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_identifier(node, node.name)
        self._check_binding(node, node.name)
        nested = not isinstance(getattr(node, "_parent", None), ast.Module)
        if nested and (node.bases or node.keywords):
            self.add("class_shape", node, f"nested class {node.name} may have no bases or keywords")
        self._check_no_decorator_or_type_params(node)
        self.generic_visit(node)

    def _check_no_decorator_or_type_params(self, node: ast.FunctionDef | ast.ClassDef) -> None:
        if node.decorator_list:
            self.add("banned_statement", node, f"decorator on {node.name}")
        # Type parameter names are plain strings, out of reach of the name rules.
        if node.type_params:
            self.add("banned_statement", node, f"type parameters on {node.name}")


def _link_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]


def _check_node_types(tree: ast.AST, checker: _Checker) -> None:
    for node in ast.walk(tree):
        if type(node).__name__ not in ALLOWED_AST_NODES:
            checker.add("node_type", node, f"{type(node).__name__} is not allowed")


class _Scope:
    def __init__(self, kind: str, parent: _Scope | None) -> None:
        self.kind = kind            # module, function, class or comprehension
        self.parent = parent
        self.names: set[str] = set()

    def sees(self, name: str) -> bool:
        """Python's lookup: this scope, then enclosing scopes except class bodies."""
        if name in self.names:
            return True
        scope = self.parent
        while scope is not None:
            if scope.kind != "class" and name in scope.names:
                return True
            scope = scope.parent
        return False


class _FreeNameChecker(ast.NodeVisitor):
    """Finds loaded names that no visible scope binds and that are not allowed builtins.

    Bindings are collected for the whole file first, then reads are resolved, so a
    name bound later in the same function counts as bound (as in Python).
    """

    def __init__(self) -> None:
        self.scope = _Scope("module", None)
        self.reads: list[tuple[ast.Name, _Scope]] = []

    def unknown_names(self, tree: ast.Module) -> list[ast.Name]:
        self.generic_visit(tree)
        return [node for node, scope in self.reads
                if node.id not in ALLOWED_BUILTINS and node.id not in _BUILTIN_NAMES
                and not scope.sees(node.id)]

    def _in_new_scope(self, kind: str, nodes: list[ast.AST], bound: tuple[str, ...] = ()) -> None:
        outer = self.scope
        self.scope = _Scope(kind, outer)
        self.scope.names.update(bound)
        for node in nodes:
            self.visit(node)
        self.scope = outer

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads.append((node, self.scope))
        else:
            self.scope.names.add(node.id)

    def visit_alias(self, node: ast.alias) -> None:
        self.scope.names.add(node.asname or node.name.split(".")[0])

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        target_scope = self.scope
        while target_scope.kind == "comprehension":
            target_scope = target_scope.parent
        target_scope.names.add(node.target.id)

    def _visit_function(self, node: ast.FunctionDef | ast.Lambda, body: list[ast.AST]) -> None:
        arguments = node.args
        every_arg = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                     *(a for a in (arguments.vararg, arguments.kwarg) if a is not None)]
        for default in [*arguments.defaults, *arguments.kw_defaults]:
            if default is not None:
                self.visit(default)
        for arg in every_arg:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        self._in_new_scope("function", body, tuple(arg.arg for arg in every_arg))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.names.add(node.name)
        for expression in [*node.decorator_list, *([node.returns] if node.returns else [])]:
            self.visit(expression)
        self._visit_function(node, node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function(node, [node.body])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.names.add(node.name)
        for expression in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(expression)
        self._in_new_scope("class", node.body)

    def _visit_comprehension(self, generators: list[ast.comprehension],
                             results: list[ast.AST]) -> None:
        self.visit(generators[0].iter)   # the first iterable is evaluated outside
        parts: list[ast.AST] = []
        for index, generator in enumerate(generators):
            parts.append(generator.target)
            if index > 0:
                parts.append(generator.iter)
            parts.extend(generator.ifs)
        self._in_new_scope("comprehension", parts + results)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])


def _check_free_names(tree: ast.Module, checker: _Checker) -> None:
    for node in _FreeNameChecker().unknown_names(tree):
        checker.add("unknown_name", node, f"name {node.id} is not bound in a visible scope")


def _imports_base_class(body: list[ast.stmt]) -> bool:
    return any(
        isinstance(statement, ast.ImportFrom) and statement.level == 0
        and statement.module == API_MODULE
        and any(a.name == BASE_CLASS and a.asname in (None, BASE_CLASS) for a in statement.names)
        for statement in body)


def _check_class_shape(statement: ast.ClassDef, base_imported: bool, checker: _Checker) -> None:
    only_the_base = (len(statement.bases) == 1 and not statement.keywords
                     and isinstance(statement.bases[0], ast.Name)
                     and statement.bases[0].id == BASE_CLASS)
    if not is_valid_class_name(statement.name):
        checker.add("class_shape", statement,
                    f"class name {statement.name!r} is not an authored key class name "
                    "(ASCII letters, digits and _, at most 64 characters)")
    if not only_the_base:
        checker.add("class_shape", statement,
                    f"class {statement.name} must subclass only {BASE_CLASS}, with no keywords")
    elif not base_imported:
        checker.add("class_shape", statement,
                    f"{BASE_CLASS} is not imported from {API_MODULE}")


def _has_literal_value(statement: ast.Assign | ast.AnnAssign) -> bool:
    """Spec 7.2 allows constant assignments only; ast.literal_eval also accepts -1."""
    if statement.value is None:
        return False
    try:
        ast.literal_eval(statement.value)
    except _LITERAL_EVAL_ERRORS:
        return False
    return True


def _is_immutable_literal(node: ast.expr) -> bool:
    """A literal that no later bar can change: no list, dict or set anywhere inside it (#164)."""
    try:
        ast.literal_eval(node)
    except _LITERAL_EVAL_ERRORS:
        return False
    return not any(isinstance(inner, (ast.List, ast.Dict, ast.Set, ast.Call)) for inner in ast.walk(node))  # set() too


# What a class constant may be built from: constants, names and operators, so ``WINDOW * 10`` passes and no call,
# list, dict, set or comprehension can leave a mutable object on the class.
_CLASS_CONSTANT_NODES = (ast.Constant, ast.Name, ast.Load, ast.Tuple, ast.BinOp, ast.UnaryOp, ast.BoolOp,
                         ast.Compare, ast.IfExp, ast.Attribute, ast.Subscript, ast.Slice, ast.operator,
                         ast.unaryop, ast.boolop, ast.cmpop)


def _check_module_assignment(statement: ast.Assign | ast.AnnAssign, checker: _Checker) -> None:
    if not _has_literal_value(statement):
        checker.add("module_statement", statement, "module-level assignment must have a literal value")
    elif not _is_immutable_literal(statement.value):
        checker.add("keeps_state", statement, "module-level value must be immutable (no list, dict or set)")


def _check_class_assignment(statement: ast.Assign | ast.AnnAssign, checker: _Checker) -> None:
    if statement.value is None:
        return                                          # an annotation alone binds nothing
    for inner in ast.walk(statement.value):
        if not isinstance(inner, _CLASS_CONSTANT_NODES):
            checker.add("keeps_state", statement,
                        f"class-level value may use only constants, names and operators, not {type(inner).__name__}")
            return


def _check_class_body(statement: ast.ClassDef, checker: _Checker) -> None:
    """The class keeps no state between bars (#164): constants and methods only."""
    body = statement.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
        body = body[1:]                                 # a class docstring is allowed
    for inner in body:
        if isinstance(inner, (ast.Assign, ast.AnnAssign)):
            _check_class_assignment(inner, checker)
        elif not isinstance(inner, (ast.FunctionDef, ast.Pass)):
            checker.add("class_statement", inner, f"class-level {type(inner).__name__}")


def _check_module_shape(tree: ast.Module, checker: _Checker) -> None:
    classes = 0
    body = tree.body
    base_imported = _imports_base_class(body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # a module docstring is allowed
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            _check_module_assignment(statement, checker)
            continue
        if isinstance(statement, ast.ClassDef):
            classes += 1
            _check_class_shape(statement, base_imported, checker)
            _check_class_body(statement, checker)
            continue
        checker.add("module_statement", statement,
                    f"module-level {type(statement).__name__}")
    if classes != 1:
        checker.add("class_shape", tree, f"expected exactly one class, found {classes}")


def _unparseable(line: int, detail: str) -> list[StaticViolation]:
    return [StaticViolation("unparseable", line, detail)]


# catch_warnings swaps the process-wide filter list; the trader checks files on
# several RPC threads, so one thread's restore must not land inside another's parse.
_PARSE_LOCK = threading.Lock()


def _parse_quietly(source: str) -> ast.Module:
    # Under -W error the compiler turns a SyntaxWarning (e.g. '\d') into a
    # SyntaxError; the verdict must not depend on the host's warning filter.
    with _PARSE_LOCK, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


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
        tree = _parse_quietly(source)
    except SyntaxError as error:
        return _unparseable(error.lineno or 0, str(error.msg))
    except (ValueError, MemoryError, RecursionError) as error:
        return _unparseable(0, f"too complex to parse: {type(error).__name__}")
    _link_parents(tree)
    checker = _Checker()
    _check_node_types(tree, checker)
    _check_module_shape(tree, checker)
    try:
        checker.visit(tree)
        _check_free_names(tree, checker)
    except RecursionError:
        return _unparseable(0, "too deeply nested to check")
    return sorted(checker.violations, key=lambda v: (v.line, v.rule, v.detail))


def require_clean(source: str) -> str:
    violations = check_source(source)
    if violations:
        raise AuthoredStaticRejected(tuple(violations))
    tree = _parse_quietly(source)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            return statement.name
    raise AuthoredStaticRejected(
        (StaticViolation("class_shape", 0, f"no {BASE_CLASS} subclass"),))
