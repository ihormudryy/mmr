# IB Scanner Ideas — Typed Query Design (sub-project B)

**Date:** 2026-08-04
**Status:** approved (design); awaiting implementation plan
**Sub-project:** B of 3 (parent: activate the four "Later" Research-tab tools)
**Depends on:** typed HMAC RPC query surface (`42101`, `cli_surface.py`), `IBIdeaScanner` + shared scoring (`trader/tools/idea_scanner.py`), the trader's live IB connection (`IBAIORx`)
**Unblocks:** sub-project C's **Scan** research tool
**Parent constraint:** `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md` — *"Scan is exposed only after `scanner_data` has an equivalent typed query contract. The old legacy RPC and CLI subprocesses are not acceptable fallbacks."*

## Problem

The IB idea scanner (ranked discovery → per-symbol snapshot + history → local RSI/EMA/SMA → preset scoring, optional fundamentals/news) exists as `IBIdeaScanner`, but it drives every data call over the **legacy dill RPC client** (`consume(self._rpc.rpc(return_type=T).method(...))`), which is unbound in split-container production. So the dashboard **Scan** tool has no non-legacy path to enriched IB scan results.

## Decision

Expose **enriched, scored** IB scan results as a typed query by running the *same* `IBIdeaScanner` pipeline **inside the trader process** against the live IB connection — no legacy RPC, no subprocess. Achieve this by refactoring `IBIdeaScanner` to depend on an injected **data-provider protocol** with two implementations: the existing RPC-backed one (CLI/offline, behavior unchanged) and a new in-process trader-backed one (the typed query).

### Non-goals
- Raw-scanner-only output (the enriched pipeline is the point — decided).
- A job/polling API for the long-running scan (synchronous request/reply with generous timeouts; see §5).
- Changing the CLI `scan` / `ideas --location` UX or output.
- Building the Scan **UI** — that's sub-project C.
- Async streaming of partial results.

## Architecture

### Provider-protocol refactor

`IBIdeaScanner.scan()` makes exactly six data calls, each currently `consume(self._rpc.rpc(return_type=T).X(...))`. Extract them behind a small **synchronous** protocol (scan() stays synchronous and keeps its own `ThreadPoolExecutor` for parallel history — unchanged):

```python
class ScannerDataProvider(Protocol):
    def scanner_data(self, *, scan_code: str, location_code: str, num_rows: int) -> list[dict]: ...
    def get_snapshots_batch(self, contracts: list, delayed_ok: bool) -> list[dict]: ...
    def get_history_bars(self, contract, duration: str, bar_size: str) -> list[dict]: ...
    def resolve_contract(self, partial) -> list: ...
    def get_fundamental_data(self, contract, report_type: str) -> str: ...
    def get_news_headlines(self, con_id: int, provider_codes: str, count: int) -> list[dict]: ...
```

Three pieces:

1. **`IBIdeaScanner(provider: ScannerDataProvider)`** — constructor takes a provider instead of `rpc_client`; `scan()` / `_resolve_symbols` / `_fetch_fundamentals` / `_fetch_news` call `self._provider.method(...)` instead of `consume(self._rpc.rpc(return_type=T).method(...))`. No other logic changes. The `consume` / `return_type` plumbing moves *into* the RPC provider (below).

2. **`RpcScannerProvider(rpc_client)`** — wraps today's exact calls: `consume(rpc_client.rpc(return_type=list[dict]).scanner_data(...))`, etc. This is what the CLI (`MMR.scan_ideas(location=...)`) constructs, so the legacy/offline path is **byte-for-byte unchanged**.

3. **`TraderScannerProvider(api, loop)`** (new, in-process) — each method bridges the sync call to the trader's async method via `asyncio.run_coroutine_threadsafe(api.<m>(...), loop).result(timeout)`, where `loop` is the trader's captured event loop (the same one already used for off-loop PnL routing) and `api` is the `TraderServiceApi` the typed surface already builds. The six underlying methods (`scanner_data`, `get_snapshots_batch`, `get_history_bars`, `resolve_contract`, `get_fundamental_data`, `get_news_headlines`) already exist as async methods on `TraderServiceApi`/`Trader`.

This keeps the scoring/indicator/filter logic single-sourced; only the data-access seam is abstracted.

### Data flow (typed query)

```
scan_ideas query (42101)
  → handler (cli_surface.py), execution="thread"
      → IBIdeaScanner(TraderScannerProvider(api, trader_loop)).scan(preset, location, num, filters, ...)
          → provider.scanner_data / resolve_contract   (discover)
          → provider.get_snapshots_batch               (price)
          → provider.get_history_bars (parallel pool)  (indicators)
          → local RSI/EMA/SMA + preset score + filter + top_n
          → provider.get_fundamental_data / get_news_headlines (optional)
      → to_dataframe(...).to_dict("records")
  → {"rows": [...]}
```

## Typed query contract

Added in `trader/messaging/cli_surface.py` (request model + async handler closure + one `registry.register('query', ...)` line; `register_cli_surface` is already called from `build_production_registry`, so no `production_api.py` change).

### `scan_ideas`

Request (`ConfigDict(extra='forbid')`):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `preset` | str | `"momentum"` | Must be in `PRESETS`; else `VALIDATION_ERROR` |
| `location` | str | `"STK.US.MAJOR"` | Validated (see §fail-loud) |
| `num` | int | 15 | `ge=1, le=50` (bounds IB round-trips) |
| `scan_code` | str \| None | None | Optional raw IB scan-code override |
| `above_price` | float | 0.0 | `ge=0` |
| `above_volume` | int | 0 | `ge=0` |
| `market_cap_above` | float | 0.0 | `ge=0` |
| `min_price` / `max_price` | float \| None | None | filter overrides |
| `min_volume` | int \| None | None | |
| `min_change` / `max_change` | float \| None | None | |
| `tickers` | list[str] | `[]` | explicit symbols → resolve path (bypasses scanner) |
| `universe` | str | `""` | universe name → symbols → resolve path |
| `fundamentals` | bool | False | IB `reqFundamentalData` enrichment (slower) |
| `news` | bool | False | IB `reqHistoricalNews` enrichment (slower) |

`tickers` and `universe` are mutually exclusive with each other; when either is set, scanner discovery is bypassed (mirrors `IBIdeaScanner.scan`). The handler loads the universe's symbols (via `UniverseAccessor`) before calling `scan(universe_symbols=...)`.

Response: `ScanIdeasResponse(rows: list[dict])` (`.model_dump()`), where each row is the enriched/scored record `to_dataframe` produces — same field shape the Massive Ideas tool returns (ticker, price/close, change_pct, rsi/ema/sma per preset, score, signal, plus fundamentals/news fields when requested). `list[dict]` pass-through (matches `DiscoverInstrumentResponse`), since the row schema varies by preset/flags.

### `scanner_locations` (companion)

Request: `{}`. Response: `{locations: list[dict]}` of `{code, name, instrument_types}` from `Trader.scanner_locations()`. Lets the Scan tool populate a location picker instead of hardcoding. Cheap, read-only, `execution="thread"`.

## Long-running execution & pacing

- Both queries register with the production registry's default `execution="thread"`, so the 30–90s sweep runs off the ROUTER event loop (`asyncio.to_thread(handler, body)`); `TraderScannerProvider` bridges its per-symbol calls back onto the trader loop via `run_coroutine_threadsafe`.
- `num ≤ 50` caps IB round-trips (discovery pulls `num*3`, history fetches `min(len, num*2)`), bounding wall-clock and pacing pressure.
- The client (sub-project C's research service) gives Scan a large per-tool timeout (~120s); the `TypedRpcClient` call timeout must exceed the worst-case sweep.
- **Operational note:** the scan shares the trader's single live IB connection with live/paper trading, so a large sweep competes for IB pacing. The `num` cap is the mitigation; heavy concurrent scanning is out of scope. The registry's in-flight cap (`SERVER_BUSY`) naturally throttles concurrent scans — acceptable for a research tool.

## Fail-loud behavior

- `IBIdeaScanner` already raises `IdeaScannerError` on empty scanner discovery, unresolved explicit tickers, or all-history-fail. The handler catches it and raises `_DispatchProblem("SCANNER_NO_RESULTS", <message>)` so the client gets a structured, actionable error (not a silent empty list — the IB error-162 gap).
- Unknown `preset` (not in `PRESETS`) or an invalid/empty `location` → `_DispatchProblem("VALIDATION_ERROR", ...)` **before** any IB work (so a typo'd location fails loudly instead of returning `[]`). Location is validated against the known set (`_LOCATION_EXCHANGE` keys / `scanner_locations`).
- Trading-filter-blocked location → the scanner already returns empty; the handler maps that to `_DispatchProblem("FILTER_BLOCKED", ...)` (distinct from no-results) when the filter is the cause.
- Any unmapped exception falls through to the transport's scrubbed `INTERNAL_ERROR` (detail server-logged only) — so every *expected* failure gets an explicit code.

## Testing

- **Scanner refactor (regression-critical):** unit tests with a **fake `ScannerDataProvider`** feeding canned scanner/snapshot/history/fundamentals/news dicts; assert the enriched/scored output (scores, indicators, filtering, top_n, `IdeaScannerError` on empty/all-fail) is identical to today's behavior. This is what proves the CLI path didn't regress. Include the existing edge cases (explicit-tickers path skips `min_change_pct`, exchange-hint derivation, currency/primary validation in `_resolve_symbols`).
- **`RpcScannerProvider`:** a thin test that it calls `consume(rpc.rpc(return_type=T).method(...))` with the right args (so the CLI wiring is preserved).
- **Handler:** unit test resolving `scan_ideas`/`scanner_locations` from a registry built with a **stub trader** (`SimpleNamespace`-style, mirroring `test_manage_surface.py`); assert rows come back and `IdeaScannerError` → `SCANNER_NO_RESULTS`, bad preset/location → `VALIDATION_ERROR`. Use an `inline`-execution registry for the unit test to avoid the thread bridge; the `TraderScannerProvider` loop-bridge gets its own focused test with a real loop + a fake async api.
- **Registration/security:** append `scan_ideas` + `scanner_locations` to the presence + wrong-role (`METHOD_NOT_ALLOWED`) parametrized lists in `tests/test_production_rpc_security.py`.

## Success criteria

1. `IBIdeaScanner` runs unchanged in behavior via `RpcScannerProvider`; the CLI `ideas --location …` path is unaffected (existing tests green).
2. `scan_ideas` returns enriched/scored rows over typed `42101` with **no** legacy RPC and **no** subprocess, driven by `TraderScannerProvider` in-process.
3. Empty/failed scans and bad params surface as explicit `_DispatchProblem` codes, never a silent `[]`.
4. `scanner_locations` returns the location list for the Scan tool's picker.
5. Both queries are registered on the query role only and reject wrong-role calls.

## Implementation order

1. Define `ScannerDataProvider` + refactor `IBIdeaScanner` to use it; add `RpcScannerProvider`; port the CLI construction (`MMR.scan_ideas`) to build `IBIdeaScanner(RpcScannerProvider(self._rpc))`. Fake-provider regression tests.
2. `TraderScannerProvider` (run_coroutine_threadsafe bridge) + its focused loop test.
3. `scan_ideas` request model + handler + registration in `cli_surface.py`; handler tests (stub trader).
4. `scanner_locations` request/handler/registration.
5. Fail-loud error mapping + validation; error-path tests.
6. `test_production_rpc_security.py` registration/role additions; full-suite regression.

## Open points (resolved in this design)

| Topic | Choice |
|-------|--------|
| Scope | Enriched/scored server-side (full `IBIdeaScanner` pipeline) |
| Wiring | Provider-protocol refactor (RPC + trader-backed impls) |
| Query name | `scan_ideas` (enriched) + `scanner_locations` (companion) |
| Transport | typed query `42101`, `execution="thread"`, sync request/reply |
| Long-running | generous timeouts + `num≤50` cap; shares live IB connection (documented) |
| Empty/162 | explicit `SCANNER_NO_RESULTS` / `VALIDATION_ERROR`, never silent `[]` |
| CLI compatibility | `RpcScannerProvider` preserves current behavior byte-for-byte |
