# Agent workflow

How an AI agent explores data, writes and backtests strategies, deploys them to paper, and runs the trading loop.

## Explore → Write → Backtest → Iterate → Deploy

**Step 1: Explore available data** (no service needed)
```bash
mmr --json data summary                                    # What data is in local DuckDB
mmr --json data query AAPL --bar-size "1 day" --days 30    # Read OHLCV from local store
mmr data status                                            # Freshness per (universe, bar_size) job from data_refresh.yaml
```

**Step 2: Download historical data** (no service needed, requires Alpaca keys; `--source massive|twelvedata|ib` to override)
```bash
mmr data download AAPL MSFT --bar-size "1 day" --days 365  # Ad-hoc download
mmr data refresh us_top20_daily                            # Declarative — runs a named job from data_refresh.yaml
mmr data refresh --all                                     # All jobs (what pycron runs daily)
```

**Step 3: Create a strategy**
```bash
mmr strategies create my_strategy                          # Creates strategies/my_strategy.py
# Edit strategies/my_strategy.py with your logic
```

**Step 4: Backtest** (no service needed)
```bash
# Single run
mmr --json backtest -s strategies/my_strategy.py --class MyStrategy --conids 265598 --days 365

# Parameter sweep (cartesian product) — persists one backtest_runs row per combo
mmr bt-sweep -s strategies/my_strategy.py --class MyStrategy --conids 265598 --days 180 \
     --grid '{"FAST":[10,20,30],"SLOW":[40,50,60]}'

# Overnight multi-strategy sweep driven by a YAML manifest — cron-able
mmr sweep run ~/mmr-sweeps/nightly.yaml --dry-run   # expand + estimate
mmr sweep run ~/mmr-sweeps/nightly.yaml             # actually run
```

**Step 4b: Review results** (no service needed)
```bash
mmr sweep list                              # curated: what sweeps have ever run
mmr sweep show 7                            # leaderboard of sweep #7
mmr backtests                               # flat list, ranked by composite quality score
mmr backtests confidence 42 43 44           # PSR / t-test / bootstrap CI / skew / streak MC for N runs
mmr backtests show 42                       # full detail (summary + statistical confidence block)
cat ~/.local/share/mmr/reports/sweep_*.md   # morning digest
```

**Step 5: Deploy to paper trading** (no service needed — writes to config)
```bash
mmr strategies deploy my_strategy --conids 265598 --paper
# strategy_service auto-detects the YAML change within 30s, or force with:
mmr strategies reload
```

**Step 6: Monitor signals** (no service needed)
```bash
mmr --json strategies signals my_strategy
mmr --json portfolio                                       # Requires trader_service
```

## LLM Trading Loop

The preferred workflow for an LLM trading autonomously. Each step is designed to give the LLM the information it needs to make good decisions and catch mistakes before they become real trades.

**Step 1: Assess current state** (requires trader_service)
```bash
mmr --json portfolio-snapshot               # compact: value, P&L, top movers (~500 tokens)
mmr --json portfolio-diff                   # what changed since last cycle
mmr --json portfolio-risk                   # HHI, group budgets, warnings, summary
mmr --json session                          # sizing config, remaining capacity
```
Use `portfolio-snapshot` and `portfolio-diff` every cycle — they're small. If `portfolio-diff` shows `unchanged_count == position_count` (nothing moved), skip the ANALYZE/PROPOSE phases entirely. Only pull the full `portfolio-risk` report when something moved or before approving a trade. The session status shows `remaining_positions` — if at the limit, the LLM should stop proposing.

**Step 2: Scan for opportunities**
```bash
mmr --json ideas momentum --num 10          # US stocks via Alpaca (free, delayed prices)
mmr --json ideas gap-up --tickers AAPL MSFT NVDA  # Specific tickers
mmr --json ideas momentum --location STK.AU.ASX --tickers BHP RIO  # International via IB
```

**Step 3: Research candidates**
```bash
mmr --json snapshot AAPL --source ib        # Current price + bid/ask (bid/ask need IB; REST quotes are Alpaca IEX / TwelveData)
mmr --json news AAPL --detail               # Recent news + sentiment
mmr --json ratios AAPL                      # P/E, ROE, D/E, etc.
```

**Step 4: Create proposals with group tagging**
```bash
mmr --json propose AAPL BUY --market --confidence 0.7 --group tech \
  --reasoning "Strong momentum, RSI 65, above 200-day MA" --source llm
```
Returns JSON with `proposal_id`, `sizing_result` (reasoning chain), `amount`, `group`. Position sizing is automatic: `base × risk × confidence × ATR_volatility`. The `--group` flag auto-registers the symbol into the named group.

**Step 5: Review before approving**
```bash
mmr --json proposals                        # List pending proposals with sizing details
mmr --json proposals show 42                # Full detail — check sizing_result.reasoning
mmr --json portfolio-risk                   # Re-check: would this trade cause any warnings?
```
The LLM should check: (1) the sizing reasoning makes sense, (2) no new risk warnings would be triggered, (3) the group isn't going over budget.

**Step 6: Approve or reject**
```bash
mmr approve 42                              # Execute (requires trader_service)
mmr reject 42 --reason "Group over budget"  # Reject with reason
```

**Key design decisions for the LLM loop:**
- **Propose first, execute later**: The propose→review→approve pipeline means the LLM never places a trade without a chance to review. An LLM can propose freely (no service needed) and review the sizing/risk before committing.
- **ATR-inverse sizing**: The LLM doesn't need to reason about position size — volatile stocks automatically get smaller positions. A $1M account with 2% base, NVDA (3.5% ATR) gets ~$10K while JNJ (1.8% ATR) gets ~$19K.
- **Group budgets as soft limits**: Over-budget groups produce warnings, not rejections. The LLM sees the warning and decides whether to proceed (maybe it has a strong thesis) or redirect to an under-allocated group.
- **Risk report before and after**: Running `portfolio-risk` before proposing catches existing issues; running it after proposing (before approving) catches issues the new trade would create.
- **Snapshot/diff for context efficiency**: `portfolio-snapshot` (~500 tokens) and `portfolio-diff` (only deltas) replace the full `portfolio` call (~2000+ tokens) for loop monitoring. The LLM should use these for every cycle and only pull full portfolio when investigating specific positions.
- **JSON everywhere for the loop**: `propose --json` returns structured data (proposal_id, sizing_result), `portfolio-snapshot`, `portfolio-diff`, `portfolio-risk`, `session`, `group list` all return JSON. The LLM never needs to parse Rich tables during autonomous operation.

## Writing a strategy (details)

Strategies have **two dispatch APIs**; subclass `trader.trading.strategy.Strategy` and implement at least one:

1. **`on_prices(prices)`** — called per bar with the accumulated window. Simple; fine for pandas `.rolling()`.
2. **`precompute(prices) + on_bar(prices, state, index)`** — the **fast path**. `precompute` runs once on the full history; `on_bar` reads precomputed arrays by index. Essential for vectorbt/numba/scipy indicators — a 30-day × 1-min run on `SMICrossOver` went from **hanging > 4 min** (`on_prices`, O(N²)) to **~1 s** (`precompute` + `on_bar`, O(N)) with identical trades.

The backtester calls `on_bar` by default; if a strategy doesn't override it, the default impl falls back to `on_prices(prices.iloc[:index+1])` — so legacy strategies keep working unchanged. The live strategy runtime still dispatches via `on_prices`, so vectorbt strategies should implement both methods (vectorbt in `on_prices` is fine live — it's only the backtest replay that makes it catastrophic).

**Lookahead contract for `precompute`**: values returned must be aligned 1:1 with `prices` such that position `i` depends only on bars `[0..i]`. Rolling/EWM/vectorbt indicators satisfy this; `shift(-1)`, centered rollings, full-series normalization (`x / x.mean()`), and `fit_transform` on the complete history do not. Use `trader.simulation.lookahead_check.assert_no_lookahead(strategy, prices)` in your strategy's test file — it runs `precompute` on the full series AND on progressively-truncated copies and asserts past-index values don't shift when future bars are hidden, catching the common leak patterns before they ship. See `skills/mmr-skill/references/STRATEGIES.md` for a full fast-path example.

**Tunable parameters**: declare them as upper-case class attributes (`EMA_PERIOD = 20`, `BAND_MULT = 2.0`) so `mmr strategies inspect` can surface them and `mmr backtest --param EMA_PERIOD=15` / `mmr bt-sweep --grid '{"EMA_PERIOD":[10,20,30]}'` can override them without touching the class. `Backtester.apply_param_overrides` uses `setattr` on the instance (not the class), so parallel subprocess-level sweeps don't stomp on each other. Lower-case `self.params.get('key', default)` access still works — those keys land in `StrategyContext.params` instead of as instance attributes, and `strategies inspect` surfaces them too by scanning `self.params.get()` AST calls. Prefer the upper-case class-attr style for new strategies; the type is inferred from the default value and enforced on overrides (typos raise `ValueError` listing known tunables).

### Legacy `on_prices`-only example:

```python
from trader.trading.strategy import Strategy, Signal
from trader.objects import Action

class MyStrategy(Strategy):
    def on_prices(self, prices):
        # prices is a DataFrame of accumulated OHLCV data
        if some_buy_condition(prices):
            return Signal(source_name=self.name, action=Action.BUY, probability=0.8)
        return None
```

Register in `config_defaults/strategy_runtime.yaml`:

```yaml
strategies:
  - name: my_strategy
    module: strategies.my_strategy
    class_name: MyStrategy
    bar_size: "1 min"
    conids: [265598]  # AAPL — use current conIds, verify with `mmr resolve AAPL`
    historical_days_prior: 5
    auto_execute: propose   # optional — see below
```

**Important**: ConIds can change. Always verify with `mmr resolve SYMBOL` before hardcoding. If a conId is stale, the strategy will log an error and be disabled — it will NOT silently subscribe to a different instrument.

**Signal → proposal bridge (`auto_execute: propose`)**: by default signals are only recorded to the event store and published on the MessageBus. With `auto_execute: propose`, each signal becomes a **PENDING trade proposal** (auto-sized via the PositionSizer, `source=strategy:<name>`, 30-min TTL after which it self-expires) that a human approves in the web dashboard or via `mmr approve N`. Paper mode only. Semantics mirror the backtester's long-only model: BUY proposes a new entry (deduped while one is pending), SELL proposes closing the currently-held long and is ignored when flat. Time-based exits on a BUY signal (`max_hold_bars`, `close_by_time`) are recorded on the proposal; once the entry executes, `SignalProposer.check_exits` (called every new completed bar) proposes the close when the condition triggers. `auto_execute: true` (full auto) is NOT implemented and is refused at load time — fail loudly, not silently inert. Implementation: `trader/strategy/signal_proposer.py`; spec: `docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md`.

## Command Latency Reference

All timings measured on local macOS. Network commands (download) depend on the provider's API latency (timings below were measured on Massive; Alpaca history needs no paid plan). Set appropriate timeouts — in particular, 1-min backtests on 16K+ bars take 10-15s.

### Data Download (`mmr data download`)
Downloads historical data from the default source (Alpaca; Massive shown here) to local DuckDB. Sequential per symbol (not parallelized).

| Operation | Time | Notes |
|-----------|------|-------|
| 1 symbol, daily, 30 days (~21 bars) | ~3s | Includes API round-trip + DuckDB write |
| 1 symbol, daily, 365 days (~252 bars) | ~3s | Same API call, slightly more data |
| 1 symbol, 1-min, 30 days (~15K bars) | ~4s | Larger payload, single API call |
| 3 symbols, daily, 365 days | ~4s | ~1s per symbol after first |
| 5 symbols, daily, 365 days | ~6s | Downloads are sequential |

**Massive API behavior**: Returns up to 50,000 bars per call. For 1-min data, 30 days is ~15-20K bars (within single call). The API may return NaN/null rows for future dates or market holidays — the backtester drops these automatically.

### Data Query (`mmr data query`)
Reads from local DuckDB. Very fast.

| Operation | Time |
|-----------|------|
| `data summary` (list all symbols/bar sizes) | ~1.5s |
| `data query SYMBOL --days 30` | ~2s |

### Backtesting (`mmr backtest`)
CPU-bound bar-by-bar replay. Time scales with number of bars × strategy complexity.

| Operation | Time | Notes |
|-----------|------|-------|
| Daily, 1 symbol, 252 bars (simple strategy) | ~1.5s | MA crossover, RSI, mean reversion |
| Daily, 1 symbol, 252 bars (vectorbt strategy) | ~4s | First run includes numba JIT compilation |
| Daily, 3 symbols, 252 bars each | ~2.5s | Multi-conid merges timelines |
| 1-min, 1 symbol, 16K bars (simple strategy) | ~14s | Scales linearly with bar count |

**Vectorbt note**: Strategies using `vectorbt` indicators (e.g. `VbtMacdBB`) incur a one-time ~2s numba JIT compilation penalty on first run. Subsequent runs in the same process are fast. The JIT also produces verbose DEBUG logs from numba — these are harmless.

### Ideas Scanner (`mmr ideas`)

| Operation | Time | Notes |
|-----------|------|-------|
| `ideas` (Alpaca, default) | not measured | movers + most-actives, snapshots, daily bars per symbol |
| `ideas --source massive` (Starter+) | ~4s | movers + indicators |
| `ideas --source massive` (Basic → TD fallback) | ~few s | liquid quote set + local indicators |
| `ideas --source twelvedata --tickers …` | ~few s | quotes; movers need Pro+ |
| `ideas --presets` | ~1s | No API calls |
| `ideas momentum --location STK.AU.ASX --tickers …` | ~30-90s | IB path (legacy) |

### Strategy Management
All local file/YAML operations.

| Operation | Time |
|-----------|------|
| `strategies create name` | ~2s |
| `strategies deploy name` | ~2s |
| `strategies list` | ~2s |


### Python Import Overhead
All commands have ~1s baseline overhead for Python startup + importing trader modules (pandas, numpy, duckdb, etc.). This is unavoidable and included in all timings above.
