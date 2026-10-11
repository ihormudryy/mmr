"""SP3 Plan 1 Task 3: the streamed-evaluation benchmark (spec 10)."""
from __future__ import annotations

import json
import multiprocessing
import time

import pytest

import scripts.sp3_benchmark as sp3_benchmark
from scripts.sp3_benchmark import (
    BenchmarkTiming, extrapolate, in_process_baseline, parse_signal, ruling_text, stream_evaluation,
    synthetic_session,
)

PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}


def test_synthetic_session_is_regular_hours_utc():
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=60)
    assert len(frame) == 60
    assert str(frame.index.tz) == "UTC"
    eastern = frame.index.tz_convert("America/New_York")
    assert eastern[0].hour == 9 and eastern[0].minute == 30


def test_stream_evaluation_returns_decisions_and_timing():
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=120)
    timing, decisions = stream_evaluation(
        "tests.authored.fixtures.orb_authored", "OpeningRangeBreakout",
        [frame], lookback=420, params=PARAMS)
    assert isinstance(timing, BenchmarkTiming)
    # One bar is a warm-up, not timed, so bars is len(frame) - 1.
    assert timing.bars == len(frame) - 1 and timing.per_bar_ms > 0
    # The synthetic session is built to fire at least one breakout, so the
    # benchmark is timing real work, not an all-None pass.
    assert any(d is not None for d in decisions)


def test_extrapolate_breaks_down_the_send_volume():
    estimate = extrapolate(per_bar_ms=0.2, conids=10, period_sessions=690,
                           holdout_sessions=90, cohort_points=3, tunables=2,
                           bars_per_session=390, folds=6)
    assert estimate["total_bar_sends"] > 0
    assert estimate["hours"] > 0
    # 3 cost multipliers + 2 tunables x 2 neighbours = 7 pool passes per point.
    assert estimate["pool_passes_per_point"] == 7


def test_ruling_text_names_the_four_hour_rule():
    text = ruling_text(extrapolate(per_bar_ms=0.2, conids=10, period_sessions=690,
                                   holdout_sessions=90, cohort_points=3, tunables=2,
                                   bars_per_session=390, folds=6))
    assert "4 h" in text and "period_sessions" in text


def _signal(**overrides) -> bytes:
    reply = {"v": 1, "type": "SIGNAL", "session": 0, "t": "T", "action": "BUY", "probability": 0.6}
    return json.dumps({**reply, **overrides}).encode()


def test_parse_signal_accepts_the_full_signal_shape():
    assert parse_signal(_signal(), session=0, t="T")["probability"] == 0.6
    assert parse_signal(_signal(action=None, probability=None), session=0, t="T")["action"] is None


@pytest.mark.parametrize("bad", [
    {"type": "ERROR"}, {"v": 2}, {"session": 1}, {"t": "U"}, {"probability": None},
])
def test_parse_signal_rejects_a_reply_that_does_not_match_the_bar(bad):
    with pytest.raises(ValueError, match="bad SIGNAL reply"):
        parse_signal(_signal(**bad), session=0, t="T")


@pytest.fixture
def started_processes(monkeypatch):
    """Every Process the benchmark creates, so a test checks its own children only."""
    processes: list[multiprocessing.Process] = []

    def recording_process(*args, **kwargs):
        process = multiprocessing.Process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sp3_benchmark, "Process", recording_process)
    return processes


@pytest.mark.parametrize("module_name, class_name, params", [
    ("tests.authored.fixtures.orb_authored", "NoSuchStrategy", PARAMS),
    ("tests.authored.fixtures.no_such_module", "OpeningRangeBreakout", PARAMS),
    ("tests.authored.fixtures.orb_authored", "OpeningRangeBreakout", {}),
])
def test_a_dying_child_fails_fast_and_leaves_no_process(module_name, class_name, params, started_processes):
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=60)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="child"):
        stream_evaluation(module_name, class_name, [frame], lookback=420, params=params)
    assert time.monotonic() - started < 10
    assert len(started_processes) == 1 and not started_processes[0].is_alive()


def test_a_normal_run_leaves_no_child_process(started_processes):
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=60)
    stream_evaluation("tests.authored.fixtures.orb_authored", "OpeningRangeBreakout",
                      [frame], lookback=420, params=PARAMS)
    assert len(started_processes) == 1 and not started_processes[0].is_alive()


def _estimate(per_bar_ms, **overrides):
    arguments = dict(per_bar_ms=per_bar_ms, conids=10, period_sessions=690, holdout_sessions=90,
                     cohort_points=3, tunables=2, bars_per_session=390, folds=6)
    return extrapolate(**{**arguments, **overrides})


def test_ruling_text_prints_the_actual_inputs():
    text = ruling_text(_estimate(0.2, conids=7, period_sessions=500, holdout_sessions=60,
                                 cohort_points=4, tunables=3, folds=5, embargo_sessions=9))
    for expected in ("7 conids", "500 sessions", "4 cohort points", "3 tunables",
                     "5 folds", "embargo 9", "60-session holdout"):
        assert expected in text


def test_extrapolate_and_extrapolate_seconds_agree():
    from scripts.sp3_benchmark import extrapolate_seconds
    estimate = _estimate(0.37)
    assert estimate["seconds"] == pytest.approx(extrapolate_seconds(
        per_bar_ms=0.37, conids=10, period_sessions=690, holdout_sessions=90,
        cohort_points=3, tunables=2, bars_per_session=390, folds=6))


def test_ruling_text_says_so_when_even_the_minimum_period_is_over_four_hours():
    estimate = _estimate(50.0)
    assert estimate["over_four_hours"] and not estimate["fits_four_hours_at_minimum"]
    text = ruling_text(estimate)
    assert "132" in text and "still over 4 h" in text and "N=132" not in text


def test_ruling_text_proposes_a_shorter_period_when_one_fits():
    estimate = _estimate(0.9)
    assert estimate["over_four_hours"] and estimate["fits_four_hours_at_minimum"]
    assert estimate["proposed_period_sessions"] < 690
    assert f"N={estimate['proposed_period_sessions']}" in ruling_text(estimate)


def test_ruling_text_keeps_the_period_when_under_four_hours():
    estimate = _estimate(0.01)
    assert not estimate["over_four_hours"]
    assert "keep" in ruling_text(estimate) and "690" in ruling_text(estimate)


def test_ruling_text_names_the_combined_upper_bound_not_a_streamed_cost():
    text = ruling_text(_estimate(0.9))
    assert "ms/bar streamed" not in text
    assert "upper-bound combined cost" in text


def test_main_labels_the_combined_line_as_an_upper_bound(monkeypatch, capsys):
    monkeypatch.setattr(sp3_benchmark, "stream_evaluation",
                        lambda *args, **kwargs: (BenchmarkTiming(bars=10, wall_seconds=0.005), ["BUY"]))
    monkeypatch.setattr(sp3_benchmark, "in_process_baseline",
                        lambda frames: (BenchmarkTiming(bars=10, wall_seconds=0.003), 1))
    assert sp3_benchmark.main(["--sessions", "1", "--bars-per-session", "10", "--runs", "1"]) == 0
    output = capsys.readouterr().out
    assert "ms/bar streamed" not in output
    assert "upper-bound combined cost per bar" in output


def test_the_in_process_baseline_removes_its_database_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sp3_benchmark.tempfile, "tempdir", str(tmp_path))
    frame = synthetic_session(conid_seed=1, sessions=1, bars_per_session=60)
    timing, _ = in_process_baseline([frame])
    assert timing.bars == 60
    assert list(tmp_path.iterdir()) == []
