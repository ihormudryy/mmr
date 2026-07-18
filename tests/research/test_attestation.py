"""P2 Task 7 -- qualitative review + signed eligibility attestations.

These tests pin the trust chain from a quantitative ``EligibilityDecision`` to a
production-verifiable, Ed25519-signed ``EligibilityAttestation``:

* the §8.5 review is mandatory and complete (every field required; the holdout
  confirmation must be exactly True);
* sign -> verify is a clean round trip, and tampering with ANY authority field
  (or presenting a wrong key) fails the signature closed;
* expiry, revocation, unknown key id, and binding mismatch each raise a specific
  error -- verification never returns a soft "maybe valid";
* Ed25519 determinism means the same key + payload gives byte-identical bytes;
* rotating to a new keypair yields a new public-key id and breaks trust in the
  old one;
* the STRUCTURAL invariant: a failed (CANDIDATE) decision can never mint a
  PAPER_ELIGIBLE / paper attestation, no matter how complete the review;
* persistence round-trips, is idempotent, supports revocation, and exposes no
  update/delete -- and stores only public material.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research import signing
from trader.research.attestation import (
    AttestationError,
    AttestationRepository,
    AttestationVerifier,
    BadSignature,
    BindingMismatch,
    EligibilityAttestation,
    ExpectedBindings,
    Expired,
    PERMITTED_MODE_NONE,
    PERMITTED_MODE_PAPER,
    Revoked,
    UnknownKey,
    attestation_payload_bytes,
    build_attestation,
    payload_digest,
    unsigned_payload,
)
from trader.research.eligibility import (
    STATE_CANDIDATE,
    STATE_PAPER_ELIGIBLE,
    evaluate_eligibility,
)
from trader.research.review import OperatorReview
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import apply_research_migrations
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
EXPIRES = T0 + dt.timedelta(days=90)
NOW_OK = T0 + dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _passing_evidence(**over):
    from trader.research.eligibility import EligibilityEvidence
    kw = dict(
        n_round_trips=250, n_instruments=10, expectancy_bps_baseline=5.0,
        expectancy_bps_1_5x=3.0, expectancy_bps_2x=1.0,
        selection_adjusted_confidence=0.97, annualized_sharpe_ci_low=0.5,
        profit_factor=1.5, walk_forward_positive_fraction=0.7,
        max_month_profit_share=0.25, max_instrument_profit_share=0.30,
        scaled_holdout_drawdown=-0.02, neighborhood_robust=True,
        order_within_envelope=True, deterministic_replay_ok=True,
        holdout_opened_once=True, benchmark_drawdown_ratio=0.40,
        eligible_regime_positive_fraction=0.80, worst_eligible_regime_loss=-0.05,
        regime_transitions_stable=True)
    kw.update(over)
    return EligibilityEvidence(**kw)


def passing_decision(**over):
    return evaluate_eligibility(PAPER_V1, _passing_evidence(**over))


def candidate_decision():
    # one failing gate -> CANDIDATE
    return evaluate_eligibility(PAPER_V1, _passing_evidence(profit_factor=1.19))


def valid_review(**over):
    kw = dict(
        artifact_id="artifact-xyz",
        eligibility_decision_digest="decision-digest-abc",
        reviewer="ihor",
        reviewed_at=T0,
        economic_rationale="Intraday liquidity-provision edge in liquid US ETFs.",
        edge_survives_costs="Median spread << modeled 2x cost stress.",
        known_failure_regimes="Gap-open trend days; halts.",
        data_and_survivorship_limits="Vendor lacks pre-2019 delisted tickers.",
        parameter_sensitivity="Flat plateau across +/- 20% neighborhood.",
        operational_dependencies="XNYS calendar; live top-of-book feed.",
        capacity_and_decay="~$5M before impact; expect slow decay.",
        episode_dominance="No single day > 8% of P&L.",
        holdout_opened_once_confirmed=True)
    kw.update(over)
    return OperatorReview(**kw)


def build_unsigned(decision, review, signer, **over):
    kw = dict(
        decision=decision,
        review=review,
        public_key_id=signer.public_key_id,
        artifact_digest="artifact-digest-1",
        source_digest="source-digest-1",
        config_digest="config-digest-1",
        dataset_manifest_digest="dataset-digest-1",
        allowlist_digest="allowlist-digest-1",
        training_boundary="2019-01-01/2022-12-31",
        validation_boundary="2023-01-01/2023-12-31",
        holdout_boundary="2024-01-01/2024-12-31",
        evidence_boundary="2019-01-01/2024-12-31",
        cost_assumptions={"commission_bps": 0.5, "spread_bps": 1.0},
        capacity_assumptions={"adv_pct": 0.25, "capacity_usd": 5_000_000.0},
        max_gross_allocation=0.06,
        permitted_instruments=("AAPL", "MSFT", "SPY"),
        created_at=T0,
        expires_at=EXPIRES,
        operator_approved_at=T0)
    kw.update(over)
    return build_attestation(**kw)


def signed_paper_attestation(signer, **over):
    decision = passing_decision()
    review = valid_review()
    fields = build_unsigned(decision, review, signer, **over)
    return signer.sign(fields)


def expected_for(att) -> ExpectedBindings:
    return ExpectedBindings(
        artifact_digest=att.artifact_digest, allowlist_digest=att.allowlist_digest,
        ruleset_digest=att.ruleset_digest, account_mode=att.permitted_account_mode,
        max_gross_allocation=att.max_gross_allocation,
        permitted_instruments=att.permitted_instruments)


@pytest.fixture
def signer():
    return AttestationSigner.generate()


@pytest.fixture
def verifier(signer):
    return AttestationVerifier([signer.public_key])


@pytest.fixture
def repo(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    return AttestationRepository(db)


# --------------------------------------------------------------------------- #
# §8.5 mandatory review
# --------------------------------------------------------------------------- #
class TestMandatoryReview:
    _NARRATIVE = (
        "artifact_id", "eligibility_decision_digest", "reviewer",
        "economic_rationale", "edge_survives_costs", "known_failure_regimes",
        "data_and_survivorship_limits", "parameter_sensitivity",
        "operational_dependencies", "capacity_and_decay", "episode_dominance")

    @pytest.mark.parametrize("field", _NARRATIVE)
    def test_every_narrative_field_required_nonempty(self, field):
        with pytest.raises(ValueError, match=field):
            valid_review(**{field: ""})

    @pytest.mark.parametrize("field", _NARRATIVE)
    def test_every_narrative_field_rejects_whitespace(self, field):
        with pytest.raises(ValueError):
            valid_review(**{field: "   "})

    def test_holdout_confirmation_false_rejected(self):
        with pytest.raises(ValueError, match="holdout_opened_once_confirmed"):
            valid_review(holdout_opened_once_confirmed=False)

    def test_holdout_confirmation_none_rejected(self):
        with pytest.raises(ValueError):
            valid_review(holdout_opened_once_confirmed=None)

    def test_holdout_confirmation_truthy_but_not_true_rejected(self):
        # exactly-True is required: a truthy 1 is not a confirmation
        with pytest.raises(ValueError):
            valid_review(holdout_opened_once_confirmed=1)

    def test_reviewed_at_must_be_tz_aware(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            valid_review(reviewed_at=dt.datetime(2026, 7, 18, 12, 0))

    def test_valid_review_has_stable_digest(self):
        assert valid_review().digest == valid_review().digest
        assert valid_review().digest != valid_review(reviewer="someone").digest


# --------------------------------------------------------------------------- #
# sign -> verify happy path
# --------------------------------------------------------------------------- #
class TestSignVerify:
    def test_round_trip(self, signer, verifier):
        att = signed_paper_attestation(signer)
        verified = verifier.verify(att, expected_for(att), now=NOW_OK)
        assert verified.eligibility_state == STATE_PAPER_ELIGIBLE
        assert verified.permitted_account_mode == PERMITTED_MODE_PAPER
        assert verified.max_gross_allocation == 0.06
        assert verified.permitted_instruments == ("AAPL", "MSFT", "SPY")
        assert verified.public_key_id == signer.public_key_id
        assert verified.payload_digest == att.payload_digest

    def test_state_and_mode_bound_to_decision(self, signer):
        att = signed_paper_attestation(signer)
        assert att.eligibility_state == STATE_PAPER_ELIGIBLE
        assert att.permitted_account_mode == PERMITTED_MODE_PAPER
        # ruleset identity + digests are copied from the decision, not free-form
        d = passing_decision()
        assert att.ruleset_digest == d.ruleset_digest
        assert att.eligibility_decision_digest == d.digest
        assert att.review_digest == valid_review().digest

    def test_signature_is_base64url(self, signer):
        import base64
        att = signed_paper_attestation(signer)
        # decodes cleanly as urlsafe base64 (raises otherwise)
        raw = base64.urlsafe_b64decode(att.signature)
        assert len(raw) == 64  # Ed25519 signature length


# --------------------------------------------------------------------------- #
# tampering + wrong key -> BadSignature
# --------------------------------------------------------------------------- #
class TestTamperFailsClosed:
    # one representative different value per authority field
    TAMPER = {
        "artifact_digest": "tampered-artifact",
        "source_digest": "tampered-source",
        "config_digest": "tampered-config",
        "dataset_manifest_digest": "tampered-dataset",
        "allowlist_digest": "tampered-allowlist",
        "training_boundary": "1999-01-01/1999-12-31",
        "validation_boundary": "1999-01-01/1999-12-31",
        "holdout_boundary": "1999-01-01/1999-12-31",
        "evidence_boundary": "1999-01-01/1999-12-31",
        "cost_assumptions": {"commission_bps": 99.0},
        "capacity_assumptions": {"capacity_usd": 1.0},
        "ruleset_name": "evil-v9",
        "ruleset_version": "9",
        "ruleset_digest": "tampered-ruleset",
        "eligibility_state": STATE_CANDIDATE,
        "permitted_account_mode": "live",
        "max_gross_allocation": 0.99,
        "permitted_instruments": ("AAPL", "GME"),
        "created_at": T0 + dt.timedelta(days=5),
        "expires_at": EXPIRES + dt.timedelta(days=365),
        "operator_approved_at": T0 + dt.timedelta(days=5),
        "reason_codes": ("hacked",),
        "evidence_refs": ("hacked",),
        "eligibility_decision_digest": "tampered-decision",
        "review_digest": "tampered-review",
    }

    @pytest.mark.parametrize("field,value", list(TAMPER.items()))
    def test_tampering_any_authority_field_fails_signature(self, signer, verifier,
                                                           field, value):
        att = signed_paper_attestation(signer)
        tampered = dataclasses.replace(att, **{field: value})
        # tampering changes the signed bytes but keeps the old signature
        assert attestation_payload_bytes(tampered) != attestation_payload_bytes(att)
        with pytest.raises(BadSignature):
            verifier.verify(tampered, expected_for(att), now=NOW_OK)

    def test_wrong_key_signature_fails(self, verifier):
        # attestation NAMES a trusted key id but was actually signed by another
        trusted = None
        for kid in verifier.trusted_key_ids:
            trusted = kid
        attacker = AttestationSigner.generate()
        decision, review = passing_decision(), valid_review()
        # build fields declaring the trusted key id
        fields = build_attestation(
            decision=decision, review=review, public_key_id=trusted,
            artifact_digest="a", source_digest="s", config_digest="c",
            dataset_manifest_digest="d", allowlist_digest="al",
            training_boundary="t", validation_boundary="v", holdout_boundary="h",
            evidence_boundary="e", cost_assumptions={}, capacity_assumptions={},
            max_gross_allocation=0.06, permitted_instruments=("AAPL",),
            created_at=T0, expires_at=EXPIRES, operator_approved_at=T0)
        # sign with the ATTACKER key over those bytes (bypassing signer.sign guard)
        sig = signing.sign_bytes(
            attacker._AttestationSigner__private_key,  # noqa: SLF001 (test only)
            attestation_payload_bytes(fields))
        forged = EligibilityAttestation(**fields, signature=sig)
        with pytest.raises(BadSignature):
            verifier.verify(forged, ExpectedBindings(
                artifact_digest="a", allowlist_digest="al", ruleset_digest=decision.ruleset_digest,
                account_mode=PERMITTED_MODE_PAPER, max_gross_allocation=0.06,
                permitted_instruments=("AAPL",)), now=NOW_OK)

    def test_signer_sign_refuses_mismatched_key_id(self, signer):
        # the signer must refuse to sign fields that name a different key id
        other = AttestationSigner.generate()
        fields = build_unsigned(passing_decision(), valid_review(), other)
        with pytest.raises(ValueError, match="does not match signing key"):
            signer.sign(fields)


# --------------------------------------------------------------------------- #
# expiry / revocation / unknown key / binding mismatch
# --------------------------------------------------------------------------- #
class TestVerificationGates:
    def test_expired_raises(self, signer, verifier):
        att = signed_paper_attestation(signer)
        with pytest.raises(Expired):
            verifier.verify(att, expected_for(att), now=EXPIRES + dt.timedelta(seconds=1))

    def test_exactly_at_expiry_raises(self, signer, verifier):
        att = signed_paper_attestation(signer)
        with pytest.raises(Expired):
            verifier.verify(att, expected_for(att), now=EXPIRES)  # now >= expires

    def test_revoked_raises(self, signer, verifier):
        att = signed_paper_attestation(signer)
        with pytest.raises(Revoked):
            verifier.verify(att, expected_for(att), now=NOW_OK,
                            revoked_digests=frozenset({att.payload_digest}))

    def test_unknown_key_id_raises(self, signer):
        att = signed_paper_attestation(signer)
        other = AttestationSigner.generate()
        verifier = AttestationVerifier([other.public_key])
        with pytest.raises(UnknownKey):
            verifier.verify(att, expected_for(att), now=NOW_OK)

    @pytest.mark.parametrize("field,value", [
        ("artifact_digest", "other-artifact"),
        ("allowlist_digest", "other-allowlist"),
        ("ruleset_digest", "other-ruleset"),
        ("account_mode", "live"),
        ("max_gross_allocation", 0.09),
        ("permitted_instruments", ("AAPL", "TSLA")),
    ])
    def test_binding_mismatch_raises(self, signer, verifier, field, value):
        att = signed_paper_attestation(signer)
        expected = dataclasses.replace(expected_for(att), **{field: value})
        with pytest.raises(BindingMismatch):
            verifier.verify(att, expected, now=NOW_OK)

    def test_all_gate_errors_subclass_attestation_error(self):
        for exc in (UnknownKey, BadSignature, Expired, Revoked, BindingMismatch):
            assert issubclass(exc, AttestationError)


# --------------------------------------------------------------------------- #
# determinism + rotation
# --------------------------------------------------------------------------- #
class TestDeterminismAndRotation:
    def test_deterministic_signature(self, signer):
        decision, review = passing_decision(), valid_review()
        fields = build_unsigned(decision, review, signer)
        a = signer.sign(fields)
        b = signer.sign(fields)
        # Ed25519 is deterministic: identical key + payload -> identical signature
        assert a.signature == b.signature
        assert a.payload_digest == b.payload_digest

    def test_rotation_new_keypair_new_id(self):
        a, b = AttestationSigner.generate(), AttestationSigner.generate()
        assert a.public_key_id != b.public_key_id
        assert a.public_key_id.startswith("ed25519-")

    def test_attestation_by_key_a_not_verified_under_key_b_only(self):
        a, b = AttestationSigner.generate(), AttestationSigner.generate()
        att = signed_paper_attestation(a)
        verifier_b = AttestationVerifier([b.public_key])
        with pytest.raises(AttestationError):
            verifier_b.verify(att, expected_for(att), now=NOW_OK)


# --------------------------------------------------------------------------- #
# STRUCTURAL invariant: qualitative can never override quantitative
# --------------------------------------------------------------------------- #
class TestQualitativeCannotOverride:
    def test_candidate_decision_yields_non_paper_attestation(self, signer):
        decision = candidate_decision()
        assert decision.state == STATE_CANDIDATE
        # even with a fully populated, valid review...
        review = valid_review()
        fields = build_unsigned(decision, review, signer)
        att = signer.sign(fields)
        # ...the attestation is NOT paper-eligible and authorizes no paper mode
        assert att.eligibility_state == STATE_CANDIDATE
        assert att.permitted_account_mode == PERMITTED_MODE_NONE
        assert att.permitted_account_mode != PERMITTED_MODE_PAPER

    def test_no_build_argument_can_force_paper_from_candidate(self, signer):
        # there is no state/mode override parameter on build_attestation
        import inspect
        params = set(inspect.signature(build_attestation).parameters)
        for banned in ("eligibility_state", "permitted_account_mode", "state",
                       "mode", "override", "force"):
            assert banned not in params

    def test_verifier_rejects_paper_expectation_on_candidate(self, signer, verifier):
        # a deployment expecting a paper attestation cannot be satisfied by a
        # candidate one: the mode binding mismatches (fails closed).
        decision = candidate_decision()
        fields = build_unsigned(decision, valid_review(), signer)
        att = signer.sign(fields)
        paper_expectation = dataclasses.replace(
            expected_for(att), account_mode=PERMITTED_MODE_PAPER)
        with pytest.raises(BindingMismatch):
            verifier.verify(att, paper_expectation, now=NOW_OK)


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
class TestPersistence:
    def test_record_then_get_round_trips(self, repo, signer):
        att = signed_paper_attestation(signer)
        digest = repo.record(att)
        assert digest == att.payload_digest
        got = repo.get(digest)
        assert got is not None
        assert got == att  # frozen dataclass equality across every field
        assert got.payload_digest == att.payload_digest

    def test_record_is_idempotent(self, repo, signer):
        att = signed_paper_attestation(signer)
        d1 = repo.record(att)
        d2 = repo.record(att)
        assert d1 == d2 == att.payload_digest

    def test_persisted_attestation_still_verifies(self, repo, signer, verifier):
        att = signed_paper_attestation(signer)
        repo.record(att)
        got = repo.get(att.payload_digest)
        verified = verifier.verify(got, expected_for(got), now=NOW_OK)
        assert verified.eligibility_state == STATE_PAPER_ELIGIBLE

    def test_get_unknown_returns_none(self, repo):
        assert repo.get("does-not-exist") is None

    def test_revocation_flow(self, repo, signer, verifier):
        att = signed_paper_attestation(signer)
        repo.record(att)
        assert repo.revoked_digests() == frozenset()
        repo.revoke(att.payload_digest, reason="drift", revoked_at=T0)
        revoked = repo.revoked_digests()
        assert att.payload_digest in revoked
        # a verifier that consults the revocation set now fails closed
        with pytest.raises(Revoked):
            verifier.verify(att, expected_for(att), now=NOW_OK, revoked_digests=revoked)

    def test_revoke_is_idempotent(self, repo, signer):
        att = signed_paper_attestation(signer)
        repo.record(att)
        repo.revoke(att.payload_digest, reason="x", revoked_at=T0)
        repo.revoke(att.payload_digest, reason="x", revoked_at=T0)
        assert repo.revoked_digests() == frozenset({att.payload_digest})

    def test_revoke_unknown_raises(self, repo):
        with pytest.raises(AttestationError):
            repo.revoke("nope", reason="x", revoked_at=T0)

    def test_repository_exposes_no_update_or_delete(self):
        for banned in ("update", "delete", "remove", "unseal", "edit", "set_state"):
            assert not hasattr(AttestationRepository, banned), banned


# --------------------------------------------------------------------------- #
# no private material anywhere in the serialized / persisted attestation
# --------------------------------------------------------------------------- #
class TestNoPrivateMaterial:
    def test_attestation_carries_only_public_key_id(self, signer):
        att = signed_paper_attestation(signer)
        payload = unsigned_payload(att)
        assert payload["public_key_id"] == signer.public_key_id
        assert payload["public_key_id"].startswith("ed25519-")
        assert "signature" not in payload  # signature excluded from signed bytes

    def test_db_row_contains_no_private_key_bytes(self, repo, signer, tmp_path):
        from cryptography.hazmat.primitives import serialization
        raw_private = signer._AttestationSigner__private_key.private_bytes(  # noqa: SLF001
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())
        att = signed_paper_attestation(signer)
        repo.record(att)
        # dump every stored value and assert the private bytes never appear
        db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
        rows = db.execute("SELECT * FROM eligibility_attestations", fetch="all")
        blob = repr(rows).encode() + raw_private.hex().encode()
        assert raw_private not in repr(rows).encode()
        assert raw_private.hex() not in repr(rows)
        # only the public key id + base64url signature are authority-side secrets
        assert signer.public_key_id in repr(rows)

    def test_payload_digest_helpers_agree(self, signer):
        att = signed_paper_attestation(signer)
        assert payload_digest(att) == att.payload_digest
        assert payload_digest(unsigned_payload(att)) == att.payload_digest


def test_apply_attestation_migrations_idempotent(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "idem.duckdb"))
    migrator = SchemaMigrator(db)
    apply_research_migrations(migrator)
    apply_research_migrations(migrator)  # second call is a no-op
    n = db.execute("SELECT COUNT(*) FROM eligibility_attestations", fetch="one")
    assert n[0] == 0
    r = db.execute("SELECT COUNT(*) FROM operator_reviews", fetch="one")
    assert r[0] == 0
