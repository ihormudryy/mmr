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
| Codex | `codex/p1-liquidation-saga` / `.worktrees/p1-liquidation-saga` | P1 Task 7: `trader/trading/liquidation_service.py`, `trader/trading/command_stack.py`, `trader/trader_service.py`, `trader/trading/command_coordinator.py`, `tests/test_liquidation_service.py`, `tests/integration/test_command_authority.py` | Foundation `a11e546` | IN PROGRESS — durable recovery `7dce3a2`, startup rescan `5a30567`; authenticated command surface/tests remain |
| Claude | `feat/p1-task8-harness` / `.claude/worktrees/p1-task8-harness` | P1 Task 8: `config_defaults/trader.yaml`, `scripts/command_plane_drill.py`, `docs/superpowers/rollout/trading-income-operations-runbook.md`, `tests/integration/test_command_plane_activation.py` | Foundation `a11e546`; Task 7 liquidation contract before final drill integration | HANDOFF READY — `575a799` |
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
