# Dashboard: watchlists (CSV upload) + deploy strategies from disk

**Date:** 2026-07-15
**Status:** approved (user-directed, continuation of dashboard-strategy-controls)

## Requirements (user's words)

1. Load strategies from disk via dashboard UI and enable them.
2. Let the user create their own portfolio/watchlist of stocks & ETFs to
   follow/monitor for signals, with CSV file upload.

## Components

### A. Watchlists tab (universes UI)

Watchlists ARE universes — the existing `UniverseAccessor` storage, resolve-
via-IB flow (`sdk.resolve` + `accessor.insert`, same as `universe add`), and
CSV machinery are reused; the dashboard only adds HTTP/UI.

Routes (CSRF + flash + 303 pattern):
- `POST /watchlists/create` {name} — name validated `[a-z0-9_-]{1,40}` (lowercased)
- `POST /watchlists/{name}/add` {symbols, exchange?, currency?, sectype?} —
  whitespace/comma-separated symbols, each resolved via IB; unresolved symbols
  reported in the flash, resolved ones inserted (precision over convenience:
  never guessed)
- `POST /watchlists/{name}/upload` — multipart CSV. Two accepted shapes:
  (a) simple: a `symbol` column (case-insensitive header; optional `exchange`,
  `currency`, `sectype` columns) or headerless one-symbol-per-line → per-row
  resolve+insert; (b) full SecurityDefinition export (`conId` column present)
  → `accessor.update_from_csv_str` directly. 1 MB size cap.
- `POST /watchlists/{name}/remove` {symbol}
- `POST /watchlists/{name}/delete`

Fetcher `fetch_watchlists()` → `[{name, count, symbols: 'AAPL, MSFT, …'}]`
via a `_get_accessor()` helper (Container config: duckdb_path +
universe_library) that tests can monkeypatch.

Tab order: Overview | Strategies | Watchlists | Risk.

### B. Deploy from disk (Available strategies table)

Each scanned strategy row gets a "Deploy" unfold form: deployment name
(default: file stem), bar size (select), target = symbols text OR existing
watchlist (dropdown; strategy then monitors every member), historical days
(default 90), `auto_execute: propose` checkbox, params prefilled from the
scanner's tunables.

`POST /strategies/deploy`:
1. Validate: (file, class) must appear in `scan_strategies` output (preserves
   the strategies-directory sandbox); deployment name unique in the YAML;
   exactly one of symbols/watchlist.
2. Symbols path: resolve each via `sdk.resolve`; failures abort with a flash
   naming the unresolved symbols (no partial deploys); resolved defs inserted
   into auto-watchlist `strat_<name>` (registers secdefs locally so
   `resolve_symbol(conId)` succeeds at load) and the YAML entry gets explicit
   conids. Watchlist path: YAML entry gets `universe: <watchlist>`.
3. Append entry to `~/.config/mmr/strategy_runtime.yaml` (safe_load → append
   → atomic tmp+rename; same discipline as update_strategy_params).
4. `reload_strategies` RPC (immediate load, not the 30s reconcile), then
   `enable_strategy` RPC — deployed strategies start enabled, per the request.
5. Flash the outcome of each step; a reload/enable failure after a successful
   YAML write says so explicitly (config persisted; enable manually).

## Error handling

No partial resolution deploys; YAML writes atomic; every route failure
flashes instead of 500ing; upload rejects >1 MB and undecodable files.

## Not in scope

Editing a deployed strategy's conids/universe from the UI (undeploy+redeploy
covers it); per-watchlist strategy auto-attachment; XLSX parsing.

## Tests (tests/test_web_dashboard.py additions)

Watchlists: create (+name validation), add (resolve stub, unresolved
reported), CSV simple shape, CSV secdef shape, remove, delete, render.
Deploy: happy path writes YAML + reload + enable calls; duplicate name 
rejected; unknown class rejected; unresolved symbol aborts; watchlist target
writes universe key; render of deploy form.
