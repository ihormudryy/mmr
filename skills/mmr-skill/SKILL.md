---
name: mmr-skill
description: Operate the MMR algorithmic trading platform on Interactive Brokers. Trade stocks and options, manage portfolios, scan for ideas, create trade proposals with auto position sizing, manage universes, download historical data, analyze options chains, and control strategies. All operations are available via async Python helper methods.
metadata:
  author: mmr
  version: "2.2"
---

# MMR Trading Skill

**LLMVM**: Use `<helpers>...</helpers>` blocks to call MMRHelpers methods. All methods are `async`. Most return JSON dicts (access data via `result["data"]`). Trading actions (`buy`, `sell`, `cancel`) return strings.

## ⚠️ TRADING POLICY — READ THIS FIRST ⚠️

**NEVER BYPASS `propose → approve/reject` FOR ACTIONABLE TRADES.** Every new position (entry, add, rotation, cover) goes through this pipeline:

1. `MMRHelpers.propose(symbol, action, ...)` — creates a reviewable plan
2. **Evaluate** (required before decide, and **mechanically enforced**):
   - `proposal_show(N)` — sizing_result, reasoning, brackets
   - `portfolio_risk()` — concentration, group budget, remaining capacity
   - Quote / session sanity is enforced **server-side** at approve time
     (`QUOTE_STALE`, `PRICE_DRIFT_EXCEEDED`, `ORDER_NOTIONAL_LIMIT`)
3. Then **either** `approve(proposal_id)` **or** `reject(proposal_id, reason=...)` with a short written reason

**The checklist is enforced, not advisory:** `approve()` refuses client-side
with `CHECKLIST_INCOMPLETE` (no order is sent) unless `proposal_show(N)` for
that proposal **and** a successful `portfolio_risk()` both ran within the last
15 minutes. There is no override — run the two calls, then decide.
`reject()` is never gated.

### Paper vs live

| Mode | Who may approve |
|------|-----------------|
| **Paper** | **You (the LLM)** may `approve` or `reject` after the checklist above. Not blind auto-approve. |
| **Live** | **Human only** via Command Center live ceremony. SDK/LLM `approve` is refused (`LLM_LIVE_APPROVE_FORBIDDEN`). You may still `reject` PENDING proposals. Propose + review only — stop before the wire. |

Blind `auto_approve` is forbidden. Do not approve without completing the evaluation checklist.

### Server guardrails — refusal codes are rules, not errors

Paper approvals still pass hard server-side limits. Never retry a refusal
verbatim; each code tells you what to change:

| Code | Meaning | What to do |
|------|---------|------------|
| `SIZING_BLOCKED` | Auto-sizing not configured on the command path | Re-propose with explicit `amount=` |
| `DUPLICATE_PENDING` | Same symbol+action already PENDING (helper-enforced for LLM sources; server-enforced for `strategy:`) | Decide the existing proposal instead |
| `QUOTE_STALE` | Freshest quote too old to trade on (paper bound configurable, default 30 min) | Wait for live data / market hours |
| `PRICE_DRIFT_EXCEEDED` | Price moved too far from the proposal's reference | Reject; re-propose at current price if the thesis holds |
| `ORDER_NOTIONAL_LIMIT` | Order exceeds `command_authority.max_order_notional` (default $25k) | Re-propose smaller |
| `NO_LONG_TO_CLOSE` | Strategy SELL while flat — dropped by design | Nothing; working as intended |
| `RISK_REJECTED` | Portfolio limit breached (daily loss, open orders, …) | Reduce risk before adding |
| `PROPOSAL_EXPIRED` | Sat unapproved past ~30 min | Re-propose if still valid |
| `CHECKLIST_INCOMPLETE` | Client-side: evaluation not done or stale | Run the listed steps, then decide |
| `LLM_LIVE_APPROVE_FORBIDDEN` | Non-human approve on a live account | Stop — a human approves on live |

If `approve()` returns **UNKNOWN/timed-out**, the order MAY be live at the
broker: do NOT re-approve — reconcile with `orders()` / `portfolio()` and
`proposal_show(N)` first.

**Unattended paper entries should carry protection:** prefer
`propose(..., trailing_stop_pct=2.0, tif="GTC")` (or `stop_loss=`/
`take_profit=` for a bracket) so a filled entry is never left unguarded
between loop cycles.

**Do NOT use `MMRHelpers.buy()` / `MMRHelpers.sell()` / `MMRHelpers.cli("buy ...")` to open or modify positions based on your own judgment.** Those exist for two narrow cases only:
- Manual human-driven single-trade CLI usage (you're not human)
- Liquidation paths (`close_all_positions`, `resize_positions`) — these set `skip_risk_gate=True` explicitly

The trader_service can be configured to **refuse direct buy/sell RPCs entirely** (`require_proposal_approval: true` in `trader.yaml`). If you see `"Direct order rejected: require_proposal_approval is true"`, stop — that's the kill switch telling you to route through `propose → approve`.

**If a written plan (trade_notes.md, user message) says "4 fresh longs via propose/approve", do EXACTLY that — do not invent a rotation on existing positions, do not decide to trim/cover/cut based on current portfolio state unless the user explicitly authorised that action in the same instruction.** Drift from written plans is the failure mode this policy exists to prevent.

## Service Requirements

- **trader_service required**: portfolio, positions, orders, trades, account, resolve, snapshot, depth, buy, sell, cancel, cancel_all, close_all_positions, resize_positions, approve, strategies (list/enable/disable/reload), `universe_add`, `buy_option`, `sell_option`, `risk`, `scan`, `ideas` (with `--location` for international markets), `listen`, `watch`
- **data_service required**: history_massive, history_twelvedata, history_ib
- **massive_api_key or twelvedata_api_key** (no service needed): balance_sheet, income_statement, cash_flow, ratios, `data_download`, `ideas` (default US path), `forex_snapshot`, `forex_quote`, `forex_convert`, and the live ticker dashboard (`watch SYM... --source twelvedata`) — all accept `source="massive"|"twelvedata"` (`forex_*` also takes `"ib"` for trader_service routing)
- **Alpaca keys** (no service needed): `movers`, `movers_detail`, `news` default to Alpaca (`source="massive"|"twelvedata"` stays opt-in for movers). `snapshot` / `snapshot_batch` default to IB (trader_service, bid/ask); `source="alpaca"|"twelvedata"` gives REST quotes for US tickers only (no exchange hints, no conIds)
- **Alpaca keys for options data** (no service needed): `options_expirations`, `options_chain`, `options_snapshot`, `options_implied` default to Alpaca's free **indicative** feed (not the OPRA NBBO; greeks/IV only on liquid contracts). `source="massive"` gives OPRA and needs a Massive key with an options plan
- **massive_api_key only** (no service needed): filing_section, `forex_snapshot` (massive source), `forex_movers`, `stream`
- **No service needed**: universe_list, universe_show, universe_create, universe_delete, universe_remove, universe_import, status, market_hours, `data_summary`, `data_query`, `backtest`, `backtest_sweep`, `backtest_batch`, `backtests_list`, `backtests_show`, `backtests_confidence`, `backtests_archive`, `backtests_unarchive`, `sweep_run`, `sweeps_list`, `sweeps_show`, `strategies_inspect`, `strategy_create`, `strategy_deploy`, `strategy_undeploy`, `strategy_signals`, `strategy_backtest`, `propose`, `proposals`, `reject`, `session_limits`, `session_status`, `group_list`, `group_create`, `group_delete`, `group_show`, `group_add`, `group_remove`, `group_set`, `logs`

## Reality check first — `preflight()` before anything else

Every MMR session, **call `MMRHelpers.preflight()` once** before doing serious work. It probes the four capability boundaries that silently break trajectories:

```python
pf = await MMRHelpers.preflight()
emit(pf)
```

Returns a dict like:

```json
{
  "trader_service": {"connected": true, "ib_upstream": true, "account": "DUM422056"},
  "ib_data_farms": {
    "ok": true,
    "farms": {"usfarm": "ok", "ushmds": "ok", "secdefil": "ok"},
    "session_conflict": false,
    "subscription_gate": false,
    "upstream_broken": false,
    "diagnosis": ["All probed farms healthy..."]
  },
  "ib_market_data": {"works": false, "reason": "snapshot timed out — paper account likely has no live market-data subscription"},
  "polygon_options": {"expirations_endpoint": true, "chain_endpoint": false, "tier": "free", "provider": "massive", "feed": "opra", "error": "massive refused options chain (NOT_AUTHORIZED: ...); use --source alpaca"},
  "local_data": {"symbols": 43, "bar_sizes": ["1 day", "1 min"], "has_daily": true, "canary_daily_rows": 8},
  "recommendations": [
    "IB live market data is unavailable. Avoid snapshot() / snapshots_batch() loops...",
    "Options chain is not entitled on massive (massive refused options chain (NOT_AUTHORIZED: ...)). implied_move() will fall back to realized-vol; use `--source alpaca` for the free indicative chain.",
    "Only 8 daily bars locally for QQQ. Top up via history_massive(...)."
  ]
}
```

**Branch on the capability matrix instead of firing seven sequential timeouts.** The five most common gotchas:

| Symptom in trajectory | Real cause | Branch on |
|---|---|---|
| `snapshot()` / `history_*` time out on *every* symbol, including liquid US ones (AAPL, SPY) | IB Gateway logged in but data farms aren't connected (codes 2103/2105 broken). Almost always downstream of a session conflict — IBKR Mobile / TWS desktop / web logged in elsewhere as the same user is bumping the Gateway off. | `pf["ib_data_farms"]["ok"]` is false; check `session_conflict`, `farms` map for `"broken"` entries. Drill in with `MMRHelpers.ib_data_farms()` for fresh details. |
| `snapshot()` returns `{"data": null, "timed_out": true}` for every symbol | IB Gateway connected but paper account has no live market-data subscription. `status()` says "connected" — that's misleading. | `pf["ib_market_data"]["works"]` |
| `options_chain()` says `alpaca is not configured` or `massive refused options chain (NOT_AUTHORIZED ...)` | Options default to Alpaca: the Alpaca keys (`ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`) are missing. Or the options source is Massive and its plan has no options data. | `pf["polygon_options"]["chain_endpoint"]` and `pf["polygon_options"]["error"]`; for Massive, use `source="alpaca"` |
| `data_query("...", bar_size="1 day")` returns `[]` despite `data_summary` listing the symbol | Symbol is registered in the universe (so `data summary` shows it), but the actual stored bars are at a different `bar_size` (often 1 min only). | `pf["local_data"]["bar_sizes"]`, or just look at `data_query()`'s new `hint` field on empty results |
| Local DuckDB has the symbol but only ~10 bars | Contract registered far back but data was never backfilled. Realized-vol estimates need ≥20–30 bars. | `pf["local_data"]["canary_daily_rows"]`; top up via `history_massive()` or `data_download()` |

When `ib_market_data.works` is False — the most common failure mode on paper accounts — every price/quote question routes through:
- **prices**: `data_query()` → fall back to `history_massive()` (downloads then queries)
- **expected move / vol**: `implied_move()` (handles fallback chain internally)
- **options chains**: `options_chain()` works on the free Alpaca indicative feed (needs Alpaca keys). If `pf["polygon_options"]["chain_endpoint"]` is false, pivot to `implied_move()` for vol estimates

## Earnings / event-driven moves — `implied_move()`

Single helper that wraps the canonical "what's the implied move through this expiration?" calc with a fallback to realized vol. Use this instead of hand-rolling ATM straddles.

```python
# Implied move for GOOGL through Friday (e.g. earnings tonight)
mv = await MMRHelpers.implied_move("GOOGL", expiration="2026-05-01")
emit(mv)
```

Returns:

```json
{
  "symbol": "GOOGL",
  "expiration": "2026-05-01",
  "dte_calendar": 2,
  "method": "atm_straddle",           // or "realized_vol"
  "confidence": "medium",              // "high" for an OPRA straddle (--source massive), "medium" for Alpaca indicative or realized vol with ≥30 bars
  "provider": "alpaca", "feed": "indicative",   // the options chain the straddle came from
  "spot": 167.45,
  "implied_move_pct": 5.42,            // 1-sigma % move
  "implied_move_dollar": 9.07,
  "expected_low": 158.38,
  "expected_high": 176.52,
  "atm_strike": 167.5, "call_mid": 4.7, "put_mid": 4.4,
  "annualized_vol_pct": null,
  "notes": "ATM straddle from the alpaca options chain (indicative feed; indicative quotes, not OPRA NBBO)."
}
```

**Method selection:** `prefer="auto"` (default) tries the ATM straddle from the options chain first (Alpaca's free indicative feed by default). Confidence is `high` only on an OPRA feed (Massive), `medium` otherwise. It falls back to realized vol from local OHLCV (with Massive `list_aggs` top-up if local is thin) and says why in `fallback_reason` when: the chain is empty ("No chain data"), the source returns `NOT_AUTHORIZED`, there is no spot price, no strike has both a call and a put price, or the chain call times out, returns no JSON or raises. Any other failure the CLI reports (for example missing Alpaca keys, a rate limit) comes back in the result's `error`, not as a silent realized-vol number. Pass `prefer="realized"` to skip the chain attempt entirely (saves ~3s) when you already know from `preflight()` that the chain endpoint is unavailable.

**Earnings caveat:** realized vol is a baseline, not a market-implied number. Earnings/event-driven moves often **double or triple** the realized-vol estimate. Treat it as a floor, not a forecast. The result's `notes` field flags this.

**By DTE shortcut:** when you don't know the exact expiration, pass `dte=2` and the helper picks today + 2 calendar days (rolled to the next trading day if it lands on a weekend).

```python
mv = await MMRHelpers.implied_move("AAPL", dte=2)   # default-pick expiry
```

## Picking a data source

Most data-fetching helpers accept `source="massive"|"twelvedata"`. Quick rules:

- **Default is `"massive"`** for every method that takes `source`. Keep it unless you have a reason to switch.
- **TwelveData for fundamentals depth** — `ratios(..., source="twelvedata")` returns ~60 flat-keyed fields (valuations, margins, MRQ balance sheet, TTM cash flow, share stats, dividend history) vs Massive's ~11 TTM ratios.
- **Massive for filings + bulk forex** — TwelveData has no SEC filing sections, no full-market `forex_snapshot_all`, no `forex_movers`, and no L2 depth. Everything else (`snapshot`, `snapshot_batch`, `forex_snapshot`, `forex_quote`, `forex_convert`, `movers`, `movers_detail`, `ratios`, balance sheet/income/cash flow, history, ideas, live `watch` streaming) now has a TwelveData branch.
- **REST snapshots have no bid/ask on TwelveData** — `snapshot(..., source="twelvedata")` returns OHLC + last + change_pct via `/quote`; `bid` and `ask` come back as `NaN`. To get streaming bid/ask from TwelveData, use `watch SYM... --source twelvedata` (WebSocket), which requires a TD Pro plan or higher.
- **TwelveData for 1-min data with pre/post-market** — intraday (1/5/15/30-min) defaults to `prepost=true` returning ~960 bars/day (04:00-19:59 ET). Massive returns 24h. If you care about overnight prints, stay on Massive; if you want regular+extended session and nothing else, TwelveData is fine.
- **Watch rate limits on TwelveData.** A Grow plan is 610 credits/min and `get_statistics` costs ~100 credits per call. Scanner's `_fetch_fundamentals` now handles this gracefully — it short-circuits remaining tickers when it sees a rate-limit error and returns partial fundamentals rather than raising. Single-shot `ratios(source="twelvedata")` in tight loops will hit the wall after ~6 calls.
- **Known data-quality gotcha on TwelveData**: specific session dates show ~95% volume under-reporting across multiple symbols (2025-04-28, 2025-07-03, 2026-04-15 observed in the batch we downloaded). Prices on those days are correct; bar counts are correct; only volume is off. If a strategy leans on absolute volume, cross-check against Massive for those days before trusting it.

## International Stocks (ASX, TSE, SEHK, etc.)

Many commands accept `exchange` and `currency` hints for international stocks. Without these hints, resolution defaults to US markets (USD/SMART). For example:

```python
# US stock (default)
result = await MMRHelpers.resolve("AMD")

# Australian stock — must specify exchange/currency
result = await MMRHelpers.resolve("BHP", exchange="ASX", currency="AUD")
result = await MMRHelpers.buy("BHP", market=True, quantity=100, exchange="ASX", currency="AUD")
result = await MMRHelpers.snapshot("BHP", exchange="ASX", currency="AUD")
result = await MMRHelpers.ideas("momentum", location="STK.AU.ASX", tickers=["BHP", "RIO", "FMG"])
```

Common location codes: `STK.US.MAJOR` (US), `STK.AU.ASX` (Australia), `STK.CA` (Canada), `STK.HK.SEHK` (Hong Kong), `STK.JP.TSE` (Japan), `STK.EU` (Europe).

## MMRHelpers

**Return convention — read this once:** every `MMRHelpers.*` method returns a **JSON string** (not a dict), even when the table below says "JSON dict". As of v2.1 most query helpers (``data_summary``, ``backtests_list``,
``backtests_show``, ``backtests_confidence``, ``sweeps_list``,
``sweeps_show``, ``proposals``, ``proposal_show``, ``strategies``,
``status``, ``data_freshness``, ``strategy_provenance``) return a
**dict directly** — no ``json.loads`` wrapper needed. Iterate them
with ``result["data"]`` / ``for entry in result["data"]: ...`` straight
out of the box.

A handful of long-tail helpers (mostly write/action commands like
``backtest``, ``backtest_batch``, ``backtest_sweep``) still return a
JSON-encoded string for back-compat. Wrap those in ``json.loads(...)``
before indexing:

```python
raw = await MMRHelpers.backtest_batch(jobs, concurrency=6)
parsed = json.loads(raw)          # now a dict
for entry in parsed["data"]:      # now indexable
    ...
```

Common field-name gotchas in the backtest summary: `total_return` (NOT `return_pct`), `sharpe_ratio` (NOT `sharpe`), `total_trades` (NOT `trades`), `max_drawdown` (negative float, e.g. `-0.08`), `profit_factor` can be the string `"inf"` on all-winners runs. See the `backtest` docstring for the full schema.

### Portfolio & Account

| Method | Description |
|--------|-------------|
| `MMRHelpers.portfolio()` | positions with P&L (symbol, position, mktPrice, avgCost, unrealizedPNL, dailyPNL) |
| `MMRHelpers.portfolio_snapshot()` | compact: total_value, daily_pnl, position_count, top movers by % change |
| `MMRHelpers.portfolio_diff()` | delta since last call: changed, new, removed positions (auto-stores snapshot) |
| `MMRHelpers.positions()` | Raw positions (symbol, secType, position, avgCost, currency) |
| `MMRHelpers.orders()` | open orders (orderId, action, orderType, lmtPrice, totalQuantity) |
| `MMRHelpers.trades()` | active trades with fill status |
| `MMRHelpers.account()` | IB account ID |
| `MMRHelpers.status()` | service health with PnL (DailyPnL, UnrealizedPnL, RealizedPnL, TotalPnL) |
| `MMRHelpers.ib_data_farms(timeout=10)` | Direct IB Gateway data-farm health probe — distinguishes "broken farm" / "session conflict" / "subscription gate" / "upstream outage". Use when snapshots / history time out, before assuming a subscription issue. |
| `MMRHelpers.market_hours()` | market open/close status for major exchanges |

### Symbol Resolution & Market Data

| Method | Description |
|--------|-------------|
| `MMRHelpers.resolve(symbol, sectype="STK", exchange="", currency="")` | resolve ticker to IB contract (conId, exchange, secType, longName) |
| `MMRHelpers.snapshot(symbol, delayed=False, exchange="", currency="")` | price snapshot (bid, ask, last, OHLC). Access: `result["data"]["last"]` |
| `MMRHelpers.snapshots_batch(symbols, exchange="", currency="")` | batch snapshots for multiple symbols in one call (~4s total). Much faster than calling snapshot() in a loop |
| `MMRHelpers.depth(symbol, rows=5, exchange="", currency="", smart=False, no_chart=False)` | Level 2 order book table + PNG depth chart |
| `MMRHelpers.depth_json(symbol, rows=5, exchange="", currency="", smart=False)` | order book with `chart_path` to PNG image for visual analysis |

### Trading

| Method | Description |
|--------|-------------|
| `MMRHelpers.buy(symbol, quantity=, amount=, limit_price=, market=, exchange=, currency=)` | Place buy order |
| `MMRHelpers.sell(symbol, quantity=, amount=, limit_price=, market=, exchange=, currency=)` | Place sell order |
| `MMRHelpers.cancel(order_id)` | Cancel order by ID |
| `MMRHelpers.cancel_all()` | Cancel all open orders |
| `MMRHelpers.close_all_positions()` | Close all positions at market (cancels orders first, bypasses risk gate) |
| `MMRHelpers.resize_positions(max_bound=, min_bound=, dry_run=)` | Proportionally resize all positions |

Buy/sell require: `market=True` or `limit_price=X`. Plus: `quantity=N` (shares) or `amount=N` (dollars).

### Trade Proposals & Position Sizing

Trade proposals are stored locally and auto-sized based on confidence, ATR volatility, and `position_sizing.yaml` config. Create proposals without trader_service; approve requires trader_service.

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.propose(symbol, action, confidence=, reasoning=, group=, stop_loss=, take_profit=, trailing_stop_pct=, tif=, allow_duplicate=, ...)` | No* | Create proposal with auto position sizing, optional protective exits, and a PENDING-duplicate guard |
| `MMRHelpers.proposals(status=, all_statuses=)` | No | List proposals |
| `MMRHelpers.approve(proposal_id)` | **Yes** | Execute a proposal — enforces the evaluation checklist (`CHECKLIST_INCOMPLETE` otherwise) |
| `MMRHelpers.reject(proposal_id, reason=)` | No | Reject a proposal (never gated) |
| `MMRHelpers.session_status()` | No | Full sizing config + portfolio state + capacity (JSON) |
| `MMRHelpers.session_limits()` | No | View position sizing hard limits |

*Auto-sizing requires trader_service for snapshot/ATR data; gracefully degrades without it. On the typed command path auto-sizing may be disabled — a `SIZING_BLOCKED` refusal means re-propose with an explicit `amount=`.

**Protective exits on propose**: `stop_loss=` alone attaches a stop;
`stop_loss=` + `take_profit=` become a bracket; `trailing_stop_pct=` attaches
a trailing stop (exclusive with the fixed exits); `tif="GTC"` keeps protection
alive beyond the session. `take_profit` alone or trailing+fixed combinations
are refused with `INVALID_EXIT` before any CLI call.

**Duplicate guard on propose**: an existing PENDING proposal for the same
symbol+action refuses with `DUPLICATE_PENDING` (decide the existing one
instead); `allow_duplicate=True` bypasses for a deliberate second tranche.
The check fails open if the pending list is unreadable.

**Sizing pipeline**: `base_position × risk_multiplier × confidence_scale × volatility_adjustment`. With `base_position_pct=0.02` and a $1M account, base is $20K. Volatile stocks (high ATR%) get smaller positions; stable stocks get larger ones. The `sizing_result` in proposal metadata shows the full reasoning chain.

**State machine (important for error handling)**: proposal status transitions are enforced: `PENDING → APPROVED | REJECTED | EXPIRED | FAILED`, then `APPROVED → EXECUTED | FAILED | REJECTED`. Terminal states (`EXECUTED`, `REJECTED`, `EXPIRED`, `FAILED`) are immutable. Practical consequences:

- `approve(pid)` on a non-`PENDING` proposal returns `SuccessFail.fail` with `Proposal #<id> is <status>, not PENDING` — don't treat this as a retryable error, the proposal has already taken its terminal path.
- `reject(pid)` on a non-`PENDING` proposal returns `False`. Check the status first if you care.
- If `place_expressive_order` itself fails on the trader_service side (e.g. margin rejection or the risk gate denies it), the proposal moves to `FAILED`, not back to `PENDING`. Create a new proposal rather than trying to re-approve the failed one.

**Bracket orders are transactional**: when a proposal has `execution.exit_type='BRACKET'`, all three legs (entry + take-profit + stop-loss) succeed together or none of them are transmitted to IB. If the TP or SL leg is rejected, the already-staged entry is cancelled and `approve()` returns `SuccessFail.fail` with a message starting `"Bracket aborted:"`. Previously a rejected TP could leave you with entry + SL only (position without a take-profit); that hole is closed.

### Position Groups

Named groups with allocation budgets for organizing thematic trades. All group operations are local (no trader_service needed).

| Method | Description |
|--------|-------------|
| `MMRHelpers.group_list()` | List all groups with members and budgets (JSON) |
| `MMRHelpers.group_create(name, budget=, description=)` | Create group (budget in %, e.g. 20 = 20%) |
| `MMRHelpers.group_delete(name)` | Delete group and members |
| `MMRHelpers.group_show(name)` | Group detail with members (JSON) |
| `MMRHelpers.group_add(name, symbols)` | Add symbols to group |
| `MMRHelpers.group_remove(name, symbol)` | Remove symbol from group |
| `MMRHelpers.group_set(name, budget=, description=)` | Update group settings |

Using `group=` in `propose()` auto-adds the symbol to the group.

### Portfolio Risk Analysis

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.portfolio_risk()` | **Yes** | Concentration (HHI), group budgets, correlation clusters, warnings (JSON) |

The risk report includes:

- `hhi` — Herfindahl-Hirschman Index on **gross** weights (0=diversified, 1=fully concentrated)
- `gross_exposure_pct` — total absolute market value / net liquidation
- `net_exposure_pct` — **signed** exposure; negative = net short, ~0 on a fully hedged book
- `long_exposure_pct` / `short_exposure_pct` — same breakdown for the long and short sides
- `top_positions` by absolute weight; each entry has both `pct` (gross) and `signed_pct` (direction-aware), plus `is_short: bool`
- `group_allocations` with over-budget flags
- `correlation_clusters` (symbols with >0.7 correlation) with both `combined_weight_pct` (gross) and `net_weight_pct` (signed). Warnings fire on **net** so a correlated long+short pair doesn't false-alarm.
- `warnings` (critical >15%, warning >10% single-position concentration, group over budget, correlated cluster with |net exposure| >30%)
- `summary` — plain-English paragraph; explicitly says "hedged" when gross and |net| diverge

If you're running a long/short book, read `net_exposure_pct` first — a $1M long + $1M short book shows `gross_exposure_pct=2.0` but `net_exposure_pct=0.0`, and the HHI-based concentration warnings are telling you about gross exposure, not real risk.

### Market Scanning & Ideas

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.ideas(preset, tickers=, universe=, num=, location=, source=, ...)` | No*/Yes** | scan for trading ideas with technical scoring |
| `MMRHelpers.news(ticker="", limit=10, detail=False)` | No (needs Alpaca keys by default) | Market news. The helper has no `source` parameter: it uses the CLI default (Alpaca, no sentiment). Sentiment needs the CLI `mmr news --source polygon` until the helper gains a source parameter (phase 9). JSON items use `summary` (was `teaser`/`description`) |
| `MMRHelpers.movers(market="stocks", losers=False, num=20, source="massive")` | No* | Top market movers |

*Requires `massive_api_key` (default) or `twelvedata_api_key` (when `source="twelvedata"`). **Requires trader_service when using `location=` for international markets; `location=` overrides `source=`.

Presets: `momentum`, `gap-up`, `gap-down`, `mean-reversion`, `breakout`, `volatile`.

`source="twelvedata"` notes: `news=True` is silently empty (TD has no news endpoint); fundamentals enrichment costs ~100 credits per enriched ticker on a Grow plan.

**Fail-loudly behaviour**: `ideas()` with `location=` raises / returns an error instead of an empty list when the IB scanner returns nothing for that location or when every supplied ticker fails to resolve. This is intentional — those two cases used to be indistinguishable from "no matches", and the usual cause is either a missing market-data subscription (→ use `--tickers`) or a typo. The CLI returns the error in the JSON payload; check for `result.get("error")` and surface the message rather than treating an empty DataFrame as "nothing found."

### Financial Statements

| Method | Description |
|--------|-------------|
| `MMRHelpers.balance_sheet(symbol, limit=4, timeframe="quarterly", source="massive")` | Balance sheet (assets, liabilities, equity) |
| `MMRHelpers.income_statement(symbol, limit=4, timeframe="quarterly", source="massive")` | Income statement (revenue, earnings, margins) |
| `MMRHelpers.cash_flow(symbol, limit=4, timeframe="quarterly", source="massive")` | Cash flow statement (operating, investing, financing) |
| `MMRHelpers.ratios(symbol, source="massive")` | Financial ratios — Massive: ~11 TTM fields; TwelveData: ~60 flat-keyed fields |
| `MMRHelpers.filing_section(symbol, section="business", limit=1)` | 10-K filing section text (Massive only) |

`source="massive"` (default) requires `massive_api_key`; `source="twelvedata"` requires `twelvedata_api_key` (Pro tier). No trader_service needed either way. `timeframe`: `"quarterly"` or `"annual"` (not applicable to ratios/filing).

Shapes deliberately differ between sources — we pass through what each provider returns rather than merging into a synthetic schema. Massive returns Polygon-style camelCase fields; TwelveData returns nested groups flattened to dot-keyed columns like `valuations_metrics.trailing_pe` and `financials.income_statement.revenue_ttm`. Inspect `df.columns` to see what each source gave you.

### Options

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.options_expirations(symbol, source=)` | No* | List expiration dates with DTE |
| `MMRHelpers.options_chain(symbol, expiration=, contract_type=, strike_min=, strike_max=, source=)` | No* | Full chain snapshot (strike, bid/ask, greeks, IV, OI); every row names `provider` and `feed` |
| `MMRHelpers.options_snapshot(option_ticker, source=)` | No* | Single contract detail |
| `MMRHelpers.options_implied(symbol, expiration, risk_free_rate=0.05, source=)` | No* | Market-implied vs constant-vol probability distribution over the quoted strike range (needs 8+ call strikes with an IV) |
| `MMRHelpers.implied_move(symbol, expiration=, dte=, prefer="auto")` | No** | **Expected 1-sigma move through expiration. Tries the ATM straddle from the options chain (labelled with `provider` / `feed`), falls back to realized vol from local OHLCV and says why in `fallback_reason`. Use this for earnings analysis.** |
| `MMRHelpers.buy_option(symbol, expiration, strike, right, quantity, limit_price=, market=)` | **Yes** | Buy option contracts |
| `MMRHelpers.sell_option(symbol, expiration, strike, right, quantity, limit_price=, market=)` | **Yes** | Sell option contracts |

\* Default source: Alpaca's free **indicative** feed. Needs `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`. Indicative is not the OPRA NBBO, and illiquid contracts have no greeks/IV. `source="massive"` gives OPRA and needs a Massive key with an options plan (otherwise `NOT_AUTHORIZED`). Use `preflight()` to check `pf["polygon_options"]["chain_endpoint"]`.
\** The ATM-straddle method uses the options source above. The realized-vol fallback needs local OHLCV or a Massive key. Missing keys, rate limits and bad input come back in `error`, so check `error` and `method` before using the number.

`right`: `"C"` for call, `"P"` for put. `option_ticker` format: `AAPL260320C00250000` (symbol + YYMMDD + C/P + strike*1000 zero-padded to 8 digits). The Massive form `O:AAPL260320C00250000` is also accepted; chain rows always use the bare form.

### Strategies

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.strategies()` | **Yes** | List strategies (name, state, paper, bar_size) |
| `MMRHelpers.enable_strategy(name)` | **Yes** | Enable a strategy |
| `MMRHelpers.disable_strategy(name)` | **Yes** | Disable a strategy |
| `MMRHelpers.reload_strategies()` | **Yes** | Reload strategies from YAML + re-subscribe (immediate reconciliation) |

### Strategy Development (No Service Required)

| Method | Description |
|--------|-------------|
| `MMRHelpers.strategy_create(name)` | Create template strategy file in `strategies/` |
| `MMRHelpers.strategies_inspect()` | **Call first.** AST scan returning each class's dispatch mode + tunable params. |
| `MMRHelpers.backtest(path, class, conids, params={...}, summary_only=True, ...)` | Single backtest. Pass an **absolute** `path`; `params` overrides class attrs (e.g. `{"EMA_PERIOD":15}`). |
| `MMRHelpers.backtest_sweep(path, class, param_grid={...}, conids, concurrency=N)` | Cartesian sweep. Use `concurrency≥4` for > ~10 combos or it will time out; fans out via `backtest_batch`. |
| `MMRHelpers.backtest_batch([jobs], concurrency=4)` | Heterogeneous parallel batch (mixed strategies/symbols). Each job = `{strategy_path, class_name, conids, days, params, ...}`. |
| `MMRHelpers.backtests_list(sort_by="score", limit=25, sweep_id=None)` | Past runs ranked by composite quality score. Filter by strategy, sweep, or archive state. |
| `MMRHelpers.backtests_show(run_id, include_raw=False)` | One run's full detail + statistical-confidence block. `include_raw=False` (default) omits MB-scale arrays. |
| `MMRHelpers.backtests_confidence([ids])` | Bulk PSR/t-stat/CI/skew/streak for N runs (~500 bytes/run). The right post-sweep ranking tool. |
| `MMRHelpers.sweep_run(manifest_path, dry_run=False, concurrency=None)` | **Cron-able nightly sweep.** YAML manifest → expand → freshness-check → parallel run → digest in `~/.local/share/mmr/reports/`. See Pattern 14. |
| `MMRHelpers.sweeps_list(limit=25)` | **Curated view** of what sweeps have ever run. Entry point for "what have we done?" |
| `MMRHelpers.sweeps_show(id, top=10)` | One sweep's metadata + top-N leaderboard by composite score. |
| `MMRHelpers.backtests_archive([ids])` / `backtests_unarchive([ids])` | Soft-delete / restore runs — hides from default list without losing the data. Pass `include_archived=True` or `archived_only=True` to `backtests_list` to see hidden runs. |
| `MMRHelpers.strategy_deploy(name, conids)` | Deploy to `strategy_runtime.yaml`. Pass `paper_only=True` to gate the entry off live trader_service (safety flag for new/untested strategies). |
| `MMRHelpers.strategy_undeploy(name)` | Remove from config |
| `MMRHelpers.strategy_signals(name, limit=20)` | View recent signals from event store |
| `MMRHelpers.strategy_backtest(name, days=365)` | Backtest a deployed strategy by name |

Three things to know when working with backtests:

1. **Module path must stay under `strategies_directory`** — the loader sandboxes paths and rejects absolute paths or `../` traversal. Stick to `strategies/my_strategy.py` (which is what `strategy_create` produces); strategy YAML loaded via `yaml.safe_load` also refuses `!!python/object` tags.
2. **Backtests fill at next-bar open by default (`fill_policy='next_open'`)** — a signal emitted on bar `t` executes at bar `t+1`'s open, not bar `t`'s close. Results from older MMR versions used the biased same-close path; if your numbers look worse than you remember, this is likely why.
3. **`backtests_list` ranks by composite quality score by default** — weighted blend of sortino, profit_factor, expectancy_bps, return, and drawdown, gated by a reliability factor that penalises low trade counts (< 10 → ×0.2, < 30 → ×0.6). Use `sort_by="time"` for chronological order, or any individual metric (`sharpe`, `return`, `pf`, `expectancy`, `calmar`, `max_dd`, etc.). See `references/STRATEGIES.md` for the full evaluation guide including the statistical-confidence tests surfaced by `backtests_show`.

### Data Exploration (No Service Required)

| Method | Description |
|--------|-------------|
| `MMRHelpers.data_summary()` | What historical data is available locally |
| `MMRHelpers.data_query(symbol, bar_size, days)` | Read OHLCV data from local DuckDB |
| `MMRHelpers.data_download(symbols, bar_size, days, source="massive", force=False)` | Download to local DuckDB |

`data_download` uses the freshness guard by default — repeat calls over the same window are a no-op (no API credits burned). Pass `force=True` only when you want to re-fetch stored days to pick up new coverage (e.g. switching sources, or after enabling TwelveData's extended-hours default). TwelveData intraday (1/5/15/30-min) defaults to `prepost=true` → ~960 bars per US trading day (04:00–19:59 ET); Massive returns 24h. All methods return JSON.

### Universe Management

Universes are named collections of stock definitions stored in DuckDB.

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.universe_list()` | No | List all universes with symbol counts |
| `MMRHelpers.universe_show(name)` | No | Show symbols in a universe |
| `MMRHelpers.universe_create(name)` | No | Create empty universe |
| `MMRHelpers.universe_delete(name)` | No | Delete universe (auto-confirms) |
| `MMRHelpers.universe_add(name, symbols, exchange="", currency="")` | **Yes** | Resolve via IB and add symbols. **Pass `exchange` + `currency` for non-US** — `exchange="ASX", currency="AUD"` for ASX, `SEHK`/`HKD`, `TSE`/`JPY`, etc. Without them resolve() defaults to SMART/USD and silently picks the US-listed ADR or fails. |
| `MMRHelpers.universe_remove(name, symbol)` | No | Remove a symbol |
| `MMRHelpers.universe_import(name, csv_file)` | No | Bulk import from CSV |

`universe_add` takes a list of ticker strings: `["AAPL", "MSFT", "AMD"]`.

### Historical Data

| Method | Description |
|--------|-------------|
| `MMRHelpers.history_massive(symbol=, universe=, bar_size="1 day", prev_days=30)` | Download from Massive.com (via data_service RPC) |
| `MMRHelpers.history_twelvedata(symbol=, universe=, bar_size="1 day", prev_days=30)` | Download from TwelveData (via data_service RPC) |
| `MMRHelpers.history_ib(symbol=, universe=, bar_size="1 min", prev_days=5)` | Download from IB (via data_service RPC) |

Must specify either `symbol` or `universe`. All three require `data_service` to be running. For no-service direct pulls to the local DuckDB, use `data_download(symbols, source=...)` instead.

**Bar sizes**: `1 secs`, `5 secs`, `10 secs`, `15 secs`, `30 secs`, `1 min`, `2 mins`, `3 mins`, `5 mins`, `10 mins`, `15 mins`, `20 mins`, `30 mins`, `1 hour`, `2 hours`, `3 hours`, `4 hours`, `8 hours`, `1 day`, `1 week`, `1 month`

### Forex

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.forex_snapshot(pair, source="ib")` | Yes/No* | Forex pair snapshot |
| `MMRHelpers.forex_movers(losers=False)` | No* | Top forex movers |

*Requires `massive_api_key` for massive source.

### Risk & Session

| Method | Service? | Description |
|--------|----------|-------------|
| `MMRHelpers.risk()` | **Yes** | View risk gate limits |
| `MMRHelpers.session_status()` | No | Full sizing config + portfolio state + capacity (JSON) |
| `MMRHelpers.session_limits()` | No | View position sizing hard limits |
| `MMRHelpers.portfolio_risk()` | **Yes** | Portfolio risk analysis (JSON) |

### Escape Hatch

| Method | Description |
|--------|-------------|
| `MMRHelpers.cli(command)` | Run any CLI command directly (e.g. `MMRHelpers.cli("resolve AAPL")`) |

## Concurrency & Error Handling

Three rules that cover 95% of the failure modes:

**1. Don't over-parallelize `trader_service` RPC calls.** Everything that goes through `trader_service` (resolve, snapshot, buy/sell, orders, portfolio, approve, etc.) shares one ZMQ connection — firing an `asyncio.gather` of 8 of them cascades into timeouts. Serialize them in a `for` loop. Use `snapshots_batch(symbols)` instead of N × `snapshot()`. You *can* parallelize across different backends (e.g. `movers` + `ideas` — both Massive.com) or across subprocess-level helpers (`backtest_batch`, `backtest_sweep(concurrency=N)`, `sweep_run`) which spawn independent CLI processes.

**2. Always `await` in the calling cell.** The helper runtime has no running event loop for `asyncio.create_task`, and `asyncio.run()` nests badly. Just `await MMRHelpers.x(...)` — the helper already offloads the CLI subprocess to a worker thread so it doesn't block. For long downloads pass `timeout=` or chunk the work across cells. **Exception:** for genuinely long-running work (sweeps > ~20 min, multi-hour nightly batches) `await` is the wrong tool — it freezes the conversation the whole time. Use the **detached-subprocess + poll** pattern in Pattern 14b instead; the underlying work persists its own state so the LLM can check progress from any later cell.

**3. Timeouts return error payloads, not exceptions.** JSON helpers return `{"error": "timed out ...", "timed_out": True}`; string helpers return `"ERROR: ..."`. Check both:

```python
result = await MMRHelpers.resolve("AAPL")
if result.get("timed_out") or result.get("error"):
    # trader_service is unresponsive — stop calling it until status() recovers
    return
```

If ANY trader_service call times out, call `status()` before retrying. Specific error types (`ValueError`, `ConnectionError`, `TraderException`, `"Bracket aborted: ..."`) are preserved in the message — branch on the phrase if you need to.

## New helpers (v2.1)

These helpers were added to close gaps surfaced in real LLM exploration sessions. Use them whenever the question is "why is this thing in this state?" — they collapse multi-step investigations into one call.

### `proposal_show(id)` — full diagnostic on a single proposal

`proposals()` gives you the list view (id, status, symbol, side). To see *why* a `FAILED` proposal failed, or get the full execution + leverage detail on a `PENDING` one, call `proposal_show(id)`. Returns the complete record including ``rejection_reason`` and metadata.

```python
listed = await MMRHelpers.proposals(all_statuses=True)
failed = [p for p in listed["data"] if p["status"] == "FAILED"]
for p in failed[:3]:
    detail = await MMRHelpers.proposal_show(p["id"])
    print(p["id"], "→", detail.get("rejection_reason"))
```

### `strategy_provenance(name)` — link deployed strategy to source backtest

Closes the "is this strategy actually using the winning params?" loop in one call. Reads ``~/.config/mmr/strategy_runtime.yaml``, extracts the source ``run_id`` (from a structured ``source_run_id:`` field if present, else from the ``description`` text via regex), looks up the matching backtest record, and diffs deployed params against source params.

```python
prov = await MMRHelpers.strategy_provenance("orb_xlk")
print("Source run:", prov["source_run_id"], "from sweep", prov["source_sweep_id"])
print("Score:", prov["source_score"])
if prov.get("params_match") is False:
    print("DRIFT:", prov["params_diff"])
```

### `data_freshness(stale_days, min_history_days)` — auto-detect data anomalies

Replaces eyeballing `data_summary()` for stale dates, missing conIds, or one-symbol-with-much-shorter-history outliers. Returns categorized issues:

  * `stale` — last bar > `stale_days` ago
  * `short_history` — span < `min_history_days`
  * `unresolved_conid` — conId is empty or zero
  * `range_gap` — symbol starts > 30d after peers on the same `bar_size`

```python
fresh = await MMRHelpers.data_freshness(stale_days=7, min_history_days=60)
print(fresh["summary"])
for issue in fresh["issues"][:10]:
    print(f"{issue['symbol']:6} {issue['kind']:18} {issue['detail']}")
```

### Filter args on `backtests_list()`

Now supports `symbol="XLK"` (case-insensitive ticker filter) on top of the existing `strategy=`, `sweep_id=`, `archived_only=` filters. Output also gained `conids`, `symbols`, and `params` fields so you don\'t need a separate lookup.

```python
xlk_runs = await MMRHelpers.backtests_list(symbol="XLK", sort_by="sharpe", limit=10)
for r in xlk_runs["data"]:
    print(r["id"], r["params"], "sharpe", r["sharpe_ratio"])
```

### Self-describing units in `backtests_confidence()`

The confidence block now exposes:

  * `mean_trade_pnl_ci_lo_dollars` / `_hi_dollars` — bootstrap 95% CI on mean per-trade P&L (in account currency)
  * `sharpe_ratio_ci_lo_annualized` / `_hi_annualized` — bootstrap 95% CI on **annualized** Sharpe; should bracket `summary.sharpe_ratio`. If it doesn\'t, the run has either (a) too few `n_bar_returns` (< 30) for a meaningful CI, or (b) a degenerate equity curve (rare-trade strategy where most bar returns are zero).

The bare `return_ci_lo` / `sharpe_ci_lo` field names are kept as deprecated aliases.

## Patterns

### Pattern 0: Pre-Flight Check (ALWAYS do this first)

```python
# 1. Check market hours
hours = await MMRHelpers.market_hours()
emit(hours)

# 2. Check service health and IB connectivity
status = await MMRHelpers.status()
if "error" in status or not status.get("data", {}).get("connected"):
    emit("trader_service is not reachable — can only use Massive.com methods (movers, ideas, news, financials)")
elif status.get("data", {}).get("ib_upstream_connected") == False:
    emit("IB Gateway not connected to IBKR — cannot resolve, snapshot, or trade")
else:
    emit(f"Connected: {status['data'].get('account', '?')}")
    # Now safe to call resolve, snapshot, buy, sell, etc.
```

### Pattern 1: Scan for Ideas and Create Proposals

```python
# Scan US market for momentum ideas (returns JSON dict)
result = await MMRHelpers.ideas("momentum", num=10)
for idea in result["data"]:
    print(f'{idea["ticker"]}: score={idea["score"]}, signal={idea["signal"]}, price={idea["price"]}')

# Scan ASX stocks via IB (use tickers with location for international)
result = await MMRHelpers.ideas("momentum", location="STK.AU.ASX", tickers=["BHP", "RIO"])
for idea in result["data"]:
    print(f'{idea["ticker"]}: {idea["change_pct"]:+.2f}% score={idea["score"]}')

# Create a proposal with auto position sizing and a protective trailing stop
result = await MMRHelpers.propose("AAPL", "BUY", confidence=0.7,
    reasoning="Breakout above 200-day MA on high volume",
    trailing_stop_pct=2.0, tif="GTC")
emit(result)

# Evaluate, then approve (the checklist is enforced — approve refuses
# CHECKLIST_INCOMPLETE without these two fresh calls)
detail = await MMRHelpers.proposal_show(42)
risk = await MMRHelpers.portfolio_risk()
result = await MMRHelpers.approve(42)   # or reject(42, reason="…")
emit(result)
```

### Pattern 2: Risk-Aware Trading Loop (LLM)

```python
# 1. MONITOR — quick check, minimal context
snap = await MMRHelpers.portfolio_snapshot()  # compact JSON
diff = await MMRHelpers.portfolio_diff()       # what changed since last cycle
# If nothing moved (all unchanged), skip to sleep
if diff["data"]["unchanged_count"] == snap["data"]["position_count"]:
    emit("Portfolio flat — skipping analysis")
else:
    emit(f"Changes: {len(diff['data']['changed'])} moved, {len(diff['data']['new'])} new")

# 2. ANALYZE — only if something interesting
risk = await MMRHelpers.portfolio_risk()       # HHI, group budgets, warnings
session = await MMRHelpers.session_status()    # remaining capacity
ideas = await MMRHelpers.ideas("momentum", num=10)  # JSON dict

# 3. PROPOSE — auto-sized, group-tagged, protected, never auto-executes
result = await MMRHelpers.propose("AAPL", "BUY", confidence=0.8,
    reasoning="Strong momentum, RSI 65, above 200-day MA",
    group="tech", source="llm", trailing_stop_pct=2.0, tif="GTC")
# result["data"]["sizing_result"]["reasoning"] shows full ATR pipeline
# result["data"]["proposal_id"] → 42
# A PENDING duplicate for the same symbol+side refuses DUPLICATE_PENDING.

# 4. EVALUATE → DECIDE — enforced checklist, then approve or reject
detail = await MMRHelpers.proposal_show(42)
risk = await MMRHelpers.portfolio_risk()
if not risk["data"]["warnings"]:
    result = await MMRHelpers.approve(42)
else:
    result = await MMRHelpers.reject(42, reason="risk warnings present")
```

Key for loop efficiency: `portfolio_snapshot()` and `portfolio_diff()` return small JSON (~500 tokens) vs `portfolio()` (~1000 tokens JSON). Use snapshot/diff for every cycle, full portfolio only when investigating.

### Pattern 3: Manage Position Groups

```python
# Create thematic groups with allocation budgets
await MMRHelpers.group_create("mining", budget=20, description="Mining & resources")
await MMRHelpers.group_create("banks", budget=25, description="Big 4 banks")
await MMRHelpers.group_add("mining", ["BHP", "RIO", "FMG", "WDS"])
await MMRHelpers.group_add("banks", ["CBA", "NAB", "ANZ", "WBC"])

# Check allocations against budgets
risk = await MMRHelpers.portfolio_risk()
for g in risk["data"]["group_allocations"]:
    status = "OVER" if g["over_budget"] else "OK"
    emit(f'{g["name"]}: {g["pct"]:.1%} / {g["budget_pct"]:.0%} budget [{status}]')

# Proposals auto-register group membership
await MMRHelpers.propose("STO", "BUY", confidence=0.6, group="mining",
    exchange="ASX", currency="AUD")
```

### Pattern 4: Portfolio Management

```python
# View portfolio with P&L
portfolio = await MMRHelpers.portfolio()
emit(portfolio)

# Check service status including PnL summary
status = await MMRHelpers.status()
emit(status)

# Close all positions (emergency liquidation)
result = await MMRHelpers.close_all_positions()
emit(result)

# Resize portfolio to fit within bounds
result = await MMRHelpers.resize_positions(max_bound=500000, dry_run=True)
emit(result)
```

### Pattern 5: Research a Symbol (Including International)

```python
# US stock — all return JSON dicts
info = await MMRHelpers.resolve("AAPL")
snap = await MMRHelpers.snapshot("AAPL")
print(f'AAPL conId: {info["data"][0]["conId"]}')
print(f'AAPL last: {snap["data"]["last"]}, bid: {snap["data"]["bid"]}, ask: {snap["data"]["ask"]}')

# Australian stock — must specify exchange/currency
snap = await MMRHelpers.snapshot("BHP", exchange="ASX", currency="AUD")
print(f'BHP: ${snap["data"]["last"]}, open: ${snap["data"]["open"]}, high: ${snap["data"]["high"]}')

# Batch snapshots — one call for multiple symbols
result = await MMRHelpers.snapshots_batch(["BHP", "CBA", "NAB"], exchange="ASX", currency="AUD")
for s in result["data"]:
    chg = ((s["last"] - s["close"]) / s["close"] * 100) if s["close"] else 0
    print(f'{s["symbol"]}: ${s["last"]:.2f} ({chg:+.2f}%)')
```

### Pattern 6: Market Depth Analysis

```python
# Get order book for a US stock (renders table + saves PNG chart)
result = await MMRHelpers.depth("AAPL")
emit(result)

# Get depth as JSON with chart path for visual analysis
data = await MMRHelpers.depth_json("AAPL")
# data["data"]["bids"] → [{price, size, marketMaker}, ...]
# data["data"]["asks"] → [{price, size, marketMaker}, ...]
# data["chart_path"] → path to PNG depth chart image
emit(str(data))

# International stock depth
result = await MMRHelpers.depth("BHP", exchange="ASX", currency="AUD")
emit(result)

# Depth with more price levels
result = await MMRHelpers.depth("AAPL", rows=10)
emit(result)

# Data only, no chart
result = await MMRHelpers.depth("AAPL", no_chart=True)
emit(result)
```

The depth chart PNG is saved to `~/.local/share/mmr/depth/{SYMBOL}_{timestamp}.png` and can be read as an image file for visual analysis. The chart shows a diverging horizontal bar chart: green bid bars extend left, red ask bars extend right, with spread highlighted in the center.

### Pattern 7: Trade International Stocks

```python
# Buy ASX stock
result = await MMRHelpers.buy("BHP", market=True, quantity=100, exchange="ASX", currency="AUD")
emit(result)

# Create proposal for ASX stock with position sizing
result = await MMRHelpers.propose("WDS", "BUY", confidence=0.75,
    reasoning="LNG producer, rising fuel prices", exchange="ASX", currency="AUD")
emit(result)
```

### Pattern 8: Download Historical Data

```python
# Small daily pull — 300s default timeout is plenty
result = await MMRHelpers.data_download(["AAPL", "MSFT"], bar_size="1 day", days=365)
emit(result)

# Large 1-min pull with live progress — RECOMMENDED for anything > 5 symbols.
# `progress=True` runs one subprocess per symbol and prints a line between each
# ("  [7/25] JPM..." → "    ✓ JPM: 1 downloaded, 0 failed"). `timeout` becomes
# per-symbol instead of total, so 120s is plenty even for 1-min × 730d.
big_list = ["JPM", "BAC", "GS", "WFC", "BLK", "V", "XOM", "CVX", "JNJ", "UNH",
            "LLY", "PFE", "WMT", "HD", "MCD", "KO", "PG", "NKE", "DIS", "BA",
            "CAT", "GE", "UPS", "ORCL", "CRM"]
result = await MMRHelpers.data_download(
    big_list, bar_size="1 min", days=730, progress=True, timeout=120,
)
emit(result)  # summary JSON: {success, completed, failed, total, per_symbol: [...]}

# If you can't use progress (e.g. scripted context with no stdout), bump the
# total timeout to accommodate the whole batch:
result = await MMRHelpers.data_download(
    big_list, bar_size="1 min", days=730, timeout=1800,
)
emit(result)

# Download for an entire universe (also supports timeout kwarg)
result = await MMRHelpers.history_massive(universe="tech_stocks", bar_size="1 day", prev_days=60)
emit(result)
```

### Pattern 9: Analyze Company Financials

```python
bs = await MMRHelpers.balance_sheet("AAPL")
income = await MMRHelpers.income_statement("AAPL", limit=8, timeframe="annual")
cf = await MMRHelpers.cash_flow("AAPL")
ratios = await MMRHelpers.ratios("AAPL")
biz = await MMRHelpers.filing_section("AAPL", section="business")
emit(bs + income + cf + ratios + biz)
```

### Pattern 10: Options Analysis and Trading

```python
exps = await MMRHelpers.options_expirations("AAPL")
chain = await MMRHelpers.options_chain("AAPL", expiration="2026-03-20", contract_type="call", strike_min=200, strike_max=250)
implied = await MMRHelpers.options_implied("AAPL", "2026-03-20")
result = await MMRHelpers.buy_option("AAPL", "2026-03-20", 250.0, "C", 5, market=True)
emit(exps + chain + implied + result)
```

### Pattern 10b: Earnings implied moves and hedge sizing

When several names report tonight (or this week) and you want to size a QQQ
hedge, use `implied_move()` directly. It uses the options chain when it can
and falls back to realized vol (saying why) without you having to bounce
between provider errors and IB timeouts.

```python
# 1. Reality check (only needed once per session)
pf = await MMRHelpers.preflight()
emit(pf["recommendations"])

# 2. Implied moves through Friday for tonight's earnings names
import asyncio
syms = ["GOOGL", "AMZN", "MSFT", "AAPL"]
moves = await asyncio.gather(*[
    MMRHelpers.implied_move(s, expiration="2026-05-01") for s in syms
])
for s, m in zip(syms, moves):
    emit(f"{s}: {m['implied_move_pct']}% via {m['method']} "
         f"({m['confidence']}) — range ${m['expected_low']}-${m['expected_high']}")

# 3. Same for the index hedge candidate
qqq = await MMRHelpers.implied_move("QQQ", expiration="2026-05-01")
emit(qqq)
```

`implied_move()` is safe to fan out via `asyncio.gather` — each call only
touches the REST data providers and local DuckDB, no shared trader_service
connection. (Don't gather `snapshot()` calls — those serialize through
one ZMQ connection.)

### Pattern 11: Build and Test a Strategy (Full Loop)

```python
# 1. Check/download data
summary = await MMRHelpers.data_summary()
result = await MMRHelpers.data_download(["AAPL", "MSFT"], bar_size="1 day", days=365)

# 2. Create strategy template
result = await MMRHelpers.strategy_create("momentum_breakout")
# Edit strategies/momentum_breakout.py with your logic

# 3. Backtest
result = await MMRHelpers.backtest("strategies/momentum_breakout.py", "MomentumBreakout", conids=[265598], days=365)
emit(result)

# 4. Review the run in context of prior runs, and check statistical confidence
ranked = await MMRHelpers.backtests_list(sort_by="score", limit=5)
emit(ranked)  # composite-score ranking of the most recent runs

# Pull the most recent run by run_id for the confidence block (PSR,
# bootstrap CIs, P&L distribution, losing-streak MC). See
# references/STRATEGIES.md for the full decision rule.
detail = await MMRHelpers.backtests_show(run_id=42)
emit(detail)

# 5. Deploy to paper trading (only if confidence tests pass)
result = await MMRHelpers.strategy_deploy("momentum_breakout", conids=[265598])
# strategy_service auto-detects the YAML change within 30s, or force immediate:
result = await MMRHelpers.reload_strategies()

# 6. Monitor
signals = await MMRHelpers.strategy_signals("momentum_breakout")
emit(signals)
```

### Pattern 12: Parameter Sweep (which strategy AND which params)

Strategy discovery + param sweep in three calls. This is the correct way to answer "which strategy works, with what parameters?" on 1-min data — the earlier "read each file, backtest one by one, edit source to try different params" pattern is a dead end.

```python
# 1. One call gets the full landscape — class names, dispatch modes,
# tunable params with defaults, no source-file reading required.
landscape = await MMRHelpers.strategies_inspect()
# Shape: [{class, file, mode, tunables: {KEY: default}, docstring}]
# mode == "precompute" → fast path (safe for 1-min × 1-year)
# mode == "on_prices"  → O(N²) on backtest. Use days=30 or skip.

# 2. Sweep params for one promising fast-path strategy.
# Small grid (<~10 combos) → sequential is fine.
# Larger grid on 1-min data → pass concurrency=4 or it will time out.
sweep = await MMRHelpers.backtest_sweep(
    "/Users/you/dev/mmr/strategies/opening_range_breakout.py", "OpeningRangeBreakout",
    param_grid={
        "RANGE_MINUTES": [10, 15, 20, 30, 45, 60],
        "VOLUME_MULT": [1.0, 1.2, 1.3, 1.5, 1.8],
    },  # 30 combos
    conids=[756733],  # SPY
    days=365,
    concurrency=4,
)
# Returns a leaderboard with run_ids + params. Every run is persisted.
emit(sweep)

# 3. Rank the top candidates by statistical confidence in a single call.
# `backtests_confidence` is the right tool here — `backtests_show` would
# ship megabytes per run; this returns ~500 bytes per run.
top_ids = [e["run_id"] for e in sweep["data"]["leaderboard"][:5] if e["run_id"]]
confidence = await MMRHelpers.backtests_confidence(top_ids)
emit(confidence)

# 4. Pull a single full report only if you want the raw trades/equity curve.
# Default is summary + confidence only (no multi-MB blobs).
best_id = top_ids[0]
best = await MMRHelpers.backtests_show(best_id)
emit(best)
```

### Pattern 13: Parallel Multi-Strategy Fanout

When the jobs aren't a clean grid — e.g. "run 5 different strategies against the same symbol" — use `backtest_batch` for subprocess-level concurrency:

```python
jobs = [
    {"strategy_path": "/abs/path/keltner_breakout.py", "class_name": "KeltnerBreakout",
     "conids": [756733], "days": 180},
    {"strategy_path": "/abs/path/vwap_reversion.py", "class_name": "VwapReversion",
     "conids": [756733], "days": 180},
    {"strategy_path": "/abs/path/opening_range_breakout.py", "class_name": "OpeningRangeBreakout",
     "conids": [756733], "days": 180},
    {"strategy_path": "/abs/path/smi_crossover.py", "class_name": "SMICrossOver",
     "conids": [756733], "days": 180},
]
batch = await MMRHelpers.backtest_batch(jobs, concurrency=4)
emit(batch)

# Leaderboard is ordered by composite score; a losing strategy batch-run
# was an antipattern that took ~16 minutes sequentially — concurrency=4
# cuts that to ~4–5 min.
ranked = await MMRHelpers.backtests_list(sort_by="score", limit=10)
emit(ranked)
```

### Pattern 14: Nightly Sweep (cron-able, morning review)

**Use this when you want to peg CPU overnight and have a digest waiting in the morning.** The manifest is reproducible (lives in git), cron-compatible, and every run is tagged with a parent `sweep_id` so you can always reconstruct what was tried.

**Step 1: Declare the sweep in YAML** (commit this to your repo):

```yaml
# ~/mmr-sweeps/nightly.yaml
sweeps:
  - name: orb_cross_sectional
    strategy: /Users/you/dev/mmr/strategies/opening_range_breakout.py
    class: OpeningRangeBreakout
    symbols: [SPY, QQQ, NVDA, GOOGL, AMD, AAPL, MSFT, META]
    param_grid:
      RANGE_MINUTES: [15, 30, 45]
      VOLUME_MULT: [1.2, 1.3, 1.5]
    days: 365
    bar_size: "1 min"
    concurrency: 8  # or omit for auto (cpu_count - 1)
    note: "nightly"

  - name: keltner_param_tune
    strategy: /Users/you/dev/mmr/strategies/keltner_breakout.py
    class: KeltnerBreakout
    symbols: [SPY, QQQ, NVDA]
    param_grid:
      EMA_PERIOD: [10, 20, 30]
      BAND_MULT: [1.5, 2.0, 2.5]
    days: 365
    bar_size: "1 min"
```

**Step 2: cron it** (macOS/Linux):

```bash
# Runs at 02:00 nightly; log to a file so you can check health in the morning.
0 2 * * * cd /Users/you/dev/mmr && /usr/local/bin/mmr sweep run ~/mmr-sweeps/nightly.yaml >> ~/mmr-sweeps/cron.log 2>&1
```

Or from an LLM/script in-session:

```python
result = await MMRHelpers.sweep_run("/Users/you/mmr-sweeps/nightly.yaml")
emit(result)
```

**Step 3: morning review** — curated, then drill down:

```python
# What has ever been run?
history = await MMRHelpers.sweeps_list(limit=10)
emit(history)
# → each row: {id, name, status, n_runs_successful, digest_path, ...}

# What did last night produce?
last = await MMRHelpers.sweeps_show(sweep_id=17, top=10)
emit(last)
# → leaderboard of last night's runs by composite score, with run_ids.

# Open the markdown digest for the full story (strong candidates, failures, risk flags):
# Or: check statistical confidence on the top 5 in one call.
top_ids = [r["run_id"] for r in last["data"]["leaderboard"][:5]]
confidence = await MMRHelpers.backtests_confidence(top_ids)
emit(confidence)
```

**Why this is better than ad-hoc sweeps:**
- Before any run, `sweeps_list` tells you what's already been done so you don't duplicate work.
- Markdown digest at `~/.local/share/mmr/reports/sweep_<id>_<name>_<timestamp>.md` is readable by humans and by the LLM via `read_file`.
- `sweep run --dry-run` expands the grid and estimates wall time before you commit compute.
- Freshness guard refuses to run on stale data (catches "IB Gateway was down, we sweept yesterday's numbers").
- `--skip-freshness` when you know better.

### Pattern 14b: Interactive detached sweep (launch, keep working, poll)

**Use this when you want to kick off a long-running sweep *now* and keep
working in the same conversation.** `await MMRHelpers.sweep_run(...)` blocks
the cell for the full wall time — fine for a 5-minute sweep, wrong for a
60-minute sweep. You'd freeze the LLM's conversation window the whole time,
and if the cell hits its own timeout the subprocess may get killed
mid-flight.

The trick is that `mmr sweep run` already persists a `sweeps` row to DuckDB
the instant it starts, and stamps every completed job with the parent
`sweep_id`. So a fire-and-forget subprocess with polling works cleanly:

```python
import subprocess

# Launch detached — start_new_session=True puts the process in its own
# session group so the LLM cell's lifecycle doesn't take it down.
log_path = "/tmp/phase1_sweep.log"
proc = subprocess.Popen(
    ["mmr", "sweep", "run", manifest_path, "--skip-freshness"],
    stdout=open(log_path, "w"),
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(f"Sweep PID {proc.pid}, log {log_path}")
```

**Poll from any later cell** — `sweeps_list` is cheap and gives you the
newest sweep's status, and `sweeps_show` gives a running leaderboard:

```python
# What's the newest sweep doing?
history = await MMRHelpers.sweeps_list(limit=3)
emit(history)
# → each row has: id, name, status ("running"/"completed"/"failed"),
#   n_runs_successful, started_at, digest_path (null until done)

# Drill into the newest sweep's partial leaderboard
latest_id = history["data"][0]["id"]
leaderboard = await MMRHelpers.sweeps_show(sweep_id=latest_id, top=10)
emit(leaderboard)
```

**When the sweep is done,** `status` will flip to `"completed"` (or
`"failed"` / `"cancelled"`) and `digest_path` will be populated. At that
point pull the confidence block for the top picks (same as Pattern 14):

```python
show = await MMRHelpers.sweeps_show(latest_id, top=5)
top_ids = [r["run_id"] for r in show["data"]["leaderboard"][:5]]
confidence = await MMRHelpers.backtests_confidence(top_ids)
emit(confidence)
```

**Recovering from a stuck sweep:** if `status` stays `"running"` long after
the estimated wall time, the subprocess is either still grinding or has
died without updating the row. Check the log file first (`cat /tmp/phase1_sweep.log`);
if the process is gone you can mark the row cancelled via
`sweeps_cancel(latest_id)` (if exposed) or just let it sit — the digest
won't write, but the successful child rows in `backtest_runs` are still
valid and visible through `backtests_list(sweep_id=latest_id)`.

**Don't try to background with `asyncio.create_task`** — the helper runtime
has no running event loop to attach to, and the subprocess tree collapses
when the cell exits. The `subprocess.Popen(..., start_new_session=True)`
pattern above is the only one that survives the cell.

### Pattern 15: Market Scanning Workflow

```python
# Check market hours
hours = await MMRHelpers.market_hours()
emit(hours)

# Get top movers
movers = await MMRHelpers.movers()
emit(movers)

# Get news for a ticker
news = await MMRHelpers.news("AAPL", detail=True)
emit(news)

# Run sector scan with specific tickers
ideas = await MMRHelpers.ideas("momentum", tickers=["XOM", "CVX", "COP", "EOG"])
emit(ideas)
```

### Pattern 16: News Enrichment (WHY is it moving?)

The `mmr news` command (Polygon headlines) tells you WHAT happened.
For WHY — actual article bodies — there's a separate optional service
at `~/dev/news` exposing an HTTP scraper at `http://127.0.0.1:8089`.
It handles Cloudflare, paywalls (via archive.ph fallback), and returns
clean Markdown. Three helpers wrap it:

```python
# Direct: scrape one URL
result = await MMRHelpers.news_fetch("https://www.ft.com/content/...")
if result.get("ok"):
    print(result["article"]["markdown"][:2000])

# Search the web for recent items
hits = await MMRHelpers.news_search("AMD earnings", limit=10)
for r in hits.get("data", []):
    print(r["source"], r["published_at"], r["title"])

# Composite: search + parallel-scrape bodies for a ticker
articles = await MMRHelpers.news_enrich("AMD", limit=3)
# → 3 dicts: {title, url, source, published_at, markdown_preview, ok}

# Pre-market / pre-open sweep across an entire universe — find which
# symbols had overnight catalysts. Default headlines-only (~30s for
# 50 syms); set include_bodies=True for excerpts (slower).
result = await MMRHelpers.news_universe("asx", since_hours=12)
for row in result.get("data", []):
    print(f'{row["symbol"]}: {row["count"]} articles')
    for a in row["articles"]:
        print(f'  {a["published_at"]} {a["source"]}: {a["title"]}')
# → only tickers with fresh news appear, sorted by article count desc.
# Great for narrowing "what should I look at this morning?"
```

**Each requires the news service to be running.** If unreachable, the
CLI fails with `cd ~/dev/news && ./docker.sh -g` instructions. None of
these block trading paths — they're optional research helpers.

**Inline enrichment on the core commands:**

```python
# `ideas` with full article bodies for the top N — answers
# "what's moving AND why?" in one call
result = await MMRHelpers.ideas("momentum",
    tickers=["AMD", "NVDA", "AAPL"],
    news_bodies=True, news_bodies_limit=3)
# Each row in result["data"][:3] gains news_title / news_url / news_body cols

# `propose` with news context appended to the saved reasoning. The
# next reviewer (LLM or human running `mmr proposals show 42`) sees
# the catalyst as a "## News context (auto-enriched)" section.
prop = await MMRHelpers.propose("AMD", "BUY", confidence=0.8,
    reasoning="Wells Fargo target raise to $615",
    enrich_news=True, enrich_news_limit=3)
```

Both `news_bodies=` and `enrich_news=` degrade silently if the news
service is down — the underlying scan / proposal still completes,
just without article bodies attached. Use when you genuinely want
the WHY recorded — they add real latency (~5s per article scrape)
and aren't free.

## Reviewing a research evaluation (paper only)

When `mmr research evaluate` ends with stage `complete` and state `PAPER_ELIGIBLE`,
you may write the operator review for a **paper** bundle with
`--reviewer-kind llm`. Read the evaluation report first (path in the output). In
each field, say what the evidence shows, not what you hope: the economic reason
for the edge, why it survives the 2x cost stress, the regimes where it fails, the
data limits, the neighbourhood results, operational dependencies, capacity, and
whether one episode dominates. Never review a live bundle; live needs a human.

The fields are `--economic-rationale`, `--edge-survives-costs`,
`--known-failure-regimes`, `--data-limits`, `--parameter-sensitivity`,
`--operational-dependencies`, `--capacity-and-decay` and `--episode-dominance`.
`--artifact-id` is the evaluate output's `artifact_id` and `--decision-id` its
`decision_digest`. Add
`--holdout-opened-once` only after the report confirms the holdout was opened once.

## References

For detailed documentation on specific topics, see:
- **`references/STRATEGIES.md`**: Strategy ABC, Signal fields (incl. `max_hold_bars` / `close_by_time` for day-trading EOD exits), on_prices() contract, tunable-param declaration, vectorization rules for `precompute`, common patterns (MA crossover, RSI, Bollinger, EMA)
- **`references/SWEEPS.md`**: sweep-manifest YAML schema, cron-able overnight pattern, detached launch, CPU/wall-time math, post-sweep review flow (`sweep show`, `backtests confidence`)
- **`references/DATA.md`**: DuckDB schema, BarSize values, data source details, TickStorage patterns, common conIds

## Python SDK Reference

For advanced scripting beyond the helpers, you can use the Python SDK directly.
The SDK lives at `trader/sdk.py` in the MMR project and must run inside the MMR venv.

```python
from trader.sdk import MMR

with MMR() as mmr:
    df = mmr.portfolio()          # pd.DataFrame
    defs = mmr.resolve("AMD")     # List[SecurityDefinition]
    snap = mmr.snapshot("AMD")    # dict with bid/ask/last/OHLC
    book = mmr.depth("AMD")       # dict with bids/asks/last — Level 2 order book
    result = mmr.buy("AMD", market=True, quantity=10)  # SuccessFail[Trade]
    result.is_success()           # bool
    result.obj                    # Trade object on success
    result.error                  # error string on failure

    # International stocks — pass exchange/currency
    snap = mmr.snapshot("BHP", exchange="ASX", currency="AUD")
    result = mmr.buy("BHP", market=True, quantity=100, exchange="ASX", currency="AUD")

    # Close all positions (bypasses risk gate)
    mmr.close_position("BHP", skip_risk_gate=True)

    # Position sizing via proposals (volatility-aware)
    pid, leverage, snapshot = mmr.propose("AAPL", "BUY", confidence=0.7,
        reasoning="Breakout", group="tech")

    # Portfolio risk analysis
    report = mmr.risk_report()  # dict with hhi, warnings, group_allocations, summary

    # Historical data (via data_service RPC)
    result = mmr.pull_massive(symbols=["AAPL"], bar_size="1 day", prev_days=30)
    result = mmr.pull_ib(symbols=["AAPL"], bar_size="1 min", prev_days=5)
```

Key SDK methods: `portfolio()`, `positions()`, `orders()`, `trades()`, `resolve()`, `snapshot()`, `depth()`, `buy()`, `sell()`, `cancel()`, `cancel_all()`, `close_position()`, `close_all_positions()`, `resize_positions()`, `propose()`, `proposals()`, `approve()`, `reject()`, `risk_report()`, `session_status()`, `strategies()`, `enable_strategy()`, `disable_strategy()`, `reload_strategies()`, `check_ib_upstream()`, `account()`, `status()`, `market_hours()`, `subscribe_ticks()`, `pull_massive()`, `pull_ib()`, `data_service_status()`, `balance_sheet()`, `income_statement()`, `cash_flow()`, `ratios()`, `filing_sections()`, `options_expirations()`, `options_chain()`, `options_snapshot()`, `options_implied()`, `buy_option()`, `sell_option()`, `news()`, `movers()`, `ideas()`, `forex_snapshot()`, `forex_movers()`.
