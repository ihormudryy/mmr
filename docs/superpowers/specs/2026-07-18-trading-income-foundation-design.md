# Trading Income Foundation Design

**Date:** 2026-07-18  
**Status:** approved
**Scope:** first deterministic, fully automated strategy for liquid US equities
and ETFs, long-only intraday, flat by the end of every session

## 1. Purpose

MMR already has substantial execution-safety, broker-state, audit, dashboard,
and research machinery. This design defines the remaining system required to
turn that machinery into a credible foundation for pursuing trading income.

The target is not a promise of profit. The target is one strategy that earns
the right to trade through reproducible research, deterministic automated
execution, bounded live risk, complete broker reconciliation, and evidence-led
capital scaling.

The first implementation follows a single-strategy vertical slice. It proves
one complete path before generalizing the platform to a second strategy.

## 2. Binding product decisions

- The first production model is deterministic, rules-based full automation.
- The LLM may generate research hypotheses, analyze evidence, and draft
  strategy changes. It cannot place, approve, retry, cancel, or resize orders.
- The initial market scope is liquid US equities and ETFs.
- The strategy is long-only and intraday. It may not intentionally carry an
  overnight position.
- The initial validation pace is accelerated: a minimum 30-calendar-day paper
  stage followed by a tightly capped live canary.
- The first live risk envelope is moderate but bounded:
  - maximum 0.20% of account equity at risk per trade;
  - maximum 0.50% account daily loss;
  - maximum 3% canary drawdown from its high-water mark.
- Capital never scales automatically. Every allocation increase requires an
  authenticated, signed operator decision.

## 3. Non-negotiable safety boundaries

- Strategies emit typed intent only. They never communicate with IBKR.
- Human and automated trading use the same authoritative command pipeline.
- Only the trader service may mutate proposals, commands, broker-correlated
  state, pause state, risk state, or live allocation state.
- Only a versioned strategy with a valid eligibility attestation may emit an
  executable intent.
- Strategy parameters cannot weaken trader-owned risk or session limits.
- Every exposure-increasing order requires current account, broker, position,
  order, quote, liquidity, calendar, pause, and risk evidence.
- Missing or inconsistent critical evidence blocks new exposure.
- A reducing command remains available during degraded conditions only when
  the trader proves that its direction and quantity cannot increase or flip
  exposure.
- A timeout is `OUTCOME_UNKNOWN`, never success or failure inferred from lack
  of response. It is reconciled and never submitted again under a new ID.
- Submission is not execution. Broker events remain authoritative for order,
  fill, commission, position, and session-flat truth.
- Dashboard availability is irrelevant to automated safety. Enforcement lives
  in the trader service.
- Container restart does not clear a pause, circuit breaker, unresolved
  command, strategy suspension, or live allocation decision.

## 4. System architecture

The design extends the existing command-authority foundation. It must not add a
parallel order path.

### 4.1 Dataset Manifest

An immutable `DatasetManifest` identifies the exact research data used by an
experiment. It contains:

- vendor and retrieval timestamp;
- instrument identifiers and point-in-time universe membership;
- bar interval, timestamp convention, session calendar, and calendar version;
- corporate-action and adjustment policy;
- start and end boundaries;
- content checksums;
- missing-bar, duplicate, outlier, and session-completeness results;
- quote/spread source or the conservative estimation method;
- all verified data corrections with their original values and reasons.

A failed required quality check makes the dataset ineligible. Experiments may
not silently drop failed instruments or bars.

### 4.2 Experiment Registry

The `ExperimentRegistry` extends the current backtest store. A record contains:

- strategy path, class, complete source hash, and repository commit;
- dependency lock and container-image identity;
- dataset manifest digest;
- full parameter search space and the selected parameters;
- every successful and failed trial in the selection family;
- training, walk-forward, embargo, validation, and holdout boundaries;
- fill, slippage, commission, liquidity, and capacity assumptions;
- baseline, 1.5x-cost, and 2x-cost results;
- instrument, month, regime, volatility, and extreme-event attribution;
- deterministic replay result;
- quantitative eligibility result and qualitative review.

Failed trials are permanent members of the statistical denominator. Archiving
may hide a record from default views but may not erase it from selection-bias
calculations.

### 4.3 Strategy Eligibility Service

The service is deterministic. It produces an `EligibilityAttestation` with one
of these states:

- `CANDIDATE`
- `PAPER_ELIGIBLE`
- `CANARY_ELIGIBLE`
- `SUSPENDED`
- `RETIRED`

The canonical attestation contains:

- artifact, source, configuration, dataset, and allowlist digests;
- training, validation, holdout, and evidence boundaries;
- cost and capacity assumptions;
- eligibility ruleset name, version, and digest;
- eligibility state and permitted account mode;
- maximum gross allocation and permitted instruments;
- creation, expiry, promotion, and operator-approval timestamps;
- reason codes and evidence references;
- an Ed25519 signature.

The trader has the verification key but no signing key. Any change to code,
parameters, allowlist, data manifest, ruleset, account mode, or allocation
invalidates the attestation.

### 4.4 Automated Strategy Runtime

The runtime loads only an eligible artifact and may emit only a typed
`ExecutionIntent`. The intent includes:

- `artifact_id`
- `session_id`
- `bar_id`
- `signal_id`
- deterministic `intent_id` and `command_id`
- account mode
- conid and side
- requested quantity or risk-derived sizing inputs
- entry, stop, target, time-exit, and session-exit policy
- artifact and eligibility-attestation digests
- signal timestamp and completed-bar timestamp

The IDs are derived canonically from stable inputs. Reprocessing the same bar
and signal produces the same command ID and therefore an idempotent replay, not
a second order.

### 4.5 Trading Command Coordinator

The existing coordinator remains the sole execution authority. It:

1. Claims the command and mandatory audit record atomically.
2. Verifies artifact and eligibility signatures.
3. Verifies account, mode, session, pause, and allocation authority.
4. Captures and validates the approval context.
5. Applies session, liquidity, position, and portfolio risk policy.
6. Dispatches with the encoded order-group reference.
7. Resolves from broker evidence or enters reconciliation.
8. Records the final command and attribution references.

All coordinator calls that touch DuckDB, broker adapters, or downstream
services run without blocking the trader asyncio loop.

### 4.6 Approval Context

The context separates broker-generation evidence from quote evidence instead
of claiming they share one clock.

#### Fenced broker risk snapshot

All values are read in one transaction from one promoted broker generation:

- generation ID, source cursor, and timestamp;
- account ID and authoritative paper/live mode;
- net liquidation and daily P&L;
- positions and reducible quantities;
- working orders and open-order count;
- existing symbol and portfolio exposure.

#### Executable-market evidence

- BUY ask or SELL bid;
- market and receipt timestamps;
- quote age;
- live/frozen/delayed feed classification;
- session and halt state;
- spread and top-of-book size;
- what-if margin response and timestamp.

The complete object is immutable after assembly. Critical reads fail closed.
Quote, what-if, and broker-generation freshness are revalidated immediately
before dispatch.

### 4.7 Session Risk Controller

The controller is trader-owned and enforces:

- permitted strategy and instrument;
- maximum allocation and position count;
- per-trade risk and stop distance;
- per-position and gross exposure;
- daily loss and canary drawdown;
- quote freshness, feed type, spread, ADV, and depth;
- regular-session entry windows;
- entry cutoff, order cancellation, flatten, and flat-confirmation deadlines;
- circuit-breaker state.

No strategy confidence field or request parameter can increase these limits.

### 4.8 Broker Truth and Reconciliation

Existing broker producers and the domain journal remain authoritative. Every
order carries an `order_group_id` correlated with the command. A submission is
unresolved until broker order evidence arrives. Fill, commission, cancel,
reject, and position updates advance the same correlated lifecycle.

An ambiguous outcome blocks equivalent target commands until reconciliation.
Reconciliation continues across process restarts and cannot turn uncertainty
into failure merely because time elapsed.

### 4.9 Attribution Ledger

The ledger joins:

- artifact, dataset, signal, and intent;
- approval context and every policy decision;
- command and order-group identity;
- broker orders, fills, commissions, and position changes;
- expected and realized entry, exit, spread, slippage, and latency;
- maximum favorable and adverse excursion;
- gross and net P&L;
- rejection, circuit-breaker, reconciliation, and operator actions.

Promotion and suspension consume this ledger, never reconstructed dashboard
state or strategy-local memory.

### 4.10 Promotion Controller

The controller evaluates evidence and prepares attestations. It never places
orders. Moving to live mode, increasing gross allocation, clearing a canary
drawdown suspension, or adding a second strategy requires an authenticated
operator action and a new signature.

## 5. Runtime flow

1. An eligible strategy observes a completed, session-valid bar.
2. It emits a deterministic `ExecutionIntent`.
3. The coordinator claims and audits the command before validation or side
   effects.
4. Artifact, eligibility, account, mode, session, and allocation authority are
   verified.
5. One fenced broker snapshot and independent executable-market evidence are
   assembled into the immutable approval context.
6. The session risk controller approves or rejects the intent.
7. The context is refreshed or revalidated immediately before dispatch.
8. The dispatcher submits with the encoded order-group reference.
9. Broker events resolve orders, fills, commissions, and positions.
10. Attribution records expected versus realized behavior.
11. Risk and promotion controllers consume only authoritative ledger evidence.

## 6. Failure, stale-data, and circuit-breaker semantics

### 6.1 Stale quotes

- Missing, non-finite, crossed, halted, delayed-disallowed, or policy-stale
  quotes reject new exposure.
- A quote that becomes stale between initial approval and dispatch rejects the
  command before submission.
- Stale quotes do not prevent broker-verified reduction. A reducing command
  uses conservative execution and cannot flip exposure.
- Three consecutive quote-readiness failures during regular trading hours
  within five minutes pause automated entries globally.

### 6.2 Immediate global auto-pause

Any of these pauses all automated trading immediately:

- account or mode mismatch;
- missing protective order after an entry;
- failure to confirm flat by the session deadline;
- broker-generation regression or inconsistent snapshot;
- evidence of duplicate submission;
- daily-loss breach;
- canary-drawdown breach.

### 6.3 Threshold auto-pause

- Three consecutive `OUTCOME_UNKNOWN` or reconciliation failures within five
  minutes.
- Three consecutive quote-readiness failures within five minutes.
- Five strategy-runtime exceptions within ten minutes.
- Broker disconnection longer than the configured grace period.

### 6.4 Reset

Reset requires authenticated operator action, current reconciliation, current
semantic readiness, and an auditable reason. Restarting a service never resets
the state.

### 6.5 Unscheduled trading halt

- An unscheduled halt immediately blocks new orders and suspends the
  instrument.
- Re-entry requires five complete qualifying sessions, current liquidity
  checks, replay of the halt session, and an operator-reviewed event record.
- A blanket fixed 30-day exclusion is deliberately not used; requalification
  is evidence-based.

## 7. Audit and forensic replay

Every automated decision carries deterministic `artifact_id`, `session_id`,
`bar_id`, `signal_id`, `intent_id`, `command_id`, and `order_group_id`, plus
broker order, permanent-order, and execution identifiers.

After every session, the system seals a replay bundle containing:

- strategy artifact and eligibility attestation;
- input bars and decision-time quote evidence;
- broker risk snapshots and policy versions;
- intents, validations, and rejection reasons;
- commands, orders, fills, and commissions;
- reconciliation and circuit-breaker transitions;
- operator actions;
- calendar package version and resolved XNYS session schedule.

Replay must reproduce every strategy signal, intent, sizing result, and policy
decision. Broker behavior is reconstructed from recorded broker events rather
than simulated again.

## 8. Research and eligibility protocol

### 8.1 Dataset qualification

A research dataset requires:

- exchange-calendar-complete regular-session bars;
- point-in-time universe membership, including delisted instruments;
- explicit corporate-action and adjustment policy;
- vendor, retrieval, and checksum provenance;
- duplicate, missing, outlier, and session-boundary reports;
- bid/ask data where available and conservative spread estimates otherwise;
- at least three years of intraday history spanning multiple volatility
  regimes;
- frozen membership for every experiment.

Genuine crashes, gaps, halts, and abnormal-volatility periods remain in the
dataset. An outlier is removed only when verified as corrupt data; the original
value and correction remain auditable. Extreme-event results are reported
separately and remain part of eligibility.

### 8.2 Validation protocol

Every strategy family uses:

1. Chronological development/training.
2. Rolling walk-forward validation with embargoed boundaries.
3. One final untouched chronological holdout.
4. Baseline, 1.5x, and 2x execution-cost stress.
5. Parameter-neighborhood robustness.
6. Regime, month, instrument, volatility, and extreme-event attribution.
7. Multiple-testing adjustment across every recorded trial.

The holdout is opened once. A failed holdout retires that artifact version; it
cannot be tuned and retested against the same holdout under a different name.

### 8.3 `PAPER_ELIGIBLE` quantitative gate

All conditions are required:

- at least 200 historical round trips;
- research across at least eight eligible instruments;
- positive net expectancy at baseline and 1.5x costs;
- non-negative net expectancy at 2x costs;
- deflated or selection-adjusted Sharpe confidence of at least 95%;
- annualized Sharpe bootstrap lower bound above zero;
- profit factor of at least 1.20 after costs;
- positive results in at least 60% of walk-forward folds;
- no single month supplies more than 35% of total profit;
- no single instrument supplies more than 40% of total profit;
- holdout drawdown fits the proposed 3% canary stop after scaling;
- profitable behavior across a reasonable parameter neighborhood;
- expected order size within the approved liquidity and depth envelope;
- deterministic replay produces identical signals and orders.

### 8.4 Benchmark and regime gates

- Holdout drawdown must remain inside the predeclared risk budget and be no
  worse than 50% of an exposure- and volatility-matched SPY benchmark
  drawdown.
- Return, downside deviation, recovery time, and time in market accompany the
  benchmark so trivial low exposure cannot game the comparison.
- Regime taxonomy is frozen before results are inspected.
- Net expectancy is positive in at least 70% of eligible regime buckets with
  adequate samples.
- No eligible regime breaches its predeclared regime-loss tolerance.
- Regime transitions do not produce material instability.
- Deterministic abstention outside eligible regimes is permitted and included
  in the artifact.
- Material post-deployment regime degradation suspends the artifact; it never
  triggers automatic parameter adaptation.

### 8.5 Mandatory qualitative review

An operator signs a review of:

- economic rationale and plausible source of edge;
- why the edge should survive costs;
- known failure regimes;
- data and survivorship limitations;
- parameter sensitivity;
- operational dependencies;
- expected capacity and decay;
- implausible episode dominance;
- confirmation that the holdout was opened only once.

The qualitative review cannot override a failed quantitative gate.

### 8.6 Accelerated paper gate

All sample floors apply simultaneously:

- at least 30 calendar days;
- at least 20 completed trading sessions;
- at least 50 round trips;
- actual trades in at least five instruments.

Promotion additionally requires:

- no unexplained broker/internal-state divergence;
- no duplicate orders;
- every ambiguous command reconciled;
- every session confirmed flat by its deadline;
- exact signal replay from recorded inputs;
- realized costs inside the stressed research envelope;
- no unresolved critical alert;
- non-negative paper expectancy after actual costs;
- no material instrument, day, or regime concentration.

A correction resets the affected evidence counter. Calendar time alone never
earns promotion.

### 8.7 Eligibility expiration

Eligibility expires when:

- code, parameters, allowlist, or risk policy changes;
- a data correction invalidates research inputs;
- live costs exceed the modeled envelope;
- drawdown breaches the approved envelope;
- an active promotion has no fresh evidence for 30 calendar days;
- a strategy-level circuit breaker trips.

## 9. Live canary

Paper eligibility cannot authorize live trading. Promotion produces a distinct
`CANARY_ELIGIBLE` attestation.

### 9.1 Allocation and risk

- one automated strategy globally through canary and Scale 1;
- one signed instrument allowlist;
- maximum three concurrent positions;
- maximum 5% of account equity gross exposure per position;
- initial maximum strategy gross-exposure budget of 6%;
- maximum 0.20% account-equity risk per trade, calculated from a broker-native
  protective stop;
- no entry without a valid stop distance and confirmed protective-order plan;
- maximum 0.50% daily loss, including realized P&L, unrealized P&L, and
  commissions;
- maximum 3% drawdown from the canary high-water mark.

The most restrictive limit always wins. Confidence cannot modify these limits.

### 9.2 Instrument liquidity

An instrument is canary-eligible only when:

- price is at least $5;
- 20-day median dollar volume is at least $50 million;
- median regular-session quoted spread is no more than 15 bps;
- proposed quantity is no more than 0.25% of 20-day ADV;
- the quote uses a permitted live feed;
- the instrument is not halted or under halt requalification.

An order larger than current top-of-book depth is rejected unless the artifact
has an explicitly approved sliced/limit execution policy. It cannot silently
become an unrestricted market order. Five consecutive sessions below a
liquidity floor suspend the instrument.

### 9.3 Session controls

All boundaries derive from the versioned `exchange_calendars` XNYS schedule:

- automated execution only during regular market hours;
- artifact-declared opening stabilization period;
- no new entries after 15:30 ET on a normal session;
- working entry orders cancelled by 15:35;
- mandatory flatten begins by 15:45;
- broker positions and working orders confirmed clear by 15:55;
- remaining exposure after 15:55 raises a critical incident and keeps
  automation paused.

Early-close sessions use the same relative offsets from the official close.

### 9.4 Loss and incident response

A daily-loss breach atomically:

1. Trips the session circuit breaker.
2. Rejects new exposure.
3. Cancels working entry orders.
4. Performs broker-verified reduction.
5. Reconciles until positions are confirmed flat.
6. Requires authenticated reset no earlier than the next permitted session.

A 3% drawdown also suspends the artifact and requires a new promotion review.
One missing protection, duplicate submission, account mismatch, or missed
flatten is enough to stop the canary. Profits cannot offset a safety incident.

### 9.5 Live evidence gate

Minimum evidence:

- at least 30 completed live trading sessions;
- at least 75 live round trips;
- at least five traded instruments.

Promotion requires:

- positive live expectancy after actual costs;
- positive daily Sharpe and Sortino as corroborating evidence;
- realized results inside the research/paper prediction envelope;
- average and tail slippage inside the stressed cost model;
- zero capital-safety incidents;
- no duplicate order or unexplained position;
- all commands resolved or reconciled within their permitted windows;
- every session confirmed flat;
- drawdown below 3%;
- no trade supplies more than 35% of live profit;
- no day supplies more than 40% of live profit;
- removing the best trade does not make the remainder materially negative;
- no material instrument or regime concentration;
- deterministic replay of all strategy and policy decisions;
- signed review of historical, paper, and live deviations.

Sharpe and Sortino are not treated as long-run certainty at this sample size.
They cannot override safety, reconciliation, or concentration failures.
Insufficient evidence extends the canary. Economic or safety failure suspends
the artifact.

## 10. Controlled scaling

Every allocation is gross exposure and every increase requires a new signed
allocation attestation:

1. **Canary:** maximum 6% gross strategy budget.
2. **Scale 1:** maximum 9% after the live evidence gate passes.
3. **Scale 2:** maximum 13.5% after at least 20 additional sessions and 50
   additional round trips.
4. **Steady state:** hard ceiling of 15% gross account exposure per strategy.

At every level:

- per-trade risk remains no more than 0.20%;
- account daily loss remains no more than 0.50%;
- allocation decreases when cost, expectancy, liquidity, or drawdown leaves
  the signed envelope;
- a strategy that requires more risk to remain profitable is retired.

A second strategy requires:

- successful Scale 2 completion by the first strategy;
- its own research, paper, and canary attestation;
- portfolio covariance and shared-factor analysis;
- a combined risk budget that does not increase the account daily-loss limit.

## 11. Program decomposition and sequencing

This design produces five independently reviewed implementation programs.

### Program 1: Safety and command-plane completion

- Finish the existing command-plane activation design.
- Use genuinely fenced broker snapshots.
- Complete non-blocking handlers, unified registry, trader-owned live gates,
  semantic readiness, circuit breakers, and verified liquidation.
- Automated execution is prohibited until this gate passes.

### Program 2: Research evidence foundation

- Dataset manifests and quality gates.
- Point-in-time universes.
- Complete experiment registry.
- Walk-forward, holdout, regime, benchmark, cost-stress, and selection-bias
  tooling.
- Eligibility attestations and qualitative review.

### Program 3: Deterministic automated execution

- Typed intent and artifact verification.
- Deterministic IDs.
- Session risk controller.
- Protective-order and flatten state machines.
- Attribution ledger and forensic replay.
- Exactly one eligible automated strategy.

### Program 4: Paper and live-canary operations

- Paper/shadow runner.
- Fault injection and daily replay.
- Paper evidence gate.
- Signed live-canary activation.
- Live metrics, incidents, and suspension.

### Program 5: Controlled scaling and portfolio reuse

- Allocation attestations.
- Gross exposure ladder.
- Capacity and degradation monitoring.
- Second-strategy covariance and combined-risk gates.

### Indicative accelerated schedule

- Weeks 1-3: Program 1.
- Weeks 1-4: Program 2 foundations in parallel where interfaces do not
  conflict.
- Weeks 4-6: candidate research and final holdout selection.
- Weeks 6-10: minimum paper/shadow evidence period.
- Weeks 11-16 or longer: minimum live-canary evidence period.
- After the evidence gate: Scale 1 review.

This schedule assumes full-time focus and clean external dependencies. Findings,
data limitations, broker behavior, or failed gates extend it automatically.

## 12. Verification hierarchy

Every program requires:

- unit tests for policies, sizing, calendars, signatures, and transitions;
- property tests for idempotency, risk monotonicity, and the invariant that a
  reducing command cannot increase exposure;
- transaction and crash tests for ledger and attribution consistency;
- deterministic replay tests against sealed day fixtures;
- broker-adapter contract tests;
- full-stack paper tests using the real Compose topology;
- fault injection for disconnects, stale quotes, timeouts, partial fills,
  rejected protection, duplicate delivery, process restart, and missed flatten;
- at least one complete market-session soak before paper promotion;
- manual paper drills for emergency pause, liquidation, unexpected power loss,
  broker disconnect, process crash, restart reconciliation, and backup restore.

Test doubles support development but cannot satisfy a final paper or canary
promotion gate.

## 13. Operational rhythm

### Before every session

- Verify account, mode, artifact, allocation attestation, and XNYS session.
- Promote a complete broker generation.
- Confirm quote coverage and risk inputs.
- Confirm the prior session is flat and reconciled.
- Confirm circuit breakers and strategy eligibility.
- Keep automation paused when any check fails.

### After every session

- Confirm positions and working orders are empty at the broker.
- Seal the replay bundle.
- Replay signals, sizing, and policy decisions.
- Attribute execution costs and P&L.
- Update circuit-breaker and eligibility evidence.
- Require operator acknowledgement of every divergence.

## 14. Definition of success

MMR becomes a foundation for controlled trading income only when:

- one strategy completes research, paper, and live canary;
- live expectancy remains positive after actual costs;
- results are not dominated by one trade, day, instrument, or regime;
- live drawdown remains inside the signed envelope;
- every trading day is forensically replayable;
- there are zero unresolved capital-safety incidents;
- the system safely pauses, restarts, and reconciles after unexpected power
  loss, broker disconnect, or process crash;
- broker state is recovered without guessing;
- allocation changes remain deliberate, signed, reversible, and bounded.

Passing these conditions does not guarantee future profits. It demonstrates
only that the system is capable of pursuing trading income with bounded risk,
full auditability, and credible evidence of edge.

## 15. Explicit non-goals for the first vertical slice

- LLM-directed order placement or approval.
- Short selling, options, futures, leverage, or intentional overnight risk.
- More than one automated strategy before the first completes Scale 2.
- Automatic strategy adaptation, parameter optimization, promotion, reset, or
  capital scaling in production.
- Replacing the command coordinator with a strategy-specific execution path.
- Treating dashboard state, HTTP acknowledgement, or process health as broker
  or trading readiness.
