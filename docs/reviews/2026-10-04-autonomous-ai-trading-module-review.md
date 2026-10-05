# Review: Autonomous AI Trading Module

**Date:** 2026-10-04
**Reviews:** `2026-10-04-autonomous-ai-trading-module-design.md`
**Status:** Do not implement the executing path from this spec as written. Rechecked 2026-10-05; verdict unchanged. See the note at the end.
**Method:** Spec read against `master` at the time of writing. Tests were not re-run. The live paper account was not read. No orders were submitted.

## Verdict

Keep the control sketch. Do not arm it. Do not let phase-1 schemas freeze the risk hole.

The spec is a serious paper-only supervisor design. It is not a strategy, and it is not evidence that anything in the current catalogue should trade. By its own definition, success can be months of measured rejection and `NO_TRADE`. That is the correct outcome. A running module is not progress.

This spec is also a deliberate reversal of three binding non-goals in the approved foundation design (`2026-07-18-trading-income-foundation-design.md`, section 15):

- LLM-directed order placement or approval.
- More than one automated strategy before the first completes Scale 2.
- Automatic strategy adaptation, parameter optimization, or promotion.

The spec discloses the first reversal. It should be treated as a new product decision, not as a configuration of the system that already exists. V1 of this module should not take all three reversals at once.

## What already holds

These parts match the code and should survive an amendment:

- The document authorizes nothing operational: no container restart, no broker order, no live authority.
- The supervisor must not connect to IBKR or write the trader journal. `CommandReceipt` stays frozen (`trader/domain/commands.py`). AI decisions need a new envelope and must still cross the existing coordinator, so there is one dispatch boundary.
- Forbidden shortcuts are the right list: `auto_approve`, impersonating the strategy-service principal, model-edited live YAML, legacy dill RPC, and `skip_risk_gate`.
- Fixtures are not qualification. `require_qualified_research_evidence` already rejects `reviewer == "bootstrap"` and `evidence_kind == "offline_fixture"`, and it requires a complete passing `paper-v1` decision whose ruleset digest matches the current module (`trader/automation/paper_materials.py`).
- `OUTCOME_UNKNOWN` must not be resubmitted under a fresh id. Risk-reducing exits stay deterministic. Telegram is a side channel: numeric user id and chat id, silence does not stop a healthy loop, and a notification is never a fill.
- The delivery order is right: read-only coverage, then research, then shadow decisions, then paper admission, then a real IB paper session. A Telegram bot is not autonomy.

## Conflicts with the current tree

### 1. Provider policy is already wrong

Section 7.2 says Massive-first for US ideas and movers, with TwelveData or IB as the configured fallback. That is not this tree.

`trader/data_providers/builtin.py` defaults history, quotes, movers, news, and ideas to Alpaca. `config_defaults/trader.yaml` sets `default_data_source: alpaca`. The CLI snapshot path does not even inherit that Alpaca history default: unless `data_providers.quotes` or `MMR_DEFAULT_DATA_SOURCE` names a quote source, snapshots stay on IB (`trader/mmr_cli.py`, `_snapshot_source_default`). Forex convert and forex movers are Frankfurter and computed ECB rates, not Massive. Index movers default to an ETF proxy.

"Discover the liquid US universe" is also not a capability MMR has. Alpaca IEX is not SIP. IB scanners pace. A liquid fallback list is not the market. The spec already says a partial scan must not be labelled complete. V1 coverage has to be a versioned, entitlement-feasible set plus scan deltas, counted from the provider that actually answered.

### 2. There is no strategy-service principal

Typed RPC authenticates with one shared HMAC key (`trader/messaging/typed_rpc.py`). `execute_automated_intent` is registered on the trader command socket. The handler stamps `source="strategy_service"` itself (`trader/messaging/production_api.py`). That string is not derived from a distinct credential. Any process that holds the shared key and can reach the command port can call the method.

The spec is right that hiding tools in a prompt is not authorization. Phase 1 has to add distinct keys and a server-side method ACL. Another `source` field will not do it.

### 3. The numerical envelope already exists, and the spec proposes to leave it

The spec's A5 allows AI-selected numerical limits to differ from "legacy paper presets" inside `ai_paper`. The current trader does not treat those numbers as presets. They are hard ceilings that request fields may only tighten.

`trader/automation/session_risk.py`:

- 3 positions
- 5% of equity per position
- 0.20% of equity at risk per trade
- 0.50% account daily loss
- 3% drawdown from the high-water mark

`trader/promotion/allocation_policy.py` repeats the same trade, position, and daily-loss ceilings, and caps steady gross at 15%. Paper automation is tighter than that: `allocation_factory` returns `min(0.06, artifact ceiling)` and the comment says no request field can raise it (`trader/automation/production_evidence.py`). The 6% figure is the canary stage ceiling (`STAGE_CANARY` in `trader/promotion/allocation_attestation.py`), applied here as a paper cap. It is not a suggestion.

R6 lets the orchestrator invent per-trade risk, gross exposure, position count, and the session loss budget, provided the numbers are finite and "valid for the account and strategy evidence." That phrase is not a ceiling. The session-loss freeze in section 9.3 happens after the model has proposed the anchor. Tightening during the day does not help if the opening budget is loose.

Paper mode limits the dollars. It does not limit the failure mode. A model that sets its own gross and loss budget can still churn the only clean paper book, collide with leftover positions, and produce a track record that cannot be interpreted.

The owner's actual ask — do not pick a number every morning — does not require the model to own the envelope. The new path must call the same `SessionRiskController` constants. An AI policy may only tighten them. No valid policy still means no new exposure. `ai_paper` must not be the mode that unlocks the 15% steady cap, or anything above 6%, by calling itself a new policy.

### 4. Multi-strategy admission is not greenfield

`SessionRiskController` already blocks an entry when more than one strategy is active and no portfolio authority is present (`PORTFOLIO_AUTHORITY_ABSENT`). `PortfolioRiskBudget` keeps the 0.50% daily-loss ceiling and a position-count cap (`trader/promotion/portfolio_risk_budget.py`). Unattended paper is still exactly one `automation.strategy_name`. Empty means no strategy may emit. `automation.live_enabled=true` is refused at config load (`trader/config.py`).

A second admission controller that the model can reparameterize would bypass this. Extend the existing evaluator. Do not invent a parallel one.

Arming must also be mutually exclusive with the existing one-strategy automation lock, and the new path must not call `approve_proposal` or `execute_automated_intent` as a shortcut. Those are different authorities (`docs/PAPER_AUTOMATION_SETUP.md`).

### 5. Flat-by-close already has an owner

`SessionController` is the trader-owned flatten path. Its states include `FLATTENING`, `VERIFYING_FLAT`, and `INCIDENT`. A missed flat deadline trips the breaker (`trader/automation/session_controller.py`). Strategy `close_by_time` is advisory. The session controller is the backstop.

Only some catalogue modules set `close_by_time`: opening-range breakout, both VWAP strategies, gap reversion, late-day momentum, and opening-drive fade. The rest of `strategies/` does not. An intraday flat mandate is a property of a bound artifact, not of the catalogue. A daily-bar pass does not qualify a 1-minute strategy. Reuse `SessionController`. Do not build a second flatten workflow that can claim flatness the broker did not confirm.

### 6. Autonomous qualification cannot reuse the current bundle

`paper-v1` is content-addressed. Editing the ruleset changes its digest and invalidates old attestations (`trader/research/rulesets/paper_v1.py`). The floors include 200 round trips, 8 instruments, positive expectancy at 1.0x and 1.5x costs, non-negative expectancy at 2x, selection-adjusted Sharpe confidence of at least 95%, profit factor of at least 1.20, 60% of walk-forward folds positive, no month above 35% of profit, no instrument above 40%, scaled holdout drawdown inside 3%, and the holdout opened once.

Export currently requires exactly one operator review (`trader/research/bundle.py`). That review cannot override the quantitative gate, and it cannot be blank (`trader/research/review.py`). The spec is right that the model must not fabricate an `OperatorReview`. The consequence is easy to miss: an autonomous paper grant cannot satisfy today's bundle exporter by omitting the review, and it must not satisfy it by writing one. A new authority type is required. It still has to require the current `paper-v1` digest. Skipping the review is not a way to skip the numbers.

`docs/superpowers/rollout/trading-income-paper-log.md` still has the signed `PAPER_ELIGIBLE` bundle, the one-strategy config, and the paper automation stack as open checklist items. This review did not find a mounted qualified bundle. Missing evidence stays `CANDIDATE` or `SHADOW`.

### 7. Jev is a veto, not a second risk system

The preserved prototype is not in this repository. The local `app/jev.py` that the spec points at is an OpenRouter TypeSafe adapter:

- default model `typesafe/jev-1.13`
- `TypeSafeClient.system_one`, not a generic chat-completions call
- `SystemOneResponse.answers`, then the choice under the question name
- off-menu, missing, non-finite, or out-of-range probability becomes `HOLD`
- default in-memory daily cap of $2, with a 4096-token fallback when usage is missing
- this file does not submit broker orders

Port the parser and the fail-closed mapping. Do not port the in-memory cap or the direct-broker loop that lived beside it. Map `HOLD` to the spec's `NO_TRADE` explicitly. Do not size from the returned probability. Pin the prototype commit or vendor the adapter contract, and confirm the installed SDK shape before naming a model id.

Jev choosing among plans the orchestrator built is independent only when `NO_TRADE` is a real option, quantities stay on the plan, and the opposing case is not edited by the model that wants the trade. Counterarguments should come from deterministic checks, or from a role whose text the orchestrator cannot revise before Jev sees it.

### 8. Expectancy is not cash profit

`paper-v1` reads `expectancy_bps`. The backtester's default cost model is 1 bp of slippage and $0.005 per share (`trader/simulation/backtester.py`). `docs/BACKTEST_METRICS.md` shows that expectancy equally weights each SELL's return on closed entry notional, while profit factor sums cash. Unequal notionals, partial exits, and exit splitting can make the two disagree in sign without either calculation being wrong. An open position can also make total return disagree with both.

Do not qualify on expectancy alone when it conflicts with cash P&L beyond a stated tolerance. Compare like-for-like exit policies. A pass at 1 bp is not an opening-range fill model.

## Required amendments before any schema is written

1. Replace the Massive-first paragraph with the current provider map: Alpaca for US history, ideas, movers, and news; IB for snapshots unless a quote source is explicitly configured; no Yahoo. Define the V1 universe as a versioned feasible set. Coverage counts name the source that answered.
2. Distinct credentials per principal, and a server-side method ACL. The supervisor, research worker, qualification signer, and Telegram bridge do not share the trader HMAC key.
3. The trader envelope is the existing hard constants, including the 6% paper gross cap, 0.20% per-trade risk, 0.50% daily loss, 5% position, 3 positions, and 3% drawdown. AI policy may only tighten. The session-loss anchor is computed from that envelope, not from the model's first proposal.
4. `paper-v1`'s current digest is the only V1 qualification contract. A new ruleset is a human commit. An objective such as "make 1% today" cannot reuse a holdout, loosen a rule, or raise the envelope.
5. One new command type. It cannot call `approve_proposal` or `execute_automated_intent`. It cannot arm while `automation.strategy_name` is active on the same account. `/flatten` refuses while ownership of pre-existing positions is unresolved.
6. Reuse `SessionController` and `PortfolioRiskBudget`. Do not add a second flatten path or a second admission controller.
7. Deployment truth is the live strategy service and the trader journal. Not `OPERATIONAL_STATE.md`, and not `config_defaults/strategy_runtime.yaml`.
8. Operational bounds (call count, tokens, wall time, concurrency) live in the deterministic controller, fail closed, and are not Telegram mutation targets. A null dollar cap does not mean unlimited retries. Unknown model cost stays unknown, and it is persisted across restarts.
9. Qualification fails closed when expectancy and cash P&L disagree beyond the declared tolerance, or when the experiment's bar size and exit rule do not match the intraday flat mandate.

## What not to build yet

Do not start Jev shadow decisions, dynamic policy acceptance, or paper admission until those amendments are in the spec. Schemas written first will bake R6 in.

Phases 1 and 2, read-only, are reasonable after the provider correction and the envelope decision are written down. Phase 2 should stop if it cannot publish honest coverage against the entitlements that are actually present.

Phase 6 is a paper session and an evidence review. It is not a promotion path. There is still no closed post-deploy scoreboard in the paper log. Another autonomous search will not create one.

If the catalogue cannot clear `paper-v1` on the intraday flat mandate, the steady state is shadow and no-trade. That is success under section 1 of the spec. Do not "fix" it by weakening the gate.

## Rechecked against origin/master on 2026-10-05

The verdict is unchanged. Conflicts 1–7 still match the code on `a6ea76f`.

Section 8 is partly stale. Expectancy is now dollar-weighted (`sum(net_pnl) / sum(entry_notional)` in `trader/simulation/backtester.py`), so its sign matches profit factor when there is at least one net loss. `research evaluate` records a realistic cost model. A library `BacktestConfig` with no cost model still uses 1 bp and $0.005 per share. Amendment 9 should compare the stored cost model and bar size, not treat an expectancy/cash sign split as the current defect.

Two facts this review did not cover, both of which tighten the verdict:

- Paper attestations may use `reviewer_kind="llm"`. Live still requires `human`. The module still must not write that review.
- An automated SELL with no quantity is still rejected (`QUANTITY_REQUIRED` in `trader/automation/production_evidence.py`) before the held size can be used. Reusing the current exit path would not close a position.
