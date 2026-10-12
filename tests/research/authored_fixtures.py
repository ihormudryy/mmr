"""Tests only: authored strategies on the synthetic research market of evaluation_fixtures."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tests.authored.fake_sandbox import FakeSandbox, caller_source
from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                               write_universe)
from trader.authored.keys import authored_key
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research.cohort import build_cohort_spec
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_request import EvaluationRequestBody
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.simulation.execution_costs import load_execution_costs_config

NOW = dt.datetime(2024, 3, 29, 14, tzinfo=dt.timezone.utc)          # 10:00 New York, 2024-03-29
RESEARCH_DAY = "2024-03-29"
POINT = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
CATALOGUE_KEY = "strategies/time_of_day.py:TimeOfDay"
CONFIG = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5,
                               authored_evaluation_sessions=38)   # equal to the period: Tasks 3-8 keep their windows


def install_fake_sandbox(monkeypatch) -> FakeSandbox:
    fake = FakeSandbox()
    monkeypatch.setattr("trader.research.authored_backtest.connect_sandbox", lambda socket_path: fake)
    return fake


def time_of_day_key(sandbox: FakeSandbox, variant: str = "") -> str:
    """``variant`` (a comment) makes another file with the same behaviour and another digest."""
    return authored_key(sandbox.add(caller_source("time_of_day_port") + variant), "TimeOfDayPort")


def request_body(key: str, cohort=(POINT,)) -> EvaluationRequestBody:
    return EvaluationRequestBody.model_validate(
        {"strategy_key": key, "cohort": [dict(point) for point in cohort], "conids": CONIDS,
         "bar_size": "15 mins", "research_day": RESEARCH_DAY})


def research_world(tmp_path, tmp_duckdb_path, *, drift: float = 0.0006, config=CONFIG) -> SimpleNamespace:
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=drift)
    build_spec_file(tmp_path)                                        # the catalogue TimeOfDay, for comparisons
    costs_path = write_costs_config(tmp_path / "execution_costs.yaml")
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    paths = EvaluationPaths(history_db=tmp_duckdb_path, universe_db=tmp_duckdb_path, universe_library="Universes",
                            execution_costs=str(costs_path), repo_root=tmp_path, reports_dir=tmp_path / "reports",
                            summaries_dir=tmp_path / "summaries")
    judge = BacktestJudgeConfig(strategy_allowlist=(CATALOGUE_KEY,), max_cohort_points=3)

    def build_body(body: EvaluationRequestBody):
        return build_cohort_spec(body, config=config, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=load_execution_costs_config(str(costs_path)), repo_root=tmp_path,
                                 registry=registry, history_db=tmp_duckdb_path)

    def build(key: str, cohort=(POINT,)):
        return build_body(request_body(key, cohort))

    return SimpleNamespace(build=build, build_body=build_body, paths=paths, db=db, registry=registry, root=tmp_path)
