# AI Paper Bot — SP2a + SP2b: Model Plumbing and the Decision Loop — Design

Status: design approved section by section by the owner on 2026-10-07; this file
is the written spec for owner review. Ticket: #36. Builds on SP1
(`docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`).

## 1. Goal

A hands-off paper bot. Models find and judge opportunities and manage the bot's
own positions; SP1's trader stays the only execution authority. The forward
paper scoreboard is the referee, so every model decision is recorded next to
deterministic baselines that show whether the model adds value.

SP2 is split into slices, each with its own spec, plan and code:

| Slice | Scope |
|---|---|
| **SP2a** (this spec) | Provider-neutral model client, model-attempt journal, budget, deterministic controller, replay, cost reporting |
| **SP2b** (this spec) | Decision loop: strategy signals and self-found ideas → Jev → SP1; model-driven closes; baselines |
| SP2c | Jev as backtest judge (DEPLOY / SHADOW / REJECT), trials, cooldowns, shadow runs |
| SP2d | AI risk-policy generation; wider discovery |
| SP2e | Telegram command bridge |

## 2. Owner decisions (do not re-argue)

- Opportunities come from **both** strategy signals and the model's own ideas.
- Baselines: **follow the signal** (strategy signals), a **fixed rule on the same
  candidates** and **no-trade** (self-found ideas), and a **matched-entry,
  bracket-only exit** (model closes).
- Model budget: **$2,000 per day**, owner-adjustable (section 5.4).
- The loop runs in a separate **`ai` container**.
- Discovery every **15 minutes** (configurable) inside the XNYS entry window;
  strategy signals are judged on arrival.
- The model may **close or partially close** the experiment's own positions
  (option B). Stop/target modification (option C) is deferred.
- **Jev runs on OpenRouter only.** The orchestrator uses one of OpenRouter, AWS
  Bedrock or Azure, by config.
- Jev judges **every** ENTER, including the orchestrator's own ideas.
  CLOSE / PARTIAL_CLOSE do not need Jev.
- Controller: one async service with a durable journal (approach A). No cron
  runs, no external workflow engine.
- Starting state (option A, 2026-10-07): the **operator** publishes the initial
  risk policy and registers a `discretionary` deployment over a universe
  (default `sp500`); self-found ideas trade only inside it.

## 3. Roles and authority

| Role | Runs on | May | May not |
|---|---|---|---|
| **Jev** (judge) | OpenRouter only | Rule TAKE / SKIP / REDUCE on every ENTER. REDUCE names an explicit, smaller permitted quantity. Judge backtests DEPLOY / SHADOW / REJECT (interface here, workflow in SP2c). | Enlarge a trade, loosen a limit, choose order type, act on closes |
| **Orchestrator** | configured backend + model | Research; propose ENTERs; request CLOSE / PARTIAL_CLOSE for experiment-owned positions | Place orders, edit protection, change the owner ceiling or budget, own scheduling or retries |
| **Controller** | deterministic code | Schedule, budget, journal, submit, reconcile, report | Make trading judgments |
| **Trader (SP1)** | not a model | Admission, sizing bounds, ownership, safe close, protection, session flatten, kill handling | — (neither model can override it) |

## 4. Components (the `ai` container)

The container holds **model-provider credentials only**; market-data services
keep their own credentials. It talks to the trader only, over Plan 2 typed RPC.

| Component | Responsibility |
|---|---|
| `RpcClients` | Two method-restricted clients: `ai_supervisor` (decisions, closes, policy publication, cost and simulation ingestion, signal cursor reads) and `ai_research` (deployment registration and readback only). **Accepted limit:** two keys in one process are not process isolation. |
| `ModelClient` | One interface, adapters for `openrouter`, `bedrock`, `azure`. Backend and model id per role in config. Jev pinned to `openrouter`. Missing, unsupported or incompatible role/backend/model config fails loudly; no fallback. |
| `AttemptJournal` | Records each model request **before** the call and the response or failure **after**. Never records credentials. An unknown outcome stays unknown. |
| `Budget` | Durable admission gate (section 5.4). |
| `Controller` | Session-aligned slots, signal intake, position management, reconciliation (section 5). Single holder, fenced by a trader-granted epoch. |
| `SignalIntake` | Cursor over the trader's durable signal record (amendment 6.1). |
| `Discovery` | Alpaca movers and most-actives, their news, optional owner watchlist. US stocks, intraday long only. |
| `Orchestrator`, `Jev` | Prompt builders and strict output parsers per role. |
| `Baselines` | Deterministic code that produces the comparison decisions (section 7). |
| `Submitter` | Persists the exact command before sending; reconciles unknown outcomes (section 5.3). |
| `ReportingOutbox` | Idempotent delivery of costs, attempts and baseline records to the trader. |
| `ai.duckdb` | On the named volume `mmr_ai_data` (survives container recreation). Serialized writes. Blocking database and model-SDK work runs off the event loop. |

## 5. Controller protocol

### 5.1 Leadership

- The **trader grants** a monotonically increasing controller epoch with a lease
  (amendment 6.2). The `ai` service persists the epoch it holds.
- The current epoch travels in the **authenticated transport envelope**, outside
  the immutable command body, and is **part of the signed bytes**
  (`rpc_signing_bytes`). It is **required** on new-command admission
  (`submit_ai_paper_decision`): a missing epoch is refused like a stale one. The
  trader checks the granted epoch in the same transaction as the command claim.
  Reads and reconciles carry the successor's signed epoch; they may not omit it.
- A successor reconciles commands created under an older epoch by their
  original id and body under its own epoch. Commands the trader already accepted
  continue independently of the old controller; a takeover never cancels or
  duplicates them.

### 5.2 Scheduling

- **Entry cycles:** fixed slots aligned to the XNYS session (default 15 min),
  only inside SP1's entry window (after the opening stabilization, ending 30 min
  before the close, early closes included). Only discovery and ENTER submission
  are gated by this window.
- **Position-management cycles:** run while the experiment is ARMED or PAUSED
  and holds owned positions, **including after the entry cutoff**. While KILLED,
  closes join the existing flatten. STOPPED admits no new decisions.
- **Reconciliation** runs independently of both cycles and of the budget.
- Missed slots are not replayed as new opportunities. Work is bounded per slot.
  A slow model call never blocks receipts, reconciliation or recovery.
- Strategy signals are processed on arrival (section 5.5).

### 5.3 Identity and submission

- Every action has a persisted identity. The decision id is derived from the
  cycle id (or the signal's source-event id) **plus** that action identity, so a
  cycle can produce several decisions and a redelivered signal is not a new
  opportunity.
- Before sending, the `Submitter` persists the **exact business body** and id
  (including expiry, evidence and policy revision).
- **Proven pre-send failure:** the command stays unsent until it expires.
- **Timeout or disconnect after a possible send:** outcome unknown; reconcile
  the same command. A permitted retry resends the unchanged body and id; it is
  never regenerated under a new id.
- Guarantee: retries cannot create a second **logical** command. This rests on
  trader-side deduplication, not on the AI journal alone. Transport requests may
  repeat.
- Before submission the controller re-checks decision expiry and leadership. The
  trader independently refreshes and revalidates execution evidence, ownership
  and the effective risk limits at admission and at dispatch.

### 5.4 Budget

- Owner setting `ai_paper.model_budget_usd_per_day` (default 2000). Only an
  operator `cli` command or `trader.yaml` changes it; no AI principal can.
  The **effective cap is persisted**. Lowering applies at once. Raising applies
  only at the next 00:00 America/New_York window; a restart never applies it
  early.
- Before each call, reserve the **worst-case** cost, atomically across Jev, the
  orchestrator and any research calls. Reservations are persisted and survive
  restarts.
- The daily window resets at 00:00 America/New_York. DST and calls spanning
  midnight never reset accounting twice or erase a reservation.
- An unknown outcome keeps its reservation counted; its cost is labelled
  unknown/estimated, never a confirmed charge. A later usage report reconciles
  the reservation without double counting. Missing price information is never
  treated as free: if a price cannot be determined, the call is **refused**
  and no reservation is written.
- Other limits: tokens per call, calls per hour, at most **2 calls in flight**,
  and **one decision deadline (default 60 s) spanning orchestrator plus Jev**.
  Hourly and concurrency limits delay or expire work in their own windows; only
  the daily cap blocks calls until the reset.
- Exhaustion blocks new model work only. Reconciliation, already-decided valid
  closes and SP1's deterministic exits keep running. It does not promise new
  model-generated closes.

### 5.5 Flows

**Entry signal (strategy BUY):** trader signal record → intake (opportunity and
consumer cursor committed atomically) → budget → fresh evidence → Jev → submit
`submit_ai_paper_decision` (ENTER) → baseline "follow the signal" recorded
regardless of Jev's ruling.

**Exit signal (strategy SELL):** branches to SP1's safe close without Jev.

**Entry cycle:** discovery → orchestrator proposes ENTERs → each through Jev →
submit; baselines "fixed rule on the same candidates" and "no-trade" recorded.

**Position cycle:** orchestrator may request CLOSE / PARTIAL_CLOSE for owned
positions; no Jev; trader enforces the reduction (section 6.4); baseline
"matched-entry, bracket-only exit" recorded.

Retained stale signals become MISSED. An expired source cursor produces a
coverage-gap record, never a claim that every missing signal was identified.

## 6. SP1 amendments

Each is a narrow, explicit trader change with its own Plan 2 allow-list entry,
read and mutation rights separate.

1. **Durable strategy signal record + cursor read.** Today the strategy path
   persists a `TradingEvent` and publishes on the MessageBus
   (`trader/strategy/strategy_runtime.py:1591`), which is not a delivery
   contract. Add a durable trader-side signal record with a monotonic cursor and
   retention, readable by `ai_supervisor` only.
2. **Controller epoch.** A trader-side epoch/lease table, a grant method for
   `ai_supervisor`, and epoch validation atomic with new-command admission on
   `submit_ai_paper_decision`. **2b:** a `controller_epoch` field in the Plan 2
   envelope, covered by the signature, required for new-command admission.
3. **Cost and simulation ingestion.** `record_ai_cost` and
   `record_simulated_decision`, `ai_supervisor` only. Idempotent ingestion, not
   scoreboard edits: stable record ids, validated experiment and action links,
   conflicting duplicates refused, later cost information as explicit
   corrections. This narrowly amends SP1 section 5.2 ("AI principals get
   read-only scoreboard methods").
4. **Model-driven closes.** CLOSE / PARTIAL_CLOSE from the orchestrator:
   - only positions attributed to this experiment, ownership verified by the
     trader;
   - only through SP1's scoped safe close (never a direct SELL or a separate
     cancel path);
   - size from fresh broker evidence; a partial close cannot exceed the held
     quantity or reverse the position;
   - SP1 owns cancellation, reconciliation and protection of the remainder at
     the **existing** stop and target prices (PARTIAL_CLOSE cannot be used to
     modify protection);
   - entry restrictions and loss breaches do not block a valid reduction; the
     kill and session flatten take precedence; uncertain closes never produce
     duplicate orders.
   The trader must **prove and enforce** the reduction; a stale close racing a
   stop fill must not oversell.

5. **Trader-owned Alpaca discovery read.** A read-only typed method (for
   example `discover_ai_candidates`) that returns Alpaca movers, most-actives and
   per-symbol news with source, timestamp, delayed label and coverage. Alpaca
   credentials stay in the trader/data services, never in `ai`. No implicit
   fallback to the IB scanner. Rights: `ai_supervisor` read only.
6. **Discretionary deployment kind.** Self-found ideas trade only inside a
   `discretionary` deployment that an **operator** registers (`cli` principal
   only, paper only). It names a universe (default `sp500`, configurable) and is
   sealed with the universe digest and the operator's attestation instead of
   backtest evidence; it is labelled `discretionary` everywhere it is shown. All
   other SP1 deployment and admission checks are unchanged; a discovered conid
   outside the universe stays refused (`CONID_NOT_IN_DEPLOYMENT`).
7. **Initial policy.** The operator publishes the initial risk policy with a
   `cli` command before arming. SP2a/b never publishes or loosens policy on
   startup or restart (policy generation is SP2d).
8. **Separate baseline books.** Simulated rows carry a versioned baseline id
   and an opportunity cohort. Storage, report, readback and display keep one
   book per baseline; counterfactual books are never summed. An incomplete book
   is reported as incomplete without hiding complete ones. This amends SP1's
   scoreboard report, which today sums all simulated rows.

## 7. Baselines and reporting

- Baselines are produced by deterministic code only; models never author
  simulated fills or performance.
- Baseline records are written even when Jev fails or the budget refuses the
  call. Missing simulation evidence is marked incomplete, never fabricated.
- Costs (both roles, failed and unknown attempts) and baseline records go
  through the `ReportingOutbox` to the trader's ingestion methods. Trader
  unavailability never loses them; a lost acknowledgement never duplicates them.

## 8. Untrusted input

News and any other text the model reads is untrusted. The enforceable boundary:

- untrusted text grants no authority;
- models only propose schema-validated actions;
- code owns tools, identities, evidence fields and ceilings; model output cannot
  override controller-generated ids, trusted evidence, tool permissions or owner
  ceilings;
- the trader independently validates execution.

Malformed, off-menu or invalid-size model output is a recorded refusal, never a
TAKE. REDUCE without an explicit bounded quantity is a refusal.

## 9. Failure handling

| Failure | Behaviour |
|---|---|
| Model timeout / error | Action abandoned and journaled **only before trader submission**. |
| Unknown model outcome | Reservation stays counted; cost unknown; nothing submitted. |
| Daily budget exhausted | No new model calls until the reset; reconciliation, decided closes and SP1 exits continue. |
| Lost submit reply | Reconcile by the same id and body. |
| Crash / restart | Missed slots not replayed; unsent stale work abandoned; possibly-sent work stays in reconciliation; successor takes a new epoch. |
| Stale controller wakes | Trader refuses its epoch. |
| Discovery partial / failed | Recorded with real coverage; only candidates actually seen are used. |
| Trader unreachable | Pre-send failure: unsent until expiry. Possible send: unknown → reconcile. Outbox retries reporting. |
| Bad Jev config | Blocks **every** ENTER (orchestrator cannot bypass its judge); closes and SP1 recovery remain. |
| Bad orchestrator config | No discovery or model closes; signal judgments by Jev continue if Jev is healthy. |

"Exits unaffected" always means SP1's deterministic exits and already-submitted
close recovery, not new discretionary exits without a working model.

## 10. Data sources

- Discovery uses Alpaca movers, most-actives and news (through amendment 6.5),
  plus an optional watchlist, limited to the discretionary universe. Alpaca discovery data is 15-minute-delayed SIP: every candidate is
  timestamped and labelled delayed. Fresh broker-side evidence (IB quote) is
  obtained before any ENTER is submitted.
- A failed or incomplete scan is never presented as complete.
- Markets and the interval are not widened until paper results and cost data
  justify it.

## 11. Replay

Replay is offline and side-effect-free: recorded inputs, tool results, model
responses, code and config versions and clock values; no model calls, no fresh
market reads, no trader commands. Missing evidence produces an explicit
**incomplete** result, never permission to fetch replacements.

## 12. Testing

All tests run without real model or IB calls. Passing them establishes simulated
integration evidence, not live-provider compatibility or real-paper readiness;
those remain separately authorized checks.

**Composition.** Real backend adapters with fake provider transports and
responses (not a wholesale `ModelClient` fake), and SP1's real coordinator, risk
gates, ownership and reconciliation, following `tests/test_safe_close_integration.py`.
`tests/test_command_stack.py`'s always-approving risk gate is not sufficient for
acceptance tests.

**Crash and leadership** (each asserts: original command id and exact body
survive; the successor reconciles under its new epoch; the original receipt or
close root is recovered; no duplicate entry or reduction; remaining positions
stay protected; accepted trader work continues through the takeover):

1. Journal committed, send not started.
2. Trader accepted, receipt not saved. **This key test terminates and restarts
   the `ai` process**, not just raises inside it.
3. Takeover around a lost submit reply.
4. A stale controller is refused by its epoch.

**Flows.** After-cutoff close driven by the controller's position cycle (not a
direct handler call); Jev failing while the orchestrator is healthy (every ENTER
blocked, closes still work); exit signal bypasses Jev; multi-action cycle;
duplicate and redelivered signals; crash between signal intake and cursor
advancement (no skipped signal); expired cursor → coverage gap; REDUCE that
would come out larger is refused; malformed output is a refusal; evidence,
policy or position changes during a model call cause the matching trader
refusal.

**Budget.** Concurrent reservations and restarts cannot exceed the shared
budget; at most two calls in flight; one deadline spans orchestrator plus Jev;
New York midnight, DST and midnight-spanning calls; unknown outcome keeps its
reservation and a later usage report reconciles it once; hourly limit delays
instead of blocking; exhaustion leaves reconciliation, decided closes and SP1
exits running.

**Config.** Missing model configuration, unsupported role/backend combinations,
malformed usage and unavailable pricing (the call is refused, no reservation).

**Durability.** Container recreation keeps `mmr_ai_data`; outbox delivers after a
trader outage; lost reporting acknowledgement creates no duplicate cost or
baseline record.

**Security.** Adversarial model outputs fed directly into validation cannot
override ids, evidence, tool permissions or ceilings. Method restrictions are
exercised through signed typed RPC, including cross-principal calls.

**Initialization.** With SP2c/d disabled and empty stores, initialize through
signed RPC only (operator publishes the policy and registers the discretionary
deployment), then run one strategy entry and one self-found entry; an
unregistered candidate stays refused; a restart neither republishes nor loosens
policy nor substitutes fixture evidence.

**Discovery route.** Through real signed trader RPC with a fake Alpaca transport
and an `ai` client without Alpaca credentials: partial coverage is reported, and
the IB scanner is asserted unused.

**Baseline books.** One experiment with fixed-rule, no-trade and matched-entry
books reports separate results; an incomplete book does not erase complete ones.

**Signed epoch.** A submit with a missing epoch, or a stale holder's resend with
an altered unsigned field, is refused.

**Replay.** With outbound network blocked, replay makes **zero** external-adapter
invocations (asserted, not just "no network"), and missing evidence produces an
explicit incomplete result.

## 13. Out of scope

Jev's backtest workflow (SP2c), AI risk-policy generation and wider discovery
(SP2d), Telegram commands (SP2e), stop/target modification (deferred option C),
swing and short styles, live trading.

## 14. Open questions

- Model ids per role and per backend, and how each backend's credentials are
  mounted into the `ai` container.
- Per-call token limit and calls-per-hour defaults.
- The fixed rule used as the self-found-idea baseline (a concrete formula, for
  example "top momentum candidate with the same stop and size").
