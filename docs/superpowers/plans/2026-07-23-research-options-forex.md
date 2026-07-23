# Research Options + Forex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the two inert "Later" Research-tab tools — **Options** and **Forex** — as read-only research tools, Massive-backed with an IB-forex typed path, reusing the existing `ResearchService` envelope.

**Architecture:** Extend `MassiveResearch` with options/forex methods returning the existing `ResearchResult` shape (options chain/snapshot logic extracted into a shared `trader/tools/options_data.py` so the CLI and tab can't drift). `ResearchService.run` gains a `backend` selector: `"massive"` (inject the provider, as today) or `"trader"` (run a closure that calls the dashboard's existing typed query link `center._query_client` — used only for IB forex snapshot/quote). New validated `GET /api/research/options/*` and `/forex/*` routes. Frontend flips the two rail buttons live and adds their control panes, reusing the generic result/detail renderers.

**Tech Stack:** Python 3.12, FastAPI, pytest (`TestClient` + `@pytest.mark.asyncio`), Massive REST client, TwelveData client, typed HMAC RPC (`42101`), vanilla JS tested under Node via `web/static/research_test_harness.js`.

## Global Constraints

- **Read-only.** No order entry (no options buy/sell, no forex trading) on any new path. Do not add a Propose affordance for options/forex — `equityResult()` (`command_center_research.js:505-521`) already suppresses Propose for non-equities; leave it.
- **No legacy RPC (`42001`), no CLI subprocess** on any research path. IB forex uses the typed query link only.
- **Uniform envelope:** success `{"data", "title", "meta":{"tool","provider","observed_at","notice"}}`; error `{"error":{"code","message","retryable"}}` at the `ResearchError.status`.
- **No fabricated data:** greeks/IV only when the Massive payload carries them; TwelveData quote emits a `notice` that bid/ask are unavailable; IB path down → `TRADER_LINK_UNAVAILABLE`, never a synthesized quote.
- **conId wire key is `instrument_id`** in typed `discover_instrument` / `get_snapshot` payloads (not `conId`).
- **`ResearchResult` shape:** `ResearchResult(data, title, provider="massive", notice=None)` (positional `data, title`).
- **Massive option ticker format:** `O:<SYMBOL><YYMMDD><C|P><strike*1000, 8 digits>` e.g. `O:AAPL260320C00250000`.
- Every new tool needs a per-tool timeout key in `DEFAULT_TIMEOUTS` (`web/command_center/research.py`).
- Spec: `docs/superpowers/specs/2026-07-23-research-options-forex-design.md`.

## File map

| File | Responsibility |
|------|----------------|
| Create `trader/tools/options_data.py` | Client-taking option helpers: `parse_option_ticker`, `build_option_ticker`, `chain_records`, `contract_snapshot`. Single source of truth for chain/contract normalization. |
| Create `tests/test_options_data.py` | Unit tests for the option helpers (fake Massive client via `SimpleNamespace`). |
| Modify `trader/tools/massive_research.py` | Add `options_expirations/options_chain/options_snapshot/options_implied` and `forex_snapshot/forex_quote/forex_movers/forex_snapshot_all/forex_convert` to `MassiveResearch`; store `api_key` for the chain.py-backed calls. |
| Modify `tests/test_massive_research.py` | Provider-method unit tests (inline `NS` client doubles). |
| Modify `web/command_center/research.py` | `run(..., backend="massive")` selector; `TRADER_LINK_UNAVAILABLE`; new `DEFAULT_TIMEOUTS` keys. |
| Modify `tests/test_dashboard_research_service.py` | `backend="trader"` path + `TRADER_LINK_UNAVAILABLE` + new timeout keys. |
| Modify `web/command_center/routes_research.py` | New `options/*` and `forex/*` routes; IB-forex closure using `cc._query_client`. |
| Modify `tests/test_dashboard_research_api.py` | Route validation/success/error tests; IB-forex via a fake `_query_client`; fix `data-research-later` count 4→2. |
| Modify `web/templates/_research_tab.html` | Promote Options + Forex out of the `{% for %}` "Later" loop into real buttons. |
| Modify `web/static/command_center_research.js` | `supportedTools`, `state`, `forms`, `run()` branches, options expiration loader + row click-through. |
| Modify `web/static/command_center_research.test.js` | JS behaviour tests for the two new tools. |
| Modify `web/static/research_test_harness.js:84` | Move `options`,`forex` into the enabled tool list. |
| Modify `trader/sdk.py` | (Task 7) Delegate `options_chain/options_snapshot` + `_parse/_build_massive_option_ticker` to the shared helpers (anti-drift). |
| Modify design spec + `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md` | (Task 7) one-line Phase-3 status bump. |

---

### Task 1: Shared option helpers (`trader/tools/options_data.py`)

**Files:**
- Create: `trader/tools/options_data.py`
- Test: `tests/test_options_data.py`

**Interfaces:**
- Produces:
  - `parse_option_ticker(ticker: str) -> dict` → `{"symbol","expiration","right","strike"}` (raises `ValueError` on unparseable).
  - `build_option_ticker(symbol: str, expiration: str, strike: float, right: str) -> str`
  - `chain_records(client, symbol: str, *, expiration: str, contract_type: str | None, strike_min: float | None, strike_max: float | None) -> list[dict]` — normalized chain rows; `client` is a Massive REST client exposing `list_snapshot_options_chain(underlying_asset=..., params={"expiration_date": ...})`.
  - `contract_snapshot(client, option_ticker: str) -> dict` — single-contract detail; `client` exposes `get_snapshot_option(underlying_asset=..., option_contract=...)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_options_data.py`:

```python
from types import SimpleNamespace as NS

import pytest

from trader.tools.options_data import (
    build_option_ticker,
    chain_records,
    contract_snapshot,
    parse_option_ticker,
)


def test_parse_option_ticker_extracts_components():
    parsed = parse_option_ticker("O:AAPL260320C00250000")
    assert parsed == {
        "symbol": "AAPL",
        "expiration": "2026-03-20",
        "right": "C",
        "strike": 250.0,
    }


def test_build_option_ticker_round_trips():
    ticker = build_option_ticker("AAPL", "2026-03-20", 250.0, "c")
    assert ticker == "O:AAPL260320C00250000"
    assert parse_option_ticker(ticker)["strike"] == 250.0


def test_parse_option_ticker_rejects_garbage():
    with pytest.raises(ValueError):
        parse_option_ticker("O:AAPL")


def test_chain_records_normalizes_and_filters_by_strike():
    snap = NS(
        details=NS(ticker="O:AAPL260320C00250000", contract_type="call",
                   strike_price=250.0, expiration_date="2026-03-20"),
        last_quote=NS(bid=12.0, ask=12.4),
        last_trade=NS(price=12.2),
        day=NS(volume=1500),
        greeks=NS(delta=0.55, gamma=0.02, theta=-0.03, vega=0.10),
        open_interest=800, implied_volatility=0.31,
        break_even_price=262.0, underlying_asset=NS(price=248.0),
    )
    low = NS(details=NS(ticker="x", contract_type="call", strike_price=100.0,
                        expiration_date="2026-03-20"),
             last_quote=None, last_trade=None, day=None, greeks=None,
             open_interest=0, implied_volatility=0.0, break_even_price=0.0,
             underlying_asset=None)
    client = NS(list_snapshot_options_chain=lambda **kw: [snap, low])

    rows = chain_records(client, "AAPL", expiration="2026-03-20",
                         contract_type="call", strike_min=200.0, strike_max=None)

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "O:AAPL260320C00250000"
    assert row["strike"] == 250.0
    assert row["mid"] == pytest.approx(12.2)
    assert row["iv"] == pytest.approx(31.0)  # percent
    assert row["delta"] == 0.55
    assert row["underlying_price"] == 248.0


def test_contract_snapshot_normalizes_single_contract():
    client = NS(get_snapshot_option=lambda **kw: NS(
        break_even_price=262.0, implied_volatility=0.31, open_interest=800,
        last_quote=NS(bid=12.0, ask=12.4), last_trade=NS(price=12.2),
        greeks=NS(delta=0.55, gamma=0.02, theta=-0.03, vega=0.10),
        underlying_asset=NS(price=248.0), day=NS(volume=1500),
    ))
    result = contract_snapshot(client, "O:AAPL260320C00250000")
    assert result["symbol"] == "AAPL"
    assert result["strike"] == 250.0
    assert result["right"] == "C"
    assert result["bid"] == 12.0
    assert result["implied_volatility"] == "31.00%"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_options_data.py -q`
Expected: FAIL — `ModuleNotFoundError: trader.tools.options_data`.

- [ ] **Step 3: Implement `trader/tools/options_data.py`**

Move the ticker parse/build verbatim from `trader/sdk.py:3155-3213`, and lift the chain-row and contract normalization from `sdk.py:3348-3408` and `sdk.py:3441-3477`:

```python
"""Shared, client-taking Massive option normalization for CLI + dashboard."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_option_ticker(ticker: str) -> dict:
    """Parse ``O:AAPL260320C00250000`` → {symbol, expiration, right, strike}."""
    t = ticker[2:] if ticker.startswith("O:") else ticker
    i = 0
    while i < len(t) and t[i].isalpha():
        i += 1
    symbol = t[:i]
    rest = t[i:]
    if len(rest) < 9:
        raise ValueError(f"Cannot parse option ticker: {ticker}")
    date_str, right, strike_str = rest[:6], rest[6], rest[7:]
    return {
        "symbol": symbol,
        "expiration": f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}",
        "right": right,
        "strike": float(strike_str) / 1000.0,
    }


def build_option_ticker(symbol: str, expiration: str, strike: float, right: str) -> str:
    date_str = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    return f"O:{symbol}{date_str}{right.upper()}{int(strike * 1000):08d}"


def chain_records(
    client: Any,
    symbol: str,
    *,
    expiration: str,
    contract_type: str | None,
    strike_min: float | None,
    strike_max: float | None,
) -> list[dict]:
    rows: list[dict] = []
    for snap in client.list_snapshot_options_chain(
        underlying_asset=symbol, params={"expiration_date": expiration},
    ):
        details = snap.details
        if not details or not details.strike_price:
            continue
        ct = (details.contract_type or "").lower()
        if contract_type and ct != contract_type.lower():
            continue
        strike = details.strike_price
        if strike_min is not None and strike < strike_min:
            continue
        if strike_max is not None and strike > strike_max:
            continue
        bid = ask = last = volume = 0.0
        if snap.last_quote:
            bid = snap.last_quote.bid or 0.0
            ask = snap.last_quote.ask or 0.0
        if snap.last_trade:
            last = getattr(snap.last_trade, "price", 0.0) or 0.0
        if snap.day:
            volume = getattr(snap.day, "volume", 0.0) or 0.0
        mid = (bid + ask) / 2.0 if (bid and ask) else 0.0
        greeks = snap.greeks
        delta = gamma = theta = vega = 0.0
        if greeks:
            delta = greeks.delta or 0.0
            gamma = greeks.gamma or 0.0
            theta = greeks.theta or 0.0
            vega = greeks.vega or 0.0
        underlying_price = snap.underlying_asset.price or 0.0 if snap.underlying_asset else 0.0
        rows.append({
            "ticker": details.ticker or "",
            "type": ct,
            "strike": strike,
            "expiration": details.expiration_date or expiration,
            "bid": bid, "ask": ask, "mid": mid, "last": last, "volume": volume,
            "open_interest": snap.open_interest or 0.0,
            "iv": (snap.implied_volatility or 0.0) * 100.0,
            "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
            "break_even": snap.break_even_price or 0.0,
            "underlying_price": underlying_price,
        })
    rows.sort(key=lambda r: (r["type"], r["strike"]))
    return rows


def contract_snapshot(client: Any, option_ticker: str) -> dict:
    parsed = parse_option_ticker(option_ticker)
    ticker_clean = option_ticker[2:] if option_ticker.startswith("O:") else option_ticker
    snap = client.get_snapshot_option(
        underlying_asset=parsed["symbol"], option_contract=ticker_clean,
    )
    result = {
        "ticker": option_ticker,
        "symbol": parsed["symbol"],
        "expiration": parsed["expiration"],
        "strike": parsed["strike"],
        "right": parsed["right"],
        "break_even": snap.break_even_price or 0.0,
        "implied_volatility": f"{(snap.implied_volatility or 0.0) * 100.0:.2f}%",
        "open_interest": snap.open_interest or 0.0,
    }
    if snap.last_quote:
        result["bid"] = snap.last_quote.bid or 0.0
        result["ask"] = snap.last_quote.ask or 0.0
        result["mid"] = ((snap.last_quote.bid or 0.0) + (snap.last_quote.ask or 0.0)) / 2.0
    if snap.last_trade:
        result["last"] = getattr(snap.last_trade, "price", 0.0) or 0.0
    if snap.greeks:
        result["delta"] = snap.greeks.delta or 0.0
        result["gamma"] = snap.greeks.gamma or 0.0
        result["theta"] = snap.greeks.theta or 0.0
        result["vega"] = snap.greeks.vega or 0.0
    if snap.underlying_asset:
        result["underlying_price"] = snap.underlying_asset.price or 0.0
    if snap.day:
        result["volume"] = getattr(snap.day, "volume", 0.0) or 0.0
    return result
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_options_data.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add trader/tools/options_data.py tests/test_options_data.py
git commit -m "feat(options): shared client-taking option normalization helpers"
```

---

### Task 2: `MassiveResearch` options + forex provider methods

**Files:**
- Modify: `trader/tools/massive_research.py`
- Modify: `tests/test_massive_research.py`

**Interfaces:**
- Consumes: Task 1 helpers; `json_clean`, `ResearchResult` (same module).
- Produces (methods on `MassiveResearch`):
  - `options_expirations(symbol) -> ResearchResult` — data: `[{"expiration": "YYYY-MM-DD", "dte": int}, ...]`
  - `options_chain(symbol, *, expiration, contract_type, strike_min, strike_max) -> ResearchResult` — data: chain records
  - `options_snapshot(option_ticker) -> ResearchResult` — data: single contract dict
  - `options_implied(symbol, *, expiration, risk_free_rate=0.05) -> ResearchResult` — data: `{"x":[...],"market_implied":[...],"constant":[...]}`
  - `forex_snapshot(pair, *, source) -> ResearchResult` (`source` in `{"massive","twelvedata"}`; raises `ValueError` on `"ib"`)
  - `forex_quote(from_ccy, to_ccy, *, source) -> ResearchResult` (same source contract)
  - `forex_movers(direction) -> ResearchResult`
  - `forex_snapshot_all(tickers) -> ResearchResult`
  - `forex_convert(from_ccy, to_ccy, amount) -> ResearchResult`
- `MassiveResearch.__init__` gains `api_key: str = ""` (used by `options_expirations`/`options_implied`, which call `trader.tools.chain.get_option_dates` / `implied_constant`).

- [ ] **Step 1: Write failing tests** — append to `tests/test_massive_research.py` (mirror the inline-`NS` style at `:41-133`):

```python
from trader.tools.massive_research import MassiveResearch, ResearchResult


def test_options_chain_returns_records(monkeypatch):
    from types import SimpleNamespace as NS
    snap = NS(
        details=NS(ticker="O:AAPL260320C00250000", contract_type="call",
                   strike_price=250.0, expiration_date="2026-03-20"),
        last_quote=NS(bid=12.0, ask=12.4), last_trade=NS(price=12.2),
        day=NS(volume=1500),
        greeks=NS(delta=0.55, gamma=0.02, theta=-0.03, vega=0.10),
        open_interest=800, implied_volatility=0.31, break_even_price=262.0,
        underlying_asset=NS(price=248.0),
    )
    client = NS(list_snapshot_options_chain=lambda **kw: [snap])
    result = MassiveResearch(client).options_chain(
        "AAPL", expiration="2026-03-20", contract_type=None,
        strike_min=None, strike_max=None)
    assert isinstance(result, ResearchResult)
    assert result.data[0]["ticker"] == "O:AAPL260320C00250000"
    assert result.title == "Options chain: AAPL 2026-03-20"


def test_options_expirations_computes_dte(monkeypatch):
    import trader.tools.massive_research as mod
    monkeypatch.setattr(
        "trader.tools.chain.get_option_dates",
        lambda symbol, api_key="": ["2026-03-20", "2026-04-17"])
    result = MassiveResearch(object(), api_key="k").options_expirations("AAPL")
    assert [row["expiration"] for row in result.data] == ["2026-03-20", "2026-04-17"]
    assert all("dte" in row for row in result.data)


def test_forex_snapshot_massive_normalizes():
    from types import SimpleNamespace as NS
    client = NS(get_snapshot_ticker=lambda **kw: NS(
        day=NS(open=1.08, high=1.09, low=1.07, close=1.085, volume=0, vwap=1.08),
        last_quote=NS(bid=1.0849, ask=1.0851),
        todays_change=0.001, todays_change_percent=0.09))
    result = MassiveResearch(client).forex_snapshot("EURUSD", source="massive")
    assert result.data["ticker"] == "C:EURUSD"
    assert result.data["bid"] == 1.0849
    assert result.provider == "massive"


def test_forex_snapshot_rejects_ib_source():
    import pytest
    with pytest.raises(ValueError):
        MassiveResearch(object()).forex_snapshot("EURUSD", source="ib")


def test_forex_quote_twelvedata_emits_no_bidask_notice():
    from types import SimpleNamespace as NS
    td = NS(exchange_rate=lambda symbol: NS(as_json=lambda: {
        "rate": 1.085, "timestamp": 1_753_000_000}))
    result = MassiveResearch(object(), td_client=td).forex_quote(
        "EUR", "USD", source="twelvedata")
    assert result.provider == "twelvedata"
    assert result.data["last"] == 1.085
    assert "bid/ask" in (result.notice or "")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_massive_research.py -k "options or forex" -q`
Expected: FAIL — methods/`api_key` kwarg not defined.

- [ ] **Step 3: Implement the provider methods** in `trader/tools/massive_research.py`.

Add `api_key` to `__init__` and the new methods (place after `news`). Forex normalization mirrors `sdk.py:3620-3805,4268-4305`; options delegate to Task 1 helpers + `trader.tools.chain`:

```python
    def __init__(self, client: Any, td_client: Any | None = None, api_key: str = ""):
        self._client = client
        self._td_client = td_client
        self._api_key = api_key

    # ---- options -------------------------------------------------------
    def options_expirations(self, symbol: str) -> ResearchResult:
        from datetime import date
        from trader.tools.chain import get_option_dates
        dates = get_option_dates(symbol, api_key=self._api_key)
        today = date.today()
        rows = [{
            "expiration": d,
            "dte": (date.fromisoformat(d) - today).days,
        } for d in dates]
        return ResearchResult(json_clean(rows), f"Options expirations: {symbol.upper()}")

    def options_chain(self, symbol, *, expiration, contract_type,
                      strike_min, strike_max) -> ResearchResult:
        from trader.tools.chain import get_option_dates
        from trader.tools.options_data import chain_records
        exp = expiration
        if not exp:
            dates = get_option_dates(symbol, api_key=self._api_key)
            if not dates:
                return ResearchResult([], f"Options chain: {symbol.upper()}")
            exp = dates[0]
        rows = chain_records(self._client, symbol, expiration=exp,
                             contract_type=contract_type,
                             strike_min=strike_min, strike_max=strike_max)
        return ResearchResult(json_clean(rows), f"Options chain: {symbol.upper()} {exp}")

    def options_snapshot(self, option_ticker: str) -> ResearchResult:
        from trader.tools.options_data import contract_snapshot
        data = contract_snapshot(self._client, option_ticker)
        return ResearchResult(json_clean(data), f"Option: {option_ticker}")

    def options_implied(self, symbol, *, expiration, risk_free_rate=0.05) -> ResearchResult:
        from trader.tools.chain import implied_constant
        data = implied_constant(symbol, expiration, risk_free_rate, api_key=self._api_key)
        return ResearchResult(
            json_clean(data), f"Implied distribution: {symbol.upper()} {expiration}")

    # ---- forex ---------------------------------------------------------
    def forex_snapshot(self, pair: str, *, source: str) -> ResearchResult:
        if source == "ib":
            raise ValueError("ib source is handled on the typed path, not the provider")
        ticker = pair.upper()
        if not ticker.startswith("C:"):
            ticker = f"C:{ticker}"
        if source == "twelvedata":
            payload = self._td_client.quote(symbol=ticker[2:]).as_json()
            data = {"ticker": ticker, "last": _td_float(payload, "close"),
                    "open": _td_float(payload, "open"), "high": _td_float(payload, "high"),
                    "low": _td_float(payload, "low"), "close": _td_float(payload, "close"),
                    "change": _td_float(payload, "change"),
                    "change_pct": _td_float(payload, "percent_change")}
            return ResearchResult(json_clean(data), f"Forex snapshot: {ticker}",
                                  provider="twelvedata",
                                  notice="TwelveData REST has no bid/ask.")
        snap = self._client.get_snapshot_ticker(market_type="forex", ticker=ticker)
        data = {"ticker": ticker}
        if snap.day:
            for f in ("open", "high", "low", "close", "volume", "vwap"):
                data[f] = getattr(snap.day, f, None)
        if snap.last_quote:
            data["bid"] = getattr(snap.last_quote, "bid", None)
            data["ask"] = getattr(snap.last_quote, "ask", None)
        data["change"] = getattr(snap, "todays_change", None)
        data["change_pct"] = getattr(snap, "todays_change_percent", None)
        return ResearchResult(json_clean(data), f"Forex snapshot: {ticker}")

    def forex_quote(self, from_ccy: str, to_ccy: str, *, source: str) -> ResearchResult:
        pair = f"{from_ccy.upper()}/{to_ccy.upper()}"
        if source == "ib":
            raise ValueError("ib source is handled on the typed path, not the provider")
        if source == "twelvedata":
            payload = self._td_client.exchange_rate(symbol=pair).as_json()
            data = {"pair": pair, "last": _td_float(payload, "rate"),
                    "timestamp": payload.get("timestamp")}
            return ResearchResult(json_clean(data), f"Forex quote: {pair}",
                                  provider="twelvedata",
                                  notice="TwelveData REST has no bid/ask.")
        result = self._client.get_last_forex_quote(from_ccy, to_ccy)
        data = {"pair": pair}
        if result.last:
            data["bid"] = result.last.bid
            data["ask"] = result.last.ask
            data["timestamp"] = result.last.timestamp
        return ResearchResult(json_clean(data), f"Forex quote: {pair}")

    def forex_movers(self, direction: str) -> ResearchResult:
        snaps = self._client.get_snapshot_direction(market_type="forex", direction=direction)
        rows = []
        for snap in snaps:
            row = {"ticker": getattr(snap, "ticker", "") or ""}
            if snap.day:
                row["close"] = getattr(snap.day, "close", None)
                row["volume"] = getattr(snap.day, "volume", None)
            row["change"] = getattr(snap, "todays_change", None)
            row["change_pct"] = getattr(snap, "todays_change_percent", None)
            rows.append(row)
        return ResearchResult(json_clean(rows), f"Forex movers ({direction})")

    def forex_snapshot_all(self, tickers: list[str] | None) -> ResearchResult:
        arg = None
        if tickers:
            arg = [t if t.startswith("C:") else f"C:{t}" for t in tickers]
        snaps = self._client.get_snapshot_all(market_type="forex", tickers=arg)
        rows = []
        for snap in snaps:
            row = {"ticker": getattr(snap, "ticker", "") or ""}
            if snap.day:
                for f in ("open", "high", "low", "close", "volume"):
                    row[f] = getattr(snap.day, f, None)
            row["change"] = getattr(snap, "todays_change", None)
            row["change_pct"] = getattr(snap, "todays_change_percent", None)
            rows.append(row)
        return ResearchResult(json_clean(rows), "Forex snapshots")

    def forex_convert(self, from_ccy: str, to_ccy: str, amount: float) -> ResearchResult:
        result = self._client.get_real_time_currency_conversion(
            from_=from_ccy.upper(), to=to_ccy.upper(), amount=amount)
        data = {"from": from_ccy.upper(), "to": to_ccy.upper(), "amount": float(amount),
                "converted": getattr(result, "converted", None),
                "rate": getattr(result, "last", None) and getattr(result.last, "exchange", None)}
        return ResearchResult(json_clean(data), f"Convert {from_ccy.upper()}→{to_ccy.upper()}")
```

Add the module-level helper near `_quote_price`:

```python
def _td_float(payload: dict, key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
```

> NOTE: `get_real_time_currency_conversion`'s exact result attributes and `get_snapshot_all`'s shape come from the Massive SDK; the executor must confirm the real attribute names against `sdk.py:4307+` and `sdk.py:3764-3788` when wiring, and adjust the `getattr` field names to match — the tests above use `SimpleNamespace` doubles that must mirror whatever the real objects expose.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_massive_research.py -k "options or forex" -q`
Expected: PASS.

- [ ] **Step 5: Full provider suite (regression)**

Run: `pytest tests/test_massive_research.py -q`
Expected: PASS (existing tests unaffected — `api_key` defaults to `""`).

- [ ] **Step 6: Commit**

```bash
git add trader/tools/massive_research.py tests/test_massive_research.py
git commit -m "feat(research): options + forex provider methods on MassiveResearch"
```

---

### Task 3: `ResearchService` backend selector + `TRADER_LINK_UNAVAILABLE` + timeouts

**Files:**
- Modify: `web/command_center/research.py`
- Modify: `tests/test_dashboard_research_service.py`

**Interfaces:**
- Consumes: `ResearchResult`, `ResearchError`.
- Produces: `ResearchService.run(tool, operation, *, backend="massive", log_params=None)`. When `backend="massive"`, `operation` is called with the Massive provider (unchanged). When `backend="trader"`, `operation` is called with **no argument** and must not touch the Massive provider (so `MASSIVE_NOT_CONFIGURED` is never raised for trader-backed tools). New timeout keys in `DEFAULT_TIMEOUTS`.

- [ ] **Step 1: Write failing tests** — append to `tests/test_dashboard_research_service.py`:

```python
@pytest.mark.asyncio
async def test_trader_backend_does_not_touch_massive_provider():
    # provider factory would raise if called; trader backend must not call it
    service = ResearchService(
        lambda: (_ for _ in ()).throw(AssertionError("provider built")),
        workers=1, clock=lambda: "2026-07-23T00:00:00Z")
    body = await service.run(
        "forex_snapshot", lambda: ResearchResult({"pair": "EUR/USD"}, "IB forex",
                                                 provider="ib"),
        backend="trader")
    assert body["meta"]["provider"] == "ib"
    assert body["data"] == {"pair": "EUR/USD"}
    service.close()


@pytest.mark.asyncio
async def test_trader_backend_propagates_trader_link_unavailable():
    service = ResearchService(lambda: object(), workers=1)
    with pytest.raises(ResearchError) as caught:
        await service.run(
            "forex_snapshot",
            lambda: (_ for _ in ()).throw(
                ResearchError(503, "TRADER_LINK_UNAVAILABLE", "down", True)),
            backend="trader")
    assert caught.value.code == "TRADER_LINK_UNAVAILABLE"
    service.close()


def test_new_timeout_keys_present():
    for key in ("options_chain", "options_snapshot", "options_expirations",
                "options_implied", "forex_snapshot", "forex_quote",
                "forex_movers", "forex_snapshot_all", "forex_convert"):
        assert key in DEFAULT_TIMEOUTS
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_dashboard_research_service.py -k "trader_backend or timeout_keys" -q`
Expected: FAIL — `run()` has no `backend` kwarg / keys missing.

- [ ] **Step 3: Implement.** In `web/command_center/research.py`:

Extend `DEFAULT_TIMEOUTS`:

```python
DEFAULT_TIMEOUTS = {
    "snapshot": 10.0, "movers": 15.0, "news": 15.0, "ideas": 30.0,
    "options_expirations": 10.0, "options_chain": 20.0,
    "options_snapshot": 10.0, "options_implied": 20.0,
    "forex_snapshot": 10.0, "forex_quote": 10.0, "forex_movers": 15.0,
    "forex_snapshot_all": 20.0, "forex_convert": 10.0,
}
```

Change the `run` signature and the single submit line (everything else in `run` is unchanged):

```python
    async def run(
        self,
        tool: str,
        operation,
        *,
        backend: str = "massive",
        log_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...
            try:
                if backend == "trader":
                    future = self._executor.submit(operation)
                else:
                    future = self._executor.submit(
                        lambda: operation(self._get_provider()))
            except Exception as exc:
                ...
```

`TRADER_LINK_UNAVAILABLE` needs no new mapping — a `ResearchError` raised inside the operation already propagates unchanged via the existing `except ResearchError: raise` branch.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard_research_service.py -q`
Expected: PASS (all, including existing).

- [ ] **Step 5: Commit**

```bash
git add web/command_center/research.py tests/test_dashboard_research_service.py
git commit -m "feat(research): backend selector for trader-backed research operations"
```

---

### Task 4: Options routes

**Files:**
- Modify: `web/command_center/routes_research.py`
- Modify: `tests/test_dashboard_research_api.py`

**Interfaces:**
- Consumes: `service.run(...)`, `MassiveResearch.options_*`.
- Produces routes: `GET /api/research/options/expirations|chain|snapshot|implied`. Each session-gated, `reject_unknown` allowlisted, `backend="massive"`.

- [ ] **Step 1: Add `options()`/`forex()` to `FakeResearchProvider`** in `tests/test_dashboard_research_api.py` (`:35-67`) so the fake mirrors the real provider surface. Add methods that record calls and return `ResearchResult`:

```python
    def options_expirations(self, symbol):
        self.calls.append(("options_expirations", {"symbol": symbol}))
        return ResearchResult([{"expiration": "2026-03-20", "dte": 240}],
                              f"Options expirations: {symbol}")

    def options_chain(self, symbol, *, expiration, contract_type, strike_min, strike_max):
        self.calls.append(("options_chain", {"symbol": symbol, "expiration": expiration}))
        return ResearchResult([{"ticker": "O:AAPL260320C00250000", "strike": 250.0}],
                              f"Options chain: {symbol}")

    def options_snapshot(self, option_ticker):
        self.calls.append(("options_snapshot", {"option_ticker": option_ticker}))
        return ResearchResult({"ticker": option_ticker, "strike": 250.0},
                              f"Option: {option_ticker}")

    def options_implied(self, symbol, *, expiration, risk_free_rate=0.05):
        self.calls.append(("options_implied", {"symbol": symbol, "expiration": expiration}))
        return ResearchResult({"x": [1], "market_implied": [0.5], "constant": [0.5]},
                              f"Implied distribution: {symbol}")
```

- [ ] **Step 2: Write failing route tests** — append to `tests/test_dashboard_research_api.py`:

```python
def test_options_chain_success(logged_in_research_client):
    r = logged_in_research_client.get(
        "/api/research/options/chain?symbol=AAPL&expiration=2026-03-20&type=call")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["tool"] == "options_chain"
    assert body["data"][0]["ticker"] == "O:AAPL260320C00250000"


def test_options_routes_require_session(app_with_research):
    client = TestClient(app_with_research)
    for path in ("options/expirations?symbol=AAPL",
                 "options/chain?symbol=AAPL",
                 "options/snapshot?option_ticker=O:AAPL260320C00250000",
                 "options/implied?symbol=AAPL&expiration=2026-03-20"):
        assert client.get(f"/api/research/{path}").status_code == 401


def test_options_chain_rejects_bad_type_and_strike_order(logged_in_research_client):
    assert logged_in_research_client.get(
        "/api/research/options/chain?symbol=AAPL&type=long").status_code == 422
    assert logged_in_research_client.get(
        "/api/research/options/chain?symbol=AAPL&strike_min=300&strike_max=100"
    ).status_code == 422


def test_options_chain_rejects_unknown_param(logged_in_research_client):
    r = logged_in_research_client.get("/api/research/options/chain?symbol=AAPL&foo=1")
    assert r.status_code == 422


def test_options_implied_requires_expiration(logged_in_research_client):
    assert logged_in_research_client.get(
        "/api/research/options/implied?symbol=AAPL").status_code == 422
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_dashboard_research_api.py -k options -q`
Expected: FAIL — 404 (routes absent).

- [ ] **Step 4: Implement the options routes** in `create_research_router` (before `return router`). Reuse `reject_unknown`, `validation_error`, `run`:

```python
    @router.get("/options/expirations")
    async def options_expirations(
        request: Request,
        symbol: str = Query(min_length=1, max_length=32,
                            pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"symbol"})
        return await run(lambda: service.run(
            "options_expirations",
            lambda provider: provider.options_expirations(symbol),
            log_params={"symbol": symbol.upper()}))

    @router.get("/options/chain")
    async def options_chain(
        request: Request,
        symbol: str = Query(min_length=1, max_length=32,
                            pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
        expiration: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
        type: Literal["call", "put"] | None = None,
        strike_min: float | None = Query(None, gt=0),
        strike_max: float | None = Query(None, gt=0),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"symbol", "expiration", "type",
                                 "strike_min", "strike_max"})
        if (strike_min is not None and strike_max is not None
                and strike_max < strike_min):
            raise validation_error("strike_max",
                                   "strike_max must be >= strike_min", strike_max)
        return await run(lambda: service.run(
            "options_chain",
            lambda provider: provider.options_chain(
                symbol, expiration=expiration or None, contract_type=type,
                strike_min=strike_min, strike_max=strike_max),
            log_params={"symbol": symbol.upper(), "expiration": expiration,
                        "type": type, "strike_min": strike_min,
                        "strike_max": strike_max}))

    @router.get("/options/snapshot")
    async def options_contract_snapshot(
        request: Request,
        option_ticker: str = Query(min_length=3, max_length=40,
                                   pattern=r"^O:[A-Z0-9]+$"),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"option_ticker"})
        return await run(lambda: service.run(
            "options_snapshot",
            lambda provider: provider.options_snapshot(option_ticker),
            log_params={"option_ticker": option_ticker}))

    @router.get("/options/implied")
    async def options_implied(
        request: Request,
        symbol: str = Query(min_length=1, max_length=32,
                            pattern=r"^[A-Za-z][A-Za-z0-9.\-]*$"),
        expiration: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"symbol", "expiration"})
        return await run(lambda: service.run(
            "options_implied",
            lambda provider: provider.options_implied(symbol, expiration=expiration),
            log_params={"symbol": symbol.upper(), "expiration": expiration}))
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_dashboard_research_api.py -k options -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/command_center/routes_research.py tests/test_dashboard_research_api.py
git commit -m "feat(research): options routes (expirations/chain/snapshot/implied)"
```

---

### Task 5: Forex routes (Massive + IB typed path)

**Files:**
- Modify: `web/command_center/routes_research.py`
- Modify: `tests/test_dashboard_research_api.py`

**Interfaces:**
- Consumes: `service.run(..., backend=...)`; `cc._query_client` (may be `None`); `MassiveResearch.forex_*`.
- Produces routes: `GET /api/research/forex/snapshot|quote|movers|snapshot-all|convert`. `snapshot`/`quote` take `source` `Literal["massive","ib","twelvedata"]` default `massive`; `source="ib"` runs `backend="trader"` via a closure that calls `cc._query_client`.

- [ ] **Step 1: Add `forex()` helpers to `FakeResearchProvider`** (record + return `ResearchResult`):

```python
    def forex_snapshot(self, pair, *, source):
        self.calls.append(("forex_snapshot", {"pair": pair, "source": source}))
        return ResearchResult({"ticker": f"C:{pair}", "bid": 1.08, "ask": 1.081},
                              f"Forex snapshot: {pair}")

    def forex_quote(self, from_ccy, to_ccy, *, source):
        self.calls.append(("forex_quote", {"from": from_ccy, "to": to_ccy}))
        return ResearchResult({"pair": f"{from_ccy}/{to_ccy}", "bid": 1.08},
                              f"Forex quote: {from_ccy}/{to_ccy}")

    def forex_movers(self, direction):
        self.calls.append(("forex_movers", {"direction": direction}))
        return ResearchResult([{"ticker": "C:EURUSD", "change_pct": 0.4}],
                              f"Forex movers ({direction})")

    def forex_snapshot_all(self, tickers):
        self.calls.append(("forex_snapshot_all", {"tickers": tickers}))
        return ResearchResult([{"ticker": "C:EURUSD"}], "Forex snapshots")

    def forex_convert(self, from_ccy, to_ccy, amount):
        self.calls.append(("forex_convert", {"from": from_ccy, "to": to_ccy, "amount": amount}))
        return ResearchResult({"from": from_ccy, "to": to_ccy, "amount": amount,
                               "converted": amount * 1.08}, "Convert")
```

- [ ] **Step 2: Write failing tests** (Massive paths + IB path with a fake query client + IB-down). For the IB path, `research_cc` must expose a `_query_client`; add a helper that builds a CC whose `_query_client.call` returns canned discover/snapshot dicts, plus one that is `None`:

```python
class _FakeQueryClient:
    def __init__(self, mapping):
        self._mapping = mapping  # method -> dict
    def call(self, method, body, _type, timeout=None):
        return self._mapping[method]


def test_forex_snapshot_massive(logged_in_research_client):
    r = logged_in_research_client.get("/api/research/forex/snapshot?pair=EURUSD")
    assert r.status_code == 200
    assert r.json()["meta"]["tool"] == "forex_snapshot"


def test_forex_movers_rejects_source_param(logged_in_research_client):
    assert logged_in_research_client.get(
        "/api/research/forex/movers?direction=gainers&source=ib").status_code == 422


def test_forex_convert_rejects_nonpositive_amount(logged_in_research_client):
    assert logged_in_research_client.get(
        "/api/research/forex/convert?from=EUR&to=USD&amount=0").status_code == 422


def test_forex_snapshot_ib_uses_typed_query(app_factory_with_query_client):
    # app whose cc._query_client returns a resolved instrument + snapshot
    client = app_factory_with_query_client(_FakeQueryClient({
        "discover_instrument": {"instruments": [{"instrument_id": 12087792}]},
        "get_snapshot": {"snapshot": {"bid": 1.0849, "ask": 1.0851, "last": 1.085}},
    }))
    _login(client)
    r = client.get("/api/research/forex/snapshot?pair=EURUSD&source=ib")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["provider"] == "ib"
    assert body["data"]["bid"] == 1.0849


def test_forex_snapshot_ib_down_returns_trader_link_unavailable(
        app_factory_with_query_client):
    client = app_factory_with_query_client(None)  # no query client
    _login(client)
    r = client.get("/api/research/forex/snapshot?pair=EURUSD&source=ib")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "TRADER_LINK_UNAVAILABLE"
```

Add the `app_factory_with_query_client` fixture near the other fixtures — it builds a `research_cc`-style CommandCenter with `_query_client` set to the supplied object (or left `None`) and returns a logged-in-capable `TestClient` over `create_app(cc, research_service)`. Model it on `research_cc` (`:111-139`) + `app_with_research` (`:142-147`); set `cc._query_client = supplied` after construction.

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_dashboard_research_api.py -k forex -q`
Expected: FAIL — routes absent.

- [ ] **Step 4: Implement forex routes.** Add a pair parser at module scope in `routes_research.py`:

```python
def _parse_pair(pair: str) -> tuple[str, str]:
    raw = pair.upper().replace("C:", "").replace("/", "")
    if len(raw) != 6 or not raw.isalpha():
        raise ValueError(f"Cannot parse forex pair: {pair}")
    return raw[:3], raw[3:]
```

Then inside `create_research_router` (the IB closure captures `cc`):

```python
    from trader.tools.massive_research import ResearchResult

    def _ib_forex_snapshot_op(pair: str):
        base, quote = _parse_pair(pair)

        def op():
            qc = getattr(cc, "_query_client", None)
            if qc is None:
                raise ResearchError(503, "TRADER_LINK_UNAVAILABLE",
                                    "Trader link unavailable for IB forex.", True)
            try:
                disc = qc.call("discover_instrument",
                               {"symbol": base, "exchange": "IDEALPRO",
                                "currency": quote, "sec_type": "CASH"}, dict)
                instruments = disc.get("instruments") or []
                if not instruments:
                    raise ResearchError(502, "RESEARCH_UPSTREAM_ERROR",
                                        f"Could not resolve forex pair {base}/{quote}.",
                                        False)
                conid = int(instruments[0]["instrument_id"])
                snap = qc.call("get_snapshot",
                               {"instrument_id": conid, "delayed": False}, dict)
            except ResearchError:
                raise
            except Exception as exc:
                raise ResearchError(503, "TRADER_LINK_UNAVAILABLE",
                                    "IB forex snapshot unavailable.", True) from exc
            s = snap.get("snapshot") or {}
            return ResearchResult(
                {"pair": f"{base}/{quote}", "bid": s.get("bid"), "ask": s.get("ask"),
                 "last": s.get("last"), "open": s.get("open"), "high": s.get("high"),
                 "low": s.get("low"), "close": s.get("close"), "time": s.get("time")},
                f"Forex snapshot: {base}/{quote} (IB)", provider="ib")
        return op

    @router.get("/forex/snapshot")
    async def forex_snapshot(
        request: Request,
        pair: str = Query(min_length=6, max_length=10),
        source: Literal["massive", "ib", "twelvedata"] = "massive",
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"pair", "source"})
        try:
            _parse_pair(pair)
        except ValueError as exc:
            raise validation_error("pair", str(exc), pair)
        if source == "ib":
            return await run(lambda: service.run(
                "forex_snapshot", _ib_forex_snapshot_op(pair),
                backend="trader", log_params={"pair": pair, "source": source}))
        return await run(lambda: service.run(
            "forex_snapshot",
            lambda provider: provider.forex_snapshot(pair, source=source),
            log_params={"pair": pair, "source": source}))

    @router.get("/forex/quote")
    async def forex_quote(
        request: Request,
        from_: str = Query(alias="from", pattern=r"^[A-Za-z]{3}$"),
        to: str = Query(pattern=r"^[A-Za-z]{3}$"),
        source: Literal["massive", "ib", "twelvedata"] = "massive",
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"from", "to", "source"})
        if source == "ib":
            return await run(lambda: service.run(
                "forex_quote", _ib_forex_snapshot_op(f"{from_}{to}"),
                backend="trader", log_params={"from": from_, "to": to, "source": source}))
        return await run(lambda: service.run(
            "forex_quote",
            lambda provider: provider.forex_quote(from_, to, source=source),
            log_params={"from": from_.upper(), "to": to.upper(), "source": source}))

    @router.get("/forex/movers")
    async def forex_movers(
        request: Request,
        direction: Literal["gainers", "losers"] = "gainers",
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"direction"})
        return await run(lambda: service.run(
            "forex_movers", lambda provider: provider.forex_movers(direction),
            log_params={"direction": direction}))

    @router.get("/forex/snapshot-all")
    async def forex_snapshot_all(
        request: Request,
        tickers: list[str] = Query(default=[]),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"tickers"})
        normalized = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
        if len(normalized) > 100:
            raise validation_error("tickers", "At most 100 tickers", tickers,
                                   error_type="too_long")
        return await run(lambda: service.run(
            "forex_snapshot_all",
            lambda provider: provider.forex_snapshot_all(normalized or None),
            log_params={"tickers": normalized}))

    @router.get("/forex/convert")
    async def forex_convert(
        request: Request,
        from_: str = Query(alias="from", pattern=r"^[A-Za-z]{3}$"),
        to: str = Query(pattern=r"^[A-Za-z]{3}$"),
        amount: float = Query(gt=0, le=1e12),
        _session: str = Depends(require_session),
    ):
        reject_unknown(request, {"from", "to", "amount"})
        return await run(lambda: service.run(
            "forex_convert",
            lambda provider: provider.forex_convert(from_, to, amount),
            log_params={"from": from_.upper(), "to": to.upper(), "amount": amount}))
```

> NOTE: `from` is a Python keyword, so the query params use `from_` with `alias="from"` — the wire param is `from`, and `reject_unknown` allowlists `"from"`.

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_dashboard_research_api.py -k forex -q`
Expected: PASS.

- [ ] **Step 6: Full API suite (regression)**

Run: `pytest tests/test_dashboard_research_api.py -q`
Expected: PASS except the `data-research-later` count test (fixed in Task 6).

- [ ] **Step 7: Commit**

```bash
git add web/command_center/routes_research.py tests/test_dashboard_research_api.py
git commit -m "feat(research): forex routes with Massive + IB typed snapshot path"
```

---

### Task 6: Frontend — activate Options + Forex panes

**Files:**
- Modify: `web/templates/_research_tab.html`
- Modify: `web/static/command_center_research.js`
- Modify: `web/static/research_test_harness.js` (line 84)
- Modify: `web/static/command_center_research.test.js`
- Modify: `tests/test_dashboard_research_api.py` (fix `data-research-later` count)

**Interfaces:**
- Consumes: the routes from Tasks 4–5.
- Produces: two live rail tools whose panes hit `/api/research/options/*` and `/api/research/forex/*`, rendered via the existing generic `renderResults`/`detailMarkup`.

- [ ] **Step 1: Fix the API assertion + write it failing first.** In `tests/test_dashboard_research_api.py:213`, change the later-count assertion from `4` to `2` and assert options/forex are no longer `disabled`:

```python
    assert html.count('data-research-later="true"') == 2  # scan, depth only
    assert 'data-research-tool="options"' in html
    assert 'data-research-tool="forex"' in html
```

Run: `pytest tests/test_dashboard_research_api.py -k shell_precedes_guide -q`
Expected: FAIL (still 4 later markers).

- [ ] **Step 2: Promote the buttons** in `web/templates/_research_tab.html` — replace the `{% for %}` block (lines 9-12) so only scan/depth remain "Later", and add real Options/Forex buttons:

```html
    <button type="button" class="research-tool" data-research-tool="options"
            aria-pressed="false">Options</button>
    <button type="button" class="research-tool" data-research-tool="forex"
            aria-pressed="false">Forex</button>
    {% for tool in ('scan', 'depth') %}
    <button type="button" class="research-tool" data-research-tool="{{ tool }}"
            data-research-later="true" disabled>{{ tool|title }} <span>Later</span></button>
    {% endfor %}
```

Run: `pytest tests/test_dashboard_research_api.py -k shell_precedes_guide -q`
Expected: PASS.

- [ ] **Step 3: Write failing JS tests** — append to `web/static/command_center_research.test.js` (mirror the `test()`/`response()` harness style at `:43-55`):

```javascript
  await test('options chain tool fetches the chain endpoint', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, response([{preset: 'momentum'}], 'Presets', {tool: 'presets', provider: 'local'}));
    h.loadProductionScript();
    await h.start();
    h.api.selectTool('options');
    h.fetch.enqueue(200, response(
      [{ticker: 'O:AAPL260320C00250000', strike: 250, mid: 12.2, change_pct: null}],
      'Options chain: AAPL', {tool: 'options_chain', provider: 'massive'}));
    await h.api.run('options', new URLSearchParams({symbol: 'AAPL', view: 'chain'}));
    const url = h.fetch.calls[h.fetch.calls.length - 1].url;
    assert.match(url, /\/api\/research\/options\/chain\?/);
    assert.equal(h.api.state.options.data[0].strike, 250);
  });

  await test('forex snapshot tool dispatches by mode', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, response([{preset: 'x'}], 'Presets', {tool: 'presets', provider: 'local'}));
    h.loadProductionScript();
    await h.start();
    h.api.selectTool('forex');
    h.fetch.enqueue(200, response({pair: 'EUR/USD', bid: 1.08, ask: 1.081},
      'Forex snapshot: EURUSD', {tool: 'forex_snapshot', provider: 'massive'}));
    await h.api.run('forex', new URLSearchParams({mode: 'snapshot', pair: 'EURUSD', source: 'massive'}));
    const url = h.fetch.calls[h.fetch.calls.length - 1].url;
    assert.match(url, /\/api\/research\/forex\/snapshot\?/);
    assert.equal(h.api.state.forex.selected.bid, 1.08);
  });
```

- [ ] **Step 4: Promote in the harness** — `web/static/research_test_harness.js:84`:

```javascript
  const tools = ['ideas', 'movers', 'lookup', 'scan', 'depth', 'options', 'forex']
    .map((tool) => makeElement({tool, disabled: !['ideas', 'movers', 'lookup', 'options', 'forex'].includes(tool)}));
```

- [ ] **Step 5: Run JS tests to verify failure**

Run: `node web/static/command_center_research.test.js`
Expected: FAIL — `options`/`forex` not in `supportedTools`, no forms, `run` returns null.

- [ ] **Step 6: Implement the JS.** In `web/static/command_center_research.js`:

(a) `supportedTools` (line 5):

```javascript
  const supportedTools = new Set(['ideas', 'movers', 'lookup', 'options', 'forex']);
```

(b) `state` (after `lookup`, line ~20):

```javascript
    options: slot(),
    forex: slot(),
```

(c) Add to the `forms` object (line ~184):

```javascript
      options: `<form data-research-form="options" class="param-form research-form">
        <label>Symbol <input name="symbol" class="w-name" required maxlength="32"></label>
        <label>Expiration <select name="expiration" id="research-option-exp"><option value="">Nearest</option></select></label>
        <button type="button" data-research-load-expirations class="w-xs">⟳</button>
        <label>Type <select name="type"><option value="">Any</option><option>call</option><option>put</option></select></label>
        <label>Strike min <input name="strike_min" class="w-sm" type="number" min="0" step="any"></label>
        <label>Strike max <input name="strike_max" class="w-sm" type="number" min="0" step="any"></label>
        <label>View <select name="view"><option value="chain">Chain</option><option value="implied">Implied</option></select></label>
        <button type="submit">Run Options</button>
      </form>`,
      forex: `<form data-research-form="forex" class="param-form research-form">
        <label>Mode <select name="mode"><option>snapshot</option><option>quote</option><option>movers</option><option value="snapshot-all">All</option><option>convert</option></select></label>
        <label>Pair <input name="pair" class="w-name" placeholder="EURUSD"></label>
        <label>From <input name="from" class="w-xs" maxlength="3" placeholder="EUR"></label>
        <label>To <input name="to" class="w-xs" maxlength="3" placeholder="USD"></label>
        <label>Amount <input name="amount" class="w-sm" type="number" min="0" step="any"></label>
        <label>Direction <select name="direction"><option>gainers</option><option>losers</option></select></label>
        <label>Tickers <input name="tickers" class="w-symbols" placeholder="EURUSD GBPUSD"></label>
        <label>Source <select name="source"><option>massive</option><option>ib</option><option>twelvedata</option></select></label>
        <button type="submit">Run Forex</button>
      </form>`,
```

(d) Add `run()` branches (in `run()`, before the final `return request(...)`):

```javascript
    if (tool === 'options') {
      const symbol = params.get('symbol') || '';
      const view = params.get('view') || 'chain';
      if (view === 'implied') {
        const p = new URLSearchParams({symbol, expiration: params.get('expiration') || ''});
        return request(state.options, 'options/implied', p);
      }
      const p = new URLSearchParams({symbol});
      for (const key of ['expiration', 'strike_min', 'strike_max']) {
        if (params.get(key)) p.set(key, params.get(key));
      }
      if (params.get('type')) p.set('type', params.get('type'));
      return request(state.options, 'options/chain', p);
    }
    if (tool === 'forex') {
      const mode = params.get('mode') || 'snapshot';
      const p = new URLSearchParams();
      if (mode === 'snapshot') {
        p.set('pair', params.get('pair') || ''); p.set('source', params.get('source') || 'massive');
      } else if (mode === 'quote') {
        p.set('from', params.get('from') || ''); p.set('to', params.get('to') || '');
        p.set('source', params.get('source') || 'massive');
      } else if (mode === 'movers') {
        p.set('direction', params.get('direction') || 'gainers');
      } else if (mode === 'snapshot-all') {
        (params.get('tickers') || '').split(/[\s,]+/).filter(Boolean)
          .forEach((t) => p.append('tickers', t.toUpperCase()));
      } else if (mode === 'convert') {
        p.set('from', params.get('from') || ''); p.set('to', params.get('to') || '');
        p.set('amount', params.get('amount') || '');
      }
      return request(state.forex, `forex/${mode}`, p);
    }
```

(e) Click-through for options rows → single-contract snapshot. Extend `selectRow(index)` (line ~166) so options fetches the authoritative contract detail:

```javascript
  function selectRow(index) {
    const target = state[currentTool] || (currentTool === 'lookup' ? state.lookup.snapshot : null);
    if (!target || !Array.isArray(target.data)) return false;
    const row = target.data[index];
    if (row === undefined) return false;
    target.selected = row;
    render();
    if (currentTool === 'options' && row && row.ticker) {
      request(state.options, 'options/snapshot',
              new URLSearchParams({option_ticker: row.ticker}));
    }
    return true;
  }
```

(f) Expirations loader — bind the `⟳` button. Add near the other event wiring (`:711-749`), inside the `#research-controls` handler, a click branch:

```javascript
  document.getElementById('research-controls').addEventListener('click', async (event) => {
    const loader = event.target.closest('[data-research-load-expirations]');
    if (!loader) return;
    event.preventDefault();
    const form = loader.closest('form');
    const symbol = form && form.querySelector('[name="symbol"]').value;
    if (!symbol) return;
    const res = await fetch(`/api/research/options/expirations?symbol=${encodeURIComponent(symbol)}`,
                            {credentials: 'same-origin', headers: {Accept: 'application/json'}});
    if (!res.ok) return;
    const body = await res.json();
    const select = document.getElementById('research-option-exp');
    if (select && Array.isArray(body.data)) {
      select.innerHTML = '<option value="">Nearest</option>' + body.data.map((row) =>
        `<option value="${esc(row.expiration)}">${esc(row.expiration)} (${esc(String(row.dte))}d)</option>`).join('');
    }
  });
```

> NOTE: the existing submit handler already delegates on `#research-controls` for `form[data-research-form]` submit; the click handler above is additive and must not intercept submits. Confirm the selectors don't conflict when wiring.

- [ ] **Step 7: Run JS tests to verify pass**

Run: `node web/static/command_center_research.test.js`
Expected: `... N tests passed`, exit 0.

- [ ] **Step 8: Run the Python JS-wrapper + API shell tests**

Run: `pytest tests/test_command_center_research_js.py tests/test_dashboard_research_api.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add web/templates/_research_tab.html web/static/command_center_research.js \
        web/static/research_test_harness.js web/static/command_center_research.test.js \
        tests/test_dashboard_research_api.py
git commit -m "feat(research): activate Options + Forex tab panes"
```

---

### Task 7: SDK anti-drift delegation + docs status bump

**Files:**
- Modify: `trader/sdk.py`
- Modify: `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md`

**Interfaces:** no new public interface — `SDK.options_chain`/`options_snapshot` keep their signatures and return values; internals delegate to `trader.tools.options_data`.

- [ ] **Step 1: Run the existing SDK option coverage to capture a green baseline**

Run: `pytest tests/ -k "option and sdk" -q`
Expected: PASS (record the set; if none exist, note it and rely on `test_options_data.py`).

- [ ] **Step 2: Delegate in `trader/sdk.py`.** Replace the bodies of `_parse_massive_option_ticker`/`_build_massive_option_ticker` (`:3155-3213`) with calls to `trader.tools.options_data.parse_option_ticker`/`build_option_ticker`, and rewrite `options_chain`'s row loop (`:3348-3413`) and `options_snapshot`'s normalization (`:3441-3477`) to call `chain_records(client, ...)` / `contract_snapshot(client, ...)`. Keep the api-key/`RESTClient` construction and the `pd.DataFrame(...)` wrapping (`options_chain` returns a DataFrame — build it from `chain_records`' list):

```python
    def options_chain(self, symbol, expiration=None, contract_type=None,
                      strike_min=None, strike_max=None):
        from trader.tools.chain import get_option_dates
        from trader.tools.options_data import chain_records
        from massive import RESTClient
        cfg = self._container.config()
        api_key = cfg.get('massive_api_key', '')
        if not api_key:
            raise ValueError("massive_api_key not configured in trader.yaml")
        if not expiration:
            dates = get_option_dates(symbol, api_key=api_key)
            if not dates:
                return pd.DataFrame()
            expiration = dates[0]
        rows = chain_records(RESTClient(api_key=api_key), symbol, expiration=expiration,
                             contract_type=contract_type, strike_min=strike_min,
                             strike_max=strike_max)
        return pd.DataFrame(rows)
```

- [ ] **Step 3: Run to verify no behaviour change**

Run: `pytest tests/test_options_data.py tests/ -k "option and sdk" -q`
Expected: PASS.

- [ ] **Step 4: Status bump.** In `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md`, under "Phase 3: options and forex" add: `**Status:** shipped (sub-project A, 2026-07-23); IB-backed forex snapshot/quote included via typed get_snapshot.`

- [ ] **Step 5: Commit**

```bash
git add trader/sdk.py docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md
git commit -m "refactor(options): SDK delegates to shared option helpers; doc status bump"
```

---

## Final verification

- [ ] Run the focused suite:

```bash
pytest tests/test_options_data.py tests/test_massive_research.py \
       tests/test_dashboard_research_service.py tests/test_dashboard_research_api.py \
       tests/test_command_center_research_js.py -q
```
Expected: all PASS.

- [ ] Run the full suite per CLAUDE.md:

```bash
pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py
```
Expected: no new failures.

## Self-review notes

- **Spec coverage:** Options expirations/chain/snapshot/implied → Tasks 2,4,6. Forex snapshot/quote/movers/snapshot-all/convert incl. IB typed path → Tasks 2,5,6. `TRADER_LINK_UNAVAILABLE` → Task 3. No-fabricated-data (greeks/IV only when present, TD notice) → Tasks 1,2. Read-only (no Propose for non-equities) → Task 6 relies on existing `equityResult()`. Anti-drift shared helpers → Tasks 1,7. Scan/Depth untouched → Task 6 leaves them in the `{% for %}` loop and disabled in the harness.
- **Scope note to surface to the user:** the spec's anti-drift goal is fully met for **options** (Task 7 makes the SDK and provider share `options_data.py`). **Forex** normalization is duplicated between the SDK's `forex_*` and the new provider methods — both are thin and low-drift, so forex de-dup is deliberately deferred (YAGNI). Flag this at handoff.
- **Massive SDK attribute risk:** `forex_convert`/`forex_snapshot_all` use `getattr` field names inferred from `sdk.py`; the executor must confirm the real Massive result attributes when wiring Task 2 (the `NS` doubles must mirror them). Called out inline in Task 2.
- **Type consistency:** provider method names (`options_chain`, `forex_snapshot`, …) match across Tasks 2/4/5/6 and the `FakeResearchProvider`; `run(..., backend=...)` matches across Tasks 3/5; wire conId key is `instrument_id` everywhere.
