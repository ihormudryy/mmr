# Research Tab — Options + Forex (Phase 3) Design

**Date:** 2026-07-23
**Status:** approved (design); awaiting implementation plan
**Sub-project:** A of 3 (see build order below)
**Depends on:** Research tab Phase 1 shell (`web/command_center/research.py`, `routes_research.py`, `command_center_research.js`), Massive REST key, dashboard typed query link (`TraderLink` on `42101`)
**Parent:** `docs/superpowers/specs/2026-07-22-dashboard-research-tab-design.md` (this is that doc's "Phase 3: options and forex", brought forward with IB-backed forex included)

## Problem

The Research tab shipped Phase 1 (Ideas, Movers, Lookup Snapshot, Lookup News) with four inert, visibly-labelled **"Later"** placeholders in the tool rail: **Scan, Depth, Options, Forex**. This design activates **Options** and **Forex**. Scan and Depth stay "Later" — they are typed-trader-backed and covered by sibling sub-projects.

### Build order (parent effort)

| # | Sub-project | Backend | Depends on |
|---|-------------|---------|-----------|
| **A** (this doc) | Options + Forex | Massive (local to web process) + IB forex via typed `get_snapshot` | — |
| B | `scanner_data` typed query | IB scanner → new typed `42101` contract | — |
| C | Depth + Scan | typed `get_market_depth` + `scanner_data` | B (for Scan) |

## Decision

Activate the Options and Forex rail entries as read-only research tools, reusing the existing `ResearchService` execution path (bounded thread pool, per-tool timeouts, busy-slot admission, credential-safe logging) and the uniform `{data, title, meta}` / `{error}` response envelope. Two backends feed that envelope:

- **Massive path** — Options (expirations, chain, snapshot, implied) and Forex (snapshot, quote, movers, snapshot-all, convert). Extend `MassiveResearch` with new methods; reuse the SDK's existing Massive option/forex logic.
- **Typed path** — IB-backed Forex snapshot/quote, via the dashboard's existing typed query link (`42101`) and typed contract resolution on a CASH/IDEALPRO contract. No new socket. **No legacy `42001`.**

### Non-goals

- Order entry of any kind (options buy/sell, forex trading). Read-only.
- Legacy dill RPC (`42001`) or CLI-subprocess fallbacks.
- Scan and Depth tools (sub-projects B/C).
- Background refresh, result persistence, scheduled scans, research alerts.
- Options implied-vol *surface* modelling beyond what the existing implied-distribution helper already computes.

## Architecture

### Two backends, one envelope

`ResearchService.run(tool, operation, *, log_params)` today hard-injects the singleton `MassiveResearch` provider: `operation: Callable[[MassiveResearch], ResearchResult]`. Generalize so an operation can be backed by **either** the Massive provider **or** an injected typed-query caller, while `run()` keeps sole ownership of the executor, per-tool timeout, slot admission, and error mapping.

Concrete shape:

- `ResearchService` gains an optional `trader_query: Callable[[str, dict], dict] | None` dependency (a thin wrapper over the existing dashboard query `TraderLink.call`). It is **not** a new socket — production wiring passes the same query link `routes_read` already uses.
- The Massive provider stays lazily built via `provider_factory` (unchanged; still yields `MASSIVE_NOT_CONFIGURED` (503) when no key).
- Massive-backed operations are `Callable[[MassiveResearch], ResearchResult]` as today.
- IB-forex operations are `Callable[[TraderQuery], ResearchResult]`, where `TraderQuery` is the injected typed caller. `run()` selects which backend to hand the operation based on an explicit `backend: Literal["massive", "trader"]` argument (default `"massive"`, preserving current call sites).
- If `backend="trader"` and no `trader_query` is wired (or the link raises a transport error), `run()` raises `ResearchError(503, "TRADER_LINK_UNAVAILABLE", ..., retryable=True)`.

This is the single point where the research subsystem gains a typed dependency. The invariant "research uses no **legacy** RPC and no subprocess" holds (typed ≠ legacy). Any Phase-1 test asserting research holds *no typed client at all* is updated to permit the injected query link on the `backend="trader"` path only.

### Provider extensions (`MassiveResearch`)

New methods, each returning the standard `ResearchResult(data, title, provider, notice)`:

- `options_expirations(symbol) -> ResearchResult`
- `options_chain(symbol, *, expiration, contract_type, strike_min, strike_max) -> ResearchResult`
- `options_snapshot(option_ticker) -> ResearchResult`
- `options_implied(symbol, *, expiration) -> ResearchResult`
- `forex_snapshot(pair, *, source) -> ResearchResult` — implements `source` `"massive"` and `"twelvedata"` only; the route dispatches `source="ib"` to the typed path **before** reaching the provider, so this method never sees `"ib"` (it raises `ValueError` if it does — defensive, not a reachable path).
- `forex_quote(from_ccy, to_ccy, *, source) -> ResearchResult` — same `massive`/`twelvedata`-only contract as `forex_snapshot`.
- `forex_movers(direction) -> ResearchResult` (Massive-only)
- `forex_snapshot_all(tickers) -> ResearchResult` (Massive-only)
- `forex_convert(from_ccy, to_ccy, amount) -> ResearchResult` (Massive-only)

Shared logic (`trader.tools.chain.get_option_dates`, the chain/implied builders, `_build_massive_option_ticker` / `_parse_massive_option_ticker`, forex pair parsing) is **extracted into importable helpers** and reused by both the SDK CLI methods and these provider methods, so the tab and the CLI cannot drift. No fabricated columns: greeks/IV appear only when the Massive payload carries them; TwelveData quote emits a `notice` that bid/ask are unavailable on its REST endpoint.

### IB-forex typed path

`forex snapshot`/`quote` with `source="ib"` run as `backend="trader"` operations:

1. Resolve the pair to a CASH/IDEALPRO contract via the typed discover/resolve query (same path `routes_read` uses).
2. Call typed `get_snapshot` with `{instrument_id, delayed: false}`.
3. Normalize to the same result shape as the Massive branch.

Trader link down, contract unresolved, or market-data not subscribed → `TRADER_LINK_UNAVAILABLE` (never a fabricated quote — fail loudly).

## Routes

All new routes live in `create_research_router` (`web/command_center/routes_research.py`), are `GET`, session-gated via `require_session`, carry a `reject_unknown` allowlist, and return the standard envelope through `run(...)`. No CSRF (reads).

### Options

| Route | Query params | Validation |
|-------|--------------|-----------|
| `GET /api/research/options/expirations` | `symbol` | `^[A-Za-z][A-Za-z0-9.\-]*$`, ≤32 |
| `GET /api/research/options/chain` | `symbol`, `expiration?`, `type?`, `strike_min?`, `strike_max?` | `expiration` strict `YYYY-MM-DD`; `type` `Literal["call","put"]`; strikes `>0`; `strike_max ≥ strike_min` cross-check |
| `GET /api/research/options/snapshot` | `option_ticker` | OCC/Massive `O:` shape, e.g. `^O:[A-Z0-9]+$` |
| `GET /api/research/options/implied` | `symbol`, `expiration` | `expiration` required, strict `YYYY-MM-DD` |

### Forex

| Route | Query params | Validation |
|-------|--------------|-----------|
| `GET /api/research/forex/snapshot` | `pair`, `source?` | `pair` normalized from `EURUSD`/`EUR/USD`/`C:EURUSD`; `source` `Literal["massive","ib","twelvedata"]` default `massive` |
| `GET /api/research/forex/quote` | `from`, `to`, `source?` | `from`/`to` `^[A-Za-z]{3}$`; `source` as above |
| `GET /api/research/forex/movers` | `direction` | `Literal["gainers","losers"]` default `gainers` |
| `GET /api/research/forex/snapshot-all` | `tickers?` | list, ≤ 100 entries; each `^C?:?[A-Za-z]{6}$` after normalization |
| `GET /api/research/forex/convert` | `from`, `to`, `amount` | currencies as above; `amount` `>0`, `≤ 1e12` |

`source` is accepted only on `snapshot`/`quote`; supplying it on a Massive-only route is a validation error. New per-tool timeout keys (`options_expirations`, `options_chain`, `options_snapshot`, `options_implied`, `forex_snapshot`, `forex_quote`, `forex_movers`, `forex_snapshot_all`, `forex_convert`) are added to `DEFAULT_TIMEOUTS`.

## Frontend (`command_center_research.js` + `_research_tab.html`)

The Options and Forex rail entries lose their disabled "Later" marker and become active tools. Scan and Depth keep theirs.

**Options pane — drill-down** (one rail entry):
1. `symbol` input → **Load expirations** populates an expiration `<select>` (each labelled with DTE).
2. Optional `type` + `strike_min`/`strike_max` → **Load chain** renders the chain table.
3. Click a chain row → contract **snapshot** detail; an **Implied distribution** action for the selected expiration renders that view.

**Forex pane — operation selector** (one rail entry): a mode switch (Snapshot · Quote · Movers · All · Convert) swaps the control set; a `source` `<select>` shows only for Snapshot/Quote. Convert shows the converted amount and the rate used.

Both reuse the existing per-tool session state, loading/empty/error states, CLI-shaped result-table rendering, and last-successful-result preservation. No propose/order affordance in either pane.

## Error handling

Reused unchanged: `MASSIVE_NOT_CONFIGURED` (503), `RESEARCH_NOT_ENTITLED` (Massive options/forex plan gate), `RESEARCH_RATE_LIMITED`, `RESEARCH_TIMEOUT` (504), `RESEARCH_BUSY` (503), `RESEARCH_UPSTREAM_ERROR` (502).

New: `TRADER_LINK_UNAVAILABLE` (503, retryable) — IB-forex path when the typed query link is down, the contract can't be resolved, or the forex market-data subscription is missing.

## Testing and verification

Mirrors the parent doc's Phase-1 matrix.

**Provider/helper (doubles, no network):**
- option-ticker parse/build round-trip; chain/expirations/implied normalization; empty provider data; greeks/IV present only when payload carries them.
- forex pair + currency normalization; `snapshot-all` ticker normalization; TwelveData no-bid/ask notice; NaN/infinity → null cleaning.

**Route/service:**
- session enforcement on every new route (401/403 without cookie).
- query defaults, bounds, enums, and incompatible params (`strike_min > strike_max`; `source` on a Massive-only route; malformed `pair`/currency; `amount ≤ 0`).
- successful CLI-shaped envelope for every operation.
- `503` missing key, `502` upstream, `504` timeout, `503` busy, and the new `TRADER_LINK_UNAVAILABLE` envelope.
- IB-forex path calls the **injected typed query link** (mock) and asserts **no** legacy RPC and **no** subprocess.
- admission slots remain occupied until timed-out work exits (existing invariant, re-checked with the new tools).

**UI:**
- Options + Forex rail entries active (not "Later"); Scan + Depth still disabled.
- Options drill-down state (expirations → chain → snapshot/implied); Forex mode + source selector.
- loading/empty/error states; last-successful-result preservation.
- read-only: no propose/order controls in either pane.

Run focused research route/service tests + the UI marker tests, then the full suite with `--timeout-method=thread`. Release check: Trading SSE stays responsive while all research workers are occupied (existing check, re-verified).

## Success criteria

1. Options tool: expirations → chain → contract snapshot → implied distribution all render CLI-shaped data from Massive; entitlement gaps surface as `RESEARCH_NOT_ENTITLED`, never a 5xx or fabricated data.
2. Forex tool: snapshot/quote across `massive`/`ib`/`twelvedata`; movers/snapshot-all/convert via Massive. IB source with the trader down yields `TRADER_LINK_UNAVAILABLE`, not a fake quote.
3. No legacy RPC, no subprocess, no order entry on any new path.
4. Scan and Depth remain inert "Later" entries, untouched.

## Implementation order

1. Extract shared Massive option/forex helpers; add `MassiveResearch` provider methods (+ helper unit tests).
2. Generalize `ResearchService.run` for the `backend` selector + `trader_query` dependency + `TRADER_LINK_UNAVAILABLE`; wire the injected query link in production.
3. Add validated Options routes; service tests.
4. Add validated Forex routes (incl. IB typed path); service tests.
5. Frontend: activate Options pane (drill-down) + Forex pane (mode/source selector); UI tests.
6. Update the Phase-1 "no typed client" test (if present); one-line status bump in the parent design doc.

## Open points (resolved in this design)

| Topic | Choice |
|-------|--------|
| Options data source | Massive (local to web process) |
| Forex snapshot/quote default source | `massive` (no trader dependency); `ib`/`twelvedata` selectable |
| IB forex transport | typed `get_snapshot` via existing query link; never legacy `42001` |
| Research service typed dependency | injected query caller, `backend` selector, default `massive` |
| Greeks/IV columns | shown only when Massive payload includes them |
| TwelveData quote bid/ask | not synthesized; emit a notice |
| Scan/Depth | untouched; sub-projects B/C |
| Order entry | out of scope (read-only) |
