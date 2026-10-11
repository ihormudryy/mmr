"""The static checker's allow-lists and deny-list, as data (spec 7.2).

An authored file may import only ALLOWED_IMPORTS. An attribute read off an
imported data module (np.x, pd.x, math.x, statistics.x) must be in
ALLOWED_MODULE_ATTRIBUTES[module]; nothing listed is a module (the walk test
in tests/authored/test_allowed_api.py proves it on the shipped build).
DENIED_CHAIN_NAMES are refused anywhere in an attribute chain. The lists
extend the spec's (Plan 1 ruling 6); they never narrow it.
"""
from __future__ import annotations

from typing import Mapping

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {"numpy", "pandas", "math", "statistics", "dataclasses", "typing",
     "trader.authored.api"})

ALLOWED_FROM_IMPORTS: frozenset[str] = frozenset(
    {"typing", "dataclasses", "trader.authored.api"})

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

DENIED_CHAIN_NAMES: frozenset[str] = frozenset({
    # spec 7.2 deny-list of module and process names
    "os", "sys", "io", "common", "core", "compat", "api", "lib", "util",
    "testing", "builtins", "modules", "subprocess", "signal", "kill",
    "getppid", "system", "popen", "plot", "style", "sql",
    # Plan 1 ruling 6 additions
    "ctypeslib", "pickle", "ctypes", "dump", "dumps", "loads", "frombuffer",
    # reachable from allowed classes: class-chain walk and matplotlib entry points
    "mro", "boxplot", "hist"})

# Introspection attributes that Cython and Python objects expose without a leading
# underscore (func_globals, func_code, gi_frame, tb_frame, ...). The checker must
# refuse any attribute in ANY chain whose name starts with one of these, in
# addition to the exact names in DENIED_CHAIN_NAMES.
DENIED_CHAIN_PREFIXES: tuple[str, ...] = (
    "func_", "gi_", "cr_", "ag_", "f_", "tb_", "co_", "im_")
