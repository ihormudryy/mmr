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


def test_every_from_import_module_has_a_name_list():
    from trader.authored.allowed_api import ALLOWED_FROM_IMPORTS
    from trader.authored.static_check import ALLOWED_FROM_IMPORT_NAMES
    assert set(ALLOWED_FROM_IMPORT_NAMES) == set(ALLOWED_FROM_IMPORTS)


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
