"""Sign the attestation for an eligible artifact and export its bundle.

The bundle lands in ``<artifacts_root>/sha256_<manifest digest>``: the path
order dispatch resolves from the digest the intent emitter sends.
"""
from __future__ import annotations

import datetime as dt
import shutil
import uuid
from pathlib import Path
from typing import Any

from trader.research.artifact import ARTIFACT_STATE_RETIRED
from trader.research.attestation import AttestationRepository, build_attestation
from trader.research.bundle import ResearchBundle
from trader.research.canonical import sha256_digest
from trader.research.eligibility import STATE_PAPER_ELIGIBLE, EligibilityDecisionRepository, Ruleset
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import OperatorReviewRepository, review_allowed_for
from trader.research.rulesets.paper_v1 import PAPER_V1

ATTESTATION_LIFETIME = dt.timedelta(days=90)


class AttestExportError(Exception):
    """The artifact cannot be attested; the message says what is missing."""


def bundle_dir_name(manifest_digest: str) -> str:
    return 'sha256_' + manifest_digest.removeprefix('sha256:')


def _rows(db: Any, sql: str, params: list) -> list:
    return db.transaction(lambda conn: conn.execute(sql, params).fetchall())


def _single(db: Any, sql: str, params: list, error: str) -> str:
    rows = _rows(db, sql, params)
    if len(rows) != 1:
        raise AttestExportError(error)
    return rows[0][0]


def attest_and_export(research_db: Any, *, artifact_id: str, signer: Any,
                      artifacts_root: Path, now: dt.datetime,
                      ruleset: Ruleset = PAPER_V1) -> Path:
    registry = ExperimentRegistry(research_db)
    artifact = registry.get_artifact(artifact_id)
    if artifact is None:
        raise AttestExportError(f'unknown artifact {artifact_id}')
    short_id = artifact_id[:12]
    if artifact.state == ARTIFACT_STATE_RETIRED or artifact.holdout_passed is not True:
        raise AttestExportError(
            f'artifact {short_id} has no passed holdout (state {artifact.state}, holdout passed '
            f'{artifact.holdout_passed}); only an artifact whose holdout passed can be attested')
    family = registry.get_family(artifact.family_id)
    if family is None:
        raise AttestExportError(f'artifact {short_id} names an unknown family')
    decision_digest = _single(
        research_db, 'SELECT decision_digest FROM eligibility_decisions WHERE artifact_id = ?',
        [artifact_id], f'artifact {short_id} must have exactly one eligibility decision')
    decision = EligibilityDecisionRepository(research_db).get(decision_digest)
    if decision is None:
        raise AttestExportError(f'eligibility decision {decision_digest[:12]} cannot be read back')
    if decision.state != STATE_PAPER_ELIGIBLE:
        raise AttestExportError(
            f'artifact {short_id} is {decision.state}, not {STATE_PAPER_ELIGIBLE}; nothing to attest')
    if decision.ruleset_digest != ruleset.digest:
        raise AttestExportError(
            f'decision was made under ruleset {decision.ruleset_name} {decision.ruleset_digest[:12]}, '
            f'attestation requires {ruleset.name} {ruleset.digest[:12]}')
    review_digest = _single(
        research_db,
        'SELECT review_digest FROM operator_reviews WHERE artifact_id = ? '
        'AND eligibility_decision_digest = ?', [artifact_id, decision_digest],
        'submit exactly one operator review for this decision first '
        '(`mmr research review submit ... --reviewer-kind human|llm`)')
    review = OperatorReviewRepository(research_db).get(review_digest)
    if review is None:
        raise AttestExportError(f'operator review {review_digest[:12]} cannot be read back')
    try:
        review_allowed_for(review, 'paper')
    except ValueError as exc:
        raise AttestExportError(str(exc)) from exc

    existing = _rows(research_db,
                     'SELECT payload_digest FROM eligibility_attestations '
                     'WHERE eligibility_decision_digest = ? AND review_digest = ?',
                     [decision_digest, review_digest])
    if existing:
        _refuse_stale_attestation(research_db, existing[0][0], short_id, signer, now)
    else:
        unsigned = _attestation(registry, family, artifact_id, decision, review, signer, now)
        AttestationRepository(research_db).record(signer.sign(unsigned))
    return _export(research_db, artifact_id, artifacts_root)


_RENEWAL = ('a decision is attested once, so renewal means evaluating again over a newer '
            'period (`mmr research evaluate`), then reviewing and attesting the new artifact')


def _refuse_stale_attestation(research_db: Any, payload_digest: str, short_id: str,
                              signer: Any, now: dt.datetime) -> None:
    stored = AttestationRepository(research_db).get(payload_digest)
    if stored is None:
        raise AttestExportError(f'attestation {payload_digest[:12]} cannot be read back')
    if stored.expires_at <= now:
        raise AttestExportError(
            f'the attestation for artifact {short_id} expired at {stored.expires_at.isoformat()}; '
            f'{_RENEWAL}')
    if stored.public_key_id != signer.public_key_id:
        raise AttestExportError(
            f'the attestation for artifact {short_id} was signed by key {stored.public_key_id}, '
            f'not the current signing key {signer.public_key_id}; {_RENEWAL}')


def _attestation(registry: ExperimentRegistry, family, artifact_id: str, decision, review,
                 signer, now: dt.datetime):
    folds = registry.get_validation_folds(family.family_id)
    trials = registry.list_trials(family.family_id, include_archived=True)
    trial_payload = [
        {'trial_id': t.trial_id, 'family_id': t.family_id, 'trial_key': t.trial_key,
         'parameters': dict(t.parameters), 'status': t.status, 'started_at': t.started_at,
         'finished_at': t.finished_at, 'metrics': dict(t.metrics),
         'traceback_digest': t.traceback_digest, 'safe_summary': t.safe_summary,
         'archived': t.archived}
        for t in trials]
    walk_forward = [f for f in folds if f.get('kind') == 'walk_forward']
    holdout = next((f for f in folds if f.get('kind') == 'holdout'), None)
    if not walk_forward or holdout is None:
        raise AttestExportError(
            f'family {family.family_id[:12]} has no recorded walk-forward folds and holdout')
    protocol = family.validation_protocol
    cost_model = family.cost_model
    instruments = tuple(str(c) for c in sorted(protocol['conids']))
    return build_attestation(
        decision=decision, review=review, public_key_id=signer.public_key_id,
        artifact_digest=artifact_id, source_digest=family.source_tree_digest,
        config_digest=family.dependency_lock_digest,
        dataset_manifest_digest=family.dataset_manifest_digest,
        allowlist_digest=sha256_digest('attestation_allowlist', list(instruments)),
        training_boundary=f"{walk_forward[0]['train_start']}/{walk_forward[-1]['train_end']}",
        validation_boundary=f"{walk_forward[0]['test_start']}/{walk_forward[-1]['test_end']}",
        holdout_boundary=f"{holdout['start']}/{holdout['end']}",
        evidence_boundary=f"{protocol['period_start']}/{protocol['period_end']}",
        cost_assumptions=dict(cost_model),
        capacity_assumptions={'order_notional': cost_model['order_notional'],
                              'account_equity': cost_model['account_equity']},
        max_gross_allocation=float(cost_model['max_gross_allocation']),
        permitted_instruments=instruments,
        created_at=now, expires_at=now + ATTESTATION_LIFETIME,
        operator_approved_at=review.reviewed_at,
        evidence_refs=tuple(decision.evidence_refs) + (
            f"bundle_trials:{sha256_digest('research_bundle_trials', trial_payload)}",
            f"bundle_folds:{sha256_digest('research_bundle_folds', folds)}"),
    )


def _remove_read_only_tree(path: Path) -> None:
    for child in path.rglob('*'):
        child.chmod(0o644)
    path.chmod(0o755)
    shutil.rmtree(path)


def _export(research_db: Any, artifact_id: str, artifacts_root: Path) -> Path:
    artifacts_root.mkdir(parents=True, exist_ok=True)
    staging = artifacts_root / f'.export-{uuid.uuid4().hex}'
    digest = ResearchBundle(research_db).export(artifact_id, staging).manifest_digest
    final = artifacts_root / bundle_dir_name(digest)
    if final.exists():
        _remove_read_only_tree(staging)
        return final
    staging.rename(final)
    return final
