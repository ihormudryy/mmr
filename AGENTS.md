# AGENTS.md

Rules and essentials for AI coding agents. Details live in the files listed in [Where to find more](#where-to-find-more).
When you resume operational work, read [`docs/OPERATIONAL_STATE.md`](docs/OPERATIONAL_STATE.md) first (what is armed and running now).

## GitHub identity for AI agents

Every AI model works on GitHub under its own GitHub App, never under the owner's account. Comments, reviews and PRs then show as `<app>[bot]`.

| Model | App | Key name | Permissions |
|-------|-----|----------|-------------|
| Claude (Anthropic) | `mmr-claude` | `claude` | issues, PRs: read/write; code: read/write (push branches) |
| OpenAI (GPT, also called "Astra") | `mmr-openai` | `openai` | issues, PRs: read/write; code: read/write (for approvals and resolving threads only) |
| Grok (xAI) | `mmr-grok` | `grok` | issues, PRs: read/write; code: read/write (for approvals and resolving threads only) |

Get a token for your own key name before any `gh` call. A token is valid for 1 hour; get a new one per session or after a 401.

```bash
export GH_TOKEN=$(scripts/gh-app-token.sh openai)   # use your key name
gh api repos/ihormudryy/mmr/issues/16/comments -f body='...'  # posts as mmr-openai[bot]
gh pr review 46 --comment -b '...'
```

Claude pushes a branch with its token:
`git push "https://x-access-token:$(scripts/gh-app-token.sh claude)@github.com/ihormudryy/mmr.git" <branch>`

Rules:
- No token (script missing, error, 401 after a refresh)? Stop and tell the owner. Never fall back to the owner's own `gh` login for any write.
- Run the script from the repo root. The keys exist only on the owner's Mac, in `~/.config/github-apps/`. Anywhere else you have no token: ask the owner, do not post.
- The installed `gh` is old (2.22): `gh pr edit` fails. Use REST, e.g. `gh api --method PATCH repos/ihormudryy/mmr/pulls/N -f body='...'`.
- Use only your own key name. Never print, log or commit a token or anything in `~/.config/github-apps/` (private keys, mode 0600).
- Apps cannot change the project board (status, priority, iteration); it belongs to a personal account. Leave board changes to the owner.
- Apps cannot be assigned to issues or requested as reviewers. Post a review or a comment instead.
- Sign review comments with the model name (for example "Reviewed by openai/gpt-...").

Asking another agent for a review:
- @-mentions do not reach an App (Apps get no notifications). Add a label instead: `review:openai`, `review:grok` (or `review:claude`) on the PR or issue, plus one comment that says what to review, the head commit, where to start and what is already known.
- Each reviewer finds its work with `gh api 'search/issues?q=repo:ihormudryy/mmr+is:open+label:review:<key>'`, posts its review as its own App, then removes its own label.
- Prefer a PR review with line comments. Put a one-line verdict (ready / not yet) at the top and name the ticket for each finding.

How to review (every reviewer model works the same way):
- Review the pushed head. Put its commit SHA at the top, next to the verdict.
- For a stacked PR, review only its own diff against its base PR. Do not re-review the base.
- Say what you verified: the tests you ran (files or focused runs), whether you ran the full suite, and that you placed no IB orders and did not run Docker against the live stack.
- Check that CI on the head is green, or say why a red check is not caused by this PR.
- A blocker needs a failing test (preferred) or an exact event order. Give the smallest regression test that proves the fix.
- Do not re-report items listed as "known" or "open" in the PR unless you have a new failure trace.
- When no blocker is left, submit `gh pr review N --approve` with the one-line verdict. Verified and resolved threads alone are not an approval.
- Once a fix is verified, resolve the threads you opened, then remove your own `review:<key>` label.

Review threads:
- The author answers in each thread: "Fixed in <sha>: ... Test: ..." after the fix is pushed, or "Disagree: <reason>". While fixes are not pushed yet, the author posts one PR comment saying so, so reviewers know why nothing has changed.
- Reviewers have code write access only so their approvals count toward master's approval rule and so they can resolve threads. A reviewer never pushes commits, never pushes to a branch it reviews, and never merges. An approval (`gh pr review N --approve`) means: the latest verdict is ready, with no blocker.
- Only the reviewer who opened a thread resolves it, after checking the fix on the pushed head. If the fix is not there, reply in the thread (do not wait silently). If a follow-up is needed, name the exact regression test that would prove it.
- The author re-adds the `review:<key>` labels after pushing fixes; that is the signal for the next round.

Automatic reviews: adding `review:openai` or `review:grok` to a PR also runs `.github/workflows/ai-review.yml`. It reads the diff only (not the whole repo, no test run), asks the model through `scripts/ai_review.py` (backend `REVIEW_PROVIDER` = openrouter, bedrock or azure; model in the `REVIEW_MODEL_OPENAI` / `REVIEW_MODEL_GROK` repo variables), posts the review as that App and removes the label. A full agentic review (reading code, running tests) is still done by the owner's tools. The script reads this section as the review rules, so keep its heading unchanged.

When a review is done (stopping rule):
- **Blocker:** a defect with a concrete failing input, a failing test, or an exact event order that loses protection, sends a wrong order or reports a false success. Only blockers hold a merge.
- **Major:** a real defect without such a trace, or a missing test for a risky path. Becomes a follow-up ticket; it does not hold the merge.
- **Minor:** style, naming, cost, docs. Mention once; no re-review round.
- A finding that repeats an item listed as "known open" in the PR is not a blocker unless it adds a new failure trace.
- The PR is ready when every reviewer's latest verdict has no blocker. The author answers each blocker with a fix and a test, or with a reasoned "disagree".

## Project overview

MMR (Make Me Rich) is a Python algorithmic trading platform for Interactive Brokers (IB). It runs automated strategies, an interactive CLI (`mmr`), historical data collection, live market data, backtests and idea scans for US and international markets. Python >= 3.12 (CI pins 3.12.13).

## Design Principles

- **Precision over convenience.** Wrong data is worse than no data. Contract IDs (conIds) resolve exactly or fail. No fuzzy matching, no string coercion, no "close enough". An integer conId must never become a ticker lookup (conId `4391` is not the TSEJ ticker `"4391"`). A symbol on the wrong exchange is a bug. ConIds change: check them with `mmr resolve SYMBOL` before hardcoding.
- **Fail loudly, not silently.** When an IB or provider call fails (scanner error 162, no market data subscription, contract not found, missing API key), raise an error that says why. Never swallow an exception and return an empty result.
- **Free providers first, IB fallback.** Alpaca (free) is the default for US history, `movers`, `news`, `ideas` and options data. International markets (ASX, TSE, SEHK, ...) and all orders use IB. Massive and TwelveData are opt-in with `--source`. Only history inherits `default_data_source`; other commands have their own `data_providers.*` setting. Do not use Yahoo Finance. Full rules: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#data-provider-selection).
- **No fake sentiment.** IB news has no sentiment score. Show only the headline; never estimate one. Sentiment comes only from the Massive (Polygon) path.

## Architecture

Production Docker runs split services, one per container (`docker-compose.yml`):

- **trader** (`trader.trader_service`): the trading runtime. Owns the IB connection (`ib_async`), orders, portfolio, risk gate and DuckDB.
- **strategy** (`trader.strategy_service`): loads strategies from `strategy_runtime.yaml`, reconciles every 30 s, sends signals.
- **data** (`trader.data_service`): history downloads.
- **dashboard** (`web/app.py`): FastAPI web UI and command center.
- **scheduler** (pycron): cron jobs only (data refresh, backups). Not a process supervisor.
- **ib-gateway**: IB Gateway. Host ports `7496` live, `7497` paper; VNC `5901`.

The CLI, SDK and dashboard talk to trader and strategy over **typed RPC** (`trader/messaging/typed_rpc.py`): JSON-safe pydantic messages signed with Ed25519. Each principal (`trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research`) has its own key pair in `~/.config/mmr/keys/rpc/` (`mmr keys init`; Docker `./docker.sh -k`). Every method has an allow-list entry in `trader/messaging/principals.py`; other callers get `PERMISSION_DENIED`. Command authority comes from the verified principal, never the body. No HMAC mode. The **legacy dill RPC** (`clientserver.py`) can run code on load; trader's port 42001 and strategy's port 42005 are unbound in production and only offline simulation turns them on. Research attestations and paper-automation bundles use separate Ed25519 keys; an RPC key is never accepted as a bundle key.

| Port  | Protocol | Service / role |
|-------|----------|----------------|
| 42101 | Typed query (Ed25519) | trader: CLI/dashboard reads |
| 42102 | Typed command (Ed25519) | trader: propose/approve, cancels |
| 42103 | Typed feed (Ed25519) | trader: internal |
| 42104 | Typed command (Ed25519) | strategy: enable/disable/reload |
| 42105 | Typed query (Ed25519) | strategy: list strategies |
| 42002 | PubSub | ticker broadcast |
| 42003 | Legacy RPC | data_service |
| 42005 | Legacy RPC | strategy: unbound in production; offline simulation only |
| 42006 | MessageBus | strategy signals |
| 42001 | Legacy dill RPC | trader: unbound in production; offline simulation only |

Top-level directories:
- `trader/`: core library and service entry points (`mmr_cli.py`, `sdk.py`, `trading/`, `messaging/`, `data/`, `simulation/`, `research/`, `automation/`, `data_providers/`).
- `strategies/`: strategy files. `research/`: specs for `mmr research evaluate`.
- `web/`: dashboard. `config_defaults/`: config templates (copied to `~/.config/mmr/` on first run only; editing them does not change a running system).
- `scripts/`: operational scripts, release gates, drills. `skills/`: Claude skills. `tests/`: pytest suite. `docs/`: reference docs.

## Safety rules (never break)

- **Propose, then approve.** Trades go `propose` → review → `approve` (or the dashboard command center), through the proposal state machine and the risk gate. Do not add a direct order path to the production command surface.
- **Direct `buy` / `sell` / `cancel` are offline only.** They need the legacy RPC (`unsafe_legacy_rpc: true` plus `--simulation True`) and a consistent paper posture (`trading_mode: paper`, a `D`-prefixed account and the IB paper port); any disagreement is refused. In production they fail with a clear error.
- **Protective orders are all or nothing.** A `BRACKET` is staged untransmitted; if the take-profit or stop leg fails, the staged legs are cancelled, so no unprotected entry is left. Keep this property in any order change.
- **Paper vs live.**
  - `auto_execute: propose` turns signals into PENDING proposals; `auto_execute: true` (full auto) is refused at load.
  - On paper an LLM may approve after its own evaluation. On live, a human must approve (`LLM_LIVE_APPROVE_FORBIDDEN`).
  - `automation.live_enabled` stays `false`. Automated paper entries need a signed, bound research bundle (see [docs/PAPER_AUTOMATION_SETUP.md](docs/PAPER_AUTOMATION_SETUP.md)).
  - `automation.quote_fallback: alpaca_iex` (paper only) uses Alpaca IEX quotes, labelled `iex_realtime`, when IB has no live feed. Never label them `live`; a live account never calls Alpaca. See [docs/OPERATIONAL_STATE.md](docs/OPERATIONAL_STATE.md).
- **Never print secrets.** No tokens, API keys, `.env` values, RPC private keys or signing keys in logs, output, commits or PR text.
- **YAML:** load untrusted YAML with `yaml.safe_load`. No `!!python/object` tags.
- **DuckDB:** access the database only through `DuckDBConnection.execute` / `execute_atomic` (short-lived connection under a lock). Never hold a long-lived connection; several services share the file.
- **Dill:** keep dill off the production surface. `MMR_DILL_STRICT=1` refuses arbitrary objects.

## AI paper mode (SP1)

Full detail: `docs/ARCHITECTURE.md` (`ai_paper` path and the paragraphs after it).

- **`ai_paper` path.** Paper only: a live account refuses the stack (`AI_PAPER_LIVE_REFUSED`). Typed commands `publish_ai_risk_policy` and `submit_ai_paper_decision` (`ai_supervisor`) and `register_ai_deployment` (`ai_research`), each with its own allow-list entry. The owner ceiling is `ai_paper.limits_ceiling` in `trader.yaml`, read only from the file (an `AI_PAPER*` env var fails load); no RPC method writes it. Entries go through the protective saga, `session_risk` and `DispatchGuard`; closes through the broker-proven safe close. Every refusal has its own code.
- No `ai_paper` entry may be enabled before #49 (gross reservations) is merged and verified. SP2 follow-up: the AI strategy runner must hash the file it loads and refuse a mismatch with the deployment's `strategy_digest`.
- **Experiments** (`trader/automation/experiments.py`, `mmr experiment ...`): `ARMED` ↔ `PAUSED`, then `KILLED` or `STOPPED`. `KILLED` is never resumed: the account flattens, run `mmr reconcile`, then `mmr experiment stop` (refused `NOT_FLAT` until the broker is flat), then a new `experiment start`. Start, resume and stop are `cli`/`dashboard` only; `ai_supervisor` may only pause. One-strategy automation and an experiment never arm together (`EXPERIMENT_ACTIVE`; both armed at startup refuses every entry `BOTH_MODES_ARMED`). Kill-line detection takes about 3 minutes, accepted for paper only.
- **Scoreboard** (`trader/scoreboard/`, `mmr scoreboard`): writes only to the journal DuckDB, one sealed `equity_daily` row per session end. A value that cannot be captured is NULL plus an incident, never 0. No principal has a scoreboard write; `verify_scoreboard` is for humans only. Telegram is send-only and off unless `ai_paper.telegram.enabled`. Every view says PAPER.
- **Acceptance** (`trader/acceptance/`, runbook `docs/PAPER_ACCEPTANCE_SP1.md`): `mmr experiment acceptance ...` runs on the host only and sends orders only with `--place-orders --confirm-account DU… --signing-key KEY`. A report is `passed` only with `oca_shrink: PROVEN` on `ib_paper` evidence and the operator key. The acceptance package never imports a store. Abort path: `mmr flatten` (paper only; `FLAT` only on broker evidence).

## Build, run and test

```bash
# Docker (split services)
./docker.sh -g              # build + start + shell into trader
./docker.sh -b -u           # rebuild and restart after code changes (images are read-only)
./docker.sh -d / -l / -e    # stop / tail logs / shell
./docker.sh -B [name]       # back up DuckDB

# Local (IB Gateway in a container, services on the host)
./start_mmr.sh --setup      # first-time wizard: IB + API keys
./start_mmr.sh --paper      # or --live
```

Do not run `start_mmr.sh` inside the split containers (port and client-id clashes).

Tests (same as CI, `.github/workflows/ci.yml`):

```bash
uv sync --python 3.12.13 --frozen --extra test
uv run --frozen pytest tests/ --timeout=30 --timeout-method=thread -q --ignore=tests/test_ibrx_async.py
uv run --frozen pytest tests/test_ibrx_async.py --timeout=30 -q   # flaky in the full run; run alone
```

- Tests live in `tests/`, shared fixtures in `tests/conftest.py` (including ugly OHLCV shapes such as `ohlcv_with_gaps`, `ohlcv_halted`).
- All are unit tests on temporary DuckDB files; no IB connection needed.
- Do not trust a test count written in a doc. Prefer the live pytest summary.

## Writing a strategy

- Subclass `trader.trading.strategy.Strategy` in `strategies/<snake_case>.py`, class name in `CamelCase`.
- Implement at least one dispatch API:
  1. `on_prices(prices)`: called per bar with the accumulated DataFrame. Simple.
  2. `precompute(prices)` + `on_bar(prices, state, index)`: the fast path for backtests. `precompute` runs once on the full history; `on_bar` reads arrays by index.
- The backtester calls `on_bar` (it falls back to `on_prices`). The live runtime calls `on_prices`. A fast-path strategy should implement both.
- **Lookahead contract.** `precompute` output at position `i` may use only bars `0..i`. No `shift(-1)`, centred windows, full-series normalisation or `fit_transform` on the whole history. Test it with `trader.simulation.lookahead_check.assert_no_lookahead(strategy, prices)`.
- Backtests fill at the next bar's open (`fill_policy='next_open'`) and use realistic costs by default.
- **Tunables:** upper-case class attributes (`EMA_PERIOD = 20`). `mmr strategies inspect` lists them; `--param EMA_PERIOD=15` and `bt-sweep --grid` override them. A typo raises `ValueError`.
- DataFrame columns: `open, high, low, close, volume, vwap, bar_count, bid, ask, last, last_size`; DatetimeIndex named `date`.

```python
class MyStrategy(Strategy):
    def on_prices(self, prices):
        if some_buy_condition(prices):
            return Signal(source_name=self.name, action=Action.BUY, probability=0.8)
        return None
```

Use `mmr --json ...` for machine-readable output from any command.

## Where to find more

- [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md): every CLI command, JSON output, which service each command needs, bar sizes, conId lookup.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): key patterns (DI container, messaging, storage, risk, sizing, research evaluation, bundle binding, backtester, sweeps, statistics), data-provider rules, configuration, logging.
- [docs/IDEAS_SCANNER.md](docs/IDEAS_SCANNER.md): the ideas scanner and its sources.
- [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md): explore → backtest → deploy, the LLM trading loop, strategy details, command latency.
- [docs/OPERATIONAL_STATE.md](docs/OPERATIONAL_STATE.md): what is deployed and armed now, known blockers.
- [docs/PAPER_AUTOMATION_SETUP.md](docs/PAPER_AUTOMATION_SETUP.md): unattended paper automation.
- [docs/AUDIT_ROADMAP.md](docs/AUDIT_ROADMAP.md): code backlog.
- [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md): setup, project rules, contributing.
- `skills/mmr-skill/references/STRATEGIES.md`: a full fast-path strategy example.
