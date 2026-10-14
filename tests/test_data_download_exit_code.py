"""Issue #177: `mmr data download` and `mmr data refresh` exit non-zero when a symbol or a job failed, and an
empty answer for completed XNYS sessions on a US REST source counts as a failure."""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from trader import mmr_cli
from trader.data.store import DateRange

WEDNESDAY = dt.date(2026, 10, 14)


@pytest.mark.parametrize("start,end,expected", [
    (dt.date(2026, 10, 10), dt.date(2026, 10, 11), []),                                    # Saturday, Sunday
    (dt.date(2026, 10, 9), dt.date(2026, 10, 13), [dt.date(2026, 10, 9), dt.date(2026, 10, 12),
                                                   dt.date(2026, 10, 13)]),
    (WEDNESDAY, WEDNESDAY, []),                    # today's session has not completed: its bars may come later
    (dt.date(2025, 12, 25), dt.date(2025, 12, 25), []),                                    # a market holiday
])
def test_completed_us_sessions_are_the_past_xnys_sessions_of_the_ranges(start, end, expected):
    assert mmr_cli._completed_us_sessions([DateRange(start=start, end=end)], today=WEDNESDAY) == expected


def _data_args(action, **extra):
    return argparse.Namespace(data_action=action, **extra)


@pytest.mark.parametrize("summary,exits", [
    ({"completed": 2, "skipped_up_to_date": 0, "failed": 0, "rows_written": 10, "refused_targets": []}, False),
    ({"completed": 1, "skipped_up_to_date": 0, "failed": 1, "rows_written": 5, "refused_targets": []}, True),
    ({"completed": 0, "skipped_up_to_date": 0, "failed": 2, "rows_written": 0, "error": "no key"}, True),
])
def test_download_exits_non_zero_when_a_symbol_failed(monkeypatch, summary, exits):
    monkeypatch.setattr(mmr_cli, "_handle_data_download", lambda args: summary)
    if exits:
        with pytest.raises(SystemExit) as stopped:
            mmr_cli._handle_data(_data_args("download"))
        assert stopped.value.code == 1
    else:
        mmr_cli._handle_data(_data_args("download"))


@pytest.mark.parametrize("ok,exits", [(True, False), (False, True)])
def test_refresh_exits_non_zero_when_a_job_failed(monkeypatch, ok, exits):
    monkeypatch.setattr(mmr_cli, "_handle_data_refresh", lambda args: ok)
    if exits:
        with pytest.raises(SystemExit) as stopped:
            mmr_cli._handle_data(_data_args("refresh"))
        assert stopped.value.code == 1
    else:
        mmr_cli._handle_data(_data_args("refresh"))


def test_refresh_with_an_unknown_job_reports_failure(monkeypatch):
    monkeypatch.setattr(mmr_cli, "_load_data_refresh_yaml", lambda: {"jobs": {"us_daily": {}}})
    assert mmr_cli._handle_data_refresh(argparse.Namespace(jobs=["nope"], refresh_all=False)) is False
