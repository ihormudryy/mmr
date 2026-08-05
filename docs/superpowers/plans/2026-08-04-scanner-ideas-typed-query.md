# IB Scanner Ideas Typed Query — Implementation Plan (sub-project B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose enriched/scored IB scan results as a typed `42101` query (`scan_ideas`) by running the existing `IBIdeaScanner` pipeline in-process on the trader — no legacy RPC, no subprocess — refactoring `IBIdeaScanner` onto an injected `ScannerDataProvider`.

**Architecture:** Extract `IBIdeaScanner`'s six data calls behind a synchronous `ScannerDataProvider` protocol. `RpcScannerProvider` wraps today's `consume(self._rpc.rpc(return_type=T).method(...))` (CLI path unchanged). `TraderScannerProvider` bridges each call to the trader's async methods via `run_coroutine_threadsafe(trader.<m>(...), trader._main_loop).result()`. A typed `scan_ideas` handler (+ `scanner_locations`) in `cli_surface.py` runs the scanner with the trader-backed provider.

**Tech Stack:** Python 3.12, typed HMAC RPC (`TypedRpcRegistry`, `cli_surface.py`), `ib_async`, pandas, pytest.

## Global Constraints

- **No legacy RPC (42001), no subprocess** on the typed path. The typed handler calls `Trader` methods in-process only.
- **CLI path preserved byte-for-byte:** `MMR.scan_ideas(location=...)` keeps using the RPC client (wrapped in `RpcScannerProvider`) and keeps its `_legacy_or_raise('ideas --location')` gate.
- **Pure refactor of the scanner:** `IBIdeaScanner`'s scoring/indicator/filter/resolution logic is unchanged; only the data-access seam moves behind the provider. No new scan behavior (no IB-level `above_price/volume/market_cap` prefilter).
- **Fail loud:** `IdeaScannerError` → `_DispatchProblem("SCANNER_NO_RESULTS", ...)`; unknown `preset`/`location` → `_DispatchProblem("VALIDATION_ERROR", ...)` before IB work; trader not connected (`_main_loop is None`) → `_DispatchProblem("SCANNER_UNAVAILABLE", ...)`. Never a silent `[]`.
- **`num` bounds:** `ge=1, le=50`.
- **Provider methods are synchronous** and return plain `list`/`list[dict]`/`str` (both impls satisfy the same `Protocol`; no coroutine leaks to `IBIdeaScanner`).
- Spec: `docs/superpowers/specs/2026-08-04-scanner-ideas-typed-query-design.md`.

## File map

| File | Responsibility |
|------|----------------|
| Modify `trader/tools/idea_scanner.py` | Add `ScannerDataProvider` Protocol + `RpcScannerProvider`; refactor `IBIdeaScanner` to take a provider and call `self._provider.X(...)`. |
| Modify `trader/sdk.py` | `MMR.scan_ideas`: `IBIdeaScanner(self._rpc)` → `IBIdeaScanner(RpcScannerProvider(self._rpc))`. |
| Modify `tests/test_idea_scanner.py` | Wrap `mock_rpc` in `RpcScannerProvider` at construction (regression guard). |
| Create `trader/messaging/scanner_bridge.py` | `TraderScannerProvider` (run_coroutine_threadsafe bridge). |
| Create `tests/test_scanner_bridge.py` | Unit test the bridge with a fake async trader + real loop. |
| Modify `trader/messaging/cli_surface.py` | `ScanIdeasRequest`, `scan_ideas` handler, `scanner_locations` handler, registrations. |
| Modify `tests/test_cli_surface.py` (or new `tests/test_scan_ideas_query.py`) | Handler unit tests (stub trader), error mapping. |
| Modify `tests/test_production_rpc_security.py` | Registration-presence + wrong-role for the two new queries. |

---

### Task 1: Provider protocol + `RpcScannerProvider` + `IBIdeaScanner` refactor

**Files:**
- Modify: `trader/tools/idea_scanner.py`
- Modify: `trader/sdk.py`
- Modify: `tests/test_idea_scanner.py`

**Interfaces:**
- Produces:
  - `class ScannerDataProvider(Protocol)` with sync methods: `scanner_data(self, *, scan_code, location_code, num_rows) -> list[dict]`; `get_snapshots_batch(self, contracts, delayed_ok) -> list[dict]`; `get_history_bars(self, contract, duration, bar_size) -> list[dict]`; `resolve_contract(self, partial) -> list`; `get_fundamental_data(self, contract, report_type) -> str`; `get_news_headlines(self, con_id, provider_codes, count) -> list[dict]`.
  - `class RpcScannerProvider` implementing it over an RPC client.
  - `IBIdeaScanner(provider: ScannerDataProvider)` (constructor signature change).

- [x] **Step 1: Write a failing regression test** proving `IBIdeaScanner` works through `RpcScannerProvider` wrapping the existing mock. Add to `tests/test_idea_scanner.py`:

```python
def test_scan_through_rpc_provider(mock_rpc):
    from trader.tools.idea_scanner import IBIdeaScanner, RpcScannerProvider
    rpc_mock = MagicMock()
    mock_rpc.rpc.return_value = rpc_mock
    rpc_mock.scanner_data.return_value = [
        _make_scanner_result('BHP', conId=100),
        _make_scanner_result('CBA', conId=200),
    ]
    rpc_mock.get_snapshots_batch.return_value = [
        _make_ib_snapshot('BHP', conId=100, last=45.0, open_=43.0, high=46.0, low=42.5, volume=2_000_000),
        _make_ib_snapshot('CBA', conId=200, last=100.0, open_=98.0, high=102.0, low=97.0, volume=1_500_000),
    ]
    rpc_mock.get_history_bars.return_value = _make_history_bars(base_price=40.0, num_bars=30)

    scanner = IBIdeaScanner(RpcScannerProvider(mock_rpc))
    df = scanner.scan(preset='momentum', location='STK.AU.ASX', top_n=10)
    assert not df.empty
    assert {'ticker', 'score', 'signal'} <= set(df.columns)
    # provider forwarded the exact scanner_data kwargs
    rpc_mock.scanner_data.assert_called_with(scan_code='TOP_PERC_GAIN', location_code='STK.AU.ASX', num_rows=30)
```

- [x] **Step 2: Run — expect failure**

Run: `.venv/bin/python -m pytest tests/test_idea_scanner.py::test_scan_through_rpc_provider -q`
Expected: FAIL — `RpcScannerProvider` undefined / constructor mismatch.

- [x] **Step 3: Add the protocol + `RpcScannerProvider`** near the top of the `IBIdeaScanner` section in `trader/tools/idea_scanner.py` (import `Protocol` from `typing` at the top of the file if not present):

```python
class ScannerDataProvider(Protocol):
    """Synchronous data access for IBIdeaScanner. Two impls: RPC (CLI/offline)
    and in-process trader-backed (typed query)."""
    def scanner_data(self, *, scan_code: str, location_code: str, num_rows: int) -> list[dict]: ...
    def get_snapshots_batch(self, contracts: list, delayed_ok: bool) -> list[dict]: ...
    def get_history_bars(self, contract, duration: str, bar_size: str) -> list[dict]: ...
    def resolve_contract(self, partial) -> list: ...
    def get_fundamental_data(self, contract, report_type: str) -> str: ...
    def get_news_headlines(self, con_id: int, provider_codes: str, count: int) -> list[dict]: ...


class RpcScannerProvider:
    """ScannerDataProvider backed by the legacy dill RPC client (CLI/offline).
    Wraps exactly the calls IBIdeaScanner made inline, so behavior is unchanged."""
    def __init__(self, rpc_client):
        self._rpc = rpc_client

    def scanner_data(self, *, scan_code, location_code, num_rows):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).scanner_data(
            scan_code=scan_code, location_code=location_code, num_rows=num_rows))

    def get_snapshots_batch(self, contracts, delayed_ok):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_snapshots_batch(contracts, delayed_ok))

    def get_history_bars(self, contract, duration, bar_size):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_history_bars(contract, duration, bar_size))

    def resolve_contract(self, partial):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list).resolve_contract(partial))

    def get_fundamental_data(self, contract, report_type):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=str).get_fundamental_data(contract, report_type))

    def get_news_headlines(self, con_id, provider_codes, count):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_news_headlines(con_id, provider_codes, count))
```

- [x] **Step 4: Refactor `IBIdeaScanner` to use the provider.** In `trader/tools/idea_scanner.py`:

Constructor (line 1074-1075):
```python
    def __init__(self, provider: 'ScannerDataProvider'):
        self._provider = provider
```

Replace the six call sites (delete the local `from ... import consume` at line 1119 and the `consume` params on the three helpers):

- scanner discovery (1154-1160):
```python
            scanner_results = self._provider.scanner_data(
                scan_code=scan_code, location_code=location, num_rows=top_n * 3)
```
- snapshots (1197-1199):
```python
        snapshots = self._provider.get_snapshots_batch(contracts, True)
```
- history (1215-1217, inside `_fetch_one`):
```python
                bars = self._provider.get_history_bars(contract, '60 D', '1 day')
```
- `_resolve_symbols`: drop the `consume` param; call `defs = self._provider.resolve_contract(partial)` (was 1354-1356). Update the caller (1142-1144) to `self._resolve_symbols(symbols_to_resolve, location)`.
- `_fetch_fundamentals`: drop the `consume` param; call `xml_str = self._provider.get_fundamental_data(contract, 'ReportSnapshot')` (was 1500-1502). Update caller (1285-1287) to `self._fetch_fundamentals(contracts, candidates)`.
- `_fetch_news`: drop the `consume` param; call `headlines = self._provider.get_news_headlines(conId, '', 1)` (was 1529-1531). Update caller (1294-1296) to `self._fetch_news(candidates, conid_map)`.

`_build_candidates` (static, 1400-1475) is unchanged.

- [x] **Step 5: Update the CLI construction** in `trader/sdk.py` (~line 4016), keeping the `_legacy_or_raise` gate above it intact:

```python
            from trader.tools.idea_scanner import IBIdeaScanner, RpcScannerProvider
            ...
            scanner = IBIdeaScanner(RpcScannerProvider(self._rpc))
```

- [x] **Step 6: Update the existing tests** in `tests/test_idea_scanner.py` to construct through the provider. The `ib_scanner` fixture (line ~1030) and every `IBIdeaScanner(mock_rpc)` (25 sites) become `IBIdeaScanner(RpcScannerProvider(mock_rpc))`. Update the fixture:

```python
@pytest.fixture
def ib_scanner(mock_rpc):
    from trader.tools.idea_scanner import IBIdeaScanner, RpcScannerProvider
    return IBIdeaScanner(RpcScannerProvider(mock_rpc))
```
and search/replace `IBIdeaScanner(mock_rpc)` → `IBIdeaScanner(RpcScannerProvider(mock_rpc))` in the test bodies. The per-test `rpc_mock.<method>.return_value = ...` stubbing is unchanged (RpcScannerProvider drives the same `.rpc().method()` chain).

- [x] **Step 7: Run the full scanner suite — expect PASS**

Run: `.venv/bin/python -m pytest tests/test_idea_scanner.py -q`
Expected: PASS (existing behavior preserved + the new provider test).

- [x] **Step 8: Commit**

```bash
git add trader/tools/idea_scanner.py trader/sdk.py tests/test_idea_scanner.py
git commit -m "refactor(scanner): IBIdeaScanner takes a ScannerDataProvider; RPC provider preserves CLI path"
```

---

### Task 2: `TraderScannerProvider` (in-process bridge)

**Files:**
- Create: `trader/messaging/scanner_bridge.py`
- Create: `tests/test_scanner_bridge.py`

**Interfaces:**
- Consumes: `ScannerDataProvider` protocol (Task 1); a `Trader`-like object with the six async methods + `_main_loop`.
- Produces: `TraderScannerProvider(trader, *, timeout=90.0)` — a `ScannerDataProvider` that bridges each sync call to `trader.<m>(...)` on `trader._main_loop` via `run_coroutine_threadsafe(...).result(timeout)`; raises `RuntimeError` if `_main_loop` is None/not running.

- [x] **Step 1: Write failing tests** in `tests/test_scanner_bridge.py`:

```python
import asyncio
import threading

import pytest

from trader.messaging.scanner_bridge import TraderScannerProvider


class _FakeTrader:
    def __init__(self, loop):
        self._main_loop = loop
        self.calls = []

    async def scanner_data(self, *, scan_code, location_code, num_rows):
        self.calls.append(('scanner_data', scan_code, location_code, num_rows))
        return [{'symbol': 'BHP', 'conId': 1, 'rank': 1}]

    async def get_snapshots_batch(self, contracts, delayed):
        return [{'symbol': 'BHP'}]

    async def get_history_bars(self, contract, duration, bar_size):
        return [{'close': 1.0}]

    async def resolve_contract(self, partial):
        return [{'conId': 1}]

    async def get_fundamental_data(self, contract, report_type):
        return '<xml/>'

    async def get_news_headlines(self, conId, provider_codes, count):
        return [{'headline': 'x'}]


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_bridge_forwards_and_returns_plain_values(running_loop):
    trader = _FakeTrader(running_loop)
    p = TraderScannerProvider(trader)
    rows = p.scanner_data(scan_code='TOP_PERC_GAIN', location_code='STK.AU.ASX', num_rows=30)
    assert rows == [{'symbol': 'BHP', 'conId': 1, 'rank': 1}]
    assert trader.calls[0] == ('scanner_data', 'TOP_PERC_GAIN', 'STK.AU.ASX', 30)
    assert p.get_fundamental_data(None, 'ReportSnapshot') == '<xml/>'


def test_bridge_raises_when_loop_absent():
    trader = _FakeTrader(None)
    p = TraderScannerProvider(trader)
    with pytest.raises(RuntimeError):
        p.scanner_data(scan_code='X', location_code='Y', num_rows=1)
```

- [x] **Step 2: Run — expect failure**

Run: `.venv/bin/python -m pytest tests/test_scanner_bridge.py -q`
Expected: FAIL — module missing.

- [x] **Step 3: Implement `trader/messaging/scanner_bridge.py`:**

```python
"""In-process ScannerDataProvider: bridges IBIdeaScanner's sync calls to the
trader's async methods via run_coroutine_threadsafe on the trader's main loop."""
from __future__ import annotations

import asyncio
from typing import Any


class TraderScannerProvider:
    def __init__(self, trader: Any, *, timeout: float = 90.0):
        self._trader = trader
        self._timeout = timeout

    def _run(self, coro):
        loop = getattr(self._trader, "_main_loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError(
                "trader is not connected to IB (no running event loop); "
                "cannot run the scanner")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(self._timeout)

    def scanner_data(self, *, scan_code, location_code, num_rows):
        return self._run(self._trader.scanner_data(
            scan_code=scan_code, location_code=location_code, num_rows=num_rows))

    def get_snapshots_batch(self, contracts, delayed_ok):
        return self._run(self._trader.get_snapshots_batch(contracts, delayed_ok))

    def get_history_bars(self, contract, duration, bar_size):
        return self._run(self._trader.get_history_bars(contract, duration, bar_size))

    def resolve_contract(self, partial):
        return self._run(self._trader.resolve_contract(partial))

    def get_fundamental_data(self, contract, report_type):
        return self._run(self._trader.get_fundamental_data(contract, report_type))

    def get_news_headlines(self, con_id, provider_codes, count):
        return self._run(self._trader.get_news_headlines(con_id, provider_codes, count))
```

- [x] **Step 4: Run — expect PASS**

Run: `.venv/bin/python -m pytest tests/test_scanner_bridge.py -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add trader/messaging/scanner_bridge.py tests/test_scanner_bridge.py
git commit -m "feat(scanner): TraderScannerProvider in-process bridge to trader async methods"
```

---

### Task 3: `scan_ideas` typed query (request + handler + registration)

**Files:**
- Modify: `trader/messaging/cli_surface.py`
- Create: `tests/test_scan_ideas_query.py`

**Interfaces:**
- Consumes: `IBIdeaScanner`, `RpcScannerProvider`? No — the handler uses `TraderScannerProvider` (Task 2); `PRESETS` from `idea_scanner`; `api.trader`.
- Produces: query `scan_ideas` (RequestModel `ScanIdeasRequest`, response `dict` `{"rows": [...]}`).

- [x] **Step 1: Write failing handler tests** in `tests/test_scan_ideas_query.py` (mirror `tests/test_manage_surface.py`'s inline-registry idiom; use `execution='inline'` and a stub trader whose async methods return canned scanner/snapshot/history dicts). Cover: happy path returns rows; `IdeaScannerError` → `SCANNER_NO_RESULTS`; unknown preset → `VALIDATION_ERROR`; `_main_loop is None` → `SCANNER_UNAVAILABLE`.

```python
import asyncio, threading
import pytest
from trader.messaging.typed_rpc import TypedRpcRegistry, _DispatchProblem
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.cli_surface import register_cli_surface, ScanIdeasRequest


class _StubTrader:
    def __init__(self, loop, scanner_rows):
        self._main_loop = loop
        self.ib_account = 'DU1'
        self._scanner_rows = scanner_rows

    async def scanner_data(self, *, scan_code, location_code, num_rows):
        return self._scanner_rows

    async def get_snapshots_batch(self, contracts, delayed):
        return [{'symbol': c.symbol, 'conId': getattr(c, 'conId', 0), 'last': 45.0,
                 'open': 43.0, 'high': 46.0, 'low': 42.5, 'volume': 2_000_000,
                 'exchange': 'ASX', 'currency': 'AUD'} for c in contracts]

    async def get_history_bars(self, contract, duration, bar_size):
        return [{'close': 40.0 + i * 0.1, 'volume': 1_000_000} for i in range(30)]


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop); t.join(timeout=2)


def _registry(trader):
    registry = TypedRpcRegistry(default_execution='inline')
    register_cli_surface(registry, TraderServiceApi(trader))
    return registry


def test_scan_ideas_returns_rows(running_loop):
    rows = [{'rank': 1, 'symbol': 'BHP', 'secType': 'STK', 'exchange': 'ASX', 'currency': 'AUD', 'conId': 100},
            {'rank': 2, 'symbol': 'CBA', 'secType': 'STK', 'exchange': 'ASX', 'currency': 'AUD', 'conId': 200}]
    handler = _registry(_StubTrader(running_loop, rows)).resolve('query', 'scan_ideas').handler
    out = handler(ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert isinstance(out['rows'], list) and out['rows']
    assert 'score' in out['rows'][0]


def test_scan_ideas_empty_maps_to_no_results(running_loop):
    handler = _registry(_StubTrader(running_loop, [])).resolve('query', 'scan_ideas').handler
    with pytest.raises(_DispatchProblem) as e:
        handler(ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert e.value.code == 'SCANNER_NO_RESULTS'


def test_scan_ideas_bad_preset_maps_to_validation_error(running_loop):
    handler = _registry(_StubTrader(running_loop, [])).resolve('query', 'scan_ideas').handler
    with pytest.raises(_DispatchProblem) as e:
        handler(ScanIdeasRequest(preset='bogus', location='STK.AU.ASX', num=10))
    assert e.value.code == 'VALIDATION_ERROR'


def test_scan_ideas_unavailable_when_loop_none():
    handler = _registry(_StubTrader(None, [])).resolve('query', 'scan_ideas').handler
    with pytest.raises(_DispatchProblem) as e:
        handler(ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert e.value.code == 'SCANNER_UNAVAILABLE'
```

- [x] **Step 2: Run — expect failure**

Run: `.venv/bin/python -m pytest tests/test_scan_ideas_query.py -q`
Expected: FAIL — `ScanIdeasRequest`/`scan_ideas` undefined.

- [x] **Step 3: Implement** in `trader/messaging/cli_surface.py`. Add the request model near the others (~line 140):

```python
class ScanIdeasRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset: str = 'momentum'
    location: str = 'STK.US.MAJOR'
    num: int = Field(default=15, ge=1, le=50)
    min_price: Optional[float] = Field(default=None, gt=0)
    max_price: Optional[float] = Field(default=None, gt=0)
    min_volume: Optional[int] = Field(default=None, ge=0)
    min_change: Optional[float] = None
    max_change: Optional[float] = None
    fundamentals: bool = False
    news: bool = False
    tickers: list[str] = Field(default_factory=list)
    universe: str = ''
```

Add the handler (sync `def`; runs in the registry's default `thread` execution in production, so the blocking scan is off-loop; the provider bridges to `trader._main_loop`):

```python
def _scan_ideas_handler(api: TraderServiceApi):
    def _handler(parsed: ScanIdeasRequest) -> Dict[str, Any]:
        from trader.messaging.typed_rpc import _DispatchProblem
        from trader.tools.idea_scanner import PRESETS, IBIdeaScanner, IdeaScannerError
        from trader.messaging.scanner_bridge import TraderScannerProvider

        if parsed.preset not in PRESETS:
            raise _DispatchProblem('VALIDATION_ERROR',
                                   f'unknown preset {parsed.preset!r}; available: {", ".join(sorted(PRESETS))}')
        if getattr(api.trader, '_main_loop', None) is None:
            raise _DispatchProblem('SCANNER_UNAVAILABLE',
                                   'trader is not connected to IB Gateway')

        custom_filters = {k: v for k, v in {
            'min_price': parsed.min_price, 'max_price': parsed.max_price,
            'min_volume': parsed.min_volume,
            'min_change_pct': parsed.min_change, 'max_change_pct': parsed.max_change,
        }.items() if v is not None}

        universe_symbols = None
        if parsed.universe.strip():
            from trader.data.universe import UniverseAccessor
            from trader.container import Container
            cfg = Container.instance().config()
            accessor = UniverseAccessor(cfg['duckdb_path'], cfg.get('universe_library', 'Universes'))
            u = accessor.get(parsed.universe.strip())
            universe_symbols = [d.symbol for d in u.security_definitions]

        scanner = IBIdeaScanner(TraderScannerProvider(api.trader))
        try:
            df = scanner.scan(
                preset=parsed.preset, location=parsed.location, top_n=parsed.num,
                custom_filters=custom_filters or None,
                fundamentals=parsed.fundamentals, news=parsed.news,
                tickers=[t.strip().upper() for t in parsed.tickers if t.strip()] or None,
                universe_symbols=universe_symbols)
        except IdeaScannerError as exc:
            raise _DispatchProblem('SCANNER_NO_RESULTS', str(exc)) from exc
        rows = _sanitize_numbers(df.to_dict('records')) if not df.empty else []
        return {'rows': rows}
    return _handler
```

Register in `register_cli_surface` (near `get_market_depth`):
```python
    registry.register('query', 'scan_ideas', ScanIdeasRequest, dict, _scan_ideas_handler(api))
```

> NOTE: confirm `_sanitize_numbers` exists in `cli_surface.py` (it's used by `get_market_depth`); if it only handles the depth shape, use it as-is on the records list — it recurses over dicts/lists. If absent, add a small numeric sanitizer (NaN/inf/numpy → JSON-safe) mirroring `web/.../massive_research.json_clean`.

- [x] **Step 4: Run — expect PASS**

Run: `.venv/bin/python -m pytest tests/test_scan_ideas_query.py -q`
Expected: PASS (4 tests).

- [x] **Step 5: Commit**

```bash
git add trader/messaging/cli_surface.py tests/test_scan_ideas_query.py
git commit -m "feat(scanner): scan_ideas typed query (enriched IB scan, fail-loud mapping)"
```

---

### Task 4: `scanner_locations` companion query

**Files:**
- Modify: `trader/messaging/cli_surface.py`
- Modify: `tests/test_scan_ideas_query.py`

**Interfaces:**
- Produces: query `scanner_locations` → `{"locations": list[dict]}` from `trader.scanner_locations()` (async; bridge via the trader loop).

- [x] **Step 1: Write failing test** (append to `tests/test_scan_ideas_query.py`):

```python
def test_scanner_locations_returns_list(running_loop):
    class _LocTrader(_StubTrader):
        async def scanner_locations(self):
            return [{'code': 'STK.US.MAJOR', 'name': 'US Major', 'instrument_types': ['STK']}]
    handler = _registry(_LocTrader(running_loop, [])).resolve('query', 'scanner_locations').handler
    out = handler({})
    assert out['locations'][0]['code'] == 'STK.US.MAJOR'
```

- [x] **Step 2: Run — expect failure** (`scanner_locations` unregistered).

Run: `.venv/bin/python -m pytest tests/test_scan_ideas_query.py::test_scanner_locations_returns_list -q`
Expected: FAIL.

- [x] **Step 3: Implement** in `cli_surface.py`:

```python
def _scanner_locations_handler(api: TraderServiceApi):
    def _handler(_body: Dict[str, Any]) -> Dict[str, Any]:
        from trader.messaging.typed_rpc import _DispatchProblem
        import asyncio
        loop = getattr(api.trader, '_main_loop', None)
        if loop is None or not loop.is_running():
            raise _DispatchProblem('SCANNER_UNAVAILABLE', 'trader is not connected to IB Gateway')
        locations = asyncio.run_coroutine_threadsafe(api.trader.scanner_locations(), loop).result(30)
        return {'locations': list(locations or [])}
    return _handler
```
Register:
```python
    registry.register('query', 'scanner_locations', dict, dict, _scanner_locations_handler(api))
```

- [x] **Step 4: Run — expect PASS**

Run: `.venv/bin/python -m pytest tests/test_scan_ideas_query.py -q`
Expected: PASS (5 tests).

- [x] **Step 5: Commit**

```bash
git add trader/messaging/cli_surface.py tests/test_scan_ideas_query.py
git commit -m "feat(scanner): scanner_locations companion query"
```

---

### Task 5: Registration/security tests + full regression

**Files:**
- Modify: `tests/test_production_rpc_security.py`

- [x] **Step 1: Add the two queries** to the registration-presence parametrized list and assert wrong-role rejection. Find the parametrized presence test (~`test_production_rpc_security.py:192`) and append `"scan_ideas"` and `"scanner_locations"` to the query-name list:

```python
@pytest.mark.parametrize("method", [
    ..., "scan_ideas", "scanner_locations",
])
def test_query_registered(production_registry, method):
    assert production_registry.contains("query", method)
```

(If `_FakeTrader` in that file lacks `scanner_data`/`scanner_locations`, no change is needed for a pure `contains(...)` presence check — it doesn't invoke the handler. Only add stub methods if you also add a behavioral/e2e case.)

- [x] **Step 2: Run the security + surface suites**

Run: `.venv/bin/python -m pytest tests/test_production_rpc_security.py tests/test_scan_ideas_query.py tests/test_scanner_bridge.py -q`
Expected: PASS.

- [x] **Step 3: Full regression**

Run: `.venv/bin/python -m pytest tests/test_idea_scanner.py tests/test_scan_ideas_query.py tests/test_scanner_bridge.py tests/test_production_rpc_security.py -q`
Expected: PASS.

- [x] **Step 4: Commit**

```bash
git add tests/test_production_rpc_security.py
git commit -m "test(scanner): register scan_ideas/scanner_locations in production RPC security suite"
```

---

## Final verification

- [x] `.venv/bin/python -m pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -k "scanner or idea_scanner or cli_surface or production_rpc"` → PASS.
- [x] `.venv/bin/python -m pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py` → no new failures vs. baseline.

## Self-review notes

- **Spec coverage:** provider refactor (Task 1); RpcScannerProvider preserves CLI (Task 1, Steps 5-7); TraderScannerProvider bridge (Task 2); `scan_ideas` typed query + fail-loud mapping (Task 3); `scanner_locations` (Task 4); registration/role (Task 5). The spec's `above_price/above_volume/market_cap_above`/`scan_code`-override request fields are intentionally dropped — `IBIdeaScanner.scan()` does not accept them and adding IB-level prefilter would change scanner behavior (out of scope for a pure refactor). Flag this trim at handoff.
- **Execution model:** the `scan_ideas` handler is a sync `def`; in production the registry's `default_execution="thread"` runs it off the ROUTER loop, and `TraderScannerProvider` bridges every call (including `IBIdeaScanner`'s parallel history pool) back to `trader._main_loop`. Unit tests use `execution='inline'` + a loop running in a background thread so the bridge is exercised.
- **Type consistency:** provider method names/signatures match across the protocol, `RpcScannerProvider`, `TraderScannerProvider`, and the `IBIdeaScanner` call sites; `_main_loop` guard consistent between the bridge, the `scan_ideas` handler, and `scanner_locations`.
- **Risk:** Task 1 touches shared scanner code + 25 tests; the regression guard is that the existing tests pass unchanged (only the construction wrapper changes). `_sanitize_numbers` reuse is flagged for confirmation in Task 3.

---

## Execution notes (2026-08-05)

Implemented in `59938dc` (Task 1), `e43364f` (Task 2), `bf63ece` (Tasks 3+4),
`d6e7bbd` (Task 5). Deviations from the plan as written:

1. **Task 1 Step 6 was not just a construction wrapper.** Four tests in
   `TestLocationExchangeResolution` injected their fake contract definitions by
   passing `consume=lambda _x: [...]` straight into `_resolve_symbols`. With
   `consume` gone from the signature, the equivalent seam is the provider's
   `resolve_contract`, so those four now stub
   `mock_rpc.rpc.return_value.resolve_contract` (`return_value` where the fake
   `consume` supplied defs, `side_effect` where the test also captures the
   partial contract's exchange). Same defs reach the same code path; intent
   preserved.

2. **An empty DataFrame is `SCANNER_NO_RESULTS`, not `{"rows": []}`.** The
   plan's draft handler returned an empty list when `df.empty`, which
   contradicts its own Global Constraint ("Never a silent `[]`"). The scanner
   returns an empty frame when every candidate is filtered out or none can be
   built — indistinguishable, to a caller, from "the scan didn't run". It now
   raises `SCANNER_NO_RESULTS` naming the preset and location.

3. **`_sanitize_numbers` reuse confirmed** (the Task 3 NOTE asked for this): it
   exists at `cli_surface.py:50` and recurses over dicts/lists, so it is
   applied to the records list as-is. It is *load-bearing* here, not
   belt-and-braces: when one candidate can compute an indicator and another
   cannot, pandas produces a float64 `NaN`, and `canonical_json`
   (`allow_nan=False`) refuses to sign it. Verified by constructing that case
   and watching the raw frame fail to encode; pinned by
   `test_scan_ideas_rows_are_json_wire_safe`. No numpy-scalar coercion was
   added — this pandas version's `to_dict('records')` already yields Python
   natives, so it would have been speculative.

4. **Still trimmed, as the plan intended:** the spec's IB-level
   `above_price`/`above_volume`/`market_cap_above` prefilter and `scan_code`
   override are not exposed — `IBIdeaScanner.scan()` takes none of them and
   adding them would change scan behaviour beyond a refactor.

5. **Tests beyond the plan's list** (21 new overall): the bridge gained
   forward-every-method, loop-stopped-mid-scan, and
   exception-propagation cases; `scan_ideas` gained wire-safety,
   validation-precedes-IB-work, `num` bounds, custom-filter forwarding, and a
   `scanner_locations` unavailable case; the security suite asserts the two
   queries are absent from the *command* socket and that `scan_ideas` resolves
   to `thread` execution.

### Pre-existing issues found while establishing a baseline (not caused by this work)

- `tests/test_watchlist_session_auth.py` **hangs the full suite** at ~96%.
  `watchlist_create` reaches a real ZMQ `socket.send` with no server bound, and
  because it blocks in C, `--timeout=30` can only dump stacks — the run then
  wedges and never reports. Two different tests in that file hang depending on
  ordering (`test_watchlist_create_survives_commands_enabled` hangs even in
  isolation), which points at cross-test pollution leaving the manage client
  pointed at a live endpoint. Baselines here were taken with that file ignored.
- `tests/test_user_guide.py::test_guide_template_has_info_bubbles_and_sections`
  fails at `5c7a93a` and is fixed by the (separate, uncommitted) heading rename
  in `web/templates/_guide_tab.html`.

### Verification

Baseline at `5c7a93a` (ignoring `test_ibrx_async.py` + `test_watchlist_session_auth.py`):
`1 failed, 3930 passed, 3 skipped`. After this work: **`3952 passed, 3 skipped`,
0 failed** — +22 = 21 new tests plus the `test_user_guide` failure the guide-tab
edit resolves. No regressions.
