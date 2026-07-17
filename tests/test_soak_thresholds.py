"""[COMPAT] Task 4: recorded soak thresholds (spec §13.3) evaluated fail-closed."""
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
