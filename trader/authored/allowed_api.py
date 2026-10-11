"""The static checker's allow-lists and deny-list, as data (spec 7.2).

`import X` is allowed only for the data modules in ALLOWED_IMPORTS, and every
attribute read off one (np.x, pd.x, math.x, statistics.x) must be in
ALLOWED_MODULE_ATTRIBUTES[module]; nothing listed is a module (the walk test
in tests/authored/test_allowed_api.py proves it on the shipped build).
typing, dataclasses and trader.authored.api come only by from-import, and a
from-import may bind only the names in ALLOWED_FROM_IMPORT_NAMES[module]
(Plan 1 rulings 5 and 11).
A builtin may be named only if it is in ALLOWED_BUILTINS, and every AST node
must be one of ALLOWED_AST_NODES.
DENIED_CHAIN_NAMES are refused anywhere in an attribute chain. The lists
extend the spec's (Plan 1 ruling 6); they never narrow it.
"""
from __future__ import annotations

from typing import Mapping

# Modules an authored file may import. typing and dataclasses are not here: they
# come only by from-import. trader.authored.api is here and in ALLOWED_FROM_IMPORTS,
# but a dotted `import` is always refused, so it too comes only by from-import.
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"numpy", "pandas", "math", "statistics", "trader.authored.api"})

ALLOWED_FROM_IMPORTS: frozenset[str] = frozenset(
    {"typing", "dataclasses", "trader.authored.api"})

# Names a from-import may bind, per module. ALLOWED_FROM_IMPORTS names the module
# only; without this list `from typing import operator` or
# `from trader.authored.api import pd` would bind a whole module.
ALLOWED_FROM_IMPORT_NAMES: Mapping[str, frozenset[str]] = {
    "typing": frozenset({
        "Any", "ClassVar", "Dict", "Final", "Iterable", "List", "Literal",
        "Mapping", "Optional", "Sequence", "Tuple", "Union"}),
    "dataclasses": frozenset({"dataclass", "field"}),
    "trader.authored.api": frozenset({
        "AuthoredStrategy", "Decision", "ACTIONS", "MIN_LOOKBACK_BARS",
        "MAX_LOOKBACK_BARS"}),
}

ALLOWED_MODULE_ATTRIBUTES: Mapping[str, frozenset[str]] = {
    "numpy": frozenset({
        "abs", "absolute", "arange", "argmax", "argmin", "argsort", "array",
        "asarray", "bool_", "ceil", "clip", "concatenate", "corrcoef", "cov",
        "cumprod", "cumsum", "diff", "exp", "float64", "floor", "full", "inf",
        "int64", "isfinite", "isnan", "log", "log1p", "max", "maximum", "mean",
        "median", "min", "minimum", "nan", "nanmax", "nanmean", "nanmedian", "nanmin",
        "nanstd", "nansum", "ones", "percentile", "quantile", "sign", "sort",
        "sqrt", "std", "sum", "unique", "var", "where", "zeros", "round"}),
    "pandas": frozenset({
        "DataFrame", "Series", "Timedelta", "Timestamp", "DatetimeIndex",
        "Index", "isna", "notna", "concat", "to_datetime", "date_range"}),
    "math": frozenset({
        "ceil", "copysign", "e", "exp", "fabs", "floor", "fsum", "inf",
        "isfinite", "isinf", "isnan", "log", "log10", "nan", "pi", "sqrt",
        "tanh", "erf"}),
    "statistics": frozenset({
        "mean", "median", "median_low", "median_high", "stdev", "pstdev",
        "variance", "pvariance", "fmean", "quantiles"}),
}

# The only builtins an authored file may name. Every other name in dir(builtins)
# (type, object, super, dir, iter, print, ...) is refused, and no builtin name may
# be re-bound.
ALLOWED_BUILTINS: frozenset[str] = frozenset({
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "isinstance",
    "len", "list", "max", "min", "range", "reversed", "round", "set", "sorted", "str",
    "sum", "tuple", "zip", "True", "False", "None"})

# Every AST node in an authored file must have one of these type names. Anything
# else (try, with, raise, match, async, yield, global, type parameters, and node
# types added by a later Python) is refused, whatever the specific rules say.
ALLOWED_AST_NODES: frozenset[str] = frozenset({
    # structure and statements
    "Module", "Import", "ImportFrom", "alias", "ClassDef", "FunctionDef",
    "arguments", "arg", "Return", "Assign", "AnnAssign", "AugAssign", "Delete",
    "If", "For", "While", "Break", "Continue", "Pass", "Expr", "Assert",
    # expressions
    "BoolOp", "BinOp", "UnaryOp", "Compare", "Call", "keyword", "IfExp",
    "Attribute", "Subscript", "Slice", "Name", "Constant", "List", "Tuple",
    "Dict", "Set", "ListComp", "SetComp", "DictComp", "GeneratorExp",
    "comprehension", "NamedExpr", "Lambda", "Starred", "JoinedStr", "FormattedValue",
    # contexts and operators
    "Load", "Store", "Del", "And", "Or",
    "Add", "Sub", "Mult", "MatMult", "Div", "Mod", "Pow", "LShift", "RShift",
    "BitOr", "BitXor", "BitAnd", "FloorDiv",
    "Invert", "Not", "UAdd", "USub",
    "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn"})

DENIED_CHAIN_NAMES: frozenset[str] = frozenset({
    # spec 7.2 deny-list of module and process names
    "os", "sys", "io", "common", "core", "compat", "api", "lib", "util",
    "testing", "builtins", "modules", "subprocess", "signal", "kill",
    "getppid", "system", "popen", "plot", "style", "sql",
    # Plan 1 ruling 6 additions
    "ctypeslib", "pickle", "ctypes", "dump", "dumps", "loads", "frombuffer",
    # reachable from allowed classes: class-chain walk, ABCMeta.register on
    # AuthoredStrategy, and matplotlib entry points
    "mro", "register", "boxplot", "hist",
    # pandas string dispatch: these take a method name, which can be built at runtime
    "apply", "agg", "aggregate", "transform", "applymap", "pipe"})

# Introspection attributes that Cython and Python objects expose without a leading
# underscore (func_globals, func_code, gi_frame, tb_frame, ...). The checker must
# refuse any attribute in ANY chain whose name starts with one of these, in
# addition to the exact names in DENIED_CHAIN_NAMES.
DENIED_CHAIN_PREFIXES: tuple[str, ...] = (
    "func_", "gi_", "cr_", "ag_", "f_", "tb_", "co_", "im_")
