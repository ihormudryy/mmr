# AI Paper Bot — SP1 Foundation — Design Specification

**Date:** 2026-10-05
**Status:** Approved in brainstorming; implementation not started.
**Replaces:** the executing path of `2026-10-04-autonomous-ai-trading-module-design.md`.
That spec and its review (`docs/reviews/2026-10-04-autonomous-ai-trading-module-review.md`)
stay as background. Where they disagree with this document, this document wins.

This document authorizes no container restart, no broker order and no live authority.

## 1. Goal

Build a **hands-off paper bot** with full AI autonomy that tries to maximise
paper profit. The owner wants to see whether autonomy works.

The work is split into three sub-projects. Each gets its own spec, plan and build:

| Sub-project | Content |
|---|---|
| **SP1 — Foundation** (this spec) | Safe exits, scoreboard, service identities, `ai_paper` command path, arming and kill line. No model calls. |
| SP2 — Autonomous loop | Orchestrator, pluggable decider (Jev first), AI risk policies, discovery, Telegram commands. |
| SP3 — Sandboxed code generation | AI-written strategies, import allow-list, auto-commit, isolated runner, auto-deploy. |

"V1" means SP1 + SP2 + SP3 before the first autonomous paper run. SP1 comes first
because SP2 and SP3 depend on it, and because a bot that cannot close its own
positions is not hands-off.

## 2. Owner decisions

| Topic | Decision |
|---|---|
| Goal | Hands-off paper bot, full autonomy, maximise paper profit. |
| AI decides | Strategy choice, trade choice, new symbols, reports, new strategy code. |
| Risk limits | The AI sets them. The trader checks only structure and timing rules. |
| Kill line | `experiment_kill_drawdown_pct`. Default `null` (off). The owner may set e.g. `20`. |
| Account | The bot owns the paper account alone. It arms only when the account is flat. |
| Decider | Pluggable interface. Jev is the first adapter. The "follow the signal" baseline is logged beside it. |
| Jev as judge | Jev also judges backtest results (SP2): code-computed stats + trial count in, `DEPLOY` / `SHADOW` / `REJECT` out. |
| Styles | Designed for intraday long, swing long, intraday short, swing short. Enabled in phases: intraday long → swing long → short. |
| New code | AI writes strategies and they auto-deploy, in V1, only into a sandboxed runner (SP3). |
| Qualification | The AI (with Jev as judge) decides. No external ruleset gates the `ai_paper` path. |
| Exits | Full and partial closes. |
| Reporting | Scoreboard tab on `/cc` and a daily Telegram summary. |

With the default configuration the only brakes are broker buying power,
`/pause` and `/flatten`.

## 3. Accepted risks

The owner chose these against the reviewer's advice. They are recorded so the
results are read correctly.

- **AI-set risk limits.** Results are harder to compare across weeks. Every
  policy is logged with its reason so the scoreboard can show what was in force.
- **AI is its own gate.** Backtest numbers produced by the AI prove nothing on
  their own, because it chooses which ones to keep. Only forward paper P&L
  (section 5.2) answers whether the bot works.
- **AI-written code in V1.** More work before the first run. The sandbox must be
  correct from the first day (SP3).
- **No default kill line.** A bad run can use the whole paper account.
- **Partial exits.** The position has no stop for a few seconds during each
  partial close (section 5.1).

## 4. What stays unchanged

- The existing one-strategy paper automation (`automation.strategy_name`, the
  `paper-v1` Activate path, bundle binding) keeps all its checks. `ai_paper` is a
  separate mode. It must not widen the old path.
- `automation.live_enabled=true` is still refused at config load. `ai_paper`
  requires a paper account.
- There is one broker dispatch boundary. `ai_paper` commands go through the
  existing coordinator.
- `CommandReceipt` stays frozen. `ExecutionIntent` is not reused for AI decisions.
- `OUTCOME_UNKNOWN` is never resubmitted under a fresh id.
- The trader journal is the source of truth, not docs or YAML templates.

## 5. SP1 design

### 5.1 Safe close (full and partial)

**Problem.**

- An automated SELL without a quantity is rejected
  (`QUANTITY_REQUIRED`, `trader/automation/production_evidence.py:79`).
- A SELL with a quantity goes through `build_bracket_plan`, which adds a reverse
  BUY stop. `reducible_quantity` (`trader/data/broker_state.py:279`) ignores
  working orders, so the entry's protective stop stays live.
- Time exits (`SessionTimeExitAdapter`, `trader/automation/session_controller.py:292`,
  wired at `trader/trading/command_stack.py:902`) call `reduce` directly without
  cancelling the protective stop. After the close, the stop can fire and open a
  short. This was found by reading code; a failing test must confirm it first.

**Design.** Extend `LiquidationService` (`trader/trading/liquidation_service.py`)
with a scope instead of adding a second close path.

- `scope=account`: today's behaviour. Used by the session flatten, `/flatten`
  and the kill line.
- `scope=conid`, full close:
  `REQUESTED → CANCELLING → VERIFYING → REDUCING → VERIFYING → CLOSED`.
  It cancels only that conid's working orders, waits for a newer broker
  generation that shows them gone, sends a reduce of the broker quantity, and
  waits for a generation that shows a zero position.
- `scope=conid, quantity=q`, partial close: the same, then
  `REPROTECTING → VERIFYING → DONE`.
  - `q` is rounded to whole shares and must satisfy `0 < q < |position|`. If
    less than one share would remain, it becomes a full close.
  - A new `reduce_partial` in `trading_runtime` checks the side and the bound.
    `reduce_position` keeps its exact-size rule.
  - After the reduce, the stop (and target, if one existed) is placed again for
    the **remaining broker quantity**. The price is the original stop price, or
    a new one from the decision, which must be on the protective side of the
    current price.
  - Re-protect orders use deterministic child ids. On recovery the service looks
    for that order ref at the broker before it places anything.
  - If re-protect fails or misses its deadline: no retry with fresh ids. Escalate
    to a full close of that conid and trip the breaker.
- All scopes keep the existing rules: never send another order without a newer
  broker generation, and a missed deadline means `FAILED_SAFE` plus the breaker.

**Users of the safe close.**

- Session time exits switch from raw `reduce` to `scope=conid` full close.
- One-strategy automation SELL signals become a close command without a quantity.
  The size comes from the broker at reduce time. They no longer go through
  `build_bracket_plan`.
- `ai_paper` `CLOSE` and `PARTIAL_CLOSE` decisions (section 5.4).

The state machine already handles both sides (`reduce_position` derives the side
from the broker), so the later short phase reuses it.

### 5.2 Scoreboard

The scoreboard is the only honest referee once the AI judges its own backtests.

**Tables** (written only by trader_service):

- `experiments`: experiment id, start time, start net liquidation, config digest,
  enabled styles, kill line, state (`ARMED`, `PAUSED`, `KILLED`, `STOPPED`).
- `equity_daily`: one row per session after `SessionController` reaches `FLAT`.
  Start and end net liquidation, realized P&L, commissions, peak gross exposure,
  trade count. `broker_account_state` keeps only the current value, so this
  table is the missing history.
- `round_trips`: a projection rebuilt from `broker_fills`. Entry and exit fills,
  cash P&L after fees, and attribution: strategy version, decider, risk-policy
  revision, style. Partial exits belong to the same round trip.
- `simulated_books`: a slot for simulated results, such as the "follow the signal"
  baseline. SP1 creates the table and the display. SP2 fills it. Every row is
  labelled `simulated`.

**Benchmarks.** SPY buy-and-hold from the experiment start (local daily bars),
the simulated baseline book, and AI cost in USD with a "P&L minus AI cost" line.

**Metrics.** Return vs SPY, max drawdown, daily Sharpe (with a small-sample
warning below 60 sessions), cash profit factor, win rate, turnover and fees.
Each can be split by strategy, decider and style.

**Integrity.**

- AI principals get read-only scoreboard methods (section 5.3).
- `mmr scoreboard verify` rebuilds every number from `broker_fills` and
  `equity_daily`. A mismatch is an incident.
- No AI service mounts the trader database.

**Output.**

- `mmr --json scoreboard`.
- A Scoreboard tab on `/cc`.
- A **send-only** Telegram daily summary: one message after each `FLAT`, through
  a durable outbox with stable event ids. It handles no inbound commands (SP2).
  The bot token comes from a secret file. Only the configured chat id receives it.
  A Telegram outage only delays the message.

Every view says "paper". Nothing on it is proof of live edge.

### 5.3 Service identities

**Authentication.**

- `TypedRpcRequest` gets a signed `principal` field, included in the signing bytes.
- The server holds a keyring `{principal: key}`. It selects the key by
  principal, verifies the signature and treats that principal as the caller.
  Responses are signed with the same key.
- Handlers receive the authenticated caller. The hard-coded
  `source="strategy_service"` (`trader/messaging/production_api.py:1124`) is
  replaced by it.

**Authorization.**

- Every `TypedRpcRegistry` registration gets `allowed_principals`. A method
  without an entry rejects everyone.
- The check runs after authentication and before dispatch. It returns
  `PERMISSION_DENIED` and is logged.
- The allow-list is code, reviewed in git, not user config.

| Principal | Rights (summary) |
|---|---|
| `strategy` | `execute_automated_intent` (old path), resolve, publish. |
| `dashboard`, `cli` | Reads, propose/approve/reject, cancel, pause, experiment start/resume. |
| `scheduler` | Data refresh. |
| `ai_supervisor` | Reads, scoreboard read, `publish_ai_risk_policy`, `submit_ai_paper_decision`, pause. |
| `ai_research` | Research jobs and market-data reads. No trading methods. |
| `telegram_bridge` (SP2) | Reads, pause/resume/flatten with confirmation. |
| `ai_sandbox` (SP3) | None. No key. Pipes only. |

**Keys.**

- `~/.config/mmr/keys/rpc/<principal>.key`, mode `0600`, created by
  `mmr keys init`.
- Each container mounts only its own key. The trader mounts all of them.
- Covers trader ports 42101–42103 and strategy ports 42104/42105.
- Hard cutover: after `mmr keys init` and `./docker.sh -b -u`, the old
  `service_hmac.key` is refused. No dual-key mode.

### 5.4 The `ai_paper` path

**Risk limits as data.**

- New `RiskLimits` dataclass: max positions, position fraction, gross fraction,
  per-trade risk fraction, daily-loss fraction, drawdown fraction, max pending
  orders.
- `FIXED_PAPER_LIMITS` holds today's constants from
  `trader/automation/session_risk.py`.
- `SessionRiskController.evaluate` takes `limits`. The existing path passes
  `FIXED_PAPER_LIMITS`, so its results do not change. A parity test proves it.
- `PortfolioRiskBudget` takes the same limits. There is no second evaluator.

**AI risk policy** (`publish_ai_risk_policy`, `ai_supervisor` only).

- Stored append-only in a trader-owned table with revision, reason and time.
- Structural checks only: every value finite and > 0; gross ≤ 1.0 (no leverage);
  position ≤ gross; positions ≥ 1; daily loss and drawdown < 1.0.
- Timing rules:
  - Tightening applies at once.
  - Loosening applies from the next session only.
  - The session daily-loss budget is frozen at the first entry of the session.
  - A breach stays a breach, even if a later policy is looser.
- No accepted policy means no new entries.

**AI deployment record.** This replaces the signed artifact in `ai_paper`. It
holds the strategy file digest, class, params, conids, bar size, style, decider
verdict and the evidence reference. A decision must match its deployment, the
same way the bundle binding works today. SP1 defines the record and the check.
SP2 and SP3 create the records.

**Command `submit_ai_paper_decision`.**

- Fields: `decision_id` (stable), `deployment_version`, `decider`, `action`
  (`ENTER`, `CLOSE`, `PARTIAL_CLOSE`), `conid`, `side`, `stop_price`,
  `target_price`, `quantity` (optional), `policy_revision`, `evidence_digest`,
  `expires_at`.
- Sizing: the trader computes the maximum quantity from the policy and the stop
  distance. A decision may ask for less, never more.
- Admission refuses unless all of these hold:
  - the caller is `ai_supervisor`;
  - the mode is `ai_paper` and the account is paper;
  - `live_enabled` is off and the one-strategy automation is not armed;
  - the experiment is `ARMED`;
  - `policy_revision` is the current accepted revision;
  - the decision has not expired and the deployment matches;
  - the session allows entries (`ENTER` only);
  - the side is in an enabled style (V1: long only);
  - the risk evaluation passes.
- One `decision_id` creates at most one entry command.
- `ENTER` uses the existing protective bracket saga. `CLOSE` and `PARTIAL_CLOSE`
  use section 5.1.
- Every refusal has a distinct reason code, recorded and visible on the scoreboard.

### 5.5 Arming and kill line

- `mmr experiment start` arms only if: the account is paper, it has no positions
  and no working orders, the one-strategy automation is off, and the keyring and
  allow-list are loaded. It records the start net liquidation.
- Kill line: when `experiment_kill_drawdown_pct` is set, every promoted broker
  snapshot is checked against the start net liquidation. On a hit:
  1. a durable `KILLED` state is set;
  2. an account-wide flatten runs through `LiquidationService`;
  3. a Telegram alert goes out through the outbox.
- Only `mmr experiment resume` (principals `cli` or `dashboard`) clears `KILLED`
  or `PAUSED`. AI principals cannot.
- Arm, pause and kill states survive restarts. On start the trader reconciles
  with the broker before it admits any decision.

Configuration (new keys in `trader.yaml`):

```yaml
ai_paper:
  enabled: false
  styles: [intraday_long]            # later: swing_long, intraday_short, swing_short
  experiment_kill_drawdown_pct: null # off by default; e.g. 20
  telegram:
    enabled: false
    chat_id: null
    token_secret_file: ""            # path only
```

## 6. Testing

Test-first for every part. Each change starts with a failing test.

- **Safe close:** a test that reproduces "time exit leaves the stop live";
  state-machine tests with fake broker generations (lost acknowledgement,
  partial fill, cancel rejected, re-protect failure, missed deadline); a
  production-composition test through `command_stack`.
- **Scoreboard:** projection rebuilt from fills matches stored rows; partial
  exits stay in one round trip; `verify` detects an edited row; the outbox sends
  once per event id and retries after an outage.
- **Identities:** wrong key rejected; principal outside the allow-list denied;
  method without an entry denied for all; source derived from the key, never
  from the body; a table test that AI principals cannot call `approve_proposal`,
  `execute_automated_intent`, `place_standalone_order` or set limits.
- **`ai_paper` path:** `FIXED_PAPER_LIMITS` parity with the old constants; policy
  timing rules (mid-session loosening refused, breach persists); idempotent
  decisions; every admission refusal code; sizing never above the policy maximum.
- **Arming and kill line:** arming refused with a leftover position or a working
  order; the kill line fires, flattens and survives a restart; AI principals
  cannot resume.

A green suite is not a paper-session result. Before SP2 starts trading, SP1 must
also pass one real IB paper session with a manual `submit_ai_paper_decision`
entry, a partial close, a full close and the session flatten.

## 7. SP1 delivery order

1. Safe close (fixes a live bug in the existing path).
2. Service identities (needed before any new principal exists).
3. `RiskLimits` as data, AI risk policy store, deployment record,
   `submit_ai_paper_decision`.
4. Experiments, arming and kill line.
5. Scoreboard, `/cc` tab and the Telegram daily summary.
6. Real IB paper session (section 6).

## 8. Roadmap after SP1 (not designed here)

**SP2 — Autonomous loop.**

- Orchestrator model through OpenRouter, run by a deterministic workflow controller
  with bounded calls, tokens, wall time and concurrency. A `null` dollar budget
  does not mean unbounded retries.
- Decider interface: Jev first, "follow the signal" baseline logged on every
  opportunity into `simulated_books`.
- Jev as backtest judge: it sees code-computed stats and the trial count, not the
  orchestrator's text. Every judgment counts as a trial. Rejected families get a
  cooldown. Rejected strategies keep running in shadow, so the scoreboard can
  measure Jev's judgment.
- AI risk policy generation, discovery with honest coverage counts (current
  provider map: Alpaca for US history, ideas, movers and news; IB for snapshots
  unless a quote source is configured; no Yahoo), Telegram command bridge.
- Every trial persisted, failed experiments never deleted, model calls recorded
  for replay.

**SP3 — Sandboxed code generation.**

- AI writes strategy files. A static check allows only listed imports (pandas,
  numpy and similar); no `os`, `socket`, `subprocess` or file access.
- Auto-commit to a dedicated branch so the deployment record binds to a hash.
- Mandatory lookahead check (`assert_no_lookahead`).
- An isolated runner: no keys, no network, bars in and signals out through pipes.
  Signals go through `submit_ai_paper_decision` like catalogue strategies.

**Style phases.** Swing long needs per-position overnight ownership in
`SessionController`, GTC protective stops and gap handling. Short needs borrow
checks, the short-sale rule, and short support in the backtester and admission.

## 9. Open questions

- Telegram bot identity and chat id (owner supplies them outside chat).
- Whether the paper account needs an IB reset before the first `experiment start`.
- OpenRouter and Jev model ids (SP2; verify the installed SDK first).
