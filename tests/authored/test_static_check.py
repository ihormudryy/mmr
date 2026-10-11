"""SP3 Plan 1 Task 5: the static checker (spec 7.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from trader.authored.static_check import (
    AuthoredStaticRejected, check_source, require_clean,
)

CLEAN = '''
import numpy as np
import pandas as pd
from typing import Mapping

from trader.authored.api import AuthoredStrategy, Decision


class Good(AuthoredStrategy):
    LOOKBACK_BARS = 50
    THRESHOLD = 1.5

    def on_bar(self, history, params):
        close = history["close"].to_numpy()
        if np.mean(close[-5:]) > close[-1] * params["THRESHOLD"]:
            return Decision(action="BUY", probability=0.6)
        return None
'''

ORB_FIXTURE = Path(__file__).parent / "fixtures" / "orb_authored.py"


def _rules(source):
    return {v.rule for v in check_source(source)}


def _with_statement(statement):
    """CLEAN with one extra statement at the start of on_bar."""
    return CLEAN.replace("        close = history", f"        {statement}\n        close = history", 1)


def test_a_clean_file_passes_and_require_clean_returns_the_class():
    assert check_source(CLEAN) == []
    assert require_clean(CLEAN) == "Good"


def test_an_import_outside_the_allow_list_is_refused():
    assert "import_not_allowed" in _rules("import os\n" + CLEAN)


@pytest.mark.parametrize("module", ["typing", "dataclasses"])
def test_a_plain_import_of_a_from_import_only_module_is_refused(module):
    # typing.operator.attrgetter reaches os.kill; these modules are from-import only.
    assert "import_not_allowed" in _rules(f"import {module}\n" + CLEAN)


def test_abc_register_on_the_base_class_is_refused():
    body = CLEAN.replace("return None", "AuthoredStrategy.register(Decision)\n        return None")
    assert "banned_name" in _rules(body)


def test_a_from_import_of_a_data_module_is_refused():
    assert "from_import_not_allowed" in _rules("from numpy import mean\n" + CLEAN)


def test_import_star_is_refused():
    assert "import_star" in _rules("from numpy import *\n" + CLEAN)


def test_a_submodule_chain_is_refused():
    body = CLEAN.replace("np.mean(close[-5:])", "pd.io.common.os")
    assert "module_attribute_not_allowed" in _rules(body) or "banned_name" in _rules(body)


def test_a_module_alias_used_bare_is_refused():
    body = CLEAN.replace("return None", "x = np\n        return None")
    assert "module_alias_misuse" in _rules(body)


def test_a_dunder_name_is_refused():
    body = CLEAN.replace("close[-1]", "close.__class__")
    assert "dunder_name" in _rules(body)


def test_a_banned_builtin_is_refused():
    body = CLEAN.replace("np.mean(close[-5:])", "eval('1')")
    assert "banned_name" in _rules(body)


def test_an_io_attribute_is_refused():
    body = CLEAN.replace("history[\"close\"].to_numpy()", "history.to_pickle('p')")
    assert "io_attribute" in _rules(body)


def test_to_numpy_is_allowed():
    assert "io_attribute" not in _rules(CLEAN)


def test_a_dunder_in_a_string_is_refused():
    body = CLEAN.replace("probability=0.6", "probability=0.6, ) if '{0.__globals__}' else Decision(action='BUY', probability=0.6")
    assert "string_literal" in _rules(body)


def test_a_decorator_is_refused():
    body = CLEAN.replace("    def on_bar", "    @staticmethod\n    def on_bar")
    assert "banned_statement" in _rules(body)


def test_try_except_is_refused():
    body = CLEAN.replace("        return None",
                         "        try:\n            return None\n        except Exception:\n            return None")
    assert "banned_statement" in _rules(body)


def test_two_classes_are_refused():
    body = CLEAN + "\n\nclass Other(AuthoredStrategy):\n    def on_bar(self, history, params):\n        return None\n"
    assert "class_shape" in _rules(body)


def test_a_module_level_statement_is_refused():
    assert "module_statement" in _rules(CLEAN + "\nprint('hi')\n")


@pytest.mark.parametrize("assignment", [
    "X = len([1])",
    "X = np.mean([1.0])",
    "X = [v for v in (1, 2)]",
    "X = 1 + 2",
    "X: int",
])
def test_a_module_level_assignment_without_a_literal_value_is_refused(assignment):
    source = CLEAN.replace("\n\nclass Good", f"\n{assignment}\n\n\nclass Good", 1)
    assert "module_statement" in _rules(source)


@pytest.mark.parametrize("assignment", [
    "X = 1", "X = -2.5", "X = (1, 'a')", "X = {'a': [1, 2]}", "X: float = 0.5", "X = None",
])
def test_a_module_level_literal_assignment_passes(assignment):
    source = CLEAN.replace("\n\nclass Good", f"\n{assignment}\n\n\nclass Good", 1)
    assert check_source(source) == []


def test_a_file_that_does_not_parse_is_one_violation():
    violations = check_source("class Good(AuthoredStrategy:\n")
    assert len(violations) == 1 and violations[0].rule == "unparseable"


def test_an_oversize_file_is_refused():
    assert "oversize" in _rules(CLEAN + "\n# " + "x" * 20_500)


def test_require_clean_raises_with_violations():
    with pytest.raises(AuthoredStaticRejected) as caught:
        require_clean("import os\n" + CLEAN)
    assert caught.value.violations


# --- controller ruling: denied names and prefixes on every attribute ---------

def _assert_refused(source, rule, attribute):
    violations = [v for v in check_source(source) if v.rule == rule]
    assert any(attribute in v.detail for v in violations), check_source(source)


@pytest.mark.parametrize("statement, attribute", [
    ("x = pd.Timedelta(1).ceil.func_globals", "func_globals"),
    ("x = pd.Timedelta(1)\n        y = x.ceil.func_code", "func_code"),
    ("x = history.gi_frame", "gi_frame"),
    ("x = history.f_globals", "f_globals"),
    ("x = history.f_globals()", "f_globals"),
    ("x = history.gi_frame[0]", "gi_frame"),
    ("x = (lambda: 0).co_consts[0]", "co_consts"),
    ("x = len((lambda: 0).co_consts)", "co_consts"),
    ("x = len(history.tb_frame.f_back)", "tb_frame"),
    ("x = history.cr_frame", "cr_frame"),
    ("x = history.ag_frame", "ag_frame"),
    ("x = history.im_func", "im_func"),
])
def test_a_denied_prefix_is_refused_in_any_position(statement, attribute):
    _assert_refused(_with_statement(statement), "banned_name", attribute)


@pytest.mark.parametrize("statement, attribute", [
    ("x = len.os", "os"),
    ("x = len(history).sys", "sys"),
    ("x = history[0].sys", "sys"),
    ("x = len(history.mro)", "mro"),
    ("x = history.boxplot()", "boxplot"),
    ("x = history['close'].hist", "hist"),
    ("x = history.plot.line", "plot"),
])
def test_a_denied_name_is_refused_on_any_base(statement, attribute):
    _assert_refused(_with_statement(statement), "banned_name", attribute)


@pytest.mark.parametrize("literal", ["func_globals", "f_globals", "gi_frame", "co_code",
                                     "os", "sys", "mro", "builtins"])
def test_a_denied_name_or_prefix_in_a_string_is_refused(literal):
    _assert_refused(_with_statement(f"x = history[{literal!r}]"), "string_literal", literal)


def test_a_denied_prefix_on_a_local_variable_is_allowed():
    assert check_source(_with_statement("f_score = 1")) == []


def test_a_dunder_in_a_bytes_literal_is_refused():
    assert "string_literal" in _rules(_with_statement("x = b'__globals__'"))


# --- robustness: never an exception ------------------------------------------

def test_a_file_too_deep_to_parse_is_one_violation():
    violations = check_source("x = " + "-" * 15_000 + "1\n")
    assert [v.rule for v in violations] == ["unparseable"]


def test_a_file_too_deep_to_walk_is_one_violation():
    violations = check_source("x = " + "a." * 2_000 + "b\n")
    assert [v.rule for v in violations] == ["unparseable"]


def test_a_file_with_a_null_byte_is_one_violation():
    violations = check_source(CLEAN + "\x00")
    assert [v.rule for v in violations] == ["unparseable"]


def test_a_file_that_is_not_utf8_encodable_is_one_violation():
    violations = check_source(CLEAN + "# \ud800\n")
    assert [v.rule for v in violations] == ["unparseable"]


# --- the ORB fixture stays clean (Task 6 owns the corpus) --------------------

def test_the_orb_fixture_passes():
    source = ORB_FIXTURE.read_text(encoding="utf-8")
    assert check_source(source) == []
    assert require_clean(source) == "OpeningRangeBreakout"


def test_a_source_that_is_not_a_string_is_one_violation():
    violations = check_source(b"x = 1\n")  # type: ignore[arg-type]
    assert [v.rule for v in violations] == ["unparseable"]


# --- names imported with from, and names a definition binds -------------------

@pytest.mark.parametrize("statement", [
    "from typing import operator",
    "from typing import types as t",
    "from typing import functools",
    "from dataclasses import inspect",
    "from dataclasses import copy",
    "from trader.authored.api import pd",
    "from trader.authored.api import math",
    "from trader.authored.api import abstractmethod",
])
def test_a_from_import_of_a_name_outside_the_allow_list_is_refused(statement):
    assert "from_import_not_allowed" in _rules(statement + "\n" + CLEAN)


@pytest.mark.parametrize("statement", [
    "from typing import Optional, Sequence",
    "from dataclasses import dataclass, field",
    "from trader.authored.api import ACTIONS, MAX_LOOKBACK_BARS, MIN_LOOKBACK_BARS",
])
def test_a_from_import_of_an_allowed_name_passes(statement):
    assert check_source(statement + "\n" + CLEAN) == []


@pytest.mark.parametrize("statement, rule", [
    ("from typing import Mapping as _m", "dunder_name"),
    ("from typing import Mapping as os", "banned_name"),
    ("import numpy as sys", "banned_name"),
    ("import numpy as _np", "dunder_name"),
])
def test_an_import_alias_is_checked_like_a_name(statement, rule):
    assert rule in _rules(statement + "\n" + CLEAN)


@pytest.mark.parametrize("definition", [
    "    def __init_subclass__(cls):\n        return None\n",
    "    def __getattribute__(self, name):\n        return None\n",
    "    def _helper(self):\n        return None\n",
])
def test_a_method_with_a_private_or_dunder_name_is_refused(definition):
    body = CLEAN.replace("    def on_bar", definition + "\n    def on_bar")
    assert "dunder_name" in _rules(body)


def test_a_parameter_with_a_dunder_name_is_refused():
    body = CLEAN.replace("def on_bar(self, history, params)", "def on_bar(self, history, params, __x=1)")
    assert "dunder_name" in _rules(body)


# --- the base class must be the real AuthoredStrategy -------------------------

MINIMAL_CLASS = "class Good(AuthoredStrategy):\n    def on_bar(self, history, params):\n        return None\n"


@pytest.mark.parametrize("header", [
    "AuthoredStrategy = object\n",
    "from typing import Mapping as AuthoredStrategy\n",
    "import numpy as AuthoredStrategy\n",
    "from trader.authored.api import Decision as AuthoredStrategy\n",
    "",
])
def test_a_base_not_imported_from_the_api_is_refused(header):
    assert "class_shape" in _rules(header + MINIMAL_CLASS)


@pytest.mark.parametrize("statement", [
    "AuthoredStrategy = history",
    "Decision = history",
    "x = [1 for AuthoredStrategy in history]",
])
def test_rebinding_an_api_name_is_refused(statement):
    assert "class_shape" in _rules(_with_statement(statement))


def test_rebinding_an_api_name_as_a_parameter_is_refused():
    body = CLEAN.replace("def on_bar(self, history, params)", "def on_bar(self, history, Decision)")
    assert "class_shape" in _rules(body)


@pytest.mark.parametrize("class_line", [
    "class Good(AuthoredStrategy, Mapping):",
    "class Good(AuthoredStrategy, metaclass=type):",
])
def test_a_class_with_extra_bases_or_keywords_is_refused(class_line):
    assert "class_shape" in _rules(CLEAN.replace("class Good(AuthoredStrategy):", class_line))


def test_a_relative_from_import_is_refused():
    body = CLEAN.replace("from trader.authored.api import", "from .trader.authored.api import")
    assert "from_import_not_allowed" in _rules(body)


def test_the_result_does_not_depend_on_the_warning_filter():
    import warnings
    source = _with_statement("x = '\\d'")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert check_source(source) == []


def test_parallel_checks_do_not_race_on_the_warning_filter():
    import sys
    import threading
    import warnings
    source = "x = '\\d'\n"  # SyntaxWarning; an error only if a thread sees the "error" filter
    unparseable = []

    def check_many():
        for _ in range(2_000):
            rules = {v.rule for v in check_source(source)}
            if "unparseable" in rules:
                unparseable.append(rules)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads often so the race shows
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            threads = [threading.Thread(target=check_many) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        sys.setswitchinterval(old_interval)
    assert unparseable == []



# --- controller probe, fix round 1 ---------------------------------------------

PROBE_HEAD = ("import numpy as np\nimport pandas as pd\n"
              "from trader.authored.api import AuthoredStrategy, Decision\n\n\n"
              "class S(AuthoredStrategy):\n    def on_bar(self, history, params):\n")


def _probe(body):
    lines = "".join("        " + line + "\n" for line in body.splitlines())
    return PROBE_HEAD + lines + "        return None\n"


@pytest.mark.parametrize("body, rule", [
    ("match history:\n    case object(__class__=c):\n        pass", "match_statement"),
    ("match self.on_bar:\n    case object(func_globals=g):\n        pass", "match_statement"),
    ("match history:\n    case object(system=s):\n        pass", "match_statement"),
    ("match history:\n    case 1:\n        pass", "match_statement"),
    ("y = \uff45val('1')", "banned_name"),
    ("y = history.\uff3f\uff3fclass\uff3f\uff3f", "unparseable"),
    ("y = type(history)", "banned_builtin"),
    ("y = type('X', (), {})", "banned_builtin"),
    ("y = object()", "banned_builtin"),
    ("y = isinstance(history, object)", "banned_builtin"),
    ("y = super()", "banned_builtin"),
    ("y = history.apply('sum')", "banned_name"),
    ("y = history.agg('sum')", "banned_name"),
    ("y = history.pipe(len)", "banned_name"),
    ("y = history.aggregate('sum')", "banned_name"),
    ("y = history.transform('sum')", "banned_name"),
    ("y = history.applymap(abs)", "banned_name"),
    ("y = b'__x'", "string_literal"),
    ("y = next(iter(history))", "banned_builtin"),
    ("y = dir(history)", "banned_builtin"),
    ("y = id(history)", "banned_builtin"),
    ("y = hasattr(history, 'close')", "banned_builtin"),
    ("print(1)", "banned_builtin"),
])
def test_a_probe_case_is_refused(body, rule):
    assert rule in _rules(_probe(body))


@pytest.mark.parametrize("body", [
    "x = 1",
    # A string built at runtime is harmless on its own: nothing that takes a name
    # string (getattr, format, operator, pandas apply/agg/pipe) is reachable.
    "k = '_' + '_class_' + '_'",
    "y = [v for v in history.close]",
    "y = f'{history.close}'",
    "y = (m := np.mean)",
    "y = dict(a=1)",
    "y = sorted(zip(range(3), reversed([1, 2, 3])))",
])
def test_a_harmless_probe_case_passes(body):
    assert check_source(_probe(body)) == []


@pytest.mark.parametrize("body", [
    "len = 1",
    "del len",
    "for sum in history:\n    pass",
    "y = [list for list in history]",
    "y = (min := 1)",
    "max += 1",
    "y = lambda type: 0",
])
def test_binding_a_builtin_name_is_refused(body):
    assert "banned_builtin" in _rules(_probe(body))


@pytest.mark.parametrize("source", [
    "import numpy as list\n" + CLEAN,
    "from typing import Any as id\n" + CLEAN,
    CLEAN.replace("def on_bar(self, history, params)", "def on_bar(self, history, max)"),
    CLEAN.replace("    def on_bar", "    def print(self):\n        return None\n\n    def on_bar"),
])
def test_shadowing_a_builtin_by_definition_or_import_is_refused(source):
    assert "banned_builtin" in _rules(source)



# --- fix round 2: node-type allow-list, type parameters, free names ------------

@pytest.mark.parametrize("body", [
    "try:\n    pass\nexcept* ValueError:\n    pass",
    "global q",
    "type X = int",
    "type X[len] = int",
    "with history as h:\n    pass",
    "raise history",
])
def test_a_node_type_outside_the_allow_list_is_refused(body):
    assert "node_type" in _rules(_probe(body))


@pytest.mark.parametrize("definition", [
    "    def helper[len](self):\n        return None\n",
    "    def helper[__class__](self):\n        return None\n",
    "    def helper[*shape](self):\n        return None\n",
    "    def helper[**spec](self):\n        return None\n",
])
def test_type_parameters_on_a_function_are_refused(definition):
    rules = _rules(CLEAN.replace("    def on_bar", definition + "\n    def on_bar"))
    assert {"node_type", "banned_statement"} <= rules


def test_type_parameters_on_the_class_are_refused():
    rules = _rules(CLEAN.replace("class Good(AuthoredStrategy):", "class Good[list](AuthoredStrategy):"))
    assert {"node_type", "banned_statement"} <= rules


@pytest.mark.parametrize("body", [
    "y = PythonFinalizationError",
    "y = undefined_name + 1",
    "y = \u1d3cpen",
])
def test_a_free_name_that_is_neither_bound_nor_an_allowed_builtin_is_refused(body):
    assert "unknown_name" in _rules(_probe(body))


PROBE2_HEAD = ("import numpy as np\nimport pandas as pd\nimport math\nimport statistics\n"
               "from trader.authored.api import AuthoredStrategy, Decision\n\n\n"
               "class S(AuthoredStrategy):\n    def on_bar(self, history, params):\n")

# Controller probe round 2: None means the case passes.
PROBE2_CASES = {
    "rolling_apply_callable": ("y = history.close.rolling(5).apply(len)", "banned_name"),
    "groupby_agg": ("y = history.groupby('close').agg('sum')", "banned_name"),
    "resample_agg": ("y = history.resample('1D').agg('sum')", "banned_name"),
    "series_map_str": ("y = history.close.map('{}'.upper)", None),
    "str_accessor_cat": ("y = history.close.astype(str).str.cat()", None),
    "isinstance_type_arg": ("y = isinstance(history, pd.DataFrame)", None),
    "dict_items_iter": ("y = [k for k in dict(a=1).items()]", None),
    "lambda_no_default": ("f = lambda v: v + 1", None),
    "nested_def": ("def inner(v):\n    return v\ny = inner(1)", None),
    "nested_class": ("class Inner:\n    pass", None),
    "self_attr_store": ("self.state = 1", None),
    "self_strategy_runtime": ("y = self.strategy_runtime", None),
    "self_ctx": ("y = self.ctx", None),
    "params_get": ("y = params.get('X')", None),
    "np_vectorize": ("y = np.vectorize(len)", "module_attribute_not_allowed"),
    "np_frompyfunc": ("y = np.frompyfunc(len, 1, 1)", "module_attribute_not_allowed"),
    "np_array_tolist": ("y = np.asarray([1]).tolist()", None),
    "np_array_tobytes": ("y = np.asarray([1]).tobytes()", None),
    "np_array_view": ("y = np.asarray([1]).view()", None),
    "df_to_dict": ("y = history.to_dict()", "io_attribute"),
    "df_values": ("y = history.values", None),
    "df_attrs": ("y = history.attrs", None),
    "df_set_flags": ("y = history.set_flags()", None),
    "series_name_assign": ("history.name = 'x'", None),
    "math_prod": ("y = math.prod([1, 2])", "module_attribute_not_allowed"),
    "statistics_mean": ("y = statistics.mean([1, 2])", None),
    "yield_from": ("yield 1", "banned_statement"),
    "global_stmt": ("global q", "banned_statement"),
    "with_stmt": ("with open('x') as f:\n    pass", "node_type"),
    "assert_stmt": ("assert True", None),
    "raise_stmt": ("raise ValueError('x')", "node_type"),
    "class_body_attr": ("y = self.LOOKBACK_BARS", None),
    "unicode_name_nfkc_open": ("y = \u1d3cpen", "unknown_name"),
    "starred_call": ("y = max(*[1, 2])", None),
    "kwargs_dict": ("y = dict(**{'a': 1})", None),
    "slice_step": ("y = history.close[::-1]", None),
    "chained_cmp": ("y = 1 < 2 < 3", None),
    "await_expr": ("y = await x", "banned_statement"),
}


@pytest.mark.parametrize("name", sorted(PROBE2_CASES))
def test_probe_round_two_verdicts(name):
    body, rule = PROBE2_CASES[name]
    lines = "".join("        " + line + "\n" for line in body.splitlines())
    rules = _rules(PROBE2_HEAD + lines + "        return None\n")
    if rule is None:
        assert rules == set()
    else:
        assert rule in rules


# --- fix round 3: free names follow Python scoping, nested class shape -------

SCOPED_HEAD = ("from trader.authored.api import AuthoredStrategy, Decision\n\n"
               "LIMIT = 3\n\n\n"
               "class S(AuthoredStrategy):\n")


@pytest.mark.parametrize("class_body", [
    # bound only in the class body; a method cannot see it
    "    PythonFinalizationError = 1\n\n"
    "    def on_bar(self, history, params):\n"
    "        y = PythonFinalizationError\n        return None\n",
    # a parameter of another method
    "    def other(self, secret):\n        return None\n\n"
    "    def on_bar(self, history, params):\n"
    "        y = secret\n        return None\n",
    # a lambda parameter outside its lambda
    "    def on_bar(self, history, params):\n"
    "        f = lambda hidden: hidden\n        y = hidden\n        return None\n",
    # a comprehension target after its comprehension
    "    def on_bar(self, history, params):\n"
    "        y = [item for item in history]\n        z = item\n        return None\n",
    # a nested function's local, read by its parent
    "    def on_bar(self, history, params):\n"
    "        def inner():\n            local = 1\n            return local\n"
    "        y = local\n        return None\n",
])
def test_a_name_bound_only_in_another_scope_is_unknown(class_body):
    assert "unknown_name" in _rules(SCOPED_HEAD + class_body)


@pytest.mark.parametrize("class_body", [
    # module-level constant read in a method
    "    def on_bar(self, history, params):\n"
    "        y = LIMIT\n        return None\n",
    # enclosing-function local read in a nested function and a lambda
    "    def on_bar(self, history, params):\n"
    "        scale = 2\n"
    "        def inner(v):\n            return v * scale\n"
    "        f = lambda v: v + scale\n"
    "        y = inner(1) + f(1)\n        return None\n",
    # a class-body name read in the class body itself
    "    WINDOW = 5\n    LOOKBACK_BARS = WINDOW * 10\n\n"
    "    def on_bar(self, history, params):\n        return None\n",
    # a comprehension reads its own target and the enclosing function
    "    def on_bar(self, history, params):\n"
    "        y = [item * LIMIT for item in history if item]\n        return None\n",
    # a walrus in a comprehension binds in the enclosing function
    "    def on_bar(self, history, params):\n"
    "        y = [last := item for item in history]\n        z = last\n        return None\n",
    # a method reads a name bound later in the same function
    "    def on_bar(self, history, params):\n"
    "        if history:\n            later = 1\n        return later\n",
])
def test_a_name_bound_in_a_visible_scope_passes(class_body):
    assert check_source(SCOPED_HEAD + class_body) == []


def test_a_comprehension_target_is_not_visible_in_its_first_iterable():
    body = ("    def on_bar(self, history, params):\n"
            "        y = [ghost for ghost in ghost]\n        return None\n")
    assert "unknown_name" in _rules(SCOPED_HEAD + body)


@pytest.mark.parametrize("nested", [
    "class Inner(metaclass=zip):\n    pass",
    "class Inner(Decision):\n    pass",
    "class Inner(dict):\n    pass",
])
def test_a_nested_class_with_bases_or_keywords_is_refused(nested):
    assert "class_shape" in _rules(_probe(nested))


def test_a_plain_nested_class_passes():
    assert check_source(_probe("class Inner:\n    pass")) == []
