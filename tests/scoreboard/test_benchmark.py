import datetime as dt

import pandas as pd
import pytest

from tests.scoreboard.common import NOW
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data.duckdb_store import DuckDBDataStore
from trader.research.market_context import BENCHMARK_CONID
from trader.scoreboard.benchmark import BenchmarkBook, BenchmarkSourceError, read_spy_closes

CAL = XNYSCalendarPolicy()
FRI, SAT, MON, TUE, WED = (dt.date(2026, 10, d) for d in (9, 10, 12, 13, 14))
D, D2 = FRI, MON


class Source:
    def __init__(self, closes):
        self.closes = dict(closes)
        self.calls = []

    def __call__(self, start, end):
        self.calls.append((start, end))
        return {day: close for day, close in self.closes.items() if start <= day <= end}


@pytest.fixture
def source():
    return Source({FRI: 500.0, SAT: 1.0, MON: 510.0, WED: 520.0})


@pytest.fixture
def book(store, source):
    return BenchmarkBook(store, source, CAL, now=lambda: NOW)


def test_refresh_stores_one_row_per_session_and_skips_weekends(book):
    assert book.current_version() is None
    assert book.refresh(FRI, MON) == 2
    assert book.closes() == {FRI: 500.0, MON: 510.0} and book.current_version() == 1
    row = book.store.fetch("benchmark_prices", {})[0]
    assert (row["symbol"], row["conid"], row["provider"], row["bar_size"]) == ("SPY", BENCHMARK_CONID,
                                                                               "history_duckdb", "1 day")


def test_refresh_is_idempotent(book):
    book.refresh(FRI, MON)
    assert book.refresh(FRI, MON) == 0 and len(book.store.fetch("benchmark_prices", {})) == 2


def test_missing_spy_bar_is_unknown_not_forward_filled(book):            # no bar for Tue 10-13
    book.refresh(MON, WED)
    assert TUE not in book.closes() and book.closes()[WED] == 520.0


def test_data_refresh_never_changes_a_stored_price(book, source):
    book.refresh(D, D)
    source.closes[D] = 999.0
    book.refresh(D, D)
    assert book.closes()[D] == 500.0
    assert "BENCHMARK_SOURCE_DIFFERS" in {i["kind"] for i in book.store.incidents()}
    assert book.store.verify_seals() == []


def test_correction_is_a_new_version_and_old_rows_stay(book):
    book.refresh(D, D2)
    v = book.correct_benchmark(D, 501.0, "bad tick")
    assert v == 2 and book.closes(version=1)[D] == 500.0 and book.closes()[D] == 501.0 and book.closes()[D2] == 510.0
    assert book.store.verify_seals() == []


def test_correction_of_a_date_that_was_never_stored_is_refused(book):
    book.refresh(D, D)
    with pytest.raises(ValueError):
        book.correct_benchmark(TUE, 1.0, "x")
    with pytest.raises(ValueError):
        book.correct_benchmark(D, 0.0, "x")
    assert book.current_version() == 1


def _write_spy(path, stamps, closes):
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                          "volume": [1.0] * len(closes), "bar_size": ["1 day"] * len(closes)},
                         index=pd.DatetimeIndex(stamps, tz="UTC", name="date"))
    DuckDBDataStore(path).write(str(BENCHMARK_CONID), frame)


def test_read_spy_closes_by_utc_bar_date(tmp_path):
    path = str(tmp_path / "history.duckdb")
    _write_spy(path, ["2026-10-09", "2026-10-12"], [500.0, 510.0])
    assert read_spy_closes(path, FRI, WED) == {FRI: 500.0, MON: 510.0}


def test_read_spy_closes_without_any_bars_names_the_download_command(tmp_path):
    path = str(tmp_path / "history.duckdb")
    _write_spy(path, ["2026-01-02"], [1.0])
    with pytest.raises(BenchmarkSourceError, match="mmr data download SPY"):
        read_spy_closes(path, FRI, WED)


def test_read_spy_closes_refuses_two_bars_for_one_date(tmp_path):
    path = str(tmp_path / "history.duckdb")
    _write_spy(path, ["2026-10-09 00:00", "2026-10-09 04:00"], [500.0, 501.0])
    with pytest.raises(BenchmarkSourceError, match="more than one"):
        read_spy_closes(path, FRI, WED)
