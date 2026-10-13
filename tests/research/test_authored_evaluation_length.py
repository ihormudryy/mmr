"""Owner ruling 2026-10-11 (spec 14): an authored evaluation spans the most recent authored_evaluation_sessions
sessions with SP2c's fold and holdout proportions; a catalogue evaluation is unchanged."""
from dataclasses import replace

import exchange_calendars as xcals
import pandas as pd
import pytest

from tests.authored.fake_sandbox import FakeSandbox
from tests.research.authored_fixtures import CATALOGUE_KEY, CONFIG, research_world, time_of_day_key
from trader.research.service_config import (ResearchConfigError, ResearchServiceConfig, authored_walk_forward,
                                            load_research_service_config)

SHORT = replace(CONFIG, authored_evaluation_sessions=20)        # 38 sessions, holdout 5 -> 20 sessions, holdout 3


def sessions(start, end) -> int:
    return len(xcals.get_calendar("XNYS").sessions_in_range(pd.Timestamp(start), pd.Timestamp(end)))


def test_the_default_is_302_with_sp2c_proportions():
    config = load_research_service_config({})
    assert (config.period_sessions, config.authored_evaluation_sessions) == (690, 302)
    short = authored_walk_forward(config)
    assert (short.period_sessions, short.holdout_sessions, short.folds, short.embargo_sessions) == (302, 39, 6, 5)
    full = replace(config, authored_evaluation_sessions=690)
    assert authored_walk_forward(full) == full


def test_a_config_built_in_code_with_a_shorter_period_keeps_its_period():
    # A test or SP2c world that builds ResearchServiceConfig directly (period 38 or 40) keeps the 302 default;
    # the authored length never exceeds the period.
    short = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    assert authored_walk_forward(short) == short


@pytest.mark.parametrize("value", [0, True, 691, "302"])
def test_a_bad_authored_length_fails_load(value):
    with pytest.raises(ResearchConfigError, match="authored_evaluation_sessions"):
        load_research_service_config({"research_service": {"authored_evaluation_sessions": value}})


def test_a_length_too_short_for_the_folds_fails_load():
    with pytest.raises(ResearchConfigError, match="authored_evaluation_sessions"):
        load_research_service_config({"research_service": {"authored_evaluation_sessions": 20}})


def test_an_authored_evaluation_spans_the_authored_length(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    spec = world.build(time_of_day_key(FakeSandbox()))
    assert sessions(spec.base.period_start, spec.base.period_end) == 20
    assert (spec.base.holdout_sessions, spec.base.folds, spec.base.embargo_sessions) == (3, 2, 1)


def test_a_catalogue_evaluation_keeps_the_full_length(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    spec = world.build(CATALOGUE_KEY)
    assert sessions(spec.base.period_start, spec.base.period_end) == 38
    assert (spec.base.holdout_sessions, spec.base.folds, spec.base.embargo_sessions) == (5, 2, 1)


def test_both_periods_end_on_the_same_last_session(tmp_path, tmp_duckdb_path):
    world = research_world(tmp_path, tmp_duckdb_path, config=SHORT)
    authored, catalogue = world.build(time_of_day_key(FakeSandbox())), world.build(CATALOGUE_KEY)
    assert authored.base.period_end == catalogue.base.period_end         # "the most recent" sessions
    assert authored.base.period_start > catalogue.base.period_start
