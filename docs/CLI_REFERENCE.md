# MMR CLI reference

Every command, which service it needs, and output conventions.

## Console entry points

```bash
pip install -e .             # Editable install

# Console entry points after install:
trader-service               # trader.trader_service:main
strategy-service             # trader.strategy_service:main
data-service                 # trader.data_service:main
mmr                          # trader.mmr_cli:main
```

## JSON output

All CLI commands support `--json` for machine-readable output:

```bash
mmr --json portfolio
mmr --json resolve AAPL
mmr --json data summary
mmr --json backtest -s strategies/my_strategy.py --class MyStrategy --conids 265598
```

JSON output always follows the structure `{"data": ..., "title": ...}` for data commands and `{"success": bool, "message": ...}` for status messages.

## Commands

```
status                       # Service connectivity check
keys init                    # Create missing RPC keypairs (never overwrites); Docker: ./docker.sh -k
keys init --rotate dashboard # Replace one principal's keypair; prints the services to restart together
keys backup --recipient FILE # age-encrypted tar of keys/rpc (Docker: ./docker.sh -k --backup)
keys restore FILE --identity-stdin  # Restore into an empty keys/rpc
resolve AMD                  # Resolve symbol to conId/universe
resolve EURUSD --sectype CASH # Resolve forex pair via IB (IDEALPRO)
portfolio                    # Current portfolio
orders                       # Open orders
buy AMD --market --amount 100.0            # offline-simulation legacy only in split Docker
sell AMD --market --quantity 10            # prefer: propose → approve
cancel 123
cancel-all                   # Cancel all orders
flatten --reason "abort" --wait              # PAPER: typed liquidate_account; FLAT only on broker evidence (asks "Type FLATTEN" unless --yes)
close 1                      # Close position by row number
strategies                   # List strategies
strategies enable my_strat   # Enable a strategy
strategies reload            # Reload strategies from YAML + re-subscribe (immediate reconciliation)
strategies inspect           # AST scan: class name, mode (precompute/on_prices), tunable params with defaults
backtest -s strategies/keltner_breakout.py --class KeltnerBreakout --conids 756733
backtest -s ... --params '{"EMA_PERIOD": 15, "BAND_MULT": 2.5}'   # JSON param overrides
backtest -s ... --param EMA_PERIOD=15 --param BAND_MULT=2.5       # repeatable KEY=VALUE form
backtest -s ... --summary-only --no-save-trades                    # skip trades blob + persist
bt-sweep -s strategies/opening_range_breakout.py --class OpeningRangeBreakout --conids 756733 \
     --grid '{"RANGE_MINUTES":[15,30,45],"VOLUME_MULT":[1.2,1.3,1.5]}' --days 365
sweep run nightly.yaml                      # declarative multi-strategy sweep (cron-able)
sweep run nightly.yaml --dry-run            # expand grid + estimate wall time
sweep run nightly.yaml --skip-freshness     # bypass stale-data guard
sweep list                                  # curated history of past sweeps
sweep show 7                                # leaderboard of sweep #7
backtests                                   # history of runs, ranked by composite quality score
backtests --sort-by time                    # chronological (newest first)
backtests --sort-by sharpe --limit 10       # best Sharpe
backtests --sweep 7                         # filter to runs from sweep #7
backtests --all                             # include archived
backtests --card                            # card view instead of table
backtests show 42                           # full detail (summary + statistical confidence)
backtests show 42 --include-raw             # also ship trades_json + equity_curve_json (multi-MB)
backtests confidence 42 43 44               # compact PSR/t-test/CI batch read across runs
backtests compare 40 41 42                  # side-by-side table
backtests archive 42 43                     # hide from default list (reversible)
backtests unarchive 42                      # restore
backtests delete 42                         # permanent
backtests help                              # metric reference
research evaluate research/orb_us.yaml --dry-run   # validate spec + job count
research evaluate research/orb_us.yaml             # real walk-forward evidence (paper-v1)
research evaluations                               # past evaluations
research review submit ... --reviewer-kind llm     # paper: llm allowed; live: human
research attest bundle <artifact_id>               # sign + export the bundle
snapshot AMD                 # Price snapshot (default IB; --source alpaca|twelvedata for REST, US only)
snapshot AAPL --source alpaca               # REST quote via Alpaca (IEX feed), US listings only; --exchange/--currency need IB
snapshot-batch AAPL MSFT --source alpaca    # Batch quotes; rows have feed + error, unknown symbols reported per symbol
depth AAPL                   # Level 2 order book (bids/asks + PNG chart)
depth AAPL --rows 10         # More price levels (max depends on subscription)
depth BHP --exchange ASX --currency AUD  # International depth
depth AAPL --no-chart        # Table only, skip PNG rendering
depth AAPL --no-open         # Render PNG but don't open in Preview
depth AAPL --smart           # SMART depth aggregation across exchanges
listen AMD                   # Stream live ticks via ZMQ
watch                        # Live portfolio monitor
history list                     # List all downloaded history
history list --symbol AAPL       # Filter by symbol
history list --bar_size "1 day"  # Filter by bar size
history massive --symbol AAPL --bar_size "1 day" --prev_days 30
history massive --universe portfolio --bar_size "1 day" --prev_days 30
history ib --symbol AAPL --universe portfolio --bar_size "1 min" --prev_days 5
history alpaca --symbol AAPL --bar_size "1 day" --prev_days 30
stream AAPL MSFT AMD         # Stream from Massive.com
stream AAPL --trades         # Stream trades instead of aggs
stream EURUSD GBPUSD --feed forex           # Forex 1-min aggs
stream EURUSD --feed forex --quotes         # Forex bid/ask quotes
stream EURUSD --feed forex --trades         # Forex per-second aggs
universe list                # List all universes with symbol counts
universe show sp500          # Show symbols in a universe
universe create my_universe  # Create an empty universe
universe delete my_universe  # Delete a universe (with confirmation)
universe add my_universe AAPL MSFT AMD  # Resolve via IB and add symbols
universe remove my_universe MSFT        # Remove a symbol from a universe
universe import my_universe symbols.csv # Bulk import from CSV file
options expirations AAPL                              # List expiry dates + DTE
options chain AAPL                                    # Full chain snapshot (nearest exp)
options chain AAPL --expiration 2026-03-20            # Specific expiration
options chain AAPL -e 2026-03-20 --type call          # Calls only
options chain AAPL -e 2026-03-20 --strike-min 200 --strike-max 250
options chain AAPL -e 3m --source massive             # OPRA via Massive (needs an options plan)
options snapshot AAPL260320C00250000                  # Single contract detail (O: prefix also accepted)
options implied AAPL -e 2026-03-20                    # Probability distribution
options buy AAPL -e 2026-03-20 -s 250 -r C -q 5 --market    # orders stay on IB; exact dates need no data source
options sell AAPL -e 3m -s 250 -r C -q 5 --limit 3.50       # relative -e asks the options source (--source picks it)
news                                         # General market news (default Alpaca)
news AAPL                                    # News for a ticker
news AAPL --limit 20                         # More articles
news AAPL --source polygon                   # Massive (Polygon) news; --source benzinga for Benzinga
news AAPL --detail                           # Full details; sentiment only with --source polygon
movers                           # Default Alpaca; drops names under --min-price (default 1.0) and warrants/rights/units
movers --min-price 5             # Stricter price floor
movers --market crypto           # Crypto gainers (Alpaca)
movers --market indices          # ETF proxies (SPY, QQQ, DIA, IWM, sector SPDRs) from Alpaca IEX prices — not the indices
movers --market indices --source massive   # Real indices (paid); options/futures still need Massive
movers --losers                  # Stock losers
movers --market crypto --losers  # Crypto losers
scan                             # Top gainers (default preset)
scan losers                      # Top losers
scan active                      # Most active by volume
scan hot-volume                  # Hot by volume change
scan --scan-code HIGH_OPT_VOLUME # Raw IB scanner code
scan gainers --above-price 10 --num 30  # Filtered
scan --instrument ETF --location STK.US  # ETFs
ideas                                        # Momentum (default Alpaca; free, delayed prices)
ideas --source massive                       # Massive (full-market snapshots, ratios; paid plan; Basic → TD quote fallback)
ideas gap-up / mean-reversion / breakout / gap-down / volatile
ideas --source twelvedata --tickers AAPL MSFT NVDA AMD  # TwelveData quotes path
ideas momentum --tickers AAPL MSFT AMD NVDA  # Scan exactly these tickers (works on Alpaca)
ideas momentum --universe sp500              # Scan a universe
ideas gap-up --min-price 10                  # Override preset filter
ideas volatile --num 25                      # Top 25 results
ideas --presets                              # List all presets
ideas momentum --detail                      # Names + news + indicators (ratios only on massive/twelvedata/IB)
ideas momentum --fundamentals --source massive  # Financial ratios (PE, D/E, ROE); errors on Alpaca until phase 4
ideas momentum --news                        # Latest headline (sentiment only with --source massive)
ideas mean-reversion --news --fundamentals --source massive  # Technicals + fundamentals + news
ideas gap-up -t AAPL MSFT --fundamentals --source twelvedata  # Specific tickers with fundamentals
ideas momentum --location STK.AU.ASX --tickers BHP CBA CSL  # ASX via IB (legacy path)
ideas mean-reversion --location STK.AU.ASX --tickers BHP CBA --detail  # ASX with enrichment
ideas gap-up --location STK.HK.SEHK --tickers 0700 0005     # Hong Kong via IB
propose AMD BUY --market --quantity 100 --bracket 180 150 --reasoning "Breakout above resistance"
propose AMD BUY --limit 165 --amount 5000 --trailing-stop-pct 2.0 --tif GTC
propose AAPL SELL --market --quantity 50 --stop-loss 140
propose BHP BUY --market --confidence 0.7 --group mining --exchange ASX --currency AUD
proposals                                    # List pending proposals
proposals --all                              # All statuses
proposals --status EXECUTED                  # Filter by status
proposals show 3                             # Full detail for proposal #3
approve 3                                    # Execute proposal #3 (typed RPC 42102)
reject 3 --reason "Changed thesis"           # Reject proposal #3
group list                                   # List groups with members + allocation
group create mining --budget 20              # Create group with 20% max allocation
group delete mining                          # Delete group and members
group show mining                            # Members + allocation details
group add mining BHP RIO FMG                 # Add symbols to group
group remove mining BHP                      # Remove symbol from group
group set mining --budget 25                 # Update group budget
portfolio-risk                               # Full risk analysis report
portfolio-risk --json                        # JSON for LLM consumption (alias: prisk)
portfolio-snapshot                           # Compact JSON: value, P&L, movers (alias: psnap)
portfolio-diff                               # Delta since last snapshot (alias: pdiff)
resize-positions --max-bound 500000          # Trim portfolio to $500k
resize-positions --min-bound 300000          # Grow portfolio to $300k
resize-positions --max-bound 500000 --min-bound 300000  # Both bounds
resize-positions --max-bound 500000 --dry-run  # Preview without executing
forex snapshot EURUSD                        # IB (default; needs trader_service)
forex snapshot EURUSD --source frankfurter   # ECB daily reference rate (free, not live)
forex snapshot EURUSD --source massive       # Massive snapshot (paid)
forex quote EUR USD                          # IB bid/ask (default)
forex quote EUR USD --source frankfurter     # Same ECB daily rate as snapshot
forex snapshot-all                           # ECB daily rates, every currency vs USD (Frankfurter)
forex snapshot-all JPY GBP --base EUR        # Chosen currencies vs EUR
forex movers                                 # 10 FX majors/crosses ranked by ECB day-over-day change (computed_fx)
forex movers --losers --source massive       # Massive forex movers (paid)
forex convert EUR USD 1000                   # ECB daily rate (Frankfurter); --source massive|twelvedata
experiment status                            # PAPER ai_paper experiment: state, active + pending kill line, entry block
experiment start --reason "first run"        # operator; flat paper account, ai_paper.enabled
experiment pause --reason "news risk"        # cli, dashboard or ai_supervisor
experiment resume --reason "ok"              # operator; never after a kill
experiment stop --reason "done"              # operator; once the account is flat (final)
ai-policy show                               # PAPER AI risk policy: published, effective, queued limits
ai-policy publish policy.yaml --reason "x"   # operator only; file = {limits: {...}}; --command-id to retry
ai-deployment register-discretionary --operator owner --statement "paper only"   # operator; the SP2 discretionary deployment
ai-deployment register-discretionary --operator owner --statement "x" --stock-types ETF --min-price 10   # narrow the rule
ai-deployment show sha256:...                # a sealed deployment, its kind and provenance
scoreboard                                   # PAPER scoreboard of the latest experiment ('-' = unknown)
--json scoreboard --experiment exp-<20 hex>  # the report as JSON: {"data": ..., "title": "Scoreboard (paper)"}
scoreboard verify                            # rebuild every number from stored inputs; exit 1 on any mismatch
experiment acceptance preflight              # SP1 clean-account gate: PASS, or STOP + reasons (exit 1)
experiment acceptance run                    # dry run: reads the gate, prints the planned calls, sends nothing
experiment acceptance run --place-orders --confirm-account DU123 --signing-key KEY   # the owner's paper session only
experiment acceptance finish --run-id acc-... --signing-key KEY   # after 15:55 ET: end checks + signed report
experiment acceptance status --run-id acc-...  # the local run journal only
experiment acceptance verify-report REPORT --public-key PUB   # signature + fields; exit 1 if bad or ephemeral
```

### `ai-deployment` (SP2 discretionary deployment)

Self-found ideas of the AI paper bot trade only under a `discretionary` deployment that the
operator registers. Its scope is a rule, not a conid list; the trader checks it at admission and
again at dispatch (`OUT_OF_DISCRETIONARY_SCOPE`, `detail.part` one of `exchange`,
`instrument_type`, `price`, `dollar_volume`, `liquidity`, `trading_filter`, `evidence_stale`).

`register-discretionary` flags (each may only narrow the default; the trader refuses a wider rule):

| Flag | Default | Meaning |
|---|---|---|
| `--operator` (required) | | who attests, `^[A-Za-z0-9_.@-]{1,64}$` |
| `--statement` (required) | | the attestation text, 1-500 printable characters |
| `--exchanges` | `ARCA,NASDAQ,NYSE` | primary listings (IB `primaryExchange`) |
| `--stock-types` | `COMMON,ETF` | IB `stockType`; a blank type never passes |
| `--min-price` | `5` | the bid must be at least this |
| `--min-dollar-volume` | `20000000` | 20-session median dollar volume; SP1's $50M floor also applies |
| `--max-order-share` | `0.01` | order notional at most this share of that median |

Registering the same content again replays one command. `show DIGEST` (`--json` for JSON) prints
`DISCRETIONARY (operator attested, no backtest evidence)` for this kind and returns
`{"digest", "kind": "strategy"|"discretionary"|null, "deployment", "strategy_digest_provenance", "error_code"}`;
a discretionary deployment has provenance `OPERATOR_ATTESTED`.

## Command Service Requirements

**No service needed** (fully local / REST keys only):
- `data summary`, `data query`, `data download`, `data refresh`, `data status`
- `backtest` / `bt`, `bt-sweep`, `sweep run/list/show`
- `backtests list/show/compare/confidence/archive/unarchive/delete`
- `strategies create`, `strategies deploy`, `strategies undeploy`, `strategies inspect`, `strategies signals`, `strategies backtest`
- `universe list/show/create/delete/remove/import`
- `propose`, `proposals`, `reject`, `group *`, `session`
- `ideas`, `movers` / `news` (Alpaca keys by default; `ideas --source massive|twelvedata` needs that provider's key; no trader_service)
- `financials` (Massive key), `options` data (Alpaca keys by default; `--source massive` needs a Massive options plan)
- `forex convert|snapshot-all|movers` (Frankfurter, no key), `forex snapshot|quote --source frankfurter|massive|twelvedata`, `movers --market indices` (Alpaca keys)

**Requires trader typed RPC (42101/42102)** — production path; no legacy 42001:
- `portfolio`, `positions`, `orders`, `trades`, `account`, `status`, `resolve`, `snapshot` (IB source), `depth`
- `approve`, `portfolio-risk` / `psnap` / `pdiff`, `reconcile`, `diagnose`
- `listen` (publish_instrument + PubSub)
- `forex snapshot`, `forex quote` (default IB source; IDEALPRO CASH contract)
- `experiment status|start|pause|resume|stop` (SP1 experiments; paper only)
- `ai-policy show|publish` (SP2 operator AI risk policy; paper only; `publish` signs as `cli`)
- `ai-deployment register-discretionary|show` (SP2 discretionary deployment; paper only; `register-discretionary` signs as `cli`)
- `experiment acceptance preflight|run|finish` (SP1 acceptance, host only; `run` and `finish` sign with the `ai_supervisor`/`ai_research` keys, plus `cli` with `--place-orders`); `experiment acceptance status|verify-report` are local
- `flatten --reason TEXT [--wait] [--yes]` (paper only; typed `liquidate_account`, prints `FLAT` only on broker evidence)
- `scoreboard`, `scoreboard verify` (SP1 scoreboard; reads the journal, not IB, so no IB-upstream check)

**Requires strategy typed RPC (42104/42105)**:
- `strategies` list, `strategies enable|disable|reload`

**Requires offline-simulation legacy RPC (42001)** — clear error in split production:
- Direct `buy` / `sell` / `cancel` / `cancel-all` / `close` / `to-market` / `resize-positions` execute
- IB `scan`, `ideas --location`, options contract resolve via dill
- Prefer `propose` → `approve` or the dashboard command center instead

## ConId Lookup

Symbols in DuckDB are stored by conId (IB contract ID). To find a conId:
```bash
mmr --json resolve AAPL    # Returns conId in JSON data
```

Common conIds: AAPL=265598, MSFT=272093, NVDA=4815747. Note: conIds can become stale — always verify before use.

## Valid Bar Sizes

`1 secs`, `5 secs`, `10 secs`, `15 secs`, `30 secs`, `1 min`, `2 mins`, `3 mins`, `5 mins`, `10 mins`, `15 mins`, `20 mins`, `30 mins`, `1 hour`, `2 hours`, `3 hours`, `4 hours`, `8 hours`, `1 day`, `1 week`, `1 month`
