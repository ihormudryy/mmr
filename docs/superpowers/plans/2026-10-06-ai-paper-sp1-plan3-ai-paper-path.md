# AI Paper SP1 — Plan 3: The `ai_paper` Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the tasks in number order; each task ends with the full suite green.

**Goal:** Give the trader a second, separate automated mode, `ai_paper`, in which an `ai_supervisor` principal publishes risk policies under an owner ceiling and submits `ENTER` / `CLOSE` / `PARTIAL_CLOSE` decisions against sealed deployments registered by `ai_research`, all going through the existing coordinator, protective saga, `session_risk`, `DispatchGuard` and safe close. On the way, make risk limits data (`RiskLimits`) and fix the dispatch re-check that today widens a tightened ceiling.

**Architecture:** `trader/automation/risk_limits.py` holds `RiskLimits`, `PAPER_LIMITS` and `STEADY_LIMITS`; `session_risk`, `PortfolioRiskBudget`, `AllocationPolicy` and `DispatchGuard` read limits from it. The `ai_paper` path adds four trader-owned pieces in `trader/automation/`: the owner ceiling config (`ai_paper_config.py`), the policy store with per-session timing (`ai_risk_policy.py`), the sealed deployment store (`ai_deployments.py`) and the decision service (`ai_paper_decision.py`), plus sizing (`ai_paper_sizing.py`) and evidence capture (`ai_paper_evidence.py`). An `ENTER` becomes a duck-typed entry order for the existing `ProtectiveOrderSaga`; a reduction goes through Plan 1's `LiquidationService.start(scope="conid")`. The three new commands and two reads are typed RPC methods with Plan 2 allow-list entries. Experiments (Plan 4) are reached through a port; until Plan 4 lands, no experiment exists and every decision is refused.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 (strict models), pytest, hypothesis. No new dependencies. No model calls and no model provider anywhere in this plan.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md` (lands with PR #46; not on master. Immutable source: PR #46 commit `2a021c204798907736b7d3380901c0d7f40e7ac6`. Read it with `git fetch origin 2a021c204798907736b7d3380901c0d7f40e7ac6 && git show 2a021c204798907736b7d3380901c0d7f40e7ac6:docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, or `gh pr checkout 46`.) Do not copy it. Binding parts: section 5.4 "The `ai_paper` path", the rows of sections 2 and 3 on risk limits, the `ai_supervisor` / `ai_research` rights in 5.3, the `ai_paper` bullets of section 6, delivery step 3 of section 7.

## Global Constraints

- **Order of merges.** This plan builds on Plan 1 (safe close, PR #46: `LiquidationService.start(scope=...)`, `ExitOwnerRegistry`, `OutcomeReconciler._reconcile_close`, `AutomatedIntentCommandService._execute_close`) and Plan 2 (identities: `principals.TRADER_ACL`, `RpcCaller`, `register(..., with_caller=True)`, `CommandRequest.principal`). Start Task 1 only after both are merged to master. Line numbers cite master `8116f6a5`; files changed by Plans 1–2 are cited by function name, which is the anchor.
- **Old path.** Old-path entry admission and risk ceilings do not change (spec 5.4). The one deliberate change to the old path, the dispatch re-check, is bug #47, fixed in its own PR against master before this plan starts (owner decision 2026-10-06); it can only make the old path stricter. The one-strategy `paper-v1` path, bundle binding and Activate are untouched. A deployment record can never arm the old path.
- **Paper only.** `ai_paper.enabled: true` with `trading_mode` other than `paper` fails config load; with a live `account_mode` the command stack refuses to build (`AI_PAPER_LIVE_REFUSED`).
- **Owner ceiling code maximum is `STEADY_LIMITS`**: only `gross_fraction` may exceed `PAPER_LIMITS` (up to `STEADY_MAX_GROSS_FRACTION` = 0.15, `allocation_policy.py:21`). Built from the existing constants, never new literals (the 0.06 paper clamp moves from `production_evidence.py:173` into `PAPER_GROSS_FRACTION`).
- **Strict input everywhere.** Wire models use `ConfigDict(extra="forbid", strict=True)` (pattern: `trader/messaging/strategy_trader_contracts.py`) and `allow_inf_nan=False` on every float. `True`, `1.0` or `"265598"` for an int field is refused; nothing is coerced. Domain constructors (`RiskLimits`, `AiDeployment`, `AiPaperDecision`) re-check types themselves, because in-process callers bypass pydantic.
- **Fail closed.** Missing broker fence, quote, margin what-if, history, policy, effective limits, deployment, experiment or resolved instrument refuses the decision with its own code. No default fills a gap.
- **Owner answers of 2026-10-06** (to this plan's first question list) are binding: R6, R7, R8, R11, R17, R18, R23 and R25 carry them; the second round of answers (same day) is in R2, R13, R19 and R22. Spec 5.4 policy timing is amended by the owner on 2026-10-06 (R8).
- **Every refusal has a distinct reason code** and is stored on the decision row (Task 7) for the scoreboard (Plan 5).
- **Journal migrations: 54, 55, 56** (P5's range 50–59 has 54–59 free; coordinated with Plan 5, which holds 60–69, and Plan 4, which holds 70–79). Task 4 adds the SP1 split to the `schema_migrations.py` docstring. All tables live in `trader.journal_db` (trader-owned; no AI container mounts it).
- `CommandReceipt` stays frozen. `ExecutionIntent` is not reused for AI decisions (Task 7 defines `AiPaperEntryOrder`). `OUTCOME_UNKNOWN` is never resubmitted under a fresh id.
- Test-first. Single files: `.venv/bin/python -m pytest <path> -q --timeout=30`. Full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects: `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order or deploy is authorized by this plan.

## Rulings (spec silent, ambiguous, or contradicted by the code)

Binding for this plan. Owner-visible ones are repeated under "Open questions".

- **R1. The dispatch re-check gap is bug #47, not this plan (owner decision).** `DispatchGuard.revalidate` re-checks gross only when `approved.allocation is not None` (`dispatch_guard.py:222-226`), and nothing in `trader/` builds `AllocationDispatchEvidence`, so a ceiling tightened after approval is never enforced. Issue #47 fixes it in a small PR against master: the protective saga attaches the allocation evidence, the tighter ceiling wins at dispatch, an order above it is `LIMIT_TIGHTENED_BEFORE_DISPATCH`, with its own regression test. **Plan 3 starts only after #47 is merged.** Task 2 adds only the `ai_paper` parts on top of it and the R2 rename; if #47 lands with other names, Task 2 uses them and does not reimplement the fix.
- **R2. The "current looser than approved" branch keeps refusing, renamed (owner answer).** `revalidate_dispatch` today refuses when the current ceiling is *looser* than the approved one, under the misleading code `ALLOCATION_CEILING_TIGHTENED` (`allocation_policy.py:321-322`; no test asserts it; grep finds no other producer or consumer in `trader/`, `web/`, `scripts/` or `tests/`). The refusal stays (stricter than "the tighter of the two") and its code becomes `ALLOCATION_CEILING_CHANGED`. Task 2 renames it and greps for every consumer in the same commit. Only the tightened branch changes its rule: tighter wins, and an order above it is `LIMIT_TIGHTENED_BEFORE_DISPATCH`.
- **R3. One gross source.** `RiskLimits.gross_fraction` becomes an extra candidate (`"risk_limits"`) in `resolve_effective_gross_ceiling`, used at admission and at dispatch. The old-path `allocation_factory` clamp becomes `min(PAPER_LIMITS.gross_fraction, artifact)`. Both give 0.06 today; the parity test pins it.
- **R4. `evaluate` takes limits through the session state.** `AutomationSessionState` gains a required `limits: RiskLimits` and an optional `daily_loss_anchor`. `SessionRiskController.evaluate` reads them there, so the saga (which passes the session state) needs no new argument. `session_risk`'s module constants stay as aliases of the `PAPER_LIMITS` values (`trader/simulation/live_rules.py:13-18` imports them). The unused duplicates in `allocation_policy.py:22-25` are deleted.
- **R5. `PortfolioRiskBudget.evaluate(..., limits)`.** Its own 0.50% / 3 positions (`portfolio_risk_budget.py:21-22`) and the constructor arguments go away; its `>` vs `>=` comparisons stay as they are.
- **R6. Trading filter (owner answer 4; replaces the earlier ruling).** `trading_filters.yaml` applies to every AI `ENTER`. Spec 5.4 lists the filter among the coordinator checks, but the typed automated-entry path does not check it today (only `sdk.py:2679-2718` and the legacy executioner do), so this plan adds it for `ai_paper` only. The input is the resolved instrument, never the decision: `trader.universe_accessor.resolve_symbol(conid)` must return exactly one row with that `conId` (else `INSTRUMENT_UNRESOLVED`), and `TradingFilter.is_allowed(symbol=row.symbol, exchange=row.primaryExchange, sec_type=row.secType, price=entry price)` decides (`TRADING_FILTER_DENIED`, reason stored). `primaryExchange` is the resolved contract's exact listing exchange; `SMART` is a route, not an exchange, and there is never a fallback to it (same rule as the existing precedent `trader/trading/proposal_command_service.py`, which passes `primaryExchange`). A blank `primaryExchange` refuses `INSTRUMENT_UNRESOLVED`, because `TradingFilter.is_allowed` skips every exchange rule when the string is empty (`trader/trading/trading_filter.py`), so a missing listing exchange would otherwise read as "allowed". An exchange allowlist that names only `SMART` therefore denies every AI entry; that is the correct failure. The same check runs in the dispatch gate (R25). The file is read fresh (`TradingFilter.load()`, mtime-cached) at approval and again in the dispatch gate (R25), so an owner edit reaches a pending dispatch; a file that fails to parse refuses `TRADING_FILTER_UNAVAILABLE`. Reductions never consult the filter: a broker-proven close of a symbol that was denylisted after entry still goes. The old path is unchanged. `session_risk`'s `CONID_NOT_PERMITTED` against the deployment's conids still runs.
- **R7. Session start and the equity anchor (owner answer 1).** A session starts on the first decision of an XNYS session date (ET), not on a scheduled roll at the open; its identity is that session date. Only decision admission calls `ensure_session`; `publish` never creates a session row (it updates the effective limits of an existing one). `ensure_session` is insert-if-absent per `(account, session_date)` and commits in its **own** transaction before admission continues, so the anchor and the (empty) breach latch are durable before the decision is admitted; a crash after it leaves the row. A restart finds the row and never starts a second session for the same date. The row freezes the owner ceiling in force and the anchor: `net_liquidation − daily_pnl` of the fenced broker snapshot at that moment, i.e. the start-of-day net liquidation even if the first decision comes at noon.
- **R8. First policy of a session (owner answer 2; spec amended by owner 2026-10-06).** While the session has no effective policy, the first valid published revision applies at once, capped by the session's ceiling (cause `first_policy`, or `session_start` when it was published before the first decision): otherwise "no accepted policy, no entry" would block the first entry. After that, a tighter field applies at once and a looser field waits for the next session.
- **R9. Ceiling changes on restart.** A ceiling field lowered in `trader.yaml` applies at the next `ensure_session` (a new effective revision, cause `ceiling_tighter`). A raised field waits for the next session (the session row keeps the old ceiling).
- **R10. Breach latch.** A `DAILY_LOSS`, `DRAWDOWN` or `PORTFOLIO_DAILY_LOSS` refusal of an `ENTER` (from `session_risk`) latches the session row (first breach wins). While latched, every `ENTER` is refused `RISK_LATCHED` before any broker read. The latch is checked only when a decision arrives (spec 3).
- **R11. Sizing order and margin (owner answer 3).** Margin what-if and liquidity both need a quantity. The factory computes the maximum from the pure limits (trade risk over stop distance, position fraction, remaining gross including working entries, ADV cap, top-of-book depth, and the attested-notional bound), picks the quantity, then captures `capture_approval_context` with it. The what-if is a pass/fail on that quantity (`DispatchGuard`'s leverage check), not a cap search. An AI entry refuses a missing or timed-out estimate (`MARGIN_UNAVAILABLE`) and an invalid one (non-finite or negative `initMarginAfter` / `equityWithLoanAfter`: `MARGIN_INVALID`), both at approval and again at final dispatch (R25). Today `DispatchGuard` only warns on paper (`dispatch_guard.py:187-220`, `WHAT_IF_UNAVAILABLE_PAPER`); the refusal is an explicit `ai_paper` exception keyed by action, and the old path keeps the warning. Sizing never names margin as a bound: `PreparedEntry.margin_checked` is `True` only after a valid what-if passed, and no field, log or receipt says margin limited the size. Sizing uses the entry limit price `ask × (1 + offset)`, not the ask.
- **R12. Attested notional in the maximum.** The spec makes `evidence_order_notional` a refusal, not a cap. Without a cap, an `ENTER` with no `quantity` (which uses the maximum) would often be refused. So the maximum includes `evidence_order_notional × (1 + LIVE_NOTIONAL_TOLERANCE) / price`. A supplied quantity above that bound is refused `ORDER_EXCEEDS_ATTESTED_NOTIONAL` (checked before `QUANTITY_ABOVE_MAXIMUM`, so the code matches the old path).
- **R13. Acknowledged entries and duplicates (owner answer).** An entry is **acknowledged** when its outcome is known (ledger `SUBMITTED`, not `OUTCOME_UNKNOWN`) **and** its working-order identity is known: the fenced broker snapshot shows an order whose `order_group_id` is the entry's deterministic `og-aip-<decision_id>`, or a position or fill from it. A new `ENTER` on a conid is refused:
  - `OUTCOME_UNKNOWN_PENDING` while an `ai_paper` decision command on that conid is `RECEIVED`, `VALIDATED`, `SUBMITTING` or `OUTCOME_UNKNOWN`, or is `SUBMITTED` but not yet acknowledged (identity not visible);
  - `ENTRY_ALREADY_WORKING` while an acknowledged entry on that conid still has a working entry leg (`is_pending_entry`).
  An acknowledged unfilled entry also counts toward `max_pending_entry_orders`, `max_positions` and gross exposure (its remaining quantity × limit price), through `pending_entry_refusal` and the gross input of sizing. After it fills, a later `ENTER` may add to the position within the limits. The step-6 check and the `RECEIVED → VALIDATED` transition run in one journal transaction, so two concurrent decisions cannot both pass it. Closes keep the old rule: a close rests in `OUTCOME_UNKNOWN` (`CLOSE_PENDING`) until the reconciler resolves it from its root. An `ENTER` stuck in `OUTCOME_UNKNOWN` blocks its conid until an operator resolves it.
- **R14. Other producers' closes.** `OUTCOME_UNKNOWN_PENDING` covers `ai_paper` decisions only. A close owned by another producer (time exit, session flatten, kill) follows the exit-owner rules of Plan 1: a `CLOSE` joins, a `PARTIAL_CLOSE` gets `EXIT_IN_PROGRESS`, an `ENTER` gets `EXIT_IN_PROGRESS`.
- **R15. `KILLED`.** A reduction while `KILLED` is sent as a full-close join (quantity ignored). If the account owner (the kill flatten) is already claimed, `LiquidationService.start(scope="conid")` returns `JOINED_FLATTEN` and creates no run. If it is not claimed yet, the decision is refused `KILL_FLATTEN_PENDING` (retryable) instead of claiming a new scoped root.
- **R16. Reductions carry no attribution fields.** For `CLOSE` / `PARTIAL_CLOSE`, `deployment_digest` and `policy_revision` must be `null` (`DECISION_INVALID` otherwise). The scoreboard attributes a close through its round trip's entry (spec 5.2). `stop_price` / `target_price` are allowed only on `PARTIAL_CLOSE` (re-protection, spec 5.1).
- **R17. Decision shape.** `decision_id` matches `^[A-Za-z0-9_-]{8,64}$`; `command_id = "aip-" + decision_id`. `quantity` is a JSON integer ≥ 1. `expires_at` is an ISO-8601 string with an offset, later than now and at most 15 minutes ahead (`DECISION_EXPIRY_TOO_FAR`; owner answer 5), and it is checked again in the dispatch gate (R25, `DECISION_EXPIRED`). `decider` matches `^[a-z][a-z0-9_.-]{0,63}$` (opaque; no provider is named or checked). `evidence_digest` matches `^sha256:[0-9a-f]{64}$` (opaque in SP1).
- **R18. The trader owns the order type (owner answer 5).** AI entries are a `DAY` `MARKETABLE_LIMIT` at most 10 bps through a fresh executable quote, an `STP` stop, a target `LMT` when given: the constant `AI_ENTRY_POLICY`, never a decision field. The decision's exact key set has no order-type, offset or TIF field, so a body that asks for `MARKET` or a wider offset is `DECISION_INVALID`. At dispatch the gate (R25) refuses `ENTRY_LIMIT_THROUGH_QUOTE` when the planned limit (`compute_entry_limit` on the approval ask) is above the guard's fresh ask × 1.001. `DAY` does not replace the session controller: an unfilled AI entry is cancelled at the entry cutoff itself (R26, Task 10) and the position is still flattened by the close (`SessionController.run_due` step 3); Task 10 pins both.
- **R19. Deployment record and seal; the strategy digest is a claim (owner answer).** Registration validates and seals in one insert; there is no unsealed row. Digest: `"sha256:" + sha256(b"mmr.ai-deployment.v1\x00" + canonical_json_bytes(record))`, conids sorted. Every read recomputes the digest (`DEPLOYMENT_TAMPERED` on mismatch). An unknown digest is `DEPLOYMENT_NOT_SEALED`. Re-registering the same content returns the same digest. `strategy_digest` is recorded as a **claim** by `ai_research`, not as a verified code binding: nothing hashes a file in SP1. Every view of it says so: the stored row has `strategy_digest_provenance = 'CLAIMED_NOT_VERIFIED'` (a constant column), `get_ai_deployment` returns it next to the record, and `DecisionLink.strategy_digest_provenance` carries it to the scoreboard. Follow-up for SP2 (listed in AGENTS.md by Task 9): the runner must hash the actual loaded file and refuse a mismatch before any generated code can trade.
- **R20. Experiments are a port.** `ExperimentStatePort.current(account_id) -> ExperimentView | None` (`ARMED`, `PAUSED`, `KILLED`, `STOPPED`). Production wiring in this plan is `NoExperiment` (always `None`), so every decision is refused `NO_EXPERIMENT` until Plan 4. The drawdown high-water mark uses `CanaryRiskStore` with strategy id `ai_paper:<experiment_id>`.
- **R21. Styles in SP1.** Config accepts only `intraday_long`; any other name fails load ("not supported until the style phases"). `ENTER` side must be `BUY`.
- **R22. `ai_paper` config block (owner answer: no environment overrides).** Plan 3 parses `enabled`, `styles`, `limits_ceiling`, `experiment_kill_drawdown_pct`, `experiment_kill_basis` and `broker_outage_pause_seconds` (Plan 4 K23: a JSON integer 60–3600, default 300) and `acceptance_probe` (Plan 6 ruling 23: a JSON bool, default `false`, paper only), and leaves `telegram` (Plan 5) unparsed; any other key fails load. `ai_paper.*` authority comes only from the validated config file. At load, an environment variable whose name starts with `AI_PAPER` or `MMR_AI_PAPER` fails load with `AiPaperConfigError("environment override refused: <NAME>")`. A variable with the bare upper-case name of a parsed key or ceiling field (`ENABLED`, `STYLES`, `LIMITS_CEILING`, `EXPERIMENT_KILL_DRAWDOWN_PCT`, `EXPERIMENT_KILL_BASIS`, `BROKER_OUTAGE_PAUSE_SECONDS`, `ACCEPTANCE_PROBE`, `GROSS_FRACTION`, …) is ignored loudly: one WARNING naming it ("ignored: ai_paper settings come only from trader.yaml"), and the loaded config is unchanged. Refusing a bare name would stop the trader on an unrelated variable in a shell or compose file. The Container's env-over-YAML rule never reaches this block, because nothing resolves it through `Container.resolve`. `trader.ai_paper_config` is the typed `AiPaperConfig`; `AiPaperConfig.raw_section` is a read-only copy of the YAML mapping for Plans 4–5.
- **R23. Who reads what (owner answer 6).** Rights in the new family are granted method by method as explicit sets, never through a Plan 2 group alias, and reads and mutations are separate entries. `get_ai_risk_policy`: `cli`, `dashboard`, `ai_supervisor`. `get_ai_deployment`: `cli`, `dashboard`, `ai_supervisor`, `ai_research` (registration readback). `ai_research` gets `register_ai_deployment` and `get_ai_deployment` only from this family. `strategy` and `scheduler` (not a principal since Plan 2) get none. Plans 4 and 5 follow the same rule for the experiment and scoreboard methods.
- **R24. Attribution read for Plan 5.** `AiPaperDecisionStore.links_for_order_ref(order_ref) -> tuple[DecisionLink, ...]` (Task 7). `DecisionLink(decision_id, action, decider, strategy_version, strategy_digest_provenance, policy_revision, effective_revision, style, digest)`: `strategy_version` is the deployment's `strategy_digest`, `policy_revision` the published revision the effective limits came from, `digest` the deployment digest. Closes return links with `None` deployment fields (R16).
- **R25. AI dispatch gate.** `DispatchGuard` gets `ai_entry_gate: Callable[[CommandRequest, ApprovalContext, Quote, datetime], Optional[str]] = lambda *a: None` and `strict_margin_actions: frozenset[str] = frozenset()`. The stack wires both for `AI_PAPER_ACTION` only. The gate checks, in order: decision expiry (`DECISION_EXPIRED`), the trading filter on the resolved instrument (R6), and the limit through the fresh ask (R18). For a strict-margin action a missing, raising or timed-out what-if is `MARGIN_UNAVAILABLE` and an invalid one `MARGIN_INVALID` (R11). The old path gets the defaults, so its behaviour is byte-identical (a parity test pins it). Plan 4 adds its `experiment_gate` beside this one.
- **R26. AI entries are cancelled at the entry cutoff (owner decision).** The calendar stops new entries 30 minutes before the close but cancels working entries only 25 minutes before (`calendar_policy.py:16-17`, `entry_cutoff_utc` 15:30 and `cancel_entries_utc` 15:35 on a normal day). For `ai_paper`, the unfilled rest of every working AI entry (`og-aip-*`, leg `entry`, not external) is cancelled at `entry_cutoff_utc`. The 15:35 session cancel stays for the old path and as a backstop. Three rules, each with a test (Task 10):
  - **Cancel at the cutoff.** `SessionController.run_due` step 1 calls an `on_entry_cutoff(state, now)` hook; `AiEntryCutoff` sends the cancels with deterministic child ids `{cancel_root}-aip-{order_entity_id}` and records `cutoff_cancel_state = 'ISSUED'` on the `ai_paper_sessions` row (migration 54 column).
  - **Ambiguous cancel is reconciled, never guessed.** A cancel that raised, or an entry still `PendingCancel` / working on a broker generation newer than the cancel, is `AMBIGUOUS` (incident `ai_entry_cancel_ambiguous`). Each later tick re-reads the fenced snapshot: entry gone or `Cancelled` → `DONE`; filled → `DONE` and the protection rule below; still working on a newer generation → the same cancel again (an entry cancel is idempotent at the broker, `SessionCancelAdapter` docstring). No new entry order and no reduce comes from this path; the 15:45 flatten takes over anything left.
  - **Protection stays sized to a partial fill.** For a cancelled entry with a fill, if its working stop or target is larger than the position, the conid is **re-protected** to the position (not closed, unlike Plan 1's `_close_oversized_protection` for the old path): a Plan 1 scoped run with the new goal `reprotect` (`REQUESTED → CANCELLING → VERIFYING → REPROTECTING → VERIFYING → DONE`, no `REDUCING`), the original stop and target prices, quantity from the broker, Plan 1's exit-only OCA refs. A failed or late re-protect escalates as Plan 1 does: full close of that conid and the breaker. If IB already shrank the children to the fill (Plan 1 left this unproven; Plan 6 observes it), nothing is sent.


## Review Focus

Inputs and failure modes the spec implies but its test list does not name. Each has a named test.

1. **A decision whose `expires_at` has no offset or lies hours ahead.** Expect `DECISION_INVALID` / `DECISION_EXPIRY_TOO_FAR`, never a long-lived order authority. → Task 7 `test_expiry_must_be_aware_and_near`.
2. **The broker state moves between sizing and the approval capture** (a fill on another conid raises gross). Expect the re-check on the captured snapshot to refuse `QUANTITY_ABOVE_MAXIMUM`, not to send the stale size. → Task 6 `test_size_is_rechecked_on_the_captured_snapshot`.
3. **Two `ensure_session` calls race on the first decision of the day.** Expect one session row and one anchor. → Task 4 `test_concurrent_session_start_creates_one_row`.
4. **A deployment row edited in the database after sealing.** Expect `DEPLOYMENT_TAMPERED` on the next decision, not a silently different strategy. → Task 5 `test_edited_row_is_tampered`.
5. **An unknown leg or an external working order on the account.** Expect it to count as a pending entry (fail safe), so `MAX_PENDING_ENTRIES` cannot be dodged. → Task 6 `test_unknown_and_external_orders_count_as_pending_entries`.

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/automation/risk_limits.py` (new) | `RiskLimits`, `PAPER_LIMITS`, `STEADY_LIMITS`, constants | 1 |
| `trader/automation/session_risk.py` | limits from the session state; anchor formula | 1 |
| `trader/promotion/portfolio_risk_budget.py` | `evaluate(..., limits)` | 1 |
| `trader/promotion/allocation_policy.py` | `risk_limits` candidate; tighter-of at dispatch | 1, 2 |
| `trader/automation/production_evidence.py` | `PAPER_LIMITS` in session state and clamp; helpers made reusable | 1, 6 |
| `trader/trading/dispatch_guard.py` | `AUTOMATED_ENTRY_ACTIONS`, current-limits provider, entry-limits re-check | 2, 6 |
| `trader/automation/ai_paper_config.py` (new), `trader/config.py`, `config_defaults/trader.yaml`, `trader/trader_service.py` | owner ceiling config | 3 |
| `trader/automation/ai_risk_policy.py` (new), `trader/data/schema_migrations.py` | policy store, timing, session, latch (migration 54) | 4 |
| `trader/automation/ai_deployments.py` (new) | deployment record, seal, store (migration 55) | 5 |
| `trader/automation/ai_paper_sizing.py` (new), `trader/automation/ai_paper_evidence.py` (new), `trader/automation/ai_paper_filter.py` (new), `trader/automation/liquidity_policy.py`, `trader/trading/approval_context.py`, `trader/trading/dispatch_guard.py` | sizing, pending entries, trading filter, evidence factory, AI dispatch gate (R25) | 6 |
| `trader/automation/ai_paper_experiment.py` (new), `trader/automation/ai_paper_decision.py` (new), `trader/automation/entry_views.py` (new), `trader/automation/command_steps.py` (new) | decision model, store (migration 56), entry path | 7 |
| `trader/automation/reduction_close.py` (new), `trader/automation/automated_intent_command.py`, `trader/trading/command_coordinator.py` | shared broker-proven close; reductions; reconciler | 8 |
| `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/command_stack.py`, `AGENTS.md` | RPC methods, ACL, composition, docs | 9 |
| `trader/automation/ai_entry_cutoff.py` (new), `trader/automation/session_controller.py`, `trader/trading/liquidation_service.py` | cancel AI entries at the cutoff, reconcile an ambiguous cancel, re-protect a partial fill (R26) | 10 |

---

### Task 1: `RiskLimits` as data, with old-path parity

**Files:**
- Create: `trader/automation/risk_limits.py`
- Modify: `trader/automation/session_risk.py:37-43` (constants), `:66-71` (`AutomationSessionState`), `:177-394` (`evaluate`); `trader/promotion/portfolio_risk_budget.py:21-22,60-116`; `trader/promotion/allocation_policy.py:22-25` (delete), `:86-136` (`resolve_effective_gross_ceiling`), `:207-285` (`evaluate`); `trader/automation/production_evidence.py:157-160,173`; `scripts/scaling_fault_drill.py:48`
- Test: `tests/automation/test_risk_limits.py` (new), `tests/automation/test_session_risk_parity.py` (new); update `tests/automation/test_session_risk.py:196-206` (`make_session` gains `limits=PAPER_LIMITS`), `tests/scaling/test_portfolio_risk_budget.py`, `tests/integration/test_scaling_vertical_slice.py:121`

**Interfaces:**
- Produces (`risk_limits.py`):
  - Constants `MAX_POSITIONS = 3`, `MAX_POSITION_FRACTION = 0.05`, `MAX_TRADE_RISK_FRACTION = 0.002`, `MAX_DAILY_LOSS_FRACTION = 0.005`, `MAX_DRAWDOWN_FRACTION = 0.03` (moved from `session_risk.py:38-43`), `PAPER_GROSS_FRACTION = 0.06` (moved from `production_evidence.py:173`), `MAX_PENDING_ENTRY_ORDERS = 3`.
  - `class RiskLimitsError(ValueError)` with `.code: str` and `.fields: tuple[str, ...]`.
  - `@dataclass(frozen=True) class RiskLimits: max_positions: int; position_fraction: float; gross_fraction: float; trade_risk_fraction: float; daily_loss_fraction: float; drawdown_fraction: float; max_pending_entry_orders: int`, with `FIELDS: ClassVar[tuple[str, ...]]`, `tighter(other) -> RiskLimits`, `fields_above(other) -> tuple[str, ...]`, `structural_problems() -> tuple[str, ...]`, `to_json() -> dict`, `from_json(value: object) -> RiskLimits` (exact keys).
  - `PAPER_LIMITS`, `STEADY_LIMITS = replace(PAPER_LIMITS, gross_fraction=STEADY_MAX_GROSS_FRACTION)`.
  - `session_risk.AutomationSessionState(high_water_mark, expected_account_id, limits: RiskLimits, liquidity=None, opening_stabilization=5 min, daily_loss_anchor: Optional[float] = None)`.
  - `PortfolioRiskBudget().evaluate(intents, broker_snapshot, authorities, *, limits: RiskLimits, portfolio_authority_present=False, strategy_count=1)`.
  - `resolve_effective_gross_ceiling(..., risk_limits_gross: Optional[float] = None)`; `AllocationPolicy.evaluate(..., risk_limits_gross: Optional[float] = None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_risk_limits.py
from dataclasses import replace
import math, pytest
from trader.automation.risk_limits import PAPER_LIMITS, STEADY_LIMITS, RiskLimits, RiskLimitsError
from trader.promotion.allocation_policy import STEADY_MAX_GROSS_FRACTION

def test_paper_limits_are_what_runs_today():
    assert PAPER_LIMITS.to_json() == {"max_positions": 3, "position_fraction": 0.05, "gross_fraction": 0.06,
        "trade_risk_fraction": 0.002, "daily_loss_fraction": 0.005, "drawdown_fraction": 0.03,
        "max_pending_entry_orders": 3}

def test_steady_limits_raise_only_gross():
    assert STEADY_LIMITS.fields_above(PAPER_LIMITS) == ("gross_fraction",)
    assert STEADY_LIMITS.gross_fraction == STEADY_MAX_GROSS_FRACTION == 0.15

@pytest.mark.parametrize("field,value", [
    ("max_positions", True), ("max_positions", 3.0), ("max_positions", "3"), ("max_positions", 0),
    ("gross_fraction", True), ("gross_fraction", "0.06"), ("gross_fraction", math.nan),
    ("gross_fraction", math.inf), ("gross_fraction", 0.0), ("gross_fraction", -0.01),
    ("max_pending_entry_orders", -1)])
def test_constructor_refuses_bad_types_and_values(field, value):
    with pytest.raises(RiskLimitsError) as exc:
        replace(PAPER_LIMITS, **{field: value})
    assert exc.value.fields == (field,)

def test_int_is_accepted_for_a_fraction_and_stored_as_float():
    assert replace(PAPER_LIMITS, gross_fraction=1).gross_fraction == 1.0

def test_tighter_is_field_wise_min():
    a = replace(PAPER_LIMITS, gross_fraction=0.10, max_positions=2)
    b = replace(PAPER_LIMITS, gross_fraction=0.04)
    assert a.tighter(b) == replace(PAPER_LIMITS, gross_fraction=0.04, max_positions=2)

@pytest.mark.parametrize("changes,problem", [
    ({"gross_fraction": 1.5}, "GROSS_ABOVE_ONE"), ({"position_fraction": 0.07}, "POSITION_ABOVE_GROSS"),
    ({"daily_loss_fraction": 1.0}, "DAILY_LOSS_NOT_BELOW_ONE"),
    ({"drawdown_fraction": 1.0}, "DRAWDOWN_NOT_BELOW_ONE")])
def test_structural_problems(changes, problem):
    assert problem in replace(PAPER_LIMITS, **changes).structural_problems()

@pytest.mark.parametrize("value", [None, [], {"max_positions": 3}, {**PAPER_LIMITS.to_json(), "extra": 1}])
def test_from_json_requires_exact_keys(value):
    with pytest.raises(RiskLimitsError):
        RiskLimits.from_json(value)
```

```python
# tests/automation/test_session_risk_parity.py — old path through evaluate (spec 6, "parity")
# Builders imported from tests/automation/test_session_risk.py (make_intent, make_artifact,
# make_approval, make_broker, make_session, make_controller).
from dataclasses import replace
from decimal import Decimal
from trader.automation.risk_limits import PAPER_LIMITS
from trader.automation.session_risk import AllocationCeiling

CASES = [  # (intent overrides, broker overrides, expected reason or None) — one row per old-path rule
    ({}, {}, None),
    ({"requested_quantity": Decimal("600")}, {}, "POSITION_PCT"),            # 6% > 5% position
    ({"stop_policy": StopPolicy(Decimal("90"), "STP"), "requested_quantity": Decimal("300")}, {}, "TRADE_RISK"),  # 3,000 = 0.30%
    ({}, {"daily_pnl": -5_000.0}, "DAILY_LOSS"),                             # 0.50% of 1M
    ({}, {"positions": three_other_positions()}, "MAX_POSITIONS"),
    ({}, {"net_liquidation": 969_000.0}, "DRAWDOWN"),                        # HWM 1M, 3.1% down
    ({"requested_quantity": Decimal("400")}, {}, "ORDER_EXCEEDS_ATTESTED_NOTIONAL"),   # 40,000 > 30,000 × 1.05
]

@pytest.mark.parametrize("intent_kw,broker_kw,expected", CASES)
def test_old_path_decisions_are_unchanged_with_paper_limits(intent_kw, broker_kw, expected):
    decision = make_controller().evaluate(make_intent(**intent_kw), make_artifact(),
        make_approval(broker=make_broker(**broker_kw)), make_session(limits=PAPER_LIMITS),
        AllocationCeiling(max_gross_fraction=min(PAPER_LIMITS.gross_fraction, 0.15)))
    assert (expected is None and decision.approved) or expected in decision.reason_codes

def test_paper_gross_still_stops_at_six_percent():
    # 5.5% held + 1% new = 6.5% > 6%; the artifact allows 15%.
    decision = make_controller().evaluate(make_intent(requested_quantity=Decimal("100")),
        make_artifact(max_gross_allocation=0.15),
        make_approval(broker=make_broker(positions=gross_positions(55_000.0))),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(max_gross_fraction=0.06))
    assert "GROSS_EXPOSURE" in decision.reason_codes
    assert decision.effective_gross_ceiling == pytest.approx(0.06)

def test_risk_limits_gross_caps_even_a_looser_allocation_ceiling():
    decision = make_controller().evaluate(make_intent(requested_quantity=Decimal("100")),
        make_artifact(max_gross_allocation=0.15),
        make_approval(broker=make_broker(positions=gross_positions(55_000.0))),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(max_gross_fraction=0.15))
    assert "GROSS_EXPOSURE" in decision.reason_codes        # R3: the risk_limits candidate binds

def test_tighter_limits_change_the_decision():
    tight = replace(PAPER_LIMITS, position_fraction=0.005)
    decision = make_controller().evaluate(make_intent(), make_artifact(), make_approval(),
        make_session(limits=tight), AllocationCeiling(max_gross_fraction=0.06))
    assert "POSITION_PCT" in decision.reason_codes

def test_daily_loss_uses_the_anchor_only_when_given():
    broker = make_broker(net_liquidation=990_000.0, daily_pnl=-4_960.0)
    old = make_controller().evaluate(make_intent(), make_artifact(), make_approval(broker=broker),
        make_session(limits=PAPER_LIMITS), AllocationCeiling(0.06))
    assert "DAILY_LOSS" in old.reason_codes          # 4960/990000 = 0.501% (today's formula)
    anchored = make_controller().evaluate(make_intent(), make_artifact(), make_approval(broker=broker),
        make_session(limits=PAPER_LIMITS, daily_loss_anchor=1_000_000.0), AllocationCeiling(0.06))
    assert "DAILY_LOSS" not in anchored.reason_codes  # budget 5000 on the frozen anchor

def test_portfolio_budget_reads_limits(): ...     # PortfolioRiskBudget().evaluate(..., limits=replace(PAPER_LIMITS, max_positions=1)) blocks 2 positions
```

  (Builders come from `tests/automation/test_session_risk.py`; `three_other_positions()` and `gross_positions(value)` are new local helpers returning `BrokerPositionRow` tuples. Every row trips exactly one rule on a 100.00 quote and 1,000,000 equity: the `TRADE_RISK` row is 300 shares with the stop at 90.00, i.e. 3,000 of risk (0.30%) at a 3% position. `make_artifact(order_notional=30_000.0)` for every row.)

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: trader.automation.risk_limits`).

- [ ] **Step 3: Implement.**

```python
# trader/automation/risk_limits.py
"""Risk limits as data (spec 5.4). PAPER_LIMITS is what runs on the paper path today."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import ClassVar

from trader.promotion.allocation_policy import STEADY_MAX_GROSS_FRACTION

MAX_POSITIONS = 3
MAX_POSITION_FRACTION = 0.05
MAX_TRADE_RISK_FRACTION = 0.002
MAX_DAILY_LOSS_FRACTION = 0.005
MAX_DRAWDOWN_FRACTION = 0.03
PAPER_GROSS_FRACTION = 0.06
MAX_PENDING_ENTRY_ORDERS = 3

_COUNT_FIELDS = ("max_positions", "max_pending_entry_orders")


class RiskLimitsError(ValueError):
    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()):
        super().__init__(f"{code}: {message}")
        self.code, self.fields = code, fields


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int
    position_fraction: float
    gross_fraction: float
    trade_risk_fraction: float
    daily_loss_fraction: float
    drawdown_fraction: float
    max_pending_entry_orders: int

    FIELDS: ClassVar[tuple[str, ...]] = ()

    def __post_init__(self):
        for name in self.FIELDS:
            value = getattr(self, name)
            if name in _COUNT_FIELDS:
                if type(value) is not int or value < 1:
                    raise RiskLimitsError("LIMIT_INVALID", f"{name} must be an integer >= 1", (name,))
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value <= 0:
                raise RiskLimitsError("LIMIT_INVALID", f"{name} must be a finite number > 0", (name,))
            object.__setattr__(self, name, float(value))

    def tighter(self, other: "RiskLimits") -> "RiskLimits":
        return RiskLimits(**{n: min(getattr(self, n), getattr(other, n)) for n in self.FIELDS})

    def fields_above(self, other: "RiskLimits") -> tuple[str, ...]:
        return tuple(n for n in self.FIELDS if getattr(self, n) > getattr(other, n))

    def structural_problems(self) -> tuple[str, ...]:
        checks = (
            (self.gross_fraction <= 1.0, "GROSS_ABOVE_ONE"),
            (self.position_fraction <= self.gross_fraction, "POSITION_ABOVE_GROSS"),
            (self.daily_loss_fraction < 1.0, "DAILY_LOSS_NOT_BELOW_ONE"),
            (self.drawdown_fraction < 1.0, "DRAWDOWN_NOT_BELOW_ONE"),
        )
        return tuple(code for ok, code in checks if not ok)

    def to_json(self) -> dict:
        return {n: getattr(self, n) for n in self.FIELDS}

    @classmethod
    def from_json(cls, value: object) -> "RiskLimits":
        if not isinstance(value, dict) or set(value) != set(cls.FIELDS):
            raise RiskLimitsError("LIMIT_INVALID", f"limits must have exactly {cls.FIELDS}")
        return cls(**value)


RiskLimits.FIELDS = tuple(f.name for f in fields(RiskLimits))

PAPER_LIMITS = RiskLimits(
    max_positions=MAX_POSITIONS, position_fraction=MAX_POSITION_FRACTION,
    gross_fraction=PAPER_GROSS_FRACTION, trade_risk_fraction=MAX_TRADE_RISK_FRACTION,
    daily_loss_fraction=MAX_DAILY_LOSS_FRACTION, drawdown_fraction=MAX_DRAWDOWN_FRACTION,
    max_pending_entry_orders=MAX_PENDING_ENTRY_ORDERS,
)
STEADY_LIMITS = replace(PAPER_LIMITS, gross_fraction=STEADY_MAX_GROSS_FRACTION)
```

  `session_risk.py`:
  - Replace `:38-43` with `from trader.automation.risk_limits import MAX_POSITIONS, MAX_POSITION_FRACTION, MAX_TRADE_RISK_FRACTION, MAX_DAILY_LOSS_FRACTION, MAX_DRAWDOWN_FRACTION, RiskLimits` (aliases kept for `live_rules.py`); keep `MAX_GROSS_FRACTION = STEADY_MAX_GROSS_FRACTION`.
  - `AutomationSessionState`: insert `limits: RiskLimits` after `expected_account_id`, append `daily_loss_anchor: Optional[float] = None`.
  - In `evaluate`, `limits = session_state.limits` and use it for every hard-coded limit:

```python
        if _finite(equity) and equity > 0:
            loss = max(0.0, -float(broker.daily_pnl))
            anchor = session_state.daily_loss_anchor
            breached = (loss >= anchor * limits.daily_loss_fraction if anchor is not None
                        else loss / equity >= limits.daily_loss_fraction)
            if breached:
                reasons.append("DAILY_LOSS")
                ...                                              # signal unchanged
            ...
                if drawdown >= limits.drawdown_fraction:
```

    `MAX_POSITIONS` → `limits.max_positions` (:284), `MAX_POSITION_FRACTION` → `limits.position_fraction` (:367), `MAX_TRADE_RISK_FRACTION` → `limits.trade_risk_fraction` (:383-385). An anchor that is not finite and > 0 appends `DAILY_LOSS_ANCHOR_INVALID` (fail closed). Pass `risk_limits_gross=limits.gross_fraction` to `self._allocation_policy.evaluate` (:299) and `limits=limits` to `self._portfolio_risk_budget.evaluate` (:342).
  - `allocation_policy.py`: delete `:22-25`. `resolve_effective_gross_ceiling` appends `AllocationLimitCandidate("risk_limits", float(risk_limits_gross))` when the argument is not `None`; `evaluate` and (Task 2) `revalidate_dispatch` forward it.
  - `portfolio_risk_budget.py`: delete `ACCOUNT_DAILY_LOSS_LIMIT`, `MAX_POSITION_COUNT` (and from `__all__`) and the constructor parameters; `evaluate` compares with `limits.max_positions` and `limits.daily_loss_fraction`. Update `scripts/scaling_fault_drill.py:48` and the two test files to pass `limits=PAPER_LIMITS` (the test that imported `ACCOUNT_DAILY_LOSS_LIMIT` uses `PAPER_LIMITS.daily_loss_fraction`).
  - `production_evidence.py`: `session_state_factory` passes `limits=PAPER_LIMITS`; `allocation_factory` returns `AllocationCeiling(max_gross_fraction=min(PAPER_LIMITS.gross_fraction, ceiling))`.

- [ ] **Step 4: Run** the two new files, `tests/automation/`, `tests/scaling/`, `tests/integration/test_scaling_vertical_slice.py`, `tests/test_live_rules.py`, then the full suite.
- [ ] **Step 5: Commit** — `refactor: make risk limits data and pass them to session risk`.

---

### Task 2: `ai_paper` parts of the dispatch re-check (on top of #47)

**Precondition:** issue #47 is merged to master. Check before starting: `git log origin/master --oneline | grep -i "#47"` finds the fix, and `grep -n "LIMIT_TIGHTENED_BEFORE_DISPATCH" trader/promotion/allocation_policy.py` and `grep -n "AllocationDispatchEvidence(" trader/automation/protective_order_saga.py` both find it. If any is missing, stop and report; do not fold the fix into this plan (R1).

**Files:**
- Modify: `trader/promotion/allocation_policy.py` (`revalidate_dispatch`: the `risk_limits_gross` argument and the R2 rename), `trader/trading/dispatch_guard.py` (constructor `current_limits`; `AUTOMATED_ENTRY_ACTIONS`; the allocation branch passes `risk_limits_gross`)
- Test: `tests/scaling/test_dispatch_tightening.py` (the file #47 creates; add cases)

**Interfaces:**
- Consumes: Task 1 `RiskLimits`, `PAPER_LIMITS`, `risk_limits_gross`; #47's `revalidate_dispatch` tighter-wins rule and the saga's `AllocationDispatchEvidence`.
- Produces:
  - `AllocationPolicy.revalidate_dispatch(..., risk_limits_gross: Optional[float] = None)` (forwarded to `resolve_effective_gross_ceiling`, R3); reason `ALLOCATION_CEILING_CHANGED` replaces `ALLOCATION_CEILING_TIGHTENED` (R2).
  - `dispatch_guard.AUTOMATED_ENTRY_ACTIONS = frozenset({"execute_automated_intent", "submit_ai_paper_decision"})`.
  - `DispatchGuard(..., current_limits: Callable[[Any], RiskLimits] = lambda request: PAPER_LIMITS)` — called with the `CommandRequest`; the default keeps every existing construction on the old-path constant. Task 9 passes a router. A raising provider refuses `LIMITS_UNAVAILABLE`.

- [ ] **Step 1: Write the failing tests** (`guard_parts` and the builders come from #47's test file and `tests/test_dispatch_guard.py`)

```python
def test_current_risk_limits_tighten_the_dispatch_ceiling(guard_parts):
    guard = DispatchGuard(**guard_parts, current_limits=lambda request: replace(PAPER_LIMITS, gross_fraction=0.03))
    approval = replace(paper_entry_approval(quantity=500, price=100.0), allocation=evidence(0.06))   # 5% gross
    with pytest.raises(DispatchGuardError) as exc:
        guard.revalidate(approval, automated_request(), NOW)
    assert exc.value.code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"

def test_unchanged_limits_still_dispatch(guard_parts):
    guard = DispatchGuard(**guard_parts, current_limits=lambda request: PAPER_LIMITS)
    guard.revalidate(replace(paper_entry_approval(quantity=500, price=100.0), allocation=evidence(0.06)), automated_request(), NOW)

def test_looser_current_ceiling_keeps_its_refusal_under_the_new_name():         # R2
    decision = AllocationPolicy(now=lambda: NOW).revalidate_dispatch(..., risk_limits_gross=0.10,
        artifact_max_gross=0.15, effective_gross_ceiling=0.06, quantity=100.0, entry_price=100.0)
    assert "ALLOCATION_CEILING_CHANGED" in decision.reason_codes
    assert "ALLOCATION_CEILING_TIGHTENED" not in decision.reason_codes

def test_limits_provider_failure_refuses(guard_parts):
    def boom(request): raise RuntimeError("store down")
    guard = DispatchGuard(**guard_parts, current_limits=boom)
    with pytest.raises(DispatchGuardError, match="LIMITS_UNAVAILABLE"):
        guard.revalidate(replace(paper_entry_approval(), allocation=evidence(0.06)), automated_request(), NOW)

def test_ai_paper_action_gets_the_automated_quote_rules(guard_parts):
    guard = DispatchGuard(**guard_parts_with_delayed_quote(), current_limits=lambda r: PAPER_LIMITS)
    with pytest.raises(DispatchGuardError, match="FEED_NOT_LIVE"):
        guard.revalidate(paper_entry_approval(), automated_request(action="submit_ai_paper_decision"), NOW)
```

- [ ] **Step 2: Run, expect FAIL** (`current_limits` and the new code do not exist yet).
- [ ] **Step 3: Implement.** In `revalidate_dispatch`, rename the looser-ceiling reason to `ALLOCATION_CEILING_CHANGED` and forward `risk_limits_gross` to `resolve_effective_gross_ceiling`. In `dispatch_guard.py` add the constant; the automated-quote check becomes `getattr(request, "action", None) in AUTOMATED_ENTRY_ACTIONS`; in the allocation branch, before `revalidate_dispatch`:

```python
            try:
                limits_now = self._current_limits(request)
            except Exception as exc:
                raise DispatchGuardError("LIMITS_UNAVAILABLE", "current risk limits unavailable") from exc
            decision = self._allocation_policy.revalidate_dispatch(
                ..., risk_limits_gross=limits_now.gross_fraction)
```

  Check by reading `resolve_effective_gross_ceiling` that on the paper path the dispatch-time effective (steady, artifact, `risk_limits` 0.06) equals the approved one, so the R2 branch cannot fire on an unchanged old-path entry; `test_unchanged_limits_still_dispatch` pins it. Rename: `grep -rn "ALLOCATION_CEILING_TIGHTENED" trader web scripts tests docs AGENTS.md` must print nothing after this task (on master today only `allocation_policy.py:322` produces it and nothing consumes it); any test #47 added for that branch moves to the new name in this commit.

- [ ] **Step 4: Run** the file, `tests/scaling/`, `tests/automation/`, `tests/test_dispatch_guard.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: feed current risk limits into the dispatch re-check`. Body: builds on #47; R2 rename to `ALLOCATION_CEILING_CHANGED`; old path unchanged except the code name.

---

### Task 3: Owner ceiling config (`ai_paper` block)

**Files:**
- Create: `trader/automation/ai_paper_config.py`
- Modify: `trader/config.py:128-140` (`MMRConfig.ai_paper`), `:225` (`from_yaml`, after the automation block at :308-312); `config_defaults/trader.yaml` (after the `automation:` block at :145-149); `trader/trader_service.py:438-439` (set `trader.ai_paper_config` after `container.resolve(Trader)`)
- Test: `tests/automation/test_ai_paper_config.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `AiPaperConfigError(ValueError)` (message names the key path); `SUPPORTED_STYLES = frozenset({"intraday_long"})`; `@dataclass(frozen=True) class AiPaperConfig: enabled: bool = False; styles: tuple[str, ...] = ("intraday_long",); limits_ceiling: RiskLimits = PAPER_LIMITS; experiment_kill_drawdown_pct: Optional[float] = None; experiment_kill_basis: Literal["start", "peak"] = "start"; broker_outage_pause_seconds: int = 300; acceptance_probe: bool = False; raw_section: Mapping[str, Any] = MappingProxyType({})`; `load_ai_paper_config(raw: object, *, trading_mode: str, code_maximum: RiskLimits = STEADY_LIMITS) -> AiPaperConfig`. `MMRConfig.ai_paper: AiPaperConfig`; `trader.ai_paper_config: AiPaperConfig`.

- [ ] **Step 1: Write the failing tests**

```python
def load(section, mode="paper", **kw):
    return load_ai_paper_config(section, trading_mode=mode, **kw)

def test_missing_section_and_missing_keys_take_paper_limits():
    assert load(None).limits_ceiling == PAPER_LIMITS
    assert load({"limits_ceiling": {}}).limits_ceiling == PAPER_LIMITS
    assert load({"limits_ceiling": {"max_positions": 2}}).limits_ceiling == replace(PAPER_LIMITS, max_positions=2)

@pytest.mark.parametrize("gross", [0.06, 0.10, 0.15])
def test_gross_may_rise_to_fifteen_percent(gross):
    assert load({"limits_ceiling": {"gross_fraction": gross}}).limits_ceiling.gross_fraction == gross

@pytest.mark.parametrize("key,value", [
    ("gross_fraction", 0.1501), ("position_fraction", 0.051), ("trade_risk_fraction", 0.0021),
    ("daily_loss_fraction", 0.0051), ("max_positions", 4), ("drawdown_fraction", 0.031),
    ("max_pending_entry_orders", 4)])
def test_any_other_field_above_today_fails_load(key, value):
    with pytest.raises(AiPaperConfigError, match=f"limits_ceiling.{key}"):
        load({"limits_ceiling": {key: value}})

@pytest.mark.parametrize("value", [0, -0.01, ".nan", ".inf", "0.05", True, None, [0.05]])
def test_wrong_type_non_finite_or_non_positive_fails_load(value, tmp_path):
    raw = yaml.safe_load(f"limits_ceiling: {{position_fraction: {value if isinstance(value, str) else json.dumps(value)}}}")
    with pytest.raises(AiPaperConfigError):
        load(raw)

def test_unknown_ceiling_key_and_unknown_section_key_fail_load():
    with pytest.raises(AiPaperConfigError, match="limits_ceiling.gross"):
        load({"limits_ceiling": {"gross": 0.05}})
    with pytest.raises(AiPaperConfigError, match="ai_paper.ceiling"):
        load({"ceiling": {}})

def test_telegram_is_left_for_plan_five_and_kept_raw():
    cfg = load({"telegram": {"enabled": False}})
    assert cfg.raw_section["telegram"] == {"enabled": False}
    with pytest.raises(TypeError):
        cfg.raw_section["x"] = 1

@pytest.mark.parametrize("styles", [[], ["swing_long"], ["intraday_long", "intraday_long"], "intraday_long", [1]])
def test_styles_must_be_supported_unique_and_non_empty(styles):
    with pytest.raises(AiPaperConfigError, match="styles"):
        load({"styles": styles})

@pytest.mark.parametrize("value", ["yes", 1, None])
def test_enabled_must_be_a_bool(value):
    with pytest.raises(AiPaperConfigError, match="enabled"):
        load({"enabled": value})

def test_enabled_requires_paper_trading_mode():
    with pytest.raises(AiPaperConfigError, match="paper"):
        load({"enabled": True}, mode="live")

@pytest.mark.parametrize("pct", [0, 100, -5, ".nan", "20", True])
def test_kill_line_must_be_a_percent_strictly_between_0_and_100(pct): ...

def test_kill_basis_is_start_or_peak(): ...    # "start", "peak" load; "high" fails

RAISED = replace(STEADY_LIMITS, drawdown_fraction=0.10)   # test-only code maximum (spec 6)

@pytest.mark.parametrize("kill", [None, 12.0])
def test_drawdown_guard_fails_with_kill_off_or_looser(kill):
    with pytest.raises(AiPaperConfigError, match="experiment_kill_drawdown_pct"):
        load({"limits_ceiling": {"drawdown_fraction": 0.05}, "experiment_kill_drawdown_pct": kill},
             code_maximum=RAISED)

def test_drawdown_guard_loads_with_a_tighter_kill_line():
    assert load({"limits_ceiling": {"drawdown_fraction": 0.05}, "experiment_kill_drawdown_pct": 4.0},
                code_maximum=RAISED).experiment_kill_drawdown_pct == 4.0

@pytest.mark.parametrize("kill", [None, 20.0])
def test_todays_legal_combinations(kill):                  # spec 5.4: 3% with kill off or 20%
    load({"limits_ceiling": {"drawdown_fraction": 0.03}, "experiment_kill_drawdown_pct": kill})

@pytest.mark.parametrize("name", ["AI_PAPER_ENABLED", "MMR_AI_PAPER_LIMITS_CEILING"])
def test_prefixed_environment_override_is_refused(monkeypatch, name):          # R22, owner answer
    monkeypatch.setenv(name, "1")
    with pytest.raises(AiPaperConfigError, match=f"environment override refused: {name}"):
        load({"enabled": True})

@pytest.mark.parametrize("name", ["GROSS_FRACTION", "EXPERIMENT_KILL_DRAWDOWN_PCT", "BROKER_OUTAGE_PAUSE_SECONDS"])
def test_bare_environment_name_is_ignored_loudly(monkeypatch, caplog, name):
    monkeypatch.setenv(name, "0.15")
    monkeypatch.delenv(name); expected = load({"enabled": True}); monkeypatch.setenv(name, "0.15")
    assert load({"enabled": True}) == expected                                  # unchanged
    assert name in caplog.text and "ignored" in caplog.text

@pytest.mark.parametrize("value", [59, 3601, 300.0, True, "300", None])
def test_outage_pause_seconds_is_a_bounded_integer(value):                     # Plan 4 K23
    with pytest.raises(AiPaperConfigError, match="broker_outage_pause_seconds"):
        load({"broker_outage_pause_seconds": value})

def test_acceptance_probe_defaults_off_and_loads_true_in_paper():                 # Plan 6 ruling 23
    assert load(None).acceptance_probe is False and load({}).acceptance_probe is False
    assert load({"acceptance_probe": True}).acceptance_probe is True

@pytest.mark.parametrize("value", ["yes", 1, None])
def test_acceptance_probe_must_be_a_bool(value):
    with pytest.raises(AiPaperConfigError, match="acceptance_probe"):
        load({"acceptance_probe": value})

def test_acceptance_probe_is_refused_outside_paper_mode():
    with pytest.raises(AiPaperConfigError, match="acceptance_probe.*paper"):
        load({"acceptance_probe": True}, mode="live")

def test_acceptance_probe_true_does_not_fail_mmr_config_load(tmp_path):          # the key must not stop trader_service
    path = tmp_path / "trader.yaml"; path.write_text("trading_mode: paper\nai_paper:\n  acceptance_probe: true\n")
    assert MMRConfig.from_yaml(str(path)).ai_paper.acceptance_probe is True

def test_bad_block_fails_mmr_config_load(tmp_path):
    path = tmp_path / "trader.yaml"; path.write_text("trading_mode: paper\nai_paper:\n  limits_ceiling: {max_positions: 9}\n")
    with pytest.raises(AiPaperConfigError):
        MMRConfig.from_yaml(str(path))

def test_template_block_loads(tmp_path):
    raw = yaml.safe_load(Path("config_defaults/trader.yaml").read_text())
    assert load(raw["ai_paper"]) == AiPaperConfig(raw_section=MappingProxyType(raw["ai_paper"]))
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** One function per concern (`_refuse_env_overrides(os.environ)` first, then `_parse_enabled`, `_parse_styles`, `_parse_ceiling`, `_parse_kill`, `_parse_outage_pause`, `_parse_acceptance_probe`, `_check_drawdown_guard`), each raising `AiPaperConfigError` with the key path. `_parse_ceiling`: unknown key → error; each value first checked with the `RiskLimits` field rules by building `replace(PAPER_LIMITS, **{key: value})` and turning `RiskLimitsError` into `AiPaperConfigError(f"ai_paper.limits_ceiling.{key}: ...")`; then `ceiling.fields_above(code_maximum)` → error naming the fields; then `structural_problems()` → error. Drawdown guard:

```python
def _check_drawdown_guard(ceiling: RiskLimits, kill_pct: Optional[float]) -> None:
    if ceiling.drawdown_fraction <= PAPER_LIMITS.drawdown_fraction:
        return
    if kill_pct is None or kill_pct / 100.0 > ceiling.drawdown_fraction:
        raise AiPaperConfigError(
            "ai_paper.experiment_kill_drawdown_pct must be set and no looser than "
            f"limits_ceiling.drawdown_fraction ({ceiling.drawdown_fraction})")
```

  `raw_section = MappingProxyType(copy.deepcopy(dict(raw or {})))`. `MMRConfig.from_yaml` calls `load_ai_paper_config(raw.get("ai_paper"), trading_mode=config.trading_mode)` after `trading_mode` is final (after the flat-key loop) and stores it on `config.ai_paper`. `trader_service.main`: `trader.ai_paper_config = container.typed_config().ai_paper` right after `container.resolve(Trader, ...)` (the command stack is built later, in `Trader.connect`). Template:

```yaml
# AI paper experiment (SP1). Owner-only: no AI principal can change this block.
# limits_ceiling caps every AI risk policy. Empty = today's paper limits. Only
# gross_fraction may rise above them (up to 0.15); every value is a fraction.
ai_paper:
  enabled: false
  styles: [intraday_long]            # later: swing_long, intraday_short, swing_short
  limits_ceiling: {}
  experiment_kill_drawdown_pct: null # percent, e.g. 20; off by default
  experiment_kill_basis: start       # start | peak
  broker_outage_pause_seconds: 300   # pause the experiment after this long without broker data
  acceptance_probe: false            # paper only; true only on the SP1 acceptance day (Plan 6), then back to false
```

- [ ] **Step 4: Run, expect PASS**, then `tests/test_config.py` and the full suite.
- [ ] **Step 5: Commit** — `feat: load the ai_paper owner ceiling with strict validation`.

---

### Task 4: AI risk policy store, session timing and breach latch

**Files:**
- Create: `trader/automation/ai_risk_policy.py`
- Modify: `trader/data/schema_migrations.py` (docstring: "SP1 `ai_paper` path (Plan 3) uses **54–56** in the P5 range: 54 risk policies and sessions, 55 deployments, 56 decisions; Plan 5 owns 60–69, Plan 4 70–79.")
- Test: `tests/automation/test_ai_risk_policy.py`

**Interfaces:**
- Consumes: Task 1 `RiskLimits`; `XNYSCalendarPolicy.resolve` (`calendar_policy.py:71`); `BrokerRiskSnapshot` (`broker_state.py:260`).
- Produces:
  - `AI_RISK_POLICY_MIGRATION_VERSION = 54`; `apply_ai_risk_policy_migration(migrator)`.
  - `class PolicyRefused(Exception)` with `.code` (`POLICY_INVALID`, `POLICY_ABOVE_CEILING`, `REASON_INVALID`, `SESSION_EVIDENCE_INVALID`) and `.fields`.
  - `@dataclass(frozen=True) class PublishResult: revision: int; applied_now: tuple[str, ...]; queued: tuple[str, ...]`.
  - `@dataclass(frozen=True) class SessionView: session_date: date; anchor: float; ceiling: RiskLimits; effective: Optional[RiskLimits]; effective_revision: Optional[int]; published_revision: Optional[int]; queued: tuple[str, ...]; latch_code: Optional[str]` and property `daily_loss_budget -> Optional[float]` (`anchor × effective.daily_loss_fraction`).
  - `class AiRiskPolicyService(*, db, account_id: str, ceiling: RiskLimits, calendar: XNYSCalendarPolicy, now: Callable[[], datetime])` with `publish(limits: RiskLimits, *, reason: str, principal: str, command_id: str, broker: BrokerRiskSnapshot) -> PublishResult`, `ensure_session(broker) -> Optional[SessionView]` (None off-session; commits in its own transaction, R7; called only by decision admission), `current() -> Optional[SessionView]` (no write), `latest_published_revision() -> Optional[int]`, `latch(code: str, detail: str) -> None`, `effective_limits() -> RiskLimits` (raises `PolicyRefused("NO_EFFECTIVE_LIMITS")`; used by the dispatch provider in Task 9).

Tables (migration 54, `sp1_ai_risk_policy`):

```sql
CREATE TABLE IF NOT EXISTS ai_risk_policy_revisions (
    account_id VARCHAR NOT NULL, revision INTEGER NOT NULL, limits_json VARCHAR NOT NULL,
    reason VARCHAR NOT NULL, principal VARCHAR NOT NULL, command_id VARCHAR NOT NULL UNIQUE,
    published_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (account_id, revision));
CREATE TABLE IF NOT EXISTS ai_paper_sessions (
    account_id VARCHAR NOT NULL, session_date DATE NOT NULL,
    anchor_net_liquidation DOUBLE NOT NULL, anchor_generation_id BIGINT NOT NULL,
    ceiling_json VARCHAR NOT NULL, started_at TIMESTAMPTZ NOT NULL,
    latch_code VARCHAR, latch_detail VARCHAR, latched_at TIMESTAMPTZ,
    cutoff_cancel_state VARCHAR,   -- NULL | ISSUED | AMBIGUOUS | DONE (R26)
    PRIMARY KEY (account_id, session_date));
CREATE TABLE IF NOT EXISTS ai_effective_limits (
    account_id VARCHAR NOT NULL, session_date DATE NOT NULL, revision INTEGER NOT NULL,
    limits_json VARCHAR NOT NULL, published_revision INTEGER NOT NULL,
    cause VARCHAR NOT NULL,   -- session_start | first_policy | published_tighter | ceiling_tighter
    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (account_id, session_date, revision));
```

All three are append-only except the latch columns (set once: `UPDATE ... WHERE latch_code IS NULL`) and `cutoff_cancel_state` (forward only, R26). No method updates or deletes a revision.

- [ ] **Step 1: Write the failing tests** (real DuckDB file in `tmp_path`; a fake clock on a known XNYS session, `NOW = 2026-07-17 15:00 UTC` as in `test_session_risk.py`; `broker(nl, pnl)` builds a fenced paper `BrokerRiskSnapshot`)

```python
CEILING = replace(PAPER_LIMITS, gross_fraction=0.10)
LOOSE = replace(PAPER_LIMITS, gross_fraction=0.08)
TIGHT = replace(PAPER_LIMITS, gross_fraction=0.04)

def svc(db, now=lambda: NOW, ceiling=CEILING):
    return AiRiskPolicyService(db=db, account_id=ACCOUNT, ceiling=ceiling, calendar=XNYSCalendarPolicy(), now=now)

def publish(s, limits, cid):
    return s.publish(limits, reason="test", principal="ai_supervisor", command_id=cid, broker=broker(1_000_000, 0))

def test_policy_above_the_owner_ceiling_is_refused_not_clamped(db):
    with pytest.raises(PolicyRefused) as exc:
        publish(svc(db), replace(PAPER_LIMITS, gross_fraction=0.11), "c1")
    assert (exc.value.code, exc.value.fields) == ("POLICY_ABOVE_CEILING", ("gross_fraction",))
    assert svc(db).latest_published_revision() is None

@pytest.mark.parametrize("changes", [{"gross_fraction": 0.03, "position_fraction": 0.04}])
def test_structural_problem_is_refused(db, changes):
    with pytest.raises(PolicyRefused, match="POLICY_INVALID"):
        publish(svc(db), replace(PAPER_LIMITS, **changes), "c1")

def test_no_policy_means_no_effective_limits(db):
    view = svc(db).ensure_session(broker(1_000_000, 0))
    assert view.effective is None
    with pytest.raises(PolicyRefused, match="NO_EFFECTIVE_LIMITS"):
        svc(db).effective_limits()

def test_first_policy_of_a_session_applies_at_once(db):            # R8
    s = svc(db); s.ensure_session(broker(1_000_000, 0))
    publish(s, LOOSE, "c1")
    assert s.current().effective == LOOSE

def test_looser_field_is_queued_not_applied_not_refused(db):
    s = svc(db); publish(s, TIGHT, "c1")
    result = publish(s, LOOSE, "c2")
    assert (result.revision, result.applied_now, result.queued) == (2, (), ("gross_fraction",))
    assert s.current().effective.gross_fraction == 0.04
    assert s.current().queued == ("gross_fraction",)

def test_mixed_revision_applies_tighter_now_and_looser_next_session(db):
    clock = {"t": NOW}; s = svc(db, now=lambda: clock["t"])
    publish(s, TIGHT, "c1")
    mixed = replace(PAPER_LIMITS, gross_fraction=0.08, max_positions=2)
    assert publish(s, mixed, "c2").applied_now == ("max_positions",)
    assert s.current().effective == replace(TIGHT, max_positions=2)
    clock["t"] = NOW + dt.timedelta(days=3)                          # next XNYS session (Monday)
    assert s.ensure_session(broker(1_000_000, 0)).effective == mixed

def test_next_session_takes_latest_published_capped_by_the_ceiling(db): ...   # ceiling lowered between sessions

def test_restart_is_not_a_session_start(db):
    s = svc(db); publish(s, TIGHT, "c1"); publish(s, LOOSE, "c2")
    restarted = svc(db)
    assert restarted.ensure_session(broker(900_000, -20_000)).effective == TIGHT   # queued loosening not applied
    assert restarted.current().anchor == 1_000_000.0                               # anchor not re-frozen

def test_restart_with_a_lower_ceiling_tightens_at_once(db):                       # R9
    s = svc(db); publish(s, LOOSE, "c1")
    view = svc(db, ceiling=replace(PAPER_LIMITS, gross_fraction=0.05)).ensure_session(broker(1_000_000, 0))
    assert view.effective.gross_fraction == 0.05

def test_restart_with_a_higher_ceiling_waits_for_the_next_session(db): ...        # session ceiling 0.10 kept

def test_anchor_is_start_of_day_net_liquidation(db):                              # R7
    view = svc(db).ensure_session(broker(995_000, -5_000))
    assert view.anchor == 1_000_000.0

def test_tighter_daily_loss_lowers_the_budget_mid_session_on_the_frozen_anchor(db):
    s = svc(db); s.ensure_session(broker(1_000_000, 0)); publish(s, PAPER_LIMITS, "c1")
    publish(s, replace(PAPER_LIMITS, daily_loss_fraction=0.002), "c2")
    s.ensure_session(broker(990_000, -10_000))
    assert s.current().daily_loss_budget == pytest.approx(2_000.0)

def test_breach_latch_survives_a_looser_revision_and_a_restart(db):
    s = svc(db); publish(s, TIGHT, "c1"); s.latch("DAILY_LOSS", "pnl=-5000")
    publish(s, LOOSE, "c2"); s.latch("DRAWDOWN", "later")                         # first breach wins
    assert svc(db).current().latch_code == "DAILY_LOSS"

def test_off_session_returns_none_and_publish_still_stores(db):
    s = svc(db, now=lambda: SATURDAY)
    assert s.ensure_session(broker(1_000_000, 0)) is None
    assert publish(s, TIGHT, "c1").revision == 1

@pytest.mark.parametrize("bad", [dict(nl=math.nan), dict(account="DU999"), dict(generation_id=0), dict(nl=-1.0)])
def test_session_start_needs_valid_broker_evidence(db, bad):
    with pytest.raises(PolicyRefused, match="SESSION_EVIDENCE_INVALID"):
        svc(db).ensure_session(broker(1_000_000, 0, **bad))

def test_concurrent_session_start_creates_one_row(db):
    with ThreadPoolExecutor(8) as pool:
        views = list(pool.map(lambda nl: svc(db).ensure_session(broker(nl, 0)), range(1_000_000, 1_000_008)))
    assert len({v.anchor for v in views}) == 1
    assert db.execute("SELECT count(*) FROM ai_paper_sessions", fetch="one")[0] == 1

@pytest.mark.parametrize("reason", ["", " ", "x" * 501, 7])
def test_reason_is_required_text(db, reason): ...     # REASON_INVALID

def test_publish_never_starts_a_session(db):                                     # R7, owner answer 1
    publish(svc(db), TIGHT, "c1")
    assert db.execute("SELECT count(*) FROM ai_paper_sessions", fetch="one")[0] == 0
    assert svc(db).ensure_session(broker(1_000_000, 0)).effective == TIGHT         # first decision: applies at once (R8)

def test_restart_never_starts_a_second_session_for_the_same_date(db):           # owner answer 1
    first = svc(db).ensure_session(broker(1_000_000, 0))
    for nl in (900_000, 1_100_000):                                              # three restarts, other broker values
        again = svc(db).ensure_session(broker(nl, 0))
        assert (again.session_date, again.anchor) == (first.session_date, first.anchor)
    assert db.execute("SELECT count(*) FROM ai_paper_sessions", fetch="one")[0] == 1

def test_session_row_is_committed_before_ensure_session_returns(db):
    s = svc(db); s.ensure_session(broker(1_000_000, 0))
    other = DuckDBConnection.get_instance(db.path)                                # a second reader sees the anchor and the empty latch
    assert other.execute("SELECT anchor_net_liquidation, latch_code FROM ai_paper_sessions", fetch="one") == (1_000_000.0, None)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Every public method runs in one `db.transaction(fn)`. Core of `publish` (after validation):

```python
    def _publish_in_tx(self, conn, limits, reason, principal, command_id, now):
        revision = self._next_revision_in_tx(conn)
        conn.execute("INSERT INTO ai_risk_policy_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [self._account_id, revision, json.dumps(limits.to_json()), reason, principal, command_id, now])
        session = self._session_in_tx(conn, self._session_date(now))
        if session is None:
            return PublishResult(revision, (), ())                   # nothing in force today; next roll reads it
        current = self._effective_in_tx(conn, session.session_date)
        if current is None:                                          # R8: first policy of the session
            new, cause, applied = limits.tighter(session.ceiling), "first_policy", RiskLimits.FIELDS
        else:
            new, cause = current.limits.tighter(limits), "published_tighter"
            applied = current.limits.fields_above(new)
        if applied:
            self._append_effective_in_tx(conn, session.session_date, new, revision, cause, now)
        queued = limits.tighter(session.ceiling).fields_above(new)
        return PublishResult(revision, applied, queued)
``` `ensure_session`: `schedule = calendar.resolve(now)`; `None` → return `None`. Existing row → if `self._ceiling` is tighter than the current effective in any field, append `current.tighter(self._ceiling)` with cause `ceiling_tighter` (R9); return the view. No row → validate the broker (account matches, `account_mode == "paper"`, `type(generation_id) is int and > 0`, `net_liquidation` and `daily_pnl` finite, anchor `> 0`), then `INSERT ... ON CONFLICT DO NOTHING`; re-read the row (the winner's anchor). If a published revision exists, append `latest.tighter(self._ceiling)` with cause `session_start`. `current()` never writes; it returns `None` off-session or before the first `ensure_session` of the day. `queued` in the view = `latest_published.tighter(session.ceiling).fields_above(effective)`.

- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add ai risk policy store with per-session timing and breach latch`.

---

### Task 5: Deployment registration and seal

**Files:**
- Create: `trader/automation/ai_deployments.py`
- Test: `tests/automation/test_ai_deployments.py`

**Interfaces:**
- Consumes: `trader/research/canonical.py:53` `canonical_json_bytes`; `trader.objects.BarSize` (valid bar size strings); Task 3 `SUPPORTED_STYLES`.
- Produces:
  - `AI_DEPLOYMENT_MIGRATION_VERSION = 55`; `apply_ai_deployment_migration(migrator)`; table `ai_deployments(digest VARCHAR PRIMARY KEY, record_json VARCHAR NOT NULL, principal VARCHAR NOT NULL, command_id VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL)`.
  - `class DeploymentRefused(Exception)` with `.code`: `DEPLOYMENT_INVALID`, `DEPLOYMENT_NOT_SEALED`, `DEPLOYMENT_TAMPERED`.
  - `VERDICTS = ("DEPLOY", "SHADOW", "REJECT")`.
  - `@dataclass(frozen=True) class AiDeployment: strategy_path: str; strategy_digest: str; class_name: str; params: Mapping[str, Any]; conids: tuple[int, ...]; bar_size: str; style: str; decider: str; decider_verdict: str; evidence_ref: str; evidence_order_notional: float` with `from_json(value) -> AiDeployment` (exact keys, strict types, conids sorted), `to_json() -> dict`.
  - `deployment_digest(d: AiDeployment) -> str`.
  - `class AiDeploymentStore(db, now)`: `register(d, *, principal, command_id) -> tuple[str, bool]` (digest, created), `get_sealed(digest: str) -> AiDeployment`, `provenance(digest) -> str`. `STRATEGY_DIGEST_PROVENANCE = "CLAIMED_NOT_VERIFIED"`; table column `strategy_digest_provenance VARCHAR NOT NULL` always holds it (R19).

- [ ] **Step 1: Write the failing tests**

```python
GOOD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
        "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [272093, 265598],
        "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
        "evidence_ref": "trial:42", "evidence_order_notional": 2000.0}

def test_register_seals_and_returns_a_stable_digest(store):
    digest, created = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="c1")
    assert created and digest.startswith("sha256:")
    again = store.register(AiDeployment.from_json({**GOOD, "conids": [265598, 272093]}), principal="ai_research", command_id="c2")
    assert again == (digest, False)                                  # same content, any conid order
    assert store.get_sealed(digest).conids == (265598, 272093)

def test_digest_is_domain_separated_from_a_plain_hash():
    d = AiDeployment.from_json(GOOD)
    assert deployment_digest(d) != "sha256:" + hashlib.sha256(canonical_json_bytes(d.to_json())).hexdigest()

def test_unknown_digest_is_not_sealed(store):
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_NOT_SEALED"):
        store.get_sealed("sha256:" + "b" * 64)

def test_edited_row_is_tampered(store, db):
    digest, _ = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="c1")
    db.execute("UPDATE ai_deployments SET record_json = replace(record_json, '2000.0', '9000.0')")
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_TAMPERED"):
        store.get_sealed(digest)

@pytest.mark.parametrize("key,value", [
    ("conids", [True]), ("conids", [1.0]), ("conids", ["265598"]), ("conids", []), ("conids", [5, 5]),
    ("conids", [-1]), ("conids", list(range(1, 22))), ("strategy_path", "../etc/x.py"),
    ("strategy_path", "/abs/x.py"), ("strategy_path", "strategies/x.txt"),
    ("strategy_digest", "sha256:XYZ"), ("class_name", "1Bad"), ("bar_size", "7 mins"),
    ("style", "swing_long"), ("decider_verdict", "deploy"), ("decider", "Jev Model"),
    ("evidence_order_notional", 0), ("evidence_order_notional", True), ("evidence_order_notional", math.nan),
    ("params", {"a": math.inf}), ("params", {1: "x"}), ("params", []), ("evidence_ref", "")])
def test_strict_validation(key, value):
    with pytest.raises(DeploymentRefused, match="DEPLOYMENT_INVALID"):
        AiDeployment.from_json({**GOOD, key: value})

def test_missing_or_extra_key_is_invalid():
    for bad in ({k: v for k, v in GOOD.items() if k != "evidence_order_notional"}, {**GOOD, "x": 1}):
        with pytest.raises(DeploymentRefused):
            AiDeployment.from_json(bad)

def test_strategy_digest_is_marked_as_a_claim(store):                          # R19, owner answer
    digest, _ = store.register(AiDeployment.from_json(GOOD), principal="ai_research", command_id="c1")
    assert store.provenance(digest) == "CLAIMED_NOT_VERIFIED"

def test_store_has_no_update_or_delete():
    assert not [m for m in dir(AiDeploymentStore) if m.startswith(("update", "delete", "seal", "unseal"))]

def test_a_deployment_digest_cannot_arm_the_old_path(tmp_path):
    # Activate only reads signed bundles under artifacts/sha256_*; a deployment digest names no bundle.
    assert find_eligible_bundle(share_dir=tmp_path, ...).bundle is None      # see bundle_finder signature
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `from_json` checks each key with a small validator table (regexes: path `^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py$`, digest `^sha256:[0-9a-f]{64}$`, class `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, decider `^[a-z][a-z0-9_.-]{0,63}$`, `evidence_ref` printable, 1–256 chars); conids: list of 1–20 unique `type(x) is int and x > 0`, stored sorted; `params`: dict with `str` keys whose values are `str`, `bool`, `int`, finite `float`, `None` or lists of those (no nesting deeper than one list); bar size must be one of the `BarSize` values. `deployment_digest = "sha256:" + sha256(b"mmr.ai-deployment.v1\x00" + canonical_json_bytes(d.to_json())).hexdigest()`. `register`: in one transaction, select by digest; present → `(digest, False)`; else insert. `get_sealed`: load, `AiDeployment.from_json(json.loads(record_json))`, recompute the digest, compare with `hmac.compare_digest`; any parse failure is `DEPLOYMENT_TAMPERED` too.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add sealed ai deployment records`.

---

### Task 6: Sizing, pending entries and the `ai_paper` evidence factory

**Files:**
- Create: `trader/automation/ai_paper_sizing.py`, `trader/automation/ai_paper_evidence.py`
- Modify: `trader/automation/liquidity_policy.py` (`LiquidityPolicy.max_quantity`); `trader/automation/production_evidence.py:95-133,175-217` (move `_validate_approval` and `_liquidity` bodies into module functions `validate_approval(order, approval, *, account_id, now)` and `liquidity_from_history(history, conid, quote, now)`; the methods delegate, behaviour unchanged); `trader/trading/approval_context.py` (`EntryLimitsEvidence`, field `ApprovalContext.entry_limits`); `trader/trading/dispatch_guard.py` (entry-limits re-check)
- Test: `tests/automation/test_ai_paper_sizing.py`, `tests/automation/test_ai_paper_evidence.py`, `tests/scaling/test_dispatch_tightening.py` (more cases)

**Interfaces:**
- Consumes: Task 1 `RiskLimits`; `compute_gross_notional` (`allocation_policy.py:186`); `LIVE_NOTIONAL_TOLERANCE` (`research/market_context.py:32`); `capture_approval_context` (`approval_context.py:143`); `CanaryRiskStore` (`promotion/canary_risk.py`).
- Produces:
  - `ai_paper_sizing.PROTECTIVE_LEGS = frozenset({"stop", "take_profit", "exit"})`; `is_pending_entry(order) -> bool`; `pending_entry_refusal(broker, conid, limits) -> Optional[str]` (`"MAX_PENDING_ENTRIES"` or `None`).
  - `@dataclass(frozen=True) class SizingInputs: equity: float; price: float; stop_price: float; existing_position_value: float; current_gross_notional: float; liquidity_max_shares: float; notional_cap: float`; `max_entry_quantity(limits, inputs) -> int`.
  - `entry_limit_violations(limits, *, broker, conid, quantity, price, evidence: EntryLimitsEvidence) -> tuple[str, ...]` (used by the guard).
  - `approval_context.EntryLimitsEvidence(limits: RiskLimits, stop_price: float, liquidity_max_shares: float, notional_cap: float, daily_loss_anchor: float, high_water_mark: float)`; `ApprovalContext.entry_limits: Optional[EntryLimitsEvidence] = None`.
  - `LiquidityPolicy.max_quantity(evidence) -> float`.
  - `ai_paper_filter.AiEntryFilter(*, universe, load_filter: Callable[[], TradingFilter] = TradingFilter.load)` with `refusal(conid: int, price: float) -> Optional[str]` (`INSTRUMENT_UNRESOLVED` for no exact single row or a blank `primaryExchange`, `TRADING_FILTER_DENIED`, `TRADING_FILTER_UNAVAILABLE`; R6) and `last_reason: str`. The loader caches by file mtime.
  - `ai_paper_evidence.ai_entry_gate(*, entry_filter: AiEntryFilter) -> Callable[[CommandRequest, ApprovalContext, Quote, datetime], Optional[str]]` (R25: expiry from `request.body["expires_at"]`, filter, limit through the fresh ask); `DispatchGuard(..., ai_entry_gate=..., strict_margin_actions=frozenset())`.
  - `ai_paper_evidence.PreparedEntry(quantity: int, approval: ApprovalContext, session_state: AutomationSessionState, allocation: AllocationCeiling, margin_checked: bool)`; `class AiPaperEvidence(*, broker, quotes, margin, history, journal, account_id, now, max_drift_bps, entry_offset_bps: Decimal, entry_filter: AiEntryFilter)` with `prepare_entry(*, conid: int, stop_price: float, requested_quantity: Optional[int], limits: RiskLimits, session: SessionView, notional: float, experiment_id: str) -> PreparedEntry`. Raises `ApprovalContextError(code, ...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_ai_paper_sizing.py
BASE = SizingInputs(equity=1_000_000.0, price=100.0, stop_price=98.0, existing_position_value=0.0,
                    current_gross_notional=0.0, liquidity_max_shares=10_000.0, notional_cap=1e9)

@pytest.mark.parametrize("changes,expected", [
    ({}, 500),                                                   # position 500; risk 1,000; gross 600
    ({"stop_price": 90.0}, 200),                                 # risk 2,000 / 10.00 = 200
    ({"current_gross_notional": 55_000.0}, 50),                  # gross 6% left: 5,000 / 100
    ({"existing_position_value": 49_950.0}, 0),                  # 50 dollars of room = 0.5 share
    ({"liquidity_max_shares": 120.4}, 120),
    ({"notional_cap": 2_100.0}, 21),
    ({"price": math.nan}, 0), ({"stop_price": 100.0}, 0), ({"stop_price": 101.0}, 0)])
def test_max_is_the_minimum_over_every_limit_rounded_down(changes, expected):
    assert max_entry_quantity(PAPER_LIMITS, replace(BASE, **changes)) == expected

def test_floor_is_robust_to_float_noise():
    assert max_entry_quantity(replace(PAPER_LIMITS, position_fraction=0.0003), BASE) == 3   # 300/100, not 2

def test_unknown_and_external_orders_count_as_pending_entries():
    broker = snapshot(working=[order(leg=None, is_external=True), order(leg="child-7"),
                               order(leg="stop"), order(leg="take_profit"), order(leg="exit"),
                               order(leg="entry", filled=10, total=10)])                    # filled: not pending
    assert [is_pending_entry(o) for o in broker.working_orders] == [True, True, False, False, False, False]

def test_pending_entries_and_position_slots():
    three = snapshot(working=[order(conid=c, leg="entry") for c in (1, 2, 3)])
    assert pending_entry_refusal(three, 4, PAPER_LIMITS) == "MAX_PENDING_ENTRIES"
    slots = snapshot(positions=[pos(1), pos(2)], working=[order(conid=3, leg="entry")])
    assert pending_entry_refusal(slots, 4, PAPER_LIMITS) == "MAX_PENDING_ENTRIES"   # 2 held + 1 working = 3
    assert pending_entry_refusal(slots, 1, PAPER_LIMITS) is None                    # adding to a held conid
```

  (Equity 1,000,000, price 100.00: every row's comment names the limit that binds.)

```python
# tests/automation/test_ai_paper_evidence.py — fakes: broker (sequence of snapshots), quotes, margin, history
def test_factory_captures_snapshot_quote_margin_hwm_and_liquidity(parts):
    prepared = AiPaperEvidence(**parts).prepare_entry(conid=CONID, stop_price=98.0, requested_quantity=None,
        limits=PAPER_LIMITS, session=SESSION, notional=1e9, experiment_id="exp1")
    a = prepared.approval
    assert a.broker.generation_id > 0 and a.market.quote.feed_type == "live" and a.what_if is not None
    assert prepared.session_state.high_water_mark == 1_000_000.0 and prepared.session_state.liquidity is not None
    assert prepared.session_state.limits == PAPER_LIMITS and prepared.session_state.daily_loss_anchor == SESSION.anchor
    assert prepared.allocation.max_gross_fraction == PAPER_LIMITS.gross_fraction
    assert a.entry_limits.stop_price == 98.0
    assert CanaryRiskStore(parts["journal"], "ai_paper:exp1", ACCOUNT).high_water_mark() == 1_000_000.0

def test_no_quantity_uses_the_maximum(parts): ...                       # == max_entry_quantity on the sized price
def test_quantity_at_or_below_the_maximum_is_used_as_is(parts): ...

def test_quantity_above_the_maximum_is_refused_not_cut(parts):
    with pytest.raises(ApprovalContextError, match="QUANTITY_ABOVE_MAXIMUM"):
        AiPaperEvidence(**parts).prepare_entry(..., requested_quantity=501, notional=1e9, ...)

def test_attested_notional_is_checked_before_the_maximum(parts):           # R12
    with pytest.raises(ApprovalContextError, match="ORDER_EXCEEDS_ATTESTED_NOTIONAL"):
        AiPaperEvidence(**parts).prepare_entry(..., requested_quantity=30, notional=2_000.0, ...)

def test_maximum_below_one_share_refuses(parts): ...                      # QUANTITY_BELOW_ONE_SHARE
def test_pending_entries_beyond_the_limit_are_refused(parts): ...         # MAX_PENDING_ENTRIES before any quote read

def test_size_is_rechecked_on_the_captured_snapshot(parts):
    parts["broker"] = SnapshotSequence(snapshot(), snapshot(positions=[pos(9, value=55_000.0)]))
    with pytest.raises(ApprovalContextError, match="QUANTITY_ABOVE_MAXIMUM"):
        AiPaperEvidence(**parts).prepare_entry(..., requested_quantity=100, ...)

@pytest.mark.parametrize("fault,code", [("no_quote", "EVIDENCE_UNAVAILABLE"), ("delayed_feed", "FEED_NOT_LIVE"),
    ("stale_quote", "QUOTE_STALE"), ("no_what_if", "MARGIN_UNAVAILABLE"), ("what_if_timeout", "MARGIN_UNAVAILABLE"),
    ("what_if_nan", "MARGIN_INVALID"), ("what_if_negative", "MARGIN_INVALID"), ("no_history", "HISTORY_UNAVAILABLE"),
    ("denylisted", "TRADING_FILTER_DENIED"), ("unresolved", "INSTRUMENT_UNRESOLVED"), ("filter_unparsable", "TRADING_FILTER_UNAVAILABLE"),
    ("live_account", "PAPER_ONLY"), ("wrong_account", "ACCOUNT_MISMATCH"), ("fence_zero", "BROKER_FENCE_INVALID")])
def test_every_missing_evidence_fails_closed_with_its_code(parts, fault, code): ...

def test_margin_is_never_reported_as_a_sizing_bound(parts):                     # R11
    prepared = AiPaperEvidence(**parts).prepare_entry(conid=CONID, stop_price=98.0, requested_quantity=None,
        limits=PAPER_LIMITS, session=SESSION, notional=1e9, experiment_id="exp1")
    assert prepared.margin_checked is True
    assert not [f.name for f in dataclasses.fields(EntryLimitsEvidence) if "margin" in f.name]

# tests/automation/test_ai_paper_filter.py
@pytest.mark.parametrize("rule", [{"denylist": ["AAPL"]}, {"deny_exchanges": ["NASDAQ"]}, {"deny_sec_types": ["STK"]},
                                  {"allowlist": ["MSFT"]}, {"min_price": 500.0}])
def test_filter_uses_the_resolved_symbol_exchange_and_sec_type(tmp_path, rule):
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ", "STK")}), load_filter=lambda: TradingFilter(**rule))
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"

def test_smart_routing_is_not_the_exchange(tmp_path):
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ", "STK", exchange="SMART")}),
                      load_filter=lambda: TradingFilter(deny_exchanges=["SMART"]))
    assert f.refusal(CONID, 100.0) is None

@pytest.mark.parametrize("rule", [{"exchanges": ["NASDAQ"]}, {"deny_exchanges": ["NYSE"]}])
def test_a_missing_listing_exchange_is_unresolved_under_an_exchange_rule(rule):      # R6, R25: blank primaryExchange
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "", "STK", exchange="SMART")}),
                      load_filter=lambda: TradingFilter(**rule))
    assert f.refusal(CONID, 100.0) == "INSTRUMENT_UNRESOLVED"                      # never "allowed", never a SMART fallback

def test_a_smart_routed_order_on_an_allowed_primary_listing_passes():
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ", "STK", exchange="SMART")}),
                      load_filter=lambda: TradingFilter(exchanges=["NASDAQ"]))
    assert f.refusal(CONID, 100.0) is None

def test_a_denylisted_primary_listing_is_refused_even_when_routed_by_smart():
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ", "STK", exchange="SMART")}),
                      load_filter=lambda: TradingFilter(deny_exchanges=["NASDAQ"]))
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"

def test_an_allowlist_of_only_smart_denies_every_entry():
    f = AiEntryFilter(universe=FakeUniverse({CONID: secdef("AAPL", "NASDAQ", "STK", exchange="SMART")}),
                      load_filter=lambda: TradingFilter(exchanges=["SMART"]))
    assert f.refusal(CONID, 100.0) == "TRADING_FILTER_DENIED"

@pytest.mark.parametrize("rows", [[], [secdef("AAPL"), secdef("AAPL")], [secdef("AAPL", conid=4391)]])
def test_no_exact_single_instrument_is_unresolved(rows): ...                   # INSTRUMENT_UNRESOLVED, no symbol guess

def test_a_filter_edit_is_seen_on_the_next_check(tmp_path): ...                 # write file, allow; add denylist, deny
```

```python
# tests/scaling/test_dispatch_tightening.py (added)
def test_entry_limits_tightened_between_approval_and_dispatch_refuse(guard_parts):
    guard = DispatchGuard(**guard_parts, current_limits=lambda r: replace(PAPER_LIMITS, position_fraction=0.02))
    approval = ai_entry_approval(quantity=400, price=100.0, limits=PAPER_LIMITS)     # 4% position
    with pytest.raises(DispatchGuardError, match="LIMIT_TIGHTENED_BEFORE_DISPATCH"):
        guard.revalidate(approval, automated_request(action="submit_ai_paper_decision"), NOW)

@pytest.mark.parametrize("tight", [{"daily_loss_fraction": 0.001}, {"max_positions": 1}, {"trade_risk_fraction": 0.0001}])
def test_each_tightened_field_is_rechecked(guard_parts, tight): ...

def test_unchanged_entry_limits_skip_the_recheck(guard_parts): ...         # no extra reads, no raise

# tests/automation/test_ai_dispatch_gate.py (R25) — the real DispatchGuard with guard_parts
@pytest.mark.parametrize("fault,code", [("expired_before_dispatch", "DECISION_EXPIRED"),
    ("denylisted_after_approval", "TRADING_FILTER_DENIED"), ("ask_fell_20bps", "ENTRY_LIMIT_THROUGH_QUOTE"),
    ("no_what_if", "MARGIN_UNAVAILABLE"), ("what_if_timeout", "MARGIN_UNAVAILABLE"), ("what_if_nan", "MARGIN_INVALID")])
def test_ai_entry_is_refused_at_dispatch(guard_parts, fault, code):
    guard = ai_guard(guard_parts, fault)                       # ai_entry_gate + strict_margin_actions={AI_PAPER_ACTION}
    with pytest.raises(DispatchGuardError, match=code):
        guard.revalidate(ai_entry_approval(), automated_request(action="submit_ai_paper_decision"), NOW)

def test_old_path_still_only_warns_without_a_what_if(guard_parts):           # old path unchanged
    guard = ai_guard(guard_parts, "no_what_if")
    assert "WHAT_IF_UNAVAILABLE_PAPER" in guard.revalidate(old_path_approval(), automated_request(action="execute_automated_intent"), NOW).warnings
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**

```python
# ai_paper_sizing.py
def _shares(value: float) -> float:
    return value if math.isfinite(value) and value > 0 else 0.0

def max_entry_quantity(limits: RiskLimits, inputs: SizingInputs) -> int:
    equity, price, stop = inputs.equity, inputs.price, inputs.stop_price
    if not all(math.isfinite(v) and v > 0 for v in (equity, price, stop)) or stop >= price:
        return 0
    bounds = (
        limits.trade_risk_fraction * equity / (price - stop),
        (limits.position_fraction * equity - inputs.existing_position_value) / price,
        (limits.gross_fraction * equity - inputs.current_gross_notional) / price,
        inputs.liquidity_max_shares,
        inputs.notional_cap / price,
    )
    return int(math.floor(min(_shares(b) for b in bounds) + 1e-9))

def is_pending_entry(order) -> bool:
    remaining = float(order.total_quantity) - float(order.filled_quantity)
    return not order.deleted and remaining > 0 and order.leg not in PROTECTIVE_LEGS

def pending_entry_refusal(broker, conid: int, limits: RiskLimits) -> Optional[str]:
    pending = [o for o in broker.working_orders if is_pending_entry(o)]
    held = {p.conid for p in broker.positions if p.quantity and float(p.quantity) != 0 and not p.deleted}
    busy = held | {o.conid for o in pending}
    if len(pending) + 1 > limits.max_pending_entry_orders:
        return "MAX_PENDING_ENTRIES"
    if conid not in busy and len(busy) + 1 > limits.max_positions:
        return "MAX_PENDING_ENTRIES"
    return None
```

  `entry_limit_violations` returns `LIMIT_TIGHTENED_BEFORE_DISPATCH` when, under the given limits on the current broker: quantity > `max_entry_quantity`, or `pending_entry_refusal` (excluding nothing: the entry is not yet working), or `-daily_pnl >= anchor × daily_loss_fraction`, or drawdown from `high_water_mark` `>= drawdown_fraction`. `DispatchGuard.revalidate`, after the allocation branch:

```python
        if approved.entry_limits is not None:
            evidence = approved.entry_limits
            tight = evidence.limits.tighter(self._limits_for(request))        # LIMITS_UNAVAILABLE on failure
            if tight != evidence.limits and entry_limit_violations(
                    tight, broker=current, conid=approved.conid, quantity=abs(float(approved.quantity)),
                    price=price, evidence=evidence):
                raise DispatchGuardError("LIMIT_TIGHTENED_BEFORE_DISPATCH", "limits tightened after approval")
```

  `LiquidityPolicy.max_quantity(evidence) = min(evidence.adv_shares_20d * MAX_ADV_FRACTION, inf if evidence.sliced_execution_approved else evidence.top_of_book_depth)`.

  `AiPaperEvidence.prepare_entry`, in this order (each failure raises `ApprovalContextError` with the code shown):
  1. Paper binding: `account_mode == "paper"` and account starts with `DU` (`PAPER_ONLY`).
  2. `snapshot = broker.capture(account_id)`; account (`ACCOUNT_MISMATCH`), paper mode, complete fence (`BROKER_FENCE_INVALID`), finite positive net liquidation (`BROKER_EVIDENCE_INVALID`).
  3. `pending_entry_refusal(snapshot, conid, limits)` → `MAX_PENDING_ENTRIES`.
  4. `quote = quotes.executable_quote(conid, side="ask")` (`EVIDENCE_UNAVAILABLE` when `None` or raising); `liquidity = liquidity_from_history(history, conid, quote, now)`.
  4b. `entry_filter.refusal(conid, price)` with the entry price of step 5 (R6) → its code.
  5. `price = ask × (1 + entry_offset_bps / 10_000)` (R11). `notional_cap = notional × (1 + LIVE_NOTIONAL_TOLERANCE)`. `max_q = max_entry_quantity(limits, SizingInputs(...))` with `current_gross_notional = compute_gross_notional(snapshot, quote_prices={conid: price})[0]`.
  6. Quantity: requested and `requested × price > notional_cap` → `ORDER_EXCEEDS_ATTESTED_NOTIONAL`; requested `> max_q` → `QUANTITY_ABOVE_MAXIMUM`; `None` → `max_q`; result `< 1` → `QUANTITY_BELOW_ONE_SHARE`.
  7. `approval = capture_approval_context(..., quantity=float(q), ...)` (exceptions → `EVIDENCE_UNAVAILABLE`), then `validate_approval(order, approval, account_id=..., now=now())` with `order = SimpleNamespace(conid=conid, side="BUY", requested_quantity=q)`. Recompute `max_q` on `approval.broker` and `approval.market.quote`; `q > max_q` → `QUANTITY_ABOVE_MAXIMUM`. `approval.what_if is None` (also after a raise or a timeout) → `MARGIN_UNAVAILABLE`; non-finite or negative `initMarginAfter` / `equityWithLoanAfter` → `MARGIN_INVALID` (R11).
  8. `hwm = CanaryRiskStore(journal, f"ai_paper:{experiment_id}", account_id).update_high_water_mark(approval.broker.net_liquidation, now)` (`HWM_UNAVAILABLE`).
  9. Return `PreparedEntry(q, replace(approval, risk_direction="INCREASING", entry_limits=EntryLimitsEvidence(limits, stop_price, liquidity_max, notional_cap, session.anchor, hwm)), AutomationSessionState(hwm, account_id, limits, liquidity=liquidity, daily_loss_anchor=session.anchor), AllocationCeiling(limits.gross_fraction), margin_checked=True)`.

  `DispatchGuard.revalidate` (R25): in the margin block, `strict = request.action in self._strict_margin_actions`; when `strict`, a `None` margin raises `DispatchGuardError("MARGIN_UNAVAILABLE", …)` and an invalid one `MARGIN_INVALID` instead of the paper warning. After the entry-limits re-check: `code = self._ai_entry_gate(request, approved, quote, now)`; a code raises `DispatchGuardError(code, …)`, a raising gate `AI_ENTRY_GATE_UNAVAILABLE`. Both default to no-ops, so the old path does not change.

- [ ] **Step 4: Run** the three files, `tests/automation/test_production_evidence.py` (unchanged, proves the helper move), then the full suite.
- [ ] **Step 5: Commit** — `feat: size ai_paper entries from effective limits and recheck them at dispatch`.

---

### Task 7: `submit_ai_paper_decision` — model, store and entry path

**Files:**
- Create: `trader/automation/ai_paper_experiment.py`, `trader/automation/entry_views.py`, `trader/automation/command_steps.py`, `trader/automation/ai_paper_decision.py`
- Modify: `trader/automation/automated_intent_command.py` (`_transition`, `_receipt`, `_claim` move to `CommandSteps`; the service uses it; no behaviour change); `trader/automation/session_risk.py` and `protective_order_saga.py` (annotate `intent` / `artifact` with the `entry_views` protocols)
- Test: `tests/automation/test_ai_paper_decision_model.py`, `tests/automation/test_ai_paper_entry.py`

**Interfaces:**
- Consumes: Tasks 4–6; `ProtectiveOrderSaga.start` (Plan 1 version); `TradingCommandCoordinator.execute` / `CommandLedger.unresolved_for_target` (`command_coordinator.py:690`); `ExitOwnerRegistry.owner_for` / `account_owner` (Plan 1, `trader/trading/exit_owner.py`).
- Produces:
  - `ai_paper_experiment`: `ExperimentState = Literal["ARMED", "PAUSED", "KILLED", "STOPPED"]`; `@dataclass(frozen=True) class ExperimentView: experiment_id: str; state: ExperimentState`; `class ExperimentStatePort(Protocol): def current(self, account_id: str) -> Optional[ExperimentView]`; `class NoExperiment` (returns `None`). Plan 4 implements the port.
  - `entry_views`: `EntryOrderView` and `EntryAuthorityView` protocols (the fields the saga, `session_risk` and `build_bracket_plan` read: `command_id, conid, side, requested_quantity, risk_fraction, entry_policy, stop_policy, target_policy, account_mode, artifact_id`; and `artifact_id, allowlist, max_gross_allocation, attested_strategy.order_notional`).
  - `ai_paper_decision`:
    - `AI_PAPER_ACTION = "submit_ai_paper_decision"`; `AI_PAPER_DECISION_MIGRATION_VERSION = 56`.
    - `@dataclass(frozen=True) class AiPaperDecision` (fields of spec 5.4 with R16–R17 types: `decision_id: str, deployment_digest: Optional[str], decider: str, action: Literal["ENTER","CLOSE","PARTIAL_CLOSE"], conid: int, side: Literal["BUY","SELL"], stop_price: Optional[float], target_price: Optional[float], quantity: Optional[int], policy_revision: Optional[int], evidence_digest: str, expires_at: datetime`), `from_body(body: Mapping) -> AiPaperDecision` (raises `DecisionInvalid(detail)`), `to_body() -> dict`, `command_id_for(decision_id) -> str`.
    - `AiPaperEntryOrder` (frozen; satisfies `EntryOrderView`) and `DeploymentBinding` (satisfies `EntryAuthorityView`).
    - `AI_ENTRY_POLICY = EntryPolicy("MARKETABLE_LIMIT", Decimal("10"), "DAY")` (R18).
    - `class AiPaperDecisionStore(db, now)`: `record_in_tx(conn, row)`, `finish(command_id, *, state, error_code, close_root_id=None)`, `row(decision_id)`, `row_by_command(command_id)`, `blocking_decision_on_conid(account_id, conid, *, exclude_command_id, broker) -> Optional[str]` (returns `OUTCOME_UNKNOWN_PENDING` or `ENTRY_ALREADY_WORKING`, R13), `links_for_order_ref(order_ref) -> tuple[DecisionLink, ...]` (R24).
    - `class AiPaperDecisionService(*, ledger, journal, controls, coordinator_ledger, policy: AiRiskPolicyService, deployments: AiDeploymentStore, evidence: AiPaperEvidence, saga, experiments: ExperimentStatePort, exit_owners: ExitOwnerRegistry, liquidation, broker, config: AiPaperConfig, account_id, now, schedule_reconcile)` with `execute(cmd: CommandRequest) -> CommandReceipt` (registered `saga=True`). Task 8 adds the reduction branch.

Decision table (migration 56): `ai_paper_decisions(command_id VARCHAR PRIMARY KEY, decision_id VARCHAR, account_id VARCHAR NOT NULL, conid BIGINT, action VARCHAR, decider VARCHAR, evidence_digest VARCHAR, deployment_digest VARCHAR, strategy_digest VARCHAR, style VARCHAR, policy_revision INTEGER, effective_revision INTEGER, principal VARCHAR, body_json VARCHAR NOT NULL, state VARCHAR NOT NULL, error_code VARCHAR, close_root_id VARCHAR, received_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)`. Keyed by command id; the typed columns are nullable so a `DECISION_INVALID` body (e.g. `conid: true`) is still recorded with its raw `body_json` and its code.

Entry admission, in this order (spec 5.4; every code distinct and stored on the row):

| # | Check | Refusal |
|---|---|---|
| 1 | `cmd.principal == "ai_supervisor"` | `PRINCIPAL_FORBIDDEN` |
| 2 | `AiPaperDecision.from_body(cmd.body)` | `DECISION_INVALID` |
| 3 | `cmd.account_id == account_id` | `ACCOUNT_MISMATCH` |
| 4 | experiment exists / not `STOPPED` / `ARMED` | `NO_EXPERIMENT` / `EXPERIMENT_STOPPED` / `EXPERIMENT_NOT_ARMED` |
| 5 | `now < expires_at <= now + 15 min` | `DECISION_EXPIRED` / `DECISION_EXPIRY_TOO_FAR` |
| 6 | no blocking `ai_paper` command on the conid and no acknowledged working entry on it (R13; same transaction as the `VALIDATED` transition); no active exit owner on it or the account (R14) | `OUTCOME_UNKNOWN_PENDING` / `ENTRY_ALREADY_WORKING` / `EXIT_IN_PROGRESS` |
| 7 | `broker.capture(...)` succeeds; `policy.ensure_session(snapshot)` not `None` (its own committed transaction, R7: anchor and latch slot are durable before step 8); not latched | `BROKER_SNAPSHOT_UNAVAILABLE` (retryable) / `SESSION_CLOSED` / `RISK_LATCHED` |
| 8 | a published revision exists; `policy_revision` equals it; effective limits exist | `NO_ACCEPTED_POLICY` / `POLICY_REVISION_STALE` / `NO_EFFECTIVE_LIMITS` |
| 9 | `deployments.get_sealed(digest)`; verdict `DEPLOY`; conid in conids; style in `config.styles` | `DEPLOYMENT_NOT_SEALED` / `DEPLOYMENT_TAMPERED` / `DEPLOYMENT_NOT_DEPLOYABLE` / `CONID_NOT_IN_DEPLOYMENT` / `STYLE_NOT_ENABLED` |
| 10 | `side == "BUY"`, `stop_price` given | `SIDE_NOT_ENABLED` / `DECISION_INVALID` |
| 11 | `evidence.prepare_entry(...)` (includes the trading filter, R6, and the margin refusal, R11) | its `ApprovalContextError` code |
| 12 | claim `VALIDATED → SUBMITTING` with `require_unpaused` | `TRADING_PAUSED` |
| 13 | `saga.start(...)` → `session_risk` (calendar, stop validity, liquidity, gross, notional…) + `DispatchGuard` with the AI gate (R25: expiry, filter, limit through the fresh ask, strict margin) | the saga's `error_code` |

After a `DAILY_LOSS`, `DRAWDOWN` or `PORTFOLIO_DAILY_LOSS` refusal at step 13, call `policy.latch(code, detail)` (R10).

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_ai_paper_decision_model.py
FUTURE = (NOW + dt.timedelta(minutes=5)).isoformat()
ENTER = {"decision_id": "dec-00000001", "deployment_digest": "sha256:" + "a" * 64, "decider": "jev",
         "action": "ENTER", "conid": 265598, "side": "BUY", "stop_price": 98.0, "target_price": None,
         "quantity": None, "policy_revision": 1, "evidence_digest": "sha256:" + "c" * 64, "expires_at": FUTURE}

@pytest.mark.parametrize("key,value", [
    ("conid", True), ("conid", 265598.0), ("conid", "265598"), ("conid", 0), ("quantity", 1.5), ("quantity", True),
    ("quantity", 0), ("stop_price", math.nan), ("stop_price", math.inf), ("stop_price", "98"), ("stop_price", True),
    ("action", "enter"), ("side", "buy"), ("decision_id", "dec:0001"), ("decision_id", "short"),
    ("policy_revision", True), ("expires_at", "2026-07-17T15:05:00"), ("expires_at", 1752764700),
    ("evidence_digest", "abc"), ("decider", "Some Provider")])
def test_strict_fields(key, value):
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**ENTER, key: value})

def test_unknown_or_missing_field_is_invalid(): ...

@pytest.mark.parametrize("extra", [{"order_type": "MARKET"}, {"limit_offset_bps": 50}, {"tif": "GTC"},
                                   {"entry_policy": {"order_type": "MARKET"}}])
def test_a_decision_cannot_choose_the_order_type_or_offset(extra):            # R18, owner answer 5
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**ENTER, **extra})

@pytest.mark.parametrize("body", [
    {**ENTER, "action": "CLOSE", "deployment_digest": "sha256:" + "a" * 64, "policy_revision": None, "stop_price": None},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": 1, "stop_price": None},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": None, "stop_price": 98.0},
    {**ENTER, "action": "CLOSE", "deployment_digest": None, "policy_revision": None, "stop_price": None, "quantity": 5},
    {**ENTER, "action": "PARTIAL_CLOSE", "deployment_digest": None, "policy_revision": None, "quantity": None},
    {**ENTER, "stop_price": None}, {**ENTER, "deployment_digest": None}, {**ENTER, "policy_revision": None}])
def test_per_action_shape(body):                                        # R16
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body(body)

def test_command_id_is_derived_and_colon_free():
    assert command_id_for("dec-00000001") == "aip-dec-00000001"
```

```python
# tests/automation/test_ai_paper_entry.py — real coordinator, ledger, journal, policy, deployment and
# decision stores on tmp DuckDB; fake broker/quotes/margin/history; a recording bracket dispatch;
# the real ProtectiveOrderSaga + SessionRiskController + DispatchGuard (current_limits = policy router).
# Fixture `world` publishes TIGHT=PAPER_LIMITS as revision 1, registers GOOD (Task 5), arms a fake experiment.
# World helpers used below: `world.filter_file.write(**rules)` writes trading_filters.yaml in tmp_path (the
# AiEntryFilter loads it fresh); `world.filter_loads` counts TradingFilter.load calls; `world.evidence.fail_with(code)`
# makes the next prepare_entry raise ApprovalContextError(code); `world.policy_restarted()` builds a new
# AiRiskPolicyService over the same DuckDB file (a restart).

def submit(world, body=None, principal="ai_supervisor", **changes):
    body = {**ENTER, "deployment_digest": world.digest, **changes} if body is None else body
    return world.coordinator.execute(CommandRequest(command_id=command_id_for(body["decision_id"]),
        action=AI_PAPER_ACTION, account_id=ACCOUNT, target_type="conid", target_id=str(body["conid"]),
        expected_version=None, body=body, source=principal, principal=principal))

def test_enter_submits_one_protective_bracket(world):
    receipt = submit(world)
    assert receipt.state == "SUBMITTED"
    plan = world.dispatch.plans[0]
    assert [leg.role for leg in plan.legs] == ["entry", "stop"] and plan.legs[0].quantity == 500
    assert plan.order_ref == "mmr:og-aip-dec-00000001"
    assert world.decisions.row("dec-00000001").state == "SUBMITTED"

def test_same_decision_id_replays_and_a_changed_body_conflicts(world):
    first = submit(world)
    assert submit(world) == first and len(world.dispatch.plans) == 1
    assert submit(world, quantity=10).error_code == "IDEMPOTENCY_CONFLICT"       # code from _conflict_receipt

def test_new_decision_id_cannot_retry_a_conid_with_an_unknown_outcome(world):
    world.dispatch.raise_ambiguous = True
    assert submit(world).state == "OUTCOME_UNKNOWN"
    world.dispatch.raise_ambiguous = False
    assert submit(world, decision_id="dec-00000002").error_code == "OUTCOME_UNKNOWN_PENDING"

def test_unacknowledged_submitted_entry_blocks_its_conid(world):              # R13, owner answer
    submit(world)                                                               # SUBMITTED; broker does not show og-aip-dec-00000001 yet
    assert submit(world, decision_id="dec-00000002", quantity=1).error_code == "OUTCOME_UNKNOWN_PENDING"

def test_acknowledged_working_entry_blocks_a_duplicate_and_counts_as_pending(world):
    submit(world); world.broker.show_working_entry("og-aip-dec-00000001", conid=CONID, quantity=500)
    assert submit(world, decision_id="dec-00000002", quantity=1).error_code == "ENTRY_ALREADY_WORKING"
    world.broker.add_working_entries(other_conids=2)                            # 3 working entries now
    assert submit(world, decision_id="dec-00000003", conid=OTHER).error_code == "MAX_PENDING_ENTRIES"
    assert len(world.dispatch.plans) == 1

def test_acknowledged_entry_counts_toward_gross(world):
    submit(world); world.broker.show_working_entry("og-aip-dec-00000001", conid=CONID, quantity=500)   # 5% working
    assert submit(world, decision_id="dec-00000002", conid=OTHER, quantity=200).error_code == "QUANTITY_ABOVE_MAXIMUM"   # 1% gross left

def test_filled_entry_allows_a_later_entry_within_limits(world): ...          # fill 100 of 500, rest cancelled: a new ENTER passes

def test_concurrent_decisions_never_create_a_duplicate_entry(world):
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda i: submit(world, decision_id=f"dec-0000010{i}"), range(4)))
    assert len(world.dispatch.plans) == 1
    assert sorted(r.error_code or "ok" for r in receipts) == ["OUTCOME_UNKNOWN_PENDING"] * 3 + ["ok"]

@pytest.mark.parametrize("setup,code", [
    ("principal_cli", "PRINCIPAL_FORBIDDEN"), ("no_experiment", "NO_EXPERIMENT"),
    ("paused", "EXPERIMENT_NOT_ARMED"), ("killed", "EXPERIMENT_NOT_ARMED"), ("stopped", "EXPERIMENT_STOPPED"),
    ("expired", "DECISION_EXPIRED"), ("expiry_too_far", "DECISION_EXPIRY_TOO_FAR"),
    ("weekend", "SESSION_CLOSED"), ("latched", "RISK_LATCHED"), ("no_policy", "NO_ACCEPTED_POLICY"),
    ("stale_revision", "POLICY_REVISION_STALE"), ("unsealed", "DEPLOYMENT_NOT_SEALED"),
    ("tampered", "DEPLOYMENT_TAMPERED"), ("shadow_verdict", "DEPLOYMENT_NOT_DEPLOYABLE"),
    ("conid_outside", "CONID_NOT_IN_DEPLOYMENT"), ("sell_entry", "SIDE_NOT_ENABLED"),
    ("exit_owner_active", "EXIT_IN_PROGRESS"), ("paused_trading", "TRADING_PAUSED")])
def test_each_admission_rule_refuses_with_its_own_code(world, setup, code):
    world.apply(setup)
    receipt = submit(world, **world.body_changes(setup))
    assert (receipt.state, receipt.error_code) == ("REJECTED", code)
    assert world.decisions.row(receipt_decision_id(receipt)).error_code == code
    assert world.dispatch.plans == []

def test_an_invalid_body_is_still_recorded(world):
    body = {**ENTER, "deployment_digest": world.digest, "conid": True}
    receipt = world.coordinator.execute(CommandRequest(command_id="aip-dec-00000009", action=AI_PAPER_ACTION,
        account_id=ACCOUNT, target_type="conid", target_id="?", expected_version=None, body=body,
        source="ai_supervisor", principal="ai_supervisor"))
    row = world.decisions.row_by_command("aip-dec-00000009")
    assert (receipt.error_code, row.error_code, row.conid) == ("DECISION_INVALID", "DECISION_INVALID", None)

def test_a_queued_looser_field_is_not_in_force_at_admission(world):
    world.policy_publish(replace(PAPER_LIMITS, position_fraction=0.06, gross_fraction=0.08))   # revision 2, queued
    receipt = submit(world, policy_revision=2, quantity=550)
    assert receipt.error_code == "QUANTITY_ABOVE_MAXIMUM"                      # 5% still in force

@pytest.mark.parametrize("fault,code", [                                       # coordinator checks reached from a decision
    ("wrong_account_snapshot", "ACCOUNT_MISMATCH"), ("stale_quote", "QUOTE_STALE"),
    ("no_what_if", "MARGIN_UNAVAILABLE"), ("leverage", "LEVERAGE_REJECTED"),
    ("after_cutoff", "ENTRY_CUTOFF"), ("stop_above_price", "STOP_INVALID"),
    ("pending_entries", "MAX_PENDING_ENTRIES"), ("over_notional", "ORDER_EXCEEDS_ATTESTED_NOTIONAL"),
    ("quantity_over_max", "QUANTITY_ABOVE_MAXIMUM"), ("invalid_what_if", "MARGIN_INVALID"),
    ("expired_at_dispatch", "DECISION_EXPIRED"), ("denylisted_at_dispatch", "TRADING_FILTER_DENIED")])
def test_existing_checks_refuse_with_their_own_code(world, fault, code): ...

def test_denylisted_entry_is_refused(world):                                  # owner answer 4
    world.filter_file.write(denylist=["AAPL"])                                  # CONID resolves to AAPL / NASDAQ / STK
    receipt = submit(world)
    assert (receipt.state, receipt.error_code, world.dispatch.plans) == ("REJECTED", "TRADING_FILTER_DENIED", [])

def test_the_entry_uses_the_trader_order_type(world):                         # R18
    submit(world)
    entry = world.dispatch.plans[0].legs[0]
    assert (entry.order_type, entry.tif) == ("LMT", "DAY") and entry.limit_price <= Decimal("100.10")   # ask 100.00

def test_session_anchor_is_durable_before_admission(world):                   # R7, owner answer 1
    world.evidence.fail_with("EVIDENCE_UNAVAILABLE")                            # admission refuses after step 7
    submit(world)
    assert world.policy_restarted().current().anchor == 1_000_000.0            # row survived the refused decision

def test_daily_loss_refusal_latches_the_session(world):
    world.broker.set(daily_pnl=-5_000.0)
    assert submit(world).error_code == "DAILY_LOSS"
    world.broker.set(daily_pnl=0.0)
    assert submit(world, decision_id="dec-00000002").error_code == "RISK_LATCHED"

def test_policy_tightened_between_approval_and_dispatch_refuses(world):        # spec 6, ai_paper path
    world.on_before_guard(lambda: world.policy_publish(replace(PAPER_LIMITS, position_fraction=0.01)))
    assert submit(world).error_code == "LIMIT_TIGHTENED_BEFORE_DISPATCH"

def test_attribution_links_for_the_entry_order_ref(world):
    submit(world)
    (link,) = world.decisions.links_for_order_ref("mmr:og-aip-dec-00000001")
    assert (link.decision_id, link.decider, link.strategy_version, link.policy_revision, link.style, link.digest) == (
        "dec-00000001", "jev", GOOD["strategy_digest"], 1, "intraday_long", world.digest)
```

  (`world.on_before_guard` wraps the guard's `revalidate` so a callback runs first; that is the race the spec names.)

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `AiPaperDecision.from_body`: strict checks by hand (no pydantic here, because the service is also called in-process): exact key set; `type(conid) is int and conid > 0`; `quantity is None or (type(quantity) is int and quantity >= 1)`; floats `isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0`; `expires_at` a `str` parsed with `datetime.fromisoformat` and `utcoffset() is not None`; regexes of R17; per-action shape of R16.
  - `AiPaperEntryOrder(command_id, conid, side, requested_quantity: Decimal, risk_fraction: Decimal, entry_policy, stop_policy, target_policy, account_mode="paper", artifact_id)`; `DeploymentBinding(artifact_id=digest, allowlist=tuple(str(c) for c in d.conids), max_gross_allocation=limits.gross_fraction, attested_strategy=_AttestedNotional(d.evidence_order_notional), expires_at=decision.expires_at)`. `risk_fraction = Decimal(str(limits.trade_risk_fraction))`.
  - `execute` follows the admission table; the decision row is written inside the same transaction that records the `RECEIVED → REJECTED` or `RECEIVED → VALIDATED` transition (`CommandSteps.transition_with(cmd, ..., extra=lambda conn: store.record_in_tx(conn, row))`), so the ledger and the decision row never disagree. Step 6 uses `coordinator_ledger.unresolved_for_target("conid", str(conid))` filtered to `action == AI_PAPER_ACTION`, state in `{"RECEIVED","VALIDATED","SUBMITTING","OUTCOME_UNKNOWN"}`, `command_id != cmd.command_id`; plus `exit_owners.owner_for(account, conid)` / `account_owner(account)` active. Steps 12–13 copy the shape of `AutomatedIntentCommandService._execute_via_saga` (same transitions and receipts: `SUBMITTED`, `REJECTED` with the saga code, `OUTCOME_UNKNOWN` + `schedule_reconcile` on an ambiguous dispatch).
  - `links_for_order_ref`: `decode_order_ref` (`order_correlation.py:26`) gives `og-<command_id>` for an entry; strip `og-`. Plan 1 close children carry their root id in the group (`liquidation_child_kind` and its root parser in `order_correlation.py`); map a close child to its root and return the decisions whose `close_root_id` is that root. Join `ai_deployments` and `ai_effective_limits` for the link fields.
- [ ] **Step 4: Run, expect PASS**, then `tests/automation/` and the full suite.
- [ ] **Step 5: Commit** — `feat: admit ai_paper entry decisions through the protective saga`.

---

### Task 8: Reductions (`CLOSE`, `PARTIAL_CLOSE`) on the safe close

**Files:**
- Create: `trader/automation/reduction_close.py`
- Modify: `trader/automation/automated_intent_command.py` (`_execute_close` delegates to the shared function; its tests stay unchanged); `trader/automation/ai_paper_decision.py` (reduction branch); `trader/trading/command_coordinator.py` (`OutcomeReconciler._reconcile_row`: the close-resolution action set becomes the constant `CLOSE_RESOLVED_ACTIONS = frozenset({"execute_automated_intent", "liquidate_account", AI_PAPER_ACTION})`)
- Test: `tests/automation/test_ai_paper_reductions.py`

**Interfaces:**
- Consumes: Plan 1 `LiquidationService.start(account_id, cause_command_id, deadline, *, scope="conid", conid, quantity, stop_price, target_price)`, `ExitInProgress`, `LiquidationRefused`, `close_resolution`; `ExitOwnerRegistry.account_owner`.
- Produces: `reduction_close.CloseOutcome(state: str, error_code: Optional[str], outcome: dict)`; `start_broker_proven_close(*, liquidation, broker, account_id, command_id, conid: int, side: str, quantity: Optional[float], stop_price=None, target_price=None, deadline: datetime) -> CloseOutcome` — the body of PR #46's `_execute_close` after the claim: fresh fenced snapshot, `held > 0`, side reduces (`SELL` for a long), `quantity ≤ held` (`NOT_A_REDUCTION` otherwise), `quantity >= held` becomes a full close, then `start(...)` with `ExitInProgress → EXIT_IN_PROGRESS`, `LiquidationRefused → its code`, other errors → `OUTCOME_UNKNOWN / DISPATCH_AMBIGUOUS`, success → `OUTCOME_UNKNOWN / CLOSE_PENDING` with `close_root_id`.

Reduction admission (spec 5.4; no entry window, budget, loss checks, policy revision or deployment; no `session_risk`):

| # | Check | Refusal / result |
|---|---|---|
| 1–3 | principal, decision shape, account fence | as Task 7 |
| 4 | experiment `None` / `STOPPED` | `NO_EXPERIMENT` / `EXPERIMENT_STOPPED` |
| 4b | `KILLED` (R15): account owner active → full-close join | `OUTCOME_UNKNOWN / CLOSE_PENDING`, `close_root_id` = the flatten root; no owner → `KILL_FLATTEN_PENDING` (retryable) |
| 5 | expiry | as Task 7 |
| 6 | blocking `ai_paper` command on the conid | `OUTCOME_UNKNOWN_PENDING` |
| 7 | claim `VALIDATED → SUBMITTING` **without** the pause check | — |
| 8 | `start_broker_proven_close(...)` (`PARTIAL_CLOSE` passes `quantity`, `stop_price`, `target_price`) | its codes |

- [ ] **Step 1: Write the failing tests** (real `LiquidationService`, `ExitOwnerRegistry`, run store and fake broker generations from Plan 1's test helpers in `tests/test_liquidation_service.py`; world from Task 7 with one 300-share long on `CONID` and its protective saga)

```python
CLOSE = {**ENTER, "action": "CLOSE", "side": "SELL", "deployment_digest": None, "policy_revision": None,
         "stop_price": None, "quantity": None}

def test_close_never_builds_a_bracket(world, monkeypatch):
    monkeypatch.setattr("trader.automation.protective_order_saga.build_bracket_plan",
                        lambda *a, **k: pytest.fail("close went through build_bracket_plan"))
    receipt = submit(world, CLOSE)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    root = receipt.outcome["close_root_id"]
    assert world.liquidation.receipt_for(root).scope == "conid"

def test_close_works_while_paused_and_after_a_daily_loss_breach(world):
    world.apply("paused"); world.policy.latch("DAILY_LOSS", "x"); world.broker.set(daily_pnl=-9_000.0)
    world.controls.pause()                                                       # trading pause too (R32)
    assert submit(world, CLOSE).error_code == "CLOSE_PENDING"

def test_a_reduction_of_a_denylisted_symbol_still_passes(world):             # owner answer 4
    world.filter_file.write(denylist=["AAPL"], deny_exchanges=["NASDAQ"])
    for body in (CLOSE, {**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100, "decision_id": "dec-00000003"}):
        assert submit(world, body).error_code == "CLOSE_PENDING"
    assert world.filter_loads == 0                                               # a reduction never reads the filter

def test_close_without_a_broker_position_is_refused(world):
    world.broker.set(positions=())
    assert submit(world, CLOSE).error_code == "NOT_A_REDUCTION"

@pytest.mark.parametrize("quantity", [301, 1_000])
def test_partial_larger_than_the_position_is_refused(world, quantity):
    assert submit(world, {**CLOSE, "action": "PARTIAL_CLOSE", "quantity": quantity}).error_code == "NOT_A_REDUCTION"

def test_partial_close_reaches_the_scoped_partial(world):
    receipt = submit(world, {**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100, "stop_price": 97.5})
    run = world.liquidation.receipt_for(receipt.outcome["close_root_id"])
    assert (run.goal, run.goal_quantity, run.stop_price) == ("partial", 100.0, 97.5)

def test_buy_side_close_of_a_long_is_not_a_reduction(world):
    assert submit(world, {**CLOSE, "side": "BUY"}).error_code == "NOT_A_REDUCTION"

def test_close_joins_the_kill_flatten_while_killed(world):
    flatten = world.liquidation.start(ACCOUNT, "kill-root", DEADLINE)            # account owner, as Plan 4's kill does
    world.apply("killed")
    receipt = submit(world, {**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100})
    assert receipt.outcome["close_root_id"] == "kill-root"
    assert world.liquidation_runs() == {"kill-root"}                             # no new run, no new work

def test_killed_without_a_flatten_yet_is_refused_retryable(world):
    world.apply("killed")
    receipt = submit(world, CLOSE)
    assert (receipt.error_code, receipt.retryable) == ("KILL_FLATTEN_PENDING", True)

def test_close_is_refused_after_stopped(world):
    world.apply("stopped")
    assert submit(world, CLOSE).error_code == "EXPERIMENT_STOPPED"

def test_close_joins_a_time_exit_root(world):                                    # spec 5.1 / R14
    time_exit = world.liquidation.start(ACCOUNT, "time-exit-1", DEADLINE, scope="conid", conid=CONID)
    assert submit(world, CLOSE).outcome["close_root_id"] == time_exit.cause_command_id

def test_partial_during_another_owners_close_is_exit_in_progress(world): ...
def test_new_decision_on_a_conid_with_a_pending_close_is_refused(world): ...    # OUTCOME_UNKNOWN_PENDING

def test_reconciler_resolves_the_close_command_from_its_root(world):
    receipt = submit(world, CLOSE)
    world.drive_close_to("CLOSED", receipt.outcome["close_root_id"])           # broker generations to zero
    world.reconciler.reconcile(receipt.command_id, NOW)
    assert world.ledger.get(receipt.command_id).state == "RESOLVED"

def test_entry_after_the_close_is_resolved_is_not_blocked(world): ...

def test_one_strategy_sell_close_is_unchanged(automated_world):                 # extraction did not change PR #46
    ...                                                                          # rerun one case of tests/automation/test_automated_command_boundary.py
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Move the post-claim body of `AutomatedIntentCommandService._execute_close` into `start_broker_proven_close`; the service keeps its account check and claim and maps the `CloseOutcome` to its existing transitions. In `AiPaperDecisionService.execute`, branch on `decision.action` after step 3. For `KILLED`: `owner = exit_owners.account_owner(account_id)`; `None` → reject `KILL_FLATTEN_PENDING` with `retryable=True`; else call `start_broker_proven_close` with `quantity=None` (R15), which joins. Record `close_root_id` on the decision row from the outcome. Add `AI_PAPER_ACTION` to `CLOSE_RESOLVED_ACTIONS` in `_reconcile_row`.
- [ ] **Step 4: Run** the new file, `tests/automation/test_automated_command_boundary.py`, `tests/test_liquidation_service.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: route ai_paper closes through the broker-proven safe close`.

---

### Task 9: RPC methods, allow-list entries and stack composition

**Files:**
- Modify: `trader/messaging/production_api.py` (wire models next to `ExecuteAutomatedIntentRequest` :681; handlers next to `_execute_automated_intent_rpc_handler` :1108; registration in `build_production_registry` near the automated-intent block); `trader/messaging/principals.py` (`TRADER_ACL`); `trader/trading/command_stack.py` (`build_command_stack` :578: migrations 54–56, `_build_ai_paper_services`, `DispatchGuard.current_limits` router, `CommandStack.ai_paper` field :404-430); `AGENTS.md` (one paragraph under "Key Patterns", the three commands under "CLI Commands" are not added: SP1 has no CLI for them)
- Test: `tests/test_ai_paper_rpc.py` (new), `tests/test_command_stack.py` (composition cases), `tests/test_rpc_acl.py` (the ACL table test)

**Interfaces:**
- Consumes: everything above; Plan 2 `register(..., with_caller=True)`, `RpcCaller`, `CommandRequest.principal`, `TRADER_ACL`.
- Produces:
  - Wire models (all `ConfigDict(extra="forbid", strict=True)`, floats `Field(allow_inf_nan=False)`): `PublishAiRiskPolicyRequest(command_id: str, limits: dict[str, int | float], reason: str)` (`command_id` checked with `_reject_colon_in_command_id`); `RegisterAiDeploymentRequest(deployment: dict)`; `SubmitAiPaperDecisionRequest` (the 12 decision fields, `expires_at: str`; a `field_validator` applies the R17 `decision_id` regex, so `dec:0001` is a `VALIDATION_ERROR` before `CommandRequest` is built); `GetAiRiskPolicyRequest()`; `GetAiDeploymentRequest(digest: str)`. Domain parsing (`RiskLimits.from_json`, `AiDeployment.from_json`, `AiPaperDecision.from_body`) runs again in the service.
  - ACL entries:

```python
    ("command", "publish_ai_risk_policy"): frozenset({"ai_supervisor"}),
    ("command", "submit_ai_paper_decision"): frozenset({"ai_supervisor"}),
    ("command", "register_ai_deployment"): frozenset({"ai_research"}),
    # R23 / owner answer 6: explicit sets per method, never a group alias
    ("query", "get_ai_risk_policy"): frozenset({"cli", "dashboard", "ai_supervisor"}),
    ("query", "get_ai_deployment"): frozenset({"cli", "dashboard", "ai_supervisor", "ai_research"}),
```

  - Coordinator actions: `publish_ai_risk_policy` and `register_ai_deployment` single-step (`saga=False`; the handler returns the outcome dict or raises `CommandValidationError(code, message)`), `submit_ai_paper_decision` `saga=True`. `register_ai_deployment`'s handler parses the body with `AiDeployment.from_json` first (refusal → `VALIDATION_ERROR`) and passes the canonical `to_json()` (conids sorted) as `CommandRequest.body`, with `command_id = "aidep-" + digest_hex[:48]`; so a re-registration with conids in another order replays instead of conflicting. `target_type`: `"ai_policy"` / `"ai_deployment"` / `"conid"`.
  - `CommandStack.ai_paper: Optional[AiPaperServices]` (`policy`, `deployments`, `decisions`, `decision_store`); `trader.ai_paper_attribution = decision_store` (Plan 5 reads `links_for_order_ref` there).
  - `DispatchGuard.current_limits` router: `request.action == AI_PAPER_ACTION` → `policy.effective_limits()`; else `PAPER_LIMITS`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ai_paper_rpc.py — `served` builds the real stack (build_command_stack with the command-stack
# fixtures of tests/test_command_stack.py, ai_paper enabled, a fake ExperimentStatePort set to ARMED)
# and the real build_production_registry behind a TypedRpcServer; clients sign with make_identities().

def test_supervisor_publishes_and_research_registers(served):
    rev = served.client("ai_supervisor").call("publish_ai_risk_policy",
        {"command_id": "pol-1", "limits": PAPER_LIMITS.to_json(), "reason": "start"}, dict)
    assert rev["outcome"]["revision"] == 1
    dep = served.client("ai_research").call("register_ai_deployment", {"deployment": GOOD}, dict)
    assert dep["outcome"]["digest"].startswith("sha256:")

@pytest.mark.parametrize("principal,method", [
    ("ai_supervisor", "register_ai_deployment"), ("ai_research", "submit_ai_paper_decision"),
    ("ai_research", "publish_ai_risk_policy"), ("cli", "submit_ai_paper_decision"),
    ("dashboard", "publish_ai_risk_policy"), ("strategy", "submit_ai_paper_decision")])
def test_wrong_principal_is_denied_by_the_allow_list(served, principal, method):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client(principal).call(method, valid_body(method), dict)
    assert exc.value.code == "PERMISSION_DENIED"

def test_wrong_principal_is_refused_by_the_service_too(served):
    receipt = served.coordinator.execute(decision_request(principal="cli"))    # bypasses the ACL
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"
    receipt = served.coordinator.execute(register_request(principal="ai_supervisor"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"

@pytest.mark.parametrize("patch", [{"conid": True}, {"conid": 1.0}, {"quantity": 1.5}, {"stop_price": "98"},
                                   {"extra": 1}, {"expires_at": 1}, {"decision_id": "dec:0001"}])
def test_wire_is_strict(served, patch):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("ai_supervisor").call("submit_ai_paper_decision", {**valid_body("submit_ai_paper_decision"), **patch}, dict)
    assert exc.value.code == "VALIDATION_ERROR"

def test_policy_limits_on_the_wire_are_strict(served):
    for bad in ({**PAPER_LIMITS.to_json(), "max_positions": True}, {**PAPER_LIMITS.to_json(), "gross_fraction": "0.06"}):
        with pytest.raises(TypedRpcRemoteError):
            served.client("ai_supervisor").call("publish_ai_risk_policy", {"command_id": "p", "limits": bad, "reason": "r"}, dict)

def test_reregistering_with_reordered_conids_replays(served):
    first = served.client("ai_research").call("register_ai_deployment", {"deployment": GOOD}, dict)
    again = served.client("ai_research").call("register_ai_deployment",
        {"deployment": {**GOOD, "conids": list(reversed(GOOD["conids"]))}}, dict)
    assert again == first

def test_publish_with_the_broker_down_is_a_retryable_refusal(served):
    served.broker.fail_capture = True
    out = served.client("ai_supervisor").call("publish_ai_risk_policy",
        {"command_id": "pol-9", "limits": PAPER_LIMITS.to_json(), "reason": "r"}, dict)
    assert (out["state"], out["error_code"], out["retryable"]) == ("REJECTED", "BROKER_SNAPSHOT_UNAVAILABLE", True)

def test_end_to_end_enter_through_the_stack(served):
    publish(served); digest = register(served)
    out = served.client("ai_supervisor").call("submit_ai_paper_decision", enter_body(digest), dict)
    assert out["state"] == "SUBMITTED" and served.dispatch.plans

def test_reads(served):
    view = served.client("ai_supervisor").call("get_ai_risk_policy", {}, dict)
    assert set(view) >= {"latest_published_revision", "effective", "effective_revision", "queued", "ceiling", "latch_code"}
    assert served.client("ai_research").call("get_ai_deployment", {"digest": register(served)}, dict)["deployment"] == GOOD_SORTED

def test_disabled_ai_paper_registers_nothing(served_disabled):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served_disabled.client("ai_supervisor").call("submit_ai_paper_decision", valid_body(...), dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"

def test_no_method_writes_the_owner_ceiling(served):
    methods = {m for _, m in TRADER_ACL}
    assert not [m for m in methods if "ceiling" in m]
```

```python
# tests/test_rpc_acl.py (added)
def test_ai_principals_get_the_ai_paper_rights_and_no_trading_rights():
    rights = lambda p: {k for k, v in TRADER_ACL.items() if p in v}
    assert ("command", "register_ai_deployment") in rights("ai_research")
    assert {("command", "publish_ai_risk_policy"), ("command", "submit_ai_paper_decision")} <= rights("ai_supervisor")
    for p in ("ai_supervisor", "ai_research"):            # inclusion/exclusion only: Plan 5 adds scoreboard reads
        assert not {("command", m) for m in ("approve_proposal", "execute_automated_intent", "liquidate_account",
                                             "resume_trading", "create_proposal")} & rights(p)
    assert ("command", "register_ai_deployment") not in rights("ai_supervisor")
    assert not {("command", "publish_ai_risk_policy"), ("command", "submit_ai_paper_decision")} & rights("ai_research")

AI_PAPER_FAMILY = {                                                    # R23, owner answer 6: exact, method by method
    ("command", "publish_ai_risk_policy"): {"ai_supervisor"},
    ("command", "submit_ai_paper_decision"): {"ai_supervisor"},
    ("command", "register_ai_deployment"): {"ai_research"},
    ("query", "get_ai_risk_policy"): {"cli", "dashboard", "ai_supervisor"},
    ("query", "get_ai_deployment"): {"cli", "dashboard", "ai_supervisor", "ai_research"},
}

def test_ai_paper_family_rights_are_exact():
    assert {k: set(TRADER_ACL[k]) for k in AI_PAPER_FAMILY} == AI_PAPER_FAMILY
    assert {k for k, v in TRADER_ACL.items() if "ai_research" in v and k in AI_PAPER_FAMILY} == {
        ("command", "register_ai_deployment"), ("query", "get_ai_deployment")}
    assert not [k for k in AI_PAPER_FAMILY if {"strategy", "scheduler", "trader"} & set(TRADER_ACL[k])]
```

```python
# tests/test_command_stack.py (added)
def test_ai_paper_on_a_live_account_refuses_to_build(live_trader):
    live_trader.ai_paper_config = AiPaperConfig(enabled=True)
    with pytest.raises(CommandStackConfigurationError, match="AI_PAPER_LIVE_REFUSED"):
        build_command_stack(live_trader, ...)

def test_ai_paper_stack_uses_no_experiment_until_plan_four(paper_trader):
    stack = build_command_stack(paper_trader_with(AiPaperConfig(enabled=True)), ...)
    assert isinstance(stack.ai_paper.decisions._experiments, NoExperiment)

def test_dispatch_guard_routes_current_limits_by_action(paper_stack): ...
def test_dispatch_guard_gets_the_ai_gate_and_strict_margin_for_ai_paper_only(paper_stack):   # R25
    guard = paper_stack.dispatch_guard
    assert guard._strict_margin_actions == frozenset({AI_PAPER_ACTION})

```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Handlers are `with_caller=True` and build `CommandRequest(..., source=caller.principal, principal=caller.principal)` (Plan 2 ruling 6). The publish action calls `policy.publish(limits, reason=..., principal=cmd.principal, command_id=cmd.command_id, broker=broker_snapshot.capture(account))` after checking `cmd.principal == "ai_supervisor"`; `PolicyRefused` becomes `CommandValidationError(code, message)`, and a failed capture becomes `CommandValidationError("BROKER_SNAPSHOT_UNAVAILABLE", ...)` (retryable), so the handler never raises raw. The register action checks `cmd.principal == "ai_research"` and the deployment style against `config.styles` (`STYLE_NOT_ENABLED`).
  - `_build_ai_paper_services(trader, ...)`: returns `None` unless `trader.ai_paper_config.enabled`; a live `account_mode` raises `CommandStackConfigurationError("AI_PAPER_LIVE_REFUSED", ...)`. Builds the policy service (ceiling from config), deployment store, decision store, `AiEntryFilter(universe=trader.universe_accessor)` (R6), `AiPaperEvidence` (same broker/quotes/margin/history/journal as `_build_automated_intent_service`, `entry_offset_bps=AI_ENTRY_POLICY.limit_offset_bps`, `entry_filter`), the `DispatchGuard` with `ai_entry_gate(entry_filter=...)` and `strict_margin_actions=frozenset({AI_PAPER_ACTION})` (R25), and `AiPaperDecisionService` with the stack's `protective_order_saga`, `liquidation_service`, its `ExitOwnerRegistry`, `NoExperiment()` (R20).
  - `build_production_registry`: register the five methods only when `ai_paper is not None`.
  - `AGENTS.md`: one paragraph "**`ai_paper` path (SP1)**" naming the modules, the three commands and their principals, the owner ceiling key, that decisions are refused until experiments exist (Plan 4), and the migrations 54–56; a "Follow-ups (SP2)" line: the AI strategy runner must hash the loaded file and refuse a mismatch with the deployment's claimed `strategy_digest` before generated code can trade (R19).
- [ ] **Step 4: Run** the three test files, then the full suite. Then `grep -rn "0\.06\b" trader/automation trader/promotion trader/trading` must find only `risk_limits.py`.
- [ ] **Step 5: Commit** — `feat: expose the ai_paper path over typed rpc with its allow-list entries`.

---

### Task 10: Cancel AI entries at the entry cutoff (R26)

**Files:**
- Create: `trader/automation/ai_entry_cutoff.py`
- Modify: `trader/automation/session_controller.py` (`SessionController(..., on_entry_cutoff: Callable[[SessionControllerState, datetime], None] | None = None)`, called in `run_due` step 1 on every tick from `entry_cutoff_utc` until the flatten starts; `_close_oversized_protection` skips `og-aip-*` groups, which R26 re-protects), `trader/trading/liquidation_service.py` (goal `reprotect` for `scope="conid"`), `trader/automation/ai_risk_policy.py` (`cutoff_cancel_state` read and forward-only write), `trader/trading/command_stack.py` (wire the hook when `ai_paper` is built)
- Test: `tests/automation/test_ai_entry_cutoff.py`, `tests/test_liquidation_service.py` (reprotect goal), `tests/test_safe_close_integration.py` (composition case)

**Interfaces:**
- Consumes: Plan 1 `SessionController.run_due`, `SessionCancelAdapter`, `LiquidationService.start(scope="conid", ...)`, the exit-only OCA refs `{root}-reprotect-stop|target`; Task 4 `AiRiskPolicyService`.
- Produces: `AiEntryCutoff(*, broker, cancel, liquidation, policy, account_id, now)` with `on_entry_cutoff(state, now) -> None`; `LiquidationService.start(..., scope="conid", conid, goal="reprotect", stop_price, target_price)`; `is_ai_entry(order) -> bool` (`og-aip-` group, leg `entry`, not external).

- [ ] **Step 1: Write the failing tests** (`composed_ai` = Plan 1's `_Composed` from `tests/test_safe_close_integration.py` with `ai_paper` on and a session row for today. Helpers on it: `working_ai_entry(decision_id, quantity) -> entity` adds a working `og-aip-<id>:entry`; `make_cancel(outcome)` makes the next cancel raise, or leaves the entry `PendingCancel` / `Submitted` on the next `promote()`; `cutoff_state()` reads `ai_paper_sessions.cutoff_cancel_state`; `ai_bracket(id, entry_qty, filled, stop, target) -> og` writes a durable saga and broker rows like `protected_entry` with the entry partly filled; `drive_cutoff_cancel(state, og)` runs the cutoff tick, lands the cancel and promotes; `drive_until_done(state)` alternates `promote()` and `tick()` until the reprotect root is terminal.)

```python
def test_ai_entry_is_cancelled_at_the_cutoff_not_five_minutes_later(composed_ai):        # rule 1
    composed_ai.sim.add_order("og-aip-dec-00000001:entry", "og-aip-dec-00000001", "entry", "BUY", "LMT", 10)
    composed_ai.sim.add_order("og-entry-1:entry", "og-entry-1", "entry", "BUY", "LMT", 10)    # old-path entry
    state = composed_ai.stack.session_controller.recover(composed_ai.clock[0])
    composed_ai.run_session(state.entry_cutoff_utc)
    assert composed_ai.sim.cancelled == ["og-aip-dec-00000001:entry"]                         # old path waits for 15:35
    composed_ai.run_session(state.cancel_entries_utc)
    assert "og-entry-1:entry" in composed_ai.sim.cancelled

@pytest.mark.parametrize("outcome", ["raises", "pending_cancel_on_newer_generation", "still_working_on_newer_generation"])
def test_an_ambiguous_cancel_is_reconciled_from_the_broker(composed_ai, outcome):         # rule 2
    entry = composed_ai.working_ai_entry("dec-00000001", quantity=10)
    composed_ai.make_cancel(outcome)
    state = composed_ai.stack.session_controller.recover(composed_ai.clock[0])
    composed_ai.run_session(state.entry_cutoff_utc)
    assert composed_ai.cutoff_state() == "AMBIGUOUS"
    composed_ai.sim.promote(); composed_ai.run_session(state.entry_cutoff_utc + dt.timedelta(seconds=5))
    if outcome == "still_working_on_newer_generation":
        assert composed_ai.sim.cancelled.count(entry) == 2                       # same cancel again, same child id
    composed_ai.cancel_landed(entry); composed_ai.sim.promote()
    composed_ai.run_session(state.entry_cutoff_utc + dt.timedelta(seconds=10))
    assert composed_ai.cutoff_state() == "DONE"
    assert [p for p in composed_ai.sim.placed if p[1] == "MKT"] == []           # no reduce, no new entry

def test_a_partial_fill_keeps_protection_sized_to_the_fill(composed_ai):               # rule 3
    og = composed_ai.ai_bracket("dec-00000001", entry_qty=10, filled=4, stop=95.0, target=120.0)   # children still 10
    state = composed_ai.stack.session_controller.recover(composed_ai.clock[0])
    composed_ai.drive_cutoff_cancel(state, og)                                   # entry rest cancelled, 4 held
    composed_ai.drive_until_done(state)
    legs = [p for p in composed_ai.sim.placed if "-reprotect-" in p[0]]
    assert sorted((p[1], p[3], p[4]) for p in legs) == [("LMT", 4.0, 120.0), ("STP", 4.0, 95.0)]
    assert composed_ai.sim.held[CONID] == 4.0                                    # re-protected, not closed

def test_children_already_shrunk_by_the_broker_send_nothing(composed_ai): ...    # children == 4: no reprotect run
def test_a_failed_reprotect_escalates_to_a_full_close_and_the_breaker(composed_ai): ...   # Plan 1 escalation
def test_the_flatten_still_closes_the_rest_by_the_close(composed_ai): ...       # 15:45: one MKT SELL of the held quantity
def test_cutoff_state_survives_a_restart(composed_ai, tmp_path): ...            # ISSUED after restart; no second cancel round before a newer generation
```

```python
# tests/test_liquidation_service.py (added)
def test_reprotect_goal_replaces_oversized_legs_without_a_reduce(tmp_path): ...
    # REQUESTED -> CANCELLING -> VERIFYING -> REPROTECTING -> VERIFYING -> DONE; no child of kind 'reduce'
def test_reprotect_goal_is_refused_without_a_position(tmp_path): ...            # NOT_A_REDUCTION-style refusal: NO_POSITION
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `AiEntryCutoff.on_entry_cutoff`: read `policy.current()`; `None` (no session today) → return. State `NULL`: capture, cancel each `is_ai_entry` working order through `SessionCancelAdapter`-style calls with child ids `{SessionController.cancel_command_id(account, date)}-aip-{order_entity_id}`, then write `ISSUED` (a raise from the cancel writes `AMBIGUOUS` and one incident). State `ISSUED` / `AMBIGUOUS`: capture; if the capture's generation is not newer than the one recorded at the cancel, return; otherwise classify each targeted entry (gone or `Cancelled` / `Filled` / still working) and resend only for still working; when none is working, write `DONE` and, for each filled entry whose working stop or target exceeds the position, call `liquidation.start(account, f"{cancel_root}-aip-reprotect-{conid}", deadline, scope="conid", conid=conid, goal="reprotect", stop_price=..., target_price=...)` with the saga's original prices. In `LiquidationService`, `goal="reprotect"` reuses the partial-close states and the exit-only OCA code with the reduce step left out; its exit-owner claim is a normal scoped claim, so a flatten supersedes it.
- [ ] **Step 4: Run** the three files, `tests/automation/test_session_controller.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: cancel ai_paper entries at the entry cutoff and re-protect partial fills`.

---

## Self-review against the spec

| Spec requirement (5.4, 2, 3, 6) | Task |
|---|---|
| `RiskLimits` dataclass with the seven fields | 1 |
| `PAPER_LIMITS` = what runs today (gross 6% from the clamp, not 15%) | 1 |
| Pending entry orders enforced in the `ai_paper` factory only (`MAX_PENDING_ENTRIES`) | 6 |
| `evaluate` takes limits; `PortfolioRiskBudget` takes the same object; no second evaluator | 1 (R4, R5) |
| Old path passes `PAPER_LIMITS`; parity via `evaluate`; gross stops at 6% | 1 |
| Attested notional kept on the old path; `evidence_order_notional` in `ai_paper` | 1, 6, 7 (R12) |
| Effective limits re-checked at dispatch (`AllocationPolicy` + `DispatchGuard`) | 2, 6 |
| Tighter of approved and current; `LIMIT_TIGHTENED_BEFORE_DISPATCH`; failing test first; both paths | #47 (prerequisite PR, R1), 2 (current limits, R2 rename), 7 |
| 15% steady cap stays, same constant as the ceiling's code maximum | 1 |
| Owner ceiling in `trader.yaml`; missing key → `PAPER_LIMITS`; code maximum table | 3 |
| Bad type / non-finite / ≤ 0 / above maximum fails config load | 3 |
| No AI method writes the ceiling; raised ceiling only from the next session | 9, 4 (R9) |
| Drawdown guard (test-only raised maximum) | 3 |
| `publish_ai_risk_policy`, `ai_supervisor` only, append-only with revision, reason, time | 4, 9 |
| Structural checks; above ceiling refused (`POLICY_ABOVE_CEILING`), not clamped | 4 |
| Effective limits per field with their own revision and source revision | 4 |
| Tighter now, looser queued, mixed both; session start takes latest capped; restart is not a session start | 4 (R7, R8) |
| Daily-loss anchor frozen; budget = anchor × fraction; old path keeps its formula | 1, 4 |
| Durable breach latch, survives revisions and restart | 4, 7 (R10) |
| No accepted policy → no entries | 4, 7 |
| Deployment record fields, `register_ai_deployment` by `ai_research` only, sealed, digest match, never arms the old path | 5, 7, 9 (R19) |
| Decision fields; entry admission list; reduction admission list | 7, 8 |
| Translated into the existing coordinator / saga; coordinator checks each refuse with their own code | 7 |
| Factory captures snapshot, quote, margin, HWM, liquidity; drops only `QUANTITY_REQUIRED` | 6 |
| Sizing = minimum over limits; no qty → max; above max refused; below one share refused | 6 (R11, R12) |
| `ENTER` via the protective saga; closes via the scoped close, never `build_bracket_plan` | 7, 8 |
| One command per `decision_id`; `OUTCOME_UNKNOWN_PENDING` for a new id | 7 (R13) |
| Every refusal has a distinct, recorded code | 7, 8 |
| `ai_supervisor` cannot call `register_ai_deployment` | 9 |
| Owner answer 1: session on the first decision, keyed by XNYS date, anchor and latch durable before admission, never two sessions per date | 4, 7 (R7) |
| Owner answer 2: first valid policy applies at once; then tighter now, looser next session (spec amended 2026-10-06) | 4 (R8) |
| Owner answer 3: AI entry refuses missing / invalid / timed-out margin at approval and dispatch; old path keeps the warning | 6, 7 (R11, R25) |
| Owner answer 4: `trading_filters.yaml` on AI entries from the resolved instrument, re-checked at dispatch; reductions never blocked | 6, 7, 8 (R6, R25) |
| Owner answer 5: trader-owned DAY marketable limit ≤ 10 bps through a fresh ask, 15-minute expiry re-checked at dispatch, session cancel and flatten kept | 6, 7, 10 (R17, R18, R25) |
| Owner decision: AI entries cancelled at the entry cutoff, ambiguous cancel reconciled, protection sized to a partial fill | 10 (R26) |
| Owner answer 6: method-by-method explicit rights, reads and mutations separate | 9 (R23) |

Not in this plan (by design): experiments, arming, the kill line and the one-strategy lock (Plan 4; Plan 3 only parses the kill keys and reads experiments through `ExperimentStatePort`); the scoreboard, `ai_costs`, Telegram (Plan 5; Plan 3 provides `links_for_order_ref` and the decision table); the acceptance harness and the real IB paper session (Plan 6); any model call (SP2).

## Open questions for the owner

Answered by the owner on 2026-10-06 and folded into the rulings: session start (R7), first policy (R8), margin (R11), trading filter (R6), decision expiry and entry order type (R17, R18), reads (R23); then acknowledged entries (R13), the renamed ceiling refusal (R2), the claimed strategy digest (R19) and no environment overrides (R22). The migration split (54–56) is settled across Plans 3–5. The filter-exchange answer (R6: exact `primaryExchange`, never `SMART`, blank refuses `INSTRUMENT_UNRESOLVED`) is folded into R6 and R25. None open.
