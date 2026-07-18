"""P2 Task 3B — dataset bar-quality qualification (design §8.1).

A research dataset must be exchange-calendar-complete over the regular session,
free of duplicate/out-of-session/corrupt bars, with genuine gaps/halts REPORTED
and KEPT (never silently dropped). Only a fully-attributed verified correction
(original + replacement + source + reason + reviewer) may alter a bar; the
original value stays auditable. Fail closed: emit a REQUIRED failing finding
rather than drop a bad instrument/bar. Identical inputs -> identical findings.

Tests are fully offline + deterministic: in-memory pandas frames + the real
XNYS calendar (no IB, no network, no live data).
"""
from __future__ import annotations

import datetime as dt

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from trader.research.dataset_manifest import DatasetCorrection, QualityFinding
from trader.research.data_quality import (
    DatasetCorrectionResult,
    DatasetQualification,
    DatasetQualificationRequest,
    DatasetQualifier,
)

UTC = dt.timezone.utc
CAL = xcals.get_calendar("XNYS")

FULL_DAY = "2024-07-01"       # regular summer session: 13:30-20:00 UTC
HALF_DAY = "2024-07-03"       # early close: 13:30-17:00 UTC
DST_PRE = "2024-03-08"        # pre-DST: 14:30-21:00 UTC
DST_POST = "2024-03-11"       # post-DST: 13:30-20:00 UTC
SATURDAY = "2024-07-06"       # not a session


# --- independent grid construction (pandas date_range, not the impl's loop) ---
def grid(session: str, step_min: int, convention: str = "bar_start") -> pd.DatetimeIndex:
    o = CAL.session_open(session)
    c = CAL.session_close(session)
    freq = f"{step_min}min"
    side = "left" if convention == "bar_start" else "right"
    return pd.date_range(o, c, freq=freq, inclusive=side)


def make_frame(index: pd.DatetimeIndex, base: float = 100.0) -> pd.DataFrame:
    n = len(index)
    df = pd.DataFrame(
        {
            "open": [base] * n,
            "high": [base + 1.0] * n,
            "low": [base - 1.0] * n,
            "close": [base] * n,
            "volume": [1000] * n,
        },
        index=index,
    )
    df.index.name = "date"
    return df


def qualify(bars, interval="30 mins", **over) -> DatasetQualification:
    req = DatasetQualificationRequest(bars=bars, bar_interval=interval, **over)
    return DatasetQualifier().qualify(req)


# --------------------------------------------------------------------------- #
# Happy path + shape
# --------------------------------------------------------------------------- #
class TestCompleteDataset:
    def test_complete_regular_session_is_eligible(self):
        q = qualify({265598: make_frame(grid(FULL_DAY, 30))})
        assert q.passed is True
        assert q.eligible is True
        assert q.finding("session_completeness").passed is True

    def test_all_findings_are_quality_findings_and_sorted(self):
        q = qualify({265598: make_frame(grid(FULL_DAY, 30))})
        assert all(isinstance(f, QualityFinding) for f in q.findings)
        names = [f.name for f in q.findings]
        assert names == sorted(names)
        # the mandated checks all appear
        for name in (
            "session_completeness",
            "timestamps_within_session",
            "duplicate_timestamps",
            "intra_session_gaps",
            "corrupt_ohlcv",
            "split_discontinuity",
            "corrections_valid",
        ):
            assert q.finding(name) is not None, name

    def test_eligibility_is_governed_only_by_required_findings(self):
        q = qualify({265598: make_frame(grid(FULL_DAY, 30))})
        for f in q.findings:
            if f.required:
                assert f.passed is True
        assert q.passed == all(f.passed for f in q.findings if f.required)


# --------------------------------------------------------------------------- #
# Completeness, early close, DST
# --------------------------------------------------------------------------- #
class TestCompleteness:
    def test_missing_bar_fails_completeness_and_is_not_backfilled(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx).drop(idx[5])          # drop one interior bar
        q = qualify({265598: df})
        sc = q.finding("session_completeness")
        assert sc.passed is False and sc.required is True
        assert q.passed is False
        # fail-closed: the gap is NOT filled and nothing else is dropped
        assert len(q.qualified_bars[265598]) == len(df)

    def test_early_close_half_day_complete_passes(self):
        # A correctly-truncated half-day (7 x 30-min bars) is complete: the
        # qualifier must NOT expect a full 6.5h session.
        q = qualify({265598: make_frame(grid(HALF_DAY, 30))})
        assert q.finding("session_completeness").passed is True
        assert q.passed is True

    def test_early_close_bar_after_close_is_out_of_session(self):
        # 17:30 UTC would be a valid bar on a normal day but the 2024-07-03
        # session closes at 17:00 -> it must be flagged out-of-session, and the
        # 17:00-20:00 window must NOT be reported as missing.
        idx = grid(HALF_DAY, 30)
        extra = pd.Timestamp("2024-07-03 17:30", tz="UTC")
        df = make_frame(idx.append(pd.DatetimeIndex([extra])))
        q = qualify({265598: df})
        assert q.finding("session_completeness").passed is True
        wis = q.finding("timestamps_within_session")
        assert wis.passed is False and wis.required is True
        assert extra in q.reports["timestamps_within_session"][265598]["unexpected"]

    def test_dst_two_sessions_complete_passes(self):
        # Pre-DST (14:30-21:00) and post-DST (13:30-20:00) have different UTC
        # windows; a fixed-offset assumption would misalign one of them.
        bars = {
            265598: make_frame(grid(DST_PRE, 30)).combine_first(
                make_frame(grid(DST_POST, 30))
            )
        }
        q = qualify(bars, expected_start=dt.date(2024, 3, 8), expected_end=dt.date(2024, 3, 11))
        assert q.finding("session_completeness").passed is True
        assert q.finding("timestamps_within_session").passed is True
        assert q.passed is True

    def test_dst_bar_after_post_dst_close_is_out_of_session(self):
        # 20:30 UTC is valid pre-DST (close 21:00) but out-of-session on the
        # post-DST 2024-03-11 session (close 20:00).
        idx = grid(DST_POST, 30)
        extra = pd.Timestamp("2024-03-11 20:30", tz="UTC")
        df = make_frame(idx.append(pd.DatetimeIndex([extra])))
        q = qualify({265598: df})
        wis = q.finding("timestamps_within_session")
        assert wis.passed is False
        assert extra in q.reports["timestamps_within_session"][265598]["unexpected"]

    def test_premarket_bar_is_out_of_session(self):
        idx = grid(FULL_DAY, 30)
        pre = pd.Timestamp("2024-07-01 13:00", tz="UTC")   # before 13:30 open
        df = make_frame(idx.append(pd.DatetimeIndex([pre])))
        q = qualify({265598: df})
        assert q.finding("timestamps_within_session").passed is False
        assert q.finding("session_completeness").passed is True

    def test_weekend_bar_is_out_of_session(self):
        idx = grid(FULL_DAY, 30)
        sat = pd.Timestamp(f"{SATURDAY} 14:00", tz="UTC")
        df = make_frame(idx.append(pd.DatetimeIndex([sat])))
        q = qualify({265598: df})
        assert q.finding("timestamps_within_session").passed is False
        assert sat in q.reports["timestamps_within_session"][265598]["unexpected"]


# --------------------------------------------------------------------------- #
# Duplicates + gaps (reported, kept)
# --------------------------------------------------------------------------- #
class TestDuplicatesAndGaps:
    def test_duplicate_timestamp_fails(self):
        idx = grid(FULL_DAY, 30)
        df = pd.concat([make_frame(idx), make_frame(idx[[3]])])  # duplicate idx[3]
        q = qualify({265598: df})
        dup = q.finding("duplicate_timestamps")
        assert dup.passed is False and dup.required is True
        assert idx[3] in q.reports["duplicate_timestamps"][265598]["duplicates"]

    def test_intra_session_gap_is_reported_and_kept_not_dropped(self):
        idx = grid(FULL_DAY, 30)
        drop = idx[[5, 6, 7]]                       # a 3-bar halt
        df = make_frame(idx).drop(drop)
        q = qualify({265598: df})
        gaps = q.finding("intra_session_gaps")
        assert gaps.required is False               # a genuine halt is informational
        assert gaps.passed is False                 # ...but still reported
        spans = q.reports["intra_session_gaps"][265598]["gap_spans"]
        assert any(s["count"] == 3 for s in spans)
        # surrounding bars kept, gap NOT backfilled
        assert len(q.qualified_bars[265598]) == len(idx) - 3
        assert idx[4] in q.qualified_bars[265598].index
        assert idx[8] in q.qualified_bars[265598].index


# --------------------------------------------------------------------------- #
# Corrupt / non-finite OHLCV
# --------------------------------------------------------------------------- #
class TestCorrupt:
    def _one(self, mutate):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        mutate(df, idx)
        return qualify({265598: df}), idx

    def test_nan_close_is_corrupt_and_kept(self):
        q, idx = self._one(lambda df, i: df.__setitem__("close", df["close"].mask(df.index == i[4], np.nan)))
        c = q.finding("corrupt_ohlcv")
        assert c.passed is False and c.required is True
        assert q.passed is False
        assert len(q.qualified_bars[265598]) == len(idx)   # not dropped

    def test_infinite_open_is_corrupt(self):
        q, _ = self._one(lambda df, i: df.__setitem__("open", df["open"].mask(df.index == i[2], np.inf)))
        assert q.finding("corrupt_ohlcv").passed is False

    def test_non_positive_price_is_corrupt(self):
        q, _ = self._one(lambda df, i: df.__setitem__("low", df["low"].mask(df.index == i[1], 0.0)))
        assert q.finding("corrupt_ohlcv").passed is False

    def test_high_below_low_is_corrupt(self):
        def mut(df, i):
            df.loc[df.index == i[3], "high"] = 90.0
            df.loc[df.index == i[3], "low"] = 110.0
        q, _ = self._one(mut)
        assert q.finding("corrupt_ohlcv").passed is False

    def test_negative_volume_is_corrupt_but_zero_volume_is_fine(self):
        q_neg, _ = self._one(lambda df, i: df.__setitem__("volume", df["volume"].mask(df.index == i[0], -5)))
        assert q_neg.finding("corrupt_ohlcv").passed is False
        q_zero, _ = self._one(lambda df, i: df.__setitem__("volume", df["volume"].mask(df.index == i[0], 0)))
        assert q_zero.finding("corrupt_ohlcv").passed is True


# --------------------------------------------------------------------------- #
# Split discontinuities
# --------------------------------------------------------------------------- #
def daily_frame(dates, closes):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates])
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )
    df.index.name = "date"
    return df


class TestSplitDiscontinuity:
    def test_overnight_halving_flags_suspected_split(self):
        df = daily_frame(["2024-03-11", "2024-03-12"], [100.0, 50.0])
        q = qualify({265598: df}, interval="1 day")
        split = q.finding("split_discontinuity")
        assert split.required is False
        assert split.passed is False
        recs = q.reports["split_discontinuity"][265598]
        assert any(abs(r["ratio"] - 0.5) < 1e-9 for r in recs)
        # a suspected split is informational: it must NOT make the dataset ineligible
        assert q.passed is True

    def test_normal_overnight_gap_is_not_a_split(self):
        df = daily_frame(["2024-03-11", "2024-03-12"], [100.0, 108.0])
        q = qualify({265598: df}, interval="1 day")
        assert q.finding("split_discontinuity").passed is True

    def test_intraday_crash_is_not_flagged_as_split(self):
        # A 45% drop between two consecutive minute bars in the SAME session is
        # a genuine crash, kept and not mislabeled a split (splits are overnight).
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        df.loc[df.index == idx[6], "close"] = 55.0   # intraday crash
        df.loc[df.index == idx[6], "low"] = 54.0
        q = qualify({265598: df})
        assert q.finding("split_discontinuity").passed is True


# --------------------------------------------------------------------------- #
# Verified corrections
# --------------------------------------------------------------------------- #
def correction(idx, i, field, orig, repl, **over):
    kw = dict(
        conid=265598,
        timestamp=idx[i].to_pydatetime(),
        field=field,
        original_value=orig,
        replacement_value=repl,
        source="vendor_notice",
        reason="verified corrupt tick",
        reviewer="analyst-1",
    )
    kw.update(over)
    return DatasetCorrection(**kw)


class TestVerifiedCorrections:
    def test_fully_attributed_correction_fixes_corrupt_bar(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        df.loc[df.index == idx[3], "close"] = -1.0     # corrupt
        corr = correction(idx, 3, "close", "-1.0", "100.0")
        q = qualify({265598: df}, corrections=(corr,))
        assert q.finding("corrupt_ohlcv").passed is True       # correction cleared it
        assert q.finding("corrections_valid").passed is True
        assert q.qualified_bars[265598].loc[idx[3], "close"] == 100.0
        applied = [r for r in q.corrections if r.applied]
        assert len(applied) == 1
        # original value stays auditable
        assert applied[0].correction.original_value == "-1.0"

    def test_correction_missing_reviewer_is_rejected(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        corr = correction(idx, 3, "close", "100.0", "100.5", reviewer="")
        q = qualify({265598: df}, corrections=(corr,))
        cv = q.finding("corrections_valid")
        assert cv.passed is False and cv.required is True       # fail loudly
        assert q.qualified_bars[265598].loc[idx[3], "close"] == 100.0  # unchanged
        rej = [r for r in q.corrections if not r.applied]
        assert len(rej) == 1 and "reviewer" in rej[0].rejected_reason.lower()

    def test_correction_missing_source_or_reason_is_rejected(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        for over in ({"source": ""}, {"reason": "  "}, {"replacement_value": ""}):
            corr = correction(idx, 3, "close", "100.0", "100.5", **over)
            q = qualify({265598: df}, corrections=(corr,))
            assert q.finding("corrections_valid").passed is False
            assert q.qualified_bars[265598].loc[idx[3], "close"] == 100.0

    def test_correction_original_value_mismatch_is_rejected(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)                            # actual close == 100.0
        corr = correction(idx, 3, "close", "150.0", "100.5")   # claims wrong original
        q = qualify({265598: df}, corrections=(corr,))
        assert q.finding("corrections_valid").passed is False
        assert q.qualified_bars[265598].loc[idx[3], "close"] == 100.0
        rej = [r for r in q.corrections if not r.applied][0]
        assert "mismatch" in rej.rejected_reason.lower()

    def test_correction_for_unknown_bar_is_rejected(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx)
        ghost = (idx[0] + pd.Timedelta(seconds=1)).to_pydatetime()
        corr = correction(idx, 0, "close", "100.0", "100.5")
        corr = DatasetCorrection(**{**corr.__dict__, "timestamp": ghost})
        q = qualify({265598: df}, corrections=(corr,))
        assert q.finding("corrections_valid").passed is False
        assert all(not r.applied for r in q.corrections)


# --------------------------------------------------------------------------- #
# Determinism, fail-closed, input validation, multi-instrument
# --------------------------------------------------------------------------- #
class TestDeterminismAndFailClosed:
    def test_identical_inputs_produce_identical_findings_and_reports(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx).drop(idx[5])
        df.loc[df.index == idx[2], "close"] = np.nan
        q1 = qualify({265598: df.copy()})
        q2 = qualify({265598: df.copy()})
        assert q1.findings == q2.findings
        assert q1.reports == q2.reports

    def test_fail_closed_keeps_every_input_row(self):
        idx = grid(FULL_DAY, 30)
        df = make_frame(idx).drop(idx[[5, 6]])           # caller-omitted bars
        df.loc[df.index == idx[2], "close"] = -1.0       # corrupt (must be kept)
        q = qualify({265598: df})
        assert q.passed is False
        assert len(q.qualified_bars[265598]) == len(df)  # nothing dropped or added

    def test_multi_instrument_one_bad_fails_whole_dataset(self):
        idx = grid(FULL_DAY, 30)
        good = make_frame(idx)
        bad = make_frame(idx).drop(idx[4])
        q = qualify({265598: good, 272093: bad})
        assert q.passed is False                          # fail closed on the bad one
        assert len(q.qualified_bars[265598]) == len(idx)  # good instrument untouched

    def test_naive_index_raises(self):
        idx = grid(FULL_DAY, 30).tz_localize(None)
        with pytest.raises(ValueError, match="tz-aware|naive|UTC"):
            qualify({265598: make_frame(idx)})

    def test_missing_ohlcv_column_raises(self):
        df = make_frame(grid(FULL_DAY, 30)).drop(columns=["volume"])
        with pytest.raises(ValueError, match="volume|column"):
            qualify({265598: df})

    def test_unsupported_interval_raises(self):
        with pytest.raises(ValueError, match="interval"):
            qualify({265598: make_frame(grid(FULL_DAY, 30))}, interval="1 week")

    def test_empty_bars_raises(self):
        with pytest.raises(ValueError):
            qualify({})
