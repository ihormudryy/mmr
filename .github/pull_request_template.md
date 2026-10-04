## What and why

<!-- What does this change, and why is it needed? Link the issue: Closes #123 -->

## How it was tested

<!-- Commands you ran and what you checked. For strategies, include the backtest command and `mmr backtests show <id>` output. -->

## Checklist

- [ ] One concern per pull request; title follows [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` ...)
- [ ] Tests added or updated, and the suite passes (`uv run --frozen pytest tests/ --timeout=30 --timeout-method=thread -q --ignore=tests/test_ibrx_async.py`)
- [ ] Docs updated (`README.md`, `CLAUDE.md`, `docs/`) if behaviour or commands changed
- [ ] `CHANGELOG.md` updated under `Unreleased` for user-facing changes
- [ ] No secrets, account numbers or local paths in the diff
