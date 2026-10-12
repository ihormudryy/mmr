import json

import pytest

from tests.ai.author.helpers import BAD_SOURCE, ORB_NOTE, ORB_SOURCE, ORB_TUNABLES, answer_json
from trader.ai.author_roles import (AUTHOR_MARKER, AUTHOR_RULES, CheckedAnswer, author_messages, check_answer,
                                   class_tunables, parse_author_answer, refusal_problems, retry_messages,
                                   trader_problems)
from trader.ai.untrusted import OutputRefusal
from trader.authored.keys import authored_key, source_digest
from trader.authored.static_check import StaticViolation
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_refusals import AuthoredRefused
from trader.automation.authored_submission import check_submission, class_constants
from trader.automation.authored_wire import SubmitAuthoredStrategyRequest

BAR_SIZES = ("5 mins", "15 mins")


def checked(text=None, **fields):
    answer = parse_author_answer(text or answer_json(**fields))
    assert not isinstance(answer, OutputRefusal), answer
    return check_answer(answer, bar_sizes=BAR_SIZES)


def test_the_orb_port_passes_and_code_sets_digest_class_and_defaults():
    result = checked()
    assert isinstance(result, CheckedAnswer)
    digest = source_digest(ORB_SOURCE.encode("utf-8"))
    assert (result.class_name, result.digest) == ("OpeningRangeBreakout", digest)
    assert result.authored_key == authored_key(digest, "OpeningRangeBreakout")
    assert result.tunables == {"RANGE_BARS": {"default": 2.0, "min": 1.0, "max": 4.0},
                               "ENTRY_MINUTE": {"default": 600.0, "min": 585.0, "max": 630.0},
                               "EXIT_MINUTE": {"default": 660.0, "min": 630.0, "max": 720.0}}
    assert result.submit_body("vendor/author-1") == {      # Plan 3's request: no class; default = class value (D5)
        "source": ORB_SOURCE,
        "tunables": {"ENTRY_MINUTE": {"low": 585.0, "high": 630.0, "default": 600.0},
                     "EXIT_MINUTE": {"low": 630.0, "high": 720.0, "default": 660.0},
                     "RANGE_BARS": {"low": 1.0, "high": 4.0, "default": 2.0}},
        "bar_size": "15 mins", "note": ORB_NOTE, "author_model": "vendor/author-1"}


def test_a_static_violation_comes_back_as_plan_1_violations():
    problems = checked(source=BAD_SOURCE)
    assert problems and all(isinstance(p, StaticViolation) for p in problems)


@pytest.mark.parametrize("fields,rule", [                                             # review focus 3
    ({"bar_size": "1 hour"}, "BAR_SIZE_NOT_ALLOWED"),
    ({"tunables": ORB_TUNABLES[:2]}, "TUNABLE_WITHOUT_RANGE"),
    ({"tunables": ORB_TUNABLES + [{"name": "LOOKBACK_BARS", "minimum": 10, "maximum": 50}]}, "TUNABLE_UNKNOWN"),
    ({"tunables": [{**ORB_TUNABLES[0], "maximum": 1}] + ORB_TUNABLES[1:]}, "TUNABLE_RANGE"),
    ({"tunables": [{**ORB_TUNABLES[1], "minimum": 601}] + ORB_TUNABLES[::2]}, "TUNABLE_RANGE"),
    ({"tunables": ORB_TUNABLES + ORB_TUNABLES[:1]}, "TUNABLE_DUPLICATE"),
])
def test_bar_size_and_tunables_are_checked_by_code(fields, rule):
    problems = checked(**fields)
    assert isinstance(problems, list) and rule in {p.rule for p in problems}


@pytest.mark.parametrize("text", [answer_json(note="x" * 601), answer_json(digest="sha256:" + "0" * 64),
                                  answer_json(tunables=[{"name": "bad name", "minimum": 1, "maximum": 2}]),
                                  "no json at all"])
def test_an_answer_of_another_shape_is_refused(text):
    refusal = parse_author_answer(text)
    assert isinstance(refusal, OutputRefusal)
    (problem,) = refusal_problems(refusal)
    assert problem.rule == refusal.code


def test_class_tunables_reads_numbers_only_and_skips_lookback():
    source = ("from trader.authored.api import AuthoredStrategy\n\nclass A(AuthoredStrategy):\n"
              "    LOOKBACK_BARS = 30\n    LOW = -1.5\n    FLAG = True\n    NAME = 'x'\n    lower = 3\n"
              "    def on_bar(self, history, params):\n        return None\n")
    assert class_tunables(source, "A") == {"LOW": -1.5}


def test_the_retry_carries_the_answer_and_the_problems():
    first = author_messages({"bar_sizes": list(BAR_SIZES)})
    again = retry_messages(first, "previous answer", [StaticViolation("IMPORT_NOT_ALLOWED", 1, "os")])
    assert again[:2] == first and again[2].role == "assistant" and again[2].content == "previous answer"
    assert AUTHOR_MARKER in again[3].content and "IMPORT_NOT_ALLOWED" in again[3].content
    assert "last try" in again[3].content and AUTHOR_MARKER in first[0].content
    assert json.loads(first[1].content.split("\n", 1)[1]) == {"bar_sizes": list(BAR_SIZES)}


# --- G1: a constant bound more than once is not a tunable to the trader ---------------------------------------

BARE_ANNOTATION = ORB_SOURCE.replace("    RANGE_BARS = 2", "    RANGE_BARS: int\n    RANGE_BARS = 2")
BOUND_TWICE = ORB_SOURCE.replace("    RANGE_BARS = 2", "    RANGE_BARS = 3\n    RANGE_BARS = 2")
NO_CONSTANTS = ORB_SOURCE.replace("    RANGE_BARS = 2\n    ENTRY_MINUTE = 600\n    EXIT_MINUTE = 660\n", "")


@pytest.mark.parametrize("source", [BARE_ANNOTATION, BOUND_TWICE])
def test_a_rebound_tunable_is_refused_locally(source):
    problems = checked(source=source)
    assert isinstance(problems, list)
    assert [p.rule for p in problems] == ["TUNABLE_REBOUND"]
    assert "RANGE_BARS" in problems[0].detail


def test_an_undeclared_rebound_constant_passes_like_at_the_trader():
    source = BOUND_TWICE.replace("    RANGE_BARS = 3\n    RANGE_BARS = 2", "    RANGE_BARS = 2\n    FIXED = 1\n    FIXED = 2")
    result = checked(source=source)
    assert isinstance(result, CheckedAnswer), result
    assert "FIXED" not in result.tunables
    check_submission(_request(result.submit_body("vendor/author-1")), _config())


def test_the_rules_tell_the_model_to_bind_each_constant_once():
    assert any("once" in rule and "annotation" in rule for rule in AUTHOR_RULES)


def test_the_trader_submission_check_pins_the_author_judgment_code():
    from trader.ai.tools import JUDGMENT_SOURCES, TRADER_ROOT
    assert TRADER_ROOT / "automation" / "authored_submission.py" in JUDGMENT_SOURCES


# --- G2: everything the trader's check_submission refuses is caught before the submit ---------------------------

def _config():
    return AuthoredStrategiesConfig(enabled=True, bar_sizes=BAR_SIZES)


def _request(body):
    return SubmitAuthoredStrategyRequest.model_validate(json.loads(json.dumps(body)))


def _trader_code(source, tunables, bar_size):
    """What the trader answers to the body an answer stands for (default = the class value, or 0 if none)."""
    constants = class_constants(source, "OpeningRangeBreakout")
    body = {"source": source, "bar_size": bar_size, "note": ORB_NOTE, "author_model": "vendor/author-1",
            "tunables": {t["name"]: {"low": t["minimum"], "high": t["maximum"],
                                     "default": constants.get(t["name"], 0)} for t in tunables}}
    with pytest.raises(AuthoredRefused) as refused:
        check_submission(_request(body), _config())
    return refused.value.code


def _with(name, **bounds):
    return [{**t, **bounds} if t["name"] == name else t for t in ORB_TUNABLES]


@pytest.mark.parametrize("source,tunables,bar_size,code,rule", [
    (BAD_SOURCE, ORB_TUNABLES, "15 mins", "AUTHORED_STATIC_REJECTED", "import_not_allowed"),
    (ORB_SOURCE, ORB_TUNABLES, "1 hour", "AUTHORED_BAD_BAR_SIZE", "BAR_SIZE_NOT_ALLOWED"),
    (NO_CONSTANTS, [], "15 mins", "AUTHORED_BAD_TUNABLES", "AUTHORED_BAD_TUNABLES"),      # zero tunables
    (ORB_SOURCE, ORB_TUNABLES + [{"name": "LOOKBACK_BARS", "minimum": 10, "maximum": 50}], "15 mins",
     "AUTHORED_BAD_TUNABLES", "TUNABLE_UNKNOWN"),
    (ORB_SOURCE, ORB_TUNABLES + [{"name": "NOPE", "minimum": 1, "maximum": 2}], "15 mins",
     "AUTHORED_BAD_TUNABLES", "TUNABLE_UNKNOWN"),
    (ORB_SOURCE, _with("EXIT_MINUTE", maximum=1e16), "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),
    (ORB_SOURCE, _with("EXIT_MINUTE", minimum=-(10 ** 400)), "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),
    (ORB_SOURCE, _with("RANGE_BARS", minimum=4, maximum=1), "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),
    (ORB_SOURCE, _with("RANGE_BARS", minimum=3), "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),
    (ORB_SOURCE.replace("RANGE_BARS = 2", "RANGE_BARS = 10000000000000000"), ORB_TUNABLES, "15 mins",
     "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),                                               # class value > 1e15
    (ORB_SOURCE, [{"name": "RANGE_BARS", "minimum": 2, "maximum": 2}, {"name": "ENTRY_MINUTE", "minimum": 600,
     "maximum": 600}, {"name": "EXIT_MINUTE", "minimum": 660, "maximum": 660}], "15 mins",
     "AUTHORED_BAD_TUNABLES", "TUNABLE_RANGE"),                                               # nothing to vary
    (BARE_ANNOTATION, ORB_TUNABLES, "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_REBOUND"),
    (BOUND_TWICE, ORB_TUNABLES, "15 mins", "AUTHORED_BAD_TUNABLES", "TUNABLE_REBOUND"),
])
def test_every_trader_refusal_is_caught_locally(source, tunables, bar_size, code, rule):
    assert _trader_code(source, tunables, bar_size) == code
    problems = checked(source=source, tunables=tunables, bar_size=bar_size)
    assert isinstance(problems, list) and rule in {p.rule for p in problems}, problems


def test_a_name_that_is_not_an_upper_case_constant_is_refused_at_parse_like_at_the_trader():
    tunables = [{"name": "Range_bars", "minimum": 1, "maximum": 4}] + ORB_TUNABLES[1:]
    assert _trader_code(ORB_SOURCE, tunables, "15 mins") == "AUTHORED_BAD_TUNABLES"
    assert isinstance(parse_author_answer(answer_json(tunables=tunables)), OutputRefusal)


@pytest.mark.parametrize("change,code", [
    ({"source": BAD_SOURCE}, "AUTHORED_STATIC_REJECTED"),
    ({"bar_size": "1 hour"}, "AUTHORED_BAD_BAR_SIZE"),
    ({"tunables": {}}, "AUTHORED_BAD_TUNABLES"),
])
def test_each_trader_refusal_code_becomes_one_violation_with_its_detail(change, code):
    body = {**checked().submit_body("vendor/author-1"), **change}
    with pytest.raises(AuthoredRefused) as refused:
        check_submission(_request(body), _config())
    (problem,) = trader_problems(body, bar_sizes=BAR_SIZES)
    assert (problem.rule, problem.line, problem.detail) == (code, 0, refused.value.detail)


def test_a_clean_answer_is_also_clean_at_the_trader():
    check_submission(_request(checked().submit_body("vendor/author-1")), _config())
