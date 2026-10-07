# Contributing to MMR

Thanks for helping. MMR places real orders, so we care more about correctness than speed. This guide covers how to set up, test and send a change.

## Ways to help

- **Report a bug**: open an [issue](https://github.com/ihormudryy/mmr/issues/new/choose) with the bug form. Include your version, paper or live mode, data source and logs.
- **Ask a question or share an idea**: use [GitHub Discussions](https://github.com/ihormudryy/mmr/discussions).
- **Fix a bug or add a feature**: for anything larger than a small fix, open an issue or discussion first so we can agree on the approach.
- **Share a strategy**: see [Contributing a strategy](#contributing-a-strategy).
- **Security problems**: never in a public issue. Follow [SECURITY.md](SECURITY.md).

## Development setup

You need Python **3.12.13** (pinned in `.python-version`) and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ihormudryy/mmr.git
cd mmr
uv sync --python 3.12.13 --frozen --extra test
```

You do not need an Interactive Brokers account to run the tests. To run the full stack, see [Getting Started](README.md#getting-started) and use a **paper** account.

[AGENTS.md](AGENTS.md) has the core rules for agents and contributors. The detailed guides are [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (services, ports, storage) and [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) (CLI). Read the parts that touch your change.

## Running the tests

Run the same commands as CI:

```bash
uv run --frozen pytest tests/ --timeout=30 --timeout-method=thread -q --ignore=tests/test_ibrx_async.py
uv run --frozen pytest tests/test_ibrx_async.py --timeout=30 -q
```

Every test is a unit test with a temporary DuckDB database. A pull request must keep the suite green.

## Project rules

These come from [AGENTS.md](AGENTS.md#design-principles). Reviews check them.

- **Precision over convenience.** Contract IDs (conIds) resolve exactly or fail. No fuzzy matching, no "close enough" lookups, no turning an integer conId into a ticker string.
- **Fail loudly.** When a broker or data call fails, raise an error the user can act on. Do not swallow exceptions and return empty results.
- **No look-ahead in backtests.** A strategy's `precompute` output at bar `i` may only use bars `0..i`. Check it with `trader.simulation.lookahead_check.assert_no_lookahead`.
- **Propose, then approve.** New trading paths go through the proposal pipeline and the risk gate. Do not add direct order paths to the production command surface.
- **Never commit secrets.** IB credentials, API keys and `service_hmac.key` stay out of git.

## Coding style

- Match the code around your change: naming, comment density, idioms.
- Small functions with one job and names that say what they do.
- Comment only what the code cannot say for itself.
- Add or update tests with every behaviour change. Bug fixes come with a test that fails without the fix.

## Commits and pull requests

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(providers): make Alpaca the default movers source
fix(cli): default snapshots to IB unless quotes source is explicit
docs: document Alpaca quotes, movers and news
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `build`, `ci`.

For a pull request:

1. Branch from `master` and keep one concern per pull request.
2. Run the tests above.
3. Update the docs (`README.md`, `AGENTS.md`, `docs/`) when behaviour or commands change.
4. Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for user-facing changes.
5. Fill in the pull request template.

Pull requests are squash-merged, so the pull request title becomes the commit subject.

## Contributing a strategy

Strategies live in `strategies/` and subclass `trader.trading.strategy.Strategy`. See [Writing a Strategy](README.md#writing-a-strategy).

A strategy pull request should include:

- Tunable parameters as upper-case class attributes (`EMA_PERIOD = 20`).
- A test that runs `assert_no_lookahead` on the strategy.
- Backtest evidence: the command you ran and the `mmr backtests show <id>` output, including the statistical-confidence block. A good Sharpe on a few trades is not evidence.

## License

MMR is licensed under the [Apache License 2.0 with the Commons Clause](LICENSE.md). By contributing, you agree that your contribution is licensed under the same terms.
