<h1 align="center">LLM-native algorithmic trading for Interactive Brokers</h1>

<p align="center">
  <a href="https://github.com/ihormudryy/mmr/actions/workflows/ci.yml"><img src="https://github.com/ihormudryy/mmr/actions/workflows/ci.yml/badge.svg?branch=master" alt="CI"></a>
  <a href="https://github.com/ihormudryy/mmr/security/code-scanning"><img src="https://github.com/ihormudryy/mmr/actions/workflows/github-code-scanning/codeql/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/ihormudryy/mmr/actions/workflows/ci.yml"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ihormudryy/mmr/badges/coverage.json" alt="Coverage"></a>
  <a href="https://github.com/ihormudryy/mmr/releases"><img src="https://img.shields.io/github/v/release/ihormudryy/mmr" alt="Release"></a>
  <a href="https://github.com/ihormudryy/mmr/actions/workflows/release.yml"><img src="https://github.com/ihormudryy/mmr/actions/workflows/release.yml/badge.svg?branch=master" alt="Release image"></a>
  <br>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
  <a href="#disclaimer"><img src="https://img.shields.io/badge/trading-paper%20only-orange" alt="Paper trading only"></a>
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-Apache%202.0%20%2B%20Commons%20Clause-lightgrey" alt="License"></a>
  <a href="https://github.com/ihormudryy/mmr/commits/master"><img src="https://img.shields.io/github/last-commit/ihormudryy/mmr" alt="Last commit"></a>
</p>

<p align="center">
  <a href="docs/SETUP.md">Setup</a> ·
  <a href="docs/CLI_REFERENCE.md">CLI reference</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/AGENT_WORKFLOW.md">Agent workflow</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

MMR is a Python trading platform for Interactive Brokers, built to be run by people and by LLM agents. Every
operation is a CLI command with JSON output, every trade goes through a proposal and a risk gate, and an optional AI
controller trades a paper account on its own within limits the owner sets.

> [!WARNING]
> MMR can place real orders through your brokerage account. It is experimental software, without warranty, and
> nothing in it is financial advice. Live trading is switched off by default; start with an IB **paper** account and
> read the [Disclaimer](#disclaimer).

## Features

- **Propose, then approve.** No direct order path: every trade is a proposal with sizing, reasoning and a risk check,
  approved by a person (or, on paper only, by an AI after its own evaluation).
- **Built for agents.** `mmr --json <command>` for every operation, compact portfolio snapshots and diffs, and
  [Claude skills](skills/) for an autonomous research and trading loop.
- **AI paper trading.** A controller that judges strategy signals and its own ideas, a backtest judge for strategy
  versions, and AI-written strategies run in a locked-down sandbox. All paper only.
- **Research you can trust.** Walk-forward backtests with realistic costs, a lookahead check, statistical confidence
  tests and signed evidence bundles.
- **Safe by design.** Split Docker services with Ed25519-signed RPC per principal, protective orders that are all or
  nothing, a circuit breaker, a kill line and broker-verified closes.

## Getting started

```bash
git clone https://github.com/ihormudryy/mmr.git
cd mmr
./docker.sh -g        # build, enter your IB paper login, start everything, open a shell
mmr --json status     # check the connection to IB
```

Full setup, local install, the prebuilt image and configuration: [docs/SETUP.md](docs/SETUP.md).
First AI paper deployment: [docs/SP2_PAPER_DEPLOY.md](docs/SP2_PAPER_DEPLOY.md).

A trade, from proposal to order:

```bash
mmr propose AAPL BUY --market --confidence 0.7 --reasoning "Momentum above the 200-day average"
mmr proposals show 1        # check the computed size
mmr portfolio-risk          # check the risk the trade adds
mmr approve 1               # or: mmr reject 1 --reason "..."
```

## Writing a strategy

Subclass `Strategy` in `strategies/<name>.py` and return a signal:

```python
from trader.objects import Action
from trader.trading.strategy import Signal, Strategy

class MyStrategy(Strategy):
    def on_prices(self, prices):
        if some_buy_condition(prices):
            return Signal(source_name=self.name, action=Action.BUY, probability=0.8)
        return None
```

Then backtest it with `mmr backtest -s strategies/<name>.py --class MyStrategy --conids <conid>`. The fast-path API,
tunable parameters and the lookahead rules are in [STRATEGIES.md](skills/mmr-skill/references/STRATEGIES.md) and
[AGENTS.md](AGENTS.md#writing-a-strategy).

## Documentation

| Topic | Document |
|---|---|
| Installation, Docker, configuration | [docs/SETUP.md](docs/SETUP.md) |
| Every CLI command and its JSON output | [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) |
| Services, messaging, storage, risk, research | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| The LLM trading loop | [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md) |
| Dashboard and command center | [docs/DASHBOARD_USER_GUIDE.md](docs/DASHBOARD_USER_GUIDE.md) |
| Idea scanner and data sources | [docs/IDEAS_SCANNER.md](docs/IDEAS_SCANNER.md) |
| Backtest metrics | [docs/BACKTEST_METRICS.md](docs/BACKTEST_METRICS.md) |
| Paper automation and the AI paper deploy | [docs/PAPER_AUTOMATION_SETUP.md](docs/PAPER_AUTOMATION_SETUP.md), [docs/SP2_PAPER_DEPLOY.md](docs/SP2_PAPER_DEPLOY.md) |
| What runs now, known limits | [docs/OPERATIONAL_STATE.md](docs/OPERATIONAL_STATE.md) |
| Rules for AI coding agents | [AGENTS.md](AGENTS.md) |

## Development

```bash
uv sync --python 3.12.13 --frozen --extra test
uv run --frozen pytest tests/ --timeout=120 --timeout-method=signal -q --ignore=tests/test_ibrx_async.py
```

The tests use temporary DuckDB files and need no IB connection. Dev setup, conventions and the pull request
checklist: [CONTRIBUTING.md](CONTRIBUTING.md). Code of conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Questions:
[GitHub Discussions](https://github.com/ihormudryy/mmr/discussions).

## Security

Do not open a public issue for a security problem. Report it privately as described in [SECURITY.md](SECURITY.md).

## Disclaimer

MMR is experimental software for research and education, provided "as is", without warranty of any kind (see the
license). Nothing in this repository is financial, investment or trading advice. Trading involves substantial risk of
loss, and backtest results do not predict future returns. You alone are responsible for every order MMR places on your
behalf. Always test with an Interactive Brokers **paper** account first.

## Acknowledgements

MMR started as [9600dev/mmr](https://github.com/9600dev/mmr) by the 9600 Developments team; this repository continues
that work. It builds on [ib_async](https://github.com/ib-api-reloaded/ib_async), [DuckDB](https://duckdb.org),
[ZeroMQ](https://zeromq.org) and [vectorbt](https://github.com/polakowo/vectorbt).

## License

Source-available under the [Apache License 2.0 with the Commons Clause](LICENSE.md): you may use, modify and share
it, but you may not sell it or a service whose value comes substantially from it. Because of the Commons Clause, it
is not an OSI-approved open-source license.
