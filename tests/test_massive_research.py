import threading
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

from trader.tools.massive_research import MassiveResearch, ResearchResult, json_clean


def test_json_clean_converts_non_finite_values_recursively():
    assert json_clean({"a": float("nan"), "b": [float("inf"), 3]}) == {
        "a": None,
        "b": [None, 3],
    }


def test_json_clean_converts_pandas_missing_scalars_to_null():
    cleaned = json_clean({
        "nat": pd.NaT,
        "na": pd.NA,
        "numpy_nat": np.datetime64("NaT"),
        "numpy_nan": np.float64("nan"),
    })

    assert cleaned == {
        "nat": None,
        "na": None,
        "numpy_nat": None,
        "numpy_nan": None,
    }


def test_presets_are_cli_shaped_rows():
    result = MassiveResearch(object()).presets()

    assert isinstance(result, ResearchResult)
    assert result.title == "Idea Scanner Presets"
    assert {row["preset"] for row in result.data} >= {"momentum", "gap-up"}


def test_snapshot_normalizes_massive_stock_snapshot():
    client = NS(get_snapshot_ticker=lambda **kwargs: NS(
        ticker="AAPL", todays_change=2.5, todays_change_percent=1.25,
        day=NS(open=198.0, high=202.0, low=197.5, close=201.0,
               volume=1_000_000, vwap=200.0),
        prev_day=NS(close=198.5, volume=900_000),
        last_trade=NS(price=201.1, size=20, timestamp=1_753_188_000_000_000_000),
        last_quote=NS(
            bid_price=201.0, ask_price=201.2, bid_size=10, ask_size=12),
    ))

    result = MassiveResearch(client).snapshot("aapl")

    assert result.data["ticker"] == "AAPL"
    assert result.data["bid"] == 201.0
    assert result.data["ask"] == 201.2
    assert result.data["day"]["volume"] == 1_000_000


def test_news_normalizes_polygon_and_benzinga_rows():
    article = NS(published_utc="2026-07-22T10:00:00Z", title="Earnings",
                 tickers=["AAPL"], insights=[NS(sentiment="positive")],
                 author="Reporter", article_url="https://example.test/a")
    client = NS(list_ticker_news=lambda **kwargs: [article])

    rows = MassiveResearch(client).news("AAPL", limit=10, source="polygon").data

    assert rows == [{
        "published": "2026-07-22T10:00:00Z", "title": "Earnings",
        "tickers": ["AAPL"], "sentiment": "positive", "author": "Reporter",
        "url": "https://example.test/a", "teaser": "",
    }]


def test_empty_provider_iterators_return_empty_data():
    client = NS(
        get_snapshot_direction=lambda **kwargs: [],
        list_ticker_news=lambda **kwargs: [],
        list_benzinga_news=lambda **kwargs: [],
    )
    research = MassiveResearch(client)

    assert research.movers(market="stocks", direction="gainers", limit=10).data == []
    assert research.news("AAPL", limit=10, source="polygon").data == []
    assert research.news("AAPL", limit=10, source="benzinga").data == []


def test_ideas_delegates_all_scan_arguments(monkeypatch):
    captured = {}

    class Scanner:
        def __init__(self, client):
            captured["client"] = client

        def scan(self, **kwargs):
            captured.update(kwargs)
            return pd.DataFrame([{"ticker": "AAPL", "score": 80.0}])

    monkeypatch.setattr("trader.tools.massive_research.IdeaScanner", Scanner)
    client = object()
    result = MassiveResearch(client).ideas(
        preset="momentum", source="tickers", tickers=["AAPL"],
        universe_symbols=None, top_n=5, custom_filters={"min_price": 10},
        fundamentals=True, news=True, names=True)

    assert result.data[0]["ticker"] == "AAPL"
    assert captured == {
        "client": client, "preset": "momentum", "source": "tickers",
        "tickers": ["AAPL"], "universe_symbols": None, "top_n": 5,
        "custom_filters": {"min_price": 10}, "fundamentals": True,
        "news": True, "names": True,
    }


def test_movers_applies_limit_and_direction():
    snapshots = [
        NS(ticker="AAPL", todays_change=2.5, todays_change_percent=1.25,
           day=NS(open=198.0, close=201.0, volume=1_000_000)),
        NS(ticker="MSFT", todays_change=1.5, todays_change_percent=0.75,
           day=NS(open=499.0, close=501.0, volume=2_000_000)),
    ]
    calls = []
    client = NS(get_snapshot_direction=lambda **kwargs: calls.append(kwargs) or snapshots)

    result = MassiveResearch(client).movers(
        market="stocks", direction="gainers", limit=1)

    assert calls == [{"market_type": "stocks", "direction": "gainers"}]
    assert result.data == [{
        "ticker": "AAPL", "open": 198.0, "close": 201.0,
        "volume": 1_000_000, "change": 2.5, "change_pct": 1.25,
        "market": "stocks",
    }]


def test_detail_subfetch_failure_keeps_base_row():
    snapshot = NS(ticker="AAPL", todays_change=2.5, todays_change_percent=1.25,
                  day=NS(open=198.0, close=201.0, volume=1_000_000))
    client = NS(
        get_snapshot_direction=lambda **kwargs: [snapshot],
        get_ticker_details=lambda ticker: (_ for _ in ()).throw(RuntimeError("nope")),
        list_financials_ratios=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("nope")),
        list_ticker_news=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    result = MassiveResearch(client).movers(
        market="stocks", direction="gainers", limit=1, detail=True)

    assert result.data == [{
        "ticker": "AAPL", "open": 198.0, "close": 201.0,
        "volume": 1_000_000, "change": 2.5, "change_pct": 1.25,
        "market": "stocks", "details": {}, "ratios": {}, "news": {},
    }]


def test_detail_enrichment_runs_concurrently_and_bounded_at_limit_100():
    snapshots = [
        NS(
            ticker=f"T{index:03}",
            todays_change=1.0,
            todays_change_percent=1.0,
            day=NS(open=10.0, close=11.0, volume=1_000),
        )
        for index in range(100)
    ]
    first_pair = threading.Barrier(2)
    calls_lock = threading.Lock()
    detail_calls = 0
    detail_threads: set[str] = set()

    def get_details(ticker):
        nonlocal detail_calls
        with calls_lock:
            detail_calls += 1
            call_number = detail_calls
            detail_threads.add(threading.current_thread().name)
        if call_number <= 2:
            first_pair.wait(timeout=1)
        return NS(name=f"Company {ticker}", market_cap=1_000)

    client = NS(
        get_snapshot_direction=lambda **kwargs: snapshots,
        get_ticker_details=get_details,
        list_financials_ratios=lambda **kwargs: [],
        list_ticker_news=lambda **kwargs: [],
    )

    result = MassiveResearch(client).movers(
        market="stocks", direction="gainers", limit=100, detail=True)

    assert len(result.data) == 100
    assert all(row["details"]["name"] == f"Company {row['ticker']}"
               for row in result.data)
    assert 2 <= len(detail_threads) <= 8


def test_benzinga_uses_published_url_and_teaser():
    article = NS(published="2026-07-22T10:00:00Z", title="Earnings",
                 tickers=["AAPL"], author="Reporter", url="https://example.test/a",
                 teaser="Quarterly results")
    client = NS(list_benzinga_news=lambda **kwargs: [article])

    result = MassiveResearch(client).news("aapl", limit=1, source="benzinga")

    assert result.data == [{
        "published": "2026-07-22T10:00:00Z", "title": "Earnings",
        "tickers": ["AAPL"], "sentiment": "", "author": "Reporter",
        "url": "https://example.test/a", "teaser": "Quarterly results",
    }]


def test_ideas_falls_back_to_twelvedata_on_massive_entitlement(monkeypatch):
    class MassiveScanner:
        def __init__(self, client):
            pass

        def scan(self, **kwargs):
            raise RuntimeError('NOT_AUTHORIZED not entitled')

    class TdScanner:
        def __init__(self, client):
            assert client == "td"

        def scan(self, **kwargs):
            frame = pd.DataFrame([{"ticker": "AAPL", "score": 1.0}])
            frame.attrs = {}
            return frame

    monkeypatch.setattr("trader.tools.massive_research.IdeaScanner", MassiveScanner)
    monkeypatch.setattr(
        "trader.tools.massive_research.TwelveDataIdeaScanner", TdScanner)

    result = MassiveResearch(object(), td_client="td").ideas(
        preset="momentum", source="movers", tickers=None,
        universe_symbols=None, top_n=5, custom_filters=None,
        fundamentals=False, news=False, names=False)

    assert result.provider == "twelvedata"
    assert result.data[0]["ticker"] == "AAPL"
    assert "Starter+" in (result.notice or "")


def test_snapshot_falls_back_to_twelvedata_on_massive_entitlement():
    client = NS(get_snapshot_ticker=lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError('{"status":"NOT_AUTHORIZED","message":"not entitled"}')))
    td = NS(quote=lambda symbol: NS(as_json=lambda: {
        "symbol": "AAPL", "open": 100, "high": 110, "low": 99, "close": 105,
        "volume": 1_000, "previous_close": 104, "change": 1, "percent_change": 0.96,
    }))

    result = MassiveResearch(client, td_client=td).snapshot("aapl")

    assert result.provider == "twelvedata"
    assert result.data["ticker"] == "AAPL"
    assert result.data["last"] == 105.0
    assert result.data["bid"] is None
