# MMR — Domain & Architecture Vocabulary

Shared names for the parts of the system, so code reviews and refactors talk
about the same seams. Architecture terms follow the deep-module vocabulary
(module · interface · implementation · depth · seam · adapter · leverage ·
locality); domain terms are named here.

## Command surface

The **command surface** is the one interface through which a human (via the
dashboard) or the CLI/SDK asks the trader to *change* state — propose, approve,
reject, cancel, pause/resume, enable/disable a strategy, activate an allocation
or paper automation. Reads (portfolio, positions, snapshot) are **not** part of
it. Every command crosses the same trader typed-RPC command registry
(`trader/messaging/production_api.py`) into the
`TradingCommandCoordinator` (`trader/trading/command_coordinator.py`), which
owns idempotency, the preflight nonce, and the audit row.

Two **adapters** sit over that one command interface:

- **`DashboardCommandGateway`** (`web/command_center/gateway.py`) — the server
  adapter. Collapses transport/remote failure into one stable
  `{code, message, retryable, correlation_id}` envelope.
- **`CCCommands`** (`web/static/command_center_commands.js`) — the browser
  adapter. A deep module holding the client-side command logic: CSRF-retry,
  the 202→ledger-reconcile pending state machine, the live two-stage preflight
  ceremony, and live-vs-paper gating. Extracted from `command_center.js` behind
  a small namespace + an injected `view()` accessor (`CCCommands.init({view,
  config})`) so that logic is testable through its interface with a stubbed
  `fetch`/`document` — see `web/static/command_center_commands.test.js`. The
  CLI/SDK path (`trader/sdk.py`) is the third caller of the same underlying
  commands.

### Trader transport (`TraderLink`)

`web/trader_link.py` is the one shared **typed-RPC transport** to the trader /
strategy services. One `TraderLink` == one socket (a role at a
`tcp://host:port` endpoint); it owns endpoint parsing, HMAC-authenticator
build, lazy client build, reconnect-after-transport-failure, and a per-socket
serialization lock, and raises exactly one `TraderLinkError`
(`kind=timeout|unavailable`, `+cause`) while letting `TypedRpcRemoteError` (a
healthy-socket application rejection) propagate. The typed stacks compose it:
`DashboardCommandGateway` holds one command-only `TraderLink` (its lock never
shared with reads); `ManageRpcClient` holds one per bucket; the event bridge
reuses only the construction primitives (`parse_endpoint` /
`build_authenticator` / `connect_client`) because it owns its own
cursor-resnapshot reconnect at a higher layer. The legacy `_get_mmr` full-RPC
SDK in `web/app.py` is a **different protocol**, deliberately outside
`TraderLink`, pending a separate legacy-SDK removal.

Safety rule embedded in the surface: a **LIVE** account routes every
risk-increasing command through a signed preflight confirmation (nonce +
authoritative summary) before it transmits; **paper** approve is one-click (the
proposal card is the review). See `docs/AUDIT_ROADMAP.md` H1/H2 for the risk
projection payload work that the dashboard's risk bars and proposal
exposure-impact line depend on.

## Command center

The **command center** is the operator dashboard at `/cc`
(`web/command_center/`, `web/templates/command_center.html`, `web/static/`). Its
read model is a live SSE view reduced by **`DashboardState`**
(`web/command_center/state.py`) — a deep module whose status-partition rules
(active/terminal proposals & orders, dispatchable strategies) are mirrored by an
incremental client reducer in `command_center.js`; the classification constants
are a known cross-language duplication (AUDIT_ROADMAP candidate).

## Client seams (browser)

- **`cc_util.js`** — the pure, DOM-free seam: quote-freshness/clock-skew math,
  the degraded predicate, the snapshot-supersede guard, and the shared
  `ccAccountModeValue` / `ccStrategyName` helpers. Fully unit-tested in
  isolation; the model for what belongs behind a clean seam.
- **`CCCommands`** — the command surface (above).
- **`CCResearch`** (`command_center_research.js`) — the Research tab; reaches
  the command surface only through the loose `globalThis.ccOpenResearchProposal`
  hook (opens the New-proposal drawer), never placing orders directly.
- **`command_center.js`** — what remains: the SSE store + reducer, connection
  management, the `render*` functions, the drawer base, view-lookup finders, and
  the thin event wiring that calls `CCCommands.*`.
