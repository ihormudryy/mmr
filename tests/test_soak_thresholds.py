"""[COMPAT] Task 4: recorded soak thresholds (spec §13.3) evaluated fail-closed."""
from scripts.run_paper_soak import Sample, SoakThresholds, evaluate_soak

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
