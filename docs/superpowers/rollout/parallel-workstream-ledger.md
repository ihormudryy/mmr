# Parallel Workstream Ledger

This tracked file is the handoff channel for concurrent work on the trading
income roadmap. It prevents branch assumptions from becoming an undocumented
interface.

## Protocol

1. A worker claims a lane here before editing production code.
2. A lane lists its branch, base commit, exact writable files, dependencies,
   and verification command.
3. Workers may not edit another active lane's writable files. Read-only
   inspection is allowed.
4. Every handoff is a small commit plus this ledger update. The handoff records
   the commit ID, tests run, and any interface contract consumed or produced.
5. Only the integration owner cherry-picks into
   `feat/command-center-foundation`, after checking the dependency commit and
   rerunning the lane's focused tests.
6. If a dependency changes, mark the dependent lane `REBASE_REQUIRED`; do not
   silently adapt its behavior while resolving a merge conflict.

## Active lanes

| Owner | Branch / worktree | Scope and writable files | Depends on | Status |
|---|---|---|---|---|
| Codex | `codex/p1-liquidation-command` / `.worktrees/p1-liquidation-command` | P1 Task 7: broker-verified liquidation root and authenticated command surface | Foundation `a11e546` | INTEGRATED — `fe4155e`; root remains `OUTCOME_UNKNOWN` until broker-confirmed flat evidence |
| Claude | `feat/p1-task8-harness` / `.claude/worktrees/p1-task8-harness` | P1 Task 8 (complete) | Foundation `a11e546` | MERGED — Task 8 done; 9-drill battery incl. 2 restored live-mode DispatchGuard drills (`77341f4`) |
| Claude | `feat/p2-research-evidence` / `.claude/worktrees/p2-research-evidence` | P2 research-DB-only modules under `trader/research/` + research tests; `pyproject.toml`/`uv.lock`/`trader/config.py`/`config_defaults/trader.yaml`/`scripts/db_backup.sh` (Task 1). **Tasks 4-5 additionally make additive-only edits to `trader/data/backtest_store.py` (pure legacy-import helpers), `trader/mmr_cli.py` (new local-only `research` subcommand), and `trader/simulation/backtester.py` (Task 5: additive module-level `trace_signature`; `run`/`__init__` untouched).** No other active lane writes those files, so still parallel-safe. | Current main `77341f4`; P2 is parallel-safe per program index (writes only the research DB; the shared-file edits above are additive and reversible) | IN PROGRESS — Tasks 1 (`417c463`), uv-lock fix (`0cdf6be`), 2 immutable dataset manifests (`cb8f4fd`), 3 point-in-time universes + bar qualification (`44be144`), 4 complete experiment registry (`5f2aeb0`), 5 leakage-safe validation protocol (`12ccc51`) done; research package 204 tests green (research + backtester = 214); next Task 6 (quantitative eligibility ruleset) |
| Codex worker: backend dashboard safety | `codex/dashboard-backend-safety` | `web/command_center/quotes.py`, `state.py`, `bridge.py`, `sse.py`, focused backend tests | Foundation `d2a9cc2`; must publish server quote receive timestamps for the browser lane | CLAIMED |
| Codex worker: browser dashboard resilience | `codex/dashboard-browser-resilience` | `web/static/command_center.js`, browser/JS-focused tests | Backend quote receive timestamp contract | INTEGRATED — `ff0d453`; Node stateful tests pass |

## Contracts and merge order

### Task 7 liquidation contract (Codex → Claude)

Task 7 will expose a broker-evidence-only terminal state: a liquidation is
`FLAT` only after a fresh promoted broker snapshot contains no positions and no
working orders. Claude's drills must assert that property and must never infer
flatness from an order acknowledgement.

The exact public receipt/state fields will be recorded here with the Task 7
commit. Until then, Task 8 may build fixtures, report serialization, and the
runbook, but its liquidation scenario remains `REBASE_REQUIRED`.

### Current handoff

Claude's `575a799 test(command-plane): add production activation and recovery
gates` changes only the Task 8 files listed above. It is safe to review in
parallel, but must be rebased or cherry-picked only after Task 7's contract is
available. Integration owner: Codex.

## Completed foundation commits

| Commit | Meaning |
|---|---|
| `41a8b7f` | Split safe pause from guarded resume; bound preflight issuance and submission to the same command/session. |
| `a11e546` | Durable automation circuit breaker and semantic readiness. |
| `22a3785` | Broker-evidence-only liquidation saga core wired to the existing dispatcher; focused tests pass. |
| `ff0d453` | Dashboard SSE sequence-gap recovery, degraded polling state, and timestamp-based quote freshness. |
| `5a30567` | Startup and periodic recovery for unresolved broker-verified liquidations. |
| `fe4155e` | Authenticated, preflight-gated liquidation root command; no acknowledgement-based success. |
