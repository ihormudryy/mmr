import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from trader.scoreboard.bar_sources import AlpacaBars, BarSourceError, default_bar_sources, frame_to_bars

UTC = dt.timezone.utc


def frame(tz):
    index = pd.DatetimeIndex(["2026-10-06 14:31", "2026-10-06 14:32"], tz=tz)
    return pd.DataFrame({"open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5], "close": [1.2, 2.2]}, index=index)


def test_frames_become_utc_bars_whatever_their_timezone():
    assert [b.start for b in frame_to_bars(frame("UTC"))] == [dt.datetime(2026, 10, 6, 14, 31, tzinfo=UTC),
                                                                dt.datetime(2026, 10, 6, 14, 32, tzinfo=UTC)]
    assert frame_to_bars(frame("US/Eastern"))[0].start == dt.datetime(2026, 10, 6, 18, 31, tzinfo=UTC)
    assert frame_to_bars(pd.DataFrame()) == [] and frame_to_bars(None) == []


class FakeProvider:
    def __init__(self, frame=None, error=None):
        self.frame, self.error, self.asked = frame, error, []

    def get_history(self, ticker, bar_size, start, end, timezone="US/Eastern"):
        self.asked.append(ticker)
        if self.error:
            raise self.error
        return self.frame


def security(**changes):
    values = dict(symbol="AAPL", secType="STK", currency="USD")
    values.update(changes)
    return SimpleNamespace(**values)


START, END = dt.datetime(2026, 10, 6, 14, 31, tzinfo=UTC), dt.datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def test_alpaca_bars_are_asked_by_the_resolved_ticker_and_filtered_to_the_window():
    provider = FakeProvider(frame("UTC"))
    bars = AlpacaBars(provider, lambda conid: security()).bars(265598, START, END)
    assert provider.asked == ["AAPL"] and len(bars) == 2


@pytest.mark.parametrize("found,code", [(None, "SYMBOL_UNRESOLVED"), (security(secType="OPT"), "UNSUPPORTED_INSTRUMENT"),
                                        (security(currency="CAD"), "UNSUPPORTED_INSTRUMENT")])
def test_an_inexact_instrument_is_refused_not_guessed(found, code):
    with pytest.raises(BarSourceError, match=code):
        AlpacaBars(FakeProvider(frame("UTC")), lambda conid: found).bars(1, START, END)


def test_a_provider_failure_keeps_only_the_error_class_name():
    provider = FakeProvider(error=RuntimeError("https://x/?key=SECRET"))
    with pytest.raises(BarSourceError) as exc:
        AlpacaBars(provider, lambda conid: security()).bars(1, START, END)
    assert str(exc.value) == "ALPACA_RuntimeError" and "SECRET" not in str(exc.value)


def test_blank_alpaca_keys_leave_only_the_local_source(caplog):
    trader = SimpleNamespace(history_duckdb_path="/x/h.duckdb", alpaca_api_key_id="", alpaca_api_secret_key="")
    assert [s.name for s in default_bar_sources(trader)] == ["history_duckdb"]
    assert "SECRET" not in caplog.text


def test_set_alpaca_keys_add_the_alpaca_source():
    trader = SimpleNamespace(history_duckdb_path="/x/h.duckdb", alpaca_api_key_id="id", alpaca_api_secret_key="s",
                             universe_accessor=SimpleNamespace(resolve_symbol=lambda conid, first_only: []))
    assert [s.name for s in default_bar_sources(trader)] == ["history_duckdb", "alpaca"]


def test_the_alpaca_client_is_built_only_when_bars_are_read(monkeypatch):
    # Startup builds no Alpaca client (a live account's command stack must not build one, #76).
    import trader.data_providers.alpaca.client as client_module
    built = []

    class RecordingClient:
        def __init__(self, key_id, secret_key, **kwargs):
            built.append(key_id)

        def paginate(self, path, params):
            return iter(())
    monkeypatch.setattr(client_module, "AlpacaClient", RecordingClient)
    trader = SimpleNamespace(history_duckdb_path="", alpaca_api_key_id="id", alpaca_api_secret_key="s",
                             universe_accessor=SimpleNamespace(resolve_symbol=lambda conid, first_only: [security()]))
    alpaca = default_bar_sources(trader)[1]
    assert built == []
    assert alpaca.bars(265598, START, END) == [] and built == ["id"]


def _local_history(tmp_path, bars_by_minute):
    from trader.data.data_access import TickStorage
    from trader.objects import BarSize
    path = str(tmp_path / "history.duckdb")
    index = pd.DatetimeIndex(bars_by_minute, tz="UTC").rename("date")
    frame = pd.DataFrame({"open": 100.0, "high": 100.3, "low": 99.7, "close": 100.0, "volume": 1000.0,
                          "bar_size": "1 min", "what_to_show": 1}, index=index)
    TickStorage(path).get_tickdata(BarSize.Mins1).write(265598, frame)
    return path


def test_local_rows_without_one_minute_provenance_are_ignored(tmp_path):        # review 4210055215
    import duckdb
    from trader.scoreboard.bar_sources import LocalHistoryBars
    path = _local_history(tmp_path, ["2026-10-06 14:31", "2026-10-06 14:32", "2026-10-06 14:33"])
    assert len(LocalHistoryBars(path).bars(265598, START, END)) == 3
    with duckdb.connect(path) as conn:
        conn.execute("UPDATE tick_data SET bar_size = NULL WHERE date > TIMESTAMPTZ '2026-10-06 14:31:30+00'")
    assert [b.start for b in LocalHistoryBars(path).bars(265598, START, END)] == [START]


@pytest.mark.parametrize("bad", [
    {"open": ["x", 2.0]},                                    # nonnumeric
    {"open": [None, 2.0]},                                   # null in an object column
])
def test_a_malformed_frame_is_a_short_source_error(bad):                         # review 4210055549
    broken = frame("UTC").astype(object)
    for column, values in bad.items():
        broken[column] = values
    with pytest.raises(BarSourceError) as exc:
        AlpacaBars(FakeProvider(broken), lambda conid: security()).bars(265598, START, END)
    assert str(exc.value) == "BAD_BAR"


@pytest.mark.parametrize("what_to_show", ["2", "NULL", "4"])                     # MIDPOINT, unknown, ASK
def test_local_rows_that_are_not_trade_prices_are_ignored(tmp_path, what_to_show):   # review 4210486721
    import duckdb
    from trader.scoreboard.bar_sources import LocalHistoryBars
    path = _local_history(tmp_path, ["2026-10-06 14:31", "2026-10-06 14:32", "2026-10-06 14:33"])
    with duckdb.connect(path) as conn:
        conn.execute(f"UPDATE tick_data SET what_to_show = {what_to_show} "
                     "WHERE date > TIMESTAMPTZ '2026-10-06 14:31:30+00'")
    assert [b.start for b in LocalHistoryBars(path).bars(265598, START, END)] == [START]


def test_both_sources_give_trade_price_bars_only():                              # review 4210486721
    from trader.objects import WhatToShow
    from trader.scoreboard.bar_sources import LocalHistoryBars
    assert LocalHistoryBars.what_to_show is WhatToShow.TRADES                   # filtered to TRADES rows
    assert AlpacaBars.what_to_show is WhatToShow.TRADES                         # Alpaca /bars are trade bars

