# Autonomous AI Trading Module — Design Specification

**Date:** 2026-10-04
**Status:** Proposed; implementation and operational validation not started.
**Scope:** Autonomous, AI-directed **paper trading**, integrated with MMR, with supplementary Telegram communication.
**Working name:** MMR AI Supervisor.

> **Superseded (2026-10-05):** the executing path of this spec is replaced by
> [`2026-10-05-ai-paper-sp1-foundation-design.md`](2026-10-05-ai-paper-sp1-foundation-design.md)
> and its SP2/SP3 roadmap. Keep this file as background.

This document specifies a product, not a running deployment. It does not authorize
restarting containers, registering external accounts, or submitting broker orders.
No live-trading authority is introduced by this design.

## 1. Product objective

Build an independent AI service that operates MMR as an autonomous research,
strategy-management, and paper-trading system. It continuously observes available
market and account information, searches for opportunities, evaluates strategies,
selects parameters, manages deployments, and obtains trade decisions from Jev.

Telegram is an optional operator interface. The supervisor must not wait for a
message, a daily profit target, or per-trade approval to perform its normal work.
Once its paper mandate is configured and explicitly armed, it operates without
routine human intervention. The owner can provide supplementary objectives,
constraints, questions, and control commands.

Success means reliable, measurable autonomous operation—not guaranteed profit.
An unmet profit objective never creates a requirement to trade, increase risk, or
recover losses immediately.

## 2. Requirements established during discussion

| ID | Requirement |
|---|---|
| R1 | Run the supervisor as a separate module/process/container, not inside the broker event loop. |
| R2 | Use a powerful, configurable OpenRouter model for orchestration and research, such as an available Astra or Claude Opus model. |
| R3 | Use Jev as the decision maker for discretionary trade-plan selection. |
| R4 | Proactively monitor all relevant, configured MMR data capabilities and search the supported market—not only existing holdings or a manually maintained watchlist. |
| R5 | Independently select strategies, enable/disable them, evaluate parameters, and deploy qualified versions. |
| R6 | Let the orchestrator define numerical paper-risk policies dynamically; do not require the owner to select fixed per-trade limits. |
| R7 | Accept high-level profit/performance objectives as guidance, not compulsory return promises. |
| R8 | Impose no daily OpenRouter dollar-spend cap by default. Maintain cost accounting and bounded computational workflows. |
| R9 | Post trade outcomes, material decisions, incidents, and summaries to Telegram; respond to authorized owner commands. |
| R10 | Telegram instructions are supplementary. Silence or a Telegram outage does not stop otherwise healthy autonomous operation. |
| R11 | Initial execution mandate: liquid US equities/ETFs, long-only, unleveraged, intraday, with broker-confirmed flatness by session close. |
| R12 | Remain paper-only. Do not revive the previously removed Docker stacks or reuse their direct broker-order loop. |

The eligible instrument scope is approved by rules, not by asking the owner to
approve every ticker. The supervisor may discover and qualify new instruments
inside that scope. International information may be research context when MMR
provides it; it does not expand execution authority to international instruments,
shorts, derivatives, leverage, or overnight positions.

## 3. Relationship to existing MMR

### 3.1 Reuse

Reuse rather than duplicate:

- MMR's configured Massive, TwelveData, and IB data capabilities; local history,
  exact contract resolution, universe management, news, fundamentals, and scanners.
- Backtesting, parameter overrides, sweep execution, experiment records, research
  qualification, artifact verification, and statistical evaluation.
- Strategy runtime and typed strategy-control commands.
- Trader-owned command coordinator, command ledger, risk evaluation, protective
  orders, broker ingestion, reconciliation, session exits, and audit trail.
- Broker-derived events and projections for portfolio and trade reporting.

### 3.2 Explicit extensions—not configuration shortcuts

The [existing foundation design](2026-07-18-trading-income-foundation-design.md)
restricts LLMs to research and begins with one deterministic automated strategy.
The [paper setup guide](../../PAPER_AUTOMATION_SETUP.md) describes that existing
mode. This specification proposes a **separate, opt-in `ai_paper` mode**.

Implementation must explicitly add:

1. Service-scoped AI query, research, policy, deployment, and intent capabilities.
2. Autonomous paper-only research qualification and deployment authority.
3. Versioned dynamic paper-risk policies and deterministic enforcement.
4. Multiple qualified strategy deployments under one portfolio authority.
5. An authenticated Telegram operator bridge and durable notification delivery.

Do not obtain these capabilities by setting `auto_approve`, impersonating the
strategy-service principal, editing live YAML from model-generated shell commands,
auto-approving arbitrary proposals, weakening signatures, or using legacy dill RPC.

Preserve existing deterministic-paper and live-mode behavior. In particular, the
legacy fixed risk constants and the current 6% paper allocation ceiling must not
be silently disabled globally. The new mode needs its own validated policy
selection at the trader boundary. Until that path exists and passes its tests,
existing limits remain effective and the AI module stays non-executing.

Existing hot parameter replacement is not sufficient for qualified AI deployment:
parameters and instruments must remain bound to newly validated artifact versions.

## 4. Architecture and responsibilities

```text
Configured MMR data providers + broker events + local research
                         |
             Capability gateway / evidence assembler
                         |
                AI supervisor workflow
         monitoring / discovery / research / risk planning
                         |
          Isolated research and qualification worker
                         |
       Qualified strategy deployments and candidate plans
                         |
                 Jev decision adapter
                 select plan / no trade
                         |
        Trader-owned AI-paper policy and command adapter
                         |
     Existing coordinator -> risk -> execution -> reconciliation
                         |
                        IBKR
                         |
          Broker events -> audit -> Telegram outbox

Telegram owner commands -> authenticated command bridge -> supervisor/trader
```

### 4.1 AI supervisor

Owns work scheduling, hypothesis generation, research prioritization, candidate
selection, proposed strategy deployments, dynamic risk planning, and reporting.
It may request actions only through its capability gateway. It never connects to
IBKR or writes the production command, risk, or portfolio stores directly.

A deterministic workflow controller invokes the powerful model at bounded steps.
Do not implement the system as an indefinitely growing chat transcript in which
the model decides whether to obey the workflow.

### 4.2 Jev

Selects among explicit admissible trade plans or returns `NO_TRADE`. It receives
verified facts, portfolio context, strategy evidence, and counterarguments—not
only the orchestrator's narrative. Missing or malformed choices are rejected.

The preserved prototype's `app/jev.py` provides an adapter reference, including
its `SystemOneResponse.answers` parsing. Port and test the adapter, not the old
prototype's direct broker submission loop or in-memory cost tracker. Confirm
current SDK/provider compatibility before selecting a model ID.

Jev's action-choice probabilities are not assumed to be calibrated probabilities
of profitable trades. They must not directly scale exposure without evaluation.

### 4.3 Research worker and qualification controller

Execute bounded research jobs from an approved strategy catalogue. They own
research outputs, immutable experiment records, and qualification checks, not
orders. The model may choose experiments but cannot supply invented passing
metrics or change the qualification rules that judge its results.

A restricted qualification controller may authorize measured, eligible versions
for paper operation without human review of every deployment. It must issue a
new, explicitly autonomous-paper authority type. It must not fabricate a human
`OperatorReview`, reuse an offline fixture, or issue live/canary authority.

### 4.4 Trader and strategy services

The trader remains the sole broker mutation authority. It validates the caller,
paper account, deployment, policy, decision identity, current evidence, and
portfolio reservation before admitting an order.

The strategy service runs qualified versions, emits signals, and applies
coordinator-approved lifecycle changes. Strategy workers cannot change shared
risk state or communicate directly with the broker.

### 4.5 Telegram bridge

Provides reports and authenticated supplementary commands. It is not the trading
scheduler, source of market truth, or prerequisite for the next decision cycle.

## 5. Deployment and storage boundaries

Proposed source layout; these modules are new work, not existing APIs:

```text
trader/agent/
  service.py                 lifecycle and scheduler
  workflow.py                durable workflow transitions
  capabilities.py            restricted MMR tool adapters
  evidence.py                provenance and snapshot assembly
  discovery.py               coverage, screening, candidate queues
  research.py                bounded experiment orchestration
  deployments.py             qualified strategy-version lifecycle
  paper_authority.py         restricted evidence verifier and paper-only signer
  policy.py                  AI-generated paper-policy proposals
  decisions.py               decision epochs and plan lifecycle
  openrouter.py              orchestrator model adapter
  jev.py                     constrained Jev adapter
  store.py                   agent-owned durable state
  telegram.py                polling, authorization, command routing
  notifications.py           event projection and durable outbox
```

Add optional Compose services/profile:

- `ai-orchestrator`: supervisor, model adapters, and Telegram transport.
- `ai-research-worker`: backtests and sweeps with limited concurrency/resources.
- `ai-paper-authority`: deterministic evidence verification and paper-only grant
  signing, with no model tools, strategy-code execution, or broker connection.
- Reuse MMR's trader, strategy, data, and gateway services.

These are three separately permissioned processes. The authority service is
needed only for the autonomous qualification/execution phases; observation and
shadow research can run without it. Jev remains an adapter, not another container.

Initially use one orchestrator writer and one Telegram poller. A trader-owned
leader lease fences duplicate supervisor instances. Workers claim individual
jobs with leases and stable IDs; deployment and order authority remain central.

The agent has its own durable database/volume for jobs, observations, model
responses, conversations, costs, and notification delivery. It must not mount the
trader journal or trading configuration writable. Research writers use the
existing research-store ownership model and bounded concurrency.

Containers must be non-root, read-only except their explicit state/cache volumes,
resource-limited, and have no Docker socket or unrestricted host mounts. Restrict
network paths to the capability gateway and required external providers. Do not
rely on prompts or a shared private network to prevent broker access.

Only the restricted qualification component may hold an AI-paper signing key.
Its key is not an orchestrator tool, is not a live authority key, and is not
mounted into the trader or research workers that execute strategy code.
The authority reads immutable research evidence and emits signed grants to a
controlled artifact output. It cannot modify experiment inputs or metrics, and
its grant requests name recorded evidence—not model-supplied passing results.

## 6. Autonomous workflow and scheduling

The persistent workflow is:

```text
OBSERVE -> DISCOVER -> RESEARCH -> QUALIFY -> DEPLOY/RETAIN
                           |                   |
                           +-> SHADOW/REJECT   v
                                  STRATEGY SIGNAL / OPPORTUNITY
                                               |
                                   BUILD PLANS -> JEV DECIDE
                                               |
                                  ADMIT -> DISPATCH -> RECONCILE
                                               |
                                      ATTRIBUTE -> REVIEW
```

These are interacting loops, not one serial batch that blocks broker monitoring.

- Continuous lightweight loop: ingest broker events, monitor freshness and health,
  reconcile outcomes, maintain exits, and schedule incident handling.
- Market discovery loop: broad screening during relevant sessions, with detailed
  research only for shortlisted candidates or material changes.
- Research loop: evaluate strategy families, parameter neighborhoods, and regimes
  on a bounded worker queue without starving execution.
- Deployment loop: activate qualified versions, adjust allocations, pause entries
  for degraded strategies, and maintain position ownership through transitions.
- Review loop: compare expected versus realized costs, fills, behavior, and returns;
  attribute failure modes and schedule new research.

Cadences, task deadlines, concurrency, cooldowns, and search-space budgets are
versioned operational configuration. The orchestrator may prioritize work within
those bounds; it cannot create an unbounded fan-out or retry loop. Closed markets
do not require expensive model calls merely to restate that trading is closed.

Persist the authorized mandate and arm state. After a restart, reconcile broker
and command state, validate authority, and recover jobs before accepting new
entries. A healthy previously armed paper system need not ask for a Telegram
message to resume. A persisted manual pause or serious safety hold must survive.

## 7. Data capabilities, coverage, and market discovery

### 7.1 Capability inventory

Expose a machine-readable catalogue of configured capabilities, including:

- Health, broker connectivity, account mode, semantic readiness, and market calendars.
- Portfolio, account equity/P&L, working orders, fills, commissions, reconciliation.
- Market discovery, movers, scans, exact instrument resolution, watchlists/universes.
- Historical bars, quotes, spread/depth, volume, news, and fundamentals where supported.
- Strategy inspection, active versions, signals, backtests, sweeps, and evidence.

Each capability advertises supported markets, entitlement state, freshness
semantics, cost/rate constraints, response schema, and failure behavior. Its
availability is measured, not inferred from a configured API key.

### 7.2 Evidence provenance

Every observation records provider, instrument identity, market timestamp,
retrieval timestamp, adjustment/session convention where relevant, freshness,
coverage, and any fallback. Model summaries reference observations by ID and are
stored separately from the underlying data.

Preserve MMR's provider policy: Massive-first for US ideas/movers with explicit
entitlement-aware fallback; configured TwelveData/IB paths for supported data;
IB for supported international context. Do not add Yahoo Finance as a fallback.
Do not invent sentiment on the IB news path.

Unavailable optional context reduces research scope and is reported. Missing
critical evidence blocks the affected decision. `available=true` is not proof of
freshness; validate actual source timestamps and completed-session boundaries.

### 7.3 Broad discovery, selective deep research

1. Discover the rule-eligible US stock/ETF universe through available MMR providers.
2. Apply deterministic liquidity, instrument, session, and data-quality filters.
3. Use broad market scans to find changes in price, volume, volatility, sector
   behavior, news/catalysts, and strategy-relevant setups.
4. Prioritize deeper evidence collection and research for candidates.
5. Resolve exact conIds and qualify instruments before execution eligibility.
6. Maintain autonomous candidate watchlists and expire stale candidates.

Publish coverage counts: discovered, screened, excluded, stale, missing, deeply
researched, and execution-eligible. A liquid-stock fallback list must never be
reported as complete market coverage. Provider pacing/subscription constraints
remain in effect even when the owner imposes no AI dollar budget.

Public agent communities, including a possible AI4Trade integration, are optional
untrusted research sources, disabled by default. They are not dependencies of this
specification. No automatic registration, public portfolio upload, copy trading,
or remote skill installation is authorized. Their content never becomes operator
commands, execution prices, or broker truth.

## 8. Strategy research and autonomous deployment

### 8.1 Catalogue and experiments

V1 selects from an approved, source-versioned strategy catalogue and declared
parameter domains. It may run, combine, tune, and retire these strategies. It may
not generate arbitrary Python and deploy it into the broker-connected runtime.
New strategy code is a separate sandbox/review workflow.

For every candidate family, record source/version, parameters/search space,
point-in-time universe, data manifest, training/walk-forward/holdout boundaries,
cost/fill assumptions, resource use, failures, and selection history.

Use completed bars, next-open or justified executable fill assumptions, explicit
commissions/slippage/spreads, cost sensitivity, and lookahead checks. Repeatedly
searching the same holdout does not create fresh out-of-sample evidence.
Qualification must consider sample size, selection bias, parameter robustness,
liquidity/capacity, attribution, and benchmark-relative behavior.

Existing `paper-v1` quantitative checks are the starting qualification contract.
Missing evidence yields `CANDIDATE` or `SHADOW`, not a synthetic pass. New AI-specific
criteria must be versioned outside model control. The model's thesis can support
an experiment but cannot substitute for measured results.

### 8.2 Deployment authority

A qualified deployment binds the strategy source and class, parameters, conIds,
bar size, session/exit rules, data and research evidence, authority type, expiry,
and rollback version. Autonomous qualification produces paper-only authority;
it must be explicitly ineligible as proof of human review or live promotion.

Lifecycle:

```text
CANDIDATE -> RESEARCHING -> SHADOW -> PAPER_QUALIFIED -> ACTIVE
                                  |                       |
                                  +-> REJECTED            +-> DRAINING -> RETIRED
                                                          +-> SUSPENDED
```

A parameter or universe change creates a new deployment version. Apply changes
through typed, revision-checked, idempotent commands—not direct YAML mutation.
The orchestrator decides which eligible version to activate without per-change
human approval. The trader verifies evidence and scope before activation.

### 8.3 Existing positions and multiple strategies

Maintain attribution by deployment version and position/order group. Disabling a
strategy stops its new entries; protection and exit ownership continue until its
positions are flat or a validated ownership transfer completes.

A single portfolio admission controller arbitrates competing strategies and
atomically reserves pending exposure. It accounts for all broker positions and
working orders, not only the last signal's strategy. Conflicting BUY/SELL plans
must not become independent orders or repeated round trips merely because two
strategies disagree. V1 rejects unresolved ownership conflicts rather than
silently netting them.

Before first arm, reconcile the existing account. Do not silently adopt, cancel,
or liquidate manual positions or positions left by removed bots. The flat-by-close
mandate requires a dedicated clean paper book or an explicit owner-approved
ownership arrangement; unresolved pre-existing exposure blocks initial arming.

## 9. Dynamic AI-defined paper-risk policies

### 9.1 Policy generation and enforcement

The orchestrator proposes numerical policies using current equity, volatility,
liquidity, correlations/concentration, tested capacity, observed performance,
drawdown, and opportunity quality. The owner is not required to supply fixed
position counts or numerical risk limits.

Required policy fields include:

- ID, revision, scope, objective/deployment references, evidence digest, expiry.
- Per-trade risk, per-position notional/exposure, gross exposure, position count.
- Portfolio/concentration budgets, pending-order limits, and turnover controls.
- Session loss budget and drawdown trigger.
- Sizing and permitted stop/exit behavior; review schedule and reasons.

All required values must be present, finite, dimensionally consistent, and valid
for the account and strategy evidence. Reject malformed policies. No valid active
policy means no new exposure—not unlimited trading or an invented default.

The trader owns the effective policy record and evaluates every admission and
pre-dispatch revalidation. Strategy/model requests cannot pass `skip_risk_gate`.
Sizing is a deterministic calculation from the accepted policy, current broker
state, stop distance, and executable market evidence. Jev selects plans; it does
not secretly replace their quantities or policy references.

### 9.2 Immutable non-numerical boundaries

The AI cannot change:

- Paper-only verified account, instrument mandate, long-only/unleveraged operation.
- Broker buying-power/cash constraints, exact contract identity, or measured capacity.
- Data validity, protective-order requirements, session deadlines, and reconciliation.
- Evidence integrity, command identities, loss history, or high-water marks.
- Owner pauses, credential permissions, qualification rules, or live authorization.

### 9.3 No moving loss limits after a breach

Policies are prospective and versioned. Ordinary updates happen at declared review
boundaries; reductions in risk may apply immediately. Record the policy in force
before each exposure-increasing order.

Before the first entry of a session, freeze its loss-budget anchor. That budget
may tighten during the session but cannot increase to erase losses. Other limits
may expand at allowed review points only with fresh evidence, sufficient cash,
valid capacity, and no active risk breach or unresolved command.

A loss/drawdown breach remains a breach even if a later proposed policy is looser.
Serious loss, account, duplicate-order, protection, or flatness failures enter a
durable safety hold. The model cannot reset that hold, change the session date,
or erase the high-water mark. This is a control rule, not a user-selected fixed
numerical trading ceiling.

## 10. Candidate plans and Jev decisions

Create an immutable `CandidatePlan` containing exact instrument/deployment IDs,
side, horizon, entry condition and expiry, exit policies, deterministic proposed
size, evidence references, costs, portfolio effect, active risk-policy revision,
and supporting/opposing observations.

A `DecisionEpoch` identifies one opportunity using stable market/event identity,
deployment version, evidence digest, policy revision, and objective revision.
Store the epoch before calling Jev and persist the response before execution.

Jev returns a strictly validated `TradeDecision`:

- `SELECT_PLAN` with an offered plan ID;
- `NO_TRADE` with a reason code; or
- `NEED_EVIDENCE` identifying a supported missing observation.

Allow only bounded additional evidence requests. Do not repeatedly rephrase a
rejected opportunity until Jev selects a trade. Provider errors, invalid responses,
model NO_TRADE decisions, and trader risk rejections must remain distinct outcomes.

The trader refreshes evidence before dispatch. Expired plans, changed deployments,
stale policy revisions, conflicting reservations, or changed broker state are
rejected or require a new substantive decision epoch. A network retry is not a
new opportunity and must not obtain a fresh order identity.

Risk-reducing protective exits, session flattening, and emergency cancellation
remain deterministic and available without a model call where broker evidence
proves they cannot increase or flip exposure.

## 11. New contracts and capability authorization

Additive proposed contracts:

| Contract | Purpose |
|---|---|
| `AgentMandate` | Account mode, universe rules, horizon, objective, owner controls, arm state. |
| `ObservationEnvelope` | Typed provider data, timestamps, coverage and provenance. |
| `ResearchJob` / `ResearchResult` | Bounded experiment family and measured results. |
| `PaperDeploymentManifest` | Qualified source/parameters/instruments and paper authority. |
| `DynamicPaperRiskPolicy` | Versioned AI-proposed, trader-accepted numerical policy. |
| `CandidatePlan` / `TradeDecision` | Immutable admissible plans and persisted Jev selection. |
| `AgentControlCommand` | Authorized supplementary owner instruction and its confirmation. |
| `NotificationEvent` | Broker/control event projected into Telegram with delivery state. |

Proposed new capabilities include `get_agent_capabilities`,
`capture_agent_observations`, `submit_research_job`, `get_research_job`,
`activate_ai_paper_deployment`, `drain_ai_paper_deployment`,
`publish_ai_paper_risk_policy`, and `submit_ai_paper_decision`. These names are
proposals, not assertions that the endpoints already exist.

Use MMR's typed transport/coordinator conventions, but add actual per-principal
method permissions. Giving the agent the existing broadly privileged HMAC key
and hiding tools in a prompt is insufficient. The server derives caller identity
from authenticated credentials; it does not trust a caller-supplied `source`.
Separate the supervisor, research worker, qualification controller, and operator
bridge capabilities. Inaccessible functions remain inaccessible even if a model
or hostile source suggests their RPC names.

Keep the frozen [`CommandReceipt`](../../../trader/domain/commands.py) unchanged.
Do not reinterpret or mutate existing
[`ExecutionIntent`](../../../trader/automation/models.py) fields to disguise an
AI deployment as the old deterministic authority. Add a versioned AI-paper
command/envelope and translate validated decisions at the existing coordinator.
There must remain only one broker dispatch boundary.

## 12. Persistence, concurrency, and replay

Persist mandates, goals, observation references, jobs, experiments, policies,
deployment revisions, model requests/responses, decision epochs, command links,
leader leases, Telegram update cursors, notification outbox, and cost records.

Trade truth and risk authority stay in trader-owned stores; agent records refer
to their IDs rather than rewriting broker state. Research evidence stays in the
research store. The agent database stores workflow and communication state.

Required properties:

- A duplicate event, Telegram update, or model-task delivery has one logical effect.
- One decision epoch cannot create multiple exposure-increasing commands.
- `OUTCOME_UNKNOWN` remains unresolved until broker evidence determines its outcome;
  never resubmit the same economic action under a fresh ID.
- Reserve aggregate risk atomically across concurrent strategy decisions.
- Recover after crashes between model response, policy acceptance, dispatch, and
  broker acknowledgement without inventing success or repeating a submit.
- Never clear pause, loss history, reservations, or unresolved commands on restart.

Record exact model/provider IDs, prompt/schema versions, sanitized inputs,
responses, tool evidence, timestamps, and policy versions. Replay model responses
as recorded external inputs; do not assume re-querying a stochastic model will
reproduce a historical decision. Replay must reproduce deterministic admission,
sizing, command identity, and portfolio attribution.

## 13. OpenRouter, Jev, and computational costs

Pin explicit supported model IDs during setup. Validate each adapter's actual API,
response schema, and tool/structured-output capabilities. Do not assume a generic
chat-completions client can replace Jev's TypeSafe interface. Model/provider
changes create new evaluated policy versions; no silent trading-model substitution.

Configuration defaults to `daily_ai_budget_usd: null`, meaning no owner-requested
daily dollar cap. It does not mean unlimited retries, tasks, concurrency, tokens
per request, wall time, or provider quotas. Those operational bounds remain
mandatory and are visible in configuration.

Persist usage/costs across restarts for orchestration, Jev, research calls, and
retries. Distinguish provider-reported costs from estimates and unknown charges;
missing usage is not zero spend. Report spend in USD separately from trading
P&L in the account base currency. Alert on abnormal call rates or cost changes.
An optional dollar cap may be added later only by an authorized owner command.

## 14. Telegram: supplementary communication

Use a dedicated bot and long polling initially, avoiding a public inbound port.
Do not reuse a token already owned by another poller/gateway. Bot credentials are
configured through secure local secret handling, never in prompts, source, or chat.
Redact token-bearing URLs from client logs and exceptions.

Send external model providers only the observations needed for each task. Strip
broker account identifiers, credentials, unrelated personal data, and raw private
logs. Preserve the account currency and required portfolio/risk measurements
without exposing the account's login identity.

Authorize both numeric sender ID and chat ID. Usernames, display names, forwarded
messages, public posts, and arbitrary group members confer no authority. Telegram
updates are untrusted input until authenticated and classified.

Supported initial commands:

| Command/intent | Behavior |
|---|---|
| `/status`, `/portfolio`, `/orders` | Current verified state, freshness and unresolved work. |
| `/strategies`, `/research`, `/risk` | Deployments, experiments and active dynamic policy. |
| `/performance`, `/cost` | Attributed results, target progress and model spending. |
| `/why <decision-or-trade-id>` | Concise rationale plus evidence and policy references. |
| `/objective ...` | Version a supplementary goal; it is not a compulsory profit quota. |
| `/pause` or "stop trading" | Stop new entries; retain protection and exit management. |
| `/resume` | Resume only after appropriate readiness/authority checks; never bypass a hold. |
| `/flatten` | Request broker-verified liquidation through the coordinator, with confirmation. |
| `/exclude <instrument>` | Restrict future entries; do not silently liquidate existing holdings. |

Free text may map to these typed intents. Unsupported or ambiguous mutations are
clarified, not executed. Commands that change the mandate, loosen constraints, or
liquidate positions require an expiring, body-bound confirmation from the owner.
Routine autonomous policy/deployment/trade decisions inside the authorized paper
mandate do **not** require Telegram confirmation.

Persist received updates and accepted commands before advancing the polling
cursor. Retry/replayed updates and callback buttons cannot duplicate actions.
Use a durable outbox with stable event IDs for outbound messages. Telegram delivery
is retryable, not a claim of exactly-once delivery: a send acknowledged remotely
but lost locally may produce a labelled duplicate notification, never a duplicate
broker order.

Report `SUBMITTED`, `PARTIALLY_FILLED`, `FILLED`, `REJECTED`, `CANCELLED`, and
`OUTCOME_UNKNOWN` distinctly. Only broker-confirmed evidence supports a fill,
closed-position, or flatness claim. Routine research belongs in digests; fills,
material strategy/policy changes, and critical incidents receive prompt updates.

If Telegram is unavailable, queue notifications and continue healthy autonomous
operation under the current mandate. Do not wait for owner messages to initiate
research or trading. Telegram failure does not disable broker-side protection.

## 15. Failure behavior

| Condition | Required behavior |
|---|---|
| Orchestrator or Jev unavailable | No new model-dependent entries; existing deterministic exits remain active. |
| Optional data source unavailable | Record degraded coverage; continue unaffected research. |
| Missing/stale critical evidence | Reject affected entries; preserve reason and recovery state. |
| Broker disconnect or ambiguous submission | Fence admissions; reconcile without duplicate submission. |
| Invalid/expired policy or deployment | Block new entries for its scope; retain position exit ownership. |
| Qualification/job failure | Preserve failed trial; do not erase it from selection statistics. |
| Missing protection, duplicate order, wrong account, loss/drawdown breach | Durable safety hold and critical notification. |
| Session close deadline | Deterministic cancel/flatten workflow; broker evidence proves completion. |
| Telegram outage | Persist outbound backlog; no healthy-trading dependency on chat replies. |
| Database/audit authority unavailable | Fail closed before new exposure; never trade without durable command identity. |

Transient health holds may recover automatically under a deterministic readiness
policy. Owner pauses and serious safety holds require authenticated recovery;
the model cannot declare its own breach resolved. A halted or unavailable market
may prevent actual flatness: report the failure and unresolved exposure rather
than claiming a guarantee that the broker could not fulfil.

## 16. Observability and evaluation

Expose semantic readiness separately from process liveness. Observe:

- Data age, provider failures, coverage counts, discovery/candidate queues.
- Broker generation/cursor, working orders, reservations, reconciliation backlog.
- Active deployment/policy/objective versions and position ownership.
- Jev selections, NO_TRADE outcomes, invalid responses, and risk rejections.
- Research trials, failed qualification, drift, costs, latency and task exhaustion.
- Realized/unrealized P&L, commissions, slippage, drawdown, and model costs separately.
- Telegram accepted updates, unauthorized attempts, outbox age and delivery failures.

Evaluate a deterministic baseline, Jev-only selection, and orchestrator-plus-Jev
on comparable opportunities and cost assumptions. Track calibration, opportunity
selection, rejected opportunities, turnover, intervention frequency, recovery,
and safety—not just headline return. Paper results do not establish live edge.

Use [MMR metric semantics](../../BACKTEST_METRICS.md) when interpreting PF,
expectancy, partial exits, and unrealized returns. User objectives must not erase
failed experiments or select only flattering performance windows.

## 17. Acceptance criteria

| ID | Evidence required before executing mode is released |
|---|---|
| A1 | With no Telegram input across simulated sessions, the agent discovers candidates, completes research, selects qualified deployments, obtains Jev decisions, and produces idempotent paper intents. |
| A2 | A newly discovered eligible instrument can progress without individual ticker approval; out-of-mandate or unresolved instruments cannot. |
| A3 | Coverage/fallback tests prevent a partial scan or stale snapshot being labelled whole-market/current. |
| A4 | Parameter changes require a new qualified version; rejected research, insufficient evidence, fixtures, and fake human review cannot activate it. |
| A5 | AI-selected numerical limits can differ from legacy paper presets only in authenticated `ai_paper` scope. Legacy deterministic and live paths retain their original safeguards. |
| A6 | Missing/non-finite policies fail closed; breached session budgets cannot be raised or histories reset to authorize another entry. |
| A7 | Concurrent strategies cannot overspend shared cash/exposure reservations, duplicate an entry, or abandon existing exit ownership during replacement. |
| A8 | Jev may choose only offered valid plans. Errors, abstentions, off-menu output, and low-quality evidence never silently become approvals. |
| A9 | Crash/restart and duplicate events around model response, dispatch and acknowledgement produce no second economic order. |
| A10 | Broker rejects, partial fills, missing protection, disconnects, and session deadlines exercise real production composition with fake external ports, followed by a real IB paper session. |
| A11 | Unauthorized Telegram users/groups, stale confirmations, replayed updates, and research-text prompt injection cannot invoke privileged actions. |
| A12 | Telegram silence/outage does not halt healthy autonomous operation; notifications recover and never claim an unconfirmed fill. |
| A13 | Costs survive restarts, unknown costs remain labelled, and null dollar budget does not disable bounded retries, concurrency, or timeouts. |
| A14 | The model cannot reach broker credentials, signing keys, Docker, unrestricted shell, live authority, or the production database directly. |
| A15 | Recorded inputs/responses reproduce deterministic admission, sizing and command identity without another model call. |
| A16 | Initial arming detects pre-existing unexplained positions/orders and never silently adopts or liquidates them. |

Tests must distinguish unit checks, production-composition tests, synthetic fault
drills, shadow evaluation, and actual broker paper evidence. A green synthetic
suite or fixture bundle is not evidence of profitable trading or a completed
real paper-session release gate.

## 18. Delivery sequence

1. **Capability and authority foundation:** define schemas, service principals,
   paper-mode separation, durable workflow/leases, costs, and test doubles.
2. **Read-only supervisor:** coverage-aware market discovery, data monitoring,
   model research, Telegram read/control authorization, and decision journals.
3. **Research/deployment manager:** bounded sweeps, measured qualification,
   versioned paper authority, safe strategy replacement and rollback.
4. **Jev and dynamic-policy shadow mode:** prospective risk policies, candidate
   plans, decisions, portfolio arbitration, and deterministic replay; no orders.
5. **Autonomous paper integration:** trader admission, reservations, protected
   execution, reconciliation, session flattening, and Telegram fill notifications.
6. **Operational qualification:** fault drills, real IB paper session(s), evidence
   review, and explicit paper arming. No automatic live promotion.

Each phase has executable tests and a measurable completion gate. Do not advertise
full autonomy after implementing only a Telegram bot, scheduler, or proposal loop.

## 19. Configuration and remaining setup decisions

Proposed configuration concepts:

```yaml
ai_supervisor:
  enabled: false
  mode: observe                 # observe | shadow | ai_paper
  account_mode: paper
  mandate:
    instrument_scope: liquid_us_equities_etfs
    long_only: true
    leverage_allowed: false
    intraday_only: true
    flat_by_session_close: true
  orchestrator_model_id: ""      # exact verified OpenRouter model ID required
  jev_model_id: ""               # exact verified adapter-compatible model ID
  daily_ai_budget_usd: null      # owner requested no daily dollar cap
  risk_policy_mode: ai_dynamic
  telegram:
    enabled: false
    allowed_user_ids: []
    allowed_chat_ids: []
    token_secret_file: ""        # path only; never a token in this file
```

This is a proposed schema, not configuration currently understood by MMR.
Operational request/job limits, research qualification settings, feed credentials,
and account bindings require validated configuration before arming. Credentials
are supplied securely outside chat. Empty model IDs or owner allowlists are not
permissive defaults.

Remaining setup choices are exact model IDs, available market-data entitlements,
Telegram bot/owner identities, paper account/initial ownership reconciliation, and
operational worker schedules/resource ceilings. The owner may later supply a
high-level return objective through Telegram. In its absence, use an explicitly
configured default objective of seeking positive after-cost, risk-aware paper
performance relative to declared baselines—not an invented daily profit quota.

This spec does not include autonomous live trading, automatic capital promotion,
public copy trading, broker migration, arbitrary self-modifying strategy code, or
recreation of previously removed applications. Those require separate designs
and authorization.
