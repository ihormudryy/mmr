"""Bars for an evaluation: load, qualify, and seal into a dataset manifest.

A conid without bars, or any failed required quality finding, stops the
evaluation; nothing is dropped silently.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Optional, Sequence

import exchange_calendars as xcals
import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.market_data import normalize_historical
from trader.data.store import DateRange
from trader.objects import BarSize
from trader.research.data_quality import DatasetQualificationRequest, DatasetQualifier
from trader.research.dataset_manifest import DatasetFile, DatasetManifest
from trader.research.market_context import BENCHMARK_CONID, SPY_LOOKBACK_SESSIONS

OHLCV = ['open', 'high', 'low', 'close', 'volume']


class EvaluationDataError(Exception):
    """The evaluation's bars are missing or failed qualification."""


def load_bars(history_db: str, conids: Sequence[int], bar_size: str,
              start: dt.datetime, end: dt.datetime) -> dict[int, pd.DataFrame]:
    tickdata = TickStorage(history_db).get_tickdata(BarSize.parse_str(bar_size))
    bars: dict[int, pd.DataFrame] = {}
    missing: list[int] = []
    for conid in conids:
        raw = tickdata.read(conid, date_range=DateRange(start=start, end=end))
        frame = None
        if raw is not None and len(raw) > 0:
            frame = normalize_historical(raw).dropna(subset=['close'])
        if frame is None or frame.empty:
            missing.append(int(conid))
            continue
        index = frame.index
        frame = frame[OHLCV].copy()
        frame.index = index.tz_localize('UTC') if index.tz is None else index.tz_convert('UTC')
        bars[int(conid)] = frame
    if missing:
        raise EvaluationDataError(
            f'no {bar_size} bars between {start:%Y-%m-%d} and {end:%Y-%m-%d} for conids {missing}; '
            f'download them before evaluating')
    return bars


def _utc_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)


def _day_end_utc(day: dt.date) -> dt.datetime:
    return _utc_midnight(day) + dt.timedelta(days=1)


def load_benchmark_closes(history_db: str, spec) -> pd.Series:
    """SPY daily closes from SPY_LOOKBACK_SESSIONS sessions before the period
    start through the period end, indexed by session date. Missing or short
    history stops the run with the exact download command."""
    calendar = xcals.get_calendar(spec.calendar)
    sessions_before = calendar.sessions_in_range(calendar.first_session, str(spec.period_start))
    if len(sessions_before) <= SPY_LOOKBACK_SESSIONS:
        raise EvaluationDataError(f'period start {spec.period_start} is too early for the calendar')
    required_start = sessions_before[-(SPY_LOOKBACK_SESSIONS + 1)].date()
    days_needed = (dt.date.today() - required_start).days + 5
    download = f'mmr data download SPY --bar-size "1 day" --days {days_needed}'
    tickdata = TickStorage(history_db).get_tickdata(BarSize.parse_str('1 day'))
    raw = tickdata.read(BENCHMARK_CONID, date_range=DateRange(
        start=_utc_midnight(required_start), end=_day_end_utc(spec.period_end)))
    if raw is None or len(raw) == 0:
        raise EvaluationDataError(
            f'no SPY (conid {BENCHMARK_CONID}) daily bars in the history DB; run: {download}')
    frame = normalize_historical(raw).dropna(subset=['close'])
    closes = frame['close'].astype(float)
    stamps = frame.index.tz_localize('UTC') if frame.index.tz is None else frame.index.tz_convert('UTC')
    closes.index = pd.Index([ts.date() for ts in stamps])
    duplicated_days = sorted(set(closes.index[closes.index.duplicated()]))
    if duplicated_days:
        raise EvaluationDataError(
            f'SPY (conid {BENCHMARK_CONID}) has more than one daily bar for session(s) '
            f'{", ".join(day.isoformat() for day in duplicated_days)}; '
            f're-download the history: {download}')
    closes = closes.sort_index()
    before = closes[closes.index < spec.period_start]
    if len(before) < SPY_LOOKBACK_SESSIONS:
        raise EvaluationDataError(
            f'SPY daily history starts too late: need {SPY_LOOKBACK_SESSIONS} sessions before '
            f'{spec.period_start} (from {required_start}), have {len(before)}; run: {download}')
    return closes


def _benchmark_frame(closes: pd.Series) -> pd.DataFrame:
    """Closes-only OHLCV for the qualifier: the evaluator uses only closes, and
    flat bars cannot trip the OHLC consistency checks."""
    index = pd.DatetimeIndex([pd.Timestamp(day, tz='UTC') for day in closes.index])
    values = closes.to_numpy()
    return pd.DataFrame({'open': values, 'high': values, 'low': values,
                         'close': values, 'volume': 0.0}, index=index)


def _qualify_benchmark(closes: pd.Series, spec) -> None:
    qualification = DatasetQualifier().qualify(DatasetQualificationRequest(
        bars={BENCHMARK_CONID: _benchmark_frame(closes)}, bar_interval='1 day',
        calendar_name=spec.calendar, expected_start=closes.index[0],
        expected_end=spec.period_end))
    failed = [f for f in qualification.findings if f.required and not f.passed]
    if failed:
        raise EvaluationDataError('benchmark dataset failed qualification: ' + '; '.join(
            f'{f.name}: {f.detail}' for f in failed))


def qualify_dataset(bars: dict[int, pd.DataFrame], spec,
                    benchmark_closes: Optional[pd.Series] = None) -> DatasetManifest:
    qualification = DatasetQualifier().qualify(DatasetQualificationRequest(
        bars=bars, bar_interval=spec.bar_size, calendar_name=spec.calendar,
        expected_start=spec.period_start, expected_end=spec.period_end))
    failed = [f for f in qualification.findings if f.required and not f.passed]
    if failed:
        raise EvaluationDataError('dataset failed qualification: ' + '; '.join(
            f'{f.name}: {f.detail}' for f in failed))
    files = tuple(
        DatasetFile(path=f'tick_data/{conid}/{spec.bar_size}',
                    sha256=hashlib.sha256(frame.to_csv().encode('utf-8')).hexdigest(),
                    rows=len(frame))
        for conid, frame in sorted(qualification.qualified_bars.items()))
    if benchmark_closes is not None:
        _qualify_benchmark(benchmark_closes, spec)
        files += (DatasetFile(
            path=f'tick_data/{BENCHMARK_CONID}/1 day',
            sha256=hashlib.sha256(benchmark_closes.to_csv().encode('utf-8')).hexdigest(),
            rows=len(benchmark_closes)),)
    as_of = max(frame.index.max() for frame in bars.values()).to_pydatetime()
    return DatasetManifest(
        vendor='mmr_history', retrieval_timestamp=as_of, bar_interval=spec.bar_size,
        timestamp_convention='bar_start', session_calendar=spec.calendar,
        calendar_version=xcals.__version__, adjustment_policy='as_stored',
        start_boundary=_utc_midnight(spec.period_start),
        end_boundary=_utc_midnight(spec.period_end),
        spread_source='estimated:tick_table', instruments=tuple(sorted(spec.conids)),
        files=files, findings=qualification.findings)
