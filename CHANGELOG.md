# Changelog

All notable changes to MMR are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, minor versions may contain breaking changes.

## [Unreleased]

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
