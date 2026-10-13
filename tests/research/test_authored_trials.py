"""SP3 spec 7.7 and 10 (trial count on the case), Plan 4 ruling 12."""
from types import SimpleNamespace

import pytest

from tests.research.authored_fixtures import NOW, RESEARCH_DAY, install_fake_sandbox, research_world, time_of_day_key
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import ExperimentFamily
from trader.research.case_builder import build_initial_case, evaluation_summary
from trader.research.cohort_evaluation import evaluate_cohort
from trader.research.evaluation import TrialDenominator, trial_denominator
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations

PATH = "authored:sha256:" + "a" * 64


@pytest.fixture
def registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    return ExperimentRegistry(db)


def trials_of(registry, path, class_name, sharpes):
    family_id = registry.create_family(ExperimentFamily(
        strategy_path=path, class_name=class_name, repository_commit="authored", source_tree_digest="s",
        dependency_lock_digest="d", container_digest="unpinned", dataset_manifest_digest="m",
        search_space={"path": path}, cost_model={}, validation_protocol={}), created_at=NOW)
    for index, sharpe in enumerate(sharpes):
        trial_id = registry.start_trial(family_id, trial_key=f"t{index}", parameters={}, started_at=NOW)
        registry.finish_trial(trial_id, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": sharpe})


def test_an_authored_key_adds_the_judged_files_to_its_own_trials(registry):
    trials_of(registry, PATH, "Port", [0.2, -0.1])
    spec = SimpleNamespace(strategy_path=PATH, class_name="Port", authored_digest="sha256:" + "a" * 64)
    assert trial_denominator(registry, spec, authored_judged_count=3) == TrialDenominator(5, (0.2, -0.1))


def test_a_catalogue_key_counts_only_its_own_trials(registry):
    trials_of(registry, "strategies/x.py", "X", [0.4])
    spec = SimpleNamespace(strategy_path="strategies/x.py", class_name="X", authored_digest=None)
    assert trial_denominator(registry, spec) == TrialDenominator(1, (0.4,))
    with pytest.raises(ValueError):
        trial_denominator(registry, spec, authored_judged_count=1)


@pytest.mark.timeout(300)
def test_the_next_files_case_counts_the_judged_file(tmp_path, tmp_duckdb_path, monkeypatch):
    world = research_world(tmp_path, tmp_duckdb_path)
    sandbox = install_fake_sandbox(monkeypatch)
    spec = world.build(time_of_day_key(sandbox))
    result = evaluate_cohort(spec, research_db=world.db, paths=world.paths, now=lambda: NOW,
                             authored_judged_count=1)
    own = world.registry.strategy_trials(spec.base.strategy_path, "TimeOfDayPort")
    assert own and result.strategy_trials == 1 + len(own)
    case = build_initial_case(spec, RESEARCH_DAY, result, created_at=NOW, warmup_sessions=5)
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["strategy_trials"] >= 1 + len(own)
    assert (summary["strategy_path"], summary["class_name"]) == (spec.base.strategy_path, "TimeOfDayPort")
