"""P4 Task 4 — fault-injection registry and automation fault-drill report.

Covers two layers:

1. ``trader.testing.faults`` — the injection-point registry, coverage
   accounting, and the generic fault primitives (duplication/reordering,
   disk-error injection, the disposable-path production-data guard).
2. ``scripts/automation_fault_drill.py`` — the actual scenario battery: a
   full run must certify all ten injection points, all eleven exercise
   modes, and all six safety invariants from the P4 Task 4 brief, and the
   resulting report must be a validly Ed25519-signed, tamper-evident JSON
   document.

No test here talks to IB or a real broker — every scenario runs against
the same in-process, journal-backed fakes (``FakeOrders``,
``FakeBracketDispatch``, ...) the P1/P3 drills already use, over a
disposable ``tempfile`` DuckDB file.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _PROJECT_ROOT / "scripts"
for _path in (_PROJECT_ROOT, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import automation_fault_drill as drill  # noqa: E402

from trader.research.signing import AttestationSigner, verify_bytes  # noqa: E402
from trader.testing.faults import (  # noqa: E402
    ALL_INJECTION_POINTS,
    DiskErrorInjector,
    ExternalBrokerMutation,
    FaultInjectionRegistry,
    InjectionPoint,
    ProductionDataGuardError,
    SimulatedCrash,
    assert_disposable_path,
    backup_copy,
    duplicate_last,
    reorder_swap_last_two,
    simulate_power_loss,
)


# ---------------------------------------------------------------------------
# FaultInjectionRegistry — coverage ledger.
# ---------------------------------------------------------------------------
class TestFaultInjectionRegistry:
    def test_starts_with_no_coverage(self):
        registry = FaultInjectionRegistry()
        report = registry.coverage_report()
        assert set(report) == {p.value for p in ALL_INJECTION_POINTS}
        assert all(ok is False for ok in report.values())

    def test_mark_records_a_fault_record_and_flips_coverage(self):
        registry = FaultInjectionRegistry()
        record = registry.mark(
            InjectionPoint.COMMAND_CLAIMED, scenario="unit_test", detail="claim replay",
        )
        assert record.point is InjectionPoint.COMMAND_CLAIMED
        assert record.scenario == "unit_test"
        assert record.detail == "claim replay"
        assert registry.fired == (record,)
        report = registry.coverage_report()
        assert report[InjectionPoint.COMMAND_CLAIMED.value] is True
        assert report[InjectionPoint.IB_SEND.value] is False

    def test_mark_accepts_the_plain_string_value_too(self):
        registry = FaultInjectionRegistry()
        registry.mark("ib_send", scenario="unit_test")
        assert InjectionPoint.IB_SEND in registry.exercised_points()

    def test_mark_rejects_unknown_injection_point(self):
        registry = FaultInjectionRegistry()
        with pytest.raises(ValueError):
            registry.mark("not_a_real_point", scenario="unit_test")

    def test_mark_requires_a_scenario_name(self):
        registry = FaultInjectionRegistry()
        with pytest.raises(ValueError):
            registry.mark(InjectionPoint.COMMAND_CLAIMED, scenario="")

    def test_assert_full_coverage_raises_when_a_point_was_never_marked(self):
        registry = FaultInjectionRegistry()
        for point in ALL_INJECTION_POINTS[:-1]:
            registry.mark(point, scenario="unit_test")
        with pytest.raises(AssertionError, match=ALL_INJECTION_POINTS[-1].value):
            registry.assert_full_coverage()

    def test_assert_full_coverage_passes_once_every_point_is_marked(self):
        registry = FaultInjectionRegistry()
        for point in ALL_INJECTION_POINTS:
            registry.mark(point, scenario="unit_test")
        registry.assert_full_coverage()  # must not raise

    def test_coverage_never_silently_fabricated_for_a_narrower_required_set(self):
        """A drill run with a restricted `required` list must not report a
        point as covered just because it's outside the required subset."""
        registry = FaultInjectionRegistry()
        registry.mark(InjectionPoint.COMMAND_CLAIMED, scenario="unit_test")
        report = registry.coverage_report(required=[InjectionPoint.IB_SEND])
        assert report == {InjectionPoint.IB_SEND.value: False}

    def test_reset_clears_fired_records(self):
        registry = FaultInjectionRegistry()
        registry.mark(InjectionPoint.COMMAND_CLAIMED, scenario="unit_test")
        registry.reset()
        assert registry.fired == ()
        assert registry.exercised_points() == frozenset()


# ---------------------------------------------------------------------------
# Generic fault primitives.
# ---------------------------------------------------------------------------
class TestSimulatedCrash:
    def test_is_a_base_exception_not_a_plain_exception(self):
        assert issubclass(SimulatedCrash, BaseException)
        assert not issubclass(SimulatedCrash, Exception)

    def test_escapes_a_bare_except_exception_handler(self):
        def _raises():
            raise SimulatedCrash("hard kill mid-write")

        with pytest.raises(SimulatedCrash):
            try:
                _raises()
            except Exception:  # noqa: BLE001 - the point is this must NOT catch it
                pytest.fail("SimulatedCrash must not be catchable via except Exception")


class TestMessageDuplicationAndReordering:
    def test_duplicate_last_appends_a_copy_of_the_final_event(self):
        events = ["a", "b", "c"]
        result = duplicate_last(events)
        assert result == ["a", "b", "c", "c"]
        assert events == ["a", "b", "c"], "must not mutate the input sequence"

    def test_duplicate_last_rejects_empty_sequence(self):
        with pytest.raises(ValueError):
            duplicate_last([])

    def test_reorder_swap_last_two_swaps_only_the_final_pair(self):
        events = [1, 2, 3, 4]
        result = reorder_swap_last_two(events)
        assert result == [1, 2, 4, 3]
        assert events == [1, 2, 3, 4], "must not mutate the input sequence"

    def test_reorder_swap_last_two_requires_at_least_two_events(self):
        with pytest.raises(ValueError):
            reorder_swap_last_two([1])


class TestDiskErrorInjector:
    def test_first_n_calls_raise_then_falls_through_to_the_real_function(self):
        calls = []
        injector = DiskErrorInjector(fail_calls=2)
        wrapped = injector.wrap(lambda *a, **k: calls.append((a, k)) or "ok")

        with pytest.raises(OSError):
            wrapped(1)
        with pytest.raises(OSError):
            wrapped(2)
        assert wrapped(3) == "ok"
        assert calls == [((3,), {})]
        assert injector.attempts == 3
        assert injector.exhausted

    def test_fail_calls_must_be_at_least_one(self):
        with pytest.raises(ValueError):
            DiskErrorInjector(fail_calls=0)

    def test_custom_message_is_used_on_the_raised_error(self):
        injector = DiskErrorInjector(fail_calls=1, message="ENOSPC: no space left on device")
        wrapped = injector.wrap(lambda: "unreachable")
        with pytest.raises(OSError, match="ENOSPC"):
            wrapped()


class TestExternalBrokerMutation:
    def test_carries_no_command_or_order_group_identity(self):
        mutation = ExternalBrokerMutation(
            kind="position", conid=265598, quantity=100.0, description="manual TWS trade",
        )
        assert mutation.kind == "position"
        assert not hasattr(mutation, "command_id")
        assert not hasattr(mutation, "order_group_id")


# ---------------------------------------------------------------------------
# Production-data safety guard + disposable-volume helpers.
# ---------------------------------------------------------------------------
class TestProductionDataGuard:
    def test_refuses_a_realistic_production_data_path(self):
        production_like = str(Path.home() / ".local" / "share" / "mmr" / "mmr.duckdb")
        with pytest.raises(ProductionDataGuardError):
            assert_disposable_path(production_like)

    def test_refuses_a_repo_config_path(self):
        with pytest.raises(ProductionDataGuardError):
            assert_disposable_path(_PROJECT_ROOT / "config_defaults" / "trader.yaml")

    def test_accepts_a_path_under_the_temp_root(self, tmp_path: Path):
        # tmp_path (pytest) is not guaranteed to be under tempfile.gettempdir()
        # on every platform, so exercise this via a real tempfile dir.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            resolved = assert_disposable_path(Path(tmp) / "scratch.duckdb")
            assert resolved == (Path(tmp) / "scratch.duckdb").resolve()

    def test_backup_copy_refuses_a_non_disposable_destination(self, tmp_path: Path):
        source = tmp_path / "source.duckdb"
        source.write_bytes(b"duckdb-bytes")
        with pytest.raises(ProductionDataGuardError):
            backup_copy(source, _PROJECT_ROOT)

    def test_backup_copy_rejects_a_missing_source(self, tmp_path: Path):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(FileNotFoundError):
                backup_copy(tmp_path / "does-not-exist.duckdb", tmp)

    def test_backup_copy_copies_the_wal_companion_when_present(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src" / "db.duckdb"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"main-file")
            source.with_name("db.duckdb.wal").write_bytes(b"wal-file")
            dest_dir = Path(tmp) / "dest"
            copied = backup_copy(source, dest_dir)
            assert copied.read_bytes() == b"main-file"
            assert (dest_dir / "db.duckdb.wal").read_bytes() == b"wal-file"

    def test_simulate_power_loss_refuses_a_non_disposable_path(self):
        with pytest.raises(ProductionDataGuardError):
            simulate_power_loss(_PROJECT_ROOT / "config_defaults" / "trader.yaml")

    def test_simulate_power_loss_truncates_the_tail_of_a_disposable_copy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copy.duckdb"
            path.write_bytes(b"x" * 100)
            simulate_power_loss(path, truncate_bytes=40)
            assert path.stat().st_size == 60

    def test_simulate_power_loss_rejects_an_empty_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.duckdb"
            path.write_bytes(b"")
            with pytest.raises(ValueError):
                simulate_power_loss(path)


# ---------------------------------------------------------------------------
# Full automation fault-drill battery (scripts/automation_fault_drill.py).
# ---------------------------------------------------------------------------
class TestAutomationFaultDrill:
    """These exercise the REAL scenario battery, not fakes-of-fakes — same
    guarantee ``mmr``'s CI gate relies on. Session-scoped so the (few
    seconds of) full battery only runs once for every assertion below.
    """

    @pytest.fixture(scope="class")
    def report(self):
        return drill.run_drills()

    def test_full_run_passes(self, report):
        failures = [r for r in report.scenario_results if r["status"] != "passed"]
        assert not failures, f"scenario failures: {failures}"
        assert report.passed is True

    def test_every_declared_scenario_ran(self, report):
        ran = {r["name"] for r in report.scenario_results}
        assert ran == set(drill.SCENARIOS)

    def test_all_ten_injection_points_are_covered(self, report):
        assert set(report.injection_point_coverage) == {p.value for p in ALL_INJECTION_POINTS}
        missing = [p for p, ok in report.injection_point_coverage.items() if not ok]
        assert not missing, f"injection points never exercised: {missing}"

    def test_all_exercise_modes_from_the_brief_are_covered(self, report):
        assert set(report.exercise_coverage) == set(drill.EXERCISE_ITEMS)
        missing = [item for item, ok in report.exercise_coverage.items() if not ok]
        assert not missing, f"exercise modes never certified: {missing}"

    def test_all_six_invariants_pass(self, report):
        assert set(report.invariant_results) == set(drill.INVARIANTS)
        failing = [inv for inv, ok in report.invariant_results.items() if not ok]
        assert not failing, f"invariants not certified: {failing}"

    @pytest.mark.parametrize("invariant", list(drill.INVARIANTS))
    def test_named_invariant_passes(self, report, invariant):
        assert report.invariant_results[invariant] is True

    def test_report_carries_git_config_container_and_artifact_digests(self, report):
        assert report.commit_digest and report.commit_digest != "unknown"
        assert report.config_digest.startswith("sha256:")
        assert report.container_digest  # "not_containerized" is a valid, honest value
        assert report.artifact_digest

    def test_report_is_deterministic_in_shape_across_repeated_runs(self):
        """Not byte-identical (timestamps/signatures differ), but the same
        scenarios, coverage keys, and pass/fail outcome every time."""
        first = drill.run_drills()
        second = drill.run_drills()
        assert [r["name"] for r in first.scenario_results] == [
            r["name"] for r in second.scenario_results
        ]
        assert [r["status"] for r in first.scenario_results] == [
            r["status"] for r in second.scenario_results
        ]
        assert first.injection_point_coverage == second.injection_point_coverage
        assert first.invariant_results == second.invariant_results
        assert first.passed == second.passed is True


class TestSubsetRunsDoNotFabricateFullCoverage:
    def test_a_single_scenario_subset_still_reports_only_its_own_coverage(self):
        report = drill.run_drills(["cp_command_claim_identity"])
        assert report.scenario_results[0]["name"] == "cp_command_claim_identity"
        assert report.scenario_results[0]["status"] == "passed"
        # A partial run must never silently claim full injection-point coverage.
        assert report.injection_point_coverage[InjectionPoint.COMMAND_CLAIMED.value] is True
        assert report.injection_point_coverage[InjectionPoint.REPLAY_SEAL.value] is False

    def test_a_partial_run_is_never_marked_passed(self):
        report = drill.run_drills(["cp_command_claim_identity"])
        assert report.passed is False

    def test_an_unknown_scenario_name_is_reported_not_silently_dropped(self):
        report = drill.run_drills(["cp_command_claim_identity", "does_not_exist"])
        statuses = {r["name"]: r["status"] for r in report.scenario_results}
        assert statuses["does_not_exist"] == "unknown"
        assert report.passed is False


class TestFaultDrillReportSigning:
    def test_report_from_run_drills_is_signed_by_default(self):
        report = drill.run_drills(["cp_command_claim_identity"])
        assert report.signature
        assert report.public_key_id
        assert report.signing_key_source == "ephemeral"

    def test_signature_verifies_against_the_signer_used(self):
        signer = AttestationSigner.generate()
        report = drill.run_drills(["cp_command_claim_identity"], signer=signer, key_source="test")
        report.verify(signer.public_key)  # must not raise
        assert report.signing_key_source == "test"

    def test_signature_verification_fails_for_the_wrong_public_key(self):
        signer = AttestationSigner.generate()
        other = AttestationSigner.generate()
        report = drill.run_drills(["cp_command_claim_identity"], signer=signer, key_source="test")
        with pytest.raises(Exception):
            report.verify(other.public_key)

    def test_tampering_with_the_payload_after_signing_breaks_verification(self):
        signer = AttestationSigner.generate()
        report = drill.run_drills(["cp_command_claim_identity"], signer=signer, key_source="test")
        report.passed = not report.passed  # tamper
        with pytest.raises(Exception):
            report.verify(signer.public_key)

    def test_unsigned_report_raises_on_verify(self):
        report = drill.run_drills(["cp_command_claim_identity"])
        report.signature = ""
        with pytest.raises(ValueError):
            report.verify(AttestationSigner.generate().public_key)

    def test_to_payload_round_trips_through_canonical_json_and_verifies(self):
        """The exact bytes a real operator would hash/store/transmit."""
        from trader.research.canonical import canonical_json_bytes

        signer = AttestationSigner.generate()
        report = drill.run_drills(["cp_command_claim_identity"], signer=signer, key_source="test")
        payload = report.to_payload()
        signature = payload.pop("signature")
        payload.pop("public_key_id")
        message = canonical_json_bytes(payload)
        verify_bytes(signer.public_key, message, signature)  # must not raise


class TestFaultDrillCLI:
    def test_main_json_flag_prints_a_single_canonical_json_line(self, capsys):
        exit_code = drill.main(["--scenarios", "cp_command_claim_identity", "--json"])
        assert exit_code == 1  # a scenario subset is never a full/passing run
        out = capsys.readouterr().out.strip()
        assert out.count("\n") == 0
        import json

        payload = json.loads(out)
        assert payload["kind"] == "automation-fault-drill"
        assert payload["scenario_results"][0]["name"] == "cp_command_claim_identity"

    def test_main_writes_the_report_to_the_requested_output_file(self, tmp_path: Path):
        out_path = tmp_path / "report.json"
        exit_code = drill.main([
            "--scenarios", "cp_command_claim_identity", "--output", str(out_path),
        ])
        assert exit_code == 1
        import json

        payload = json.loads(out_path.read_text())
        assert payload["scenario_results"][0]["name"] == "cp_command_claim_identity"

    def test_main_exit_code_is_zero_only_for_a_full_passing_run(self):
        assert drill.main([]) == 0


class TestScenarioDeclarationsAreConsistent:
    """Guards against the report's coverage blocks silently drifting from
    the actual scenario registry (e.g. a new scenario added to SCENARIOS
    but never wired into the exercise/invariant maps)."""

    def test_every_scenario_has_an_exercise_declaration(self):
        assert set(drill.SCENARIO_EXERCISES) == set(drill.SCENARIOS)

    def test_every_scenario_has_an_invariant_declaration(self):
        assert set(drill.SCENARIO_INVARIANTS) == set(drill.SCENARIOS)

    def test_every_exercise_item_has_at_least_one_contributing_scenario(self):
        declared_items = {
            item for items in drill.SCENARIO_EXERCISES.values() for item in items
        }
        assert declared_items == set(drill.EXERCISE_ITEMS)

    def test_every_invariant_has_at_least_one_contributing_scenario(self):
        declared_invariants = {
            inv for invs in drill.SCENARIO_INVARIANTS.values() for inv in invs
        }
        assert declared_invariants == set(drill.INVARIANTS)

    def test_every_injection_point_enum_value_is_a_plain_string(self):
        # Guards the report's coverage dict keys (json-serializable, stable).
        for point in ALL_INJECTION_POINTS:
            assert isinstance(point.value, str)
            copy.deepcopy(point)  # enum members must remain picklable/copyable
