"""The shipped research refresh (config_defaults) covers what an SP2c evaluation reads (issue #111)."""
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import exchange_calendars as xcals
import yaml

from trader.research.cohort import evaluation_period
from trader.research.market_context import SPY_LOOKBACK_SESSIONS
from trader.research.service_config import ResearchServiceConfig

ROOT = Path(__file__).resolve().parents[2]
INTRADAY_JOBS = {"1 min": "research_1min", "5 mins": "research_5mins", "15 mins": "research_15mins"}
DAILY_JOB = "research_daily"


def _yaml(name):
    return yaml.safe_load((ROOT / "config_defaults" / name).read_text())


def _longest_spans(first_day: dt.date, last_day: dt.date) -> tuple[int, int]:
    """Calendar days from a research day back to its period start, and back to SPY's lookback start."""
    calendar = xcals.get_calendar("XNYS")
    config = SimpleNamespace(period_sessions=ResearchServiceConfig().period_sessions)
    period, benchmark = 0, 0
    day = first_day
    while day <= last_day:
        start, _ = evaluation_period(day, config)
        spy_start = calendar.session_offset(start, -SPY_LOOKBACK_SESSIONS).date()
        period, benchmark = max(period, (day - start).days), max(benchmark, (day - spy_start).days)
        day += dt.timedelta(days=1)
    return period, benchmark


def test_every_research_bar_size_has_a_refresh_job_over_the_research_universe():
    jobs = _yaml("data_refresh.yaml")["jobs"]
    for bar_size in _yaml("ai.yaml")["research"]["bar_sizes"]:
        job = jobs[INTRADAY_JOBS[bar_size]]
        assert (job["universe"], job["bar_size"], job["source"]) == ("research", bar_size, "alpaca")
    assert (jobs[DAILY_JOB]["universe"], jobs[DAILY_JOB]["bar_size"]) == ("research", "1 day")


def test_the_refresh_windows_cover_the_evaluation_period_and_the_spy_lookback():
    period, benchmark = _longest_spans(dt.date(2025, 1, 1), dt.date(2026, 12, 31))
    jobs = _yaml("data_refresh.yaml")["jobs"]
    # the refresh counts its days back from the moment it runs, so a strict margin is needed
    assert all(jobs[name]["days"] > period for name in INTRADAY_JOBS.values())
    assert jobs[DAILY_JOB]["days"] > benchmark


def test_both_schedulers_run_every_research_job_after_the_us_refresh():
    research_jobs = sorted([DAILY_JOB, *INTRADAY_JOBS.values()])
    for name in ("pycron.yaml", "no_docker_pycron.yaml"):
        entries = {job["name"]: job for job in _yaml(name)["jobs"]}
        entry = entries["data_refresh_research"]
        assert sorted(entry["arguments"].split("data refresh ")[1].split()) == research_jobs
        assert entry["start"] == "15 21 * * 1-5" and entries["data_refresh_us"]["start"] == "30 20 * * 1-5"
