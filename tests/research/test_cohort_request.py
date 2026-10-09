import datetime as dt

import pytest

from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                               write_universe)
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.cohort import (RequestRefused, build_cohort_spec, neighbours_of, request_body,
                                    previously_revealed, require_fresh_holdout)
from trader.research.evaluation_request import evaluation_request_id
from trader.research.service_config import (ResearchConfigError, ResearchServiceConfig,
                                            load_research_service_config)
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
TODAY = dt.date(2024, 3, 29)
CONFIG = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
JUDGE = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)


class Windows:
    def __init__(self, windows=()):
        self.windows = list(windows)

    def opened_holdout_windows(self, path, cls):
        return self.windows


def raw(**overrides):
    return {"strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600}], "conids": CONIDS, "bar_size": "15 mins",
            **overrides}


@pytest.fixture
def build(tmp_path, tmp_duckdb_path):
    from trader.data.universe import UniverseAccessor
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))

    def _build(request, windows=(), judge=JUDGE, history_db=tmp_duckdb_path):
        return build_cohort_spec(request_body(request, TODAY), config=CONFIG, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=costs, repo_root=tmp_path, registry=Windows(windows),
                                 history_db=history_db)
    return _build


def test_the_service_sets_the_day_so_the_id_changes_per_day():
    today, tomorrow = request_body(raw(), TODAY), request_body(raw(), dt.date(2024, 4, 1))
    assert today.research_day == "2024-03-29"
    assert evaluation_request_id(today) != evaluation_request_id(tomorrow)
    with pytest.raises(RequestRefused) as refused:
        request_body({**raw(), "research_day": "2020-01-01"}, TODAY)     # the caller cannot pick the day
    assert refused.value.code == "REQUEST_INVALID"


def test_neighbours_are_code_derived():
    assert neighbours_of({"A": 600}) == ({"A": 540}, {"A": 660})
    assert neighbours_of({"A": 3, "B": 1.5}) == ({"A": 2, "B": 1.5}, {"A": 4, "B": 1.5},
                                                 {"A": 3, "B": 1.35}, {"A": 3, "B": 1.65})
    assert neighbours_of({"FLAG": True, "NAME": "x"}) == ()


def test_a_good_request_freezes_period_cohort_and_file_hash(build):
    spec = build(raw(cohort=[{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}]))
    assert spec.base.period_end == dt.date(2024, 3, 28) and spec.base.holdout_sessions == 5
    assert [len(n) for n in spec.neighbours] == [2, 2] and spec.file_hash.startswith("sha256:")
    assert spec.request_id == evaluation_request_id(spec.body)


@pytest.mark.parametrize("request_raw,code", [
    (raw(strategy_key="strategies/other.py:Other"), "STRATEGY_NOT_ALLOWED"),
    (raw(cohort=[{"ENTRY_MINUTE": m} for m in (600, 615, 630, 645)]), "COHORT_TOO_LARGE"),
    (raw(cohort=[{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 600}]), "REQUEST_INVALID"),
    (raw(conids=list(reversed(CONIDS))), "REQUEST_INVALID"),
    (raw(cohort=[{"UNKNOWN": 1}]), "SPEC_INVALID"),
    (raw(cohort=[{"ENTRY_MINUTE": 600}, {"NOT_A_TUNABLE": 1}]), "COHORT_POINT_INVALID"),
    (raw(cohort=[{"NAME": "x"}]), "COHORT_POINT_INVALID"),
    (raw(conids=CONIDS[:7]), "CONIDS_OUT_OF_SCOPE"),
    (raw(bar_size="1 hour"), "REQUEST_INVALID"),
    ({**raw(), "extra": 1}, "REQUEST_INVALID"),
])
def test_bad_requests_are_refused_by_code(build, request_raw, code):
    with pytest.raises(RequestRefused) as refused:
        build(request_raw)
    assert refused.value.code == code


def test_a_request_without_its_bars_is_refused_retryable(build, tmp_path):
    with pytest.raises(RequestRefused) as refused:
        build(raw(), history_db=str(tmp_path / "empty.duckdb"))
    assert (refused.value.code, refused.value.retryable) == ("BARS_MISSING", True)
    assert "no 15 mins bars" in refused.value.detail and "SPY" in refused.value.detail


def test_other_refusals_are_not_retryable(build):
    with pytest.raises(RequestRefused) as refused:
        build(raw(strategy_key="strategies/other.py:Other"))
    assert refused.value.retryable is False


def test_a_revealed_window_blocks_any_holdout_that_does_not_start_after_it(build):
    revealed = {"artifact_id": "a", "family_id": "f", "start": dt.date(2024, 3, 20), "end": dt.date(2024, 3, 27)}
    with pytest.raises(RequestRefused) as refused:
        build(raw(), windows=[revealed])                              # the new holdout ends 2024-03-28
    assert refused.value.code == "HOLDOUT_NOT_AVAILABLE"
    require_fresh_holdout([revealed], dt.date(2024, 3, 28))          # starts after: allowed
    with pytest.raises(RequestRefused):
        require_fresh_holdout([revealed], dt.date(2024, 3, 27))      # a shifted window still touches it


def test_config_block_refuses_unknown_keys_and_bad_values():
    assert load_research_service_config({}) == ResearchServiceConfig()
    with pytest.raises(ValueError):
        load_research_service_config({"research_service": {"folds": 0}})
    with pytest.raises(ValueError):
        load_research_service_config({"research_service": {"surprise": 1}})


@pytest.mark.parametrize("block", [
    {"period_sessions": 30, "folds": 6, "embargo_sessions": 5, "holdout_sessions": 5},    # 25 / 7 = 3 < 6
    {"period_sessions": 10, "folds": 6, "embargo_sessions": 0, "holdout_sessions": 5},    # pool 5 < 7 segments
    {"period_sessions": 100, "folds": 6, "embargo_sessions": 5, "holdout_sessions": 100},  # no pool at all
])
def test_config_refuses_a_walk_forward_it_cannot_build(block):
    with pytest.raises(ResearchConfigError, match="research_service"):
        load_research_service_config({"research_service": block})


@pytest.mark.parametrize("block", [0, [], False, "", "x", 5])
def test_config_refuses_a_present_non_mapping_block(block):
    with pytest.raises(ResearchConfigError):
        load_research_service_config({"research_service": block})


def test_config_refuses_huge_numbers_and_mixed_key_types_as_config_errors():
    with pytest.raises(ResearchConfigError):
        load_research_service_config({"research_service": {"order_notional": 10**400}})
    with pytest.raises(ResearchConfigError):
        load_research_service_config({"research_service": {"account_equity": float("inf")}})
    with pytest.raises(ResearchConfigError):
        load_research_service_config({"research_service": {1: 1, "surprise": 2}})


def test_a_walk_forward_the_calendar_cannot_build_is_refused_as_spec_invalid(tmp_path, tmp_duckdb_path):
    from trader.data.universe import UniverseAccessor
    write_universe(tmp_duckdb_path)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))
    too_many_folds = ResearchServiceConfig(period_sessions=38, folds=30, embargo_sessions=1, holdout_sessions=5)
    with pytest.raises(RequestRefused) as refused:
        build_cohort_spec(request_body(raw(), TODAY), config=too_many_folds, judge=JUDGE,
                          universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                          costs_config=costs, repo_root=tmp_path, registry=Windows(),
                          history_db=tmp_duckdb_path)
    assert refused.value.code == "SPEC_INVALID" and "walk_forward" in refused.value.detail


@pytest.mark.parametrize("block", [
    {"period_sessions": 10**400}, {"folds": 10**400}, {"queue_max": 10**400},
    {"shadow_incomplete_after_hours": 10**400}, {"period_sessions": 2521},
    {"shadow_incomplete_after_hours": 721},
])
def test_config_refuses_huge_integers_as_config_errors(block):
    with pytest.raises(ResearchConfigError, match="research_service"):
        load_research_service_config({"research_service": block})


def test_config_accepts_the_largest_bounded_values():
    config = load_research_service_config({"research_service": {"period_sessions": 2520,
                                                                 "shadow_incomplete_after_hours": 720}})
    assert (config.period_sessions, config.shadow_incomplete_after_hours) == (2520, 720)


def test_a_period_the_calendar_does_not_cover_is_refused_as_spec_invalid(tmp_path, tmp_duckdb_path):
    from trader.data.universe import UniverseAccessor
    write_universe(tmp_duckdb_path)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))
    before_the_calendar = ResearchServiceConfig(period_sessions=100_000, folds=2, embargo_sessions=1,
                                                holdout_sessions=5)
    with pytest.raises(RequestRefused) as refused:
        build_cohort_spec(request_body(raw(), TODAY), config=before_the_calendar, judge=JUDGE,
                          universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                          costs_config=costs, repo_root=tmp_path, registry=Windows(),
                          history_db=tmp_duckdb_path)
    assert refused.value.code == "SPEC_INVALID" and "period" in refused.value.detail


def test_previously_revealed_lists_sessions_inside_a_window_including_both_ends():
    window = {"start": dt.date(2024, 3, 20), "end": dt.date(2024, 3, 22)}
    sessions = [dt.date(2024, 3, 19), dt.date(2024, 3, 20), dt.date(2024, 3, 21), dt.date(2024, 3, 22),
                dt.date(2024, 3, 25)]
    assert previously_revealed([window], sessions) == ["2024-03-20", "2024-03-21", "2024-03-22"]
    assert previously_revealed([window], [dt.date(2024, 3, 25)]) == []
    assert previously_revealed([], sessions) == []
