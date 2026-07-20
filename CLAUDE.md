# CLAUDE.md

> **Current deployed/running state** (armed strategies, backtest findings, data
> coverage, backups, restart policy, Monday plan) lives in
> [`docs/OPERATIONAL_STATE.md`](docs/OPERATIONAL_STATE.md) — read it first when
> resuming operational work. Code backlog is in `docs/AUDIT_ROADMAP.md`.

## Project Overview

MMR (Make Me Rich) is a Python-based algorithmic trading platform for Interactive Brokers. It supports automated strategy execution, interactive CLI trading, historical data collection, real-time market data streaming, and idea scanning across US and international markets.

## Design Principles

**Precision over convenience**: This is a trading system — wrong data is worse than no data. Contract identifiers (conIds) must resolve exactly or fail. Never fall back to fuzzy matching, string coercion, or "close enough" lookups. If a conId isn't found, return an error. If a symbol resolves to the wrong exchange, that's a bug. Integer conIds passed to `resolve_symbol` must not be converted to string symbol lookups (e.g. conId `4391` must not become ticker `"4391"` which matches a Japanese stock on TSEJ).

**Fail loudly, not silently**: When an IB API call fails (scanner error 162, market data not subscribed, contract not found), surface the error to the caller. Don't swallow exceptions and return empty results — the user needs to know *why* something failed so they can fix it (subscribe to market data, use a different location code, etc.).

**Massive first, IB fallback**: For US markets, Massive.com (Polygon.io) is the primary data source for ideas/movers when the plan includes snapshots (Starter+). TwelveData is the default for cheap US history/quotes (`default_data_source: twelvedata`). International markets (ASX, TSE, SEHK, etc.) use IB. Don't use Yahoo Finance. Bare `ideas` / `movers` always default to Massive (they do **not** inherit `default_data_source`); if Massive snapshots aren't entitled (Stocks Basic), the scanner falls back to TwelveData quotes on a small liquid US set (or `--tickers` / `--universe`).

**No sentiment analysis on IB path**: IB's news API doesn't provide sentiment scoring. On the Massive path, sentiment comes from Polygon's insights. On the IB path, we only show the headline — no fake or estimated sentiment.

## Architecture

Production Docker runs **split services** (not one monolithic process). The CLI and dashboard talk to trader/strategy over **typed HMAC RPC**; legacy dill RPC on 42001 is unbound unless offline simulation explicitly enables it.

```
trader.trader_service ──► Trader (trading_runtime.py)
                           ├── IBAIORx (ibreactive.py) ──► IB Gateway (ib_async)
                           ├── TradeExecutioner, BookSubject, Portfolio
                           ├── Typed RPC query/command (42101 / 42102)  ← production CLI/dashboard
                           ├── Typed feed (42103, internal)
                           ├── ZMQ PubSub Publisher (42002)
                           ├── ZMQ MessageBus (42006)
                           └── Legacy dill RPC (42001) — offline simulation only

trader.strategy_service ──► StrategyRuntime (strategy_runtime.py)
                              ├── Loads strategies from strategy_runtime.yaml
                              ├── Reconciliation loop (30s)
                              ├── Typed trader gateway (resolve/publish via 42101)
                              ├── Typed strategy control (42104 command / 42105 query)
                              ├── ZMQ PubSub Subscriber ← tickers
                              └── Legacy strategy RPC (42005) — optional/compat

trader.data_service ──► DataService (data_service.py)
                          ├── Concurrent history downloads
                          ├── MassiveHistoryWorker, TwelveDataHistoryWorker, IBHistoryWorker
                          └── ZMQ RPC Server (42003)

web/app.py (dashboard) ──► typed query/command + strategy typed ports
trader.mmr_cli / sdk.py ──► typed RPC (reads + propose/approve); legacy only for offline direct orders
scheduler (pycron) ──► cron jobs only (data refresh, backups) — not the process supervisor for split services
```

### Key Patterns

**Dependency injection**: `Container.resolve(Type)` introspects `__init__` parameter names, matches them against env vars (uppercased) then config YAML values, and constructs the instance. Constructor param names must match config keys. Missing required params raise `ContainerResolutionError` naming the param (not a cryptic `TypeError`). Env vars are coerced to the annotated type (`int`/`float`/`bool`); malformed values raise with the offending var + value. Singleton init and the type-instance cache are thread-safe, and circular dependencies during `resolve()` are detected and raised as `ContainerResolutionError`. YAML config is loaded with `yaml.safe_load` — `!!python/object` tags are refused.

**Messaging (pyzmq)**: Inter-process communication uses ZeroMQ (not HTTP) for latency and pub/sub. Two layers:

**Typed HMAC RPC (production)** — JSON-safe request/reply on ROUTER sockets with HMAC service authentication (`trader/messaging/typed_rpc.py`). Trader query **42101**, command **42102**, feed **42103**; strategy command **42104**, query **42105**. The CLI/SDK and dashboard use this path for portfolio, resolve, propose/approve, strategies list/enable/disable/reload, snapshots, etc. Direct `buy`/`sell`/`cancel`/`set_risk_limits` are **not** registered on the production command surface (BYPASS methods) — use `propose` → `approve` or the dashboard command center; offline simulation can bind legacy dill RPC with `unsafe_legacy_rpc: true` + `--simulation True`.

**Legacy dill RPC** (`trader/messaging/clientserver.py`) — DEALER/ROUTER + msgpack (including dill ExtType). Ports **42001** (trader), **42003** (data), **42005** (strategy). Unbound for trader in split-container production. Still used by data_service and some IB-only tools (scanner, options resolve) when available.

Additional patterns:

- **PubSub** (port 42002): ZMQ PUB/SUB for live ticker broadcast. `MultithreadedTopicPubSub` runs PUB on a dedicated thread.
- **MessageBus** (port 42006): DEALER/ROUTER with per-client topic routing for strategy signals.

**Serialization**: Legacy ZMQ messages use msgpack with ExtType handlers for datetime/date/time/timedelta, pandas DataFrames (PyArrow IPC), and a dill fallback for arbitrary objects. Because `dill.loads` can execute code, set `MMR_DILL_STRICT=1` to refuse `EXT_OBJECT`, or `set_dill_whitelist([...])`. Typed RPC uses JSON-safe pydantic models — no dill on the production surface.

**Storage (DuckDB)**: `trader/data/duckdb_store.py` wraps every query in a short-lived connection held under a per-database lock (`execute_atomic` opens, runs, closes atomically; `execute(query, params, fetch='all'|'one'|'df'|'none')` is the common-case wrapper). This lets multiple services share the same database file without leaking connections or tearing rows across concurrent writers. Two tables: `tick_data` (time-series OHLCV) and `object_store` (dill-serialized blobs). The OHLCV `write()` upsert filters its DELETE by `bar_size` as well as `symbol + date range` — without that filter a wide 1-min write (potentially expanded by `write_resolve_overlap` merging in years of pre-existing rows) would clobber every daily bar for the same conid in that range. The DuckDB live file lives in a named volume (`mmr_db_data`) rather than a host bind mount — on macOS Docker Desktop, VirtioFS has quirky mmap/fsync semantics for write-heavy single-file DBs. Use `./docker.sh -B [name]` to snapshot DB files out to the host bind-mount backup dir.

**Event store**: `trader/data/event_store.py` records trading events (signals, orders, fills, rejections) in DuckDB for audit trail and risk gate lookback. All writes and queries use the atomic `DuckDBConnection.execute`/`execute_atomic` APIs — earlier versions leaked connections on the hot path.

**Risk gate**: `trader/trading/risk_gate.py` enforces pre-trade risk limits (max position size, daily loss, open orders, signal rate) by querying the event store.

**Position sizing**: `trader/trading/position_sizing.py` computes position sizes based on confidence, risk level, ATR volatility, portfolio state, and liquidity (ADV, spread). The sizing pipeline is: `base_position × risk_multiplier × confidence_scale × volatility_adjustment`. Volatile stocks (high ATR%) automatically get smaller positions; stable stocks get larger ones. Configured via `config_defaults/position_sizing.yaml`. Used automatically by `propose` when no quantity/amount is specified.

**Position groups**: `trader/data/position_groups.py` stores named groups with allocation budgets in DuckDB (e.g. "mining" at 20% max). The `propose` command accepts `--group` to tag trades and auto-register membership. The portfolio risk analyzer checks group allocations against budgets.

**Portfolio risk**: `trader/trading/portfolio_risk.py` analyzes concentration (HHI), position weights, group budget compliance, and return correlation clusters. It reports both gross and signed exposure (`gross_exposure_pct`, `net_exposure_pct`, `long_exposure_pct`, `short_exposure_pct`) so hedged books aren't mis-flagged as concentrated. HHI is computed on gross weights (risk-exposure view); concentration warnings fire on gross (>10% warning, >15% critical), but correlation-cluster warnings fire on *signed* combined weight — a correlated long/short pair nets near zero and is correctly treated as hedged, not clustered. The plain-English summary explicitly notes "hedged" when gross and |net| diverge.

**Trading filter**: `trader/trading/trading_filter.py` enforces symbol/exchange denylist/allowlist rules. Checked by the executioner and risk gate before any order placement.

**Portfolio resizing**: `trader/sdk.py` provides `compute_resize_deltas()` (pure function) and `compute_resize_plan()`/`execute_resize_plan()` (SDK methods) for proportionally scaling all positions to fit within a target portfolio value. The resize workflow: (1) compute scale factor from max/min bounds, (2) find associated protective orders (stops, trailing stops, take-profits) for each position, (3) cancel protective orders, (4) place market orders for position deltas, (5) re-create protective orders at new quantities preserving original prices. Exposed via `resize-positions` CLI command. The `place_standalone_order()` RPC method on `trading_runtime.py` supports placing standalone STP/TRAIL/LMT orders for existing positions (used to re-create protectives after resizing).

**Strategy reconciliation**: The strategy_service runs a reconciliation loop every 30 seconds (`strategy_runtime.py:_reconcile()`). It re-reads the portfolio universe from DuckDB and re-subscribes strategies to any new instruments (idempotent — `subscribe()` skips already-subscribed conIds). It also checks the YAML config file's modification time and loads any newly added strategies. This means: (1) an empty portfolio at startup automatically picks up positions as they're added via trades, (2) new strategies deployed to the YAML are loaded without restarting the service, (3) the `reload_strategies` RPC method triggers immediate reconciliation without waiting for the 30-second cycle. Note: modifying an existing strategy's config (changing conIds or bar_size) still requires a service restart. If the YAML is mid-write when reconcile reads it, the parse error is caught and the mtime is *not* advanced, so the reload retries on the next tick instead of silently leaving zombie strategies loaded. Strategy modules are loaded with `yaml.safe_load` (no Python-object tags) and path-sandboxed to `strategies_directory` (absolute paths or `../` traversal are rejected). Each strategy gets a unique `sys.modules` key derived from its `name` so two strategies that share a filename don't clobber each other and a reload actually re-imports the new source.

**IB upstream connectivity detection**: The trader_service tracks IB Gateway's upstream connection to IBKR servers via IB error codes (1100/2103/2105/2157 = lost, 1102/2104/2106/2158 = restored). The `get_status()` RPC exposes `ib_upstream_connected` and `ib_upstream_error`. The CLI checks this before any IB-dependent command (portfolio, orders, buy/sell, snapshot, etc.) and shows a clear error with VNC/restart instructions instead of silently timing out. The `status` command also shows upstream connectivity and a warning when it's down.

**Reactive streams (RxPY)**: `IBAIORx` converts IB events into RxPY Subjects/Observables. Strategies receive accumulated DataFrames via reactive pipelines.

**Proposal state machine**: `trader/data/proposal_store.py` enforces valid status transitions: `PENDING → APPROVED | REJECTED | EXPIRED | FAILED`, `APPROVED → EXECUTED | FAILED | REJECTED`, and terminal states (`EXECUTED`, `REJECTED`, `EXPIRED`, `FAILED`) are immutable. Illegal transitions raise `InvalidProposalTransition`. This prevents double-approvals, re-executions, and resurrected proposals. `update_metadata` is also atomic (the read-merge-write runs under a single connection), so concurrent metadata updates can't lose each other.

**Bracket order transactionality**: `trading_runtime.place_expressive_order` treats a `BRACKET` exit as all-or-nothing. Entry is staged with `transmit=False`, then TP, then SL (which transmits the whole group). If the TP leg fails, the staged entry is cancelled and `SuccessFail.fail` returned. If the SL leg fails, both entry and TP are cancelled. Because the bracket isn't transmitted to the market until the SL is placed, a failure at any earlier leg leaves no live orders behind.

**Backtester fill policy**: `trader/simulation/backtester.py` defaults to `fill_policy='next_open'` — a signal emitted while observing bar `t` fills at bar `t+1`'s open. This eliminates lookahead bias: the strategy can see bar `t`'s close (public info at that moment) but can't fill at that same close. `fill_policy='same_close'` preserves the legacy (biased) behaviour and exists only for regression tests that pre-date the fix.

**Backtest parameter overrides**: `Backtester.apply_param_overrides(instance, params)` is called after `install(context)` and supports both tunable idioms — upper-case class attributes (`EMA_PERIOD = 20`, read as `self.EMA_PERIOD`) get shadowed via `setattr` on the instance so parallel sweeps don't collide on each other. Lower-case keys land in `instance._context.params` to serve the legacy `self.params.get('key', default)` pattern. Typos on upper-case keys raise `ValueError` listing known tunables; lower-case keys are free-form because `self.params.get(...)` is. `_coerce_param` converts CLI strings (e.g. `"15"`) to the class attr's current type; `_coerce_loose` best-effort-coerces dict-bound values. Effective overrides round-trip to `BacktestResult.applied_params` and on to `BacktestRecord.params` for later reproducibility via `backtests show`.

**Statistical-confidence tests** (`trader/simulation/backtest_stats.py`): On-demand computation from persisted `trades_json` + `equity_curve_json` (persisted by default; opt out with `--no-save-trades`). Five tests answer "is this edge real?" beyond what Sharpe/PF can: (1) **Probabilistic Sharpe Ratio** (López de Prado 2012) — probability that the true Sharpe > 0 given sample size, skew, kurtosis; (2) **t-test** on mean per-trade P&L == 0; (3) **Bootstrap 95% CI** on mean P&L and annualised Sharpe; (4) **P&L skew + excess kurtosis** catching the "negative skew + fat tails = blow-up risk" signature Sharpe misses; (5) **Longest losing streak vs Monte Carlo** — compares actual to 95th-percentile random-reorder streaks, detects loss clustering. All five fail gracefully to `None` below their minimum sample (PSR needs n ≥ 3, bootstrap needs n ≥ 10, MC streak needs n ≥ 5). Surfaced via `backtests show` and `backtests confidence` — the latter is a bulk helper that strips raw trades/equity JSON (multi-MB per run) and returns just the confidence block.

**Nightly sweep pipeline** (`trader/mmr_cli.py` + `trader/data/backtest_store.py`): Declarative YAML manifests (`sweeps: [{name, strategy, class, symbols|conids|universe, param_grid, days, bar_size, concurrency, note}]`) become concrete per-(symbol, param) jobs via `_expand_sweep_jobs`. A parent `sweeps` table row is created up front with `status='running'`, every child `backtest_runs` row is stamped with `sweep_id`, and the sweep is finalised to `completed`/`failed`/`cancelled` with a markdown digest path. Concurrency auto-tunes to `cpu_count - 1` (cap 16) unless overridden; children are launched via `asyncio.create_subprocess_exec` with a per-semaphore gate so the same machine-level parallelism that `backtest_batch` uses applies here too. The SIGINT handler lets in-flight subprocesses finish rather than killing them, so a 90%-complete sweep still persists 90% of its runs. Freshness guard (`_freshness_check`) refuses to run if any conid lacks a daily bar from the last 3 trading days; `--skip-freshness` overrides. `_write_sweep_digest` produces `~/.local/share/mmr/reports/sweep_<id>_<name>_<ts>.md` with strong-candidates / all-runs / failures tables plus pointers into `backtests list --sweep <id>` and `backtests confidence`. Digest-write failures never take down a sweep — they return an empty path and log a warning.

**Composite quality score** (`_bt_composite_score` in `mmr_cli.py`): Ranks backtest runs by a weighted blend of sortino, profit_factor, expectancy_bps, return, and drawdown, each clipped to a sensible band, multiplied by a reliability factor that penalises low trade counts (< 10 → ×0.2, < 30 → ×0.6, < 100 → ×0.9). Used as the default sort for `backtests list` and the leaderboard sort in `sweep show`. Not a decision metric — use the statistical-confidence block for deploy/reject — just an ordering heuristic so strong runs float to the top.

**Subprocess concurrency + DuckDB**: The `mmr-skill` helper's `_CLI_SLOTS = asyncio.Semaphore(16)` caps concurrent CLI subprocess launches without serialising them (the previous `_CLI_LOCK` made `backtest_batch(concurrency=6)` a no-op). DuckDB has file-level locking across processes; `DuckDBConnection.execute_atomic` retries on `IOException` with exponential backoff + jitter up to **32 attempts (~45s total)** so a burst of 15+ concurrent backtest-end writes don't get starved (the prior 8-attempt budget was too tight and caused silent persist failures). This is what makes `sweep run` and `backtest_batch` actually peg CPU instead of bottlenecking at 10%. The sweep parent also checks for `run_id=None` in each child's JSON response and reports `persist_failed` distinctly, so a silently-dropped persist isn't counted as success. Child persist failures additionally always log to stderr (not just rich console), so the digest captures the traceback even in `--json` runs. Sweep manifests pass strategy paths relative to the project root; `_resolve_strategy_path` resolves them to absolute before subprocess launch (subprocess CWD is `/home/trader`, not the mmr install root, so a raw relative path resolves to a non-existent file).

**PnL subscription race**: `__subscribe_pnl` registers `(account, conId)` under `_pnl_subscriptions_lock` using first-claim-wins semantics. If the actual `subscribe_single_pnl` call fails, the registry entry is backed out so a retry can re-attempt. Portfolio updates fired from IB-eventkit threads are routed onto the main loop via `run_coroutine_threadsafe` (the main loop is captured in `connected_event`), so disk I/O during a universe update doesn't block the IB callback thread.

**Idea scanner**: Raises `IdeaScannerError` (not an empty DataFrame) on IB discovery failure or when every ticker fails to resolve. Massive movers/snapshots require Stocks Starter+; TwelveData `/market_movers` requires Pro+. Entitlement errors fall back to TwelveData quote scans (liquid US set or `--tickers`/`--universe`) with a yellow CLI notice. Batch IB ops use `asyncio.gather` / `ThreadPoolExecutor` — keep DuckDB warm via `data refresh` so IB historical isn't on the hot path.

**Data refresh loop** (`trader/mmr_cli.py:_handle_data_refresh` + `config_defaults/data_refresh.yaml`): Declarative jobs `{universe, source?, bar_size, days, force?}` keep universes' OHLCV current in the local DuckDB. Pycron owns the schedule (`data_refresh_us` and `data_refresh_asx` cron entries in `pycron.yaml`); the YAML owns *what* to fetch. Source auto-detects from the universe's dominant exchange when omitted (US → twelvedata; else IB). Incremental by default — only missing date ranges are fetched — so a daily cron run costs ~seconds for fresh windows. `mmr data status` shows per-(job, bar_size) coverage and stale-days, color-coded; `mmr data refresh JOB [JOB ...]` runs jobs ad-hoc. Failures in one job are isolated (per-job result, batch keeps going) and don't take down the cron entry.

## Project Structure

```
mmr/
├── pycron/pycron.py           # Process manager / scheduler
│
├── config_defaults/           # Template defaults (copied to ~/.config/mmr/ on first run only; edits here don't affect a running system)
│   ├── trader.yaml            # IB connection, ZMQ ports, DuckDB path
│   ├── pycron.yaml            # Service job definitions (Docker mode)
│   ├── no_docker_pycron.yaml  # Service job definitions (non-Docker)
│   ├── strategy_runtime.yaml  # Strategy definitions
│   ├── position_sizing.yaml   # Position sizing defaults (base size, risk level, limits)
│   ├── trading_filters.yaml   # Trading filter config (denylist, allowlist, exchanges)
│   ├── data_refresh.yaml      # Declarative refresh jobs (universe × bar_size × days, cron-driven)
│   └── logging.yaml           # Python logging config
│
├── trader/                    # Core library + entry points
│   ├── trader_service.py      # Entry point: trading runtime
│   ├── strategy_service.py    # Entry point: strategy runtime
│   ├── data_service.py        # Entry point: data download service (RPC + direct)
│   ├── mmr_cli.py             # Entry point: CLI REPL (argparse + prompt_toolkit)
│   ├── sdk.py                 # SDK used by mmr_cli (ZMQ RPC client wrapper)
│   ├── __main__.py            # python -m trader support
│   ├── container.py           # DI container (Singleton, YAML + env var resolution)
│   ├── config.py              # Typed config dataclasses (IBConfig, StorageConfig, ZMQConfig)
│   ├── objects.py             # Domain enums/dataclasses (Action, BarSize, etc.)
│   ├── common/
│   │   ├── singleton.py       # Singleton metaclass
│   │   ├── helpers.py         # Utility functions (dateify, rich_table, etc.)
│   │   ├── logging_helper.py  # Per-module logger setup from logging.yaml
│   │   ├── reactivex.py       # RxPY helpers (AnonymousObserver, EventSubject)
│   │   ├── listener_helpers.py
│   │   ├── exceptions.py      # TraderException, TraderConnectionException
│   │   ├── contract_sink.py
│   │   └── dataclass_cache.py # Cache with reactive update notifications
│   ├── data/
│   │   ├── store.py           # Abstract DataStore/ObjectStore + DateRange
│   │   ├── duckdb_store.py    # DuckDB implementations (DuckDBDataStore, DuckDBObjectStore)
│   │   ├── data_access.py     # TickData, DictData, TickStorage, SecurityDefinition
│   │   ├── event_store.py     # EventStore — trading event audit trail (DuckDB)
│   │   ├── backtest_store.py  # BacktestStore + SweepStore — run history + parent sweep metadata
│   │   ├── proposal_store.py  # ProposalStore — trade proposal storage (DuckDB)
│   │   ├── position_groups.py # PositionGroupStore — named groups with allocation budgets (DuckDB)
│   │   ├── universe.py        # Universe, UniverseAccessor
│   │   └── market_data.py     # MarketData, SecurityDataStream
│   ├── listeners/
│   │   ├── ibreactive.py      # IBAIORx: RxPY wrapper around ib_async
│   │   ├── ib_history_worker.py
│   │   ├── massive_history.py # Massive.com REST history worker
│   │   └── massive_reactive.py # Massive.com WebSocket streaming
│   ├── messaging/
│   │   ├── clientserver.py    # ZMQ RPC, PubSub, MessageBus
│   │   ├── trader_service_api.py
│   │   ├── strategy_service_api.py
│   │   └── data_service_api.py
│   ├── trading/
│   │   ├── trading_runtime.py # Trader class (main runtime, Singleton)
│   │   ├── executioner.py     # TradeExecutioner
│   │   ├── book.py            # BookSubject (reactive order book)
│   │   ├── portfolio.py       # Portfolio positions
│   │   ├── order_validator.py
│   │   ├── risk_gate.py       # RiskGate — pre-trade risk limit enforcement
│   │   ├── position_sizing.py # PositionSizer — confidence/risk/volatility/liquidity-aware sizing
│   │   ├── portfolio_risk.py  # PortfolioRiskAnalyzer — concentration, correlation, group budgets
│   │   ├── trading_filter.py  # TradingFilter — denylist/allowlist/exchange filtering
│   │   ├── strategy.py        # Strategy ABC, Signal, StrategyConfig, StrategyContext
│   │   └── proposal.py        # TradeProposal, ExecutionSpec — propose/review/approve pipeline
│   ├── strategy/
│   │   └── strategy_runtime.py # StrategyRuntime (loads/runs strategies)
│   ├── simulation/
│   │   ├── historical_simulator.py
│   │   ├── backtester.py      # Backtester — replay historical data through strategies
│   │   ├── backtest_stats.py  # PSR, t-test, bootstrap CI, skew/kurt, MC streak — "is this real?"
│   │   └── lookahead_check.py # assert_no_lookahead walk-forward consistency check
│   └── tools/                 # Importable scripts (moved from scripts/)
│       ├── idea_scanner.py    # IdeaScanner (Massive) + IBIdeaScanner (IB international)
│       ├── depth_chart.py     # Market depth chart (PNG) + Rich table rendering
│       ├── chain.py           # Options chain analysis
│       ├── trader_check.py    # Service health check
│       ├── zmq_pub_listener.py # ZMQ PubSub pretty printer
│       ├── ib_instrument_scraper.py # IB product scraper
│       └── ib_resolve.py      # IB symbol resolver
│
├── strategies/                # User strategy implementations
│   ├── global.py              # Global (no-op) strategy
│   └── smi_crossover.py       # SMI crossover example
│
├── scripts/                   # Standalone operational scripts (not imported by library)
│   ├── docker-entrypoint.sh   # Container entrypoint
│   ├── ib-gateway-run.sh      # IB Gateway startup script
│   ├── ib_status.py           # IB Gateway health check
│   ├── reporting.py           # Trade reporting
│   ├── test_pycron.py         # Pycron integration test
│   └── test_pycron_sync.py    # Pycron sync test
│
├── tests/                     # Test suite (pytest)
│   ├── conftest.py            # Shared fixtures (DuckDB, strategies, OHLCV data)
│   ├── test_backtester.py
│   ├── test_book.py
│   ├── test_config.py
│   ├── test_container.py
│   ├── test_duckdb_store.py
│   ├── test_event_store.py
│   ├── test_depth.py          # 19 tests: depth chart PNG rendering, Rich table output
│   ├── test_idea_scanner.py   # 123 tests: presets, indicators, IB scanner, tickers path, fundamentals, news
│   ├── test_portfolio.py
│   ├── test_position_sizing.py # 90 tests: sizing, confidence, risk, volatility/ATR, liquidity, resize deltas
│   ├── test_position_groups.py # 22 tests: group store CRUD, member management
│   ├── test_portfolio_risk.py  # 18 tests: concentration, HHI, group budgets, correlation, warnings
│   ├── test_proposal.py
│   ├── test_proposal_store.py
│   ├── test_risk_gate.py
│   ├── test_sdk.py
│   ├── test_serialization.py
│   ├── test_strategy.py
│   └── test_trading_filter.py
│
├── Dockerfile                 # Debian bookworm + Python venv
├── docker-compose.yml         # IB Gateway sidecar + MMR container
├── docker.sh                  # Docker/Podman build/deploy helper
├── start_mmr.sh               # Startup script (tmux + pycron)
└── pyproject.toml             # Package config, dependencies, entry points
```

## Docker Setup

Split-compose topology (`docker-compose.yml`) — each process is its own container (`read_only: true`, baked image):

- **ib-gateway**: `ghcr.io/gnzsnz/ib-gateway:latest` — IB Gateway. Credentials in `.env`. `scripts/ib-gateway-run.sh` patches upstream `inst_jre.cfg`.
- **trader**: `python -m trader.trader_service` — typed ports 42101/42102 published to host loopback; owns DuckDB volume.
- **strategy**: `python -m trader.strategy_service` — typed control 42104/42105; dials trader typed + PubSub.
- **data**: `python -m trader.data_service` — history downloads (42003).
- **dashboard**: FastAPI web UI + command center (host port published).
- **scheduler**: pycron for one-shot cron only (data refresh, backups) — not a multi-service supervisor.

Do **not** run the legacy monolithic `start_mmr.sh` inside split containers (it collides on ports/client ids). Local non-Docker still uses `./start_mmr.sh`. Code changes require `./docker.sh -b -u` (sync `-s` is retired — images are immutable).

IB Gateway API ports map to host `7496` (live) / `7497` (paper); VNC at `5901`.

Storage layout (host paths):
- `~/.local/share/mmr/logs/` — bind mount
- `~/.local/share/mmr/tws_settings/` — IB session state
- `~/.local/share/mmr/backups/` — `docker.sh -B` / `mmr data backup`
- `~/.local/share/mmr/artifacts/` — signed paper-automation bundles (ro in trader/strategy)
- DuckDB (`mmr.duckdb`, `mmr_history.duckdb`) in named volume **`mmr_db_data`**

## Build & Run

```bash
# Docker (first-time prompts for IB credentials, writes .env)
./docker.sh -g              # Build + start + shell into trader
./docker.sh -b              # Build shared image
./docker.sh -u              # Start containers
./docker.sh -d              # Stop containers
./docker.sh -e              # Shell into trader (default)
./docker.sh -e dashboard    # Shell into another split service
./docker.sh -l              # Tail logs
./docker.sh -c              # Clean images/volumes
./docker.sh -B              # Backup DuckDB (auto-timestamped)
./docker.sh -B before_run   # Backup with a custom name
# After code changes: ./docker.sh -b -u  (images are read-only; -s sync is retired)

# Local non-Docker
./start_mmr.sh --setup      # Wizard: IB + API keys
./start_mmr.sh              # tmux + services
./start_mmr.sh --paper
./start_mmr.sh --no-tmux

# Individual services (local)
python3 -m trader.trader_service
python3 -m trader.strategy_service
python3 -m trader.data_service
python3 -m trader.mmr_cli
```

## Package Installation

The project is a proper Python package installable via pip:

```bash
pip install -e .             # Editable install

# Console entry points after install:
trader-service               # trader.trader_service:main
strategy-service             # trader.strategy_service:main
data-service                 # trader.data_service:main
mmr                          # trader.mmr_cli:main
```

## CLI Commands

```
status                       # Service connectivity check
resolve AMD                  # Resolve symbol to conId/universe
resolve EURUSD --sectype CASH # Resolve forex pair via IB (IDEALPRO)
portfolio                    # Current portfolio
orders                       # Open orders
buy AMD --market --amount 100.0            # offline-simulation legacy only in split Docker
sell AMD --market --quantity 10            # prefer: propose → approve
cancel 123
cancel-all                   # Cancel all orders
close 1                      # Close position by row number
strategies                   # List strategies
strategies enable my_strat   # Enable a strategy
strategies reload            # Reload strategies from YAML + re-subscribe (immediate reconciliation)
strategies inspect           # AST scan: class name, mode (precompute/on_prices), tunable params with defaults
backtest -s strategies/keltner_breakout.py --class KeltnerBreakout --conids 756733
backtest -s ... --params '{"EMA_PERIOD": 15, "BAND_MULT": 2.5}'   # JSON param overrides
backtest -s ... --param EMA_PERIOD=15 --param BAND_MULT=2.5       # repeatable KEY=VALUE form
backtest -s ... --summary-only --no-save-trades                    # skip trades blob + persist
bt-sweep -s strategies/orb.py --class OpeningRangeBreakout --conids 756733 \
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
snapshot AMD                 # Price snapshot
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
options snapshot O:AAPL260320C00250000                # Single contract detail
options implied AAPL -e 2026-03-20                    # Probability distribution
options buy AAPL -e 2026-03-20 -s 250 -r C -q 5 --market
options sell AAPL -e 2026-03-20 -s 250 -r C -q 5 --limit 3.50
news                                         # General market news
news AAPL                                    # News for a ticker
news AAPL --limit 20                         # More articles
news AAPL --source benzinga                  # Benzinga source
news AAPL --detail                           # Full details + sentiment
movers                           # Top stock gainers (default)
movers --market crypto           # Crypto gainers
movers --market indices          # Index gainers
movers --losers                  # Stock losers
movers --market crypto --losers  # Crypto losers
scan                             # Top gainers (default preset)
scan losers                      # Top losers
scan active                      # Most active by volume
scan hot-volume                  # Hot by volume change
scan --scan-code HIGH_OPT_VOLUME # Raw IB scanner code
scan gainers --above-price 10 --num 30  # Filtered
scan --instrument ETF --location STK.US  # ETFs
ideas                                        # Momentum scan (default, US/Massive)
ideas gap-up                                 # Gap-up preset
ideas mean-reversion                         # Mean-reversion preset
ideas breakout                               # Breakout preset
ideas gap-down                               # Gap-down preset
ideas volatile                               # Volatile/scalping preset
ideas momentum --tickers AAPL MSFT AMD NVDA  # Scan specific tickers
ideas momentum --universe sp500              # Scan a universe
ideas gap-up --min-price 10                  # Override preset filter
ideas volatile --num 25                      # Top 25 results
ideas --presets                              # List all presets
ideas momentum --detail                      # Show all columns incl. indicators
ideas momentum --fundamentals                # Enrich with financial ratios (PE, D/E, ROE, etc.)
ideas momentum --news                        # Enrich with latest news headline + sentiment
ideas mean-reversion --news --fundamentals   # Full picture: technicals + fundamentals + news
ideas gap-up -t AAPL MSFT --fundamentals     # Specific tickers with fundamentals
ideas momentum --location STK.AU.ASX --tickers BHP CBA CSL  # ASX via IB
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
approve 3                                    # Execute proposal #3 (requires trader_service)
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
forex snapshot EURUSD                        # Forex snapshot via IB (default)
forex snapshot EURUSD --source massive       # Forex snapshot via Massive
forex quote EUR USD                          # Last bid/ask via IB (default)
forex quote EUR USD --source massive         # Last bid/ask via Massive
forex snapshot-all                           # All forex snapshots (Massive only)
forex movers                                 # Top forex gainers (Massive only)
forex movers --losers                        # Top forex losers (Massive only)
forex convert EUR USD 1000                   # Currency conversion (Massive only)
```

## Ideas Scanner Architecture

Three backends share scoring/filtering:

```
ideas momentum                                  → IdeaScanner (Massive, US, ~4s) — needs Stocks Starter+
ideas --source twelvedata --tickers …           → TwelveDataIdeaScanner (quotes + local indicators)
ideas momentum --location STK.AU.ASX --tickers  → IBIdeaScanner (IB, international, ~30-90s)
```

Bare `ideas` defaults to Massive. On Massive `NOT_AUTHORIZED` (Stocks Basic) or TwelveData movers 403 (non-Pro), the SDK falls back to TwelveData quotes on a small liquid US set (or your `--tickers`/`--universe`) and prints a yellow notice.

### IdeaScanner (Massive path — default)

```
Massive movers / snapshot_all  →  filter  →  Massive indicator API (parallel)  →  score  →  rank
```

- Fast (~4s) when entitled — server-side indicators, batch snapshots
- US only; fundamentals + news with sentiment when plan allows

### TwelveDataIdeaScanner (`--source twelvedata`)

```
movers (Pro+) or batch /quote  →  filter  →  time_series + local RSI/EMA/SMA  →  score  →  rank
```

- Quotes work on Basic/Starter; `/market_movers` needs Pro+
- No news endpoint — `--news` is a no-op on this path

### IBIdeaScanner (IB path — `--location`)

```
IB scanner / resolve_contract  →  get_snapshot (sequential)  →  reqHistoricalData  →  local RSI/EMA/SMA  →  score  →  rank
```

- Slower (~30-90s) — sequential IB snapshot requests, IB pacing limits
- Works for any IB-supported market: ASX, TSE, SEHK, EU exchanges, etc.
- Discovery: IB scanner API (when available) or explicit `--tickers`/`--universe`
- The IB scanner API may not support all location codes (error 162). When this happens, use `--tickers` to specify symbols explicitly
- Indicators computed locally from history bars (pure pandas) — `compute_rsi()`, `compute_ema()`, `compute_sma()`
- Fundamentals from `reqFundamentalData` (ReportSnapshot XML), news from `reqHistoricalNews` (no sentiment)
- IB news headlines include metadata prefixes like `{A:800015:L:en}...` which are stripped before display

### Shared module-level functions (used by Massive, TwelveData, and IB scanners)

- `PRESETS` dict, `ScanFilter`/`ScanPreset` dataclasses
- Scoring functions: `_score_momentum`, `_score_gap_up`, `_score_gap_down`, `_score_mean_reversion`, `_score_breakout`, `_score_volatile`
- `apply_filters()`, `to_dataframe()`, `merge_filters()`
- `PRESET_SCAN_CODES` maps preset names to IB scan codes

### IB Scanner Location Codes

Common codes: `STK.US.MAJOR` (US), `STK.AU.ASX` (Australia), `STK.CA` (Canada), `STK.HK.SEHK` (Hong Kong), `STK.JP.TSE` (Japan), `STK.EU` (Europe).

The scanner API requires market data subscriptions for the target exchange. If the scanner returns error 162 ("Market Scanner is not configured for one of the chosen locations"), use `--tickers` to provide symbols explicitly — they'll be resolved via `resolve_contract` with the exchange extracted from the location code.

When explicit tickers are provided with `--location`, the `min_change_pct`/`max_change_pct` preset defaults are relaxed (the user chose these symbols specifically and shouldn't have them filtered out).

## Contract Resolution

CLI/SDK `resolve()` uses typed `discover_instrument` / `resolve_instrument` (42101) — not legacy dill. Server-side, `resolve_symbol()` in `trading_runtime.py` remains a **local DB lookup** (no fuzzy matching). Integer conIds must never be coerced to ticker strings (`4391` ≠ TSEJ `"4391"`). IB discovery with exchange hints goes through `resolve_contract` / typed discover. Forex (`sec_type='CASH'`) uses IDEALPRO.

## Configuration

User configs live in `~/.config/mmr/`. On first run, bundled defaults from `config_defaults/` are copied there automatically (`container.ensure_config_dir()`). The `TRADER_CONFIG` env var overrides the config file path.

**`~/.config/mmr/trader.yaml`**: IB connection (address, port, client IDs, account), DuckDB path, ZMQ port assignments. Env vars override config values (uppercased param name). Two CLI-only knobs the Container doesn't otherwise know about:
- `default_data_source` (default `twelvedata`) — default `--source` for history download, snapshot, watch, financials, fx where that choice is valid. **`ideas` and `movers` always default to `massive`** (Massive-first; TD movers is Pro+-gated). Override the global default per-shell with `MMR_DEFAULT_DATA_SOURCE`.
- `equity_decimation` (default `daily`) — how aggressively backtest persist downsamples `equity_curve_json`. `daily` ≈ 17 KB/run vs ~9.9 MB raw 1-min; statistically lossless for PSR/Sharpe-CI. Override with `MMR_EQUITY_DECIMATION`.

**`~/.config/mmr/pycron.yaml`**: Service definitions with cron scheduling, auto-restart, dependency ordering. Also hosts `data_refresh_us` / `data_refresh_asx` cron entries that drive the data-refresh loop (see below).

**`~/.config/mmr/data_refresh.yaml`**: Declarative refresh jobs that keep universes' OHLCV current in the local DuckDB. Each job is `{universe, source, bar_size, days, force?}`. Pycron owns the *when* (`data_refresh_*` entries in `pycron.yaml`), this file owns the *what*. Source auto-detects from the universe's dominant exchange (US → twelvedata, else IB) when omitted. Commands: `mmr data refresh <job> [<job> ...]` runs jobs ad-hoc, `mmr data refresh --all` runs everything, `mmr data status` shows per-(job, bar_size) freshness with stale-day coloring. Refresh is incremental by default (only missing date ranges fetched, so daily crons are cheap); set `force: true` to refetch the full window.

**`~/.config/mmr/strategy_runtime.yaml`**: Strategy name, Python module path, class name, bar_size, conids/universe, historical_days_prior.

**`~/.config/mmr/logging.yaml`**: Python logging config (Rich console handler + rotating file handlers).

**`.env`** (gitignored): IB Gateway credentials (TWS_USERID, TWS_PASSWORD, TRADING_MODE, IB_ACCOUNT).

## Logging

Logs are written to `~/.local/share/mmr/logs/` with per-session timestamps (e.g. `trader_service_2026-02-19_18-38-06.log`). The directory is created automatically. Console output uses Rich for colored log levels and timestamps. Configured in `~/.config/mmr/logging.yaml`.

## Key ZMQ / typed ports

| Port  | Protocol | Service / role |
|-------|----------|----------------|
| 42101 | Typed query (HMAC) | trader — production CLI/dashboard reads |
| 42102 | Typed command (HMAC) | trader — propose/approve, cancels via command center |
| 42103 | Typed feed (HMAC) | trader — internal (not host-published) |
| 42104 | Typed command (HMAC) | strategy — enable/disable/reload |
| 42105 | Typed query (HMAC) | strategy — list_strategies |
| 42002 | PubSub | ticker broadcast |
| 42003 | Legacy RPC | data_service |
| 42005 | Legacy RPC | strategy (compat) |
| 42006 | MessageBus | strategy signals |
| 42001 | Legacy dill RPC | trader — **unbound in split production**; offline simulation only |

## Dependencies

Key packages: `ib_async`, `duckdb`, `pyzmq`, `msgpack`, `reactivex`, `pandas`, `numpy`, `pyarrow`, `dill`, `rich`, `backoff`, `psutil`, `exchange-calendars`, `massive`.

Python >= 3.12. Install: `pip install -e .` or `pip install -r requirements.txt`

## Testing

Tests use pytest with shared fixtures in `tests/conftest.py`. All tests are unit tests that use temporary DuckDB databases (no IB connection required). The suite currently runs **1059 tests in ~48s** with zero failures:

```bash
pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py
```

`test_ibrx_async.py` is excluded because it spins up long-lived asyncio tasks that interact with a mocked ib_async event loop; it works in isolation but flakes in the full suite.

Fixtures include edge-case OHLCV shapes (`ohlcv_with_gaps`, `ohlcv_high_volatility`, `ohlcv_zero_volume`, `ohlcv_halted`) in addition to the clean `sample_ohlcv`. Use the edge-case ones when testing indicator computation, position sizing, or backtesting against realistic-ugly data.

Key behaviour-focused test files:

- `test_clientserver_rpc.py` — RPC error-type preservation, dill whitelist/strict-mode policy, in-process round-trip with a threaded server
- `test_trading_runtime.py` — PnL subscription lock, off-loop portfolio routing, bracket-order rollback
- `test_executioner.py` — trading filter + risk gate rejection paths, `skip_risk_gate` bypass, IB account mismatch
- `test_strategy_runtime_reconcile.py` — load-strategy sandbox (absolute-path + traversal rejection, `sys.modules` collision), config-reload resilience (`yaml.safe_load`, partial-write mtime handling)
- `test_propose_approve_integration.py` — end-to-end propose → approve → execute, failure-path transitions to `FAILED`, state-machine enforcement
- `test_backtester.py::test_no_lookahead_fill_at_next_bar_open` — hand-crafted bars that prove `next_open` fills come from bar t+1's open
- `test_backtest_stats.py` — PSR monotonicity, bootstrap CI tightness with sample size, MC streak expectations, graceful degradation below minimum samples
- `test_backtest_params.py` — type coercion across int/float/bool, class-attr shadowing vs class mutation, upper-case typo rejection, lower-case params-dict idiom
- `test_backtest_highlighting.py` — composite-score ranking + per-metric classifier thresholds
- `test_sweep.py` — sweep lifecycle (create / finalize / get / list), manifest validation (missing fields, scalar-instead-of-list, mutually-exclusive symbol sources), digest-markdown shape, crash-safe digest writing
- `test_portfolio_risk.py::TestSignedExposure` — hedged vs stacked correlation clusters, long/short exposure breakdown
- `test_duckdb_store.py::TestConcurrentAccess` — multi-thread write serialization
- `test_container.py::TestContainerHardening` — missing-param diagnostics, env-var coercion, YAML safety

Some test files have import errors due to missing optional dependencies (`aioreactive`) — these are pre-existing and can be ignored: `test_aiorx.py`, `test_aiozmq_simple.py`, `test_disposable.py`, `test_mmr_client.py`, `test_mmr_server.py`, `test_perf2.py`, `test_performance.py`.

## Writing a Strategy

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

## Claude Code Agent Workflow

### JSON Output

All CLI commands support `--json` for machine-readable output:

```bash
mmr --json portfolio
mmr --json resolve AAPL
mmr --json data summary
mmr --json backtest -s strategies/my_strategy.py --class MyStrategy --conids 265598
```

JSON output always follows the structure `{"data": ..., "title": ...}` for data commands and `{"success": bool, "message": ...}` for status messages.

### Explore → Write → Backtest → Iterate → Deploy

**Step 1: Explore available data** (no service needed)
```bash
mmr --json data summary                                    # What data is in local DuckDB
mmr --json data query AAPL --bar-size "1 day" --days 30    # Read OHLCV from local store
mmr data status                                            # Freshness per (universe, bar_size) job from data_refresh.yaml
```

**Step 2: Download historical data** (no service needed, requires massive_api_key)
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

### LLM Trading Loop

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
mmr --json ideas momentum --num 10          # US stocks via Massive (~4s)
mmr --json ideas gap-up --tickers AAPL MSFT NVDA  # Specific tickers
mmr --json ideas momentum --location STK.AU.ASX --tickers BHP RIO  # International via IB
```

**Step 3: Research candidates**
```bash
mmr --json snapshot AAPL                    # Current price + bid/ask
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

### Command Service Requirements

**No service needed** (fully local / REST keys only):
- `data summary`, `data query`, `data download`, `data refresh`, `data status`
- `backtest` / `bt`, `bt-sweep`, `sweep run/list/show`
- `backtests list/show/compare/confidence/archive/unarchive/delete`
- `strategies create`, `strategies deploy`, `strategies undeploy`, `strategies inspect`, `strategies signals`, `strategies backtest`
- `universe list/show/create/delete/remove/import`
- `propose`, `proposals`, `reject`, `group *`, `session`
- `ideas` / `movers` (Massive or TwelveData API keys; no trader_service)
- `financials`, `options`, `news` (Massive key)

**Requires trader typed RPC (42101/42102)** — production path; no legacy 42001:
- `portfolio`, `positions`, `orders`, `trades`, `account`, `status`, `resolve`, `snapshot` (IB), `depth`
- `approve`, `portfolio-risk` / `psnap` / `pdiff`, `reconcile`, `diagnose`
- `listen` (publish_instrument + PubSub)

**Requires strategy typed RPC (42104/42105)**:
- `strategies` list, `strategies enable|disable|reload`

**Requires offline-simulation legacy RPC (42001)** — clear error in split production:
- Direct `buy` / `sell` / `cancel` / `cancel-all` / `close` / `to-market` / `resize-positions` execute
- IB `scan`, `ideas --location`, options contract resolve via dill
- Prefer `propose` → `approve` or the dashboard command center instead

### ConId Lookup

Symbols in DuckDB are stored by conId (IB contract ID). To find a conId:
```bash
mmr --json resolve AAPL    # Returns conId in JSON data
```

Common conIds: AAPL=265598, MSFT=272093, NVDA=4815747. Note: conIds can become stale — always verify before use.

### Valid Bar Sizes

`1 secs`, `5 secs`, `10 secs`, `15 secs`, `30 secs`, `1 min`, `2 mins`, `3 mins`, `5 mins`, `10 mins`, `15 mins`, `20 mins`, `30 mins`, `1 hour`, `2 hours`, `3 hours`, `4 hours`, `8 hours`, `1 day`, `1 week`, `1 month`

### Strategy File Conventions

- Strategy files live in `strategies/` directory
- File name: `snake_case.py` (e.g. `my_strategy.py`)
- Class name: `CamelCase` (e.g. `MyStrategy`)
- Must subclass `trader.trading.strategy.Strategy`
- Must implement at least one of `on_prices(self, prices)` or the precompute+`on_bar` pair
- DataFrame columns: `open`, `high`, `low`, `close`, `volume`, `vwap`, `bar_count`, `bid`, `ask`, `last`, `last_size` (DatetimeIndex named `date`)

### Command Latency Reference

All timings measured on local macOS. Network commands (download) depend on Massive.com API latency. Set appropriate timeouts — in particular, 1-min backtests on 16K+ bars take 10-15s.

#### Data Download (`mmr data download`)
Downloads historical data from Massive.com REST API to local DuckDB. Sequential per symbol (not parallelized).

| Operation | Time | Notes |
|-----------|------|-------|
| 1 symbol, daily, 30 days (~21 bars) | ~3s | Includes API round-trip + DuckDB write |
| 1 symbol, daily, 365 days (~252 bars) | ~3s | Same API call, slightly more data |
| 1 symbol, 1-min, 30 days (~15K bars) | ~4s | Larger payload, single API call |
| 3 symbols, daily, 365 days | ~4s | ~1s per symbol after first |
| 5 symbols, daily, 365 days | ~6s | Downloads are sequential |

**Massive API behavior**: Returns up to 50,000 bars per call. For 1-min data, 30 days is ~15-20K bars (within single call). The API may return NaN/null rows for future dates or market holidays — the backtester drops these automatically.

#### Data Query (`mmr data query`)
Reads from local DuckDB. Very fast.

| Operation | Time |
|-----------|------|
| `data summary` (list all symbols/bar sizes) | ~1.5s |
| `data query SYMBOL --days 30` | ~2s |

#### Backtesting (`mmr backtest`)
CPU-bound bar-by-bar replay. Time scales with number of bars × strategy complexity.

| Operation | Time | Notes |
|-----------|------|-------|
| Daily, 1 symbol, 252 bars (simple strategy) | ~1.5s | MA crossover, RSI, mean reversion |
| Daily, 1 symbol, 252 bars (vectorbt strategy) | ~4s | First run includes numba JIT compilation |
| Daily, 3 symbols, 252 bars each | ~2.5s | Multi-conid merges timelines |
| 1-min, 1 symbol, 16K bars (simple strategy) | ~14s | Scales linearly with bar count |

**Vectorbt note**: Strategies using `vectorbt` indicators (e.g. `VbtMacdBB`) incur a one-time ~2s numba JIT compilation penalty on first run. Subsequent runs in the same process are fast. The JIT also produces verbose DEBUG logs from numba — these are harmless.

#### Ideas Scanner (`mmr ideas`)

| Operation | Time | Notes |
|-----------|------|-------|
| `ideas` (Massive, movers, momentum preset) | ~4s | 2 snapshot + ~40 indicator calls (parallelized) |
| `ideas momentum --tickers AAPL MSFT AMD` | ~2s | 1 batch snapshot + ~6 indicator calls |
| `ideas --presets` | ~1s | No API calls, prints preset table |
| `ideas volatile --num 25` | ~4s | Same as movers, just more output rows |
| `ideas momentum --location STK.AU.ASX --tickers BHP CBA CSL` | ~30-90s | IB path: sequential snapshots + history |

#### Strategy Management
All local file/YAML operations.

| Operation | Time |
|-----------|------|
| `strategies create name` | ~2s |
| `strategies deploy name` | ~2s |
| `strategies list` | ~2s |

#### Test Suite
| Operation | Time |
|-----------|------|
| Full working suite (483 tests) | ~20s |
| Single test file | ~1-2s |

#### Python Import Overhead
All commands have ~1s baseline overhead for Python startup + importing trader modules (pandas, numpy, duckdb, etc.). This is unavoidable and included in all timings above.
