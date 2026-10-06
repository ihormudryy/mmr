# AI Paper SP1 — Plan 1: Safe Close — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Run the tasks in the "Execution order" below, not in number order.** The tasks appear in this file in execution order.

**Goal:** Make every exit on the paper path go through one broker-verified close service. It hands protection over before it cancels a stop, owns one close per position, re-protects after a partial close with a linked stop/target, journals every child order before it is sent, and never sends a reduce while a child order is unknown.

**Architecture:** `LiquidationService` gains a `scope` (`account` | `conid`) and a `goal` (`zero` | `partial`). Runs, child orders and "which command joined which root" are journal rows (migration 36). A new `ExitOwnerRegistry` (migration 35) holds at most one active close per `(account, conid)` and one account flatten per account; a claim and the run it creates commit in one transaction. `ProtectiveOrderSaga` gets a `CLOSE_OWNED` state with the exact order ids the close will cancel (migration 37). All exits use one reduce-only order path on `Trader` that does not run the entry gates. Every `LiquidationService` entry point runs on one worker thread, never on the IB event loop. Time exits, one-strategy SELL intents, the session flatten, `/flatten` and protective failure all use this service. Commands that join a root resolve from that root.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DomainJournal`), dataclasses, `concurrent.futures`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, section 5.1 (plus the bindings in 5.5 step 2 and the test list in section 6, "Safe close"). This plan is delivery step 1 of section 7. Steps 2–6 get their own plans.

## Global Constraints

- No live authority. Nothing here may run on a live account; `account_mode` checks stay as they are.
- One broker dispatch boundary: every order goes through `TradingRuntimeOrderDispatch` → `Trader`. Entries use `Trader.place_expressive_order`; every exit (full reduce, partial reduce, re-protect leg) uses the new `Trader.place_reduce_only_order`. There is no second IB order path and no "skip checks" flag.
- `CommandReceipt` stays frozen. `ExecutionIntent` is not changed.
- `OUTCOME_UNKNOWN` is never resubmitted under a fresh id. An `UNKNOWN` child order is never sent again (R3).
- The trader journal (`trader.journal_db`) is the source of truth. No in-memory receipt is authoritative: the service reads runs, children and owners from the journal on every step.
- Journal migrations: **35** exit owners, **36** liquidation run columns + `liquidation_children` + `liquidation_joins`, **37** saga close-ownership columns + `automated_order_saga_groups`. Versions 30 and 31 are taken, and **32–34 belong to `trader/data/attribution_store.py`**. `SchemaMigrator` records a version once, so reusing a taken number silently skips the new DDL. Free after this plan: 38–39, 46–49, 54+.
- Command ids and child ids never contain `:` (child ids become IB `orderRef` values via `encode_order_ref`).
- Routine progress of a scoped close never trips the breaker. Account-scope breaker behaviour is unchanged (every state except `FLAT` trips).
- Test-first. Each task begins with a failing test. Run tests with `.venv/bin/python -m pytest <path> -q --timeout=30`. The full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects follow the repo style (`feat:`, `fix:`, `test:`, `refactor:`), lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Old-path entry admission and risk ceilings do not change. The only old-path behaviour changes are: time exits and SELL intents use the scoped close; the session cancel phase cancels unfilled entries only; every exit skips the entry gates (R11).
- Nothing calls `SessionController.on_bar` live yet (checked: no caller in `trader/`). The time-exit bug is therefore latent, but the adapter is fixed here so the first live caller (SP2) is safe.

## Execution order

New tasks 14–17 must run before some old ones. Run the tasks in this order (each row lists what it needs):

1. **Task 1** — red test for the time-exit bug (needs nothing).
2. **Task 2** — child ids and leg classification (needs nothing).
3. **Task 3** — exit owner registry, migration 35.
4. **Task 4** — data model (frozen interfaces), migration 36, account scope on write-ahead children (needs 2, 3).
5. **Task 14** — reduce-only order path at the broker boundary (needs 4 for `DispatchRefused`).
6. **Task 15** — one serialized liquidation worker (needs 4, 14).
7. **Task 5** — scoped full close (needs 4).
8. **Task 6** — partial close, re-protect, escalation (needs 5).
9. **Task 7** — account flatten takes over scoped closes (needs 6).
10. **Task 8** — scoped claims: join, upgrade, refuse (needs 6, 7).
11. **Task 9** — protective saga `CLOSE_OWNED`, migration 37 (needs 4 for `HandoverInfo`, `CancelTarget`).
12. **Task 10** — exit legs by order identity (needs 14).
13. **Task 16** — session cancel phase cancels entries only.
14. **Task 11** — time exits use the scoped close; the session polls its returned root (needs 1, 5, 15).
15. **Task 12** — SELL intents become a proven-reduction close (needs 6, 8).
16. **Task 17** — joined commands resolve from their exact root (needs 4, 12).
17. **Task 13** — production composition and the end-to-end tests (needs all).

## Review Focus

Spec-implied inputs no test list in the spec names, and the review-round-1 traces. Each has a named test.

1. **Short positions.** A scoped close of a short reduces with `BUY`; a partial re-protects with the stop *above* the price. → Task 5 `test_full_close_of_short_reduces_with_buy`, Task 6 `test_partial_close_of_short_reprotects_above_price`.
2. **Partial quantity.** `q < 1` refused, `q ≥ |position|` refused at start, less than one share left = full close, live position `≤ q` at dispatch = full close of the remainder (R15). → Task 6 `test_partial_quantity_edge_cases`, `test_live_position_at_or_below_q_at_dispatch_is_fully_closed`.
3. **Position already gone.** The stop filled during the cancel race: the close ends `CLOSED` with no reduce. → Task 5 `test_close_ends_closed_without_reduce_when_the_stop_filled_in_the_cancel_race`.
4. **Root rebinding.** A root id or a joined command id cannot be rebound to another scope, conid or goal. → Task 5 `test_start_refuses_rebinding_root_to_another_scope`, Task 8 `test_joined_request_retried_after_restart_returns_the_same_root`.
5. **Restart in the middle of re-protect.** The restart sends only the planned target, never the stop again. → Task 6 `test_recovery_after_restart_sends_only_the_planned_target`.
6. **Invisible child / late fill.** No second reduce while a child is not visible yet, or when its fill arrives after the position was captured. → Tasks 4, 5, 7, 13.
7. **Two account producers.** One root, one reduce. → Task 4, Task 13.
8. **Exits after a loss breach.** The reduce-only path passes while a new entry is refused. → Task 14, Task 13.
9. **Event loop.** A tick awaited on the trader loop does not deadlock; a blocking call on the loop is refused. → Task 15.
10. **The generation refresh (new behaviour).** The close asks for a fresh broker sync while it waits (ruling 1). This adds `run_broker_sync` calls during a session; `capture` raises `GENERATION_STAGING` while a sync is staging, so every reader of the broker snapshot can see short "unavailable" windows. Check this against the paper session in plan 6.


## Design amendments (review round 1)

Two reviews on tickets #16–#29 (2026-10-06) found that the first version of this plan could send a second reduce, lose ownership in a crash, and cancel protection before a lower gate refused the exit. The findings were checked against the code and accepted. The rules below are binding. Every task follows them. Where a task's text disagrees, these rules win.

**Children and evidence**

- **R1. Write-ahead children.** Every child order (cancel, reduce, re-protect stop, re-protect target) is written to the journal **before** the broker call, with its own child id and a submission fence (the broker generation at send time). Child id: `{root}-{kind}-{conid}-{attempt}` (no `:`). The OCA pair journals both leg intents before either leg is sent.
- **R2. Child states.** `UNKNOWN` (written, outcome not proven), `WORKING`, `FILLED`, `CANCELLED`, `REJECTED`, `ABSENT`, `NOT_SENT`. `NOT_SENT` only for a proven pre-submit refusal (the call raised or returned a typed refusal before the broker boundary was crossed). A timeout or any other exception after the boundary is `UNKNOWN`.
- **R3. Never resubmit an unknown.** An `UNKNOWN` child is never sent again, under the same id or a new one. Only a `NOT_SENT` attempt may be followed by a new attempt, and that attempt gets a new id (`attempt + 1`). This is spec section 4 ("never resubmitted under a fresh id") applied per child.
- **R4. Absence needs positive evidence.** A child becomes terminal only when a broker generation newer than its fence shows it `Filled`, `Cancelled`, `ApiCancelled`, `Inactive` or `Rejected`, or when a complete, fenced broker enumeration newer than its fence proves it absent (the same standard `OutcomeReconciler._reconcile_approve` uses). An empty `find_orders` result means `UNKNOWN`. Inherited re-protect legs follow the same rule.
- **R5. Reduce rule.** No reduce and no OCA placement while any child of the root is `UNKNOWN`, or while a reduce child is `WORKING`. A further reduce needs a position snapshot whose generation is newer than the terminal observation of every earlier reduce child, and it is sized from that snapshot. A deadline with any child still `UNKNOWN` ends `FAILED_SAFE` with no new order.

**Ownership and durability**

- **R6. One transaction.** Exit owner rows, liquidation runs, goals, cursors, `SUPERSEDED` marks and child rows live in the trader journal. Claim plus run creation, goal upgrade plus cursor change, account claim plus supersede plus child inheritance, and every terminal transition each happen in **one** journal transaction. No in-memory receipt is authoritative.
- **R7. Re-read before dispatch.** Before every broker call the service re-reads the run and its owner from the journal. It dispatches only if the owner is `ACTIVE`, the run is neither terminal nor `SUPERSEDED`, and the goal still matches. `_set` refuses to overwrite `SUPERSEDED` or any terminal state.
- **R8. Durable cleanup.** A terminal transition sets `cleanup_pending` in the same transaction. Cleanup (saga hand-back or close, owner release) is idempotent, and recovery loads every run with `cleanup_pending` and finishes it. Account `FLAT` closes every saga the flatten owns and releases the account owner.
- **R9. Inheritance after failure or takeover.** A new owner of a conid or of the account inherits every non-terminal child of any `SUPERSEDED` or `FAILED_SAFE` root on that scope, and obeys R5 for them. `FAILED_SAFE` marks the owner row `FAILED_SAFE` (never "released as flat"); a later claim may start a new root, but that root inherits the old unknown children first.
- **R10. Account join.** `start(scope="account")` claims before it creates a run. `JOINED_FLATTEN` returns the existing root's receipt and creates nothing. Every account producer (session flatten, `/flatten`, protective failure, later the kill) persists and polls the root it got back, including after a join.

**Broker boundary and threading**

- **R11. One reduce-only path.** A new method on `Trader` at the existing order boundary sends reduce-only orders: full reduce, partial reduce and the exit OCA legs. It checks the account fence, that the side reduces the broker position, and that the quantity is at most the position. It does **not** run the entry gates (`RiskGate` daily loss, open orders, rate, concentration, leverage). There is no caller flag to skip checks. `reduce_position` (account flatten) moves onto it too. This also fixes a bug in today's code: an account flatten after a daily-loss breach can be refused by `RiskGate`. The after-breach regression test belongs to this plan, not to plan 3.
- **R12. One serialized worker.** Every `LiquidationService` entry point (`start`, `rescan`, `upgrade_to_zero`, `supersede`) runs on one dedicated single-thread worker, never on the IB event loop. The trader_service recovery loop, the session controller loop and the ingest thread (protective failure → `start`) submit to that worker and await it. Dispatch from the worker uses `run_coroutine_threadsafe` onto the main loop, which is then safe. This fixes a second bug in today's code: `rescan()` runs on the loop it waits on (`trader/trader_service.py` `_liquidation_recovery_loop`). Tested with a real asyncio loop, not `asyncio.run` stand-ins.
- **R13. OCA by identity.** A local `Trade` echo (`PendingSubmit`) is not acceptance. Events are matched by order identity. The target leg is sent only after the stop's state is reconciled: if the stop already filled in part or in full, the target is sized from the live remaining position; if the stop is `Inactive` or `Rejected`, escalate. `DONE` compares the outstanding quantity (`total_quantity − filled_quantity`) of each leg with the remaining position, and checks the same OCA group, the protective side and a working status.

**Protection**

- **R14. Exact hand-over.** `CLOSE_OWNED` stores the exact refs the close will cancel and the protection generation. Only `Cancelled` / `ApiCancelled` for those refs is suppressed. `Inactive`, `Rejected`, or a cancel of any other ref still takes today's incident path. Active legs are tracked apart from historical aliases; an event for a retired leg never changes current protection. Account takeover transfers `CLOSE_OWNED` sagas to the account root.

**Quantities**

- **R15. Partial quantity.** `q = floor(requested)`. `q < 1` → refused `PARTIAL_QUANTITY_INVALID`. `q ≥ |position|` at `start` → refused `QUANTITY_ABOVE_POSITION` (send `CLOSE` instead). `|position| − q < 1` → full close. Right before dispatch the rule is applied again to the live position; if the live position is now `≤ q`, the remainder is fully closed (ruling: protection is already cancelled, so refusing would leave it unprotected; the spec is silent). After a terminal partial fill the close re-protects the **actual** remaining quantity from a fresh snapshot; if it is zero the close ends `CLOSED`.

**Session controller and commands**

- **R16. Cancel entries only.** The session cancel phase cancels only working entry orders with zero fill. It never cancels the protective children of a filled position. A partly filled entry is left to the flatten, which owns its protection.
- **R17. Joined commands resolve.** A command that joins or upgrades another root records `(command_id, root_id)` in the journal (migration 36). `OutcomeReconciler` gets an `execute_automated_intent` branch: `CLOSED` / `DONE` on the exact root resolves it; `SUPERSEDED` follows to the superseding root; `FAILED_SAFE` or unknown never becomes success. A root that ends before the command records `CLOSE_PENDING` is handled too.

**Tests**

- **R18.** Task 1's red test uses today's constructor (`SessionTimeExitAdapter(dispatch)`) with a fake broker that keeps a working stop, and asserts the behaviour (no working stop after the position is closed). It is not a constructor `TypeError`.
- **R19.** Task 4's rescan test asserts that the new root advanced and the old root stayed `FAILED_SAFE`. `FLAT` is asserted only after a newer empty snapshot and a terminal reduce child.
- **R20.** Crash-injection tests restart at every boundary: after a child is journaled and before the broker call; after the broker accepted and before the ack is stored; after a goal upgrade; after a supersede; after a terminal save and before each cleanup step.
- **R21.** Task 13 calls the real `build_command_stack` with a temporary DuckDB journal, the real registry, run store, saga, coordinator, risk gates and `TradingRuntimeOrderDispatch` over a fake IB client, on a real asyncio loop with the R12 worker. Only broker and market ports are fake. It covers cold start and hot-arm wiring and every integration case listed on #29.


### Rulings made while rewriting

The rules above were checked against the real code while the tasks were rewritten. Where a rule could not be applied as written, or two rules met, the smallest spec-consistent option was chosen. Each ruling is binding like the rules.

1. **The fence is the promoted generation, and the close asks for a new one.** A promoted broker generation changes only when `BrokerIngest.run_broker_sync` runs, and today that is only at (re)connect (`trader/trading/trading_runtime.py:894` and `:908`). With R1/R4/R5 as written, every close would wait for a newer generation that never comes in a session and end `FAILED_SAFE`. So the fence stays `BrokerRiskSnapshot.generation_id` (a complete enumeration, as R1 says), and the service calls a non-blocking `GenerationRefreshPort.request_refresh` whenever it waits. Task 13 wires it to `run_broker_sync`, at most once every 5 seconds. `source_cursor` is not used as a fence, because our own journal writes also move it.
2. **Absence needs the second newer generation.** R4 accepts "a complete, fenced broker enumeration newer than its fence". The first newer generation can have started between our snapshot and our send, so it can miss the order. An empty lookup therefore becomes `ABSENT` only when `generation >= fence + 2`; on `fence + 1` the child stays `UNKNOWN`. An `ABSENT` child counts as "may have filled".
3. **Terminal needs a newer generation; WORKING does not.** A terminal status counts only when `generation > fence` (R4). A `Submitted`/`PreSubmitted`/`PendingCancel` row makes a child `WORKING` at once. `PendingSubmit` and `ApiPending` keep it `UNKNOWN` (R13: a local echo is not acceptance).
4. **R5 covers every fill, not only reduce children.** A reduce, a re-protect leg, `CLOSED`, `DONE` and `FLAT` need a snapshot whose generation is newer than the last observation of *every* child of the root that filled or may have filled. That includes a cancel target (the stop) that filled before our cancel landed: it changes the position exactly like a reduce. A cancel target that ended with no fill puts no limit on sizing.
5. **New child state `PLANNED`.** R1 journals both OCA leg intents before either is sent; R13 sends the target only after the stop is reconciled; R3 never re-sends an `UNKNOWN` child. So a written but not yet sent target needs its own state: `PLANNED`. It becomes `UNKNOWN` in one transaction just before its broker call. When a root stops (terminal, `SUPERSEDED`, goal upgrade, escalation) its `PLANNED` children become `NOT_SENT`. `PLANNED` never blocks a reduce.
6. **Child ids and the spec's re-protect refs.** The R1 child id `{root}-{kind}-{conid}-{attempt}` is also the decoded order ref. So spec 5.1's refs `{root}-reprotect-stop` / `{root}-reprotect-target` / OCA group `{root}-reprotect` become `{root}-reprotect-stop-{conid}-{n}`, `{root}-reprotect-target-{conid}-{n}` and `{root}-reprotect-{conid}-{n}`. A leg tried again after `NOT_SENT` gets attempt `n+1` and keeps its pair's OCA group. For cancels, `attempt` counts the cancels of that root on that conid.
7. **Cancel evidence is the target order.** IB attaches no order ref to a cancel, so a cancel child is reconciled from the row of the order it cancels (`get_order(order_entity_id)`), never from `find_orders(child_id)`. `CancelUnresolved` (no live order was found, nothing was sent) is a proven pre-submit refusal and makes the child `NOT_SENT`. A cancel the broker does not apply leaves the child `WORKING`; it is not re-sent, and the deadline ends `FAILED_SAFE` (spec test "cancel rejected").
8. **R12 and the ingest thread.** The protective-failure producer runs on the broker ingest thread while `_apply_batch` holds `_apply_lock` (`trader/trading/broker_ingest.py:677`). The worker's first step, `broker_snapshot.capture` → `_broker_ready` → `BrokerIngest.is_ready`, takes the same lock (`broker_ingest.py:451-453`). If the ingest thread waited for the worker, both would block for ever (today the same call already blocks the ingest thread on itself). So the ingest producer queues `start` on the worker and does not wait (`SerializedLiquidation.nonblocking()`). It stays durable: the saga row is `SAFETY_FAILED`, and every worker tick starts a flatten for each `SAFETY_FAILED` saga that has no liquidation join row. The recovery loop and the session loop *await* the worker; RPC threads block on it; a blocking call on an event loop raises.
9. **Cleanup order.** R8 cleanup is: (1) the saga step; (2) owner release plus clearing `cleanup_pending`, in one transaction; (3) resolving the commands of the root. Step 3 is not a durable cleanup step: if it is cut off, the R17 reconciler branch (Task 17) resolves the command, because `rescan_on_startup` requeues every `OUTCOME_UNKNOWN` row. `FAILED_SAFE` releases nothing: the owner row becomes `FAILED_SAFE` in the terminal transaction and the sagas stay `CLOSE_OWNED` until the next owner takes them.
10. **Partial size at `start` and at the SELL intent.** R15 refuses `q ≥ |position|` at `start` with `QUANTITY_ABOVE_POSITION`. The SELL-intent path (Task 12) turns `requested == held` into a full close (`quantity=None`) before it calls `start`, because spec 5.1 says "a close takes the broker quantity"; only `requested > held` is `NOT_A_REDUCTION`. Both follow the spec.
11. **R14 refs are order entity ids; the protection generation is a counter.** The exact refs `CLOSE_OWNED` stores are the broker order entity ids the close cancels. `BrokerOrderEvent` gains `order_entity_id`, and the ingest fills it. The protection generation is a counter on the saga: every release starts a new one, and only order groups of the current generation can change protection.
12. **Under `CLOSE_OWNED` only a loss of protection is an incident.** `Inactive`/`Rejected`, or a cancel of a ref the close did not ask for, still takes today's incident path (R14). Other events (a late entry fill, a working status, a stop fill in the cancel race) are bookkeeping and keep `CLOSE_OWNED`. Running them through today's `_apply_event` would call a filled position with a cancelled stop `MISSING_PROTECTION`.
13. **Several sagas on one conid.** If a close owned more than one saga on the conid, `release_after_partial` gives the new legs to the first saga (by command id) and closes the others with `PROTECTION_MERGED`.
14. **`supersede` is not a separate entry point.** R12 lists `supersede`; here the supersede happens inside the account claim transaction (R6), so there is nothing extra to serialize.
15. **R21 and SELL intents.** Running `execute_automated_intent` end to end needs a signed research bundle. Task 13 checks the SELL wiring on cold start and hot-arm (the built service holds the facade and the broker port). It drives the joined-command path end to end with `liquidate_account` through the same coordinator and reconciler. The SELL rules themselves are tested in Tasks 12 and 17.
16. **Startup recovery runs inside the loop.** `_maybe_start_liquidation_recovery` used to call `rescan()` before `trader.run()`. Orders need a running loop, so the first tick is now the first step of the recovery task. Session recovery keeps its "before readiness" order: `SessionController.restore()` loads the durable deadlines synchronously, and the first `run_due` runs on the worker inside the loop.
17. **Join rows carry the conid.** A retried command is checked against its first request (account, conid, goal, quantity), so a joined command id cannot be re-used for another conid (#24).
18. **Intermediate behaviour between Tasks 5 and 6.** Task 5 refuses a partial request with `PARTIAL_CLOSE_UNAVAILABLE`; Task 6 replaces that line. No run is created for the refused request.


---

## File Structure

| File | Task(s) | Responsibility |
|---|---|---|
| `trader/trading/order_correlation.py` (modify) | 2 | `liquidation_child_id`, `reprotect_oca_group`, `liquidation_child_kind`; `classify_leg` learns re-protect and reduce children. |
| `trader/trading/broker_ingest.py` (modify) | 2, 9 | pass the order group to `classify_leg` (two call sites); pass `order_entity_id` to the saga event. |
| `trader/trading/exit_owner.py` (create) | 3 | `ExitOwnerRegistry` with `*_in_tx` claims, migration 35. |
| `trader/trading/liquidation_service.py` (modify) | 4, 5, 6, 7, 8 | migration 36; frozen data model (`ChildRef`, `LiquidationReceipt`, `JoinRow`, `CloseResolution`, ports, errors); `LiquidationRunStore`; write-ahead children, evidence, account and conid scopes, partial close, re-protect, escalation, takeover, claims, cleanup. |
| `trader/trading/liquidation_worker.py` (create) | 15 | `LiquidationWorker`, `SerializedLiquidation` (R12). |
| `trader/trading/trading_runtime.py` (modify) | 14, 10 | `Trader.place_reduce_only_order` (R11, R13); `TradingRuntimeOrderDispatch.reduce_position` / `reduce_partial` / `place_exit_leg` / `cancel_on_loop`. |
| `trader/trading/command_stack.py` (modify) | 4, 14, 10, 15, 11, 13 | `_LiquidationDispatch` (evidence + reduce-only methods); `_BrokerGenerationRefresh`; composition of registry, store, worker facade, saga, session adapters, intent service, reconciler. |
| `trader/trader_service.py` (modify) | 15 | recovery and session loops await the worker. |
| `trader/automation/protective_order_saga.py` (modify) | 9 | `CLOSE_OWNED`, exact expected cancels, protection generations, hand-over / release / close, migration 37. |
| `trader/automation/session_controller.py` (modify) | 15, 16, 11 | `restore()`; cancel entries only; time exit = scoped close; persist and poll the returned flatten root. |
| `trader/automation/automated_intent_command.py` (modify) | 12 | SELL intents become a proven-reduction close. |
| `trader/trading/command_coordinator.py` (modify) | 17 | `OutcomeReconciler` resolves close commands from their exact root. |
| `scripts/command_plane_drill.py` (modify) | 4 | the liquidation drill uses the journal store and broker evidence. |
| `tests/test_order_correlation.py`, `tests/automation/test_attribution_ledger.py` (modify) | 2, 9 | child ids, leg classification, ingest forwarding. |
| `tests/test_exit_owner.py` (create) | 3 | registry rules. |
| `tests/test_liquidation_service.py` (rewrite) | 4–8 | state machine on a real DuckDB journal with fake broker generations, crash injection. |
| `tests/test_reduce_only_order_path.py` (create) | 14, 10 | the reduce-only boundary and exit legs by order identity. |
| `tests/test_liquidation_worker.py` (create) | 15 | worker serialization, real asyncio loop, ingest-lock case. |
| `tests/automation/test_protective_order_saga.py` (modify) | 9 | hand-over, owned events, retired legs. |
| `tests/automation/test_session_controller.py` (modify) | 1, 16, 11 | the time-exit regression, cancel entries only, exact-root polling. |
| `tests/automation/test_automated_command_boundary.py` (modify) | 12 | SELL → close. |
| `tests/test_close_reconciliation.py` (create) | 17 | joined-command resolution. |
| `tests/test_safe_close_integration.py` (create) | 13 | the real `build_command_stack` on a real loop, fake broker only. |

---

### Task 1: Pin the latent time-exit bug with a failing test

The spec requires a failing test before the fix. Per R18 the test uses **today's** constructor (`SessionTimeExitAdapter(dispatch)`, `trader/automation/session_controller.py:286`) and a fake broker that keeps the working protective stop. It asserts the behaviour: after the position is closed, no stop is still working. Today the adapter calls `reduce` only, so the stop stays live and the assertion fails. It is committed as `xfail(strict=True)` so the suite stays green; Task 11 rewrites the same test for the new constructor and removes the marker.

**Files:**
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Consumes: `SessionTimeExitAdapter(dispatch)` and `request_exit(*, command_id, conid, quantity, side)` as they are today.
- Produces: `_SimBroker` (test helper): one long position of 10 and its working stop `og-1:stop`; every `capture` is a newer generation; it also answers `cancel`, `reduce`, `find_orders`, `get_order`, so Task 11 can run a real `LiquidationService` on it.

- [ ] **Step 1: Write the failing test**

Append to `tests/automation/test_session_controller.py`:

```python
# ---------------------------------------------------------------------------
# SP1 plan 1: time exits close through the scoped liquidation (Tasks 1, 11)
# ---------------------------------------------------------------------------

class _SimBroker:
    """A broker with one long position and its working protective stop.

    It is the snapshot port and the dispatch port at once; every capture is a
    newer broker generation.
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


@pytest.mark.xfail(strict=True, reason="the time exit reduces without cancelling the stop; fixed in plan 1 task 11")
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
Expected: `1 xfailed`. To confirm the cause, run it once more with `--runxfail`: it must fail with `AssertionError: a live stop on a closed position can open a short` (the reduce ran, the stop stayed working). A `TypeError` would mean the test is wrong.

- [ ] **Step 3: Commit**

```bash
git add tests/automation/test_session_controller.py
git commit -m "test: pin time exit leaving the protective stop live

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 2: Child ids, and `classify_leg` understands re-protect and reduce children

Liquidation children have no parent order, so `classify_leg` (`trader/trading/order_correlation.py:32`) calls them `"entry"`. The ingest would then feed the saga an "entry" event for a replacement stop, and the session cancel phase (Task 16) would cancel a working reduce as an "entry". Every child id is `{root}-{kind}-{conid}-{attempt}` (R1, ruling 6) and becomes the order ref, so the decoded group names the kind.

**Files:**
- Modify: `trader/trading/order_correlation.py` (`classify_leg`, new helpers)
- Modify: `trader/trading/broker_ingest.py:751` and `:1281` (pass the order group)
- Test: `tests/test_order_correlation.py`, `tests/automation/test_attribution_ledger.py`

**Interfaces:**
- Produces:

```python
LIQUIDATION_CHILD_KINDS = ("cancel", "reduce", "reprotect-stop", "reprotect-target")
def liquidation_child_id(root_id: str, kind: str, conid: int, attempt: int) -> str   # ValueError on bad input
def reprotect_oca_group(root_id: str, conid: int, attempt: int) -> str              # "{root}-reprotect-{conid}-{attempt}"
def liquidation_child_kind(order_group_id: Optional[str]) -> Optional[str]
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

Append to `tests/automation/test_attribution_ledger.py` (the helper is reused in Task 9):

```python
def _forwarded_events(tmp_path, *, order_ref, order_type, name):
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
    obs = OrderObservation(
        account_id=ACCOUNT, perm_id=77, client_order_id=7, parent_id=0, conid=CONID, symbol="AAPL",
        action="SELL", order_type=order_type, total_quantity=6.0, filled_quantity=0.0, avg_fill_price=None,
        limit_price=None, stop_price=95.0, tif="DAY", status="Submitted", order_ref=order_ref,
        source_timestamp=NOW,
    )
    conn = journal.connect()
    ingest._apply_record(conn, obs, lambda mutation, write: journal.mutate(conn, mutation, write))
    return seen


def test_broker_ingest_classifies_a_reprotect_stop_leg_without_a_parent(tmp_path):
    """SP1 plan 1 Task 2: a replacement stop has no parent but is a stop, not an entry."""
    seen = _forwarded_events(tmp_path, order_ref=encode_order_ref("p-1-reprotect-stop-265598-1"),
                             order_type="STP", name="reprotect-leg.duckdb")
    assert [(e.order_group_id, e.leg) for e in seen] == [("p-1-reprotect-stop-265598-1", "stop")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_order_correlation.py tests/automation/test_attribution_ledger.py -q --timeout=30 -k "liquidation_child or groups or reprotect"`
Expected: FAIL — `ImportError: cannot import name 'liquidation_child_id'`.

- [ ] **Step 3: Implement**

In `trader/trading/order_correlation.py` add `import re` after `import datetime as dt`, and replace `classify_leg` with:

```python
LIQUIDATION_CHILD_KINDS = ("cancel", "reduce", "reprotect-stop", "reprotect-target")
_LIQUIDATION_CHILD = re.compile(r"-(cancel|reduce|reprotect-stop|reprotect-target)-(\d+)-(\d+)$")


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
    match = _LIQUIDATION_CHILD.search(order_group_id or "")
    return match.group(1) if match else None


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

In `trader/trading/broker_ingest.py` pass the group at both call sites:

```python
# line 751, inside the merged BrokerOrderRow leg= expression
                    classify_leg(obs.order_type, obs.parent_id, obs.client_order_id, group_id)
# line 1281, in _notify_protective_saga
        leg = order.leg or classify_leg(
            obs.order_type, obs.parent_id, obs.client_order_id, order.order_group_id,
        )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_order_correlation.py tests/automation/test_attribution_ledger.py tests/test_broker_ingest*.py -q --timeout=30`
Expected: all PASS (the old `test_classify_leg` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/order_correlation.py trader/trading/broker_ingest.py tests/test_order_correlation.py tests/automation/test_attribution_ledger.py
git commit -m "feat: deterministic liquidation child ids and leg classification

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 3: `ExitOwnerRegistry` — one owner per position, one flatten per account

**Files:**
- Create: `trader/trading/exit_owner.py`
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
    # one transaction each (tests, simple readers)
    def get / owner_for / account_owner / claim_scoped / claim_account / release(root_id, now)
```

Rules (spec 5.1 "One execution owner per position"):
1. `claim_scoped_in_tx` checks the **account owner first**. If one is `ACTIVE`: a full request returns `JOINED_FLATTEN` with the flatten root; a partial request raises `ExitInProgress`.
2. Else, with an `ACTIVE` scoped owner on `(account, conid)`: the same root id → `JOINED`; a partial request → `ExitInProgress`; a full request against a `partial` owner upgrades it to `zero` in the caller's transaction → `UPGRADED`; a full request against a `zero` owner → `JOINED`.
3. Else insert `ACTIVE` → `CLAIMED`. A root id that was ever used raises `ValueError`.
4. `claim_account_in_tx`: another `ACTIVE` account owner → `JOINED_FLATTEN`; the same root → `CLAIMED`; else every `ACTIVE` scoped owner of the account becomes `SUPERSEDED` in the same transaction and their ids come back in `superseded`.
5. `SUPERSEDED`, `RELEASED` and `FAILED_SAFE` rows are never owners and are never upgraded (R9: a `FAILED_SAFE` owner does not block a later claim).
6. Uniqueness of the active owner relies on serialization: every claim runs inside the R12 worker and inside the per-database lock of `DuckDBConnection.transaction`. (A partial unique index is not available in DuckDB.)

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

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_exit_owner.py -q --timeout=30`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/exit_owner.py tests/test_exit_owner.py
git commit -m "feat: durable exit owner registry with in-transaction claims

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 4: Frozen data model, migration 36, write-ahead children; account scope on the new model

This task freezes the data model every later task uses, and rebuilds the account flatten on it: journaled children (R1, R2), evidence rules (R4), the reduce rule (R5), one-transaction claims (R6, R10), re-read before dispatch (R7), durable cleanup (R8) and inheritance from `FAILED_SAFE` roots (R9). The conid scope arrives in Task 5.

**Files:**
- Modify (rewrite): `trader/trading/liquidation_service.py`
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch`, migrations 35/36, the `LiquidationService(` construction, the two session adapters)
- Modify: `scripts/command_plane_drill.py` (`scn_liquidation`)
- Test (rewrite): `tests/test_liquidation_service.py`; Test: `tests/test_command_stack.py`

**Interfaces (frozen — every later task uses exactly these names and types):**

```python
# trader/trading/liquidation_service.py
LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION = 36
def apply_liquidation_migration(migrator) -> None        # applies 25 and 36
RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "SUPERSEDED", "FAILED_SAFE"})
SUCCESS_STATES = frozenset({"FLAT", "CLOSED", "DONE"})
CHILD_STATES = {"PLANNED", "UNKNOWN", "WORKING", "FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"}
CHILD_TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"}

class DispatchRefused(RuntimeError)      # (code, detail): proven refusal before the broker -> child NOT_SENT
class LiquidationRefused(ValueError)     # (code, detail): request refused up front, nothing written
class RunStateError(RuntimeError)        # overwrite of a terminal or SUPERSEDED run

class BrokerSnapshotPort(Protocol):       capture(account_id) -> BrokerRiskSnapshot
class LiquidationDispatchPort(Protocol):
    cancel(order, child_id) -> None
    reduce(position, side, quantity, child_id) -> None                 # whole position
    reduce_partial(position, side, quantity, child_id) -> None         # 0 < q < |position|
    place_exit_leg(position, *, leg, quantity, price, oca_group, child_id) -> None   # leg "stop" | "target"
    find_orders(account_id, child_id) -> list                          # rows whose decoded ref == child_id
    get_order(order_entity_id) -> Optional[row]                        # incl. deleted rows
class LiquidationBreakerPort(Protocol):   trip_liquidation(cause_command_id, detail) -> None
class GenerationRefreshPort(Protocol):    request_refresh(account_id) -> None       # never blocks
@dataclass(frozen=True) class CancelTarget(order_entity_id: str, order_group_id: Optional[str])
@dataclass(frozen=True) class HandoverInfo(stop_price: Optional[float], target_price: Optional[float])
class ProtectionOwnershipPort(Protocol):  # ProtectiveOrderSaga implements it in Task 9; all idempotent
    handover(*, account_id, conid, close_root_id, cancels: tuple[CancelTarget, ...], generation, now) -> HandoverInfo
    handover_account(*, account_id, close_root_id, cancels, generation, now) -> None
    release_after_partial(*, close_root_id, remaining_quantity, stop_group, target_group, now) -> None
    close_after_full(*, close_root_id, now) -> None

@dataclass(frozen=True)
class ChildRef:
    child_id: str; root_id: str; owner_root_id: str; account_id: str; conid: int
    kind: str                      # cancel | reduce | reprotect-stop | reprotect-target
    attempt: int; state: str       # CHILD_STATES
    fence_generation: int          # generation of the snapshot it was sent on (R1)
    side=None; quantity=None; price=None; oca_group=None; target_order_entity_id=None
    filled_at_send=0.0; filled_quantity=0.0; outstanding_quantity=None; observed_generation=None
    fill_bearing: bool             # property: ABSENT, or filled_quantity > filled_at_send

@dataclass(frozen=True)
class LiquidationReceipt:
    account_id; cause_command_id; state; deadline; generation_id=None; detail=""
    scope="account"; conid=None; goal="zero"; goal_quantity=None; phase=""   # phase "" | cancel | reduce | reprotect
    opened_generation=None; stop_price=None; target_price=None; remaining_quantity=None
    escalated=False; superseded_by=None; cleanup_pending=False
    children: tuple[ChildRef, ...] = ()     # every child whose owner_root_id is this root

@dataclass(frozen=True) class JoinRow(command_id, root_id, account_id, conid, outcome, requested_goal, requested_quantity)
@dataclass(frozen=True) class CloseResolution(command_id, root_id, state, success: bool, outcome: dict)

class LiquidationRunStore:                # the journal rows; R6 in-transaction API
    transaction(fn)
    get_run_in_tx / insert_run_in_tx / update_run_in_tx(conn, receipt, now)   # update refuses terminal changes
    roots_to_advance_in_tx(conn) -> list[str]                                  # cleanup-pending first, then open roots
    children_in_tx / next_attempt_in_tx / insert_child_in_tx / update_child_in_tx / drop_planned_in_tx
    inherit_children_in_tx(conn, *, account_id, conid, to_root_id, now) -> int  # R9
    record_join_in_tx / join_for_in_tx / joins_resolving_to_in_tx
    receipt(root_id) / root_for(command_id) / close_resolution(command_id)     # own transaction each

class LiquidationService:
    __init__(broker, dispatch, *, store, registry, now, breaker=None, journal=None, ledger=None,
             protection=None, refresh=None, deadline_seconds=300.0)
    attach_protection(protection) -> None
    liquidate(cmd) -> CommandReceipt
    start(account_id, cause_command_id, deadline, *, scope="account", conid=None, quantity=None,
          stop_price=None, target_price=None) -> LiquidationReceipt   # the root the caller must poll
    rescan() -> Optional[LiquidationReceipt]
    receipt_for(root_id) / root_for(command_id) / close_resolution(command_id)
    upgrade_to_zero(root_id) -> LiquidationReceipt                    # added in Task 6
```

Journal tables of migration 36: `liquidation_runs` gains `scope, conid, goal, goal_quantity, phase, opened_generation, stop_price, target_price, remaining_quantity, escalated, superseded_by, cleanup_pending`; new `liquidation_children` (one row per child order, keyed by `child_id`, with `owner_root_id` for inheritance); new `liquidation_joins` (one row per command that started or joined a root — R17's `(command_id, root_id)`).

Rules this task implements (they also hold for every later scope):
- **Write-ahead (R1, R7).** `_reserve` re-reads the run and its owner, checks the root may still dispatch (owner `ACTIVE`, run not terminal, goal unchanged), and writes the children as `UNKNOWN` in one transaction. Only then `_send` calls the broker, after one more re-read. `DispatchRefused` → `NOT_SENT`; any other exception → stays `UNKNOWN` (R2).
- **Evidence (R4, rulings 2, 3, 7).** `_observe_children` classifies every `UNKNOWN`/`WORKING` child from broker rows on each step.
- **Reduce rule (R5, ruling 4).** `_blocking`: any `UNKNOWN` child, a `WORKING` cancel or reduce, or a snapshot not newer than the last observed fill stops every new reduce. Cancels of identified working orders still go out (spec 5.1 step 4).
- **Claims (R6, R10).** `start(scope="account")` claims, creates the run and records the join row in one transaction; `JOINED_FLATTEN` creates nothing and returns the existing root's receipt.
- **Terminal + cleanup (R6, R8, R9, ruling 9).** `_finish` writes the terminal state with `cleanup_pending` (and owner `FAILED_SAFE` when failed) in one transaction; `_cleanup` runs the idempotent steps and recovery re-runs them.

- [ ] **Step 1: Write the failing tests**

Replace the whole of `tests/test_liquidation_service.py` with the following. The eight existing tests are kept (same names, one renamed to say what it now checks: `test_flat_requires_newer_generation_and_a_terminal_reduce_child`, R19); they now run on a real temporary DuckDB journal and the fake dispatch answers evidence lookups.

```python
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import (
    ChildRef, DispatchRefused, LiquidationReceipt, LiquidationRunStore, LiquidationService,
    RunStateError, apply_liquidation_migration,
)


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


def _row(status, filled=0.0, total=10.0):
    return SimpleNamespace(status=status, filled_quantity=filled, total_quantity=total, deleted=False)


class _Broker:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def capture(self, account_id):
        self.calls += 1
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, Exception):
            raise value
        return value


class _Dispatch:
    """Records orders; answers evidence lookups from dicts the test fills."""
    def __init__(self):
        self.calls = []
        self.rows: dict[str, list] = {}       # child id -> broker rows found by its order ref
        self.entities: dict[str, object] = {}  # order entity id -> broker row (cancel targets)
        self.refuse: set[str] = set()          # methods that raise DispatchRefused before the boundary
        self.fail_after_send: set[str] = set() # methods that raise after the order was sent

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
        return list(self.rows.get(child_id, []))

    def get_order(self, order_entity_id):
        return self.entities.get(order_entity_id)


class _Breaker:
    def __init__(self): self.calls = []
    def trip_liquidation(self, cause, detail): self.calls.append((cause, detail))


class _LedgerRow:
    def __init__(self, state): self.state = state


class _Ledger:
    def __init__(self): self.rows = {}; self.transitions = []
    def get(self, command_id): return self.rows.setdefault(command_id, _LedgerRow("OUTCOME_UNKNOWN"))
    def transition_in_tx(self, _conn, command_id, before, after, **kwargs):
        self.transitions.append((command_id, before, after, kwargs)); self.rows[command_id].state = after


class _Journal:
    def connect(self): return object()
    def mutate(self, conn, mutation, write, event_id): write(conn, 1)


class _Crash(BaseException):
    """Simulated process death: never caught by the service."""


class _Stack:
    """A service over a real DuckDB journal; restart() rebuilds it on the same file."""
    def __init__(self, tmp_path, snapshots, *, protection=None, journal=None, ledger=None, deadline_seconds=300.0):
        self.db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
        migrator = SchemaMigrator(self.db)
        apply_exit_owner_migration(migrator)
        apply_liquidation_migration(migrator)
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.broker = _Broker(snapshots)
        self.dispatch = _Dispatch()
        self.breaker = _Breaker()
        self.clock = {"now": NOW}
        self.protection, self.journal, self.ledger = protection, journal, ledger
        self.deadline_seconds = deadline_seconds
        self.service = self._build()

    def _build(self):
        return LiquidationService(
            self.broker, self.dispatch, store=self.store, registry=self.registry,
            now=lambda: self.clock["now"], breaker=self.breaker, journal=self.journal, ledger=self.ledger,
            protection=self.protection, deadline_seconds=self.deadline_seconds)

    def restart(self):
        self.store = LiquidationRunStore(self.db)
        self.registry = ExitOwnerRegistry(self.db)
        self.service = self._build()
        return self.service

    def push(self, *snapshots):
        self.broker.snapshots = list(snapshots)


def _stack(tmp_path, snapshots, **kwargs):
    return _Stack(tmp_path, snapshots, **kwargs)


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
    assert (child.child_id, child.kind, child.state, child.fence_generation) == ("root-1-reduce-1-1", "reduce", "UNKNOWN", 1)
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    assert s.service.rescan().state == "VERIFYING"      # generation 2 observes the fill
    assert s.service.rescan().state == "FLAT"           # generation 3 is newer than that fill


def test_broker_flat_proof_resolves_pending_command_root(tmp_path):
    ledger = _Ledger()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])],
               journal=_Journal(), ledger=ledger)
    s.service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    s.dispatch.rows["root-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    assert s.service.rescan().state == "FLAT"
    assert ledger.transitions[0][:3] == ("root-1", "OUTCOME_UNKNOWN", "RESOLVED")


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
    assert db.execute("SELECT name FROM schema_migrations WHERE version = 36", fetch="one") == ("sp1_liquidation_safe_close",)
    run_cols = {r[0] for r in db.execute("DESCRIBE liquidation_runs", fetch="all")}
    assert {"scope", "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price",
            "target_price", "remaining_quantity", "escalated", "superseded_by", "cleanup_pending"} <= run_cols
    child_cols = {r[0] for r in db.execute("DESCRIBE liquidation_children", fetch="all")}
    assert {"child_id", "owner_root_id", "kind", "attempt", "state", "fence_generation",
            "target_order_entity_id", "filled_at_send", "observed_generation"} <= child_cols
    join_cols = {r[0] for r in db.execute("DESCRIBE liquidation_joins", fetch="all")}
    assert {"command_id", "root_id", "outcome", "requested_goal"} <= join_cols


def test_store_round_trips_a_run_with_children(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    receipt = LiquidationReceipt(ACCOUNT, "root-7", "REPROTECTING", DEADLINE, generation_id=3, detail="x",
                                 scope="conid", conid=1, goal="partial", goal_quantity=4.0, phase="reprotect",
                                 opened_generation=1, stop_price=95.0, target_price=120.0, escalated=True)
    child = ChildRef("root-7-reprotect-stop-1-1", "root-7", "root-7", ACCOUNT, 1, "reprotect-stop", 1,
                     "UNKNOWN", 3, side="SELL", quantity=6.0, price=95.0, oca_group="root-7-reprotect-1-1")

    def write(conn):
        s.store.insert_run_in_tx(conn, receipt, NOW)
        s.store.insert_child_in_tx(conn, child, NOW)
    s.store.transaction(write)
    assert s.store.receipt("root-7") == LiquidationReceipt(**{**receipt.__dict__, "children": (child,)})


def test_store_refuses_to_overwrite_a_terminal_or_superseded_run(tmp_path):
    s = _stack(tmp_path, [_snapshot(1)])
    for state in ("SUPERSEDED", "FLAT"):
        root = f"r-{state}"
        s.store.transaction(lambda conn: s.store.insert_run_in_tx(
            conn, LiquidationReceipt(ACCOUNT, root, state, DEADLINE), NOW))
        with pytest.raises(RunStateError):
            s.store.transaction(lambda conn: s.store.update_run_in_tx(
                conn, LiquidationReceipt(ACCOUNT, root, "CANCELLING", DEADLINE), NOW))
        assert s.store.receipt(root).state == state


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


def test_invisible_reduce_child_on_a_newer_generation_never_gets_a_second_reduce(tmp_path):
    """#21: no broker row for the child on the next generation is UNKNOWN, not absent."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    receipt = s.service.rescan()
    assert receipt.state == "VERIFYING"
    assert "outcome unknown" in receipt.detail
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


def test_absence_is_proven_only_two_generations_after_the_send(tmp_path):
    """R4: an empty lookup becomes ABSENT on the second newer generation; then a new attempt id."""
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()]),
                          _snapshot(3, [_position()]), _snapshot(4, [_position()])])
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.service.rescan()                                         # generation 2: still UNKNOWN
    receipt = s.service.rescan()                               # generation 3: ABSENT, fill unknown
    assert receipt.children[0].state == "ABSENT"
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]
    s.service.rescan()                                         # generation 4 is newer than that observation
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
    s.service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))
    s.clock["now"] = NOW + dt.timedelta(seconds=31)
    assert s.service.rescan().state == "FAILED_SAFE"
    s.push(_snapshot(2, [_position()]))
    receipt = s.service.start(ACCOUNT, "flat-2", NOW + dt.timedelta(minutes=5))
    assert [c.child_id for c in receipt.children] == ["flat-1-reduce-1-1"]
    assert receipt.children[0].owner_root_id == "flat-2"
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


@pytest.mark.parametrize("boundary", ["before_send", "after_send"])
def test_restart_after_reduce_child_is_journaled_never_resends_it(tmp_path, boundary):
    """R20: crash after the child is journaled, before or after the broker call."""
    s = _stack(tmp_path, [_snapshot(1, [_position()])])

    def crash(*_args):
        if boundary == "after_send":
            s.dispatch.calls.append(("reduce-sent",))
        raise _Crash()
    s.dispatch.reduce = crash
    with pytest.raises(_Crash):
        s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert s.store.receipt("flat-1").children[0].state == "UNKNOWN"
    s.dispatch = _Dispatch()
    service = s.restart()
    s.push(_snapshot(2, [_position()]))
    assert "outcome unknown" in service.rescan().detail
    assert s.dispatch.calls == []


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
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])])
    s.dispatch.fail_after_send.add("reduce")
    receipt = s.service.start(ACCOUNT, "flat-1", DEADLINE)
    assert receipt.children[0].state == "UNKNOWN"
    s.service.rescan()
    assert [c[0] for c in s.dispatch.calls] == ["reduce"]


class _Protection:
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

    def release_after_partial(self, *, close_root_id, remaining_quantity, stop_group, target_group, now):
        self._call("release_after_partial", close_root_id, remaining_quantity, stop_group, target_group)

    def close_after_full(self, *, close_root_id, now):
        self._call("close_after_full", close_root_id)


@pytest.mark.parametrize("step", ["protection", "release"])
def test_restart_after_terminal_save_finishes_every_cleanup_step(tmp_path, step):
    """R8 / R20: a crash between the terminal save and a cleanup step is finished on recovery."""
    protection = _Protection()
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, []), _snapshot(3, [])], protection=protection)
    s.service.start(ACCOUNT, "flat-1", DEADLINE)
    s.dispatch.rows["flat-1-reduce-1-1"] = [_row("Filled", filled=10.0)]
    s.service.rescan()
    if step == "protection":
        protection.crash_on.add("close_after_full")
    else:
        real = s.registry.finish_in_tx

        def crash_on_release(conn, root_id, state, now):
            if state == "RELEASED":
                raise _Crash()
            return real(conn, root_id, state, now)
        s.registry.finish_in_tx = crash_on_release
    with pytest.raises(_Crash):
        s.service.rescan()
    assert s.store.receipt("flat-1").state == "FLAT"
    assert s.store.receipt("flat-1").cleanup_pending is True
    service = s.restart()
    service.rescan()
    assert s.store.receipt("flat-1").cleanup_pending is False
    assert s.registry.get("flat-1").state == "RELEASED"
    assert ("close_after_full", "flat-1") in protection.calls
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

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_command_stack.py -q --timeout=30`
Expected: FAIL at import — `ImportError: cannot import name 'ChildRef' from 'trader.trading.liquidation_service'`.

- [ ] **Step 3: Implement the module**

Replace the whole of `trader/trading/liquidation_service.py` with:

```python
"""Broker-verified liquidation and safe close state machine.

Order acknowledgements are *evidence of uncertainty*, never evidence that a
position is gone. Every child order is journaled before the broker call
(write-ahead), and a child becomes terminal only from broker evidence on a
generation newer than the one it was sent on. A further reduce is sized only
from a broker generation newer than the last fill this root observed.

Scopes: ``account`` (flatten everything; session flatten, /flatten,
protective failure, kill) and ``conid`` (close one position, fully or in
part, with protection hand-over and a linked re-protect for a partial).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id
from trader.trading.exit_owner import (
    CLAIMED, JOINED_FLATTEN, STATE_ACTIVE, STATE_FAILED_SAFE, STATE_RELEASED, ExitOwnerRegistry,
)
from trader.trading.order_correlation import liquidation_child_id

LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SAFE_CLOSE_MIGRATION_VERSION = 36

RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "SUPERSEDED", "FAILED_SAFE"})
SUCCESS_STATES = frozenset({"FLAT", "CLOSED", "DONE"})
_SUCCESS_FOR_GOAL = {
    "account": frozenset({"FLAT"}),
    "zero": frozenset({"CLOSED", "FLAT"}),
    "partial": frozenset({"DONE", "CLOSED", "FLAT"}),
}

CHILD_STATES = frozenset({
    "PLANNED", "UNKNOWN", "WORKING", "FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT",
})
CHILD_TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED", "ABSENT", "NOT_SENT"})
_BROKER_ACCEPTED = frozenset({"PreSubmitted", "Submitted", "PendingCancel"})
_BROKER_TERMINAL = {
    "Filled": "FILLED", "Cancelled": "CANCELLED", "ApiCancelled": "CANCELLED",
    "Inactive": "REJECTED", "Rejected": "REJECTED",
}


def apply_liquidation_migration(migrator: SchemaMigrator) -> None:
    """Journal migrations 25 (runs) and 36 (scope, goal, children, joins)."""
    migrator.apply(LIQUIDATION_MIGRATION_VERSION, "p1_liquidation_runs", (
        """CREATE TABLE IF NOT EXISTS liquidation_runs (
            cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL, deadline TIMESTAMPTZ NOT NULL,
            generation_id BIGINT, detail VARCHAR NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        )""",
    ))
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
        """CREATE TABLE IF NOT EXISTS liquidation_children (
            child_id VARCHAR PRIMARY KEY,
            root_id VARCHAR NOT NULL,
            owner_root_id VARCHAR NOT NULL,
            account_id VARCHAR NOT NULL,
            conid INTEGER NOT NULL,
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
    ))


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

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
    """An attempt to change a terminal or SUPERSEDED run."""


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
    def get_order(self, order_entity_id: str) -> Optional[Any]: ...


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
    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              stop_group: str, target_group: Optional[str], now: dt.datetime) -> None: ...
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
    conid: int
    kind: str                     # cancel | reduce | reprotect-stop | reprotect-target
    attempt: int
    state: str                    # see CHILD_STATES
    fence_generation: int         # broker generation of the snapshot it was sent on
    side: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    oca_group: Optional[str] = None
    target_order_entity_id: Optional[str] = None
    filled_at_send: float = 0.0
    filled_quantity: float = 0.0
    outstanding_quantity: Optional[float] = None
    observed_generation: Optional[int] = None

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
    requested_goal: str           # account | zero | partial
    requested_quantity: Optional[float]


@dataclass(frozen=True)
class CloseResolution:
    command_id: str
    root_id: str                  # the root that decided (after following SUPERSEDED)
    state: str
    success: bool
    outcome: dict


_RUN_COLUMNS = (
    "cause_command_id", "account_id", "state", "deadline", "generation_id", "detail", "scope",
    "conid", "goal", "goal_quantity", "phase", "opened_generation", "stop_price", "target_price",
    "remaining_quantity", "escalated", "superseded_by", "cleanup_pending",
)
_CHILD_COLUMNS = (
    "child_id", "root_id", "owner_root_id", "account_id", "conid", "kind", "attempt", "state",
    "fence_generation", "side", "quantity", "price", "oca_group", "target_order_entity_id",
    "filled_at_send", "filled_quantity", "outstanding_quantity", "observed_generation",
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
    values["conid"] = int(values["conid"])
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
        assignments = ", ".join(f"{c} = ?" for c in _RUN_COLUMNS[1:])
        conn.execute(
            f"UPDATE liquidation_runs SET {assignments}, updated_at = ? WHERE cause_command_id = ?",
            [getattr(receipt, c) for c in _RUN_COLUMNS[1:]] + [now, receipt.cause_command_id])

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
        """Outcome of the exact root a command started or joined; None while undecided."""
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
            return CloseResolution(
                command_id=command_id, root_id=run.cause_command_id, state=run.state, success=success,
                outcome={"close_root_id": run.cause_command_id, "liquidation_state": run.state,
                         "generation_id": run.generation_id, "detail": run.detail},
            )
        return self._db.transaction(read)


def _reducing_side(quantity: float) -> str:
    return "SELL" if float(quantity) > 0 else "BUY"


def _targets(orders) -> tuple[CancelTarget, ...]:
    return tuple(CancelTarget(o.order_entity_id, getattr(o, "order_group_id", None)) for o in orders)


class LiquidationService:
    """One state machine for every exit. Call every entry point from one worker thread (R12)."""

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
        protection: Optional[ProtectionOwnershipPort] = None,
        refresh: Optional[GenerationRefreshPort] = None,
        deadline_seconds: float = 300.0,
    ):
        self._broker = broker
        self._dispatch = dispatch
        self._store = store
        self._registry = registry
        self._now = now
        self._breaker = breaker
        self._journal, self._ledger = journal, ledger
        self._protection = protection
        self._refresh = refresh
        self._deadline_seconds = deadline_seconds

    def attach_protection(self, protection: ProtectionOwnershipPort) -> None:
        self._protection = protection

    # -- command entry -------------------------------------------------------------

    def liquidate(self, cmd) -> CommandReceipt:
        """Coordinator saga entry: acknowledgement is explicitly non-terminal."""
        if self._journal is None or self._ledger is None:
            raise RuntimeError("liquidation command authority is not configured")
        receipt = self.start(cmd.account_id, cmd.command_id, self._now() + dt.timedelta(seconds=self._deadline_seconds))
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
            return self._tick(root)
        return self._store.receipt(root)

    def rescan(self) -> Optional[LiquidationReceipt]:
        """Finish pending cleanups, then advance every root that is not terminal."""
        first: Optional[LiquidationReceipt] = None
        for root in self._store.transaction(self._store.roots_to_advance_in_tx):
            try:
                advanced = self._tick(root)
            except Exception:  # one bad root must not stop the others
                import logging
                logging.getLogger(__name__).exception("liquidation root %s failed to advance", root)
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
        if self._now() >= receipt.deadline:
            return self._on_deadline(receipt)
        try:
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as exc:
            return self._snapshot_unavailable(receipt, f"broker snapshot unavailable: {exc}")
        if getattr(snapshot, "account_id", None) != receipt.account_id:
            return self._snapshot_unavailable(receipt, "broker snapshot account mismatch")
        receipt = self._observe_children(receipt, snapshot)
        return self._advance_account(receipt, snapshot)

    def _snapshot_unavailable(self, receipt, detail):
        state = "OUTCOME_UNKNOWN" if receipt.scope == "account" else "VERIFYING"
        return self._set(receipt, state, detail=detail)

    def _on_deadline(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        return self._finish(receipt, "FAILED_SAFE", detail="deadline elapsed without broker-confirmed result")

    # -- evidence (R4) ---------------------------------------------------------------

    def _observe_children(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        changed = [observed for child in receipt.children if child.state in ("UNKNOWN", "WORKING")
                   for observed in (self._evidence(child, generation),) if observed != child]
        if not changed:
            return receipt

        def write(conn):
            for child in changed:
                self._store.update_child_in_tx(conn, child, self._now())
        self._store.transaction(write)
        return self._store.receipt(receipt.cause_command_id)

    def _evidence(self, child: ChildRef, generation: int) -> ChildRef:
        if child.kind == "cancel":
            row = self._dispatch.get_order(child.target_order_entity_id)
            rows = [] if row is None or getattr(row, "deleted", False) else [row]
        else:
            rows = list(self._dispatch.find_orders(child.account_id, child.child_id))
        newer = generation > child.fence_generation
        if not rows:
            # Absence needs a complete enumeration that began after the send. The first newer
            # generation may have opened between our capture and our send; the second cannot.
            proven = generation >= child.fence_generation + 2
            return replace(child, state="ABSENT", observed_generation=generation) if proven else child
        if len(rows) > 1:
            return child  # an ambiguous correlation proves nothing
        row = rows[0]
        status = getattr(row, "status", None)
        filled = float(getattr(row, "filled_quantity", 0.0) or 0.0)
        total = float(getattr(row, "total_quantity", 0.0) or 0.0)
        if status in _BROKER_TERMINAL:
            if not newer:
                return child
            state = _BROKER_TERMINAL[status]
        elif status in _BROKER_ACCEPTED:
            state = "WORKING"
        else:
            state = "UNKNOWN"  # PendingSubmit, ApiPending: a local echo is not acceptance
        outstanding = max(total - filled, 0.0)
        if (state, filled, outstanding) == (child.state, child.filled_quantity, child.outstanding_quantity):
            return child
        return replace(child, state=state, filled_quantity=filled, outstanding_quantity=outstanding,
                       observed_generation=generation)

    @staticmethod
    def _fresh(receipt, generation: int) -> bool:
        """R5: sizing needs a generation newer than every fill this root observed."""
        return all(generation > c.observed_generation for c in receipt.children
                   if c.fill_bearing and c.observed_generation is not None)

    def _blocking(self, receipt, generation: int) -> Optional[str]:
        for child in receipt.children:
            if child.state == "UNKNOWN":
                return f"{child.child_id} outcome unknown"
            if child.state == "WORKING" and child.kind in ("cancel", "reduce"):
                return f"{child.child_id} still working"
        if not self._fresh(receipt, generation):
            return "awaiting a broker generation newer than the last observed fill"
        return None

    @staticmethod
    def _last_action_generation(receipt) -> int:
        fences = [c.fence_generation for c in receipt.children if c.state != "NOT_SENT"]
        return max(fences + [receipt.opened_generation or 0])

    # -- writes -------------------------------------------------------------------------

    def _trips_breaker(self, receipt, state: str) -> bool:
        if receipt.scope == "account":
            return state != "FLAT"
        return state == "FAILED_SAFE"

    def _set(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        updated = replace(receipt, state=state, detail=detail,
                          generation_id=receipt.generation_id if generation_id is None else generation_id,
                          **fields)
        self._store.transaction(lambda conn: self._store.update_run_in_tx(conn, updated, self._now()))
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(receipt.cause_command_id, detail or state)
        return self._store.receipt(receipt.cause_command_id)

    def _wait(self, receipt, generation: int, detail: str) -> LiquidationReceipt:
        if self._refresh is not None:
            self._refresh.request_refresh(receipt.account_id)
        return self._set(receipt, "VERIFYING", generation_id=generation, detail=detail)

    def _finish(self, receipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        """Terminal transition plus cleanup_pending in one transaction (R6, R8)."""
        root = receipt.cause_command_id

        def write(conn):
            run = self._store.get_run_in_tx(conn, root)
            updated = replace(run, state=state, detail=detail, cleanup_pending=True,
                              generation_id=run.generation_id if generation_id is None else generation_id,
                              **fields)
            self._store.update_run_in_tx(conn, updated, self._now())
            self._store.drop_planned_in_tx(conn, root, self._now())
            if state == "FAILED_SAFE":
                self._registry.finish_in_tx(conn, root, STATE_FAILED_SAFE, self._now())
        self._store.transaction(write)
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(root, detail or state)
        return self._cleanup(self._store.receipt(root))

    def _cleanup(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R8: every step is idempotent; recovery re-runs it until the flag clears."""
        root = receipt.cause_command_id
        if self._protection is not None and receipt.state in ("CLOSED", "FLAT"):
            self._protection.close_after_full(close_root_id=root, now=self._now())

        def write(conn):
            if receipt.state in SUCCESS_STATES:
                self._registry.finish_in_tx(conn, root, STATE_RELEASED, self._now())
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(run, cleanup_pending=False), self._now())
        self._store.transaction(write)
        self._resolve_commands(root)
        return self._store.receipt(root)

    def _resolve_commands(self, root: str) -> None:
        """Resolve the commands that started or joined this root, only on proven success."""
        if self._journal is None or self._ledger is None:
            return
        joins = self._store.transaction(lambda conn: self._store.joins_resolving_to_in_tx(conn, root))
        for join in joins:
            resolution = self._store.close_resolution(join.command_id)
            if resolution is None or not resolution.success:
                continue
            row = self._ledger.get(join.command_id)
            if row is None or row.state != "OUTCOME_UNKNOWN":
                continue
            now = self._now()

            def write(conn, _revision, command_id=join.command_id, outcome=resolution.outcome):
                self._ledger.transition_in_tx(conn, command_id, "OUTCOME_UNKNOWN", "RESOLVED",
                                              outcome=outcome, error_code=None, now=now)
            self._journal.mutate(self._journal.connect(), DomainMutation(
                event_type="command.updated", entity_type="command",
                entity_id=command_entity_id(join.command_id), operation="upsert",
                account_id=join.account_id, source="trader_service", source_timestamp=now,
                correlation_id=join.command_id, payload={"state": "RESOLVED", **resolution.outcome}),
                write, event_id=f"command:{join.command_id}:liquidation-{resolution.state.lower()}")

    # -- dispatch (R1, R2, R3, R7) ------------------------------------------------------

    def _check_dispatchable_in_tx(self, conn, root_id: str, goal: str) -> None:
        run = self._store.get_run_in_tx(conn, root_id)
        owner = self._registry.get_in_tx(conn, root_id)
        if run is None or run.state in RESCAN_TERMINAL or run.cleanup_pending:
            raise _StaleDispatch(f"root {root_id} is no longer open")
        if owner is None or owner.state != STATE_ACTIVE:
            raise _StaleDispatch(f"root {root_id} no longer owns its scope")
        if run.goal != goal:
            raise _StaleDispatch(f"root {root_id} goal changed to {run.goal}")

    def _reserve(self, receipt, build) -> Optional[list]:
        """Journal children before any broker call; None when R7 says stop."""
        def write(conn):
            self._check_dispatchable_in_tx(conn, receipt.cause_command_id, receipt.goal)
            return build(conn)
        try:
            return self._store.transaction(write)
        except _StaleDispatch:
            return None

    def _still_dispatchable(self, root_id: str, goal: str) -> bool:
        try:
            self._store.transaction(lambda conn: self._check_dispatchable_in_tx(conn, root_id, goal))
        except _StaleDispatch:
            return False
        return True

    def _send(self, receipt, child: ChildRef, call: Callable[[], Any]) -> None:
        if not self._still_dispatchable(receipt.cause_command_id, receipt.goal):
            self._mark(child, "NOT_SENT")
            return
        try:
            call()
        except DispatchRefused:
            self._mark(child, "NOT_SENT")
        except Exception:
            pass  # the call may have crossed the broker boundary: the child stays UNKNOWN

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

    def _send_cancels(self, receipt, snapshot, orders) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        covered = {c.target_order_entity_id for c in receipt.children
                   if c.kind == "cancel" and c.state in ("UNKNOWN", "WORKING")}
        targets = [o for o in orders if o.order_entity_id not in covered]
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
        if receipt.phase == "":
            if self._protection is not None:
                self._protection.handover_account(
                    account_id=receipt.account_id, close_root_id=receipt.cause_command_id,
                    cancels=_targets(working), generation=generation, now=self._now())
            receipt = self._set(receipt, "CANCELLING_ENTRIES" if working else "VERIFYING",
                                generation_id=generation, phase="cancel", opened_generation=generation,
                                detail="account owner claimed; protection handed over")
        receipt = self._send_cancels(receipt, snapshot, working)
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
        receipt = self._set(self._store.receipt(receipt.cause_command_id), "REDUCING", generation_id=generation,
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

1. Imports: add `from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration`.
2. Replace `_LiquidationDispatch` (line 206) with:

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

    def get_order(self, order_entity_id: str):
        return self._orders_view.get_order(order_entity_id)
```

3. After `apply_liquidation_migration(migrator)` (line 599):

```python
    apply_exit_owner_migration(migrator)
    exit_owner_registry = ExitOwnerRegistry(trader.journal_db)
    liquidation_store = LiquidationRunStore(trader.journal_db)
```

4. Replace the `LiquidationService(` construction (line 808) with:

```python
    liquidation_service = LiquidationService(
        broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
        store=liquidation_store, registry=exit_owner_registry, now=now,
        breaker=_LiquidationBreaker(circuit_breaker, now),
        journal=journal, ledger=ledger,
    )
```

5. In the `SessionController(` call (line 899 and 902) change both `_LiquidationDispatch(dispatch)` to `_LiquidationDispatch(dispatch, orders_view)`.

In `scripts/command_plane_drill.py` replace `scn_liquidation` (it builds the service without a journal today):

```python
def scn_liquidation(db_path: str) -> dict:
    from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
    from trader.trading.liquidation_service import LiquidationRunStore, apply_liquidation_migration

    db = DuckDBConnection.get_instance(db_path + ".liquidation")
    migrator = SchemaMigrator(db)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    pos = SimpleNamespace(quantity=10.0, conid=CONID)
    snapshots = [
        SimpleNamespace(account_id=ACCOUNT, generation_id=1, positions=(pos,), working_orders=()),
        SimpleNamespace(account_id=ACCOUNT, generation_id=2, positions=(), working_orders=()),
        SimpleNamespace(account_id=ACCOUNT, generation_id=3, positions=(), working_orders=()),
    ]
    broker = SimpleNamespace(capture=lambda _account: snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
    calls, rows = [], {}

    def reduce(_position, _side, quantity, child_id):
        calls.append(("reduce", child_id))
        rows[child_id] = [SimpleNamespace(status="Filled", filled_quantity=quantity, total_quantity=quantity)]
    dispatch = SimpleNamespace(cancel=lambda *args: calls.append(("cancel", args)), reduce=reduce,
                               find_orders=lambda _account, child_id: rows.get(child_id, []),
                               get_order=lambda _entity: None)
    service = LiquidationService(broker, dispatch, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                                 now=lambda: NOW)
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

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_command_stack.py tests/integration/test_command_plane_activation.py tests/integration/test_p1_release_gate.py tests/integration/test_p3_release_gate.py -q --timeout=60`
Expected: all PASS (25 in `test_liquidation_service.py`). Then the full suite: green.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/liquidation_service.py trader/trading/command_stack.py scripts/command_plane_drill.py tests/test_liquidation_service.py tests/test_command_stack.py
git commit -m "feat: journaled liquidation children and one-transaction account claims

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 14: One reduce-only order path at the broker boundary (R11)

Today every liquidation reduce goes through `Trader.place_expressive_order` (`trader/trading/trading_runtime.py:1296`). That method runs the entry gates (`RiskGate.evaluate`: open orders, daily loss, concentration, signal rate; and the leverage check, `trading_runtime.py:1347-1437`). So an account flatten after a daily-loss breach is refused — after it may already have cancelled protection. This task adds `Trader.place_reduce_only_order`: it checks the account fence, that the side reduces IB's position, and that the quantity is at most that position, and nothing else. `reduce_position` (account flatten, full close) and the new `reduce_partial` move onto it. It also waits for a broker status of *this* order id (R13): the local `PendingSubmit` echo is not acceptance. Cancels from the liquidation run on the trader loop (`cancel_on_loop`). Exit legs (stop/target) are added in Task 10.

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`Trader`, `TradingRuntimeOrderDispatch.reduce_position` at line 2338)
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch.cancel`, new `reduce_partial`)
- Test: `tests/test_reduce_only_order_path.py` (create)

**Interfaces:**
- Produces on `Trader`:

```python
async def place_reduce_only_order(self, contract, action: str, quantity: float, *, order_ref: str,
                                  ack_timeout: float = 10.0) -> SuccessFail
    # refusal before IB: SuccessFail.fail(error="REDUCE_ONLY_REFUSED: ...")
    # after placeOrder: Submitted/PreSubmitted/Filled -> success([trade]);
    #                   Inactive/Cancelled/ApiCancelled -> fail(error="EXIT_ORDER_REJECTED: <status>");
    #                   no status for this order id in time -> fail(exception=TimeoutError)
async def _place_and_await_status(self, contract, order, timeout) -> tuple[str, Trade]
def _reduce_only_refusal(self, contract, action, quantity) -> Optional[str]
```

- Produces on `TradingRuntimeOrderDispatch`: `_run_on_trader_loop(coro)`, `_contract_for(position)`, `_refuse(detail)` (raises `DispatchRefused("REDUCE_ONLY_REFUSED", ...)`), `_reducing_side(position)`, `_reduce_only(position, side, quantity, order_ref, **order)`, `reduce_position(position, side, quantity, order_ref)` (exact size), `reduce_partial(position, side, quantity, order_ref)` (whole, strictly inside the position), `cancel_on_loop(order_entity_id, order_ref)` (`CancelUnresolved` → `DispatchRefused("CANCEL_UNRESOLVED", ...)`, ruling 7).
- Produces on `_LiquidationDispatch`: `cancel` → `cancel_on_loop`; `reduce_partial(position, side, quantity, child_id)`.
- No caller flag skips checks; `place_expressive_order` is not changed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reduce_only_order_path.py
"""SP1 plan 1 Tasks 14 and 10: the one reduce-only order path at the broker boundary."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import reactivex as rx
from ib_async import Contract

from trader.common.reactivex import SuccessFail
from trader.trading.liquidation_service import DispatchRefused
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.trading.trading_runtime import Trader, TradingRuntimeOrderDispatch

ACCOUNT = "DU12345"
CONID = 265598


def _trade(order, status):
    return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=status),
                           contract=SimpleNamespace(conId=CONID))


class _FakeExecutioner:
    """Each placed order emits its local echo, then the statuses the test scripted."""
    def __init__(self, script=("Submitted",), others=()):
        self.placed = []
        self.script = list(script)
        self.others = list(others)   # (order_id, status) events of other orders on the contract
        self._next_id = 100

    async def subscribe_place_order_direct(self, contract, order):
        self._next_id += 1
        order.orderId = self._next_id
        self.placed.append(order)
        events = [_trade(order, "PendingSubmit")]
        events += [_trade(SimpleNamespace(orderId=oid), status) for oid, status in self.others]
        events += [_trade(order, status) for status in self.script]
        return rx.from_iterable(events)


class _FakeIB:
    def __init__(self, held):
        self.held = held

    def positions(self, account=None):
        return [SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=self.held)]


def _trader(*, held=10.0, script=("Submitted",), others=(), daily_pnl=0.0):
    trader = object.__new__(Trader)
    trader.ib_account = ACCOUNT
    trader.client = SimpleNamespace(ib=_FakeIB(held))
    trader.executioner = _FakeExecutioner(script, others)
    trader.risk_gate = RiskGate(RiskLimits(max_daily_loss=1000.0), event_store=SimpleNamespace(count_since=lambda **_k: 0))
    trader.get_pnl = lambda: [SimpleNamespace(dailyPnL=daily_pnl)]
    trader.book = SimpleNamespace(get_open_order_count=lambda: 0)
    trader.check_order_margin = None
    return trader


def _contract():
    return Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")


def _run(coro):
    return asyncio.run(coro)


# -- Task 14: reduce-only market orders ----------------------------------------------------

def test_reduce_after_a_daily_loss_breach_passes_while_a_new_entry_is_refused():
    """R11: the entry gates refuse a BUY after the breach; the reduce-only path still reduces."""
    trader = _trader(daily_pnl=-5000.0)
    async def _no_margin(*_a):
        raise RuntimeError("skip margin")
    trader.check_order_margin = _no_margin
    trader.client.ib.accountValues = lambda: []
    entry = _run(trader.place_expressive_order(_contract(), "BUY", 5.0, {"order_type": "MARKET"}, algo_name="mmr:og-x"))
    assert not entry.is_success() and "daily loss" in str(entry.error)
    exit_ = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, order_ref="mmr:c-1-reduce-265598-1"))
    assert exit_.is_success()
    order = trader.executioner.placed[-1]
    assert (order.orderType, order.action, order.totalQuantity, order.orderRef) == ("MKT", "SELL", 10.0, "mmr:c-1-reduce-265598-1")
    assert (order.account, order.tif, order.transmit) == (ACCOUNT, "DAY", True)


@pytest.mark.parametrize("action,quantity,held", [("BUY", 5.0, 10.0), ("SELL", 11.0, 10.0), ("SELL", 0.0, 10.0),
                                                  ("SELL", 1.0, 0.0), ("SELL", 3.0, -10.0)])
def test_reduce_only_refuses_wrong_side_oversize_zero_or_no_position(action, quantity, held):
    trader = _trader(held=held)
    result = _run(trader.place_reduce_only_order(_contract(), action, quantity, order_ref="mmr:x"))
    assert not result.is_success() and str(result.error).startswith("REDUCE_ONLY_REFUSED")
    assert trader.executioner.placed == []


def test_reduce_only_refuses_without_a_pinned_account():
    trader = _trader()
    trader.ib_account = ""
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 1.0, order_ref="mmr:x"))
    assert str(result.error).startswith("REDUCE_ONLY_REFUSED")


def test_local_echo_alone_is_not_acceptance_and_times_out():
    """R13: PendingSubmit is our own echo; with no broker status the result is an exception."""
    trader = _trader(script=())
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, order_ref="mmr:x", ack_timeout=0.05))
    assert not result.is_success() and isinstance(result.exception, TimeoutError)


def test_status_of_another_order_on_the_contract_is_ignored():
    trader = _trader(script=(), others=((999, "Submitted"),))
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, order_ref="mmr:x", ack_timeout=0.05))
    assert isinstance(result.exception, TimeoutError)


def test_pending_submit_then_inactive_is_a_rejection():
    trader = _trader(script=("PendingSubmit", "Inactive"))
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 10.0, order_ref="mmr:x"))
    assert result.error == "EXIT_ORDER_REJECTED: Inactive"


def test_place_expressive_order_is_not_used_by_the_reduce_path():
    trader = _trader()
    def must_not_run(*_a, **_k):
        raise AssertionError("entry path used for an exit")
    trader.place_expressive_order = must_not_run
    assert _run(trader.place_reduce_only_order(_contract(), "SELL", 4.0, order_ref="mmr:x")).is_success()


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
                           quantity=quantity)


def test_dispatch_reduce_position_and_partial_use_the_reduce_only_path(loop_thread):
    trader = _trader()
    trader._main_loop = loop_thread.loop
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
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
    trader._main_loop = loop_thread.loop
    with pytest.raises(DispatchRefused):
        call(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0))
    assert trader.executioner.placed == []


def test_dispatch_maps_a_trader_refusal_to_dispatch_refused_and_a_timeout_to_an_exception(loop_thread):
    trader = _trader(held=3.0)
    trader._main_loop = loop_thread.loop
    with pytest.raises(DispatchRefused):            # IB now holds only 3: refused at the trader
        TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")
    silent = _trader(script=())
    silent._main_loop = loop_thread.loop
    async def slow(contract, action, quantity, **kwargs):
        return await Trader.place_reduce_only_order(silent, contract, action, quantity, ack_timeout=0.05, **kwargs)
    silent.place_reduce_only_order = slow
    with pytest.raises(TimeoutError):
        TradingRuntimeOrderDispatch(silent, dispatch_timeout=2.0).reduce_position(_position(10.0), "SELL", 10.0, "mmr:x")



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


def test_cancel_on_loop_runs_on_the_trader_loop_and_maps_unresolved_to_refused(loop_thread):
    trader = _trader()
    trader._main_loop = loop_thread.loop
    threads = []
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)

    def cancel(entity, ref):
        threads.append(threading.get_ident())
        return "ack"
    dispatch.cancel = cancel
    assert dispatch.cancel_on_loop("og-1:stop", "mmr:c") == "ack"
    assert threads == [loop_thread.thread.ident]

    from trader.trading.command_ports import CancelUnresolved

    def unresolved(entity, ref):
        raise CancelUnresolved("no live order")
    dispatch.cancel = unresolved
    with pytest.raises(DispatchRefused) as ex:
        dispatch.cancel_on_loop("og-1:stop", "mmr:c")
    assert ex.value.code == "CANCEL_UNRESOLVED"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py -q --timeout=30`
Expected: FAIL — `AttributeError: 'Trader' object has no attribute 'place_reduce_only_order'` (and on the dispatch: no `reduce_partial`, `cancel_on_loop`).

- [ ] **Step 3: Implement on `Trader`**

Add before `place_standalone_order` (line 1693):

```python
    _ACK_STATUSES = frozenset({'PreSubmitted', 'Submitted', 'Filled'})
    _DEAD_STATUSES = frozenset({'Inactive', 'Cancelled', 'ApiCancelled'})

    def _reduce_only_refusal(self, contract: Contract, action: str, quantity: float) -> Optional[str]:
        if not self.ib_account:
            return 'no ib_account is configured'
        held = sum(
            float(p.position) for p in self.get_positions()
            if int(p.contract.conId) == int(contract.conId)
            and (not getattr(p, 'account', None) or p.account == self.ib_account)
        )
        if held == 0:
            return f'no broker position on conid {contract.conId}'
        if action != ('SELL' if held > 0 else 'BUY'):
            return f'{action} does not reduce a position of {held:g}'
        if not 0 < float(quantity) <= abs(held):
            return f'quantity {quantity:g} is not within (0, {abs(held):g}]'
        return None

    async def place_reduce_only_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        *,
        order_ref: str,
        ack_timeout: float = 10.0,
    ) -> SuccessFail:
        """The one reduce-only order path (R11): full and partial reduce-only MARKET orders.

        It checks the account fence, that ``action`` reduces IB's position on
        this contract and that ``quantity`` is at most that position. It does
        not run the entry gates (RiskGate daily loss, open orders, rate,
        concentration, leverage): blocking an exit does not reduce risk.
        There is no flag to skip checks.

        A refusal before the IB call has the ``REDUCE_ONLY_REFUSED:`` prefix.
        After ``placeOrder`` the result follows the status of *this* order id
        (R13): the local PendingSubmit echo is not acceptance; Inactive or a
        cancel is ``EXIT_ORDER_REJECTED:``; no status in time is an exception.
        """
        refusal = self._reduce_only_refusal(contract, action, quantity)
        if refusal is not None:
            return SuccessFail.fail(error=f'REDUCE_ONLY_REFUSED: {refusal}')
        order = MarketOrder(action=action, totalQuantity=float(quantity), account=self.ib_account,
                            orderRef=order_ref, tif='DAY', outsideRth=False, transmit=True)
        try:
            status, trade = await self._place_and_await_status(contract, order, ack_timeout)
        except Exception as ex:
            return SuccessFail.fail(exception=ex)
        if status in self._DEAD_STATUSES:
            return SuccessFail.fail(error=f'EXIT_ORDER_REJECTED: {status}')
        return SuccessFail.success(obj=[trade])

    async def _place_and_await_status(self, contract: Contract, order: Order,
                                      timeout: float) -> Tuple[str, Trade]:
        """Place one order and wait for a broker status of that exact order id.

        The executioner stream is filtered by contract, not by order. The
        first emission is our own ``placeOrder`` echo, which fixes the order
        id; emissions for other orders on the contract are ignored.
        """
        done = asyncio.Event()
        seen: Dict[str, object] = {'order_id': None, 'status': None, 'trade': None, 'error': None}

        def on_next(trade: Trade):
            order_id = getattr(trade.order, 'orderId', None)
            if seen['order_id'] is None:
                seen['order_id'] = order_id
            if order_id != seen['order_id']:
                return
            status = getattr(trade.orderStatus, 'status', None)
            if status in self._ACK_STATUSES or status in self._DEAD_STATUSES:
                seen['status'], seen['trade'] = status, trade
                done.set()

        def on_error(ex):
            seen['error'] = ex
            done.set()

        observable = await self.executioner.subscribe_place_order_direct(contract, order)
        subscription = observable.subscribe(Observer(on_next=on_next, on_error=on_error,
                                                     on_completed=lambda: None))
        try:
            await asyncio.wait_for(done.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f'no broker status for order {seen["order_id"]} within {timeout}s')
        finally:
            subscription.dispose()
        if seen['error'] is not None:
            raise seen['error']
        return cast(str, seen['status']), cast(Trade, seen['trade'])
```

(`Tuple`, `cast`, `Dict`, `MarketOrder`, `Observer` are already imported at the top of the file.)

- [ ] **Step 4: Implement on `TradingRuntimeOrderDispatch` and `_LiquidationDispatch`**

Replace `reduce_position` (lines 2338–2370) with:

```python
    def _run_on_trader_loop(self, coro):
        """Run ``coro`` on the trader loop from a non-loop thread and wait for it."""
        loop = getattr(self._trader, '_main_loop', None)
        if loop is None or not loop.is_running():
            coro.close()
            raise RuntimeError('trader event loop unavailable for order dispatch')
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=self._dispatch_timeout)

    @staticmethod
    def _contract_for(position) -> Contract:
        return Contract(
            conId=int(position.conid), symbol=position.symbol,
            secType=getattr(position, 'sec_type', None) or 'STK',
            exchange=getattr(position, 'exchange', None) or 'SMART',
            currency=getattr(position, 'currency', None) or 'USD',
        )

    @staticmethod
    def _refuse(detail: str):
        from trader.trading.liquidation_service import DispatchRefused
        raise DispatchRefused('REDUCE_ONLY_REFUSED', detail)

    def _reducing_side(self, position) -> str:
        broker_quantity = float(position.quantity)
        if broker_quantity == 0:
            self._refuse('no position to reduce')
        return 'SELL' if broker_quantity > 0 else 'BUY'

    def _reduce_only(self, position, side: str, quantity: float, order_ref: str, **order):
        result = self._run_on_trader_loop(self._trader.place_reduce_only_order(
            self._contract_for(position), side, float(quantity), order_ref=order_ref, **order))
        if result.is_success():
            return result.obj or []
        error = str(result.error or '')
        if error.startswith('REDUCE_ONLY_REFUSED'):
            self._refuse(error)
        if result.exception is not None:
            raise result.exception
        raise RuntimeError(error or 'reduce-only dispatch failed')

    def reduce_position(self, position, side: str, quantity: float, order_ref: str):
        """Reduce-only MARKET order for the whole broker position (account flatten, full close)."""
        if side != self._reducing_side(position) or float(quantity) != abs(float(position.quantity)):
            self._refuse('a full reduce must exactly match the broker position')
        return self._reduce_only(position, side, quantity, order_ref)

    def reduce_partial(self, position, side: str, quantity: float, order_ref: str):
        """Reduce-only MARKET order for a strict whole-share part of the position."""
        held = abs(float(position.quantity))
        if side != self._reducing_side(position):
            self._refuse('a partial reduce must be on the reducing side')
        if not float(quantity).is_integer() or not 0 < float(quantity) < held:
            self._refuse('a partial reduce needs a whole quantity strictly between 0 and the position')
        return self._reduce_only(position, side, quantity, order_ref)

    def cancel_on_loop(self, order_entity_id: str, order_ref: str):
        """``cancel`` executed on the trader loop, as every IB call must be."""
        from trader.trading.command_ports import CancelUnresolved

        async def _cancel():
            return self.cancel(order_entity_id, order_ref)
        try:
            return self._run_on_trader_loop(_cancel())
        except CancelUnresolved as ex:
            # Nothing was sent to IB: a proven refusal before the boundary.
            from trader.trading.liquidation_service import DispatchRefused
            raise DispatchRefused('CANCEL_UNRESOLVED', str(ex)) from ex
```

In `trader/trading/command_stack.py`, in `_LiquidationDispatch` change `cancel` and add `reduce_partial`:

```python
    def cancel(self, order, child_id: str) -> None:
        self._dispatch.cancel_on_loop(order.order_entity_id, encode_order_ref(child_id))

    def reduce_partial(self, position, side: str, quantity: float, child_id: str) -> None:
        self._dispatch.reduce_partial(position, side, quantity, encode_order_ref(child_id))
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py tests/test_order_dispatch_ports.py tests/test_trading_runtime.py tests/test_liquidation_service.py -q --timeout=30`
Expected: all PASS (20 in the new file). Then the full suite: green.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_reduce_only_order_path.py
git commit -m "fix: exits use a reduce-only path that skips entry gates

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 15: One serialized liquidation worker (R12)

Today `_liquidation_recovery_loop` (`trader/trader_service.py:175`) and `_session_controller_loop` (`:208`) call `rescan()` / `run_due()` directly on the IB event loop. Those calls send orders with `run_coroutine_threadsafe(..., _main_loop).result()` — they wait on the loop they are blocking. The protective-failure producer calls `start` from the broker ingest thread while it holds the ingest apply lock (ruling 8). This task puts every `LiquidationService` entry point on one dedicated worker thread behind a facade, and makes each producer use it correctly: coroutines await it, RPC threads block on it, the ingest thread queues without waiting.

**Files:**
- Create: `trader/trading/liquidation_worker.py`
- Modify: `trader/trader_service.py` (`_liquidation_recovery_loop`, `_maybe_start_liquidation_recovery`, `_session_controller_loop`, `_maybe_start_session_recovery`)
- Modify: `trader/automation/session_controller.py` (`recover` → `restore` + `run_due`)
- Modify: `trader/trading/command_stack.py` (wrap the service; `CommandStack.liquidation_worker`)
- Test: `tests/test_liquidation_worker.py` (create)

**Interfaces:**

```python
class LiquidationWorker:
    def __init__(self, name: str = "liquidation-worker")
    def in_worker(self) -> bool
    def submit(self, fn, *args, **kwargs) -> concurrent.futures.Future
    def call(self, fn, *args, **kwargs)            # waits; inline on the worker; RuntimeError on a running loop
    async def run_async(self, fn, *args, **kwargs)
    def shutdown(self) -> None

class SerializedLiquidation:                       # what the stack, trader and producers hold
    def __init__(self, service, worker, *, account_id, now, deadline_seconds=300.0)
    worker: LiquidationWorker                      # property
    attach_protection(saga) -> None                # protection port + source of unhandled failures (Task 13)
    start(...) / rescan() / upgrade_to_zero(root_id) / liquidate(cmd)      # on the worker, waiting
    start_nowait(account_id, cause_command_id, deadline) -> Future
    nonblocking() -> object with start(account_id, cause_command_id, deadline) -> None   # for the saga
    tick() / async tick_async()                    # flatten unhandled SAFETY_FAILED sagas, then rescan
    async run_async(fn, *args, **kwargs)           # run another component (session controller) on the worker
    receipt_for(root_id) / root_for(command_id) / close_resolution(command_id)          # reads, any thread
SessionController.restore(now) -> SessionControllerState   # load or open the session; sends nothing
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

    def capture(self, account_id):
        if self.lock is not None:
            if not self.lock.acquire(timeout=2):
                raise TimeoutError("capture could not get the ingest lock")
            self.lock.release()
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


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
    trader._main_loop = loop
    trader.client = SimpleNamespace(ib=SimpleNamespace(positions=lambda account=None: [
        SimpleNamespace(account=ACCOUNT, contract=SimpleNamespace(conId=CONID), position=held)]))
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
    dispatch = _LiquidationDispatch(TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0), view)
    dispatch.find_orders = lambda account, child: []
    service = _service(tmp_path, _Broker([_snapshot(1, 0.0), _snapshot(2, 10.0)]), dispatch)
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
    dispatch = SimpleNamespace(reduce=lambda *a: None, find_orders=lambda *a: [], get_order=lambda e: None)
    service = _service(tmp_path, _Broker([_snapshot(1, 0.0)], lock=apply_lock), dispatch)
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
    dispatch = SimpleNamespace(reduce=lambda *a: None, find_orders=lambda *a: [], get_order=lambda e: None)
    service = _service(tmp_path, _Broker([_snapshot(1, 0.0)]), dispatch)
    liquidation = SerializedLiquidation(service, LiquidationWorker(), account_id=ACCOUNT, now=lambda: NOW)
    liquidation.attach_protection(SimpleNamespace(
        unhandled_failures=lambda account: ["entry-1"],
        handover_account=lambda **_k: None, close_after_full=lambda **_k: None))
    liquidation.tick()
    liquidation.tick()
    assert liquidation.root_for("entry-1") == "entry-1"
    assert liquidation.receipt_for("entry-1").scope == "account"


def test_trader_service_recovery_loop_ticks_through_the_worker(loop_thread):
    from trader import trader_service

    ticks = []
    worker = LiquidationWorker()
    service = SimpleNamespace(rescan=lambda: ticks.append(threading.get_ident()), root_for=lambda c: None,
                              attach_protection=lambda p: None)
    liquidation = SerializedLiquidation(service, worker, account_id=ACCOUNT, now=lambda: NOW)

    async def one_tick():
        task = asyncio.ensure_future(trader_service._liquidation_recovery_loop(liquidation, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
    loop_thread.run(one_tick())
    assert ticks and all(t == ticks[0] for t in ticks) and ticks[0] != loop_thread.thread.ident
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
service on the loop. Every producer goes through ``SerializedLiquidation``:

- a thread with no running loop (RPC handler) calls ``start`` and blocks;
- a coroutine on the trader loop awaits ``tick_async`` / ``run_async``;
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


class LiquidationWorker:
    def __init__(self, name: str = "liquidation-worker"):
        self._thread_id: Optional[int] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name,
                                            initializer=self._remember_thread)

    def _remember_thread(self) -> None:
        self._thread_id = threading.get_ident()

    def in_worker(self) -> bool:
        return threading.get_ident() == self._thread_id

    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

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

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)


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
        return self._worker.call(self._service.rescan)

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

    def tick(self):
        """Start a flatten for every SAFETY_FAILED saga that has none, then rescan every root."""
        return self._worker.call(self._tick)

    async def tick_async(self):
        return await self._worker.run_async(self._tick)

    async def run_async(self, fn: Callable[..., Any], *args, **kwargs):
        """Run another component (the session controller) on the same worker."""
        return await self._worker.run_async(fn, *args, **kwargs)

    def _tick(self):
        if self._saga is not None:
            for command_id in self._saga.unhandled_failures(self._account_id):
                if self._service.root_for(command_id) is None:
                    self._service.start(self._account_id, command_id,
                                        self._now() + dt.timedelta(seconds=self._deadline_seconds))
        return self._service.rescan()

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

(`tick` uses `unhandled_failures`, which the saga gets in Task 9; until Task 13 attaches the saga, `_saga` is `None` and the tick only rescans.)

- [ ] **Step 4: Route the loops through the worker**

`trader/trader_service.py`:

```python
async def _liquidation_recovery_loop(service, *, interval: float = 5.0) -> None:
    """Keep unresolved verified-liquidation roots moving after restart.

    Every transition still requires a newly promoted broker snapshot; a loop
    tick can never manufacture a flat result.  Errors are contained so an IB
    outage preserves the durable run for the next tick rather than killing the
    trader process.
    """
    while True:
        try:
            # R12: the tick runs on the liquidation worker; this coroutine only awaits it,
            # so the orders the worker sends onto this loop can run.
            receipt = await service.tick_async()
            if receipt is not None and receipt.state != 'FLAT':
                logging.warning('liquidation %s remains %s: %s', receipt.cause_command_id,
                                receipt.state, receipt.detail)
        except Exception as ex:
            logging.error('liquidation recovery tick failed: {}'.format(ex))
        await asyncio.sleep(interval)


def _maybe_start_liquidation_recovery(trader: Trader, loop: AbstractEventLoop) -> None:
    """Rescan durable liquidation roots before normal service operation."""
    service = getattr(trader, 'liquidation_service', None)
    if service is None:
        return
    try:
        # The first tick (cleanups, then every open root) runs as soon as the loop runs:
        # its orders need the running loop, so it cannot run before trader.run().
        loop.create_task(_liquidation_recovery_loop(service))
    except Exception as ex:
        logging.error('failed to start liquidation recovery: {}'.format(ex))


async def _session_controller_loop(controller, liquidation, *, interval: float = 5.0) -> None:
    """Tick absolute session deadlines until flat or incident (on the liquidation worker, R12)."""
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            state = await liquidation.run_async(controller.run_due, now)
            if state.state not in ('FLAT', 'INCIDENT', 'CLOSED'):
                logging.debug(
                    'session controller %s state=%s cutoff=%s',
                    state.session_date, state.state, state.entry_cutoff_reached,
                )
        except Exception as ex:
            logging.error('session controller tick failed: {}'.format(ex))
        await asyncio.sleep(interval)


def _maybe_start_session_recovery(trader: Trader, loop: AbstractEventLoop) -> None:
    """P3 Task 6: resume session deadlines BEFORE semantic readiness / run().

    Session recovery must precede readiness so a restart mid-flatten cannot
    open a window where automation is 'ready' but deadlines are unenforced.
    """
    controller = getattr(trader, 'session_controller', None)
    if controller is None:
        return
    try:
        now = dt.datetime.now(dt.timezone.utc)
        # Load durable deadlines now (no orders); the first run_due runs on the worker.
        state = controller.restore(now)
        if state.state not in ('FLAT', 'CLOSED'):
            logging.warning(
                'resumed session %s in state %s (incident=%s)',
                state.session_date, state.state, state.incident,
            )
        loop.create_task(_session_controller_loop(controller, trader.liquidation_service))
    except Exception as ex:
        logging.error('failed to start session recovery: {}'.format(ex))
```

`trader/automation/session_controller.py`: replace the head of `recover` and its last line so the loading part becomes `restore`:

```python
    def recover(self, now: dt.datetime) -> SessionControllerState:
        """Load durable state (or open today's session) and catch up deadlines."""
        self.restore(now)
        return self.run_due(_as_utc(now))

    def restore(self, now: dt.datetime) -> SessionControllerState:
        """Load durable state (or open today's session) without sending anything."""
        now_utc = _as_utc(now)
        schedule = self._calendar.resolve(now_utc)
        if schedule is None:
            session_date = now_utc.astimezone(ET).date()
            existing = self._store.load(self._account_id, session_date)
            if existing is not None:
                self._state = existing
                return existing
            state = self._closed_state(session_date)
            self._persist(state, now_utc)
            return state

        existing = self._store.load(self._account_id, schedule.session_date)
        if existing is not None and existing.state == "INCIDENT":
            # Never self-reset an incident across recover.
            self._state = existing
            return existing

        if existing is None:
            state = self._from_schedule(schedule, state="OPEN")
        else:
            # Rebind schedule fields from current calendar; preserve sticky flags.
            state = SessionControllerState(
                account_id=self._account_id,
                session_date=schedule.session_date,
                calendar_name=schedule.calendar_name,
                calendar_version=schedule.calendar_version,
                state=existing.state if existing.state != "CLOSED" else "OPEN",
                open_utc=schedule.open_utc,
                close_utc=schedule.close_utc,
                entry_cutoff_utc=schedule.entry_cutoff_utc,
                cancel_entries_utc=schedule.cancel_entries_utc,
                flatten_start_utc=schedule.flatten_start_utc,
                flat_deadline_utc=schedule.flat_deadline_utc,
                entry_cutoff_reached=existing.entry_cutoff_reached,
                flatten_command_id=existing.flatten_command_id,
                flat_generation=existing.flat_generation,
                incident=existing.incident,
                cancel_issued=existing.cancel_issued,
                flatten_issued=existing.flatten_issued,
                time_exits=existing.time_exits,
            )
        self._state = state
        self._persist(state, now_utc)
        return state
```

(`restore` is the old body of `recover`, unchanged except that it ends with `return state` instead of `return self.run_due(now_utc)`.)

`trader/trading/command_stack.py`:

1. Import `from trader.trading.liquidation_worker import LiquidationWorker, SerializedLiquidation`.
2. Wrap the service built in Task 4:

```python
    liquidation_worker = LiquidationWorker()
    liquidation_service = SerializedLiquidation(
        LiquidationService(
            broker_snapshot, _LiquidationDispatch(dispatch, orders_view),
            store=liquidation_store, registry=exit_owner_registry, now=now,
            breaker=_LiquidationBreaker(circuit_breaker, now),
            journal=journal, ledger=ledger,
        ),
        liquidation_worker, account_id=trader.ib_account, now=now,
    )
```

3. In `ProtectiveOrderSaga(` change `liquidation=liquidation_service,` to:

```python
        # The ingest thread reports protection failures; it must queue the flatten, not wait (R12).
        liquidation=liquidation_service.nonblocking(),
```

4. `CommandStack`: change the field to `liquidation_service: Any  # SerializedLiquidation: every entry point on one worker (R12)` and add `liquidation_worker: Any = None`; pass `liquidation_worker=liquidation_worker` in `CommandStack(...)`; add `trader.liquidation_worker = liquidation_worker` next to `trader.liquidation_service = liquidation_service`.

The session controller keeps `liquidation=liquidation_service` (the facade): its `run_due` runs on the worker, where the facade calls the service inline.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_worker.py tests/automation/test_session_controller.py tests/test_command_stack.py -q --timeout=60`
Expected: all PASS (6 in the new file; `test_trader_service_starts_session_recovery_before_readiness` still passes). Then the full suite: green.

- [ ] **Step 6: Commit**

```bash
git add trader/trading/liquidation_worker.py trader/trader_service.py trader/automation/session_controller.py trader/trading/command_stack.py tests/test_liquidation_worker.py
git commit -m "fix: run every liquidation entry point on one worker, off the ib loop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 5: Scoped full close (`scope="conid"`, goal zero)

`REQUESTED → CANCELLING → VERIFYING → REDUCING → VERIFYING → CLOSED`. Before the first cancel the service hands protection over (`ProtectionOwnershipPort.handover` with the exact order ids it will cancel). Cancels and the reduce are journaled children; nothing new is sent while a child is unknown; the reduce is sized only from a fresh generation; routine progress never trips the breaker. A second full close on the same conid joins the owner (one root, one order); the full join/upgrade/refuse rules are pinned in Task 8.

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`start`, `_tick`; new `_claim_scoped_in_tx`, `_position_for`, `_working_for`, `_advance_conid`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: Task 4 frozen model; `ExitOwnerRegistry.claim_scoped_in_tx`; `ProtectionOwnershipPort.handover`.
- Produces: `start(..., scope="conid", conid=...)` returns the receipt of the root the caller must poll (another root after a join). `phase` goes `"" → "cancel" → "reduce"`. Until Task 6, a request with `quantity` raises `LiquidationRefused("PARTIAL_CLOSE_UNAVAILABLE")` before anything is written (ruling 18).

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


def test_full_close_reduces_only_after_the_cancel_is_terminal_on_a_newer_generation(tmp_path):
    s = _stack(tmp_path, [
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], []),            # same generation: not proof
        _snapshot(2, [_position()], []),
    ], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    s.dispatch.entities["stop-1"] = _row("Cancelled")
    assert s.service.rescan().state == "VERIFYING"
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]
    receipt = s.service.rescan()
    assert receipt.phase == "reduce"
    assert s.dispatch.calls[-1] == ("reduce", 1, "SELL", 10.0, "close-1-reduce-1-1")


def test_full_close_waits_while_the_cancel_child_is_invisible(tmp_path):
    """#21: no row for the cancelled stop on the next generation is UNKNOWN, not gone."""
    s = _stack(tmp_path, [_snapshot(1, [_position()], [_stop_order()]), _snapshot(2, [_position()], [])],
               protection=_Protection())
    s.service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    receipt = s.service.rescan()
    assert "outcome unknown" in receipt.detail
    assert [c[0] for c in s.dispatch.calls] == ["cancel"]


def test_invisible_reduce_child_on_a_newer_generation_gets_no_second_scoped_reduce(tmp_path):
    s = _stack(tmp_path, [_snapshot(1, [_position()]), _snapshot(2, [_position()])], protection=_Protection())
    s.service.start(ACCOUNT, "close-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1)
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 13 failed (`ValueError: unknown liquidation scope 'conid'`), 25 passed.

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
            if conid is None:
                raise ValueError("conid scope requires a conid")
            if quantity is not None:
                raise LiquidationRefused("PARTIAL_CLOSE_UNAVAILABLE", "partial closes arrive in plan 1 task 6")
            outcome, root = self._store.transaction(lambda conn: self._claim_scoped_in_tx(
                conn, account_id, cause_command_id, int(conid), quantity, deadline, stop_price, target_price))
        else:
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if outcome in (CLAIMED, "EXISTING"):
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
        if self._now() >= receipt.deadline:
            return self._on_deadline(receipt)
        try:
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as exc:
            return self._snapshot_unavailable(receipt, f"broker snapshot unavailable: {exc}")
        if getattr(snapshot, "account_id", None) != receipt.account_id:
            return self._snapshot_unavailable(receipt, "broker snapshot account mismatch")
        receipt = self._observe_children(receipt, snapshot)
        if receipt.scope == "account":
            return self._advance_account(receipt, snapshot)
        return self._advance_conid(receipt, snapshot)
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

    def _advance_conid(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)
        if receipt.phase == "":
            info = HandoverInfo(None, None)
            if self._protection is not None:
                info = self._protection.handover(
                    account_id=receipt.account_id, conid=conid, close_root_id=receipt.cause_command_id,
                    cancels=_targets(working), generation=generation, now=self._now())
            receipt = self._set(
                receipt, "CANCELLING" if working else "VERIFYING", generation_id=generation, phase="cancel",
                opened_generation=generation,
                stop_price=receipt.stop_price if receipt.stop_price is not None else info.stop_price,
                target_price=receipt.target_price if receipt.target_price is not None else info.target_price,
                detail="protection handed over to the close")
        receipt = self._send_cancels(receipt, snapshot, working)
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

Why this order: `handover` runs before `_send_cancels`, so the saga is already `CLOSE_OWNED` with the exact ids when the broker reports the stop cancelled. `CLOSED` needs no working order on the conid, no blocking child, and a generation newer than the last action (`_last_action_generation`).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 38 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: conid-scoped full close with protection hand-over

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 6: Partial close, re-protect with a linked stop/target, recovery, escalation

After the cancels, the service sends a partial reduce of `q`, waits until that child is terminal and a generation newer than its fill, then re-protects the **actual** remainder with an exit-only OCA pair (`REPROTECTING → VERIFYING → DONE`). Per R13 the stop goes first; the target is journaled `PLANNED` (ruling 5) and sent only after the stop is accepted, sized from the live remaining position. `DONE` checks each leg's outstanding quantity, the OCA group, the protective side and a working status. Any re-protect failure, or a missed deadline in that phase, escalates to a full close and trips the breaker.

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `liquidation_child_id`, `reprotect_oca_group` (Task 2); `LiquidationDispatchPort.reduce_partial` / `place_exit_leg` (frozen in Task 4; real in Tasks 14 and 10).
- Produces: `upgrade_to_zero(root_id) -> LiquidationReceipt` (registry goal and run goal in one transaction; `PLANNED` children → `NOT_SENT`; a `reduce`/`reprotect` phase goes back to `cancel`, so replacement legs are cancelled). Quantity rules (R15): `q = floor(requested)`; `q < 1` → `LiquidationRefused("PARTIAL_QUANTITY_INVALID")`; `q ≥ |position|` → `LiquidationRefused("QUANTITY_ABOVE_POSITION")`; less than one share left → full close; live position `≤ q` (or less than one share left) at dispatch → close the live remainder. Stop side: long → stop below the market price, short → above; a target on the profit side; no market price → escalate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_liquidation_service.py`:

```python
# ---------------------------------------------------------------------------
# Task 6: partial close, re-protect, escalation
# ---------------------------------------------------------------------------

from trader.trading.liquidation_service import LiquidationRefused  # noqa: E402


def _priced(quantity=10.0, conid=1, price=100.0):
    return _position(quantity, conid=conid, market_price=price)


def _leg_row(status="Submitted", total=6.0, filled=0.0):
    return _row(status, filled=filled, total=total)


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
    """Review focus 5 / R20: crash after the stop was sent; the restart sends only the target."""
    s, _protection = _to_reprotect(tmp_path, extra=(_snapshot(4, [_priced(6.0)]),))
    sent = [c for c in s.dispatch.calls if c[0] == "place_exit_leg"]
    assert len(sent) == 1
    service = s.restart()
    s.dispatch.rows["p-1-reprotect-stop-1-1"] = [_leg_row()]
    service.rescan()
    legs = [c[2] for c in s.dispatch.calls if c[0] == "place_exit_leg"]
    assert legs == ["stop", "target"]


def test_unknown_reprotect_leg_is_never_resent(tmp_path):
    """R3: a stop leg whose send timed out stays UNKNOWN; it is never placed again."""
    s = _stack(tmp_path, [_snapshot(1, [_priced(10.0)]), _snapshot(1, [_priced(10.0)]), _snapshot(2, [_priced(6.0)]), _snapshot(3, [_priced(6.0)]),
                          _snapshot(4, [_priced(6.0)])], protection=_Protection(target_price=None))
    s.dispatch.fail_after_send.add("place_exit_leg")
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    s.dispatch.rows["p-1-reduce-1-1"] = [_row("Filled", filled=4.0, total=4.0)]
    s.service.rescan()
    s.service.rescan()                                  # gen 3: stop sent, ack timed out
    receipt = s.service.rescan()                        # gen 4: still no row -> still UNKNOWN
    assert [c.state for c in receipt.children if c.kind == "reprotect-stop"] == ["UNKNOWN"]
    assert len([c for c in s.dispatch.calls if c[0] == "place_exit_leg"]) == 1


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
    s, _protection = _to_reprotect(tmp_path)
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
```

Note: a partial `start` reads one snapshot to admit the quantity, so these tests list generation 1 twice.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 16 failed (`PARTIAL_CLOSE_UNAVAILABLE`, `AttributeError: ... 'upgrade_to_zero'`), 38 passed.

- [ ] **Step 3: Implement**

1. Import: `from trader.trading.order_correlation import liquidation_child_id, reprotect_oca_group`.
2. In `start`, replace the `PARTIAL_CLOSE_UNAVAILABLE` line with:

```python
            if quantity is not None:
                quantity = self._admit_partial(account_id, int(conid), quantity)
```

3. Add after `close_resolution`:

```python
    def upgrade_to_zero(self, root_id: str) -> LiquidationReceipt:
        """partial -> zero for an active scoped root, registry and run in one transaction."""
        def write(conn):
            self._registry.upgrade_goal_in_tx(conn, root_id, self._now())
            self._upgrade_run_in_tx(conn, root_id, "goal upgraded to zero exposure")
        self._store.transaction(write)
        return self._store.receipt(root_id)
```

4. Add after `_claim_scoped_in_tx`:

```python
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

5. Replace `_on_deadline`, `_cleanup`, `_submit_reduces` and `_advance_conid` with:

```python
    def _on_deadline(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        if receipt.scope == "conid" and receipt.phase == "reprotect" and not receipt.escalated:
            self._escalate(receipt, "REPROTECT_DEADLINE: re-protect missed its deadline")
            return self._store.receipt(receipt.cause_command_id)
        return self._finish(receipt, "FAILED_SAFE", detail="deadline elapsed without broker-confirmed result")
```

```python
    def _cleanup(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """R8: every step is idempotent; recovery re-runs it until the flag clears."""
        root = receipt.cause_command_id
        if self._protection is not None:
            if receipt.state in ("CLOSED", "FLAT"):
                self._protection.close_after_full(close_root_id=root, now=self._now())
            elif receipt.state == "DONE":
                stop, target = self._working_legs(receipt)
                self._protection.release_after_partial(
                    close_root_id=root, remaining_quantity=float(receipt.remaining_quantity),
                    stop_group=stop.child_id, target_group=None if target is None else target.child_id,
                    now=self._now())

        def write(conn):
            if receipt.state in SUCCESS_STATES:
                self._registry.finish_in_tx(conn, root, STATE_RELEASED, self._now())
            run = self._store.get_run_in_tx(conn, root)
            self._store.update_run_in_tx(conn, replace(run, cleanup_pending=False), self._now())
        self._store.transaction(write)
        self._resolve_commands(root)
        return self._store.receipt(root)
```

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
        receipt = self._set(self._store.receipt(receipt.cause_command_id), "REDUCING", generation_id=generation,
                            phase="reduce" if receipt.scope == "conid" else receipt.phase,
                            detail="submitting reduce-only orders")
        for child, position in zip(children, positions):
            send = self._dispatch.reduce if partial is None else self._dispatch.reduce_partial
            self._send(receipt, child, lambda p=position, c=child, s=send: s(p, c.side, c.quantity, c.child_id))
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          "reduction submitted; awaiting broker evidence")
```

```python
    def _advance_conid(self, receipt, snapshot) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)
        if receipt.phase == "":
            info = HandoverInfo(None, None)
            if self._protection is not None:
                info = self._protection.handover(
                    account_id=receipt.account_id, conid=conid, close_root_id=receipt.cause_command_id,
                    cancels=_targets(working), generation=generation, now=self._now())
            receipt = self._set(
                receipt, "CANCELLING" if working else "VERIFYING", generation_id=generation, phase="cancel",
                opened_generation=generation,
                stop_price=receipt.stop_price if receipt.stop_price is not None else info.stop_price,
                target_price=receipt.target_price if receipt.target_price is not None else info.target_price,
                detail="protection handed over to the close")
        if receipt.phase == "reprotect":
            return self._advance_reprotect(receipt, snapshot, working, position)
        receipt = self._send_cancels(receipt, snapshot, working)
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
            if any(c.kind == "reduce" and c.state in ("FILLED", "CANCELLED", "REJECTED", "ABSENT")
                   for c in receipt.children):
                return self._start_reprotect(receipt, snapshot, position)
            return self._submit_partial(receipt, snapshot, position)
        return self._submit_reduces(receipt, snapshot, (position,))
```

6. Add after `_advance_conid`:

```python
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
        legs = [c for c in receipt.children if c.kind == kind]
        return max(legs, key=lambda c: c.attempt) if legs else None

    def _working_legs(self, receipt) -> tuple[ChildRef, Optional[ChildRef]]:
        return self._latest(receipt, "reprotect-stop"), self._latest(receipt, "reprotect-target")

    def _start_reprotect(self, receipt, snapshot, position) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        if receipt.stop_price is None:
            return self._escalate_now(receipt, snapshot, "STOP_PRICE_MISSING: no stop price for re-protect")
        problem = self._protective(position, float(receipt.stop_price), receipt.target_price)
        if problem is not None:
            return self._escalate_now(receipt, snapshot, problem)
        remaining = abs(float(position.quantity))
        side = _reducing_side(position.quantity)
        conid = int(receipt.conid)

        def build(conn):
            attempt = self._store.next_attempt_in_tx(conn, receipt.cause_command_id, "reprotect-stop", conid)
            group = reprotect_oca_group(receipt.cause_command_id, conid, attempt)
            legs = [self._new_child(conn, receipt, kind="reprotect-stop", conid=conid, generation=generation,
                                    side=side, quantity=remaining, price=float(receipt.stop_price), oca_group=group)]
            if receipt.target_price is not None:
                legs.append(self._new_child(conn, receipt, kind="reprotect-target", conid=conid,
                                            generation=generation, state="PLANNED", side=side,
                                            quantity=remaining, price=float(receipt.target_price), oca_group=group))
            return legs
        legs = self._reserve(receipt, build)
        if legs is None:
            return self._store.receipt(receipt.cause_command_id)
        receipt = self._set(self._store.receipt(receipt.cause_command_id), "REPROTECTING",
                            generation_id=generation, phase="reprotect",
                            detail="re-protect stop submitted; target waits for the stop")
        self._send_leg(receipt, legs[0], position)
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          "awaiting broker acceptance of the re-protect stop")

    def _send_leg(self, receipt, leg: ChildRef, position) -> None:
        self._send(receipt, leg, lambda: self._dispatch.place_exit_leg(
            position, leg="stop" if leg.kind == "reprotect-stop" else "target", quantity=leg.quantity,
            price=leg.price, oca_group=leg.oca_group, child_id=leg.child_id))

    def _retry_leg(self, receipt, snapshot, leg: ChildRef, position) -> LiquidationReceipt:
        """R3: only a NOT_SENT leg is tried again, under a new attempt id, in the same OCA group."""
        generation = int(snapshot.generation_id)
        remaining = abs(float(position.quantity))
        new = self._reserve(receipt, lambda conn: [self._new_child(
            conn, receipt, kind=leg.kind, conid=leg.conid, generation=generation, side=leg.side,
            quantity=remaining, price=leg.price, oca_group=leg.oca_group)])
        if new:
            self._send_leg(receipt, new[0], position)
        return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                          f"{leg.kind} retried after a refusal before the broker")

    def _advance_reprotect(self, receipt, snapshot, working, position) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        stop, target = self._working_legs(receipt)
        if any(c.state == "UNKNOWN" for c in receipt.children):
            return self._wait(receipt, generation, "awaiting broker evidence for a re-protect leg")
        if stop.state == "NOT_SENT":
            return self._retry_leg(receipt, snapshot, stop, position) if position is not None else \
                self._finish_reprotect_closed(receipt, snapshot, working)
        if stop.state in ("REJECTED", "CANCELLED", "ABSENT"):
            return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: stop leg {stop.state}")
        if not self._fresh(receipt, generation):
            return self._wait(receipt, generation, "awaiting a broker generation newer than the last leg fill")
        if position is None:
            return self._finish_reprotect_closed(receipt, snapshot, working)
        remaining = abs(float(position.quantity))
        if target is not None:
            if target.state == "PLANNED":
                if stop.state != "WORKING":
                    return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: stop leg {stop.state} with a position left")
                sized = replace(target, state="UNKNOWN", quantity=remaining, fence_generation=generation)

                def promote(conn):
                    self._store.update_child_in_tx(conn, sized, self._now())
                    return [sized]
                if self._reserve(receipt, promote):
                    self._send_leg(receipt, sized, position)
                return self._wait(self._store.receipt(receipt.cause_command_id), generation,
                                  "re-protect target submitted for the live remaining position")
            if target.state == "NOT_SENT":
                return self._retry_leg(receipt, snapshot, target, position)
            if target.state != "WORKING":
                return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: target leg {target.state}")
        legs = [stop] + ([target] if target is not None else [])
        if generation <= max(leg.fence_generation for leg in legs):
            return self._wait(receipt, generation, "awaiting a generation newer than the re-protect legs")
        side = _reducing_side(position.quantity)
        for leg in legs:
            if leg.state != "WORKING" or leg.oca_group != stop.oca_group or leg.side != side:
                return self._escalate_now(receipt, snapshot, f"REPROTECT_FAILED: {leg.child_id} is not a working protective leg")
            if leg.outstanding_quantity != remaining:
                return self._wait(receipt, generation,
                                  f"{leg.child_id} outstanding {leg.outstanding_quantity} != position {remaining}")
        return self._finish(receipt, "DONE", generation_id=generation, remaining_quantity=remaining,
                            detail="re-protect legs working in one OCA group for the remaining quantity")

    def _finish_reprotect_closed(self, receipt, snapshot, working) -> LiquidationReceipt:
        generation = int(snapshot.generation_id)
        receipt = self._send_cancels(receipt, snapshot, working)
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

Notes: `_escalate` never captures a snapshot; `_escalate_now` continues on the snapshot it holds, and `escalated=True` makes a second deadline miss `FAILED_SAFE`, so there is no loop. A `NOT_SENT` leg (proven refusal) is retried under attempt `n+1` in the pair's OCA group (R3, ruling 6); an `UNKNOWN` leg never is.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 54 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: partial close with a linked re-protect and escalation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 7: Account flatten takes over scoped closes in the right order

Spec 5.1, "An account flatten takes over every scoped owner": (1) claim and mark the scoped owners **and their runs** `SUPERSEDED` and inherit their open children, all in one transaction (R6, R9); (2) superseded closes stop at once (their `PLANNED` legs become `NOT_SENT`; `update_run_in_tx` refuses to move a `SUPERSEDED` run); (3) the inherited children are reconciled from broker evidence like any child; (4) every identified working order is cancelled, replacement protection included; (5) no reduce while any child is unknown or the last fill is not yet behind a newer generation; (6) then reduce. A deadline with a child still unknown ends `FAILED_SAFE`, never a second order. `handover_account` takes every live saga, including sagas a scoped close already owns (R14).

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
    s.service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    receipt = s.service.start(ACCOUNT, "kill-1", NOW + dt.timedelta(seconds=30))
    assert [c.child_id for c in receipt.children] == ["p-1-reduce-1-1"]
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 6 failed (the scoped runs are not `SUPERSEDED`, so they keep re-protecting and reducing), 55 passed. `test_later_scoped_close_inherits_the_unknown_child_of_a_failed_safe_close` already passes: inheritance came with Task 4; the test pins it for the scoped case.

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
Expected: 61 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: account flatten supersedes scoped closes and inherits their children

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 8: Scoped `start` claims through the registry — join, upgrade, refuse

A scoped request never creates a competing root. The registry decides inside the claim transaction: `CLAIMED` creates the run; `JOINED` and `JOINED_FLATTEN` record the join row and return the owner's receipt (the caller polls that root, R10); `UPGRADED` moves the owner's registry goal **and** its run to zero in the same transaction (R6), so a crash can never leave a `zero` owner with a `partial` cursor; a partial request against any owner raises `ExitInProgress` and writes nothing. The account owner is checked first and a `SUPERSEDED` owner is never upgraded (registry rules, Task 3; `_upgrade_run_in_tx` also refuses a terminal run). A retried command id returns its durable root, after a restart too, and cannot be rebound (ruling 17).

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`_claim_scoped_in_tx`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `UPGRADED` from `trader.trading.exit_owner`; `_upgrade_run_in_tx` (Task 6).
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: 1 failed — `test_time_exit_during_partial_close_upgrades_goal_and_ends_closed` (the registry says `zero`, the run still says `partial`, so it re-protects); 68 passed. The other new tests pin rules that Tasks 3–7 already give (atomic claim, durable join, no revival of a superseded close).

- [ ] **Step 3: Implement**

Import `UPGRADED` (`from trader.trading.exit_owner import (CLAIMED, JOINED_FLATTEN, STATE_ACTIVE, STATE_FAILED_SAFE, STATE_RELEASED, UPGRADED, ExitOwnerRegistry)`) and replace `_claim_scoped_in_tx` with:

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
        elif claim.outcome == UPGRADED:
            self._upgrade_run_in_tx(conn, claim.root_id, f"goal upgraded to zero by {cause}")
        return (claim.outcome, claim.root_id)
```

`start` does not advance a joined or upgraded root (it returns `self._store.receipt(root)`): the owner acts on its own ticks, so a joining caller never sends an order on a snapshot the owner already used.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_liquidation_service.py tests/test_exit_owner.py -q --timeout=30`
Expected: 83 passed. Then the full suite: green.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: scoped closes join, upgrade or refuse through the exit owner registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 9: Protective saga — `CLOSE_OWNED`, exact hand-over, release, retired legs

While a close owns protection, a cancel of one of the exact order ids the close asked to cancel is expected and is not `MISSING_PROTECTION`. `Inactive`/`Rejected`, or a cancel of any other order, still takes today's incident path (R14, ruling 12). After a partial close the saga goes back to `PROTECTED` for the remaining quantity with the new legs as its only live protection (a new protection generation); events of retired legs never change it. After a full close it is `CLOSED`. An account flatten takes every live saga, including those a scoped close owns.

**Files:**
- Modify: `trader/automation/protective_order_saga.py`
- Modify: `trader/trading/broker_ingest.py` (`_notify_protective_saga`, line 1274: pass `order_entity_id`)
- Test: `tests/automation/test_protective_order_saga.py`, `tests/automation/test_attribution_ledger.py`

**Interfaces (the saga implements `ProtectionOwnershipPort` from Task 4):**

```python
PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 37
def apply_protective_order_saga_migration(migrator) -> bool       # applies 30 and 37; returns 30's result
SAGA_STATES |= {"CLOSE_OWNED"}
BrokerOrderEvent: + order_entity_id: Optional[str] = None
SagaState: + close_root_id: Optional[str] = None, + expected_cancel_ids: tuple[str, ...] = (),
           + handover_generation: Optional[int] = None, + protection_generation: int = 0,
           + active_groups: tuple[str, ...] = (); property current_groups
ProtectiveOrderSagaStore.load_by_group(order_group_id) -> Optional[tuple[SagaState, int]]   # (saga, group generation)
ProtectiveOrderSagaStore.load_live(account_id, conid=None) -> list[SagaState]   # not CLOSED / SAFETY_FAILED
ProtectiveOrderSagaStore.load_by_close_root(close_root_id) -> list[SagaState]
ProtectiveOrderSagaStore.load_safety_failed(account_id) -> list[SagaState]
ProtectiveOrderSaga.handover(*, account_id, conid, close_root_id, cancels, generation, now) -> HandoverInfo
ProtectiveOrderSaga.handover_account(*, account_id, close_root_id, cancels, generation, now) -> None
ProtectiveOrderSaga.release_after_partial(*, close_root_id, remaining_quantity, stop_group, target_group, now) -> None
ProtectiveOrderSaga.close_after_full(*, close_root_id, now) -> None
ProtectiveOrderSaga.unhandled_failures(account_id) -> list[str]   # SAFETY_FAILED command ids (Task 15's tick)
```

- Migration 37: `account_id`, `conid`, `close_root_id` columns on `automated_order_sagas` (backfilled from the payload) and `automated_order_saga_groups(order_group_id PK, command_id, protection_generation)`.
- Hand-over prices come from `plan_json`: the `stop` leg's `stop_price`, the `take_profit` leg's `limit_price`.
- Several saga rows change in one journal transaction (`_persist_all` over `mutate_batch_work`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/automation/test_protective_order_saga.py`:

```python
# ---------------------------------------------------------------------------
# SP1 plan 1 Task 9: close ownership (CLOSE_OWNED), hand-over, release
# ---------------------------------------------------------------------------

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
                               stop_group="p-1-reprotect-stop-265598-1",
                               target_group="p-1-reprotect-target-265598-1", now=NOW)
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
                               stop_group="p-1-reprotect-stop-265598-1", target_group=None, now=NOW)
    state = saga.on_broker_event(_event(og, leg="stop", status="Cancelled", order_id=2, event_id="late-old-stop"))
    assert state.state == "PROTECTED"
    assert breaker.signals == [] and liquidation.starts == []
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="p-2", cancels=(), generation=9, now=NOW)
    saga.release_after_partial(close_root_id="p-2", remaining_quantity=3.0,
                               stop_group="p-2-reprotect-stop-265598-1", target_group=None, now=NOW)
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
```

Append to `tests/automation/test_attribution_ledger.py` (uses `_forwarded_events` from Task 2):

```python
def test_broker_ingest_forwards_the_order_entity_id_to_the_saga(tmp_path):
    """SP1 plan 1 Task 9 (R14): the saga matches expected cancels by order identity."""
    seen = _forwarded_events(tmp_path, order_ref=encode_order_ref(ORDER_GROUP), order_type="STP",
                             name="entity-id.duckdb")
    assert seen[0].order_entity_id is not None
    assert seen[0].order_entity_id.startswith(ORDER_GROUP)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py -q --timeout=30`
Expected: FAIL — `ImportError: cannot import name 'PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION'`, `TypeError: BrokerOrderEvent.__init__() got an unexpected keyword argument 'order_entity_id'`, `AttributeError: ... 'handover'`.

- [ ] **Step 3: Implement**

Constants: add `PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 37` under the migration-30 constants and `"CLOSE_OWNED"` to `SAGA_STATES`. Replace the migration function:

```python
def apply_protective_order_saga_migration(migrator: SchemaMigrator) -> bool:
    """Journal migrations 30 (saga rows) and 37 (close ownership, protection groups).

    Returns whether migration 30 was newly applied, as before.
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
        """UPDATE automated_order_sagas
           SET account_id = json_extract_string(payload, '$.account_id'),
               conid = CAST(json_extract(payload, '$.conid') AS INTEGER)
           WHERE account_id IS NULL""",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_groups (
            order_group_id VARCHAR PRIMARY KEY,
            command_id VARCHAR NOT NULL,
            protection_generation INTEGER NOT NULL
        )""",
    ))
    return applied
```

Replace `BrokerOrderEvent`:

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
```

In `SagaState` add after `plan_json`:

```python
    close_root_id: Optional[str] = None
    expected_cancel_ids: tuple[str, ...] = ()
    handover_generation: Optional[int] = None
    protection_generation: int = 0
    active_groups: tuple[str, ...] = ()

    @property
    def current_groups(self) -> tuple[str, ...]:
        """Order groups of the protection that is live now (older groups are retired)."""
        return self.active_groups or (self.order_group_id,)
```

and the same five fields to `to_payload` (`"close_root_id"`, `"expected_cancel_ids": list(...)`, `"handover_generation"`, `"protection_generation"`, `"active_groups": list(...)`) and to `from_payload`:

```python
            close_root_id=payload.get("close_root_id"),
            expected_cancel_ids=tuple(payload.get("expected_cancel_ids") or ()),
            handover_generation=payload.get("handover_generation"),
            protection_generation=int(payload.get("protection_generation", 0)),
            active_groups=tuple(payload.get("active_groups") or ()),
```

In `ProtectiveOrderSagaStore` replace `load_by_group` and `save_in_tx`, and add the loaders:

```python
    def load_by_group(self, order_group_id: str) -> Optional[tuple[SagaState, int]]:
        """The saga that owns ``order_group_id`` and the protection generation of that group.

        A re-protect group is in ``automated_order_saga_groups``; the entry
        bracket group is the saga row itself (generation 0).
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
        terminal = sorted(_TERMINAL_SAGA)
        where = "account_id = ? AND state NOT IN (?, ?)"
        params: list = [account_id, *terminal]
        if conid is not None:
            where += " AND conid = ?"
            params.append(int(conid))
        return self._load_many(where, params)

    def load_by_close_root(self, close_root_id: str) -> list[SagaState]:
        return self._load_many("state = 'CLOSE_OWNED' AND close_root_id = ?", [close_root_id])

    def load_safety_failed(self, account_id: str) -> list[SagaState]:
        return self._load_many("account_id = ? AND state = 'SAFETY_FAILED'", [account_id])

    def save_in_tx(self, conn, state: SagaState, now: dt.datetime) -> None:
        payload = json.dumps(state.to_payload(), sort_keys=True, default=str)
        conn.execute("DELETE FROM automated_order_sagas WHERE command_id = ?", [state.command_id])
        conn.execute(
            "INSERT INTO automated_order_sagas "
            "(command_id, order_group_id, state, payload, updated_at, account_id, conid, close_root_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [state.command_id, state.order_group_id, state.state, payload, now,
             state.account_id, int(state.conid), state.close_root_id],
        )
        for group in state.active_groups:
            conn.execute(
                "INSERT OR REPLACE INTO automated_order_saga_groups VALUES (?, ?, ?)",
                [group, state.command_id, state.protection_generation],
            )
```

Replace `on_broker_event`:

```python
    def on_broker_event(self, event: BrokerOrderEvent) -> SagaState:
        found = self._store.load_by_group(event.order_group_id)
        if found is None:
            raise KeyError(f"no saga for order_group_id={event.order_group_id!r}")
        state, group_generation = found
        if event.event_id in state.seen_event_ids:
            return state
        if state.state in _TERMINAL_SAGA:
            return state

        now = self._now_utc()
        if group_generation != state.protection_generation:
            # A retired leg: record the event, never let it change today's protection.
            return self._record_only(state, event, now)
        if state.state == "CLOSE_OWNED":
            return self._on_owned_event(state, event, now)
        updated = self._apply_event(state, event)
        if updated is state:
            # Still record the event id for idempotency even if no transition.
            updated = replace(
                state,
                seen_event_ids=state.seen_event_ids + (event.event_id,),
                revision=state.revision + 1,
            )
            self._persist(updated, now, from_state=state.state, event_id=event.event_id)
            return updated

        updated = replace(
            updated,
            seen_event_ids=state.seen_event_ids + (event.event_id,),
            revision=state.revision + 1,
        )
        self._persist(updated, now, from_state=state.state, event_id=event.event_id)

        if updated.state == "SAFETY_FAILED" and state.state != "SAFETY_FAILED":
            self._trip_and_liquidate(updated, now)
        return updated
```

Add before `# -- event application`:

```python
    def _record_only(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        recorded = replace(state, seen_event_ids=state.seen_event_ids + (event.event_id,),
                           revision=state.revision + 1)
        self._persist(recorded, now, from_state=state.state, event_id=event.event_id)
        return recorded

    def _on_owned_event(self, state: SagaState, event: BrokerOrderEvent, now: dt.datetime) -> SagaState:
        """R14: only a cancel of a ref the close asked to cancel is expected."""
        expected = event.order_entity_id is not None and event.order_entity_id in state.expected_cancel_ids
        lost = event.status in _REJECTED_STATUSES or (event.status in _CANCELLED_STATUSES and not expected)
        if lost:
            failed = replace(
                state, state="SAFETY_FAILED", error_code="PROTECTION_LOST_DURING_CLOSE",
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

    def _own(self, states: list[SagaState], close_root_id: str, cancels, generation: int) -> list[SagaState]:
        owned = []
        for state in states:
            groups = set(state.current_groups)
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
        states = self._store.load_live(account_id, conid)
        self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)], _as_utc(now))
        return self._prices_from_plan(states[0]) if states else HandoverInfo(None, None)

    def handover_account(self, *, account_id: str, close_root_id: str, cancels, generation: int,
                         now: dt.datetime) -> None:
        states = self._store.load_live(account_id)
        self._persist_all([(s, None) for s in self._own(states, close_root_id, cancels, generation)], _as_utc(now))

    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float, stop_group: str,
                              target_group: Optional[str], now: dt.datetime) -> None:
        """Back to PROTECTED for the remainder; the new legs become the only live protection."""
        states = self._store.load_by_close_root(close_root_id)
        if not states:
            return
        keeper, merged = states[0], states[1:]
        remaining = _dec(remaining_quantity)
        groups = (stop_group,) + ((target_group,) if target_group else ())
        released = replace(
            keeper, state="PROTECTED", close_root_id=None, expected_cancel_ids=(), handover_generation=None,
            protection_generation=keeper.protection_generation + 1, active_groups=groups,
            requested_quantity=remaining, filled_quantity=remaining, protection_quantity=remaining,
            protection_working=True, stop_working=True, target_working=target_group is not None,
            stop_filled=False, target_filled=False, stop_rejected=False, target_rejected=False,
            entry_working=False, entry_cancelled=False, error_code=None, revision=keeper.revision + 1,
        )
        closed = [replace(s, state="CLOSED", close_root_id=None, error_code="PROTECTION_MERGED",
                          revision=s.revision + 1) for s in merged]
        self._persist_all([(released, None)] + [(s, None) for s in closed], _as_utc(now))

    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None:
        closed = [replace(s, state="CLOSED", error_code=None, close_root_id=None, stop_working=False,
                          target_working=False, revision=s.revision + 1)
                  for s in self._store.load_by_close_root(close_root_id)]
        self._persist_all([(s, None) for s in closed], _as_utc(now))

    def unhandled_failures(self, account_id: str) -> list[str]:
        """SAFETY_FAILED sagas; the liquidation worker makes sure each one started a flatten."""
        return [s.command_id for s in self._store.load_safety_failed(account_id)]
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
        """Several saga rows in one journal transaction."""
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

In `trader/trading/broker_ingest.py` `_notify_protective_saga`, add `order_entity_id=order.order_entity_id,` as the last argument of `BrokerOrderEvent(...)`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py tests/test_broker_ingest.py -q --timeout=30`
Expected: all PASS (37 in the saga file), including `test_migration_30_creates_automated_order_sagas_table`.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/protective_order_saga.py trader/trading/broker_ingest.py tests/automation/test_protective_order_saga.py tests/automation/test_attribution_ledger.py
git commit -m "feat: protective saga hands exact protection over to a close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 10: Exit legs by order identity — stop and target of the re-protect pair

The re-protect stop and target go through the same reduce-only method as every exit (R11). This task teaches `place_reduce_only_order` STP and LMT legs with a price and an OCA group (`ocaType=2`: a fill of one leg reduces the other to what is left), and adds `place_exit_leg` on the dispatch and on `_LiquidationDispatch`. The service sends the legs one by one (Task 6), so there is no `place_exit_oca` that places both: each leg follows the status of its own order id (R13). A real IB paper session still has to prove `ocaType=2` (plan 6).

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`Trader._reduce_only_refusal`, `Trader.place_reduce_only_order`, new `TradingRuntimeOrderDispatch.place_exit_leg`)
- Modify: `trader/trading/command_stack.py` (`_LiquidationDispatch.place_exit_leg`)
- Test: `tests/test_reduce_only_order_path.py`

**Interfaces:**

```python
async def Trader.place_reduce_only_order(self, contract, action, quantity, *, order_ref: str,
                                         order_type: str = "MKT", price: Optional[float] = None,
                                         oca_group: Optional[str] = None, ack_timeout: float = 10.0) -> SuccessFail
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
    _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, order_ref="mmr:p-1-reprotect-stop-265598-1",
                                        order_type="STP", price=95.0, oca_group="p-1-reprotect-265598-1"))
    _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, order_ref="mmr:p-1-reprotect-target-265598-1",
                                        order_type="LMT", price=120.0, oca_group="p-1-reprotect-265598-1"))
    stop, target = trader.executioner.placed
    assert (stop.orderType, stop.auxPrice, target.orderType, target.lmtPrice) == ("STP", 95.0, "LMT", 120.0)
    for order in (stop, target):
        assert (order.ocaGroup, order.ocaType, order.transmit, order.parentId) == ("p-1-reprotect-265598-1", 2, True, 0)


def test_stop_that_fills_before_its_ack_is_reported_as_filled():
    trader = _trader(held=6.0, script=("Filled",))
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, order_ref="mmr:s", order_type="STP",
                                                 price=95.0, oca_group="g"))
    assert result.is_success() and result.obj[0].orderStatus.status == "Filled"


def test_target_that_goes_inactive_after_its_echo_is_rejected():
    trader = _trader(held=6.0, script=("PendingSubmit", "Inactive"))
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, order_ref="mmr:t", order_type="LMT",
                                                 price=120.0, oca_group="g"))
    assert result.error == "EXIT_ORDER_REJECTED: Inactive"


@pytest.mark.parametrize("order_type,price", [("STP", None), ("LMT", 0.0), ("TRAIL", 1.0)])
def test_exit_leg_without_a_valid_type_or_price_is_refused(order_type, price):
    trader = _trader(held=6.0)
    result = _run(trader.place_reduce_only_order(_contract(), "SELL", 6.0, order_ref="mmr:x",
                                                 order_type=order_type, price=price, oca_group="g"))
    assert str(result.error).startswith("REDUCE_ONLY_REFUSED") and trader.executioner.placed == []


def test_dispatch_place_exit_leg_derives_side_and_type(loop_thread):
    trader = _trader(held=-6.0)
    trader._main_loop = loop_thread.loop
    dispatch = TradingRuntimeOrderDispatch(trader, dispatch_timeout=2.0)
    dispatch.place_exit_leg(_position(-6.0), leg="stop", quantity=6.0, price=105.0, oca_group="g", order_ref="mmr:s")
    order = trader.executioner.placed[-1]
    assert (order.action, order.orderType, order.auxPrice) == ("BUY", "STP", 105.0)
    with pytest.raises(DispatchRefused):
        dispatch.place_exit_leg(_position(-6.0), leg="trail", quantity=6.0, price=1.0, oca_group="g", order_ref="mmr:x")


def test_liquidation_dispatch_sends_exit_legs_with_the_child_id_as_order_ref():
    from trader.trading.command_stack import _LiquidationDispatch

    calls = []
    inner = SimpleNamespace(place_exit_leg=lambda p, **kw: calls.append(kw))
    _LiquidationDispatch(inner, SimpleNamespace()).place_exit_leg(
        SimpleNamespace(conid=CONID, quantity=6.0), leg="stop", quantity=6.0, price=95.0, oca_group="g",
        child_id="p-1-reprotect-stop-265598-1")
    assert calls == [{"leg": "stop", "quantity": 6.0, "price": 95.0, "oca_group": "g",
                      "order_ref": "mmr:p-1-reprotect-stop-265598-1"}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py -q --timeout=30`
Expected: 8 failed (`TypeError: ... unexpected keyword argument 'order_type'`, no `place_exit_leg`), 20 passed.

- [ ] **Step 3: Implement**

In `Trader`, add `_REDUCE_ONLY_TYPES = ('MKT', 'STP', 'LMT')` next to `_ACK_STATUSES` and replace the two methods:

```python
    def _reduce_only_refusal(self, contract: Contract, action: str, quantity: float,
                             order_type: str, price: Optional[float]) -> Optional[str]:
        if order_type not in self._REDUCE_ONLY_TYPES:
            return f'order type {order_type!r} is not a reduce-only type'
        if order_type != 'MKT' and (price is None or not price > 0):
            return f'{order_type} needs a positive price'
        if not self.ib_account:
            return 'no ib_account is configured'
        held = sum(
            float(p.position) for p in self.get_positions()
            if int(p.contract.conId) == int(contract.conId)
            and (not getattr(p, 'account', None) or p.account == self.ib_account)
        )
        if held == 0:
            return f'no broker position on conid {contract.conId}'
        if action != ('SELL' if held > 0 else 'BUY'):
            return f'{action} does not reduce a position of {held:g}'
        if not 0 < float(quantity) <= abs(held):
            return f'quantity {quantity:g} is not within (0, {abs(held):g}]'
        return None

    async def place_reduce_only_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        *,
        order_ref: str,
        order_type: str = 'MKT',
        price: Optional[float] = None,
        oca_group: Optional[str] = None,
        ack_timeout: float = 10.0,
    ) -> SuccessFail:
        """The one reduce-only order path (R11): full reduce, partial reduce, exit legs.

        It checks the account fence, that ``action`` reduces IB's position on
        this contract and that ``quantity`` is at most that position. It does
        not run the entry gates (RiskGate daily loss, open orders, rate,
        concentration, leverage): blocking an exit does not reduce risk.
        There is no flag to skip checks.

        A refusal before the IB call has the ``REDUCE_ONLY_REFUSED:`` prefix.
        After ``placeOrder`` the result follows the status of *this* order id
        (R13): the local PendingSubmit echo is not acceptance; Inactive or a
        cancel is ``EXIT_ORDER_REJECTED:``; no status in time is an exception.
        """
        refusal = self._reduce_only_refusal(contract, action, quantity, order_type, price)
        if refusal is not None:
            return SuccessFail.fail(error=f'REDUCE_ONLY_REFUSED: {refusal}')
        common = dict(action=action, totalQuantity=float(quantity), account=self.ib_account,
                      orderRef=order_ref, tif='DAY', outsideRth=False, transmit=True)
        if order_type == 'MKT':
            order: Order = MarketOrder(**common)
        elif order_type == 'STP':
            order = StopOrder(stopPrice=float(price), **common)
        else:
            order = LimitOrder(lmtPrice=float(price), **common)
        if oca_group:
            order.ocaGroup = oca_group
            order.ocaType = 2  # a fill reduces the sibling to what is left
        try:
            status, trade = await self._place_and_await_status(contract, order, ack_timeout)
        except Exception as ex:
            return SuccessFail.fail(exception=ex)
        if status in self._DEAD_STATUSES:
            return SuccessFail.fail(error=f'EXIT_ORDER_REJECTED: {status}')
        return SuccessFail.success(obj=[trade])
```

In `TradingRuntimeOrderDispatch` add after `reduce_partial`:

```python
    def place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
                       oca_group: str, order_ref: str):
        """One exit-only leg (stop or target) of a re-protect OCA pair."""
        if leg not in ('stop', 'target'):
            self._refuse(f'unknown exit leg {leg!r}')
        side = self._reducing_side(position)
        return self._reduce_only(position, side, quantity, order_ref,
                                 order_type='STP' if leg == 'stop' else 'LMT',
                                 price=float(price), oca_group=oca_group)
```

In `_LiquidationDispatch` (command_stack) add:

```python
    def place_exit_leg(self, position, *, leg: str, quantity: float, price: float,
                       oca_group: str, child_id: str) -> None:
        self._dispatch.place_exit_leg(position, leg=leg, quantity=quantity, price=price,
                                      oca_group=oca_group, order_ref=encode_order_ref(child_id))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_reduce_only_order_path.py tests/test_trading_runtime.py -q --timeout=30`
Expected: all PASS (28 in the file).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_reduce_only_order_path.py
git commit -m "feat: exit-only stop and target legs on the reduce-only path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 16: The session cancel phase cancels unfilled entries only (R16)

`_issue_cancel` (`trader/automation/session_controller.py:640`) passes **every** working order to `SessionCancelAdapter`, which cancels each one (`:272-282`). A filled position's protective stop is therefore cancelled at the cancel deadline, before the flatten owns protection; the saga reads that bare cancel as `MISSING_PROTECTION` and starts an avoidable emergency liquidation. The cancel phase now selects only working entry orders with zero fill. Protective children, external orders and partly filled entries are left to the flatten, which hands protection over first.

**Files:**
- Modify: `trader/automation/session_controller.py` (`_issue_cancel`)
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Produces: `_issue_cancel` passes `orders = [o for o in working if o.leg == "entry" and o.filled_quantity == 0]` to `CancelPort.cancel_working_entries`; the port is unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/automation/test_session_controller.py` replace `test_partial_fill_during_cancel_still_cancels_remainder` with the two tests below (the old one asserted the behaviour R16 removes):

```python
def test_partially_filled_entry_is_left_to_the_flatten(tmp_path):
    """R16: a partly filled entry owns protection for its filled part; the flatten handles it."""
    working = (_order("ord-partial", filled=4.0, total=10.0),)
    broker = FakeBroker([_snapshot(1, positions=[_position(4.0)], working=working)])
    controller, _b, cancel, _l, _br, _t, _j, _db, clock = _build_controller(
        tmp_path, broker=broker,
    )
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert cancel.calls == []
    assert state.cancel_issued is True


def test_session_cancel_entries_keeps_protective_children(tmp_path):
    """R16 / #27: stops and targets of a filled position and external orders are not cancelled."""
    working = (
        _order("og-1:entry", leg="entry"),
        _order("og-2:stop", leg="stop"),
        _order("og-2:take_profit", leg="take_profit"),
        _order("ext-1", leg=None, is_external=True),
    )
    broker = FakeBroker([_snapshot(1, positions=[_position()], working=working)])
    controller, _b, cancel, _l, breaker, _t, _j, _db, clock = _build_controller(tmp_path, broker=broker)
    clock[0] = _utc(15, 35)
    controller.recover(clock[0])
    controller.run_due(clock[0])
    assert [c[1] for c in cancel.calls] == ["og-1:entry"]
    assert breaker.signals == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py -q --timeout=30 -k "partially_filled_entry or keeps_protective"`
Expected: 2 failed (the stop, target, external order and the partly filled entry are cancelled).

- [ ] **Step 3: Implement**

In `_issue_cancel`, replace the block from `snapshot = self._broker.capture(...)` to the `cancel_working_entries` call with:

```python
            snapshot = self._broker.capture(self._account_id)
            entries = tuple(
                order for order in getattr(snapshot, "working_orders", ()) or ()
                if getattr(order, "leg", None) == "entry"
                and float(getattr(order, "filled_quantity", 0.0) or 0.0) == 0.0
            )
            # R16: only unfilled entries. Protective children and partly filled
            # entries are left to the flatten, which owns their protection.
            if entries:
                self._cancel.cancel_working_entries(
                    root_command_id=root, orders=entries,
                )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py -q --timeout=30`
Expected: all PASS (`test_cancel_entries_at_cancel_deadline`, `test_external_position_is_included_in_flatten` and `test_delayed_run_due_catches_up_all_missed_deadlines_in_order` still pass: their working orders are unfilled entries, and the flatten still starts).

- [ ] **Step 5: Commit**

```bash
git add trader/automation/session_controller.py tests/automation/test_session_controller.py
git commit -m "fix: session cancel phase cancels unfilled entries only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 11: Time exits use the scoped close; the session flatten polls the root it got back

`SessionTimeExitAdapter` asks the liquidation service for a conid-scoped full close instead of calling `reduce` (Task 1's test turns green). `_issue_flatten` persists the root `start` returned — after a join that is another producer's root (R10) — and `_poll_flat` reads exactly that root with `receipt_for`, never another root's `FLAT`.

**Files:**
- Modify: `trader/automation/session_controller.py` (`LiquidationPort`, `SessionTimeExitAdapter`, `_issue_flatten`, `_poll_flat`; drop the unused `SimpleNamespace` import)
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

2. Replace the Task 1 test (remove the `xfail` marker; the name and the final assertions stay) and add a helper:

```python
def _real_liquidation(tmp_path, broker):
    from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
    from trader.trading.liquidation_service import (
        LiquidationRunStore, LiquidationService, apply_liquidation_migration,
    )
    db = DuckDBConnection.get_instance(str(tmp_path / "liquidation.duckdb"))
    migrator = SchemaMigrator(db)
    apply_exit_owner_migration(migrator)
    apply_liquidation_migration(migrator)
    return LiquidationService(broker, broker, store=LiquidationRunStore(db), registry=ExitOwnerRegistry(db),
                              now=lambda: _utc(15, 0))


def test_time_exit_leaves_no_live_stop_after_the_position_is_closed(tmp_path):
    """Spec 5.1: a time exit cancels the protective stop first, then closes from broker truth."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    broker = _SimBroker()
    service = _real_liquidation(tmp_path, broker)
    adapter = SessionTimeExitAdapter(service, account_id=ACCOUNT, now=lambda: _utc(15, 0))
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    for _ in range(4):
        service.rescan()
    assert broker.calls == [("cancel", "og-1:stop"), ("reduce", "SELL", 10.0)]
    assert broker.quantity == 0.0
    assert broker.stop_working is False
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
    restarted.restore(_utc(15, 47))
    liquidation.mark_flat(generation_id=4, root_id="kill-1")
    state = restarted.run_due(_utc(15, 48))
    assert (state.state, state.flatten_command_id, state.flat_generation) == ("FLAT", "kill-1", 4)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py -q --timeout=30`
Expected: FAIL — the time-exit tests (`TypeError: SessionTimeExitAdapter.__init__() got an unexpected keyword argument 'account_id'`), `test_poll_flat_ignores_another_roots_flat_receipt` (another root's FLAT is accepted), the join test (`flatten_command_id` is the session's own cause).

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

In `_issue_flatten` replace the `start` call and the `flatten_command_id=` argument:

```python
        receipt = self._liquidation.start(self._account_id, cause, deadline)
        # R10: persist and poll the root we got back; it is another root after a join.
        root = getattr(receipt, "cause_command_id", None) or cause
        state = self._evolve(
            state,
            state="FLATTENING",
            flatten_command_id=root,
```

In `_poll_flat` replace the receipt lookup:

```python
        receipt = None
        try:
            self._liquidation.rescan()
            if state.flatten_command_id:
                receipt = self._liquidation.receipt_for(state.flatten_command_id)
        except Exception:
            receipt = None
```

In `trader/trading/command_stack.py` (line 902) the session controller gets:

```python
        time_exit=SessionTimeExitAdapter(liquidation_service, account_id=trader.ib_account, now=now),
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/automation/test_session_controller.py tests/test_command_stack.py -q --timeout=30`
Expected: all PASS, no xfail left.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/session_controller.py trader/trading/command_stack.py tests/automation/test_session_controller.py
git commit -m "fix: time exits close through the scoped liquidation and poll their own root

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 12: One-strategy SELL intents become a proven-reduction close

A SELL intent on the old path is an exit of the held long (long-only model). It must never go through `build_bracket_plan`, which adds a reverse BUY stop. On a fresh fenced snapshot the trader checks: there is a position on the conid; SELL reduces it; the requested quantity (if any) is at most the position. Anything else is refused `NOT_A_REDUCTION` (an entry attempt). The close is then a scoped root keyed by the command id, or the root it joins; the command sits in `OUTCOME_UNKNOWN` (`CLOSE_PENDING`) with the root it must follow, and is scheduled for reconciliation (Task 17 resolves it). The close skips `session_risk` and, through Task 14, the entry gates, so it works after a loss breach (spec 5.1).

**Files:**
- Modify: `trader/automation/automated_intent_command.py`
- Test: `tests/automation/test_automated_command_boundary.py`

**Interfaces:**
- Consumes: `LiquidationService.start(..., scope="conid", conid, quantity)` (through the facade), `ExitInProgress` (Task 3), `LiquidationRefused` (Task 4), `BrokerRiskSnapshot.reducible_quantity` (`trader/data/broker_state.py:279`).
- Produces: `AutomatedIntentCommandService(..., liquidation: Optional[Any] = None, broker: Optional[Any] = None, close_deadline_seconds: float = 300.0)`. With both set, every SELL intent takes the close path after the artifact and claim checks; BUY intents are unchanged. Refusal codes: `NOT_A_REDUCTION`, `EXIT_IN_PROGRESS`, `BROKER_SNAPSHOT_UNAVAILABLE`, and the `LiquidationRefused` codes (`PARTIAL_QUANTITY_INVALID`, ...). Accepted close: `OUTCOME_UNKNOWN`, `error_code="CLOSE_PENDING"`, `outcome={"close_root_id", "liquidation_state", "generation_id", "detail"}`. `requested == held` becomes a full close (ruling 10).

- [ ] **Step 1: Write the failing tests**

In `tests/automation/test_automated_command_boundary.py` give `_build_stack` two keyword parameters and pass them on:

```python
def _build_stack(tmp_path: Path, *, dispatch=None, verifier=None, now=None, liquidation=None, broker=None):
    ...
        expected_artifact_id=ARMED_ARTIFACT_ID,
        schedule_reconcile=schedule.schedule,
        liquidation=liquidation,
        broker=broker,
    )
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
        return SimpleNamespace(account_id=account_id, generation_id=1,
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_automated_command_boundary.py -q --timeout=30`
Expected: FAIL — `TypeError: AutomatedIntentCommandService.__init__() got an unexpected keyword argument 'liquidation'`.

- [ ] **Step 3: Implement**

Add the three constructor parameters and store them (`self._liquidation`, `self._broker`, `self._close_deadline_seconds`). In `execute`, right after the claim block and before `# Task 5 path`:

```python
        # A SELL on the long-only path is an exit, never a bracket (spec 5.1).
        if intent.side == "SELL" and self._liquidation is not None and self._broker is not None:
            return self._execute_close(cmd, intent)
```

Add before `_finish_submitted`:

```python
    def _execute_close(self, cmd, intent) -> CommandReceipt:
        """Prove the SELL reduces the held long on a fresh fenced snapshot, then close."""
        from trader.trading.exit_owner import ExitInProgress
        from trader.trading.liquidation_service import LiquidationRefused

        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception as ex:
            return self._reject_close(cmd, "BROKER_SNAPSHOT_UNAVAILABLE", {"detail": str(ex)})
        held = float(snapshot.reducible_quantity(intent.conid))
        requested = None if intent.requested_quantity is None else float(intent.requested_quantity)
        if held <= 0 or (requested is not None and requested > held):
            return self._reject_close(cmd, "NOT_A_REDUCTION", {"held": held, "requested": requested})
        # A close of the whole position takes the broker quantity at reduce time.
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

A command that starts or joins a close (SELL intent, `/flatten`, session flatten) has a row in `liquidation_joins` (Task 4). Today `OutcomeReconciler._try_resolve` (`trader/trading/command_coordinator.py:2671`) has no branch for `execute_automated_intent` or `liquidate_account`, so a joining command stays `OUTCOME_UNKNOWN` for ever and blocks `reconciliation_complete`. The new branch reads `LiquidationRunStore.close_resolution(command_id)`: it follows `SUPERSEDED` to the account root that took over, and resolves only on a broker-proven goal with cleanup finished (`CLOSED`/`FLAT` for a full request, also `DONE` for a partial one). `FAILED_SAFE`, an open root, or a command with no join row (a bracket entry) stays unresolved. A command still in `SUBMITTING` (the root ended before `CLOSE_PENDING` was written) resolves too.

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
    return SimpleNamespace(db=db, journal=journal, ledger=ledger, store=store, reconciler=reconciler)


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


@pytest.mark.parametrize("state,cleanup_pending", [("FAILED_SAFE", False), ("VERIFYING", False), ("CLOSED", True)])
def test_failed_safe_open_or_uncleaned_root_never_becomes_success(env, state, cleanup_pending):
    _root(env, "exit-1", state, cleanup_pending=cleanup_pending)
    _join(env, "sell-5", "exit-1", "zero")
    _command(env, "sell-5")
    assert env.reconciler.reconcile_once("sell-5", NOW).resolved is False
    assert _state(env, "sell-5") == "OUTCOME_UNKNOWN"


def test_done_resolves_a_partial_request_but_not_a_full_one(env):
    _root(env, "p-1", "DONE", goal="partial")
    _join(env, "sell-6", "p-1", "partial", quantity=4.0, outcome="CLAIMED")
    _join(env, "sell-7", "p-1", "zero")
    _command(env, "sell-6")
    _command(env, "sell-7")
    assert env.reconciler.reconcile_once("sell-6", NOW).resolved is True
    assert env.reconciler.reconcile_once("sell-7", NOW).resolved is False


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


def test_flatten_cleanup_resolves_the_root_and_every_joined_flatten_command(env):
    """R10 + R17: two /flatten commands, one root, both resolved once FLAT is proven."""
    snapshots = [_snapshot(1, 10.0), _snapshot(2, 0.0), _snapshot(3, 0.0)]
    rows = {}
    broker = SimpleNamespace(capture=lambda a: snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
    dispatch = SimpleNamespace(reduce=lambda p, s, q, cid: rows.setdefault(cid, [SimpleNamespace(
        status="Filled", filled_quantity=q, total_quantity=q)]), find_orders=lambda a, cid: rows.get(cid, []),
        get_order=lambda e: None)
    service = LiquidationService(broker, dispatch, store=env.store, registry=ExitOwnerRegistry(env.db),
                                 now=lambda: NOW, journal=env.journal, ledger=env.ledger)
    for command_id in ("flatten-a", "flatten-b"):
        _command(env, command_id, action="liquidate_account")
        service.start(ACCOUNT, command_id, DEADLINE)
    service.rescan()
    assert service.rescan().state == "FLAT"
    assert (_state(env, "flatten-a"), _state(env, "flatten-b")) == ("RESOLVED", "RESOLVED")
    assert env.ledger.get("flatten-b").outcome["close_root_id"] == "flatten-a"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_close_reconciliation.py -q --timeout=30`
Expected: FAIL — `TypeError: OutcomeReconciler.__init__() got an unexpected keyword argument 'closes'`.

- [ ] **Step 3: Implement**

Add `closes: Optional[Any] = None` as the last constructor parameter and `self._closes = closes`. In `_try_resolve`, before the "Unmapped action" fallback:

```python
        if action in ("execute_automated_intent", "liquidate_account"):
            return self._reconcile_close(row, now)
```

Add before `_reconcile_create`:

```python
    def _reconcile_close(self, row: LedgerRow, now: dt.datetime) -> bool:
        """R17: a command that started or joined a close root resolves from that exact root.

        SUPERSEDED is followed to the account root that took over. Only a
        broker-proven goal (CLOSED / DONE / FLAT, cleanup finished) resolves;
        FAILED_SAFE or an open root leaves the command OUTCOME_UNKNOWN. A
        command with no close root (a bracket entry) is not decided here.
        """
        if self._closes is None:
            return False
        resolution = self._closes.close_resolution(row.command_id)
        if resolution is None or not resolution.success:
            return False
        self._resolve_command_only(row, dict(resolution.outcome), now)
        return True
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_close_reconciliation.py tests/test_command_coordinator.py -q --timeout=60`
Expected: all PASS (13 in the new file).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_coordinator.py tests/test_close_reconciliation.py
git commit -m "feat: reconcile close commands from the exact root they joined

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---


### Task 13: Wire it into `build_command_stack` and prove it end to end (R21)

The rest of the composition: the generation refresh (ruling 1), the saga as protection port and failure source for the worker, the reconciler's close resolver, the intent service's close path on cold start **and** hot-arm, and the registry on the stack. Then the integration tests run the **real** `build_command_stack` on a temporary DuckDB journal and a real asyncio loop: real registry, run store, worker, saga, session controller, coordinator, reconciler, `RiskGate`, `TradingRuntimeOrderDispatch` and `Trader.place_reduce_only_order`. Only the broker is fake (`_BrokerSim` plays IB and writes promoted generations into the journal, as broker sync would). The smaller state-machine tests of Tasks 4–8 stay.

**Files:**
- Modify: `trader/trading/command_stack.py` (`_BrokerGenerationRefresh`, `_build_automated_intent_service` line 479 and its two call sites, the `OutcomeReconciler(` call, the `LiquidationService(` call, after `ProtectiveOrderSaga(`, `CommandStack`, the `trader.*` assignments)
- Test: `tests/test_safe_close_integration.py` (create)

**Interfaces:**
- Produces: `CommandStack.exit_owner_registry: Any = None`; `trader.exit_owner_registry`. `_BrokerGenerationRefresh(trader, *, min_interval_seconds=5.0, clock=None)` implements `GenerationRefreshPort`. `_build_automated_intent_service(..., liquidation: Any = None)`.
- Consumes: everything from Tasks 2–17.

#29 cases and where they run here: two account producers (`test_two_account_producers_make_one_root_and_one_reduce`); restart at a boundary (`test_restart_mid_close_continues_from_the_journal_without_a_second_order`; every R20 boundary is in Tasks 4–8); invisible child and late fill (`test_invisible_child_and_a_late_fill_never_send_a_second_reduce`); terminal partial fills, replacement protection, retired legs (`test_partial_close_reprotects_and_a_late_retired_leg_keeps_protection`); exits after a loss breach (`test_after_a_loss_breach_a_close_reduces_and_a_new_entry_is_refused`); the real loop (every test ticks with `tick_async` on the trader loop); OCA rejection (`test_target_leg_rejected_by_the_broker_escalates_to_a_full_close`); cancel-entry deadline and the returned root (`test_session_cancel_keeps_protection_and_the_flatten_owns_it`); joined commands (`test_joined_flatten_command_resolves_from_its_root_through_the_reconciler`); cold start and hot-arm wiring (first two tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_safe_close_integration.py
"""SP1 plan 1 Task 13 (R21): the safe close through the real ``build_command_stack``.

Real: DuckDB journal, exit owner registry, liquidation run store, worker,
protective saga, session controller, coordinator, reconciler, RiskGate,
``TradingRuntimeOrderDispatch`` and ``Trader.place_reduce_only_order`` on a
real asyncio loop. Fake: only the broker (``_BrokerSim`` plays IB and writes
promoted broker generations into the journal, as broker sync would).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
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
from trader.trading.order_correlation import classify_leg, decode_order_ref
from trader.trading.risk_gate import RiskGate, RiskLimits
from trader.trading.trading_runtime import Trader

UTC = dt.timezone.utc
ACCOUNT = "DU111111"
CONID = 265598
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
        self.add_order(entity, group, leg, order.action, order.orderType, order.totalQuantity, order=order)
        echo = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PendingSubmit"))
        ack = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status=self.ack[order.orderType]))
        return rx.from_iterable([echo, ack])

    # -- broker state ------------------------------------------------------------------------
    def add_order(self, entity, group, leg, action, order_type, quantity, *, status="Submitted", order=None):
        self._next_perm += 0 if order is not None else 1
        perm = order.permId if order is not None else self._next_perm
        self.perm[entity] = perm
        self.trades[entity] = SimpleNamespace(order=order or SimpleNamespace(permId=perm))
        self.orders[entity] = BrokerOrderRow(
            order_entity_id=entity, account_id=ACCOUNT, conid=CONID, symbol="AAPL", order_group_id=group,
            leg=leg, is_external=False, action=action, order_type=order_type, total_quantity=float(quantity),
            filled_quantity=0.0, avg_fill_price=None, limit_price=None, stop_price=None, tif="DAY",
            status=status, deleted=False, revision=1, source_timestamp=_et(11, 0))

    def set_status(self, entity, status, *, filled=None, total=None):
        from dataclasses import replace
        row = self.orders[entity]
        self.orders[entity] = replace(row, status=status,
                                      filled_quantity=row.filled_quantity if filled is None else float(filled),
                                      total_quantity=row.total_quantity if total is None else float(total))
        if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.trades.pop(entity, None)

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
        return db.transaction(write)


class _Universe:
    def resolve_symbol(self, conid, **_kwargs):
        return [SimpleNamespace(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART",
                                primaryExchange="NASDAQ", currency="USD")] if conid == CONID else []


class _Composed:
    def __init__(self, tmp_path, loop_thread, clock):
        from trader.trading.command_stack import build_command_stack

        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        store = BrokerStateStore(db)
        store.migrate(migrator)
        trader = object.__new__(Trader)
        self.trader, self.clock, self.loop_thread = trader, clock, loop_thread
        self.sim = _BrokerSim(trader)
        trader.journal_db, trader.domain_journal, trader.broker_state_store = db, journal, store

        async def no_sync(_client):
            return True
        trader.broker_ingest = SimpleNamespace(is_ready=lambda: True, run_broker_sync=no_sync)
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

    def protected_entry(self, *, quantity=10.0, stop=95.0, target=120.0, command_id="entry-1"):
        """A durable PROTECTED saga and its working stop and target, as after a filled bracket."""
        og = f"og-{command_id}"
        state = SagaState(
            command_id=command_id, order_group_id=og, order_ref=f"mmr:{og}", state="PROTECTED",
            account_id=ACCOUNT, conid=CONID, side="BUY", requested_quantity=Decimal(str(quantity)),
            filled_quantity=Decimal(str(quantity)), protection_quantity=Decimal(str(quantity)),
            protection_working=True, stop_working=True, target_working=True, revision=3,
            plan_json={"legs": [{"role": "stop", "stop_price": str(stop)},
                                {"role": "take_profit", "limit_price": str(target)}]})
        self.saga._persist(state, self.clock[0], from_state=None)
        self.sim.held[CONID] = quantity
        self.sim.add_order(f"{og}:stop", og, "stop", "SELL", "STP", quantity, status="PreSubmitted")
        self.sim.add_order(f"{og}:take_profit", og, "take_profit", "SELL", "LMT", quantity)
        return og

    def saga_event(self, og, leg, entity, status, filled=0.0):
        return self.saga.on_broker_event(BrokerOrderEvent(og, leg, status, filled, 10.0, 1,
                                                          f"{entity}:{status}:{filled}", self.clock[0],
                                                          order_entity_id=entity))


@pytest.fixture
def composed(tmp_path):
    loop_thread = _LoopThread()
    clock = [_et(11, 0)]
    yield _Composed(tmp_path, loop_thread, clock)
    loop_thread.stop()


def test_cold_start_wires_registry_worker_saga_time_exit_and_reconciler(composed):
    stack, trader = composed.stack, composed.trader
    assert trader.exit_owner_registry is stack.exit_owner_registry
    assert trader.liquidation_service is stack.liquidation_service
    assert trader.liquidation_worker is stack.liquidation_worker
    assert stack.session_controller._time_exit._liquidation is stack.liquidation_service
    assert composed.saga._liquidation._serialized is stack.liquidation_service
    assert stack.reconciler._closes is not None


def test_hot_arm_builds_the_intent_service_with_the_close_path(tmp_path, composed):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from trader.research.signing import public_key_pem

    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "verify.pem").write_bytes(public_key_pem(ed25519.Ed25519PrivateKey.generate().public_key()))
    (tmp_path / "artifacts").mkdir()
    trader = composed.trader
    trader.automation_enabled, trader.automation_live_enabled = True, False
    trader.automation_public_key_ring_path = str(keys)
    trader.automation_artifact_bundle_path = str(tmp_path / "artifacts")
    trader.automation_expected_artifact_id = "artifact-test-1"
    service = composed.stack.paper_hot_arm._build_intent_service(trader)
    assert service._liquidation is composed.stack.liquidation_service
    assert service._broker is not None


def test_two_account_producers_make_one_root_and_one_reduce(composed):
    """#23 through the stack: the session flatten joins the /flatten root."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    first = composed.liquidation.start(ACCOUNT, "flatten-ui-1", _et(15, 59))
    state = composed.run_session(_et(15, 46))
    assert state.flatten_command_id == "flatten-ui-1" == first.cause_command_id
    assert [p[1] for p in composed.sim.placed] == ["MKT"]


def test_after_a_loss_breach_a_close_reduces_and_a_new_entry_is_refused(composed):
    """R11: the entry gate refuses; the reduce-only path does not use it."""
    composed.sim.daily_pnl = -5000.0
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    from ib_async import Contract
    entry = composed.loop_thread.run(composed.trader.place_expressive_order(
        Contract(conId=CONID, symbol="AAPL", secType="STK", exchange="SMART", currency="USD"),
        "BUY", 5.0, {"order_type": "MARKET"}, algo_name="mmr:og-new"))
    assert "daily loss" in str(entry.error)
    composed.stack.session_controller._time_exit.request_exit(
        command_id="time-exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")
    assert composed.sim.placed == [("time-exit-1-reduce-265598-1", "MKT", "SELL", 10.0, None, None)]


def test_invisible_child_and_a_late_fill_never_send_a_second_reduce(composed):
    """#21 through the stack."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    entity = composed.sim.entity_for("c-1-reduce")
    composed.sim.hidden.add(entity)
    composed.sim.promote()                       # newer generation, child not visible
    composed.tick()
    composed.sim.hidden.clear()
    composed.sim.set_status(entity, "Filled", filled=10.0)
    composed.sim.promote()                       # fill visible, position not yet updated
    composed.tick()
    assert [p[0] for p in composed.sim.placed] == ["c-1-reduce-265598-1"]
    composed.sim.held[CONID] = 0.0
    composed.sim.promote()
    receipt = composed.tick()
    assert composed.liquidation.receipt_for("c-1").state == "CLOSED"
    assert len(composed.sim.placed) == 1


def test_partial_close_reprotects_and_a_late_retired_leg_keeps_protection(composed):
    """#22 / #25 through the stack: hand-over, partial reduce, stop then target, DONE, release."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    assert sorted(composed.sim.cancelled) == [f"{og}:stop", f"{og}:take_profit"]
    for leg in ("stop", "take_profit"):
        composed.sim.set_status(f"{og}:{leg}", "Cancelled")
        composed.saga_event(og, leg, f"{og}:{leg}", "Cancelled")
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"
    composed.sim.promote()
    composed.tick()                                                   # partial reduce of 4
    assert composed.sim.placed[-1][:4] == ("p-1-reduce-265598-1", "MKT", "SELL", 4.0)
    composed.sim.set_status(composed.sim.entity_for("p-1-reduce"), "Filled", filled=4.0)
    composed.sim.held[CONID] = 6.0
    composed.sim.promote()
    composed.tick()                                                   # the fill is seen
    composed.sim.promote()
    composed.tick()                                                   # stop leg for 6
    assert composed.sim.placed[-1] == ("p-1-reprotect-stop-265598-1", "STP", "SELL", 6.0, 95.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()                                                   # target leg after the stop is accepted
    assert composed.sim.placed[-1] == ("p-1-reprotect-target-265598-1", "LMT", "SELL", 6.0, 120.0, "p-1-reprotect-265598-1")
    composed.sim.promote()
    composed.tick()
    assert composed.liquidation.receipt_for("p-1").state == "DONE"
    released = composed.saga.resume("entry-1")
    assert (released.state, released.protection_quantity) == ("PROTECTED", Decimal("6"))
    late = composed.saga_event(og, "stop", f"{og}:stop", "Cancelled", filled=0.0)
    assert late.state == "PROTECTED"
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"


def test_restart_mid_close_continues_from_the_journal_without_a_second_order(tmp_path, composed):
    """#19 / #20 / #24: a new stack over the same journal finishes the close."""
    composed.sim.held[CONID] = 10.0
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "c-1", _et(11, 5), scope="conid", conid=CONID)
    sim = composed.sim
    restarted = _Composed.__new__(_Composed)
    restarted.__init__(tmp_path, composed.loop_thread, composed.clock)
    restarted.sim.held, restarted.sim.orders, restarted.sim.perm = sim.held, sim.orders, sim.perm
    entity = sim.entity_for("c-1-reduce")
    restarted.sim.set_status(entity, "Filled", filled=10.0)
    restarted.sim.held[CONID] = 0.0
    restarted.sim.promote()
    restarted.tick()
    restarted.sim.promote()
    restarted.tick()
    assert restarted.liquidation.receipt_for("c-1").state == "CLOSED"
    assert restarted.sim.placed == []
    assert restarted.stack.exit_owner_registry.get("c-1").state == "RELEASED"


def test_session_cancel_keeps_protection_and_the_flatten_owns_it(composed):
    """#27: at the cancel deadline only the unfilled entry is cancelled; the breaker stays clear."""
    og = composed.protected_entry()
    composed.sim.add_order("og-entry-2:entry", "og-entry-2", "entry", "BUY", "LMT", 5.0)
    composed.sim.promote()
    composed.run_session(_et(15, 35))
    assert composed.sim.cancelled == ["og-entry-2:entry"]
    assert composed.saga.resume("entry-1").state == "PROTECTED"
    assert composed.stack.circuit_breaker.store.get().state == "CLEAR"
    state = composed.run_session(_et(15, 46))
    assert state.flatten_command_id == SessionController.flatten_command_id(ACCOUNT, FRIDAY)
    assert composed.saga.resume("entry-1").state == "CLOSE_OWNED"


def test_joined_flatten_command_resolves_from_its_root_through_the_reconciler(composed):
    """#28: the /flatten command that joined the session flatten resolves when FLAT is proven."""
    from trader.messaging.production_api import build_production_registry
    from trader.domain.feed_service import DomainFeedService
    from trader.domain.snapshot_service import DomainSnapshotService
    from trader.messaging.typed_rpc import HmacServiceAuthenticator

    build_production_registry(
        composed.trader, HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0),
        snapshot_service=DomainSnapshotService(composed.trader.domain_journal),
        feed_service=DomainFeedService(composed.trader.domain_journal), command_stack=composed.stack)
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
    composed.stack.reconciler.run_due(composed.clock[0])
    assert composed.stack.ledger.get("flatten-ui-1").state == "RESOLVED"


def test_target_leg_rejected_by_the_broker_escalates_to_a_full_close(composed):
    """#26 through the stack: a target that goes Inactive after its echo is a failed re-protect."""
    og = composed.protected_entry()
    composed.sim.promote()
    composed.liquidation.start(ACCOUNT, "p-1", _et(11, 5), scope="conid", conid=CONID, quantity=4.0)
    for leg in ("stop", "take_profit"):
        composed.sim.set_status(f"{og}:{leg}", "Cancelled")
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
    target = composed.sim.entity_for("p-1-reprotect-target")
    composed.sim.set_status(target, "Inactive")
    composed.sim.promote()
    receipt = composed.tick()
    receipt = composed.liquidation.receipt_for("p-1")
    assert receipt.escalated is True and receipt.goal == "zero"
    assert composed.sim.cancelled[-1] == composed.sim.entity_for("p-1-reprotect-stop")
    assert composed.stack.circuit_breaker.store.get().state == "TRIPPED"


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
Expected: FAIL — `ImportError: cannot import name '_BrokerGenerationRefresh'` at first; after Step 3.1 alone, the wiring tests fail (`stack.reconciler._closes is None`, the saga's protection is not attached, the hot-arm service has no `_liquidation`).

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

2. `OutcomeReconciler(` (line 734) gets `closes=liquidation_store,`.
3. The inner `LiquidationService(` gets `refresh=_BrokerGenerationRefresh(trader),`.
4. After `protective_order_saga = ProtectiveOrderSaga(...)`:

```python
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
            journal=journal, ledger=ledger, refresh=_BrokerGenerationRefresh(trader),
        ),
        liquidation_worker, account_id=trader.ib_account, now=now,
    )
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`
Expected: all PASS (11 in the new file). Watch `tests/test_production_rpc_security.py`, `tests/test_web_dashboard.py` and `tests/test_command_stack.py`: they build the stack with fakes; the new pieces only need `journal_db` and `ib_account`, which those fakes already have.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_stack.py tests/test_safe_close_integration.py
git commit -m "feat: wire exit ownership, the liquidation worker and close resolution into the command stack

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Out of scope for this plan (owned by later plans)

- The `ai_paper` `CLOSE` / `PARTIAL_CLOSE` decisions and their admission rules (plan 3). They call the same `start(scope="conid", ...)`.
- The kill line (plan 4) claims the account owner through `start(scope="account")`, as `/flatten` already does (`liquidate_account`, `trader/messaging/production_api.py:1876` → `LiquidationService.liquidate`).
- Wiring a live caller for `SessionController.on_bar` (SP2).
- The real IB paper session that proves `ocaType=2`, the generation-refresh load and the reduce-only path against IB (plan 6, acceptance harness, #35). A green fake is not that proof.
- The old-path regression "after a loss breach a new entry is refused and a safe close is allowed" is **in scope now** (Task 14 unit test, Task 13 through the stack). Plan 3 adds the `ai_paper` variant on top.

## Self-review notes (done while writing)

- Every task's code was run on a scratch copy of this branch, task by task, and the full suite passed on the final state (5373 passed, 23 skipped). The "Expected" lines give the counts seen there.
- Spec 5.1 coverage: problem list (Tasks 1, 2, 14, 15); scope and goal (Tasks 4–6); protection ownership (Task 9); breaker signals (Task 4 `_trips_breaker`, Task 6 escalation); one execution owner, goal upgrade, account owner first, supersede, exact-root polling (Tasks 3, 4, 7, 8, 11); flatten order and the unknown-child rule (Tasks 4, 7); exit-only OCA, recovery, `DONE`/`CLOSED` meaning (Tasks 6, 10); users of the safe close (Tasks 11, 12, 16); SELL must prove a reduction (Task 12). Section 5.5 step 2 (kill = account owner): Task 4 `start(scope="account")`.
- Spec section 6 "Safe close" list → tests: time exit leaves the stop live (T1/T11 `test_time_exit_leaves_no_live_stop_after_the_position_is_closed`); lost acknowledgement (T4 `test_timeout_after_the_boundary_stays_unknown_and_is_never_resent`); partial fill (T6 `test_terminal_partial_fill_reprotects_the_actual_remainder`); cancel rejected (T5 `test_cancel_rejected_by_the_broker_ends_failed_safe_without_reduce`); re-protect failure and missed deadline (T6); two protected positions (T13 `test_partial_close_reprotects_...`, T5 `test_full_close_hands_over_then_cancels_only_that_conids_orders`); unrequested stop cancel (T9); routine progress (T5); time exit + AI close (T8); time exit during partial (T8); flatten during partial (T7); kill during REPROTECTING (T7); invisible child (T4, T5, T7); full close during flatten (T8); old FAILED_SAFE root (T4); exact-root polling (T11); exit OCA cases (T6, T10, T13); production composition (T13).
- R-rule coverage: R1/R2/R3 (Task 4 `_reserve`, `_send`), R4 (`_evidence`, rulings 2–3, 7), R5 (`_blocking`, `_fresh`, ruling 4), R6 (claims and `_finish` in one transaction; Task 8 atomicity test), R7 (`_check_dispatchable_in_tx`, store refuses terminal overwrite), R8 (`_cleanup`, Task 4 crash tests), R9 (`inherit_children_in_tx`; Tasks 4, 7), R10 (Tasks 4, 11, 13), R11 (Task 14), R12 (Task 15, ruling 8), R13 (Tasks 6, 10, 14), R14 (Task 9), R15 (Task 6), R16 (Task 16), R17 (Tasks 4, 12, 17), R18 (Task 1), R19 (Task 4), R20 (Tasks 4, 6, 7, 8), R21 (Task 13).
- Names used across tasks were checked against the frozen blocks of Tasks 3 and 4: `ChildRef`, `LiquidationReceipt`, `JoinRow`, `CloseResolution`, `CancelTarget`, `HandoverInfo`, `DispatchRefused`, `LiquidationRefused`, `RunStateError`, `LiquidationRunStore.*_in_tx`, `ExitOwnerRegistry.*_in_tx`, `liquidation_child_id`, `reprotect_oca_group`, `LiquidationService.start/rescan/receipt_for/root_for/close_resolution/upgrade_to_zero/attach_protection/liquidate`, `SerializedLiquidation`, `LiquidationWorker`, `Trader.place_reduce_only_order`, `TradingRuntimeOrderDispatch.reduce_position/reduce_partial/place_exit_leg/cancel_on_loop`, `_LiquidationDispatch(dispatch, orders_view)`, `SessionTimeExitAdapter(liquidation, *, account_id, now, deadline_seconds)`, `SessionController.restore`, `AutomatedIntentCommandService(liquidation=, broker=)`, `OutcomeReconciler(closes=)`.
