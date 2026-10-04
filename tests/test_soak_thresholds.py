"""[COMPAT] Task 4: recorded soak thresholds (spec §13.3) evaluated fail-closed."""
import dataclasses
import json

import pytest

from scripts.run_paper_soak import (
    Sample,
    SoakThresholds,
    cc_health_metrics,
    evaluate_soak,
    fold_read_model_sample,
)

T = SoakThresholds()

GOOD_HARNESS = {'p95_critical_ms': 180.0, 'unhandled_errors': 0,
                'unresolved_commands': 0, 'max_replay_ring_events': 6000,
                'max_client_fifo_depth': 400, 'max_terminal_rows': 480}


def _samples(rss_start=1.0e9, rss_end=1.1e9, cpu=0.4, minutes=480):
    # one sample per minute; warm-up is the first 60
    step = (rss_end - rss_start) / max(minutes - T.warmup_minutes, 1)
    out = []
    for m in range(minutes):
        rss = rss_start if m < T.warmup_minutes else \
            rss_start + step * (m - T.warmup_minutes)
        out.append(Sample(minute=m, rss_bytes=rss, cpu_cores=cpu))
    return out


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_healthy_run_passes():
    report = evaluate_soak(_samples(), GOOD_HARNESS, {'trader_outage': 'coherent'}, T)
    assert report.passed


def test_rss_growth_at_exactly_twenty_percent_passes():
    report = evaluate_soak(_samples(rss_end=1.2e9), GOOD_HARNESS, {}, T)
    assert _check(report, 'rss_growth').passed


def test_rss_growth_beyond_twenty_percent_fails():
    report = evaluate_soak(_samples(rss_end=1.21e9), GOOD_HARNESS, {}, T)
    assert not _check(report, 'rss_growth').passed and not report.passed


def test_warmup_hour_is_excluded_from_rss_baseline():
    # A big jump entirely inside the warm-up hour must not count as growth.
    samples = _samples()
    for s in samples[:T.warmup_minutes]:
        object.__setattr__(s, 'rss_bytes', 0.5e9)
    report = evaluate_soak(samples, GOOD_HARNESS, {}, T)
    assert _check(report, 'rss_growth').passed


def test_cpu_average_of_one_core_or_more_fails():
    report = evaluate_soak(_samples(cpu=1.0), GOOD_HARNESS, {}, T)
    assert not _check(report, 'cpu_avg_cores').passed


def test_p95_over_500ms_fails():
    harness = {**GOOD_HARNESS, 'p95_critical_ms': 500.1}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_any_unresolved_command_fails():
    harness = {**GOOD_HARNESS, 'unresolved_commands': 1}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_bound_violations_fail():
    harness = {**GOOD_HARNESS, 'max_client_fifo_depth': 1001}
    assert not evaluate_soak(_samples(), harness, {}, T).passed


def test_missing_samples_fail_closed():
    report = evaluate_soak([], GOOD_HARNESS, {}, T)
    assert not report.passed
    assert not _check(report, 'samples_present').passed


def test_failed_scenario_fails_run():
    report = evaluate_soak(_samples(), GOOD_HARNESS, {'trader_outage': 'failed'}, T)
    assert not report.passed


# --- COMPAT exporters: replay-ring / FIFO / terminal-rows soak metrics ------
#
# `cc_health_metrics` / `fold_read_model_sample` translate `/api/cc-health`
# samples (state.ring_depth() / fanout.max_fifo_depth() /
# state.terminal_row_count(), see web/command_center/health.py) into the
# flat `max_*` keys `evaluate_soak` understands -- mirrors `harness_metrics`
# for the M1-R soak-harness report.

def test_cc_health_metrics_maps_cc_health_keys_to_evaluate_soak_keys():
    raw = {"replay_ring_events": 42, "client_fifo_depth_max": 7,
           "terminal_rows": 12, "sse_clients": 2}  # unrelated key ignored
    assert cc_health_metrics(raw) == {
        "max_replay_ring_events": 42,
        "max_client_fifo_depth": 7,
        "max_terminal_rows": 12,
    }


def test_cc_health_metrics_defaults_missing_keys_to_zero():
    assert cc_health_metrics({}) == {
        "max_replay_ring_events": 0,
        "max_client_fifo_depth": 0,
        "max_terminal_rows": 0,
    }


def test_fold_read_model_sample_keeps_running_max_across_samples():
    maxima = {}
    maxima = fold_read_model_sample(maxima, {"replay_ring_events": 10,
                                             "client_fifo_depth_max": 2,
                                             "terminal_rows": 5})
    maxima = fold_read_model_sample(maxima, {"replay_ring_events": 3,
                                             "client_fifo_depth_max": 9,
                                             "terminal_rows": 1})
    assert maxima == {"max_replay_ring_events": 10, "max_client_fifo_depth": 9,
                      "max_terminal_rows": 5}


def test_fold_read_model_sample_does_not_mutate_input():
    maxima = {"max_replay_ring_events": 1, "max_client_fifo_depth": 1,
              "max_terminal_rows": 1}
    frozen = dict(maxima)
    fold_read_model_sample(maxima, {"replay_ring_events": 99,
                                    "client_fifo_depth_max": 99,
                                    "terminal_rows": 99})
    assert maxima == frozen


# --- P4 Task 2: opt-in breaker/readiness/reconciliation/protection/flat/
# replay metrics + signed configuration digest ------------------------------

def test_new_thresholds_default_to_none_and_add_no_checks():
    """Backward compatibility is the whole point: the default
    ``SoakThresholds()`` (used by every pre-existing call site and by
    ``main()``) must add none of the six new checks."""
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, T)
    names = {c.name for c in report.checks}
    for new_metric in ('breaker_trips', 'readiness_pass_rate',
                       'reconciliation_mismatches', 'protection_gaps',
                       'missed_flats', 'replay_mismatches'):
        assert new_metric not in names
    assert report.passed


def test_breaker_trips_threshold_when_set_is_fail_closed_on_missing_observation():
    from scripts.run_paper_soak import SoakThresholds as ST

    t = dataclasses.replace(T, max_breaker_trips=0)
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, t)
    check = _check(report, 'breaker_trips')
    assert check.observed is None and not check.passed
    assert not report.passed
    assert isinstance(t, ST)


def test_breaker_trips_threshold_passes_when_observed_and_within_limit():
    t = dataclasses.replace(T, max_breaker_trips=0)
    harness = {**GOOD_HARNESS, 'breaker_trips': 0}
    report = evaluate_soak(_samples(), harness, {}, t)
    assert _check(report, 'breaker_trips').passed
    assert report.passed


def test_breaker_trips_threshold_fails_when_over_limit():
    t = dataclasses.replace(T, max_breaker_trips=0)
    harness = {**GOOD_HARNESS, 'breaker_trips': 1}
    report = evaluate_soak(_samples(), harness, {}, t)
    assert not _check(report, 'breaker_trips').passed


def test_readiness_pass_rate_is_a_minimum_not_a_maximum():
    t = dataclasses.replace(T, min_readiness_pass_rate=1.0)
    below = evaluate_soak(_samples(), {**GOOD_HARNESS, 'readiness_pass_rate': 0.9}, {}, t)
    at_min = evaluate_soak(_samples(), {**GOOD_HARNESS, 'readiness_pass_rate': 1.0}, {}, t)
    assert not _check(below, 'readiness_pass_rate').passed
    assert _check(at_min, 'readiness_pass_rate').passed


def test_all_six_new_optional_metrics_can_be_set_independently():
    t = dataclasses.replace(
        T,
        max_breaker_trips=0, min_readiness_pass_rate=1.0,
        max_reconciliation_mismatches=0, max_protection_gaps=0,
        max_missed_flats=0, max_replay_mismatches=0,
    )
    harness = {
        **GOOD_HARNESS,
        'breaker_trips': 0, 'readiness_pass_rate': 1.0,
        'reconciliation_mismatches': 0, 'protection_gaps': 0,
        'missed_flats': 0, 'replay_mismatches': 0,
    }
    report = evaluate_soak(_samples(), harness, {}, t)
    assert report.passed
    for name in ('breaker_trips', 'readiness_pass_rate', 'reconciliation_mismatches',
                'protection_gaps', 'missed_flats', 'replay_mismatches'):
        assert _check(report, name).passed


def test_config_digest_absent_when_no_config_supplied():
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, T)
    assert report.config_digest is None
    assert report.config_signature is None


def test_config_digest_is_deterministic_and_present_when_config_supplied():
    from scripts.run_paper_soak import compute_config_digest

    config = {"artifact_id": "art-1", "allowlist": ["265598"], "max_gross_allocation": 0.06}
    report_a = evaluate_soak(_samples(), GOOD_HARNESS, {}, T, config=config)
    report_b = evaluate_soak(_samples(), GOOD_HARNESS, {}, T, config=dict(config))
    assert report_a.config_digest == report_b.config_digest == compute_config_digest(config)
    assert report_a.config_signature is None  # no signer supplied


def test_config_digest_changes_when_config_changes():
    from scripts.run_paper_soak import compute_config_digest

    a = compute_config_digest({"artifact_id": "art-1"})
    b = compute_config_digest({"artifact_id": "art-2"})
    assert a != b


def test_signed_config_digest_verifies_against_the_signer_public_key():
    from trader.research import signing
    from trader.research.canonical import canonical_json_bytes

    signer = signing.AttestationSigner.generate()
    config = {"artifact_id": "art-1", "allowlist": ["265598"]}
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, T, config=config, signer=signer)

    assert report.config_signature is not None
    assert report.config_signer_key_id == signer.public_key_id
    # The signature verifies over the exact canonical config bytes.
    signing.verify_bytes(signer.public_key, canonical_json_bytes(config), report.config_signature)


def test_tampered_config_fails_signature_verification():
    from trader.research import signing
    from trader.research.canonical import canonical_json_bytes

    signer = signing.AttestationSigner.generate()
    config = {"artifact_id": "art-1"}
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, T, config=config, signer=signer)

    tampered = {"artifact_id": "art-TAMPERED"}
    with pytest.raises(signing.BadSignature):
        signing.verify_bytes(signer.public_key, canonical_json_bytes(tampered), report.config_signature)


def test_report_to_json_includes_config_fields():
    from trader.research import signing

    signer = signing.AttestationSigner.generate()
    config = {"artifact_id": "art-1"}
    report = evaluate_soak(_samples(), GOOD_HARNESS, {}, T, config=config, signer=signer)
    body = json.loads(report.to_json())
    assert body["config_digest"] == report.config_digest
    assert body["config_signature"] == report.config_signature
    assert body["config_signer_key_id"] == signer.public_key_id


def test_real_harness_shape_reports_three_new_metrics_live_two_stay_fail_closed():
    """Mirrors what `run_paper_soak.py` actually assembles today: p95 from
    the M1-R soak-harness subprocess + the three read-model maxima sampled
    from `/api/cc-health` over the run, but `unhandled_errors`/
    `unresolved_commands` absent -- no live exporter exists for either yet
    (trader-side command-ledger / error-log surface -- Worker-B/F3
    follow-up), so those two must still fail closed."""
    harness = {
        "p95_critical_ms": 180.0,
        **fold_read_model_sample({}, {"replay_ring_events": 42,
                                     "client_fifo_depth_max": 3,
                                     "terminal_rows": 12}),
    }
    report = evaluate_soak(_samples(), harness, {}, T)

    ring = _check(report, "replay_ring_events")
    fifo = _check(report, "client_fifo_depth")
    terminal = _check(report, "terminal_rows")
    assert (ring.observed, ring.passed) == (42, True)
    assert (fifo.observed, fifo.passed) == (3, True)
    assert (terminal.observed, terminal.passed) == (12, True)

    unhandled = _check(report, "unhandled_errors")
    unresolved = _check(report, "unresolved_commands")
    assert unhandled.observed is None and not unhandled.passed
    assert unresolved.observed is None and not unresolved.passed
    assert not report.passed  # the two still-fail-closed checks sink the run
