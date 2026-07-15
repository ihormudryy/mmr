# Signal → Proposal Bridge (`auto_execute: propose`)

**Date:** 2026-07-15
**Status:** approved (user chose semi-auto mode over full-auto)

## Problem

Deployed strategies record and publish signals (`strategy_runtime.py` `on_ticker_next`)
but nothing consumes them — the `auto_execute` config flag is parsed, stored, and
displayed but never read by any execution path (AUDIT_2026-07 findings ~1097/1110).
Signals also publish with `conid=0` and there is no duplicate suppression.

## Decision

Semi-automatic bridge: `auto_execute: propose` on a strategy turns each signal into
a **PENDING TradeProposal** with automatic position sizing. A human approves or
rejects in the web dashboard (POST `/proposals/{id}/approve`) or via `mmr approve`.
No order is ever placed without human approval. Full-auto (`auto_execute: true`)
remains unimplemented and is now **rejected at load time** (fail loudly) instead of
being silently inert.

## Config semantics

| `auto_execute` value | Behavior |
|---|---|
| absent / `false` | Current behavior: signal recorded + published only |
| `propose` | Bridge active: signal → PENDING proposal (paper mode only) |
| `true` / anything else | Strategy refused at load with a clear error (not silently inert) |

Propose mode is gated to `paper_trading=True`. In live mode the bridge logs a
warning once per strategy and does nothing.

## Components

### 1. Signal conid stamping (unconditional)

`on_ticker_next` sets `signal.conid = conId` before persisting/publishing.
Fixes the audit's "signals carry conid=0" finding for all consumers.

### 2. `SignalProposer` (new: `trader/strategy/signal_proposer.py`)

Constructed by `StrategyRuntime` with: `ProposalStore(duckdb_path)`, the existing
`trader_client` RPC client, `paper_trading`, and a proposal TTL (default 30 min).

`on_signal(strategy, signal, frame)` — called after the signal is recorded, only
for propose-mode strategies:

- **Expire stale bridge proposals**: PENDING proposals created by this bridge whose
  `metadata.expires_at` has passed are transitioned PENDING → EXPIRED
  (`try_transition`, so races with a concurrent approve are safe). Nothing else
  expires proposals today; a 3-hour-old 1-min entry signal must not look actionable.
- **Dedup**: skip if a PENDING proposal with the same `(strategy, conid, action)`
  metadata already exists.
- **BUY**: size via `PositionSizer` — confidence = `signal.probability`, portfolio
  state from `get_account_values`/`get_portfolio` RPC, price = frame's last close,
  ATR computed from the frame. Create proposal with `source='strategy:<name>'`,
  auto-generated reasoning, sizing metadata, and the signal's exit conditions
  (`max_hold_bars`, `close_by_time`) recorded in metadata. If account state is
  unavailable (trader_service down) → log ERROR and skip (no unsized proposals).
- **SELL**: close-only, matching backtester long-only semantics. Current position
  from `get_portfolio` RPC by conid; flat → skip (log); long → proposal with
  `quantity = held`. Exits must not silently vanish: RPC failure → log ERROR.
- Symbol/exchange/currency resolved from the local universe DB via
  `resolve_symbol(conId)` RPC (precision-over-convenience: no proposal if
  unresolvable).

`check_exits(strategy, conId, frame)` — called once per new completed bar:

- Finds EXECUTED bridge proposals for `(strategy, conid)` carrying exit conditions
  not yet acted on (`metadata.exit_proposed` unset).
- Entry time = the proposal's `updated_at` (execution transition timestamp);
  `bars_held` = count of frame bars after entry — restart-proof, no in-memory state.
- Triggers (mirrors backtester semantics): `bars_held >= max_hold_bars` or last bar
  time-of-day `>= close_by_time`.
- On trigger: verify still long via RPC, create a SELL close proposal (deduped),
  set `exit_proposed=true` on the entry proposal (`update_metadata` is atomic).

### 3. Load-time validation (`strategy_runtime.load_strategy`)

Normalize `auto_execute`: `False`/`None` → off; `'propose'` → propose mode;
anything else → log ERROR and refuse to load that strategy (other strategies
unaffected).

### 4. Failure isolation

Every bridge call is wrapped like the existing signal persist/publish path: an
exception creating one proposal never kills the tick feed or other strategies.

## Not in scope

Shorts; full-auto execution; live-mode bridging; changes to the MessageBus signal
topic; a general proposal-expiry sweeper (bridge proposals only); dashboard changes
(PENDING proposals already render with approve/reject buttons).

## Tests (`tests/test_signal_proposer.py`)

BUY → PENDING proposal with sizing + metadata; dedup while PENDING; SELL flat →
no proposal; SELL long → close qty; live-mode no-op; TTL expiry; `close_by_time` /
`max_hold_bars` exit proposals (once, flag set); conid stamping;
`auto_execute: true` rejected at load; `auto_execute: propose` accepted.
