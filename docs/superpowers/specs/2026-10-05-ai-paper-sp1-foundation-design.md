# AI Paper Bot — SP1 Foundation — Design Specification

**Date:** 2026-10-05
**Status:** Draft in review (revised four times on 2026-10-05 after review; owner approved the Ed25519 identities and the old-path close correction). Not approved as a whole. Implementation not started. Line numbers refer to this branch before the master rebase.
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
| Risk limits | The AI sets them, under an owner ceiling in `trader.yaml`. The default ceiling is today's paper limits. The ceiling's code maximum is today's steady caps: only gross can rise (6% → 15%); every other field stays at today's value or tighter. The AI can never raise the ceiling (revised 2026-10-05 after review). |
| Kill line | `experiment_kill_drawdown_pct`. Default `null` (off). The owner may set e.g. `20`. |
| Account | The bot owns the paper account alone. It arms only when the account is flat. |
| Decider | Pluggable interface. Jev is the first adapter. The "follow the signal" baseline is logged beside it. |
| Jev as judge | Jev also judges backtest results (SP2): code-computed stats + trial count in, `DEPLOY` / `SHADOW` / `REJECT` out. |
| Styles | Designed for intraday long, swing long, intraday short, swing short. Enabled in phases: intraday long → swing long → short. |
| New code | AI writes strategies and they auto-deploy, in V1, only into a sandboxed runner (SP3). |
| Qualification | The AI (with Jev as judge) decides. No external ruleset gates the `ai_paper` path. |
| Exits | Full and partial closes. |
| Reporting | Scoreboard tab on `/cc` and a daily Telegram summary. |

With the default configuration the brakes are the owner ceiling (today's paper
limits, including the 3% drawdown from the peak), broker buying power, `/pause`
and `/flatten`.

## 3. Accepted risks

The owner chose these against the reviewer's advice. They are recorded so the
results are read correctly.

- **AI-set risk limits (under the owner ceiling).** Results are harder to
  compare across weeks. Every policy is logged with its reason so the scoreboard
  can show what was in force.
- **AI is its own gate.** Backtest numbers produced by the AI prove nothing on
  their own, because it chooses which ones to keep. Only forward paper P&L
  (section 5.2) answers whether the bot works.
- **AI-written code in V1.** More work before the first run. The sandbox must be
  correct from the first day (SP3).
- **No default kill line.** The 3% drawdown limit still blocks new entries, but
  it is checked only when a decision arrives and it does not flatten. Open
  positions keep running until their stops, the session flatten or `/flatten`.
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
- A close cannot simply cancel the stop today. `ProtectiveOrderSaga` treats a
  bare stop cancel as lost protection (`stop_rejected`, then `SAFETY_FAILED` /
  `MISSING_PROTECTION`) and starts an account-wide liquidation
  (`trader/automation/protective_order_saga.py:676-722, 751-759`).
- `LiquidationService._set` trips the breaker on every state except `FLAT`,
  including healthy progress (`trader/trading/liquidation_service.py:155-156`).
- Runs are keyed by cause command id, so two producers can reduce the same
  position. `rescan()` returns after the first non-`FLAT` root, and
  `FAILED_SAFE` counts as one, so an old failed run stops later runs from
  advancing (`liquidation_service.py:21, 138-143, 179`).

**Design.** Extend `LiquidationService` (`trader/trading/liquidation_service.py`)
with a scope instead of adding a second close path.

- `scope=account`: today's behaviour. Used by the session flatten, `/flatten`,
  protective failure and the kill line. Its breaker behaviour does not change.
- `scope=conid`, full close:
  `REQUESTED → CANCELLING → VERIFYING → REDUCING → VERIFYING → CLOSED`.
  It cancels only that conid's working orders, waits for a newer broker
  generation that shows them gone, sends a reduce of the broker quantity, and
  waits for a generation that shows a zero position and no residual exits.
- `scope=conid, quantity=q`, partial close: the same, then
  `REPROTECTING → VERIFYING → DONE`.
  - `q` is rounded to whole shares and must satisfy `0 < q < |position|`. If
    less than one share would remain, it becomes a full close.
  - A new `reduce_partial` in `trading_runtime` checks the side and the bound.
    `reduce_position` keeps its exact-size rule.
  - Re-protection uses the exit-only linked operation below.
  - If re-protect fails or misses its deadline: no retry with fresh ids. Escalate
    to a full close of that conid and trip the breaker.
- All scopes keep the existing rules: never send another order without a newer
  broker generation, and a missed deadline means `FAILED_SAFE` plus the breaker.

**Protection ownership.**

- Before a scoped close cancels anything, it takes over protection from the
  entry saga **durably, in one journal transaction**: the saga row moves to a new
  state `CLOSE_OWNED` with the close root id and the order refs the close will
  cancel.
- A cancel event for one of those refs, on a `CLOSE_OWNED` saga, is expected. It
  does not set `stop_rejected` and does not start a liquidation.
- Any other loss of protection still is an incident, exactly as today: a cancel
  or reject that the close did not request, a cancel on a saga that was not
  handed over, or a leg vanishing outside a close.
- After a partial close reaches `DONE`, ownership goes back to the saga for the
  remaining quantity, with the new exit refs. The saga watches them the same way
  as the original legs.
- After a full close reaches `CLOSED`, the saga row is closed.

**Breaker signals.** Routine progress of a scoped close (`REQUESTED`,
`CANCELLING`, `REDUCING`, `VERIFYING`, `REPROTECTING`) never trips the breaker.
Only `FAILED_SAFE`, a missed deadline, a re-protect failure and unexpected
protection loss do.

**One execution owner per position.**

- A durable `exit_owner` table holds at most one owner per `(account, conid)` and
  at most one account-wide owner per account: owner kind (`scoped_close`,
  `account_flatten`), root command id, and state.
- Every exit producer claims there first: time exits, one-strategy SELL, `ai_paper`
  `CLOSE` / `PARTIAL_CLOSE`, protective failure, session flatten, `/flatten`, kill.
- A scoped request for a conid that already has an owner does not create work. It
  returns the existing root id (`JOINED`). A partial request against an existing
  owner is refused (`EXIT_IN_PROGRESS`).
- An account flatten takes over every scoped owner on that account:
  1. it marks them `SUPERSEDED` in the same transaction as its claim;
  2. superseded closes stop at once and never re-protect;
  3. it waits for a broker generation that shows every child they submitted
     (found by their deterministic order refs) as filled, cancelled or absent;
  4. only then does it cancel and reduce.
- While an account owner is active, every scoped request joins it.
- Callers poll the **exact root id** they got back. A receipt of another root is
  never accepted as their result.
- `rescan()` advances every root that is not terminal. `FLAT`, `CLOSED`, `DONE`,
  `SUPERSEDED` and `FAILED_SAFE` are terminal for rescan. `FAILED_SAFE` stays
  latched for the breaker but no longer blocks other roots.

**Exit-only linked stop and target.**

- New `place_exit_oca` in `trading_runtime`. It places a stop and an optional
  target for an existing position, both transmitted, in one OCA group. It never
  creates an entry parent (unlike the bracket API) and never uses
  `place_standalone_order` (which does not link orders).
- Quantity is the remaining broker quantity. Side is derived from the broker
  position. The stop price is the original stop or a new one from the decision,
  which must be on the protective side of the current price.
- Deterministic refs: `{root}-reprotect-stop`, `{root}-reprotect-target`, OCA
  group `{root}-reprotect`.
- A partial fill of one sibling must reduce the other to the remaining position.
  The plan picks the IB OCA type and proves it with the fake broker and in the
  real paper session.
- Recovery, before placing anything, reads the broker by those refs:
  - position zero: cancel any residual sibling; the close ends `CLOSED` (closed
    by an exit), not `DONE`;
  - one sibling present, the other missing: place only the missing one, in the
    same group, for the remaining quantity;
  - both present and working: no order; go to `VERIFYING`.
- `DONE` means a broker generation shows the stop (and target, if any) working in
  one group for the remaining quantity. `CLOSED` means zero position and no
  residual exits. Nothing else counts.

**Users of the safe close.**

- Session time exits switch from raw `reduce` to `scope=conid` full close.
- One-strategy automation SELL signals become a close command without a quantity.
  The size comes from the broker at reduce time. They no longer go through
  `build_bracket_plan`.
- Time exits and one-strategy SELL closes use the reduction admission rules of
  section 5.4, not `session_risk`. So a close still works after a daily-loss or
  drawdown breach. This removes today's SELL-after-breach refusal on the old
  path on purpose, as a safety correction: blocking an exit does not reduce risk.
- "SELL" alone never qualifies for that exemption. Before it treats a request as
  a reduction, the trader checks, on a fresh fenced broker snapshot:
  - there is a current position on that conid;
  - the side reduces it (SELL for a long);
  - the quantity is at most the position (a close takes the broker quantity);
  - the exit owner check passes, and competing working orders on that conid are
    handed over or reconciled first, so the close cannot reverse exposure.
  Anything else is an entry and goes through entry admission. Protection
  hand-over and close/flatten arbitration apply as above.
- `ai_paper` `CLOSE` and `PARTIAL_CLOSE` decisions (section 5.4).

The state machine already handles both sides (`reduce_position` derives the side
from the broker), so the later short phase reuses it.

### 5.2 Scoreboard

The scoreboard is the only honest referee once the AI judges its own backtests.

**Tables** (written only by trader_service):

- `experiments`: experiment id, start time, start net liquidation, config digest,
  enabled styles, kill line, state (`ARMED`, `PAUSED`, `KILLED`, `STOPPED`).
- `equity_daily`: one row per session, written when the session ends in any
  way: `FLAT`, `KILLED` or `FAILED_SAFE`. A `session_end_state` column records
  which. Start and end net liquidation, realized P&L, commissions, peak gross
  exposure, trade count, open positions at the end (zero after `FLAT`).
  `broker_account_state` keeps only the current value, so this table is the
  missing history. A killed or failed day is the row the scoreboard most needs.
- `round_trips`: a projection rebuilt from `broker_fills`. Entry and exit fills,
  cash P&L after fees, and attribution: strategy version, decider, risk-policy
  revision, style. Partial exits belong to the same round trip.
- `simulated_books`: a slot for simulated results, such as the "follow the signal"
  baseline. SP1 creates the table and the display. SP2 fills it. Every row is
  labelled `simulated`.
- `benchmark_prices`: the exact SPY daily closes the scoreboard used, with
  provider, bar date, fetch time and a benchmark version. Later data refreshes
  never change a stored row; a correction is a new version.
- `ai_costs`: one row per model call (SP2 fills it; SP1 creates it): call id,
  provider, model, tokens, cost in USD, time, and the decision or job it served.
- `equity_adjustments`: a commission or fill that arrives after its session's
  `equity_daily` row was written. The old row is never edited.
- Attribution comes from stored links, not from text: broker fill → order ref →
  command → `ai_paper` decision → deployment record and policy revision.

**Currency.**

- The reporting currency is USD. AI costs and US commissions are USD.
- Net liquidation is stored in the account base currency **and** in USD. Each
  `equity_daily` row keeps the FX rate used, its source (IB account values) and
  its time. If the base currency is USD, the rate is 1 and says so.
- A row with no FX evidence for a non-USD base is not written as USD. It is an
  incident.

**Benchmarks.** SPY buy-and-hold from the experiment start (from
`benchmark_prices`), the simulated baseline book, and AI cost in USD with a
"P&L minus AI cost" line.

**Metrics.** Return vs SPY, max drawdown, daily Sharpe (with a small-sample
warning below 60 sessions), cash profit factor, win rate, turnover and fees.
Each can be split by strategy, decider and style.

**Integrity.**

- AI principals get read-only scoreboard methods (section 5.3).
- `mmr scoreboard verify` rebuilds every number from its stored inputs:
  `broker_fills`, `equity_daily` (with FX), `equity_adjustments`,
  `benchmark_prices`, `ai_costs`, decisions, deployment records and policy
  revisions. A mismatch is an incident.
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

**Authentication.** Asymmetric signatures (Ed25519, already used in
`trader/research/signing.py`), not shared HMAC keys.

- Each principal has one private key. Only its own container mounts it.
- Every server mounts the **public** keys of the principals it accepts. A public
  key cannot sign, so mounting it gives no power.
- `TypedRpcRequest` gets a `principal` field, included in the signed bytes. The
  server selects the public key by principal, verifies the signature and the
  existing timestamp and nonce rules, and treats that principal as the caller.
- Each server also has its own private key (`trader`, `strategy`) and signs its
  responses with it. Clients verify responses with the server's public key.
- This works for both servers. The strategy service verifies `cli` and
  `dashboard` with their public keys, and the trader with the `trader` public key.
- RPC keys are separate from bundle-signing keys. Only the cryptographic code is
  reused, never a key. A bundle key is refused as an RPC key and the other way round.
- A principal's public key comes only from the server's trusted keyring on disk.
  A request can name a principal but can never supply or point to a key.
- Kept and extended from today:
  - method ACLs (below);
  - replay protection (timestamp window and nonce);
  - the signed bytes cover the principal, the **destination** (server principal
    and method) and the payload, so a request for one server or method cannot be
    replayed against another;
  - each response is signed with the server's own private key and names the
    request id and a digest of the request, so a client accepts only the
    response to its own request.

**Trust matrix** (caller → server). Anything not listed is refused.

| Caller | Trader 42101–42103 | Strategy 42104/42105 |
|---|---|---|
| `cli`, `dashboard` | yes | yes |
| `strategy` | yes (intent, resolve, publish, feed) | — |
| `trader` | — | yes (hot-arm and reload, which the trader already sends: `trader/automation/paper_hot_arm.py:170`, `trader/trading/command_stack.py:175`) |
| `scheduler` | yes | — |
| `ai_supervisor`, `ai_research` | yes | no |
| `telegram_bridge` (SP2) | yes (reads, pause/resume/flatten) | no |

**Forwarding.** When the trader calls the strategy service for a user command,
it signs as `trader`. The strategy ACL authorizes `trader`, not the original
user. The original principal is sent as `on_behalf_of` for the log only. It never
grants rights.
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
| `trader` | Strategy-service methods it forwards today (hot-arm, reload). |
| `strategy` | `execute_automated_intent` (old path), resolve, publish. |
| `dashboard`, `cli` | Reads, propose/approve/reject, cancel, pause, experiment start/resume/stop. |
| `scheduler` | Data refresh. |
| `ai_supervisor` | Reads, scoreboard read, `publish_ai_risk_policy`, `submit_ai_paper_decision`, pause. |
| `ai_research` | Research jobs, market-data reads, `register_ai_deployment`. No trading methods. |
| `telegram_bridge` (SP2) | Reads, pause/resume/flatten with confirmation. |
| `ai_sandbox` (SP3) | None. No key. Pipes only. |

**Keys.**

- Private: `~/.config/mmr/keys/rpc/<principal>.key`, mode `0600`. Public:
  `~/.config/mmr/keys/rpc/<principal>.pub`. Both created by `mmr keys init`.
- Each container mounts its own private key, the public keys of the callers it
  accepts as a server, and the public keys of the servers it calls (to verify
  their responses). No container mounts another principal's private key, the
  trader included.
- Covers trader ports 42101–42103 and strategy ports 42104/42105.
- Before the cutover, a split-service test runs real round trips over every
  edge of the trust matrix: CLI → trader, CLI → strategy, dashboard → both,
  strategy → trader, trader → strategy, and `ai_supervisor` → trader. It also
  sends a wrong-principal, a tampered, a replayed, a wrong-destination and a
  legacy-HMAC request on each server, and all must be refused.
- Hard cutover: after `mmr keys init` and `./docker.sh -b -u`, the old
  `service_hmac.key` is refused. No dual-key mode.

### 5.4 The `ai_paper` path

**Risk limits as data.**

- New `RiskLimits` dataclass: max positions, position fraction, gross fraction,
  per-trade risk fraction, daily-loss fraction, drawdown fraction, max pending
  entry orders.
- `PAPER_LIMITS` holds the limits that **actually run** on the paper path today.
  It is not a copy of the module constants:
  - gross 6% (the paper clamp in `production_evidence.allocation_factory`,
    `trader/automation/production_evidence.py:173`), **not** the 15% steady cap
    in `MAX_GROSS_FRACTION`;
  - position 5%, per-trade risk 0.20%, daily loss 0.50%, 3 positions, drawdown
    3% from the high-water mark;
  - 3 pending entry orders (new field, see below).
- **Pending entry orders.** Today `session_risk` counts only filled positions
  (`trader/automation/session_risk.py:279`), so unfilled entries on new conids
  are not bounded on this path. The `ai_paper` approval factory enforces it on
  `ENTER`: working non-protective orders in the fenced broker snapshot, plus
  this entry, must not exceed `max_pending_entry_orders`, and positions plus
  conids with a working entry must not exceed `max_positions`
  (`MAX_PENDING_ENTRIES`). It lives in the `ai_paper` factory, so the old path
  does not change.
- `SessionRiskController.evaluate` takes `limits`. `PortfolioRiskBudget` takes
  the same object, so its own copies of 0.50% and 3 positions
  (`trader/promotion/portfolio_risk_budget.py:21-22`) go away. There is no
  second evaluator.
- The old path passes `PAPER_LIMITS`. **Old-path entry admission and risk
  ceilings remain unchanged. Time exits and one-strategy SELL exits use the
  shared safe-close path; entry-only loss limits do not block broker-proven
  reductions** (a safety correction, see section 5.1). The parity test calls
  `evaluate` (not a field comparison) on the old path and checks that gross
  still stops at 6%.
- **Attested notional.** Master added `ORDER_EXCEEDS_ATTESTED_NOTIONAL` to
  `session_risk` (commit `27c9ab96`). This branch is rebased on master before
  the plan is written. The old path keeps the check, and the parity test covers
  it. In `ai_paper` the deployment record carries `evidence_order_notional` (the
  notional its evidence priced, required). An entry above it, beyond the same
  `LIVE_NOTIONAL_TOLERANCE`, is refused with the same code.

**Effective limits at dispatch.**

- The current effective limits are checked again at the final dispatch check
  (`AllocationPolicy` revalidation and `DispatchGuard`), not only at admission.
- Today the dispatch re-check replaces a tighter current ceiling with the larger
  approved one (`trader/promotion/allocation_policy.py:321-325`). The rule must
  be: dispatch uses the tighter of the approved and the current ceiling. An
  order above it is refused (`LIMIT_TIGHTENED_BEFORE_DISPATCH`).
- This is a safety bug on the old path too. A failing test proves it first;
  then it is fixed on both paths. This is the one deliberate exception to the
  old-path risk-limit parity, and it can only make the old path stricter.
- The 15% `STEADY_MAX_GROSS_FRACTION` cap stays in `AllocationPolicy`. It is the
  same constant as the ceiling's code maximum, not a second value.

**Owner ceiling.**

- `ai_paper.limits_ceiling` in `trader.yaml`. A missing key defaults to the
  `PAPER_LIMITS` value.
- **Code maximum** (`STEADY_LIMITS`, from the existing hard ceilings in
  `trader/automation/session_risk.py` and `trader/promotion/allocation_policy.py`,
  not new literals):

  | Field | `PAPER_LIMITS` (default) | Code maximum |
  |---|---|---|
  | gross | 6% | 15% (`STEADY_MAX_GROSS_FRACTION`, top of the promotion ladder) |
  | position | 5% | 5% |
  | per-trade risk | 0.20% | 0.20% |
  | daily loss | 0.50% | 0.50% |
  | positions | 3 | 3 |
  | drawdown | 3% | 3% |
  | pending entry orders | 3 | 3 |

  So config can move only gross up. Any field may be set tighter. Going past
  the steady caps is a code change, made only after this experiment has a
  scoreboard.
- A value of the wrong type, not finite, ≤ 0, or above the code maximum
  **fails config load**. It never falls through to the AI policy.
- Only the owner edits it. No AI principal has a method that writes it, and no
  AI container mounts `trader.yaml`.
- A raised ceiling applies from the next session, never mid-session.
- **Drawdown guard.** If the drawdown ceiling is above `PAPER_LIMITS.drawdown`,
  the kill line must be set and must be no looser than the drawdown ceiling.
  Otherwise config load fails. With today's code maximum this cannot fire from
  YAML. It stays so that a later code change cannot reopen the hole. Legal
  today: ceiling 3% with the kill line off, or ceiling 3% with a kill line of 20%.

**AI risk policy** (`publish_ai_risk_policy`, `ai_supervisor` only).

- Stored append-only in a trader-owned table with revision, reason and time.
- Structural checks: every value finite and > 0; gross ≤ 1.0 (no leverage);
  position ≤ gross; positions ≥ 1; daily loss and drawdown < 1.0.
- A policy with any value above the owner ceiling is **refused**
  (`POLICY_ABOVE_CEILING`), not clamped. The AI sees exactly what is in force.
- Every accepted policy is a **published** revision (append-only).

**Policy timing.**

- The **effective** limits are what admission and dispatch use. They are
  computed per field and stored as their own revision, with the published
  revision they came from.
- A newly published revision is split per field:
  - a field tighter than the current effective value applies at once;
  - a field looser than it is **queued** until the next session start;
  - a mixed revision does both: its tighter fields now, its looser fields at the
    next session.
- At each session start the effective limits become the latest published
  revision, capped by the owner ceiling. A restart is not a session start.
- **Daily loss.** At session start the equity anchor (start net liquidation) is
  frozen. The budget is `anchor × effective daily-loss fraction`. A tighter
  fraction lowers it mid-session; a looser one waits for the next session. The
  old path keeps today's formula; the anchor is passed only by `ai_paper`.
- **Breach latch.** A daily-loss or drawdown breach sets a durable latch for the
  rest of the session. No later revision clears it. It survives a restart.
- No accepted policy means no new entries.

**AI deployment record.** This stands in for the binding fields of the signed
artifact in `ai_paper`: strategy file digest, class, params, conids, bar size.
It also holds style, decider verdict and the evidence reference.

- It is created by `register_ai_deployment` (`ai_research`), never by
  `ai_supervisor`. SP1 builds the method, the store and the seal. SP2 and SP3
  call it.
- It is sealed (content digest, immutable) before any decision may reference it.
  A decision names it by digest and must match it, the same way bundle binding
  works today.
- It is an experiment record, not `paper-v1` eligibility. It can never arm the
  old path.
- In SP1 the acceptance harness (section 6) registers a catalogue strategy
  through the same method. The database is never seeded directly.

**Command `submit_ai_paper_decision`.**

- Fields: `decision_id` (stable), `deployment_digest`, `decider`, `action`
  (`ENTER`, `CLOSE`, `PARTIAL_CLOSE`), `conid`, `side`, `stop_price`,
  `target_price`, `quantity` (optional), `policy_revision`, `evidence_digest`,
  `expires_at`.
- **Entry admission** (`ENTER`). The handler checks only what is new:
  - the caller is `ai_supervisor` (its own key, section 5.3);
  - the mode is `ai_paper`, the experiment is `ARMED`;
  - `policy_revision` is the latest published revision;
  - the deployment is sealed and matches; the decision has not expired;
  - the side is in an enabled style (V1: long only).
- **Reduction admission** (`CLOSE`, `PARTIAL_CLOSE`). Separate rules, because a
  close must work when entries are blocked:
  - kept: the caller is `ai_supervisor`; the account fence; a broker-proven
    position on that conid and side; the quantity bound; the exit owner check
    (section 5.1); expiry;
  - allowed while the experiment is `ARMED` or `PAUSED`;
  - **not** required: the entry window, the entry budget, daily-loss and
    drawdown checks, the current policy revision, or a matching deployment;
  - `session_risk` is not run for reductions (today it rejects a SELL after a
    daily-loss or drawdown breach, `trader/automation/session_risk.py:226-244`);
  - while `KILLED`, a reduction does not create work. It joins the kill flatten
    and returns that root id;
  - `STOPPED` refuses everything.
- An `ENTER` is then **translated into a command for the existing
  coordinator**. It does not keep its own admission list. The coordinator runs
  every check that already refuses an order: account fence, trading filter
  allowlist, long-only, liquidity, quote freshness, margin, calendar entry
  window, stop validity, and `session_risk` with the effective limits.
- A new `ai_paper` approval factory feeds the coordinator. It captures the
  same evidence as today's `approval_factory`: the fenced broker snapshot
  (account, paper mode, complete fence), a live quote, margin, the high-water
  mark and liquidity. Skipping `QUANTITY_REQUIRED` is the only rule it drops.
- Sizing (`ENTER`): the maximum quantity is the **minimum** allowed by every
  effective limit: per-trade risk over the stop distance, position fraction,
  remaining gross, margin and liquidity. Whole shares, rounded down.
  - No `quantity`: the trader uses the maximum.
  - A `quantity` at or below the maximum is used as is.
  - A `quantity` above the maximum is **refused** (`QUANTITY_ABOVE_MAXIMUM`),
    not cut down.
  - A maximum below one share refuses the entry.
- `ENTER` uses the existing protective bracket saga. `CLOSE` and `PARTIAL_CLOSE`
  use the scoped close (section 5.1), never `build_bracket_plan`. A close takes
  its size from the broker.
- Retries:
  - One `decision_id` creates at most one command.
  - While a command or a close for a conid is `OUTCOME_UNKNOWN` (broker result
    not known) or not finished, every new decision on that conid is refused
    (`OUTCOME_UNKNOWN_PENDING`), whatever its id. Only reconciliation clears it.
- Every refusal has a distinct reason code, recorded and visible on the scoreboard.

### 5.5 Arming and kill line

**Arming.** `mmr experiment start` arms only if:

- `ai_paper.enabled` is `true`;
- the account is paper;
- it has no positions and no working orders, because the kill flatten is
  account-wide and must not close anything the bot does not own;
- the one-strategy automation is not armed;
- the `ai_supervisor` key is in the keyring and the allow-list is loaded (the old
  shared `service_hmac.key` does not count).

It records the start net liquidation.

**Lock in both directions.** Arming the one-strategy automation (Activate or
hot-arm) is refused while an experiment is `ARMED`, `PAUSED` or `KILLED`.

**Kill line.** When `experiment_kill_drawdown_pct` is set, every promoted broker
snapshot is checked against `experiment_kill_basis`: `start` (start net
liquidation, default) or `peak` (highest net liquidation since start). The
ceiling's drawdown limit keeps working beside it; whichever is tighter acts
first. On a hit:

1. A durable `KILLED` state is set **before** any order. Admission refuses from
   that moment.
2. `SessionController` starts an account-wide flatten through the existing
   `LiquidationService`.
3. A Telegram alert "kill started" goes out through the outbox.
4. "Flat" is reported only after a broker generation shows no positions and no
   working orders. A missed deadline is `FAILED_SAFE` plus the breaker, as today.

**Pause, resume, stop and restarts.**

- Only `mmr experiment resume` (principals `cli` or `dashboard`) clears `KILLED`
  or `PAUSED`. It arms the AI again. AI principals cannot call it.
- `mmr experiment stop` (principals `cli` or `dashboard` only) moves an `ARMED`,
  `PAUSED` or `KILLED` experiment to `STOPPED` without resuming it.
  - It refuses (`NOT_FLAT`) while a liquidation is running or the broker shows a
    position or a working order. Flatten first (`/flatten`), so no AI position
    is left for another mode.
  - `STOPPED` is final. It releases the one-strategy lock. A new run needs a new
    `experiment start` and a new experiment id.
- Arm, pause and kill states survive restarts.
- A restart is not a new session. It does not apply a queued loosening (of the
  policy or the ceiling) and does not clear `KILLED`.
- On start the trader reconciles with the broker before it admits any decision.

Configuration (new keys in `trader.yaml`):

```yaml
ai_paper:
  enabled: false
  styles: [intraday_long]            # later: swing_long, intraday_short, swing_short
  limits_ceiling: {}                 # empty = today's paper limits; owner-only
  experiment_kill_drawdown_pct: null # off by default; e.g. 20
  experiment_kill_basis: start       # start | peak
  telegram:
    enabled: false
    chat_id: null
    token_secret_file: ""            # path only
```

## 6. Testing

Test-first for every part. Each change starts with a failing test.

- **Safe close:**
  - a test that reproduces "time exit leaves the stop live";
  - state-machine tests with fake broker generations (lost acknowledgement,
    partial fill, cancel rejected, re-protect failure, missed deadline);
  - partially close one of two protected positions: the other position and its
    orders stay untouched and the breaker stays clear;
  - a stop cancel the close did not request still starts the emergency path;
  - routine scoped-close progress does not trip the breaker;
  - a time exit and an AI close on the same conid: one root, the second joins;
  - an account flatten during a partial close: the close is superseded, does
    not re-protect, its children are reconciled before the flatten orders;
  - an old `FAILED_SAFE` root does not stop `rescan()` from advancing a newer one;
  - a caller polling its root never accepts another root's receipt;
  - exit OCA: one sibling fills before the other is acknowledged; recovery
    places only the missing sibling; position zero cancels the residual exit;
    `DONE` only with both siblings working for the remaining quantity;
  - a production-composition test through `command_stack`.
- **Scoreboard:** projection rebuilt from fills matches stored rows; partial
  exits stay in one round trip; `verify` detects an edited row in any input
  table; a late commission becomes an adjustment, not an edit; a non-USD base
  without FX evidence is an incident; a data refresh does not change a stored
  benchmark price; the outbox sends once per event id and retries after an
  outage.
- **Identities:** split-service round trips over every trust-matrix edge,
  including CLI → strategy and trader → strategy; a forwarded call is authorized
  as `trader`, never as `on_behalf_of`; no container image mounts another
  principal's private key; wrong key rejected; principal outside the allow-list denied;
  method without an entry denied for all; source derived from the key, never
  from the body; a table test that AI principals cannot call `approve_proposal`,
  `execute_automated_intent`, `place_standalone_order` or set limits.
- **`ai_paper` path:**
  - parity: the old path, run through `evaluate` and `PortfolioRiskBudget`, gives
    the same decisions with `PAPER_LIMITS`, and paper gross still stops at 6%;
  - an AI policy above the owner ceiling is refused (`POLICY_ABOVE_CEILING`);
  - config load: gross up to 15% accepted; any other field above today's value,
    or a non-finite, zero or negative value, fails load; a missing key takes
    `PAPER_LIMITS`;
  - drawdown guard (with a test-only raised code maximum): drawdown ceiling above
    `PAPER_LIMITS.drawdown` fails load with the kill line off or looser than the
    ceiling, and loads with a tighter kill line;
  - policy timing: a looser field is queued (not applied, not refused) until
    the next session; a mixed revision applies its tighter fields at once and
    its looser fields at the next session; a tighter daily-loss fraction lowers
    the budget mid-session on the frozen anchor; the breach latch survives a
    looser revision and a restart; a restart does not apply a queued loosening;
  - approval → policy tightening → dispatch: the stale approval cannot execute
    above the new limit (`LIMIT_TIGHTENED_BEFORE_DISPATCH`), on both paths;
  - attested notional: the old path still refuses `ORDER_EXCEEDS_ATTESTED_NOTIONAL`;
    `ai_paper` refuses an entry above `evidence_order_notional`;
  - old-path regression: after a loss breach, a new entry is refused, a safe
    close of the held position is allowed, and an oversized close or a close
    conflicting with another owner is refused; a SELL with no position, or
    larger than the position, is treated as an entry and refused;
  - reductions: `CLOSE` works while `PAUSED` and after a daily-loss breach;
    is refused without a broker position; joins the flatten while `KILLED`;
    is refused after `STOPPED`;
  - idempotent decisions; a **new** `decision_id` cannot retry a conid whose
    command or close is `OUTCOME_UNKNOWN`;
  - coordinator checks reached from a decision (account fence, allowlist,
    quote freshness, margin, calendar, stop validity) each refuse with their
    own code; the factory captures snapshot, quote, margin, high-water mark
    and liquidity; the maximum is the minimum over every limit; a quantity
    above it is refused, not cut; pending entries beyond the limit are refused;
    a close never goes through `build_bracket_plan`;
  - a decision on an unsealed or mismatched deployment is refused;
    `ai_supervisor` cannot call `register_ai_deployment`.
- **Arming and kill line:** arming refused with `ai_paper.enabled: false`, with a
  leftover position or a working order, or with only the old shared key;
  `experiment stop` refused while not flat, moves `KILLED` to `STOPPED` without
  re-arming, releases the lock, and cannot be called by AI principals;
  `equity_daily` gets a row after `KILLED` and after `FAILED_SAFE`; arming the one-strategy automation
  refused while an experiment is armed; `KILLED` is stored before the first
  flatten order; "flat" is not reported before the broker confirms it; the kill
  line survives a restart; both kill bases; AI principals cannot resume.

A green suite is not a paper-session result. Before SP2 starts trading, SP1 must
also pass one real IB paper session.

**Acceptance harness** (`mmr experiment acceptance`, run by the operator):

- It registers a catalogue strategy with `register_ai_deployment`, signed with
  the `ai_research` key, and the seal is applied as in production.
- It publishes a policy and submits decisions with the `ai_supervisor` key:
  one entry, a partial close, a full close; then it waits for the session
  flatten.
- It uses the real methods and the real keys. It never seeds the database and
  never skips the seal.
- It runs on the host, as the operator, and reads the `ai_research` and
  `ai_supervisor` private keys from `~/.config/mmr/keys/rpc/`, where
  `mmr keys init` wrote them. No container gets those keys for it.
- It refuses to run unless the account is paper and an experiment is `ARMED`.
- It is not an autonomous loop. Orchestration stays in SP2.

## 7. SP1 delivery order

0. Rebase on master (attested-notional check, commit `27c9ab96`).
1. Safe close: protection ownership, exit owner, exit-only OCA (fixes a live bug
   in the existing path).
2. Service identities (needed before any new principal exists).
3. `RiskLimits` as data, dispatch re-check fix, AI risk policy store and timing,
   deployment registration and seal, `submit_ai_paper_decision`.
4. Experiments, arming and kill line.
5. Scoreboard, `/cc` tab and the Telegram daily summary.
6. Acceptance harness and the real IB paper session (section 6).

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
