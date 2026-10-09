import datetime as dt

from tests.test_backtest_store import _make_record
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.artifact import TRIAL_SUCCEEDED, ExperimentFamily
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.strategy_paths import repo_root

T0 = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)


def _registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    return ExperimentRegistry(db)


def _family(path, minutes):
    return ExperimentFamily(
        strategy_path=path, class_name='RSIStrategy', repository_commit='c',
        source_tree_digest='s', dependency_lock_digest='d', container_digest='x',
        dataset_manifest_digest='m', search_space={'MINUTES': [minutes]},
        cost_model={}, validation_protocol={})


def _trial(registry, family_id, key, finish=True):
    tid = registry.start_trial(family_id, trial_key=key, parameters={}, started_at=T0)
    if finish:
        registry.finish_trial(tid, status=TRIAL_SUCCEEDED, finished_at=T0, metrics={'daily_sharpe': 0.1})
    return tid


def test_trials_of_all_families_of_one_strategy_are_counted(tmp_path):
    registry = _registry(tmp_path)
    relative = registry.create_family(_family('strategies/rsi.py', 15), created_at=T0)
    absolute = registry.create_family(
        _family(str(repo_root() / 'strategies' / 'rsi.py'), 30), created_at=T0)
    other = registry.create_family(_family('strategies/other.py', 15), created_at=T0)
    _trial(registry, relative, 'a')
    _trial(registry, absolute, 'b')
    _trial(registry, absolute, 'running', finish=False)
    _trial(registry, other, 'c')

    trials = registry.strategy_trials('./strategies/rsi.py', 'RSIStrategy')

    assert sorted(t.trial_key for t in trials) == ['a', 'b']


def test_interrupted_trials_of_every_family_of_one_strategy_are_closed_and_then_counted(tmp_path):
    registry = _registry(tmp_path)
    older = registry.create_family(_family('strategies/rsi.py', 15), created_at=T0)
    newer = registry.create_family(_family(str(repo_root() / 'strategies' / 'rsi.py'), 30), created_at=T0)
    other = registry.create_family(_family('strategies/other.py', 15), created_at=T0)
    stranded = [_trial(registry, older, 'crashed', finish=False), _trial(registry, newer, 'running', finish=False)]
    untouched = _trial(registry, other, 'elsewhere', finish=False)

    assert registry.close_interrupted_strategy_trials('./strategies/rsi.py', 'RSIStrategy', finished_at=T0) == 2

    assert [registry.get_trial(t).status for t in stranded] == ['FAILED', 'FAILED']
    assert registry.get_trial(untouched).status == 'RUNNING'
    assert sorted(t.trial_key for t in registry.strategy_trials('strategies/rsi.py', 'RSIStrategy')) == [
        'crashed', 'running']
    assert registry.close_interrupted_strategy_trials('strategies/rsi.py', 'RSIStrategy', finished_at=T0) == 0


def test_legacy_imported_backtests_count(tmp_path):
    registry = _registry(tmp_path)
    record = _make_record()
    record.id = 7
    registry.import_legacy_backtests([record], imported_at=T0)
    assert len(registry.strategy_trials('/home/trader/mmr/strategies/rsi.py', 'RSIStrategy')) == 1


def test_family_artifacts(tmp_path):
    registry = _registry(tmp_path)
    fid = registry.create_family(_family('strategies/rsi.py', 15), created_at=T0)
    tid = _trial(registry, fid, 'a')
    aid = registry.seal_artifact(fid, selected_trial_id=tid, selected_parameters={}, sealed_at=T0)
    assert [a.artifact_id for a in registry.family_artifacts(fid)] == [aid]
