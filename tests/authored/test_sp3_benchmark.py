"""SP3 Plan 1 Task 3: the streamed-evaluation benchmark (spec 10)."""
from __future__ import annotations

from scripts.sp3_benchmark import (
    BenchmarkTiming, extrapolate, ruling_text, stream_evaluation, synthetic_session,
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
