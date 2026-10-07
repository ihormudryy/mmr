# AI Paper SP1 — Plan 4: Experiments, Arming and the Kill Line — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the tasks in number order; each task ends with the full suite green.

**Goal:** Give the `ai_paper` mode a durable experiment (`ARMED` / `PAUSED` / `KILLED` / `STOPPED`) that the owner arms only on a flat paper account, that locks out the one-strategy automation in both directions, and whose optional kill line flattens the whole account through Plan 1's account flatten when the drawdown from the start (or the peak) reaches `experiment_kill_drawdown_pct`.

**Architecture:** Four new trader-owned modules in `trader/automation/`: `experiments.py` (table, record, state machine, store), `kill_line.py` (pure evaluation), `experiment_service.py` (start / pause / resume / stop and the arming checks) and `kill_monitor.py` (the kill check, the kill flatten driver and the notices to Plan 5). The monitor ticks on the liquidation worker next to `SessionController.run_due`; on a hit it writes `KILLED` first, then asks `SessionController` to start an account flatten, which goes through `LiquidationService.start(scope="account")` and therefore through Plan 1's account-owner claim and takeover rules. Plan 3 reads experiments through its `ExperimentStatePort`; this plan implements it and adds an experiment check at dispatch. Five typed RPC methods and `mmr experiment …` expose it to `cli` and `dashboard`.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 (strict wire models), pytest. No new dependencies. No model calls.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md` (lands with PR #46; not on master. Immutable source: PR #46 commit `2a021c204798907736b7d3380901c0d7f40e7ac6`. Read it with `git fetch origin 2a021c204798907736b7d3380901c0d7f40e7ac6 && git show 2a021c204798907736b7d3380901c0d7f40e7ac6:docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, or `gh pr checkout 46`.) Do not copy it. Binding parts: section 5.5 "Arming and kill line" (main source), the kill-line rows of sections 2 and 3, the `experiments` table of 5.2, the experiment rights of 5.3, the reduction rules for `KILLED` / `STOPPED` in 5.4, the "Arming and kill line" bullets of section 6, delivery step 4 of section 7.

## Global Constraints

- **Order of merges.** Builds on Plan 1 (safe close, PR #46: `LiquidationService.start(scope=...)`, `LiquidationService.receipt_for`, `LiquidationRunStore.roots_to_advance_in_tx`, `ExitOwnerRegistry.account_owner`, `SessionController._issue_flatten`, all in `trader/trading/liquidation_service.py`, `trader/trading/exit_owner.py`, `trader/automation/session_controller.py`), Plan 2 (identities: `principals.TRADER_ACL`, `HUMAN`, `ACCOUNT_READERS`, `RpcCaller`, `register(..., with_caller=True)`, `CommandRequest.principal`, `ServiceIdentity`, `trader.rpc_identity`, `TypedRpcRegistration.allowed_principals`) and Plan 3 (`AiPaperConfig` with `experiment_kill_drawdown_pct` / `experiment_kill_basis` / `raw_section`, `ExperimentStatePort`, `ExperimentView`, `NoExperiment`, `AiPaperDecisionService`, `AI_PAPER_ACTION`, `DispatchGuard(current_limits=...)`, `CommandStack.ai_paper`, `_build_ai_paper_services`). Start Task 1 only after all three are on master. Master lines cite `8116f6a5`; Plan 1–3 code is cited by file and function name, which is the anchor.
- **Kill flatten = Plan 1's account flatten.** The kill never calls the dispatch, never claims `exit_owners` itself and never bypasses `LiquidationService.start(scope="account")`. `SessionController` starts it (spec 5.5 step 2).
- **`KILLED` is durable before any order** (spec 5.5 step 1). It is committed in its own transaction before `SessionController` is called.
- **"Flat" only on broker evidence** (spec 5.5 step 4): the kill flatten root is `FLAT` **and** a later fenced capture shows no positions, no working orders and no unresolved command.
- **Fail closed.** A missing or invalid broker capture never creates a kill and never lets an `ENTER` through: while the kill line cannot be evaluated, entries are refused `KILL_LINE_UNKNOWN`. Every arming check that cannot be proven refuses with its own code.
- **Strict input.** Wire models use `ConfigDict(extra="forbid", strict=True)`; `experiment_id` and `command_id` are checked with regexes; `True` is never an int and `1` is never a bool. Domain functions re-check types (`type(x) is float or type(x) is int`, never `isinstance(x, int)` alone).
- **Journal migration: 70** (Plan 4 holds 70–79; Plan 1 35–38, Plan 3 54–56, Plan 5 60–69). Tables live in `trader.journal_db` (trader-owned; no AI container mounts it). Only trader_service writes them.
- **Old path.** The one-strategy `paper-v1` path is unchanged except that arming it is refused while an experiment is `ARMED`, `PAUSED` or `KILLED` (spec 5.5 "Lock in both directions").
- **Paper only.** `start_experiment` refuses a live account (`ACCOUNT_NOT_PAPER`).
- `CommandReceipt` stays frozen. `OUTCOME_UNKNOWN` is never resubmitted under a fresh id.
- Test-first. Single files: `.venv/bin/python -m pytest <path> -q --timeout=30`. Full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects: `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order or deploy is authorized by this plan.
- **Owner answers of 2026-10-06** are binding and carried by K1, K7, K9, K10 (replaced), K11, K12, K14, K17, K18 and the new K23. Where they differ from spec 5.5 (a killed experiment can no longer be resumed), the owner answer wins.

## Rulings (spec silent, ambiguous, or in conflict)

Binding for this plan. Owner-visible ones are repeated under "Open questions".

- **K1. Where and how often the kill line is checked.** The spec says "every promoted broker snapshot". A promoted generation changes only when `run_broker_sync` runs, which is at (re)connect and on a close's `_BrokerGenerationRefresh.request_refresh` (`command_stack.py:258-282` on the PR #46 branch; the class docstring says so). Between promotions, live account and position updates are applied to the broker tables (`BrokerIngest.on_account_value`, `broker_ingest.py:377-380`) and `capture_risk_snapshot_in_tx` includes them, fenced by `source_cursor` (`broker_state.py` docstring of `capture_risk_snapshot_in_tx`). So the monitor evaluates **every fenced `BrokerRiskSnapshot` it captures**, every 5 s, on the liquidation worker (Plan 1 requires every `LiquidationService` entry point on that one worker). It never reads unfenced values and never forces a broker sync (a full IB enumeration every 5 s is too heavy). Detection latency is therefore IB's account-update cadence (about 3 minutes for `NetLiquidation`). **Owner answer:** this cadence is accepted for paper only. It is disclosed in `AGENTS.md`, `docs/OPERATIONAL_STATE.md` and in `get_experiment` / `mmr experiment status` (`kill_line.detection = "IB account updates, about 3 minutes; paper only"`). Protective stops, entry gates (admission, `session_risk`, the dispatch gates) and the session-close cancel and flatten never depend on it. Before any live use, a faster, verified detector is required: Task 7 adds it as a **live blocker** to the "Known blockers" list in `docs/OPERATIONAL_STATE.md`.
- **K2. The kill flatten does not end the session schedule.** `SessionController` gets `flatten_account_now(cause_command_id, deadline) -> str`, which shares the start-and-busy logic of `_issue_flatten` (extracted into `_start_account_flatten`) but does **not** change the session state. Why: if the kill moved the session to `FLATTENING` / `FLAT`, a same-day resume would leave no end-of-day flatten (`run_due` skips a terminal session) and an intraday position could stay overnight. The monitor polls the kill root itself. The kill deadline is `now + 300 s` (the `LiquidationService` default `deadline_seconds`), not the session's `flat_deadline_utc`; a missed deadline is `FAILED_SAFE` plus the breaker inside `LiquidationService`, as today.
- **K3. Kill vs session flatten.** Both are account owners. Whichever claims first owns the root; the other gets `JOINED_FLATTEN` from `ExitOwnerRegistry.claim_account_in_tx` and follows that root. The monitor stores the root it got back (`kill_flatten_root`) and polls it, never its own planned id.
- **K4. Kill root ids.** `kill_root_id = f"experiment-kill-{experiment_id}-{kill_seq}-{kill_round}"` (no colon; `_check_root_id`). `kill_seq` counts kills (a resume then a second kill gets a new one); `kill_round` counts flatten rounds inside one kill (K6).
- **K5. Restart while `KILLED`.** `KillLineMonitor.recover()` runs on the worker before readiness, after session recovery. If `kill_flatten_root` is recorded it only polls `receipt_for(root)` (calling `start` again with a released root would raise "root id … was already used"). If only `kill_root_id` is recorded (crash between the `KILLED` write and `start`), it first looks up `receipt_for(kill_root_id)`; a receipt means `start` had committed, so it adopts that root; no receipt means it calls `flatten_account_now`.
- **K6. Exposure after "flat".** An entry validated before the kill can be acknowledged after the flatten read the broker. Flat is reported only on a capture that is newer than or equal to the root's `generation_id` and shows no positions, no working orders and `reconciliation_safe()`. While `KILLED`, every tick re-checks the capture; any position or working order after a `FLAT` root starts a new round (`kill_round + 1`, new root) and resets `kill_flat_state` to `PENDING`. A `FAILED_SAFE` root ends the kill with `kill_flat_state = FAILED_SAFE` (the breaker is already tripped by `LiquidationService`); no new round starts on its own, so no second order follows an unknown outcome.
- **K7. Unknown P&L.** A capture that raises (`BrokerRiskSnapshotError`: `NO_PROMOTED_GENERATION`, `GENERATION_STAGING`, `INVALID_NET_LIQUIDATION`, `DAILY_PNL_UNAVAILABLE`, `ACCOUNT_MISMATCH`, …), a non-paper account mode, or a generation lower than the last one seen is **unknown**. Unknown never kills (a kill is irreversible and flattens the account) and never changes the experiment state. While the kill line is set and the last successful evaluation is older than 30 s (or none in this process), `ExperimentView.entry_block = "KILL_LINE_UNKNOWN"` and every `ENTER` is refused, at admission and at dispatch. Reductions still work. One journal incident `experiment.kill_line_unknown` is written per unknown streak. A long outage pauses the experiment (K23).
- **K8. Peak.** `peak_net_liquidation` is a column of the experiment, raised (never lowered) on every successful evaluation, also while `PAUSED`. It is separate from Plan 3's `CanaryRiskStore("ai_paper:<id>")` high-water mark, which is updated only at decision admission; writing a journal event every 5 s there is too noisy. Both checks act; the tighter one acts first (spec 5.5).
- **K9. Frozen kill line, tighter config wins.** `experiment_kill_drawdown_pct` and `experiment_kill_basis` are copied into the experiment at start. The effective percent is the smaller of the frozen value and the loaded `trader.ai_paper_config` value (`None` = off). That config is read once at process start (Plan 3 R22), so an edit to `trader.yaml` takes effect at the next trader_service start, as Plan 3 R9 does for the ceiling; nothing re-reads the file. A looser or removed config value has no effect until a new experiment. The basis stays the frozen one; a different config basis is logged once at WARNING. Why: the owner can always tighten at once; loosening mid-experiment would change the run being measured. **Owner answer (operator visibility):** an edit takes effect at the next restart, and until then the operator is told so. `get_experiment` returns `kill_line = {"active": {pct, basis} | None, "configured": {pct, basis} | None | "UNREADABLE", "pending_restart": bool, "detection": ...}`: `active` is `effective_kill_line(record, in-process config)`; `configured` is the same function over the `ai_paper` block re-read from the trader's own config file at query time (`load_ai_paper_config` on a fresh `yaml.safe_load`, never applied); `pending_restart` is `configured != active` for a tighter edit. An edited YAML value is never shown as `active` while the old process runs. `mmr experiment status` prints both lines and, when pending, "edited kill line X% is NOT active until trader_service restarts". At startup the trader logs `experiment kill line active: X% (<basis>)`; the runbook's verification step after a restart is `mmr experiment status` showing the new value under `active` and `pending_restart: false`.
- **K10. `KILLED` cannot be resumed (owner answer; replaces the re-anchor ruling).** The flow after a kill is: the kill flatten reaches `FLAT` on broker evidence (Task 5), the operator reconciles (`mmr reconcile`), then `mmr experiment stop` (refused `NOT_FLAT` until flat), then a new `mmr experiment start`, which records a new baseline and a new experiment id. `("KILLED", "ARMED")` is not an allowed transition; `resume_experiment` on a `KILLED` experiment is refused `EXPERIMENT_KILLED`. `kill_anchor_net_liquidation` therefore always equals `start_net_liquidation` (kept as a column so the kill evaluation reads one field). `kill_seq` stays (it is part of the alert id, K18) and is always 1 for the one kill an experiment can have.
- **K11. Stop needs a flat, quiet account; resume from an outage pause needs fresh evidence.** `stop` refuses `NOT_FLAT` when the capture shows a position or a working order, an account owner is active, or any liquidation root is not terminal (`LiquidationRunStore.roots_to_advance_in_tx`). `resume` from an operator `PAUSED` needs neither (the AI may hold positions while paused). `resume` from a K23 outage pause (`pause_cause = 'BROKER_DATA_OUTAGE'`) needs a fresh capture with `generation_id > pause_generation_id`, `reconciliation_safe()` and `resume_ready()` (`RESUME_EVIDENCE_STALE` / `RECONCILIATION_INCOMPLETE` / `TRADER_NOT_READY`).
- **K12. Arming checks beyond the spec.** Besides the five spec checks, `start` also requires: broker readiness (`resume_ready()`), `reconciliation_safe()`, a clear breaker, no active exit owner and no non-terminal liquidation root, and USD evidence for the start value (K13). Each can only refuse. Why: the kill flatten and the scoreboard start must not inherit an unfinished close or an unknown start value. **Owner answer:** all five spec checks are required, and missing or unreadable evidence refuses with a specific code for each: a port that raises or returns a non-bool is `<CHECK>_UNREADABLE` (`AI_PAPER_CONFIG_UNREADABLE`, `ACCOUNT_MODE_UNREADABLE`, `IDENTITY_CHECK_UNREADABLE`, `ONE_STRATEGY_STATE_UNREADABLE`; the flat check reuses `BROKER_SNAPSHOT_UNAVAILABLE`), never a pass.
- **K13. Start FX.** `base_currency` and `start_usd_per_base` come from `TraderServiceApi.get_account_cash_by_currency` (`trader_service_api.py:321-345`): base `USD` → rate 1.0, source `base_is_usd`; otherwise `1 / currencies["USD"]["exchange_rate"]`, source `ib_account_values`. A non-USD base without a finite positive rate refuses `START_FX_UNAVAILABLE` (Plan 5 would accept `null`; a start value without USD evidence can never be repaired, spec 5.2). The kill line compares base-currency net liquidation values, so FX moves count as P&L for the kill.
- **K14. Who may pause, resume and stop (owner answer).** `pause_experiment`: `cli`, `dashboard`, `ai_supervisor` (risk-reducing). `start`, `resume`, `stop`: operator principals only, `cli` and `dashboard` (Plan 2's `HUMAN` pair; the dashboard is the operator's UI). `get_experiment`: `cli`, `dashboard`, `ai_supervisor` (explicit set, Plan 3 R23). The existing `pause_trading` is unchanged and does not change the experiment state (Plan 3 already refuses AI entries while trading is paused).
- **K15. Experiment methods exist whenever the command stack runs on paper**, also with `ai_paper.enabled: false`. `start` then refuses `AI_PAPER_DISABLED`; `get_experiment`, `pause`, `stop` keep working, and the monitor keeps running for a non-`STOPPED` experiment. Why: an owner who disables `ai_paper` while an experiment holds positions must still see it, kill it and stop it.
- **K16. One active experiment per account**, enforced by a separate `experiment_active(account_id PRIMARY KEY)` row inserted with the experiment and deleted with `STOPPED`, in the same transactions (DuckDB has no partial unique index). `experiment_id = "exp-" + sha256(account_id + "\x00" + command_id)[:20]`, so a retried start with the same command id finds its own row.
- **K17. Startup with both modes (owner answer; confirmed by both reviewers).** "Refuse" means refuse trading authority, never terminate `trader_service`: a process that does not start cannot flatten. Having both modes configured (`automation.enabled: true` and `ai_paper.enabled: true`) is not a conflict. Only when both are **actually armed** — the one-strategy automation is armed (`paper_automation_service.status().lifecycle` is an armed state, not `disabled`) **and** an experiment is `ARMED`, `PAUSED` or `KILLED` (a paused or killed experiment still counts as armed) — is there a conflict. Then `build_command_stack` still builds, sets `CommandStack.mode_conflict = "BOTH_MODES_ARMED"`, logs ERROR and writes one **durable** journal incident (`automation.mode_conflict`, written once per start, not per call), and every **entry** on both paths is refused `BOTH_MODES_ARMED` (one-strategy BUY intents in `AutomatedIntentCommandService`, `ai_paper` `ENTER` via `ExperimentView.entry_block`). Recovery, reconciliation and exits keep working: session recovery and flatten, the liquidation rescan, the kill monitor, time exits, broker-proven closes, one-strategy SELL closes and `ai_paper` `CLOSE` / `PARTIAL_CLOSE`. The trader never picks a mode and never disarms either one silently. The operator clears it deliberately: deactivate the one-strategy automation, or stop the experiment once it is flat (`KILLED` is never resumed: stop only after broker-confirmed flat, then start a new experiment). The next start is normal.
- **K18. Contract conflicts with Plan 5 (spec wins).**
  - *Delivery order.* Spec 7 lands Plan 4 before Plan 5, so Plan 4 cannot import `trader/scoreboard/*`. Plan 4 defines its own ports with the same method shapes: `KillAlertPort.enqueue(event_id, kind, text) -> bool` (Plan 5's `TelegramOutbox` satisfies it) and `SessionEndPort.record_session_end(end)` where `end` has `account_id, session_date, state, ended_at` (Plan 5's `SessionLedger` satisfies it). Production wires `None` for both; `KillLineMonitor.attach_notices(alerts=..., session_end=...)` is the one line Plan 5 calls. `ExperimentStore.latest()` / `get(experiment_id)` return `ExperimentRecord`, a superset of Plan 5's A1 fields with the same names and types (`start_usd_per_base` is never `null` here, K13). `ExperimentStore` satisfies Plan 5's `ExperimentReader` structurally; Plan 5 keeps its narrow read-view dataclass for its own fakes.
  - *Event id (owner answer).* `kill_started:{experiment_id}:{kill_seq}`: stable across retries of the same kill (the outbox drops a repeated id) and distinct for each kill. With K10 an experiment has at most one kill, so `kill_seq` is 1; the format stays so the id never depends on that rule.
  - *Text.* Plan 4 writes the alert text (`kill_alert_text`, it has the drawdown numbers); Plan 5's `format_kill_alert(experiment_id, started_at)` is not used. The text starts with `PAPER`.
  - *Before Plan 5.* With no outbox the alert is marked `NO_OUTBOX` (not pending), so enabling Telegram later does not send old kills (matches Plan 5 ruling 17). With no session-end sink, `kill_session_end_state = NO_SINK`.
  - *Session end.* When the kill flatten ends (`FLAT` or `FAILED_SAFE`), the monitor calls `record_session_end` once with `state="KILLED"`, `session_date` = ET date of `killed_at` and `ended_at` = the flat or failure time. Plan 5 ruling 4 maps a killed date to `KILLED` anyway.
- **K19. Plan 3's `ExperimentView` grows one optional field**, `entry_block: Optional[str] = None` (K7, and `EXPERIMENT_MONITOR_NOT_READY` until `recover()` finished). Plan 3's admission adds one row for `ENTER` only. Reductions ignore it.
- **K20. Dispatch gate.** A decision admitted before a kill can reach `DispatchGuard.revalidate` after it. `DispatchGuard` gets `experiment_gate: Callable[[request], Optional[str]] = lambda request: None`; for `AI_PAPER_ACTION` it returns `NO_EXPERIMENT`, `EXPERIMENT_NOT_ARMED` or the `entry_block` code, and the guard refuses with it. A raising gate refuses `EXPERIMENT_STATE_UNAVAILABLE`. The old path's gate is the default no-op.
- **K21. Restart during start.** `start` writes the experiment, the active row and the first transition in one transaction. A crash before it leaves nothing (a new `start` succeeds); a crash after it leaves a complete `ARMED` experiment (a new `start` refuses `EXPERIMENT_ACTIVE` and names it). The coordinator replays a repeated command id from its ledger and never re-runs the handler (`TradingCommandCoordinator.execute`, `command_coordinator.py:950-1060`).
- **K22. Identity readiness** (spec: "the `ai_supervisor` key is in the keyring and the allow-list is loaded; the old shared key does not count"). Proven by `trader.rpc_identity` being a Plan 2 `ServiceIdentity` whose keyring holds `ai_supervisor` (new one-line `ServiceIdentity.accepts(principal) -> bool`) **and** the served registry's `resolve("command", "submit_ai_paper_decision").allowed_principals == {"ai_supervisor"}`. Until trading_runtime attaches the registry, the check refuses `AI_ALLOW_LIST_NOT_LOADED`.
- **K23. Broker-data outage pauses the experiment (owner answer).** The monitor tracks the start of each unknown streak (K7). When a streak lasts `ai_paper.broker_outage_pause_seconds` (Plan 3 R22; default 300) and the experiment is `ARMED`, it writes, in one transaction, `ARMED → PAUSED` with principal `kill_monitor`, reason `broker data unavailable for N s`, and `pause_cause = 'BROKER_DATA_OUTAGE'`, `pause_generation_id` = the last good generation. Then it enqueues an alert through the `KillAlertPort` (kind `broker_outage`, event id `broker_outage:{experiment_id}:{revision}`, text starting `PAPER`; `NO_OUTBOX` without Plan 5) and entries stay blocked. It never flattens and never kills from stale state: a kill needs a successful evaluation (K7). An operator `PAUSED` experiment is not touched, but the alert still goes out once per streak. Resume needs fresh reconciliation and an operator (K11, K14).

## Review Focus

Inputs and failure modes the spec implies but its test list does not name. Each has a named test.

1. **A capture whose net liquidation is invalid** (zero, NaN, missing) at the moment the account is down. Expect no kill and `KILL_LINE_UNKNOWN` on entries, never a flatten from bad data. → Task 4 `test_invalid_net_liquidation_never_kills_and_blocks_entries`.
2. **A capture older than one already seen** (generation goes backwards after a reconnect). Expect unknown, no kill, no peak update. → Task 4 `test_generation_regression_is_unknown`.
3. **Resume racing a kill on the same tick.** Expect exactly one of `PAUSED`→`ARMED` or `PAUSED`→`KILLED` to commit, never a lost kill. → Task 1 `test_concurrent_transitions_one_wins`, Task 5 `test_resume_racing_a_kill_never_loses_the_kill`.
4. **The owner edits the kill line in `trader.yaml` during an experiment.** Expect a tighter value to apply after the next trader_service start (not mid-process) and a looser or removed one to be ignored. → Task 2 `test_tighter_config_wins_looser_is_ignored`.
5. **The Telegram outbox raises while a kill starts.** Expect the flatten to proceed, the alert to be retried on the next tick and sent once. → Task 5 `test_alert_failure_never_blocks_the_flatten_and_retries_once`.

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/automation/experiments.py` (new), `trader/data/schema_migrations.py` (docstring) | migration 70, `ExperimentRecord`, state machine, `ExperimentStore` | 1 |
| `trader/automation/kill_line.py` (new) | `KillLine`, `effective_kill_line`, `evaluate_kill_line` | 2 |
| `trader/automation/experiment_service.py` (new), `trader/messaging/typed_rpc.py` (`ServiceIdentity.accepts`) | start / pause / resume / stop, arming checks, `ArmingLock`, start FX | 3 |
| `trader/automation/paper_activation.py`, `trader/automation/paper_hot_arm.py`, `trader/trading/command_stack.py` | lock in both directions | 4 |
| `trader/automation/session_controller.py`, `trader/automation/kill_monitor.py` (new) | `flatten_account_now`; kill detection, `KILLED` first, entry block | 4 |
| `trader/automation/kill_monitor.py` | flatten driving, flat proof, rounds, notices, recovery | 5 |
| `trader/automation/ai_paper_experiment.py`, `trader/automation/ai_paper_decision.py`, `trader/trading/dispatch_guard.py`, `trader/trading/command_stack.py` | Plan 3 port, entry block, dispatch gate, composition | 6 |
| `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/trading_runtime.py`, `trader/trader_service.py`, `trader/sdk.py`, `trader/mmr_cli.py`, `AGENTS.md` | RPC, ACL, monitor loop, CLI, docs | 7 |

---

### Task 1: Experiment table, record and state machine

**Files:**
- Create: `trader/automation/experiments.py`
- Modify: `trader/data/schema_migrations.py` (docstring line Plan 3 adds: append "Plan 4 uses **70** (experiments).")
- Test: `tests/automation/test_experiments.py`

**Interfaces:**
- Consumes: `SchemaMigrator` (`schema_migrations.py`), `DuckDBConnection.transaction`.
- Produces:
  - `EXPERIMENT_MIGRATION_VERSION = 70`; `apply_experiment_migration(migrator) -> bool`.
  - `ExperimentState = Literal["ARMED", "PAUSED", "KILLED", "STOPPED"]`; `ALLOWED_TRANSITIONS: frozenset[tuple[str, str]]` = `{("ARMED","PAUSED"), ("PAUSED","ARMED"), ("ARMED","KILLED"), ("PAUSED","KILLED"), ("ARMED","STOPPED"), ("PAUSED","STOPPED"), ("KILLED","STOPPED")}`.
  - `class ExperimentRefused(Exception)` with `.code`, `.message`.
  - `experiment_id_for(account_id: str, command_id: str) -> str`.
  - `@dataclass(frozen=True) class ExperimentRecord` — Plan 5 A1 fields first: `experiment_id: str, account_id: str, started_at: datetime, start_net_liquidation: float, base_currency: str, start_usd_per_base: float, state: str, killed_at: Optional[datetime]`; then `start_command_id: str, start_generation_id: int, start_fx_source: str, config_digest: str, styles: tuple[str, ...], kill_drawdown_pct: Optional[float], kill_basis: str, revision: int, peak_net_liquidation: float, kill_anchor_net_liquidation: float, kill_seq: int, kill_round: int, kill_root_id: Optional[str], kill_flatten_root: Optional[str], kill_net_liquidation: Optional[float], kill_observed_drawdown_pct: Optional[float], kill_generation_id: Optional[int], kill_flat_state: Optional[str], kill_flat_generation: Optional[int], kill_flat_at: Optional[datetime], kill_alert_state: Optional[str], kill_session_end_state: Optional[str], stopped_at: Optional[datetime]`.
  - `class ExperimentStore(db, account_id: str, now: Callable[[], datetime])`: `insert_armed(record: ExperimentRecord, *, principal: str, reason: str) -> ExperimentRecord`; `get(experiment_id) -> Optional[ExperimentRecord]`; `latest() -> Optional[ExperimentRecord]` (newest `started_at` for the account, any state); `active() -> Optional[ExperimentRecord]` (the non-`STOPPED` one); `transition(experiment_id, *, expected: frozenset[str], to: str, principal: str, command_id: Optional[str], reason: str, changes: Mapping[str, Any] = {}) -> ExperimentRecord`; `update_kill_progress(experiment_id, *, expected_state: str, changes: Mapping[str, Any]) -> ExperimentRecord`; `raise_peak(experiment_id, net_liquidation: float) -> float`; `transitions(experiment_id) -> list[dict]`.

Migration 70 (`sp1_experiments`):

```sql
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
    start_command_id VARCHAR NOT NULL UNIQUE, started_at TIMESTAMPTZ NOT NULL,
    start_net_liquidation DOUBLE NOT NULL, start_generation_id BIGINT NOT NULL,
    base_currency VARCHAR NOT NULL, start_usd_per_base DOUBLE NOT NULL, start_fx_source VARCHAR NOT NULL,
    config_digest VARCHAR NOT NULL, styles_json VARCHAR NOT NULL,
    kill_drawdown_pct DOUBLE, kill_basis VARCHAR NOT NULL,
    state VARCHAR NOT NULL, revision INTEGER NOT NULL,
    peak_net_liquidation DOUBLE NOT NULL, kill_anchor_net_liquidation DOUBLE NOT NULL,
    killed_at TIMESTAMPTZ, kill_seq INTEGER NOT NULL, kill_round INTEGER NOT NULL,
    kill_root_id VARCHAR, kill_flatten_root VARCHAR,
    kill_net_liquidation DOUBLE, kill_observed_drawdown_pct DOUBLE, kill_generation_id BIGINT,
    kill_flat_state VARCHAR, kill_flat_generation BIGINT, kill_flat_at TIMESTAMPTZ,
    kill_alert_state VARCHAR, kill_session_end_state VARCHAR,
    pause_cause VARCHAR, pause_generation_id BIGINT,
    stopped_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_active (
    account_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS experiment_transitions (
    experiment_id VARCHAR NOT NULL, revision INTEGER NOT NULL,
    from_state VARCHAR, to_state VARCHAR NOT NULL, principal VARCHAR NOT NULL,
    command_id VARCHAR, reason VARCHAR NOT NULL, detail_json VARCHAR NOT NULL,
    at TIMESTAMPTZ NOT NULL, PRIMARY KEY (experiment_id, revision));
```

`experiment_transitions` is append-only. `update_kill_progress` and the `changes` argument of `transition` may write only the kill columns, `peak_net_liquidation`, `pause_cause` and `pause_generation_id` (one fixed allow-list, `KILL_COLUMNS`; `kill_anchor_net_liquidation` is set once at insert, K10; any other key, including `state` and every start field, raises `ValueError`). `update_kill_progress` writes only while `state = expected_state` (else `ExperimentRefused("EXPERIMENT_STATE_CHANGED")`).

- [ ] **Step 1: Write the failing tests** (real DuckDB file in `tmp_path`; `NOW = 2026-07-17 15:00 UTC`; `armed_record(**changes)` builds a valid record)

```python
def test_migration_70_creates_the_three_tables(db):
    assert {"experiments", "experiment_active", "experiment_transitions"} <= tables(db)
    assert db.execute("SELECT version FROM schema_migrations WHERE version = 70", fetch="one")

def test_experiment_id_is_deterministic_and_colon_free():
    a = experiment_id_for("DU1", "start-1")
    assert a == experiment_id_for("DU1", "start-1") and a.startswith("exp-") and ":" not in a
    assert a != experiment_id_for("DU1", "start-2")

def test_insert_armed_then_reads(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.active() == rec == store.latest() == store.get(rec.experiment_id)
    assert (rec.state, rec.revision, rec.kill_seq, rec.peak_net_liquidation) == ("ARMED", 1, 0, 100_000.0)
    assert store.transitions(rec.experiment_id)[0]["to_state"] == "ARMED"

def test_plan5_a1_fields_exist_with_their_types(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    for name, kind in (("experiment_id", str), ("account_id", str), ("started_at", dt.datetime),
                       ("start_net_liquidation", float), ("base_currency", str),
                       ("start_usd_per_base", float), ("state", str)):
        assert type(getattr(rec, name)) is kind
    assert rec.killed_at is None

def test_second_active_experiment_is_refused(store):
    store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ExperimentRefused) as exc:
        store.insert_armed(armed_record(start_command_id="start-2"), principal="cli", reason="again")
    assert exc.value.code == "EXPERIMENT_ACTIVE"

def test_same_start_command_returns_its_own_row(store):
    first = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.insert_armed(armed_record(), principal="cli", reason="go") == first

def test_concurrent_starts_create_one_experiment(db):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: _try_insert(db, f"start-{i}"), range(8)))
    assert sum(1 for r in results if r == "ok") == 1
    assert db.execute("SELECT count(*) FROM experiment_active", fetch="one")[0] == 1

@pytest.mark.parametrize("frm,to", [("ARMED", "PAUSED"), ("PAUSED", "ARMED"), ("ARMED", "KILLED"),
                                    ("PAUSED", "KILLED"), ("KILLED", "STOPPED")])
def test_allowed_transitions(store, frm, to): ...

@pytest.mark.parametrize("frm,to", [("KILLED", "PAUSED"), ("KILLED", "ARMED"), ("STOPPED", "ARMED"),
                                    ("STOPPED", "KILLED"), ("ARMED", "ARMED")])
def test_illegal_transitions_are_refused(store, frm, to):          # code ILLEGAL_TRANSITION
    ...

def test_transition_is_compare_and_set(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                     principal="cli", command_id="p1", reason="x")
    with pytest.raises(ExperimentRefused, match="EXPERIMENT_STATE_CHANGED"):
        store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="KILLED",
                         principal="kill_monitor", command_id=None, reason="hit")

def test_concurrent_transitions_one_wins(db, store):                     # Review Focus 3
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                     principal="cli", command_id="p", reason="p")
    def resume(): return _try(lambda: ExperimentStore(db, ACCOUNT, lambda: NOW).transition(
        rec.experiment_id, expected=frozenset({"PAUSED"}), to="ARMED", principal="cli", command_id="r", reason="r"))
    def kill_again(): return _try(lambda: ExperimentStore(db, ACCOUNT, lambda: NOW).transition(
        rec.experiment_id, expected=frozenset({"PAUSED"}), to="KILLED", principal="kill_monitor", command_id=None, reason="hit"))
    with ThreadPoolExecutor(2) as pool:
        outcomes = [f.result() for f in (pool.submit(resume), pool.submit(kill_again))]
    assert sorted(outcomes) == ["EXPERIMENT_STATE_CHANGED", "ok"]

def test_stop_releases_the_active_row_and_is_final(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    stopped = store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="STOPPED",
                               principal="cli", command_id="s", reason="done")
    assert store.active() is None and store.latest() == stopped and stopped.stopped_at == NOW
    store.insert_armed(armed_record(start_command_id="start-2"), principal="cli", reason="new run")

def test_state_survives_a_restart(db, store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="KILLED",
                     principal="kill_monitor", command_id=None, reason="hit",
                     changes={"killed_at": NOW, "kill_seq": 1})
    assert ExperimentStore(db, ACCOUNT, lambda: NOW).active().state == "KILLED"

def test_peak_only_rises(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    assert store.raise_peak(rec.experiment_id, 101_000.0) == 101_000.0
    assert store.raise_peak(rec.experiment_id, 99_000.0) == 101_000.0

def test_kill_progress_cannot_touch_state_or_start_fields(store):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED", changes={"state": "STOPPED"})
    with pytest.raises(ValueError):
        store.update_kill_progress(rec.experiment_id, expected_state="ARMED",
                                   changes={"start_net_liquidation": 1.0})

@pytest.mark.parametrize("key", ["state", "start_net_liquidation", "started_at", "experiment_id", "revision"])
def test_transition_changes_use_the_same_allow_list(store, key):
    rec = store.insert_armed(armed_record(), principal="cli", reason="go")
    with pytest.raises(ValueError):
        store.transition(rec.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                         principal="cli", command_id="p", reason="x", changes={key: 1})

@pytest.mark.parametrize("bad", [dict(start_net_liquidation=True), dict(start_net_liquidation=math.nan),
    dict(start_net_liquidation=0.0), dict(start_usd_per_base=-1.0), dict(kill_drawdown_pct=True),
    dict(kill_basis="high"), dict(base_currency="usd"), dict(start_generation_id=True),
    dict(styles=("swing_short",))])
def test_record_constructor_is_strict(bad):
    with pytest.raises(ValueError):
        armed_record(**bad)
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: trader.automation.experiments`).
- [ ] **Step 3: Implement.** `ExperimentRecord.__post_init__` checks every field type with `type(x) is …` (floats: `type(x) in (int, float)` and finite and `> 0`; `kill_drawdown_pct` `None` or `0 < x < 100`; `base_currency` `re.fullmatch("[A-Z]{3}")`; `styles` a tuple of `SUPPORTED_STYLES` from `ai_paper_config`). Core of `insert_armed`:

```python
def insert_armed(self, record, *, principal, reason):
    def tx(conn):
        own = conn.execute("SELECT 1 FROM experiments WHERE experiment_id = ?", [record.experiment_id]).fetchone()
        if own:
            return self._get_in_tx(conn, record.experiment_id)
        active = conn.execute("SELECT experiment_id FROM experiment_active WHERE account_id = ?",
                              [record.account_id]).fetchone()
        if active:
            raise ExperimentRefused("EXPERIMENT_ACTIVE", f"experiment {active[0]} is not stopped")
        conn.execute(_INSERT_EXPERIMENT, _row_values(record))
        conn.execute("INSERT INTO experiment_active VALUES (?, ?)", [record.account_id, record.experiment_id])
        self._append_transition_in_tx(conn, record.experiment_id, 1, None, "ARMED", principal,
                                      record.start_command_id, reason, {})
        return self._get_in_tx(conn, record.experiment_id)
    try:
        return self._db.transaction(tx)
    except duckdb.ConstraintException as exc:               # lost the race on experiment_active
        raise ExperimentRefused("EXPERIMENT_ACTIVE", "another experiment was armed first") from exc
```

  `transition` validates `(current, to) in ALLOWED_TRANSITIONS` (`ILLEGAL_TRANSITION`), then `UPDATE experiments SET state = ?, revision = revision + 1, … WHERE experiment_id = ? AND state = ? AND revision = ?`; zero rows → `EXPERIMENT_STATE_CHANGED`. `to == "STOPPED"` also sets `stopped_at` and deletes the `experiment_active` row. Every call appends one transition row.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add the durable experiment table and state machine`.

---

### Task 2: Kill-line evaluation (pure)

**Files:**
- Create: `trader/automation/kill_line.py`
- Test: `tests/automation/test_kill_line.py`

**Interfaces:**
- Consumes: Plan 3 `AiPaperConfig.experiment_kill_drawdown_pct`, `.experiment_kill_basis`.
- Produces: `@dataclass(frozen=True) class KillLine: pct: float; basis: Literal["start", "peak"]`; `effective_kill_line(record: ExperimentRecord, config: AiPaperConfig) -> Optional[KillLine]` (K9); `@dataclass(frozen=True) class KillEvaluation: hit: bool; reference: float; net_liquidation: float; drawdown_pct: float`; `class KillLineInputError(ValueError)`; `evaluate_kill_line(line: KillLine, *, anchor: float, peak: float, net_liquidation: float) -> KillEvaluation`.

- [ ] **Step 1: Write the failing tests**

```python
START = KillLine(20.0, "start"); PEAK = KillLine(20.0, "peak")

@pytest.mark.parametrize("nlv,hit", [(80_000.0, True), (80_000.01, False), (79_000.0, True), (120_000.0, False)])
def test_start_basis_hits_at_the_line(nlv, hit):
    assert evaluate_kill_line(START, anchor=100_000.0, peak=130_000.0, net_liquidation=nlv).hit is hit

def test_peak_basis_measures_from_the_peak():
    ev = evaluate_kill_line(PEAK, anchor=100_000.0, peak=125_000.0, net_liquidation=100_000.0)
    assert (ev.hit, ev.reference, ev.drawdown_pct) == (True, 125_000.0, pytest.approx(20.0))

def test_peak_never_below_anchor():                       # a peak column can lag one tick
    ev = evaluate_kill_line(PEAK, anchor=100_000.0, peak=90_000.0, net_liquidation=95_000.0)
    assert ev.reference == 100_000.0

@pytest.mark.parametrize("field,value", [("net_liquidation", math.nan), ("net_liquidation", 0.0),
    ("net_liquidation", -5.0), ("net_liquidation", True), ("anchor", math.inf), ("peak", "1e5")])
def test_bad_inputs_raise_not_kill(field, value):
    kwargs = dict(anchor=100_000.0, peak=100_000.0, net_liquidation=90_000.0) | {field: value}
    with pytest.raises(KillLineInputError):
        evaluate_kill_line(START, **kwargs)

@pytest.mark.parametrize("frozen,config,expected", [
    (None, None, None), (20.0, None, 20.0), (None, 15.0, 15.0), (20.0, 15.0, 15.0), (15.0, 20.0, 15.0)])
def test_tighter_config_wins_looser_is_ignored(frozen, config, expected):        # Review Focus 4, K9
    line = effective_kill_line(record(kill_drawdown_pct=frozen, kill_basis="start"),
                               AiPaperConfig(experiment_kill_drawdown_pct=config, experiment_kill_basis="peak"))
    assert (line.pct if line else None) == expected and (line is None or line.basis == "start")

def test_kill_pct_true_fails_config_load():               # pins Plan 3's parser: no bool-as-number
    with pytest.raises(AiPaperConfigError):
        load_ai_paper_config({"enabled": True, "experiment_kill_drawdown_pct": True}, trading_mode="paper")
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**

```python
def evaluate_kill_line(line, *, anchor, peak, net_liquidation):
    for name, value in (("anchor", anchor), ("peak", peak), ("net_liquidation", net_liquidation)):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise KillLineInputError(f"{name} must be a finite positive number, got {value!r}")
    reference = float(anchor) if line.basis == "start" else max(float(peak), float(anchor))
    drawdown_pct = (reference - float(net_liquidation)) / reference * 100.0
    return KillEvaluation(drawdown_pct >= line.pct, reference, float(net_liquidation), drawdown_pct)
```

  `effective_kill_line`: candidates = the non-`None` values of `record.kill_drawdown_pct` and `config.experiment_kill_drawdown_pct`; none → `None`; else `KillLine(min(candidates), record.kill_basis)`.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add pure kill-line evaluation with start and peak bases`.

---

### Task 3: Experiment service — start, pause, resume, stop

**Files:**
- Create: `trader/automation/experiment_service.py`
- Modify: `trader/messaging/typed_rpc.py` (`ServiceIdentity.accepts(principal: str) -> bool`, returns `principal in self._keyring.principals()`)
- Test: `tests/automation/test_experiment_service.py`, `tests/test_typed_rpc_identity.py` (one case for `accepts`)

**Interfaces:**
- Consumes: Task 1 store; Task 2 `effective_kill_line`, `evaluate_kill_line`; `BrokerRiskSnapshot` / `BrokerRiskSnapshotError` (`broker_state.py`); `CommandRequest` with Plan 2 `.principal`; `CommandValidationError` (`command_coordinator.py:413`); Plan 3 `AiPaperConfig`; `sha256_digest` (`trader/research/canonical.py:64`).
- Produces:
  - `class ArmingLock` (a `threading.Lock` with `hold(timeout=10.0)` context manager raising `ExperimentRefused("ARMING_BUSY")`). One instance per trader, shared with Task 4.
  - `@dataclass(frozen=True) class StartFx: base_currency: str; usd_per_base: float; source: str`; `start_fx_from_cash(cash: Mapping[str, Any]) -> StartFx` (K13; raises `ExperimentRefused("START_FX_UNAVAILABLE")`).
  - `@dataclass(frozen=True) class ArmingPorts`: `broker` (`.capture(account_id)`), `account_cash: Callable[[], Mapping]`, `resume_ready: Callable[[], bool]`, `reconciliation_safe: Callable[[], bool]`, `breaker_clear: Callable[[], bool]`, `exit_owners` (`.account_owner(account_id)`), `liquidation_roots: Callable[[], list[str]]` (non-terminal roots), `old_path_armed: Callable[[], Optional[str]]` (Task 4 fills it; returns a reason or `None`), `ai_paper_built: Callable[[], bool]`.
  - `class ExperimentService(*, store, ports: ArmingPorts, lock: ArmingLock, config: AiPaperConfig, account_id: str, account_mode: str, now)`, with `attach_identity_check(check: Callable[[], Optional[str]])`, `identity_problem() -> Optional[str]`, and four coordinator actions (single-step; each returns `record_view(record) -> dict` or raises `CommandValidationError(code, message)`): `start(cmd)`, `pause(cmd)`, `resume(cmd)`, `stop(cmd)`. `record_view` gives `experiment_id, state, started_at, start_net_liquidation, base_currency, start_usd_per_base, kill_drawdown_pct, kill_basis, effective_kill_pct, peak_net_liquidation, killed_at, kill_flat_state, pause_cause, revision`.
  - Bodies (validated again here, not only by pydantic): start `{"reason": str}`; pause / resume / stop `{"experiment_id": str, "reason": str}`. `experiment_id` must match `^exp-[0-9a-f]{20}$`; `reason` 1–200 characters of `str`.

Arming order for `start` (each refusal its own code; all on **one** fresh capture, under `lock.hold()`):

| # | Check | Refusal |
|---|---|---|
| 1 | `cmd.principal in {"cli","dashboard"}` | `PRINCIPAL_FORBIDDEN` |
| 2 | body shape | `EXPERIMENT_REQUEST_INVALID` |
| 3 | `config.enabled` and `ports.ai_paper_built()` | `AI_PAPER_DISABLED` |
| 4 | `account_mode == "paper"` | `ACCOUNT_NOT_PAPER` |
| 5 | identity check (K22) | `AI_SUPERVISOR_KEY_MISSING` / `AI_ALLOW_LIST_NOT_LOADED` |
| 6 | `ports.old_path_armed()` is `None` | `ONE_STRATEGY_ARMED` |
| 7 | `store.active()` is `None` | `EXPERIMENT_ACTIVE` |
| 8 | `resume_ready()`; `breaker_clear()`; `reconciliation_safe()` | `TRADER_NOT_READY` / `BREAKER_TRIPPED` / `RECONCILIATION_INCOMPLETE` |
| 9 | `broker.capture(account_id)` succeeds; account and `account_mode == "paper"` match | `BROKER_SNAPSHOT_UNAVAILABLE` / `ACCOUNT_NOT_PAPER` |
| 10 | no position with non-zero quantity, no working order, no account owner, no non-terminal root | `NOT_FLAT` (message lists what is open) |
| 11 | `start_fx_from_cash(account_cash())` | `START_FX_UNAVAILABLE` |
| 12 | `store.insert_armed(...)` | `EXPERIMENT_ACTIVE` (race) |

- [ ] **Step 1: Write the failing tests** (fakes for every port, a real store on `tmp_path`; `svc(**port_changes)` builds the service with all checks passing; `cmd(action, principal="cli", **body)` builds a `CommandRequest` with `principal`)

```python
def test_start_records_the_start_net_liquidation_and_frozen_kill_line(svc):
    view = svc.start(cmd("start_experiment", reason="first run"))
    rec = svc.store.active()
    assert (view["state"], rec.start_net_liquidation, rec.start_generation_id) == ("ARMED", 100_000.0, 7)
    assert (rec.kill_drawdown_pct, rec.kill_basis, rec.base_currency, rec.start_usd_per_base) == (20.0, "start", "USD", 1.0)
    assert rec.peak_net_liquidation == rec.kill_anchor_net_liquidation == 100_000.0
    assert rec.config_digest == sha256_digest("mmr.ai-paper-config.v1", dict(CONFIG.raw_section))

@pytest.mark.parametrize("setup,code", [
    ("principal_ai_supervisor", "PRINCIPAL_FORBIDDEN"), ("principal_ai_research", "PRINCIPAL_FORBIDDEN"),
    ("ai_paper_disabled", "AI_PAPER_DISABLED"), ("live_account", "ACCOUNT_NOT_PAPER"),
    ("legacy_hmac_identity", "AI_SUPERVISOR_KEY_MISSING"), ("keyring_without_ai_supervisor", "AI_SUPERVISOR_KEY_MISSING"),
    ("registry_not_attached", "AI_ALLOW_LIST_NOT_LOADED"), ("acl_grants_cli_too", "AI_ALLOW_LIST_NOT_LOADED"),
    ("one_strategy_armed", "ONE_STRATEGY_ARMED"), ("broker_not_ready", "TRADER_NOT_READY"),
    ("breaker_tripped", "BREAKER_TRIPPED"), ("unresolved_command", "RECONCILIATION_INCOMPLETE"),
    ("capture_staging", "BROKER_SNAPSHOT_UNAVAILABLE"), ("capture_no_generation", "BROKER_SNAPSHOT_UNAVAILABLE"),
    ("snapshot_live_mode", "ACCOUNT_NOT_PAPER"), ("leftover_position", "NOT_FLAT"),
    ("working_order", "NOT_FLAT"), ("external_working_order", "NOT_FLAT"), ("active_exit_owner", "NOT_FLAT"),
    ("unfinished_liquidation_root", "NOT_FLAT"), ("eur_base_without_usd_rate", "START_FX_UNAVAILABLE")])
def test_each_arming_check_refuses_with_its_own_code(svc_with, setup, code):
    service = svc_with(setup)
    with pytest.raises(CommandValidationError) as exc:
        service.start(cmd("start_experiment", principal=principal_for(setup), reason="go"))
    assert exc.value.code == code
    assert service.store.latest() is None

def test_double_arm_is_refused(svc):                                  # spec edge "double arm"
    svc.start(cmd("start_experiment", command_id="s1", reason="go"))
    with pytest.raises(CommandValidationError, match="EXPERIMENT_ACTIVE"):
        svc.start(cmd("start_experiment", command_id="s2", reason="again"))

def test_arm_with_stale_evidence_is_refused(svc_with):                # spec edge "stale evidence"
    service = svc_with("broker_not_ready")                            # resume_ready() False: no current generation
    with pytest.raises(CommandValidationError, match="TRADER_NOT_READY"):
        service.start(cmd("start_experiment", reason="go"))

def test_flat_is_checked_on_the_same_capture_that_records_the_start(svc_with):
    service = svc_with("position_appears_on_second_capture")          # capture() called once only
    service.start(cmd("start_experiment", reason="go"))
    assert service.ports.broker.calls == 1

def test_crash_after_insert_leaves_a_complete_armed_experiment(svc, monkeypatch):     # K21
    monkeypatch.setattr(svc, "_view", lambda rec: (_ for _ in ()).throw(_Crash()))
    with pytest.raises(_Crash):
        svc.start(cmd("start_experiment", command_id="s1", reason="go"))
    with pytest.raises(CommandValidationError, match="EXPERIMENT_ACTIVE"):
        svc.start(cmd("start_experiment", command_id="s2", reason="go"))
    assert svc.store.active().state == "ARMED"

def test_crash_before_insert_leaves_nothing(svc_with):
    service = svc_with("insert_crashes_once")
    with pytest.raises(_Crash):
        service.start(cmd("start_experiment", command_id="s1", reason="go"))
    assert service.store.latest() is None
    assert service.start(cmd("start_experiment", command_id="s2", reason="go"))["state"] == "ARMED"

@pytest.mark.parametrize("cash,expected", [
    ({"base_currency": "USD", "currencies": {}}, StartFx("USD", 1.0, "base_is_usd")),
    ({"base_currency": "CAD", "currencies": {"USD": {"exchange_rate": 1.25}}}, StartFx("CAD", 0.8, "ib_account_values"))])
def test_start_fx(cash, expected):
    assert start_fx_from_cash(cash) == expected

@pytest.mark.parametrize("cash", [{}, {"base_currency": None}, {"base_currency": "eur"},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": None}}},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": True}}},
    {"base_currency": "EUR", "currencies": {"USD": {"exchange_rate": math.inf}}}])
def test_start_fx_refuses_without_evidence(cash):
    with pytest.raises(ExperimentRefused, match="START_FX_UNAVAILABLE"):
        start_fx_from_cash(cash)

# pause / resume / stop
@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_supervisor"])
def test_pause_by_allowed_principals(armed, principal):
    assert armed.pause(cmd("pause_experiment", principal=principal, experiment_id=armed.id, reason="p"))["state"] == "PAUSED"

def test_pause_while_killed_is_refused(killed):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_KILLED"):
        killed.pause(cmd("pause_experiment", experiment_id=killed.id, reason="p"))

@pytest.mark.parametrize("principal", ["ai_supervisor", "ai_research", "strategy", "trader"])
def test_ai_principals_cannot_resume_or_stop(paused, principal):
    for action, fn in (("resume_experiment", paused.resume), ("stop_experiment", paused.stop)):
        with pytest.raises(CommandValidationError, match="PRINCIPAL_FORBIDDEN"):
            fn(cmd(action, principal=principal, experiment_id=paused.id, reason="x"))

def test_resume_from_paused_rearms_with_positions_open(paused):
    paused.ports.broker.set(positions=(position(100),))
    assert paused.resume(cmd("resume_experiment", experiment_id=paused.id, reason="r"))["state"] == "ARMED"

@pytest.mark.parametrize("flat", [True, False])
def test_resume_from_killed_is_refused(killed, flat):                                    # K10, owner answer
    killed.ports.broker.set(positions=() if flat else (position(100),))
    with pytest.raises(CommandValidationError, match="EXPERIMENT_KILLED"):
        killed.resume(cmd("resume_experiment", experiment_id=killed.id, reason="r"))
    assert killed.store.active().state == "KILLED"

def test_after_a_kill_stop_then_start_records_a_new_baseline(killed):                  # K10 flow
    killed.ports.broker.set(positions=(), net_liquidation=78_000.0)
    killed.stop(cmd("stop_experiment", experiment_id=killed.id, reason="done"))
    view = killed.start(cmd("start_experiment", command_id="s2", reason="new run"))
    assert view["experiment_id"] != killed.id and view["start_net_liquidation"] == 78_000.0

def test_resume_after_an_outage_pause_needs_fresh_evidence(outage_paused):              # K11, K23
    outage_paused.ports.broker.set(generation_id=outage_paused.pause_generation_id)     # nothing newer yet
    with pytest.raises(CommandValidationError, match="RESUME_EVIDENCE_STALE"):
        outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))
    outage_paused.ports.broker.set(generation_id=outage_paused.pause_generation_id + 1)
    outage_paused.ports.reconciliation_safe = lambda: False
    with pytest.raises(CommandValidationError, match="RECONCILIATION_INCOMPLETE"):
        outage_paused.resume(cmd("resume_experiment", experiment_id=outage_paused.id, reason="r"))

def test_resume_requires_ai_paper_enabled(paused_with_config_disabled): ...           # AI_PAPER_DISABLED

def test_resume_or_stop_with_the_wrong_experiment_id(paused):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_MISMATCH"):
        paused.resume(cmd("resume_experiment", experiment_id="exp-" + "0" * 20, reason="r"))

@pytest.mark.parametrize("open_thing", ["position", "working_order", "account_owner", "liquidation_root"])
def test_stop_refused_while_not_flat(killed, open_thing):
    killed.open(open_thing)
    with pytest.raises(CommandValidationError, match="NOT_FLAT"):
        killed.stop(cmd("stop_experiment", experiment_id=killed.id, reason="s"))

def test_stop_moves_killed_to_stopped_without_rearming_and_releases_the_lock(killed):
    view = killed.stop(cmd("stop_experiment", experiment_id=killed.id, reason="done"))
    assert view["state"] == "STOPPED" and killed.store.active() is None
    assert killed.store.transitions(killed.id)[-1]["from_state"] == "KILLED"
    with pytest.raises(CommandValidationError, match="EXPERIMENT_STOPPED"):
        killed.resume(cmd("resume_experiment", experiment_id=killed.id, reason="r"))

@pytest.mark.parametrize("port,code", [("ai_paper_built", "AI_PAPER_CONFIG_UNREADABLE"),
    ("account_mode", "ACCOUNT_MODE_UNREADABLE"), ("identity_check", "IDENTITY_CHECK_UNREADABLE"),
    ("old_path_armed", "ONE_STRATEGY_STATE_UNREADABLE"), ("broker_capture", "BROKER_SNAPSHOT_UNAVAILABLE")])
@pytest.mark.parametrize("failure", ["raises", "returns_non_bool"])
def test_unreadable_arming_evidence_refuses_with_its_own_code(svc_with, port, code, failure):   # K12, owner answer
    service = svc_with(f"{port}_{failure}")
    with pytest.raises(CommandValidationError) as exc:
        service.start(cmd("start_experiment", reason="go"))
    assert exc.value.code == code and service.store.latest() is None

@pytest.mark.parametrize("body", [{"reason": True}, {"reason": ""}, {"reason": "x" * 201},
    {"experiment_id": 7, "reason": "r"}, {"experiment_id": "exp-1", "reason": "r"},
    {"experiment_id": "exp-" + "a" * 20, "reason": "r", "extra": 1}])
def test_bodies_are_strict_in_process_too(paused, body):
    with pytest.raises(CommandValidationError, match="EXPERIMENT_REQUEST_INVALID"):
        paused.pause(replace(cmd("pause_experiment", experiment_id=paused.id, reason="p"), body=body))
```

```python
# tests/test_typed_rpc_identity.py (added)
def test_accepts_reports_keyring_membership():
    ids = make_identities()
    assert ids["trader"].accepts("ai_supervisor") and not ids["strategy"].accepts("ai_supervisor")
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** One private method per check, in table order, each raising `ExperimentRefused(code, message)`; `start` wraps them as `CommandValidationError`. Core:

```python
def start(self, cmd):
    self._require_principal(cmd, HUMAN_PRINCIPALS)
    reason = _reason(cmd.body, keys={"reason"})
    with self._lock.hold():
        try:
            self._require_enabled(); self._require_paper(); self._require_identity()
            self._require_old_path_disarmed(); self._require_no_active()
            self._require_ready()
            snapshot = self._capture()
            self._require_flat(snapshot)
            fx = start_fx_from_cash(self._ports.account_cash())
            record = self._armed_record(cmd.command_id, snapshot, fx)
            return self._view(self._store.insert_armed(record, principal=cmd.principal, reason=reason))
        except ExperimentRefused as exc:
            raise CommandValidationError(exc.code, exc.message) from exc
```

  `_require_flat` treats any `BrokerPositionRow` with `quantity != 0`, any `working_orders` row (external included), `exit_owners.account_owner(account)` and `liquidation_roots()` as open. `resume` from `KILLED` raises `EXPERIMENT_KILLED` (K10). `resume` from a `BROKER_DATA_OUTAGE` pause calls `_require_ready`, `_capture` (must be newer than `pause_generation_id`, else `RESUME_EVIDENCE_STALE`) and `reconciliation_safe()`, then `transition(..., expected={"PAUSED"}, to="ARMED", changes={"pause_cause": None, "pause_generation_id": None})`. Every port call goes through `_read(port_name, fn)`, which turns an exception or a non-bool into `<CHECK>_UNREADABLE` (K12). `stop` calls `_capture` and `_require_flat` (no readiness needed). `pause` / `resume` / `stop` compare `body.experiment_id` with `store.active()` (`EXPERIMENT_MISMATCH`; no active and latest `STOPPED` → `EXPERIMENT_STOPPED`; none → `NO_EXPERIMENT`). Pause of a `PAUSED` experiment returns the current view (no new transition). Store `ExperimentRefused("EXPERIMENT_STATE_CHANGED")` passes through as its code.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add experiment start, pause, resume and stop with arming checks`.

---

### Task 4: Lock in both directions, `flatten_account_now`, and kill detection

**Files:**
- Modify: `trader/automation/paper_activation.py` (`PaperAutomationActivationService.__init__` gains `experiment_lock: Optional[ExperimentLockPort] = None`; `activate` checks it first, `paper_activation.py:250-254`), `trader/automation/paper_hot_arm.py` (`ProductionPaperHotArmPorts.trader_commit` checks the same port, `paper_hot_arm.py:182`), `trader/trading/command_stack.py` (K17 at the old-path build, `command_stack.py:968-981` and `:1068`), `trader/automation/session_controller.py` (`flatten_account_now`, `_start_account_flatten` extracted from `_issue_flatten`)
- Create: `trader/automation/kill_monitor.py` (detection half; Task 5 adds the flatten driver)
- Test: `tests/automation/test_experiment_lock.py`, `tests/automation/test_kill_monitor.py`, `tests/automation/test_session_controller.py` (two cases)

**Interfaces:**
- Consumes: Tasks 1–3; `SessionController._issue_flatten` (Plan 1 version); `BrokerRiskSnapshotError`.
- Produces:
  - `experiment_service.ExperimentLockPort` protocol `blocking_state() -> Optional[str]` and `ExperimentLock(store, lock: ArmingLock)` implementing it (`ARMED` / `PAUSED` / `KILLED`, or `None`). `PaperAutomationActivationError("EXPERIMENT_ACTIVE", ...)` from `activate` and `trader_commit`.
  - `SessionController.flatten_account_now(cause_command_id: str, deadline: datetime) -> str` (returns the root to poll; never changes the session state, K2).
  - `kill_monitor.KillLineMonitor(*, store, broker, session: SessionController-like, liquidation, config: AiPaperConfig, account_id, now, journal=None, stale_after_seconds=30.0, flatten_seconds=300.0)` with `tick() -> None`, `recover() -> None` (Task 5 fills recovery), `entry_block(record: ExperimentRecord) -> Optional[str]`, `last_evaluation -> Optional[KillEvaluation]`.

Kill path inside `tick()` (Task 4 part):

```python
def tick(self):
    record = self._store.active()
    if record is None:
        return
    if record.state == "KILLED":
        self._drive_kill(record)                     # Task 5
        return
    snapshot = self._evaluable_capture()             # None = unknown (K7)
    if snapshot is None:
        return
    peak = self._store.raise_peak(record.experiment_id, snapshot.net_liquidation)
    line = effective_kill_line(record, self._config)
    self._mark_evaluated(snapshot)
    if line is None:
        return
    evaluation = evaluate_kill_line(line, anchor=record.kill_anchor_net_liquidation, peak=peak,
                                    net_liquidation=snapshot.net_liquidation)
    if evaluation.hit:
        self._kill(record, snapshot, evaluation)

def _kill(self, record, snapshot, evaluation):
    seq = record.kill_seq + 1
    killed = self._store.transition(                # 1. KILLED is durable before any order
        record.experiment_id, expected=frozenset({"ARMED", "PAUSED"}), to="KILLED",
        principal="kill_monitor", command_id=None, reason="kill line hit",
        changes={"killed_at": self._now(), "kill_seq": seq, "kill_round": 0,
                 "kill_root_id": kill_root_id(record.experiment_id, seq, 0), "kill_flatten_root": None,
                 "kill_net_liquidation": evaluation.net_liquidation,
                 "kill_observed_drawdown_pct": evaluation.drawdown_pct,
                 "kill_generation_id": snapshot.generation_id, "kill_flat_state": "PENDING",
                 "kill_alert_state": "PENDING", "kill_session_end_state": "PENDING"})
    self._drive_kill(killed)                         # 2–4 (Task 5)
```

  `_evaluable_capture()`: `broker.capture(account)`; any exception, `account_mode != "paper"`, account mismatch, or `generation_id < self._last_generation` → record the unknown streak (one journal incident `experiment.kill_line_unknown` per streak, when `journal` is set) and return `None`. `entry_block(record)`: `None` for a non-`ARMED` record (Plan 3 refuses those by state); `"EXPERIMENT_MONITOR_NOT_READY"` before `recover()` finished; `"KILL_LINE_UNKNOWN"` when `effective_kill_line(record, config)` is set and no successful evaluation in the last `stale_after_seconds`; else `None`. In Task 4, `_drive_kill` only calls `self._session.flatten_account_now(killed.kill_root_id, now + flatten_seconds)` and stores the returned root in `kill_flatten_root`; Task 5 replaces it with the full driver.

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_experiment_lock.py
@pytest.mark.parametrize("state", ["ARMED", "PAUSED", "KILLED"])
@pytest.mark.parametrize("mode", ["hot_arm", "restart_required"])
def test_activate_is_refused_while_an_experiment_is_not_stopped(activation_with_experiment, state, mode):
    service = activation_with_experiment(state, mode)
    with pytest.raises(PaperAutomationActivationError) as exc:
        service.activate(strategy_name="orb", reason="x")
    assert exc.value.code == "EXPERIMENT_ACTIVE"
    assert service.status().lifecycle == "disabled"            # nothing written to trader.yaml

def test_activate_allowed_after_stop(activation_with_experiment): ...           # STOPPED releases the lock

def test_hot_arm_trader_commit_refuses_too(hot_arm_ports_with_experiment):
    with pytest.raises(PaperAutomationActivationError, match="EXPERIMENT_ACTIVE"):
        hot_arm_ports_with_experiment("ARMED").trader_commit(...)

def test_experiment_start_refused_while_one_strategy_is_armed(stack_with_old_path_armed):
    with pytest.raises(CommandValidationError, match="ONE_STRATEGY_ARMED"):
        stack_with_old_path_armed.experiments.service.start(cmd("start_experiment", reason="go"))

def test_activate_and_start_race_one_wins(paper_stack):            # shared ArmingLock
    results = race(lambda: activate(paper_stack), lambda: start(paper_stack))
    assert sorted(r.code_or_ok for r in results) in (["EXPERIMENT_ACTIVE", "ok"], ["ONE_STRATEGY_ARMED", "ok"])

def test_both_modes_configured_but_not_both_armed_starts_normally(paper_trader_with_old_path_configured):   # K17
    stack = build_command_stack(paper_trader_with_old_path_configured(experiment="ARMED", old_path="disabled"), POLICY, now=lambda: NOW)
    assert stack.mode_conflict is None

def test_both_modes_armed_refuses_entries_but_keeps_recovery_and_exits(paper_trader_with_old_path_configured):  # K17
    stack = build_command_stack(paper_trader_with_old_path_configured(experiment="ARMED", old_path="armed"), POLICY, now=lambda: NOW)
    assert stack.mode_conflict == "BOTH_MODES_ARMED"
    assert submit_old_path_buy(stack).error_code == "BOTH_MODES_ARMED"
    assert submit_ai_enter(stack).error_code == "BOTH_MODES_ARMED"
    assert submit_ai_close(stack).error_code == "CLOSE_PENDING"                 # a safe exit still works
    assert stack.session_controller.recover(NOW) is not None                    # recovery runs
    assert stack.journal.events("automation.mode_conflict") == 1
    assert [e for e in stack.journal.events("automation.mode_conflict")][0].durable is True    # survives a restart of the process

@pytest.mark.parametrize("state", ["ARMED", "PAUSED", "KILLED"])
def test_a_paused_or_killed_experiment_counts_as_armed_for_the_conflict(paper_trader_with_old_path_configured, state):   # K17
    stack = build_command_stack(paper_trader_with_old_path_configured(experiment=state, old_path="armed"), POLICY, now=lambda: NOW)
    assert stack.mode_conflict == "BOTH_MODES_ARMED"
    assert submit_ai_enter(stack).error_code == "BOTH_MODES_ARMED" and submit_old_path_buy(stack).error_code == "BOTH_MODES_ARMED"

def test_the_trader_starts_and_does_not_raise_with_both_modes_armed(paper_trader_with_old_path_configured, caplog):   # K17
    stack = build_command_stack(paper_trader_with_old_path_configured(experiment="KILLED", old_path="armed"), POLICY, now=lambda: NOW)
    assert stack is not None and any(r.levelname == "ERROR" and "BOTH_MODES_ARMED" in r.message for r in caplog.records)

def test_both_modes_armed_keeps_flatten_rescan_kill_monitor_and_time_exits_running(paper_trader_with_old_path_configured):  # K17
    stack = build_command_stack(paper_trader_with_old_path_configured(experiment="KILLED", old_path="armed", held={AAPL: 3}), POLICY, now=lambda: NOW)
    assert stack.session_controller.flatten_account_now(NOW) is not None
    assert stack.liquidation_service.rescan() is not None and stack.kill_monitor.is_running() and stack.time_exit_scheduler.is_running()

def test_both_modes_armed_is_cleared_only_by_the_operator_and_neither_mode_is_disarmed_silently(paper_trader_with_old_path_configured):  # K17
    cfg = paper_trader_with_old_path_configured(experiment="ARMED", old_path="armed")
    stack = build_command_stack(cfg, POLICY, now=lambda: NOW)
    assert cfg.old_path_lifecycle() != "disabled" and cfg.experiment_state() == "ARMED"             # nothing changed behind the operator's back
    cfg.deactivate_old_path()                                                                      # the operator's deliberate act
    assert build_command_stack(cfg, POLICY, now=lambda: NOW).mode_conflict is None
```

```python
# tests/automation/test_kill_monitor.py — real store; fake broker (sequence of snapshots built with
# dataclasses.replace(_snapshot(g), net_liquidation=x) from tests/test_liquidation_service.py);
# a recording session port whose flatten_account_now asserts the store state when it is called.

def test_kill_line_hit_writes_killed_before_the_first_flatten_call(world):
    world.broker.push(nlv=79_000.0)
    world.session.on_flatten = lambda cause, deadline: world.seen.append(world.store.active().state)
    world.monitor.tick()
    rec = world.store.active()
    assert world.seen == ["KILLED"]
    assert (rec.state, rec.kill_seq, rec.kill_root_id, rec.kill_flatten_root) == (
        "KILLED", 1, f"experiment-kill-{rec.experiment_id}-1-0", f"experiment-kill-{rec.experiment_id}-1-0")
    assert (rec.kill_net_liquidation, rec.kill_observed_drawdown_pct) == (79_000.0, pytest.approx(21.0))

def test_no_kill_above_the_line(world):
    world.broker.push(nlv=80_500.0); world.monitor.tick()
    assert world.store.active().state == "ARMED" and world.session.calls == []

def test_kill_line_off_never_kills(world_without_kill_line):
    world_without_kill_line.broker.push(nlv=10_000.0); world_without_kill_line.monitor.tick()
    assert world_without_kill_line.store.active().state == "ARMED"

def test_peak_basis_kills_from_the_highest_value_since_start(world_peak):
    for nlv in (100_000.0, 125_000.0, 101_000.0, 100_000.0):
        world_peak.broker.push(nlv=nlv); world_peak.monitor.tick()
    assert world_peak.store.active().state == "KILLED"          # 20% below 125k

def test_start_basis_ignores_the_peak(world):
    for nlv in (100_000.0, 125_000.0, 100_000.0):
        world.broker.push(nlv=nlv); world.monitor.tick()
    assert world.store.active().state == "ARMED"

def test_paused_experiment_is_killed_too(world):
    world.pause(); world.broker.push(nlv=70_000.0); world.monitor.tick()
    assert world.store.active().state == "KILLED"

@pytest.mark.parametrize("fault", ["NO_PROMOTED_GENERATION", "GENERATION_STAGING", "INVALID_NET_LIQUIDATION",
                                   "DAILY_PNL_UNAVAILABLE", "ACCOUNT_MISMATCH", "live_mode"])
def test_invalid_net_liquidation_never_kills_and_blocks_entries(world, fault):           # Review Focus 1, K7
    world.broker.fail_with(fault); world.monitor.tick()
    rec = world.store.active()
    assert rec.state == "ARMED" and world.session.calls == []
    world.clock.advance(seconds=31)
    assert world.monitor.entry_block(rec) == "KILL_LINE_UNKNOWN"
    assert world.journal.events("experiment.kill_line_unknown") == 1

def test_unknown_streak_ends_on_the_next_good_capture(world): ...               # entry_block back to None

def test_long_outage_pauses_durably_alerts_and_never_flattens(world):              # K23, owner answer
    world.broker.fail_with("NO_PROMOTED_GENERATION"); world.monitor.tick()
    world.clock.advance(seconds=299); world.monitor.tick()
    assert world.store.active().state == "ARMED"
    world.clock.advance(seconds=2); world.monitor.tick()
    rec = world.store.active()
    assert (rec.state, rec.pause_cause) == ("PAUSED", "BROKER_DATA_OUTAGE")
    assert world.session.calls == [] and world.outbox.kinds() == ["broker_outage"]
    world.restart().monitor.recover()
    assert world.store.active().state == "PAUSED"                                  # durable

def test_outage_pause_seconds_comes_from_config(world_with_outage_seconds_60): ...  # pauses after 60 s

def test_generation_regression_is_unknown(world):                               # Review Focus 2
    world.broker.push(generation=9, nlv=100_000.0); world.monitor.tick()
    world.broker.push(generation=8, nlv=50_000.0); world.monitor.tick()
    assert world.store.active().state == "ARMED"
    assert world.store.active().peak_net_liquidation == 100_000.0

def test_entries_blocked_until_recovered(world_fresh):
    assert world_fresh.monitor.entry_block(world_fresh.store.active()) == "EXPERIMENT_MONITOR_NOT_READY"

def test_kill_line_survives_a_restart(world):
    world.broker.push(nlv=79_000.0)
    restarted = world.restart()                                   # new store + monitor on the same file
    restarted.monitor.recover(); restarted.monitor.tick()
    assert restarted.store.active().state == "KILLED"

def test_a_killed_experiment_stays_killed_after_a_restart(world):
    world.broker.push(nlv=79_000.0); world.monitor.tick()
    restarted = world.restart(); restarted.monitor.recover()
    assert restarted.store.active().state == "KILLED"             # a restart never clears KILLED
```

```python
# tests/automation/test_session_controller.py (added)
def test_flatten_account_now_starts_an_account_flatten_and_keeps_the_schedule(controller, liquidation):
    controller.recover(OPEN_TIME)
    root = controller.flatten_account_now("experiment-kill-exp-1-1-0", OPEN_TIME + dt.timedelta(minutes=5))
    assert root == "experiment-kill-exp-1-1-0"
    assert liquidation.starts == [("experiment-kill-exp-1-1-0", "account")]
    assert controller.run_due(OPEN_TIME).state == "OPEN"              # schedule untouched (K2)

def test_flatten_account_now_returns_the_joined_root(controller, liquidation):
    liquidation.join_with("session-flatten-abc")
    assert controller.flatten_account_now("experiment-kill-exp-1-1-0", LATER) == "session-flatten-abc"
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `SessionController`: extract the `try: receipt = self._liquidation.start(...) … except LiquidationBusy: root = cause` block of `_issue_flatten` into `_start_account_flatten(cause, deadline) -> str`; `_issue_flatten` calls it (no behaviour change; Plan 1's tests stay green); `flatten_account_now` calls it and returns the root.
  - `PaperAutomationActivationService.activate`: `if self._experiment_lock is not None and (state := self._experiment_lock.blocking_state()): raise PaperAutomationActivationError("EXPERIMENT_ACTIVE", f"an ai_paper experiment is {state}; stop it first")`. The body after that check runs under `experiment_lock.hold()` (the shared `ArmingLock`), so `start` and `activate` cannot interleave. `trader_commit` repeats the check.
  - `build_command_stack`: build the experiment store before `_build_automated_intent_service`; if the one-strategy automation is armed (`paper_automation_service.status().lifecycle != "disabled"`) and `store.active()` is not `None`, set `mode_conflict = "BOTH_MODES_ARMED"` (K17): one durable journal incident `automation.mode_conflict`, an ERROR log (a `PAUSED` or `KILLED` experiment counts as armed), the old path's `AutomatedIntentCommandService` refuses BUY intents with that code and `ExperimentStateReader` reports it as `entry_block`; nothing raises. `ArmingPorts.old_path_armed = lambda: "ONE_STRATEGY_ARMED" if paper_automation_service.status().lifecycle != "disabled" else None` (configured alone is not armed).
- [ ] **Step 4: Run, expect PASS**, then `tests/automation/test_session_controller.py`, `tests/test_paper_activation*.py`, `tests/test_paper_hot_arm*.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: lock one-strategy arming against experiments and detect the kill line`.

---

### Task 5: The kill flatten — drive, prove flat, notify, recover

**Files:**
- Modify: `trader/automation/kill_monitor.py`
- Test: `tests/automation/test_kill_flatten.py`

**Interfaces:**
- Consumes: Task 4 monitor; Plan 1 `LiquidationService.receipt_for(root) -> Optional[LiquidationReceipt]` (`state`, `generation_id`, `detail`), real `LiquidationService` in tests via `tests/test_liquidation_service.py::_stack`.
- Produces:
  - `class KillAlertPort(Protocol): def enqueue(self, event_id: str, kind: str, text: str) -> bool`.
  - `class SessionEndPort(Protocol): def record_session_end(self, end: Any) -> Any`.
  - `@dataclass(frozen=True) class KillSessionEnd: account_id: str; session_date: date; state: str; ended_at: datetime` (field names of Plan 5's `SessionEnd`).
  - `kill_alert_text(record: ExperimentRecord) -> str`; `kill_alert_event_id(record) -> str` = `f"kill_started:{record.experiment_id}:{record.kill_seq}"` (K18).
  - `KillLineMonitor.attach_notices(*, alerts: Optional[KillAlertPort], session_end: Optional[SessionEndPort])`; constructor gains `reconciliation_safe: Callable[[], bool]`.
  - `kill_flat_state` values: `PENDING`, `FLAT`, `FAILED_SAFE`. `kill_alert_state`: `PENDING`, `ENQUEUED`, `NO_OUTBOX`. `kill_session_end_state`: `PENDING`, `RECORDED`, `NO_SINK`.

`_drive_kill(record)` (every tick while `KILLED`; each step independent, a failure is logged and retried on the next tick):

1. **Flatten.** `kill_flatten_root` empty → `receipt_for(kill_root_id)`; a receipt means `start` committed before a crash: adopt `kill_root_id`. Else `root = session.flatten_account_now(kill_root_id, now + flatten_seconds)`. Store `kill_flatten_root = root` (K3, K5).
2. **Alert.** `kill_alert_state == "PENDING"`: no port → `NO_OUTBOX`; else `alerts.enqueue(kill_alert_event_id(r), "kill_started", kill_alert_text(r))` → `ENQUEUED` (a `False` return means already there, also `ENQUEUED`). An exception leaves `PENDING`.
3. **Prove flat.** `kill_flat_state == "PENDING"`: `receipt = liquidation.receipt_for(kill_flatten_root)`. `FAILED_SAFE` → `kill_flat_state = "FAILED_SAFE"`, `kill_flat_at = now`. `FLAT` → capture; if `capture.generation_id >= receipt.generation_id`, no non-zero position, no working order and `reconciliation_safe()` → `FLAT`, `kill_flat_generation`, `kill_flat_at`. Otherwise wait.
4. **Late exposure (K6).** `kill_flat_state == "FLAT"` and a capture shows a position or a working order → `kill_round + 1`, new `kill_root_id`, `kill_flatten_root = None`, `kill_flat_state = "PENDING"`, `kill_session_end_state` unchanged; step 1 runs on the next tick.
5. **Session end.** `kill_flat_state in {"FLAT","FAILED_SAFE"}` and `kill_session_end_state == "PENDING"`: no port → `NO_SINK`; else `record_session_end(KillSessionEnd(account, et_date(killed_at), "KILLED", kill_flat_at))` → `RECORDED`.

All writes use `store.update_kill_progress(id, expected_state="KILLED", changes=...)`, so a concurrent stop makes the write fail with `EXPERIMENT_STATE_CHANGED` and the tick ends.

- [ ] **Step 1: Write the failing tests** (`world` = real `LiquidationService` + `ExitOwnerRegistry` from `_stack(tmp_path, snapshots)`, migration 70 applied to the same DuckDB, a real `SessionController` over that service, the monitor, a recording outbox and session-end sink)

```python
def test_kill_flattens_through_the_account_owner_and_reports_flat_only_on_broker_evidence(world):
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick()
    rec = world.store.active()
    owner = world.registry.account_owner(ACCOUNT)
    assert (owner.root_id, owner.kind) == (rec.kill_flatten_root, "account_flatten")
    assert rec.kill_flat_state == "PENDING"                              # reduce sent, not proven
    world.broker_shows_flat(generation=world.next_generation())
    world.run_liquidation(); world.monitor.tick()
    assert world.store.active().kill_flat_state == "FLAT"

def test_flat_is_not_reported_on_a_capture_older_than_the_root_proof(world): ...

def test_flat_is_not_reported_while_a_command_is_unresolved(world):
    world.reconciliation_safe = False
    world.drive_kill_to_flat()
    assert world.store.active().kill_flat_state == "PENDING"

def test_kill_during_an_open_entry_cancels_it_before_reducing(world):          # spec edge
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.working_entry(conid=2, quantity=100, group="mmr:og-aip-dec-00000001")
    world.monitor.tick(); world.run_liquidation()
    kinds = [call[0] for call in world.dispatch.calls]
    assert kinds.index("cancel") < kinds.index("reduce")

def test_kill_during_a_partial_fill_cancels_the_rest_and_reduces_the_filled_part(world):     # spec edge
    world.hold_position(conid=2, quantity=40, nlv=79_000.0)
    world.working_entry(conid=2, quantity=100, filled=40, group="mmr:og-aip-dec-00000001")
    world.monitor.tick(); world.run_liquidation()
    assert ("reduce", 2, 40.0) in world.dispatch.reduces() and world.dispatch.cancels() == ["entry:2"]

def test_late_entry_after_flat_starts_a_new_round(world):                        # K6
    world.drive_kill_to_flat()
    world.hold_position(conid=2, quantity=100, nlv=78_000.0)                     # entry acknowledged late
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.kill_round, rec.kill_flat_state, rec.kill_flatten_root) == (1, "PENDING", None)
    world.monitor.tick()
    assert world.store.active().kill_flatten_root.endswith("-1-1")

def test_failed_safe_flatten_ends_the_kill_without_a_new_round(world):
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick(); world.miss_the_flatten_deadline()
    world.monitor.tick()
    rec = world.store.active()
    assert (rec.kill_flat_state, rec.kill_round) == ("FAILED_SAFE", 0)
    assert world.breaker.calls                                                   # tripped by LiquidationService
    assert world.session_end.calls == [KillSessionEnd(ACCOUNT, ET_DATE, "KILLED", rec.kill_flat_at)]

def test_session_end_notice_once_after_flat(world):
    world.drive_kill_to_flat(); world.monitor.tick(); world.monitor.tick()
    assert len(world.session_end.calls) == 1 and world.session_end.calls[0].state == "KILLED"

def test_alert_goes_through_the_outbox_once(world):
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick(); world.monitor.tick()
    rec = world.store.active()
    assert world.outbox.calls == [(f"kill_started:{rec.experiment_id}:1", "kill_started", kill_alert_text(rec))]
    assert kill_alert_text(rec).startswith("PAPER")

def test_alert_failure_never_blocks_the_flatten_and_retries_once(world):         # Review Focus 5
    world.outbox.fail_next = True
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick()
    assert world.registry.account_owner(ACCOUNT) is not None
    assert world.store.active().kill_alert_state == "PENDING"
    world.monitor.tick()
    assert world.store.active().kill_alert_state == "ENQUEUED" and len(world.outbox.sent) == 1

def test_without_plan5_ports_nothing_is_left_pending(world_without_notices):
    world_without_notices.drive_kill_to_flat()
    rec = world_without_notices.store.active()
    assert (rec.kill_alert_state, rec.kill_session_end_state) == ("NO_OUTBOX", "NO_SINK")

def test_kill_while_the_session_flatten_owns_the_account_joins_it(world):       # spec edge, K3
    session_root = world.session.flatten_account_now("session-flatten-abc", LATER)   # session first
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick()
    assert world.store.active().kill_flatten_root == session_root
    assert world.liquidation_roots() == {session_root}

def test_session_flatten_after_the_kill_joins_the_kill_root(world):
    world.hold_position(conid=1, quantity=300, nlv=79_000.0); world.monitor.tick()
    kill_root = world.store.active().kill_flatten_root
    world.clock.set(FLATTEN_START); state = world.session.run_due(FLATTEN_START)
    assert state.flatten_command_id == kill_root

def test_kill_during_reprotect_reaches_flat_without_waiting_for_the_exits(world):
    world.partial_close_in_reprotecting(conid=1)                 # Plan 1 setup, as in
    world.drop_nlv(79_000.0); world.monitor.tick()               # test_kill_during_reprotect_cancels_replacement_exits_and_reaches_flat
    world.run_liquidation(); world.broker_shows_flat(generation=world.next_generation()); world.monitor.tick()
    assert world.store.active().kill_flat_state == "FLAT"

@pytest.mark.parametrize("crash_point", ["after_killed_write", "after_start_before_root_saved", "after_root_saved"])
def test_restart_while_killed_resumes_the_same_flatten(world, crash_point):     # K5
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.crash_at(crash_point); world.monitor.tick()
    restarted = world.restart(); restarted.liquidation.rescan(); restarted.monitor.recover()
    restarted.drive_kill_to_flat()
    assert restarted.store.active().kill_flat_state == "FLAT"
    assert len(restarted.liquidation_roots()) == 1                # never a second root for one round

def test_resume_racing_a_kill_never_loses_the_kill(world):                     # Review Focus 3
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.on_before_kill_write(lambda: world.experiments.pause(cmd("pause_experiment", experiment_id=world.id, reason="p")))
    world.monitor.tick()
    assert world.store.active().state == "KILLED"                 # kill CAS accepts PAUSED as well

def test_no_order_before_killed_is_committed(world):
    world.store_write_fails_once_on("KILLED")
    world.hold_position(conid=1, quantity=300, nlv=79_000.0)
    world.monitor.tick()
    assert world.dispatch.calls == [] and world.registry.account_owner(ACCOUNT) is None
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `_drive_kill` as the five steps above, one private method each (`_ensure_flatten`, `_ensure_alert`, `_prove_flat`, `_check_late_exposure`, `_ensure_session_end`). `recover()`: `record = store.active()`; if `KILLED`, call `_drive_kill(record)` once (it adopts or re-issues the root per K5); then set `_recovered = True`. `kill_alert_text`:

```python
def kill_alert_text(r: ExperimentRecord) -> str:
    return (f"PAPER experiment {r.experiment_id} KILLED at {r.killed_at:%Y-%m-%d %H:%M %Z}: "
            f"drawdown {r.kill_observed_drawdown_pct:.2f}% vs kill line {r.kill_drawdown_pct}% "
            f"({r.kill_basis} basis), net liquidation {r.kill_net_liquidation:,.2f} {r.base_currency}. "
            f"Account flatten started. Resume or stop with mmr experiment.")
```

- [ ] **Step 4: Run** the new file, `tests/test_liquidation_service.py`, `tests/automation/test_session_controller.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: drive the kill flatten to broker-proven flat and send the kill notices`.

---

### Task 6: Plan 3 integration — real experiments, entry block, dispatch gate

**Files:**
- Modify: `trader/automation/ai_paper_experiment.py` (`ExperimentView.entry_block: Optional[str] = None`; `ExperimentStateReader(store, monitor)`), `trader/automation/ai_paper_decision.py` (admission row 4c), `trader/trading/dispatch_guard.py` (`experiment_gate`, beside the `current_limits` provider Plan 3 added), `trader/trading/command_stack.py` (`_build_experiment_services`, pass the reader to `_build_ai_paper_services` instead of `NoExperiment()`, pass `experiment_gate` to `DispatchGuard`, `CommandStack.experiments`)
- Test: `tests/automation/test_ai_paper_experiment_integration.py`, `tests/test_command_stack.py` (replace Plan 3's `test_ai_paper_stack_uses_no_experiment_until_plan_four`)

**Interfaces:**
- Consumes: Tasks 1–5; Plan 3 `AiPaperDecisionService.execute`, `AI_PAPER_ACTION`, Task 7/8 test world of Plan 3 (`tests/automation/test_ai_paper_entry.py::world`, `submit`, `ENTER`, `CLOSE`).
- Produces:
  - `ExperimentStateReader.current(account_id) -> Optional[ExperimentView]`: `store.latest()` mapped to `ExperimentView(experiment_id, state, entry_block=monitor.entry_block(record))`; `None` if no experiment or another account.
  - `experiment_entry_refusal(reader, account_id) -> Optional[str]`: `NO_EXPERIMENT`, `EXPERIMENT_NOT_ARMED` (any state but `ARMED`), the `entry_block`, or `None`.
  - `DispatchGuard(..., experiment_gate: Callable[[Any], Optional[str]] = lambda request: None)`; refusal codes as K20.
  - `@dataclass(frozen=True) class ExperimentServices: store: ExperimentStore; service: ExperimentService; monitor: KillLineMonitor; reader: ExperimentStateReader; lock: ArmingLock`; `CommandStack.experiments: Optional[ExperimentServices]` (built on paper whenever the command stack is built, K15; `None` on live); `trader.experiment_store = store` (Plan 5's A1 reader).

Plan 3 admission gains, for `ENTER` only, after row 4 (`ARMED`):

| # | Check | Refusal |
|---|---|---|
| 4c | `view.entry_block is None` | the `entry_block` code (`KILL_LINE_UNKNOWN`, `EXPERIMENT_MONITOR_NOT_READY`) |

- [ ] **Step 1: Write the failing tests** (Plan 3's `world`, with the fake experiment port replaced by a real store + monitor on the same DuckDB)

```python
def test_enter_is_refused_from_the_moment_killed_is_stored(world):
    world.kill_line_hit()                                        # monitor writes KILLED, flatten pending
    assert submit(world).error_code == "EXPERIMENT_NOT_ARMED"

def test_enter_refused_while_the_kill_line_is_unknown(world):
    world.broker.fail_with("DAILY_PNL_UNAVAILABLE"); world.monitor.tick(); world.clock.advance(seconds=31)
    assert submit(world).error_code == "KILL_LINE_UNKNOWN"

def test_close_still_works_while_the_kill_line_is_unknown(world_with_position):
    world_with_position.broker.fail_with("DAILY_PNL_UNAVAILABLE")      # reductions use their own capture
    world_with_position.broker.recover_after_monitor()
    assert submit(world_with_position, CLOSE).error_code == "CLOSE_PENDING"

def test_entry_validated_before_the_kill_is_refused_at_dispatch(world):         # K20, spec edge
    world.on_before_guard(world.kill_line_hit)
    receipt = submit(world)
    assert receipt.error_code == "EXPERIMENT_NOT_ARMED" and world.dispatch.plans == []

def test_old_path_dispatch_ignores_the_experiment_gate(old_path_world): ...    # gate returns None for other actions

def test_raising_gate_refuses(world):
    world.reader.fail = True
    assert world.guard_refusal_for_ai_entry() == "EXPERIMENT_STATE_UNAVAILABLE"

def test_close_while_killed_joins_the_kill_flatten(world_with_position):       # Plan 3 R15 with the real kill
    world_with_position.kill_line_hit()
    receipt = submit(world_with_position, {**CLOSE, "action": "PARTIAL_CLOSE", "quantity": 100})
    assert receipt.outcome["close_root_id"] == world_with_position.store.active().kill_flatten_root

def test_close_after_killed_but_before_the_flatten_is_retryable(world_with_position):
    world_with_position.session.block_flatten_start = True                   # KILLED stored, no owner yet
    world_with_position.kill_line_hit()
    receipt = submit(world_with_position, CLOSE)
    assert (receipt.error_code, receipt.retryable) == ("KILL_FLATTEN_PENDING", True)

def test_decisions_refused_after_stop(world):
    world.drive_kill_to_flat(); world.stop()
    assert submit(world).error_code == "EXPERIMENT_STOPPED"
    assert submit(world, CLOSE).error_code == "EXPERIMENT_STOPPED"
```

```python
# tests/test_command_stack.py (replaces test_ai_paper_stack_uses_no_experiment_until_plan_four)
def test_ai_paper_stack_reads_real_experiments(paper_trader):
    stack = build_command_stack(paper_trader_with(AiPaperConfig(enabled=True)), POLICY, now=lambda: NOW)
    assert isinstance(stack.ai_paper.decisions._experiments, ExperimentStateReader)
    assert stack.experiments.reader is stack.ai_paper.decisions._experiments

def test_experiments_exist_with_ai_paper_disabled(paper_trader):              # K15
    stack = build_command_stack(paper_trader, POLICY, now=lambda: NOW)
    assert stack.ai_paper is None and stack.experiments is not None

def test_live_stack_has_no_experiments(live_trader): ...
def test_migration_70_is_applied(paper_stack): ...
def test_dispatch_guard_gets_the_experiment_gate(paper_stack): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `_build_experiment_services(trader, *, journal, broker_snapshot, liquidation_service, liquidation_store, exit_owners, session_controller, ledger, breaker_store, resume_ready, reconciliation_safe, paper_automation_service, ai_paper_built, now)` returns `None` on live; else applies migration 70 and builds the five objects. It runs **before** `_build_ai_paper_services` (which takes the reader), so `ArmingPorts.ai_paper_built` must be late-bound: `lambda: getattr(trader.command_stack, "ai_paper", None) is not None` (or a closure over a one-slot holder set after the stack is built), never a value captured at build time; `test_experiments_exist_with_ai_paper_disabled` and `test_cli_starts_and_dashboard_reads` cover both values. `ArmingPorts.account_cash = TraderServiceApi(trader).get_account_cash_by_currency`. `liquidation_roots = lambda: liquidation_store.transaction(liquidation_store.roots_to_advance_in_tx)`. `breaker_clear = lambda: breaker_store.get().state == "CLEAR"` (same lambda as `command_stack.py:780`). The gate:

```python
def _experiment_gate(reader, account_id):
    def gate(request):
        if getattr(request, "action", None) != AI_PAPER_ACTION:
            return None
        return experiment_entry_refusal(reader, account_id)
    return gate
```

  In `DispatchGuard.revalidate`, first thing after the account check: `try: code = self._experiment_gate(request) except Exception as exc: raise DispatchGuardError("EXPERIMENT_STATE_UNAVAILABLE", ...) from exc`; `if code: raise DispatchGuardError(code, "the experiment does not allow new exposure")`.
- [ ] **Step 4: Run** the new file, Plan 3's `tests/automation/test_ai_paper_entry.py` and `test_ai_paper_reductions.py`, `tests/test_command_stack.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: read real experiments in ai_paper admission and at dispatch`.

---

### Task 7: RPC, allow-list, monitor loop, CLI and docs

**Files:**
- Modify: `trader/messaging/production_api.py` (wire models next to `PauseTradingRequest` :544; handlers next to `_pause_trading_rpc_handler` :1274; registration in `build_production_registry` near the `liquidate_account` block :1874-1882), `trader/messaging/principals.py` (`TRADER_ACL`), `trader/trading/trading_runtime.py` (after `build_production_registry` at :482-491: `stack.experiments.service.attach_identity_check(...)`), `trader/trader_service.py` (`_maybe_start_experiment_monitor` after `_maybe_start_session_recovery`, :507-509), `trader/sdk.py` (`experiment_start/pause/resume/stop/status` over `_typed_command` / `_typed_query`, :333), `trader/mmr_cli.py` (`experiment` parser near `approve_p` :1353; dispatch near :2562; add `experiment` to `_ib_commands` :2066-2076 and to the trader-service set :12000-12006), `AGENTS.md`
- Test: `tests/test_experiment_rpc.py` (new), `tests/test_rpc_acl.py`, `tests/test_trader_service_experiment.py` (new), `tests/test_mmr_cli_experiment.py` (new)

**Interfaces:**
- Consumes: Tasks 1–6; Plan 2 `register(..., with_caller=True)`, `RpcCaller`; `_on_worker`, `_watched_ticks` (`trader_service.py:198-240`).
- Produces:
  - Wire models (`ConfigDict(extra="forbid", strict=True)`): `StartExperimentRequest(command_id: str, reason: str)`; `ExperimentCommandRequest(command_id: str, experiment_id: str, reason: str)` for pause / resume / stop; `GetExperimentRequest()`. `command_id` via `_reject_colon_in_command_id`; `experiment_id` `^exp-[0-9a-f]{20}$`; `reason` 1–200 characters.
  - Coordinator actions `start_experiment`, `pause_experiment`, `resume_experiment`, `stop_experiment`: single-step (`saga=False`), `requires_preflight=False` (paper only; `start` refuses live). `target_type="experiment"`, `target_id` = account id for start, else the experiment id. Handlers build `CommandRequest(..., source=caller.principal, principal=caller.principal)`.
  - Query `get_experiment` → `{"experiment": record_view | None, "entry_block": str | None, "last_evaluation": {...} | None, "kill_line": {"active", "configured", "pending_restart", "detection"}, "mode_conflict": str | None, "transitions": [...]}` (newest experiment of the account; `kill_line` per K1 and K9).
  - ACL entries:

```python
    # explicit sets per method, never a group alias (Plan 3 R23, owner answer 6)
    ("command", "start_experiment"): frozenset({"cli", "dashboard"}),
    ("command", "pause_experiment"): frozenset({"cli", "dashboard", "ai_supervisor"}),   # K14
    ("command", "resume_experiment"): frozenset({"cli", "dashboard"}),
    ("command", "stop_experiment"): frozenset({"cli", "dashboard"}),
    ("query", "get_experiment"): frozenset({"cli", "dashboard", "ai_supervisor"}),
```

  - CLI: `mmr experiment start --reason TEXT`, `mmr experiment pause|resume|stop [--experiment-id ID] --reason TEXT` (the id defaults to the active experiment from `get_experiment`; the server still checks it), `mmr experiment status`. All support `--json`. `status` prints "flatten pending" until `kill_flat_state == "FLAT"`, the word `PAPER`, the active kill line and its detection delay (K1), and, when `pending_restart`, "edited kill line X% is NOT active until trader_service restarts" (K9).
  - `_maybe_start_experiment_monitor(trader, loop, worker, stopping)`: runs `monitor.recover()` on the worker before readiness, then `_watched_ticks('experiment monitor', lambda: _on_worker(worker, monitor.tick), interval=5.0, stuck_after=_WORKER_STUCK_AFTER_SECONDS)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_experiment_rpc.py — `served` = real build_command_stack (paper, ai_paper enabled) and the real
# build_production_registry behind a TypedRpcServer; clients sign with make_identities().
def test_edited_yaml_is_never_shown_as_active(served, trader_yaml):            # K9
    trader_yaml.set_kill_pct(10.0)                                               # edit on disk, no restart
    line = served.client("cli").call("get_experiment", {}, dict)["kill_line"]
    assert (line["active"]["pct"], line["configured"]["pct"], line["pending_restart"]) == (20.0, 10.0, True)
    restarted = served.restart()
    line = restarted.client("cli").call("get_experiment", {}, dict)["kill_line"]
    assert (line["active"]["pct"], line["pending_restart"]) == (10.0, False)      # the post-restart verification

def test_cli_starts_and_dashboard_reads(served):
    out = served.client("cli").call("start_experiment", {"command_id": "s1", "reason": "go"}, dict)
    assert out["state"] == "RESOLVED" and out["outcome"]["state"] == "ARMED"
    assert served.client("dashboard").call("get_experiment", {}, dict)["experiment"]["state"] == "ARMED"

@pytest.mark.parametrize("principal,method", [
    ("ai_supervisor", "start_experiment"), ("ai_supervisor", "resume_experiment"),
    ("ai_supervisor", "stop_experiment"), ("ai_research", "pause_experiment"),
    ("ai_research", "get_experiment"), ("strategy", "start_experiment"), ("strategy", "get_experiment")])
def test_wrong_principal_is_denied(served, principal, method):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client(principal).call(method, valid_body(method, served), dict)
    assert exc.value.code == "PERMISSION_DENIED"

def test_ai_supervisor_may_pause(served):
    start(served)
    out = served.client("ai_supervisor").call("pause_experiment", body("pause_experiment", served), dict)
    assert out["outcome"]["state"] == "PAUSED"

def test_service_refuses_a_wrong_principal_that_bypasses_the_acl(served):
    receipt = served.coordinator.execute(resume_request(served, principal="ai_supervisor"))
    assert receipt.error_code == "PRINCIPAL_FORBIDDEN"

@pytest.mark.parametrize("patch", [{"reason": True}, {"reason": 1}, {"experiment_id": 7},
    {"experiment_id": True}, {"command_id": "a:b"}, {"extra": 1}])
def test_wire_is_strict(served, patch):
    start(served)
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("cli").call("pause_experiment", {**body("pause_experiment", served), **patch}, dict)
    assert exc.value.code == "VALIDATION_ERROR"

def test_same_command_id_replays_the_receipt(served):
    first = start(served, command_id="s1")
    assert start(served, command_id="s1") == first

def test_start_refused_with_ai_paper_disabled(served_disabled):
    out = served_disabled.client("cli").call("start_experiment", {"command_id": "s1", "reason": "go"}, dict)
    assert (out["state"], out["error_code"]) == ("REJECTED", "AI_PAPER_DISABLED")

def test_identity_check_attached_after_the_registry(served):
    assert served.stack.experiments.service.identity_problem() is None

def test_no_experiment_method_writes_the_kill_line_or_ceiling():
    assert not [m for _, m in TRADER_ACL if "kill" in m or "ceiling" in m]
```

```python
# tests/test_rpc_acl.py (added)
def test_experiment_rights():
    rights = lambda p: {k for k, v in TRADER_ACL.items() if p in v}
    for p in ("cli", "dashboard"):
        assert {("command", f"{a}_experiment") for a in ("start", "pause", "resume", "stop")} <= rights(p)
    assert {("command", "pause_experiment"), ("query", "get_experiment")} <= rights("ai_supervisor")
    assert not {("command", "start_experiment"), ("command", "resume_experiment"),
                ("command", "stop_experiment")} & rights("ai_supervisor")
    assert not {k for k in rights("ai_research") if "experiment" in k[1]}
```

```python
# tests/test_trader_service_experiment.py
def test_monitor_recovers_before_readiness_and_after_session_recovery(fake_trader, order_log):
    run_startup(fake_trader)
    assert order_log.index("liquidation_rescan") < order_log.index("session_recover") \
        < order_log.index("experiment_recover") < order_log.index("trader_run")

def test_monitor_ticks_on_the_liquidation_worker(fake_trader): ...   # thread name of the worker
def test_monitor_loop_not_started_while_stopping(fake_trader): ...
def test_monitor_loop_survives_a_failing_tick(fake_trader): ...
```

```python
# tests/test_mmr_cli_experiment.py — fake SDK
def test_status_says_flatten_pending_until_flat(cli, sdk):
    sdk.experiment = {"state": "KILLED", "kill_flat_state": "PENDING"}
    assert "flatten pending" in cli("experiment status") and "PAPER" in cli("experiment status")

def test_resume_defaults_to_the_active_experiment_id(cli, sdk): ...
def test_status_shows_active_and_pending_kill_lines(cli, sdk):                  # K9, owner answer
    sdk.kill_line = {"active": {"pct": 20.0, "basis": "start"}, "configured": {"pct": 15.0, "basis": "start"},
                     "pending_restart": True, "detection": "IB account updates, about 3 minutes; paper only"}
    out = cli("experiment status")
    assert "active kill line 20.0%" in out and "15.0% is NOT active until trader_service restarts" in out
    assert "about 3 minutes" in out
def test_json_output_shape(cli, sdk): ...                         # {"data": ..., "title": ...}
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Registration in `build_production_registry` when `command_stack.experiments is not None` (K15), with `with_caller=True` handlers like Plan 3's.
  - `trading_runtime`: after the registry is built, `stack.experiments.service.attach_identity_check(lambda: ai_supervisor_identity_problem(self.rpc_identity, production_registry))`, where `ai_supervisor_identity_problem` (in `experiment_service.py`) returns `AI_SUPERVISOR_KEY_MISSING` unless `isinstance(identity, ServiceIdentity) and identity.accepts("ai_supervisor")`, and `AI_ALLOW_LIST_NOT_LOADED` unless `registry.resolve("command", "submit_ai_paper_decision")` exists with `allowed_principals == frozenset({"ai_supervisor"})`.
  - `trader_service`: `_maybe_start_experiment_monitor` mirrors `_maybe_start_session_recovery` (same stopping rules, same log lines with "experiment monitor").
  - `AGENTS.md`: one paragraph "**Experiments and the kill line (SP1)**" under "Key Patterns": the four states, who may call which method, arming checks, the lock with the one-strategy path, the kill flow (KILLED first → `SessionController.flatten_account_now` → account flatten → flat on broker evidence → stop → new experiment; `KILLED` is never resumed), `KILL_LINE_UNKNOWN`, the outage pause (K23), the about-3-minute kill detection on paper only (K1), kill-line edits active only after a restart (K9), `BOTH_MODES_ARMED` (K17), migration 70; in `docs/OPERATIONAL_STATE.md` "Known blockers": **live blocker: the kill line reads IB account updates (about 3 minutes); a faster, verified detector is required before any live use** (K1); and the `mmr experiment …` lines under "CLI Commands" and "Requires trader typed RPC".
- [ ] **Step 4: Run** the four test files, `tests/test_rpc_acl.py`, `tests/test_production_rpc_security.py`, then the full suite.
- [ ] **Step 5: Commit** — `feat: expose experiments over typed rpc and the mmr experiment command`.

---

## Self-review against the spec

| Spec requirement (5.5, 2, 3, 5.2, 5.3, 5.4, 6) | Task |
|---|---|
| `experiment start` arms only with `ai_paper.enabled` | 3 (`AI_PAPER_DISABLED`), 7 |
| only on a paper account | 3 (`ACCOUNT_NOT_PAPER`) |
| no positions and no working orders | 3 (`NOT_FLAT`, external orders included) |
| one-strategy automation not armed | 3, 4 (`ONE_STRATEGY_ARMED`) |
| `ai_supervisor` key in the keyring and allow-list loaded; old shared key does not count | 3, 7 (K22) |
| records the start net liquidation | 1, 3 |
| Activate / hot-arm refused while `ARMED`, `PAUSED`, `KILLED` | 4 (and K17 at startup) |
| kill line `experiment_kill_drawdown_pct`, default off; basis `start` / `peak` | 2, 4 (K9) |
| checked against every promoted snapshot | 4 (K1) |
| ceiling drawdown keeps working beside it; tighter acts first | K8 (Plan 3 check untouched) |
| 1. `KILLED` durable before any order; admission refuses from then | 4, 6 |
| 2. `SessionController` claims the account owner, flatten via `LiquidationService` under takeover rules | 4, 5 (K2, K3) |
| 3. "kill started" alert through the outbox | 5 (K18) |
| 4. "flat" only after broker evidence; missed deadline is `FAILED_SAFE` + breaker | 5 (K6) |
| only `resume` (`cli`, `dashboard`) clears `PAUSED`; `KILLED` is never resumed (owner answer, overrides spec 5.5); AI cannot | 3, 7 (K10, K14) |
| `stop` (`cli`, `dashboard`) → `STOPPED`; `NOT_FLAT`; final; releases the lock; new id needed | 1, 3, 4 |
| states survive restarts; restart does not clear `KILLED` | 1, 4, 5 |
| trader reconciles with the broker before admitting a decision | 5 (`recover` before readiness), 6 (`EXPERIMENT_MONITOR_NOT_READY`) |
| `experiments` table fields (5.2): id, start time, start NLV, config digest, styles, kill line, state | 1 |
| reductions while `KILLED` join the flatten; `STOPPED` refuses everything | 6 (with Plan 3 R15) |
| section 6: refused with `enabled: false`, leftover position, working order, only the old key | 3 |
| section 6: `stop` refused while not flat, `KILLED`→`STOPPED` without re-arming, releases the lock, not callable by AI | 3, 7 |
| section 6: `equity_daily` row after `KILLED` and `FAILED_SAFE` | 5 (the notice; the row is Plan 5's test) |
| section 6: one-strategy arming refused while armed | 4 |
| section 6: `KILLED` stored before the first flatten order | 4, 5 |
| section 6: "flat" not reported before the broker confirms it | 5 |
| section 6: kill line survives a restart; both bases; AI cannot resume | 4, 2, 3 |
| brief edges: open entry, partial fill, restart while KILLED / arming, double arm, stale evidence, unknown P&L, concurrent kill and session flatten, wrong principal, strict types | 5, 5, 5 / 3, 3, 3, 4, 5, 3 / 7, 1 / 3 / 7 |

Not in this plan: the scoreboard tables, `equity_daily` and the Telegram sender (Plan 5; this plan calls its ports); the acceptance harness and the real IB paper session (Plan 6); dashboard buttons for experiments (none in SP1; the methods allow `dashboard` for later); `/pause`, `/flatten` over Telegram (SP2).

## Open questions for the owner

All eight questions were answered by the owner on 2026-10-06 and are folded into K1, K7/K23, K10, K14, K9, K12, K17 and K18. None open.
