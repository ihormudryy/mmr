"""A strategy that passes walk-forward and then loses in its holdout is retired:
no evaluation record, summary or attestation may call it paper eligible."""
import json

import pytest

from tests.research.evaluation_fixtures import FIXED_NOW, evaluate_synthetic, holdout_ruleset
from trader.research.artifact import ARTIFACT_STATE_RETIRED
from trader.research.attest_export import AttestExportError, attest_and_export
from trader.research.evaluation_store import (
    STAGE_HOLDOUT_FAILED, EvaluationRepository, describe_latest_evaluation,
)
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.signing import AttestationSigner


@pytest.fixture(scope='module')
def retired(tmp_path_factory):
    repo = tmp_path_factory.mktemp('retired')
    return evaluate_synthetic(repo, str(repo / 'market.duckdb'), holdout_drift=-0.0003)


def _decision_rows(research_db, artifact_id):
    return research_db.transaction(lambda conn: conn.execute(
        'SELECT decision_digest FROM eligibility_decisions WHERE artifact_id = ?',
        [artifact_id]).fetchall())


@pytest.mark.timeout(240)
def test_a_failed_holdout_is_reported_as_retired_with_no_decision(retired):
    result = retired.result
    artifact = ExperimentRegistry(retired.research_db).get_artifact(result.artifact_id)

    assert artifact.state == ARTIFACT_STATE_RETIRED and artifact.holdout_passed is False
    assert (result.stage, result.state, result.decision_digest) == (
        STAGE_HOLDOUT_FAILED, ARTIFACT_STATE_RETIRED, None)
    assert _decision_rows(retired.research_db, result.artifact_id) == []
    record = EvaluationRepository(retired.research_db).list()[0]
    assert (record.stage, record.state, record.decision_digest) == (
        STAGE_HOLDOUT_FAILED, ARTIFACT_STATE_RETIRED, None)
    summary_file = next(retired.paths.summaries_dir.glob('*.json'))
    assert json.loads(summary_file.read_text())['state'] == ARTIFACT_STATE_RETIRED
    line = describe_latest_evaluation(retired.paths.summaries_dir,
                                      'strategies/time_of_day.py', 'TimeOfDay')
    assert 'RETIRED' in line and 'PAPER_ELIGIBLE' not in line


@pytest.mark.timeout(240)
def test_a_failed_holdout_cannot_be_attested(retired, tmp_path):
    with pytest.raises(AttestExportError, match='holdout'):
        attest_and_export(retired.research_db, artifact_id=retired.result.artifact_id,
                          signer=AttestationSigner.generate(), artifacts_root=tmp_path / 'artifacts',
                          now=FIXED_NOW, ruleset=holdout_ruleset())
    assert not (tmp_path / 'artifacts').exists() or not any((tmp_path / 'artifacts').glob('sha256_*'))
