---
name: mmr-loop-skill
description: Autonomous trading loop for the MMR platform. Continuously monitors portfolio, scans for opportunities, creates trade proposals, and manages risk. Requires the 'mmr' skill to be loaded first. Use when the user asks to start monitoring, trading autonomously, or running a trading loop.
metadata:
  author: mmr
  version: "1.1"
  dependencies: mmr-skill
---

# MMR Trading Loop

This skill turns the MMR trading platform into an autonomous trading agent. It implements a phased state machine that continuously monitors your portfolio, scans for opportunities, and creates trade proposals — then on **paper** evaluates and decides approve/reject; on **live** leaves execution to a human.

**Important**: This skill requires the `mmr` skill to be loaded first. Load both:
```
await load_skill("mmr", "all")
await load_skill("mmr-loop", "all")
```

## How It Works

The loop runs a repeating cycle:

```
PRE-FLIGHT → MONITOR → ANALYZE → PROPOSE → [paper: EVALUATE → APPROVE|REJECT] → DIGEST → (sleep) → ...
```

**PRE-FLIGHT**: Checks `status()` for trader_service connectivity and IB Gateway upstream connection. If IB Gateway can't reach IBKR servers, the cycle skips directly to DIGEST — no wasted API calls that would timeout.

**MONITOR**: Quick health check. Calls `portfolio_snapshot()` and `portfolio_diff()` (~500 tokens). If nothing moved (all positions unchanged), skips directly to DIGEST — no wasted API calls or context.

**ANALYZE**: Only runs when MONITOR finds something interesting (positions moved, market open, new cycle). Runs `portfolio_risk()` for warnings, scans with `ideas()` using rotating presets (momentum → mean-reversion → breakout → volatile → gap-down), checks news for held positions.

**PROPOSE**: Creates trade proposals via `propose()` with auto-sizing (ATR-adjusted), group tagging, confidence scores, reasoning, and a **protective trailing stop on every loop entry** (`propose_trailing_stop_pct`, default 2%, `tif="GTC"`). Skips symbols that already have a PENDING proposal (`propose()` also refuses these with `DUPLICATE_PENDING`). Skipped entirely while RISK-OFF (see Safety Boundaries).

**EVALUATE → APPROVE|REJECT (paper only)**: Every proposal the loop created this cycle gets decided — never left to expire undecided. For each: `proposal_show(N)` (the checklist is **mechanically enforced** — `approve()` refuses `CHECKLIST_INCOMPLETE` without it and a fresh `portfolio_risk()`), then `approve(N)` **or** `reject(N, reason=...)`. Up to `max_decisions_per_cycle` decisions, remaining budget goes to older pendings (e.g. from strategies). Never blind auto-approve (`auto_approve` stays false). On **live**, skip this phase — human approves in the Command Center; SDK approve is refused (`LLM_LIVE_APPROVE_FORBIDDEN`); reject stays allowed. Server refusals (`QUOTE_STALE`, `PRICE_DRIFT_EXCEEDED`, `ORDER_NOTIONAL_LIMIT`, `RISK_REJECTED`) are guardrails — adjust, don't retry verbatim. An UNKNOWN/timed-out approve may be live at the broker: reconcile, never re-approve.

**DIGEST**: Writes cycle summary to memory (persists across compeds), compacts context with `compact("drop-helpers-results")`, sleeps until next cycle.

## Starting the Loop

To start the trading loop, register hooks and initialize state:

```python
result = await load_skill("mmr", "all")
result = await load_skill("mmr-loop", "all")

# Initialize the loop engine
await TradingLoop.start()
```

The loop will run until you say "stop" or call `await TradingLoop.stop()`.

## User Interaction

While the loop is running, you can interrupt at any time:
- **"stop"** or **"pause"** — stops the loop
- **"approve 42"** — approve a pending proposal (paper: LLM may do this after evaluation; live: human / dashboard only)
- **"reject 42"** — reject a proposal
- **"status"** — get current loop state and cycle count
- **"skip"** — skip to next cycle immediately
- Any other question — the loop pauses, answers, then resumes

## Configuration

The loop reads configuration from the `TradingLoop.config` dict. Override before starting:

```python
TradingLoop.config["scan_interval_seconds"] = 300  # 5 minutes between cycles
TradingLoop.config["scan_presets"] = ["momentum", "mean-reversion"]
TradingLoop.config["max_proposals_per_cycle"] = 1
TradingLoop.config["max_decisions_per_cycle"] = 4      # approve/reject budget per paper cycle
TradingLoop.config["daily_pnl_alert_pct"] = 0.02       # RISK-OFF brake: day P&L <= -2% of net liq → no new entries
TradingLoop.config["risk_hhi_warning"] = 0.15          # HHI above this → no adds to concentrated names
TradingLoop.config["propose_trailing_stop_pct"] = 2.0  # protective trailing stop on every loop entry (0 disables)
TradingLoop.config["auto_approve"] = False  # NEVER true — blind fire is forbidden; paper uses evaluate-then-decide
```

See [references/LOOP_CONFIG.md](references/LOOP_CONFIG.md) for full configuration reference.

## Safety Boundaries

1. **Paper evaluate-then-decide; live human-only**: On paper, after propose, evaluate then `approve` or `reject`. The checklist is **enforced by the helper** — `approve()` refuses `CHECKLIST_INCOMPLETE` unless `proposal_show(N)` and a successful `portfolio_risk()` ran within 15 minutes. On live, never call `approve` (server refuses SDK/LLM). Reject remains allowed for PENDING cleanup. Never blind auto-approve.
2. **RISK-OFF daily-loss brake**: when day P&L breaches `-daily_pnl_alert_pct` of net liquidation, the cycle creates no proposals and approves no new entries (rejects and closes stay allowed) and alerts the operator. The server's risk gate additionally enforces its own daily-loss halt at approve time.
3. **Protected entries**: every loop-created BUY carries a trailing stop (`propose_trailing_stop_pct`, GTC) so a fill is never unguarded between cycles.
4. **Pending dedupe**: the loop skips symbols with an existing PENDING proposal, and `propose()` refuses duplicates (`DUPLICATE_PENDING`) as a backstop — no re-proposing the same idea every cycle.
5. **Concentration ceiling**: HHI above `risk_hhi_warning` → no adds to the dominant names; diversify or do nothing.
6. **Position limits**: Respects `max_positions` from position_sizing.yaml. Stops proposing at the limit.
7. **Group budgets**: Checks group allocation budgets before proposing. Over-budget = warning, not block.
8. **Server guardrails**: approved trades still pass the risk gate, trading filter, per-order notional ceiling (`ORDER_NOTIONAL_LIMIT`), quote-staleness (`QUOTE_STALE`) and price-drift (`PRICE_DRIFT_EXCEEDED`) checks. Refusals are rules — adjust the proposal, never retry verbatim.
9. **Proposal state machine**: Terminal statuses (EXECUTED / REJECTED / FAILED / EXPIRED) are immutable. If `approve()` fails at the broker (e.g. margin rejection, bracket rollback), the proposal moves to FAILED — the loop should create a new proposal rather than try to re-approve the old one. An UNKNOWN/timed-out approve may be live: reconcile with `orders()` / `portfolio()`, never re-approve.
10. **Scanner fails loudly**: `ideas()` with `location=` raises on "no results" rather than returning an empty list, so the loop will see a real error for misconfigured markets instead of silently making zero proposals. Log the error, skip the scan for that cycle, and continue.
11. **Signed exposure in risk reports**: `portfolio_risk()` returns `net_exposure_pct`, `long_exposure_pct`, `short_exposure_pct` alongside `gross_exposure_pct`. For long-only loops these are effectively the same; for long/short loops, read `net_exposure_pct` first — correlation-cluster warnings now fire on net, so a correlated long/short hedge pair won't false-alarm.
12. **Context management**: Aggressive compaction keeps context at ~12K tokens. Can run indefinitely.

## What Gets Written to Memory

Each cycle writes a summary entry:
```
Key: trading_loop_cycle_{N}
Summary: "Cycle N — Portfolio $67K (+$20 daily). Scanned momentum. Created 1 proposal (BHP BUY $3,600). HHI 0.064, no warnings."
```

You can review cycle history anytime:
```python
keys = read_memory_keys()
for k in keys:
    if k["key"].startswith("trading_loop_cycle_"):
        print(f'{k["key"]}: {k["summary"]}')
```

## Position Tracking

The loop has a built-in position monitor that checks tracked positions between cycles. When a position hits its stop-loss or take-profit threshold, the LLM is alerted. Between alerts, monitoring happens silently in the hook — no LLM turns are wasted.

```python
# Track positions after entering trades
await TradingLoop.track_position("BHP", "LONG", entry=50.15, qty=350, stop_pct=-1.5, add_pct=2.0)
await TradingLoop.track_position("PLS", "SHORT", entry=4.58, qty=3800)

# Or use module-level convenience functions
await track_position("BHP", "LONG", 50.15, 350)

# Check what's being tracked
status = await TradingLoop.status()
# status["tracked_symbols"] → ["BHP", "PLS"]

# Stop tracking
await TradingLoop.untrack_position("BHP")
await TradingLoop.untrack_all()  # clear all
```

The monitor uses `snapshots_batch()` for efficiency (~4s for all symbols vs ~4s per symbol). Configure timing with `monitor_interval_seconds` (default 180s). Default thresholds: `monitor_stop_pct=-1.5`, `monitor_add_pct=2.0`.

## Quick Reference

| Function | Description |
|----------|-------------|
| `await TradingLoop.start()` | Start the loop (registers hooks) |
| `await TradingLoop.stop()` | Stop the loop (unregisters hooks) |
| `await TradingLoop.status()` | Get running state, cycle count, tracked positions |
| `await TradingLoop.track_position(sym, side, entry, qty, ...)` | Add position to monitor watchlist |
| `await TradingLoop.untrack_position(sym)` | Remove from watchlist |
| `await TradingLoop.untrack_all()` | Clear all tracked positions |
| `await start_trading_loop(**overrides)` | Start with config overrides in one call |
| `await stop_trading_loop()` | Alias for `TradingLoop.stop()` |
| `TradingLoop.config["key"] = value` | Change config before or during loop |

## Architecture Notes

- Hook sleeps internally via `asyncio.sleep()` — no wasted LLM turns between cycles
- Timer-based gating: pure elapsed time, no iteration-modulo tricks
- Position monitor runs between full cycles, only alerts the LLM when thresholds are breached
- Uses `snapshots_batch()` for efficient multi-symbol price checks (~4s total)
- Uses `delegate_task()` for parallel data gathering (portfolio + risk + scan simultaneously)
- Context stays at ~12K tokens after compaction across unlimited cycles
- Rotating scan presets ensure different patterns are checked each cycle
