# Changelog

All notable changes to MMR are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, minor versions may contain breaking changes.

## [0.2.0] - 2026-10-07

The foundation for the AI paper bot (SP1). All of it is paper only.

### Breaking

- **Typed RPC uses Ed25519 keys, one per principal** (`trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research`), instead of the shared HMAC key. Create them before starting: `./docker.sh -k` (Docker) or `mmr keys init` (local); check the mounts with `./docker.sh -K`. Every method has an allow-list; other callers get `PERMISSION_DENIED`. Command authority comes from the verified principal, never the body. `service_hmac.key` is no longer used; retire it after you have verified the new keys. (#50)
- **Legacy dill RPC** (trader 42001, strategy 42005) binds only in offline simulation with a consistent paper posture (`trading_mode: paper`, a `D` account, the paper port). (#50)
- **No migration from 0.1.x state.** Start the trader journal database fresh; price history is not affected.
- `CLAUDE.md` is now `AGENTS.md`.

### Added

- **Safe close**: every paper exit goes through one broker-verified close service. It hands protection over before it cancels a stop, owns one close per position, re-protects the rest after a partial close, journals each child order before sending it and never reports a close that sold nothing as a success. Late fills after a flat close start a new safety close. (#46)
- **`ai_paper` decision path**: `submit_ai_paper_decision` for `ENTER`, `CLOSE` and `PARTIAL_CLOSE`. Owner risk ceiling plus a published AI risk policy (tighter applies at once, looser next session), sealed deployments, trading filters, margin evidence, an entry cutoff and trader-owned order types (DAY marketable limit, no market orders). (#55)
- **Experiments and the kill line**: `mmr experiment start|pause|resume|stop|status`. Arming only on a flat paper account, a lock against the one-strategy automation in both directions, and an optional drawdown kill line that flattens the account. (#56)
- **Paper scoreboard**: realized P&L, round trips, SPY benchmark, a `/cc` dashboard tab and an optional Telegram daily summary. (#57)
- **Acceptance harness**: a scripted SP1 acceptance run, a shrink probe and the runbook `docs/PAPER_ACCEPTANCE_SP1.md`. (#58)
- **Design for SP2a+b** (the autonomous loop): `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`. (#69)
- CI runs the tests in eight balanced shards and shows a coverage badge in the README. (#66, #67)

### Fixed

- Liquidation exits skip entry gates and no longer block the trader loop. (#42)
- Tightened allocation limits are re-checked at dispatch. (#48)
- In-flight automated entries reserve gross exposure; orphan reservations are retired only on broker evidence. (#52, #53)
- A partial fill reduces the bracket sibling instead of cancelling it. (#61)
- Automated buy commands are resolved from broker evidence. (#62)
- Non-finite RPC timestamps are refused at decode. (#63)
- YAML config is loaded with the safe loader. (#65)
- The scoreboard page no longer shows raw exception text on a timeout. (#57)

## [0.1.1] - 2026-10-05

### Added

- **Phase B evidence**: `research evaluate` now computes the liquidity envelope, the vol-matched SPY benchmark and the regime values from SPY daily bars, so a strategy can reach `PAPER_ELIGIBLE` on real data. "Holdout opened once" is keyed on strategy + window across families. Automated entries larger than 105% of the attested order notional are refused (`ORDER_EXCEEDS_ATTESTED_NOTIONAL`).
- **Research evidence for paper automation**: `mmr research evaluate <spec.yaml>` runs walk-forward backtests under the live paper rules at 1x/1.5x/2x costs and records paper-v1 evidence; the holdout opens once, only after every other rule passes. `research evaluations`, `research review submit --reviewer-kind human|llm` and `research attest bundle` complete the flow. Example spec: `research/example_spec.yaml`.
- **Bundle binding**: a bundle must attest the loaded strategy code, class, params, conids and bar size. It is checked at load, at arm and in Activate; dispatch rejects other artifacts (`ARTIFACT_NOT_ARMED`) and source mismatches (`STRATEGY_SOURCE_MISMATCH`).

### Changed

- **Activate** finds the newest eligible, qualified bundle bound to the strategy, or refuses with `NO_ELIGIBLE_BUNDLE` naming the latest evaluation. It no longer needs the bundle configured first. Production code can no longer create fixture bundles; `scripts/bootstrap_paper_automation.py` is read-only and takes `--bundle`.
- **Backtest costs**: CLI backtests default to `--cost-model realistic` (per-venue broker commission, tick half-spread, square-root impact). A conid without a known venue fails loudly. `--cost-model legacy` keeps the old flat costs.
- **`expectancy_bps`** is dollar-weighted (net P&L over entry notional), so its sign matches return and profit factor. Stored runs keep their old values.

## [0.1.0] - 2026-10-04

First tagged release. It captures the platform as it runs today.

### Added

- **Split services**: trader, strategy, data, dashboard and scheduler run as separate read-only Docker containers next to IB Gateway (`docker-compose.yml`, `docker.sh`). `start_mmr.sh` runs a hybrid local setup.
- **Typed HMAC RPC** on ports 42101–42105 for the CLI, SDK and dashboard. The legacy dill RPC is off in production.
- **Propose → approve pipeline** with a proposal state machine, ATR-aware position sizing, position groups with budgets, a pre-trade risk gate and trading filters.
- **Command center dashboard** (FastAPI): live portfolio, approve/reject/cancel, strategy controls with live parameter changes, watchlists, research tab and paper-automation activation.
- **Strategies**: `on_prices` and fast `precompute` + `on_bar` APIs, a signal → proposal bridge (`auto_execute: propose`), and 16 example strategies.
- **Backtesting**: next-bar-open fills (no look-ahead), parameter overrides and sweeps, YAML nightly sweep manifests, and statistical-confidence tests (PSR, t-test, bootstrap CI, skew/kurtosis, losing-streak Monte Carlo).
- **Free data providers**: Alpaca is the default for US history, movers and news. Massive (Polygon.io), TwelveData and IB remain available with `--source`.
- **Paper automation**: signed strategy bundles, release gates, fault drills and soak tooling.
- **Prebuilt Docker image**: each release publishes `ghcr.io/ihormudryy/mmr` for `linux/amd64` and `linux/arm64`.
- **LLM workflow**: every CLI command supports `--json`, compact portfolio snapshots and diffs, and Claude Code skills in `skills/`.

### Removed

- Arctic, MongoDB and Redis era code (`trader/cli`, `trader/batch`, `trader/portfolio`), outdated docs and unused dependencies.

[0.1.1]: https://github.com/ihormudryy/mmr/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ihormudryy/mmr/releases/tag/v0.1.0
