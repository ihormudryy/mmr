# Changelog

All notable changes to MMR are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, minor versions may contain breaking changes.

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/ihormudryy/mmr/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ihormudryy/mmr/releases/tag/v0.1.0
