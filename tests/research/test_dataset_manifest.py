"""P2 Task 2 — canonical digests + immutable dataset manifests.

The canonical form here is the ROOT of the research evidence chain: these exact
digest bytes are what every later artifact and Ed25519 attestation sign over, so
they must be deterministic across processes/platforms and reject anything
non-canonical (naive datetimes, NaN/Infinity). Golden byte assertions pin the
form; changing it is a breaking change to every downstream digest.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.dataset_manifest import (
    DatasetCorrection,
    DatasetFile,
    DatasetManifest,
    QualityFinding,
    dataset_manifest_digest,
)
from trader.research.schema import (
    DatasetManifestRepository,
    DigestConflict,
    apply_research_migrations,
)

UTC = dt.timezone.utc
SEALED_AT = dt.datetime(2026, 7, 18, 16, 0, tzinfo=UTC)


def _finding(name="session_completeness", passed=True, required=True, detail=""):
    return QualityFinding(name=name, passed=passed, required=required, detail=detail)


def _manifest(**over):
    kw = dict(
        vendor="polygon",
        retrieval_timestamp=dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        bar_interval="1 day",
        timestamp_convention="bar_end_utc",
        session_calendar="XNYS",
        calendar_version="4.5.0",
        adjustment_policy="split_and_dividend",
        start_boundary=dt.datetime(2020, 1, 1, tzinfo=UTC),
        end_boundary=dt.datetime(2025, 12, 31, tzinfo=UTC),
        spread_source="polygon_nbbo",
        instruments=(265598, 272093),
        files=(DatasetFile(path="aapl.parquet", sha256="abc123", rows=1000),),
        findings=(_finding(),),
        corrections=(),
    )
    kw.update(over)
    return DatasetManifest(**kw)


class TestCanonicalJsonBytes:
    def test_mapping_keys_are_sorted_and_separators_compact(self):
        assert canonical_json_bytes({"b": 1, "a": 2, "c": 3}) == b'{"a":2,"b":1,"c":3}'

    def test_nested_mapping_keys_sorted_recursively(self):
        assert canonical_json_bytes({"z": {"b": 1, "a": 2}}) == b'{"z":{"a":2,"b":1}}'

    def test_array_order_is_preserved_for_list_and_tuple(self):
        assert canonical_json_bytes([3, 1, 2]) == b'[3,1,2]'
        assert canonical_json_bytes((3, 1, 2)) == b'[3,1,2]'

    def test_aware_datetime_serialized_as_utc_isoformat(self):
        ts = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
        assert canonical_json_bytes({"t": ts}) == b'{"t":"2026-07-18T14:30:00+00:00"}'

    def test_non_utc_datetime_is_converted_to_utc(self):
        est = dt.timezone(dt.timedelta(hours=-4))
        ts = dt.datetime(2026, 7, 18, 10, 30, tzinfo=est)  # == 14:30 UTC
        assert canonical_json_bytes(ts) == b'"2026-07-18T14:30:00+00:00"'

    def test_naive_datetime_is_rejected(self):
        with pytest.raises(ValueError, match="naive"):
            canonical_json_bytes({"t": dt.datetime(2026, 7, 18, 14, 30)})

    def test_decimal_serialized_as_exact_string(self):
        assert canonical_json_bytes({"p": Decimal("1.50")}) == b'{"p":"1.50"}'
        assert canonical_json_bytes(Decimal("0")) == b'"0"'

    def test_non_finite_floats_are_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="finite"):
                canonical_json_bytes({"x": bad})

    def test_unicode_is_utf8_not_ascii_escaped(self):
        assert canonical_json_bytes({"s": "café"}) == '{"s":"café"}'.encode("utf-8")

    def test_unsupported_type_is_rejected(self):
        with pytest.raises(TypeError):
            canonical_json_bytes({"x": object()})

    def test_bool_is_not_treated_as_int(self):
        assert canonical_json_bytes({"a": True, "b": False}) == b'{"a":true,"b":false}'

    def test_is_deterministic_across_equal_inputs(self):
        a = canonical_json_bytes({"a": 1, "b": [Decimal("2.0"), "x"]})
        b = canonical_json_bytes({"b": [Decimal("2.0"), "x"], "a": 1})
        assert a == b


class TestSha256Digest:
    def test_digest_is_prefixed_sha256_of_canonical_bytes(self):
        value = {"a": 1}
        expected = hashlib.sha256(
            b"dataset_manifest\n" + canonical_json_bytes(value)).hexdigest()
        assert sha256_digest("dataset_manifest", value) == expected

    def test_prefix_changes_the_digest(self):
        value = {"a": 1}
        assert sha256_digest("dataset_manifest", value) != sha256_digest("trial", value)

    def test_digest_is_stable_for_equal_values(self):
        assert sha256_digest("x", {"a": 1, "b": 2}) == sha256_digest("x", {"b": 2, "a": 1})


class TestResearchEligibility:
    def test_eligible_when_all_required_findings_pass(self):
        assert _manifest(findings=(_finding(passed=True, required=True),)).research_eligible is True

    def test_required_failing_finding_makes_ineligible(self):
        assert _manifest(findings=(_finding(passed=False, required=True),)).research_eligible is False

    def test_optional_failing_finding_stays_eligible(self):
        m = _manifest(findings=(_finding(passed=False, required=False),
                                _finding(name="dupes", passed=True, required=True)))
        assert m.research_eligible is True

    def test_one_required_failure_among_passes_is_ineligible(self):
        m = _manifest(findings=(
            _finding(name="completeness", passed=True, required=True),
            _finding(name="outliers", passed=False, required=True),
            _finding(name="dupes", passed=True, required=True)))
        assert m.research_eligible is False

    def test_eligibility_has_no_override_flag(self):
        # There is deliberately no constructor/keyword that can force eligibility;
        # it is derived purely from the findings.
        import dataclasses
        fields = {f.name for f in dataclasses.fields(DatasetManifest)}
        assert "research_eligible" not in fields  # a property, not a settable field


class TestManifestDigest:
    def test_digest_is_deterministic_and_prefixed(self):
        m = _manifest()
        assert dataset_manifest_digest(m) == m.digest
        assert dataset_manifest_digest(m) == dataset_manifest_digest(_manifest())

    def test_digest_is_independent_of_finding_and_file_order(self):
        base_findings = (_finding(name="a"), _finding(name="b"), _finding(name="c"))
        m1 = _manifest(findings=base_findings)
        m2 = _manifest(findings=tuple(reversed(base_findings)))
        assert dataset_manifest_digest(m1) == dataset_manifest_digest(m2)

    def test_digest_is_independent_of_instrument_order(self):
        assert dataset_manifest_digest(_manifest(instruments=(265598, 272093))) \
            == dataset_manifest_digest(_manifest(instruments=(272093, 265598)))

    def test_digest_changes_when_any_provenance_field_changes(self):
        base = dataset_manifest_digest(_manifest())
        for field, value in [
            ("vendor", "twelvedata"),
            ("retrieval_timestamp", dt.datetime(2026, 7, 2, tzinfo=UTC)),
            ("calendar_version", "4.6.0"),
            ("timestamp_convention", "bar_start_utc"),
            ("adjustment_policy", "unadjusted"),
            ("start_boundary", dt.datetime(2019, 1, 1, tzinfo=UTC)),
            ("spread_source", "estimated_10bps"),
        ]:
            assert dataset_manifest_digest(_manifest(**{field: value})) != base, field

    def test_digest_captures_checksums_and_correction_lineage(self):
        base = dataset_manifest_digest(_manifest())
        diff_file = _manifest(files=(DatasetFile(path="aapl.parquet", sha256="DIFFERENT", rows=1000),))
        assert dataset_manifest_digest(diff_file) != base
        with_correction = _manifest(corrections=(DatasetCorrection(
            conid=265598, timestamp=dt.datetime(2021, 3, 1, tzinfo=UTC), field="close",
            original_value="150.0", replacement_value="150.5", source="vendor_notice",
            reason="split adjustment error", reviewer="analyst-1"),))
        assert dataset_manifest_digest(with_correction) != base


@pytest.fixture
def repo(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    migrator = SchemaMigrator(db)
    apply_research_migrations(migrator)
    return DatasetManifestRepository(db)


class TestDatasetManifestRepository:
    def test_seal_returns_digest_and_get_round_trips_by_content(self, repo):
        m = _manifest(corrections=(DatasetCorrection(
            conid=265598, timestamp=dt.datetime(2021, 3, 1, tzinfo=UTC), field="close",
            original_value="150.0", replacement_value="150.5", source="notice",
            reason="split fix", reviewer="a1"),))
        digest = repo.seal(m, sealed_at=SEALED_AT)
        assert digest == m.digest
        got = repo.get(digest)
        assert got is not None
        assert got.digest == m.digest          # content round-trips
        assert got.vendor == "polygon"
        assert got.instruments == (265598, 272093)
        assert got.research_eligible is True
        assert len(got.files) == 1 and len(got.corrections) == 1

    def test_get_unknown_digest_is_none(self, repo):
        assert repo.get("deadbeef") is None

    def test_seal_is_idempotent(self, repo):
        m = _manifest()
        d1 = repo.seal(m, sealed_at=SEALED_AT)
        d2 = repo.seal(m, sealed_at=SEALED_AT)          # re-seal identical content
        assert d1 == d2
        got = repo.get(d1)
        assert len(got.files) == 1 and len(got.findings) == 1   # no duplicate rows

    def test_seal_rejects_claimed_digest_mismatch(self, repo):
        with pytest.raises(DigestConflict):
            repo.seal(_manifest(), sealed_at=SEALED_AT, expected_digest="not-the-digest")

    def test_get_detects_storage_tampering(self, repo):
        # A stored row whose content no longer matches its digest key is a
        # corruption -> get fails closed rather than returning wrong evidence.
        digest = repo.seal(_manifest(), sealed_at=SEALED_AT)
        repo._db.transaction(lambda conn: conn.execute(
            "UPDATE dataset_files SET sha256 = 'TAMPERED' WHERE manifest_digest = ?", [digest]))
        with pytest.raises(DigestConflict):
            repo.get(digest)

    def test_persists_ineligibility(self, repo):
        m = _manifest(findings=(_finding(passed=False, required=True),))
        digest = repo.seal(m, sealed_at=SEALED_AT)
        assert repo.get(digest).research_eligible is False

    def test_no_update_or_delete_api(self):
        # Sealed records are append-only: the repository exposes no mutation.
        for forbidden in ("update", "delete", "unseal", "remove"):
            assert not hasattr(DatasetManifestRepository, forbidden)
