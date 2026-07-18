"""Dataset bar-quality qualification (P2 Task 3B, design §8.1).

A research dataset is eligible only when its regular-session bars are
exchange-calendar-complete and structurally sound. ``DatasetQualifier.qualify``
runs a fixed battery of checks over per-instrument OHLCV frames and returns a
``DatasetQualification`` carrying the ``QualityFinding`` list, per-check report
detail, the qualified (corrected) bars, and the correction audit trail.

Design invariants (this is a trading system -- wrong data is worse than none):

* **Nothing is silently dropped.** Genuine crashes, gaps, halts, and
  abnormal-volatility periods REMAIN in the qualified data; they are REPORTED
  (findings + reports), never removed. The only sanctioned mutation is a fully
  attributed verified correction.
* **Fail closed.** A structural/data-quality problem emits a REQUIRED
  ``QualityFinding(passed=False)`` -- the dataset is marked ineligible rather
  than quietly patched or a bad instrument skipped. Programmer errors (naive
  index, missing columns, unsupported interval, empty input) raise ``ValueError``
  (precision over convenience: surface the cause).
* **Deterministic.** Identical inputs produce byte-identical findings + reports;
  findings are emitted sorted by name and all report collections are sorted.
* **Calendar-driven.** Expected bars come from the real ``exchange_calendars``
  session schedule, so early closes (half-days) and DST are handled by the actual
  per-session open/close instants -- never a fixed session length.

A verified correction (``DatasetCorrection``) may alter a bar ONLY when it
carries original value, replacement, source, reason AND reviewer, the target bar
exists and is unambiguous, and the claimed original matches the actual value.
Any failing predicate rejects the correction (fail loudly) and the original
value stays auditable in the result.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from trader.research.dataset_manifest import DatasetCorrection, QualityFinding

_PRICE_COLUMNS = ("open", "high", "low", "close")
_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
_VALID_CONVENTIONS = ("bar_start", "bar_end")


# --------------------------------------------------------------------------- #
# Request / result dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatasetQualificationRequest:
    """Inputs to qualification.

    ``bars`` maps conid -> OHLCV ``DataFrame`` with a tz-aware UTC
    ``DatetimeIndex`` and at least ``open/high/low/close/volume`` columns.
    ``corrections`` are verified corrections (reusing ``DatasetCorrection``);
    only fully-attributed, value-matching ones are applied. When
    ``expected_start``/``expected_end`` are omitted the expected session window
    is inferred per-instrument from that instrument's own first/last bar dates.
    """

    bars: Mapping[int, pd.DataFrame]
    bar_interval: str
    calendar_name: str = "XNYS"
    corrections: tuple[DatasetCorrection, ...] = ()
    timestamp_convention: str = "bar_start"
    expected_start: Optional[dt.date] = None
    expected_end: Optional[dt.date] = None
    split_ratio_threshold: float = 1.5


@dataclass(frozen=True)
class DatasetCorrectionResult:
    """Audit record for one submitted correction: whether it was applied, and if
    not, exactly why it was rejected. The correction (with its original value)
    is retained regardless so nothing about the attempt is lost."""

    correction: DatasetCorrection
    applied: bool
    rejected_reason: str = ""


@dataclass(frozen=True)
class DatasetQualification:
    """Result of qualification.

    ``findings`` is the sorted ``QualityFinding`` battery (the same shape a
    ``DatasetManifest`` carries). ``reports`` holds deterministic per-check
    detail keyed by check name. ``qualified_bars`` is the corrected/qualified
    data (sorted, verified corrections applied, nothing dropped).
    ``corrections`` is the per-correction audit trail.
    """

    findings: tuple[QualityFinding, ...]
    reports: dict[str, Any]
    qualified_bars: dict[int, pd.DataFrame]
    corrections: tuple[DatasetCorrectionResult, ...] = ()

    @property
    def passed(self) -> bool:
        """Eligible iff every REQUIRED finding passed (mirrors
        ``DatasetManifest.research_eligible``); informational findings never
        change eligibility."""
        return all(f.passed for f in self.findings if f.required)

    # convenience alias
    eligible = passed

    def finding(self, name: str) -> Optional[QualityFinding]:
        for f in self.findings:
            if f.name == name:
                return f
        return None


# --------------------------------------------------------------------------- #
# Interval parsing
# --------------------------------------------------------------------------- #
def _parse_interval(interval: str) -> tuple[str, Optional[pd.Timedelta]]:
    """('intraday', step) for sub-daily bars, ('daily', None) for '1 day'.

    Raises ``ValueError`` for anything else (week/month/multi-day) -- fail
    loudly rather than half-qualify an unsupported granularity."""
    parts = str(interval).strip().lower().split()
    if len(parts) != 2:
        raise ValueError(f"unsupported bar_interval for qualification: {interval!r}")
    try:
        n = int(parts[0])
    except ValueError:
        raise ValueError(f"unsupported bar_interval for qualification: {interval!r}")
    unit = parts[1]
    if n <= 0:
        raise ValueError(f"unsupported bar_interval for qualification: {interval!r}")
    if unit in ("sec", "secs", "second", "seconds"):
        return "intraday", pd.Timedelta(seconds=n)
    if unit in ("min", "mins", "minute", "minutes"):
        return "intraday", pd.Timedelta(minutes=n)
    if unit in ("hour", "hours"):
        return "intraday", pd.Timedelta(hours=n)
    if unit in ("day", "days") and n == 1:
        return "daily", None
    raise ValueError(f"unsupported bar_interval for qualification: {interval!r}")


# --------------------------------------------------------------------------- #
# Expected-timestamp construction (calendar-driven)
# --------------------------------------------------------------------------- #
def _intraday_labels(open_ts: pd.Timestamp, close_ts: pd.Timestamp,
                     step: pd.Timedelta, convention: str) -> list[pd.Timestamp]:
    """Expected bar labels for one session. Walks the actual [open, close)
    instants, so early closes (shorter window) and DST (shifted window) fall out
    for free. A final partial bar (interval not dividing the session) is clamped
    to the close."""
    labels: list[pd.Timestamp] = []
    t = open_ts
    while t < close_ts:
        nxt = t + step
        labels.append(t if convention == "bar_start" else min(nxt, close_ts))
        t = nxt
    return labels


def _expected_labels(cal, mode: str, step: Optional[pd.Timedelta], convention: str,
                     start_date, end_date) -> list[pd.Timestamp]:
    sessions = cal.sessions_in_range(str(start_date), str(end_date))
    labels: list[pd.Timestamp] = []
    if mode == "daily":
        # one bar per session, labelled at the session date (midnight UTC).
        for s in sessions:
            labels.append(pd.Timestamp(s).tz_localize("UTC"))
        return labels
    for s in sessions:
        o = cal.session_open(s)
        c = cal.session_close(s)
        labels.extend(_intraday_labels(o, c, step, convention))
    return labels


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def _fmt(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def _is_blank(value: str) -> bool:
    return value is None or str(value).strip() == ""


def _finite(x: Any) -> bool:
    try:
        return bool(np.isfinite(float(x)))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Qualifier
# --------------------------------------------------------------------------- #
class DatasetQualifier:
    """Runs the §8.1 bar-quality battery over a request. Stateless: safe to
    reuse across datasets and threads."""

    def qualify(self, request: DatasetQualificationRequest) -> DatasetQualification:
        bars = request.bars
        if not bars:
            raise ValueError("no instruments to qualify: bars mapping is empty")

        mode, step = _parse_interval(request.bar_interval)
        convention = request.timestamp_convention
        if convention not in _VALID_CONVENTIONS:
            raise ValueError(
                f"unknown timestamp_convention {convention!r}; "
                f"expected one of {_VALID_CONVENTIONS}")
        cal = xcals.get_calendar(request.calendar_name)

        # 1. validate + canonicalise (sort, UTC index) -- copy so we never mutate
        #    the caller's frames; keep every row (fail-closed, no drops).
        qualified: dict[int, pd.DataFrame] = {}
        for conid in sorted(bars):
            frame = bars[conid]
            self._validate_frame(conid, frame)
            df = frame.copy()
            df.index = df.index.tz_convert("UTC")
            df = df.sort_index(kind="stable")
            qualified[conid] = df

        # 2. apply verified corrections (mutates qualified values only).
        correction_results = self._apply_corrections(qualified, request.corrections)

        reports: dict[str, Any] = {}

        # 3. structural + completeness checks over each instrument.
        completeness = self._check_completeness(
            qualified, cal, mode, step, convention,
            request.expected_start, request.expected_end, reports)
        within = self._check_within_session(reports)  # uses completeness scratch
        duplicates = self._check_duplicates(qualified, reports)
        gaps = self._check_gaps(reports)              # uses completeness scratch
        corrupt = self._check_corrupt(qualified, reports)
        splits = self._check_splits(qualified, request.split_ratio_threshold, reports)
        corrections_finding = self._corrections_finding(correction_results, reports)

        # scratch used to pass data between completeness/within/gaps
        reports.pop("_scratch", None)

        findings = tuple(sorted(
            (completeness, within, duplicates, gaps, corrupt, splits,
             corrections_finding),
            key=lambda f: f.name))

        return DatasetQualification(
            findings=findings,
            reports=reports,
            qualified_bars=qualified,
            corrections=correction_results,
        )

    # -- validation --------------------------------------------------------- #
    @staticmethod
    def _validate_frame(conid: int, frame: pd.DataFrame) -> None:
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"conid {conid}: bars must be a pandas DataFrame")
        idx = frame.index
        if not isinstance(idx, pd.DatetimeIndex):
            raise ValueError(f"conid {conid}: index must be a DatetimeIndex")
        if idx.tz is None:
            raise ValueError(
                f"conid {conid}: index must be tz-aware UTC, got naive datetimes")
        missing = [c for c in _REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"conid {conid}: missing required column(s): {missing}")

    # -- corrections -------------------------------------------------------- #
    def _apply_corrections(self, qualified: dict[int, pd.DataFrame],
                           corrections) -> tuple[DatasetCorrectionResult, ...]:
        results: list[DatasetCorrectionResult] = []
        for corr in corrections:
            reason = self._reject_reason(qualified, corr)
            if reason:
                results.append(DatasetCorrectionResult(corr, applied=False,
                                                       rejected_reason=reason))
                continue
            df = qualified[corr.conid]
            ts = pd.Timestamp(corr.timestamp).tz_convert("UTC")
            mask = df.index == ts
            replacement = float(corr.replacement_value)
            df.loc[mask, corr.field] = replacement
            results.append(DatasetCorrectionResult(corr, applied=True))
        # deterministic order: sort by (conid, timestamp, field)
        results.sort(key=lambda r: (r.correction.conid,
                                    pd.Timestamp(r.correction.timestamp).value,
                                    r.correction.field))
        return tuple(results)

    def _reject_reason(self, qualified: dict[int, pd.DataFrame],
                       corr: DatasetCorrection) -> str:
        # every field must be present (fail loudly on an under-attributed edit)
        for name in ("field", "original_value", "replacement_value",
                     "source", "reason", "reviewer"):
            if _is_blank(getattr(corr, name)):
                return f"missing required field: {name}"
        if corr.conid not in qualified:
            return f"unknown instrument: conid {corr.conid}"
        df = qualified[corr.conid]
        if corr.field not in df.columns:
            return f"unknown field: {corr.field}"
        ts_raw = pd.Timestamp(corr.timestamp)
        if ts_raw.tz is None:
            return "correction timestamp is tz-naive"
        ts = ts_raw.tz_convert("UTC")
        mask = df.index == ts
        n = int(mask.sum())
        if n == 0:
            return f"no matching bar at {_fmt(ts)}"
        if n > 1:
            return f"ambiguous correction: {n} bars at {_fmt(ts)}"
        try:
            claimed = float(corr.original_value)
            replacement = float(corr.replacement_value)
        except (TypeError, ValueError):
            return f"non-numeric correction value(s) for field {corr.field}"
        if not math.isfinite(replacement):
            return "replacement value is non-finite"
        current = df.loc[mask, corr.field].iloc[0]
        cur = float(current)
        claim_nan = math.isnan(claimed)
        cur_nan = math.isnan(cur)
        if claim_nan or cur_nan:
            if claim_nan != cur_nan:
                return (f"original value mismatch: actual={current!r} "
                        f"claimed={corr.original_value!r}")
        elif not math.isclose(cur, claimed, rel_tol=1e-9, abs_tol=1e-9):
            return (f"original value mismatch: actual={current!r} "
                    f"claimed={corr.original_value!r}")
        return ""

    @staticmethod
    def _corrections_finding(results, reports) -> QualityFinding:
        reports["corrections"] = results
        rejected = [r for r in results if not r.applied]
        applied = [r for r in results if r.applied]
        passed = not rejected
        detail = f"{len(applied)} applied, {len(rejected)} rejected"
        if rejected:
            detail += "; " + "; ".join(
                f"conid {r.correction.conid} {r.correction.field}"
                f"@{_fmt(pd.Timestamp(r.correction.timestamp).tz_convert('UTC'))}: "
                f"{r.rejected_reason}" for r in rejected)
        return QualityFinding(name="corrections_valid", passed=passed,
                              required=True, detail=detail)

    # -- completeness / within-session / gaps ------------------------------- #
    def _check_completeness(self, qualified, cal, mode, step, convention,
                            exp_start, exp_end, reports) -> QualityFinding:
        comp_report: dict[int, dict] = {}
        scratch: dict[int, dict] = {}
        for conid, df in qualified.items():
            idx_utc = df.index
            actual_ns = {ts.value for ts in idx_utc}
            start = exp_start if exp_start is not None else idx_utc.min().date()
            end = exp_end if exp_end is not None else idx_utc.max().date()
            expected = _expected_labels(cal, mode, step, convention, start, end)
            expected_map = {ts.value: ts for ts in expected}
            expected_ns = set(expected_map)
            missing_ns = sorted(expected_ns - actual_ns)
            unexpected_ns = sorted(actual_ns - expected_ns)
            actual_map = {ts.value: ts for ts in idx_utc}
            missing = tuple(expected_map[v] for v in missing_ns)
            unexpected = tuple(actual_map[v] for v in unexpected_ns)
            comp_report[conid] = {
                "expected_count": len(expected_ns),
                "actual_count": len(actual_ns),
                "missing_count": len(missing),
                "missing": missing,
            }
            scratch[conid] = {
                "expected_sorted": [expected_map[v] for v in sorted(expected_ns)],
                "expected_ns": expected_ns,
                "actual_ns": actual_ns,
                "unexpected": unexpected,
            }
        reports["session_completeness"] = comp_report
        reports["_scratch"] = scratch
        total_missing = sum(r["missing_count"] for r in comp_report.values())
        passed = total_missing == 0
        detail = "; ".join(
            f"conid {c}: {comp_report[c]['missing_count']} missing "
            f"of {comp_report[c]['expected_count']} expected"
            for c in sorted(comp_report))
        return QualityFinding(name="session_completeness", passed=passed,
                              required=True, detail=detail)

    def _check_within_session(self, reports) -> QualityFinding:
        scratch = reports["_scratch"]
        wis_report: dict[int, dict] = {}
        for conid, sc in scratch.items():
            wis_report[conid] = {"unexpected": sc["unexpected"]}
        reports["timestamps_within_session"] = wis_report
        total = sum(len(r["unexpected"]) for r in wis_report.values())
        passed = total == 0
        detail = "; ".join(
            f"conid {c}: {len(wis_report[c]['unexpected'])} out-of-session"
            for c in sorted(wis_report))
        return QualityFinding(name="timestamps_within_session", passed=passed,
                              required=True, detail=detail)

    def _check_gaps(self, reports) -> QualityFinding:
        scratch = reports["_scratch"]
        gap_report: dict[int, dict] = {}
        total_spans = 0
        for conid, sc in scratch.items():
            actual_ns = sc["actual_ns"]
            spans = []
            run: list[pd.Timestamp] = []
            for ts in sc["expected_sorted"]:
                if ts.value not in actual_ns:
                    run.append(ts)
                elif run:
                    spans.append({"start": run[0], "end": run[-1], "count": len(run)})
                    run = []
            if run:
                spans.append({"start": run[0], "end": run[-1], "count": len(run)})
            gap_report[conid] = {"gap_spans": spans}
            total_spans += len(spans)
        reports["intra_session_gaps"] = gap_report
        passed = total_spans == 0
        detail = "; ".join(
            f"conid {c}: {len(gap_report[c]['gap_spans'])} gap span(s)"
            for c in sorted(gap_report))
        return QualityFinding(name="intra_session_gaps", passed=passed,
                              required=False, detail=detail)

    # -- duplicates --------------------------------------------------------- #
    @staticmethod
    def _check_duplicates(qualified, reports) -> QualityFinding:
        dup_report: dict[int, dict] = {}
        total = 0
        for conid, df in qualified.items():
            dup_mask = df.index.duplicated(keep=False)
            dups = sorted({ts for ts in df.index[dup_mask]}, key=lambda t: t.value)
            dup_report[conid] = {"duplicates": tuple(dups)}
            total += len(dups)
        reports["duplicate_timestamps"] = dup_report
        passed = total == 0
        detail = "; ".join(
            f"conid {c}: {len(dup_report[c]['duplicates'])} duplicated timestamp(s)"
            for c in sorted(dup_report))
        return QualityFinding(name="duplicate_timestamps", passed=passed,
                              required=True, detail=detail)

    # -- corrupt OHLCV ------------------------------------------------------ #
    @staticmethod
    def _check_corrupt(qualified, reports) -> QualityFinding:
        corrupt_report: dict[int, tuple] = {}
        total = 0
        for conid, df in qualified.items():
            records = []
            for ts, row in df.iterrows():
                o, h, l, c, v = (row["open"], row["high"], row["low"],
                                 row["close"], row["volume"])
                reasons = []
                for name, val in (("open", o), ("high", h), ("low", l), ("close", c)):
                    if not _finite(val):
                        reasons.append(f"{name} non-finite")
                    elif float(val) <= 0:
                        reasons.append(f"{name} non-positive")
                if _finite(h) and _finite(l) and float(h) < float(l):
                    reasons.append("high<low")
                if all(_finite(x) for x in (o, h, l, c)):
                    o, h, l, c = float(o), float(h), float(l), float(c)
                    if h < max(o, c):
                        reasons.append("high<max(open,close)")
                    if l > min(o, c):
                        reasons.append("low>min(open,close)")
                if not _finite(v):
                    reasons.append("volume non-finite")
                elif float(v) < 0:
                    reasons.append("volume negative")
                if reasons:
                    records.append({"timestamp": ts, "reasons": tuple(reasons)})
            records.sort(key=lambda r: r["timestamp"].value)
            corrupt_report[conid] = tuple(records)
            total += len(records)
        reports["corrupt_ohlcv"] = corrupt_report
        passed = total == 0
        detail = "; ".join(
            f"conid {c}: {len(corrupt_report[c])} corrupt bar(s)"
            for c in sorted(corrupt_report))
        return QualityFinding(name="corrupt_ohlcv", passed=passed,
                              required=True, detail=detail)

    # -- split discontinuities --------------------------------------------- #
    @staticmethod
    def _check_splits(qualified, threshold, reports) -> QualityFinding:
        split_report: dict[int, tuple] = {}
        total = 0
        lo = 1.0 / threshold
        for conid, df in qualified.items():
            records = []
            prev_ts = prev_close = None
            for ts, row in df.iterrows():
                close = row["close"]
                if (prev_close is not None and _finite(close) and _finite(prev_close)
                        and float(prev_close) > 0 and float(close) > 0):
                    # only an OVERNIGHT (session-boundary) jump can be a split;
                    # an intraday move is a genuine crash and is left alone.
                    if ts.normalize() != prev_ts.normalize():
                        r = float(close) / float(prev_close)
                        if r >= threshold or r <= lo:
                            records.append({
                                "prev_ts": prev_ts, "ts": ts,
                                "prev_close": float(prev_close),
                                "close": float(close), "ratio": r})
                prev_ts, prev_close = ts, close
            records.sort(key=lambda x: x["ts"].value)
            split_report[conid] = tuple(records)
            total += len(records)
        reports["split_discontinuity"] = split_report
        passed = total == 0
        detail = "; ".join(
            f"conid {c}: {len(split_report[c])} suspected split(s)"
            for c in sorted(split_report))
        return QualityFinding(name="split_discontinuity", passed=passed,
                              required=False, detail=detail)
