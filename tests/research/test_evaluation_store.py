import datetime as dt
import os

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_store import (
    STAGE_PRE_HOLDOUT, EvaluationRecord, EvaluationRepository, describe_latest_evaluation,
    write_evaluation_summary,
)
from trader.research.schema import apply_research_migrations


def _db(path):
    db = DuckDBConnection.get_instance(str(path))
    apply_research_migrations(SchemaMigrator(db))
    return db


def _record(day, **overrides):
    fields = dict(
        spec_name='orb_us', family_id='f', strategy_path='strategies/orb.py',
        class_name='OpeningRangeBreakout', stage=STAGE_PRE_HOLDOUT, state='CANDIDATE',
        artifact_id=None, decision_digest=None, failed_rules=('min_round_trips',),
        missing_rules=('liquidity_capacity_envelope',), report_path='/tmp/r.md',
        created_at=dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc))
    fields.update(overrides)
    return EvaluationRecord(**fields)


def test_latest_for_strategy_matches_normalised_paths(tmp_path):
    repo = EvaluationRepository(_db(tmp_path / 'research.duckdb'))
    repo.record(_record(1))
    repo.record(_record(3, spec_name='orb_us_v2'))
    latest = repo.latest_for_strategy('/somewhere/strategies/orb.py', 'OpeningRangeBreakout')
    assert latest.spec_name == 'orb_us_v2'
    assert latest.failed_rules == ('min_round_trips',)


def test_summary_file_describes_the_latest_run(tmp_path):
    summaries = tmp_path / 'evaluations'
    assert 'no evaluation' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'X')
    write_evaluation_summary(summaries, _record(2))
    text = describe_latest_evaluation(summaries, '/elsewhere/strategies/orb.py', 'OpeningRangeBreakout')
    assert 'pre_holdout' in text and 'liquidity_capacity_envelope' in text


def test_unreadable_summary_does_not_raise(tmp_path):
    summaries = tmp_path / 'evaluations'
    path = write_evaluation_summary(summaries, _record(2))
    path.write_text('{not json')
    assert 'unavailable' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'OpeningRangeBreakout')


def test_summary_that_is_not_an_object_does_not_raise(tmp_path):
    summaries = tmp_path / 'evaluations'
    path = write_evaluation_summary(summaries, _record(2))
    path.write_text('[1, 2]')
    assert 'unavailable' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'OpeningRangeBreakout')


def test_next_run_overwrites_the_summary_of_the_same_strategy(tmp_path):
    summaries = tmp_path / 'evaluations'
    first = write_evaluation_summary(summaries, _record(1))
    second = write_evaluation_summary(summaries, _record(2, spec_name='orb_us_v2'))
    assert first == second
    assert list(summaries.iterdir()) == [second]
    assert 'orb_us_v2' in describe_latest_evaluation(summaries, 'strategies/orb.py', 'OpeningRangeBreakout')


def test_list_returns_newest_first_and_round_trips_fields(tmp_path):
    repo = EvaluationRepository(_db(tmp_path / 'research.duckdb'))
    older, newer = _record(1), _record(3, spec_name='orb_us_v2')
    repo.record(older)
    repo.record(newer)
    assert [r.spec_name for r in repo.list()] == ['orb_us_v2', 'orb_us']
    assert repo.list(limit=1)[0] == newer


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_summaries_directory_does_not_raise(tmp_path):
    summaries = tmp_path / 'evaluations'
    write_evaluation_summary(summaries, _record(2))
    summaries.chmod(0o000)
    try:
        text = describe_latest_evaluation(summaries, 'strategies/orb.py', 'OpeningRangeBreakout')
    finally:
        summaries.chmod(0o700)
    assert 'unavailable' in text and 'PermissionError' in text
