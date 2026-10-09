import dataclasses
import datetime as dt
import json

import pytest

from tests.ai.fakes import config_text, write_config
from tests.research.evaluation_fixtures import TIME_OF_DAY_STRATEGY
from trader.ai.config import load_ai_config
from trader.ai.research_menu import ResearchMenu, build_menu, screen_picks
from trader.ai.research_roles import RESEARCH_MARKER, parse_research_proposal, research_messages
from trader.ai.untrusted import OutputRefusal
from trader.research.evaluation_request import EvaluationRequestBody

KEY = "strategies/time_of_day.py:TimeOfDay"
FLOATY = "from trader.trading.strategy import Strategy\n\nclass Floaty(Strategy):\n    RISK = 0.5\n    NAME = 'x'\n" \
         "    ON = True\n"
UNIVERSE = list(range(1001, 1009))


def load_research(tmp_path, block):
    return load_ai_config(str(write_config(tmp_path, config_text(extra_top_level=block)))).research


@pytest.fixture
def config(tmp_path):
    block = ("research:\n  enabled: true\n"
             f'  strategy_keys: ["{KEY}", "strategies/floaty.py:Floaty", "strategies/gone.py:Gone"]\n'
             "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n"
             "  max_candidates_per_cycle: 2\n  max_cohort_points: 2\n")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    (tmp_path / "strategies" / "floaty.py").write_text(FLOATY)
    return load_research(tmp_path, block)


def picks(*items):
    parsed = parse_research_proposal(json.dumps({"candidates": [
        {"strategy": s, "universe": u, "bar_size": b, "points": p, "thesis": "drift"} for s, u, b, p in items]}))
    assert not isinstance(parsed, OutputRefusal), parsed
    return parsed


def test_menu_has_numeric_tunables_and_leaves_out_missing_and_cooling(config, tmp_path):
    menu, dropped = build_menu(config, strategies_root=tmp_path, cooling=frozenset({KEY}))
    assert [c.strategy_key for c in menu.strategies.values()] == ["strategies/floaty.py:Floaty"]
    assert dict(menu.strategies["S1"].tunables) == {"RISK": 0.5}               # NAME (text) and ON (bool) are not
    assert {(d.code, d.detail) for d in dropped} == {("COOLING_DOWN", KEY),
                                                     ("STRATEGY_NOT_FOUND", "strategies/gone.py:Gone")}
    assert menu.universes["U1"] == ("us_eight", tuple(UNIVERSE))


def test_screen_drops_what_is_not_on_the_menu(config, tmp_path):                       # review focus 4
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    cohorts, dropped = screen_picks(picks(
        ("S9", "U1", "B1", [{}]),                                   # off-menu strategy
        ("S1", "U7", "B1", [{}]),                                   # off-menu universe
        ("S1", "U1", "B8", [{}]),                                   # off-menu bar size
        ("S1", "U1", "B3", [{"ENTRY_MINUTE": 615}, {"STOP": 1},     # undeclared tunable
                            {"EXIT_MINUTE": 645.0}, {"ENTRY_MINUTE": True}]),   # float for int, bool for int
        ("S2", "U1", "B1", [{"RISK": 1}])), menu)                   # an int is fine for a float default
    assert [(c.strategy_key, c.bar_size, c.points) for c in cohorts] == [
        (KEY, "15 mins", ({"ENTRY_MINUTE": 615, "EXIT_MINUTE": 660},)),
        ("strategies/floaty.py:Floaty", "1 min", ({"RISK": 1.0},))]
    assert [d.code for d in dropped] == ["OFF_MENU_STRATEGY", "OFF_MENU_UNIVERSE", "OFF_MENU_BAR_SIZE",
                                         "UNDECLARED_TUNABLE", "TUNABLE_TYPE", "TUNABLE_TYPE"]
    assert type(cohorts[1].points[0]["RISK"]) is float


def test_one_frozen_cohort_per_strategy_key(config, tmp_path):
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    cohorts, dropped = screen_picks(picks(
        ("S1", "U1", "B3", [{}, {"ENTRY_MINUTE": 615}]),                       # {} is the defaults
        ("S1", "U1", "B2", [{"ENTRY_MINUTE": 630}]),                           # other bar size: conflict
        ("S1", "U1", "B3", [{"ENTRY_MINUTE": 615}, {"ENTRY_MINUTE": 630}])), menu)
    defaults, moved = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}, {"ENTRY_MINUTE": 615, "EXIT_MINUTE": 660}
    assert cohorts[0].points == (defaults, moved)
    assert [d.code for d in dropped] == ["COHORT_CONFLICT", "DUPLICATE_POINT", "COHORT_POINT_LIMIT"]
    assert cohorts[0].submit_body() == {"kind": "INITIAL", "strategy_key": KEY, "cohort": [defaults, moved],
                                        "conids": list(range(1001, 1009)), "bar_size": "15 mins"}


@pytest.mark.parametrize("text", ["not json", '{"candidates": [{"strategy": "S1"}]}',
                                  '{"candidates": [], "conids": [4391]}',
                                  '{"candidates": [{"strategy": "S1", "universe": "U1", "bar_size": "B1", '
                                  '"points": [{"lower": 1}], "thesis": "x"}]}'])
def test_bad_proposals_are_refusals(text):
    assert isinstance(parse_research_proposal(text), OutputRefusal)


def test_candidate_limit_drops_the_extra_strategies(tmp_path):
    block = ("research:\n  enabled: true\n"
             f'  strategy_keys: ["{KEY}", "strategies/floaty.py:Floaty"]\n'
             "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n"
             "  max_candidates_per_cycle: 1\n")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    (tmp_path / "strategies" / "floaty.py").write_text(FLOATY)
    menu, _ = build_menu(load_research(tmp_path, block), strategies_root=tmp_path, cooling=frozenset())
    cohorts, dropped = screen_picks(picks(("S1", "U1", "B1", [{}]), ("S2", "U1", "B1", [{}])), menu)
    assert [c.strategy_key for c in cohorts] == [KEY]
    assert [d.code for d in dropped] == ["CANDIDATE_LIMIT"]


def test_a_non_finite_number_is_dropped_by_the_request_rules_and_takes_no_slot(config, tmp_path):
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    cohorts, dropped = screen_picks(parse_research_proposal(
        '{"candidates": [{"strategy": "S2", "universe": "U1", "bar_size": "B1", "points": [{"RISK": 1e999}], '
        '"thesis": "x"}, {"strategy": "S2", "universe": "U1", "bar_size": "B2", "points": [{"RISK": 2}], '
        '"thesis": "y"}]}'), menu)
    assert [d.code for d in dropped] == ["POINT_INVALID", "NO_VALID_POINTS"]
    assert [(c.bar_size, c.points) for c in cohorts] == [("5 mins", ({"RISK": 2.0},))]      # not a conflict


def test_every_kept_cohort_is_accepted_by_the_research_request_model(config, tmp_path):
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    cohorts, _ = screen_picks(picks(("S1", "U1", "B3", [{}, {"ENTRY_MINUTE": 615}]),
                                    ("S2", "U1", "B1", [{"RISK": 2}])), menu)
    for cohort in cohorts:
        body = {key: value for key, value in cohort.submit_body().items() if key != "kind"}
        assert EvaluationRequestBody.model_validate({**body, "research_day": "2026-10-08"})


def test_the_menu_keeps_only_eligible_bar_sizes_and_valid_universes(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "floaty.py").write_text(FLOATY)
    config = load_research(tmp_path, ("research:\n  enabled: true\n  strategy_keys: [\"strategies/floaty.py:Floaty\"]\n"
                                      "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n"
                                      '  bar_sizes: ["5 mins", "5 mins", "15 mins"]\n'))
    # a config object built outside the loader is checked again
    config = config.model_copy(update={
        "bar_sizes": ("5 mins", "5 mins", "1 hour", "nonsense", "15 mins"),
        "universes": {"dupes": (1001, 1001, 1002, 1003, 1004, 1005, 1006, 1007), "ok": tuple(range(1008, 1016))}})
    menu, dropped = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    assert dict(menu.bar_sizes) == {"B1": "5 mins", "B2": "15 mins"}
    assert [u[0] for u in menu.universes.values()] == ["ok"]
    assert {(d.code, d.detail) for d in dropped} == {
        ("BAR_SIZE_NOT_ELIGIBLE", "1 hour"), ("BAR_SIZE_NOT_ELIGIBLE", "nonsense"), ("UNIVERSE_INVALID", "dupes")}


def test_tunables_are_read_from_the_syntax_tree_and_the_file_never_runs(tmp_path):
    marker = tmp_path / "ran.txt"
    source = (f"open({str(marker)!r}, 'w').write('ran')\n"
              "from trader.trading.strategy import Strategy\n\nclass Loud(Strategy):\n    SPAN = 5\n"
              "    lower = 3\n    HUGE = 1e999\n")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "loud.py").write_text(source)
    config = load_research(tmp_path, ("research:\n  enabled: true\n  strategy_keys: [\"strategies/loud.py:Loud\"]\n"
                                      "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n"))
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    assert dict(menu.strategies["S1"].tunables) == {"SPAN": 5}
    assert not marker.exists()


def test_an_unreadable_strategy_file_leaves_the_menu_empty_but_does_not_raise(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "floaty.py").write_bytes(b"\xff\xfe\x00 not utf-8")
    config = load_research(tmp_path, ("research:\n  enabled: true\n  strategy_keys: [\"strategies/floaty.py:Floaty\"]\n"
                                      "  universes: {us_eight: [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]}\n"))
    menu, dropped = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    assert not menu.strategies
    assert [d.code for d in dropped] == ["STRATEGY_SCAN_FAILED"]


def test_the_prompt_carries_the_marker_and_only_code_built_facts(config, tmp_path):
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    system, user = research_messages(menu, session_date=dt.date(2026, 10, 8))
    assert system.content.startswith(RESEARCH_MARKER)
    facts = json.loads(user.content.split("\n", 1)[1])
    assert facts["session_date"] == "2026-10-08"
    assert facts["universes"] == [{"universe": "U1", "name": "us_eight", "conids": UNIVERSE}]
    assert [row["strategy"] for row in facts["strategies"]] == ["S1", "S2"]
    assert isinstance(menu, ResearchMenu)


def one_strategy_config(tmp_path, source, *, universe="[1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008]"):
    (tmp_path / "strategies").mkdir(exist_ok=True)
    (tmp_path / "strategies" / "odd.py").write_text(source)
    return load_research(tmp_path, ("research:\n  enabled: true\n  strategy_keys: [\"strategies/odd.py:Odd\"]\n"
                                    f"  universes: {{us_eight: {universe}}}\n"))


def test_a_strategy_without_a_numeric_tunable_is_left_out(tmp_path):
    source = ("from trader.trading.strategy import Strategy\n\nclass Odd(Strategy):\n    MODE = 'fast'\n"
              "    ON = True\n")
    menu, dropped = build_menu(one_strategy_config(tmp_path, source), strategies_root=tmp_path, cooling=frozenset())
    assert not menu.strategies
    assert [(d.code, d.detail) for d in dropped] == [("NO_NUMERIC_TUNABLES", "strategies/odd.py:Odd")]


def test_only_class_attribute_tunables_are_offered_not_params_keys(tmp_path):
    source = ("from trader.trading.strategy import Strategy\n\nclass Odd(Strategy):\n    SPAN = 5\n"
              "    def on_prices(self, prices):\n        return self.params.get('WINDOW', 7) + self.params.get('SPAN', 9)\n")
    menu, _ = build_menu(one_strategy_config(tmp_path, source), strategies_root=tmp_path, cooling=frozenset())
    assert dict(menu.strategies["S1"].tunables) == {"SPAN": 5}


def test_a_strategy_whose_only_tunables_are_params_keys_is_left_out(tmp_path):
    source = ("from trader.trading.strategy import Strategy\n\nclass Odd(Strategy):\n"
              "    def on_prices(self, prices):\n        return self.params.get('WINDOW', 7)\n")
    menu, dropped = build_menu(one_strategy_config(tmp_path, source), strategies_root=tmp_path, cooling=frozenset())
    assert not menu.strategies and [d.code for d in dropped] == ["NO_NUMERIC_TUNABLES"]


def test_a_point_the_service_cannot_build_neighbours_for_is_dropped(tmp_path):
    source = ("from trader.trading.strategy import Strategy\n\nclass Odd(Strategy):\n    BAND = 0.0\n    SPAN = 5\n")
    menu, _ = build_menu(one_strategy_config(tmp_path, source), strategies_root=tmp_path, cooling=frozenset())
    only_band = dataclass_menu(menu, {"BAND": 0.0})
    cohorts, dropped = screen_picks(picks(("S1", "U1", "B1", [{}])), only_band)
    assert cohorts == () and [d.code for d in dropped] == ["POINT_INVALID", "NO_VALID_POINTS"]
    cohorts, _ = screen_picks(picks(("S1", "U1", "B1", [{}])), menu)        # SPAN = 5 gives neighbours
    assert len(cohorts) == 1


def dataclass_menu(menu, tunables):
    choice = dataclasses.replace(menu.strategies["S1"], tunables=tunables)
    return dataclasses.replace(menu, strategies={"S1": choice})


@pytest.mark.parametrize("value", [10 ** 400, 2 ** 53, -(2 ** 53), 10 ** 20], ids=["400digits", "2^53", "-2^53", "1e20"])
def test_a_huge_integer_is_dropped_not_raised(config, tmp_path, value):
    menu, _ = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    cohorts, dropped = screen_picks(picks(("S2", "U1", "B1", [{"RISK": value}]),
                                          ("S1", "U1", "B1", [{"ENTRY_MINUTE": value}])), menu)
    assert cohorts == ()
    assert {d.code for d in dropped} == {"POINT_INVALID", "NO_VALID_POINTS"}


def test_the_service_universe_size_is_enforced_again(tmp_path):
    source = "from trader.trading.strategy import Strategy\n\nclass Odd(Strategy):\n    SPAN = 5\n"
    config = one_strategy_config(tmp_path, source).model_copy(update={"universes": {
        "small": tuple(range(1, 8)), "big": tuple(range(1, 22)), "fine": tuple(range(1, 9))}})
    menu, dropped = build_menu(config, strategies_root=tmp_path, cooling=frozenset())
    assert [name for name, _ in menu.universes.values()] == ["fine"]
    assert {(d.code, d.detail) for d in dropped} == {("UNIVERSE_INVALID", "small"), ("UNIVERSE_INVALID", "big")}
