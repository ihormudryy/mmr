import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.review import OperatorReview, OperatorReviewRepository, review_allowed_for
from trader.research.schema import apply_research_migrations

T0 = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)


def _review(**overrides):
    fields = dict(
        artifact_id='a1', eligibility_decision_digest='d1', reviewer='claude', reviewed_at=T0,
        economic_rationale='x', edge_survives_costs='x', known_failure_regimes='x',
        data_and_survivorship_limits='x', parameter_sensitivity='x',
        operational_dependencies='x', capacity_and_decay='x', episode_dominance='x',
        holdout_opened_once_confirmed=True)
    fields.update(overrides)
    return OperatorReview(**fields)


def test_unknown_kind_keeps_the_old_digest():
    assert 'reviewer_kind' not in _review()._body()


def test_kind_is_part_of_the_digest():
    assert _review(reviewer_kind='llm').digest != _review(reviewer_kind='human').digest


def test_invalid_kind_is_refused():
    with pytest.raises(ValueError, match='reviewer_kind'):
        _review(reviewer_kind='robot')


def test_kind_round_trips_through_the_repository(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / 'research.duckdb'))
    apply_research_migrations(SchemaMigrator(db))
    repo = OperatorReviewRepository(db)
    digest = repo.record(_review(reviewer_kind='llm'))
    assert repo.get(digest).reviewer_kind == 'llm'


def test_live_needs_a_human_review():
    review_allowed_for(_review(reviewer_kind='llm'), 'paper')
    review_allowed_for(_review(reviewer_kind='human'), 'live')
    with pytest.raises(ValueError, match='human'):
        review_allowed_for(_review(reviewer_kind='llm'), 'live')


def test_review_recorded_before_the_column_existed_reads_back_as_unknown_with_its_old_digest(tmp_path):
    from trader.research.canonical import sha256_digest
    from trader.research.review import (
        REVIEW_DIGEST_PREFIX, apply_review_migrations, apply_reviewer_kind_migration)

    db = DuckDBConnection.get_instance(str(tmp_path / 'legacy.duckdb'))
    migrator = SchemaMigrator(db)
    apply_review_migrations(migrator)
    legacy = _review()
    legacy_digest = sha256_digest(REVIEW_DIGEST_PREFIX, {
        'artifact_id': 'a1', 'eligibility_decision_digest': 'd1', 'reviewer': 'claude',
        'reviewed_at': T0, 'economic_rationale': 'x', 'edge_survives_costs': 'x',
        'known_failure_regimes': 'x', 'data_and_survivorship_limits': 'x',
        'parameter_sensitivity': 'x', 'operational_dependencies': 'x',
        'capacity_and_decay': 'x', 'episode_dominance': 'x',
        'holdout_opened_once_confirmed': True})
    db.execute(
        "INSERT INTO operator_reviews VALUES (?, 'a1', 'd1', 'claude', ?, 'x', 'x', 'x', 'x', "
        "'x', 'x', 'x', 'x', TRUE)", [legacy_digest, T0], fetch='none')

    apply_reviewer_kind_migration(migrator)
    apply_reviewer_kind_migration(migrator)

    assert legacy.digest == legacy_digest
    assert OperatorReviewRepository(db).get(legacy_digest).reviewer_kind == 'unknown'


def _rewrite_review_and_manifest(root, mutate):
    import hashlib
    import json

    from trader.research.canonical import canonical_json_bytes

    review_path = root / 'review.json'
    review_path.chmod(0o644)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    mutate(review)
    review_bytes = canonical_json_bytes(review)
    review_path.write_bytes(review_bytes)
    manifest_path = root / 'manifest.json'
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files']['review.json'] = hashlib.sha256(review_bytes).hexdigest()
    body = dict(manifest)
    body.pop('manifest_digest')
    manifest['manifest_digest'] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    review_path.chmod(0o444)
    manifest_path.chmod(0o444)


def test_bundle_exported_before_reviewer_kind_still_verifies(tmp_path):
    from tests.research.test_research_bundle import build_populated_db
    from trader.research.bundle import ResearchBundle

    db, artifact_id, signer = build_populated_db(tmp_path, return_signer=True)
    root = tmp_path / 'bundle'
    bundle = ResearchBundle(db)
    bundle.export(artifact_id, root)
    _rewrite_review_and_manifest(root, lambda review: review.pop('reviewer_kind'))

    bundle.verify(root, trusted_public_keys={signer.public_key_id: signer.public_key})


def test_bundle_review_kind_cannot_be_upgraded_to_human(tmp_path):
    from tests.research.test_research_bundle import build_populated_db
    from trader.research.bundle import BundleError, ResearchBundle

    db, artifact_id, signer = build_populated_db(tmp_path, return_signer=True)
    root = tmp_path / 'bundle'
    bundle = ResearchBundle(db)
    bundle.export(artifact_id, root)
    _rewrite_review_and_manifest(root, lambda review: review.__setitem__('reviewer_kind', 'human'))

    with pytest.raises(BundleError, match='review digest binding'):
        bundle.verify(root, trusted_public_keys={signer.public_key_id: signer.public_key})
