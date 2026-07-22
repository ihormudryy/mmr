# Dashboard Research Tab Design

## Purpose

The Command Center gains a session-gated **Research** tab that brings selected
CLI research workflows into `/cc` without turning the dashboard into a general
terminal. The work is delivered in phases. Phase 1 provides a reusable shell
and the Massive-backed `ideas`, `movers`, `snapshot`, and `news` workflows.
Later phases add IB scanners and depth, then options and forex.

This design intentionally revises the scanner disposition in Section 8.5 of
`2026-07-15-realtime-trading-command-center-design.md`. Backtesting, universe
bulk operations, and data download or refresh management remain CLI-only.

The Research tab is read-only by default. Its only bridge to trading is an
explicit **Propose** action that opens the existing New Proposal drawer. A
research result never places an order or bypasses the proposal pipeline.

## Scope and delivery

Phase 1 includes:

- a sixth `#research` tab beside Trading, Scaling, Deploy, Watchlists, and Guide;
- a shared split-pane Research shell;
- Ideas and Movers result lists;
- Lookup with a Massive stock snapshot and ticker news;
- proposal-drawer entry from eligible equity results when dashboard commands
  are enabled; and
- inert, visibly labelled placeholders for Scan, Depth, Options, and Forex.

Phase 1 excludes IB-backed `ideas --location`, the IB `scan` command, market
depth and PNG export, options, forex, background refresh, and cross-request
result caching. It does not add direct order entry.

## User experience

### Research shell

Research uses the existing Command Center visual language and tab mechanics.
Its left rail contains **Ideas**, **Movers**, and **Lookup**, followed by disabled
**Scan**, **Depth**, **Options**, and **Forex** entries marked “Later.” Disabled
entries make no requests and expose no nonfunctional form controls.

The main area is a split pane:

- the left side contains the selected tool's controls and result rows; and
- the right side is a sticky detail pane for the selected row or Lookup result.

On narrow screens the detail pane stacks below the results. Keyboard selection,
focus indication, headings, table semantics, loading text, and inline status
messages follow the accessibility conventions already used by `/cc`.

Research maintains state per tool for the browser session. Changing tools does
not discard the last successful result. A failed refresh leaves that result in
place and shows the new error beside the affected tool. Initial empty state,
valid empty results, loading, configuration unavailable, provider error, and
timeout are visually distinct states.

If `massive_api_key` is absent, the tab still renders. A configuration banner
explains that Massive-backed research is unavailable; this condition never
turns `/cc` itself into a 500 response.

### Phase 1 tools

**Ideas** exposes the existing scanner presets and the Massive-backed CLI
controls that do not require IB: preset, movers/tickers/universe input,
result limit, filter overrides, and optional fundamentals/news/name enrichment.
IB location is not accepted. The selected row drives the detail pane.

**Movers** exposes the existing Massive market and gainers/losers controls plus
the result limit and optional detail enrichment. Results use the same field
semantics as the CLI JSON output. Equity rows can enter the proposal flow;
non-equity market results remain research-only.

**Lookup** accepts one ticker and loads its Massive stock snapshot and news.
Snapshot and news have independent loading and error states so one successful
response remains useful if the other provider call fails. News supports the
existing Polygon/Benzinga source choice and bounded result limit.

The browser does not automatically execute a scan on tab load. Every provider
call follows an explicit operator action.

## Architecture

### Boundaries

The feature has three bounded pieces:

1. A thin FastAPI research router owns authentication, query validation, HTTP
   status mapping, and response serialization.
2. A research service owns provider-client construction, executor admission,
   timeout policy, and translation of provider exceptions into stable domain
   errors.
3. Reusable scanner helpers own the actual Massive calls and normalization.
   Existing logic is extracted or reused from `trader/tools/idea_scanner.py`
   and `trader/sdk.py`; the web process does not invoke `trader.mmr_cli`, spawn
   subprocesses, scrape terminal output, or create an IB-dependent `MMR`
   instance for Phase 1.

The service accepts its client factory, executor, and clock as dependencies so
routes can be tested without network access or real sleeps. Provider DataFrames
and SDK objects are normalized before reaching the router. NaN and infinity
become JSON `null`; dates and times use ISO-8601 strings.

Massive-backed work runs locally in the web process. IB-backed work is deferred
until it has typed query contracts. Research must never reopen the unsafe legacy
RPC path in split-container production.

### HTTP API

All routes require the same signed dashboard session as the existing read-side
Command Center routes. They are GET requests and require no command CSRF token.
They do not depend on `DASHBOARD_COMMANDS_ENABLED`.

The Phase 1 router exposes:

- `GET /api/research/presets`
- `GET /api/research/ideas`
- `GET /api/research/movers`
- `GET /api/research/snapshot`
- `GET /api/research/news`

Query names, defaults, enum values, and bounds mirror the corresponding CLI
options, except for the Phase 1 exclusions above. List-like ticker input is
normalized once at the HTTP boundary; blank tickers, incompatible source
arguments, unsupported provider or location requests, and out-of-range limits
fail validation before occupying a worker.

Successful responses preserve the CLI `--json` convention while adding stable
machine-readable metadata:

```json
{
  "data": [],
  "title": "Ideas: momentum",
  "meta": {
    "tool": "ideas",
    "provider": "massive",
    "observed_at": "2026-07-22T12:00:00Z",
    "notice": null
  }
}
```

`data` is an array for presets, ideas, movers, and news, and an object for a
snapshot. A valid no-result response is `200` with an empty array or object.
The API never returns rendered Rich text or HTML.

Errors use one envelope:

```json
{
  "error": {
    "code": "RESEARCH_TIMEOUT",
    "message": "Ideas did not complete within 30 seconds.",
    "retryable": true
  }
}
```

The status mapping is:

| Condition | Status | Code | Retryable |
| --- | ---: | --- | --- |
| Missing Massive configuration | 503 | `MASSIVE_NOT_CONFIGURED` | false |
| All research worker slots occupied | 503 | `RESEARCH_BUSY` | true |
| Provider call exceeded its budget | 504 | `RESEARCH_TIMEOUT` | true |
| Massive/provider failure | 502 | `RESEARCH_UPSTREAM_ERROR` | true |
| Invalid query | 422 | FastAPI validation response | false |

Provider credentials, raw tracebacks, and unfiltered exception text never enter
the response. Server logs contain the tool, sanitized parameters, elapsed time,
outcome, and exception class, but never the API key.

### Isolation, admission, and timeouts

Research owns a four-worker `ThreadPoolExecutor`; it does not use the default
application executor. A matching four-slot admission guard prevents an
unbounded executor queue. If no slot is available when a provider request is
admitted, the route fails quickly with `RESEARCH_BUSY`.

The request budgets are:

| Operation | Budget |
| --- | ---: |
| Snapshot | 10 seconds |
| Movers | 15 seconds |
| News | 15 seconds |
| Ideas | 30 seconds |
| Presets | local, no worker |

Python cannot safely terminate a running provider thread. When an HTTP request
times out, its admission slot therefore remains held until the underlying call
actually returns. This prevents abandoned work from allowing more calls into an
already saturated executor. Application shutdown stops admission and shuts down
the executor through the FastAPI lifecycle.

Phase 1 deliberately has no automatic retry. The UI exposes an explicit retry,
and retryability in the error envelope tells it whether that action is useful.

## Proposal workflow and feature flags

Ideas and eligible Movers rows, plus Lookup detail, show **Propose** only when
`DASHBOARD_COMMANDS_ENABLED=true`. Research remains fully readable when commands
are disabled; the command-only drawer markup and controls remain absent under
the existing template gate.

Selecting **Propose**:

1. resets the existing New Proposal form so stale values from an earlier action
   cannot carry into the research result;
2. opens that same drawer rather than creating a Research-specific form;
3. prefills symbol and available exchange/currency hints;
4. resolves the symbol through the existing session-gated `/api/resolve` typed
   IB discovery path; and
5. fills the exact conId when resolution succeeds.

Resolution errors or multiple matches stay visible in the drawer so the
operator can adjust the hints. The result does not infer or prefill side, size,
confidence, thesis, or reasoning. The operator supplies those fields and
submits through the existing CSRF-protected
`POST /api/commands/proposals` endpoint.

Proposal creation produces only a pending proposal. It does not execute an
order, so `DASHBOARD_LIVE_COMMANDS_ENABLED` does not gate this entry point.
Existing approval controls and live-command ceremony continue to govern any
later execution.

## Later phases

### Phase 2: IB research

Phase 2 adds Scan and Depth to the same shell. Depth uses the existing typed
`get_market_depth` query. Scan is exposed only after `scanner_data` has an
equivalent typed query contract. The old legacy RPC and CLI subprocesses are
not acceptable fallbacks. IB snapshot can then augment Lookup through an
explicit provider choice; every result identifies its provider and observation
time. Depth table and PNG/export behavior are designed as part of that phase.

### Phase 3: options and forex

Phase 3 adds options expirations, chain, snapshot, and implied-distribution
views, followed by forex snapshot, quote, movers, bulk snapshot, and conversion.
Massive-backed operations remain local to the web process. IB-backed operations
use typed queries. These views do not imply proposal or direct-order support;
any additional trading bridge requires a separate approved design.

The Phase 1 implementation plan covers only its functional tools and inert
later-phase shell entries. Each later phase receives its own detailed spec and
implementation plan.

## Testing and verification

Helper tests cover normalization, empty provider data, notices, field
preservation, and NaN/infinity conversion. They use provider doubles and make no
network calls.

Route and service tests cover:

- session enforcement on every route;
- query defaults, bounds, enums, and incompatible parameters;
- successful CLI-shaped responses for every tool;
- missing-key `503`, upstream `502`, timeout `504`, and busy `503` envelopes;
- independent Lookup snapshot/news failures;
- admission slots remaining occupied until timed-out work exits;
- no use of the shared executor, legacy RPC, or subprocess CLI; and
- executor shutdown through application lifecycle.

UI tests cover the sixth tab and tool markers, loading/empty/error states,
preservation of the last successful result, disabled later-phase entries, and
responsive split-pane markers. Command-enabled tests cover drawer reset,
prefill, resolution, and submission through the existing proposal endpoint.
Command-disabled tests assert that Propose and command drawer controls are
absent while research reads remain available.

Phase 1 verification runs focused research route/service tests and lightweight
UI marker tests, followed by the full suite using `--timeout-method=thread`.
The release check must confirm that Trading SSE remains responsive while all
research workers are occupied.

## Non-goals

This design does not replace the CLI, add direct order entry, change proposal
approval semantics, expose provider credentials, add scheduled scans, persist
research results, implement research alerts, or revisit the unrelated Command
Center visual redesign currently present in the working tree.
