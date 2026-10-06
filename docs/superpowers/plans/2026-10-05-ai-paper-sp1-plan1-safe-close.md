# AI Paper SP1 — Plan 1: Safe Close — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Run the tasks in the "Execution order" below, not in number order.** The tasks appear in this file in execution order.

**Goal:** Make every exit on the paper path go through one broker-verified close service. It hands protection over before it cancels a stop, owns one close per position, re-protects after a partial close with a linked stop/target, journals every child order before it is sent, never sends a reduce while a child order is unknown or still working, and never reports a close that sold nothing as a success.

**Architecture:** `LiquidationService` gains a `scope` (`account` | `conid`) and a `goal` (`zero` | `partial`). Runs, child orders and "which command joined which root" are journal rows (migration 36, which also adopts runs that were open before the upgrade). A new `ExitOwnerRegistry` (migration 35) holds at most one active close per `(account, conid)` and one account flatten per account; a claim and the run it creates commit in one transaction. `ProtectiveOrderSaga` gets a `CLOSE_OWNED` state with the exact order ids the close will cancel (migration 37), and every saga write is revision-checked. Broker order rows carry their OCA group and type (migration 38). All exits use one reduce-only order path on `Trader` that does not run the entry gates. Every `LiquidationService` entry point runs on one worker thread, never on the IB event loop. Time exits, one-strategy SELL intents, the session flatten, `/flatten` and protective failure all use this service. Commands that join a root resolve from that root, through the reconciler only.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DomainJournal`), dataclasses, `concurrent.futures`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, section 5.1 (plus the bindings in 5.5 step 2 and the test list in section 6, "Safe close"). This plan is delivery step 1 of section 7. Steps 2–6 get their own plans.

## Global Constraints

- No live authority. Nothing here may run on a live account; `account_mode` checks stay as they are.
- One broker dispatch boundary: every order goes through `TradingRuntimeOrderDispatch` → `Trader`. Entries use `Trader.place_expressive_order`; every exit (full reduce, partial reduce, re-protect leg) uses `Trader.place_reduce_only_order` (added by PR #42, extended by Tasks 14 and 10). There is no second IB order path and no "skip checks" flag.
- `CommandReceipt` stays frozen. `ExecutionIntent` is not changed.
- `OUTCOME_UNKNOWN` is never resubmitted under a fresh id. An `UNKNOWN` child order is never sent again (R3).
- The trader journal (`trader.journal_db`) is the source of truth. No in-memory receipt is authoritative: the service reads runs, children and owners from the journal on every step.
- Journal migrations: **35** exit owners, **36** liquidation run columns + `liquidation_children` + `liquidation_joins` (and the adoption of open pre-SP1 runs), **37** saga close-ownership columns + `automated_order_saga_groups`, **38** `oca_group`/`oca_type` on `broker_orders`. Versions 30 and 31 are taken, and **32–34 belong to `trader/data/attribution_store.py`**. P3 owns 30–39 (`trader/data/schema_migrations.py`; Task 3 registers 35–38 there), P4 owns 40–49 and P5 50–59. `SchemaMigrator` records a version once, so reusing a taken number silently skips the new DDL. Free in the P3 range after this plan: **39** only.
- Line numbers in this plan cite master as of `15f9e715` (PR #42 merged). An earlier task can shift them; the function or class name next to each number is the anchor. Where a task edits a file PR #42 changed (`trading_runtime.py`, `trader_service.py`, `liquidation_service.py`, `session_controller.py`), it names functions, not lines.
- Command ids and child ids never contain `:` (child ids become IB `orderRef` values via `encode_order_ref`).
- Routine progress of a scoped close never trips the breaker; neither does `REDUCE_FAILED` (its command fails with an operator alert instead). Account-scope breaker behaviour is unchanged (every state except `FLAT` trips).
- Test-first. Each task begins with a failing test. Run tests with `.venv/bin/python -m pytest <path> -q --timeout=30`. The full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects follow the repo style (`feat:`, `fix:`, `test:`, `refactor:`), lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Old-path entry admission and risk ceilings do not change. The only old-path behaviour changes are: time exits and SELL intents use the scoped close; the session cancel phase cancels our working entries only (a partly filled entry's rest included) and closes protection that is bigger than its position; every exit skips the entry gates (R11) and a SELL exit skips the pause on new exposure (R32).
- Nothing calls `SessionController.on_bar` live yet (checked: no caller in `trader/`). The time-exit bug is therefore latent, but the adapter is fixed here so the first live caller (SP2) is safe.

## Execution order

New tasks 14–18 must run before some old ones. Run the tasks in this order (each row lists what it needs):

1. **Task 1** — red test for the time-exit bug (needs nothing).
2. **Task 2** — child ids and leg classification (needs nothing).
3. **Task 3** — exit owner registry, migration 35.
4. **Task 18** — broker evidence: readiness property, newest generation, OCA fields, migration 38 (needs 2).
5. **Task 4** — data model (frozen interfaces), migration 36, account scope on write-ahead children (needs 2, 3, 18).
6. **Task 14** — extend master's reduce-only path: the R35 bound, `DispatchRefused` before the boundary, `reduce_partial`, `cancel_on_loop` (needs 4 for `DispatchRefused`).
7. **Task 15** — share master's liquidation worker with every producer (needs 4, 14).
8. **Task 5** — scoped full close (needs 4).
9. **Task 6** — partial close, re-protect, escalation (needs 5).
10. **Task 7** — account flatten takes over scoped closes (needs 6).
11. **Task 8** — scoped claims: join, upgrade, refuse (needs 6, 7).
12. **Task 9** — protective saga `CLOSE_OWNED`, revision-checked writes, migration 37 (needs 4 for `HandoverInfo`, `CancelTarget`).
13. **Task 10** — exit legs by order identity (needs 14).
14. **Task 11** — time exits use the scoped close; the session polls its returned root (needs 1, 5, 15).
15. **Task 16** — session cancel phase cancels our entries only (needs 11: an oversized stop is closed through the time exit).
16. **Task 12** — SELL intents become a proven-reduction close (needs 6, 8).
17. **Task 17** — joined commands resolve from their exact root (needs 4, 12).
18. **Task 13** — production composition and the end-to-end tests (needs all).

## Review Focus

Spec-implied inputs no test list in the spec names, and the review traces of rounds 1 and 2. Each has a named test.

1. **Short positions.** A scoped close of a short reduces with `BUY`; a partial re-protects with the stop *above* the price. → Task 5 `test_full_close_of_short_reduces_with_buy`, Task 6 `test_partial_close_of_short_reprotects_above_price`.
2. **Partial quantity.** `q < 1` refused, `q ≥ |position|` refused at start, less than one share left = full close, live position `≤ q` at dispatch = full close of the remainder (R15). → Task 6 `test_partial_quantity_edge_cases`, `test_live_position_at_or_below_q_at_dispatch_is_fully_closed`.
3. **Position already gone.** The stop filled during the cancel race: the close ends `CLOSED` with no reduce. → Task 5 `test_close_ends_closed_without_reduce_when_the_stop_filled_in_the_cancel_race`.
4. **Root rebinding.** A root id or a joined command id cannot be rebound to another scope, conid or goal. → Task 5 `test_start_refuses_rebinding_root_to_another_scope`, Task 8 `test_joined_request_retried_after_restart_returns_the_same_root`.
5. **Restart in the middle of re-protect.** The restart sends only the planned target, never the stop again. → Task 6 `test_recovery_after_restart_sends_only_the_planned_target`.
6. **Invisible child / late fill.** No second reduce while a child is not visible yet, or when its fill arrives after the position was captured; absence needs a complete enumeration opened after the send. → Tasks 4, 5, 7, 13.
7. **Two account producers.** One root, one reduce. → Task 4, Task 13.
8. **Exits after a loss breach.** The reduce-only path passes while a new entry is refused. → master's `test_reduce_only_passes_after_daily_loss_breach_while_entry_is_refused` (PR #42), Task 13.
9. **Event loop.** A tick awaited on the trader loop does not deadlock; a blocking call on the loop is refused. → Task 15 (master's `tests/test_trader_service_loops.py` already pins the ticks on a real loop).
10. **The generation refresh (new behaviour).** The close asks for a fresh broker sync while it waits (ruling 1). This adds `run_broker_sync` calls during a session; `capture` raises `GENERATION_STAGING` while a sync is staging, so every reader of the broker snapshot can see short "unavailable" windows. Check this against the paper session in plan 6.
11. **Round 2 traces.** A target fill that cancels its OCA stop ends `CLOSED` (Task 6 `test_target_fill_that_cancels_its_oca_stop_ends_closed_without_a_failure`); a partial reduce that sold nothing ends `REDUCE_FAILED`, never success (Task 6, Task 13); an ingest event racing the worker never loses the close owner (Task 9 race tests); an order found later is handed over before its cancel (Task 4 `test_hand_over_is_updated_before_every_cancel_batch`, Task 9); a run open before the upgrade settles its old reduce before any new one (Task 4 adoption tests).
12. **Round 4 traces.** A `PendingCancel` stop never lets the target out or DONE commit (ruling 47); DONE and the owner release commit while broker changes are held (ruling 48); an old `FLAT` run and a late fill on a terminal child both block the next reduce (rulings 49, 50); a conId or partial quantity is never coerced (ruling 51); malformed close input is `NOT_SENT` (ruling 52); Task 18 runs green before Task 4 (ruling 53).


## Design amendments (review round 1)

Two reviews on tickets #16–#29 (2026-10-06) found that the first version of this plan could send a second reduce, lose ownership in a crash, and cancel protection before a lower gate refused the exit. The findings were checked against the code and accepted. The rules below are binding. Every task follows them. Where a task's text disagrees, these rules win.

**Children and evidence**

- **R1. Write-ahead children.** Every child order (cancel, reduce, re-protect stop, re-protect target) is written to the journal **before** the broker call, with its own child id and a submission fence (the broker generation at send time). Child id: `{root}-{kind}-{conid}-{attempt}` (no `:`). The OCA pair journals both leg intents before either leg is sent.
- **R2. Child states.** `UNKNOWN` (written, outcome not proven), `WORKING`, `FILLED`, `CANCELLED`, `REJECTED`, `ABSENT`, `NOT_SENT`. `NOT_SENT` only for a proven pre-submit refusal (the call raised or returned a typed refusal before the broker boundary was crossed). A timeout or any other exception after the boundary is `UNKNOWN`.
- **R3. Never resubmit an unknown.** An `UNKNOWN` child is never sent again, under the same id or a new one. Only a `NOT_SENT` attempt — or, for a reduce or cancel, an attempt a complete enumeration proved `ABSENT` (R22) — may be followed by a new attempt, and that attempt gets a new id (`attempt + 1`). A re-protect leg is never tried again (R30). This is spec section 4 ("never resubmitted under a fresh id") applied per child.
- **R4. Absence needs positive evidence.** A child becomes terminal only when a broker generation newer than its fence shows it `Filled`, `Cancelled`, `ApiCancelled`, `Inactive` or `Rejected`, or when a complete, fenced broker enumeration newer than its fence proves it absent (the same standard `OutcomeReconciler._reconcile_approve` uses). An empty `find_orders` result means `UNKNOWN`. Inherited re-protect legs follow the same rule. (Made exact by R22: `enumeration_complete()` and a generation newer than the child's send fence; ruling 2 is gone.)
- **R5. Reduce rule.** No reduce and no OCA placement while any child of the root is `UNKNOWN`, or while ~~a reduce child~~ any child (R23) is `WORKING`. A further reduce needs a position snapshot whose generation is newer than the terminal observation of every earlier reduce child, and it is sized from that snapshot. A deadline with any child still `UNKNOWN` ends `FAILED_SAFE` with no new order.

**Ownership and durability**

- **R6. One transaction.** Exit owner rows, liquidation runs, goals, cursors, `SUPERSEDED` marks and child rows live in the trader journal. Claim plus run creation, goal upgrade plus cursor change, account claim plus supersede plus child inheritance, and every terminal transition each happen in **one** journal transaction. No in-memory receipt is authoritative.
- **R7. Re-read before dispatch.** Before every broker call the service re-reads the run and its owner from the journal. It dispatches only if the owner is `ACTIVE`, the run is neither terminal nor `SUPERSEDED`, and the goal still matches. `_set` refuses to overwrite `SUPERSEDED` or any terminal state.
- **R8. Durable cleanup.** A terminal transition sets `cleanup_pending` in the same transaction. Cleanup (saga hand-back or close; ~~owner release~~ — the owner is released in the terminal transaction, R24) is idempotent, and recovery loads every run with `cleanup_pending` and finishes it. Account `FLAT` closes every saga the flatten owns and releases the account owner.
- **R9. Inheritance after failure or takeover.** A new owner of a conid or of the account inherits every non-terminal child of any `SUPERSEDED` or `FAILED_SAFE` root on that scope, and obeys R5 for them. `FAILED_SAFE` marks the owner row `FAILED_SAFE` (never "released as flat"); a later claim may start a new root, but that root inherits the old unknown children first.
- **R10. Account join.** `start(scope="account")` claims before it creates a run. `JOINED_FLATTEN` returns the existing root's receipt and creates nothing. Every account producer (session flatten, `/flatten`, protective failure, later the kill) persists and polls the root it got back, including after a join.

**Broker boundary and threading**

- **R11. One reduce-only path.** A new method on `Trader` at the existing order boundary sends reduce-only orders: full reduce, partial reduce and the exit OCA legs. It checks the account fence, that the side reduces the broker position, and that the quantity is at most the position. It does **not** run the entry gates (`RiskGate` daily loss, open orders, rate, concentration, leverage). There is no caller flag to skip checks. `reduce_position` (account flatten) moves onto it too. This also fixes a bug in today's code: an account flatten after a daily-loss breach can be refused by `RiskGate`. The after-breach regression test belongs to this plan, not to plan 3.
- **R12. One serialized worker.** Every `LiquidationService` entry point (`start`, `rescan`, `upgrade_to_zero`, `supersede`) runs on one dedicated single-thread worker, never on the IB event loop. The trader_service recovery loop, the session controller loop and the ingest thread (protective failure → `start`) submit to that worker and await it. Dispatch from the worker uses `run_coroutine_threadsafe` onto the main loop, which is then safe. This fixes a second bug in today's code: `rescan()` runs on the loop it waits on (`trader/trader_service.py` `_liquidation_recovery_loop`). Tested with a real asyncio loop, not `asyncio.run` stand-ins.
- **R13. OCA by identity.** A local `Trade` echo (`PendingSubmit`) is not acceptance. Events are matched by order identity. The target leg is sent only after the stop's state is reconciled: if the stop already filled in part or in full, the target is sized from the live remaining position; if the stop is `Inactive` or `Rejected`, escalate. `DONE` compares the outstanding quantity (`total_quantity − filled_quantity`) of each leg with the remaining position, and checks the same OCA group, the protective side and a working status — as the broker row reports them (R38, Task 18), not as the journal wrote them.

**Protection**

- **R14. Exact hand-over.** `CLOSE_OWNED` stores the exact refs the close will cancel and the protection generation. Only `Cancelled` / `ApiCancelled` for those refs is suppressed. `Inactive`, `Rejected`, or a cancel of any other ref still takes today's incident path. Active legs are tracked apart from historical aliases; an event for a retired leg never changes current protection. Account takeover transfers `CLOSE_OWNED` sagas to the account root.

**Quantities**

- **R15. Partial quantity.** `q = floor(requested)`. `q < 1` → refused `PARTIAL_QUANTITY_INVALID`. `q ≥ |position|` at `start` → refused `QUANTITY_ABOVE_POSITION` (send `CLOSE` instead). `|position| − q < 1` → full close. Right before dispatch the rule is applied again to the live position; if the live position is now `≤ q`, the remainder is fully closed (ruling: protection is already cancelled, so refusing would leave it unprotected; the spec is silent). After a terminal partial fill the close re-protects the **actual** remaining quantity from a fresh snapshot; if it is zero the close ends `CLOSED`.

**Session controller and commands**

- **R16. Cancel entries only.** The session cancel phase cancels only working entry orders with zero fill. It never cancels the protective children of a filled position. ~~A partly filled entry is left to the flatten, which owns its protection.~~ Replaced by R37: a partly filled entry's rest is cancelled too, and protection bigger than its position is closed.
- **R17. Joined commands resolve.** A command that joins or upgrades another root records `(command_id, root_id)` in the journal (migration 36). `OutcomeReconciler` gets an `execute_automated_intent` branch: `CLOSED` / `DONE` on the exact root resolves it; `SUPERSEDED` follows to the superseding root; `FAILED_SAFE` or unknown never becomes success (R33: a decided failure is rejected with an operator alert; an open root stays unknown). A root that ends before the command records `CLOSE_PENDING` is handled too.

**Tests**

- **R18.** Task 1's red test uses today's constructor (`SessionTimeExitAdapter(dispatch)`) with a fake broker that keeps a working stop, and asserts the behaviour (no working stop after the position is closed). It is not a constructor `TypeError`.
- **R19.** Task 4's rescan test asserts that the new root advanced and the old root stayed `FAILED_SAFE`. `FLAT` is asserted only after a newer empty snapshot and a terminal reduce child.
- **R20.** Crash-injection tests restart at every boundary: after a child is journaled and before the broker call; after the broker accepted and before the ack is stored; after a goal upgrade; after a supersede; after a terminal save and before each cleanup step.
- **R21.** Task 13 calls the real `build_command_stack` with a temporary DuckDB journal, the real registry, run store, saga, coordinator, risk gates and `TradingRuntimeOrderDispatch` over a fake IB client, on a real asyncio loop with the R12 worker. Only broker and market ports are fake. It covers cold start and hot-arm wiring and every integration case listed on #29.


### Rulings made while rewriting

The rules above were checked against the real code while the tasks were rewritten in round 1. Rulings struck through below are replaced by the round-2 rules (R22–R38); rulings 19–30 were added in round 2. Where a rule could not be applied as written, or two rules met, the smallest spec-consistent option was chosen. Each ruling is binding like the rules.

1. **The fence is the promoted generation, and the close asks for a new one.** A promoted broker generation changes only when `BrokerIngest.run_broker_sync` runs, and today that is only at (re)connect (`trader/trading/trading_runtime.py:894` and `:908`). With R1/R4/R5 as written, every close would wait for a newer generation that never comes in a session and end `FAILED_SAFE`. So the fence stays `BrokerRiskSnapshot.generation_id` (a complete enumeration, as R1 says), and the service calls a non-blocking `GenerationRefreshPort.request_refresh` whenever it waits. Task 13 wires it to `run_broker_sync`, at most once every 5 seconds. `source_cursor` is not used as a fence, because our own journal writes also move it.
2. ~~**Absence needs the second newer generation.**~~ **Replaced by R22 (D1).** ~~R4 accepts "a complete, fenced broker enumeration newer than its fence". The first newer generation can have started between our snapshot and our send, so it can miss the order. An empty lookup therefore becomes `ABSENT` only when `generation >= fence + 2`; on `fence + 1` the child stays `UNKNOWN`. An `ABSENT` child counts as "may have filled".~~
3. **~~Terminal needs a newer generation;~~ WORKING does not.** ~~A terminal status counts only when `generation > fence` (R4).~~ Replaced by R23: a terminal status of the child's own row counts at once. A `Submitted`/`PreSubmitted`/`PendingCancel` row makes a child `WORKING` at once. `PendingSubmit` and `ApiPending` keep it `UNKNOWN` (R13: a local echo is not acceptance).
4. **R5 covers every fill, not only reduce children.** A reduce, a re-protect leg, `CLOSED`, `DONE` and `FLAT` need a snapshot whose generation is newer than the last observation of *every* child of the root that filled or may have filled. That includes a cancel target (the stop) that filled before our cancel landed: it changes the position exactly like a reduce. A cancel target that ended with no fill puts no limit on sizing.
5. **New child state `PLANNED`.** R1 journals both OCA leg intents before either is sent; R13 sends the target only after the stop is reconciled; R3 never re-sends an `UNKNOWN` child. So a written but not yet sent target needs its own state: `PLANNED`. It becomes `UNKNOWN` in one transaction just before its broker call. When a root stops (terminal, `SUPERSEDED`, goal upgrade, escalation) its `PLANNED` children become `NOT_SENT`. `PLANNED` never blocks a reduce.
6. **Child ids and the spec's re-protect refs.** The R1 child id `{root}-{kind}-{conid}-{attempt}` is also the decoded order ref. So spec 5.1's refs `{root}-reprotect-stop` / `{root}-reprotect-target` / OCA group `{root}-reprotect` become `{root}-reprotect-stop-{conid}-{n}`, `{root}-reprotect-target-{conid}-{n}` and `{root}-reprotect-{conid}-{n}`. ~~A leg tried again after `NOT_SENT` gets attempt `n+1` and keeps its pair's OCA group.~~ (Replaced by R30: a re-protect leg is never tried again.) For cancels, `attempt` counts the cancels of that root on that conid.
7. **Cancel evidence is the target order.** IB attaches no order ref to a cancel, so a cancel child is reconciled from the row of the order it cancels (`get_order(order_entity_id)`), never from `find_orders(child_id)`. `CancelUnresolved` (no live order was found, nothing was sent) is a proven pre-submit refusal and makes the child `NOT_SENT`. A cancel the broker does not apply leaves the child `WORKING`; this root does not send it again, and the deadline ends `FAILED_SAFE` (spec test "cancel rejected"). A cancel that was journaled but cut off before its send becomes `NOT_SENT` at the next tick and is sent again (R38); a later owner sends its own cancel for an inherited one.
8. **R12 and the ingest thread.** The protective-failure producer runs on the broker ingest thread while `_apply_batch` holds `_apply_lock` (`trader/trading/broker_ingest.py:677`). The worker's first step, `broker_snapshot.capture` → `_broker_ready` → `BrokerIngest.is_ready`, takes the same lock (`broker_ingest.py:451-453`). If the ingest thread waited for the worker, both would block for ever (today the same call already blocks the ingest thread on itself). So the ingest producer queues `start` on the worker and does not wait (`SerializedLiquidation.nonblocking()`). It stays durable: the saga row is `SAFETY_FAILED` with `flatten_requested`, and every worker tick starts a flatten for each flagged saga that has no liquidation join row (R29: a saga that was already `SAFETY_FAILED` before the upgrade is not flagged). The recovery loop and the session loop *await* the worker; RPC threads block on it; a blocking call on an event loop raises.
9. ~~**Cleanup order.**~~ **Replaced by R24 and R33.** ~~R8 cleanup is: (1) the saga step; (2) owner release plus clearing `cleanup_pending`, in one transaction; (3) resolving the commands of the root. Step 3 is not a durable cleanup step: if it is cut off, the R17 reconciler branch (Task 17) resolves the command, because `rescan_on_startup` requeues every `OUTCOME_UNKNOWN` row. `FAILED_SAFE` releases nothing: the owner row becomes `FAILED_SAFE` in the terminal transaction and the sagas stay `CLOSE_OWNED` until the next owner takes them.~~ Now: the terminal transaction also releases the owner (`RELEASED`, or `FAILED_SAFE`); cleanup is only the idempotent saga step plus clearing `cleanup_pending`, then the root's waiting commands are handed to the reconciler. `FAILED_SAFE` sagas stay `CLOSE_OWNED` until the next owner takes them.
10. **Partial size at `start` and at the SELL intent.** R15 refuses `q ≥ |position|` at `start` with `QUANTITY_ABOVE_POSITION`. The SELL-intent path (Task 12) turns `requested == held` into a full close (`quantity=None`) before it calls `start`, because spec 5.1 says "a close takes the broker quantity"; only `requested > held` is `NOT_A_REDUCTION`. Both follow the spec.
11. **R14 refs are order entity ids; the protection generation is a counter.** The exact refs `CLOSE_OWNED` stores are the broker order entity ids the close cancels. `BrokerOrderEvent` gains `order_entity_id`, and the ingest fills it. The protection generation is a counter on the saga: every release starts a new one, and only order groups of the current generation can change protection.
12. **Under `CLOSE_OWNED` only a loss of protection is an incident.** `Inactive`/`Rejected`, or a cancel of a ref the close did not ask for, still takes today's incident path (R14). Other events (a late entry fill, a working status, a stop fill in the cancel race) are bookkeeping and keep `CLOSE_OWNED`. Running them through today's `_apply_event` would call a filled position with a cancelled stop `MISSING_PROTECTION`.
13. **Several sagas on one conid.** If a close owned more than one saga on the conid, `release_after_partial` gives the new legs to the first saga (by command id) and closes the others with `PROTECTION_MERGED`.
14. **`supersede` is not a separate entry point.** R12 lists `supersede`; here the supersede happens inside the account claim transaction (R6), so there is nothing extra to serialize.
15. ~~**R21 and SELL intents.** Running `execute_automated_intent` end to end needs a signed research bundle. Task 13 checks the SELL wiring on cold start and hot-arm (the built service holds the facade and the broker port). It drives the joined-command path end to end with `liquidate_account` through the same coordinator and reconciler. The SELL rules themselves are tested in Tasks 12 and 17.~~ Replaced (round 2): Task 13 enables paper automation on cold start, checks the built intent service, and drives a SELL intent end to end through the real coordinator, close, reduce-only path and reconciler; only the research-bundle verifier is a test double (a real signed bundle needs a research database, and the production evidence check refuses fixture provenance).
16. ~~**Startup recovery runs inside the loop.** `_maybe_start_liquidation_recovery` used to call `rescan()` before `trader.run()`. Orders need a running loop, so the first tick is now the first step of the recovery task. Session recovery keeps its "before readiness" order: `SessionController.restore()` loads the durable deadlines synchronously, and the first `run_due` runs on the worker inside the loop.~~ Replaced by ruling 36: master (PR #42) runs the first rescan and the session `recover` on the worker inside `run_until_complete`, before `trader.run()`.
17. **Join rows carry the conid.** A retried command is checked against its first request (account, conid, goal, quantity), so a joined command id cannot be re-used for another conid (#24).
18. **Intermediate behaviour between Tasks 5 and 6.** Task 5 refuses a partial request with `PARTIAL_CLOSE_UNAVAILABLE`; Task 6 replaces that line. No run is created for the refused request.
19. **Reduce orders are never cancelled (round 2).** Spec 5.1 step 4 says the flatten cancels every child it identified as working. A working reduce (this root's own, an inherited one, or a pre-SP1 one) only reduces; cancelling it would undo a reduction in flight. So no root cancels a reduce: it blocks every new reduce while it works (R23) and is reconciled like any child.
20. **`ABSENT` means "may have filled".** An absent child has no fill evidence, so it is fill-bearing: the next reduce, `CLOSED`, `DONE` or `FLAT` needs a generation newer than the observation that proved the absence.
21. **The send fence (R22).** A child's fence is `sent_generation`: the newest broker generation id, staging included, read right after the broker call returned or raised. A generation with a higher id opened after the send, so its complete enumeration would include the order. If a crash cut the send off before the fence was stored, the next tick sets it: every entry point runs on one worker, so no send is in flight when a tick starts. `observed_generation` uses the same "newest" read, so the fill-freshness margin and the absence margin are the same.
22. **The deadline decides on this tick's evidence (Task 6).** `_tick` reads the snapshot and the children's rows before it looks at the deadline, so "a child is UNKNOWN at the deadline" means unknown now, not in a stale journal row. When the broker cannot be read at the deadline, the journal's children decide.
23. **Re-protect recovery.** Spec 5.1: "one sibling present, the other missing: place only the missing one." The only missing sibling that may be placed is the `PLANNED` target, which was never sent. A sent leg that is absent, refused, rejected or cancelled is a re-protect failure and escalates (R30). The spec names `place_exit_oca`, which transmits both legs in one call; the plan sends one leg at a time on the one reduce-only path, because R13 sends the target only after the stop is reconciled.
24. **An OCA stop cancel is judged on the next generation.** A stop `Cancelled` while its target is working or filled is classified only on a generation newer than that observation, together with the target and the position, so the order of the two callbacks never turns a normal target exit into a failure (R26).
25. **Adopting runs open before the upgrade (R29, R2-5).** Migration 36 gives every old run a join row and the oldest open run of an account an `ACTIVE` account owner (others are `SUPERSEDED` by it). On its first tick an adopted run journals ~~, for every position the account holds, the reduce the old service may have sent (old ref `{root}-liquidation-reduce-{conid}`) as an `UNKNOWN` child~~ one wildcard `UNKNOWN` child per old run for every reduce the old service may have sent (ruling 42), fenced on the newest generation; nothing is invented as `NOT_SENT` or `ABSENT`. Task 2 classifies those old refs as `exit`, so no producer treats them as entries.
26. **A session that follows a `FAILED_SAFE` root goes `INCIDENT` at once (Task 11)** with a `LIQUIDATION_FAILED` signal, instead of polling a dead root until the flat deadline. R9 lets the operator start a new root, which inherits the unknown children.
27. **`exit` in `classify_cancel`.** Cancelling a close's reduce keeps exposure the close was removing, so it is `INCREASING` (it needs the ceremony), like a protective leg.
28. **Entry cancels of the session are not liquidation children.** A cancel is idempotent at the broker; a crash before `cancel_issued` is stored sends it again. They are not journaled as children (Task 16).
29. **`REDUCE_FAILED` trips no breaker.** The position is protected again and the owner is released; the command fails with an operator alert (Task 17). The breaker stays for unprotected or unknown states (spec 5.1 "breaker signals").
30. **The saga after a release models the remaining position.** `release_after_partial` sets the requested, filled and protected quantities to the remainder, because today's event rules (`_apply_event`) compare them with the leg fills. The entry's history stays in the domain event journal.


### Rulings made while aligning with master (PR #42)

PR #42 (`15f9e715`, "let liquidation exits skip entry gates and stop blocking the trader loop") merged after round 2. It already has `Trader.place_reduce_only_order`, loop-side `reduce_position` / `cancel` with on-loop and stopped-loop refusals, a timed `LiquidationService` lock with `LiquidationBusy`, one liquidation worker thread in `trader_service.py` (stuck-tick watchdog, `_BusyStreak`, startup rescan and session recover on the worker, shutdown during startup), `rescan` that skips `FAILED_SAFE`, and the "never on an RPC surface" guard (`NEVER_EXPOSED_METHODS`). The tasks below now extend that code. Where the plan and the merged code differed, the stricter one was kept. Rulings 31–41 are binding like the others.

31. **The worker serializes; master's lock stays as a second guard (Task 4).** R12 puts every entry point on one worker, so the lock is never contended in production. It is kept anyway for a caller that bypasses the worker (the drill, tests, a future producer): `start` commits its claim, join row and run in one transaction (R6) *before* it waits for the lock, and only `_tick` runs under it; `rescan` holds it for the whole pass. A caller that waits past `lock_timeout_seconds` gets `LiquidationBusy` and the root is not lost: the next `rescan` advances it. `liquidate` on `LiquidationBusy` records `OUTCOME_UNKNOWN` for the root, schedules the reconciler and re-raises (master's behaviour; R33 still holds: the service never resolves the command). `upgrade_to_zero` and the claims are single journal transactions and take no lock (`_tick` calls `upgrade_to_zero`; a nested lock would deadlock).
32. **Master's lock tests are adapted, not dropped (Task 4).** `test_start_and_rescan_serialize_across_threads`, the busy tests, `test_root_bound_to_one_account_rejects_another_account` and `test_rescan_returns_none_when_only_failed_safe_roots_remain` run on the journal. `test_rescan_skips_failed_safe_root_and_advances_a_busy_registered_root` is dropped (Task 4 `test_failed_safe_root_does_not_block_rescan_of_a_newer_root` plus the busy-start test cover it). Master's FLAT-resolves-the-command assertion is dropped (R33). The saga test `test_busy_liquidation_keeps_protective_failure_root_for_rescan` and the five real-loop tests in `tests/test_trader_service_loops.py` move to the journal store (their old `_ResumeStore` / in-memory `_runs` are gone).
33. **One pre-boundary error type (Task 14).** `DispatchRefused` (a `RuntimeError`) is raised for every refusal proven before the order leaves: `_dispatch_loop` (`TRADER_LOOP_UNAVAILABLE`, `ON_TRADER_LOOP`, with master's messages, so master's `pytest.raises(RuntimeError, match=...)` tests still pass), the size, side and account checks of `reduce_position` (were `ValueError`), and a `reduce-only refused:` result from the trader (was `BrokerRejectedError`). IB's own rejection after the send stays `BrokerRejectedError`: the order was sent, so the child stays `UNKNOWN` until its row proves `REJECTED` (R2). Three master assertions in `tests/test_order_dispatch_ports.py` change type accordingly.
34. **R13 is met by master's order tracker (Tasks 14, 10).** `_confirm_reduce_only` waits on `OrderLifecycleTracker.wait_decisive(order_id)`: the status of *this* order id, where `PendingSubmit` is not decisive. The plan's `_place_and_await_status`, `ack_timeout`, `_ACK_STATUSES` and the `EXIT_ORDER_REJECTED:` text are dropped, and so are their stream-script tests. The tracker's `timeout` verdict still returns success: the service never reads the return value as evidence (a sent child is `UNKNOWN` until its own broker row), so the difference is only a log line. The refusal text stays master's `reduce-only refused:` (now the constant `REDUCE_ONLY_REFUSED`).
35. **Master's signature and checks stay; the plan's are added (Tasks 14, 10).** `place_reduce_only_order(contract, side, quantity, *, broker_quantity, order_ref)` keeps master's account/mode pin, contract check, the cross-check of the caller's broker quantity against ib_async's live `positions()`, the pre-send exception-is-a-refusal rule and the IB verdict mapping. Task 14 adds the connection check and the R35 subtraction of reducing orders already working (`openTrades()`, the sibling of the same OCA group excluded). Task 10 adds `order_type`, `price` and `oca_group` to the same method (one reduce-only path). Duplicates of master's tests (daily-loss breach, wrong side, oversize, unpinned account, live cache, the RPC guard) are not added again.
36. **One worker, master's loops (Task 15).** Master's `trader_service` worker and loops are kept as they are (`_watched_ticks`, `_BusyStreak`, `stopping()`, startup rescan and `recover` on the worker inside `run_until_complete`). Task 15 only (a) makes that worker the one the command stack builds (`LiquidationWorker` is a `ThreadPoolExecutor` subclass, so `run_in_executor` takes it), so RPC threads, the ingest thread and the ticks share one thread (`_shared_liquidation_worker`); (b) wraps the service in `SerializedLiquidation`, whose `rescan` first starts a flatten for every unhandled protective failure (so master's recovery tick needs no change); (c) gives the saga `nonblocking()` (ruling 8). Ruling 16 is replaced: master already runs the first rescan and the session `recover` on the worker while the loop runs, so `SessionController.restore` is not added, and the "no liquidation service → session deadlines not running" branch is dropped (the session ticks no longer depend on the liquidation facade). Task 11's restart test uses `recover`.
37. **`cancel_on_loop` is a separate method (Task 14).** Master's `cancel` (coordinator path) keeps matching the open trade off the loop and raising `CancelUnresolved`; three master tests pin that. The liquidation uses `cancel_on_loop`: the perm id read stays on the worker, the match and `cancelOrder` run on the loop, and "no live order" is `DispatchRefused("CANCEL_UNRESOLVED")`.
38. **A busy session flatten polls its own cause (Task 11).** Master's `_issue_flatten` swallows `LiquidationBusy`. Only a root this call claimed waits for the lock (a join returns at once), so on `LiquidationBusy` the session persists and polls its own cause.
39. **N1: the entry write after `submit_bracket` reads again (Task 9).** R28 made every saga save revision-checked, but `start` built its post-dispatch write from the `SUBMITTING` copy it held before the call. An ingest event saved meanwhile made it raise `SagaRevisionConflict` with the bracket live. `_after_dispatch` re-reads under `_retrying`: the submitted ids are added on top of the ingest's state, and an error state is written only while the saga is still `SUBMITTING`.
40. **N2: every pre-upgrade run's old reduce is tracked (Task 4).** Migration 36 marks every old run ~~that did not end `FLAT`~~, `FLAT` included (ruling 49), `pre_sp1_open`. On every tick, before the deadline check, a root journals ~~`{run}-liquidation-reduce-{conid}` of each marked run of its account, for each position in its scope, as an `UNKNOWN` child it owns (unless that child already exists). An account root then clears the marks.~~ one wildcard `UNKNOWN` child for each marked run of its account that has none yet (ruling 42), whatever its scope. The mark is cleared only in the transaction that settles that run's wildcard child, never on journaling. So the adopted run, the runs it superseded and old `FAILED_SAFE` runs all block a new reduce until their old orders are settled, also when no run was open at the upgrade.
41. **Global constraint 21:** line numbers now cite master at `15f9e715`; where a task edits a file PR #42 changed, the task names functions, not lines.
42. **A pre-upgrade run's reduces are one wildcard child (Task 4, Grok round 3 on #20).** Master's `liquidation_runs` stores no conid list, so the conids an old run sent reduces for are unknowable. A position missing from the snapshot is not evidence: the old reduce of a position already at 0 may not be in `working_orders` yet, and its late fill opens the other side. So each marked run becomes ONE `UNKNOWN` child (`child_id` `{run}-liquidation-reduce-*`, `conid` NULL, `ref_prefix` `{run}-liquidation-reduce-`), owned by the root that journals it and fenced on the newest generation (after the upgrade). It matches every broker row whose decoded ref is the prefix plus digits (`find_orders_with_prefix`). While it is `UNKNOWN` or `WORKING` it blocks every reduce of every scope on the account, account and position-scoped roots alike, whoever owns it (`_children_in_force` in `_blocking`, `_observe_children` and `_on_deadline`). It settles only on positive evidence (R22): every matching row terminal AND `enumeration_complete()` on a generation newer than its fence, even when the rows seen are terminal (another conid's order may still be invisible). A visible working match keeps it `WORKING`: waited on, never cancelled. An invisible one blocks to the deadline, then the normal deadline path; nothing is invented as `NOT_SENT` or `ABSENT`. Settled, it is `ABSENT` (no row), `FILLED` (any fill, the sum recorded) or `CANCELLED`; `ABSENT` and `FILLED` are fill-bearing, so sizing waits for a newer generation (R5). Its run's `pre_sp1_open` is cleared in the same transaction. Migration 36 is not shipped, so `liquidation_children.conid` becomes nullable and gains `ref_prefix` there.
43. **A fill fence outlives its root (Task 4, Astra round 3 on #20, #23).** `_fresh` no longer looks only at the root's own children. `fill_watermark_in_tx` returns the newest `observed_generation` of any fill-bearing child (`ABSENT`, or `filled_quantity > filled_at_send`) on the scope: the whole account for an account root, the conid plus wildcard children for a conid root; any root, any state, so own, inherited, superseded, `FAILED_SAFE`, re-protect legs, cancelled stops that filled and pre-upgrade wildcard children all count, and it survives a restart (it is read from the journal). Every root needs a position generation strictly newer than it before it sizes or sends a reduce. Tests: a fill seen on the deadline tick fences the next root (Task 4); a settled legacy fill fences the next root across a restart (Task 4); an account takeover waits for the scoped root's fill (Task 7). A saga stop that fills while no close is open is not a liquidation child; the next close sees it only through the position snapshot.
44. **DONE is decided from one read of the leg rows (Task 6, Astra round 3 on #22).** `_advance_reprotect` reads each leg's row once and takes the OCA link, side, status and quantities from that read. A row whose status is no longer accepted, or whose filled or outstanding quantity differs from the child, is observed again (`_observe_children`) and the tick waits; it never finishes on the old child values. ~~Right before `_finish` the rows are read again and compared field by field (`_leg_fingerprint`, with `revision`); any change waits.~~ The second read now happens while broker changes are held, together with the terminal write (ruling 48). Tests: stop cancelled after observation, stop partly filled after observation, row changed while DONE was decided.
45. **An explicit empty OCA clears the link (Task 18, Astra round 3 on #45).** `OrderObservation.oca_reported` says the source carries OCA fields. When it does, `''` / `0` (normalised to `None`) overwrite the stored group and type; only a source without the fields keeps them. Tests through real `BrokerIngest` + DuckDB, with a readback through a new store.
46. **Working reduces count only for the pinned account (Task 14, Astra round 3 on #38).** `_working_reduce_quantity` skips orders of another account; an order without an account is counted, so an unknown owner fails closed. Also: the Task 5 and Task 6 replacements of `start` keep `with self._exclusive():` around `_tick` (ruling 31, Astra on #21, #22), and the Task 6 test helper `_leg_row` forwards `entity` (Astra on #23).


### Rulings made in review round 4

Round 4 (`ba3f70fe`, reviewer summary on #16) found one blocker and six majors. Rulings 47–54 are binding like the others; struck-through text above is what they replace.

47. **PendingCancel is live, never protection (Task 6, #22 blocker).** A broker row in `PendingCancel` is the child state `PENDING_CANCEL`: it blocks every new reduce like `WORKING` (`CHILD_LIVE`), it is re-read and inherited (`CHILD_OPEN`), and a cancel this root sent still covers its target. It is never healthy protection (`_BROKER_HEALTHY` = `Submitted`, `PreSubmitted`): the `PLANNED` target is sent only while the stop's own row is healthy right then; at DONE a `PendingCancel` row is a change (`_leg_changed`), so the leg is observed again and the tick waits. On a newer generation a `PENDING_CANCEL` stop escalates like a `CANCELLED` one (R26's wait still applies while its target is working or filled), and a position that is gone ends `CLOSED`. `_cancel_targets` does not cancel a leg that is already pending cancellation. `_legacy_evidence` keeps `PendingCancel` as `WORKING` (a wildcard child only blocks). Tests: Task 6 `test_a_pending_cancel_stop_never_gets_its_target_and_escalates`, `test_a_stop_that_goes_pending_cancel_while_done_is_decided_never_ends_done`.
48. **DONE commits while broker changes are held (Task 6, #22 major).** The leg read and the terminal write were two steps, so an ingest batch between them could cancel or fill a leg and DONE (with the owner release) committed on old evidence. A journal transaction alone cannot fix it: broker rows are written through the domain journal's own connection and locks, not `journal_db.transaction`. So `BrokerIngest.hold_changes()` takes the ingest's `_apply_lock` (now an `RLock`, because the holder's snapshot read checks readiness under the same lock) and refuses while a generation is staging: live batches apply under that lock, and a promote runs only while a generation is staging, so nothing writes broker rows while it is held. `_finish_held` re-reads the leg rows (fingerprint with `revision`), captures the snapshot again (same promoted generation, same position quantity) and commits `DONE` / `REDUCE_FAILED` with the owner release inside the hold; cleanup (saga release, breaker, scheduling) runs after it. A change, an unreadable broker or a hold that cannot be taken (`BrokerChangesBusy`, 2 s) waits for the next tick. Lock order stays `LiquidationService._lock` before `BrokerIngest._apply_lock`. A leg that changes after the commit (the hold is released before cleanup) reaches the saga through `release_after_partial` and its own events: a later `Cancelled` is the saga's protection-lost path. Tests: Task 6 `test_an_ingest_update_before_the_terminal_write_never_commits_done[leg row|position|generation|hold busy]`, `test_done_commits_while_broker_changes_are_held`; the ingest's own `test_holding_broker_changes_stops_an_ingest_batch_until_released`, `test_broker_changes_cannot_be_held_while_a_generation_is_staging`.
49. **Old FLAT runs are tracked too (Task 4, #20).** Master wrote `FLAT` from an empty promoted snapshot, which does not prove that an earlier reduce will not arrive late. Migration 36 marks every old run `pre_sp1_open`, `FLAT` included (`_OPEN_BEFORE_SP1` still decides owner adoption: a `FLAT` run never becomes an owner). Its wildcard child blocks every reduce on the account until ruling 42's positive evidence settles it. Cost: after the upgrade each account with old runs waits for one complete, newer enumeration before its first reduce. Tests: Task 4 `test_an_old_flat_runs_late_reduce_blocks_a_new_flatten`, Task 5 `test_an_old_flat_runs_late_reduce_blocks_a_scoped_close`.
50. **A terminal child's late fill raises the fill fence (Task 4, #20).** IB can report `Cancelled` with 0 filled and later a fill. Every tick (`_observe_late_fills`, right after `_observe_children`) re-reads every settled child (`FILLED`, `CANCELLED`, `REJECTED`) of the scope, any root, wildcard children included, and moves only its fill, only upwards, with `observed_generation` = the newest generation. The state stays terminal; an empty or ambiguous lookup changes nothing. The watermark of ruling 43 then makes every root wait for a newer position generation before it sizes a reduce. Cost: one row lookup per settled child of the scope per tick, with no age limit (a known open item). Test: Task 4 `test_a_cancelled_child_that_reports_a_late_fill_fences_the_next_root` (an external order cancelled by an account flatten: the same cancel-child path as a stop, used because Task 4 has no stop fixture yet).
51. **Exact identifiers at admission (Tasks 5 and 6, #21).** `start(scope="conid")` accepts only an exact positive integer conId (`numbers.Integral`, not a `bool`); `1.5`, `True`, `"1"`, `0` and `None` raise `LiquidationRefused("CONID_INVALID")` before any join row, owner, run, snapshot read or order. A partial quantity must be a finite real number (not a `bool` or a string), else `LiquidationRefused("PARTIAL_QUANTITY_INVALID")`. Tests: Task 5 `test_a_conid_that_is_not_an_exact_positive_integer_is_refused_before_any_claim`, Task 6 `test_a_partial_quantity_that_is_not_a_finite_number_is_refused_before_any_claim`.
52. **Malformed close input is refused before scheduling (Tasks 14 and 10, #38 and the round-3 minor).** `reduce_position`, `reduce_partial` and `place_exit_leg` build the contract, the broker quantity, the size and (for a leg) the price before `run_coroutine_threadsafe` (`_close_inputs`, `_finite_number`). A missing field, a conId that is not an exact positive integer, an empty symbol, or a size or price that is `None`, a `bool`, a string or not finite is `DispatchRefused("REDUCE_ONLY_REFUSED")`, so the child is `NOT_SENT`. Only what follows the scheduling call may be "maybe sent". Tests: Task 14 `test_a_malformed_position_is_refused_before_anything_is_scheduled`, `test_a_quantity_that_is_not_a_finite_number_is_refused_before_anything_is_scheduled`; Task 10 `test_a_malformed_exit_leg_is_refused_before_anything_is_scheduled`.
53. **Task 18's tests use master's dispatch only (#45).** Task 18 runs before Task 4, so the OCA-clear test reads the row through `TradingRuntimeOrderDispatch.find_by_order_ref(account, encode_order_ref(child_id))`; Task 14's `test_liquidation_dispatch_encodes_child_ids_and_reads_evidence` pins the adapter. Each task's Step 4 must be green before the next task starts.
54. **The deadline decides on the tick's evidence from Task 4 on (found while fixing #20).** Task 4's and Task 5's `_tick` now observe the children (and late fills) before they look at the deadline, as Task 6's already did (R31 note 22). Ruling 43's `test_a_fill_seen_on_the_deadline_tick_fences_the_next_root` and ruling 50's test need it in plan order; the code at the end of the plan is unchanged.


## Review round 2

Round 2 (2026-10-06) checked the round-1 rewrite ticket by ticket (verification on #16–#31 and a completeness critic) and took ten more comments from the external reviewers (`review-round2-comments.txt`: R2-1 … R2-5 and the Grok notes on #16, #20, #21, #22, #24). The owner decided D1–D17. They are binding as R22–R38 and win over every rule and ruling above; struck-through text above is what they replace. `round2-mapping.md` lists every finding with the task and test that fix it.

**Evidence and children**

- **R22 (D1). Absence needs a complete, fenced enumeration.** An unseen child stays `UNKNOWN` until a complete broker enumeration (`enumeration_complete()`, the `_reconcile_approve` standard) on a generation newer than the child's send fence does not show it; otherwise the deadline ends `FAILED_SAFE` with no new order. The fence is the newest broker generation id, staging included, read right after the broker call (ruling 21). Ruling 2 ("ABSENT at fence + 2") is gone, and so is the round-1 test that sent a second reduce for a never-seen child. `enumeration_complete()` was broken on master (it called the `is_ready` property); Task 18 fixes it with a red test.
- **R23 (D2). Working children block; a terminal row counts at once.** Any `WORKING` child, an own or inherited re-protect leg included, blocks a reduce. A terminal status on the child's own row counts on any generation. The cancel targets are read together with the child evidence: a working leg that appeared after the snapshot is cancelled (found by its own row) and blocks; a leg that filled since the snapshot is a fill that needs a newer generation. A root never cancels a reduce order (ruling 19).
- **R34 (D13). Every send is logged; only a proven pre-send failure is `NOT_SENT`.** `_send` logs every exception. A failure proven before the order leaves (the trader refused, IB not connected, no live position, no running trader loop, a call on the trader loop itself, an unresolved cancel) is `NOT_SENT`; anything after `placeOrder` may have run is `UNKNOWN`.

**Ownership and outcomes**

- **R24 (D3). The owner is released in the terminal transaction.** `_finish` writes the terminal state, `cleanup_pending` and the owner's end state together, so no claim can join or upgrade a finished root.
- **R25 (D4). A partial reduce that sold nothing is not `DONE`.** It still re-protects the untouched position, then ends `REDUCE_FAILED`: terminal, owner released, no breaker trip, and the command fails with an operator alert (never a success).
- **R26 (D5). A normal exit is not a re-protect failure.** The fresh position is checked before a leg's state: a target fill that cancels its OCA stop ends `CLOSED`.
- **R29 (D8). The upgrade.** Migration 36 adopts runs open before the upgrade (join rows, one `ACTIVE` owner per account, the others `SUPERSEDED`), and an adopted run treats the reduce the old service may have sent as an `UNKNOWN` child (ruling 25). The worker's tick catches errors per saga. A saga that was `SAFETY_FAILED` before the upgrade starts no flatten on the first deploy without a fresh trigger.
- **R31 (D10). A missed deadline in a partial close.** With protection already cancelled (phase `cancel`, `reduce` or `reprotect`), the partial close escalates once to a full close of the live remainder with a bounded extension, unless a child is `UNKNOWN`: then `FAILED_SAFE` and no order.
- **R33 (D12). One resolver per command.** The reconciler alone resolves close commands; the liquidation service only schedules them, and leaves a `SUBMITTING` command to its producer. A root that ended `FAILED_SAFE` (or `REDUCE_FAILED`, or `DONE` for a full request) rejects the command with an operator alert, so reconciliation is never blocked for ever.
- **R36 (D15). Retries before admission.** A partial request finds its join row (its durable root) before its quantity is admitted, and learns `ExitInProgress` before a snapshot is read.

**Protection**

- **R27 (D6). Hand over before every cancel batch.** The close hands the targets to the saga before each batch, not only on the first tick; the saga merges them.
- **R28 (D7). One writer discipline for saga rows.** Every saga write is a revision-checked update; a conflict rolls the whole transaction back and the writer retries from a fresh read inside the saga. Replacement legs are bound to the saga before they are sent (pending groups), and the release reads the legs' broker status.
- **R30 (D9). No retry of a re-protect leg.** Spec 5.1: "no retry with fresh ids. Escalate." A refused, rejected, cancelled or lost leg escalates to a full close; only the never-sent `PLANNED` target is sent later.

**Broker boundary and producers**

- **R32 (D11). Exits pass the pause.** The SELL close runs before the check of the pause on new exposure, keeping the account fence, the claim and idempotency. A SELL when the close path is not wired is refused (`CLOSE_PATH_UNAVAILABLE`), never sent as a bracket.
- **R35 (D14). The reduce-only bound.** It reads IB's live `positions()` only (refused when that fails, never the portfolio cache) and subtracts the reducing orders already working on the contract, except the sibling in the same OCA group.
- **R37 (D16). The session cancel phase.** It cancels our working entries, a partly filled one's rest included, so nothing fills after the cutoff; it checks `is_external` and skips a close's children. Protection bigger than the position after that cancel closes the conid through the scoped close.

**Tests and hygiene**

- **R38 (D17).** Everything else from the verification: crash tests that inject the failure inside a transaction (account claim, upgrade, `upgrade_to_zero`), between journal and send, after the send, for a cancel and for the target promotion; the DONE checks from the broker row; a retired-leg test with its own event id; cold-start automation in Task 13 and a broker sync that does something; `xfail(raises=AssertionError)`; the third `classify_leg` call site; `classify_cancel` and the `exit` leg; no DuckDB read on the IB loop in `cancel_on_loop`; the session loop tested through `trader_service`; content anchors instead of line numbers where earlier tasks edit the file; the cancel livelock (#20).


---

## File Structure

| File | Task(s) | Responsibility |
|---|---|---|
| `trader/trading/order_correlation.py` (modify) | 2, 18 | `liquidation_child_id`, `reprotect_oca_group`, `liquidation_child_kind`; `classify_leg` learns re-protect and reduce children (three call sites); `OrderObservation` carries the OCA group and type. |
| `trader/trading/broker_ingest.py` (modify) | 2, 18, 9 | `_order_leg` (liquidation children re-classified); copy the OCA fields; pass `order_entity_id` to the saga event. |
| `trader/data/broker_state.py` (modify) | 18 | `BrokerOrderRow.oca_group/oca_type`, migration 38, `newest_generation_in_tx`. |
| `trader/trading/command_ports.py` (modify) | 18 | `ingest_ready` (the readiness property). |
| `trader/data/schema_migrations.py` (modify) | 3 | docstring registers 35–38. |
| `trader/trading/exit_owner.py` (create) | 3 | `ExitOwnerRegistry` with `*_in_tx` claims, migration 35. |
| `trader/trading/liquidation_service.py` (modify) | 4, 5, 6, 7, 8 | migration 36 (with the `pre_sp1_open` mark); master's timed lock kept as a second guard; frozen data model (`ChildRef`, `LiquidationReceipt`, `JoinRow`, `CloseResolution`, ports, errors); `LiquidationRunStore`; write-ahead children, evidence, account and conid scopes, partial close, re-protect, escalation, takeover, claims, cleanup. |
| `trader/trading/liquidation_worker.py` (create) | 15 | `LiquidationWorker`, `SerializedLiquidation` (R12). |
| `trader/trading/trading_runtime.py` (modify) | 18, 14, 10 | `TradingRuntimeOrderDispatch.enumeration_complete` / `newest_generation`; master's `Trader.place_reduce_only_order` gets the connection check, the R35 bound and the exit legs; `TradingRuntimeOrderDispatch` refuses with `DispatchRefused`, gains `reduce_partial` / `place_exit_leg` / `cancel_on_loop`. |
| `trader/trading/command_stack.py` (modify) | 18, 4, 14, 10, 15, 11, 13 | `_LiquidationDispatch` (evidence + reduce-only methods); `_BrokerGenerationRefresh`; composition of registry, store, worker facade, saga, session adapters, intent service, reconciler. |
| `trader/trader_service.py` (modify) | 15 | master's loops use the liquidation worker the command stack built (`_shared_liquidation_worker`). |
| `trader/automation/protective_order_saga.py` (modify) | 9 | `CLOSE_OWNED`, exact expected cancels, protection generations, pending replacement legs, revision-checked writes, hand-over / release / close, migration 37. |
| `trader/automation/session_controller.py` (modify) | 11, 16 | time exit = scoped close; persist and poll the returned flatten root (a busy start polls its own cause; `FAILED_SAFE` → `INCIDENT`); cancel our entries only; close oversized protection. |
| `trader/automation/automated_intent_command.py` (modify) | 12 | SELL intents become a proven-reduction close, outside the pause on new exposure. |
| `trader/trading/command_coordinator.py` (modify) | 2, 17 | `classify_cancel` docstring (`exit`); `OutcomeReconciler` resolves or rejects close commands from their exact root. |
| `web/command_center/routes_commands.py`, `web/static/command_center*.js` (modify) | 2 | the `exit` leg value and chip. |
| `scripts/command_plane_drill.py` (modify) | 4 | the liquidation drill uses the journal store and broker evidence. |
| `tests/test_order_correlation.py`, `tests/automation/test_attribution_ledger.py`, `tests/test_cancel_command.py` (modify) | 2, 9 | child ids, leg classification, ingest forwarding, the `exit` cancel. |
| `tests/test_close_broker_evidence.py` (create) | 18 | readiness property, newest generation, OCA fields. |
| `tests/test_order_dispatch_ports.py`, `tests/test_trading_runtime.py` (modify) | 14 | master's reduce-only tests: refusals are `DispatchRefused`; the trader fake has `isConnected`/`openTrades`. |
| `tests/test_trader_service_loops.py` (modify) | 4, 14 | master's real-loop tests on the journal store; a refused reduce is `NOT_SENT`. |
| `tests/test_exit_owner.py` (create) | 3 | registry rules. |
| `tests/test_liquidation_service.py` (rewrite) | 4–8 | state machine on a real DuckDB journal with fake broker generations, crash injection, a pre-SP1 journal; master's lock tests adapted. |
| `tests/test_reduce_only_order_path.py` (create) | 14, 10 | the reduce-only boundary and exit legs by order identity. |
| `tests/test_liquidation_worker.py` (create) | 15 | the shared worker, real asyncio loop, ingest-lock case. |
| `tests/automation/test_protective_order_saga.py` (modify) | 4, 9, 16 | master's busy-lock test on the journal store (4); hand-over, owned events, retired and pending legs, barrier-controlled races, the entry write after `submit_bracket`, saga rows from before the upgrade, partly filled entry. |
| `tests/automation/test_session_controller.py` (modify) | 1, 11, 16 | the time-exit regression, exact-root polling, cancel our entries only, oversized protection. |
| `tests/automation/test_automated_command_boundary.py` (modify) | 12 | SELL → close. |
| `tests/test_close_reconciliation.py` (create) | 17 | joined-command resolution. |
| `tests/test_safe_close_integration.py` (create) | 13 | the real `build_command_stack` on a real loop, fake broker only. |

---

### Task 1: Pin the latent time-exit bug with a failing test

The spec requires a failing test before the fix. Per R18 the test uses **today's** constructor (`SessionTimeExitAdapter(dispatch)`, `trader/automation/session_controller.py:286`) and a fake broker that keeps the working protective stop. It asserts the behaviour: after the position is closed, no stop is still working. Today the adapter calls `reduce` only, so the stop stays live and the assertion fails. It is committed as `xfail(strict=True, raises=AssertionError)`, so the suite stays green and only the behavioural failure counts as the expected one (a `TypeError` or a fixture error from a later task fails the suite); Task 11 rewrites the same test for the new constructor and removes the marker.

**Files:**
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Consumes: `SessionTimeExitAdapter(dispatch)` and `request_exit(*, command_id, conid, quantity, side)` as they are today.
- Produces: `_SimBroker` (test helper): one long position of 10 and its working stop `og-1:stop`; every `capture` is a newer, complete generation; it also answers `cancel`, `reduce`, `find_orders`, `get_order`, `enumeration_complete`, `newest_generation`, so Task 11 can run a real `LiquidationService` on it.

- [ ] **Step 1: Write the failing test**

Append to `tests/automation/test_session_controller.py`:

```python
# ---------------------------------------------------------------------------
# SP1 plan 1: time exits close through the scoped liquidation (Tasks 1, 11)
# ---------------------------------------------------------------------------

class _SimBroker:
    """A broker with one long position and its working protective stop.

    It is the snapshot port and the dispatch port at once; every capture is a
    newer, complete broker generation.
    """
    def __init__(self, quantity: float = 10.0):
        self.generation = 0
        self.quantity = quantity
        self.stop_working = True
        self.rows: dict[str, list] = {}
        self.calls: list[tuple] = []

    def _stop_row(self, status: str = "Submitted") -> BrokerOrderRow:
        return BrokerOrderRow(
            order_entity_id="og-1:stop", account_id=ACCOUNT, conid=CONID, symbol="AAPL",
            order_group_id="og-1", leg="stop", is_external=False, action="SELL", order_type="STP",
            total_quantity=10.0, filled_quantity=0.0, avg_fill_price=None, limit_price=None,
            stop_price=150.0, tif="DAY", status=status, deleted=False, revision=1,
            source_timestamp=_utc(11, 0),
        )

    def capture(self, account_id):
        self.generation += 1
        positions = [_position(self.quantity)] if self.quantity else []
        working = [self._stop_row()] if self.stop_working else []
        return _snapshot(self.generation, positions, working)

    def cancel(self, order, child_id):
        self.calls.append(("cancel", order.order_entity_id))
        self.stop_working = False

    def reduce(self, position, side, quantity, child_id):
        self.calls.append(("reduce", side, float(quantity)))
        self.quantity -= float(quantity) if side == "SELL" else -float(quantity)
        self.rows[child_id] = [SimpleNamespace(status="Filled", filled_quantity=float(quantity),
                                               total_quantity=float(quantity))]

    def find_orders(self, account_id, child_id):
        return self.rows.get(child_id, [])

    def get_order(self, order_entity_id):
        return None if self.stop_working else self._stop_row("Cancelled")

    def enumeration_complete(self):
        return True

    def newest_generation(self):
        return self.generation


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="the time exit reduces without cancelling the stop; fixed in plan 1 task 11")
def test_time_exit_leaves_no_live_stop_after_the_position_is_closed():
    """Spec 5.1: a time exit must cancel the protective stop first. Today it only reduces,
    so a live stop is left on a closed position and can open a short."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    broker = _SimBroker()
    adapter = SessionTimeExitAdapter(broker)                  # today's constructor
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    assert broker.quantity == 0.0
    assert broker.stop_working is False, "a live stop on a closed position can open a short"
```

- [ ] **Step 2: Run it to verify it fails for the right reason**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py -q --timeout=30 -k time_exit_leaves_no_live_stop`
Expected: `1 xfailed`. To confirm the cause, run it once more with `--runxfail`: it must fail with `AssertionError: a live stop on a closed position can open a short` (the reduce ran, the stop stayed working). Because the marker has `raises=AssertionError`, any other exception (a `TypeError`, a fixture error) is reported as a failure, not as xfailed.

- [ ] **Step 3: Commit**

```bash
git add tests/automation/test_session_controller.py
git commit -m "test: pin time exit leaving the protective stop live

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 2: Child ids, and `classify_leg` understands re-protect and reduce children

Liquidation children have no parent order, so `classify_leg` (`def classify_leg` in `trader/trading/order_correlation.py`) calls them `"entry"`. The ingest would then feed the saga an "entry" event for a replacement stop, and the session cancel phase (Task 16) would cancel a working reduce as an "entry". Every child id is `{root}-{kind}-{conid}-{attempt}` (R1, ruling 6) and becomes the order ref, so the decoded group names the kind.

There are **three** call sites, and all three get the group: the order row's `leg` in `BrokerIngest._apply_order`, the saga event in `_notify_protective_saga`, and the entity id that `OrderCorrelator.resolve_in_tx` mints on first sight (`order_group_id:leg`, `trader/domain/identity.py`). Without the third, a replacement stop would be stored as `...:entry` with leg `stop`.

Two upgrade cases (review round 2):
- Reduce orders of the liquidation service before SP1 have refs `{root}-liquidation-reduce-{conid}`. They are exits too.
- The ingest keeps the first leg of a row ("sticky"). A row stored before this task may say `entry` for a liquidation reduce. For liquidation children the group names the leg, so the ingest classifies them again on every update.

`classify_cancel` (`trader/trading/command_coordinator.py`) gets one new value, `exit`. Cancelling a close's reduce keeps exposure that the close was removing, so it stays `INCREASING` (it needs the ceremony); the docstring and the web docs now list `exit`, and the order chip shows `EXIT`.

**Files:**
- Modify: `trader/trading/order_correlation.py` (`classify_leg`, new helpers, `OrderCorrelator.resolve_in_tx`)
- Modify: `trader/trading/broker_ingest.py` (`_order_leg`; `_apply_order` and `_notify_protective_saga` pass the order group)
- Modify: `trader/trading/command_coordinator.py` (`classify_cancel` docstring), `web/command_center/routes_commands.py` and `web/static/command_center_commands.js` (leg value lists in comments), `web/static/command_center.js` (`kindOf`: `exit` chip)
- Test: `tests/test_order_correlation.py`, `tests/automation/test_attribution_ledger.py`, `tests/test_cancel_command.py`

**Interfaces:**
- Produces:

```python
LIQUIDATION_CHILD_KINDS = ("cancel", "reduce", "reprotect-stop", "reprotect-target")
def liquidation_child_id(root_id: str, kind: str, conid: int, attempt: int) -> str   # ValueError on bad input
def reprotect_oca_group(root_id: str, conid: int, attempt: int) -> str              # "{root}-reprotect-{conid}-{attempt}"
def liquidation_child_kind(order_group_id: Optional[str]) -> Optional[str]         # also "reduce" for pre-SP1 refs
def classify_leg(order_type: str, parent_id: int, client_order_id: int,
                 order_group_id: Optional[str] = None) -> str
    # reprotect-stop -> "stop", reprotect-target -> "take_profit", reduce -> "exit"; else unchanged
```

- [ ] **Step 1: Write the failing tests**

In `tests/test_order_correlation.py` replace the import line `from trader.trading.order_correlation import classify_leg, decode_order_ref, encode_order_ref` with:

```python
from trader.trading.order_correlation import (
    classify_leg, decode_order_ref, encode_order_ref, liquidation_child_id, liquidation_child_kind,
    reprotect_oca_group,
)
```

and append:

```python
def test_liquidation_children_classify_by_group_not_parent():
    assert classify_leg("STP", 0, 7, "p-1-reprotect-stop-265598-1") == "stop"
    assert classify_leg("LMT", 0, 8, "p-1-reprotect-target-265598-1") == "take_profit"
    assert classify_leg("MKT", 0, 9, "p-1-reduce-265598-2") == "exit"


def test_a_pre_sp1_liquidation_reduce_ref_is_an_exit():
    """Refs written before SP1 ({root}-liquidation-reduce-{conid}) are exits, not entries."""
    assert liquidation_child_kind("flat-1-liquidation-reduce-265598") == "reduce"
    assert classify_leg("MKT", 0, 9, "flat-1-liquidation-reduce-265598") == "exit"


def test_other_groups_keep_the_parent_rule():
    assert classify_leg("STP", 0, 7, "og-cmd1") == "entry"
    assert classify_leg("STP", 5, 7, "og-cmd1") == "stop"
    assert classify_leg("LMT", 5, 7) == "take_profit"


def test_liquidation_child_ids_are_deterministic_and_colon_free():
    assert liquidation_child_id("root-1", "reprotect-stop", 265598, 1) == "root-1-reprotect-stop-265598-1"
    assert reprotect_oca_group("root-1", 265598, 2) == "root-1-reprotect-265598-2"
    assert liquidation_child_kind("root-1-reduce-265598-3") == "reduce"
    assert liquidation_child_kind("og-root-1") is None
    for bad in (("root:1", "reduce", 1, 1), ("root-1", "entry", 1, 1), ("root-1", "reduce", 1, 0)):
        with pytest.raises(ValueError):
            liquidation_child_id(*bad)
```

In `tests/automation/test_attribution_ledger.py` replace `test_broker_ingest_forwards_order_events_to_protective_saga` with the helper below, the same test on the helper, and two new tests (the helper is reused in Task 9):

```python
def _forwarded_events(tmp_path, *, order_ref, order_type, name, action="SELL", parent_id=0,
                      observations=1):
    """Apply order observations through the real ingest; return saga events and stored rows."""
    from trader.automation.attribution import AttributionLedger
    from trader.data.attribution_store import apply_attribution_migrations
    from trader.trading.broker_ingest import BrokerIngest
    from trader.trading.order_correlation import OrderObservation

    db, migrator, journal = _db(tmp_path, name)
    apply_attribution_migrations(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    seen = []

    class FakeSaga:
        def on_broker_event(self, event):
            seen.append(event)
            return SimpleNamespace(state="PROTECTED")

    ingest = BrokerIngest(
        db=db, journal=journal, store=store, account_id=ACCOUNT, account_mode="paper",
        attribution_ledger=AttributionLedger(journal=journal, db=db, account_id=ACCOUNT, now=lambda: NOW),
        protective_order_saga=FakeSaga(),
    )
    conn = journal.connect()
    for index in range(observations):
        obs = OrderObservation(
            account_id=ACCOUNT, perm_id=77, client_order_id=7, parent_id=parent_id, conid=CONID,
            symbol="AAPL", action=action, order_type=order_type, total_quantity=6.0,
            filled_quantity=0.0, avg_fill_price=None, limit_price=None, stop_price=95.0, tif="DAY",
            status="Submitted" if index == 0 else "PreSubmitted", order_ref=order_ref,
            source_timestamp=NOW + dt.timedelta(seconds=index),
        )
        ingest._apply_record(conn, obs, lambda mutation, write: journal.mutate(conn, mutation, write))
    return seen, store.select_active_orders_in_tx(journal.connect())


def test_broker_ingest_forwards_order_events_to_protective_saga(tmp_path):
    seen, _rows = _forwarded_events(tmp_path, order_ref=encode_order_ref(ORDER_GROUP), order_type="LMT",
                                    action="BUY", name="saga-wire.duckdb")
    assert len(seen) == 1
    assert seen[0].order_group_id == ORDER_GROUP
    assert seen[0].leg == "entry"
    assert seen[0].status == "Submitted"


def test_broker_ingest_classifies_a_reprotect_stop_leg_without_a_parent(tmp_path):
    """SP1 plan 1 Task 2: a replacement stop has no parent but is a stop, not an entry."""
    seen, rows = _forwarded_events(tmp_path, order_ref=encode_order_ref("p-1-reprotect-stop-265598-1"),
                                   order_type="STP", name="reprotect-leg.duckdb")
    assert [(e.order_group_id, e.leg) for e in seen] == [("p-1-reprotect-stop-265598-1", "stop")]
    # The correlator mints the entity id from the same leg (order_group_id:leg).
    assert [(r.order_entity_id, r.leg) for r in rows] == [("p-1-reprotect-stop-265598-1:stop", "stop")]


def test_broker_ingest_reclassifies_a_pre_sp1_reduce_stored_as_an_entry(tmp_path):
    """A reduce row written before SP1 has the sticky leg ``entry``; its next update fixes it."""
    from trader.data.broker_state import BrokerOrderRow

    name = "legacy-reduce.duckdb"
    ref = encode_order_ref("flat-1-liquidation-reduce-265598")
    db, migrator, journal = _db(tmp_path, name)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    legacy = BrokerOrderRow(
        order_entity_id="flat-1-liquidation-reduce-265598:entry", account_id=ACCOUNT, conid=CONID,
        symbol="AAPL", order_group_id="flat-1-liquidation-reduce-265598", leg="entry", is_external=False,
        action="SELL", order_type="MKT", total_quantity=6.0, filled_quantity=0.0, avg_fill_price=None,
        limit_price=None, stop_price=None, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW)
    db.transaction(lambda conn: (store.upsert_order_in_tx(conn, legacy),
                                 store.bind_alias_in_tx(conn, "perm_id", "77", ACCOUNT, "",
                                                        legacy.order_entity_id, NOW)))
    _seen, rows = _forwarded_events(tmp_path, order_ref=ref, order_type="MKT", name=name)
    assert [(r.order_entity_id, r.leg) for r in rows] == [("flat-1-liquidation-reduce-265598:entry", "exit")]
```

Append to `tests/test_cancel_command.py` (after `test_classify_cancel_of_missing_order_is_increasing`):

```python
def test_cancelling_a_liquidation_exit_needs_the_ceremony():
    """SP1 plan 1 Task 2: a reduce of a close is an ``exit``; cancelling it keeps exposure."""
    assert classify_cancel(_order("ord-x", leg="exit", status="Submitted")) is RiskDirection.INCREASING
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_order_correlation.py -q --timeout=30`
Expected: FAIL at collection — `ImportError: cannot import name 'liquidation_child_id'`.

Run: `.venv/bin/python -m pytest tests/automation/test_attribution_ledger.py tests/test_cancel_command.py -q --timeout=30`
Expected: 2 failed — `test_broker_ingest_classifies_a_reprotect_stop_leg_without_a_parent` (leg `entry`, entity `...:entry`) and `test_broker_ingest_reclassifies_a_pre_sp1_reduce_stored_as_an_entry` (leg stays `entry`). `test_cancelling_a_liquidation_exit_needs_the_ceremony` already passes: it pins the contract the docstring now states.

- [ ] **Step 3: Implement**

In `trader/trading/order_correlation.py` add `import re` after `import datetime as dt`, and replace `classify_leg` with:

```python
LIQUIDATION_CHILD_KINDS = ("cancel", "reduce", "reprotect-stop", "reprotect-target")
_LIQUIDATION_CHILD = re.compile(r"-(cancel|reduce|reprotect-stop|reprotect-target)-(\d+)-(\d+)$")
# Order refs of the liquidation service before SP1: {root}-liquidation-reduce-{conid}.
_LEGACY_REDUCE = re.compile(r"-liquidation-reduce-(\d+)$")


def liquidation_child_id(root_id: str, kind: str, conid: int, attempt: int) -> str:
    """Deterministic, colon-free id of one liquidation child order.

    It is also the decoded order ref (``mmr:<child id>``) of a reduce or
    re-protect order, so the broker rows of that order can be found by it.
    """
    if kind not in LIQUIDATION_CHILD_KINDS:
        raise ValueError(f"unknown liquidation child kind {kind!r}")
    if not root_id or ":" in root_id:
        raise ValueError("root id must be non-empty and may not contain ':'")
    if int(attempt) < 1:
        raise ValueError("attempt starts at 1")
    return f"{root_id}-{kind}-{int(conid)}-{int(attempt)}"


def reprotect_oca_group(root_id: str, conid: int, attempt: int) -> str:
    """OCA group shared by the stop and target of one re-protect pair."""
    return f"{root_id}-reprotect-{int(conid)}-{int(attempt)}"


def liquidation_child_kind(order_group_id: Optional[str]) -> Optional[str]:
    group = order_group_id or ""
    match = _LIQUIDATION_CHILD.search(group)
    if match:
        return match.group(1)
    return "reduce" if _LEGACY_REDUCE.search(group) else None


def classify_leg(
    order_type: str, parent_id: int, client_order_id: int,
    order_group_id: Optional[str] = None,
) -> str:
    # Liquidation children have no parent; their order group names the leg.
    kind = liquidation_child_kind(order_group_id)
    if kind == "reprotect-stop":
        return "stop"
    if kind == "reprotect-target":
        return "take_profit"
    if kind == "reduce":
        return "exit"
    if not parent_id:
        return "entry"
    if order_type in _STOP_TYPES:
        return "stop"
    if order_type == "LMT":
        return "take_profit"
    return f"child-{client_order_id}"
```

In `OrderCorrelator.resolve_in_tx` pass the group when it mints the entity id:

```python
        group_id = decode_order_ref(obs.order_ref)
        if group_id is not None:
            return order_group_leg_entity_id(
                group_id, classify_leg(obs.order_type, obs.parent_id, obs.client_order_id, group_id)
            )
```

In `trader/trading/broker_ingest.py` import `liquidation_child_kind` from `trader.trading.order_correlation`, add before `class AccountValueObservation`:

```python
def _order_leg(obs: Any, group_id: Optional[str], current: Any) -> Optional[str]:
    """The leg of an order row. The first classification sticks, except for
    liquidation children: their group names the leg, so an older row (for
    example a pre-SP1 reduce stored as ``entry``) is classified again."""
    if group_id and liquidation_child_kind(group_id) is not None:
        return classify_leg(obs.order_type, obs.parent_id, obs.client_order_id, group_id)
    if current is not None and current.leg:
        return current.leg
    if group_id:
        return classify_leg(obs.order_type, obs.parent_id, obs.client_order_id, group_id)
    return None
```

In `_apply_order` replace the whole `leg=(current.leg if current and current.leg else (...))` argument with `leg=_order_leg(obs, group_id, current),`. In `_notify_protective_saga` pass the group:

```python
        leg = order.leg or classify_leg(
            obs.order_type, obs.parent_id, obs.client_order_id, order.order_group_id,
        )
```

In `classify_cancel` (`trader/trading/command_coordinator.py`) replace the docstring paragraph that lists the `leg` values with:

```python
    [Task 6 addendum §2]: ``order_correlation.classify_leg`` is the ONLY
    producer of ``leg``; its values are ``"entry"`` (no parent), ``"stop"``,
    ``"take_profit"``, ``"exit"`` (a liquidation reduce),
    ``f"child-{client_order_id}"``, or ``None`` (external / no group).
    Cancelling an entry removes PENDING exposure (REDUCING). Cancelling a
    protective leg strips protection from an already-open position, and
    cancelling an exit keeps exposure the close was removing (both
    INCREASING). A ``None`` row, a ``None`` leg, or any non-entry leg is
    treated as protective -- fail safe toward requiring the ceremony, never
    toward a silent unprotected cancel.
```

In `web/command_center/routes_commands.py` and `web/static/command_center_commands.js` add `"exit"` to the listed leg values. In `web/static/command_center.js` `kindOf`, after the `entry` line, add `if (l === 'exit') return ['EXIT', 'prot'];`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_order_correlation.py tests/automation/test_attribution_ledger.py tests/test_cancel_command.py tests/test_broker_ingest.py -q --timeout=30`
Expected: all PASS (the old `test_classify_leg` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/order_correlation.py trader/trading/broker_ingest.py trader/trading/command_coordinator.py web/command_center/routes_commands.py web/static/command_center_commands.js web/static/command_center.js tests/test_order_correlation.py tests/automation/test_attribution_ledger.py tests/test_cancel_command.py
git commit -m "feat: deterministic liquidation child ids and leg classification

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 3: `ExitOwnerRegistry` — one owner per position, one flatten per account

**Files:**
- Create: `trader/trading/exit_owner.py`
- Modify: `trader/data/schema_migrations.py` (module docstring: register 35–38 in the P3 range)
- Test: `tests/test_exit_owner.py`

**Interfaces (frozen for the rest of the plan):**

```python
EXIT_OWNER_MIGRATION_VERSION = 35
def apply_exit_owner_migration(migrator: SchemaMigrator) -> bool
KIND_SCOPED = "scoped_close"; KIND_ACCOUNT = "account_flatten"
GOAL_ZERO = "zero"; GOAL_PARTIAL = "partial"
STATE_ACTIVE / STATE_SUPERSEDED / STATE_RELEASED / STATE_FAILED_SAFE
CLAIMED / JOINED / UPGRADED / JOINED_FLATTEN            # ExitClaim.outcome values

class ExitInProgress(RuntimeError)                       # .code == "EXIT_IN_PROGRESS", .root_id
@dataclass(frozen=True) class ExitOwnerRow(root_id, account_id, conid, kind, goal, goal_quantity, state)
@dataclass(frozen=True) class ExitClaim(root_id, outcome, superseded: tuple[str, ...] = ())

class ExitOwnerRegistry:
    def __init__(self, db)                               # a DuckDBConnection (trader.journal_db)
    # R6: called inside the caller's db.transaction(fn); conn.execute only
    def get_in_tx(self, conn, root_id) -> Optional[ExitOwnerRow]
    def owner_for_in_tx(self, conn, account_id, conid) -> Optional[ExitOwnerRow]
    def account_owner_in_tx(self, conn, account_id) -> Optional[ExitOwnerRow]
    def claim_scoped_in_tx(self, conn, *, account_id, conid, root_id, goal_quantity, now) -> ExitClaim
    def claim_account_in_tx(self, conn, *, account_id, root_id, now) -> ExitClaim
    def upgrade_goal_in_tx(self, conn, root_id, now) -> None          # partial -> zero, ACTIVE rows only
    def finish_in_tx(self, conn, root_id, state, now) -> None         # ACTIVE -> RELEASED | FAILED_SAFE
    def ensure_partial_allowed_in_tx(self, conn, account_id, conid) -> None   # ExitInProgress if any owner is active
    # one transaction each (tests, simple readers)
    def get / owner_for / account_owner / claim_scoped / claim_account / release(root_id, now)
```

Rules (spec 5.1 "One execution owner per position"):
1. `claim_scoped_in_tx` checks the **account owner first**. If one is `ACTIVE`: a full request returns `JOINED_FLATTEN` with the flatten root; a partial request raises `ExitInProgress`.
2. Else, with an `ACTIVE` scoped owner on `(account, conid)`: the same root id → `JOINED`; a partial request → `ExitInProgress`; a full request against a `partial` owner upgrades it to `zero` in the caller's transaction → `UPGRADED`; a full request against a `zero` owner → `JOINED`.
3. Else insert `ACTIVE` → `CLAIMED`. A root id that was ever used raises `ValueError`.
4. `claim_account_in_tx`: another `ACTIVE` account owner → `JOINED_FLATTEN`; the same root → `CLAIMED`; else every `ACTIVE` scoped owner of the account becomes `SUPERSEDED` in the same transaction and their ids come back in `superseded`.
5. `SUPERSEDED`, `RELEASED` and `FAILED_SAFE` rows are never owners and are never upgraded (R9: a `FAILED_SAFE` owner does not block a later claim).
6. `ensure_partial_allowed_in_tx` lets a partial request learn `ExitInProgress` before its quantity is admitted against a broker snapshot (D15, Task 6). It reads only.
7. Uniqueness of the active owner relies on serialization: every claim runs inside the R12 worker and inside the per-database lock of `DuckDBConnection.transaction`. (A partial unique index is not available in DuckDB.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_exit_owner.py
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import (
    EXIT_OWNER_MIGRATION_VERSION, ExitInProgress, ExitOwnerRegistry, apply_exit_owner_migration,
)

NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=dt.timezone.utc)
ACCOUNT = "DU123"


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "owners.duckdb"))
    apply_exit_owner_migration(SchemaMigrator(db))
    return db


@pytest.fixture
def registry(db):
    return ExitOwnerRegistry(db)


def test_migration_35_creates_exit_owners_table(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "m.duckdb"))
    migrator = SchemaMigrator(db)
    assert EXIT_OWNER_MIGRATION_VERSION == 35
    assert apply_exit_owner_migration(migrator) is True
    assert apply_exit_owner_migration(migrator) is False
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 35", fetch="one") == ("sp1_exit_owners",)


def test_first_scoped_claim_is_claimed(registry):
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("c-1", "CLAIMED")
    assert registry.owner_for(ACCOUNT, 1).goal == "zero"


def test_second_full_close_joins_existing_full_owner(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("c-1", "JOINED")
    assert registry.get("c-2") is None


def test_full_close_upgrades_partial_owner_to_zero(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("p-1", "UPGRADED")
    owner = registry.get("p-1")
    assert (owner.goal, owner.goal_quantity) == ("zero", None)


def test_partial_against_any_existing_owner_is_refused(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-2", goal_quantity=3.0, now=NOW)
    assert ex.value.code == "EXIT_IN_PROGRESS"
    assert ex.value.root_id == "c-1"


def test_other_conid_is_independent(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-9", goal_quantity=None, now=NOW).outcome == "CLAIMED"


def test_account_claim_supersedes_scoped_owners_in_one_step(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-2", goal_quantity=None, now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    assert (claim.outcome, claim.superseded) == ("CLAIMED", ("c-2", "p-1"))
    assert registry.get("p-1").state == "SUPERSEDED"
    assert registry.owner_for(ACCOUNT, 1) is None
    assert registry.account_owner(ACCOUNT).root_id == "flat-1"


def test_scoped_full_request_joins_active_flatten_and_partial_is_refused(registry):
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    assert (claim.root_id, claim.outcome) == ("flat-1", "JOINED_FLATTEN")
    with pytest.raises(ExitInProgress):
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=2.0, now=NOW)


def test_superseded_owner_is_never_upgraded_or_revived(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW)
    assert claim.root_id == "flat-1"
    assert (registry.get("p-1").state, registry.get("p-1").goal) == ("SUPERSEDED", "partial")


def test_second_account_claim_joins_the_first(registry):
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-2", now=NOW)
    assert (claim.root_id, claim.outcome) == ("flat-1", "JOINED_FLATTEN")
    assert registry.get("flat-2") is None


def test_release_frees_the_slot_and_failed_safe_is_not_an_owner(registry, db):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    registry.release("c-1", NOW)
    assert registry.get("c-1").state == "RELEASED"
    assert registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW).outcome == "CLAIMED"
    db.transaction(lambda conn: registry.finish_in_tx(conn, "c-2", "FAILED_SAFE", NOW))
    assert registry.owner_for(ACCOUNT, 1) is None
    assert registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-3", goal_quantity=None, now=NOW).outcome == "CLAIMED"


def test_a_used_root_id_cannot_be_claimed_again(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    registry.release("c-1", NOW)
    with pytest.raises(ValueError):
        registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)


def test_claim_rolls_back_with_the_callers_transaction(registry, db):
    """R6: a claim made inside a caller's transaction disappears when that transaction fails."""
    class _Boom(Exception):
        pass

    def write(conn):
        registry.claim_scoped_in_tx(conn, account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
        raise _Boom()

    with pytest.raises(_Boom):
        db.transaction(write)
    assert registry.get("c-1") is None


def test_root_ids_with_a_colon_are_refused(registry):
    with pytest.raises(ValueError):
        registry.claim_account(account_id=ACCOUNT, root_id="flat:1", now=NOW)


def test_partial_check_refuses_any_active_owner_and_writes_nothing(registry, db):
    """D15: a partial request learns ExitInProgress before its quantity is admitted."""
    db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 1))
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 1))
    assert ex.value.root_id == "c-1"
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    with pytest.raises(ExitInProgress) as ex:
        db.transaction(lambda conn: registry.ensure_partial_allowed_in_tx(conn, ACCOUNT, 2))
    assert ex.value.root_id == "flat-1"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_exit_owner.py -q --timeout=30`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.trading.exit_owner'`.

- [ ] **Step 3: Implement**

```python
# trader/trading/exit_owner.py
"""Durable exit ownership: one close per position, one flatten per account.

Every exit producer (time exit, SELL intent, AI close, protective failure,
session flatten, /flatten, kill) claims here before it does anything. The
registry decides whether the caller starts a new root, joins an existing
root, upgrades a partial close to a full one, or is refused.

The ``*_in_tx`` methods take an open DuckDB connection. ``LiquidationService``
calls them inside one ``db.transaction`` together with its own run and child
rows, so a claim and the run it creates commit together or not at all.
Never call ``self._db`` from inside an ``*_in_tx`` method: the connection
lock is not reentrant.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator

EXIT_OWNER_MIGRATION_VERSION = 35

KIND_SCOPED = "scoped_close"
KIND_ACCOUNT = "account_flatten"
GOAL_ZERO = "zero"
GOAL_PARTIAL = "partial"
STATE_ACTIVE = "ACTIVE"
STATE_SUPERSEDED = "SUPERSEDED"
STATE_RELEASED = "RELEASED"
STATE_FAILED_SAFE = "FAILED_SAFE"

CLAIMED = "CLAIMED"
JOINED = "JOINED"
UPGRADED = "UPGRADED"
JOINED_FLATTEN = "JOINED_FLATTEN"


def apply_exit_owner_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(EXIT_OWNER_MIGRATION_VERSION, "sp1_exit_owners", (
        """CREATE TABLE IF NOT EXISTS exit_owners (
            root_id VARCHAR PRIMARY KEY,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            kind VARCHAR NOT NULL,
            goal VARCHAR NOT NULL,
            goal_quantity DOUBLE,
            state VARCHAR NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exit_owners_account ON exit_owners(account_id, state)",
    ))


class ExitInProgress(RuntimeError):
    code = "EXIT_IN_PROGRESS"

    def __init__(self, root_id: str):
        self.root_id = root_id
        super().__init__(f"an exit already owns this position: {root_id}")


@dataclass(frozen=True)
class ExitOwnerRow:
    root_id: str
    account_id: str
    conid: Optional[int]
    kind: str
    goal: str
    goal_quantity: Optional[float]
    state: str


@dataclass(frozen=True)
class ExitClaim:
    root_id: str
    outcome: str  # CLAIMED | JOINED | UPGRADED | JOINED_FLATTEN
    superseded: tuple[str, ...] = ()


_COLUMNS = "root_id, account_id, conid, kind, goal, goal_quantity, state"


def _row(values: Optional[tuple]) -> Optional[ExitOwnerRow]:
    if values is None:
        return None
    root_id, account_id, conid, kind, goal, goal_quantity, state = values
    return ExitOwnerRow(root_id, account_id, None if conid is None else int(conid),
                        kind, goal, goal_quantity, state)


def _check_root_id(root_id: str) -> None:
    if not root_id or ":" in root_id:
        raise ValueError("root id must be non-empty and may not contain ':'")


class ExitOwnerRegistry:
    def __init__(self, db: Any):
        self._db = db

    # -- reads inside a caller's transaction --------------------------------

    def get_in_tx(self, conn, root_id: str) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE root_id = ?", [root_id]).fetchone())

    def owner_for_in_tx(self, conn, account_id: str, conid: int) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND conid = ? "
            "AND kind = ? AND state = ?",
            [account_id, int(conid), KIND_SCOPED, STATE_ACTIVE]).fetchone())

    def account_owner_in_tx(self, conn, account_id: str) -> Optional[ExitOwnerRow]:
        return _row(conn.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
            [account_id, KIND_ACCOUNT, STATE_ACTIVE]).fetchone())

    def ensure_partial_allowed_in_tx(self, conn, account_id: str, conid: int) -> None:
        """A partial request against any active owner is refused, before anything is read or written."""
        owner = self.account_owner_in_tx(conn, account_id) or self.owner_for_in_tx(conn, account_id, conid)
        if owner is not None:
            raise ExitInProgress(owner.root_id)

    # -- claims inside a caller's transaction -------------------------------

    def claim_scoped_in_tx(self, conn, *, account_id: str, conid: int, root_id: str,
                           goal_quantity: Optional[float], now: dt.datetime) -> ExitClaim:
        _check_root_id(root_id)
        wants_partial = goal_quantity is not None
        flatten = self.account_owner_in_tx(conn, account_id)
        if flatten is not None:
            if wants_partial:
                raise ExitInProgress(flatten.root_id)
            return ExitClaim(flatten.root_id, JOINED_FLATTEN)
        owner = self.owner_for_in_tx(conn, account_id, conid)
        if owner is not None:
            if owner.root_id == root_id:
                return ExitClaim(owner.root_id, JOINED)
            if wants_partial:
                raise ExitInProgress(owner.root_id)
            if owner.goal == GOAL_PARTIAL:
                self.upgrade_goal_in_tx(conn, owner.root_id, now)
                return ExitClaim(owner.root_id, UPGRADED)
            return ExitClaim(owner.root_id, JOINED)
        self._insert_in_tx(conn, root_id, account_id, int(conid), KIND_SCOPED,
                           GOAL_PARTIAL if wants_partial else GOAL_ZERO, goal_quantity, now)
        return ExitClaim(root_id, CLAIMED)

    def claim_account_in_tx(self, conn, *, account_id: str, root_id: str,
                            now: dt.datetime) -> ExitClaim:
        _check_root_id(root_id)
        flatten = self.account_owner_in_tx(conn, account_id)
        if flatten is not None:
            outcome = CLAIMED if flatten.root_id == root_id else JOINED_FLATTEN
            return ExitClaim(flatten.root_id, outcome)
        scoped = tuple(sorted(r[0] for r in conn.execute(
            "SELECT root_id FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
            [account_id, KIND_SCOPED, STATE_ACTIVE]).fetchall()))
        conn.execute(
            "UPDATE exit_owners SET state = ?, updated_at = ? "
            "WHERE account_id = ? AND kind = ? AND state = ?",
            [STATE_SUPERSEDED, now, account_id, KIND_SCOPED, STATE_ACTIVE])
        self._insert_in_tx(conn, root_id, account_id, None, KIND_ACCOUNT, GOAL_ZERO, None, now)
        return ExitClaim(root_id, CLAIMED, scoped)

    def upgrade_goal_in_tx(self, conn, root_id: str, now: dt.datetime) -> None:
        """partial -> zero. Never the other way, never on a non-ACTIVE row."""
        conn.execute(
            "UPDATE exit_owners SET goal = ?, goal_quantity = NULL, updated_at = ? "
            "WHERE root_id = ? AND state = ?", [GOAL_ZERO, now, root_id, STATE_ACTIVE])

    def finish_in_tx(self, conn, root_id: str, state: str, now: dt.datetime) -> None:
        """ACTIVE -> RELEASED (proven done) or FAILED_SAFE (outcome not proven)."""
        if state not in (STATE_RELEASED, STATE_FAILED_SAFE):
            raise ValueError(f"an owner can only finish as RELEASED or FAILED_SAFE, not {state!r}")
        conn.execute("UPDATE exit_owners SET state = ?, updated_at = ? WHERE root_id = ? AND state = ?",
                     [state, now, root_id, STATE_ACTIVE])

    def _insert_in_tx(self, conn, root_id, account_id, conid, kind, goal, goal_quantity, now) -> None:
        if conn.execute("SELECT 1 FROM exit_owners WHERE root_id = ?", [root_id]).fetchone():
            raise ValueError(f"root id {root_id!r} was already used for an exit")
        conn.execute("INSERT INTO exit_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     [root_id, account_id, conid, kind, goal, goal_quantity, STATE_ACTIVE, now])

    # -- one-transaction conveniences ---------------------------------------

    def get(self, root_id: str) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.get_in_tx(conn, root_id))

    def owner_for(self, account_id: str, conid: int) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.owner_for_in_tx(conn, account_id, conid))

    def account_owner(self, account_id: str) -> Optional[ExitOwnerRow]:
        return self._db.transaction(lambda conn: self.account_owner_in_tx(conn, account_id))

    def claim_scoped(self, **kwargs) -> ExitClaim:
        return self._db.transaction(lambda conn: self.claim_scoped_in_tx(conn, **kwargs))

    def claim_account(self, **kwargs) -> ExitClaim:
        return self._db.transaction(lambda conn: self.claim_account_in_tx(conn, **kwargs))

    def release(self, root_id: str, now: dt.datetime) -> None:
        self._db.transaction(lambda conn: self.finish_in_tx(conn, root_id, STATE_RELEASED, now))
```

In the docstring of `trader/data/schema_migrations.py` replace the sentence that gives P3 its range with:

```python
P1 command-plane safety continues the F3-owned range: migration 24 is the
durable automation breaker and incident ledger. P3 deterministic automation
owns **30-39** (protective order sagas begin at migration 30; 31 session
controller; 32-34 trade attribution; SP1 safe close: 35 exit owners, 36
liquidation children and joins, 37 saga close ownership, 38 broker order OCA
fields; 39 is the last free number).
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_exit_owner.py -q --timeout=30`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/exit_owner.py trader/data/schema_migrations.py tests/test_exit_owner.py
git commit -m "feat: durable exit owner registry with in-transaction claims

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 18: Broker evidence the close relies on — readiness, the newest generation, OCA fields

Review round 2 found three gaps in the broker evidence the close needs (R22, R38):

- `TradingRuntimeOrderDispatch.enumeration_complete()` and `TraderBrokerAuthority.is_ready()` (`trader/trading/command_ports.py`) call `ingest.is_ready()`. `BrokerIngest.is_ready` is a **property** (`@property def is_ready` in `trader/trading/broker_ingest.py`), so both raise `TypeError: 'bool' object is not callable` on the real ingest. `_reconcile_approve` relies on the first one today. Both now read the property through one helper, `ingest_ready` (which also accepts the method form some test doubles use); `_broker_ready` in `command_stack.py` already reads it correctly on master and switches to the helper.
- The close fences a child order on the newest broker generation **including a staging one**, read right after the broker call (R22). `BrokerStateStore.newest_generation_in_tx` and `TradingRuntimeOrderDispatch.newest_generation()` give that number. A missing store raises: a fence that cannot be read must stop the close, never look like an old generation.
- `DONE` must check the OCA linkage the broker reports, not the ids the service wrote itself (R13, R38). Order rows get `oca_group` and `oca_type` (journal migration **38**), copied from IB's `Order.ocaGroup` / `Order.ocaType` by `normalize_open_order` and the ingest.

**Files:**
- Modify: `trader/data/broker_state.py` (`BrokerOrderRow.oca_group/oca_type`, migration 38, `_ORDER_COLUMNS`, `newest_generation_in_tx`)
- Modify: `trader/trading/order_correlation.py` (`OrderObservation.oca_group/oca_type`, `normalize_open_order`)
- Modify: `trader/trading/broker_ingest.py` (`_apply_order` copies the OCA fields)
- Modify: `trader/trading/command_ports.py` (`ingest_ready`, `TraderBrokerAuthority.is_ready`), `trader/trading/command_stack.py` (`_broker_ready`), `trader/trading/trading_runtime.py` (`TradingRuntimeOrderDispatch.enumeration_complete`, `newest_generation`)
- Test: `tests/test_close_broker_evidence.py` (create)

**Interfaces:**

```python
BROKER_ORDER_OCA_MIGRATION_VERSION = 38                   # trader/data/broker_state.py
BrokerOrderRow: + oca_group: Optional[str] = None, + oca_type: Optional[int] = None   # last fields
OrderObservation: + oca_group: Optional[str] = None, + oca_type: Optional[int] = None
BrokerStateStore.newest_generation_in_tx(conn) -> Optional[int]                     # any status
def ingest_ready(ingest) -> bool                                                     # trader/trading/command_ports.py
TradingRuntimeOrderDispatch.enumeration_complete() -> bool
TradingRuntimeOrderDispatch.newest_generation() -> int                               # RuntimeError when unavailable
```

- [ ] **Step 1: Write the failing tests**

```python
"""SP1 plan 1 Task 18: the broker evidence the safe close relies on."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.broker_ingest import BrokerIngest
from trader.trading.command_ports import TraderBrokerAuthority
from trader.trading.trading_runtime import TradingRuntimeOrderDispatch

NOW = dt.datetime(2026, 7, 15, 13, 0, tzinfo=dt.timezone.utc)
ACCOUNT = "DU123"


class _Ingest:
    """``BrokerIngest.is_ready`` is a property, not a method."""
    def __init__(self, ready):
        self._ready = ready

    @property
    def is_ready(self):
        return self._ready


@pytest.mark.parametrize("ready", [True, False])
def test_enumeration_complete_reads_the_readiness_property(ready):
    trader = SimpleNamespace(broker_ingest=_Ingest(ready))
    assert TradingRuntimeOrderDispatch(trader).enumeration_complete() is ready


def test_enumeration_is_not_complete_without_an_ingest():
    assert TradingRuntimeOrderDispatch(SimpleNamespace()).enumeration_complete() is False


@pytest.mark.parametrize("ready", [True, False])
def test_broker_authority_readiness_reads_the_property(ready):
    trader = SimpleNamespace(broker_ingest=_Ingest(ready))
    authority = TraderBrokerAuthority(trader, run_coro=lambda c: None, resolve_contract=lambda conid: None)
    assert authority.is_ready() is ready


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "evidence.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    store = BrokerStateStore(db)
    store.migrate(migrator)
    ingest = BrokerIngest(db=db, journal=journal, store=store, account_id=ACCOUNT, account_mode="paper",
                          session_epoch="s1", clock=lambda: NOW)
    trader = SimpleNamespace(broker_state_store=store, domain_journal=journal, broker_ingest=ingest)
    return SimpleNamespace(db=db, journal=journal, store=store, ingest=ingest, migrator=migrator,
                           dispatch=TradingRuntimeOrderDispatch(trader))


def test_newest_generation_counts_a_generation_that_is_still_staging(env):
    def promoted_then_staging(conn):
        first = env.store.open_generation_in_tx(conn, ("account",), NOW)
        env.store.mark_generation_promoted_in_tx(conn, first, 1, NOW)
        return first, env.store.open_generation_in_tx(conn, ("account",), NOW)
    first, staging = env.db.transaction(promoted_then_staging)
    assert env.db.transaction(env.store.latest_promoted_generation_in_tx) == first
    assert env.dispatch.newest_generation() == staging > first


def test_newest_generation_fails_loudly_without_a_store():
    with pytest.raises(RuntimeError):
        TradingRuntimeOrderDispatch(SimpleNamespace()).newest_generation()


def test_order_rows_carry_the_oca_group_and_type(env):
    assert 38 in env.migrator.applied_versions()
    order = SimpleNamespace(orderId=7, permId=70, parentId=0, orderRef="mmr:p-1-reprotect-stop-265598-1",
                            account=ACCOUNT, action="SELL", orderType="STP", totalQuantity=6.0, lmtPrice=0.0,
                            auxPrice=95.0, tif="DAY", ocaGroup="p-1-reprotect-265598-1", ocaType=2)
    trade = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted", filled=0.0,
                                                                     avgFillPrice=0.0),
                            contract=SimpleNamespace(conId=265598, symbol="AAPL"))
    env.ingest.on_open_order(trade)
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type, row.leg) == ("p-1-reprotect-265598-1", 2, "stop")


def _stop_trade(oca_group, oca_type, status="Submitted"):
    order = SimpleNamespace(orderId=7, permId=70, parentId=0, orderRef="mmr:p-1-reprotect-stop-265598-1",
                            account=ACCOUNT, action="SELL", orderType="STP", totalQuantity=6.0, lmtPrice=0.0,
                            auxPrice=95.0, tif="DAY", ocaGroup=oca_group, ocaType=oca_type)
    return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=status, filled=0.0, avgFillPrice=0.0),
                           contract=SimpleNamespace(conId=265598, symbol="AAPL"))


def test_an_explicit_empty_oca_clears_the_stored_link(env):
    """#45: the broker saying "no OCA" ('' and 0) is not a missing field; it clears the link, also after a
    restart, so a close cannot take the leg as linked protection and end DONE."""
    from trader.trading.order_correlation import encode_order_ref

    env.ingest.on_open_order(_stop_trade("p-1-reprotect-265598-1", 2))
    env.ingest.drain_once()
    env.ingest.on_open_order(_stop_trade("", 0))
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == (None, None)
    restarted = BrokerStateStore(DuckDBConnection.get_instance(str(env.db.db_path)))
    [row] = restarted.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == (None, None)
    [found] = env.dispatch.find_by_order_ref(ACCOUNT, encode_order_ref("p-1-reprotect-stop-265598-1"))
    assert found.oca_type != 2                     # DONE's link check needs type 2 and the group


def test_an_observation_without_oca_fields_keeps_the_stored_link(env):
    env.ingest.on_open_order(_stop_trade("p-1-reprotect-265598-1", 2))
    env.ingest.drain_once()
    trade = _stop_trade("x", 0)
    del trade.order.ocaGroup, trade.order.ocaType
    env.ingest.on_open_order(trade)
    env.ingest.drain_once()
    [row] = env.store.select_active_orders_in_tx(env.journal.connect())
    assert (row.oca_group, row.oca_type) == ("p-1-reprotect-265598-1", 2)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_close_broker_evidence.py -q --timeout=30`
Expected: 9 failed, 1 passed. The readiness tests fail with `TypeError: 'bool' object is not callable` (the bug), the generation tests with `AttributeError: ... 'newest_generation'`, the three OCA tests with `AttributeError: ... 'oca_group'`. `test_enumeration_is_not_complete_without_an_ingest` already passes.

- [ ] **Step 3: Implement**

`trader/data/broker_state.py`: add `BROKER_ORDER_OCA_MIGRATION_VERSION = 38` under `BROKER_STATE_MIGRATION_VERSION`; add the two fields at the end of `BrokerOrderRow`:

```python
    revision: int
    source_timestamp: dt.datetime
    oca_group: Optional[str] = None
    oca_type: Optional[int] = None
```

Add one column list before `_WORKING_ORDER_STATUSES` and use it in the three `SELECT ... FROM broker_orders` statements (`get_order_in_tx`, `select_active_orders_in_tx`, `select_working_orders_in_tx`), so `_order_from_row` keeps mapping by position:

```python
_ORDER_COLUMNS = (
    "order_entity_id, account_id, conid, symbol, order_group_id, leg, is_external, action, order_type, "
    "total_quantity, filled_quantity, avg_fill_price, limit_price, stop_price, tif, status, deleted, "
    "revision, source_timestamp, oca_group, oca_type"
)
```

```python
    def get_order_in_tx(self, conn: Any, order_entity_id: str) -> Optional[BrokerOrderRow]:
        row = conn.execute(
            f"SELECT {_ORDER_COLUMNS} "
            "FROM broker_orders WHERE order_entity_id = ?",
            [order_entity_id],
        ).fetchone()
        return None if row is None else self._order_from_row(row)
```

`migrate` applies 38, and `upsert_order_in_tx` writes the two columns (`"oca_group": row.oca_group, "oca_type": row.oca_type` after `"updated_at"`):

```python
    def migrate(self, migrator: Any) -> None:
        migrator.apply(BROKER_STATE_MIGRATION_VERSION, "broker_state_tables", _STATEMENTS)
        migrator.apply(BROKER_ORDER_OCA_MIGRATION_VERSION, "sp1_broker_order_oca", (
            "ALTER TABLE broker_orders ADD COLUMN IF NOT EXISTS oca_group VARCHAR",
            "ALTER TABLE broker_orders ADD COLUMN IF NOT EXISTS oca_type INTEGER",
        ))
```

Add before `latest_promoted_generation_in_tx`:

```python
    def newest_generation_in_tx(self, conn: Any) -> Optional[int]:
        """The highest broker generation id in any state, staging included.

        A generation with a higher id was opened after this read, so its
        enumeration started after anything this process sent before the read.
        """
        row = conn.execute("SELECT MAX(generation_id) FROM broker_sync_generations").fetchone()
        return None if row is None or row[0] is None else int(row[0])
```

`trader/trading/order_correlation.py`: add as the last fields of `OrderObservation`:

```python
    oca_group: Optional[str] = None
    oca_type: Optional[int] = None
    oca_reported: bool = False   # True when the source carries OCA fields; '' / 0 then mean "no OCA" (#45)
```

and in `normalize_open_order` after `source_timestamp=now,`:

```python
        oca_group=getattr(order, "ocaGroup", None) or None,
        oca_type=int(getattr(order, "ocaType", 0) or 0) or None,
        oca_reported=hasattr(order, "ocaGroup") and hasattr(order, "ocaType"),
```

Ruling 45: a source that reports OCA fields overwrites the stored link, so an explicit empty group and type 0 clear it; only a source without the fields keeps the stored link.

`trader/trading/broker_ingest.py` `_apply_order`: after `source_timestamp=obs.source_timestamp,` in the merged row:

```python
            oca_group=obs.oca_group if obs.oca_reported else (current.oca_group if current else None),
            oca_type=obs.oca_type if obs.oca_reported else (current.oca_type if current else None),
```

`trader/trading/command_ports.py`: add before `class TraderBrokerAuthority` and use it in `TraderBrokerAuthority.is_ready` (`return ingest_ready(ingest)`):

```python
def ingest_ready(ingest: Any) -> bool:
    """``BrokerIngest.is_ready`` is a property; some test doubles make it a method."""
    readiness = ingest.is_ready
    return bool(readiness() if callable(readiness) else readiness)
```

`trader/trading/command_stack.py`: import `ingest_ready` from `trader.trading.command_ports`; `_broker_ready` ends with `return ingest_ready(trader.broker_ingest)`.

`trader/trading/trading_runtime.py`: replace `TradingRuntimeOrderDispatch.enumeration_complete` with:

```python
    def enumeration_complete(self) -> bool:
        """A promoted broker generation exists and no newer one is staging."""
        from trader.trading.command_ports import ingest_ready
        ingest = getattr(self._trader, 'broker_ingest', None)
        return ingest is not None and ingest_ready(ingest)

    def newest_generation(self) -> int:
        """The highest broker generation id, staging included (fence for a child order).

        Raises when the broker store is not wired: a fence that cannot be read
        must stop the close, never look like an old generation.
        """
        store = getattr(self._trader, 'broker_state_store', None)
        journal = getattr(self._trader, 'domain_journal', None)
        if store is None or journal is None:
            raise RuntimeError('broker state store unavailable for a generation fence')
        newest = store.newest_generation_in_tx(journal.connect())
        if newest is None:
            raise RuntimeError('no broker generation has been opened yet')
        return newest
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_close_broker_evidence.py tests/test_broker_*.py tests/test_order_correlation.py tests/test_order_dispatch_ports.py tests/test_command_stack.py tests/test_command_coordinator.py -q --timeout=60`
Expected: all PASS. Task 18 runs before Task 4, so its tests use only master's dispatch (`find_by_order_ref` + `encode_order_ref`), never Task 4's `_LiquidationDispatch` (ruling 53). Do not start Task 4 until this step is green. Then the full suite: green.

- [ ] **Step 5: Commit**

```bash
git add trader/data/broker_state.py trader/trading/order_correlation.py trader/trading/broker_ingest.py trader/trading/command_ports.py trader/trading/command_stack.py trader/trading/trading_runtime.py tests/test_close_broker_evidence.py
git commit -m "fix: read broker readiness as a property; fence and oca evidence for the close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 4: Frozen data model, migration 36, write-ahead children; account scope on the new model

This task freezes the data model every later task uses, and rebuilds the account flatten on it: journaled children (R1, R2), evidence rules (R4, R22, R23), the reduce rule (R5, R23), one-transaction claims (R6, R10), re-read before dispatch (R7), durable cleanup with the owner released in the terminal transaction (R8, R24), inheritance from `FAILED_SAFE` roots (R9), one resolver for commands (R33), every send logged (R34) and the adoption of runs that were open before the upgrade (R29). The conid scope arrives in Task 5.

**Files:**
- Modify (rewrite): `trader/trading/liquidation_service.py`
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch`, the registry and run store after `apply_liquidation_migration`, the `LiquidationService(` construction, the two session adapters)
- Modify: `scripts/command_plane_drill.py` (`scn_liquidation`)
- Modify: `trader/trading/order_correlation.py`, `trader/trading/command_ports.py`, `trader/trading/trading_runtime.py` (the ref-prefix lookup of ruling 42)
- Test (rewrite): `tests/test_liquidation_service.py`; Test: `tests/test_command_stack.py`, `tests/test_order_correlation.py`; modify master's `tests/test_trader_service_loops.py` (`_liquidation` builds the journal service) and `tests/automation/test_protective_order_saga.py` (`test_busy_liquidation_keeps_protective_failure_root_for_rescan` builds the journal service)

**Interfaces (frozen — every later task uses exactly these names and types):**

```python
# trader/trading/liquidation_service.py
LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION = 36
def apply_liquidation_migration(migrator) -> None        # applies 25, 35 (exit owners) and 36
RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "REDUCE_FAILED", "SUPERSEDED", "FAILED_SAFE"})
SUCCESS_STATES = frozenset({"FLAT", "CLOSED", "DONE"})
OWNER_RELEASED_STATES = SUCCESS_STATES | {"REDUCE_FAILED"}   # every other terminal state: owner FAILED_SAFE
CHILD_STATES = {"PLANNED", "UNKNOWN", "WORKING", "FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"}
CHILD_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"}

class LiquidationBusy(RuntimeError)      # kept from master (PR #42): the timed lock was held past the timeout
class DispatchRefused(RuntimeError)      # (code, detail): proven refusal before the broker -> child NOT_SENT
class LiquidationRefused(ValueError)     # (code, detail): request refused up front, nothing written
class RunStateError(RuntimeError)        # a terminal or SUPERSEDED run changed, or a goal lowered

class BrokerSnapshotPort(Protocol):       capture(account_id) -> BrokerRiskSnapshot
class LiquidationDispatchPort(Protocol):
    cancel(order, child_id) -> None
    reduce(position, side, quantity, child_id) -> None                 # whole position
    reduce_partial(position, side, quantity, child_id) -> None         # 0 < q < |position|
    place_exit_leg(position, *, leg, quantity, price, oca_group, child_id) -> None   # leg "stop" | "target"
    find_orders(account_id, child_id) -> list                          # rows whose decoded ref == child_id
    find_orders_with_prefix(account_id, prefix) -> list                # rows whose decoded ref is prefix + digits (ruling 42)
    get_order(order_entity_id) -> Optional[row]                        # incl. deleted rows
    enumeration_complete() -> bool                                     # Task 18
    newest_generation() -> int                                         # Task 18; staging included
    # Task 6 adds hold_broker_changes() -> ContextManager (ruling 48); Task 6 adds child state PENDING_CANCEL (ruling 47)
class LiquidationBreakerPort(Protocol):   trip_liquidation(cause_command_id, detail) -> None
class GenerationRefreshPort(Protocol):    request_refresh(account_id) -> None       # never blocks
@dataclass(frozen=True) class CancelTarget(order_entity_id: str, order_group_id: Optional[str])
@dataclass(frozen=True) class HandoverInfo(stop_price: Optional[float], target_price: Optional[float])
class ProtectionOwnershipPort(Protocol):  # ProtectiveOrderSaga implements it in Task 9; all idempotent
    handover(*, account_id, conid, close_root_id, cancels: tuple[CancelTarget, ...], generation, now) -> HandoverInfo
    handover_account(*, account_id, close_root_id, cancels, generation, now) -> None
    expect_reprotect(*, close_root_id, groups: tuple[str, ...], now) -> None             # Task 6 calls it
    release_after_partial(*, close_root_id, remaining_quantity, stop_group, stop_status,
                          target_group, target_status, now) -> None
    close_after_full(*, close_root_id, now) -> None

@dataclass(frozen=True)
class ChildRef:
    child_id: str; root_id: str; owner_root_id: str; account_id: str
    conid: Optional[int]           # None only for a pre-SP1 wildcard child (ruling 42)
    kind: str                      # cancel | reduce | reprotect-stop | reprotect-target
    attempt: int; state: str       # CHILD_STATES
    fence_generation: int          # generation of the snapshot it was planned on
    side=None; quantity=None; price=None; oca_group=None; target_order_entity_id=None
    filled_at_send=0.0; filled_quantity=0.0; outstanding_quantity=None
    observed_generation=None       # newest generation (staging included) when last observed
    sent_generation=None           # newest generation (staging included) right after the send (R22)
    order_entity_id=None           # the child's own broker order row
    ref_prefix=None                # pre-SP1 wildcard: every ref {prefix}{conid} (ruling 42)
    fill_bearing: bool             # property: ABSENT, or filled_quantity > filled_at_send

@dataclass(frozen=True)
class LiquidationReceipt:
    account_id; cause_command_id; state; deadline; generation_id=None; detail=""
    scope="account"; conid=None; goal="zero"; goal_quantity=None; phase=""   # phase "" | legacy | cancel | reduce | reprotect
    opened_generation=None; stop_price=None; target_price=None; remaining_quantity=None
    escalated=False; superseded_by=None; cleanup_pending=False
    children: tuple[ChildRef, ...] = ()     # every child whose owner_root_id is this root

@dataclass(frozen=True) class JoinRow(command_id, root_id, account_id, conid, outcome, requested_goal, requested_quantity)
    # requested_goal / requested_quantity: the request as it came in, before admission (D15)
@dataclass(frozen=True) class CloseResolution(command_id, root_id, state, success: bool, outcome: dict,
                                              error_code: Optional[str] = None)
    # error_code: CLOSE_FAILED_SAFE | REDUCE_FAILED | CLOSE_GOAL_NOT_MET when success is False
    # outcome: close_root_id, liquidation_state, generation_id, detail, requested_goal, requested_quantity,
    #          root_goal, filled_quantity (sold by the root's reduces), remaining_quantity

class LiquidationRunStore:                # the journal rows; R6 in-transaction API
    transaction(fn)
    get_run_in_tx / insert_run_in_tx / update_run_in_tx(conn, receipt, now)   # update refuses terminal changes and goal zero -> partial
    roots_to_advance_in_tx(conn) -> list[str]                                  # cleanup-pending first, then open roots
    children_in_tx / child_in_tx / next_attempt_in_tx / insert_child_in_tx / update_child_in_tx / drop_planned_in_tx
    inherit_children_in_tx(conn, *, account_id, conid, to_root_id, now) -> int  # R9
    record_join_in_tx / join_for_in_tx / joins_resolving_to_in_tx
    pre_sp1_roots_in_tx(conn, account_id) / clear_pre_sp1_mark_in_tx(conn, run_id)       # N2, rulings 40, 42
    legacy_reduces_in_tx(conn, account_id) -> tuple[ChildRef, ...]                       # every wildcard child, any owner
    fill_watermark_in_tx(conn, account_id, conid) -> Optional[int]                       # ruling 43
    receipt(root_id) / root_for(command_id) / close_resolution(command_id)     # own transaction each

class LiquidationService:
    __init__(broker, dispatch, *, store, registry, now, breaker=None, journal=None, ledger=None,
             schedule_reconcile=None, protection=None, refresh=None, deadline_seconds=300.0,
             lock_timeout_seconds=60.0)                                  # master's lock (ruling 31)
    attach_protection(protection) -> None
    liquidate(cmd) -> CommandReceipt                                  # OUTCOME_UNKNOWN, then schedule_reconcile(cmd)
    start(account_id, cause_command_id, deadline, *, scope="account", conid=None, quantity=None,
          stop_price=None, target_price=None) -> LiquidationReceipt   # the root the caller must poll
    rescan() -> Optional[LiquidationReceipt]
    receipt_for(root_id) / root_for(command_id) / close_resolution(command_id)
    upgrade_to_zero(root_id) -> LiquidationReceipt                    # added in Task 6
```

Journal tables of migration 36: `liquidation_runs` gains `scope, conid, goal, goal_quantity, phase, opened_generation, stop_price, target_price, remaining_quantity, escalated, superseded_by, cleanup_pending`; new `liquidation_children` (one row per child order, keyed by `child_id`, with `owner_root_id` for inheritance, `sent_generation` and `order_entity_id`); new `liquidation_joins` (one row per command that started or joined a root — R17's `(command_id, root_id)`). Migration 36 also adopts the runs that were open before the upgrade (R29): every old run gets a join row (so a retry of its command id is bound and returns it); the oldest open run of an account becomes its `ACTIVE` account owner in phase `legacy`; other open runs of that account become `SUPERSEDED` by it. Every old run that did not end `FLAT` is marked `pre_sp1_open` (ruling 40, N2). On its first tick a root of any scope (the adopted one, or the first one claimed after the upgrade) journals, for every marked run of its account, ONE wildcard `UNKNOWN` child it owns that stands for every reduce the old service may have sent (old refs `{run}-liquidation-reduce-{conid}`, any conid; ruling 42), fenced on the newest generation. The snapshot's positions are not used: master's runs record no conids, and a missing position is never evidence. While any wildcard child of the account is unsettled, it blocks every reduce of every root on the account, whoever owns it. It settles only when every matching broker row is terminal and a complete enumeration on a generation newer than its fence holds; a visible working match is waited on, never cancelled; an invisible one blocks to the deadline. The run's mark is cleared in the transaction that settles its wildcard child, never on journaling. Nothing is invented as `NOT_SENT` or `ABSENT` to make the old run fit the new schema (R2-5).

Rules this task implements (they also hold for every later scope):
- **Write-ahead (R1, R7).** `_reserve` re-reads the run and its owner, checks the root may still dispatch (owner `ACTIVE`, run not terminal, owner goal = run goal = the tick's goal), and writes the children as `UNKNOWN` in one transaction. Only then `_send` calls the broker, after one more re-read.
- **Send (R2, R22, R34).** `DispatchRefused` → `NOT_SENT`; any other exception → stays `UNKNOWN`; both are logged (`log.exception` for the second). Right after the call the child gets `sent_generation` = `dispatch.newest_generation()`, the newest broker generation including a staging one. A child that never got it (a crash) is fenced at the start of the next tick (`_fence_unsent`): every entry point runs on one worker, so no send is in flight then. A cancel cut off that way becomes `NOT_SENT`, because sending a cancel again is harmless (R38, the cancel livelock of #20).
- **Evidence (R4, R22, R23, ruling 7).** `_observe_children` classifies every `UNKNOWN`/`WORKING` child from its own broker row on each step. A terminal status counts at once, on any generation. An empty lookup is `ABSENT` only when `enumeration_complete()` is true and the snapshot's generation is newer than the child's `sent_generation`; otherwise the child stays as it is until the deadline. `observed_generation` is the newest generation (staging included) at the read, so the fill-freshness margin is the same as the send fence.
- **Reduce rule (R5, R23, ruling 4).** `_blocking`: any `UNKNOWN` child, any `WORKING` child of any kind, or a snapshot not newer than the last observed fill stops every new reduce. The children checked are the root's own plus every wildcard child of its account, whoever owns it (ruling 42).
- **Cancels (R23, R27, spec 5.1 step 4).** `_cancel_targets` is every working order of the scope in the snapshot, plus every working re-protect leg found by its own row (it may have appeared after the capture). A reduce order is never cancelled (not the root's own, not an inherited one, not a pre-SP1 one): it only reduces, and while it works it blocks. A cancel the root sent covers its target; an inherited cancel does not. Before every cancel batch the protection is handed over again with the new targets (`handover_account`), so the saga expects every cancel.
- **Claims (R6, R10).** `start(scope="account")` claims, creates the run and records the join row in one transaction; `JOINED_FLATTEN` creates nothing and returns the existing root's receipt.
- **Terminal + cleanup (R6, R8, R9, R24, ruling 9).** `_finish` writes the terminal state, `cleanup_pending` and the owner's end state (`RELEASED` for `OWNER_RELEASED_STATES`, else `FAILED_SAFE`) in one transaction; `_cleanup` runs the idempotent saga step and recovery re-runs it. `_set` changes only the named fields of the run as the journal has it now; the store refuses to change a terminal run or to lower a goal.
- **Lock (ruling 31).** The worker (Task 15) is the serialization; master's timed lock stays as a second guard. `start` commits its claim before it waits; only `_tick` runs under the lock, `rescan` holds it for its whole pass; a wait past `lock_timeout_seconds` raises `LiquidationBusy` and the root survives for the next `rescan`. `liquidate` on `LiquidationBusy` records `OUTCOME_UNKNOWN` for the root, schedules it and re-raises.
- **Commands (R17, R33).** The service never resolves a command. `liquidate` writes `OUTCOME_UNKNOWN` and schedules the command for the reconciler; cleanup schedules every `OUTCOME_UNKNOWN` command that joined the root. A command still in `SUBMITTING` is left to its producer.

- [ ] **Step 1: Write the failing tests**

Replace the whole of `tests/test_liquidation_service.py` with the following. Master's first eight tests are kept (one renamed to say what it now checks: `test_flat_requires_newer_generation_and_a_terminal_reduce_child`, R19); they now run on a real temporary DuckDB journal and the fake dispatch answers evidence lookups. Of the seven tests PR #42 added, five are kept on the journal (the section "Kept from master"), `test_rescan_skips_failed_safe_root_and_advances_a_busy_registered_root` is covered by `test_failed_safe_root_does_not_block_rescan_of_a_newer_root` plus the busy-start test, and the FLAT-resolves-the-command part of the busy flatten test is gone (R33, ruling 32). The round-2 section holds the verification fixes for the upgrade (N2, R2-5); the last section holds the round-3 wildcard tests (ruling 42). The fake's `newest_generation` is the last captured generation plus `staging`: a test sets `staging = 1` to model a broker sync that opened before the send.

```python
import datetime as dt
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import (
    ChildRef, DispatchRefused, LiquidationBusy, LiquidationReceipt, LiquidationRefused, LiquidationRunStore,
    LiquidationService, RunStateError, apply_liquidation_migration,
)
from trader.trading.order_correlation import matches_legacy_reduce


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"
DEADLINE = NOW + dt.timedelta(minutes=5)


def _position(quantity=10.0, conid=1, market_price=None):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="AAPL", sec_type="STK", exchange="SMART",
        currency="USD", quantity=quantity, average_cost=None, market_price=market_price,
        market_value=None, unrealized_pnl=None, realized_pnl=None, daily_pnl=None,
        deleted=False, revision=1, source_timestamp=NOW,
    )


def _order(entity="external-1", group=None, conid=1, leg=None, filled=0.0, total=10.0, order_type="LMT"):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL",
        order_group_id=group, leg=leg, is_external=group is None, action="SELL", order_type=order_type,
        total_quantity=total, filled_quantity=filled, avg_fill_price=None, limit_price=100,
        stop_price=None, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def _snapshot(generation, positions=(), working=()):
    return BrokerRiskSnapshot(
        generation_id=generation, source_cursor=generation, promoted_at=NOW,
        account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
        daily_pnl=0, positions=tuple(positions), working_orders=tuple(working),
    )


def _row(status, filled=0.0, total=10.0, entity=None, action="SELL", oca_group=None, oca_type=None):
    return SimpleNamespace(status=status, filled_quantity=filled, total_quantity=total, deleted=False,
                           order_entity_id=entity, action=action, oca_group=oca_group, oca_type=oca_type)


class _Broker:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0
        self.last = 0          # generation of the last captured snapshot

    def capture(self, account_id):
        self.calls += 1
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        self.last = value.generation_id
        return value


class _Dispatch:
    """Records orders; answers evidence lookups from dicts the test fills.

    ``newest_generation`` is the last captured generation plus ``staging``:
    set ``staging = 1`` to model a broker sync that opened before a send.
    ``complete`` is ``enumeration_complete()``.
    """
    def __init__(self, broker):
        self.broker = broker
        self.calls = []
        self.rows: dict[str, list] = {}       # child id -> broker rows found by its order ref
        self.sequences: dict[str, list] = {}  # child id -> successive answers; the last one repeats
        self.entities: dict[str, object] = {}  # order entity id -> broker row (cancel targets)
        self.refuse: set[str] = set()          # methods that raise DispatchRefused before the boundary
        self.fail_after_send: set[str] = set() # methods that raise after the order was sent
        self.staging = 0
        self.complete = True

    def _record(self, name, call):
        if name in self.refuse:
            raise DispatchRefused("REDUCE_ONLY_REFUSED", name)
        self.calls.append(call)
        if name in self.fail_after_send:
            raise TimeoutError(f"{name} acknowledgement timed out")

    def cancel(self, order, child_id):
        self._record("cancel", ("cancel", order.order_entity_id, child_id))

    def reduce(self, position, side, quantity, child_id):
        self._record("reduce", ("reduce", position.conid, side, quantity, child_id))

    def reduce_partial(self, position, side, quantity, child_id):
        self._record("reduce_partial", ("reduce_partial", position.conid, side, quantity, child_id))

    def place_exit_leg(self, position, *, leg, quantity, price, oca_group, child_id):
        self._record("place_exit_leg", ("place_exit_leg", position.conid, leg, quantity, price, oca_group, child_id))

    def find_orders(self, account_id, child_id):
        answers = self.sequences.get(child_id)
        if answers:
            return list(answers.pop(0) if len(answers) > 1 else answers[0])
        return list(self.rows.get(child_id, []))

    def find_orders_with_prefix(self, account_id, prefix):
        return [row for ref, rows in self.rows.items() if matches_legacy_reduce(ref, prefix) for row in rows]

    def get_order(self, order_entity_id):
        return self.entities.get(order_entity_id)

    def enumeration_complete(self):
        return self.complete

    def newest_generation(self):
        return self.broker.last + self.staging


class _Breaker:
    def __init__(self): self.calls = []
    def trip_liquidation(self, cause, detail): self.calls.append((cause, detail))


class _LedgerRow:
    def __init__(self, state): self.state = state


class _Ledger:
    def __init__(self, states=None):
        self.rows = {command_id: _LedgerRow(state) for command_id, state in (states or {}).items()}
        self.transitions = []
    def get(self, command_id): return self.rows.get(command_id)
    def transition_in_tx(self, _conn, command_id, before, after, **kwargs):
        self.transitions.append((command_id, before, after, kwargs))


class _Journal:
    def connect(self): return object()
    def mutate(self, conn, mutation, write, event_id): write(conn, 1)


class _Crash(BaseException):
    """Simulated process death: never caught by the service."""


class _Stack:
    """A service over a real DuckDB journal; restart() rebuilds it on the same file."""
    def __init__(self, tmp_path, snapshots, *, protection=None, journal=None, ledger=None, deadline_seconds=300.0,
                 lock_timeout_seconds=60.0):
        self.db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
        migrator = SchemaMigrator(self.db)
        apply_exit_owner_migration(migrator)
        apply_liquidation_migration(migrator)
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.broker = _Broker(snapshots)
        self.dispatch = _Dispatch(self.broker)
        self.breaker = _Breaker()
        self.scheduled: list[str] = []
        self.clock = {"now": NOW}
        self.protection, self.journal, self.ledger = protection, journal, ledger
        self.deadline_seconds = deadline_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self.service = self._build()

    def _build(self):
        return LiquidationService(
            self.broker, self.dispatch, store=self.store, registry=self.registry,
            now=lambda: self.clock["now"], breaker=self.breaker, journal=self.journal, ledger=self.ledger,
            schedule_reconcile=self.scheduled.append, protection=self.protection,
            deadline_seconds=self.deadline_seconds, lock_timeout_seconds=self.lock_timeout_seconds)

    def restart(self):
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.service = self._build()
        return self.service

    def push(self, *snapshots):
        self.broker.snapshots = list(snapshots)


def _stack(tmp_path, snapshots, **kwargs):
    return _Stack(tmp_path, snapshots, **kwargs)


class _Protection:
    """Records every call; ``crash_on`` makes the next call of that name die."""
    def __init__(self, stop_price=95.0, target_price=None):
        self.calls = []
        self.info = (stop_price, target_price)
        self.crash_on: set[str] = set()

    def _call(self, name, *args):
        if name in self.crash_on:
            self.crash_on.discard(name)
            raise _Crash()
        self.calls.append((name, *args))

    def handover(self, *, account_id, conid, close_root_id, cancels, generation, now):
        from trader.trading.liquidation_service import HandoverInfo
        self._call("handover", conid, close_root_id, tuple(c.order_entity_id for c in cancels))
        return HandoverInfo(*self.info)

    def handover_account(self, *, account_id, close_root_id, cancels, generation, now):
        self._call("handover_account", close_root_id, tuple(c.order_entity_id for c in cancels))

    def expect_reprotect(self, *, close_root_id, groups, now):
        self._call("expect_reprotect", close_root_id, tuple(groups))

    def release_after_partial(self, *, close_root_id, remaining_quantity, stop_group, stop_status,
                              target_group, target_status, now):
        self._call("release_after_partial", close_root_id, remaining_quantity, stop_group, target_group)

    def close_after_full(self, *, close_root_id, now):
        self._call("close_after_full", close_root_id)


# ---------------------------------------------------------------------------
# Task 4: data model, store, account scope
# ---------------------------------------------------------------------------

def test_cancels_external_orders_before_submitting_any_reduction(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_order()]), _snapshot(2, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    s.dispatch.entities["external-1"] = _row("Cancelled")
    receipt = s.service.rescan()
    assert receipt.state == "VERIFYING"
    assert [call[0] for call in s.dispatch.calls] == ["cancel", "reduce"]
    assert s.dispatch.calls[1][2:4] == ("SELL", 10.0)


def test_submitted_reduction_is_not_treated_as_flat_without_new_broker_snapshot(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "VERIFYING"
    assert len(s.dispatch.calls) == 1
    assert s.breaker.calls


def test_flat_requires_newer_generation_and_a_terminal_reduce_child(tmp_path):
    """R19: FLAT only after a newer empty snapshot and a terminal reduce child."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])])
    assert s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1)).state == "VERIFYING"
    child = s.service.receipt_for("root-1").children[0]
    assert (child.child_id, child.kind, child.state, child.fence_generation, child.sent_generation) == (
        "root-1-reduce-1-1", "reduce", "UNKNOWN", 1, 1)
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    assert s.service.rescan().state == "VERIFYING"      # generation 2 observes the fill
    assert s.service.rescan().state == "FLAT"           # generation 3 is newer than that fill


def test_flat_hands_waiting_commands_to_the_reconciler_and_resolves_nothing(tmp_path):
    """D12: the reconciler is the only resolver; a SUBMITTING command is left to its producer."""
    ledger = _Ledger({"root-1": "OUTCOME_UNKNOWN", "root-2": "SUBMITTING"})
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])],
               journal=_Journal(), ledger=ledger)
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    s.service.start(ACCOUNT, "root-2", NOW + dt.timedelta(minutes=1))       # joins root-1
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.scheduled == ["root-1"]
    assert ledger.transitions == []


def test_liquidate_records_the_pending_command_and_schedules_it(tmp_path):
    ledger = _Ledger({"flatten-ui-1": "RECEIVED"})
    s = _stack(tmp_path, [_snapshot(1, [_position()])], journal=_Journal(), ledger=ledger)
    receipt = s.service.liquidate(SimpleNamespace(account_id=ACCOUNT, command_id="flatten-ui-1"))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "LIQUIDATION_PENDING")
    assert ledger.transitions[0][:3] == ("flatten-ui-1", "RECEIVED", "OUTCOME_UNKNOWN")
    assert s.scheduled == ["flatten-ui-1"]


def test_disconnect_is_outcome_unknown_and_keeps_breaker_tripped(tmp_path):
    s = _stack(tmp_path, [RuntimeError("IB down")])
    receipt = s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert receipt.state == "OUTCOME_UNKNOWN"
    assert not s.dispatch.calls
    assert s.breaker.calls


def test_timeout_never_claims_flat_or_submits_after_deadline(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    receipt = s.service.start(ACCOUNT, "root-1", NOW)
    assert receipt.state == "FAILED_SAFE"
    assert not s.dispatch.calls
    assert s.breaker.calls
    assert s.registry.get("root-1").state == "FAILED_SAFE"


def test_repeated_root_is_idempotent_while_waiting_for_broker_resolution(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    assert [call[0] for call in s.dispatch.calls] == ["reduce"]


@pytest.mark.parametrize(("quantity", "side"), [(10.0, "SELL"), (-7.0, "BUY")])
def test_reduction_never_flips_position(tmp_path, quantity, side):
    s = _stack(tmp_path, [_snapshot(1, [_position(quantity)])])
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    _kind, _conid, actual_side, actual_quantity, _child = s.dispatch.calls[0]
    assert actual_side == side
    assert actual_quantity == abs(quantity)


def test_migration_36_adds_safe_close_columns_children_and_joins(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "m.duckdb"))
    migrator = SchemaMigrator(db)
    apply_liquidation_migration(migrator)
    apply_liquidation_migration(migrator)
    assert {35, 36} <= migrator.applied_versions()
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 36", fetch="one") == ("sp1_liquidation_safe_close",)
    run_cols = {r[0] for r in db.execute("DESCRIBE liquidation_runs", fetch="all")}
    assert {"scope", "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price",
            "target_price", "remaining_quantity", "escalated", "superseded_by", "cleanup_pending"} <= run_cols
    child_cols = {r[0] for r in db.execute("DESCRIBE liquidation_children", fetch="all")}
    assert {"child_id", "owner_root_id", "kind", "attempt", "state", "fence_generation",
            "target_order_entity_id", "filled_at_send", "observed_generation", "sent_generation",
            "order_entity_id"} <= child_cols
    join_cols = {r[0] for r in db.execute("DESCRIBE liquidation_joins", fetch="all")}
    assert {"command_id", "root_id", "outcome", "requested_goal"} <= join_cols


_LEGACY_RUNS = (("old-flat", "FLAT", 1), ("old-failed", "FAILED_SAFE", 2), ("open-a", "OUTCOME_UNKNOWN", 3),
                ("open-b", "VERIFYING", 4), ("open-c", "REQUESTED", 5))


def _legacy_db(tmp_path, runs=_LEGACY_RUNS):
    """A journal as it was before SP1: migration 25 only, with old runs (root, state, minute)."""
    db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
    migrator = SchemaMigrator(db)
    migrator.apply(25, "p1_liquidation_runs", ("""CREATE TABLE IF NOT EXISTS liquidation_runs (
        cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL, state VARCHAR NOT NULL,
        deadline TIMESTAMPTZ NOT NULL, generation_id BIGINT, detail VARCHAR NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL)""",))
    for root, state, minute in runs:
        db.execute("INSERT INTO liquidation_runs VALUES (?, ?, ?, ?, 1, 'x', ?)",
                   [root, ACCOUNT, state, DEADLINE, NOW + dt.timedelta(minutes=minute)], fetch="none")
    return db


def test_migration_36_adopts_runs_that_were_open_before_the_upgrade(tmp_path):
    """D8: every old run gets a join row; the oldest open run owns the account; the rest are superseded."""
    db = _legacy_db(tmp_path)
    apply_liquidation_migration(SchemaMigrator(db))
    store, registry = LiquidationRunStore(db), ExitOwnerRegistry(db)
    assert all(store.root_for(r) == r for r in ("old-flat", "old-failed", "open-a", "open-b", "open-c"))
    assert registry.account_owner(ACCOUNT).root_id == "open-a"
    assert (registry.get("open-b").state, registry.get("open-c").state) == ("SUPERSEDED", "SUPERSEDED")
    assert registry.get("old-flat") is None and registry.get("old-failed") is None
    assert (store.receipt("open-a").phase, store.receipt("open-b").state,
            store.receipt("open-b").superseded_by) == ("legacy", "SUPERSEDED", "open-a")


def test_an_adopted_run_settles_its_old_reduce_before_any_new_reduce_across_two_restarts(tmp_path):
    """R2-5 / R29: the old service may have sent a reduce; it is an UNKNOWN child, never 'nothing sent'."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(5, [_position()])])
    s.service.rescan()                                      # adopt: old reduce of conid 1 is UNKNOWN
    adopted = s.service.receipt_for("open-a")
    assert {(c.child_id, c.state, c.sent_generation) for c in adopted.children} == {
        (child, "UNKNOWN", 5) for child in ("open-a-liquidation-reduce-*", "open-b-liquidation-reduce-*",
                                            "open-c-liquidation-reduce-*", "old-failed-liquidation-reduce-*",
                                            "old-flat-liquidation-reduce-*")}     # ruling 49: FLAT too
    s.service.rescan()
    s.restart()
    s.push(_snapshot(6, [_position()]))
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Submitted", entity="open-a-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail
    s.restart()
    s.push(_snapshot(7, []), _snapshot(8, []))
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.dispatch.calls == []                           # the old reduce closed it; nothing new was sent
    assert s.registry.account_owner(ACCOUNT) is None and s.registry.get("open-b").state == "SUPERSEDED"
    assert s.service.root_for("open-b") == "open-b"         # a retry of the superseded command is bound
    assert s.service.close_resolution("open-b").root_id == "open-a"
    resolution = s.service.close_resolution("open-a")              # the old command resolves, with what sold
    assert (resolution.success, resolution.outcome["filled_quantity"]) == (True, 10.0)


def test_an_invisible_old_reduce_blocks_the_adopted_run_until_its_deadline(tmp_path):
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()])])
    s.dispatch.complete = False                             # nothing proves the old reduce absent
    s.service.rescan()
    assert "outcome unknown" in s.service.rescan().detail
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == []


def test_an_old_reduce_proven_absent_lets_the_adopted_run_close_the_position(tmp_path):
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    s.service.rescan()                                      # adopt, fenced on 5
    assert s.service.rescan().children[0].state == "ABSENT"  # generation 6: complete, newer, no row
    s.service.rescan()                                      # generation 7 is newer than that observation
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "open-a-reduce-1-1")]


def test_store_round_trips_a_run_with_children(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    receipt = LiquidationReceipt(ACCOUNT, "root-7", "REPROTECTING", DEADLINE, generation_id=3, detail="x",
                                 scope="conid", conid=1, goal="partial", goal_quantity=4.0, phase="reprotect",
                                 opened_generation=1, stop_price=95.0, target_price=120.0, escalated=True)
    child = ChildRef("root-7-reprotect-stop-1-1", "root-7", "root-7", ACCOUNT, 1, "reprotect-stop", 1,
                     "UNKNOWN", 3, side="SELL", quantity=6.0, price=95.0, oca_group="root-7-reprotect-1-1",
                     sent_generation=4, order_entity_id="root-7-reprotect-stop-1-1:stop")

    def write(conn):
        s.store.insert_run_in_tx(conn, receipt, NOW)
        s.store.insert_child_in_tx(conn, child, NOW)
    s.store.transaction(write)
    assert s.store.receipt("root-7") == LiquidationReceipt(**{**receipt.__dict__, "children": (child,)})


def test_store_refuses_to_overwrite_a_terminal_or_superseded_run_or_lower_a_goal(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    for state in ("SUPERSEDED", "FLAT"):
        root = f"r-{state}"
        s.store.transaction(lambda conn: s.store.insert_run_in_tx(
            conn, LiquidationReceipt(ACCOUNT, root, state, DEADLINE), NOW))
        with pytest.raises(RunStateError):
            s.store.transaction(lambda conn: s.store.update_run_in_tx(
                conn, LiquidationReceipt(ACCOUNT, root, "CANCELLING", DEADLINE), NOW))
        assert s.store.receipt(root).state == state
    s.store.transaction(lambda conn: s.store.insert_run_in_tx(
        conn, LiquidationReceipt(ACCOUNT, "r-zero", "VERIFYING", DEADLINE, scope="conid", conid=1), NOW))
    with pytest.raises(RunStateError):                       # a goal never goes back to partial
        s.store.transaction(lambda conn: s.store.update_run_in_tx(conn, LiquidationReceipt(
            ACCOUNT, "r-zero", "VERIFYING", DEADLINE, scope="conid", conid=1, goal="partial"), NOW))


def test_two_account_producers_on_one_generation_make_one_root_and_one_reduce(tmp_path):
    """R10 / #23: the second flatten joins the first and creates nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    first = s.service.start(ACCOUNT, "flat-a", DEADLINE)
    joined = s.service.start(ACCOUNT, "flat-b", DEADLINE)
    assert joined.cause_command_id == "flat-a"
    assert s.service.receipt_for("flat-b") is None
    assert s.service.root_for("flat-b") == "flat-a"
    assert first.cause_command_id == "flat-a"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_unseen_child_without_a_complete_enumeration_never_gets_a_second_reduce(tmp_path):
    """D1 (reverses round 1): no row and no complete enumeration keeps the child UNKNOWN to the deadline."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]),
                          _snapshot(3, [_position()]), _snapshot(4, [_position()])])
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    for _ in range(3):
        receipt = s.service.rescan()
        assert "outcome unknown" in receipt.detail
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_invisible_child_on_a_generation_opened_before_its_send_stays_unknown(tmp_path):
    """#21: generation 2 began before the send, so its empty lookup proves nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.dispatch.staging = 1                                    # a sync opened generation 2 before the send
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    s.dispatch.staging = 0
    assert s.service.receipt_for("flat-1").children[0].sent_generation == 2
    receipt = s.service.rescan()
    assert (receipt.state, receipt.children[0].state) == ("VERIFYING", "UNKNOWN")
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_absence_needs_a_complete_enumeration_opened_after_the_send(tmp_path):
    """D1: absence is proven only by a complete generation newer than the send fence; then a new attempt."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]),
                          _snapshot(3, [_position()]), _snapshot(4, [_position()])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)                       # sent fence 2
    s.dispatch.staging = 0
    assert s.service.rescan().children[0].state == "UNKNOWN"           # generation 2 is not newer
    receipt = s.service.rescan()                                       # generation 3: complete, nothing
    assert receipt.children[0].state == "ABSENT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]              # ABSENT may have filled: wait
    s.service.rescan()                                                 # generation 4 is newer
    assert [c[4] for c in s.dispatch.calls] == ["flat-1-reduce-1-1", "flat-1-reduce-1-2"]


def test_filled_callback_after_position_capture_never_allows_a_second_reduce(tmp_path):
    """#21: the fill is seen after the snapshot that still shows the position."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    assert s.service.rescan().state == "VERIFYING"            # generation 2: position still 10, fill just seen
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    assert s.service.rescan().state == "FLAT"                 # generation 3 proves it
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_terminal_status_counts_on_the_same_generation(tmp_path):
    """D2: a Filled row is evidence at once; no newer generation is needed to see it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(1, [_position()]), _snapshot(2, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0, entity="flat-1-reduce-1-1:exit")]
    receipt = s.service.rescan()                               # still generation 1
    assert (receipt.children[0].state, receipt.children[0].observed_generation) == ("FILLED", 1)
    assert s.service.rescan().state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_own_working_reduce_is_never_cancelled_by_its_root(tmp_path):
    """#16: the root's own reduce shows up as a working order; it blocks, it is not cancelled."""
    own = _order("flat-1-reduce-1-1:exit", group="flat-1-reduce-1-1", leg="exit", order_type="MKT")
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [own]),
                          _snapshot(3, []), _snapshot(4, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted", entity=own.order_entity_id)]
    assert "still working" in s.service.rescan().detail
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0, entity=own.order_entity_id)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_known_working_order_is_cancelled_while_another_child_is_unknown(tmp_path):
    """#21: an unknown outcome forbids a new reduce, never a cancel of an identified working order."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [_order("ext-2")])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.staging = 0
    receipt = s.service.rescan()
    assert "outcome unknown" in receipt.detail
    assert [c[0:2] for c in s.dispatch.calls] == [("reduce", 1), ("cancel", "ext-2")]


def test_hand_over_is_updated_before_every_cancel_batch(tmp_path):
    """D6: an order that appears later is handed over before its cancel, so the saga expects it."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()], [_order("ext-2")])],
               protection=protection)
    protection.calls = s.dispatch.calls                        # one log for both, in call order
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.staging = 0
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["handover_account", "reduce", "handover_account", "cancel"]
    assert s.dispatch.calls[2] == ("handover_account", "flat-1", ("ext-2",))


def test_failed_safe_root_does_not_block_rescan_of_a_newer_root(tmp_path):
    """R19: the new root advances; the old root stays FAILED_SAFE."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.service.start(ACCOUNT, "old", NOW)                       # deadline already passed: no snapshot consumed
    assert s.service.receipt_for("old").state == "FAILED_SAFE"
    s.service.start(ACCOUNT, "new", DEADLINE)                  # consumes generation 1: reduce
    advanced = s.service.rescan()                              # consumes generation 2
    assert advanced.cause_command_id == "new"
    assert advanced.generation_id == 2
    assert s.service.receipt_for("old").state == "FAILED_SAFE"


def test_new_flatten_inherits_the_unknown_child_of_a_failed_safe_root(tmp_path):
    """R9: a later owner obeys R5 for the unknown children it inherits."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    s.dispatch.staging = 0
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    s.push(_snapshot(2, [_position()]))
    receipt = s.service.start(ACCOUNT, "flat-2", NOW + dt.timedelta(minutes=5))
    assert [(c.child_id, c.owner_root_id, c.state) for c in receipt.children] == [
        ("flat-1-reduce-1-1", "flat-2", "UNKNOWN")]
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_flat_releases_the_account_owner_and_a_later_flatten_claims_again_after_restart(tmp_path):
    """R8 / #23: FLAT releases the owner; after a restart a new flatten is a new root."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert s.registry.get("flat-1").state == "RELEASED"
    assert s.service.receipt_for("flat-1").cleanup_pending is False
    service = s.restart()
    s.push(_snapshot(4, [_position(5.0)]))
    receipt = service.start(ACCOUNT, "flat-2", DEADLINE)
    assert receipt.cause_command_id == "flat-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 5.0, "flat-2-reduce-1-1")


def test_crash_after_the_broker_call_never_sends_a_second_reduce(tmp_path):
    """R20: the order left, the process died before the fence was stored; the restart only observes it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    real_reduce = s.dispatch.reduce

    def sent_then_crash(*args):
        real_reduce(*args)
        raise _Crash()
    s.dispatch.reduce = sent_then_crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert s.store.receipt("flat-1").children[0].sent_generation is None
    s.dispatch.reduce = real_reduce
    service = s.restart()
    s.push(_snapshot(2, [_position()]), _snapshot(3, [_position()]), _snapshot(4, []))
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted")]
    assert "still working" in service.rescan().detail
    assert service.receipt_for("flat-1").children[0].sent_generation == 2   # fenced by the restart
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    service.rescan()                                        # generation 3: the fill is seen
    assert service.rescan().state == "FLAT"                 # generation 4
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_crash_between_journal_and_broker_call_is_proven_absent_before_a_new_attempt(tmp_path):
    """R20: the child was journaled, the broker call never ran; a complete newer enumeration proves it."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])

    def crash(*_args):
        raise _Crash()
    s.service._still_dispatchable = crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "flat-1", DEADLINE)
    service = s.restart()
    s.push(_snapshot(2, [_position()]), _snapshot(3, [_position()]), _snapshot(4, [_position()]))
    assert service.rescan().children[0].state == "UNKNOWN"     # fenced on generation 2 by the restart
    assert service.rescan().children[0].state == "ABSENT"      # generation 3 is complete and newer
    service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-1-reduce-1-2")]


def test_crash_after_a_cancel_was_journaled_sends_a_new_cancel(tmp_path):
    """#20 livelock: a cancel cut off before its send is NOT_SENT after restart; a new cancel goes out."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_order()])])

    def crash(*_args):
        raise _Crash()
    s.service._still_dispatchable = crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "root-1", DEADLINE)
    service = s.restart()
    s.push(_snapshot(2, [_position()], [_order()]))
    s.dispatch.entities["external-1"] = _order()               # the order is still working
    receipt = service.rescan()
    assert [(c.child_id, c.state) for c in receipt.children] == [
        ("root-1-cancel-1-1", "NOT_SENT"), ("root-1-cancel-1-2", "UNKNOWN")]
    assert s.dispatch.calls == [("cancel", "external-1", "root-1-cancel-1-2")]


def test_refusal_before_the_boundary_is_not_sent_and_may_be_retried_with_a_new_id(tmp_path):
    """R2/R3: only a proven pre-submit refusal is NOT_SENT; the next attempt has a new id."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.dispatch.refuse.add("reduce")
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "NOT_SENT"
    s.dispatch.refuse.clear()
    s.service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-1-reduce-1-2")]


def test_timeout_after_the_boundary_stays_unknown_and_is_never_resent(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [_position()])])
    s.dispatch.fail_after_send.add("reduce")
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert (receipt.children[0].state, receipt.children[0].sent_generation) == ("UNKNOWN", 1)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Submitted")]   # it did reach the broker
    s.service.rescan()
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_an_unexpected_send_error_is_logged_and_the_child_stays_unknown(tmp_path, caplog):
    """D13: every exception is logged; only DispatchRefused proves nothing left."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])

    def broken(*_args):
        raise AttributeError("a bug after placeOrder")
    s.dispatch.reduce = broken
    with caplog.at_level(logging.ERROR, logger="trader.trading.liquidation_service"):
        receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "UNKNOWN"
    assert any("flat-1-reduce-1-1" in r.getMessage() and r.exc_info for r in caplog.records)


def test_a_root_superseded_between_journal_and_send_sends_nothing(tmp_path):
    """R7: the owner is re-read right before the broker call."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    real = s.service._still_dispatchable

    def superseded_first(root_id, goal):
        s.db.execute("UPDATE exit_owners SET state = 'SUPERSEDED' WHERE root_id = ?", [root_id], fetch="none")
        return real(root_id, goal)
    s.service._still_dispatchable = superseded_first
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "NOT_SENT"
    assert s.dispatch.calls == []


@pytest.mark.parametrize("step", ["terminal", "protection"])
def test_restart_after_a_crash_at_the_end_finishes_the_root(tmp_path, step):
    """R8 / R20 / D3: terminal state and owner release commit together; the saga step is recovered."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=protection)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    if step == "terminal":
        real = s.registry.finish_in_tx

        def crash_on_release(conn, root_id, state, now):
            raise _Crash()
        s.registry.finish_in_tx = crash_on_release
    else:
        protection.crash_on.add("close_after_full")
    with pytest.raises(_Crash):
        s.service.rescan()
    stored = s.store.receipt("flat-1")
    if step == "terminal":                                   # the whole terminal transaction rolled back
        assert (stored.state, s.registry.get("flat-1").state) == ("VERIFYING", "ACTIVE")
    else:                                                    # terminal and release committed together
        assert (stored.state, stored.cleanup_pending, s.registry.get("flat-1").state) == ("FLAT", True, "RELEASED")
    service = s.restart()
    service.rescan()
    assert (s.store.receipt("flat-1").state, s.store.receipt("flat-1").cleanup_pending) == ("FLAT", False)
    assert s.registry.get("flat-1").state == "RELEASED"
    assert ("close_after_full", "flat-1") in protection.calls


def test_a_flatten_during_unfinished_cleanup_starts_a_new_root(tmp_path):
    """D3: a finished root no longer owns the account, even before its cleanup ran."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, []),
                          _snapshot(4, [_position(3.0)])], protection=protection)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    protection.crash_on.add("close_after_full")
    with pytest.raises(_Crash):
        s.service.rescan()
    receipt = s.service.start(ACCOUNT, "flat-2", DEADLINE)
    assert receipt.cause_command_id == "flat-2"
    assert s.service.root_for("flat-2") == "flat-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "flat-2-reduce-1-1")


# ---------------------------------------------------------------------------
# Kept from master (PR #42): the timed lock guards a caller that bypasses the worker
# ---------------------------------------------------------------------------

class _FirstCaptureBlocks(_Broker):
    """The first capture waits for ``release``; later captures return at once."""
    def __init__(self, snapshots):
        super().__init__(snapshots)
        self.first_entered, self.release = threading.Event(), threading.Event()

    def capture(self, account_id):
        if self.calls == 0:
            self.first_entered.set()
            assert self.release.wait(5.0)
        return super().capture(account_id)


def _hold_lock(service):
    """Hold the service lock from another thread until ``release`` is set."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with service._lock:
            held.set()
            release.wait(5.0)
    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(2.0)
    return holder, release


def test_start_and_rescan_serialize_across_threads(tmp_path):
    s = _stack(tmp_path, [])
    s.broker = _FirstCaptureBlocks([_snapshot(1, [_position()])])
    s.dispatch = _Dispatch(s.broker)
    s.service = s._build()
    results = {}
    a = threading.Thread(target=lambda: results.setdefault("a", s.service.start(ACCOUNT, "root-1", DEADLINE)))
    a.start()
    assert s.broker.first_entered.wait(2.0)
    b = threading.Thread(target=lambda: results.setdefault("b", s.service.rescan()))
    b.start()
    b.join(0.2)              # without the lock B would size a reduce now, while A is still capturing
    s.broker.release.set()
    a.join(2.0)
    b.join(2.0)
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    assert results["a"].state == results["b"].state == "VERIFYING"


def test_busy_start_commits_its_claim_and_a_later_rescan_advances_it(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])], lock_timeout_seconds=0.05)
    holder, release = _hold_lock(s.service)
    try:
        started = time.monotonic()
        with pytest.raises(LiquidationBusy):
            s.service.start(ACCOUNT, "saga-root", DEADLINE)
        with pytest.raises(LiquidationBusy):
            s.service.rescan()
        assert time.monotonic() - started < 1.0
        assert s.dispatch.calls == []
    finally:
        release.set()
        holder.join(2.0)
    assert s.registry.account_owner(ACCOUNT).root_id == "saga-root"
    receipt = s.service.rescan()
    assert (receipt.cause_command_id, receipt.state) == ("saga-root", "VERIFYING")
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_busy_flatten_records_the_pending_root_and_surfaces_busy(tmp_path):
    ledger = _Ledger({"flatten-1": "RECEIVED"})
    s = _stack(tmp_path, [_snapshot(1, [_position()])], journal=_Journal(), ledger=ledger,
               lock_timeout_seconds=0.05)
    holder, release = _hold_lock(s.service)
    try:
        with pytest.raises(LiquidationBusy):
            s.service.liquidate(SimpleNamespace(account_id=ACCOUNT, command_id="flatten-1"))
    finally:
        release.set()
        holder.join(2.0)
    assert ledger.transitions[0][:3] == ("flatten-1", "RECEIVED", "OUTCOME_UNKNOWN")
    assert ledger.transitions[0][3]["outcome"]["liquidation_state"] == "REQUESTED"
    assert s.scheduled == ["flatten-1"]
    assert s.service.rescan().state == "VERIFYING"


def test_root_bound_to_one_account_rejects_another_account(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    with pytest.raises(ValueError):
        s.service.start("DU999", "root-1", DEADLINE)


def test_rescan_returns_none_when_only_failed_safe_roots_remain(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "old", NOW)                       # deadline already passed: FAILED_SAFE
    captured = s.broker.calls
    assert s.service.rescan() is None
    assert s.broker.calls == captured                          # nothing left to advance: no capture


# ---------------------------------------------------------------------------
# Round-2 verification fixes (N2, R2-5): every pre-upgrade run's old reduce is tracked
# ---------------------------------------------------------------------------

_OLD_REDUCES = {"open-a-liquidation-reduce-*", "open-b-liquidation-reduce-*",
                "open-c-liquidation-reduce-*", "old-failed-liquidation-reduce-*",
                "old-flat-liquidation-reduce-*"}        # ruling 49: an old FLAT run is tracked too


def test_a_superseded_legacy_runs_invisible_reduce_blocks_the_adopted_root(tmp_path):
    """N2: before the upgrade two flattens could each send a reduce. The adopted root settles its
    own old reduce, but the superseded and FAILED_SAFE runs' old reduces still block a new one."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    s.dispatch.complete = False                              # nothing proves an unseen old reduce absent
    s.dispatch.rows["open-a-liquidation-reduce-1"] = [_row("Cancelled", entity="open-a-liquidation-reduce-1:exit")]
    s.service.rescan()                                       # adopt at 5
    assert {c.child_id for c in s.service.receipt_for("open-a").children} == _OLD_REDUCES
    s.service.rescan()                                       # 6: open-a's old reduce is CANCELLED
    s.service.rescan()                                       # 7: the others are still unknown
    assert s.dispatch.calls == []
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == []


def test_a_legacy_run_whose_deadline_passed_fails_safe_and_a_new_flatten_inherits_its_old_reduces(tmp_path):
    """R2-5 gap: a real upgrade finds the old deadline passed. Nothing is sent, the breaker trips,
    and the next flatten inherits every old reduce as UNKNOWN before it may send its own."""
    _legacy_db(tmp_path)
    s = _stack(tmp_path, [_snapshot(5, [_position()])])
    s.dispatch.complete = False
    s.clock["now"] = DEADLINE + dt.timedelta(minutes=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == [] and s.breaker.calls
    assert s.registry.get("open-a").state == "FAILED_SAFE"
    receipt = s.service.start(ACCOUNT, "flat-new", s.clock["now"] + dt.timedelta(minutes=5))
    assert {(c.child_id, c.owner_root_id, c.state) for c in receipt.children} == {
        (child, "flat-new", "UNKNOWN") for child in _OLD_REDUCES}
    assert s.dispatch.calls == []


def test_old_failed_safe_runs_reduces_are_tracked_even_with_no_run_open_at_the_upgrade(tmp_path):
    """N2: only FAILED_SAFE runs survived the upgrade; the first flatten after it still tracks them."""
    db = _legacy_db(tmp_path)
    db.execute("DELETE FROM liquidation_runs WHERE cause_command_id LIKE 'open-%' OR cause_command_id = 'old-flat'",
               fetch="none")
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    receipt = s.service.start(ACCOUNT, "flat-new", DEADLINE)
    assert [(c.child_id, c.state) for c in receipt.children] == [("old-failed-liquidation-reduce-*", "UNKNOWN")]
    assert s.dispatch.calls == []
    s.service.rescan()                                       # 6: complete and newer, no row: ABSENT
    s.service.rescan()                                       # 7: newer than that observation: reduce
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "flat-new-reduce-1-1")]


# ---------------------------------------------------------------------------
# Round-3 fix (#20, ruling 42): a pre-upgrade run's reduces are one wildcard child
# ---------------------------------------------------------------------------

def _pre_sp1_open(db, run_id):
    return db.execute("SELECT pre_sp1_open FROM liquidation_runs WHERE cause_command_id = ?",
                      [run_id], fetch="one")[0]


def test_an_old_reduce_of_a_flat_position_blocks_the_adopted_run_until_it_settles(tmp_path):
    """Ruling 42: the old reduce of a position already at 0 is not in working_orders yet. The run's
    wildcard child blocks; its mark stays until a complete, newer enumeration settles it."""
    _legacy_db(tmp_path, (("flat-1", "VERIFYING", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)])])
    s.dispatch.complete = False                              # the old order is not visible yet
    s.service.rescan()                                       # adopt at 5
    receipt = s.service.rescan()                             # 6
    assert [(c.child_id, c.conid, c.state) for c in receipt.children] == [
        ("flat-1-liquidation-reduce-*", None, "UNKNOWN")]
    assert receipt.state not in ("FLAT", "CLOSED") and "outcome unknown" in receipt.detail
    assert _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(7, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Submitted", entity="flat-1-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail     # visible and working: waited on, not cancelled
    assert _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(8, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Cancelled", entity="flat-1-liquidation-reduce-1:exit")]
    s.dispatch.complete = True
    assert s.service.rescan().state == "FLAT"               # 8: complete and newer than the fence
    assert s.service.receipt_for("flat-1").children[0].state == "CANCELLED"
    assert not _pre_sp1_open(s.db, "flat-1")
    assert s.dispatch.calls == []


def test_an_old_reduce_that_stays_invisible_blocks_to_the_deadline(tmp_path):
    _legacy_db(tmp_path, (("flat-1", "VERIFYING", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)]),
                          _snapshot(7, [_position(0.0)])])
    s.dispatch.complete = False
    s.service.rescan()
    s.service.rescan()
    assert "outcome unknown" in s.service.rescan().detail
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert s.dispatch.calls == [] and _pre_sp1_open(s.db, "flat-1")


# ---------------------------------------------------------------------------
# Round-3 fix (#20, ruling 43): a fill fence outlives the root that observed it
# ---------------------------------------------------------------------------

def test_a_fill_seen_on_the_deadline_tick_fences_the_next_root(tmp_path):
    """Ruling 43: root-1's reduce sold 6 on the tick its deadline passed; the position cache still says 10.
    root-2 claimed on that generation sends nothing; a newer generation shows the real remainder."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Cancelled", filled=6.0)]
    s.push(_snapshot(2, [_position()]))
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"         # gen 2: the fill is observed, then the deadline
    receipt = s.service.start(ACCOUNT, "root-2", s.clock["now"] + dt.timedelta(minutes=5))
    assert "newer than the last observed fill" in receipt.detail
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "root-1-reduce-1-1")]
    s.push(_snapshot(3, [_position(4.0)]))
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 4.0, "root-2-reduce-1-1")


def test_a_settled_legacy_fill_fences_the_next_root_across_a_restart(tmp_path):
    """Ruling 43: the old run's late reduce filled 10 of 20; the root that settled it dies at its deadline.
    After a restart the next root waits for a generation newer than that fill."""
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(20.0)])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Filled", filled=10.0)]
    s.push(_snapshot(6, [_position(20.0)]))
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert not _pre_sp1_open(s.db, "flat-1")                 # settled FILLED on generation 6
    s.restart()
    receipt = s.service.start(ACCOUNT, "root-2", s.clock["now"] + dt.timedelta(minutes=5))
    assert "newer than the last observed fill" in receipt.detail and s.dispatch.calls == []
    s.push(_snapshot(7, [_position(10.0)]))
    s.service.rescan()
    assert s.dispatch.calls == [("reduce", 1, "SELL", 10.0, "root-2-reduce-1-1")]


# ---------------------------------------------------------------------------
# Round-4 fixes (#20): legacy FLAT runs and late fills of terminal children
# ---------------------------------------------------------------------------

def test_an_old_flat_runs_late_reduce_blocks_a_new_flatten(tmp_path):
    """Ruling 49: master wrote FLAT from an empty snapshot, which does not prove an earlier reduce will not
    arrive late. The old FLAT run is tracked: its late reduce blocks every new reduce on the account."""
    _legacy_db(tmp_path, (("old-flat", "FLAT", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()]), _snapshot(7, [_position()])])
    s.dispatch.complete = False                              # the late reduce is not visible yet
    receipt = s.service.start(ACCOUNT, "flat-new", DEADLINE)
    assert [(c.child_id, c.state) for c in receipt.children] == [("old-flat-liquidation-reduce-*", "UNKNOWN")]
    s.dispatch.rows["old-flat-liquidation-reduce-1"] = [_row("Submitted", entity="old-flat-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail     # visible now: waited on, never cancelled
    s.service.rescan()
    assert s.dispatch.calls == []


def test_a_cancelled_child_that_reports_a_late_fill_fences_the_next_root(tmp_path):
    """Ruling 50: the order was reported Cancelled with 0 filled, then the broker reported 4 filled while
    the position cache still said 10. The next root waits for a newer generation, then sells 6, not 10."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_order()])])
    s.service.start(ACCOUNT, "root-1", DEADLINE)                                    # gen 1: cancel the order
    s.dispatch.entities["external-1"] = _row("Cancelled", filled=0.0)
    s.push(_snapshot(2, [_position()]))
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"         # gen 2: cancel CANCELLED with 0, then the deadline
    s.dispatch.entities["external-1"] = _row("Cancelled", filled=4.0)
    s.push(_snapshot(3, [_position()]))
    receipt = s.service.start(ACCOUNT, "root-2", s.clock["now"] + dt.timedelta(minutes=5))
    assert "newer than the last observed fill" in receipt.detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]    # no reduce sized from the stale 10
    cancel = next(c for c in s.service.receipt_for("root-1").children if c.kind == "cancel")
    assert (cancel.state, cancel.filled_quantity, cancel.observed_generation) == ("CANCELLED", 4.0, 3)
    s.push(_snapshot(4, [_position(6.0)]))
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "root-2-reduce-1-1")
```

Append to `tests/test_command_stack.py`:

```python
def test_enabled_stack_applies_safe_close_migrations(tmp_path):
    from trader.trading.command_stack import build_command_stack

    trader = _trader(tmp_path)
    build_command_stack(trader, _policy(), now=lambda: NOW)
    versions = {r[0] for r in trader.journal_db.execute("SELECT version FROM schema_migrations", fetch="all")}
    assert {35, 36} <= versions
```

Append to `tests/test_order_correlation.py` (and add `legacy_reduce_prefix, matches_legacy_reduce` to its `trader.trading.order_correlation` import):

```python
def test_a_legacy_reduce_prefix_matches_every_conid_of_its_run_only():
    """Ruling 42: {run}-liquidation-reduce-{conid}, any conid; never another run or a new child."""
    prefix = legacy_reduce_prefix("flat-1")
    assert matches_legacy_reduce("flat-1-liquidation-reduce-265598", prefix)
    assert matches_legacy_reduce("flat-1-liquidation-reduce-1", prefix)
    assert not matches_legacy_reduce("flat-10-liquidation-reduce-1", prefix)
    assert not matches_legacy_reduce("flat-1-liquidation-reduce-1-2", prefix)
    assert not matches_legacy_reduce("flat-1-liquidation-reduce-", prefix)
    assert not matches_legacy_reduce(None, prefix)
```

Master's real-loop tests and the saga's busy-lock test move to the journal service (ruling 32):

```diff
diff --git a/tests/automation/test_protective_order_saga.py b/tests/automation/test_protective_order_saga.py
index eed23969..f41a8768 100644
--- a/tests/automation/test_protective_order_saga.py
+++ b/tests/automation/test_protective_order_saga.py
@@ -802,7 +802,10 @@ def test_busy_liquidation_keeps_protective_failure_root_for_rescan(tmp_path):
     import threading
 
     from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
-    from trader.trading.liquidation_service import LiquidationBusy, LiquidationService
+    from trader.trading.exit_owner import ExitOwnerRegistry
+    from trader.trading.liquidation_service import (
+        LiquidationBusy, LiquidationRunStore, LiquidationService, apply_liquidation_migration,
+    )
 
     position = BrokerPositionRow(
         account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART",
@@ -816,10 +819,15 @@ def test_busy_liquidation_keeps_protective_failure_root_for_rescan(tmp_path):
         positions=(position,), working_orders=(),
     )
     reduces = []
+    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
+    apply_liquidation_migration(SchemaMigrator(db))
     liquidation = LiquidationService(
         SimpleNamespace(capture=lambda account_id: snapshot),
-        SimpleNamespace(reduce=lambda *args: reduces.append(args), cancel=lambda *args: None),
-        now=lambda: NOW, lock_timeout_seconds=0.05,
+        SimpleNamespace(reduce=lambda *args: reduces.append(args), cancel=lambda *args: None,
+                        find_orders=lambda *args: [], get_order=lambda entity: None,
+                        enumeration_complete=lambda: True, newest_generation=lambda: 1),
+        store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db), now=lambda: NOW,
+        lock_timeout_seconds=0.05,
     )
     saga, intent, state, breaker, _, _ = _started(tmp_path, liquidation=liquidation)
     og = state.order_group_id
diff --git a/tests/test_trader_service_loops.py b/tests/test_trader_service_loops.py
index 5fb98a6c..bcc45939 100644
--- a/tests/test_trader_service_loops.py
+++ b/tests/test_trader_service_loops.py
@@ -5,9 +5,10 @@ loop, and ``reduce_position`` then waited on that same loop: a timeout, then a
 late order. The ticks now run on the single liquidation worker thread while
 the loop stays free to place the order.
 
-Wiring: a real ``LiquidationService`` -> ``command_stack._LiquidationDispatch``
--> ``TradingRuntimeOrderDispatch`` -> a fake trader whose
-``place_reduce_only_order`` records which thread ran it and when.
+Wiring: a real ``LiquidationService`` on a DuckDB journal ->
+``command_stack._LiquidationDispatch`` -> ``TradingRuntimeOrderDispatch`` ->
+a fake trader whose ``place_reduce_only_order`` records which thread ran it
+and when. Broker evidence (order rows, generations) is answered by the test.
 """
 from __future__ import annotations
 
@@ -25,8 +26,13 @@ import pytest
 from trader import trader_service
 from trader.common.reactivex import SuccessFail
 from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
+from trader.data.duckdb_store import DuckDBConnection
+from trader.data.schema_migrations import SchemaMigrator
 from trader.trading.command_stack import _LiquidationDispatch
-from trader.trading.liquidation_service import LiquidationReceipt, LiquidationService
+from trader.trading.exit_owner import ExitOwnerRegistry
+from trader.trading.liquidation_service import (
+    LiquidationRunStore, LiquidationService, apply_liquidation_migration,
+)
 from trader.trading.trading_runtime import TradingRuntimeOrderDispatch
 
 UTC = dt.timezone.utc
@@ -68,23 +74,32 @@ class _FakeTrader:
         return SuccessFail.success(obj=[])
 
 
-class _ResumeStore:
-    """A durable run left REQUESTED by a previous process."""
-    def __init__(self, deadline):
-        self._deadline = deadline
+class _EvidenceDispatch(TradingRuntimeOrderDispatch):
+    """The real order dispatch; the fake trader has no broker store, so the test answers the evidence."""
+    def find_by_order_ref(self, account_id, order_ref):
+        return []
 
-    def load_unresolved(self):
-        return [LiquidationReceipt(ACCOUNT, "root-1", "REQUESTED", self._deadline)]
+    def enumeration_complete(self):
+        return True
 
-    def save(self, receipt, now):
-        pass
+    def newest_generation(self):
+        return 1
 
 
-def _liquidation(trader, *, resume=True, clock=None):
+def _liquidation(trader, tmp_path, *, resume=True, clock=None):
+    """``resume``: a run a previous process claimed and left REQUESTED."""
     clock = clock or [FLATTEN_TIME]
-    dispatch = _LiquidationDispatch(TradingRuntimeOrderDispatch(trader, dispatch_timeout=0.5))
-    store = _ResumeStore(clock[0] + dt.timedelta(minutes=5)) if resume else None
-    return LiquidationService(_Broker(), dispatch, now=lambda: clock[0], store=store)
+    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
+    apply_liquidation_migration(SchemaMigrator(db))
+    dispatch = _LiquidationDispatch(_EvidenceDispatch(trader, dispatch_timeout=0.5),
+                                    SimpleNamespace(get_order=lambda entity: None))
+    service = LiquidationService(_Broker(), dispatch, store=LiquidationRunStore(db),
+                                 registry=ExitOwnerRegistry(db), now=lambda: clock[0])
+    if resume:
+        deadline = clock[0] + dt.timedelta(minutes=5)
+        LiquidationRunStore(db).transaction(
+            lambda conn: service._claim_account_in_tx(conn, ACCOUNT, "root-1", deadline))
+    return service
 
 
 @pytest.fixture
@@ -108,9 +123,9 @@ def _close_loop(loop):
 # ---------------------------------------------------------------------------
 
 @pytest.mark.asyncio
-async def test_liquidation_recovery_tick_on_real_loop_places_exactly_one_order(worker):
+async def test_liquidation_recovery_tick_on_real_loop_places_exactly_one_order(tmp_path, worker):
     trader = _FakeTrader(asyncio.get_running_loop())
-    service = _liquidation(trader)
+    service = _liquidation(trader, tmp_path)
 
     started = time.monotonic()
     receipt = await trader_service._liquidation_recovery_tick(service, worker)
@@ -161,7 +176,7 @@ def _session_controller(tmp_path: Path, liquidation, clock):
 async def test_session_controller_tick_flatten_on_real_loop_does_not_block_loop(tmp_path, worker):
     clock = [FLATTEN_TIME]
     trader = _FakeTrader(asyncio.get_running_loop(), send_seconds=0.1)
-    liquidation = _liquidation(trader, resume=False, clock=clock)
+    liquidation = _liquidation(trader, tmp_path, resume=False, clock=clock)
     controller = _session_controller(tmp_path, liquidation, clock)
     controller.recover(dt.datetime(2026, 7, 17, 11, 0, tzinfo=ET))
 
@@ -222,11 +237,11 @@ async def test_watched_loop_logs_critical_and_never_stacks_worker_calls(monkeypa
 # Startup recovery (before trader.run())
 # ---------------------------------------------------------------------------
 
-def test_startup_liquidation_recovery_dispatches_while_loop_runs(worker):
+def test_startup_liquidation_recovery_dispatches_while_loop_runs(tmp_path, worker):
     loop = asyncio.new_event_loop()
     try:
         trader = _FakeTrader(loop)
-        service = _liquidation(trader)
+        service = _liquidation(trader, tmp_path)
         holder = SimpleNamespace(liquidation_service=service)
 
         started = time.monotonic()
@@ -236,25 +251,24 @@ def test_startup_liquidation_recovery_dispatches_while_loop_runs(worker):
         assert len(trader.orders) == 1
         assert trader.orders[0][1] < returned
         assert returned - started < 0.5
-        assert service._runs["root-1"].state == "VERIFYING"
+        assert service.receipt_for("root-1").state == "VERIFYING"
     finally:
         _close_loop(loop)
 
 
-def test_startup_liquidation_recovery_without_trader_loop_sends_nothing_late(worker):
+def test_startup_liquidation_recovery_without_trader_loop_sends_nothing_late(tmp_path, worker):
     loop = asyncio.new_event_loop()
     try:
         trader = _FakeTrader(None)  # e.g. the fake-broker path: _main_loop not set yet
-        service = _liquidation(trader)
+        service = _liquidation(trader, tmp_path)
         holder = SimpleNamespace(liquidation_service=service)
 
         started = time.monotonic()
         trader_service._maybe_start_liquidation_recovery(holder, loop, worker)
         assert time.monotonic() - started < 0.5
 
-        receipt = service._runs["root-1"]
-        assert receipt.state == "OUTCOME_UNKNOWN"
-        assert "not running" in receipt.detail
+        [child] = service.receipt_for("root-1").children
+        assert (child.kind, child.state) == ("reduce", "UNKNOWN")
         trader._main_loop = loop
         loop.run_until_complete(asyncio.sleep(0.05))
         assert trader.orders == []
@@ -267,7 +281,7 @@ def test_startup_session_recovery_flattens_while_loop_runs(tmp_path, worker, mon
     loop = asyncio.new_event_loop()
     try:
         trader = _FakeTrader(loop)
-        liquidation = _liquidation(trader, resume=False, clock=clock)
+        liquidation = _liquidation(trader, tmp_path, resume=False, clock=clock)
         controller = _session_controller(tmp_path, liquidation, clock)
         monkeypatch.setattr(trader_service, "dt", SimpleNamespace(
             datetime=_FrozenDatetime, timezone=dt.timezone, timedelta=dt.timedelta))
@@ -276,7 +290,7 @@ def test_startup_session_recovery_flattens_while_loop_runs(tmp_path, worker, mon
         trader_service._maybe_start_session_recovery(holder, loop, worker)
 
         assert len(trader.orders) == 1
-        assert liquidation._runs[controller.flatten_command_id(ACCOUNT, SESSION_DATE)].state == "VERIFYING"
+        assert liquidation.receipt_for(controller.flatten_command_id(ACCOUNT, SESSION_DATE)).state == "VERIFYING"
     finally:
         _close_loop(loop)
 
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_command_stack.py tests/test_trader_service_loops.py tests/automation/test_protective_order_saga.py -q --timeout=30`
Expected: FAIL at import — `ImportError: cannot import name 'ChildRef' from 'trader.trading.liquidation_service'`.

- [ ] **Step 3: Implement the module**

Replace the whole of `trader/trading/liquidation_service.py` with:

```python
"""Broker-verified liquidation and safe close state machine.

Order acknowledgements are *evidence of uncertainty*, never evidence that a
position is gone. Every child order is journaled before the broker call
(write-ahead) and fenced on the newest broker generation right after it. A
child becomes terminal only from its own broker row, or absent only when a
complete broker enumeration opened after that fence does not show it. A
further reduce is sized only from a broker generation newer than the last
fill this root observed.

Scopes: ``account`` (flatten everything; session flatten, /flatten,
protective failure, kill) and ``conid`` (close one position, fully or in
part, with protection hand-over and a linked re-protect for a partial).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.trading.exit_owner import (
    CLAIMED, JOINED_FLATTEN, STATE_ACTIVE, STATE_FAILED_SAFE, STATE_RELEASED, ExitOwnerRegistry,
    apply_exit_owner_migration,
)
from trader.trading.order_correlation import legacy_reduce_prefix, liquidation_child_id, liquidation_child_kind

log = logging.getLogger(__name__)

LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION = 36

RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "REDUCE_FAILED", "SUPERSEDED", "FAILED_SAFE"})
SUCCESS_STATES = frozenset({"FLAT", "CLOSED", "DONE"})
# Proven end states: the scope is closed or protected again, so the owner is released.
OWNER_RELEASED_STATES = SUCCESS_STATES | {"REDUCE_FAILED"}
_SUCCESS_FOR_GOAL = {
    "account": frozenset({"FLAT"}),
    "zero": frozenset({"CLOSED", "FLAT"}),
    "partial": frozenset({"DONE", "CLOSED", "FLAT"}),
}
_FAILURE_CODES = {"FAILED_SAFE": "CLOSE_FAILED_SAFE", "REDUCE_FAILED": "REDUCE_FAILED"}

CHILD_STATES = frozenset({
    "PLANNED", "UNKNOWN", "WORKING", "FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT",
})
CHILD_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"})
# Terminal children whose broker row may still report a later fill (ruling 50).
_FILL_MAY_GROW = ("FILLED", "CANCELLED", "REJECTED")
_BROKER_ACCEPTED = frozenset({"PreSubmitted", "Submitted", "PendingCancel"})
_BROKER_TERMINAL = {
    "Filled": "FILLED", "Cancelled": "CANCELLED", "ApiCancelled": "CANCELLED",
    "Inactive": "REJECTED", "Rejected": "REJECTED",
}
_LEG_KINDS = ("reprotect-stop", "reprotect-target")
_OPEN_BEFORE_SP1 = "state NOT IN ('FLAT', 'FAILED_SAFE')"


def apply_liquidation_migration(migrator: SchemaMigrator) -> None:
    """Journal migrations 25 (runs) and 36 (scope, goal, children, joins).

    Migration 36 also adopts runs that were open before the upgrade (D8): each
    run gets a join row; the oldest open run of an account becomes its ACTIVE
    account owner, in phase ``legacy`` (it acts only on a broker generation
    opened after the upgrade); other open runs of that account are SUPERSEDED
    by it. Every old run, FLAT included, is marked ``pre_sp1_open``: it may
    have sent a reduce the journal does not know (N2). An old FLAT was decided
    from an empty snapshot, which does not prove that an earlier reduce will
    not arrive late (ruling 49). Such a run is
    tracked by one wildcard child (``ref_prefix`` set, ``conid`` NULL), because
    old runs record no conids (ruling 42). Exit owners (migration 35) are
    applied first.
    """
    migrator.apply(LIQUIDATION_MIGRATION_VERSION, "p1_liquidation_runs", (
        """CREATE TABLE IF NOT EXISTS liquidation_runs (
            cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL, deadline TIMESTAMPTZ NOT NULL,
            generation_id BIGINT, detail VARCHAR NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        )""",
    ))
    apply_exit_owner_migration(migrator)
    migrator.apply(LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION, "sp1_liquidation_safe_close", (
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS scope VARCHAR DEFAULT 'account'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS conid INTEGER",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS goal VARCHAR DEFAULT 'zero'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS goal_quantity DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS phase VARCHAR DEFAULT ''",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS opened_generation BIGINT",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS stop_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS target_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS remaining_quantity DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS escalated BOOLEAN DEFAULT FALSE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS superseded_by VARCHAR",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS cleanup_pending BOOLEAN DEFAULT FALSE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS pre_sp1_open BOOLEAN DEFAULT FALSE",
        "UPDATE liquidation_runs SET pre_sp1_open = TRUE",
        """CREATE TABLE IF NOT EXISTS liquidation_children (
            child_id VARCHAR PRIMARY KEY,
            root_id VARCHAR NOT NULL,
            owner_root_id VARCHAR NOT NULL,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            kind VARCHAR NOT NULL,
            attempt INTEGER NOT NULL,
            state VARCHAR NOT NULL,
            fence_generation BIGINT NOT NULL,
            side VARCHAR,
            quantity DOUBLE,
            price DOUBLE,
            oca_group VARCHAR,
            target_order_entity_id VARCHAR,
            filled_at_send DOUBLE NOT NULL DEFAULT 0,
            filled_quantity DOUBLE NOT NULL DEFAULT 0,
            outstanding_quantity DOUBLE,
            observed_generation BIGINT,
            sent_generation BIGINT,
            order_entity_id VARCHAR,
            ref_prefix VARCHAR,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_children_owner ON liquidation_children(owner_root_id)",
        """CREATE TABLE IF NOT EXISTS liquidation_joins (
            command_id VARCHAR PRIMARY KEY,
            root_id VARCHAR NOT NULL,
            account_id VARCHAR NOT NULL,
            conid INTEGER,
            outcome VARCHAR NOT NULL,
            requested_goal VARCHAR NOT NULL,
            requested_quantity DOUBLE,
            recorded_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_liquidation_joins_root ON liquidation_joins(root_id)",
        """INSERT INTO liquidation_joins
           SELECT cause_command_id, cause_command_id, account_id, NULL, 'CLAIMED', 'account', NULL, updated_at
           FROM liquidation_runs""",
        f"""INSERT INTO exit_owners
           SELECT cause_command_id, account_id, NULL, 'account_flatten', 'zero', NULL,
                  CASE WHEN row_number() OVER (PARTITION BY account_id ORDER BY updated_at, cause_command_id) = 1
                       THEN 'ACTIVE' ELSE 'SUPERSEDED' END,
                  updated_at
           FROM liquidation_runs WHERE {_OPEN_BEFORE_SP1}""",
        """UPDATE liquidation_runs SET state = 'SUPERSEDED', detail = 'taken over at the SP1 upgrade',
               superseded_by = (SELECT o.root_id FROM exit_owners o
                                WHERE o.account_id = liquidation_runs.account_id
                                  AND o.kind = 'account_flatten' AND o.state = 'ACTIVE')
           WHERE cause_command_id IN (SELECT root_id FROM exit_owners WHERE state = 'SUPERSEDED')""",
        f"UPDATE liquidation_runs SET phase = 'legacy' WHERE {_OPEN_BEFORE_SP1} AND state <> 'SUPERSEDED'",
    ))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LiquidationBusy(RuntimeError):
    """Another caller held the liquidation lock past the timeout; retry later."""


class DispatchRefused(RuntimeError):
    """A proven refusal *before* the broker boundary. The child becomes NOT_SENT."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class LiquidationRefused(ValueError):
    """A request the close refuses up front (no claim, no run, no order)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class RunStateError(RuntimeError):
    """A forbidden run change: a terminal or SUPERSEDED run, or a goal moving back to partial."""


class _StaleDispatch(RuntimeError):
    """R7: the journal says this root may no longer dispatch."""


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

class BrokerSnapshotPort(Protocol):
    def capture(self, account_id: str) -> Any: ...


class LiquidationDispatchPort(Protocol):
    """Reduce-only order boundary plus read-only broker evidence.

    Order methods raise ``DispatchRefused`` only for a proven refusal before
    the broker boundary. Any other exception means the order may exist.
    """
    def cancel(self, order: Any, child_id: str) -> None: ...
    def reduce(self, position: Any, side: str, quantity: float, child_id: str) -> None: ...
    def reduce_partial(self, position: Any, side: str, quantity: float, child_id: str) -> None: ...
    def place_exit_leg(self, position: Any, *, leg: str, quantity: float, price: float,
                       oca_group: str, child_id: str) -> None: ...
    def find_orders(self, account_id: str, child_id: str) -> list: ...
    def find_orders_with_prefix(self, account_id: str, prefix: str) -> list: ...
    def get_order(self, order_entity_id: str) -> Optional[Any]: ...
    def enumeration_complete(self) -> bool: ...
    def newest_generation(self) -> int: ...


class LiquidationBreakerPort(Protocol):
    def trip_liquidation(self, cause_command_id: str, detail: str) -> None: ...


class GenerationRefreshPort(Protocol):
    def request_refresh(self, account_id: str) -> None: ...


@dataclass(frozen=True)
class CancelTarget:
    order_entity_id: str
    order_group_id: Optional[str]


@dataclass(frozen=True)
class HandoverInfo:
    stop_price: Optional[float]
    target_price: Optional[float]


class ProtectionOwnershipPort(Protocol):
    """Implemented by ``ProtectiveOrderSaga``. Every method is idempotent."""
    def handover(self, *, account_id: str, conid: int, close_root_id: str,
                 cancels: tuple[CancelTarget, ...], generation: int, now: dt.datetime) -> HandoverInfo: ...
    def handover_account(self, *, account_id: str, close_root_id: str,
                         cancels: tuple[CancelTarget, ...], generation: int, now: dt.datetime) -> None: ...
    def expect_reprotect(self, *, close_root_id: str, groups: tuple[str, ...], now: dt.datetime) -> None: ...
    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              stop_group: str, stop_status: str, target_group: Optional[str],
                              target_status: Optional[str], now: dt.datetime) -> None: ...
    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None: ...


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChildRef:
    child_id: str                 # {root}-{kind}-{conid}-{attempt}; also the decoded order ref
    root_id: str                  # root that wrote it
    owner_root_id: str            # root that must reconcile it now (R9 inheritance)
    account_id: str
    conid: Optional[int]          # None only for a pre-SP1 wildcard child
    kind: str                     # cancel | reduce | reprotect-stop | reprotect-target
    attempt: int
    state: str                    # see CHILD_STATES
    fence_generation: int         # broker generation of the snapshot it was planned on
    side: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    oca_group: Optional[str] = None
    target_order_entity_id: Optional[str] = None
    filled_at_send: float = 0.0
    filled_quantity: float = 0.0
    outstanding_quantity: Optional[float] = None
    observed_generation: Optional[int] = None   # newest generation (staging included) when last observed
    sent_generation: Optional[int] = None       # newest generation (staging included) right after the send
    order_entity_id: Optional[str] = None       # the broker row of this child's own order
    ref_prefix: Optional[str] = None            # pre-SP1 wildcard: every ref {prefix}{conid} (ruling 42)

    @property
    def fill_bearing(self) -> bool:
        """True when this child may have changed the position since it was sent.

        An ABSENT child has an unknown fill, so it counts as fill-bearing.
        """
        return self.state == "ABSENT" or self.filled_quantity > self.filled_at_send


@dataclass(frozen=True)
class LiquidationReceipt:
    account_id: str
    cause_command_id: str
    state: str
    deadline: dt.datetime
    generation_id: Optional[int] = None
    detail: str = ""
    scope: str = "account"
    conid: Optional[int] = None
    goal: str = "zero"
    goal_quantity: Optional[float] = None
    phase: str = ""
    opened_generation: Optional[int] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    remaining_quantity: Optional[float] = None
    escalated: bool = False
    superseded_by: Optional[str] = None
    cleanup_pending: bool = False
    children: tuple[ChildRef, ...] = ()


@dataclass(frozen=True)
class JoinRow:
    command_id: str
    root_id: str
    account_id: str
    conid: Optional[int]          # None for an account request
    outcome: str
    requested_goal: str           # account | zero | partial (the request as it came in)
    requested_quantity: Optional[float]   # the raw requested quantity, before admission


@dataclass(frozen=True)
class CloseResolution:
    command_id: str
    root_id: str                  # the root that decided (after following SUPERSEDED)
    state: str
    success: bool
    outcome: dict
    error_code: Optional[str] = None      # set when success is False


@dataclass(frozen=True)
class _ChildOrder:
    """A working child order found by its own broker row, not in the captured snapshot."""
    order_entity_id: str
    conid: int
    order_group_id: str
    filled_quantity: float


_RUN_COLUMNS = (
    "cause_command_id", "account_id", "state", "deadline", "generation_id", "detail", "scope",
    "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price", "target_price",
    "remaining_quantity", "escalated", "superseded_by", "cleanup_pending",
)
_CHILD_COLUMNS = (
    "child_id", "root_id", "owner_root_id", "account_id", "conid", "kind", "attempt", "state",
    "fence_generation", "side", "quantity", "price", "oca_group", "target_order_entity_id",
    "filled_at_send", "filled_quantity", "outstanding_quantity", "observed_generation",
    "sent_generation", "order_entity_id", "ref_prefix",
)
_JOIN_COLUMNS = ("command_id", "root_id", "account_id", "conid", "outcome", "requested_goal",
                 "requested_quantity")


def _run_from_row(row) -> LiquidationReceipt:
    values = dict(zip(_RUN_COLUMNS, row))
    return LiquidationReceipt(
        account_id=values["account_id"], cause_command_id=values["cause_command_id"],
        state=values["state"], deadline=values["deadline"], generation_id=values["generation_id"],
        detail=values["detail"], scope=values["scope"] or "account",
        conid=None if values["conid"] is None else int(values["conid"]),
        goal=values["goal"] or "zero", goal_quantity=values["goal_quantity"],
        phase=values["phase"] or "", opened_generation=values["opened_generation"],
        stop_price=values["stop_price"], target_price=values["target_price"],
        remaining_quantity=values["remaining_quantity"], escalated=bool(values["escalated"]),
        superseded_by=values["superseded_by"], cleanup_pending=bool(values["cleanup_pending"]),
    )


def _child_from_row(row) -> ChildRef:
    values = dict(zip(_CHILD_COLUMNS, row))
    values["conid"] = None if values["conid"] is None else int(values["conid"])
    values["attempt"] = int(values["attempt"])
    values["fence_generation"] = int(values["fence_generation"])
    return ChildRef(**values)


class LiquidationRunStore:
    """Journal rows of runs, children and joins. The broker stays the authority."""

    def __init__(self, db):
        self._db = db

    def transaction(self, fn):
        return self._db.transaction(fn)

    # -- runs ------------------------------------------------------------------

    def get_run_in_tx(self, conn, root_id: str) -> Optional[LiquidationReceipt]:
        row = conn.execute(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM liquidation_runs WHERE cause_command_id = ?",
            [root_id]).fetchone()
        return None if row is None else _run_from_row(row)

    def insert_run_in_tx(self, conn, receipt: LiquidationReceipt, now: dt.datetime) -> None:
        values = [getattr(receipt, c) for c in _RUN_COLUMNS]
        conn.execute(
            f"INSERT INTO liquidation_runs ({', '.join(_RUN_COLUMNS)}, updated_at) "
            f"VALUES ({', '.join('?' for _ in _RUN_COLUMNS)}, ?)", values + [now])

    def update_run_in_tx(self, conn, receipt: LiquidationReceipt, now: dt.datetime) -> None:
        current = self.get_run_in_tx(conn, receipt.cause_command_id)
        if current is None:
            raise RunStateError(f"unknown liquidation root {receipt.cause_command_id!r}")
        if current.state in RESCAN_TERMINAL and receipt.state != current.state:
            raise RunStateError(
                f"root {receipt.cause_command_id} is {current.state}; it cannot become {receipt.state}")
        if current.goal == "zero" and receipt.goal != "zero":
            raise RunStateError(f"root {receipt.cause_command_id} goal is zero; it never goes back")
        assignments = ", ".join(f"{c} = ?" for c in _RUN_COLUMNS[1:])
        conn.execute(
            f"UPDATE liquidation_runs SET {assignments}, updated_at = ? WHERE cause_command_id = ?",
            [getattr(receipt, c) for c in _RUN_COLUMNS[1:]] + [now, receipt.cause_command_id])

    def pre_sp1_roots_in_tx(self, conn, account_id: str) -> list[str]:
        """Runs from before the upgrade whose old reduces are not settled yet (N2, ruling 42)."""
        return [r[0] for r in conn.execute(
            "SELECT cause_command_id FROM liquidation_runs WHERE account_id = ? AND pre_sp1_open "
            "ORDER BY updated_at, cause_command_id", [account_id]).fetchall()]

    def clear_pre_sp1_mark_in_tx(self, conn, run_id: str) -> None:
        """Only in the transaction that settles the run's wildcard child (ruling 42)."""
        conn.execute("UPDATE liquidation_runs SET pre_sp1_open = FALSE WHERE cause_command_id = ?", [run_id])

    def fill_watermark_in_tx(self, conn, account_id: str, conid: Optional[int]) -> Optional[int]:
        """Newest generation at which any child of the scope was seen fill-bearing (ruling 43).

        Any root, any state. A conid scope also counts wildcard children
        (conid NULL), which may hold a fill of any conid.
        """
        conid_filter = "" if conid is None else " AND (conid = ? OR conid IS NULL)"
        params: list = [account_id] + ([] if conid is None else [int(conid)])
        row = conn.execute(
            "SELECT MAX(observed_generation) FROM liquidation_children WHERE account_id = ? "
            "AND (state = 'ABSENT' OR filled_quantity > filled_at_send)" + conid_filter, params).fetchone()
        return None if row[0] is None else int(row[0])

    def settled_children_in_tx(self, conn, account_id: str, conid: Optional[int]) -> tuple[ChildRef, ...]:
        """Terminal children of the scope, any root, whose row may still report a later fill (ruling 50).

        A conid scope also reads wildcard children (conid NULL), like the fill watermark.
        """
        conid_filter = "" if conid is None else " AND (conid = ? OR conid IS NULL)"
        params: list = [account_id, *_FILL_MAY_GROW] + ([] if conid is None else [int(conid)])
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE account_id = ? "
            f"AND state IN ({', '.join('?' for _ in _FILL_MAY_GROW)})" + conid_filter + " ORDER BY child_id",
            params).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def legacy_reduces_in_tx(self, conn, account_id: str) -> tuple[ChildRef, ...]:
        """Every wildcard child of the account, whoever owns it (ruling 42)."""
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children "
            "WHERE account_id = ? AND ref_prefix IS NOT NULL ORDER BY child_id", [account_id]).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def roots_to_advance_in_tx(self, conn) -> list[str]:
        """Roots with unfinished cleanup first, then every non-terminal root."""
        markers = ", ".join("?" for _ in RESCAN_TERMINAL)
        cleanup = [r[0] for r in conn.execute(
            "SELECT cause_command_id FROM liquidation_runs WHERE cleanup_pending ORDER BY updated_at").fetchall()]
        open_roots = [r[0] for r in conn.execute(
            f"SELECT cause_command_id FROM liquidation_runs WHERE state NOT IN ({markers}) "
            "AND NOT cleanup_pending ORDER BY updated_at", list(RESCAN_TERMINAL)).fetchall()]
        return cleanup + open_roots

    # -- children ----------------------------------------------------------------

    def children_in_tx(self, conn, owner_root_id: str) -> tuple[ChildRef, ...]:
        rows = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE owner_root_id = ? "
            "ORDER BY fence_generation, child_id", [owner_root_id]).fetchall()
        return tuple(_child_from_row(r) for r in rows)

    def child_in_tx(self, conn, child_id: str) -> Optional[ChildRef]:
        row = conn.execute(
            f"SELECT {', '.join(_CHILD_COLUMNS)} FROM liquidation_children WHERE child_id = ?",
            [child_id]).fetchone()
        return None if row is None else _child_from_row(row)

    def next_attempt_in_tx(self, conn, root_id: str, kind: str, conid: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) FROM liquidation_children WHERE root_id = ? AND kind = ? AND conid = ?",
            [root_id, kind, int(conid)]).fetchone()
        return int(row[0]) + 1

    def insert_child_in_tx(self, conn, child: ChildRef, now: dt.datetime) -> None:
        if child.state not in CHILD_STATES:
            raise ValueError(f"unknown child state {child.state!r}")
        conn.execute(
            f"INSERT INTO liquidation_children ({', '.join(_CHILD_COLUMNS)}, updated_at) "
            f"VALUES ({', '.join('?' for _ in _CHILD_COLUMNS)}, ?)",
            [getattr(child, c) for c in _CHILD_COLUMNS] + [now])

    def update_child_in_tx(self, conn, child: ChildRef, now: dt.datetime) -> None:
        if child.state not in CHILD_STATES:
            raise ValueError(f"unknown child state {child.state!r}")
        assignments = ", ".join(f"{c} = ?" for c in _CHILD_COLUMNS[1:])
        conn.execute(
            f"UPDATE liquidation_children SET {assignments}, updated_at = ? WHERE child_id = ?",
            [getattr(child, c) for c in _CHILD_COLUMNS[1:]] + [now, child.child_id])

    def drop_planned_in_tx(self, conn, owner_root_id: str, now: dt.datetime) -> None:
        """A PLANNED child was never sent; a root that stops needs it no more."""
        conn.execute(
            "UPDATE liquidation_children SET state = 'NOT_SENT', updated_at = ? "
            "WHERE owner_root_id = ? AND state = 'PLANNED'", [now, owner_root_id])

    def inherit_children_in_tx(self, conn, *, account_id: str, conid: Optional[int],
                               to_root_id: str, now: dt.datetime) -> int:
        """R9: the new owner takes every open child of a SUPERSEDED or FAILED_SAFE root on its scope."""
        conid_filter = "" if conid is None else " AND conid = ?"
        params: list = [to_root_id, now, account_id, to_root_id]
        if conid is not None:
            params.append(int(conid))
        rows = conn.execute(
            "UPDATE liquidation_children SET owner_root_id = ?, updated_at = ? "
            "WHERE account_id = ? AND state IN ('UNKNOWN', 'WORKING') AND owner_root_id <> ?"
            f"{conid_filter} AND owner_root_id IN ("
            "SELECT cause_command_id FROM liquidation_runs WHERE state IN ('SUPERSEDED', 'FAILED_SAFE')) "
            "RETURNING child_id", params).fetchall()
        return len(rows)

    # -- joins ---------------------------------------------------------------------

    def record_join_in_tx(self, conn, join: JoinRow, now: dt.datetime) -> None:
        conn.execute(
            f"INSERT INTO liquidation_joins ({', '.join(_JOIN_COLUMNS)}, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [getattr(join, c) for c in _JOIN_COLUMNS] + [now])

    def join_for_in_tx(self, conn, command_id: str) -> Optional[JoinRow]:
        row = conn.execute(
            f"SELECT {', '.join(_JOIN_COLUMNS)} FROM liquidation_joins WHERE command_id = ?",
            [command_id]).fetchone()
        return None if row is None else JoinRow(*row)

    def joins_resolving_to_in_tx(self, conn, root_id: str) -> list[JoinRow]:
        rows = conn.execute(
            f"SELECT {', '.join(_JOIN_COLUMNS)} FROM liquidation_joins WHERE root_id = ? OR root_id IN ("
            "SELECT cause_command_id FROM liquidation_runs WHERE superseded_by = ?) ORDER BY command_id",
            [root_id, root_id]).fetchall()
        return [JoinRow(*r) for r in rows]

    # -- reads in their own transaction ---------------------------------------------

    def receipt(self, root_id: str) -> Optional[LiquidationReceipt]:
        def read(conn):
            run = self.get_run_in_tx(conn, root_id)
            return None if run is None else replace(run, children=self.children_in_tx(conn, root_id))
        return self._db.transaction(read)

    def root_for(self, command_id: str) -> Optional[str]:
        join = self._db.transaction(lambda conn: self.join_for_in_tx(conn, command_id))
        return None if join is None else join.root_id

    def close_resolution(self, command_id: str) -> Optional[CloseResolution]:
        """Outcome of the exact root a command started or joined; None while undecided.

        SUPERSEDED is followed to the root that took over. A decided root that
        does not meet the request's goal (FAILED_SAFE, REDUCE_FAILED, or DONE
        for a full close) is a failure with an error code, never a success.
        """
        def read(conn):
            join = self.join_for_in_tx(conn, command_id)
            if join is None:
                return None
            run = self.get_run_in_tx(conn, join.root_id)
            seen: set[str] = set()
            while run is not None and run.state == "SUPERSEDED" and run.superseded_by \
                    and run.cause_command_id not in seen:
                seen.add(run.cause_command_id)
                run = self.get_run_in_tx(conn, run.superseded_by)
            if run is None or run.state not in RESCAN_TERMINAL or run.state == "SUPERSEDED" \
                    or run.cleanup_pending:
                return None
            success = run.state in _SUCCESS_FOR_GOAL[join.requested_goal]
            error_code = None if success else _FAILURE_CODES.get(run.state, "CLOSE_GOAL_NOT_MET")
            sold = conn.execute(
                "SELECT COALESCE(SUM(filled_quantity), 0) FROM liquidation_children "
                "WHERE root_id = ? AND kind = 'reduce'", [run.cause_command_id]).fetchone()[0]
            return CloseResolution(
                command_id=command_id, root_id=run.cause_command_id, state=run.state, success=success,
                error_code=error_code,
                outcome={"close_root_id": run.cause_command_id, "liquidation_state": run.state,
                         "generation_id": run.generation_id, "detail": run.detail,
                         "requested_goal": join.requested_goal, "requested_quantity": join.requested_quantity,
                         "root_goal": run.goal, "filled_quantity": float(sold),
                         "remaining_quantity": run.remaining_quantity},
            )
        return self._db.transaction(read)


def _reducing_side(quantity: float) -> str:
    return "SELL" if float(quantity) > 0 else "BUY"


def _targets(orders) -> tuple[CancelTarget, ...]:
    return tuple(CancelTarget(o.order_entity_id, getattr(o, "order_group_id", None)) for o in orders)


class LiquidationService:
    """One state machine for every exit. Call every entry point from one worker thread (R12).

    The worker is the serialization. The timed lock kept from master is a
    second guard for a caller that bypasses the worker (the drill, a test, a
    future producer): ``start`` and ``rescan`` hold it across the broker
    capture and the dispatch wait, and a caller that waits longer than the
    timeout gets :class:`LiquidationBusy`. ``start`` commits its claim before
    it waits, so a busy caller never loses the root: the next ``rescan``
    advances it. Lock order: ``LiquidationService._lock`` before
    ``BrokerIngest._apply_lock``.
    """

    def __init__(
        self,
        broker: BrokerSnapshotPort,
        dispatch: LiquidationDispatchPort,
        *,
        store: LiquidationRunStore,
        registry: ExitOwnerRegistry,
        now: Callable[[], dt.datetime],
        breaker: Optional[LiquidationBreakerPort] = None,
        journal=None,
        ledger=None,
        schedule_reconcile: Optional[Callable[[str], None]] = None,
        protection: Optional[ProtectionOwnershipPort] = None,
        refresh: Optional[GenerationRefreshPort] = None,
        deadline_seconds: float = 300.0,
        lock_timeout_seconds: float = 60.0,
    ):
        self._broker = broker
        self._dispatch = dispatch
        self._store = store
        self._registry = registry
        self._now = now
        self._breaker = breaker
        self._journal, self._ledger = journal, ledger
        self._schedule_reconcile = schedule_reconcile
        self._protection = protection
        self._refresh = refresh
        self._deadline_seconds = deadline_seconds
        # Default: twice the 30s order-dispatch timeout.
        self._lock = threading.Lock()
        self._lock_timeout_seconds = lock_timeout_seconds

    @contextmanager
    def _exclusive(self):
        if not self._lock.acquire(timeout=self._lock_timeout_seconds):
            raise LiquidationBusy(
                f"liquidation busy for more than {self._lock_timeout_seconds}s; retry later")
        try:
            yield
        finally:
            self._lock.release()

    def attach_protection(self, protection: ProtectionOwnershipPort) -> None:
        self._protection = protection

    # -- command entry -------------------------------------------------------------

    def liquidate(self, cmd) -> CommandReceipt:
        """Coordinator saga entry: acknowledgement is explicitly non-terminal.

        Only the reconciler resolves the command, from the exact root it
        started or joined (R17, D12).
        """
        if self._journal is None or self._ledger is None:
            raise RuntimeError("liquidation command authority is not configured")
        try:
            receipt = self.start(cmd.account_id, cmd.command_id,
                                 self._now() + dt.timedelta(seconds=self._deadline_seconds))
        except LiquidationBusy:
            # The claim committed before the wait; record the root as pending so the reconciler resolves it.
            self._record_pending(cmd, self._store.receipt(cmd.command_id))
            raise
        return self._record_pending(cmd, receipt)

    def _record_pending(self, cmd, receipt: LiquidationReceipt) -> CommandReceipt:
        outcome = {"liquidation_state": receipt.state, "detail": receipt.detail,
                   "generation_id": receipt.generation_id, "close_root_id": receipt.cause_command_id}
        now = self._now()

        def write(conn, _revision):
            self._ledger.transition_in_tx(conn, cmd.command_id, "RECEIVED", "OUTCOME_UNKNOWN",
                                          outcome=outcome, error_code="LIQUIDATION_PENDING", now=now)
        self._journal.mutate(self._journal.connect(), DomainMutation(
            event_type="command.updated", entity_type="command", entity_id=command_entity_id(cmd.command_id),
            operation="upsert", account_id=cmd.account_id, source="trader_service", source_timestamp=now,
            correlation_id=cmd.command_id, payload={"state": "OUTCOME_UNKNOWN", **outcome}),
            write, event_id=f"command:{cmd.command_id}:liquidation-pending")
        if self._schedule_reconcile is not None:
            self._schedule_reconcile(cmd.command_id)
        return CommandReceipt(cmd.command_id, cmd.command_id, "OUTCOME_UNKNOWN", outcome,
                              "LIQUIDATION_PENDING", False)

    # -- public API ----------------------------------------------------------------

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, *,
              scope: str = "account", conid: Optional[int] = None, quantity: Optional[float] = None,
              stop_price: Optional[float] = None, target_price: Optional[float] = None) -> LiquidationReceipt:
        """Claim, then create or join a root. Returns the receipt of the root the caller must poll."""
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        if ":" in cause_command_id:
            raise ValueError("cause command id may not contain ':'")
        if scope == "account":
            if conid is not None or quantity is not None:
                raise ValueError("account scope takes no conid or quantity")
            outcome, root = self._store.transaction(
                lambda conn: self._claim_account_in_tx(conn, account_id, cause_command_id, deadline))
        else:
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if outcome in (CLAIMED, "EXISTING"):
            with self._exclusive():
                return self._tick(root)
        return self._store.receipt(root)

    def rescan(self) -> Optional[LiquidationReceipt]:
        """Finish pending cleanups, then advance every root that is not terminal."""
        first: Optional[LiquidationReceipt] = None
        with self._exclusive():
            for root in self._store.transaction(self._store.roots_to_advance_in_tx):
                try:
                    advanced = self._tick(root)
                except Exception:  # one bad root must not stop the others
                    log.exception("liquidation root %s failed to advance", root)
                    continue
                if first is None:
                    first = advanced
        return first

    def receipt_for(self, root_id: str) -> Optional[LiquidationReceipt]:
        return self._store.receipt(root_id)

    def root_for(self, command_id: str) -> Optional[str]:
        return self._store.root_for(command_id)

    def close_resolution(self, command_id: str) -> Optional[CloseResolution]:
        return self._store.close_resolution(command_id)

    # -- claims (inside one transaction each, R6) ----------------------------------------

    def _existing_in_tx(self, conn, account_id, cause, *, conid, goal, quantity):
        join = self._store.join_for_in_tx(conn, cause)
        if join is None:
            return None
        if (join.account_id, join.conid, join.requested_goal, join.requested_quantity) != (
                account_id, conid, goal, quantity):
            raise ValueError("cause command id is already bound to another scope, conid or goal")
        if join.root_id != cause:
            return ("JOINED_BEFORE", join.root_id)
        return ("EXISTING", cause)

    def _claim_account_in_tx(self, conn, account_id, cause, deadline):
        existing = self._existing_in_tx(conn, account_id, cause, conid=None, goal="account", quantity=None)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_account_in_tx(conn, account_id=account_id, root_id=cause, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, None, claim.outcome,
                                                    "account", None), now)
        if claim.outcome == JOINED_FLATTEN:
            return (JOINED_FLATTEN, claim.root_id)
        self._store.insert_run_in_tx(conn, LiquidationReceipt(account_id, cause, "REQUESTED", deadline), now)
        self._store.inherit_children_in_tx(conn, account_id=account_id, conid=None, to_root_id=cause, now=now)
        return (CLAIMED, cause)

    # -- one step of one root --------------------------------------------------------

    def _tick(self, root_id: str) -> Optional[LiquidationReceipt]:
        receipt = self._store.receipt(root_id)
        if receipt is None:
            return None
        if receipt.cleanup_pending:
            return self._cleanup(receipt)
        if receipt.state in RESCAN_TERMINAL:
            return receipt
        try:
            snapshot = self._broker.capture(receipt.account_id)
            newest = int(self._dispatch.newest_generation())
            if getattr(snapshot, "account_id", None) != receipt.account_id:
                raise RuntimeError("broker snapshot account mismatch")
        except Exception as exc:
            if self._now() >= receipt.deadline:
                return self._on_deadline(receipt)
            return self._snapshot_unavailable(receipt, f"broker evidence unavailable: {exc}")
        receipt = self._fence_unsent(receipt, snapshot, newest)
        receipt = self._observe_children(receipt, snapshot, newest)
        receipt = self._observe_late_fills(receipt, newest)
        if self._now() >= receipt.deadline:
            # Ruling 54: the deadline decides on this tick's evidence, so a fill seen now still fences.
            return self._on_deadline(receipt)
        return self._advance_account(receipt, snapshot)

    def _snapshot_unavailable(self, receipt, detail):
        state = "OUTCOME_UNKNOWN" if receipt.scope == "account" else "VERIFYING"
        return self._set(receipt, state, detail=detail)

    def _on_deadline(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        return self._finish(receipt, "FAILED_SAFE", detail="deadline elapsed without broker-confirmed result")

    # -- evidence (R4) ---------------------------------------------------------------

    def _fence_unsent(self, receipt, snapshot, newest: int) -> LiquidationReceipt:
        """Fence children whose send never recorded a fence, and adopt a pre-SP1 run.

        Every entry point runs on one worker, so no send is in flight when a
        tick starts: a child still without ``sent_generation`` was cut off by
        a crash, and every generation newer than ``newest`` opened after it. A
        cancel cut off that way becomes NOT_SENT (sending a cancel again is
        harmless). A run adopted at the upgrade is handled by ``_adopt_legacy``.
        """
        unfenced = [replace(c, sent_generation=newest, state="NOT_SENT" if c.kind == "cancel" else c.state)
                    for c in receipt.children if c.state == "UNKNOWN" and c.sent_generation is None]
        if unfenced:
            def write(conn):
                for child in unfenced:
                    self._store.update_child_in_tx(conn, child, self._now())
            self._store.transaction(write)
        tracked = self._track_pre_sp1_reduces(receipt, newest)
        if receipt.phase == "legacy":
            return self._adopt_legacy(receipt, newest)
        return self._store.receipt(receipt.cause_command_id) if unfenced or tracked else receipt

    def _track_pre_sp1_reduces(self, receipt, newest: int) -> bool:
        """R29 / N2, ruling 42: a run from before the upgrade may have sent reduces the journal does not know.

        Old runs record no conids, so each run of the account that is still
        marked ``pre_sp1_open`` becomes ONE wildcard ``UNKNOWN`` child of this
        root: it stands for every ref ``{run}-liquidation-reduce-{conid}``,
        fenced on ``newest``. The snapshot's positions are not used: a
        missing position is never evidence. The mark is cleared only when the
        child settles (``_observe_children``), never here.
        """
        def write(conn):
            added = False
            for old in self._store.pre_sp1_roots_in_tx(conn, receipt.account_id):
                prefix = legacy_reduce_prefix(old)
                child_id = f"{prefix}*"
                if self._store.child_in_tx(conn, child_id) is not None:
                    continue
                self._store.insert_child_in_tx(conn, ChildRef(
                    child_id=child_id, root_id=old, owner_root_id=receipt.cause_command_id,
                    account_id=receipt.account_id, conid=None, kind="reduce", attempt=0, state="UNKNOWN",
                    fence_generation=newest, sent_generation=newest, ref_prefix=prefix), self._now())
                added = True
            return added
        return self._store.transaction(write)

    def _adopt_legacy(self, receipt, newest: int) -> LiquidationReceipt:
        """R29: the adopted run waits for a broker generation opened after the upgrade."""
        return self._set(self._store.receipt(receipt.cause_command_id), receipt.state, phase="",
                         opened_generation=newest,
                         detail="adopted at the upgrade; old reduces are unknown until the broker settles them")

    def _observe_children(self, receipt, snapshot, newest: int) -> LiquidationReceipt:
        """Classify this root's open children and every open wildcard child of its account.

        A wildcard child that settles clears its run's ``pre_sp1_open`` mark
        in the same transaction (ruling 42).
        """
        generation = int(snapshot.generation_id)
        changed = [observed for child in self._children_in_force(receipt) if child.state in ("UNKNOWN", "WORKING")
                   for observed in (self._evidence(child, generation, newest),) if observed != child]
        if not changed:
            return receipt

        def write(conn):
            for child in changed:
                self._store.update_child_in_tx(conn, child, self._now())
                if child.ref_prefix is not None and child.state in CHILD_TERMINAL:
                    self._store.clear_pre_sp1_mark_in_tx(conn, child.root_id)
        self._store.transaction(write)
        return self._store.receipt(receipt.cause_command_id)

    def _observe_late_fills(self, receipt, newest: int) -> LiquidationReceipt:
        """#20, ruling 50: a terminal child's row can report a fill later (``Cancelled`` with 0, then a fill).

        Every settled child of the scope, any root, is read again. Only its
        fill moves, only upwards, with ``observed_generation`` = ``newest``,
        so the fill watermark makes every root wait for a newer position
        generation. Its state stays terminal; an empty or ambiguous lookup
        changes nothing.
        """
        settled = self._store.transaction(
            lambda conn: self._store.settled_children_in_tx(conn, receipt.account_id, receipt.conid))
        grown = [replace(child, filled_quantity=filled, observed_generation=newest) for child in settled
                 for filled in (self._row_fill(child),) if filled is not None and filled > child.filled_quantity]
        if not grown:
            return receipt

        def write(conn):
            for child in grown:
                self._store.update_child_in_tx(conn, child, self._now())
        self._store.transaction(write)
        return self._store.receipt(receipt.cause_command_id)

    def _row_fill(self, child: ChildRef) -> Optional[float]:
        """The fill the broker reports now for a child's own order(s); None when no single row answers."""
        if child.ref_prefix is not None:
            rows = list(self._dispatch.find_orders_with_prefix(child.account_id, child.ref_prefix))
        elif child.kind == "cancel":
            row = self._dispatch.get_order(child.target_order_entity_id)
            rows = [] if row is None or getattr(row, "deleted", False) else [row]
        else:
            rows = list(self._dispatch.find_orders(child.account_id, child.child_id))
        if not rows or (child.ref_prefix is None and len(rows) > 1):
            return None
        return sum(float(getattr(r, "filled_quantity", 0.0) or 0.0) for r in rows)

    def _children_in_force(self, receipt) -> tuple[ChildRef, ...]:
        """The root's own children plus every wildcard child of its account, whoever owns it (ruling 42)."""
        own = {c.child_id for c in receipt.children}
        legacy = self._store.transaction(lambda conn: self._store.legacy_reduces_in_tx(conn, receipt.account_id))
        return receipt.children + tuple(c for c in legacy if c.child_id not in own)

    def _evidence(self, child: ChildRef, generation: int, newest: int) -> ChildRef:
        """Classify one child from its own broker row (R4, D1, D2).

        A terminal status counts at once, on any generation. An empty lookup
        is absence only when a complete enumeration opened after the child's
        send fence shows nothing; otherwise the child stays as it is.
        """
        if child.ref_prefix is not None:
            return self._legacy_evidence(child, generation, newest)
        if child.kind == "cancel":
            row = self._dispatch.get_order(child.target_order_entity_id)
            rows = [] if row is None or getattr(row, "deleted", False) else [row]
        else:
            rows = list(self._dispatch.find_orders(child.account_id, child.child_id))
        if not rows:
            proven = (child.sent_generation is not None and generation > child.sent_generation
                      and self._dispatch.enumeration_complete())
            return replace(child, state="ABSENT", observed_generation=newest) if proven else child
        if len(rows) > 1:
            return child  # an ambiguous correlation proves nothing
        row = rows[0]
        status = getattr(row, "status", None)
        filled = float(getattr(row, "filled_quantity", 0.0) or 0.0)
        total = float(getattr(row, "total_quantity", 0.0) or 0.0)
        if status in _BROKER_TERMINAL:
            state = _BROKER_TERMINAL[status]
        elif status in _BROKER_ACCEPTED:
            state = "WORKING"
        else:
            state = "UNKNOWN"  # PendingSubmit, ApiPending: a local echo is not acceptance
        outstanding = max(total - filled, 0.0)
        entity = child.order_entity_id if child.kind == "cancel" else getattr(row, "order_entity_id", None)
        if (state, filled, outstanding, entity) == (
                child.state, child.filled_quantity, child.outstanding_quantity, child.order_entity_id):
            return child
        return replace(child, state=state, filled_quantity=filled, outstanding_quantity=outstanding,
                       order_entity_id=entity, observed_generation=newest)

    def _legacy_evidence(self, child: ChildRef, generation: int, newest: int) -> ChildRef:
        """Ruling 42: a wildcard child settles only on positive evidence for every conid at once.

        Every broker row matching the prefix must be terminal AND a complete
        enumeration on a generation newer than the fence must hold, even when
        rows are terminal: another conid's old reduce may still be invisible.
        A visible working row keeps the child WORKING; it is waited on, never
        cancelled.
        """
        rows = list(self._dispatch.find_orders_with_prefix(child.account_id, child.ref_prefix))
        statuses = [getattr(r, "status", None) for r in rows]
        filled = sum(float(getattr(r, "filled_quantity", 0.0) or 0.0) for r in rows)
        if any(s not in _BROKER_TERMINAL for s in statuses):
            state = "WORKING" if any(s in _BROKER_ACCEPTED for s in statuses) else "UNKNOWN"
        elif generation > child.sent_generation and self._dispatch.enumeration_complete():
            state = "ABSENT" if not rows else ("FILLED" if filled > 0 else "CANCELLED")
        else:
            state = "UNKNOWN"
        if (state, filled) == (child.state, child.filled_quantity):
            return child
        return replace(child, state=state, filled_quantity=filled, observed_generation=newest)

    def _fresh(self, receipt, generation: int) -> bool:
        """R5, ruling 43: sizing needs a generation newer than every fill observed on the scope.

        The fence is any root's: a fill seen by a root that has since ended,
        been superseded or been restarted still binds the next root.
        """
        watermark = self._store.transaction(
            lambda conn: self._store.fill_watermark_in_tx(conn, receipt.account_id, receipt.conid))
        return watermark is None or generation > watermark

    def _blocking(self, receipt, generation: int) -> Optional[str]:
        """R5, D2: a child that is unknown or still working stops every new reduce.

        Ruling 42: so does every wildcard child of the account, whoever owns it.
        """
        receipt = replace(receipt, children=self._children_in_force(receipt))
        for child in receipt.children:
            if child.state == "UNKNOWN":
                return f"{child.child_id} outcome unknown"
            if child.state == "WORKING":
                return f"{child.child_id} still working"
        if not self._fresh(receipt, generation):
            return "awaiting a broker generation newer than the last observed fill"
        return None

    @staticmethod
    def _last_action_generation(receipt) -> int:
        fences = [c.sent_generation if c.sent_generation is not None else c.fence_generation
                  for c in receipt.children if c.state != "NOT_SENT"]
        return max(fences + [receipt.opened_generation or 0])

    # -- writes -------------------------------------------------------------------------

    def _trips_breaker(self, receipt, state: str) -> bool:
        if receipt.scope == "account":
            return state != "FLAT"
        return state == "FAILED_SAFE"

    def _set(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """Change the named fields of the run as the journal has it now, never a stale copy."""
        root = receipt.cause_command_id

        def write(conn):
            current = self._store.get_run_in_tx(conn, root)
            updated = replace(current, state=state, detail=detail,
                              generation_id=current.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
        self._store.transaction(write)
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._store.receipt(root)

    def _wait(self, receipt, generation: int, detail: str) -> LiquidationReceipt:
        if self._refresh is not None:
            self._refresh.request_refresh(receipt.account_id)
        return self._set(receipt, "VERIFYING", generation_id=generation, detail=detail)

    def _finish(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """Terminal state, owner release and cleanup_pending in one transaction (R6, R8, D3).

        The owner is released here, not in cleanup, so no claim can join a
        root that has already finished.
        """
        root = receipt.cause_command_id
        owner_state = STATE_RELEASED if state in OWNER_RELEASED_STATES else STATE_FAILED_SAFE

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            updated = replace(run, state=state, detail=detail, cleanup_pending=True,
                              generation_id=run.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
            self._registry.finish_in_tx(conn, root, owner_state, self._now())
        self._store.transaction(write)
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._cleanup(self._store.receipt(root))

    def _cleanup(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R8: the saga step is idempotent; recovery re-runs it until the flag clears."""
        root = receipt.cause_command_id
        if self._protection is not None and receipt.state in ("CLOSED", "FLAT"):
            self._protection.close_after_full(close_root_id=root, now=self._now())

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(run, cleanup_pending=False), self._now())
        self._store.transaction(write)
        self._schedule_commands(root)
        return self._store.receipt(root)

    def _schedule_commands(self, root: str) -> None:
        """Hand every command waiting on this root to the reconciler, its only resolver (D12).

        A command still in SUBMITTING is left alone: its producer moves it to
        OUTCOME_UNKNOWN and schedules it itself, so the two never race.
        """
        if self._schedule_reconcile is None or self._ledger is None:
            return
        joins = self._store.transaction(lambda conn: self._store.joins_resolving_to_in_tx(conn, root))
        for join in joins:
            row = self._ledger.get(join.command_id)
            if row is not None and row.state == "OUTCOME_UNKNOWN":
                self._schedule_reconcile(join.command_id)

    # -- dispatch (R1, R2, R3, R7) ------------------------------------------------------

    def _check_dispatchable_in_tx(self, conn, root_id: str, goal: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        owner = self._registry.get_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL or run.cleanup_pending:
            raise _StaleDispatch(f"root {root_id} is no longer open")
        if owner is None or owner.state != STATE_ACTIVE:
            raise _StaleDispatch(f"root {root_id} no longer owns its scope")
        if run.goal != goal or owner.goal != run.goal:
            raise _StaleDispatch(f"root {root_id} goal is {run.goal} (owner {owner.goal}), not {goal}")

    def _reserve(self, receipt, build) -> Optional[list]:
        """Journal children before any broker call; None when R7 says stop."""
        def write(conn):
            self._check_dispatchable_in_tx(conn, receipt.cause_command_id, receipt.goal)
            return build(conn)
        try:
            return self._store.transaction(write)
        except _StaleDispatch as ex:
            log.warning("liquidation %s reserves nothing: %s", receipt.cause_command_id, ex)
            return None

    def _still_dispatchable(self, root_id: str, goal: str) -> bool:
        try:
            self._store.transaction(lambda conn: self._check_dispatchable_in_tx(conn, root_id, goal))
        except _StaleDispatch as ex:
            log.warning("liquidation %s stops before the broker: %s", root_id, ex)
            return False
        return True

    def _send(self, receipt, child: ChildRef, call: Callable[[], Any]) -> None:
        """One broker call for one journaled child (R2, D13).

        ``DispatchRefused`` is a proven refusal before the order left: NOT_SENT.
        Any other exception may have crossed the boundary: the child stays
        UNKNOWN. Both are logged. A sent child is fenced on the newest broker
        generation right after the call (R22).
        """
        if not self._still_dispatchable(receipt.cause_command_id, receipt.goal):
            self._mark(child, "NOT_SENT")
            return
        try:
            call()
        except DispatchRefused as ex:
            log.warning("liquidation child %s refused before the broker: %s", child.child_id, ex)
            self._mark(child, "NOT_SENT")
            return
        except Exception:
            log.exception("liquidation child %s: outcome unknown after the broker call", child.child_id)
        try:
            fence = int(self._dispatch.newest_generation())
        except Exception:
            log.exception("liquidation child %s has no send fence; the next tick sets it", child.child_id)
            return

        def write(conn):
            current = self._store.child_in_tx(conn, child.child_id)
            self._store.update_child_in_tx(conn, replace(current, sent_generation=fence), self._now())
        self._store.transaction(write)

    def _mark(self, child: ChildRef, state: str) -> None:
        self._store.transaction(
            lambda conn: self._store.update_child_in_tx(conn, replace(child, state=state), self._now()))

    def _new_child(self, conn, receipt, *, kind: str, conid: int, generation: int, state: str = "UNKNOWN",
                   **fields) -> ChildRef:
        root = receipt.cause_command_id
        attempt = self._store.next_attempt_in_tx(conn, root, kind, conid)
        child = ChildRef(child_id=liquidation_child_id(root, kind, conid, attempt), root_id=root,
                         owner_root_id=root, account_id=receipt.account_id, conid=int(conid), kind=kind,
                         attempt=attempt, state=state, fence_generation=generation, **fields)
        self._store.insert_child_in_tx(conn, child, self._now())
        return child

    def _cancel_targets(self, receipt, orders, conid: Optional[int] = None) -> tuple:
        """Every identified working order of the scope this root has not cancelled yet (D2).

        That is the orders of the captured snapshot plus working re-protect
        legs whose own row shows them working (they may have appeared after
        the capture). Reduce orders are never cancelled: they only reduce, and
        while working they block the next reduce. A cancel this root sent
        covers its target; an inherited one does not (this root sends its own).
        """
        found = {o.order_entity_id: o for o in orders}
        for child in receipt.children:
            if child.kind in _LEG_KINDS and child.state == "WORKING" and child.order_entity_id \
                    and (conid is None or child.conid == conid):
                found.setdefault(child.order_entity_id, _ChildOrder(
                    child.order_entity_id, child.conid, child.child_id, child.filled_quantity))
        covered = {c.target_order_entity_id for c in receipt.children
                   if c.kind == "cancel" and c.root_id == receipt.cause_command_id
                   and c.state in ("UNKNOWN", "WORKING")}
        return tuple(o for entity, o in found.items() if entity not in covered
                     and liquidation_child_kind(getattr(o, "order_group_id", None)) != "reduce")

    def _send_cancels(self, receipt, snapshot, targets) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        if not targets:
            return receipt
        children = self._reserve(receipt, lambda conn: [
            self._new_child(conn, receipt, kind="cancel", conid=int(o.conid), generation=generation,
                            target_order_entity_id=o.order_entity_id,
                            filled_at_send=float(getattr(o, "filled_quantity", 0.0) or 0.0))
            for o in targets])
        for child, order in zip(children or (), targets):
            self._send(receipt, child, lambda o=order, c=child: self._dispatch.cancel(o, c.child_id))
        return self._store.receipt(receipt.cause_command_id)

    # -- account scope ----------------------------------------------------------------

    def _advance_account(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        working = tuple(snapshot.working_orders)
        if receipt.phase == "" and receipt.opened_generation is not None \
                and generation <= receipt.opened_generation:
            return self._wait(receipt, generation, "adopted at the upgrade; awaiting a newer broker generation")
        targets = self._cancel_targets(receipt, working)
        if receipt.phase == "" or targets:
            # D6: the saga learns every order the close will cancel before the cancel is sent.
            if self._protection is not None:
                self._protection.handover_account(
                    account_id=receipt.account_id, close_root_id=receipt.cause_command_id,
                    cancels=_targets(targets), generation=generation, now=self._now())
        if receipt.phase == "":
            receipt = self._set(receipt, "CANCELLING_ENTRIES" if working else "VERIFYING",
                                generation_id=generation, phase="cancel", opened_generation=generation,
                                detail="account owner claimed; protection handed over")
        receipt = self._send_cancels(receipt, snapshot, targets)
        why = self._blocking(receipt, generation)
        if why is not None:
            return self._wait(receipt, generation, f"awaiting child confirmation: {why}")
        if working:
            return self._wait(receipt, generation, "awaiting broker confirmation that working orders are gone")
        positions = tuple(p for p in snapshot.positions if float(p.quantity) != 0.0)
        if not positions:
            if generation <= self._last_action_generation(receipt):
                return self._wait(receipt, generation, "awaiting fresh broker flat confirmation")
            return self._finish(receipt, "FLAT", generation_id=generation,
                                detail="fresh broker snapshot confirms no positions or working orders")
        return self._submit_reduces(receipt, snapshot, positions)

    def _submit_reduces(self, receipt, snapshot, positions) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        children = self._reserve(receipt, lambda conn: [
            self._new_child(conn, receipt, kind="reduce", conid=int(p.conid), generation=generation,
                            side=_reducing_side(p.quantity),
                            quantity=abs(float(p.quantity)))
            for p in positions])
        if children is None:
            return self._store.receipt(receipt.cause_command_id)
        receipt = self._set(receipt, "REDUCING", generation_id=generation,
                            phase="reduce" if receipt.scope == "conid" else receipt.phase,
                            detail="submitting reduce-only orders")
        for child, position in zip(children, positions):
            self._send(receipt, child, lambda p=position, c=child: self._dispatch.reduce(p, c.side, c.quantity, c.child_id))
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          "reduction submitted; awaiting broker evidence")

    # -- conid scope --------------------------------------------------------------------

    # -- re-protect (exit-only OCA, R13) ------------------------------------------------

    # -- escalation ----------------------------------------------------------------------
```

The old `_NON_FLAT` set and the in-memory `_runs` map are gone (`grep -rn "_NON_FLAT\|_runs\b" trader tests scripts` must find nothing outside this file).

- [ ] **Step 4: Compose it and keep the drill working**

In `trader/trading/command_stack.py`:

1. Imports: add `from trader.trading.exit_owner import ExitOwnerRegistry`.
2. Replace `_LiquidationDispatch` with:

```python
class _LiquidationDispatch:
    """Keeps every exit on the one IB boundary; child ids become order refs."""
    def __init__(self, dispatch, orders_view):
        self._dispatch = dispatch
        self._orders_view = orders_view

    def cancel(self, order, child_id: str) -> None:
        self._dispatch.cancel(order.order_entity_id, encode_order_ref(child_id))

    def reduce(self, position, side: str, quantity: float, child_id: str) -> None:
        self._dispatch.reduce_position(position, side, quantity, encode_order_ref(child_id))

    def find_orders(self, account_id: str, child_id: str) -> list:
        return self._dispatch.find_by_order_ref(account_id, encode_order_ref(child_id))

    def find_orders_with_prefix(self, account_id: str, prefix: str) -> list:
        return self._dispatch.find_legacy_reduces(account_id, prefix)

    def get_order(self, order_entity_id: str):
        return self._orders_view.get_order(order_entity_id)

    def enumeration_complete(self) -> bool:
        return self._dispatch.enumeration_complete()

    def newest_generation(self) -> int:
        return self._dispatch.newest_generation()
```

3. Right after `apply_liquidation_migration(migrator)`:

```python
    exit_owner_registry = ExitOwnerRegistry(trader.journal_db)
    liquidation_store = LiquidationRunStore(trader.journal_db)
```

4. Replace the `LiquidationService(` construction with (the reconciler is built a few lines earlier):

```python
    liquidation_service = LiquidationService(
        broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
        store=liquidation_store, registry=exit_owner_registry, now=now,
        breaker=_LiquidationBreaker(circuit_breaker, now),
        journal=journal, ledger=ledger,
        schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
    )
```

5. In the `SessionController(` call change both `_LiquidationDispatch(dispatch)` to `_LiquidationDispatch(dispatch, orders_view)`.

The wildcard child (ruling 42) looks up a pre-SP1 run's reduces by ref prefix. In `trader/trading/order_correlation.py`, right after `_LEGACY_REDUCE`, add:

```python
def legacy_reduce_prefix(run_id: str) -> str:
    """Prefix of every reduce ref a pre-SP1 run may have sent, whatever the conid."""
    return f"{run_id}-liquidation-reduce-"


def matches_legacy_reduce(order_group_id: Optional[str], prefix: str) -> bool:
    """True for ``{prefix}{conid}``: the prefix followed by digits only."""
    group = order_group_id or ""
    return group.startswith(prefix) and group[len(prefix):].isdigit()
```

In `trader/trading/command_ports.py` import `matches_legacy_reduce` from `trader.trading.order_correlation` and add after `orders_matching_group`:

```python
def orders_matching_legacy_reduces(rows: Iterable[Any], account_id: str, prefix: str) -> list:
    """Broker rows for (account, ``{prefix}{conid}``) of any conid: a pre-SP1 run's reduces."""
    return [r for r in rows
            if getattr(r, "account_id", None) == account_id
            and matches_legacy_reduce(getattr(r, "order_group_id", None), prefix)]
```

In `trader/trading/trading_runtime.py`, on the dispatch class, right after `find_by_order_ref`:

```python
    def find_legacy_reduces(self, account_id: str, prefix: str) -> list:
        from trader.trading.command_ports import orders_matching_legacy_reduces
        return orders_matching_legacy_reduces(self._active_order_rows(), account_id, prefix)
```

In `scripts/command_plane_drill.py` replace `scn_liquidation` (it builds the service without a journal today):

```python
def scn_liquidation(db_path: str) -> dict:
    from trader.trading.exit_owner import ExitOwnerRegistry
    from trader.trading.liquidation_service import LiquidationRunStore, apply_liquidation_migration

    db = DuckDBConnection.get_instance(db_path + ".liquidation")
    apply_liquidation_migration(SchemaMigrator(db))
    pos = SimpleNamespace(quantity=10.0, conid=CONID)
    snapshots = [
        SimpleNamespace(account_id=ACCOUNT, generation_id=1, positions=(pos,), working_orders=()),
        SimpleNamespace(account_id=ACCOUNT, generation_id=2, positions=(), working_orders=()),
        SimpleNamespace(account_id=ACCOUNT, generation_id=3, positions=(), working_orders=()),
    ]
    seen = {"generation": 0}

    def capture(_account):
        snapshot = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
        seen["generation"] = snapshot.generation_id
        return snapshot
    calls, rows = [], {}

    def reduce(_position, _side, quantity, child_id):
        calls.append(("reduce", child_id))
        rows[child_id] = [SimpleNamespace(status="Filled", filled_quantity=quantity, total_quantity=quantity)]
    dispatch = SimpleNamespace(cancel=lambda *args: calls.append(("cancel", args)), reduce=reduce,
                               find_orders=lambda _account, child_id: rows.get(child_id, []),
                               get_order=lambda _entity: None, enumeration_complete=lambda: True,
                               newest_generation=lambda: seen["generation"])
    service = LiquidationService(SimpleNamespace(capture=capture), dispatch, store=LiquidationRunStore(db),
                                 registry=ExitOwnerRegistry(db), now=lambda: NOW)
    first = service.start(ACCOUNT, "drill-liquidation", NOW + dt.timedelta(minutes=1))
    if first.state == "FLAT" or not calls:
        raise AssertionError("order acknowledgement was treated as broker-flat proof")
    observed = service.rescan()
    if observed is None or observed.state == "FLAT":
        raise AssertionError("the generation that first showed the fill was treated as flat proof")
    terminal = service.rescan()
    if terminal is None or terminal.state != "FLAT":
        raise AssertionError("fresh zero-position broker generation did not resolve liquidation")
    return {"initial": first.state, "terminal": terminal.state, "reductions": len(calls)}
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_command_stack.py tests/test_order_correlation.py tests/test_trader_service_loops.py tests/automation/test_protective_order_saga.py tests/integration/test_command_plane_activation.py tests/integration/test_p1_release_gate.py tests/integration/test_p3_release_gate.py -q --timeout=60`
Expected: all PASS (51 in `test_liquidation_service.py`). Then the full suite: green.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/liquidation_service.py trader/trading/command_stack.py trader/trading/order_correlation.py trader/trading/command_ports.py trader/trading/trading_runtime.py scripts/command_plane_drill.py tests/test_liquidation_service.py tests/test_command_stack.py tests/test_order_correlation.py tests/test_trader_service_loops.py tests/automation/test_protective_order_saga.py
git commit -m "feat: journaled liquidation children and one-transaction account claims

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 14: Extend master's reduce-only path for the close (R11, R13, R34, R35)

PR #42 (`15f9e715`) already added `Trader.place_reduce_only_order(contract, side, quantity, *, broker_quantity, order_ref)`. It skips the entry gates (trading filter, whatIf, leverage, `RiskGate.evaluate`), keeps the account and mode pin, the contract check, the reducing side and size against the caller's broker quantity and against ib_async's live `positions()`, turns any pre-send exception into a refusal (`reduce-only refused: ...`), and maps IB's verdict for this order id through `OrderLifecycleTracker.wait_decisive` (R13: `PendingSubmit` is not decisive). `TradingRuntimeOrderDispatch.reduce_position` already uses it, `_dispatch_loop` refuses a stopped loop and a call on the loop thread, `_wait_on_loop` cancels a timed-out future, and `NEVER_EXPOSED_METHODS` keeps the method off every RPC surface. Master's tests pin all of that (`tests/test_trading_runtime.py`, `tests/test_order_dispatch_ports.py`, `tests/test_production_rpc_security.py`); this task does not repeat them (rulings 33–35, 37).

What the close still needs:

- **R35 bound.** The trader also refuses when IB is not connected, and subtracts the reducing orders already working on the contract (`openTrades()`, outstanding = total − filled; the sibling of the same OCA group excluded, used by Task 10).
- **R34 errors.** Every refusal proven before the order leaves is `DispatchRefused`, so the close marks the child `NOT_SENT`: `_dispatch_loop` (`TRADER_LOOP_UNAVAILABLE`, `ON_TRADER_LOOP`, master's messages kept), the size/side/account checks of `reduce_position` (were `ValueError`), and a `reduce-only refused:` result from the trader (was `BrokerRejectedError`). IB's rejection after the send stays `BrokerRejectedError`: the order was sent, the child stays `UNKNOWN` until its row says `REJECTED`.
- **`reduce_partial`** — a whole-share part strictly inside the position — on the same path, sharing `_reduce_only` with `reduce_position`.
- **`cancel_on_loop`** for the liquidation: the perm id is read from the journal on the worker thread; the open-trade match and `cancelOrder` run on the trader loop, so no DuckDB read blocks the IB loop; no live order is `DispatchRefused("CANCEL_UNRESOLVED")` (ruling 7). Master's `cancel` (coordinator path) is not changed.

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`REDUCE_ONLY_REFUSED`; `Trader.place_reduce_only_order`, `Trader._reduce_only_refusal`, new `Trader._working_reduce_quantity`; `TradingRuntimeOrderDispatch.reduce_position`, `_dispatch_loop`, new `reduce_partial`, `cancel_on_loop`, `_reducing_side`, `_refuse`, `_contract_for`, `_reduce_only`)
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch.cancel`, new `reduce_partial`)
- Test: `tests/test_reduce_only_order_path.py` (create); modify `tests/test_order_dispatch_ports.py` (three refusal assertions change type, one new IB-rejection test), `tests/test_trading_runtime.py` (`_reduce_trader` fake gets `isConnected` and `openTrades`), `tests/test_trader_service_loops.py` (a refused reduce is now `NOT_SENT`)

**Interfaces:**

```python
REDUCE_ONLY_REFUSED = 'reduce-only refused'      # trader/trading/trading_runtime.py; prefix of a refusal, nothing sent
Trader._reduce_only_refusal(contract, side, quantity, broker_quantity, *, oca_group=None) -> Optional[str]
Trader._working_reduce_quantity(conid, reducing_side, oca_group) -> float
TradingRuntimeOrderDispatch.reduce_position(position, side, quantity, order_ref)    # exact size
TradingRuntimeOrderDispatch.reduce_partial(position, side, quantity, order_ref)     # whole, 0 < q < |position|
TradingRuntimeOrderDispatch.cancel_on_loop(order_entity_id, order_ref)              # DispatchRefused("CANCEL_UNRESOLVED")
# DispatchRefused codes: REDUCE_ONLY_REFUSED, TRADER_LOOP_UNAVAILABLE, ON_TRADER_LOOP, CANCEL_UNRESOLVED
_LiquidationDispatch.cancel -> cancel_on_loop; _LiquidationDispatch.reduce_partial(position, side, quantity, child_id)
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reduce_only_order_path.py
"""SP1 plan 1 Tasks 14 and 10: what the one reduce-only order path adds to master's (PR #42).

Master already pins the entry gates it skips, the account, side, size and
live-position checks, and the IB verdict mapping (``tests/test_trading_runtime.py``)
and the loop rules of ``reduce_position`` (``tests/test_order_dispatch_ports.py``).
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import reactivex as rx
from ib_async import Contract

from trader.trading.command_coordinator import BrokerRejectedError
from trader.trading.liquidation_service import DispatchRefused
from trader.trading.trading_runtime import Trader, TradingRuntimeOrderDispatch

ACCOUNT = "DU12345"
CONID = 265598


class _FakeExecutioner:
    """Each placed order gets an id and emits its local echo."""
    def __init__(self):
        self.placed = []
        self._next_id = 100

    async def subscribe_place_order_direct(self, contract, order):
        self._next_id += 1
        order.orderId = self._next_id
        self.placed.append(order)
        return rx.from_iterable([SimpleNamespace(order=order, orderStatus=SimpleNamespace(
            status="PendingSubmit", filled=0.0), contract=SimpleNamespace(conId=CONID))])


class _Tracker:
    """IB's verdict for an order id (``OrderLifecycleTracker.wait_decisive``)."""
    def __init__(self, verdict="accepted"):
        self.verdict = verdict

    async def wait_decisive(self, order_id, timeout=10.0):
        return self.verdict

    def latest_status(self, order_id):
        return "Inactive" if self.verdict == "rejected" else "Submitted"


class _FakeIB:
    def __init__(self, held):
        self.held = held
        self.connected = True
        self.open_trades = []          # trades working at IB (reducing orders count against the bound)
        self.cancelled = []

    def isConnected(self):
        return self.connected

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=self.held)]

    def openTrades(self):
        return list(self.open_trades)

    def cancelOrder(self, order):
        self.cancelled.append(order)


def _working(action, quantity, *, filled=0.0, oca_group="", account=ACCOUNT):
    return SimpleNamespace(contract=SimpleNamespace(conId=CONID),
                           order=SimpleNamespace(action=action, totalQuantity=quantity, ocaGroup=oca_group,
                                                 account=account),
                           orderStatus=SimpleNamespace(filled=filled))


def _trader(*, held=10.0, verdict="accepted"):
    trader = object.__new__(Trader)
    trader.ib_account = ACCOUNT
    trader.paper_trading = True
    trader.client = SimpleNamespace(ib=_FakeIB(held))
    trader.executioner = _FakeExecutioner()
    trader.order_tracker = _Tracker(verdict)
    return trader


def _contract():
    return Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")


def _run(coro):
    return asyncio.run(coro)


# -- Task 14: what the Trader adds -------------------------------------------------------

def test_reduce_only_refuses_when_ib_is_not_connected():
    """D13: no connection is a refusal before anything is sent."""
    trader = _trader()
    trader.client.ib.connected = False
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                 order_ref="mmr:x"))
    assert result.exception is None and "IB is not connected" in result.error
    assert trader.executioner.placed == []


def test_reduce_only_subtracts_reducing_orders_already_working():
    """D14: a stop whose cancel has not landed still sells 6; a second SELL 10 would reverse the position."""
    trader = _trader(held=10.0)
    trader.client.ib.open_trades = [_working("SELL", 10.0, filled=4.0), _working("BUY", 5.0)]
    refused = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                  order_ref="mmr:x"))
    assert refused.error.startswith("reduce-only refused: quantity 10 is above 4")
    assert _run(trader.place_reduce_only_order(_contract(), "SELL", 4.0, broker_quantity=10.0,
                                               order_ref="mmr:y")).is_success()
    assert [o.totalQuantity for o in trader.executioner.placed] == [4.0]


def test_another_accounts_working_order_does_not_reduce_the_pinned_accounts_capacity():
    """#38: the client cache may hold orders of another account; only the pinned account's count.
    An order with no account is counted: an unknown owner fails closed."""
    trader = _trader(held=10.0)
    trader.client.ib.open_trades = [_working("SELL", 10.0, account="DU99999")]
    assert _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                               order_ref="mmr:x")).is_success()
    trader.client.ib.open_trades = [_working("SELL", 10.0, account="")]
    refused = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, broker_quantity=10.0,
                                                  order_ref="mmr:y"))
    assert refused.error.startswith("reduce-only refused")


# -- Task 14: the dispatch ---------------------------------------------------------------

class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


def _position(quantity=10.0):
    return SimpleNamespace(conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
                           quantity=quantity, account_id=ACCOUNT)


def _dispatch(trader, loop_thread):
    trader._main_loop = loop_thread.loop
    return TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)


def test_dispatch_reduce_position_and_partial_use_the_reduce_only_path(loop_thread):
    trader = _trader()
    dispatch = _dispatch(trader, loop_thread)
    dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:a-reduce-265598-1")
    dispatch.reduce_partial(_position(10.0), "SELL", 4.0, "mmr:p-reduce-265598-1")
    assert [(o.orderType, o.totalQuantity, o.orderRef) for o in trader.executioner.placed] == [
        ("MKT", 10.0, "mmr:a-reduce-265598-1"), ("MKT", 4.0, "mmr:p-reduce-265598-1")]


@pytest.mark.parametrize("call", [
    lambda d: d.reduce_position(_position(10.0), "SELL", 4.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "SELL", 10.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "SELL", 4.5, "mmr:x"),
    lambda d: d.reduce_partial(_position(10.0), "BUY", 4.0, "mmr:x"),
    lambda d: d.reduce_partial(_position(0.0), "SELL", 1.0, "mmr:x"),
])
def test_dispatch_refuses_before_the_boundary_with_dispatch_refused(loop_thread, call):
    trader = _trader()
    with pytest.raises(DispatchRefused) as ex:
        call(_dispatch(trader, loop_thread))
    assert ex.value.code == "REDUCE_ONLY_REFUSED"
    assert trader.executioner.placed == []


class _SpyTrader:
    """Records every order the dispatch schedules on the trader loop."""
    def __init__(self, loop):
        self._main_loop = loop
        self.ib_account = ACCOUNT
        self.scheduled = []

    async def place_reduce_only_order(self, *args, **kwargs):
        self.scheduled.append((args, kwargs))
        raise AssertionError("a malformed close input must never be scheduled")


_MISSING = object()


def _malformed(**fields):
    position = dict(conid=CONID, symbol="AAPL", quantity=10.0, account_id=ACCOUNT)
    position.update(fields)
    return SimpleNamespace(**{k: v for k, v in position.items() if v is not _MISSING})


_MALFORMED_POSITIONS = {
    "no symbol": _malformed(symbol=_MISSING),
    "empty symbol": _malformed(symbol=""),
    "no conid": _malformed(conid=_MISSING),
    "fractional conid": _malformed(conid=265598.5),
    "string conid": _malformed(conid=str(CONID)),
    "bool conid": _malformed(conid=True),
    "no quantity": _malformed(quantity=_MISSING),
    "None quantity": _malformed(quantity=None),
    "NaN quantity": _malformed(quantity=float("nan")),
}


@pytest.mark.parametrize("position", _MALFORMED_POSITIONS.values(), ids=_MALFORMED_POSITIONS.keys())
@pytest.mark.parametrize("call", ["reduce_position", "reduce_partial"])
def test_a_malformed_position_is_refused_before_anything_is_scheduled(loop_thread, call, position):
    """#38, ruling 52: built before the boundary, so DispatchRefused (NOT_SENT), never "maybe sent"."""
    trader = _SpyTrader(loop_thread.loop)
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    with pytest.raises(DispatchRefused) as ex:
        getattr(dispatch, call)(position, "SELL", 10.0 if call == "reduce_position" else 4.0, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


@pytest.mark.parametrize("quantity", [None, "4", True, float("inf")])
@pytest.mark.parametrize("call", ["reduce_position", "reduce_partial"])
def test_a_quantity_that_is_not_a_finite_number_is_refused_before_anything_is_scheduled(loop_thread, call,
                                                                                         quantity):
    """The known minor of round 3: a None quantity raised TypeError, which the close read as maybe sent."""
    trader = _SpyTrader(loop_thread.loop)
    with pytest.raises(DispatchRefused) as ex:
        getattr(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0), call)(
            _malformed(), "SELL", quantity, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


def test_a_trader_refusal_is_dispatch_refused_and_an_ib_rejection_is_not(loop_thread):
    """R2 / R34: a refusal sent nothing (NOT_SENT); an IB rejection was sent (UNKNOWN until its row)."""
    trader = _trader(held=3.0)                      # IB now holds only 3
    with pytest.raises(DispatchRefused) as ex:
        _dispatch(trader, loop_thread).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.executioner.placed == []
    rejected = _trader(verdict="rejected")
    with pytest.raises(BrokerRejectedError) as ex:
        _dispatch(rejected, loop_thread).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert not isinstance(ex.value, DispatchRefused) and len(rejected.executioner.placed) == 1


def test_dispatch_refusals_without_a_running_loop_or_on_the_loop_carry_their_codes(loop_thread):
    """D13: both would block or fail before the order leaves, so both are a proven refusal."""
    trader = _trader()
    trader._main_loop = None
    with pytest.raises(DispatchRefused) as ex:
        TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    assert ex.value.code == "TRADER_LOOP_UNAVAILABLE"
    dispatch = _dispatch(trader, loop_thread)

    async def on_the_loop():
        dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    with pytest.raises(DispatchRefused) as ex:
        asyncio.run_coroutine_threadsafe(on_the_loop(), loop_thread.loop).result(timeout=5)
    assert ex.value.code == "ON_TRADER_LOOP"
    assert trader.executioner.placed == []


def test_cancel_on_loop_reads_the_journal_off_the_loop_and_maps_unresolved_to_refused(loop_thread):
    """The perm id read (DuckDB) runs on the caller's thread; the match and cancelOrder run on the IB loop."""
    trader = _trader()
    threads = {}
    live = SimpleNamespace(permId=77)
    trader.client.ib.open_trades = [SimpleNamespace(order=live, orderStatus=SimpleNamespace(status="Submitted"))]
    trader.client.ib.cancelOrder = lambda order: threads.setdefault("cancel", (threading.get_ident(), order))
    dispatch = _dispatch(trader, loop_thread)

    def perm_id(entity):
        threads["perm"] = threading.get_ident()
        return 77 if entity == "og-1:stop" else 99
    dispatch._perm_id_for_order = perm_id
    dispatch.cancel_on_loop("og-1:stop", "mmr:c")
    assert threads["perm"] == threading.get_ident()
    assert threads["cancel"] == (loop_thread.thread.ident, live)
    with pytest.raises(DispatchRefused) as ex:
        dispatch.cancel_on_loop("og-2:stop", "mmr:c")           # perm id 99 has no live order
    assert ex.value.code == "CANCEL_UNRESOLVED"


def test_liquidation_dispatch_encodes_child_ids_and_reads_evidence():
    from trader.trading.command_stack import _LiquidationDispatch

    calls = []
    inner = SimpleNamespace(
        cancel_on_loop=lambda entity, ref: calls.append(("cancel", entity, ref)),
        reduce_position=lambda p, side, q, ref: calls.append(("reduce", side, q, ref)),
        reduce_partial=lambda p, side, q, ref: calls.append(("reduce_partial", side, q, ref)),
        find_by_order_ref=lambda account, ref: calls.append(("find", account, ref)) or ["row"],
    )
    view = SimpleNamespace(get_order=lambda entity: ("order", entity))
    adapter = _LiquidationDispatch(inner, view)
    pos = SimpleNamespace(conid=CONID, quantity=10.0)
    adapter.cancel(SimpleNamespace(order_entity_id="og-1:stop"), "c-1-cancel-265598-1")
    adapter.reduce(pos, "SELL", 10.0, "c-1-reduce-265598-1")
    adapter.reduce_partial(pos, "SELL", 4.0, "p-1-reduce-265598-1")
    assert adapter.find_orders(ACCOUNT, "c-1-reduce-265598-1") == ["row"]
    assert adapter.get_order("og-1:stop") == ("order", "og-1:stop")
    assert calls == [
        ("cancel", "og-1:stop", "mmr:c-1-cancel-265598-1"),
        ("reduce", "SELL", 10.0, "mmr:c-1-reduce-265598-1"),
        ("reduce_partial", "SELL", 4.0, "mmr:p-1-reduce-265598-1"),
        ("find", ACCOUNT, "mmr:c-1-reduce-265598-1"),
    ]
```

Change master's tests:

```diff
diff --git a/tests/test_order_dispatch_ports.py b/tests/test_order_dispatch_ports.py
index f7b27d1b..d99777c6 100644
--- a/tests/test_order_dispatch_ports.py
+++ b/tests/test_order_dispatch_ports.py
@@ -24,6 +24,7 @@ from trader.data.broker_state import BrokerPositionRow
 from trader.trading.command_coordinator import BrokerRejectedError, CancelAck
 from trader.trading.command_policy import CommandAuthorityPolicy
 from trader.trading.command_ports import CancelUnresolved
+from trader.trading.liquidation_service import DispatchRefused
 from trader.trading.order_correlation import encode_order_ref
 from trader.trading.trading_runtime import TradingRuntimeOrderDispatch
 
@@ -250,9 +251,9 @@ def test_reduce_position_uses_reduce_only_path_and_keeps_exact_size(running_loop
     assert dispatch.reduce_position(_position(10.0), "SELL", 10.0, "mmr:og-x") == ["trade"]
     assert trader.calls == [(1, "AAPL", "SELL", 10.0, 10.0, "mmr:og-x")]
 
-    with pytest.raises(ValueError, match="exactly reduce"):
+    with pytest.raises(DispatchRefused, match="exactly reduce"):
         dispatch.reduce_position(_position(10.0), "SELL", 5.0, "mmr:og-x")
-    with pytest.raises(ValueError, match="exactly reduce"):
+    with pytest.raises(DispatchRefused, match="exactly reduce"):
         dispatch.reduce_position(_position(10.0), "BUY", 10.0, "mmr:og-x")
     assert dispatch.reduce_position(_position(-4.0), "BUY", 4.0, "mmr:og-y") == ["trade"]
     assert trader.calls[-1] == (1, "AAPL", "BUY", 4.0, -4.0, "mmr:og-y")
@@ -263,7 +264,7 @@ def test_reduce_position_refuses_a_position_from_another_account(running_loop):
     loop, _ = running_loop
     trader = _ReduceTrader(loop)
     dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
-    with pytest.raises(ValueError, match="account"):
+    with pytest.raises(DispatchRefused, match="account"):
         dispatch.reduce_position(_position(10.0, account="DU999"), "SELL", 10.0, "mmr:og-x")
     assert trader.calls == []
 
@@ -303,14 +304,23 @@ def test_reduce_position_refuses_on_the_trader_loop_thread(running_loop):
     assert trader.calls == []
 
 
-def test_reduce_position_refusal_is_broker_rejected(running_loop):
+def test_reduce_position_refusal_is_dispatch_refused(running_loop):
+    loop, _ = running_loop
+    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="reduce-only refused: x"))
+    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
+    with pytest.raises(DispatchRefused, match="reduce-only refused"):
+        dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
+
+
+def test_reduce_position_ib_rejection_is_broker_rejected_not_refused(running_loop):
     from trader.trading.command_coordinator import BrokerRejectedError
 
     loop, _ = running_loop
-    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="reduce-only refused: x"))
+    trader = _ReduceTrader(loop, result=SuccessFail.fail(error="Order rejected by IB (entry status=Inactive)"))
     dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
-    with pytest.raises(BrokerRejectedError, match="reduce-only refused"):
+    with pytest.raises(BrokerRejectedError, match="rejected by IB") as raised:
         dispatch.reduce_position(_position(), "SELL", 10.0, "mmr:og-x")
+    assert not isinstance(raised.value, DispatchRefused)
 
 
 def test_reduce_position_ambiguous_send_is_not_broker_rejected(running_loop):
diff --git a/tests/test_trader_service_loops.py b/tests/test_trader_service_loops.py
index bcc45939..42988acf 100644
--- a/tests/test_trader_service_loops.py
+++ b/tests/test_trader_service_loops.py
@@ -268,10 +268,13 @@ def test_startup_liquidation_recovery_without_trader_loop_sends_nothing_late(tmp
         assert time.monotonic() - started < 0.5
 
         [child] = service.receipt_for("root-1").children
-        assert (child.kind, child.state) == ("reduce", "UNKNOWN")
+        assert (child.kind, child.state) == ("reduce", "NOT_SENT")    # a proven refusal (R34)
         trader._main_loop = loop
         loop.run_until_complete(asyncio.sleep(0.05))
-        assert trader.orders == []
+        loop.run_until_complete(loop.run_in_executor(worker, lambda: None))   # a tick in flight has ended
+        # The refused attempt never leaves late; the recovery loop may send a new attempt (R3).
+        sent = [c for c in service.receipt_for("root-1").children if c.state != "NOT_SENT"]
+        assert len(trader.orders) == len(sent) and all(c.attempt > 1 for c in sent)
     finally:
         _close_loop(loop)
 
diff --git a/tests/test_trading_runtime.py b/tests/test_trading_runtime.py
index 90f48751..83570bf7 100644
--- a/tests/test_trading_runtime.py
+++ b/tests/test_trading_runtime.py
@@ -974,6 +974,8 @@ def _reduce_trader(*, live_position=10.0, account='DU12345', paper=True, trade=N
         account=account, contract=SimpleNamespace(conId=1), position=live_position, avgCost=1.0)]
     trader.client = SimpleNamespace(ib=SimpleNamespace(
         positions=lambda acct='': list(positions) if acct == account else [],
+        isConnected=lambda: True,
+        openTrades=lambda: [],
         cancelOrder=lambda order: cancelled.append(order),
         accountValues=lambda: [],
         managedAccounts=lambda: [account],
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py tests/test_order_dispatch_ports.py tests/test_trading_runtime.py tests/test_trader_service_loops.py -q --timeout=30`
Expected: 17 failed, 107 passed — in the new file the connection, R35 (two, one of them the pinned-account test), dispatch and adapter tests (`AttributeError: ... 'reduce_partial'` / `'cancel_on_loop'`, `DispatchRefused` not raised); in master's files the three assertions that now expect `DispatchRefused`, the new IB-rejection test and the loop test that now expects `NOT_SENT`

- [ ] **Step 3: Implement**

```diff
diff --git a/trader/trading/command_stack.py b/trader/trading/command_stack.py
index 12519653..27566665 100644
--- a/trader/trading/command_stack.py
+++ b/trader/trading/command_stack.py
@@ -212,11 +212,14 @@ class _LiquidationDispatch:
         self._orders_view = orders_view
 
     def cancel(self, order, child_id: str) -> None:
-        self._dispatch.cancel(order.order_entity_id, encode_order_ref(child_id))
+        self._dispatch.cancel_on_loop(order.order_entity_id, encode_order_ref(child_id))
 
     def reduce(self, position, side: str, quantity: float, child_id: str) -> None:
         self._dispatch.reduce_position(position, side, quantity, encode_order_ref(child_id))
 
+    def reduce_partial(self, position, side: str, quantity: float, child_id: str) -> None:
+        self._dispatch.reduce_partial(position, side, quantity, encode_order_ref(child_id))
+
     def find_orders(self, account_id: str, child_id: str) -> list:
         return self._dispatch.find_by_order_ref(account_id, encode_order_ref(child_id))
 
diff --git a/trader/trading/trading_runtime.py b/trader/trading/trading_runtime.py
index c3d03cdb..1b2e1738 100644
--- a/trader/trading/trading_runtime.py
+++ b/trader/trading/trading_runtime.py
@@ -65,6 +65,10 @@ class AccountNotPinnedError(Exception):
     """
 
 
+# Prefix of a reduce-only refusal: nothing was sent.
+REDUCE_ONLY_REFUSED = 'reduce-only refused'
+
+
 class Trader():
     def __init__(self,
                  ib_server_address: str,
@@ -1720,7 +1724,7 @@ class Trader():
             refusal = f'pre-send check failed: {ex}'
         if refusal:
             logging.error('reduce-only order refused before send: %s', refusal)
-            return SuccessFail.fail(error=f'reduce-only refused: {refusal}')
+            return SuccessFail.fail(error=f'{REDUCE_ONLY_REFUSED}: {refusal}')
 
         order = MarketOrder(
             action=side, totalQuantity=quantity, account=self.ib_account,
@@ -1734,7 +1738,8 @@ class Trader():
             return SuccessFail.fail(exception=ex)
 
     def _reduce_only_refusal(
-        self, contract: Contract, side: str, quantity: float, broker_quantity: float,
+        self, contract: Contract, side: str, quantity: float, broker_quantity: float, *,
+        oca_group: Optional[str] = None,
     ) -> Optional[str]:
         import math
 
@@ -1744,6 +1749,8 @@ class Trader():
         if self.paper_trading != account.startswith('D'):
             mode = 'paper' if self.paper_trading else 'live'
             return f'ib_account {account!r} does not match trading mode {mode}'
+        if not self.is_ib_connected():
+            return 'IB is not connected'
         if int(contract.conId or 0) <= 0 or not contract.symbol:
             return f'invalid contract (conId={contract.conId!r}, symbol={contract.symbol!r})'
         try:
@@ -1763,8 +1770,30 @@ class Trader():
         if live == 0 or (live > 0) != (broker_quantity > 0) or abs(live) < quantity:
             return (f'live position cache shows {live} for conId {contract.conId}; '
                     f'cannot {side} {quantity} against broker position {broker_quantity}')
+        working = self._working_reduce_quantity(int(contract.conId), reducing_side, oca_group)
+        if quantity > abs(live) - working:
+            return (f'quantity {quantity:g} is above {abs(live) - working:g}: live position {live:g}, '
+                    f'{working:g} already working to reduce it')
         return None
 
+    def _working_reduce_quantity(self, conid: int, reducing_side: str, oca_group: Optional[str]) -> float:
+        """Outstanding quantity of open orders that already reduce this position (R35).
+
+        A stop whose cancel has not landed still sells, so a second reduce of
+        the full position could reverse it. The sibling of ``oca_group`` does
+        not count: one OCA pair protects the same shares once. Only orders of
+        the pinned account count; an order with no account is counted, so an
+        unknown owner fails closed (#38).
+        """
+        return sum(
+            max(float(t.order.totalQuantity) - float(getattr(t.orderStatus, 'filled', 0.0) or 0.0), 0.0)
+            for t in self.client.ib.openTrades()
+            if int(getattr(t.contract, 'conId', 0) or 0) == conid
+            and (getattr(t.order, 'account', '') or self.ib_account) == self.ib_account
+            and t.order.action == reducing_side
+            and not (oca_group and getattr(t.order, 'ocaGroup', '') == oca_group)
+        )
+
     def _live_position_quantity(self, conid: int) -> float:
         """Signed quantity from ib_async's position cache (no IB request)."""
         return sum(
@@ -2471,7 +2500,7 @@ class TradingRuntimeOrderDispatch:
         return CancelAck(order_entity_id=order_entity_id, cancelled=True)
 
     def reduce_position(self, position, side: str, quantity: float, order_ref: str):
-        """Submit an emergency reduce-only market order.
+        """Reduce-only MARKET order for the whole broker position (account flatten, full close).
 
         This intentionally bypasses proposal semantics and the entry gates
         (``Trader.place_reduce_only_order``), but not the trader's one
@@ -2481,57 +2510,115 @@ class TradingRuntimeOrderDispatch:
 
         Must be called off the trader loop (the liquidation worker or an RPC
         thread). Errors:
-        - ``ValueError`` / ``RuntimeError`` before scheduling: nothing sent.
-        - ``BrokerRejectedError``: refused before send, or rejected by IB with
-          nothing filled.
+        - ``DispatchRefused``: refused before send (size, side, account, no
+          running trader loop, a call on the loop, the trader's reduce-only
+          checks). Nothing was sent (R34).
+        - ``BrokerRejectedError``: rejected by IB with nothing filled.
         - any other exception, including ``TimeoutError``: the order may have
           been sent.
         """
+        contract, held, size = self._close_inputs(position, quantity)
+        if side != self._side_for(held) or size != abs(held):
+            self._refuse('liquidation order must exactly reduce the broker position')
+        return self._reduce_only(position, contract, held, side, size, order_ref)
+
+    def reduce_partial(self, position, side: str, quantity: float, order_ref: str):
+        """Reduce-only MARKET order for a whole-share part strictly inside the position."""
+        contract, held, size = self._close_inputs(position, quantity)
+        if side != self._side_for(held):
+            self._refuse('a partial reduce must be on the reducing side of a position')
+        if not size.is_integer() or not 0 < size < abs(held):
+            self._refuse('a partial reduce needs a whole quantity strictly between 0 and the position')
+        return self._reduce_only(position, contract, held, side, size, order_ref)
+
+    def cancel_on_loop(self, order_entity_id: str, order_ref: str):
+        """``cancel`` for the liquidation worker (R34, ruling 7).
+
+        The perm id is read from the journal here, on the calling thread; the
+        open-trade match and ``cancelOrder`` run on the trader loop, so no
+        DuckDB read blocks the IB loop. No live order means nothing was sent:
+        a proven refusal before the boundary.
+        """
+        from trader.trading.command_coordinator import CancelAck
+        from trader.trading.command_ports import CancelUnresolved, resolve_cancel_target
+        from trader.trading.liquidation_service import DispatchRefused
+        perm_id = self._perm_id_for_order(order_entity_id)
+        loop = self._dispatch_loop('cancel')
+
+        async def _cancel():
+            order = resolve_cancel_target(perm_id, self._open_trades())
+            if order is None:
+                raise CancelUnresolved(
+                    f'no live order to cancel for {order_entity_id!r} (perm_id={perm_id})')
+            self._trader.client.ib.cancelOrder(order)
+            return CancelAck(order_entity_id=order_entity_id, cancelled=True)
+        try:
+            return self._wait_on_loop(
+                asyncio.run_coroutine_threadsafe(_cancel(), loop), 'cancel', sent='the cancel')
+        except CancelUnresolved as ex:
+            raise DispatchRefused('CANCEL_UNRESOLVED', str(ex)) from ex
+
+    @staticmethod
+    def _side_for(held: float) -> Optional[str]:
+        return None if held == 0 else ('SELL' if held > 0 else 'BUY')
+
+    @classmethod
+    def _close_inputs(cls, position, quantity) -> tuple:
+        """Contract, broker quantity and order size, built before anything is scheduled (#38, ruling 52).
+
+        A missing field, a conId that is not an exact positive integer or a
+        size that is not a finite number is a refusal: nothing was sent.
+        """
+        try:
+            return (cls._contract_for(position), _finite_number(position.quantity, 'position quantity'),
+                    _finite_number(quantity, 'quantity'))
+        except (AttributeError, TypeError, ValueError) as ex:
+            cls._refuse(f'malformed close input: {ex}')
+
+    @staticmethod
+    def _refuse(detail: str):
+        from trader.trading.liquidation_service import DispatchRefused
+        raise DispatchRefused('REDUCE_ONLY_REFUSED', detail)
+
+    @staticmethod
+    def _contract_for(position) -> Contract:
+        conid, symbol = position.conid, position.symbol
+        if isinstance(conid, bool) or not isinstance(conid, numbers.Integral) or conid <= 0:
+            raise ValueError(f'conId {conid!r} is not a positive integer')
+        if not isinstance(symbol, str) or not symbol:
+            raise ValueError(f'symbol {symbol!r} is missing')
+        return Contract(
+            conId=int(conid), symbol=symbol,
+            secType=getattr(position, 'sec_type', None) or 'STK',
+            exchange=getattr(position, 'exchange', None) or 'SMART',
+            currency=getattr(position, 'currency', None) or 'USD',
+        )
+
+    def _reduce_only(self, position, contract: Contract, held: float, side: str, quantity: float,
+                     order_ref: str, **order):
+        """One reduce-only order on the trader loop; maps the result to the errors above.
+
+        Every argument is already built (``_close_inputs``): only the
+        scheduled coroutine can cross the boundary, so only what follows
+        ``run_coroutine_threadsafe`` may be "maybe sent".
+        """
         from trader.trading.command_coordinator import BrokerRejectedError
 
-        broker_quantity = float(position.quantity)
-        expected_side = 'SELL' if broker_quantity > 0 else 'BUY'
-        if broker_quantity == 0 or side != expected_side or float(quantity) != abs(broker_quantity):
-            raise ValueError('liquidation order must exactly reduce the broker position')
         position_account = getattr(position, 'account_id', None)
         if position_account and position_account != getattr(self._trader, 'ib_account', None):
-            raise ValueError('liquidation position account does not match trader account')
+            self._refuse('liquidation position account does not match trader account')
         loop = self._dispatch_loop('liquidation')
-        contract = Contract(
-            conId=int(position.conid), symbol=position.symbol,
-            secType=position.sec_type or 'STK', exchange=position.exchange or 'SMART',
-            currency=position.currency or 'USD',
-        )
         future = asyncio.run_coroutine_threadsafe(
             self._trader.place_reduce_only_order(
-                contract, side, abs(broker_quantity),
-                broker_quantity=broker_quantity, order_ref=order_ref,
+                contract, side, quantity, broker_quantity=held, order_ref=order_ref, **order,
             ), loop,
         )
         result = self._wait_on_loop(future, 'liquidation dispatch')
         if result.is_success():
             return result.obj or []
         if result.error is not None:
+            if str(result.error).startswith(REDUCE_ONLY_REFUSED):
+                self._refuse(str(result.error))
             raise BrokerRejectedError(str(result.error))
         if result.exception is not None:
             raise result.exception
         raise RuntimeError('liquidation dispatch failed with no detail; the order may have been sent')
 
     def _dispatch_loop(self, purpose: str) -> asyncio.AbstractEventLoop:
-        """The trader loop, or RuntimeError before anything is scheduled.
+        """The trader loop, or ``DispatchRefused`` before anything is scheduled.
 
         A stopped loop would run the order late, after the caller gave up; a
         call from the loop thread would block the loop it waits on.
         """
+        from trader.trading.liquidation_service import DispatchRefused
         loop = getattr(self._trader, '_main_loop', None)
         if loop is None or not loop.is_running():
-            raise RuntimeError(f'trader event loop is not running; {purpose} refused, nothing sent')
+            raise DispatchRefused(
+                'TRADER_LOOP_UNAVAILABLE', f'trader event loop is not running; {purpose} refused, nothing sent')
         try:
             running = asyncio.get_running_loop()
         except RuntimeError:
             running = None
         if running is loop:
-            raise RuntimeError(
+            raise DispatchRefused(
+                'ON_TRADER_LOOP',
                 f'{purpose} called on the trader loop thread; refused to avoid a deadlock, nothing sent')
         return loop
 
```

Add `import math` and `import numbers` to the module imports (the module-level `math` replaces the local `import math` in `_reduce_only_refusal`), and this helper right before `class TradingRuntimeOrderDispatch` (ruling 52):

```python
def _finite_number(value, label: str) -> float:
    """A finite real number; ``None``, a bool or a string is refused, never coerced."""
    if value is None or isinstance(value, (bool, str, bytes)):
        raise ValueError(f'{label} {value!r} is not a number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{label} {value!r} is not finite')
    return number
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py tests/test_order_dispatch_ports.py tests/test_trading_runtime.py tests/test_trader_service_loops.py tests/test_liquidation_service.py tests/test_production_rpc_security.py tests/test_cancel_command.py -q --timeout=30`
Expected: all PASS (13 in the new file). Then the full suite: green.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_reduce_only_order_path.py tests/test_order_dispatch_ports.py tests/test_trading_runtime.py tests/test_trader_service_loops.py
git commit -m "fix: reduce-only exits refuse before the boundary as dispatch refused

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 15: Share master's liquidation worker with every producer (R12)

PR #42 already runs every `LiquidationService` / `SessionController` call that `trader_service` makes on one worker thread (`_new_liquidation_worker`, `_on_worker`): the startup rescan and the session `recover` run there inside `run_until_complete` (so their orders find a running trader loop before `trader.run()`), the periodic ticks run there through `_watched_ticks` (a stuck tick is logged CRITICAL and never stacked; `_BusyStreak` reports a lock held elsewhere), and shutdown during startup is handled (`stopping()`, `_finish_startup_shutdown`). `tests/test_trader_service_loops.py` pins it on a real loop. That code is kept as it is (ruling 36).

What is still missing is that the *other* producers use the same thread: typed RPC threads (`/flatten` → `liquidate`), the session controller's own calls (they already run on the worker), and the broker ingest thread (protective failure → `start`) which holds the ingest apply lock and so must not wait (ruling 8). This task:

- adds `trader/trading/liquidation_worker.py`: `LiquidationWorker` (a single-thread `ThreadPoolExecutor`, so `run_in_executor` takes it; `call` runs inline on the worker, blocks on a plain thread, and refuses on an event loop) and `SerializedLiquidation` (the facade the stack, the trader and the producers hold). Its `rescan` first starts a flatten for every unhandled protective failure (R29; one failing saga does not stop the others), so master's recovery tick, which calls `service.rescan` on the worker, picks them up unchanged;
- builds the worker and the facade in `build_command_stack`, gives the saga `nonblocking()`, and exposes `trader.liquidation_worker`;
- makes `trader_service.main` use that worker after `trader.connect()` built the stack (`_shared_liquidation_worker`), so there is one liquidation thread, and makes `_new_liquidation_worker` return a `LiquidationWorker`.

`SessionController.restore` is not added: master's startup already runs `recover` on the worker while the loop runs.

**Files:**
- Create: `trader/trading/liquidation_worker.py`
- Modify: `trader/trader_service.py` (`_new_liquidation_worker`, new `_shared_liquidation_worker`, `main` after `trader.connect()`)
- Modify: `trader/trading/command_stack.py` (wrap the service; `CommandStack.liquidation_worker`; the saga's `liquidation=`)
- Test: `tests/test_liquidation_worker.py` (create)

**Interfaces:**

```python
class LiquidationWorker(ThreadPoolExecutor):       # max_workers=1
    def __init__(self, name: str = "liquidation-worker")
    def in_worker(self) -> bool
    def call(self, fn, *args, **kwargs)            # waits; inline on the worker; RuntimeError on a running loop
    async def run_async(self, fn, *args, **kwargs)

class SerializedLiquidation:                       # what the stack, trader and producers hold
    def __init__(self, service, worker, *, account_id, now, deadline_seconds=300.0)
    worker: LiquidationWorker                      # property
    attach_protection(saga) -> None                # protection port + source of unhandled failures (Task 13)
    start(...) / upgrade_to_zero(root_id) / liquidate(cmd)     # on the worker, waiting
    rescan() (alias tick()) / async tick_async()   # flatten unhandled SAFETY_FAILED sagas, then rescan
    start_nowait(account_id, cause_command_id, deadline) -> Future
    nonblocking() -> object with start(account_id, cause_command_id, deadline) -> None   # for the saga
    async run_async(fn, *args, **kwargs)           # run another component on the worker
    receipt_for(root_id) / root_for(command_id) / close_resolution(command_id)          # reads, any thread
trader_service._shared_liquidation_worker(trader, fallback) -> LiquidationWorker
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_liquidation_worker.py
"""SP1 plan 1 Task 15 (R12): one serialized worker; never block the IB event loop."""
import asyncio
import datetime as dt
import threading
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_stack import _LiquidationDispatch
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import LiquidationRunStore, LiquidationService, apply_liquidation_migration
from trader.trading.liquidation_worker import LiquidationWorker, SerializedLiquidation
from trader.trading.trading_runtime import Trader, TradingRuntimeOrderDispatch

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU12345"
CONID = 265598


def _snapshot(generation, quantity):
    positions = () if not quantity else (BrokerPositionRow(
        account_id=ACCOUNT, conid=CONID, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=None, market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, deleted=False, revision=1, source_timestamp=NOW),)
    return BrokerRiskSnapshot(generation_id=generation, source_cursor=generation, promoted_at=NOW,
                              account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
                              daily_pnl=0, positions=positions, working_orders=())


class _Broker:
    def __init__(self, snapshots, lock=None):
        self.snapshots, self.lock = list(snapshots), lock
        self.last = 0

    def capture(self, account_id):
        if self.lock is not None:
            if not self.lock.acquire(timeout=2):
                raise TimeoutError("capture could not get the ingest lock")
            self.lock.release()
        snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        self.last = snapshot.generation_id
        return snapshot


def _evidence_dispatch(broker, **orders):
    """A dispatch fake: order methods from ``orders``, broker evidence from ``broker``."""
    return SimpleNamespace(find_orders=lambda *a: [], get_order=lambda e: None,
                           enumeration_complete=lambda: True, newest_generation=lambda: broker.last, **orders)


class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=5.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


def _real_dispatch_trader(loop, held):
    """A Trader whose reduce-only path runs for real against a fake IB stream."""
    import reactivex as rx
    trader = object.__new__(Trader)
    trader.ib_account = ACCOUNT
    trader.paper_trading = True
    trader._main_loop = loop
    trader.client = SimpleNamespace(ib=SimpleNamespace(
        positions=lambda account=None: [
            SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=held)],
        isConnected=lambda: True, openTrades=lambda: []))
    placed = []

    class _Executioner:
        async def subscribe_place_order_direct(self, contract, order):
            order.orderId = 500 + len(placed)
            placed.append(order)
            echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
            ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted"))
            return rx.from_iterable([echo, ack])
    trader.executioner = _Executioner()
    return trader, placed


def _service(tmp_path, broker, dispatch):
    db = DuckDBConnection.get_instance(str(tmp_path / "worker.duckdb"))
    migrator = SchemaMigrator(db)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    return LiquidationService(broker, dispatch, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                              now=lambda: NOW)


def test_rescan_from_a_coroutine_on_a_real_loop_does_not_deadlock(tmp_path, loop_thread):
    """R12 / #26: the tick awaits the worker, and the worker's order runs on the same loop."""
    trader, placed = _real_dispatch_trader(loop_thread.loop, held=10.0)
    view = SimpleNamespace(get_order=lambda entity: None)
    broker = _Broker([_snapshot(1, 0.0), _snapshot(2, 10.0)])
    dispatch = _LiquidationDispatch(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0), view)
    dispatch.find_orders = lambda account, child: []
    dispatch.newest_generation = lambda: broker.last
    service = _service(tmp_path, broker, dispatch)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.start(ACCOUNT, "flat-1", NOW + dt.timedelta(minutes=5))      # flat at generation 1: no order

    async def tick_on_the_loop():
        return await liquidation.tick_async()
    receipt = loop_thread.run(tick_on_the_loop())
    assert [o.orderRef for o in placed] == ["mmr:flat-1-reduce-265598-1"]
    assert receipt.children[0].state == "UNKNOWN"


def test_a_blocking_liquidation_call_on_the_loop_is_refused_not_deadlocked(tmp_path, loop_thread):
    service = SimpleNamespace(rescan=lambda: None, attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)

    async def blocking_on_the_loop():
        liquidation.rescan()
    with pytest.raises(RuntimeError, match="deadlock"):
        loop_thread.run(blocking_on_the_loop())


def test_ingest_thread_holding_the_apply_lock_does_not_deadlock(tmp_path):
    """The protective-failure producer queues the flatten; it never waits on the worker."""
    apply_lock = threading.Lock()
    broker = _Broker([_snapshot(1, 0.0)], lock=apply_lock)
    service = _service(tmp_path, broker, _evidence_dispatch(broker, reduce=lambda *a: None))
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    saga_port = liquidation.nonblocking()

    def ingest_thread():
        with apply_lock:
            saga_port.start(ACCOUNT, "entry-1", NOW + dt.timedelta(minutes=5))
    t = threading.Thread(target=ingest_thread)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    liquidation.worker.submit(lambda: None).result(timeout=5)   # FIFO: the queued start has run
    assert liquidation.root_for("entry-1") == "entry-1"


def test_every_entry_point_runs_on_the_one_worker_thread():
    seen = []

    def record(*_a, **_k):
        seen.append(threading.get_ident())
    service = SimpleNamespace(start=record, rescan=record, upgrade_to_zero=record, liquidate=record,
                              root_for=lambda c: None, attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    threads = [threading.Thread(target=f) for f in (
        lambda: liquidation.start(ACCOUNT, "a", NOW), lambda: liquidation.rescan(),
        lambda: liquidation.upgrade_to_zero("a"), lambda: liquidation.liquidate(object()))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(seen) == 4 and len(set(seen)) == 1 and seen[0] != threading.get_ident()


def test_tick_starts_a_flatten_for_an_unhandled_safety_failed_saga_once(tmp_path):
    broker = _Broker([_snapshot(1, 0.0)])
    service = _service(tmp_path, broker, _evidence_dispatch(broker, reduce=lambda *a: None))
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.attach_protection(SimpleNamespace(
        unhandled_failures=lambda account: ["entry-1"],
        handover_account=lambda **_k: None, close_after_full=lambda **_k: None))
    liquidation.tick()
    liquidation.tick()
    assert liquidation.root_for("entry-1") == "entry-1"
    assert liquidation.receipt_for("entry-1").scope == "account"


def test_trader_service_recovery_loop_ticks_on_the_shared_worker_inline(loop_thread):
    """PR #42's recovery loop runs ``rescan`` on its worker; with the shared worker the facade runs inline."""
    from trader import trader_service

    ticks = []
    worker = LiquidationWorker()
    service = SimpleNamespace(rescan=lambda: ticks.append(threading.get_ident()), root_for=lambda c: None,
                              attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, worker, account_id=ACCOUNT, now=lambda: NOW)

    async def one_tick():
        task = asyncio.ensure_future(trader_service._liquidation_recovery_loop(liquidation, worker, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
    loop_thread.run(one_tick())
    assert ticks and all(t == ticks[0] for t in ticks) and ticks[0] != loop_thread.thread.ident
    assert worker.submit(threading.get_ident).result(timeout=5) == ticks[0]


def test_trader_service_uses_the_worker_the_command_stack_built():
    from trader import trader_service

    built, fallback = LiquidationWorker(), LiquidationWorker()
    assert trader_service._shared_liquidation_worker(SimpleNamespace(liquidation_worker=built), fallback) is built
    assert fallback._shutdown
    other = LiquidationWorker()
    assert trader_service._shared_liquidation_worker(SimpleNamespace(), other) is other


def test_tick_keeps_going_when_one_protective_failure_cannot_start(tmp_path):
    """D8: an error for one saga is logged; the other sagas and the rescan still run."""
    starts, rescans = [], []

    def start(account_id, cause, deadline):
        if cause == "broken":
            raise RuntimeError("cannot claim")
        starts.append(cause)
    service = SimpleNamespace(start=start, rescan=lambda: rescans.append(1), root_for=lambda c: None,
                              attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.attach_protection(SimpleNamespace(unhandled_failures=lambda account: ["broken", "entry-2"]))
    liquidation.tick()
    assert (starts, rescans) == (["entry-2"], [1])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_worker.py -q --timeout=30`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.trading.liquidation_worker'`.

- [ ] **Step 3: Implement the worker**

```python
# trader/trading/liquidation_worker.py
"""One serialized worker for every LiquidationService entry point (R12).

The service sends orders with ``run_coroutine_threadsafe(...).result()`` onto
the trader loop. That wait is safe only off the loop, so nothing may run the
service on the loop. ``trader_service`` already runs its recovery and session
ticks on a single worker thread (PR #42); this module makes that worker the
one every producer shares, through ``SerializedLiquidation``:

- trader_service's ticks run on the worker (``run_in_executor``); a call
  made there runs inline;
- a thread with no running loop (RPC handler) calls ``start`` and blocks;
- a coroutine on an event loop awaits ``run_async``; a blocking call there
  is refused;
- the broker ingest thread uses ``start_nowait``: it holds the ingest apply
  lock, and the worker's broker snapshot needs that lock, so it must not wait.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional


class LiquidationWorker(ThreadPoolExecutor):
    """The single liquidation thread. An executor, so ``loop.run_in_executor`` can use it."""

    def __init__(self, name: str = "liquidation-worker"):
        self._thread_id: Optional[int] = None
        super().__init__(max_workers=1, thread_name_prefix=name, initializer=self._remember_thread)

    def _remember_thread(self) -> None:
        self._thread_id = threading.get_ident()

    def in_worker(self) -> bool:
        return threading.get_ident() == self._thread_id

    def call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Run on the worker and wait. Inline when already on the worker."""
        if self.in_worker():
            return fn(*args, **kwargs)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.submit(fn, *args, **kwargs).result()
        raise RuntimeError("a blocking liquidation call on an event loop would deadlock; await run_async")

    async def run_async(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))


class SerializedLiquidation:
    """``LiquidationService`` behind the worker. Reads go straight to the journal."""

    def __init__(self, service, worker: LiquidationWorker, *, account_id: str,
                 now: Callable[[], dt.datetime], deadline_seconds: float = 300.0):
        self._service = service
        self._worker = worker
        self._account_id = account_id
        self._now = now
        self._deadline_seconds = deadline_seconds
        self._saga = None

    @property
    def worker(self) -> LiquidationWorker:
        return self._worker

    def attach_protection(self, saga) -> None:
        """The saga is both the protection port and the source of unhandled failures."""
        self._saga = saga
        self._service.attach_protection(saga)

    # -- entry points (serialized) ----------------------------------------------------

    def start(self, *args, **kwargs):
        return self._worker.call(self._service.start, *args, **kwargs)

    def rescan(self):
        """Start a flatten for every unhandled protective failure, then rescan every root.

        trader_service's recovery tick calls this on the worker, so the
        failures are picked up on every tick.
        """
        return self._worker.call(self._tick)

    tick = rescan

    async def tick_async(self):
        return await self._worker.run_async(self._tick)

    def upgrade_to_zero(self, root_id: str):
        return self._worker.call(self._service.upgrade_to_zero, root_id)

    def liquidate(self, cmd):
        return self._worker.call(self._service.liquidate, cmd)

    def start_nowait(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> Future:
        future = self._worker.submit(self._service.start, account_id, cause_command_id, deadline)
        future.add_done_callback(_log_failure)
        return future

    def nonblocking(self) -> "_NonBlockingStart":
        return _NonBlockingStart(self)

    async def run_async(self, fn: Callable[..., Any], *args, **kwargs):
        """Run another component (the session controller) on the same worker."""
        return await self._worker.run_async(fn, *args, **kwargs)

    def _tick(self):
        if self._saga is not None:
            for command_id in self._saga.unhandled_failures(self._account_id):
                self._flatten_failure(command_id)
        return self._service.rescan()

    def _flatten_failure(self, command_id: str) -> None:
        """One failed saga must not stop the others or the rescan (D8)."""
        try:
            if self._service.root_for(command_id) is None:
                self._service.start(self._account_id, command_id,
                                    self._now() + dt.timedelta(seconds=self._deadline_seconds))
        except Exception:
            logging.getLogger(__name__).exception(
                "could not start the flatten for protective failure %s", command_id)

    # -- reads (any thread) --------------------------------------------------------------

    def receipt_for(self, root_id: str):
        return self._service.receipt_for(root_id)

    def root_for(self, command_id: str):
        return self._service.root_for(command_id)

    def close_resolution(self, command_id: str):
        return self._service.close_resolution(command_id)


class _NonBlockingStart:
    """What the protective saga gets: ``start`` queues the flatten and returns at once."""

    def __init__(self, serialized: SerializedLiquidation):
        self._serialized = serialized

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime) -> None:
        self._serialized.start_nowait(account_id, cause_command_id, deadline)


def _log_failure(future: Future) -> None:
    exc = future.exception()
    if exc is not None:
        logging.getLogger(__name__).error("queued liquidation start failed: %s", exc)
```

(`rescan` uses `unhandled_failures`, which the saga gets in Task 9; until Task 13 attaches the saga, `_saga` is `None` and the tick only rescans.)

- [ ] **Step 4: Share it**

```diff
diff --git a/trader/trader_service.py b/trader/trader_service.py
index ef093fbe..2a599cd7 100644
--- a/trader/trader_service.py
+++ b/trader/trader_service.py
@@ -5,6 +5,7 @@ from trader.common.logging_helper import LogLevels, set_all_log_level, setup_log
 from trader.container import Container, default_config_path
 from trader.data.schema_migrations import SchemaMigrator
 from trader.trading.liquidation_service import LiquidationBusy
+from trader.trading.liquidation_worker import LiquidationWorker
 from trader.trading.trading_control import TradingControlStore, apply_trading_control_migration
 from trader.trading.trading_runtime import Trader
 
@@ -191,8 +192,19 @@ def _new_liquidation_worker() -> concurrent.futures.ThreadPoolExecutor:
     order wait times out, so process exit can be delayed by at most about the
     dispatch timeout (30s).
     """
-    return concurrent.futures.ThreadPoolExecutor(
-        max_workers=1, thread_name_prefix='liquidation-worker')
+    return LiquidationWorker()
+
+
+def _shared_liquidation_worker(trader, fallback):
+    """The worker the command stack built (R12), so RPC, ingest and these ticks share one thread.
+
+    ``fallback`` is shut down when the stack has its own worker.
+    """
+    built = getattr(trader, 'liquidation_worker', None)
+    if built is None:
+        return fallback
+    fallback.shutdown(wait=False)
+    return built
 
 
 async def _on_worker(worker, fn, *args):
@@ -491,6 +503,8 @@ def main(simulation: bool,
         loop.add_signal_handler(signal.SIGTERM, handle_sigint)
 
         trader.connect()
+        # R12: connect() built the command stack and its liquidation worker.
+        liquidation_worker = _shared_liquidation_worker(trader, liquidation_worker)
 
         # [M1-F3] Task 4: the durable per-account pause gate must be seeded
         # before this service is considered ready -- an exposure-increasing
diff --git a/trader/trading/command_stack.py b/trader/trading/command_stack.py
index 27566665..5c988280 100644
--- a/trader/trading/command_stack.py
+++ b/trader/trading/command_stack.py
@@ -47,6 +47,7 @@ from trader.trading.circuit_breaker import CircuitBreaker
 from trader.trading.circuit_breaker import BreakerSignal
 from trader.trading.exit_owner import ExitOwnerRegistry
 from trader.trading.liquidation_service import LiquidationService, LiquidationRunStore, apply_liquidation_migration
+from trader.trading.liquidation_worker import LiquidationWorker, SerializedLiquidation
 from trader.trading.order_correlation import encode_order_ref
 from trader.trading.semantic_readiness import (
     SemanticReadiness,
@@ -418,8 +419,9 @@ class CommandStack:
     reconciliation_complete: Callable[[str], bool]
     circuit_breaker: CircuitBreaker
     semantic_readiness: SemanticReadiness
-    liquidation_service: LiquidationService
+    liquidation_service: Any  # SerializedLiquidation: every entry point on one worker (R12)
     session_risk: Any = None  # SessionRiskController when automation stack is active
+    liquidation_worker: Any = None  # LiquidationWorker behind liquidation_service (R12)
     protective_order_saga: Any = None  # ProtectiveOrderSaga (P3 Task 5)
     session_controller: Any = None  # SessionController (P3 Task 6)
     attribution_ledger: Any = None  # AttributionLedger (P3 Task 7)
@@ -824,12 +826,16 @@ def build_command_stack(
             now=now(),
         ),
     )
-    liquidation_service = LiquidationService(
-        broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
-        store=liquidation_store, registry=exit_owner_registry, now=now,
-        breaker=_LiquidationBreaker(circuit_breaker, now),
-        journal=journal, ledger=ledger,
-        schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
+    liquidation_worker = LiquidationWorker()
+    liquidation_service = SerializedLiquidation(
+        LiquidationService(
+            broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
+            store=liquidation_store, registry=exit_owner_registry, now=now,
+            breaker=_LiquidationBreaker(circuit_breaker, now),
+            journal=journal, ledger=ledger,
+            schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
+        ),
+        liquidation_worker, account_id=trader.ib_account, now=now,
     )
     proposal_service = ProposalCommandService(
         repository=repository,
@@ -897,7 +903,8 @@ def build_command_stack(
         dispatch_guard=dispatch_guard,
         session_risk=session_risk,
         breaker=circuit_breaker,
-        liquidation=liquidation_service,
+        # The ingest thread reports protection failures; it must queue the flatten, not wait (R12).
+        liquidation=liquidation_service.nonblocking(),
         account_id=trader.ib_account,
         account_mode=account_mode,
         now=now,
@@ -1040,6 +1047,7 @@ def build_command_stack(
         circuit_breaker=circuit_breaker,
         semantic_readiness=semantic_readiness,
         liquidation_service=liquidation_service,
+        liquidation_worker=liquidation_worker,
         session_risk=session_risk,
         protective_order_saga=protective_order_saga,
         session_controller=session_controller,
@@ -1108,6 +1116,7 @@ def build_command_stack(
     trader.automation_circuit_breaker = circuit_breaker
     trader.semantic_readiness = semantic_readiness
     trader.liquidation_service = liquidation_service
+    trader.liquidation_worker = liquidation_worker
     trader.session_risk = session_risk
     trader.protective_order_saga = protective_order_saga
     trader.session_controller = session_controller
```

The session controller keeps `liquidation=liquidation_service` (the facade): its `run_due` runs on the worker, where the facade calls the service inline.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_worker.py tests/test_trader_service_loops.py tests/automation/test_session_controller.py tests/test_command_stack.py tests/test_liquidation_service.py -q --timeout=60`
Expected: all PASS (8 in the new file; `test_trader_service_starts_session_recovery_before_readiness` still passes). Then the full suite: green.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/liquidation_worker.py trader/trader_service.py trader/trading/command_stack.py tests/test_liquidation_worker.py
git commit -m "fix: share one liquidation worker between rpc, ingest and the service loops

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 5: Scoped full close (`scope="conid"`, goal zero)

`REQUESTED → CANCELLING → VERIFYING → REDUCING → VERIFYING → CLOSED`. Before the first cancel the service hands protection over (`ProtectionOwnershipPort.handover` with the exact order ids it will cancel), and it hands over again before every later cancel batch, with the new ids (R27). Cancels and the reduce are journaled children; nothing new is sent while a child is unknown; the reduce is sized only from a fresh generation; routine progress never trips the breaker. A second full close on the same conid joins the owner (one root, one order); the full join/upgrade/refuse rules are pinned in Task 8.

A conid root journals and honours the pre-upgrade wildcard children of ruling 42 like an account root: Task 4's `_track_pre_sp1_reduces` and `_blocking` run for every scope, so `CLOSED` and every reduce wait while any wildcard child of the account is unsettled, also for a conid the old run may never have touched. A conid root never clears another run's mark early; only the settle does. The last two tests below cover a scoped close of the old reduce's conid and of another conid.

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`start`, `_tick`; new `_claim_scoped_in_tx`, `_position_for`, `_working_for`, `_cancel_conid_orders`, `_advance_conid`, `_exact_conid`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: Task 4 frozen model; `ExitOwnerRegistry.claim_scoped_in_tx`; `ProtectionOwnershipPort.handover`.
- Produces: `start(..., scope="conid", conid=...)` returns the receipt of the root the caller must poll (another root after a join). `phase` goes `"" → "cancel" → "reduce"`. Until Task 6, a request with `quantity` raises `LiquidationRefused("PARTIAL_CLOSE_UNAVAILABLE")` before anything is written (ruling 18). A `conid` that is not an exact positive integer (`numbers.Integral`, not a `bool`) raises `LiquidationRefused("CONID_INVALID")` before any claim, read or order: `1.5`, `True` and `"1"` are never coerced (ruling 51).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_liquidation_service.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: conid-scoped full close
# ---------------------------------------------------------------------------

def _stop_order(entity="stop-1", conid=1, group="og-entry-1", quantity=10.0):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL",
        order_group_id=group, leg="stop", is_external=False, action="SELL", order_type="STP",
        total_quantity=quantity, filled_quantity=0, avg_fill_price=None, limit_price=None,
        stop_price=95.0, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def test_full_close_hands_over_then_cancels_only_that_conids_orders(tmp_path):
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position(), _position(5.0, conid=2)],
                                    [_stop_order(), _stop_order("stop-2", conid=2, group="og-entry-2")])],
               protection=protection)
    receipt = s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert protection.calls[0] == ("handover", 1, "close-1", ("stop-1",))
    assert (receipt.state, receipt.phase) == ("VERIFYING", "cancel")
    assert s.dispatch.calls == [("cancel", "stop-1", "close-1-cancel-1-1")]
    child = receipt.children[0]
    assert (child.kind, child.target_order_entity_id, child.fence_generation) == ("cancel", "stop-1", 1)
    assert s.breaker.calls == []


def test_full_close_reduces_only_after_the_cancel_is_terminal(tmp_path):
    """D2: the stop's own row is the evidence. Cancelled with no fill allows the reduce, even on the same generation."""
    s = _stack(tmp_path, [
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], []),
    ], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _stop_order()                # the cancel has not landed yet
    assert "still working" in s.service.rescan().detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    receipt = s.service.rescan()
    assert receipt.phase == "reduce"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 10.0, "close-1-reduce-1-1")


def test_full_close_waits_while_the_cancelled_stop_is_invisible(tmp_path):
    """#21: the stop's row is gone on a generation that opened before the cancel; that proves nothing."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], [])],
               protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.staging = 0
    receipt = s.service.rescan()
    assert "outcome unknown" in receipt.detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_invisible_reduce_child_on_a_newer_generation_gets_no_second_scoped_reduce(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])], protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "close-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.staging = 0
    s.service.rescan()
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_filled_callback_after_position_capture_gets_no_second_scoped_reduce(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_full_close_ends_closed_and_releases_owner_and_saga(tmp_path):
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=protection)
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "close-1")
    assert s.registry.get("close-1").state == "RELEASED"
    assert s.breaker.calls == []


def test_close_ends_closed_without_reduce_when_the_stop_filled_in_the_cancel_race(tmp_path):
    """Review focus 3."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [], []), _snapshot(3, [], [])],
               protection=protection)
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Filled", filled=10.0)
    assert s.service.rescan().state == "VERIFYING"           # the stop fill is a fill: wait one more generation
    assert s.service.rescan().state == "CLOSED"
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]
    assert ("close_after_full", "close-1") in protection.calls


def test_full_close_of_short_reduces_with_buy(tmp_path):
    """Review focus 1."""
    s = _stack(tmp_path, [_snapshot(1, [_position(-7.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert s.dispatch.calls == [("reduce", 1, "BUY", 7.0, "close-1-reduce-1-1")]


def test_full_close_ignores_another_conids_position(tmp_path):
    other = _position(5.0, conid=2)
    s = _stack(tmp_path, [_snapshot(1, [_position(), other]), _snapshot(2, [other]), _snapshot(3, [other])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert all(c[1] == 1 for c in s.dispatch.calls)


def test_scoped_close_deadline_is_failed_safe_and_trips_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    receipt = s.service.start(ACCOUNT, "close-1", NOW, scope="conid", conid=1)
    assert receipt.state == "FAILED_SAFE"
    assert s.dispatch.calls == []
    assert s.breaker.calls == [("close-1", receipt.detail)]


def test_cancel_rejected_by_the_broker_ends_failed_safe_without_reduce(tmp_path):
    """Spec test list: cancel rejected. The stop stays working; no reduce is ever sent."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], [_stop_order()])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Submitted")
    assert "still working" in s.service.rescan().detail
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_routine_scoped_progress_never_trips_the_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], []),
                          _snapshot(3, []), _snapshot(4, [])], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    s.service.rescan()
    s.dispatch.rows["close-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert s.breaker.calls == []


def test_start_refuses_rebinding_root_to_another_scope(tmp_path):
    """Review focus 4."""
    s = _stack(tmp_path, [_snapshot(1, [_position(), _position(5.0, conid=2)])], protection=_Protection())
    s.service.start(ACCOUNT, "root-1", DEADLINE)
    with pytest.raises(ValueError):
        s.service.start(ACCOUNT, "root-1", DEADLINE, scope="conid", conid=1)
    s.service.start(ACCOUNT, "root-2", DEADLINE, scope="conid", conid=2)
    with pytest.raises(ValueError):
        s.service.start(ACCOUNT, "root-2", DEADLINE, scope="conid", conid=1)


def test_an_old_flat_runs_late_reduce_blocks_a_scoped_close(tmp_path):
    """Ruling 49 on the conid scope: the old FLAT run's wildcard child blocks a full close too."""
    _legacy_db(tmp_path, (("old-flat", "FLAT", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position()]), _snapshot(6, [_position()])], protection=_Protection())
    s.dispatch.rows["old-flat-liquidation-reduce-1"] = [_row("Submitted", entity="old-flat-liquidation-reduce-1:exit")]
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert "still working" in s.service.rescan().detail
    assert s.dispatch.calls == []


@pytest.mark.parametrize("conid", [1.5, True, "1", 0, -1, None], ids=repr)
def test_a_conid_that_is_not_an_exact_positive_integer_is_refused_before_any_claim(tmp_path, conid):
    """#21, ruling 51: 1.5, True or "1" never become conId 1; nothing is claimed, read or sent."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    with pytest.raises(LiquidationRefused) as ex:
        s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=conid)
    assert ex.value.code == "CONID_INVALID"
    assert s.service.root_for("close-1") is None and s.registry.owner_for(ACCOUNT, 1) is None
    assert (s.broker.calls, s.dispatch.calls) == (0, [])


def test_an_old_reduce_of_a_flat_position_blocks_a_scoped_close_of_that_conid(tmp_path):
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    s = _stack(tmp_path, [_snapshot(5, [_position(0.0)]), _snapshot(6, [_position(0.0)])])
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    receipt = s.service.rescan()
    assert [(c.child_id, c.owner_root_id, c.state) for c in receipt.children] == [
        ("flat-1-liquidation-reduce-*", "close-1", "UNKNOWN")]
    assert receipt.state != "CLOSED" and _pre_sp1_open(s.db, "flat-1")
    s.push(_snapshot(7, [_position(0.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [_row("Submitted", entity="flat-1-liquidation-reduce-1:exit")]
    assert "still working" in s.service.rescan().detail
    s.push(_snapshot(8, [_position(-10.0)]), _snapshot(9, [_position(-10.0)]))
    s.dispatch.rows["flat-1-liquidation-reduce-1"] = [
        _row("Filled", filled=10.0, entity="flat-1-liquidation-reduce-1:exit")]
    s.dispatch.complete = True
    s.service.rescan()                                       # 8: settled FILLED; the late fill opened a short
    assert not _pre_sp1_open(s.db, "flat-1")
    assert s.dispatch.calls == []                            # 8 is not newer than the fill it observed
    s.service.rescan()                                       # 9: the close reduces the short it now sees
    assert s.dispatch.calls == [("reduce", 1, "BUY", 10.0, "close-1-reduce-1-1")]


def test_the_wildcard_blocks_a_scoped_close_of_another_conid(tmp_path):
    """Ruling 42: the scoped root on conid 2 never looks at conid 1. The old run's reduce of conid 2
    is settled, but its reduce of conid 1 is not, so the run is not settled and nothing is reduced."""
    _legacy_db(tmp_path, (("flat-1", "FAILED_SAFE", 1),))
    held = [_position(0.0), _position(10.0, conid=2)]
    s = _stack(tmp_path, [_snapshot(5, held), _snapshot(6, held), _snapshot(7, held)])
    s.dispatch.complete = False
    s.dispatch.rows["flat-1-liquidation-reduce-2"] = [_row("Cancelled", entity="flat-1-liquidation-reduce-2:exit")]
    s.service.start(ACCOUNT, "close-2", DEADLINE, scope="conid", conid=2)
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls == []
    assert _pre_sp1_open(s.db, "flat-1")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 15 failed (`ValueError: unknown liquidation scope 'conid'`), 51 passed.

- [ ] **Step 3: Implement**

Replace `start` and `_tick` with:

```python
    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, *,
              scope: str = "account", conid: Optional[int] = None, quantity: Optional[float] = None,
              stop_price: Optional[float] = None, target_price: Optional[float] = None) -> LiquidationReceipt:
        """Claim, then create or join a root. Returns the receipt of the root the caller must poll."""
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        if ":" in cause_command_id:
            raise ValueError("cause command id may not contain ':'")
        if scope == "account":
            if conid is not None or quantity is not None:
                raise ValueError("account scope takes no conid or quantity")
            outcome, root = self._store.transaction(
                lambda conn: self._claim_account_in_tx(conn, account_id, cause_command_id, deadline))
        elif scope == "conid":
            conid = _exact_conid(conid)
            if quantity is not None:
                raise LiquidationRefused("PARTIAL_CLOSE_UNAVAILABLE", "partial closes arrive in plan 1 task 6")
            outcome, root = self._store.transaction(lambda conn: self._claim_scoped_in_tx(
                conn, account_id, cause_command_id, conid, quantity, deadline, stop_price, target_price))
        else:
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if outcome in (CLAIMED, "EXISTING"):
            with self._exclusive():
                return self._tick(root)
        return self._store.receipt(root)
```

```python
    def _tick(self, root_id: str) -> Optional[LiquidationReceipt]:
        receipt = self._store.receipt(root_id)
        if receipt is None:
            return None
        if receipt.cleanup_pending:
            return self._cleanup(receipt)
        if receipt.state in RESCAN_TERMINAL:
            return receipt
        try:
            snapshot = self._broker.capture(receipt.account_id)
            newest = int(self._dispatch.newest_generation())
            if getattr(snapshot, "account_id", None) != receipt.account_id:
                raise RuntimeError("broker snapshot account mismatch")
        except Exception as exc:
            if self._now() >= receipt.deadline:
                return self._on_deadline(receipt)
            return self._snapshot_unavailable(receipt, f"broker evidence unavailable: {exc}")
        receipt = self._fence_unsent(receipt, snapshot, newest)
        receipt = self._observe_children(receipt, snapshot, newest)
        receipt = self._observe_late_fills(receipt, newest)
        if self._now() >= receipt.deadline:
            return self._on_deadline(receipt)
        if receipt.scope == "account":
            return self._advance_account(receipt, snapshot)
        return self._advance_conid(receipt, snapshot)
```

Add `import numbers` to the module imports, and next to `_reducing_side` (ruling 51):

```python
def _exact_conid(conid) -> int:
    """#21, ruling 51: an exact positive integer conId, or the close is refused before any claim.

    ``1.5``, ``True`` and ``"265598"`` are never coerced: a coerced id can close another instrument.
    """
    if isinstance(conid, bool) or not isinstance(conid, numbers.Integral) or int(conid) <= 0:
        raise LiquidationRefused("CONID_INVALID", f"conid must be a positive integer, got {conid!r}")
    return int(conid)
```

Add after `_claim_account_in_tx`:

```python
    def _claim_scoped_in_tx(self, conn, account_id, cause, conid, quantity, deadline, stop_price, target_price):
        goal = "zero" if quantity is None else "partial"
        existing = self._existing_in_tx(conn, account_id, cause, conid=conid, goal=goal, quantity=quantity)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_scoped_in_tx(conn, account_id=account_id, conid=conid, root_id=cause,
                                                  goal_quantity=quantity, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, conid, claim.outcome,
                                                    goal, quantity), now)
        if claim.outcome == CLAIMED:
            self._store.insert_run_in_tx(conn, LiquidationReceipt(
                account_id, cause, "REQUESTED", deadline, scope="conid", conid=conid, goal=goal,
                goal_quantity=quantity, stop_price=stop_price, target_price=target_price), now)
            self._store.inherit_children_in_tx(conn, account_id=account_id, conid=conid, to_root_id=cause, now=now)
        return (claim.outcome, claim.root_id)
```

Add a conid section after `_submit_reduces`:

```python
    # -- conid scope --------------------------------------------------------------------

    @staticmethod
    def _position_for(snapshot, conid: int):
        for position in snapshot.positions:
            if int(position.conid) == int(conid) and float(position.quantity) != 0.0:
                return position
        return None

    @staticmethod
    def _working_for(snapshot, conid: int) -> tuple:
        return tuple(o for o in snapshot.working_orders if int(o.conid) == int(conid))

    def _cancel_conid_orders(self, receipt, snapshot, working) -> LiquidationReceipt:
        """Hand over the orders about to be cancelled (D6), then cancel them."""
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        targets = self._cancel_targets(receipt, working, conid)
        if receipt.phase == "" or targets:
            info = HandoverInfo(None, None)
            if self._protection is not None:
                info = self._protection.handover(
                    account_id=receipt.account_id, conid=conid, close_root_id=receipt.cause_command_id,
                    cancels=_targets(targets), generation=generation, now=self._now())
            if receipt.phase == "":
                receipt = self._set(
                    receipt, "CANCELLING" if working else "VERIFYING", generation_id=generation, phase="cancel",
                    opened_generation=generation,
                    stop_price=receipt.stop_price if receipt.stop_price is not None else info.stop_price,
                    target_price=receipt.target_price if receipt.target_price is not None else info.target_price,
                    detail="protection handed over to the close")
        return self._send_cancels(receipt, snapshot, targets)

    def _advance_conid(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)
        receipt = self._cancel_conid_orders(receipt, snapshot, working)
        why = self._blocking(receipt, generation)
        if why is not None:
            return self._wait(receipt, generation, f"awaiting child confirmation: {why}")
        if working:
            return self._wait(receipt, generation, "awaiting broker confirmation that conid orders are gone")
        if position is None:
            if generation <= self._last_action_generation(receipt):
                return self._wait(receipt, generation, "awaiting a newer generation to prove the position is closed")
            return self._finish(receipt, "CLOSED", generation_id=generation,
                                detail="fresh broker generation shows no position and no working orders for conid")
        return self._submit_reduces(receipt, snapshot, (position,))
```

Why this order: `handover` runs before `_send_cancels`, every time, so the saga is already `CLOSE_OWNED` with the exact ids when the broker reports any of those orders cancelled. `CLOSED` needs no working order on the conid, no blocking child, and a generation newer than the last action (`_last_action_generation`). A stop cancelled with no fill is evidence at once (R23): the reduce may follow on the same generation, because a zero-fill cancel did not change the position (ruling 4).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 66 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: conid-scoped full close with protection hand-over

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 6: Partial close, re-protect with a linked stop/target, recovery, escalation

After the cancels, the service sends a partial reduce of `q`, waits until that child is terminal and a generation newer than its fill, then re-protects the **actual** remainder with an exit-only OCA pair (`REPROTECTING → VERIFYING → DONE`). Per R13 the stop goes first; the target is journaled `PLANNED` (ruling 5) and sent only after the stop is accepted, sized from the live remaining position. Before any leg is sent the saga is told the legs' order groups (`expect_reprotect`), so their events reach the saga while the close still owns protection (R2-4, Task 9).

Review round 2 rules in this task:
- **What `DONE` means (R25, R2-2).** `DONE` needs a proven fill of the partial reduce and working protection for the remainder. A partial reduce that sold nothing (`REJECTED`, `CANCELLED` with zero fill, `ABSENT`) still re-protects the untouched position — protection must come back — but the root ends `REDUCE_FAILED`. That is a terminal, owner-releasing state that is **not** a success: `close_resolution` reports `success=False`, `error_code="REDUCE_FAILED"`, and the reconciler fails the command with an operator alert (Task 17). The resolution outcome carries what was asked and what happened: the requested goal and quantity, the root's goal, the quantity the root's reduces sold, and the remaining position (R2-2).
- **A normal exit is not a failure (R26, R2-1).** `_advance_reprotect` checks the fresh position before it classifies a leg. A target fill that cancels its OCA stop, or a stop fill that leaves a target, ends `CLOSED` after the residual sibling is cancelled. A stop `Cancelled` while its target works is judged only on a generation newer than that observation, together with the target and the position, so the order of the two callbacks does not matter.
- **No retry with fresh ids (R30, spec 5.1).** A leg that is `NOT_SENT`, `REJECTED`, `CANCELLED` or `ABSENT` with a position left escalates to a full close of the conid and trips the breaker. Only the `PLANNED` target — never sent — is sent later ("place only the missing sibling"); a sent leg is never sent again.
- **The broker's OCA link (R13, R38).** `DONE` re-reads each leg's own broker row: working, the protective action, the pair's OCA group and `ocaType` 2 as the broker reports them (Task 18), and an outstanding quantity equal to the remaining position. A quantity mismatch waits; a wrong link escalates.
- **Deadline (R31, D10).** The deadline decides on this tick's evidence (`_tick` now observes the children first). A partial close whose protection is already cancelled (phase `cancel`, `reduce` or `reprotect`) escalates once to a full close of the live remainder with one more deadline, unless a child is `UNKNOWN`: then it is `FAILED_SAFE` with no new order. A second missed deadline is `FAILED_SAFE`.
- **Retries and refusals before admission (R36, D15).** `start` looks up the command's join row first: a retry returns its durable root and never re-admits the quantity (the join row keeps the request as it came in, the run gets the admitted goal). A new partial request against any active owner gets `ExitInProgress` before its quantity is checked against a snapshot.
- **Own reduce only.** Only this root's own partial reduce moves it to re-protect; an inherited reduce (R9) is evidence, not this close's result.

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Modify: `trader/trading/broker_ingest.py` (`hold_changes`, reentrant `_apply_lock`), `trader/trading/trading_runtime.py` and `trader/trading/command_stack.py` (`hold_broker_changes`)
- Test: `tests/test_liquidation_service.py`, `tests/test_close_broker_evidence.py`

**Interfaces:**
- Consumes: `liquidation_child_id`, `reprotect_oca_group` (Task 2); `ExitOwnerRegistry.ensure_partial_allowed_in_tx` (Task 3); `LiquidationDispatchPort.reduce_partial` / `place_exit_leg` (frozen in Task 4; real in Tasks 14 and 10); `ProtectionOwnershipPort.expect_reprotect` / `release_after_partial` (frozen in Task 4; real in Task 9).
- Produces: `upgrade_to_zero(root_id) -> LiquidationReceipt` (registry goal and run goal in one transaction; `PLANNED` children → `NOT_SENT`; a `reduce`/`reprotect` phase goes back to `cancel`, so replacement legs are cancelled). Terminal state `REDUCE_FAILED`. Quantity rules (R15): `q = floor(requested)`; `q < 1` → `LiquidationRefused("PARTIAL_QUANTITY_INVALID")`; `q ≥ |position|` → `LiquidationRefused("QUANTITY_ABOVE_POSITION")`; less than one share left → full close; live position `≤ q` (or less than one share left) at dispatch → close the live remainder. Stop side: long → stop below the market price, short → above; a target on the profit side; no market price → escalate. A `quantity` that is not a finite real number (a `bool`, a string, NaN) → `LiquidationRefused("PARTIAL_QUANTITY_INVALID")` before any claim (ruling 51). Child state `PENDING_CANCEL` (ruling 47): live, blocks every reduce, never healthy protection. `LiquidationDispatchPort.hold_broker_changes()` and `BrokerChangesBusy` (ruling 48).

Round 4 rules in this task:
- **PendingCancel is not protection (ruling 47, #22 blocker).** The target is sent only while the stop's own row is `Submitted`/`PreSubmitted`; a `PendingCancel` stop is observed again (`PENDING_CANCEL`) and, on the next generation, escalates like a cancelled stop (or ends `CLOSED` when the position is gone). At DONE a `PendingCancel` row counts as a change.
- **DONE commits under a hold (ruling 48, #22 major).** The terminal write of `DONE` / `REDUCE_FAILED` (and the owner release in it) runs while broker changes are held. Under the hold the leg rows, the position and the promoted generation are read again; any change, or a hold that cannot be taken, waits for the next tick.

- [ ] **Step 1: Write the failing tests**

In the fakes at the top of `tests/test_liquidation_service.py` (Task 4): import `from contextlib import contextmanager` and `BrokerChangesBusy`; replace `_Broker` with

```python
class _Broker:
    """``held``: broker changes are held, so a capture repeats the last snapshot (``current``)."""
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0
        self.last = 0          # generation of the last captured snapshot
        self.held = False
        self.current = None

    def capture(self, account_id):
        self.calls += 1
        if self.held and self.current is not None:
            return self.current
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        self.last = value.generation_id
        self.current = value
        return value
```

give `_Dispatch.__init__` three more fields

```python
        self.before_hold = None                # an ingest batch applied just before the hold is taken
        self.hold_busy = False
        self.holds = 0
```

and add to `_Dispatch`:

```python
    @contextmanager
    def hold_broker_changes(self):
        if self.before_hold is not None:
            self.before_hold()
        if self.hold_busy:
            raise BrokerChangesBusy("broker ingest busy")
        self.holds += 1
        self.broker.held = True
        try:
            yield
        finally:
            self.broker.held = False
```

Append to `tests/test_liquidation_service.py`:

```python
# ---------------------------------------------------------------------------
# Task 6: partial close, re-protect, escalation
# ---------------------------------------------------------------------------


def _priced(quantity=10.0, conid=1, price=100.0):
    return _position(quantity, conid=conid, market_price=price)


def _leg_row(status="Submitted", total=6.0, filled=0.0, group="p-1-reprotect-1-1", oca_type=2, action="SELL",
             entity=None):
    """A re-protect leg's own broker row, with the OCA link the broker reports (Task 18)."""
    return _row(status, filled=filled, total=total, oca_group=group, oca_type=oca_type, action=action, entity=entity)


def _to_reprotect(tmp_path, *, target=120.0, stop=95.0, held=10.0, q=4.0, extra=()):
    """Partial close of `q` that reaches REPROTECTING with the stop leg sent (generation 3)."""
    left = held - q
    protection = _Protection(stop_price=stop, target_price=target)
    s = _stack(tmp_path, [_snapshot(1, [_priced(held)]), _snapshot(1, [_priced(held)]), _snapshot(2, [_priced(left)]),
                          _snapshot(3, [_priced(left)]), *extra], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=q)      # gen 1: admit + partial reduce
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=q, total=q)]
    s.service.rescan()                                                                 # gen 2: fill observed
    s.service.rescan()                                                                 # gen 3: fresh -> stop leg
    return s, protection


def test_partial_quantity_edge_cases(tmp_path):
    """Review focus 2 / R15."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0), _priced(10.5, conid=2)])], protection=_Protection())
    with pytest.raises(LiquidationRefused) as ex:
        s.service.start(ACCOUNT, "p-a", DEADLINE, scope="conid", conid=1, quantity=0.4)
    assert ex.value.code == "PARTIAL_QUANTITY_INVALID"
    for q in (10.0, 12.0):
        with pytest.raises(LiquidationRefused) as ex:
            s.service.start(ACCOUNT, f"p-{q:g}", DEADLINE, scope="conid", conid=1, quantity=q)
        assert ex.value.code == "QUANTITY_ABOVE_POSITION"
    assert s.registry.owner_for(ACCOUNT, 1) is None
    receipt = s.service.start(ACCOUNT, "p-b", DEADLINE, scope="conid", conid=1, quantity=4.7)
    assert (receipt.goal, receipt.goal_quantity) == ("partial", 4.0)
    full = s.service.start(ACCOUNT, "p-c", DEADLINE, scope="conid", conid=2, quantity=10.0)
    assert (full.goal, full.goal_quantity) == ("zero", None)          # 0.5 share would remain


def test_live_position_at_or_below_q_at_dispatch_is_fully_closed(tmp_path):
    """R15: the stop sold 7 of 10 during the cancel; the live 3 <= q=4, so close all 3."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(2, [_priced(3.0)]),
                          _snapshot(3, [_priced(3.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.entities["stop-1"] = _row("Cancelled", filled=7.0)
    s.service.rescan()
    receipt = s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "p-1-reduce-1-1")
    assert receipt.goal == "zero"
    assert s.registry.get("p-1").goal == "zero"


def test_partial_close_sends_stop_then_target_and_ends_done(tmp_path):
    """R13: the target is sent only after the stop is accepted, sized from the live position."""
    s, protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)])))
    assert s.dispatch.calls[0] == ("reduce_partial", 1, "SELL", 4.0, "p-1-reduce-1-1")
    assert ("expect_reprotect", "p-1", ("p-1-reprotect-stop-1-1", "p-1-reprotect-target-1-1")) in protection.calls
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 6.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")
    receipt = s.service.receipt_for("p-1")
    assert (receipt.state, receipt.phase) == ("VERIFYING", "reprotect")
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["PLANNED"]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: stop working -> target
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "target", 6.0, 120.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-target-1-1")
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    receipt = s.service.rescan()                                          # gen 5: both working
    assert receipt.state == "DONE"
    assert protection.calls[-1] == ("release_after_partial", "p-1", 6.0, "p-1-reprotect-stop-1-1",
                                    "p-1-reprotect-target-1-1")
    assert s.registry.get("p-1").state == "RELEASED"
    assert s.breaker.calls == []
    outcome = s.service.close_resolution("p-1").outcome                  # what was asked, sold and kept
    assert (outcome["requested_quantity"], outcome["filled_quantity"], outcome["remaining_quantity"]) == (4.0, 4.0, 6.0)


def _both_legs_working_next(tmp_path, *stop_answers):
    """Partial close at generation 5 with both legs sent; the stop's row answers come in order."""
    s, _ = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]),
                                          _snapshot(6, [_priced(6.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: stop working -> target
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    s.dispatch.sequences["p-1-reprotect-stop-1-1"] = [list(answer) for answer in stop_answers]
    return s


def test_a_stop_cancelled_after_it_was_observed_never_ends_done(tmp_path):
    """#22: the stop was WORKING when observed, then ingest saw it Cancelled; DONE is not decided from
    the old child status with the fresh row's OCA link."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row("Cancelled")])
    receipt = s.service.rescan()                                          # gen 5
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None
    stop = next(c for c in receipt.children if c.kind == "reprotect-stop")
    assert stop.state == "CANCELLED"                                     # observed again, not finished
    receipt = s.service.rescan()                                          # gen 6: a failed re-protect
    assert receipt.state != "DONE" and receipt.goal == "zero"


def test_a_stop_fill_after_it_was_observed_never_ends_done(tmp_path):
    """#22: ingest saw 2 of the stop's 6 fill after it was observed; outstanding is 4, not 6."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row(filled=2.0)])
    receipt = s.service.rescan()
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None
    stop = next(c for c in receipt.children if c.kind == "reprotect-stop")
    assert (stop.filled_quantity, stop.outstanding_quantity) == (2.0, 4.0)


def test_a_leg_row_that_changes_while_done_is_decided_is_read_again(tmp_path):
    """#22: the rows DONE was decided on must still be the rows at the terminal write."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row()], [_leg_row("Cancelled")])
    receipt = s.service.rescan()
    assert receipt.state != "DONE" and s.service.close_resolution("p-1") is None


@pytest.mark.parametrize("quantity", [True, "4", float("nan"), float("inf")], ids=repr)
def test_a_partial_quantity_that_is_not_a_finite_number_is_refused_before_any_claim(tmp_path, quantity):
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    with pytest.raises(LiquidationRefused) as ex:
        s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=quantity)
    assert ex.value.code == "PARTIAL_QUANTITY_INVALID"
    assert s.service.root_for("p-1") is None and (s.broker.calls, s.dispatch.calls) == (0, [])


def test_a_pending_cancel_stop_never_gets_its_target_and_escalates(tmp_path):
    """#22 blocker, ruling 47: a stop already pending cancellation is not protection. Its target is
    never sent, and the close escalates instead of ending DONE."""
    s, protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("PendingCancel")]
    receipt = s.service.rescan()                                          # gen 4
    stop = next(c for c in receipt.children if c.kind == "reprotect-stop")
    assert stop.state == "PENDING_CANCEL" and receipt.state != "DONE"
    receipt = s.service.rescan()                                          # gen 5: still pending cancel
    assert receipt.goal == "zero" and receipt.escalated
    assert not any(c[0] == "place_exit_leg" and c[2] == "target" for c in s.dispatch.calls)
    assert s.registry.get("p-1").state == "ACTIVE" and s.service.close_resolution("p-1") is None


def test_a_stop_that_goes_pending_cancel_while_done_is_decided_never_ends_done(tmp_path):
    """#22 blocker: the stop's row turns PendingCancel after it was observed WORKING. DONE is not
    committed and the owner is not released, on this generation or the next."""
    s = _both_legs_working_next(tmp_path, [_leg_row()], [_leg_row("PendingCancel")])
    receipt = s.service.rescan()                                          # gen 5
    assert receipt.state != "DONE" and s.registry.get("p-1").state == "ACTIVE"
    assert next(c for c in receipt.children if c.kind == "reprotect-stop").state == "PENDING_CANCEL"
    receipt = s.service.rescan()                                          # gen 6
    assert receipt.state != "DONE" and receipt.goal == "zero"
    assert s.service.close_resolution("p-1") is None


def _done_ready(tmp_path):
    """Both legs working and matching on generation 5; the next tick would commit DONE."""
    return _both_legs_working_next(tmp_path, [_leg_row()])


_INGEST_BEFORE_THE_WRITE = {
    "leg row": lambda s: s.dispatch.sequences.__setitem__("p-1-reprotect-stop-1-1", [[_leg_row("Cancelled")]]),
    "position": lambda s: setattr(s.broker, "current", _snapshot(5, [_priced(4.0)])),
    "generation": lambda s: setattr(s.broker, "current", _snapshot(6, [_priced(6.0)])),
    "hold busy": lambda s: setattr(s.dispatch, "hold_busy", True),
}


@pytest.mark.parametrize("change", _INGEST_BEFORE_THE_WRITE.values(), ids=_INGEST_BEFORE_THE_WRITE.keys())
def test_an_ingest_update_before_the_terminal_write_never_commits_done(tmp_path, change):
    """#22 major, ruling 48: an ingest batch lands after the final read and before the terminal write.
    The write is made while broker changes are held, after the rows, position and generation are read
    again; any change (or a hold that cannot be taken) waits, and the owner stays ACTIVE."""
    s = _done_ready(tmp_path)
    s.dispatch.before_hold = lambda: change(s)
    receipt = s.service.rescan()                                          # gen 5
    assert receipt.state != "DONE" and "not committed" in receipt.detail
    assert s.registry.get("p-1").state == "ACTIVE" and s.service.close_resolution("p-1") is None


def test_done_commits_while_broker_changes_are_held(tmp_path):
    s = _done_ready(tmp_path)
    receipt = s.service.rescan()
    assert receipt.state == "DONE" and s.dispatch.holds == 1


def test_terminal_partial_fill_reprotects_the_actual_remainder(tmp_path):
    """#22: SELL 4 fills 2 then is cancelled; the remaining 8 is protected, not the planned 6."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]), _snapshot(3, [_priced(8.0)])],
               protection=_Protection(target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Cancelled", filled=2.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 8.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")


def test_done_checks_outstanding_quantity_and_sizes_the_target_from_the_live_position(tmp_path):
    """#22 P2 / R13: the stop filled 2 of 6 before the target was sent."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(4.0)]), _snapshot(5, [_priced(4.0)]), _snapshot(6, [_priced(4.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=6.0, filled=2.0)]
    s.service.rescan()                       # gen 4: the stop fill is new -> wait for a newer generation
    assert s.dispatch.calls[-1][2] == "stop"
    s.service.rescan()                       # gen 5: target sized from the live 4
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "target", 4.0, 120.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-target-1-1")
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row(total=4.0)]
    receipt = s.service.rescan()             # gen 6: outstanding 6-2=4 and 4 match the position
    assert (receipt.state, receipt.remaining_quantity) == ("DONE", 4.0)
    assert protection.calls[-1][2] == 4.0


def test_recovery_after_restart_sends_only_the_planned_target(tmp_path):
    """Review focus 5 / R20: the stop left, the process died before its fence; the restart sends only the target."""
    protection = _Protection(stop_price=95.0, target_price=120.0)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    real_leg = s.dispatch.place_exit_leg

    def sent_then_crash(*args, **kwargs):
        real_leg(*args, **kwargs)
        raise _Crash()
    s.dispatch.place_exit_leg = sent_then_crash
    with pytest.raises(_Crash):
        s.service.rescan()                                              # generation 3: the stop leaves
    s.dispatch.place_exit_leg = real_leg
    service = s.restart()
    s.push(_snapshot(4, [_priced(6.0)]))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    service.rescan()
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop", "target"]


def test_crash_while_the_target_is_promoted_never_sends_it_again(tmp_path):
    """R20 / R30: the target was journaled UNKNOWN, the process died before its broker call."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]

    def crash(*_args, **_kwargs):
        raise _Crash()
    s.dispatch.place_exit_leg = crash
    with pytest.raises(_Crash):
        s.service.rescan()                                              # generation 4: target promoted
    service = s.restart()
    s.push(_snapshot(5, [_priced(6.0)]), _snapshot(6, [_priced(6.0)]))
    receipt = service.rescan()                                          # fenced on 5; nothing proves it yet
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["UNKNOWN"]
    service.rescan()                                                    # generation 6: proven absent
    s.push(_snapshot(7, [_priced(6.0)]))
    receipt = service.rescan()                                          # generation 7: escalate, no resend
    assert receipt.escalated is True and receipt.goal == "zero"
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_unknown_reprotect_leg_is_never_resent(tmp_path):
    """R3 / R30: a stop whose send timed out stays UNKNOWN; once proven absent it escalates, never resent."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]), _snapshot(3, [_priced(6.0)]),
                          _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]), _snapshot(6, [_priced(6.0)])],
               protection=_Protection(target_price=None))
    s.dispatch.fail_after_send.add("place_exit_leg")
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.dispatch.staging = 1
    s.service.rescan()                                  # gen 3: stop sent (fence 4), ack timed out
    s.dispatch.staging = 0
    receipt = s.service.rescan()                        # gen 4: no row, not newer than the fence
    assert [c.state for c in receipt.children if c.kind == "reprotect-stop"] == ["UNKNOWN"]
    assert [c.state for c in s.service.rescan().children if c.kind == "reprotect-stop"] == ["ABSENT"]   # gen 5
    receipt = s.service.rescan()                        # gen 6: escalate; the stop is never placed again
    assert receipt.escalated is True
    assert len([c for c in s.dispatch.calls if c[0] == "place_exit_leg"]) == 1


def test_a_reprotect_leg_refused_before_the_broker_escalates_and_is_never_retried(tmp_path):
    """R30 / spec 5.1: re-protect failure means no retry with fresh ids; escalate to a full close."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])],
               protection=_Protection(target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.dispatch.refuse.add("place_exit_leg")
    s.service.rescan()                                  # gen 3: stop refused before the broker -> NOT_SENT
    receipt = s.service.rescan()                        # gen 4: escalate, no retry with a fresh id
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("REPROTECT_FAILED: stop leg NOT_SENT" in detail for _root, detail in s.breaker.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_stop_fill_before_target_is_sent_ends_closed_without_a_target(tmp_path):
    """R13: the stop filled fully before the target went out."""
    s, protection = _to_reprotect(tmp_path, extra=(_snapshot(4, []), _snapshot(5, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Filled", filled=6.0)]
    s.service.rescan()
    receipt = s.service.rescan()
    assert receipt.state == "CLOSED"
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["NOT_SENT"]
    assert protection.calls[-1] == ("close_after_full", "p-1")


@pytest.mark.parametrize("order", ["target_first", "stop_first"])
def test_target_fill_that_cancels_its_oca_stop_ends_closed_without_a_failure(tmp_path, order):
    """R26 / R2-1: a normal exit is not a re-protect failure, whichever callback lands first."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    rows = {"p-1-reprotect-target-1-1": [_leg_row("Filled", filled=6.0)],
            "p-1-reprotect-stop-1-1": [_leg_row("Cancelled")]}
    first, second = list(rows) if order == "target_first" else list(rows)[::-1]
    s.dispatch.rows[first] = rows[first]
    s.service.rescan()                                                    # gen 5: one callback seen
    s.dispatch.rows[second] = rows[second]
    for _ in range(2):
        s.service.rescan()                                                # gens 6-7: flat, then behind the fill
    receipt = s.service.receipt_for("p-1")
    assert (receipt.state, receipt.escalated) == ("CLOSED", False)
    assert s.breaker.calls == []
    assert not any(c[0] in ("reduce", "cancel") for c in s.dispatch.calls)
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_target_fill_waits_for_the_stop_to_match_the_remainder(tmp_path):
    """R2-1: the target filled 2 of 6; the stop shrinks to 4 (OCA type 2); then DONE for 4."""
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(4.0)]), _snapshot(6, [_priced(4.0)]),
        _snapshot(7, [_priced(4.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row(filled=2.0)]
    s.service.rescan()                                                    # gen 5: the fill is new
    assert "outstanding 6.0 != position 4.0" in s.service.rescan().detail  # gen 6: stop still for 6
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=4.0)]
    receipt = s.service.rescan()                                          # gen 7
    assert (receipt.state, receipt.remaining_quantity) == ("DONE", 4.0)
    assert s.breaker.calls == []


def test_a_leg_the_broker_does_not_link_by_oca_escalates(tmp_path):
    """R13 / R38: DONE reads the OCA group and type from the broker row, not from the journal."""
    s, _protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(oca_type=0)]
    receipt = s.service.rescan()
    assert receipt.escalated is True
    assert any("not a linked protective leg" in detail for _root, detail in s.breaker.calls)


def test_stop_that_goes_pending_submit_then_inactive_escalates(tmp_path):
    """R13: PendingSubmit is a local echo, not acceptance; Inactive after it is a failed re-protect."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]), _snapshot(5, [_priced(6.0)])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("PendingSubmit")]
    receipt = s.service.rescan()
    assert [c.state for c in receipt.children if c.kind == "reprotect-stop"] == ["UNKNOWN"]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Inactive")]
    receipt = s.service.rescan()
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("REPROTECT_FAILED" in detail for _root, detail in s.breaker.calls)
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_target_rejected_escalates_and_cancels_the_working_stop(tmp_path):
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, _protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)], [stop_leg])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                   # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row("Rejected")]
    receipt = s.service.rescan()                         # gen 5: target rejected
    assert receipt.escalated is True
    assert s.dispatch.calls[-1] == ("cancel", "rs", "p-1-cancel-1-1")


def test_reprotect_deadline_escalates_instead_of_failing_safe(tmp_path):
    """R31: no child is UNKNOWN (the stop works), so the missed deadline escalates once."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    receipt = s.service.rescan()
    assert receipt.state != "FAILED_SAFE"
    assert receipt.escalated is True and receipt.goal == "zero"
    assert receipt.deadline == s.clock["now"] + dt.timedelta(seconds=300)
    assert s.breaker.calls


def test_position_zero_during_reprotect_cancels_the_residual_leg_and_ends_closed(tmp_path):
    target_leg = _order("rt", group="p-1-reprotect-target-1-1", total=6.0)
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)]), _snapshot(5, [], [target_leg]), _snapshot(6, [], [target_leg]),
        _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    s.service.rescan()                                                    # gen 4: target sent
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row()]
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Filled", filled=6.0)]
    s.service.rescan()                                                    # gen 5: stop fill seen
    s.service.rescan()                                                    # gen 6: cancel the residual target
    assert s.dispatch.calls[-1] == ("cancel", "rt", "p-1-cancel-1-1")
    s.dispatch.entities["rt"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-target-1-1"] = [_leg_row("Cancelled")]
    receipt = s.service.rescan()                                          # gen 7
    assert receipt.state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_close_of_short_reprotects_above_price(tmp_path):
    """Review focus 1."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(-10.0)]), _snapshot(1, [_priced(-10.0)]), _snapshot(2, [_priced(-6.0)]), _snapshot(3, [_priced(-6.0)])],
               protection=_Protection(stop_price=105.0, target_price=None))
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    assert s.dispatch.calls[0] == ("reduce_partial", 1, "BUY", 4.0, "p-1-reduce-1-1")
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 6.0, 105.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")


def test_non_protective_stop_escalates_to_full_close_and_trips_breaker(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]), _snapshot(3, [_priced(6.0)])],
               protection=_Protection(stop_price=101.0, target_price=None))     # above market on a long
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    receipt = s.service.rescan()
    assert receipt.escalated is True and receipt.goal == "zero"
    assert any("STOP_NOT_PROTECTIVE" in detail for _root, detail in s.breaker.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_upgrade_to_zero_during_reprotect_cancels_replacement_exits_and_closes(tmp_path):
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, protection = _to_reprotect(tmp_path, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    receipt = s.service.upgrade_to_zero("p-1")
    assert (receipt.goal, receipt.phase) == ("zero", "cancel")
    assert [c.state for c in receipt.children if c.kind == "reprotect-target"] == ["NOT_SENT"]
    s.service.rescan()                                                     # gen 4: cancel the replacement stop
    assert s.dispatch.calls[-1] == ("cancel", "rs", "p-1-cancel-1-1")
    s.dispatch.entities["rs"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Cancelled")]   # the same order, by its own ref
    s.service.rescan()                                                     # gen 5: reduce the remainder
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert not any(c[0] == "release_after_partial" for c in protection.calls)
    assert [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"] == ["stop"]


def test_restart_after_a_goal_upgrade_never_reprotects(tmp_path):
    """R20: crash right after the upgrade transaction; the restarted root closes everything."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, []), _snapshot(5, [])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.upgrade_to_zero("p-1")
    service = s.restart()
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    service.rescan()
    service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    service.rescan()
    assert service.rescan().state == "CLOSED"
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)


def test_reprotect_deadline_with_an_unknown_leg_is_failed_safe_with_no_order(tmp_path):
    """R5 / R31: an UNKNOWN child at the deadline forbids every new order."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.complete = False
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    sent = len(s.dispatch.calls)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert len(s.dispatch.calls) == sent


def test_partial_deadline_in_the_reduce_phase_escalates_to_a_full_close(tmp_path):
    """R31 / D10: protection is already cancelled; a working partial reduce at the deadline is escalated once."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]),
                          _snapshot(3, [_priced(8.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Submitted", filled=2.0, total=4.0, entity="p-1-reduce-1-1:exit")]
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    receipt = s.service.rescan()                                         # gen 2: reduce WORKING, nothing UNKNOWN
    assert (receipt.state, receipt.escalated, receipt.goal) == ("CANCELLING", True, "zero")
    assert receipt.deadline == s.clock["now"] + dt.timedelta(seconds=300)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()                                                   # gen 3: the fill is seen
    s.push(_snapshot(4, [_priced(6.0)]))
    s.service.rescan()                                                   # gen 4: close the live 6
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")


def test_partial_deadline_with_an_unknown_reduce_is_failed_safe_with_no_order(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)])],
               protection=_Protection())
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.clock["now"] = DEADLINE + dt.timedelta(seconds=1)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


def test_invisible_partial_reduce_never_reprotects_or_reduces_again(tmp_path):
    """#22: while the partial reduce is unknown, no leg and no second reduce goes out."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]),
                          _snapshot(3, [_priced(10.0)])], protection=_Protection())
    s.dispatch.complete = False
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    for _ in range(2):
        assert "outcome unknown" in s.service.rescan().detail
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


@pytest.mark.parametrize("status", ["Rejected", "Cancelled"])
def test_a_partial_reduce_that_sold_nothing_reprotects_and_ends_reduce_failed(tmp_path, status):
    """R25 / R2-2: protection comes back, but the close is a failure, never DONE."""
    protection = _Protection(stop_price=95.0, target_price=None)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]),
                          _snapshot(3, [_priced(10.0)]), _snapshot(4, [_priced(10.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row(status, filled=0.0, total=4.0)]
    s.service.rescan()                                                   # gen 2: stop for the untouched 10
    assert s.dispatch.calls[-1] == ("place_exit_leg", 1, "stop", 10.0, 95.0, "p-1-reprotect-1-1",
                                    "p-1-reprotect-stop-1-1")
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(total=10.0)]
    receipt = s.service.rescan()                                         # gen 3: the stop works
    assert (receipt.state, receipt.remaining_quantity) == ("REDUCE_FAILED", 10.0)
    assert protection.calls[-1][:3] == ("release_after_partial", "p-1", 10.0)
    assert s.registry.get("p-1").state == "RELEASED"
    resolution = s.service.close_resolution("p-1")
    assert (resolution.success, resolution.error_code) == (False, "REDUCE_FAILED")
    assert (resolution.outcome["requested_quantity"], resolution.outcome["filled_quantity"],
            resolution.outcome["remaining_quantity"]) == (4.0, 0.0, 10.0)


def test_restart_before_the_done_cleanup_finishes_the_release(tmp_path):
    """R8 / R20: DONE and the owner release committed; the saga release is recovered."""
    s, protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    protection.crash_on.add("release_after_partial")
    with pytest.raises(_Crash):
        s.service.rescan()
    stored = s.store.receipt("p-1")
    assert (stored.state, stored.cleanup_pending, s.registry.get("p-1").state) == ("DONE", True, "RELEASED")
    s.restart().rescan()
    assert s.store.receipt("p-1").cleanup_pending is False
    assert protection.calls[-1][:2] == ("release_after_partial", "p-1")


def test_partial_retry_after_the_fill_returns_its_root(tmp_path):
    """R36 / D15: a retried partial command finds its root before any admission check."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)])], protection=_Protection())
    first = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.7)
    s.push(_snapshot(2, []))                                             # the position is gone now
    captures = s.broker.calls
    again = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.7)
    assert again.cause_command_id == first.cause_command_id == "p-1"
    assert s.service.root_for("p-1") == "p-1"
    assert s.broker.calls == captures + 1                               # the tick only, no admission capture


def test_partial_request_against_an_owner_is_exit_in_progress_before_admission(tmp_path):
    from trader.trading.exit_owner import ExitInProgress
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    captures = s.broker.calls
    with pytest.raises(ExitInProgress):
        s.service.start(ACCOUNT, "p-2", DEADLINE, scope="conid", conid=1, quantity=12.0)
    assert s.broker.calls == captures


def test_upgrade_rolls_back_with_its_run_change(tmp_path):
    """R6 / R20: the registry goal and the run goal change in one transaction or not at all."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, root_id, detail):
        raise _Crash()
    s.service._upgrade_run_in_tx = broken
    with pytest.raises(_Crash):
        s.service.upgrade_to_zero("p-1")
    assert (s.registry.get("p-1").goal, s.store.receipt("p-1").goal) == ("partial", "partial")


def test_a_stale_run_write_never_lowers_the_goal(tmp_path):
    """#24: a write from an old receipt changes only its named fields on the current row."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    stale = s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.upgrade_to_zero("p-1")
    s.service._set(stale, "VERIFYING", detail="late write")
    assert (s.store.receipt("p-1").goal, s.store.receipt("p-1").detail) == ("zero", "late write")


def test_dispatch_stops_when_the_owner_goal_and_the_run_goal_disagree(tmp_path):
    """R7 / #24: a registry/cursor split is caught before any broker call."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()]), _snapshot(1, [_priced(10.0)], [_stop_order()]),
                          _snapshot(2, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    s.db.execute("UPDATE exit_owners SET goal = 'zero' WHERE root_id = 'p-1'", fetch="none")
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_an_inherited_reduce_never_counts_as_this_partials_reduce(tmp_path):
    """A partial close that inherits an old reduce still sends its own partial reduce."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.dispatch.staging = 1
    s.service.start(ACCOUNT, "c-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.dispatch.staging = 0
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    s.dispatch.complete = False
    assert s.service.rescan().state == "FAILED_SAFE"                     # c-1's reduce is UNKNOWN
    s.dispatch.complete = True
    s.push(_snapshot(2, [_priced(10.0)]), _snapshot(2, [_priced(10.0)]), _snapshot(3, [_priced(10.0)]))
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Cancelled", filled=0.0)]
    s.service.start(ACCOUNT, "p-2", NOW + dt.timedelta(minutes=5), scope="conid", conid=1, quantity=4.0)
    assert s.dispatch.calls[-1] == ("reduce_partial", 1, "SELL", 4.0, "p-2-reduce-1-1")
```

Note: a partial `start` reads one snapshot to admit the quantity, so these tests list generation 1 twice.

Append to `tests/test_close_broker_evidence.py` (Task 18's file; it already has the real `BrokerIngest` fixture):

```python
# -- Task 6, ruling 48: the close's terminal write holds broker changes ----------------------

def test_holding_broker_changes_stops_an_ingest_batch_until_released(env):
    import threading

    from trader.trading.command_stack import _LiquidationDispatch

    applied = threading.Event()

    def ingest_batch():
        env.ingest.on_open_order(_stop_trade("p-1-reprotect-265598-1", 2))
        env.ingest.drain_once()
        applied.set()

    with _LiquidationDispatch(env.dispatch, None).hold_broker_changes():
        writer = threading.Thread(target=ingest_batch)
        writer.start()
        assert not applied.wait(0.3)                        # the batch waits for the hold
        assert env.store.select_active_orders_in_tx(env.journal.connect()) == []
        assert env.ingest.is_ready in (True, False)         # the holder may read readiness (reentrant)
    writer.join(timeout=5)
    assert applied.is_set() and len(env.store.select_active_orders_in_tx(env.journal.connect())) == 1


def test_broker_changes_cannot_be_held_while_a_generation_is_staging(env):
    from trader.trading.liquidation_service import BrokerChangesBusy

    env.ingest.begin_generation()
    with pytest.raises(BrokerChangesBusy, match="staging"):
        with env.dispatch.hold_broker_changes():
            pass
    env.ingest.abandon_generation("test")
    with env.dispatch.hold_broker_changes():
        pass


def test_broker_changes_cannot_be_held_without_an_ingest():
    from trader.trading.liquidation_service import BrokerChangesBusy

    with pytest.raises(BrokerChangesBusy):
        TradingRuntimeOrderDispatch(SimpleNamespace()).hold_broker_changes()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 38 failed (`PARTIAL_CLOSE_UNAVAILABLE`, `AttributeError: ... 'upgrade_to_zero'`), 66 passed.

- [ ] **Step 3: Implement**

1. Import: `from trader.trading.order_correlation import (legacy_reduce_prefix, liquidation_child_id, liquidation_child_kind, reprotect_oca_group)`.
2. Replace `start` and `_claim_scoped_in_tx`, and add `_claim_scoped`, `upgrade_to_zero`, `_upgrade_run_in_tx` and `_admit_partial`:

```python
    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, *,
              scope: str = "account", conid: Optional[int] = None, quantity: Optional[float] = None,
              stop_price: Optional[float] = None, target_price: Optional[float] = None) -> LiquidationReceipt:
        """Claim, then create or join a root. Returns the receipt of the root the caller must poll."""
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        if ":" in cause_command_id:
            raise ValueError("cause command id may not contain ':'")
        if scope == "account":
            if conid is not None or quantity is not None:
                raise ValueError("account scope takes no conid or quantity")
            outcome, root = self._store.transaction(
                lambda conn: self._claim_account_in_tx(conn, account_id, cause_command_id, deadline))
        elif scope == "conid":
            outcome, root = self._claim_scoped(account_id, cause_command_id, _exact_conid(conid),
                                               _requested_quantity(quantity), deadline, stop_price, target_price)
        else:
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if outcome in (CLAIMED, "EXISTING"):
            with self._exclusive():
                return self._tick(root)
        return self._store.receipt(root)

    def _claim_scoped(self, account_id, cause, conid, quantity, deadline, stop_price, target_price):
        """D15: a retry finds its root first; a partial request then learns ExitInProgress, and
        only a new partial request is admitted against a broker snapshot before it claims."""
        requested = quantity
        goal = "zero" if requested is None else "partial"
        existing = self._store.transaction(lambda conn: self._existing_in_tx(
            conn, account_id, cause, conid=conid, goal=goal, quantity=requested))
        if existing is not None:
            return existing
        admitted = None
        if requested is not None:
            self._store.transaction(
                lambda conn: self._registry.ensure_partial_allowed_in_tx(conn, account_id, conid))
            admitted = self._admit_partial(account_id, conid, requested)
        return self._store.transaction(lambda conn: self._claim_scoped_in_tx(
            conn, account_id, cause, conid, requested, admitted, deadline, stop_price, target_price))
```

```python
    def _claim_scoped_in_tx(self, conn, account_id, cause, conid, requested, admitted, deadline,
                            stop_price, target_price):
        """The join row keeps the request as it came in; the owner and run get the admitted goal."""
        goal = "zero" if requested is None else "partial"
        existing = self._existing_in_tx(conn, account_id, cause, conid=conid, goal=goal, quantity=requested)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_scoped_in_tx(conn, account_id=account_id, conid=conid, root_id=cause,
                                                  goal_quantity=admitted, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, conid, claim.outcome,
                                                    goal, requested), now)
        if claim.outcome == CLAIMED:
            self._store.insert_run_in_tx(conn, LiquidationReceipt(
                account_id, cause, "REQUESTED", deadline, scope="conid", conid=conid,
                goal="zero" if admitted is None else "partial", goal_quantity=admitted,
                stop_price=stop_price, target_price=target_price), now)
            self._store.inherit_children_in_tx(conn, account_id=account_id, conid=conid, to_root_id=cause, now=now)
        return (claim.outcome, claim.root_id)

    def upgrade_to_zero(self, root_id: str) -> LiquidationReceipt:
        """partial -> zero for an active scoped root, registry and run in one transaction."""
        def write(conn):
            self._registry.upgrade_goal_in_tx(conn, root_id, self._now())
            self._upgrade_run_in_tx(conn, root_id, "goal upgraded to zero exposure")
        self._store.transaction(write)
        return self._store.receipt(root_id)

    def _upgrade_run_in_tx(self, conn, root_id: str, detail: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL or run.goal == "zero":
            return
        phase = "cancel" if run.phase in ("reduce", "reprotect") else run.phase
        self._store.update_run_in_tx(conn, replace(run, goal="zero", goal_quantity=None, phase=phase,
                                                   detail=detail), self._now())
        self._store.drop_planned_in_tx(conn, root_id, self._now())

    def _admit_partial(self, account_id: str, conid: int, requested: float) -> Optional[float]:
        """R15 at start: whole shares, 0 < q < |position|; less than one share left = full close."""
        shares = math.floor(float(requested))
        if shares < 1:
            raise LiquidationRefused("PARTIAL_QUANTITY_INVALID", f"{requested!r} rounds to {shares}")
        snapshot = self._broker.capture(account_id)
        position = self._position_for(snapshot, conid)
        if position is None:
            raise LiquidationRefused("NO_POSITION", f"no position on conid {conid}")
        held = abs(float(position.quantity))
        if shares >= held:
            raise LiquidationRefused("QUANTITY_ABOVE_POSITION", f"{shares} >= {held}; send a full close")
        if held - shares < 1:
            return None
        return float(shares)
```

   Next to `_exact_conid` (Task 5), add (ruling 51: a quantity is never coerced either):

```python
def _requested_quantity(quantity) -> Optional[float]:
    """A partial quantity is a finite real number (not a bool, not a string); None is a full close."""
    if quantity is None:
        return None
    if isinstance(quantity, bool) or not isinstance(quantity, numbers.Real) or not math.isfinite(quantity):
        raise LiquidationRefused("PARTIAL_QUANTITY_INVALID", f"quantity must be a finite number, got {quantity!r}")
    return float(quantity)
```

3. Replace `_tick`, `_on_deadline` and `_cleanup`:

```python
    def _tick(self, root_id: str) -> Optional[LiquidationReceipt]:
        receipt = self._store.receipt(root_id)
        if receipt is None:
            return None
        if receipt.cleanup_pending:
            return self._cleanup(receipt)
        if receipt.state in RESCAN_TERMINAL:
            return receipt
        try:
            snapshot = self._broker.capture(receipt.account_id)
            newest = int(self._dispatch.newest_generation())
            if getattr(snapshot, "account_id", None) != receipt.account_id:
                raise RuntimeError("broker snapshot account mismatch")
        except Exception as exc:
            if self._now() >= receipt.deadline:
                return self._on_deadline(receipt)
            return self._snapshot_unavailable(receipt, f"broker evidence unavailable: {exc}")
        receipt = self._fence_unsent(receipt, snapshot, newest)
        receipt = self._observe_children(receipt, snapshot, newest)
        receipt = self._observe_late_fills(receipt, newest)
        if self._now() >= receipt.deadline:
            # The deadline decides on this tick's evidence: an UNKNOWN child means FAILED_SAFE (R31).
            return self._on_deadline(receipt)
        if receipt.scope == "account":
            return self._advance_account(receipt, snapshot)
        return self._advance_conid(receipt, snapshot)
```

```python
    def _on_deadline(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R31 / D10: a partial close whose protection is already cancelled escalates once to a
        full close of the live remainder, unless a child is UNKNOWN; everything else is FAILED_SAFE."""
        unknown = any(c.state == "UNKNOWN" for c in self._children_in_force(receipt))
        if (receipt.scope == "conid" and receipt.goal == "partial" and not receipt.escalated and not unknown
                and receipt.phase in ("cancel", "reduce", "reprotect")):
            label = "REPROTECT_DEADLINE" if receipt.phase == "reprotect" else "PARTIAL_DEADLINE"
            self._escalate(receipt, f"{label}: the partial close missed its deadline in phase {receipt.phase}")
            return self._store.receipt(receipt.cause_command_id)
        return self._finish(receipt, "FAILED_SAFE", detail="deadline elapsed without broker-confirmed result")
```

```python
    def _cleanup(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R8: every saga step is idempotent; recovery re-runs it until the flag clears."""
        root = receipt.cause_command_id
        if self._protection is not None:
            if receipt.state in ("CLOSED", "FLAT"):
                self._protection.close_after_full(close_root_id=root, now=self._now())
            elif receipt.state in ("DONE", "REDUCE_FAILED"):
                stop, target = self._working_legs(receipt)
                self._protection.release_after_partial(
                    close_root_id=root, remaining_quantity=float(receipt.remaining_quantity),
                    stop_group=stop.child_id, stop_status=self._leg_status(stop),
                    target_group=None if target is None else target.child_id,
                    target_status=None if target is None else self._leg_status(target),
                    now=self._now())

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(run, cleanup_pending=False), self._now())
        self._store.transaction(write)
        self._schedule_commands(root)
        return self._store.receipt(root)
```

4. Replace `_submit_reduces`:

```python
    def _submit_reduces(self, receipt, snapshot, positions, *, partial: Optional[float] = None) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        children = self._reserve(receipt, lambda conn: [
            self._new_child(conn, receipt, kind="reduce", conid=int(p.conid), generation=generation,
                            side=_reducing_side(p.quantity),
                            quantity=abs(float(p.quantity)) if partial is None else partial)
            for p in positions])
        if children is None:
            return self._store.receipt(receipt.cause_command_id)
        receipt = self._set(receipt, "REDUCING", generation_id=generation,
                            phase="reduce" if receipt.scope == "conid" else receipt.phase,
                            detail="submitting reduce-only orders")
        for child, position in zip(children, positions):
            send = self._dispatch.reduce if partial is None else self._dispatch.reduce_partial
            self._send(receipt, child, lambda p=position, c=child, s=send: s(p, c.side, c.quantity, c.child_id))
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          "reduction submitted; awaiting broker evidence")
```

5. Replace `_advance_conid`, and replace the two empty section headers after it (re-protect, escalation) with:

```python
    def _advance_conid(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)
        if receipt.phase == "reprotect":
            return self._advance_reprotect(receipt, snapshot, working, position)
        receipt = self._cancel_conid_orders(receipt, snapshot, working)
        why = self._blocking(receipt, generation)
        if why is not None:
            return self._wait(receipt, generation, f"awaiting child confirmation: {why}")
        if working:
            return self._wait(receipt, generation, "awaiting broker confirmation that conid orders are gone")
        if position is None:
            if generation <= self._last_action_generation(receipt):
                return self._wait(receipt, generation, "awaiting a newer generation to prove the position is closed")
            return self._finish(receipt, "CLOSED", generation_id=generation,
                                detail="fresh broker generation shows no position and no working orders for conid")
        if receipt.goal == "partial":
            own = [c for c in receipt.children if c.kind == "reduce" and c.root_id == receipt.cause_command_id]
            if any(c.state in ("FILLED", "CANCELLED", "REJECTED", "ABSENT") for c in own):
                return self._start_reprotect(receipt, snapshot, position)
            return self._submit_partial(receipt, snapshot, position)
        return self._submit_reduces(receipt, snapshot, (position,))

    def _submit_partial(self, receipt, snapshot, position) -> LiquidationReceipt:
        held = abs(float(position.quantity))
        q = float(receipt.goal_quantity)
        if held <= q or held - q < 1:
            # R15 at dispatch: protection is already cancelled, so close the live remainder.
            self.upgrade_to_zero(receipt.cause_command_id)
            receipt = self._store.receipt(receipt.cause_command_id)
            return self._submit_reduces(receipt, snapshot, (position,))
        return self._submit_reduces(receipt, snapshot, (position,), partial=q)

    # -- re-protect (exit-only OCA, R13) ------------------------------------------------

    @staticmethod
    def _protective(position, stop_price: float, target_price: Optional[float]) -> Optional[str]:
        price = getattr(position, "market_price", None)
        if price is None or not math.isfinite(float(price)):
            return "MARKET_PRICE_MISSING: cannot check the stop side without a market price"
        long = float(position.quantity) > 0
        if (long and not stop_price < price) or (not long and not stop_price > price):
            return "STOP_NOT_PROTECTIVE: stop is not on the protective side of the market price"
        if target_price is not None and ((long and not target_price > price) or (not long and not target_price < price)):
            return "TARGET_NOT_VALID: target is not on the profit side of the market price"
        return None

    def _latest(self, receipt, kind: str) -> Optional[ChildRef]:
        legs = [c for c in receipt.children if c.kind == kind and c.root_id == receipt.cause_command_id]
        return max(legs, key=lambda c: c.attempt) if legs else None

    def _working_legs(self, receipt) -> tuple[ChildRef, Optional[ChildRef]]:
        return self._latest(receipt, "reprotect-stop"), self._latest(receipt, "reprotect-target")

    def _leg_row(self, leg: ChildRef):
        rows = list(self._dispatch.find_orders(leg.account_id, leg.child_id))
        return rows[0] if len(rows) == 1 else None

    def _leg_status(self, leg: ChildRef) -> str:
        row = self._leg_row(leg)
        return "Unknown" if row is None else str(getattr(row, "status", "Unknown"))

    def _start_reprotect(self, receipt, snapshot, position) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        if receipt.stop_price is None:
            return self._escalate_now(receipt, snapshot, "STOP_PRICE_MISSING: no stop price for re-protect")
        problem = self._protective(position, float(receipt.stop_price), receipt.target_price)
        if problem is not None:
            return self._escalate_now(receipt, snapshot, problem)
        root = receipt.cause_command_id
        remaining = abs(float(position.quantity))
        side = _reducing_side(position.quantity)
        conid = int(receipt.conid)
        attempt = self._store.transaction(
            lambda conn: self._store.next_attempt_in_tx(conn, root, "reprotect-stop", conid))
        groups = (liquidation_child_id(root, "reprotect-stop", conid, attempt),) + (
            (liquidation_child_id(root, "reprotect-target", conid, attempt),)
            if receipt.target_price is not None else ())
        if self._protection is not None:
            # R2-4: the saga binds the replacement legs before they exist, so their events are never lost.
            self._protection.expect_reprotect(close_root_id=root, groups=groups, now=self._now())

        def build(conn):
            group = reprotect_oca_group(root, conid, attempt)
            legs = [self._new_child(conn, receipt, kind="reprotect-stop", conid=conid, generation=generation,
                                    side=side, quantity=remaining, price=float(receipt.stop_price), oca_group=group)]
            if receipt.target_price is not None:
                legs.append(self._new_child(conn, receipt, kind="reprotect-target", conid=conid,
                                            generation=generation, state="PLANNED", side=side,
                                            quantity=remaining, price=float(receipt.target_price), oca_group=group))
            return legs
        legs = self._reserve(receipt, build)
        if legs is None:
            return self._store.receipt(root)
        receipt = self._set(receipt, "REPROTECTING", generation_id=generation, phase="reprotect",
                            detail="re-protect stop submitted; target waits for the stop")
        self._send_leg(receipt, legs[0], position)
        return self._wait(self._store.receipt(root), generation, "awaiting broker acceptance of the re-protect stop")

    def _send_leg(self, receipt, leg: ChildRef, position) -> None:
        self._send(receipt, leg, lambda: self._dispatch.place_exit_leg(
            position, leg="stop" if leg.kind == "reprotect-stop" else "target", quantity=leg.quantity,
            price=leg.price, oca_group=leg.oca_group, child_id=leg.child_id))

    def _advance_reprotect(self, receipt, snapshot, working, position) -> LiquidationReceipt:
        """R13, R26, R30: the legs' own rows decide; a normal exit ends CLOSED; a leg that
        was refused, rejected, cancelled or lost is a re-protect failure (never sent again).

        #22: DONE is decided from one read of each leg's row. A row that no
        longer matches the child is observed again instead of finishing. Only
        a ``WORKING`` leg on a ``Submitted``/``PreSubmitted`` row is healthy
        protection; ``PendingCancel`` is not (ruling 47). The terminal write
        commits while broker changes are held, after the rows, the position
        and the generation are read again (ruling 48).
        """
        generation = int(snapshot.generation_id)
        stop, target = self._working_legs(receipt)
        if any(c.state == "UNKNOWN" for c in receipt.children):
            return self._wait(receipt, generation, "awaiting broker evidence for a re-protect leg")
        if not self._fresh(receipt, generation):
            return self._wait(receipt, generation, "awaiting a broker generation newer than the last leg fill")
        if position is None:
            # R26: a target fill cancels its OCA stop (or the stop filled): the position was closed by an exit.
            return self._finish_reprotect_closed(receipt, snapshot, working)
        if stop.state in ("CANCELLED", "PENDING_CANCEL") and target is not None \
                and target.state in ("WORKING", "FILLED") and generation <= stop.observed_generation:
            # R26: a target fill cancels its OCA stop; judge the cancel together with the target
            # and the position on a newer generation, never on the callback that came first.
            return self._wait(receipt, generation, "reconciling an OCA stop cancel with its target")
        if stop.state != "WORKING":
            return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: stop leg {stop.state}")
        remaining = abs(float(position.quantity))
        if target is not None and target.state == "PLANNED":
            status = self._leg_status(stop)
            if status not in _BROKER_HEALTHY:
                # Ruling 47: the target is sent only next to a stop whose row is healthy right now.
                receipt = self._observe_children(receipt, snapshot, int(self._dispatch.newest_generation()))
                return self._wait(receipt, generation, f"stop leg row is {status}, not healthy protection")
            sized = replace(target, state="UNKNOWN", quantity=remaining, fence_generation=generation)

            def promote(conn):
                self._store.update_child_in_tx(conn, sized, self._now())
                return [sized]
            if self._reserve(receipt, promote):
                self._send_leg(receipt, sized, position)
            return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                              "re-protect target submitted for the live remaining position")
        if target is not None and target.state != "WORKING":
            return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: target leg {target.state}")
        legs = [stop] + ([target] if target is not None else [])
        if generation <= max(leg.sent_generation or leg.fence_generation for leg in legs):
            return self._wait(receipt, generation, "awaiting a generation newer than the re-protect legs")
        side = _reducing_side(position.quantity)
        rows = [self._leg_row(leg) for leg in legs]
        for leg, row in zip(legs, rows):
            linked = row is not None and getattr(row, "oca_group", None) == stop.oca_group \
                and getattr(row, "oca_type", None) == 2 and getattr(row, "action", None) == side
            if not linked:
                return self._escalate_now(
                    receipt, snapshot, f"REPROTECT_FAILED: {leg.child_id} is not a linked protective leg at the broker")
        for leg, row in zip(legs, rows):
            if self._leg_changed(leg, row):
                receipt = self._observe_children(receipt, snapshot, int(self._dispatch.newest_generation()))
                return self._wait(receipt, generation, f"{leg.child_id} changed since it was observed; observed again")
            if leg.outstanding_quantity != remaining:
                return self._wait(receipt, generation,
                                  f"{leg.child_id} outstanding {leg.outstanding_quantity} != position {remaining}")
        state = self._partial_outcome(receipt)
        detail = ("re-protect legs working in one OCA group for the remaining quantity" if state == "DONE" else
                  "the partial reduce sold nothing; the position is protected again, the close failed")
        return self._finish_held(receipt, state, generation=generation, legs=legs, rows=rows,
                                 remaining=remaining, detail=detail)

    def _finish_held(self, receipt, state: str, *, generation: int, legs, rows, remaining: float,
                     detail: str) -> LiquidationReceipt:
        """Ruling 48: DONE / REDUCE_FAILED and the owner release commit while broker changes are held.

        Under the hold no ingest batch or generation promote can write, so
        the rows, position and generation read again here are the ones the
        terminal transaction commits against. Any change since the decision,
        or a hold that cannot be taken, waits for the next tick.
        """
        try:
            with self._dispatch.hold_broker_changes():
                why = self._changed_since_decision(receipt, generation, legs, rows, remaining)
                if why is None:
                    self._commit_terminal(receipt, state, generation_id=generation,
                                          remaining_quantity=remaining, detail=detail)
        except BrokerChangesBusy as ex:
            why = f"broker changes could not be held: {ex}"
        if why is not None:
            return self._wait(receipt, generation, f"{state} not committed: {why}")
        return self._after_terminal(receipt, state, detail)

    def _changed_since_decision(self, receipt, generation: int, legs, rows, remaining: float) -> Optional[str]:
        try:
            if [self._leg_fingerprint(self._leg_row(leg)) for leg in legs] != [self._leg_fingerprint(r) for r in rows]:
                return "a re-protect leg row changed since the decision"
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as ex:  # an unreadable broker is a reason to decide again, never to finish
            return f"broker evidence unreadable under the hold: {ex}"
        if int(snapshot.generation_id) != generation:
            return f"broker generation moved from {generation} to {snapshot.generation_id}"
        position = self._position_for(snapshot, receipt.conid)
        if position is None or abs(float(position.quantity)) != remaining:
            return "the position changed since the decision"
        return None

    @staticmethod
    def _leg_changed(leg: ChildRef, row) -> bool:
        """#22: True when the leg's row no longer says what the WORKING child recorded.

        Only ``Submitted`` / ``PreSubmitted`` count: a ``PendingCancel`` row is changed (ruling 47).
        """
        filled = float(getattr(row, "filled_quantity", 0.0) or 0.0)
        total = float(getattr(row, "total_quantity", 0.0) or 0.0)
        return (getattr(row, "status", None) not in _BROKER_HEALTHY or filled != leg.filled_quantity
                or max(total - filled, 0.0) != leg.outstanding_quantity)

    @staticmethod
    def _leg_fingerprint(row) -> Optional[tuple]:
        """Everything DONE reads from a leg's row; it must not change before the terminal write (#22, ruling 48)."""
        if row is None:
            return None
        return tuple(getattr(row, field, None) for field in
                     ("status", "filled_quantity", "total_quantity", "oca_group", "oca_type", "action", "revision"))

    def _partial_outcome(self, receipt) -> str:
        """R25 / D4: protection restored is not the requested reduction. Only a proven fill is DONE."""
        sold = sum(c.filled_quantity for c in receipt.children
                   if c.kind == "reduce" and c.root_id == receipt.cause_command_id and c.state != "ABSENT")
        return "DONE" if sold > 0 else "REDUCE_FAILED"

    def _finish_reprotect_closed(self, receipt, snapshot, working) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        receipt = self._cancel_conid_orders(receipt, snapshot, working)
        why = self._blocking(receipt, generation)
        if why is not None or working:
            return self._wait(receipt, generation, f"position closed by an exit; cancelling residual legs ({why})")
        if generation <= self._last_action_generation(receipt):
            return self._wait(receipt, generation, "awaiting a newer generation to prove no residual exits")
        return self._finish(receipt, "CLOSED", generation_id=generation,
                            detail="position closed by a re-protect exit; no residual exits")

    # -- escalation ----------------------------------------------------------------------

    def _escalate(self, receipt, reason: str) -> None:
        """Give up on the partial goal: trip the breaker and continue as a full close."""
        if self._breaker is not None:
            self._breaker.trip_liquidation(receipt.cause_command_id, reason)
        root = receipt.cause_command_id

        def write(conn):
            self._registry.upgrade_goal_in_tx(conn, root, self._now())
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(
                run, state="CANCELLING", goal="zero", goal_quantity=None, phase="cancel", escalated=True,
                deadline=self._now() + dt.timedelta(seconds=self._deadline_seconds),
                detail=f"escalated to full close: {reason}"), self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
        self._store.transaction(write)

    def _escalate_now(self, receipt, snapshot, reason: str) -> LiquidationReceipt:
        self._escalate(receipt, reason)
        return self._advance_conid(self._store.receipt(receipt.cause_command_id), snapshot)
```

6. PendingCancel and the held terminal write (rulings 47, 48). In Task 4's code:

   - constants: `CHILD_STATES` gains `"PENDING_CANCEL"`, and add
     ```python
     # Live at the broker: blocks every new reduce. Only WORKING is healthy protection (ruling 47).
     CHILD_LIVE = ("WORKING", "PENDING_CANCEL")
     CHILD_OPEN = ("UNKNOWN",) + CHILD_LIVE
     _BROKER_HEALTHY = frozenset({"PreSubmitted", "Submitted"})
     ```
   - `_evidence`: a `PendingCancel` row is its own state, checked before `_BROKER_ACCEPTED`:
     ```python
             elif status == "PendingCancel":
                 state = "PENDING_CANCEL"  # still live (it blocks), but never healthy protection (ruling 47)
     ```
     (`_legacy_evidence` keeps `PendingCancel` as `WORKING`: a wildcard child only blocks.)
   - `_observe_children` re-reads `child.state in CHILD_OPEN`; `_blocking` blocks on `child.state in CHILD_LIVE` (detail `f"{child.child_id} still working ({child.state})"`); the cancels a root already sent cover their target while `c.state in CHILD_OPEN`; `inherit_children_in_tx` moves `state IN ('UNKNOWN', 'WORKING', 'PENDING_CANCEL')`. `_cancel_targets` still takes only `WORKING` legs: a leg pending cancellation is not cancelled again.
   - a new error and port method (`from typing import ..., ContextManager`):
     ```python
     class BrokerChangesBusy(RuntimeError):
         """Broker writes could not be held (lock busy, or a generation is staging); decide again later."""

     # LiquidationDispatchPort
         def hold_broker_changes(self) -> ContextManager[None]:
             """No broker row or position changes while held; raises ``BrokerChangesBusy`` (ruling 48)."""
     ```
   - `_finish` splits so the DONE path can commit inside the hold and clean up after it:

```python
    def _finish(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """(docstring unchanged)"""
        self._commit_terminal(receipt, state, generation_id=generation_id, detail=detail, **fields)
        return self._after_terminal(receipt, state, detail)

    def _commit_terminal(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> None:
        root = receipt.cause_command_id
        owner_state = STATE_RELEASED if state in OWNER_RELEASED_STATES else STATE_FAILED_SAFE

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            updated = replace(run, state=state, detail=detail, cleanup_pending=True,
                              generation_id=run.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
            self._registry.finish_in_tx(conn, root, owner_state, self._now())
        self._store.transaction(write)

    def _after_terminal(self, receipt, state: str, detail: str) -> LiquidationReceipt:
        root = receipt.cause_command_id
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._cleanup(self._store.receipt(root))
```

   `trader/trading/broker_ingest.py`: `_apply_lock` becomes a `threading.RLock()` (the holder reads the snapshot, whose readiness check takes the lock again), and add (`from contextlib import contextmanager`):

```python
    @contextmanager
    def hold_changes(self, timeout_seconds: float = 2.0):
        """No broker row or position changes while held (SP1 ruling 48).

        Live batches apply under ``_apply_lock``; a promote runs only while a
        generation is staging, and staging begins under the same lock. So
        holding the lock with no generation staging stops every broker write.
        Raises ``BrokerChangesBusy`` when the lock is not free in time or a
        generation is staging. Keep the held section short: ingest waits.
        """
        from trader.trading.liquidation_service import BrokerChangesBusy
        if not self._apply_lock.acquire(timeout=timeout_seconds):
            raise BrokerChangesBusy(f"broker ingest busy for more than {timeout_seconds}s")
        try:
            if self._generation is not None:
                raise BrokerChangesBusy(f"broker generation {self._generation.generation_id} is staging")
            yield
        finally:
            self._apply_lock.release()
```

   `TradingRuntimeOrderDispatch` (`trader/trading/trading_runtime.py`), next to `newest_generation`:

```python
    def hold_broker_changes(self):
        """``BrokerIngest.hold_changes`` for the close's terminal write (SP1 ruling 48)."""
        from trader.trading.liquidation_service import BrokerChangesBusy
        ingest = getattr(self._trader, 'broker_ingest', None)
        if ingest is None:
            raise BrokerChangesBusy('no broker ingest to hold')
        return ingest.hold_changes()
```

   `_LiquidationDispatch` (`trader/trading/command_stack.py`): `def hold_broker_changes(self): return self._dispatch.hold_broker_changes()`.

Notes: `_escalate` never captures a snapshot; `_escalate_now` continues on the snapshot it holds, and `escalated=True` makes a second deadline miss `FAILED_SAFE`, so there is no loop. There is no `_retry_leg`: R30.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_close_broker_evidence.py -q --timeout=30`
Expected: all PASS (104 in `test_liquidation_service.py` before round 4; the round-4 tests are added on top).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py trader/trading/broker_ingest.py trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_liquidation_service.py tests/test_close_broker_evidence.py
git commit -m "feat: partial close with a linked re-protect and escalation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 7: Account flatten takes over scoped closes in the right order

Spec 5.1, "An account flatten takes over every scoped owner": (1) claim and mark the scoped owners **and their runs** `SUPERSEDED` and inherit their open children, all in one transaction (R6, R9); (2) superseded closes stop at once (their `PLANNED` legs become `NOT_SENT`; `update_run_in_tx` refuses to move a `SUPERSEDED` run); (3) the inherited children are reconciled from broker evidence like any child; (4) every identified working order is cancelled, replacement protection included — also a working leg the captured snapshot missed, found by its own row, and the saga is told before each cancel batch (R23, R27); (5) no reduce while any child is unknown or the last fill is not yet behind a newer generation; (6) then reduce. A deadline with a child still unknown ends `FAILED_SAFE`, never a second order. The takeover (claim, supersede, inheritance) is one transaction; a failure inside it leaves the scoped close as it was (test injects the failure in the middle). `handover_account` takes every live saga, including sagas a scoped close already owns (R14).

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`_claim_account_in_tx`, new `_supersede_run_in_tx`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `ExitClaim.superseded` (Task 3); `inherit_children_in_tx` (Task 4); `drop_planned_in_tx`.
- Produces: `LiquidationReceipt.superseded_by` is set on every run an account flatten takes over. `supersede` is not a public entry point (ruling 14).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_liquidation_service.py`:

```python
# ---------------------------------------------------------------------------
# Task 7: account flatten takes over scoped closes
# ---------------------------------------------------------------------------

def test_kill_during_reprotect_cancels_replacement_exits_and_reaches_flat(tmp_path):
    """Spec test: kill during REPROTECTING with a working replacement stop."""
    stop_leg = _order("rs", group="p-1-reprotect-stop-1-1", total=6.0, order_type="STP")
    s, protection = _to_reprotect(tmp_path, target=None, extra=(
        _snapshot(4, [_priced(6.0)], [stop_leg]), _snapshot(5, [_priced(6.0)]), _snapshot(6, []), _snapshot(7, [])))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)                 # gen 4
    p1 = s.service.receipt_for("p-1")
    assert (p1.state, p1.superseded_by) == ("SUPERSEDED", "kill-1")
    assert s.registry.get("p-1").state == "SUPERSEDED"
    assert s.registry.account_owner(ACCOUNT).root_id == "kill-1"
    assert ("handover_account", "kill-1", ("rs",)) in protection.calls
    assert [c.child_id for c in receipt.children if c.kind == "reprotect-stop"] == ["p-1-reprotect-stop-1-1"]
    assert s.dispatch.calls[-1] == ("cancel", "rs", "kill-1-cancel-1-1")   # cancelled, not waited on
    s.dispatch.entities["rs"] = _row("Cancelled", total=6.0)
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row("Cancelled")]
    s.service.rescan()                                                     # gen 5: reduce the rest
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")
    s.dispatch.rows["kill-1-reduce-1-1"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.receipt_for("kill-1").state == "VERIFYING"
    assert s.service.rescan().state == "FLAT"                               # gen 7
    assert s.service.receipt_for("p-1").state == "SUPERSEDED"
    assert protection.calls[-1] == ("close_after_full", "kill-1")
    assert s.registry.get("kill-1").state == "RELEASED"


def test_flatten_never_reduces_while_an_inherited_reprotect_leg_is_invisible(tmp_path):
    """#23: a submitted replacement stop that find_orders does not show yet blocks the reduce."""
    s, _protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.complete = False                                                       # nothing proves it absent
    receipt = s.service.start(ACCOUNT, "kill-1", NOW + dt.timedelta(seconds=30))     # gen 4 = leg fence + 1
    assert "outcome unknown" in receipt.detail
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)


def test_flatten_deadline_with_an_inherited_unknown_reduce_is_failed_safe_not_a_second_order(tmp_path):
    """Spec test: a submitted child not yet visible blocks the flatten; deadline = FAILED_SAFE."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(10.0)])],
               protection=_Protection())
    s.dispatch.complete = False                                         # the partial reduce stays invisible
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    receipt = s.service.start(ACCOUNT, "kill-1", NOW + dt.timedelta(seconds=30))
    assert [(c.child_id, c.state) for c in receipt.children] == [("p-1-reduce-1-1", "UNKNOWN")]
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial"]


def test_account_flatten_during_partial_close_reconciles_its_children_before_reducing(tmp_path):
    """Spec test: the partial reduce is still working when the flatten starts."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(8.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Submitted", filled=2.0, total=4.0)]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)                     # gen 2: still working
    assert "still working" in receipt.detail
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()                                                        # gen 3: fill seen
    s.service.rescan()                                                        # gen 4: fresh
    assert [c[0] for c in s.dispatch.calls] == ["reduce_partial", "reduce"]
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")


def test_superseded_close_stops_at_once_and_never_reprotects(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.start(ACCOUNT, "kill-1", DEADLINE)
    s.service.rescan()
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert s.service.receipt_for("p-1").state == "SUPERSEDED"


def test_restart_after_supersede_never_dispatches_for_the_superseded_root(tmp_path):
    """R20: crash after the takeover transaction, before the account hand-over."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    protection.crash_on.add("handover_account")
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert s.store.receipt("p-1").state == "SUPERSEDED"
    service = s.restart()
    service.rescan()
    service.rescan()
    assert ("handover_account", "kill-1", ()) in protection.calls
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "kill-1-reduce-1-1")


def test_later_scoped_close_inherits_the_unknown_child_of_a_failed_safe_close(tmp_path):
    """R9 scoped: the new close of the conid waits for the old unknown reduce."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    s.push(_snapshot(2, [_priced(10.0)]))
    receipt = s.service.start(ACCOUNT, "c-2", NOW + dt.timedelta(minutes=5), scope="conid", conid=1)
    assert receipt.cause_command_id == "c-2"
    assert [c.child_id for c in receipt.children] == ["c-1-reduce-1-1"]
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_a_working_leg_that_appeared_after_the_capture_is_cancelled_and_blocks(tmp_path):
    """R23 / #21: the snapshot missed the replacement stop; its own row shows it working."""
    protection = _Protection(stop_price=95.0, target_price=None)
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, [_priced(6.0)])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()                                                  # gen 3: stop leg sent
    leg = "p-1-reprotect-stop-1-1:stop"
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row(entity=leg)]
    receipt = s.service.start(ACCOUNT, "kill-1", DEADLINE)              # gen 4: no working order in the snapshot
    assert ("handover_account", "kill-1", (leg,)) in protection.calls
    assert s.dispatch.calls[-1] == ("cancel", leg, "kill-1-cancel-1-1")
    assert "still working" in receipt.detail
    assert not any(c[0] == "reduce" for c in s.dispatch.calls)


def test_account_claim_supersede_and_inheritance_commit_together_or_not_at_all(tmp_path):
    """R6 / R20: a failure inside the takeover transaction leaves the scoped close untouched."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, **_kwargs):
        raise _Crash()
    s.store.inherit_children_in_tx = broken
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert (s.registry.get("p-1").state, s.registry.account_owner(ACCOUNT)) == ("ACTIVE", None)
    assert s.store.receipt("p-1").state != "SUPERSEDED" and s.store.receipt("kill-1") is None
    assert s.service.root_for("kill-1") is None


def test_account_takeover_waits_for_a_generation_newer_than_the_scoped_roots_fill(tmp_path):
    """Ruling 43: the scoped close's reduce filled on generation 2 and the cache still says 10.
    The account flatten that takes over on generation 2 sends nothing until a newer one."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.push(_snapshot(2, [_priced(10.0)]))
    s.service.rescan()                                        # gen 2: the fill is observed
    s.service.start(ACCOUNT, "kill-1", DEADLINE)
    assert s.store.receipt("c-1").state == "SUPERSEDED"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    s.push(_snapshot(3, []), _snapshot(4, []))
    s.service.rescan()
    assert s.service.receipt_for("kill-1").state == "FLAT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 7 failed (the scoped runs are not `SUPERSEDED`, so they keep re-protecting and reducing), 107 passed. `test_later_scoped_close_inherits_the_unknown_child_of_a_failed_safe_close`, `test_account_claim_supersede_and_inheritance_commit_together_or_not_at_all` and `test_account_takeover_waits_for_a_generation_newer_than_the_scoped_roots_fill` already pass: inheritance, the one-transaction claim and the fill watermark (ruling 43) came with Task 4; the tests pin them for the takeover.

- [ ] **Step 3: Implement**

Replace `_claim_account_in_tx` and add `_supersede_run_in_tx` after it:

```python
    def _claim_account_in_tx(self, conn, account_id, cause, deadline):
        existing = self._existing_in_tx(conn, account_id, cause, conid=None, goal="account", quantity=None)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_account_in_tx(conn, account_id=account_id, root_id=cause, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, None, claim.outcome,
                                                    "account", None), now)
        if claim.outcome == JOINED_FLATTEN:
            return (JOINED_FLATTEN, claim.root_id)
        self._store.insert_run_in_tx(conn, LiquidationReceipt(account_id, cause, "REQUESTED", deadline), now)
        for root in claim.superseded:
            self._supersede_run_in_tx(conn, root, by_root_id=cause)
        self._store.inherit_children_in_tx(conn, account_id=account_id, conid=None, to_root_id=cause, now=now)
        return (CLAIMED, cause)
```

```python
    def _supersede_run_in_tx(self, conn, root_id: str, *, by_root_id: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL:
            return
        self._store.update_run_in_tx(conn, replace(
            run, state="SUPERSEDED", superseded_by=by_root_id,
            detail=f"taken over by account flatten {by_root_id}"), self._now())
        self._store.drop_planned_in_tx(conn, root_id, self._now())
```

The supersede runs before `inherit_children_in_tx` in the same transaction, so the inherit query already sees the runs as `SUPERSEDED`. The `_set` of a superseded root is refused by the store (R7), and `_tick` returns a `SUPERSEDED` receipt without acting.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 114 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: account flatten supersedes scoped closes and inherits their children

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 8: Scoped `start` claims through the registry — join, upgrade, refuse

A scoped request never creates a competing root. The registry decides inside the claim transaction: `CLAIMED` creates the run; `JOINED` and `JOINED_FLATTEN` record the join row and return the owner's receipt (the caller polls that root, R10); `UPGRADED` moves the owner's registry goal **and** its run to zero in the same transaction (R6), so a crash can never leave a `zero` owner with a `partial` cursor; a partial request against any owner raises `ExitInProgress` and writes nothing. The account owner is checked first and a `SUPERSEDED` owner is never upgraded (registry rules, Task 3). A finished root can never be joined or upgraded: its owner was released (or marked `FAILED_SAFE`) in the same transaction as its terminal state (R24), so `_upgrade_run_in_tx` only ever meets an open run. A retried command id returns its durable root, after a restart too, and cannot be rebound (ruling 17).

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`_claim_scoped_in_tx`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `UPGRADED` from `trader.trading.exit_owner`; `_upgrade_run_in_tx` and the request/admitted split of `_claim_scoped_in_tx` (Task 6).
- Produces: `start(..., scope="conid")` → `ExitInProgress` propagates; `root_for(command_id)` names the root a command must follow.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_liquidation_service.py`:

```python
# ---------------------------------------------------------------------------
# Task 8: scoped claims join, upgrade or refuse
# ---------------------------------------------------------------------------

from trader.trading.exit_owner import ExitInProgress  # noqa: E402


def test_second_full_close_joins_and_returns_the_owners_receipt(tmp_path):
    """Spec test: a time exit and an AI close on the same conid make one root."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    joined = s.service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "exit-1"
    assert s.service.receipt_for("ai-close-1") is None
    assert s.service.root_for("ai-close-1") == "exit-1"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_time_exit_during_partial_close_upgrades_goal_and_ends_closed(tmp_path):
    """Spec test: zero position and no residual exits, not DONE."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]),
                          _snapshot(3, [_priced(6.0)]), _snapshot(4, []), _snapshot(5, [])], protection=protection)
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    upgraded = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert (upgraded.cause_command_id, upgraded.goal) == ("p-1", "zero")
    assert s.registry.get("p-1").goal == "zero"
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-reduce-1-2")
    s.dispatch.rows["p-1-reduce-1-2"] = [_row("Filled", filled=6.0, total=6.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    assert not any(c[0] == "place_exit_leg" for c in s.dispatch.calls)
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_against_an_existing_owner_raises_and_records_nothing(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    with pytest.raises(ExitInProgress):
        s.service.start(ACCOUNT, "p-2", DEADLINE, scope="conid", conid=1, quantity=3.0)
    assert s.service.root_for("p-2") is None


def test_full_close_during_active_flatten_joins_it_and_leaves_the_superseded_owner_alone(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    joined = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "flat-1"
    p1 = s.service.receipt_for("p-1")
    assert (p1.state, p1.goal, p1.goal_quantity) == ("SUPERSEDED", "partial", 4.0)
    assert s.registry.get("p-1").state == "SUPERSEDED"


def test_upgrade_never_touches_a_superseded_run(tmp_path):
    """#24: an upgrade that arrives after the takeover cannot revive the close."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)])],
               protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    receipt = s.service.upgrade_to_zero("p-1")
    assert (receipt.state, receipt.goal) == ("SUPERSEDED", "partial")


def test_claim_and_run_commit_together_or_not_at_all(tmp_path):
    """R6 / #24: a failure after the claim insert leaves no owner, no join and no run."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    real_insert = s.store.insert_run_in_tx

    def broken_insert(conn, receipt, now):
        raise RuntimeError("disk full")
    s.store.insert_run_in_tx = broken_insert
    with pytest.raises(RuntimeError):
        s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    assert s.registry.get("c-1") is None
    assert s.service.root_for("c-1") is None
    s.store.insert_run_in_tx = real_insert
    assert s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1).cause_command_id == "c-1"


def test_joined_request_retried_after_restart_returns_the_same_root(tmp_path):
    """#24: a retry of a joined command finds its durable root, not a new one."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])], protection=_Protection())
    s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    s.service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    service = s.restart()
    again = service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    assert again.cause_command_id == "exit-1"
    with pytest.raises(ValueError):
        service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=2)
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_closed_root_releases_the_owner_and_a_later_close_is_a_new_root_after_restart(tmp_path):
    """#23: after CLOSED and a restart, the next close of the conid starts fresh."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=_Protection())
    s.service.start(ACCOUNT, "c-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.rows["c-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "CLOSED"
    service = s.restart()
    s.push(_snapshot(4, [_position(3.0)]))
    receipt = service.start(ACCOUNT, "c-2", DEADLINE, scope="conid", conid=1)
    assert receipt.cause_command_id == "c-2"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 3.0, "c-2-reduce-1-1")


def test_upgraded_claim_and_run_change_commit_together_or_not_at_all(tmp_path):
    """R6 / R20: a failure after the registry upgrade, inside the claim transaction, undoes it."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)], [_stop_order()])], protection=_Protection())
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)

    def broken(conn, root_id, detail):
        raise _Crash()
    s.service._upgrade_run_in_tx = broken
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert (s.registry.get("p-1").goal, s.store.receipt("p-1").goal) == ("partial", "partial")
    assert s.service.root_for("exit-1") is None


def test_a_full_close_during_an_unfinished_done_cleanup_starts_a_new_root(tmp_path):
    """R24 / #24: DONE released the owner already, so a later close never joins the finished root."""
    s, protection = _to_reprotect(tmp_path, target=None, extra=(_snapshot(4, [_priced(6.0)]),))
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    protection.crash_on.add("release_after_partial")
    with pytest.raises(_Crash):
        s.service.rescan()                                             # DONE, cleanup cut off
    receipt = s.service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert receipt.cause_command_id == "exit-1"
    assert (s.service.receipt_for("p-1").state, s.service.receipt_for("p-1").goal) == ("DONE", "partial")
    assert s.registry.owner_for(ACCOUNT, 1).root_id == "exit-1"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 2 failed — `test_time_exit_during_partial_close_upgrades_goal_and_ends_closed` (the registry says `zero`, the run still says `partial`, so it re-protects) and `test_upgraded_claim_and_run_change_commit_together_or_not_at_all` (nothing changes the run inside the claim, so the injected failure never happens); 122 passed. The other new tests pin rules that Tasks 3–7 already give (atomic claim, durable join, no revival of a superseded close, no join of a finished root).

- [ ] **Step 3: Implement**

Import `UPGRADED` from `trader.trading.exit_owner` and replace `_claim_scoped_in_tx` with:

```python
    def _claim_scoped_in_tx(self, conn, account_id, cause, conid, requested, admitted, deadline,
                            stop_price, target_price):
        """The join row keeps the request as it came in; the owner and run get the admitted goal."""
        goal = "zero" if requested is None else "partial"
        existing = self._existing_in_tx(conn, account_id, cause, conid=conid, goal=goal, quantity=requested)
        if existing is not None:
            return existing
        now = self._now()
        claim = self._registry.claim_scoped_in_tx(conn, account_id=account_id, conid=conid, root_id=cause,
                                                  goal_quantity=admitted, now=now)
        self._store.record_join_in_tx(conn, JoinRow(cause, claim.root_id, account_id, conid, claim.outcome,
                                                    goal, requested), now)
        if claim.outcome == CLAIMED:
            self._store.insert_run_in_tx(conn, LiquidationReceipt(
                account_id, cause, "REQUESTED", deadline, scope="conid", conid=conid,
                goal="zero" if admitted is None else "partial", goal_quantity=admitted,
                stop_price=stop_price, target_price=target_price), now)
            self._store.inherit_children_in_tx(conn, account_id=account_id, conid=conid, to_root_id=cause, now=now)
        elif claim.outcome == UPGRADED:
            self._upgrade_run_in_tx(conn, claim.root_id, f"goal upgraded to zero by {cause}")
        return (claim.outcome, claim.root_id)
```

`start` does not advance a joined or upgraded root (it returns `self._store.receipt(root)`): the owner acts on its own ticks, so a joining caller never sends an order on a snapshot the owner already used.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_exit_owner.py -q --timeout=30`
Expected: 139 passed. Then the full suite: green.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: scoped closes join, upgrade or refuse through the exit owner registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 9: Protective saga — `CLOSE_OWNED`, exact hand-over, release, retired legs, one writer discipline

While a close owns protection, a cancel of one of the exact order ids the close asked to cancel is expected and is not `MISSING_PROTECTION`. `Inactive`/`Rejected`, or a cancel of any other order, still takes today's incident path (R14, ruling 12). The close hands over again before every cancel batch, so a later target is expected too (R27). After a partial close the saga goes back to protection for the remaining quantity with the new legs as its only live protection (a new protection generation); events of retired legs never change it. After a full close it is `CLOSED`. An account flatten takes every live saga, including those a scoped close owns.

Review round 2 adds three rules here:
- **One writer discipline (R28, R2-3).** The ingest thread (`on_broker_event`) and the liquidation worker (hand-over, release, close) both read, change and save saga rows. `save_in_tx` is now an `UPDATE ... WHERE command_id = ?` that first checks the stored revision is the one the change was built on (an `INSERT` only for a new saga); otherwise it raises `SagaRevisionConflict` and the whole journal transaction rolls back, event-id record included. Every writer retries from a fresh read inside the saga (`_retrying`), so a conflict never reaches the ingest, which would log and drop the event. No lock spans the worker and the ingest, so ruling 8's deadlock cannot come back. The update never touches the indexed `order_group_id` column.
- **Replacement legs are bound before they exist (R2-4).** The close calls `expect_reprotect` with the legs' order groups before it sends them. They are stored as *pending* groups (protection generation + 1). A pending leg's event under `CLOSE_OWNED` is bookkeeping (the close judges its own legs and escalates), but a rejected or unasked cancel is remembered (`pending_protection_lost`). A cancel of a pending leg by the close, an upgrade or an account takeover is expected like any other handed-over ref.
- **Release from broker evidence.** `release_after_partial` gets each leg's broker status from the close (read when it cleans up) and runs it through today's event rules: a working stop gives `PROTECTED`; a cancelled or rejected stop, or a remembered pending loss, is `SAFETY_FAILED` with the breaker trip and the account flatten.
- **No flatten for a historic failure (R29).** `flatten_requested` is set when this version moves a saga to `SAFETY_FAILED`; migration 37 sets it `FALSE` for existing rows. `unhandled_failures` returns only flagged sagas, so the first deploy does not flatten for an old failure without a fresh trigger.

**Files:**
- Modify: `trader/automation/protective_order_saga.py`
- Modify: `trader/trading/broker_ingest.py` (`_notify_protective_saga`: pass `order_entity_id`)
- Test: `tests/automation/test_protective_order_saga.py`, `tests/automation/test_attribution_ledger.py`

**Interfaces (the saga implements `ProtectionOwnershipPort` from Task 4):**

```python
PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 37
def apply_protective_order_saga_migration(migrator) -> bool       # applies 30 and 37; returns 30's result
SAGA_STATES |= {"CLOSE_OWNED"}
class SagaRevisionConflict(RuntimeError)
BrokerOrderEvent: + order_entity_id: Optional[str] = None
SagaState: + close_root_id: Optional[str] = None, + expected_cancel_ids: tuple[str, ...] = (),
           + handover_generation: Optional[int] = None, + protection_generation: int = 0,
           + active_groups: tuple[str, ...] = (), + pending_groups: tuple[str, ...] = (),
           + pending_protection_lost: bool = False, + flatten_requested: bool = False; property current_groups
ProtectiveOrderSagaStore.load_by_group(order_group_id) -> Optional[tuple[SagaState, int]]   # (saga, group generation)
ProtectiveOrderSagaStore.load_live(account_id, conid=None) -> list[SagaState]   # not CLOSED / SAFETY_FAILED
ProtectiveOrderSagaStore.load_by_close_root(close_root_id) -> list[SagaState]
ProtectiveOrderSagaStore.load_flatten_requested(account_id) -> list[SagaState]
ProtectiveOrderSagaStore.save_in_tx(conn, state, now)           # revision-checked; SagaRevisionConflict
ProtectiveOrderSaga.handover(*, account_id, conid, close_root_id, cancels, generation, now) -> HandoverInfo
ProtectiveOrderSaga.handover_account(*, account_id, close_root_id, cancels, generation, now) -> None
ProtectiveOrderSaga.expect_reprotect(*, close_root_id, groups, now) -> None
ProtectiveOrderSaga.release_after_partial(*, close_root_id, remaining_quantity, stop_group, stop_status,
                                          target_group, target_status, now) -> None
ProtectiveOrderSaga.close_after_full(*, close_root_id, now) -> None
ProtectiveOrderSaga.unhandled_failures(account_id) -> list[str]   # flagged SAFETY_FAILED command ids (Task 15's tick)
```

- Migration 37: `account_id`, `conid`, `close_root_id`, `flatten_requested` columns on `automated_order_sagas` (backfilled from the payload; `flatten_requested` `FALSE`) and `automated_order_saga_groups(order_group_id PK, command_id, protection_generation)`.
- Hand-over prices come from `plan_json`: the `stop` leg's `stop_price`, the `take_profit` leg's `limit_price`.
- Several saga rows change in one journal transaction (`_persist_all` over `mutate_batch_work`), all revision-checked.

- [ ] **Step 1: Write the failing tests**

Append to `tests/automation/test_protective_order_saga.py`:

```python
# ---------------------------------------------------------------------------
# SP1 plan 1 Task 9: close ownership (CLOSE_OWNED), hand-over, release
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402


def _protected(tmp_path, **kw):
    saga, intent, state, breaker, liquidation, dispatch = _started(tmp_path, **kw)
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2), ("take_profit", 3)):
        saga.on_broker_event(_event(og, leg=leg, status="Submitted", order_id=oid))
    state = saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0, total=10.0))
    assert state.state == "PROTECTED"
    return saga, intent, state, breaker, liquidation


def _owned_event(order_group_id, *, leg, status, entity, filled=0.0, total=10.0):
    from trader.automation.protective_order_saga import BrokerOrderEvent
    return BrokerOrderEvent(order_group_id, leg, status, filled, total, 2,
                            f"{entity}:{status}:{filled}", NOW, order_entity_id=entity)


def _cancels(og, *entities):
    from trader.trading.liquidation_service import CancelTarget
    return tuple(CancelTarget(entity, og) for entity in entities)


def test_migration_37_adds_ownership_columns_and_groups_table(tmp_path):
    from trader.automation.protective_order_saga import PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION
    *_rest, db = _build_saga(tmp_path)
    assert PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION == 37
    cols = {r[0] for r in db.execute("DESCRIBE automated_order_sagas", fetch="all")}
    assert {"account_id", "conid", "close_root_id"} <= cols
    assert "flatten_requested" in cols
    group_cols = {r[0] for r in db.execute("DESCRIBE automated_order_saga_groups", fetch="all")}
    assert group_cols == {"order_group_id", "command_id", "protection_generation"}
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 37", fetch="one") == ("sp1_saga_close_ownership",)


def test_handover_records_exact_refs_and_generation_and_returns_prices(tmp_path):
    saga, intent, state, *_ = _protected(tmp_path)
    og = state.order_group_id
    info = saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                         cancels=_cancels(og, "og:stop", "og:tp") + _cancels("other", "x"), generation=7, now=NOW)
    owned = saga.resume(intent.command_id)
    assert (owned.state, owned.close_root_id, owned.handover_generation) == ("CLOSE_OWNED", "close-1", 7)
    assert owned.expected_cancel_ids == ("og:stop", "og:tp")
    assert (info.stop_price, info.target_price) == (150.0, 200.0)


def test_expected_cancel_under_close_owned_is_not_an_incident(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop"))
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert (state.state, state.stop_rejected) == ("CLOSE_OWNED", False)
    assert breaker.signals == [] and liquidation.starts == []


@pytest.mark.parametrize("status", ["Inactive", "Rejected"])
def test_unexpected_reject_during_handover_is_still_an_incident(tmp_path, status):
    """R14: only Cancelled/ApiCancelled of an expected ref is suppressed."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    state = saga.on_broker_event(_owned_event(og, leg="stop", status=status, entity="og:stop"))
    assert (state.state, state.error_code) == ("SAFETY_FAILED", "PROTECTION_LOST_DURING_CLOSE")
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert liquidation.starts[0][1] == intent.command_id


def test_cancel_of_a_ref_the_close_did_not_ask_for_is_an_incident(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert state.state == "SAFETY_FAILED"
    assert len(liquidation.starts) == 1


def test_late_entry_fill_event_while_owned_is_bookkeeping_only(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop"))
    state = saga.on_broker_event(_owned_event(og, leg="entry", status="Filled", entity="og:entry", filled=10.0))
    assert state.state == "CLOSE_OWNED" and breaker.signals == []


def test_unexpected_stop_cancel_without_handover_still_liquidates(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    state = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert state.state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert len(liquidation.starts) == 1


def test_account_takeover_transfers_a_close_owned_saga_and_keeps_its_expected_refs(tmp_path):
    """R14: the account root takes over a saga a scoped close already owns."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.handover_account(account_id=ACCOUNT, close_root_id="kill-1",
                          cancels=_cancels(og, "og:tp"), generation=8, now=NOW)
    owned = saga.resume(intent.command_id)
    assert (owned.state, owned.close_root_id) == ("CLOSE_OWNED", "kill-1")
    assert owned.expected_cancel_ids == ("og:stop", "og:tp")
    saga.close_after_full(close_root_id="kill-1", now=NOW)
    assert saga.resume(intent.command_id).state == "CLOSED"


def test_release_after_partial_protects_the_remainder_with_the_new_legs(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="PreSubmitted",
                               target_group="p-1-reprotect-target-265598-1", target_status="Submitted", now=NOW)
    released = saga.resume(intent.command_id)
    assert (released.state, released.close_root_id, released.protection_generation) == ("PROTECTED", None, 1)
    assert released.protection_quantity == Decimal("6")
    assert released.current_groups == ("p-1-reprotect-stop-265598-1", "p-1-reprotect-target-265598-1")
    state = saga.on_broker_event(_event("p-1-reprotect-stop-265598-1", leg="stop", status="Filled",
                                        filled=6.0, total=6.0, order_id=9))
    assert state.command_id == intent.command_id
    assert state.state in ("EXITING", "CLOSED")
    assert breaker.signals == []


def test_retired_leg_event_does_not_change_current_protection(tmp_path):
    """R14 / #25: a late cancel of the original stop after a release is ignored."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    state = saga.on_broker_event(_event(og, leg="stop", status="Cancelled", order_id=2, event_id="late-old-stop"))
    assert state.state == "PROTECTED"
    assert breaker.signals == [] and liquidation.starts == []
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-2", cancels=(), generation=9, now=NOW)
    saga.release_after_partial(close_root_id="p-2", remaining_quantity=3.0,
                               stop_group="p-2-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    state = saga.on_broker_event(_event("p-1-reprotect-stop-265598-1", leg="stop", status="Cancelled",
                                        order_id=9, event_id="late-first-replacement"))
    assert (state.state, state.protection_quantity) == ("PROTECTED", Decimal("3"))
    assert breaker.signals == []


def test_close_after_full_closes_the_saga_without_error(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    saga.close_after_full(close_root_id="close-1", now=NOW)
    closed = saga.resume(intent.command_id)
    assert (closed.state, closed.error_code) == ("CLOSED", None)
    later = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert later.state == "CLOSED" and breaker.signals == []


def test_ownership_survives_a_restart(tmp_path):
    saga, intent, state, *_ = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    from trader.automation.protective_order_saga import ProtectiveOrderSagaStore
    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    reloaded = ProtectiveOrderSagaStore(db).load_by_close_root("close-1")
    assert [s.command_id for s in reloaded] == [intent.command_id]


def test_handover_with_no_live_saga_returns_empty_prices(tmp_path):
    saga, *_rest = _build_saga(tmp_path)
    info = saga.handover(account_id=ACCOUNT, conid=999, close_root_id="close-x", cancels=(), generation=1, now=NOW)
    assert (info.stop_price, info.target_price) == (None, None)


def test_pending_replacement_legs_are_bound_before_release_and_their_loss_is_remembered(tmp_path):
    """R2-4 / #25: events of the close's own legs reach the saga before release; a lost leg is an incident at release."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop", "og:tp"), generation=7, now=NOW)
    saga.expect_reprotect(close_root_id="p-1", groups=("p-1-reprotect-stop-265598-1",), now=NOW)
    state = saga.on_broker_event(_owned_event("p-1-reprotect-stop-265598-1", leg="stop", status="Inactive",
                                              entity="p-1-reprotect-stop-265598-1:stop"))
    assert (state.state, state.pending_protection_lost) == ("CLOSE_OWNED", True)
    assert breaker.signals == [] and liquidation.starts == []      # the close escalates on its own evidence
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    released = saga.resume(intent.command_id)
    assert (released.state, released.error_code) == ("SAFETY_FAILED", "PROTECTION_LOST_DURING_CLOSE")
    assert liquidation.starts[0][1] == intent.command_id


def test_release_with_a_stop_that_is_not_working_at_the_broker_is_an_incident(tmp_path):
    """#25: the release takes the legs' broker status; it never assumes they work."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(state.order_group_id, "og:stop"), generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Cancelled",
                               target_group=None, target_status=None, now=NOW)
    assert saga.resume(intent.command_id).state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)


@pytest.mark.parametrize("taker", ["account", "upgrade"])
def test_cancelling_pending_legs_after_a_takeover_or_upgrade_is_expected(tmp_path, taker):
    """R2-4: the kill (or the upgraded close) cancels the replacement legs; that is not an incident."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1",
                  cancels=_cancels(og, "og:stop"), generation=7, now=NOW)
    saga.expect_reprotect(close_root_id="p-1", groups=("p-1-reprotect-stop-265598-1",), now=NOW)
    leg = _cancels("p-1-reprotect-stop-265598-1", "p-1-reprotect-stop-265598-1:stop")
    if taker == "account":
        saga.handover_account(account_id=ACCOUNT, close_root_id="kill-1", cancels=leg, generation=8, now=NOW)
    else:
        saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=leg, generation=8, now=NOW)
    restarted, *_rest = _build_saga(tmp_path)                       # a restart in between
    state = restarted.on_broker_event(_owned_event("p-1-reprotect-stop-265598-1", leg="stop", status="Cancelled",
                                                   entity="p-1-reprotect-stop-265598-1:stop"))
    assert (state.state, state.pending_protection_lost) == ("CLOSE_OWNED", False)
    assert breaker.signals == []


def test_a_later_cancel_target_is_added_to_the_expected_set(tmp_path):
    """R27 / R2-4: an order the close finds later is handed over before its cancel; an unasked cancel still fails."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:tp"),
                  generation=8, now=NOW)
    assert saga.resume(intent.command_id).expected_cancel_ids == ("og:stop", "og:tp")
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:tp"))
    assert state.state == "CLOSE_OWNED"
    state = saga.on_broker_event(_owned_event(og, leg="take_profit", status="Cancelled", entity="og:other"))
    assert state.state == "SAFETY_FAILED"


def _race(saga, *, reader, writer):
    """Run ``reader`` in a thread that stops right after its read; run ``writer``; let the reader save."""
    import threading
    read_done, go, result = threading.Event(), threading.Event(), {}
    store = saga._store
    real = store.load_by_group

    def paused(group):
        found = real(group)
        if not read_done.is_set():
            read_done.set()
            go.wait(timeout=5)
        return found
    store.load_by_group = paused
    thread = threading.Thread(target=lambda: result.setdefault("state", reader()))
    thread.start()
    assert read_done.wait(timeout=5)
    writer()
    go.set()
    thread.join(timeout=10)
    store.load_by_group = real
    return result["state"]


def test_an_ingest_event_racing_the_hand_over_never_loses_the_close_owner(tmp_path):
    """R28 / R2-3: both read PROTECTED; the hand-over commits first; the event is applied again on top."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    state = _race(saga, reader=lambda: saga.on_broker_event(_event(og, leg="stop", status="PreSubmitted",
                                                                   order_id=2, event_id="ib-working-7")),
                  writer=lambda: saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                                               cancels=_cancels(og, "og:stop"), generation=7, now=NOW))
    stored = saga.resume(intent.command_id)
    assert (stored.state, stored.close_root_id, stored.expected_cancel_ids) == ("CLOSE_OWNED", "close-1", ("og:stop",))
    assert "ib-working-7" in stored.seen_event_ids


def test_an_ingest_event_racing_the_release_keeps_the_new_protection_generation(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    _race(saga, reader=lambda: saga.on_broker_event(_owned_event(og, leg="stop", status="Cancelled", entity="og:stop")),
          writer=lambda: saga.release_after_partial(
              close_root_id="p-1", remaining_quantity=6.0, stop_group="p-1-reprotect-stop-265598-1",
              stop_status="Submitted", target_group=None, target_status=None, now=NOW))
    stored = saga.resume(intent.command_id)
    assert (stored.state, stored.protection_generation, stored.current_groups) == (
        "PROTECTED", 1, ("p-1-reprotect-stop-265598-1",))
    assert breaker.signals == []                                     # the retried event is a retired leg's


def test_an_ingest_event_racing_the_final_close_never_resurrects_the_saga(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    _race(saga, reader=lambda: saga.on_broker_event(_owned_event(og, leg="entry", status="Filled",
                                                                 entity="og:entry", filled=10.0)),
          writer=lambda: saga.close_after_full(close_root_id="close-1", now=NOW))
    assert saga.resume(intent.command_id).state == "CLOSED"


def test_a_stale_save_is_refused_with_a_revision_conflict(tmp_path):
    from trader.automation.protective_order_saga import SagaRevisionConflict
    saga, intent, state, *_ = _protected(tmp_path)
    stale = saga.resume(intent.command_id)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", cancels=(), generation=7, now=NOW)
    with pytest.raises(SagaRevisionConflict):
        saga._persist(replace(stale, revision=stale.revision + 1), NOW, from_state=stale.state)


def test_only_a_failure_seen_by_this_version_asks_the_worker_for_a_flatten(tmp_path):
    """R29: a saga that was SAFETY_FAILED before the upgrade starts no flatten on the first deploy."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    historic = replace(saga.resume(intent.command_id), state="SAFETY_FAILED", revision=state.revision + 1)
    saga._persist(historic, NOW, from_state="PROTECTED")             # as migration 37 leaves it
    assert saga.unhandled_failures(ACCOUNT) == []
    fresh, intent2, state2, *_ = _protected(tmp_path / "fresh")
    fresh.on_broker_event(_event(state2.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert fresh.unhandled_failures(ACCOUNT) == [intent2.command_id]
```

The race tests (`_race`) stop the ingest event right after its read, let the worker commit, then let the event save: it must retry on top of the worker's change. They run on the real journal and the real `on_broker_event`.

Append to `tests/automation/test_attribution_ledger.py` (uses `_forwarded_events` from Task 2):

```python
def test_broker_ingest_forwards_the_order_entity_id_to_the_saga(tmp_path):
    """SP1 plan 1 Task 9 (R14): the saga matches expected cancels by order identity."""
    seen, rows = _forwarded_events(tmp_path, order_ref=encode_order_ref(ORDER_GROUP), order_type="STP",
                                   parent_id=5, name="entity-id.duckdb")
    assert [e.order_entity_id for e in seen] == [r.order_entity_id for r in rows] == [f"{ORDER_GROUP}:stop"]
```

Round-2 verification fixes (ruling 39 and the R2-5 gaps). Append to `tests/automation/test_protective_order_saga.py`:

```python
def test_an_entry_event_saved_while_submit_bracket_waits_keeps_the_submitted_ids(tmp_path):
    """Round-2 verification N1: the ingest thread saves the entry's Submitted event while
    ``submit_bracket`` waits. ``start`` must not then fail its own write with a revision conflict
    (DISPATCH_AMBIGUOUS, ids lost): it re-reads and adds the ids on top of the ingest's state.
    (``_race`` pauses a read by order group; ``start`` holds a copy from before the call instead,
    so the ingest runs inside the dispatch here.)"""
    import threading

    holder = {}

    class _IngestDuringSubmit(FakeBracketDispatch):
        def submit_bracket(self, *, plan, intent, account_id):
            submitted = super().submit_bracket(plan=plan, intent=intent, account_id=account_id)
            ingest = threading.Thread(target=lambda: holder.setdefault("event", holder["saga"].on_broker_event(
                _event(plan.order_group_id, leg="entry", status="Submitted"))))
            ingest.start()
            ingest.join(timeout=5)
            return submitted

    saga, *_ = _build_saga(tmp_path, dispatch=_IngestDuringSubmit())
    holder["saga"] = saga
    intent = make_intent()
    state = saga.start(
        intent=intent, approval=make_approval(), request=FakeCommandRequest(intent.command_id),
        artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(CONID),),
                                 max_gross_allocation=0.06, parameters={}),
        session_state=SimpleNamespace(high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None),
        allocation=SimpleNamespace(max_gross_fraction=0.06),
    )
    assert holder["event"].state == "ENTRY_WORKING"
    stored = saga.resume(intent.command_id)
    assert (state.state, state.error_code) == ("ENTRY_WORKING", None)
    assert stored.state == "ENTRY_WORKING" and stored.submitted_order_ids == state.submitted_order_ids
    assert len(stored.submitted_order_ids) == 3


def test_saga_rows_from_before_the_upgrade_survive_migration_37(tmp_path):
    """R2-5 gap: a migration-30 journal with old payloads. Migration 37 backfills the columns; an old
    PROTECTED saga still takes its broker events and a hand-over; an old SAFETY_FAILED saga asks for
    no flatten (R29)."""
    import json

    from trader.automation.protective_order_saga import PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION

    db = DuckDBConnection.get_instance(str(tmp_path / "saga.duckdb"))
    SchemaMigrator(db).apply(PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION, "before_sp1", (
        """CREATE TABLE IF NOT EXISTS automated_order_sagas (command_id VARCHAR PRIMARY KEY,
           order_group_id VARCHAR NOT NULL, state VARCHAR NOT NULL, payload VARCHAR NOT NULL,
           updated_at TIMESTAMPTZ NOT NULL)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_automated_order_sagas_group ON automated_order_sagas(order_group_id)",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_events (event_id VARCHAR PRIMARY KEY,
           command_id VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    ))
    for command_id, state in (("old-1", "PROTECTED"), ("old-2", "SAFETY_FAILED")):
        payload = {  # the payload keys of master before SP1
            "command_id": command_id, "order_group_id": f"og-{command_id}", "order_ref": f"mmr:og-{command_id}",
            "state": state, "account_id": ACCOUNT, "conid": CONID, "side": "BUY", "requested_quantity": "10",
            "filled_quantity": "10", "protection_quantity": "10", "protection_working": True,
            "protection_adjusted": False, "entry_working": False, "stop_working": True, "target_working": True,
            "stop_filled": False, "target_filled": False, "entry_cancelled": False, "stop_rejected": False,
            "target_rejected": False, "submitted_order_ids": [1, 2, 3], "seen_event_ids": [], "revision": 5,
            "error_code": None, "plan_json": None}
        db.execute("INSERT INTO automated_order_sagas VALUES (?, ?, ?, ?, ?)",
                   [command_id, f"og-{command_id}", state, json.dumps(payload), NOW], fetch="none")

    saga, *_ = _build_saga(tmp_path)
    assert db.execute("SELECT account_id, conid FROM automated_order_sagas WHERE command_id = 'old-1'",
                      fetch="one") == (ACCOUNT, CONID)
    assert saga.unhandled_failures(ACCOUNT) == []
    state = saga.on_broker_event(_event("og-old-1", leg="stop", status="Submitted", order_id=2))
    assert state.state == "PROTECTED"
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1",
                  cancels=_cancels("og-old-1", "og-old-1:stop"), generation=7, now=NOW)
    owned = saga.resume("old-1")
    assert (owned.state, owned.close_root_id, owned.expected_cancel_ids) == ("CLOSE_OWNED", "close-1",
                                                                             ("og-old-1:stop",))


def test_an_old_leg_event_after_a_release_and_a_restart_changes_nothing(tmp_path):
    """#25 item 2: after the release the original bracket's legs are retired, also for a new process."""
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-1", cancels=_cancels(og, "og:stop"),
                  generation=7, now=NOW)
    saga.release_after_partial(close_root_id="p-1", remaining_quantity=6.0,
                               stop_group="p-1-reprotect-stop-265598-1", stop_status="Submitted",
                               target_group=None, target_status=None, now=NOW)
    before = saga.resume(intent.command_id)
    restarted, *_rest = _build_saga(tmp_path, breaker=breaker)
    after = restarted.on_broker_event(_owned_event(og, leg="stop", status="Inactive", entity="og:stop-late"))
    assert (after.state, after.protection_generation, after.current_groups) == (
        "PROTECTED", before.protection_generation, before.current_groups)
    assert breaker.signals == []
    assert "og:stop-late:Inactive:0.0" in restarted.resume(intent.command_id).seen_event_ids
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py -q --timeout=30`
Expected: 27 failed, 42 passed — `ImportError: cannot import name 'PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION'`, `TypeError: BrokerOrderEvent.__init__() got an unexpected keyword argument 'order_entity_id'`, `AttributeError: ... 'handover'`. (Checked while writing: with the revision check removed from `save_in_tx`, the three race tests and `test_a_stale_save_is_refused_with_a_revision_conflict` fail; they test the check, not the sequence.)

- [ ] **Step 3: Implement**

Constants: add `PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 37` and `_SAVE_ATTEMPTS = 5` under the migration-30 constants and `"CLOSE_OWNED"` to `SAGA_STATES`. Replace the migration function:

```python
def apply_protective_order_saga_migration(migrator: SchemaMigrator) -> bool:
    """Journal migrations 30 (saga rows) and 37 (close ownership, protection groups).

    Migration 37 backfills ``account_id`` / ``conid`` from the payload. Sagas
    that were already SAFETY_FAILED before the upgrade get
    ``flatten_requested = FALSE``: the worker starts a flatten only for a
    failure seen by this version (R29). Returns whether migration 30 was
    newly applied, as before.
    """
    applied = migrator.apply(
        PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION,
        PROTECTIVE_ORDER_SAGA_MIGRATION_NAME,
        (
            """CREATE TABLE IF NOT EXISTS automated_order_sagas (
                command_id VARCHAR PRIMARY KEY,
                order_group_id VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                payload VARCHAR NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_automated_order_sagas_group
                ON automated_order_sagas(order_group_id)""",
            """CREATE TABLE IF NOT EXISTS automated_order_saga_events (
                event_id VARCHAR PRIMARY KEY,
                command_id VARCHAR NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )""",
        ),
    )
    migrator.apply(PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION, "sp1_saga_close_ownership", (
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS account_id VARCHAR",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS conid INTEGER",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS close_root_id VARCHAR",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS flatten_requested BOOLEAN DEFAULT FALSE",
        """UPDATE automated_order_sagas
           SET account_id = json_extract_string(payload, '$.account_id'),
               conid = CAST(json_extract(payload, '$.conid') AS INTEGER),
               flatten_requested = FALSE
           WHERE account_id IS NULL""",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_groups (
            order_group_id VARCHAR PRIMARY KEY,
            command_id VARCHAR NOT NULL,
            protection_generation INTEGER NOT NULL
        )""",
    ))
    return applied
```

Replace `BrokerOrderEvent` and add the conflict error after it:

```python
@dataclass(frozen=True)
class BrokerOrderEvent:
    order_group_id: str
    leg: str
    status: str
    filled_quantity: float
    total_quantity: float
    order_id: int
    event_id: str
    source_timestamp: dt.datetime
    order_entity_id: Optional[str] = None


class SagaRevisionConflict(RuntimeError):
    """Another writer saved this saga since it was read (R28). Re-read and apply again."""
```

In `SagaState` add after `plan_json`:

```python
    close_root_id: Optional[str] = None
    expected_cancel_ids: tuple[str, ...] = ()
    handover_generation: Optional[int] = None
    protection_generation: int = 0
    active_groups: tuple[str, ...] = ()
    pending_groups: tuple[str, ...] = ()      # re-protect legs of the owning close, not yet released
    pending_protection_lost: bool = False      # a pending leg was rejected or cancelled unasked
    flatten_requested: bool = False            # SAFETY_FAILED seen by this version: the worker flattens

    @property
    def current_groups(self) -> tuple[str, ...]:
        """Order groups of the protection that is live now (older groups are retired)."""
        return self.active_groups or (self.order_group_id,)
```

and the same eight fields to `to_payload` (`"close_root_id"`, `"expected_cancel_ids": list(...)`, `"handover_generation"`, `"protection_generation"`, `"active_groups": list(...)`, `"pending_groups": list(...)`, `"pending_protection_lost"`, `"flatten_requested"`) and to `from_payload`:

```python
            close_root_id=payload.get("close_root_id"),
            expected_cancel_ids=tuple(payload.get("expected_cancel_ids") or ()),
            handover_generation=payload.get("handover_generation"),
            protection_generation=int(payload.get("protection_generation", 0)),
            active_groups=tuple(payload.get("active_groups") or ()),
            pending_groups=tuple(payload.get("pending_groups") or ()),
            pending_protection_lost=bool(payload.get("pending_protection_lost", False)),
            flatten_requested=bool(payload.get("flatten_requested", False)),
```

In `ProtectiveOrderSagaStore` replace `load_by_group` and `save_in_tx`, and add the loaders:

```python
    def load_by_group(self, order_group_id: str) -> Optional[tuple[SagaState, int]]:
        """The saga that owns ``order_group_id`` and the protection generation of that group.

        Re-protect groups are in ``automated_order_saga_groups`` (a pending
        group has the generation after the current one); the entry bracket
        group is the saga row itself (generation 0).
        """
        row = self._db.execute(
            "SELECT s.payload, g.protection_generation FROM automated_order_saga_groups g "
            "JOIN automated_order_sagas s ON s.command_id = g.command_id WHERE g.order_group_id = ?",
            [order_group_id], fetch="one",
        )
        if row is None:
            row = self._db.execute(
                "SELECT payload, 0 FROM automated_order_sagas WHERE order_group_id = ?",
                [order_group_id], fetch="one",
            )
        if row is None:
            return None
        return SagaState.from_payload(json.loads(row[0])), int(row[1])

    def _load_many(self, where: str, params: list) -> list[SagaState]:
        rows = self._db.execute(
            f"SELECT payload FROM automated_order_sagas WHERE {where} ORDER BY command_id",
            params, fetch="all",
        )
        return [SagaState.from_payload(json.loads(r[0])) for r in rows]

    def load_live(self, account_id: str, conid: Optional[int] = None) -> list[SagaState]:
        """Every saga that is not terminal: open ones and ones a close owns."""
        where = "account_id = ? AND state NOT IN ('CLOSED', 'SAFETY_FAILED')"
        params: list = [account_id]
        if conid is not None:
            where += " AND conid = ?"
            params.append(int(conid))
        return self._load_many(where, params)

    def load_by_close_root(self, close_root_id: str) -> list[SagaState]:
        return self._load_many("state = 'CLOSE_OWNED' AND close_root_id = ?", [close_root_id])

    def load_flatten_requested(self, account_id: str) -> list[SagaState]:
        return self._load_many("account_id = ? AND state = 'SAFETY_FAILED' AND flatten_requested", [account_id])

    def save_in_tx(self, conn, state: SagaState, now: dt.datetime) -> None:
        """Insert a new saga, or update one with a revision check (R28).

        An update must be built from the stored revision (``state.revision``
        is that plus one); otherwise another writer won and this one raises
        ``SagaRevisionConflict``. ``order_group_id`` never changes, so the
        update never touches the indexed column.
        """
        payload = json.dumps(state.to_payload(), sort_keys=True, default=str)
        stored = conn.execute(
            "SELECT CAST(json_extract(payload, '$.revision') AS INTEGER) FROM automated_order_sagas "
            "WHERE command_id = ?", [state.command_id]).fetchone()
        if stored is None:
            conn.execute(
                "INSERT INTO automated_order_sagas (command_id, order_group_id, state, payload, updated_at, "
                "account_id, conid, close_root_id, flatten_requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [state.command_id, state.order_group_id, state.state, payload, now,
                 state.account_id, int(state.conid), state.close_root_id, state.flatten_requested],
            )
        elif int(stored[0]) != state.revision - 1:
            raise SagaRevisionConflict(
                f"saga {state.command_id} is at revision {stored[0]}; this write was built on {state.revision - 1}")
        else:
            conn.execute(
                "UPDATE automated_order_sagas SET state = ?, payload = ?, updated_at = ?, account_id = ?, "
                "conid = ?, close_root_id = ?, flatten_requested = ? WHERE command_id = ?",
                [state.state, payload, now, state.account_id, int(state.conid), state.close_root_id,
                 state.flatten_requested, state.command_id],
            )
        groups = [(g, state.protection_generation) for g in state.active_groups] + \
                 [(g, state.protection_generation + 1) for g in state.pending_groups]
        for group, generation in groups:
            conn.execute(
                "INSERT INTO automated_order_saga_groups VALUES (?, ?, ?) ON CONFLICT (order_group_id) "
                "DO UPDATE SET command_id = excluded.command_id, protection_generation = excluded.protection_generation",
                [group, state.command_id, generation],
            )
```

Replace `on_broker_event` and add everything up to `# -- event application`:

```python
    def on_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        """Apply one broker event. A lost race with another writer re-reads and applies again (R28)."""
        return self._retrying(lambda: self._apply_broker_event(event))

    def _retrying(self, attempt: Callable[[], Any]) -> Any:
        """Read, change and save under the revision check; on a conflict start from a fresh read."""
        for _ in range(_SAVE_ATTEMPTS - 1):
            try:
                return attempt()
            except SagaRevisionConflict:
                continue
        return attempt()

    def _apply_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        found = self._store.load_by_group(event.order_group_id)
        if found is None:
            raise KeyError(f"no saga for order_group_id={event.order_group_id!r}")
        state, group_generation = found
        if event.event_id in state.seen_event_ids:
            return state
        if state.state in _TERMINAL_SAGA:
            return state

        now = self._now_utc()
        if group_generation > state.protection_generation:
            # A re-protect leg of the close that owns this saga, before its release.
            return self._on_pending_event(state, event, now)
        if group_generation < state.protection_generation:
            # A retired leg: record the event, never let it change today's protection.
            return self._record_only(state, event, now)
        if state.state == "CLOSE_OWNED":
            return self._on_owned_event(state, event, now)
        updated = self._apply_event(state, event)
        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            updated = replace(updated, flatten_requested=True)
        updated = replace(
            updated,
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(updated, now, from_state=state.state, event_id=event.event_id)

        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            self._trip_and_liquidate(updated, now)
        return updated

    def _record_only(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        recorded = replace(state, seen_event_ids=state.seen_event_ids + (event.event_id,),
                           revision=state.revision + 1)
        self._persist(recorded, now, from_state=state.state, event_id=event.event_id)
        return recorded

    def _lost(self, state: SagaState, event: BrokerOrderEvent) -> bool:
        """R14: only a cancel of a ref the close asked to cancel is expected."""
        expected = event.order_entity_id is not None and event.order_entity_id in state.expected_cancel_ids
        return event.status in _REJECTED_STATUSES or (event.status in _CANCELLED_STATUSES and not expected)

    def _on_owned_event(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        if self._lost(state, event):
            failed = replace(
                state, state="SAFETY_FAILED", error_code="PROTECTION_LOST_DURING_CLOSE", flatten_requested=True,
                seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1,
            )
            self._persist(failed, now, from_state=state.state, event_id=event.event_id)
            self._trip_and_liquidate(failed, now)
            return failed
        owned = replace(
            self._owned_bookkeeping(state, event),
            seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1,
        )
        self._persist(owned, now, from_state=state.state, event_id=event.event_id)
        return owned

    def _on_pending_event(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        """A replacement leg before release. The close judges its own legs (and escalates); the saga
        only remembers a loss, so the release cannot report protection that is gone (R2-4)."""
        lost = state.state == "CLOSE_OWNED" and self._lost(state, event)
        recorded = replace(state, pending_protection_lost=state.pending_protection_lost or lost,
                           seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1)
        self._persist(recorded, now, from_state=state.state, event_id=event.event_id)
        return recorded

    @staticmethod
    def _owned_bookkeeping(state: SagaState, event: BrokerOrderEvent) -> SagaState:
        working = event.status in _WORKING_STATUSES
        filled = event.status in _FILLED_STATUSES
        if event.leg == "entry":
            return replace(state, entry_working=working)
        if event.leg == "stop":
            return replace(state, stop_working=working, stop_filled=state.stop_filled or filled)
        if event.leg == "take_profit":
            return replace(state, target_working=working, target_filled=state.target_filled or filled)
        return state

    # -- close ownership (ProtectionOwnershipPort) --------------------------

    @staticmethod
    def _prices_from_plan(state: SagaState):
        from trader.trading.liquidation_service import HandoverInfo
        legs = (state.plan_json or {}).get("legs", [])
        stop = next((leg for leg in legs if leg.get("role") == "stop"), None)
        target = next((leg for leg in legs if leg.get("role") == "take_profit"), None)
        return HandoverInfo(
            stop_price=None if stop is None or stop.get("stop_price") is None else float(stop["stop_price"]),
            target_price=None if target is None or target.get("limit_price") is None else float(target["limit_price"]),
        )

    @staticmethod
    def _own(states: list[SagaState], close_root_id: str, cancels, generation: int) -> list[SagaState]:
        """CLOSE_OWNED with the expected cancels merged in; a pending leg's cancel is expected too."""
        owned = []
        for state in states:
            groups = set(state.current_groups) | set(state.pending_groups)
            mine = {c.order_entity_id for c in cancels if c.order_group_id in groups}
            expected = tuple(sorted(set(state.expected_cancel_ids) | mine))
            if (state.state, state.close_root_id, state.expected_cancel_ids) == ("CLOSE_OWNED", close_root_id, expected):
                continue  # idempotent: already handed over with these refs
            owned.append(replace(state, state="CLOSE_OWNED", close_root_id=close_root_id,
                                 expected_cancel_ids=expected, handover_generation=generation,
                                 revision=state.revision + 1))
        return owned

    def handover(self, *, account_id: str, conid: int, close_root_id: str, cancels, generation: int,
                 now: dt.datetime):
        from trader.trading.liquidation_service import HandoverInfo

        def attempt():
            states = self._store.load_live(account_id, conid)
            self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)],
                              _as_utc(now))
            return self._prices_from_plan(states[0]) if states else HandoverInfo(None, None)
        return self._retrying(attempt)

    def handover_account(self, *, account_id: str, close_root_id: str, cancels, generation: int,
                         now: dt.datetime) -> None:
        def attempt():
            states = self._store.load_live(account_id)
            self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)],
                              _as_utc(now))
        self._retrying(attempt)

    def expect_reprotect(self, *, close_root_id: str, groups: tuple[str, ...], now: dt.datetime) -> None:
        """Bind the replacement legs to the saga before they are sent, as pending groups."""
        def attempt():
            changed = [replace(s, pending_groups=tuple(groups), revision=s.revision + 1)
                       for s in self._store.load_by_close_root(close_root_id)[:1]
                       if s.pending_groups != tuple(groups)]
            self._persist_all([(s, None) for s in changed], _as_utc(now))
        self._retrying(attempt)

    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float, stop_group: str,
                              stop_status: str, target_group: Optional[str], target_status: Optional[str],
                              now: dt.datetime) -> None:
        """Back to protection of the remainder, judged from the legs' broker status at release.

        The new legs become the only live protection (a new protection
        generation). A leg that is not working, or a pending leg lost while
        the close owned the saga, is today's incident path.
        """
        def attempt():
            states = self._store.load_by_close_root(close_root_id)
            if not states:
                return None
            keeper, merged = states[0], states[1:]
            remaining = _dec(remaining_quantity)
            groups = (stop_group,) + ((target_group,) if target_group else ())
            base = replace(
                keeper, state="PROTECTED", close_root_id=None, expected_cancel_ids=(), handover_generation=None,
                protection_generation=keeper.protection_generation + 1, active_groups=groups, pending_groups=(),
                pending_protection_lost=False, requested_quantity=remaining, filled_quantity=remaining,
                protection_quantity=remaining, protection_working=False, stop_working=False,
                target_working=False, stop_filled=False, target_filled=False, stop_rejected=False,
                target_rejected=False, entry_working=False, entry_cancelled=False, error_code=None,
                revision=keeper.revision + 1,
            )
            released = self._apply_event(base, BrokerOrderEvent(
                stop_group, "stop", stop_status, 0.0, float(remaining), 0, f"release:{close_root_id}:stop", now))
            if target_group:
                released = self._apply_event(released, BrokerOrderEvent(
                    target_group, "take_profit", target_status or "Unknown", 0.0, float(remaining), 0,
                    f"release:{close_root_id}:target", now))
            if keeper.pending_protection_lost or released.state not in ("PROTECTED", "EXITING", "CLOSED"):
                released = replace(released, state="SAFETY_FAILED", flatten_requested=True,
                                   error_code=released.error_code or "PROTECTION_LOST_DURING_CLOSE")
            closed = [replace(s, state="CLOSED", close_root_id=None, error_code="PROTECTION_MERGED",
                              revision=s.revision + 1) for s in merged]
            self._persist_all([(released, "CLOSE_OWNED")] + [(s, "CLOSE_OWNED") for s in closed], _as_utc(now))
            return released
        released = self._retrying(attempt)
        if released is not None and released.state == "SAFETY_FAILED":
            self._trip_and_liquidate(released, _as_utc(now))

    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None:
        def attempt():
            closed = [replace(s, state="CLOSED", error_code=None, close_root_id=None, stop_working=False,
                              target_working=False, revision=s.revision + 1)
                      for s in self._store.load_by_close_root(close_root_id)]
            self._persist_all([(s, "CLOSE_OWNED") for s in closed], _as_utc(now))
        self._retrying(attempt)

    def unhandled_failures(self, account_id: str) -> list[str]:
        """SAFETY_FAILED sagas this version asked to flatten; the worker makes sure each one has a root.

        A saga that was already SAFETY_FAILED before the upgrade is not here (R29):
        no flatten starts on the first deploy without a fresh trigger.
        """
        return [s.command_id for s in self._store.load_flatten_requested(account_id)]
```

Replace `_persist` (it keeps its signature) and add `_persist_all` and `_mutation` (`_mutation` is the old body of `_persist` up to the event key; the payload also carries `close_root_id`):

```python
    def _persist(
        self,
        state: SagaState,
        now: dt.datetime,
        *,
        from_state: Optional[str],
        event_id: Optional[str] = None,
    ) -> None:
        mutation, write, event_key = self._mutation(state, now, from_state=from_state, event_id=event_id)
        self._journal.mutate(self._journal.connect(), mutation, write, event_id=event_key)

    def _persist_all(self, items: list[tuple[SagaState, Optional[str]]], now: dt.datetime) -> None:
        """Several saga rows in one journal transaction (all or none, revision-checked)."""
        if not items:
            return
        prepared = [self._mutation(state, now, from_state=from_state, event_id=None) for state, from_state in items]
        self._journal.mutate_batch_work(
            self._journal.connect(),
            lambda _conn, append: [append(mutation, write, key) for mutation, write, key in prepared],
        )

    def _mutation(self, state: SagaState, now: dt.datetime, *, from_state: Optional[str],
                  event_id: Optional[str]):
        if state.state not in SAGA_STATES:
            raise ValueError(f"invalid saga state {state.state!r}")

        mutation = DomainMutation(
            event_type="automated_order_saga.updated",
            entity_type="automated_order_saga",
            entity_id=command_entity_id(state.command_id),
            operation="upsert",
            account_id=state.account_id,
            source="trader_service",
            source_timestamp=now,
            correlation_id=state.command_id,
            payload={
                "state": state.state,
                "from_state": from_state,
                "order_group_id": state.order_group_id,
                "order_ref": state.order_ref,
                "error_code": state.error_code,
                "filled_quantity": str(state.filled_quantity),
                "protection_working": state.protection_working,
                "close_root_id": state.close_root_id,
                "revision": state.revision,
            },
        )

        def write(conn, _revision: int) -> None:
            self._store.save_in_tx(conn, state, now)
            if event_id is not None:
                self._store.record_event_in_tx(conn, event_id, state.command_id, now)

        event_key = event_id or f"saga:{state.command_id}:{state.state}:{state.revision}"
        return mutation, write, event_key
```

In `start`, build the writes after `submit_bracket` from a fresh read (ruling 39, N1): the ingest thread may save an event of the bracket while the call waits, and the revision check would then refuse the old copy with the bracket live.

```diff
diff --git a/trader/automation/protective_order_saga.py b/trader/automation/protective_order_saga.py
index 04092890..65bb3a9d 100644
--- a/trader/automation/protective_order_saga.py
+++ b/trader/automation/protective_order_saga.py
@@ -700,29 +700,34 @@ class ProtectiveOrderSaga:
                 plan=plan, intent=intent, account_id=self._account_id,
             )
         except BrokerRejectedError:
-            closed = replace(
-                submitting, state="CLOSED", error_code="BROKER_REJECTED",
-                revision=submitting.revision + 1,
-            )
-            self._persist(closed, now, from_state="SUBMITTING")
-            return closed
+            return self._after_dispatch(intent.command_id, now, lambda s: replace(
+                s, state="CLOSED", error_code="BROKER_REJECTED"))
         except Exception:
-            unknown = replace(
-                submitting, state="OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS",
-                revision=submitting.revision + 1,
-            )
-            self._persist(unknown, now, from_state="SUBMITTING")
-            return unknown
+            return self._after_dispatch(intent.command_id, now, lambda s: replace(
+                s, state="OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS"))
 
         order_ids = tuple(int(x) for x in (getattr(submitted, "order_ids", None) or ()))
         # Submit returns correlation ids only — broker events advance working.
-        recorded = replace(
-            submitting,
-            submitted_order_ids=order_ids,
-            revision=submitting.revision + 1,
-        )
-        self._persist(recorded, now, from_state="SUBMITTING")
-        return recorded
+        return self._after_dispatch(
+            intent.command_id, now, lambda s: replace(s, submitted_order_ids=order_ids), always=True)
+
+    def _after_dispatch(self, command_id: str, now: dt.datetime,
+                        change: Callable[[SagaState], SagaState], *, always: bool = False) -> SagaState:
+        """The write after ``submit_bracket``, from a fresh read under the revision check (N1).
+
+        The ingest thread may have saved an event of this bracket while the
+        call waited. A state change is made only while the saga is still
+        SUBMITTING (an event already moved it on, so the broker has the
+        order); ``always`` changes apply on top of whatever the ingest wrote.
+        """
+        def attempt() -> SagaState:
+            current = self._store.load(command_id)
+            if current.state != "SUBMITTING" and not always:
+                return current
+            updated = replace(change(current), revision=current.revision + 1)
+            self._persist(updated, now, from_state=current.state)
+            return updated
+        return self._retrying(attempt)
 
     def resume(self, command_id: str) -> Optional[SagaState]:
         return self._store.load(command_id)
```

In `trader/trading/broker_ingest.py` `_notify_protective_saga`, add `order_entity_id=order.order_entity_id,` as the last argument of `BrokerOrderEvent(...)`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py tests/test_broker_ingest.py tests/test_liquidation_service.py -q --timeout=30`
Expected: all PASS (51 in the saga file), including `test_migration_30_creates_automated_order_sagas_table`. The N1 test fails without `_after_dispatch` (`SagaRevisionConflict` from `start`); the upgrade and retired-leg tests pin behaviour the other Task 9 changes give.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/protective_order_saga.py trader/trading/broker_ingest.py tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py
git commit -m "feat: protective saga hands exact protection over to a close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 10: Exit legs on the same reduce-only path — stop and target of the re-protect pair

The re-protect stop and target go through the same reduce-only method as every exit (R11). This task adds `order_type` (`MKT`, `STP`, `LMT`), `price` and `oca_group` to master's `place_reduce_only_order` (ruling 35): `STP`/`LMT` need a positive price, and an OCA group sets `ocaType=2` (a fill of one leg reduces the other to what is left). The R35 bound from Task 14 already leaves out the working sibling of the same OCA group, so the target is not refused next to its own stop. It adds `place_exit_leg` on the dispatch and on `_LiquidationDispatch`. The service sends the legs one by one (Task 6), so there is no `place_exit_oca` that places both: each leg follows IB's verdict for its own order id through master's order tracker (R13, ruling 34). A real IB paper session still has to prove `ocaType=2` (plan 6).

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`Trader.place_reduce_only_order`, `Trader._reduce_only_refusal`, new `Trader._reduce_only_order`, new `TradingRuntimeOrderDispatch.place_exit_leg`)
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch.place_exit_leg`)
- Test: `tests/test_reduce_only_order_path.py`

**Interfaces:**

```python
async def Trader.place_reduce_only_order(self, contract, side, quantity, *, broker_quantity, order_ref,
                                         order_type: str = "MKT", price: Optional[float] = None,
                                         oca_group: Optional[str] = None) -> SuccessFail
    # order_type in ("MKT", "STP", "LMT"); STP/LMT need a positive price; oca_group -> ocaType 2
def TradingRuntimeOrderDispatch.place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
                                               oca_group: str, order_ref: str)    # leg "stop" -> STP, "target" -> LMT
def _LiquidationDispatch.place_exit_leg(self, position, *, leg, quantity, price, oca_group, child_id) -> None
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reduce_only_order_path.py`:

```python
# -- Task 10: exit legs by order identity -----------------------------------------------

def test_exit_legs_carry_price_oca_group_and_reduce_oca_type():
    trader = _trader(held=6.0)
    for order_type, price, ref in (("STP", 95.0, "mmr:p-1-reprotect-stop-265598-1"),
                                   ("LMT", 120.0, "mmr:p-1-reprotect-target-265598-1")):
        result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref=ref,
                                                     order_type=order_type, price=price,
                                                     oca_group="p-1-reprotect-265598-1"))
        assert result.is_success()
    stop, target = trader.executioner.placed
    assert (stop.orderType, stop.auxPrice, target.orderType, target.lmtPrice) == ("STP", 95.0, "LMT", 120.0)
    for order in (stop, target):
        assert (order.ocaGroup, order.ocaType, order.transmit, order.parentId, order.tif) == (
            "p-1-reprotect-265598-1", 2, True, 0, "DAY")


def test_an_exit_leg_rejected_by_ib_is_not_a_refusal():
    """R13: IB's verdict for this order id; a rejected leg was sent, so it is not ``reduce-only refused``."""
    trader = _trader(held=6.0, verdict="rejected")
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:t",
                                                 order_type="LMT", price=120.0, oca_group="g"))
    assert result.exception is None and result.error.startswith("Order rejected by IB")
    assert len(trader.executioner.placed) == 1


@pytest.mark.parametrize("order_type,price", [("STP", None), ("LMT", 0.0), ("TRAIL", 1.0)])
def test_exit_leg_without_a_valid_type_or_price_is_refused(order_type, price):
    trader = _trader(held=6.0)
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:x",
                                                 order_type=order_type, price=price, oca_group="g"))
    assert result.error.startswith("reduce-only refused") and trader.executioner.placed == []


def test_dispatch_place_exit_leg_derives_side_and_type(loop_thread):
    trader = _trader(held=-6.0)
    dispatch = _dispatch(trader, loop_thread)
    dispatch.place_exit_leg(_position(-6.0), leg="stop", quantity=6.0, price=105.0, oca_group="g", order_ref="mmr:s")
    order = trader.executioner.placed[-1]
    assert (order.action, order.orderType, order.auxPrice, order.ocaGroup) == ("BUY", "STP", 105.0, "g")
    with pytest.raises(DispatchRefused):
        dispatch.place_exit_leg(_position(-6.0), leg="trail", quantity=6.0, price=1.0, oca_group="g", order_ref="mmr:x")


@pytest.mark.parametrize("position,quantity,price", [
    (_MALFORMED_POSITIONS["no symbol"], 6.0, 95.0),
    (_MALFORMED_POSITIONS["fractional conid"], 6.0, 95.0),
    (_MALFORMED_POSITIONS["None quantity"], 6.0, 95.0),
    (_malformed(), None, 95.0),
    (_malformed(), 6.0, None),
    (_malformed(), 6.0, float("nan")),
], ids=["no symbol", "fractional conid", "None position quantity", "None quantity", "None price", "NaN price"])
@pytest.mark.parametrize("leg", ["stop", "target"])
def test_a_malformed_exit_leg_is_refused_before_anything_is_scheduled(loop_thread, leg, position, quantity, price):
    """#38, ruling 52: a leg built from bad input is NOT_SENT; the close never waits on it as UNKNOWN."""
    trader = _SpyTrader(loop_thread.loop)
    with pytest.raises(DispatchRefused) as ex:
        TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0).place_exit_leg(
            position, leg=leg, quantity=quantity, price=price, oca_group="g", order_ref="mmr:x")
    assert ex.value.code == "REDUCE_ONLY_REFUSED" and trader.scheduled == []


def test_liquidation_dispatch_sends_exit_legs_with_the_child_id_as_order_ref():
    from trader.trading.command_stack import _LiquidationDispatch

    calls = []
    inner = SimpleNamespace(place_exit_leg=lambda p, **kw: calls.append(kw))
    _LiquidationDispatch(inner, SimpleNamespace()).place_exit_leg(
        SimpleNamespace(conid=CONID, quantity=6.0), leg="stop", quantity=6.0, price=95.0, oca_group="g",
        child_id="p-1-reprotect-stop-265598-1")
    assert calls == [{"leg": "stop", "quantity": 6.0, "price": 95.0, "oca_group": "g",
                      "order_ref": "mmr:p-1-reprotect-stop-265598-1"}]


def test_target_leg_is_allowed_next_to_its_own_oca_stop_but_not_next_to_another_order():
    """D14: the working stop of the same OCA pair does not count against the target's bound."""
    trader = _trader(held=6.0)
    trader.client.ib.open_trades = [_working("SELL", 6.0, oca_group="p-1-reprotect-265598-1")]
    target = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:t",
                                                 order_type="LMT", price=120.0, oca_group="p-1-reprotect-265598-1"))
    assert target.is_success()
    other = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, broker_quantity=6.0, order_ref="mmr:u",
                                                order_type="LMT", price=120.0, oca_group="another-group"))
    assert other.error.startswith("reduce-only refused")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py -q --timeout=30`
Expected: 8 failed, 12 passed (`TypeError: ... unexpected keyword argument 'order_type'`, no `place_exit_leg`)

- [ ] **Step 3: Implement**

```diff
diff --git a/trader/trading/command_stack.py b/trader/trading/command_stack.py
index 5c988280..ef799780 100644
--- a/trader/trading/command_stack.py
+++ b/trader/trading/command_stack.py
@@ -221,6 +221,11 @@ class _LiquidationDispatch:
     def reduce_partial(self, position, side: str, quantity: float, child_id: str) -> None:
         self._dispatch.reduce_partial(position, side, quantity, encode_order_ref(child_id))
 
+    def place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
+                       oca_group: str, child_id: str) -> None:
+        self._dispatch.place_exit_leg(position, leg=leg, quantity=quantity, price=price,
+                                      oca_group=oca_group, order_ref=encode_order_ref(child_id))
+
     def find_orders(self, account_id: str, child_id: str) -> list:
         return self._dispatch.find_by_order_ref(account_id, encode_order_ref(child_id))
 
diff --git a/trader/trading/trading_runtime.py b/trader/trading/trading_runtime.py
index 1b2e1738..7ae619bf 100644
--- a/trader/trading/trading_runtime.py
+++ b/trader/trading/trading_runtime.py
@@ -1702,8 +1702,16 @@ class Trader():
         *,
         broker_quantity: float,
         order_ref: str,
+        order_type: str = 'MKT',
+        price: Optional[float] = None,
+        oca_group: Optional[str] = None,
     ) -> SuccessFail:
-        """Send a MARKET order that can only shrink an existing position.
+        """Send an order that can only shrink an existing position.
+
+        ``order_type`` is ``MKT`` (full or partial reduce), or ``STP`` / ``LMT``
+        with a positive ``price`` for the exit legs of a re-protect pair. An
+        ``oca_group`` links the legs with ``ocaType=2``: a fill of one leg
+        reduces the other to what is left.
 
         The emergency-exit path (liquidation, session flatten). It keeps the
         boundary checks — account and mode pin, contract, reduce-only side and
@@ -1719,17 +1727,15 @@ class Trader():
         - ``fail(exception=...)``: the order may have been sent.
         """
         try:
-            refusal = self._reduce_only_refusal(contract, side, quantity, broker_quantity)
+            refusal = self._reduce_only_refusal(contract, side, quantity, broker_quantity,
+                                                order_type=order_type, price=price, oca_group=oca_group)
         except Exception as ex:
             refusal = f'pre-send check failed: {ex}'
         if refusal:
             logging.error('reduce-only order refused before send: %s', refusal)
             return SuccessFail.fail(error=f'{REDUCE_ONLY_REFUSED}: {refusal}')
 
-        order = MarketOrder(
-            action=side, totalQuantity=quantity, account=self.ib_account,
-            orderRef=order_ref, tif='DAY', outsideRth=False,
-        )
+        order = self._reduce_only_order(side, quantity, order_ref, order_type, price, oca_group)
         try:
             trade = await self._send_reduce_only(contract, order)
             return await self._confirm_reduce_only(trade)
@@ -1737,12 +1743,33 @@ class Trader():
             logging.error('reduce-only order may have been sent: %s', ex)
             return SuccessFail.fail(exception=ex)
 
+    _REDUCE_ONLY_TYPES = ('MKT', 'STP', 'LMT')
+
+    def _reduce_only_order(self, side: str, quantity: float, order_ref: str, order_type: str,
+                           price: Optional[float], oca_group: Optional[str]) -> Order:
+        common = dict(action=side, totalQuantity=quantity, account=self.ib_account,
+                      orderRef=order_ref, tif='DAY', outsideRth=False)
+        if order_type == 'STP':
+            order: Order = StopOrder(stopPrice=float(price), **common)
+        elif order_type == 'LMT':
+            order = LimitOrder(lmtPrice=float(price), **common)
+        else:
+            order = MarketOrder(**common)
+        if oca_group:
+            order.ocaGroup = oca_group
+            order.ocaType = 2  # a fill reduces the sibling to what is left
+        return order
+
     def _reduce_only_refusal(
         self, contract: Contract, side: str, quantity: float, broker_quantity: float, *,
-        oca_group: Optional[str] = None,
+        order_type: str = 'MKT', price: Optional[float] = None, oca_group: Optional[str] = None,
     ) -> Optional[str]:
         import math
 
+        if order_type not in self._REDUCE_ONLY_TYPES:
+            return f'order type {order_type!r} is not a reduce-only type'
+        if order_type != 'MKT' and (price is None or not math.isfinite(float(price)) or not float(price) > 0):
+            return f'{order_type} needs a positive price'
         account = self.ib_account
         if not account:
             return 'no ib_account is configured on the trader'
@@ -2527,6 +2554,18 @@ class TradingRuntimeOrderDispatch:
             self._refuse('a partial reduce needs a whole quantity strictly between 0 and the position')
         return self._reduce_only(position, contract, held, side, size, order_ref)
 
+    def place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
+                       oca_group: str, order_ref: str):
+        """One exit-only leg (stop or target) of a re-protect OCA pair."""
+        if leg not in ('stop', 'target'):
+            self._refuse(f'unknown exit leg {leg!r}')
+        contract, held, size = self._close_inputs(position, quantity)
+        try:
+            limit = _finite_number(price, 'price')
+        except (TypeError, ValueError) as ex:
+            self._refuse(f'malformed exit leg price: {ex}')
+        side = self._side_for(held)
+        if side is None:
+            self._refuse('no position to protect')
+        return self._reduce_only(position, contract, held, side, size, order_ref,
+                                 order_type='STP' if leg == 'stop' else 'LMT',
+                                 price=limit, oca_group=oca_group)
+
     def cancel_on_loop(self, order_entity_id: str, order_ref: str):
         """``cancel`` for the liquidation worker (R34, ruling 7).
 
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py tests/test_trading_runtime.py tests/test_order_dispatch_ports.py -q --timeout=30`
Expected: all PASS (20 in the file).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_reduce_only_order_path.py
git commit -m "feat: exit-only stop and target legs on the reduce-only path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 11: Time exits use the scoped close; the session flatten polls the root it got back

`SessionTimeExitAdapter` asks the liquidation service for a conid-scoped full close instead of calling `reduce` (Task 1's test turns green). `_issue_flatten` persists the root `start` returned — after a join that is another producer's root (R10) — and `_poll_flat` reads exactly that root with `receipt_for`, never another root's `FLAT`. A `start` that returns no root raises (no silent fallback to the session's own cause; the next tick tries again). Ruling (round 2): when the root the session follows ends `FAILED_SAFE` — its own or a joined one — the session goes `INCIDENT` at once with a `LIQUIDATION_FAILED` breaker signal, instead of polling a dead root until the flat deadline; R9 lets the operator start a new root, which inherits the unknown children.

The Task 1 regression keeps its name and final assertions, and now runs the real `LiquidationService` with a saga stand-in that writes into the same call log, so the test pins the order hand-over → cancel → reduce → close (spec 5.1).

**Files:**
- Modify: `trader/automation/session_controller.py` (`LiquidationPort`, `SessionTimeExitAdapter`, `_issue_flatten`, `_poll_flat`; drop the unused `SimpleNamespace` import; `BreakerSignal` is already imported)
- Modify: `trader/trading/command_stack.py` (the `time_exit=` argument of `SessionController(`)
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Produces: `SessionTimeExitAdapter(liquidation, *, account_id: str, now: Callable[[], dt.datetime], deadline_seconds: float = 300.0)`; `request_exit(*, command_id, conid, quantity, side)` calls `liquidation.start(account_id, command_id, now() + deadline, scope="conid", conid=conid)` — never with a quantity, so the only possible `ExitInProgress` is swallowed safely (#27). `quantity` and `side` stay in the port signature; the broker decides the size.
- `LiquidationPort`: `start(account_id, cause_command_id, deadline, **kwargs)`, `rescan()`, `receipt_for(root_id)`.

- [ ] **Step 1: Rewrite the Task 1 test and add the new tests**

In `tests/automation/test_session_controller.py`:

1. Replace `FakeLiquidation` with:

```python
class FakeLiquidation:
    """Records starts; ``joined_root`` makes the next account start join that root."""
    def __init__(self):
        self.starts: list[tuple] = []
        self.kwargs: list[dict] = []
        self.receipts: dict[str, Any] = {}
        self.rescans = 0
        self.joined_root: Optional[str] = None
        self.other_flat: Any = None

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        self.starts.append((account_id, cause_command_id, deadline))
        self.kwargs.append(kwargs)
        root = self.joined_root or cause_command_id
        self.receipts.setdefault(root, SimpleNamespace(
            account_id=account_id, cause_command_id=root, state="REQUESTED",
            deadline=deadline, generation_id=None, detail="started",
        ))
        return self.receipts[root]

    def rescan(self):
        self.rescans += 1
        return self.other_flat or next(iter(self.receipts.values()), None)

    def receipt_for(self, root_id):
        return self.receipts.get(root_id)

    def mark_flat(self, generation_id: int = 2, root_id: Optional[str] = None):
        if not self.receipts:
            return
        root = root_id or next(iter(self.receipts))
        current = self.receipts[root]
        self.receipts[root] = SimpleNamespace(
            account_id=current.account_id, cause_command_id=root, state="FLAT",
            deadline=current.deadline, generation_id=generation_id, detail="flat",
        )
```

2. Replace the Task 1 test (remove the `xfail` marker; the name and the final assertions stay) and add two helpers:

```python
class _SimProtection:
    """Saga stand-in that writes its hand-over into the broker's call log, so the order is visible."""
    def __init__(self, calls):
        self.calls = calls

    def handover(self, *, account_id, conid, close_root_id, cancels, generation, now):
        from trader.trading.liquidation_service import HandoverInfo
        self.calls.append(("handover", tuple(c.order_entity_id for c in cancels)))
        return HandoverInfo(150.0, None)

    def handover_account(self, **_kwargs):
        raise AssertionError("a time exit is a conid-scoped close")

    def expect_reprotect(self, **_kwargs):
        raise AssertionError("a time exit never re-protects")

    def release_after_partial(self, **_kwargs):
        raise AssertionError("a time exit never re-protects")

    def close_after_full(self, *, close_root_id, now):
        self.calls.append(("close_after_full", close_root_id))


def _real_liquidation(tmp_path, broker, protection=None):
    from trader.trading.exit_owner import ExitOwnerRegistry
    from trader.trading.liquidation_service import (
        LiquidationRunStore, LiquidationService, apply_liquidation_migration,
    )
    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    return LiquidationService(broker, broker, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                              now=lambda: _utc(15, 0), protection=protection)


def test_time_exit_leaves_no_live_stop_after_the_position_is_closed(tmp_path):
    """Spec 5.1: a time exit hands protection over, cancels the stop, then closes from broker truth."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    broker = _SimBroker()
    service = _real_liquidation(tmp_path, broker, protection=_SimProtection(broker.calls))
    adapter = SessionTimeExitAdapter(service, account_id=ACCOUNT, now=lambda: _utc(15, 0))
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    for _ in range(4):
        service.rescan()
    assert broker.calls == [("handover", ("og-1:stop",)), ("cancel", "og-1:stop"), ("reduce", "SELL", 10.0),
                            ("close_after_full", "exit-1")]
    assert broker.quantity == 0.0
    assert broker.stop_working is False, "a live stop on a closed position can open a short"
    assert service.receipt_for("exit-1").state == "CLOSED"
```

3. Append:

```python
def test_time_exit_adapter_starts_a_full_conid_close_without_a_quantity():
    """#27: a quantity would make a join refusable; the adapter never passes one."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    liquidation = FakeLiquidation()
    SessionTimeExitAdapter(liquidation, account_id=ACCOUNT, now=lambda: _utc(15, 0), deadline_seconds=120.0) \
        .request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    assert liquidation.starts == [(ACCOUNT, "exit-1", _utc(15, 2))]
    assert liquidation.kwargs == [{"scope": "conid", "conid": CONID}]


def test_time_exit_adapter_tolerates_exit_in_progress():
    from trader.automation.session_controller import SessionTimeExitAdapter
    from trader.trading.exit_owner import ExitInProgress

    class _Refusing:
        def start(self, *a, **k):
            raise ExitInProgress("other-root")

    SessionTimeExitAdapter(_Refusing(), account_id=ACCOUNT, now=lambda: _utc(15, 0)).request_exit(
        command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")


def test_poll_flat_ignores_another_roots_flat_receipt(tmp_path):
    controller, broker, _c, liquidation, _br, _t, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    liquidation.other_flat = SimpleNamespace(cause_command_id="someone-else", state="FLAT", generation_id=9)
    state = controller.run_due(_utc(15, 47))
    assert state.state in ("FLATTENING", "VERIFYING_FLAT")
    liquidation.mark_flat(generation_id=3, root_id=state.flatten_command_id)
    state = controller.run_due(_utc(15, 48))
    assert (state.state, state.flat_generation) == ("FLAT", 3)


def test_session_flatten_joining_an_existing_account_flatten_polls_the_returned_root(tmp_path):
    """R10 / #27: the session persists the root it got back, also across a restart."""
    liquidation = FakeLiquidation()
    liquidation.joined_root = "kill-1"
    controller, _b, _c, _l, _br, _t, _j, db, clock = _build_controller(tmp_path, liquidation=liquidation)
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.flatten_command_id == "kill-1"
    from trader.automation.session_controller import SessionController
    restarted = SessionController(
        journal=DomainJournal(db), db=db, calendar=XNYSCalendarPolicy(), broker=FakeBroker(),
        cancel=FakeCancel(), liquidation=liquidation, breaker=FakeBreaker(),
        time_exit=FakeTimeExitDispatch(), account_id=ACCOUNT, now=lambda: _utc(15, 47))
    restarted.recover(_utc(15, 47))
    liquidation.mark_flat(generation_id=4, root_id="kill-1")
    state = restarted.run_due(_utc(15, 48))
    assert (state.state, state.flatten_command_id, state.flat_generation) == ("FLAT", "kill-1", 4)


def test_time_exit_adapter_lets_a_refusal_reach_the_caller():
    """Fail loudly: only ExitInProgress (never possible without a quantity) is swallowed."""
    from trader.automation.session_controller import SessionTimeExitAdapter
    from trader.trading.liquidation_service import LiquidationRefused

    class _Refusing:
        def start(self, *a, **k):
            raise LiquidationRefused("NO_POSITION")

    with pytest.raises(LiquidationRefused):
        SessionTimeExitAdapter(_Refusing(), account_id=ACCOUNT, now=lambda: _utc(15, 0)).request_exit(
            command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")


def test_session_flatten_without_a_root_to_poll_fails_loudly(tmp_path):
    """R10: no silent fallback to the session's own cause; the flatten is retried on the next tick."""
    liquidation = FakeLiquidation()
    liquidation.start = lambda *a, **k: None
    controller, _b, _c, _l, _br, _t, _j, _db, clock = _build_controller(tmp_path, liquidation=liquidation)
    clock[0] = _utc(15, 46)
    with pytest.raises(RuntimeError, match="no root to poll"):
        controller.recover(clock[0])


def test_session_flatten_whose_root_ends_failed_safe_is_an_incident_at_once(tmp_path):
    """Ruling: a FAILED_SAFE root (joined or own) is not polled until the flat deadline."""
    controller, _b, _c, liquidation, breaker, _t, _j, _db, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 46)
    state = controller.recover(clock[0])
    root = state.flatten_command_id
    liquidation.receipts[root] = SimpleNamespace(cause_command_id=root, state="FAILED_SAFE", generation_id=5,
                                                 detail="deadline elapsed")
    state = controller.run_due(_utc(15, 47))
    assert state.state == "INCIDENT" and "FAILED_SAFE" in state.incident
    assert [s.kind for s in breaker.signals] == ["LIQUIDATION_FAILED"]
```

In master's `test_missed_flat_deadline_records_incident_when_rescan_finds_no_advanceable_root` the first state may now be `VERIFYING_FLAT` (the session polls the root it got back, not `rescan()`):

```diff
-    assert state.state == "FLATTENING"
+    assert state.state in ("FLATTENING", "VERIFYING_FLAT")   # Task 11 polls the root it got back
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py -q --timeout=30`
Expected: 8 failed, 29 passed — the time-exit tests (`TypeError: SessionTimeExitAdapter.__init__() got an unexpected keyword argument 'account_id'`), `test_poll_flat_ignores_another_roots_flat_receipt` (another root's FLAT is accepted), the join test (`flatten_command_id` is the session's own cause), the no-root test (the session silently polls its own cause) and the `FAILED_SAFE` test (it keeps polling).

- [ ] **Step 3: Implement**

```python
class LiquidationPort(Protocol):
    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, **kwargs: Any) -> Any: ...
    def rescan(self) -> Any: ...
    def receipt_for(self, root_id: str) -> Any: ...


class SessionTimeExitAdapter:
    """A time exit is a conid-scoped full close.

    The close hands protection over, cancels the stop first, sizes the reduce
    from the broker and owns the position until it is closed. ``quantity``
    and ``side`` are part of the port; the broker decides the size.
    """

    def __init__(self, liquidation: Any, *, account_id: str, now: Callable[[], dt.datetime],
                 deadline_seconds: float = 300.0):
        self._liquidation = liquidation
        self._account_id = account_id
        self._now = now
        self._deadline_seconds = deadline_seconds

    def request_exit(
        self, *, command_id: str, conid: int, quantity: Decimal, side: str,
    ) -> None:
        from trader.trading.exit_owner import ExitInProgress

        deadline = _as_utc(self._now()) + dt.timedelta(seconds=self._deadline_seconds)
        try:
            # Never pass a quantity: only a partial request can be refused with ExitInProgress.
            self._liquidation.start(self._account_id, command_id, deadline, scope="conid", conid=int(conid))
        except ExitInProgress:
            return
```

In `_issue_flatten` replace master's `try: self._liquidation.start(...) except LiquidationBusy: pass` and the arguments up to `flatten_issued=True,` (ruling 38):

```python
        try:
            receipt = self._liquidation.start(self._account_id, cause, deadline)
            # R10: persist and poll the root we got back; it is another root after a join.
            root = getattr(receipt, "cause_command_id", None)
        except LiquidationBusy:
            # start() committed its own claim before it waited for the lock (only an own
            # root waits), so the root is this cause; a later rescan advances it.
            root = cause
        if not root:
            raise RuntimeError(f"liquidation start for {cause} returned no root to poll")
        state = self._evolve(
            state,
            state="FLATTENING",
            flatten_command_id=root,
```

In `_poll_flat` replace the receipt lookup and add the `FAILED_SAFE` branch before the `FLAT` check:

```python
        receipt = None
        try:
            self._liquidation.rescan()
            if state.flatten_command_id:
                receipt = self._liquidation.receipt_for(state.flatten_command_id)
        except Exception:
            receipt = None

        if receipt is not None and getattr(receipt, "state", None) == "FAILED_SAFE":
            # Ruling: the root this session follows failed safe (its breaker is tripped);
            # say so now instead of polling a dead root until the flat deadline.
            detail = f"flatten root {state.flatten_command_id} ended FAILED_SAFE: {getattr(receipt, 'detail', '')}"
            self._breaker.record(BreakerSignal(kind="LIQUIDATION_FAILED", occurred_at=now_utc, detail=detail,
                                               key=state.flatten_command_id))
            state = self._evolve(state, state="INCIDENT", incident=detail)
            self._persist(state, now_utc)
            return state
```

In `trader/trading/command_stack.py`, in the `SessionController(` call, the `time_exit=` argument becomes:

```python
        time_exit=SessionTimeExitAdapter(liquidation_service, account_id=trader.ib_account, now=now),
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py tests/test_command_stack.py -q --timeout=30`
Expected: all PASS (37 in the file), no xfail left.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/session_controller.py trader/trading/command_stack.py tests/automation/test_session_controller.py
git commit -m "fix: time exits close through the scoped liquidation and poll their own root

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 16: The session cancel phase cancels our entries only, and closes oversized protection (R16, R37)

`_issue_cancel` (`trader/automation/session_controller.py`) passes **every** working order to `SessionCancelAdapter`, which cancels each one. A filled position's protective stop is therefore cancelled at the cancel deadline, before the flatten owns protection; the saga reads that bare cancel as `MISSING_PROTECTION` and starts an avoidable emergency liquidation.

The cancel phase now selects only **our working entries** (`leg == "entry"`, not external, not an order of a liquidation root). A partly filled entry is one of them: its unfilled rest is cancelled, so nothing fills after the entry cutoff (R37). Its filled part keeps its protection; the saga treats `entry Cancelled` with a fill as `PARTIALLY_FILLED`, not an incident (a test pins it), so no hand-over is needed for this cancel. Protective children, a close's reduce (also a pre-SP1 one, Task 2) and external orders are left to the flatten, which hands protection over first.

The children of a partly filled entry may still be sized for the whole entry (a stop for 10 on a position of 4 would open a short of 6 if it triggers; whether IB shrinks them is not proven here). So after the cancel, while the flatten has not started, every tick checks each position: if a working stop or target for that conid has more outstanding than the position, the conid is closed now through the scoped close (`time_exit.request_exit` with a deterministic id `{cancel root}-protect-{conid}`; Task 11 made that a hand-over, cancel and reduce). One entry that cannot be cancelled no longer stops the others: the adapter logs it and goes on. Entry cancels are not journaled liquidation children: a cancel is idempotent at the broker, and a crash before `cancel_issued` is stored simply sends it again.

Run this task after Task 11: the oversized-protection close needs the time exit to be a scoped close.

**Files:**
- Modify: `trader/automation/session_controller.py` (`_is_own_entry`, `SessionCancelAdapter.cancel_working_entries`, `_issue_cancel`, `run_due`, new `_close_oversized_protection`)
- Test: `tests/automation/test_session_controller.py`, `tests/automation/test_protective_order_saga.py`

**Interfaces:**
- Produces: `_issue_cancel` passes `[o for o in working if _is_own_entry(o)]` to `CancelPort.cancel_working_entries`; the port is unchanged. `run_due` step 2b calls `_close_oversized_protection` while `cancel_issued and not flatten_issued`.

- [ ] **Step 1: Write the failing tests**

In `tests/automation/test_session_controller.py` give `_order` two keyword parameters (`group: str = "og-1"`, `action: str = "BUY"`, used for `order_group_id` and `action`). Keep `test_partial_fill_during_cancel_still_cancels_remainder` as it is (it states R37), and add before `test_flatten_at_flatten_deadline_uses_liquidation`:

```python
def test_session_cancel_entries_keeps_protective_children(tmp_path):
    """R16 / #27: stops, targets, a close's reduce and external orders are not cancelled."""
    working = (
        _order("og-1:entry", leg="entry"),
        _order("og-2:stop", leg="stop", group="og-2", action="SELL"),
        _order("og-2:take_profit", leg="take_profit", group="og-2", action="SELL"),
        _order("ext-1", leg="entry", is_external=True, group=None),
        _order("flat-1-liquidation-reduce-265598:entry", leg="entry",
               group="flat-1-liquidation-reduce-265598", action="SELL"),
    )
    broker = FakeBroker([_snapshot(1, positions=[_position()], working=working)])
    controller, _b, cancel, _l, breaker, _t, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert [c[1] for c in cancel.calls] == ["og-1:entry"]
    assert breaker.signals == []


def test_protection_bigger_than_the_position_after_the_cancel_closes_the_conid(tmp_path):
    """D16: the entry filled 4 of 10 and its rest was cancelled; a stop for 10 would reverse the position."""
    working = (_order("og-1:stop", leg="stop", total=10.0, action="SELL"),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 36)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    from trader.automation.session_controller import SessionController
    root = SessionController.cancel_command_id(ACCOUNT, SESSION_DATE)
    expected = {"command_id": f"{root}-protect-{CONID}", "conid": CONID, "quantity": Decimal("4.0"), "side": "BUY"}
    assert time_exit.exits and all(e == expected for e in time_exit.exits)   # one root id: the close joins itself


def test_protection_that_matches_the_position_is_left_to_the_flatten(tmp_path):
    working = (_order("og-1:stop", leg="stop", total=4.0, action="SELL"),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, _c, _l, _br, time_exit, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 36)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert time_exit.exits == []


def test_an_entry_that_cannot_be_cancelled_does_not_stop_the_others():
    from trader.automation.session_controller import SessionCancelAdapter
    from trader.trading.liquidation_service import DispatchRefused

    sent = []

    def cancel(order, child):
        if order.order_entity_id == "gone":
            raise DispatchRefused("CANCEL_UNRESOLVED", "no live order")
        sent.append(order.order_entity_id)
    SessionCancelAdapter(SimpleNamespace(cancel=cancel)).cancel_working_entries(
        root_command_id="session-cancel-x", orders=(_order("gone"), _order("og-2:entry")))
    assert sent == ["og-2:entry"]
```

Append to `tests/automation/test_protective_order_saga.py`:

```python
def test_cancelling_the_rest_of_a_partly_filled_entry_keeps_protection(tmp_path):
    """D16: the session cancels the entry's unfilled rest; the filled part stays protected, no incident."""
    saga, intent, state, breaker, liquidation, _dispatch = _started(tmp_path)
    og = state.order_group_id
    saga.on_broker_event(_event(og, leg="stop", status="Submitted", order_id=2))
    saga.on_broker_event(_event(og, leg="entry", status="Submitted", filled=4.0, order_id=1))
    state = saga.on_broker_event(_event(og, leg="entry", status="Cancelled", filled=4.0, order_id=1))
    assert (state.state, state.protection_quantity) == ("PARTIALLY_FILLED", Decimal("4"))
    assert breaker.signals == [] and liquidation.starts == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py tests/automation/test_protective_order_saga.py -q --timeout=30`
Expected: 3 failed — the stop, target, external order and the old reduce are cancelled; the oversized stop is not closed; one refused cancel stops the loop. `test_protection_that_matches_the_position_is_left_to_the_flatten` and the saga test already pass: they pin what must not change.

- [ ] **Step 3: Implement**

Add after the `# Adapters` header:

```python
def _is_own_entry(order: Any) -> bool:
    """A working entry order of ours: not external, not a protective child, not a close's child."""
    from trader.trading.order_correlation import liquidation_child_kind
    return (getattr(order, "leg", None) == "entry" and not getattr(order, "is_external", False)
            and liquidation_child_kind(getattr(order, "order_group_id", None)) is None)
```

Replace `SessionCancelAdapter.cancel_working_entries` (add `import logging` to the module imports):

```python
    def cancel_working_entries(
        self, *, root_command_id: str, orders: Sequence[Any],
    ) -> list[str]:
        """Cancel each entry; one that cannot be cancelled is logged and the others still go.

        An entry cancel is idempotent at the broker, so it is not a journaled
        child: a crash before ``cancel_issued`` is stored sends it again.
        """
        from trader.trading.liquidation_service import DispatchRefused
        child_ids: list[str] = []
        for index, order in enumerate(orders):
            child = f"{root_command_id}-{index}"
            child_ids.append(child)
            cancel = getattr(self._dispatch, "cancel", None) if self._dispatch is not None else None
            if cancel is None:
                continue
            try:
                cancel(order, child)
            except DispatchRefused as ex:
                logging.getLogger(__name__).info("entry %s needs no cancel: %s", order.order_entity_id, ex)
            except Exception:
                logging.getLogger(__name__).exception("entry cancel %s failed", order.order_entity_id)
        return child_ids
```

In `_issue_cancel`, replace the block from `snapshot = self._broker.capture(...)` to the `cancel_working_entries` call with:

```python
            snapshot = self._broker.capture(self._account_id)
            entries = tuple(order for order in getattr(snapshot, "working_orders", ()) or ()
                            if _is_own_entry(order))
            # R16 / D16: only our working entries, a partly filled one included, so
            # nothing fills after the cutoff. Protective children, exits and external
            # orders are left to the flatten, which owns their protection.
            if entries:
                self._cancel.cancel_working_entries(root_command_id=root, orders=entries)
```

In `run_due`, between step 2 (cancel) and step 3 (flatten):

```python
        # 2b) A cancelled partly filled entry must not leave bigger protection than the position
        if state.cancel_issued and not state.flatten_issued and state.state not in _TERMINAL:
            self._close_oversized_protection(state)
```

Add before `_issue_flatten`:

```python
    def _close_oversized_protection(self, state: SessionControllerState) -> None:
        """D16: after an entry's remainder is cancelled, its children may still be sized for the
        whole entry. A working stop or target for more than the position would reverse it, so the
        conid is closed now through the scoped close (hand-over, cancel, reduce)."""
        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception:
            return
        held = {int(p.conid): float(p.quantity) for p in getattr(snapshot, "positions", ()) or ()
                if float(p.quantity) != 0.0}
        for conid, quantity in sorted(held.items()):
            protective = [o for o in getattr(snapshot, "working_orders", ()) or ()
                          if int(o.conid) == conid and not o.is_external and o.leg in ("stop", "take_profit")]
            if any(float(o.total_quantity) - float(o.filled_quantity) > abs(quantity) for o in protective):
                self._time_exit.request_exit(
                    command_id=f"{self.cancel_command_id(self._account_id, state.session_date)}-protect-{conid}",
                    conid=conid, quantity=Decimal(str(abs(quantity))), side="BUY" if quantity > 0 else "SELL")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py tests/automation/test_protective_order_saga.py -q --timeout=30`
Expected: all PASS (`test_cancel_entries_at_cancel_deadline`, `test_external_position_is_included_in_flatten` and `test_delayed_run_due_catches_up_all_missed_deadlines_in_order` still pass: their working orders are our unfilled entries, and the flatten still starts).

- [ ] **Step 5: Commit**

```bash
git add trader/automation/session_controller.py tests/automation/test_session_controller.py tests/automation/test_protective_order_saga.py
git commit -m "fix: session cancel phase cancels our entries only and closes oversized protection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 12: One-strategy SELL intents become a proven-reduction close

A SELL intent on the old path is an exit of the held long (long-only model). It must never go through `build_bracket_plan`, which adds a reverse BUY stop. On the account's broker snapshot the trader checks: there is a position on the conid; SELL reduces it; the requested quantity (if any) is at most the position. Anything else is refused `NOT_A_REDUCTION` (an entry attempt). The close is then a scoped root keyed by the command id, or the root it joins; the command sits in `OUTCOME_UNKNOWN` (`CLOSE_PENDING`) with the root it must follow, and is scheduled for reconciliation (Task 17 resolves it). The close skips `session_risk` and, through Task 14, the entry gates, so it works after a loss breach (spec 5.1). This check is a reduction proof, not a sizing: the close sizes every reduce from its own fenced generations, and the reduce-only boundary checks IB's live position again (the round-1 text said "fresh fenced snapshot"; the code never fenced it, and does not need to).

Review round 2 (R32, D11): the SELL branch runs **before** the claim that checks the pause on new exposure — an exit adds no exposure — with its own `VALIDATED → SUBMITTING` claim; the account fence (the command's account is the trader's, the snapshot is the account's) and the claim and idempotency stay. A SELL when `liquidation`/`broker` are not wired is refused `CLOSE_PATH_UNAVAILABLE`; it never falls back to the bracket path. The claim block is one method now (`_claim(cmd, require_unpaused=...)`), shared by both paths.

**Files:**
- Modify: `trader/automation/automated_intent_command.py`
- Test: `tests/automation/test_automated_command_boundary.py`

**Interfaces:**
- Consumes: `LiquidationService.start(..., scope="conid", conid, quantity)` (through the facade), `ExitInProgress` (Task 3), `LiquidationRefused` (Task 4), `BrokerRiskSnapshot.reducible_quantity` (`trader/data/broker_state.py:279`).
- Produces: `AutomatedIntentCommandService(..., liquidation: Optional[Any] = None, broker: Optional[Any] = None, close_deadline_seconds: float = 300.0)`. Every SELL intent takes the close path after the artifact checks and before the pause check; BUY intents are unchanged. Refusal codes: `CLOSE_PATH_UNAVAILABLE`, `ACCOUNT_MISMATCH`, `NOT_A_REDUCTION`, `EXIT_IN_PROGRESS`, `BROKER_SNAPSHOT_UNAVAILABLE`, and the `LiquidationRefused` codes (`PARTIAL_QUANTITY_INVALID`, ...). Accepted close: `OUTCOME_UNKNOWN`, `error_code="CLOSE_PENDING"`, `outcome={"close_root_id", "liquidation_state", "generation_id", "detail"}`. `requested == held` becomes a full close (ruling 10).

- [ ] **Step 1: Write the failing tests**

In `tests/automation/test_automated_command_boundary.py` change the signature of `_build_stack` to:

```python
def _build_stack(tmp_path: Path, *, dispatch=None, verifier=None, now=None, liquidation=None, broker=None,
                 protective_saga=None, approval_factory=None):
```

and add these four arguments after `schedule_reconcile=schedule.schedule,` in its `AutomatedIntentCommandService(` call:

```python
        liquidation=liquidation,
        broker=broker,
        protective_saga=protective_saga,
        approval_factory=approval_factory,
```

and append:

```python
# ---------------------------------------------------------------------------
# SP1 plan 1 Task 12: SELL intents become a proven-reduction close
# ---------------------------------------------------------------------------

class _FakeCloseLiquidation:
    def __init__(self, *, raise_exc=None, root=None):
        self.starts = []
        self.raise_exc = raise_exc
        self.root = root

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.starts.append((account_id, cause_command_id, deadline, kwargs))
        return SimpleNamespace(cause_command_id=self.root or cause_command_id, state="VERIFYING",
                               generation_id=1, detail="close submitted")


class _FakeBrokerSnapshot:
    def __init__(self, held):
        self.held = held

    def capture(self, account_id):
        return SimpleNamespace(account_id=ACCOUNT, generation_id=1,
                               reducible_quantity=lambda conid: self.held if conid == 265598 else 0.0)


def _execute_sell(stack, tmp_path, requested):
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent(side="SELL", requested_quantity=None if requested is None else Decimal(str(requested)))
    return stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service",
    )), intent


def test_sell_intent_becomes_a_scoped_close_and_never_a_bracket(tmp_path):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    receipt, intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert receipt.outcome["close_root_id"] == intent.command_id
    _account, root, _deadline, kwargs = liquidation.starts[0]
    assert (root, kwargs) == (intent.command_id, {"scope": "conid", "conid": 265598, "quantity": None})
    assert stack.dispatch.calls == []
    assert stack.schedule.calls == 1          # R17: reconciled from its exact root


@pytest.mark.parametrize("requested,expected", [(4, 4.0), (10, None)])
def test_sell_intent_quantity_is_a_partial_or_a_full_close(tmp_path, requested, expected):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    _execute_sell(stack, tmp_path, requested)
    assert liquidation.starts[0][3]["quantity"] == expected


@pytest.mark.parametrize("held,requested", [(0.0, None), (10.0, 11), (-5.0, None)])
def test_sell_that_is_not_a_reduction_is_refused(tmp_path, held, requested):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(held))
    receipt, _intent = _execute_sell(stack, tmp_path, requested)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "NOT_A_REDUCTION")
    assert liquidation.starts == [] and stack.dispatch.calls == []


def test_sell_intent_refused_while_another_close_owns_the_conid(tmp_path):
    from trader.trading.exit_owner import ExitInProgress
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(raise_exc=ExitInProgress("other-root")),
                         broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, 3)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "EXIT_IN_PROGRESS")


def test_sell_intent_refused_by_the_partial_quantity_rule(tmp_path):
    from trader.trading.liquidation_service import LiquidationRefused
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(
        raise_exc=LiquidationRefused("PARTIAL_QUANTITY_INVALID")), broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, 0.4)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "PARTIAL_QUANTITY_INVALID")


def test_joined_sell_records_the_root_it_must_follow(tmp_path):
    stack = _build_stack(tmp_path, liquidation=_FakeCloseLiquidation(root="time-exit-1"),
                         broker=_FakeBrokerSnapshot(10.0))
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert receipt.outcome["close_root_id"] == "time-exit-1"


def test_buy_intent_path_is_unchanged_with_close_configured(tmp_path):
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(0.0))
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True)
    intent = make_intent()
    receipt = stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service",
    ))
    assert receipt.state in ("SUBMITTED", "RESOLVED")
    assert len(stack.dispatch.calls) == 1 and liquidation.starts == []


def _execute_buy(stack, tmp_path):
    (tmp_path / "bundles" / ARTIFACT_DIGEST.replace(":", "_")).mkdir(parents=True, exist_ok=True)
    intent = make_intent()
    return stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent), source="strategy_service",
    ))


def test_sell_closes_while_new_exposure_is_paused_and_a_buy_is_refused(tmp_path):
    """R32 / D11: the pause stops new exposure, never an exit."""
    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    stack.controls.set(ACCOUNT, True, None, "pause-1", "test", NOW)
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert len(liquidation.starts) == 1
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "TRADING_PAUSED")


def test_sell_without_the_close_path_is_refused_never_bracketed(tmp_path):
    """R32 / #31: an unwired close path fails loudly; the bracket path would add a reverse stop."""
    stack = _build_stack(tmp_path)
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("REJECTED", "CLOSE_PATH_UNAVAILABLE")
    assert stack.dispatch.calls == []


def test_after_a_session_loss_breach_a_sell_closes_and_a_buy_is_refused(tmp_path):
    """#31: session_risk (inside the saga) refuses a BUY after a daily-loss breach; a SELL never reaches it."""
    saga_calls = []

    class _BreachedSaga:
        def start(self, *, intent, **_kwargs):
            saga_calls.append(intent.side)
            return SimpleNamespace(state="CLOSED", error_code="DAILY_LOSS", submitted_order_ids=())

    liquidation = _FakeCloseLiquidation()
    stack = _build_stack(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0),
                         protective_saga=_BreachedSaga(), approval_factory=lambda **_k: SimpleNamespace())
    buy = _execute_buy(stack, tmp_path)
    assert (buy.state, buy.error_code) == ("REJECTED", "DAILY_LOSS")
    receipt, _intent = _execute_sell(stack, tmp_path, None)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert saga_calls == ["BUY"] and len(liquidation.starts) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_automated_command_boundary.py -q --timeout=30`
Expected: 40 failed, 5 passed — `TypeError: AutomatedIntentCommandService.__init__() got an unexpected keyword argument 'liquidation'` (every test that builds the stack).

- [ ] **Step 3: Implement**

Add the three constructor parameters (`liquidation: Optional[Any] = None, broker: Optional[Any] = None, close_deadline_seconds: float = 300.0`) and store them (`self._liquidation`, `self._broker`, `self._close_deadline_seconds`). In `execute`, replace everything from `self._transition(cmd, "RECEIVED", "VALIDATED")` to `# Task 5 path` with:

```python
        self._transition(cmd, "RECEIVED", "VALIDATED")

        # A SELL on the long-only path is an exit, never a bracket (spec 5.1). It adds no
        # exposure, so the pause on new exposure does not stop it (R32).
        if intent.side == "SELL":
            return self._execute_close(cmd, intent)

        order_group_id = f"og-{cmd.command_id}"
        order_ref = encode_order_ref(order_group_id)

        try:
            self._claim(cmd, require_unpaused=True)
        except Exception as ex:
            code = "TRADING_PAUSED"
            if getattr(ex, "code", None):
                code = str(ex.code)
            self._transition(cmd, "VALIDATED", "REJECTED", error_code=code)
            return self._receipt(cmd.command_id, "REJECTED", code, True)
```

Add before `_finish_submitted`:

```python
    def _claim(self, cmd, *, require_unpaused: bool) -> None:
        """VALIDATED -> SUBMITTING in one journal transaction; an entry also checks the pause."""
        from trader.trading.command_coordinator import _command_updated_mutation, _noop_write

        def claim(conn, append):
            if require_unpaused:
                self._controls.require_unpaused_in_tx(conn, self._account_id)
            self._ledger.transition_in_tx(conn, cmd.command_id, "VALIDATED", "SUBMITTING")
            append(
                _command_updated_mutation(cmd, "SUBMITTING", self._now_utc()),
                _noop_write,
                f"command:{cmd.command_id}:submitting",
            )
        self._journal.mutate_batch_work(self._journal.connect(), claim)

    def _execute_close(self, cmd, intent) -> CommandReceipt:
        """Prove the SELL reduces the held long on the account's broker snapshot, then close.

        The proof is a reduction check, not a sizing: the close sizes every
        reduce from its own fenced generations, and the reduce-only boundary
        checks IB's live position again (Task 14).
        """
        from trader.trading.exit_owner import ExitInProgress
        from trader.trading.liquidation_service import LiquidationRefused

        if self._liquidation is None or self._broker is None:
            # Fail loudly: never fall back to the bracket path, which would add a reverse stop.
            self._transition(cmd, "VALIDATED", "REJECTED", error_code="CLOSE_PATH_UNAVAILABLE")
            return self._receipt(cmd.command_id, "REJECTED", "CLOSE_PATH_UNAVAILABLE", False)
        if cmd.account_id != self._account_id:
            self._transition(cmd, "VALIDATED", "REJECTED", error_code="ACCOUNT_MISMATCH")
            return self._receipt(cmd.command_id, "REJECTED", "ACCOUNT_MISMATCH", False)
        self._claim(cmd, require_unpaused=False)
        try:
            snapshot = self._broker.capture(self._account_id)
            if getattr(snapshot, "account_id", None) != self._account_id:
                raise RuntimeError("broker snapshot is for another account")
        except Exception as ex:
            return self._reject_close(cmd, "BROKER_SNAPSHOT_UNAVAILABLE", {"detail": str(ex)})
        held = float(snapshot.reducible_quantity(intent.conid))
        requested = None if intent.requested_quantity is None else float(intent.requested_quantity)
        if held <= 0 or (requested is not None and requested > held):
            return self._reject_close(cmd, "NOT_A_REDUCTION", {"held": held, "requested": requested})
        # A close of the whole position takes the broker quantity at reduce time (ruling 10).
        quantity = None if requested is None or requested >= held else requested
        deadline = self._now_utc() + dt.timedelta(seconds=self._close_deadline_seconds)
        try:
            receipt = self._liquidation.start(
                self._account_id, cmd.command_id, deadline, scope="conid", conid=intent.conid, quantity=quantity,
            )
        except ExitInProgress as ex:
            return self._reject_close(cmd, "EXIT_IN_PROGRESS", {"close_root_id": ex.root_id})
        except LiquidationRefused as ex:
            return self._reject_close(cmd, ex.code, {"detail": str(ex)})
        except Exception as ex:
            self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS")
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
                                 outcome={"detail": str(ex)})

        outcome = {"close_root_id": receipt.cause_command_id, "liquidation_state": receipt.state,
                   "generation_id": receipt.generation_id, "detail": receipt.detail}
        self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="CLOSE_PENDING", outcome=outcome)
        if self._schedule_reconcile is not None:
            # R17: the reconciler resolves this command from the exact root it started or joined.
            self._schedule_reconcile(cmd.command_id)
        return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "CLOSE_PENDING", False, outcome=outcome)

    def _reject_close(self, cmd, code: str, outcome: dict) -> CommandReceipt:
        self._transition(cmd, "SUBMITTING", "REJECTED", error_code=code)
        return self._receipt(cmd.command_id, "REJECTED", code, False, outcome=outcome)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_automated_command_boundary.py tests/automation/test_execution_intent.py -q --timeout=30`
Expected: all PASS. Existing BUY-path tests are unaffected (they build the service without `liquidation`/`broker`).

- [ ] **Step 5: Commit**

```bash
git add trader/automation/automated_intent_command.py tests/automation/test_automated_command_boundary.py
git commit -m "feat: sell intents close through the scoped liquidation after a reduction proof

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 17: Joined commands resolve from their exact root (R17)

A command that starts or joins a close (SELL intent, `/flatten`, session flatten) has a row in `liquidation_joins` (Task 4). Today `OutcomeReconciler._try_resolve` (`trader/trading/command_coordinator.py:2671`) has no branch for `execute_automated_intent` or `liquidate_account`, so a joining command stays `OUTCOME_UNKNOWN` for ever and blocks `reconciliation_complete`. The new branch reads `LiquidationRunStore.close_resolution(command_id)`: it follows `SUPERSEDED` to the account root that took over, and resolves only on a broker-proven goal with cleanup finished (`CLOSED`/`FLAT` for a full request, also `DONE` for a partial one). An open root, or a command with no join row (a bracket entry), stays unresolved.

Review round 2 (R33, D12):
- **The reconciler is the only resolver.** The liquidation service no longer resolves anything; it schedules the commands of a finished root (Task 4). A command still in `SUBMITTING` when its root ends is left to its producer, which moves it to `OUTCOME_UNKNOWN` and schedules it, so two writers never race on one ledger row (tested).
- **A failed root has a terminal path.** `FAILED_SAFE`, `REDUCE_FAILED` (Task 6), or a `DONE` root for a full request reject the command with `CLOSE_FAILED_SAFE` / `REDUCE_FAILED` / `CLOSE_GOAL_NOT_MET` and raise an operator alert. The command never becomes a success, and it does not stay `OUTCOME_UNKNOWN` for ever, which would block `reconciliation_complete` and the breaker reset with no way out. The breaker of a `FAILED_SAFE` root stays tripped until an operator resets it.
- The resolution outcome carries the requested goal and the root's goal, so a partial request that was closed fully says so.

**Files:**
- Modify: `trader/trading/command_coordinator.py` (`OutcomeReconciler.__init__`, `_try_resolve`, new `_reconcile_close`)
- Test: `tests/test_close_reconciliation.py` (create)

**Interfaces:**
- Consumes: `LiquidationRunStore.close_resolution`, `CloseResolution` (Task 4).
- Produces: `OutcomeReconciler(..., closes: Optional[Any] = None)` — any object with `close_resolution(command_id) -> Optional[CloseResolution]`. Task 13 passes the run store.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_close_reconciliation.py
"""SP1 plan 1 Task 17 (R17): commands that start or join a close resolve from that exact root."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerPositionRow, BrokerRiskSnapshot
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import (
    CommandLedger, CommandRequest, OutcomeReconciler, apply_command_ledger_migration,
)
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import (
    JoinRow, LiquidationReceipt, LiquidationRunStore, LiquidationService, apply_liquidation_migration,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU111111"
DEADLINE = NOW + dt.timedelta(minutes=5)


class _Alerts:
    def __init__(self): self.raised = []
    def raise_alert(self, command_id, detail): self.raised.append((command_id, detail))


@pytest.fixture
def env(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "close-recon.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    ledger = CommandLedger(journal)
    store = LiquidationRunStore(db)
    reconciler = OutcomeReconciler(
        journal=journal, ledger=ledger,
        orders=SimpleNamespace(find_by_order_ref=lambda *a: [], enumeration_complete=lambda: False),
        strategy=SimpleNamespace(), alerts=_Alerts(), now=lambda: NOW, closes=store,
    )
    return SimpleNamespace(db=db, journal=journal, ledger=ledger, store=store, reconciler=reconciler,
                           alerts=reconciler._alerts)


def _command(env, command_id, *, action="execute_automated_intent", state="OUTCOME_UNKNOWN"):
    request = CommandRequest(command_id=command_id, action=action, account_id=ACCOUNT, target_type="intent",
                             target_id=command_id, expected_version=None, body={}, source="strategy_service")

    def work(conn, _append):
        env.ledger.insert_received_in_tx(conn, request, f"hash-{command_id}", NOW)
        previous = "RECEIVED"
        for step in ("SUBMITTING", "OUTCOME_UNKNOWN")[: ("SUBMITTING", "OUTCOME_UNKNOWN").index(state) + 1]:
            env.ledger.transition_in_tx(conn, command_id, previous, step, now=NOW)
            previous = step
    env.journal.mutate_batch_work(env.journal.connect(), work)


def _root(env, root_id, state, *, scope="conid", goal="zero", superseded_by=None, cleanup_pending=False):
    env.store.transaction(lambda conn: env.store.insert_run_in_tx(conn, LiquidationReceipt(
        ACCOUNT, root_id, state, DEADLINE, generation_id=9, scope=scope, conid=None if scope == "account" else 1,
        goal=goal, superseded_by=superseded_by, cleanup_pending=cleanup_pending), NOW))


def _join(env, command_id, root_id, goal, *, quantity=None, outcome="JOINED"):
    env.store.transaction(lambda conn: env.store.record_join_in_tx(conn, JoinRow(
        command_id, root_id, ACCOUNT, None if goal == "account" else 1, outcome, goal, quantity), NOW))


def _state(env, command_id):
    return env.ledger.get(command_id).state


def test_sell_that_joined_a_time_exit_resolves_when_that_root_is_closed(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-1", "exit-1", "zero")
    _command(env, "sell-1")
    assert env.reconciler.reconcile_once("sell-1", NOW).resolved is True
    row = env.ledger.get("sell-1")
    assert (row.state, row.outcome["close_root_id"], row.outcome["liquidation_state"]) == ("RESOLVED", "exit-1", "CLOSED")
    assert env.ledger.unresolved_for_account(ACCOUNT) == []        # #28: reconciliation is ready again


def test_sell_that_upgraded_a_partial_root_resolves_when_it_is_closed(env):
    _root(env, "p-1", "CLOSED")
    _join(env, "sell-2", "p-1", "zero", outcome="UPGRADED")
    _command(env, "sell-2")
    assert env.reconciler.reconcile_once("sell-2", NOW).resolved is True


def test_sell_that_joined_an_account_flatten_resolves_on_flat(env):
    _root(env, "flat-1", "FLAT", scope="account")
    _join(env, "sell-3", "flat-1", "zero", outcome="JOINED_FLATTEN")
    _command(env, "sell-3")
    assert env.reconciler.reconcile_once("sell-3", NOW).resolved is True


def test_superseded_root_is_followed_to_the_account_root(env):
    _root(env, "p-1", "SUPERSEDED", goal="partial", superseded_by="flat-1")
    _root(env, "flat-1", "FLAT", scope="account")
    _join(env, "sell-4", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _command(env, "sell-4")
    assert env.reconciler.reconcile_once("sell-4", NOW).resolved is True
    assert env.ledger.get("sell-4").outcome["close_root_id"] == "flat-1"


@pytest.mark.parametrize("state,cleanup_pending", [("VERIFYING", False), ("CLOSED", True)])
def test_open_or_uncleaned_root_leaves_the_command_unknown(env, state, cleanup_pending):
    _root(env, "exit-1", state, cleanup_pending=cleanup_pending)
    _join(env, "sell-5", "exit-1", "zero")
    _command(env, "sell-5")
    assert env.reconciler.reconcile_once("sell-5", NOW).resolved is False
    assert _state(env, "sell-5") == "OUTCOME_UNKNOWN"


@pytest.mark.parametrize("state,code", [("FAILED_SAFE", "CLOSE_FAILED_SAFE"), ("REDUCE_FAILED", "REDUCE_FAILED")])
def test_a_root_that_failed_rejects_the_command_with_an_operator_alert(env, state, code):
    """R33 / D12: never a success, and never OUTCOME_UNKNOWN for ever (that blocks reconciliation)."""
    _root(env, "exit-1", state, goal="partial" if state == "REDUCE_FAILED" else "zero")
    _join(env, "sell-5", "exit-1", "zero")
    _command(env, "sell-5")
    assert env.reconciler.reconcile_once("sell-5", NOW).resolved is True
    row = env.ledger.get("sell-5")
    assert (row.state, row.error_code, row.outcome["liquidation_state"]) == ("REJECTED", code, state)
    assert [c for c, _detail in env.alerts.raised] == ["sell-5"]
    assert env.ledger.unresolved_for_account(ACCOUNT) == []


def test_done_resolves_a_partial_request_but_fails_a_full_one(env):
    _root(env, "p-1", "DONE", goal="partial")
    _join(env, "sell-6", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _join(env, "sell-7", "p-1", "zero")
    _command(env, "sell-6")
    _command(env, "sell-7")
    assert env.reconciler.reconcile_once("sell-6", NOW).resolved is True
    assert env.reconciler.reconcile_once("sell-7", NOW).resolved is True
    assert (_state(env, "sell-6"), env.ledger.get("sell-7").error_code) == ("RESOLVED", "CLOSE_GOAL_NOT_MET")


def test_root_that_ended_before_close_pending_was_recorded_resolves_the_submitting_command(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-8", "exit-1", "zero")
    _command(env, "sell-8", state="SUBMITTING")
    assert env.reconciler.reconcile_once("sell-8", NOW).resolved is True
    assert _state(env, "sell-8") == "RESOLVED"


def test_restart_requeues_and_resolves_joined_commands(env):
    _root(env, "exit-1", "CLOSED")
    _join(env, "sell-9", "exit-1", "zero")
    _command(env, "sell-9")
    assert "sell-9" in env.reconciler.rescan_on_startup()
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-9") == "RESOLVED"


def test_bracket_entry_without_a_close_root_is_left_alone(env):
    _command(env, "buy-1")
    assert env.reconciler.reconcile_once("buy-1", NOW).resolved is False


def test_joined_flatten_command_resolves_from_its_root(env):
    _root(env, "session-flatten-1", "FLAT", scope="account")
    _join(env, "flatten-ui-1", "session-flatten-1", "account", outcome="JOINED_FLATTEN")
    _command(env, "flatten-ui-1", action="liquidate_account")
    assert env.reconciler.reconcile_once("flatten-ui-1", NOW).resolved is True


def _snapshot(generation, quantity):
    positions = () if not quantity else (BrokerPositionRow(
        account_id=ACCOUNT, conid=1, symbol="AAPL", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=None, market_price=None, market_value=None, unrealized_pnl=None,
        realized_pnl=None, daily_pnl=None, deleted=False, revision=1, source_timestamp=NOW),)
    return BrokerRiskSnapshot(generation_id=generation, source_cursor=generation, promoted_at=NOW,
                              account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000,
                              daily_pnl=0, positions=positions, working_orders=())


def _service(env, snapshots, rows):
    broker = SimpleNamespace(last=0)

    def capture(_account):
        snapshot = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
        broker.last = snapshot.generation_id
        return snapshot
    broker.capture = capture
    dispatch = SimpleNamespace(
        reduce=lambda p, s, q, cid: rows.setdefault(cid, [SimpleNamespace(
            status="Filled", filled_quantity=q, total_quantity=q)]),
        find_orders=lambda a, cid: rows.get(cid, []), get_order=lambda e: None,
        enumeration_complete=lambda: True, newest_generation=lambda: broker.last)
    return LiquidationService(broker, dispatch, store=env.store, registry=ExitOwnerRegistry(env.db),
                              now=lambda: NOW, journal=env.journal, ledger=env.ledger,
                              schedule_reconcile=lambda command_id: env.reconciler.schedule(command_id, NOW))


def test_flatten_cleanup_hands_every_joined_flatten_command_to_the_reconciler(env):
    """R10 + R17 + R33: two /flatten commands, one root; the reconciler resolves both once FLAT is proven."""
    service = _service(env, [_snapshot(1, 10.0), _snapshot(2, 0.0), _snapshot(3, 0.0)], {})
    for command_id in ("flatten-a", "flatten-b"):
        _command(env, command_id, action="liquidate_account")
        service.start(ACCOUNT, command_id, DEADLINE)
    service.rescan()
    assert service.rescan().state == "FLAT"
    assert (_state(env, "flatten-a"), _state(env, "flatten-b")) == ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN")
    env.reconciler.run_due(NOW)
    assert (_state(env, "flatten-a"), _state(env, "flatten-b")) == ("RESOLVED", "RESOLVED")
    assert env.ledger.get("flatten-b").outcome["close_root_id"] == "flatten-a"


def test_a_command_still_submitting_when_its_root_ends_is_left_to_its_producer(env):
    """R33: one resolver per row. Cleanup skips a SUBMITTING row; the producer moves it and schedules it."""
    service = _service(env, [_snapshot(1, 0.0), _snapshot(2, 0.0)], {})
    _command(env, "sell-1", state="SUBMITTING")
    service.start(ACCOUNT, "sell-1", DEADLINE, scope="conid", conid=1)    # flat at once: no order
    service.rescan()
    assert service.receipt_for("sell-1").state == "CLOSED"
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-1") == "SUBMITTING"                          # nobody raced the producer

    def to_unknown(conn, _append):
        env.ledger.transition_in_tx(conn, "sell-1", "SUBMITTING", "OUTCOME_UNKNOWN", now=NOW)
    env.journal.mutate_batch_work(env.journal.connect(), to_unknown)
    env.reconciler.schedule("sell-1", NOW)
    env.reconciler.run_due(NOW)
    assert _state(env, "sell-1") == "RESOLVED"
    env.reconciler.schedule("sell-1", NOW)                                # a late schedule is a no-op
    assert env.reconciler.reconcile_once("sell-1", NOW).resolved is True and _state(env, "sell-1") == "RESOLVED"


def test_a_joined_sell_on_a_reduce_failed_root_is_rejected_after_a_restart(env):
    """R2-2 / #29: the original partial and the joined SELL both fail; restart requeues them."""
    _root(env, "p-1", "REDUCE_FAILED", goal="partial")
    _join(env, "p-1", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _join(env, "sell-2", "p-1", "partial", quantity=2.0, outcome="JOINED")
    _command(env, "p-1")
    _command(env, "sell-2")
    restarted = OutcomeReconciler(journal=env.journal, ledger=env.ledger,
                                  orders=SimpleNamespace(find_by_order_ref=lambda *a: [], enumeration_complete=lambda: False),
                                  strategy=SimpleNamespace(), alerts=_Alerts(), now=lambda: NOW, closes=env.store)
    assert set(restarted.rescan_on_startup()) >= {"p-1", "sell-2"}
    restarted.run_due(NOW)
    assert [env.ledger.get(c).error_code for c in ("p-1", "sell-2")] == ["REDUCE_FAILED", "REDUCE_FAILED"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_close_reconciliation.py -q --timeout=30`
Expected: FAIL — `TypeError: OutcomeReconciler.__init__() got an unexpected keyword argument 'closes'`.

- [ ] **Step 3: Implement**

Add `closes: Optional[Any] = None` as the last constructor parameter of `OutcomeReconciler` and `self._closes = closes`. In `_try_resolve`, before the "Unmapped action" fallback:

```python
        if action in ("execute_automated_intent", "liquidate_account"):
            return self._reconcile_close(row, now)
```

Add before `_reconcile_create`:

```python
    def _reconcile_close(self, row: LedgerRow, now: dt.datetime) -> bool:
        """R17 / R33: a command that started or joined a close root resolves from that exact root.

        This reconciler is the only resolver of close commands; the
        liquidation service only schedules them. SUPERSEDED is followed to
        the account root that took over. A broker-proven goal (CLOSED / DONE
        / FLAT, cleanup finished) resolves the command. A decided root that
        did not meet the goal (FAILED_SAFE, REDUCE_FAILED, DONE for a full
        close) rejects it with that code and raises an operator alert: the
        position needs a person, and the reconciliation gate must not stay
        blocked for ever. An open root, or a command with no close root (a
        bracket entry), stays OUTCOME_UNKNOWN.
        """
        if self._closes is None:
            return False
        resolution = self._closes.close_resolution(row.command_id)
        if resolution is None:
            return False
        if resolution.success:
            self._resolve_command_only(row, dict(resolution.outcome), now)
            return True
        self._reject_command_only(row, error_code=resolution.error_code or "CLOSE_FAILED",
                                  outcome=dict(resolution.outcome), now=now)
        self._alerts.raise_alert(
            row.command_id,
            f"{row.action} ended {resolution.state} on close root {resolution.root_id} "
            f"({resolution.error_code}); operator check of the position required",
        )
        return True
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_close_reconciliation.py tests/test_command_coordinator.py -q --timeout=60`
Expected: all PASS (16 in the new file).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_coordinator.py tests/test_close_reconciliation.py
git commit -m "feat: reconcile close commands from the exact root they joined

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 13: Wire it into `build_command_stack` and prove it end to end (R21)

The rest of the composition: the generation refresh (ruling 1), the saga as protection port and failure source for the worker, the reconciler's close resolver, the intent service's close path on cold start **and** hot-arm, and the registry on the stack. Then the integration tests run the **real** `build_command_stack` on a temporary DuckDB journal and a real asyncio loop: real registry, run store, worker, saga, session controller, coordinator, reconciler, `RiskGate`, `TradingRuntimeOrderDispatch` and `Trader.place_reduce_only_order`. Only the broker is fake (`_BrokerSim` plays IB, including `openTrades` for the R35 bound, and writes promoted generations into the journal, as broker sync would). The SELL tests also replace the research-bundle verifier: signing a bundle needs a research database, and the production evidence check refuses fixture provenance; every close-path component stays real. The smaller state-machine tests of Tasks 4–8 stay.

Round 2 fixes to the fixture (R38): the fake ingest has `is_ready` as a **property** (the shape Task 18 fixed) and a `run_broker_sync` that counts calls and, when a test asks for it, promotes a generation, so the refresh path runs in composition; the cold-start fixture can enable paper automation, so the cold-start intent service is built and checked; saga events carry their own event ids; the restart tests stop the old worker and keep the same broker, open trades included.

**Files:**
- Modify: `trader/trading/command_stack.py` (`_BrokerGenerationRefresh`, `_build_automated_intent_service` and its two call sites, the `OutcomeReconciler(` call, the inner `LiquidationService(` call, after `ProtectiveOrderSaga(`, `CommandStack`, the `trader.*` assignments)
- Test: `tests/test_safe_close_integration.py` (create)

**Interfaces:**
- Produces: `CommandStack.exit_owner_registry: Any = None`; `trader.exit_owner_registry`. `_BrokerGenerationRefresh(trader, *, min_interval_seconds=5.0, clock=None)` implements `GenerationRefreshPort`. `_build_automated_intent_service(..., liquidation: Any = None)`.
- Consumes: everything from Tasks 2–18.

#29 cases and where they run here: two account producers, one root, the session polling it to `FLAT` and the owner released (`test_two_account_producers_make_one_root_one_reduce_and_release_the_owner`); restart between journal and send, and after the order left (`test_crash_between_journal_and_broker_call_restarts_without_a_duplicate`, `test_restart_after_the_order_left_continues_without_a_second_order`; the other R20 boundaries are crash-tested in Tasks 4–9); invisible child and late fill (`test_invisible_child_and_a_late_fill_never_send_a_second_reduce`); terminal partial fill, replacement protection, retired legs, the other position untouched (`test_partial_close_of_one_of_two_positions_reprotects_the_actual_remainder`); exits after a loss breach with no stop left (`test_after_a_loss_breach_a_time_exit_hands_over_cancels_the_stop_and_reduces`); the real loop (every test ticks with `tick_async` on the trader loop; the session runs through the worker); OCA rejection (`test_target_leg_rejected_by_the_broker_escalates_to_a_full_close`); cancel-entry deadline and the returned root through `FLAT` (`test_session_cancel_keeps_protection_and_the_flatten_owns_it_until_flat`); joined commands through the reconciler only (`test_joined_flatten_command_resolves_only_through_the_reconciler`); a SELL intent end to end, and a partial SELL that sells nothing (`test_a_sell_intent_closes_end_to_end_and_resolves_from_its_root`, `test_a_partial_sell_that_sells_nothing_is_protected_again_and_rejected`); the refresh in composition; cold start and hot-arm wiring; migrations 35–38 with attribution still on 32–34.

- [ ] **Step 1: Write the failing tests**

```python
"""SP1 plan 1 Task 13 (R21): the safe close through the real ``build_command_stack``.

Real: DuckDB journal, exit owner registry, liquidation run store, worker,
protective saga, session controller, coordinator, reconciler, RiskGate,
``TradingRuntimeOrderDispatch`` and ``Trader.place_reduce_only_order`` on a
real asyncio loop. Fake: only the broker (``_BrokerSim`` plays IB and writes
promoted broker generations into the journal, as broker sync would) and, in
the SELL tests, the research-bundle verifier (signing a bundle needs a
research database; every close-path component stays real).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import reactivex as rx

from trader.automation.protective_order_saga import BrokerOrderEvent, SagaState
from trader.automation.session_controller import SessionController
from trader.data.broker_state import BrokerAccountRow, BrokerOrderRow, BrokerPositionRow, BrokerStateStore
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.command_coordinator import CommandRequest
from trader.trading.command_policy import CommandAuthorityPolicy
from trader.trading.liquidation_service import BrokerChangesBusy
from trader.trading.order_correlation import classify_leg, decode_order_ref
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.trading.trading_runtime import Trader

UTC = dt.timezone.utc
ACCOUNT = "DU111111"
CONID = 265598
OTHER = 4815747
FRIDAY = dt.date(2026, 7, 17)


def _et(hour, minute):
    from zoneinfo import ZoneInfo
    return dt.datetime(FRIDAY.year, FRIDAY.month, FRIDAY.day, hour, minute,
                       tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class _Ingest:
    """Production shape: ``is_ready`` is a property; a sync request promotes a generation."""
    def __init__(self, sim):
        self.sim = sim
        self.syncs = 0
        self.ready = True          # False: a newer generation is staging, the enumeration is not complete

    @property
    def is_ready(self):
        return self.ready

    @contextmanager
    def hold_changes(self):
        """Ruling 48: the sim writes only inside a sync, so holding is refusing while one is staging."""
        if not self.ready:
            raise BrokerChangesBusy("broker generation is staging")
        yield

    async def run_broker_sync(self, _client):
        self.syncs += 1
        if self.sim.auto_refresh:
            self.sim.promote()
        return True


class _BrokerSim:
    """IB and the broker enumeration. Orders become visible only when promote() writes them."""

    def __init__(self, trader):
        self.trader = trader
        self.held: dict[int, float] = {}
        self.orders: dict[str, BrokerOrderRow] = {}
        self.perm: dict[str, int] = {}
        self.trades: dict[str, SimpleNamespace] = {}
        self.hidden: set[str] = set()
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []
        self.ack = {"MKT": "Submitted", "STP": "PreSubmitted", "LMT": "Submitted"}
        self.daily_pnl = 0.0
        self.auto_refresh = False
        self.generations = 0
        self._next_perm = 9000

    # -- fake IB client ----------------------------------------------------------------
    def isConnected(self):
        return True

    def accountValues(self, account=None):
        return [SimpleNamespace(tag="NetLiquidation", currency="USD", account=ACCOUNT, value="100000")]

    def managedAccounts(self):
        return [ACCOUNT]

    def openTrades(self):
        return list(self.trades.values())

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=c), position=q)
                for c, q in self.held.items() if q]

    def cancelOrder(self, order):
        entity = next(e for e, t in self.trades.items() if t.order is order)
        self.cancelled.append(entity)

    # -- fake executioner ------------------------------------------------------------------
    async def subscribe_place_order_direct(self, contract, order):
        self._next_perm += 1
        order.orderId = order.permId = self._next_perm
        group = decode_order_ref(order.orderRef)
        leg = classify_leg(order.orderType, 0, order.orderId, group)
        entity = f"{group}:{leg}"
        price = {"STP": order.auxPrice, "LMT": order.lmtPrice}.get(order.orderType)
        self.placed.append((group, order.orderType, order.action, order.totalQuantity, price, order.ocaGroup or None))
        self.add_order(entity, group, leg, order.action, order.orderType, order.totalQuantity,
                       conid=int(contract.conId), order=order)
        echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
        ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=self.ack[order.orderType]))
        return rx.from_iterable([echo, ack])

    # -- broker state ------------------------------------------------------------------------
    def add_order(self, entity, group, leg, action, order_type, quantity, *, conid=CONID, status="Submitted",
                  order=None):
        if order is None:
            self._next_perm += 1
            order = SimpleNamespace(permId=self._next_perm, action=action, totalQuantity=float(quantity), ocaGroup="")
        self.perm[entity] = order.permId
        self.trades[entity] = SimpleNamespace(order=order, contract=SimpleNamespace(conId=conid),
                                              orderStatus=SimpleNamespace(filled=0.0))
        self.orders[entity] = BrokerOrderRow(
            order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL", order_group_id=group,
            leg=leg, is_external=False, action=action, order_type=order_type, total_quantity=float(quantity),
            filled_quantity=0.0, avg_fill_price=None, limit_price=None, stop_price=None, tif="DAY",
            status=status, deleted=False, revision=1, source_timestamp=_et(11, 0),
            oca_group=getattr(order, "ocaGroup", "") or None,
            oca_type=getattr(order, "ocaType", 0) or None)

    def set_status(self, entity, status, *, filled=None, total=None):
        row = self.orders[entity]
        self.orders[entity] = replace(row, status=status,
                                      filled_quantity=row.filled_quantity if filled is None else float(filled),
                                      total_quantity=row.total_quantity if total is None else float(total))
        if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.trades.pop(entity, None)
        elif entity in self.trades and filled is not None:
            self.trades[entity].orderStatus.filled = float(filled)

    def entity_for(self, group_prefix):
        return next(e for e in self.orders if e.startswith(group_prefix))

    def promote(self):
        store, db = self.trader.broker_state_store, self.trader.journal_db

        def write(conn):
            gid = store.open_generation_in_tx(conn, ("account",), _et(11, 0))
            store.upsert_account_in_tx(conn, BrokerAccountRow(
                ACCOUNT, "paper", 100_000.0, None, None, None, None,
                {"DailyPnL:USD": str(self.daily_pnl)}, 1, _et(11, 0)))
            for conid, quantity in self.held.items():
                store.upsert_position_in_tx(conn, BrokerPositionRow(
                    ACCOUNT, conid, "AAPL", "STK", "SMART", "USD", quantity, 90.0, 100.0, quantity * 100.0,
                    0.0, 0.0, 0.0, quantity == 0, 1, _et(11, 0)))
            for entity, row in self.orders.items():
                if entity in self.hidden:
                    continue
                store.upsert_order_in_tx(conn, row)
                store.bind_alias_in_tx(conn, "perm_id", str(self.perm[entity]), ACCOUNT, "", entity, _et(11, 0))
            cursor = conn.execute("SELECT COALESCE(MAX(source_cursor), 0) FROM domain_event_journal").fetchone()[0]
            store.mark_generation_promoted_in_tx(conn, gid, int(cursor), _et(11, 0))
            return gid
        self.generations += 1
        return db.transaction(write)


class _Universe:
    def resolve_symbol(self, conid, **_kwargs):
        if conid not in (CONID, OTHER):
            return []
        return [SimpleNamespace(conId=conid, symbol="AAPL", secType="STK", exchange="SMART",
                                primaryExchange="NASDAQ", currency="USD")]


class _Composed:
    def __init__(self, tmp_path, loop_thread, clock, *, automation=False, sim=None):
        from trader.trading.command_stack import build_command_stack

        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        store = BrokerStateStore(db)
        store.migrate(migrator)
        trader = object.__new__(Trader)
        self.trader, self.clock, self.loop_thread = trader, clock, loop_thread
        self.sim = sim or _BrokerSim(trader)
        self.sim.trader = trader
        trader.journal_db, trader.domain_journal, trader.broker_state_store = db, journal, store
        trader.broker_ingest = _Ingest(self.sim)
        trader.risk_gate = RiskGate(RiskLimits(max_daily_loss=1000.0),
                                    event_store=SimpleNamespace(count_since=lambda **_k: 0))
        trader.universe_accessor = _Universe()
        trader.portfolio = SimpleNamespace(get_positions=lambda: [], get_portfolio_items=lambda: [])
        trader.book = SimpleNamespace(get_open_order_count=lambda: 0)
        trader.client = SimpleNamespace(ib=self.sim)
        trader.executioner = self.sim
        trader.ib_account = ACCOUNT
        trader.paper_trading = True
        trader._main_loop = loop_thread.loop
        trader.get_pnl = lambda: [SimpleNamespace(dailyPnL=self.sim.daily_pnl)]
        if automation:
            _enable_automation(trader, tmp_path)

        async def no_margin(*_a):
            raise RuntimeError("what-if is not part of this test")
        trader.check_order_margin = no_margin
        self.stack = build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                         now=lambda: clock[0])
        self.liquidation = self.stack.liquidation_service
        self.saga = self.stack.protective_order_saga

    def tick(self):
        """One production recovery tick: a coroutine on the trader loop awaiting the worker."""
        return self.loop_thread.run(self.liquidation.tick_async())

    def run_session(self, at):
        self.clock[0] = at
        return self.loop_thread.run(self.liquidation.run_async(self.stack.session_controller.run_due, at))

    def protected_entry(self, *, quantity=10.0, stop=95.0, target=120.0, command_id="entry-1", conid=CONID):
        """A durable PROTECTED saga and its working stop and target, as after a filled bracket."""
        og = f"og-{command_id}"
        state = SagaState(
            command_id=command_id, order_group_id=og, order_ref=f"mmr:{og}", state="PROTECTED",
            account_id=ACCOUNT, conid=conid, side="BUY", requested_quantity=Decimal(str(quantity)),
            filled_quantity=Decimal(str(quantity)), protection_quantity=Decimal(str(quantity)),
            protection_working=True, stop_working=True, target_working=True, revision=3,
            plan_json={"legs": [{"role": "stop", "stop_price": str(stop)},
                                {"role": "take_profit", "limit_price": str(target)}]})
        self.saga._persist(state, self.clock[0], from_state=None)
        self.sim.held[conid] = quantity
        self.sim.add_order(f"{og}:stop", og, "stop", "SELL", "STP", quantity, conid=conid, status="PreSubmitted")
        self.sim.add_order(f"{og}:take_profit", og, "take_profit", "SELL", "LMT", quantity, conid=conid)
        return og

    def saga_event(self, og, leg, entity, status, *, event_id, filled=0.0):
        return self.saga.on_broker_event(BrokerOrderEvent(og, leg, status, filled, 10.0, 1, event_id,
                                                          self.clock[0], order_entity_id=entity))

    def cancel_landed(self, *entities):
        """The broker applied the cancels: rows Cancelled, open trades gone."""
        for entity in entities:
            self.sim.set_status(entity, "Cancelled")


def _enable_automation(trader, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from trader.research.signing import public_key_pem

    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    (keys / "verify.pem").write_bytes(public_key_pem(ed25519.Ed25519PrivateKey.generate().public_key()))
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    trader.automation_enabled, trader.automation_live_enabled = True, False
    trader.automation_public_key_ring_path = str(keys)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    yield lt
    lt.stop()


@pytest.fixture
def composed(tmp_path, loop_thread):
    stack = _Composed(tmp_path, loop_thread, [_et(11, 0)])
    yield stack
    stack.liquidation.worker.shutdown()


@pytest.fixture
def automated(tmp_path, loop_thread):
    """Cold start with paper automation on, so the SELL close path is composed (R32)."""
    stack = _Composed(tmp_path, loop_thread, [_et(11, 0)], automation=True)
    yield stack
    stack.liquidation.worker.shutdown()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_cold_start_wires_registry_worker_saga_time_exit_reconciler_and_the_sell_close_path(automated):
    stack, trader = automated.stack, automated.trader
    assert trader.exit_owner_registry is stack.exit_owner_registry
    assert trader.liquidation_service is stack.liquidation_service
    assert trader.liquidation_worker is stack.liquidation_worker
    assert stack.session_controller._time_exit._liquidation is stack.liquidation_service
    assert automated.saga._liquidation._serialized is stack.liquidation_service
    assert stack.liquidation_service._service._protection is automated.saga
    assert stack.liquidation_service._service._refresh is not None
    assert stack.reconciler._closes is not None
    service = stack.automated_intent_service                # built on cold start (R32)
    assert service._liquidation is stack.liquidation_service and service._broker is not None


def test_hot_arm_builds_the_intent_service_with_the_close_path(tmp_path, composed):
    _enable_automation(composed.trader, tmp_path)
    service = composed.stack.paper_hot_arm._build_intent_service(composed.trader)
    assert service._liquidation is composed.stack.liquidation_service
    assert service._broker is not None


def test_the_stack_applies_the_sp1_migrations_and_keeps_attribution_on_32_to_34(composed):
    rows = dict(composed.trader.journal_db.execute("SELECT version, name FROM schema_migrations", fetch="all"))
    assert {35, 36, 37, 38} <= set(rows)
    assert all(rows[v].startswith("p3_") for v in (32, 33, 34))


# ---------------------------------------------------------------------------
# #23 / R10: two account producers, one root
# ---------------------------------------------------------------------------

def test_two_account_producers_make_one_root_one_reduce_and_release_the_owner(composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    first = composed.liquidation.start(ACCOUNT, "flatten-ui-1", _et(15, 59))
    state = composed.run_session(_et(15, 46))
    assert state.flatten_command_id == "flatten-ui-1" == first.cause_command_id
    assert [p[1] for p in composed.sim.placed] == ["MKT"]
    composed.sim.set_status(composed.sim.entity_for("flatten-ui-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.run_session(_et(15, 47))
    composed.sim.promote()
    state = composed.run_session(_et(15, 48))                   # the session polls the root it got back
    assert (state.state, state.flatten_command_id) == ("FLAT", "flatten-ui-1")
    assert composed.stack.exit_owner_registry.get("flatten-ui-1").state == "RELEASED"
    assert [p[1] for p in composed.sim.placed] == ["MKT"]


# ---------------------------------------------------------------------------
# R11 / #26 / #31: exits after a loss breach; the time exit keeps no live stop
# ---------------------------------------------------------------------------

def test_after_a_loss_breach_a_time_exit_hands_over_cancels_the_stop_and_reduces(composed):
    """R11 + spec 5.1: the entry gate refuses; the exit hands protection over, cancels it, then reduces."""
    og = composed.protected_entry()
    composed.sim.daily_pnl = -5000.0
    composed.sim.promote()
    from ib_async import Contract
    entry = composed.loop_thread.run(composed.trader.place_expressive_order(
        Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD"),
        "BUY", 5.0, {"order_type": "MARKET"}, algo_name="mmr:og-new"))
    assert "daily loss" in str(entry.error)
    composed.stack.session_controller._time_exit.request_exit(
        command_id="time-exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    owned = composed.saga.resume("entry-1")
    assert (owned.state, owned.expected_cancel_ids) == ("CLOSE_OWNED", (f"{og}:stop", f"{og}:take_profit"))
    assert sorted(composed.sim.cancelled) == [f"{og}:stop", f"{og}:take_profit"] and composed.sim.placed == []
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()
    assert composed.sim.placed == [("time-exit-1-reduce-265598-1", "MKT", "SELL", 10.0, None, None)]
    composed.sim.set_status(composed.sim.entity_for("time-exit-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("time-exit-1").state == "CLOSED"
    assert composed.saga.resume("entry-1").state == "CLOSED"
    assert composed.sim.trades == {}                             # no stop is left working
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


# ---------------------------------------------------------------------------
# #21: invisible child and late fill
# ---------------------------------------------------------------------------

def test_invisible_child_and_a_late_fill_never_send_a_second_reduce(composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    entity = composed.sim.entity_for("c-1-reduce")
    composed.sim.hidden.add(entity)
    composed.sim.promote()
    composed.trader.broker_ingest.ready = False  # a newer sync is staging: no complete enumeration yet
    composed.tick()                              # child unseen and not provably absent: UNKNOWN
    assert composed.liquidation.receipt_for("c-1").children[0].state == "UNKNOWN"
    composed.trader.broker_ingest.ready = True
    composed.sim.hidden.clear()
    composed.sim.set_status(entity, "Filled", filled=10.0)
    composed.sim.promote()                       # fill visible, position not yet updated
    composed.tick()
    assert [p[0] for p in composed.sim.placed] == ["c-1-reduce-265598-1"]
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("c-1").state == "CLOSED"
    assert len(composed.sim.placed) == 1


# ---------------------------------------------------------------------------
# #22 / #25 / spec 6: partial close of one of two protected positions
# ---------------------------------------------------------------------------

def test_partial_close_of_one_of_two_positions_reprotects_the_actual_remainder(composed):
    """Terminal partial fill (2 of 4), stop then target for the live 8, release, a late retired-leg event.
    The other position and its protection stay untouched; the breaker stays clear."""
    og = composed.protected_entry()
    other = composed.protected_entry(command_id="entry-2", conid=OTHER)
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    assert sorted(composed.sim.cancelled) == [f"{og}:stop", f"{og}:take_profit"]
    for leg in ("stop", "take_profit"):
        composed.cancel_landed(f"{og}:{leg}")
        composed.saga_event(og, leg, f"{og}:{leg}", "Cancelled", event_id=f"cancel-{leg}")
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    composed.sim.promote()
    composed.tick()                                                   # partial reduce of 4
    assert composed.sim.placed[-1][:4] == ("p-1-reduce-265598-1", "MKT", "SELL", 4.0)
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Cancelled", filled=2.0)
    composed.sim.held[CONID] = 8.0
    composed.sim.promote()
    composed.tick()                                                   # the partial fill is seen
    composed.sim.promote()
    composed.tick()                                                   # stop leg for the actual 8
    assert composed.sim.placed[-1] == ("p-1-reprotect-stop-265598-1", "STP", "SELL", 8.0, 95.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()                                                   # target after the stop is accepted
    assert composed.sim.placed[-1] == ("p-1-reprotect-target-265598-1", "LMT", "SELL", 8.0, 120.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("p-1").state == "DONE"
    released = composed.saga.resume("entry-1")
    assert (released.state, released.protection_quantity) == ("PROTECTED", Decimal("8"))
    late = composed.saga_event(og, "stop", f"{og}:stop", "Cancelled", event_id="late-original-stop")
    assert late.state == "PROTECTED"                                 # a retired leg, a new event id
    assert composed.saga.resume("entry-2").state == "PROTECTED"
    assert {f"{other}:stop", f"{other}:take_profit"} <= set(composed.sim.trades)
    assert all(entity.startswith(og) for entity in composed.sim.cancelled)
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


def test_target_leg_rejected_by_the_broker_escalates_to_a_full_close(composed):
    """#26 through the stack: a target that goes Inactive after its echo is a failed re-protect."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.tick()
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()                                        # stop leg
    composed.sim.ack["LMT"] = "Inactive"
    composed.sim.promote()
    composed.tick()                                        # target sent; the ack says Inactive
    composed.sim.set_status(composed.sim.entity_for("p-1-reprotect-target"), "Inactive")
    composed.sim.promote()
    composed.tick()
    receipt = composed.liquidation.receipt_for("p-1")
    assert receipt.escalated is True and receipt.goal == "zero"
    assert composed.sim.cancelled[-1] == composed.sim.entity_for("p-1-reprotect-stop")
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


# ---------------------------------------------------------------------------
# R20 / #19 / #20 / #24: restarts through the stack
# ---------------------------------------------------------------------------

def _restart(composed, tmp_path):
    """Stop the old worker, build a new stack over the same journal and the same broker."""
    composed.liquidation.worker.shutdown()
    return _Composed(tmp_path, composed.loop_thread, composed.clock, sim=composed.sim)


def test_crash_between_journal_and_broker_call_restarts_without_a_duplicate(tmp_path, composed):
    """The reduce child was journaled; the process died before the broker call. The restart
    proves it absent on a complete newer generation and sends one reduce, attempt 2."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()

    class _Crash(BaseException):
        pass

    def crash(*_args, **_kwargs):
        raise _Crash()
    composed.stack.liquidation_service._service._still_dispatchable = crash
    with pytest.raises(_Crash):
        composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    restarted = _restart(composed, tmp_path)
    composed.sim.promote()
    restarted.tick()                                       # fenced on the restart's newest generation
    composed.sim.promote()
    restarted.tick()                                       # complete and newer: proven absent
    composed.sim.promote()
    restarted.tick()                                       # newer than that observation: reduce
    assert composed.sim.placed == [("c-1-reduce-265598-2", "MKT", "SELL", 10.0, None, None)]
    restarted.liquidation.worker.shutdown()


def test_restart_after_the_order_left_continues_without_a_second_order(tmp_path, composed):
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    restarted = _restart(composed, tmp_path)
    composed.sim.set_status(composed.sim.entity_for("c-1-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    restarted.tick()
    composed.sim.promote()
    restarted.tick()
    assert restarted.liquidation.receipt_for("c-1").state == "CLOSED"
    assert len(composed.sim.placed) == 1
    assert restarted.stack.exit_owner_registry.get("c-1").state == "RELEASED"
    restarted.liquidation.worker.shutdown()


# ---------------------------------------------------------------------------
# #27: the session cancel phase, the flatten and its exact root
# ---------------------------------------------------------------------------

def test_session_cancel_keeps_protection_and_the_flatten_owns_it_until_flat(composed):
    og = composed.protected_entry()
    composed.sim.add_order("og-entry-2:entry", "og-entry-2", "entry", "BUY", "LMT", 5.0)
    composed.sim.promote()
    composed.run_session(_et(15, 35))
    assert composed.sim.cancelled == ["og-entry-2:entry"]
    assert composed.saga.resume("entry-1").state == "PROTECTED"
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"
    composed.cancel_landed("og-entry-2:entry")
    composed.sim.promote()
    state = composed.run_session(_et(15, 46))
    root = SessionController.flatten_command_id(ACCOUNT, FRIDAY)
    assert state.flatten_command_id == root
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    composed.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    composed.sim.promote()
    composed.run_session(_et(15, 47))                                 # the flatten reduces
    composed.sim.set_status(composed.sim.entity_for(f"{root}-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.run_session(_et(15, 48))
    composed.sim.promote()
    state = composed.run_session(_et(15, 49))
    assert (state.state, state.flatten_command_id) == ("FLAT", root)
    assert composed.saga.resume("entry-1").state == "CLOSED"


# ---------------------------------------------------------------------------
# #28 / R17 / R33: commands resolve from their root, through the reconciler only
# ---------------------------------------------------------------------------

def test_joined_flatten_command_resolves_only_through_the_reconciler(composed):
    """#28: the /flatten command that joined the session flatten resolves when FLAT is proven."""
    _register_production_actions(composed)
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    root = composed.run_session(_et(15, 46)).flatten_command_id
    receipt = composed.stack.coordinator.execute(CommandRequest(
        command_id="flatten-ui-1", action="liquidate_account", account_id=ACCOUNT, target_type="account",
        target_id=ACCOUNT, expected_version=None, body={"reason": "test"}, source="dashboard"))
    assert receipt.outcome["close_root_id"] == root
    composed.sim.set_status(composed.sim.entity_for(f"{root}-reduce"), "Filled", filled=10.0)
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    composed.tick()
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for(root).state == "FLAT"
    assert composed.stack.ledger.get("flatten-ui-1").state == "OUTCOME_UNKNOWN"   # the close never resolves it
    composed.stack.reconciler.run_due(composed.clock[0])
    assert composed.stack.ledger.get("flatten-ui-1").state == "RESOLVED"
    assert composed.stack.ledger.unresolved_for_account(ACCOUNT) == []
    assert composed.stack.exit_owner_registry.account_owner(ACCOUNT) is None


def _register_production_actions(composed):
    """The production RPC registry registers the coordinator actions (liquidate_account, intents)."""
    from trader.messaging.production_api import build_production_registry
    from trader.domain.feed_service import DomainFeedService
    from trader.domain.snapshot_service import DomainSnapshotService
    from trader.messaging.typed_rpc import HmacServiceAuthenticator

    build_production_registry(
        composed.trader, HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=DomainSnapshotService(composed.trader.domain_journal),
        feed_service=DomainFeedService(composed.trader.domain_journal), command_stack=composed.stack)


def _sell(automated, *, requested=None):
    """A SELL intent through the real coordinator and intent service; only the bundle check is faked."""
    from tests.automation.test_automated_command_boundary import (
        FakeArtifactVerifier, intent_to_request_body, make_intent,
    )
    _register_production_actions(automated)
    service = automated.stack.automated_intent_service
    service._verifier = FakeArtifactVerifier()
    service._bundle_evidence_validator = None
    intent = make_intent(side="SELL", requested_quantity=None if requested is None else Decimal(str(requested)))
    receipt = automated.stack.coordinator.execute(CommandRequest(
        command_id=intent.command_id, action="execute_automated_intent", account_id=ACCOUNT,
        target_type="intent", target_id=intent.intent_id, expected_version=None,
        body=intent_to_request_body(intent, bundle_digest="sha256:manifest-ok"), source="strategy_service"))
    return receipt, intent


def test_a_sell_intent_closes_end_to_end_and_resolves_from_its_root(automated):
    """R21 / #28: SELL -> scoped close -> reduce-only order -> CLOSED -> reconciler RESOLVED."""
    automated.sim.held[CONID] = 10.0
    automated.sim.promote()
    receipt, intent = _sell(automated)
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert automated.sim.placed == [(f"{intent.command_id}-reduce-265598-1", "MKT", "SELL", 10.0, None, None)]
    automated.sim.set_status(automated.sim.entity_for(f"{intent.command_id}-reduce"), "Filled", filled=10.0)
    automated.sim.held[CONID] = 0.0
    automated.sim.promote()
    automated.tick()
    automated.sim.promote()
    automated.tick()
    assert automated.liquidation.receipt_for(intent.command_id).state == "CLOSED"
    automated.stack.reconciler.run_due(automated.clock[0])
    row = automated.stack.ledger.get(intent.command_id)
    assert (row.state, row.outcome["liquidation_state"]) == ("RESOLVED", "CLOSED")


def test_a_partial_sell_that_sells_nothing_is_protected_again_and_rejected(automated):
    """R25 / R2-2: the reduce is rejected; protection comes back; the command fails with an alert."""
    og = automated.protected_entry()
    automated.sim.promote()
    receipt, intent = _sell(automated, requested=4)
    root = intent.command_id
    automated.cancel_landed(f"{og}:stop", f"{og}:take_profit")
    automated.sim.promote()
    automated.tick()                                                   # partial reduce of 4
    automated.sim.set_status(automated.sim.entity_for(f"{root}-reduce"), "Inactive")
    automated.sim.promote()
    automated.tick()                                                   # rejected: re-protect the untouched 10
    assert automated.sim.placed[-1][1:4] == ("STP", "SELL", 10.0)
    for _ in range(3):
        automated.sim.promote()
        automated.tick()                                               # target, then both legs working
    assert automated.liquidation.receipt_for(root).state == "REDUCE_FAILED"
    assert automated.saga.resume("entry-1").state == "PROTECTED"
    automated.stack.reconciler.run_due(automated.clock[0])
    row = automated.stack.ledger.get(root)
    assert (row.state, row.error_code) == ("REJECTED", "REDUCE_FAILED")


def test_a_waiting_close_asks_for_a_broker_sync_through_the_trader_loop(composed):
    """Ruling 1 in composition: the wait triggers run_broker_sync, which promotes a newer generation."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.sim.auto_refresh = True
    before = composed.sim.generations
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    composed.loop_thread.run(asyncio.sleep(0.05))
    assert composed.trader.broker_ingest.syncs == 1
    assert composed.sim.generations == before + 1


def test_generation_refresh_is_rate_limited_and_never_blocks():
    """Ruling 1: the close asks for a newer broker generation while it waits."""
    from trader.trading.command_stack import _BrokerGenerationRefresh

    loop_thread = _LoopThread()
    try:
        syncs, tick = [], [100.0]

        async def sync(client):
            syncs.append(client)
            return True
        trader = SimpleNamespace(_main_loop=loop_thread.loop, client="ib",
                                 broker_ingest=SimpleNamespace(run_broker_sync=sync))
        refresh = _BrokerGenerationRefresh(trader, min_interval_seconds=5.0, clock=lambda: tick[0])
        refresh.request_refresh(ACCOUNT)
        refresh.request_refresh(ACCOUNT)          # inside 5 s: skipped
        tick[0] = 106.0
        refresh.request_refresh(ACCOUNT)
        loop_thread.run(asyncio.sleep(0.05))
        assert syncs == ["ib", "ib"]
        trader._main_loop = None
        refresh.request_refresh(ACCOUNT)          # no loop: nothing, no error
    finally:
        loop_thread.stop()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_safe_close_integration.py -q --timeout=60`
Expected: 13 failed, 3 passed — the wiring tests (`stack.reconciler._closes is None`, the saga's protection is not attached, the intent service has no `_liquidation`), the refresh tests (`ImportError: cannot import name '_BrokerGenerationRefresh'`), and every close that relies on the saga hand-over or on the reconciler. Three already pass: the migrations, the invisible-child case and the crash between journal and send pin state-machine rules from Tasks 4–8 through the composed stack.

- [ ] **Step 3: Wire `build_command_stack`**

1. Add `import time` to the module imports, and add before `_LiquidationBreaker`:

```python
class _BrokerGenerationRefresh:
    """Ask for a newer complete broker generation without waiting for it.

    A promoted generation only changes when ``run_broker_sync`` runs, which
    today is only at (re)connect. The close needs newer generations to see
    absence and fresh positions (R4, R5), so it asks for one while it waits.
    """
    def __init__(self, trader, *, min_interval_seconds: float = 5.0,
                 clock: Optional[Callable[[], float]] = None):
        self._trader = trader
        self._min_interval = min_interval_seconds
        self._clock = clock or time.monotonic
        self._last: Optional[float] = None

    def request_refresh(self, account_id: str) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self._min_interval:
            return
        loop = getattr(self._trader, "_main_loop", None)
        sync = getattr(getattr(self._trader, "broker_ingest", None), "run_broker_sync", None)
        if loop is None or not loop.is_running() or sync is None:
            return
        self._last = now
        future = asyncio.run_coroutine_threadsafe(sync(self._trader.client), loop)
        future.add_done_callback(_log_refresh_failure)


def _log_refresh_failure(future) -> None:
    import logging
    if not future.cancelled() and future.exception() is not None:
        logging.getLogger(__name__).warning("broker generation refresh failed: %s", future.exception())
```

2. `OutcomeReconciler(` gets `closes=liquidation_store,`.
3. The inner `LiquidationService(` gets `refresh=_BrokerGenerationRefresh(trader),`.
4. After `protective_order_saga = ProtectiveOrderSaga(...)`:

```python
    # The saga is the protection port of every close and the source of unhandled failures.
    liquidation_service.attach_protection(protective_order_saga)
```

5. `_build_automated_intent_service`: add the keyword parameter `liquidation: Any = None` and pass `liquidation=liquidation, broker=broker,` into `AutomatedIntentCommandService(...)`. At both call sites (the cold-start call and `_build_intent_for_hot_arm`) pass `liquidation=liquidation_service,`.
6. `CommandStack`: add `exit_owner_registry: Any = None  # ExitOwnerRegistry (SP1 safe close)`; pass `exit_owner_registry=exit_owner_registry`; next to `trader.liquidation_service = liquidation_service` add `trader.exit_owner_registry = exit_owner_registry`.

After this task the whole composition reads:

```python
    liquidation_worker = LiquidationWorker()
    liquidation_service = SerializedLiquidation(
        LiquidationService(
            broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
            store=liquidation_store, registry=exit_owner_registry, now=now,
            breaker=_LiquidationBreaker(circuit_breaker, now),
            journal=journal, ledger=ledger,
            schedule_reconcile=lambda command_id: reconciler.schedule(command_id, now()),
            refresh=_BrokerGenerationRefresh(trader),
        ),
        liquidation_worker, account_id=trader.ib_account, now=now,
    )
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`
Expected: all PASS (16 in the new file). Watch `tests/test_production_rpc_security.py`, `tests/test_web_dashboard.py` and `tests/test_command_stack.py`: they build the stack with fakes; the new pieces only need `journal_db` and `ib_account`, which those fakes already have.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_stack.py tests/test_safe_close_integration.py
git commit -m "feat: wire exit ownership, the liquidation worker and close resolution into the command stack

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Out of scope for this plan (owned by later plans)

- The `ai_paper` `CLOSE` / `PARTIAL_CLOSE` decisions and their admission rules (plan 3). They call the same `start(scope="conid", ...)`.
- The kill line (plan 4) claims the account owner through `start(scope="account")`, as `/flatten` already does (`liquidate_account` in `trader/messaging/production_api.py` → `LiquidationService.liquidate`).
- Wiring a live caller for `SessionController.on_bar` (SP2).
- The real IB paper session that proves `ocaType=2`, the generation-refresh load and the reduce-only path against IB (plan 6, acceptance harness, #35). A green fake is not that proof.
- The old-path regression "after a loss breach a new entry is refused and a safe close is allowed" is **in scope now** (master's unit test from PR #42, Task 12 for a SELL intent against a breached session risk, Task 13 through the stack). Plan 3 adds the `ai_paper` variant on top.
- Gate carried to plan 3 (#31): owner-owned risk ceilings stay hard trader constants (`session_risk` `MAX_DAILY_LOSS_FRACTION`, `MAX_DRAWDOWN_FRACTION`, allocation ceilings); a model policy may only tighten them, and dispatch re-checks a tightened limit. Nothing in this plan changes those constants.
- Whether IB shrinks the children of a bracket whose parent was partly filled and then cancelled is not proven here; Task 16 closes the conid when they are bigger than the position. Plan 6 (#35) should observe it in the paper session.

## Self-review notes (done while writing)

- Alignment with master (PR #42): every task's code was applied again on a scratch worktree of this branch (on `15f9e715`) in the execution order above, one commit per task, with the round-2 verification fixes folded into Tasks 4 and 9. For each task the changed test files were run against the previous task's code and after the task; the Step 2 and Step 4 counts above come from those runs. Baseline on master: 5273 passed, 23 skipped. Final state: 5517 passed, 23 skipped (`.venv/bin/python -m pytest tests/ -q --timeout=30 --ignore=tests/test_ibrx_async.py -p no:cacheprovider`).
- Round 2: every task's code was applied on a scratch worktree of this branch in the execution order above, one commit per task. Each new test was run against the previous task's code (the "Expected" failure counts in Step 2 come from those runs) and after the task (the pass counts). The full suite passed at Task 4 (5264 passed), Task 8 (5368 passed) and on the final state (5460 passed, 23 skipped). Baseline before the plan: 5205 passed, 23 skipped. Those counts are from before PR #42; the alignment line above has the current ones.
- The four race tests of Task 9 were also run with the revision check removed from `save_in_tx`: all four fail, so they test the check.
- Spec 5.1 coverage: problem list (Tasks 1, 2, 14, 15); scope and goal (Tasks 4–6); protection ownership (Task 9); breaker signals (Task 4 `_trips_breaker`, Task 6 escalation, R29); one execution owner, goal upgrade, account owner first, supersede, exact-root polling (Tasks 3, 4, 7, 8, 11); flatten order and the unknown-child rule (Tasks 4, 7, R22, R23); exit-only OCA, recovery, `DONE`/`CLOSED` meaning (Tasks 6, 10, 18, R25, R26, R30; rulings 23, 24); users of the safe close (Tasks 11, 12, 16); SELL must prove a reduction (Task 12). Section 5.5 step 2 (kill = account owner): Task 4 `start(scope="account")`.
- Spec section 6 "Safe close" list → tests: time exit leaves the stop live (T1/T11 `test_time_exit_leaves_no_live_stop_after_the_position_is_closed`, T13 `test_after_a_loss_breach_a_time_exit_hands_over_cancels_the_stop_and_reduces`); lost acknowledgement (T4 `test_timeout_after_the_boundary_stays_unknown_and_is_never_resent`); partial fill (T6 `test_terminal_partial_fill_reprotects_the_actual_remainder`, T13); cancel rejected (T5 `test_cancel_rejected_by_the_broker_ends_failed_safe_without_reduce`); re-protect failure and missed deadline (T6); partially close one of two protected positions, the other untouched, breaker clear (T13 `test_partial_close_of_one_of_two_positions_reprotects_the_actual_remainder`); unrequested stop cancel (T9); routine progress (T5); time exit + AI close (T8); time exit during partial (T8); flatten during partial (T7); kill during REPROTECTING (T7); invisible child (T4, T5, T7, T13); full close during flatten (T8); old FAILED_SAFE root (T4); exact-root polling (T11, T13); exit OCA cases (T6, T10, T13); production composition (T13).
- R-rule coverage: R1/R2/R3 (Task 4 `_reserve`, `_send`), R4 and R22 (`_evidence`, Task 18), R5 and R23 (`_blocking`, `_fresh`, `_cancel_targets`), R6 (claims and `_finish` in one transaction; failure-injection tests in Tasks 6, 7, 8), R7 (`_check_dispatchable_in_tx`; Tasks 4, 6), R8 and R24 (`_finish`, `_cleanup`; Tasks 4, 6, 8), R9 (`inherit_children_in_tx`; Tasks 4, 6, 7), R10 (Tasks 4, 11, 13), R11 and R35 (Tasks 14, 10), R12 (Task 15, ruling 8), R13 (Tasks 6, 10, 14, 18), R14 and R27/R28 (Task 9), R15 and R36 (Task 6), R16 and R37 (Task 16), R17 and R33 (Tasks 4, 12, 17, 13), R18 (Task 1), R19 (Task 4), R20 (Tasks 4, 6, 7, 8, 13), R21 (Task 13), R25/R26/R30/R31 (Task 6), R29 (Tasks 4, 9, 15), R32 (Task 12), R34 (Tasks 4, 14), R38 (all).
- Names used across tasks were checked against the frozen blocks of Tasks 3, 4 and 18: `ChildRef` (with `sent_generation`, `order_entity_id`), `LiquidationReceipt`, `JoinRow`, `CloseResolution` (with `error_code`), `CancelTarget`, `HandoverInfo`, `DispatchRefused`, `LiquidationRefused`, `RunStateError`, `LiquidationRunStore.*_in_tx`, `ExitOwnerRegistry.*_in_tx`, `liquidation_child_id`, `reprotect_oca_group`, `LiquidationDispatchPort.enumeration_complete/newest_generation`, `ProtectionOwnershipPort.expect_reprotect/release_after_partial(…, stop_status, target_status)`, `LiquidationService.start/rescan/receipt_for/root_for/close_resolution/upgrade_to_zero/attach_protection/liquidate`, `SerializedLiquidation`, `LiquidationWorker`, `Trader.place_reduce_only_order`, `TradingRuntimeOrderDispatch.reduce_position/reduce_partial/place_exit_leg/cancel_on_loop/enumeration_complete/newest_generation`, `_LiquidationDispatch(dispatch, orders_view)`, `SessionTimeExitAdapter(liquidation, *, account_id, now, deadline_seconds)`, `LiquidationBusy`, `REDUCE_ONLY_REFUSED`, `AutomatedIntentCommandService(liquidation=, broker=)`, `OutcomeReconciler(closes=)`, `SagaRevisionConflict`.
- Review round 2 mapping (every verification item and reviewer comment → task and test, or why it is not fixed): `.superpowers/sdd/2026-10-05-ai-paper-sp1-plan1-safe-close/round2-mapping.md`.
