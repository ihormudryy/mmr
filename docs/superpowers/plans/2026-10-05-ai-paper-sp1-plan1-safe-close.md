# AI Paper SP1 — Plan 1: Safe Close — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every exit on the paper path go through one broker-verified close service that hands protection over before it cancels a stop, owns one close per position, re-protects after a partial close with a linked stop/target, and never lets a flatten reduce while a child order is still unknown.

**Architecture:** `LiquidationService` gains a `scope` (`account` | `conid`) and a `goal` (zero or partial). A new `ExitOwnerRegistry` table holds at most one active close per `(account, conid)` and one account flatten per account; every exit producer claims there first. `ProtectiveOrderSaga` gets a `CLOSE_OWNED` state so an expected cancel is not an incident. The runtime gets two reduce-only order primitives (`reduce_partial`, `place_exit_oca`). Time exits and one-strategy SELL intents are rerouted onto the scoped close.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DomainJournal`), dataclasses, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, section 5.1 (plus the bindings in 5.5 step 2 and the test list in section 6, "Safe close"). This plan is delivery step 1 of section 7. Steps 2–6 get their own plans.

## Global Constraints

- No live authority. Nothing here may run on a live account; `account_mode` checks stay as they are.
- One broker dispatch boundary: every order goes through `TradingRuntimeOrderDispatch` → `Trader.place_expressive_order` / the new `Trader.place_exit_oca`. No second IB order path.
- `CommandReceipt` stays frozen. `ExecutionIntent` is not changed.
- `OUTCOME_UNKNOWN` is never resubmitted under a fresh id. A child order that was submitted but is not yet visible on a newer broker generation is unknown; no new reduce follows an unknown.
- The trader journal (`trader.journal_db`) is the source of truth. Every new table is a journal migration with the next free version: **35** exit owners, **36** liquidation run columns and children, **37** saga columns and saga groups. Versions 30 and 31 are taken, and **32–34 belong to `trader/data/attribution_store.py`**. `SchemaMigrator` records a version once, so reusing a taken number silently skips the new DDL. Free after this plan: 38–39, 46–49, 54+.
- Command ids and child ids never contain `:` (they become IB `orderRef` values via `encode_order_ref`).
- Routine progress of a scoped close never trips the breaker. Account-scope breaker behaviour is unchanged.
- Test-first. Each task begins with a failing test. Run tests with `pytest <path> -q --timeout=30`. The full suite: `pytest tests/ -q --timeout=30 --ignore=tests/test_ibrx_async.py`.
- Commit subjects follow the repo style (`feat:`, `fix:`, `test:`, `refactor:`), lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Old-path entry admission and risk ceilings do not change in this plan. The only old-path behaviour change is that time exits and SELL intents use the scoped close.
- Nothing calls `SessionController.on_bar` live yet (checked: no caller in `trader/`). The time-exit bug is therefore latent, but the adapter is fixed here so the first live caller (SP2) is safe.

## Review Focus

Five spec-implied inputs no test list in the spec names. Each has a test pinned to a task below.

1. **Short positions.** A scoped close of a short (`quantity < 0`) must reduce with `BUY`, bound a partial by `|quantity|`, and place the re-protect stop *above* the market price. → Task 6 (`test_partial_close_of_short_reprotects_above_price`), Task 5 (`test_full_close_of_short_reduces_with_buy`).
2. **Partial quantity edge cases.** `q` that rounds to zero is refused; `q ≥ |position|` at `start` is refused (spec 5.1: `0 < q < |position|`); `q` leaving less than one share becomes a full close; a position that shrank to `≤ q` by dispatch time is fully closed (amendment R15). → Task 6 (`test_partial_quantity_edge_cases`).
3. **Position already gone when the close reduces.** If the stop filled during the cancel race, the close must end `CLOSED` without sending a reduce. → Task 5 (`test_close_ends_closed_without_reduce_when_position_vanished`).
4. **Root id reuse with a different scope or conid.** Starting an existing root with another `conid` or `scope` must raise, never silently rebind. → Task 4 (`test_start_refuses_rebinding_root_to_another_scope`).
5. **Restart in the middle of re-protect.** On recovery the service must read the broker by ref before placing anything, so a restart never doubles the OCA pair. → Task 6 (`test_recovery_places_only_missing_reprotect_leg`).

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

---

## File Structure

| File | Responsibility |
|---|---|
| `trader/trading/order_correlation.py` (modify) | `classify_leg` learns re-protect groups so replacement exits are not called "entry". |
| `trader/trading/broker_ingest.py` (modify, two call sites) | pass `order_group_id` to `classify_leg`. |
| `trader/trading/exit_owner.py` (create) | `ExitOwnerRegistry`, `ExitOwnerRow`, `ExitClaim`, `ExitInProgress`, migration 32. One active owner per `(account, conid)`, one account owner per account, goal upgrade, supersede. |
| `trader/trading/liquidation_service.py` (modify) | scope, goal, phase, child refs and the confirmation standard, per-root polling, rescan over every open root, breaker policy by scope, protection hand-over calls, account takeover order, re-protect with recovery, escalation. Migration 33. |
| `trader/automation/protective_order_saga.py` (modify) | `CLOSE_OWNED` state, `handover`, `handover_account`, `release_after_partial`, `close_after_full`, saga groups lookup, event handling while owned. Migration 34. |
| `trader/trading/trading_runtime.py` (modify) | `Trader.place_exit_oca`, `TradingRuntimeOrderDispatch.reduce_partial`, `.place_exit_oca`. |
| `trader/trading/command_stack.py` (modify) | `_LiquidationDispatch` gains `reduce_partial`, `place_exit_oca`, `find_orders`; wiring of registry, protection port and the new time-exit adapter; `CommandStack.exit_owner_registry`. |
| `trader/automation/session_controller.py` (modify) | `SessionTimeExitAdapter` requests a scoped close; `_poll_flat` polls the exact flatten root. |
| `trader/automation/automated_intent_command.py` (modify) | SELL intents become a proven-reduction close; never `build_bracket_plan`. |
| `tests/test_liquidation_service.py` (modify) | state-machine tests with fake broker generations. |
| `tests/test_exit_owner.py` (create) | registry rules. |
| `tests/automation/test_protective_order_saga.py` (modify) | hand-over and owned-event tests. |
| `tests/automation/test_session_controller.py` (modify) | time-exit adapter and exact-root polling. |
| `tests/automation/test_automated_command_boundary.py` (modify) | SELL → close. |
| `tests/test_order_dispatch_ports.py` (modify) | `reduce_partial`, `place_exit_oca` adapter glue. |
| `tests/test_order_correlation.py` or `tests/test_command_ports.py` (modify) | `classify_leg` re-protect groups. |
| `tests/test_safe_close_integration.py` (create) | real DuckDB registry + real saga + fake dispatch: the spec's "partially close one of two protected positions" scenario and the flatten-during-reprotect scenario. |

---

### Task 1: Pin the latent time-exit bug with a failing test

The spec requires a failing test before the fix. The test asserts the *desired* behaviour (a time exit asks the liquidation service for a scoped close of that conid, and never calls `reduce` directly). It fails today. It is committed marked `xfail(strict=True)` so the suite stays green; Task 11 removes the marker when it fixes the adapter.

**Files:**
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Consumes: `SessionTimeExitAdapter` (`trader/automation/session_controller.py:286`), today constructed as `SessionTimeExitAdapter(dispatch)`.
- Produces: the contract Task 11 implements: `SessionTimeExitAdapter(liquidation, *, account_id, now, deadline_seconds=300.0)`; `request_exit(...)` calls `liquidation.start(account_id, command_id, deadline, scope="conid", conid=conid)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/automation/test_session_controller.py`:

```python
class _RecordingLiquidation:
    def __init__(self):
        self.starts: list[dict] = []
        self.reduces: list[tuple] = []

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        self.starts.append({"account_id": account_id, "root": cause_command_id,
                            "deadline": deadline, **kwargs})
        return SimpleNamespace(cause_command_id=cause_command_id, state="REQUESTED")

    def reduce(self, position, side, quantity, command_id):
        self.reduces.append((position.conid, side, quantity, command_id))


@pytest.mark.xfail(strict=True, reason="time exit still calls reduce directly; fixed in plan 1 task 10")
def test_time_exit_requests_scoped_close_and_never_reduces_directly():
    """Spec 5.1: a time exit must cancel the protective stop first. Only the
    scoped close does that, so the adapter must ask for a conid-scoped close
    and must never call ``reduce`` itself (which leaves the stop live and can
    open a short after the position is gone)."""
    from trader.automation.session_controller import SessionTimeExitAdapter

    liquidation = _RecordingLiquidation()
    adapter = SessionTimeExitAdapter(
        liquidation, account_id=ACCOUNT, now=lambda: _utc(15, 0), deadline_seconds=120.0,
    )
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")

    assert liquidation.reduces == []
    assert len(liquidation.starts) == 1
    start = liquidation.starts[0]
    assert start["account_id"] == ACCOUNT
    assert start["root"] == "exit-1"
    assert start["scope"] == "conid"
    assert start["conid"] == CONID
    assert start["deadline"] == _utc(15, 2)
```

- [ ] **Step 2: Run it to verify it is XFAIL (the underlying assertion fails)**

Run: `pytest tests/automation/test_session_controller.py::test_time_exit_requests_scoped_close_and_never_reduces_directly -q --timeout=30`
Expected: `1 xfailed`. (Today `SessionTimeExitAdapter.__init__` takes one positional argument, so the constructor raises `TypeError`; that counts as the expected failure.)

- [ ] **Step 3: Commit**

```bash
git add tests/automation/test_session_controller.py
git commit -m "test: pin time exit leaving the protective stop live

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `classify_leg` understands re-protect groups

Replacement exits have no parent order, so `classify_leg` (`trader/trading/order_correlation.py:32`) calls them `"entry"`. The ingest would then feed the saga an "entry" event for a stop. Re-protect orders carry `orderRef` `mmr:<root>-reprotect-stop` / `mmr:<root>-reprotect-target`, so the decoded group names the leg.

**Files:**
- Modify: `trader/trading/order_correlation.py:32-40`
- Modify: `trader/trading/broker_ingest.py:751` and `:1281`
- Test: `tests/test_command_ports.py` (has the `classify_leg` tests today; if not, create `tests/test_order_correlation.py`)

**Interfaces:**
- Produces: `classify_leg(order_type: str, parent_id: int, client_order_id: int, order_group_id: Optional[str] = None) -> str`. A group ending in `-reprotect-stop` returns `"stop"`; `-reprotect-target` returns `"take_profit"`; otherwise behaviour is unchanged.
- Produces: module constants `REPROTECT_STOP_SUFFIX = "-reprotect-stop"`, `REPROTECT_TARGET_SUFFIX = "-reprotect-target"`, and `def reprotect_ref(root_id: str, leg: str) -> str` returning `f"{root_id}-reprotect-{leg}"` for `leg in ("stop", "target")` (raises `ValueError` otherwise). Tasks 5, 8 and 9 use these.

- [ ] **Step 1: Write the failing tests**

```python
# in tests/test_command_ports.py (or tests/test_order_correlation.py)
import pytest
from trader.trading.order_correlation import classify_leg, reprotect_ref


def test_reprotect_groups_classify_by_group_not_parent():
    assert classify_leg("STP", 0, 7, order_group_id="root-1-reprotect-stop") == "stop"
    assert classify_leg("LMT", 0, 8, order_group_id="root-1-reprotect-target") == "take_profit"


def test_non_reprotect_groups_keep_parent_rule():
    assert classify_leg("STP", 0, 7, order_group_id="og-cmd1") == "entry"
    assert classify_leg("STP", 5, 7, order_group_id="og-cmd1") == "stop"
    assert classify_leg("LMT", 5, 7) == "take_profit"


def test_reprotect_ref_is_deterministic_and_colon_free():
    assert reprotect_ref("root-1", "stop") == "root-1-reprotect-stop"
    assert reprotect_ref("root-1", "target") == "root-1-reprotect-target"
    assert ":" not in reprotect_ref("root-1", "stop")
    with pytest.raises(ValueError):
        reprotect_ref("root-1", "entry")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_command_ports.py -q --timeout=30 -k "reprotect"`
Expected: FAIL — `ImportError: cannot import name 'reprotect_ref'` / `TypeError: classify_leg() got an unexpected keyword argument`.

- [ ] **Step 3: Implement**

In `trader/trading/order_correlation.py` replace `classify_leg` and add the helpers:

```python
REPROTECT_STOP_SUFFIX = "-reprotect-stop"
REPROTECT_TARGET_SUFFIX = "-reprotect-target"


def reprotect_ref(root_id: str, leg: str) -> str:
    """Deterministic order ref (and order group) of a re-protect exit leg."""
    if leg == "stop":
        return f"{root_id}{REPROTECT_STOP_SUFFIX}"
    if leg == "target":
        return f"{root_id}{REPROTECT_TARGET_SUFFIX}"
    raise ValueError(f"unknown re-protect leg {leg!r}")


def classify_leg(
    order_type: str, parent_id: int, client_order_id: int,
    order_group_id: Optional[str] = None,
) -> str:
    # Re-protect exits have no parent; the group name carries the leg.
    if order_group_id:
        if order_group_id.endswith(REPROTECT_STOP_SUFFIX):
            return "stop"
        if order_group_id.endswith(REPROTECT_TARGET_SUFFIX):
            return "take_profit"
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
# ~line 751 (inside the merged BrokerOrderRow leg= expression)
classify_leg(obs.order_type, obs.parent_id, obs.client_order_id, order_group_id=group_id)
# ~line 1281 (_notify_protective_saga)
leg = order.leg or classify_leg(
    obs.order_type, obs.parent_id, obs.client_order_id, order_group_id=order.order_group_id,
)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_command_ports.py tests/test_broker_ingest*.py -q --timeout=30`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/order_correlation.py trader/trading/broker_ingest.py tests/test_command_ports.py
git commit -m "feat: classify re-protect exit legs by order group

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `ExitOwnerRegistry` — one owner per position, one flatten per account

**Files:**
- Create: `trader/trading/exit_owner.py`
- Test: `tests/test_exit_owner.py`

**Interfaces:**
- Produces:

```python
EXIT_OWNER_MIGRATION_VERSION = 32
def apply_exit_owner_migration(migrator: SchemaMigrator) -> bool

class ExitInProgress(RuntimeError):      # .code == "EXIT_IN_PROGRESS"
@dataclass(frozen=True) class ExitOwnerRow:
    root_id: str; account_id: str; conid: Optional[int]; kind: str   # "scoped_close" | "account_flatten"
    goal: str; goal_quantity: Optional[float]; state: str           # goal "zero"|"partial"; state ACTIVE|SUPERSEDED|RELEASED
@dataclass(frozen=True) class ExitClaim:
    root_id: str; outcome: str; superseded: tuple[str, ...] = ()   # outcome CLAIMED|JOINED|UPGRADED|JOINED_FLATTEN

class ExitOwnerRegistry:
    def __init__(self, db)
    def claim_scoped(self, *, account_id: str, conid: int, root_id: str,
                     goal_quantity: Optional[float], now: dt.datetime) -> ExitClaim
    def claim_account(self, *, account_id: str, root_id: str, now: dt.datetime) -> ExitClaim
    def get(self, root_id: str) -> Optional[ExitOwnerRow]
    def owner_for(self, account_id: str, conid: int) -> Optional[ExitOwnerRow]
    def account_owner(self, account_id: str) -> Optional[ExitOwnerRow]
    def release(self, root_id: str, now: dt.datetime) -> None
```

Rules (spec 5.1 "One execution owner per position"):
1. `claim_scoped` checks the **account owner first**. If one is `ACTIVE`: a full request (`goal_quantity is None`) returns `ExitClaim(account_root, "JOINED_FLATTEN")`; a partial request raises `ExitInProgress`.
2. Else, if an `ACTIVE` scoped owner exists for `(account, conid)`: a partial request raises `ExitInProgress`; a full request against a `partial` owner upgrades it to `zero` in the same transaction and returns `UPGRADED`; a full request against a `zero` owner returns `JOINED`. Re-claiming with the owner's own `root_id` returns `JOINED`.
3. Else insert `ACTIVE` and return `CLAIMED`.
4. `claim_account`: if an `ACTIVE` account owner exists with another root → `JOINED_FLATTEN` with that root; same root → `CLAIMED` with no superseded; else insert and mark every `ACTIVE` scoped owner of that account `SUPERSEDED` **in the same transaction**, returning their root ids.
5. `SUPERSEDED` and `RELEASED` rows are never considered owners and are never upgraded.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_exit_owner.py
import datetime as dt

import pytest

from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.exit_owner import (
    ExitInProgress, ExitOwnerRegistry, apply_exit_owner_migration,
)

NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=dt.timezone.utc)
ACCOUNT = "DU123"


@pytest.fixture
def registry(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "owners.duckdb"))
    migrator = SchemaMigrator(db)
    apply_exit_owner_migration(migrator)
    return ExitOwnerRegistry(db)


def test_migration_32_creates_exit_owners_table(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "m.duckdb"))
    migrator = SchemaMigrator(db)
    assert apply_exit_owner_migration(migrator) is True
    assert apply_exit_owner_migration(migrator) is False
    row = db.execute("SELECT version FROM schema_migrations WHERE version = 32", fetch="one")
    assert row is not None


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


def test_other_conid_is_independent(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    claim = registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-9", goal_quantity=None, now=NOW)
    assert claim.outcome == "CLAIMED"


def test_account_claim_supersedes_scoped_owners_in_one_step(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="p-1", goal_quantity=4.0, now=NOW)
    registry.claim_scoped(account_id=ACCOUNT, conid=2, root_id="c-2", goal_quantity=None, now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    assert claim.outcome == "CLAIMED"
    assert set(claim.superseded) == {"p-1", "c-2"}
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
    superseded = registry.get("p-1")
    assert (superseded.state, superseded.goal) == ("SUPERSEDED", "partial")


def test_second_account_claim_joins_the_first(registry):
    registry.claim_account(account_id=ACCOUNT, root_id="flat-1", now=NOW)
    claim = registry.claim_account(account_id=ACCOUNT, root_id="flat-2", now=NOW)
    assert (claim.root_id, claim.outcome) == ("flat-1", "JOINED_FLATTEN")


def test_release_frees_the_slot(registry):
    registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-1", goal_quantity=None, now=NOW)
    registry.release("c-1", NOW)
    assert registry.owner_for(ACCOUNT, 1) is None
    assert registry.get("c-1").state == "RELEASED"
    assert registry.claim_scoped(account_id=ACCOUNT, conid=1, root_id="c-2", goal_quantity=None, now=NOW).outcome == "CLAIMED"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_exit_owner.py -q --timeout=30`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.trading.exit_owner'`.

- [ ] **Step 3: Implement**

```python
# trader/trading/exit_owner.py
"""Durable exit ownership: one close per position, one flatten per account.

Every exit producer (time exit, SELL intent, AI close, protective failure,
session flatten, /flatten, kill) claims here before it does anything. The
registry decides whether the caller starts work, joins an existing root,
upgrades a partial close to a full one, or is refused.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from trader.data.schema_migrations import SchemaMigrator

EXIT_OWNER_MIGRATION_VERSION = 32

KIND_SCOPED = "scoped_close"
KIND_ACCOUNT = "account_flatten"
GOAL_ZERO = "zero"
GOAL_PARTIAL = "partial"
STATE_ACTIVE = "ACTIVE"
STATE_SUPERSEDED = "SUPERSEDED"
STATE_RELEASED = "RELEASED"


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
        """CREATE INDEX IF NOT EXISTS idx_exit_owners_account
            ON exit_owners(account_id, state)""",
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


class ExitOwnerRegistry:
    def __init__(self, db: Any):
        self._db = db

    # -- reads ---------------------------------------------------------------

    def get(self, root_id: str) -> Optional[ExitOwnerRow]:
        return _row(self._db.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE root_id = ?", [root_id], fetch="one"))

    def owner_for(self, account_id: str, conid: int) -> Optional[ExitOwnerRow]:
        return _row(self._db.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND conid = ? "
            f"AND kind = ? AND state = ?",
            [account_id, int(conid), KIND_SCOPED, STATE_ACTIVE], fetch="one"))

    def account_owner(self, account_id: str) -> Optional[ExitOwnerRow]:
        return _row(self._db.execute(
            f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
            [account_id, KIND_ACCOUNT, STATE_ACTIVE], fetch="one"))

    # -- claims --------------------------------------------------------------

    def claim_scoped(self, *, account_id: str, conid: int, root_id: str,
                     goal_quantity: Optional[float], now: dt.datetime) -> ExitClaim:
        if ":" in root_id:
            raise ValueError("root id may not contain ':'")
        wants_partial = goal_quantity is not None
        result: dict[str, ExitClaim] = {}

        def write(conn):
            flatten = _row(conn.execute(
                f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
                [account_id, KIND_ACCOUNT, STATE_ACTIVE]).fetchone())
            if flatten is not None:
                if wants_partial:
                    raise ExitInProgress(flatten.root_id)
                result["claim"] = ExitClaim(flatten.root_id, "JOINED_FLATTEN")
                return
            owner = _row(conn.execute(
                f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND conid = ? "
                f"AND kind = ? AND state = ?",
                [account_id, int(conid), KIND_SCOPED, STATE_ACTIVE]).fetchone())
            if owner is not None:
                if owner.root_id == root_id:
                    result["claim"] = ExitClaim(owner.root_id, "JOINED")
                    return
                if wants_partial:
                    raise ExitInProgress(owner.root_id)
                if owner.goal == GOAL_PARTIAL:
                    conn.execute(
                        "UPDATE exit_owners SET goal = ?, goal_quantity = NULL, updated_at = ? "
                        "WHERE root_id = ?", [GOAL_ZERO, now, owner.root_id])
                    result["claim"] = ExitClaim(owner.root_id, "UPGRADED")
                    return
                result["claim"] = ExitClaim(owner.root_id, "JOINED")
                return
            conn.execute(
                "INSERT INTO exit_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [root_id, account_id, int(conid), KIND_SCOPED,
                 GOAL_PARTIAL if wants_partial else GOAL_ZERO, goal_quantity, STATE_ACTIVE, now])
            result["claim"] = ExitClaim(root_id, "CLAIMED")

        self._db.transaction(write)
        return result["claim"]

    def claim_account(self, *, account_id: str, root_id: str, now: dt.datetime) -> ExitClaim:
        if ":" in root_id:
            raise ValueError("root id may not contain ':'")
        result: dict[str, ExitClaim] = {}

        def write(conn):
            flatten = _row(conn.execute(
                f"SELECT {_COLUMNS} FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
                [account_id, KIND_ACCOUNT, STATE_ACTIVE]).fetchone())
            if flatten is not None:
                outcome = "CLAIMED" if flatten.root_id == root_id else "JOINED_FLATTEN"
                result["claim"] = ExitClaim(flatten.root_id, outcome)
                return
            scoped = [r[0] for r in conn.execute(
                "SELECT root_id FROM exit_owners WHERE account_id = ? AND kind = ? AND state = ?",
                [account_id, KIND_SCOPED, STATE_ACTIVE]).fetchall()]
            conn.execute(
                "UPDATE exit_owners SET state = ?, updated_at = ? "
                "WHERE account_id = ? AND kind = ? AND state = ?",
                [STATE_SUPERSEDED, now, account_id, KIND_SCOPED, STATE_ACTIVE])
            conn.execute(
                "INSERT INTO exit_owners VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
                [root_id, account_id, KIND_ACCOUNT, GOAL_ZERO, STATE_ACTIVE, now])
            result["claim"] = ExitClaim(root_id, "CLAIMED", tuple(sorted(scoped)))

        self._db.transaction(write)
        return result["claim"]

    def release(self, root_id: str, now: dt.datetime) -> None:
        def write(conn):
            conn.execute("UPDATE exit_owners SET state = ?, updated_at = ? WHERE root_id = ? AND state = ?",
                         [STATE_RELEASED, now, root_id, STATE_ACTIVE])
        self._db.transaction(write)
```

Note: `self._db.transaction(write)` is the same API `LiquidationRunStore.save` and `SessionStateStore.save` use (`DuckDBConnection.transaction(fn)` passes an open connection and commits). `ExitInProgress` raised inside `write` propagates out of `transaction` after rollback; the test for a partial against an existing owner relies on that.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_exit_owner.py -q --timeout=30`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/exit_owner.py tests/test_exit_owner.py
git commit -m "feat: durable exit owner registry for closes and flattens

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Liquidation receipt gets scope, goal, phase and child refs; rescan advances every open root

This task changes the data model and the service skeleton without adding the new close flow. All existing tests in `tests/test_liquidation_service.py` must still pass (parity for the account scope).

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Produces (module level):

```python
LIQUIDATION_SCOPE_MIGRATION_VERSION = 33
def apply_liquidation_migration(migrator) -> None      # now applies 25 and 33
_RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "SUPERSEDED", "FAILED_SAFE"})
_ORDER_TERMINAL = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"})

@dataclass(frozen=True)
class ChildRef:
    command_id: str          # also the order ref/group once encoded
    phase: str               # cancel | reduce | reprotect
    submitted_generation: int

@dataclass(frozen=True)
class LiquidationReceipt:
    account_id: str; cause_command_id: str; state: str; deadline: dt.datetime
    generation_id: Optional[int] = None; detail: str = ""
    scope: str = "account"                 # account | conid
    conid: Optional[int] = None
    goal_quantity: Optional[float] = None  # None = close to zero
    phase: str = ""                        # "" | cancel | reduce | reprotect
    expected_remaining: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    children: tuple[ChildRef, ...] = ()
    escalated: bool = False
```

- Produces (service):

```python
class LiquidationDispatchPort(Protocol):
    def cancel(self, order, command_id: str) -> None
    def reduce(self, position, side: str, quantity: float, command_id: str) -> None
    def reduce_partial(self, position, side: str, quantity: float, command_id: str) -> None   # Task 6/10
    def place_exit_oca(self, position, *, quantity: float, stop_price: float,
                       target_price: Optional[float], command_id: str, legs: tuple[str, ...]) -> None  # Task 6/10
    def find_orders(self, account_id: str, command_id: str) -> list   # rows with .status; Task 10

LiquidationService.__init__(..., registry=None, protection=None)   # registry: ExitOwnerRegistry (Task 8); protection: ProtectionOwnershipPort (Task 5/9)
LiquidationService.start(account_id, cause_command_id, deadline, *, scope="account", conid=None, quantity=None,
                         stop_price=None, target_price=None) -> LiquidationReceipt
LiquidationService.receipt_for(root_id) -> Optional[LiquidationReceipt]
LiquidationService.rescan() -> Optional[LiquidationReceipt]   # advances EVERY non-terminal root; returns the first advanced one or None
LiquidationService.attach_protection(port) -> None            # wiring helper (saga is built after the service)
```

- Breaker rule (`_set`): account scope trips on every state except `FLAT` (unchanged). Conid scope trips only on `FAILED_SAFE`; escalation (Task 6) trips explicitly.
- Store: `LiquidationRunStore.save/load_unresolved` persist every new field; `children` as JSON; `load_unresolved` returns rows whose state is not in `_RESCAN_TERMINAL`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_liquidation_service.py`:

```python
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.trading.liquidation_service import (
    ChildRef, LiquidationReceipt, LiquidationRunStore, apply_liquidation_migration,
)


def test_migration_33_adds_scope_columns_and_store_round_trips(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "liq.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    store = LiquidationRunStore(db)
    receipt = LiquidationReceipt(
        ACCOUNT, "root-7", "REPROTECTING", NOW + dt.timedelta(minutes=5), generation_id=3,
        detail="x", scope="conid", conid=1, goal_quantity=4.0, phase="reprotect",
        expected_remaining=6.0, stop_price=95.0, target_price=120.0,
        children=(ChildRef("root-7-reprotect-stop", "reprotect", 3),), escalated=True,
    )
    store.save(receipt, NOW)
    loaded = store.load_unresolved()
    assert loaded == [receipt]


def test_store_treats_closed_done_superseded_as_resolved(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "liq2.duckdb"))
    apply_liquidation_migration(SchemaMigrator(db))
    store = LiquidationRunStore(db)
    for state in ("FLAT", "CLOSED", "DONE", "SUPERSEDED"):
        store.save(LiquidationReceipt(ACCOUNT, f"r-{state}", state, NOW), NOW)
    store.save(LiquidationReceipt(ACCOUNT, "r-open", "VERIFYING", NOW), NOW)
    store.save(LiquidationReceipt(ACCOUNT, "r-failed", "FAILED_SAFE", NOW), NOW)
    assert sorted(r.cause_command_id for r in store.load_unresolved()) == ["r-failed", "r-open"]


def test_receipt_for_returns_exact_root_and_rescan_advances_every_open_root():
    service, dispatch, _ = _service([
        _snapshot(1, [_position()]),
        _snapshot(2, [_position()]),
    ])
    deadline = NOW + dt.timedelta(minutes=1)
    service.start(ACCOUNT, "root-a", deadline)
    assert service.receipt_for("root-a").cause_command_id == "root-a"
    assert service.receipt_for("missing") is None


def test_failed_safe_root_does_not_block_rescan_of_a_newer_root():
    broker = _Broker([_snapshot(1, [_position()]), _snapshot(2, [_position()]), _snapshot(3, [])])
    dispatch, breaker = _Dispatch(), _Breaker()
    clock = {"now": NOW}
    service = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: clock["now"])
    service.start(ACCOUNT, "old", NOW)                       # deadline already passed → FAILED_SAFE
    assert service.receipt_for("old").state == "FAILED_SAFE"
    service.start(ACCOUNT, "new", NOW + dt.timedelta(minutes=5))
    advanced = service.rescan()
    assert service.receipt_for("new").state == "FLAT"
    assert service.receipt_for("old").state == "FAILED_SAFE"
    assert advanced is not None


def test_start_refuses_rebinding_root_to_another_scope():
    service, _dispatch, _ = _service([_snapshot(1, [_position()])])
    service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1))
    with pytest.raises(ValueError):
        service.start(ACCOUNT, "root-1", NOW + dt.timedelta(minutes=1), scope="conid", conid=1)
    service2, _d, _ = _service([_snapshot(1, [_position()])])
    service2.start(ACCOUNT, "root-2", NOW + dt.timedelta(minutes=1), scope="conid", conid=1)
    with pytest.raises(ValueError):
        service2.start(ACCOUNT, "root-2", NOW + dt.timedelta(minutes=1), scope="conid", conid=2)


def test_conid_scope_routine_progress_does_not_trip_breaker():
    """Only the data model is in place in this task; the conid flow (Task 5)
    must inherit the no-breaker rule, so pin it on _set now."""
    service, _dispatch, breaker = _service([_snapshot(1, [_position()])])
    receipt = LiquidationReceipt(ACCOUNT, "c-1", "REQUESTED", NOW + dt.timedelta(minutes=1), scope="conid", conid=1)
    updated = service._set(receipt, "CANCELLING", generation_id=1, detail="routine")
    assert updated.state == "CANCELLING"
    assert breaker.calls == []
    service._set(updated, "FAILED_SAFE", detail="deadline")
    assert breaker.calls == [("c-1", "deadline")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: the six new tests FAIL (`ImportError: cannot import name 'ChildRef'`, `TypeError` on `start(... scope=)`); the eight old tests PASS.

- [ ] **Step 3: Implement**

Replace the top of `trader/trading/liquidation_service.py` (constants through `LiquidationRunStore`) with:

```python
import datetime as dt
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol

from trader.data.schema_migrations import SchemaMigrator
from trader.domain.commands import CommandReceipt
from trader.domain.events import DomainMutation
from trader.domain.identity import command_entity_id


_RESCAN_TERMINAL = frozenset({"FLAT", "CLOSED", "DONE", "SUPERSEDED", "FAILED_SAFE"})
_ORDER_TERMINAL = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"})
_ROOT_RESOLVED = frozenset({"FLAT", "CLOSED", "DONE"})
LIQUIDATION_MIGRATION_VERSION = 25
LIQUIDATION_SCOPE_MIGRATION_VERSION = 33


def apply_liquidation_migration(migrator: SchemaMigrator) -> None:
    migrator.apply(LIQUIDATION_MIGRATION_VERSION, "p1_liquidation_runs", (
        """CREATE TABLE IF NOT EXISTS liquidation_runs (
            cause_command_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL, deadline TIMESTAMPTZ NOT NULL,
            generation_id BIGINT, detail VARCHAR NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        )""",
    ))
    migrator.apply(LIQUIDATION_SCOPE_MIGRATION_VERSION, "sp1_liquidation_scope", (
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS scope VARCHAR DEFAULT 'account'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS conid INTEGER",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS goal_quantity DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS phase VARCHAR DEFAULT ''",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS expected_remaining DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS stop_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS target_price DOUBLE",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS children_json VARCHAR DEFAULT '[]'",
        "ALTER TABLE liquidation_runs ADD COLUMN IF NOT EXISTS escalated BOOLEAN DEFAULT FALSE",
    ))


class BrokerSnapshotPort(Protocol):
    def capture(self, account_id: str) -> Any: ...


class LiquidationDispatchPort(Protocol):
    """Narrow, reduce-only order boundary.

    Implementations must reject an order that can increase or flip the broker
    position.  ``command_id`` is a deterministic child id for audit/correlation.
    """
    def cancel(self, order: Any, command_id: str) -> None: ...
    def reduce(self, position: Any, side: str, quantity: float, command_id: str) -> None: ...
    def reduce_partial(self, position: Any, side: str, quantity: float, command_id: str) -> None: ...
    def place_exit_oca(self, position: Any, *, quantity: float, stop_price: float,
                       target_price: Optional[float], command_id: str, legs: tuple[str, ...]) -> None: ...
    def find_orders(self, account_id: str, command_id: str) -> list: ...


class LiquidationBreakerPort(Protocol):
    def trip_liquidation(self, cause_command_id: str, detail: str) -> None: ...


@dataclass(frozen=True)
class ChildRef:
    command_id: str
    phase: str
    submitted_generation: int


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
    goal_quantity: Optional[float] = None
    phase: str = ""
    expected_remaining: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    children: tuple[ChildRef, ...] = ()
    escalated: bool = False


_RUN_COLUMNS = ("account_id, cause_command_id, state, deadline, generation_id, detail, scope, conid, "
                "goal_quantity, phase, expected_remaining, stop_price, target_price, children_json, escalated")


def _children_to_json(children: tuple[ChildRef, ...]) -> str:
    return json.dumps([[c.command_id, c.phase, c.submitted_generation] for c in children])


def _children_from_json(raw: Optional[str]) -> tuple[ChildRef, ...]:
    return tuple(ChildRef(str(c[0]), str(c[1]), int(c[2])) for c in json.loads(raw or "[]"))


class LiquidationRunStore:
    """Small durable resume cursor; broker state remains the authority."""
    def __init__(self, db): self._db = db

    def load_unresolved(self):
        markers = ", ".join("?" for _ in _RESCAN_TERMINAL)
        rows = self._db.execute(
            f"SELECT {_RUN_COLUMNS} FROM liquidation_runs WHERE state NOT IN ({markers}) OR state = 'FAILED_SAFE'",
            list(_RESCAN_TERMINAL), fetch="all")
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row):
        (account_id, root, state, deadline, generation_id, detail, scope, conid, goal_quantity,
         phase, expected_remaining, stop_price, target_price, children_json, escalated) = row
        return LiquidationReceipt(
            account_id, root, state, deadline, generation_id, detail,
            scope=scope or "account", conid=None if conid is None else int(conid),
            goal_quantity=goal_quantity, phase=phase or "", expected_remaining=expected_remaining,
            stop_price=stop_price, target_price=target_price,
            children=_children_from_json(children_json), escalated=bool(escalated),
        )

    def save(self, receipt, now):
        def write(conn):
            conn.execute("DELETE FROM liquidation_runs WHERE cause_command_id=?", [receipt.cause_command_id])
            conn.execute(
                f"INSERT INTO liquidation_runs ({_RUN_COLUMNS}, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [receipt.account_id, receipt.cause_command_id, receipt.state, receipt.deadline,
                 receipt.generation_id, receipt.detail, receipt.scope, receipt.conid,
                 receipt.goal_quantity, receipt.phase, receipt.expected_remaining, receipt.stop_price,
                 receipt.target_price, _children_to_json(receipt.children), receipt.escalated, now])
        self._db.transaction(write)
```

Note on `load_unresolved`: `FAILED_SAFE` rows are loaded on restart so the breaker latch and the audit trail survive, but `rescan` skips them (they are in `_RESCAN_TERMINAL`). This is what the spec means by "`FAILED_SAFE` stays latched for the breaker but no longer blocks other roots".

Then change the service skeleton. Replace `__init__`, `start`, `rescan`, `_set`, `_resolve_root_command` and the head of `_advance`:

```python
class LiquidationService:
    """Conservative single-process saga; persistent orchestration is added by its owner.

    A root is idempotent while it waits for broker truth: it never emits another
    reduce order until a later broker generation is observed.  This eliminates
    the dangerous retry-on-timeout/partial-fill pattern.  A caller may invoke
    :meth:`rescan` whenever a promoted broker generation arrives.
    """

    def __init__(
        self,
        broker: BrokerSnapshotPort,
        dispatch: LiquidationDispatchPort,
        *,
        breaker: Optional[LiquidationBreakerPort] = None,
        now: Callable[[], dt.datetime], store: Optional[LiquidationRunStore] = None,
        journal=None, ledger=None, deadline_seconds: float = 300.0,
        registry=None, protection=None,
    ):
        self._broker = broker
        self._dispatch = dispatch
        self._breaker = breaker
        self._now = now
        self._store = store
        self._runs: dict[str, LiquidationReceipt] = {r.cause_command_id: r for r in (store.load_unresolved() if store else ())}
        self._journal, self._ledger, self._deadline_seconds = journal, ledger, deadline_seconds
        self._registry = registry
        self._protection = protection

    def attach_protection(self, protection) -> None:
        self._protection = protection

    # liquidate(cmd) is unchanged.

    @staticmethod
    def child_command_id(cause_command_id: str, phase: str, key: str) -> str:
        # command ids may not contain ':' because they become order references.
        return f"{cause_command_id}-liquidation-{phase}-{key}"

    def receipt_for(self, root_id: str) -> Optional[LiquidationReceipt]:
        return self._runs.get(root_id)

    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, *,
              scope: str = "account", conid: Optional[int] = None, quantity: Optional[float] = None,
              stop_price: Optional[float] = None, target_price: Optional[float] = None) -> LiquidationReceipt:
        if not account_id or not cause_command_id:
            raise ValueError("account_id and cause_command_id are required")
        if ":" in cause_command_id:
            raise ValueError("cause command id may not contain ':'")
        if scope not in ("account", "conid"):
            raise ValueError(f"unknown liquidation scope {scope!r}")
        if scope == "conid" and conid is None:
            raise ValueError("conid scope requires a conid")
        if scope == "account" and (conid is not None or quantity is not None):
            raise ValueError("account scope takes no conid or quantity")
        current = self._runs.get(cause_command_id)
        if current is not None:
            if current.account_id != account_id:
                raise ValueError("cause command id is already bound to another account")
            if current.scope != scope or current.conid != conid:
                raise ValueError("cause command id is already bound to another scope or conid")
            return self._advance(current)
        receipt = LiquidationReceipt(
            account_id, cause_command_id, "REQUESTED", deadline, scope=scope, conid=conid,
            goal_quantity=quantity, stop_price=stop_price, target_price=target_price,
        )
        self._runs[cause_command_id] = receipt
        return self._advance(receipt)

    def rescan(self) -> Optional[LiquidationReceipt]:
        """Advance every unresolved root from newly promoted broker evidence."""
        first: Optional[LiquidationReceipt] = None
        for receipt in tuple(self._runs.values()):
            if receipt.state in _RESCAN_TERMINAL:
                continue
            advanced = self._advance(receipt)
            if first is None:
                first = advanced
        return first

    def _trips_breaker(self, receipt: LiquidationReceipt, state: str) -> bool:
        if receipt.scope == "account":
            return state != "FLAT"
        return state == "FAILED_SAFE"

    def _set(self, receipt: LiquidationReceipt, state: str, *, generation_id=None, detail="", **fields) -> LiquidationReceipt:
        updated = replace(
            receipt, state=state, detail=detail,
            generation_id=receipt.generation_id if generation_id is None else generation_id,
            **fields,
        )
        self._runs[receipt.cause_command_id] = updated
        if self._store is not None:
            self._store.save(updated, self._now())
        if state in _ROOT_RESOLVED:
            self._resolve_root_command(updated)
        if self._breaker is not None and self._trips_breaker(receipt, state):
            self._breaker.trip_liquidation(receipt.cause_command_id, detail or state)
        return updated

    def _resolve_root_command(self, receipt: LiquidationReceipt) -> None:
        """Resolve only a coordinator-owned root after broker proof."""
        if self._journal is None or self._ledger is None:
            return
        row = self._ledger.get(receipt.cause_command_id)
        if row is None or row.state != "OUTCOME_UNKNOWN":
            return
        now = self._now()
        outcome = {"liquidation_state": receipt.state, "generation_id": receipt.generation_id,
                   "detail": receipt.detail}
        def write(conn, _revision):
            self._ledger.transition_in_tx(conn, receipt.cause_command_id, "OUTCOME_UNKNOWN", "RESOLVED",
                                          outcome=outcome, error_code=None, now=now)
        self._journal.mutate(self._journal.connect(), DomainMutation(
            event_type="command.updated", entity_type="command", entity_id=command_entity_id(receipt.cause_command_id),
            operation="upsert", account_id=receipt.account_id, source="trader_service", source_timestamp=now,
            correlation_id=receipt.cause_command_id, payload={"state": "RESOLVED", **outcome}),
            write, event_id=f"command:{receipt.cause_command_id}:liquidation-{receipt.state.lower()}")

    def _advance(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        if receipt.state in _RESCAN_TERMINAL:
            return receipt
        if self._now() >= receipt.deadline:
            return self._set(receipt, "FAILED_SAFE", detail="liquidation deadline elapsed without broker-confirmed flat state")
        try:
            snapshot = self._broker.capture(receipt.account_id)
        except Exception as exc:
            return self._set(receipt, "OUTCOME_UNKNOWN", detail=f"broker snapshot unavailable: {exc}")
        if getattr(snapshot, "account_id", None) != receipt.account_id:
            return self._set(receipt, "OUTCOME_UNKNOWN", detail="broker snapshot account mismatch")
        generation = int(snapshot.generation_id)
        if receipt.scope == "conid":
            return self._advance_conid(receipt, snapshot, generation)
        return self._advance_account(receipt, snapshot, generation)

    def _advance_conid(self, receipt, snapshot, generation):
        raise NotImplementedError("conid scope arrives in plan 1 task 5")

    def _advance_account(self, receipt: LiquidationReceipt, snapshot, generation: int) -> LiquidationReceipt:
        # Task 4: the old flow, plus child bookkeeping. Task 7 adds ownership and the wait-for-every-child rule.
        if (not snapshot.positions and not snapshot.working_orders and receipt.generation_id is not None
                and generation > receipt.generation_id):
            return self._set(receipt, "FLAT", generation_id=generation, detail="fresh broker snapshot confirms no positions or working orders")
        if receipt.state in {"REDUCING", "VERIFYING", "OUTCOME_UNKNOWN"} and receipt.generation_id is not None and generation <= receipt.generation_id:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting newer broker generation")
        working = tuple(snapshot.working_orders)
        if working:
            current = self._set(receipt, "CANCELLING_ENTRIES", generation_id=generation, detail="cancelling broker-reported working orders")
            try:
                for order in working:
                    child = self.child_command_id(receipt.cause_command_id, "cancel", str(order.order_entity_id))
                    self._dispatch.cancel(order, child)
                    current = self._with_child(current, child, "cancel", generation)
            except Exception as exc:
                return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"cancel outcome unknown: {exc}")
            return self._set(current, "VERIFYING", generation_id=generation, detail="awaiting broker confirmation that working orders are gone")
        positions = tuple(position for position in snapshot.positions if float(position.quantity) != 0.0)
        if not positions:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting fresh broker flat confirmation")
        current = self._set(receipt, "REDUCING", generation_id=generation, detail="submitting reduce-only liquidation orders")
        try:
            for position in positions:
                quantity = abs(float(position.quantity))
                side = "SELL" if position.quantity > 0 else "BUY"
                child = self.child_command_id(receipt.cause_command_id, "reduce", str(position.conid))
                self._dispatch.reduce(position, side, quantity, child)
                current = self._with_child(current, child, "reduce", generation)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"reduction outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="reduction submitted; awaiting broker-confirmed flat state")

    def _with_child(self, receipt: LiquidationReceipt, command_id: str, phase: str, generation: int) -> LiquidationReceipt:
        child = ChildRef(command_id, phase, generation)
        if any(c.command_id == command_id for c in receipt.children):
            return receipt
        updated = replace(receipt, children=receipt.children + (child,))
        self._runs[receipt.cause_command_id] = updated
        if self._store is not None:
            self._store.save(updated, self._now())
        return updated
```

The old `_NON_FLAT` constant is deleted; nothing else imports it (checked with `grep -rn _NON_FLAT trader tests`; re-check before deleting).

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_liquidation_service.py tests/automation/test_session_controller.py -q --timeout=30`
Expected: all PASS except the one `xfailed` from Task 1. `test_disconnect_is_outcome_unknown_and_keeps_breaker_tripped` still passes (account scope keeps the old breaker rule).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "refactor: liquidation receipt carries scope, goal, phase and child refs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Scoped full close (`scope="conid"`, goal zero)

`REQUESTED → CANCELLING → VERIFYING → REDUCING → VERIFYING → CLOSED`. Before the first cancel the service hands protection over to the close (`ProtectionOwnershipPort.handover`). Every child is tracked; nothing is sent while a child is unknown. Routine progress never trips the breaker.

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class HandoverInfo:
    stop_price: Optional[float]
    target_price: Optional[float]

class ProtectionOwnershipPort(Protocol):
    def handover(self, *, account_id: str, conid: int, close_root_id: str, now: dt.datetime) -> HandoverInfo: ...
    def handover_account(self, *, account_id: str, close_root_id: str, now: dt.datetime) -> None: ...
    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              has_target: bool, now: dt.datetime) -> None: ...
    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None: ...
```

- The **confirmation standard** (used by every later task): a child is confirmed when the current generation is newer than the child's `submitted_generation` **and**, for a `cancel` or `reduce` child, `dispatch.find_orders(account_id, child.command_id)` returns only rows whose `status` is in `_ORDER_TERMINAL` (no rows at all = absent = confirmed). A `reprotect` child is confirmed as soon as the generation is newer: a working replacement leg is the *intended* outcome, and an absent one is handled by recovery (Task 6). Anything else is unknown. `_children_confirmed(receipt, generation) -> tuple[bool, str]`.

- [ ] **Step 1: Extend the test fakes and write the failing tests**

Replace `_Dispatch` in `tests/test_liquidation_service.py` with a version that records partial reduces and OCA placements and answers `find_orders` from a dict you fill per test:

```python
class _Dispatch:
    def __init__(self):
        self.calls = []
        self.orders: dict[str, list] = {}      # child command id -> rows with .status
        self.fail_place_exit_oca: Exception | None = None

    def cancel(self, order, command_id):
        self.calls.append(("cancel", order.order_entity_id, command_id))

    def reduce(self, position, side, quantity, command_id):
        self.calls.append(("reduce", position.conid, side, quantity, command_id))

    def reduce_partial(self, position, side, quantity, command_id):
        self.calls.append(("reduce_partial", position.conid, side, quantity, command_id))

    def place_exit_oca(self, position, *, quantity, stop_price, target_price, command_id, legs):
        if self.fail_place_exit_oca is not None:
            raise self.fail_place_exit_oca
        self.calls.append(("place_exit_oca", position.conid, quantity, stop_price, target_price, command_id, legs))

    def find_orders(self, account_id, command_id):
        return list(self.orders.get(command_id, []))


class _Protection:
    def __init__(self, stop_price=95.0, target_price=None):
        self.calls = []
        self.info = (stop_price, target_price)

    def handover(self, *, account_id, conid, close_root_id, now):
        from trader.trading.liquidation_service import HandoverInfo
        self.calls.append(("handover", conid, close_root_id))
        return HandoverInfo(*self.info)

    def handover_account(self, *, account_id, close_root_id, now):
        self.calls.append(("handover_account", close_root_id))

    def release_after_partial(self, *, close_root_id, remaining_quantity, has_target, now):
        self.calls.append(("release_after_partial", close_root_id, remaining_quantity, has_target))

    def close_after_full(self, *, close_root_id, now):
        self.calls.append(("close_after_full", close_root_id))


def _row(status, group):
    return SimpleNamespace(status=status, order_group_id=group)


def _stop_order(entity="stop-1", conid=1, group="og-entry-1", quantity=10):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="AAPL",
        order_group_id=group, leg="stop", is_external=False, action="SELL", order_type="STP",
        total_quantity=quantity, filled_quantity=0, avg_fill_price=None, limit_price=None,
        stop_price=95.0, tif="DAY", status="Submitted", deleted=False, revision=1,
        source_timestamp=NOW,
    )


def _scoped(snapshots, *, protection=None, now=NOW):
    dispatch, breaker = _Dispatch(), _Breaker()
    protection = protection or _Protection()
    service = LiquidationService(_Broker(snapshots), dispatch, breaker=breaker,
                                 now=lambda: now, protection=protection)
    return service, dispatch, breaker, protection
```

Add `from types import SimpleNamespace` at the top of the file. Then the tests:

```python
DEADLINE = NOW + dt.timedelta(minutes=5)


def test_full_close_hands_over_then_cancels_only_that_conids_orders():
    other = _position(quantity=5.0)
    other = BrokerPositionRow(**{**other.__dict__, "conid": 2, "symbol": "MSFT"})
    service, dispatch, breaker, protection = _scoped([
        _snapshot(1, [_position(), other], [_stop_order(), _stop_order("stop-2", conid=2, group="og-entry-2")]),
    ])
    receipt = service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert protection.calls[0] == ("handover", 1, "close-1")
    assert receipt.state == "VERIFYING"
    assert receipt.phase == "cancel"
    assert dispatch.calls == [("cancel", "stop-1", "close-1-liquidation-cancel-stop-1")]
    assert receipt.children[0].command_id == "close-1-liquidation-cancel-stop-1"
    assert receipt.children[0].submitted_generation == 1
    assert breaker.calls == []


def test_full_close_reduces_only_after_cancel_is_confirmed_on_newer_generation():
    service, dispatch, _breaker, _ = _scoped([
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(1, [_position()], []),            # same generation: not proof
        _snapshot(2, [_position()], []),            # newer generation, stop gone
    ])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert service.rescan().state == "VERIFYING"    # generation 1 again → wait
    assert [c[0] for c in dispatch.calls] == ["cancel"]
    dispatch.orders["close-1-liquidation-cancel-stop-1"] = [_row("Cancelled", "close-1-liquidation-cancel-stop-1")]
    receipt = service.rescan()
    assert receipt.state == "VERIFYING"
    assert receipt.phase == "reduce"
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 10.0, "close-1-liquidation-reduce-1")


def test_full_close_waits_while_cancel_child_is_unknown():
    service, dispatch, _b, _ = _scoped([
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(2, [_position()], []),
    ])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    dispatch.orders["close-1-liquidation-cancel-stop-1"] = [_row("PendingCancel", "x")]
    receipt = service.rescan()
    assert receipt.state == "VERIFYING"
    assert "awaiting child confirmation" in receipt.detail
    assert [c[0] for c in dispatch.calls] == ["cancel"]


def test_full_close_ends_closed_on_zero_position_and_releases_saga():
    service, dispatch, breaker, protection = _scoped([
        _snapshot(1, [_position()], []),
        _snapshot(2, [], []),
    ])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert dispatch.calls == [("reduce", 1, "SELL", 10.0, "close-1-liquidation-reduce-1")]
    dispatch.orders["close-1-liquidation-reduce-1"] = [_row("Filled", "close-1-liquidation-reduce-1")]
    receipt = service.rescan()
    assert receipt.state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "close-1")
    assert breaker.calls == []


def test_close_ends_closed_without_reduce_when_position_vanished():
    """Review focus 3: the stop filled during the cancel race."""
    service, dispatch, _b, protection = _scoped([
        _snapshot(1, [_position()], [_stop_order()]),
        _snapshot(2, [], []),
    ])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    dispatch.orders["close-1-liquidation-cancel-stop-1"] = [_row("Filled", "x")]
    receipt = service.rescan()
    assert receipt.state == "CLOSED"
    assert [c[0] for c in dispatch.calls] == ["cancel"]
    assert ("close_after_full", "close-1") in protection.calls


def test_full_close_of_short_reduces_with_buy():
    """Review focus 1."""
    service, dispatch, _b, _ = _scoped([_snapshot(1, [_position(-7.0)], [])])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    assert dispatch.calls == [("reduce", 1, "BUY", 7.0, "close-1-liquidation-reduce-1")]


def test_full_close_ignores_other_conids_position():
    other = BrokerPositionRow(**{**_position(5.0).__dict__, "conid": 2, "symbol": "MSFT"})
    service, dispatch, _b, _ = _scoped([
        _snapshot(1, [_position(), other], []),
        _snapshot(2, [other], []),
    ])
    service.start(ACCOUNT, "close-1", DEADLINE, scope="conid", conid=1)
    dispatch.orders["close-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    assert service.rescan().state == "CLOSED"
    assert all(c[1] == 1 for c in dispatch.calls)


def test_scoped_close_deadline_is_failed_safe_and_trips_breaker():
    service, dispatch, breaker, _ = _scoped([_snapshot(1, [_position()], [])], now=NOW)
    receipt = service.start(ACCOUNT, "close-1", NOW, scope="conid", conid=1)
    assert receipt.state == "FAILED_SAFE"
    assert dispatch.calls == []
    assert breaker.calls == [("close-1", receipt.detail)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30 -k "full_close or vanished or short or other_conids or scoped_close_deadline"`
Expected: FAIL with `NotImplementedError: conid scope arrives in plan 1 task 5` (and `ImportError` for `HandoverInfo`).

- [ ] **Step 3: Implement**

Add after `ChildRef`:

```python
@dataclass(frozen=True)
class HandoverInfo:
    stop_price: Optional[float]
    target_price: Optional[float]


class ProtectionOwnershipPort(Protocol):
    def handover(self, *, account_id: str, conid: int, close_root_id: str, now: dt.datetime) -> HandoverInfo: ...
    def handover_account(self, *, account_id: str, close_root_id: str, now: dt.datetime) -> None: ...
    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              has_target: bool, now: dt.datetime) -> None: ...
    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None: ...
```

Replace the `_advance_conid` stub and add helpers:

```python
    # -- shared helpers ---------------------------------------------------

    @staticmethod
    def _position_for(snapshot, conid: int):
        for position in snapshot.positions:
            if int(position.conid) == int(conid) and float(position.quantity) != 0.0:
                return position
        return None

    @staticmethod
    def _working_for(snapshot, conid: int) -> tuple:
        return tuple(order for order in snapshot.working_orders if int(order.conid) == int(conid))

    def _children_confirmed(self, receipt: LiquidationReceipt, generation: int) -> tuple[bool, str]:
        for child in receipt.children:
            if generation <= child.submitted_generation:
                return False, f"{child.command_id} submitted at generation {child.submitted_generation}"
            if child.phase == "reprotect":
                continue  # visible-or-absent on a newer generation is enough; recovery handles absence
            rows = self._dispatch.find_orders(receipt.account_id, child.command_id)
            for row in rows:
                if getattr(row, "status", None) not in _ORDER_TERMINAL:
                    return False, f"{child.command_id} is {getattr(row, 'status', '?')}"
        return True, ""

    def _finish_closed(self, receipt: LiquidationReceipt, generation: int, detail: str) -> LiquidationReceipt:
        closed = self._set(receipt, "CLOSED", generation_id=generation, detail=detail)
        if self._protection is not None:
            self._protection.close_after_full(close_root_id=receipt.cause_command_id, now=self._now())
        if self._registry is not None:
            self._registry.release(receipt.cause_command_id, self._now())
        return closed

    def _cancel_conid_orders(self, receipt: LiquidationReceipt, orders: tuple, generation: int) -> LiquidationReceipt:
        current = self._set(receipt, "CANCELLING", generation_id=generation, phase="cancel",
                            detail=f"cancelling {len(orders)} working orders on conid {receipt.conid}")
        try:
            for order in orders:
                child = self.child_command_id(receipt.cause_command_id, "cancel", str(order.order_entity_id))
                self._dispatch.cancel(order, child)
                current = self._with_child(current, child, "cancel", generation)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"cancel outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation,
                         detail="awaiting broker confirmation that conid orders are gone")

    def _submit_reduce(self, receipt: LiquidationReceipt, position, generation: int) -> LiquidationReceipt:
        broker_quantity = float(position.quantity)
        side = "SELL" if broker_quantity > 0 else "BUY"
        child = self.child_command_id(receipt.cause_command_id, "reduce", str(receipt.conid))
        current = self._set(receipt, "REDUCING", generation_id=generation, phase="reduce",
                            detail="submitting reduce-only close order")
        try:
            self._dispatch.reduce(position, side, abs(broker_quantity), child)
            current = self._with_child(current, child, "reduce", generation)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"reduction outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="close submitted; awaiting zero position")

    # -- conid scope -------------------------------------------------------

    def _advance_conid(self, receipt: LiquidationReceipt, snapshot, generation: int) -> LiquidationReceipt:
        result = self._advance_conid_once(receipt, snapshot, generation)
        if result.escalated and not receipt.escalated and result.state == "CANCELLING":
            # Escalation happened on this snapshot: run the full-close step right away.
            return self._advance_conid_once(result, snapshot, generation)
        return result

    def _advance_conid_once(self, receipt: LiquidationReceipt, snapshot, generation: int) -> LiquidationReceipt:
        conid = int(receipt.conid)
        working = self._working_for(snapshot, conid)
        position = self._position_for(snapshot, conid)

        if receipt.state != "REQUESTED" and receipt.generation_id is not None and generation <= receipt.generation_id:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting newer broker generation")

        confirmed, why = self._children_confirmed(receipt, generation)
        if not confirmed:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail=f"awaiting child confirmation: {why}")

        if receipt.phase == "":
            receipt = self._handover(receipt, generation)

        if receipt.phase in ("cancel", "reduce") and receipt.goal_quantity is None:
            if working:
                return self._cancel_conid_orders(receipt, working, generation)
            if position is None:
                return self._finish_closed(receipt, generation, "fresh broker snapshot shows no position and no working orders for conid")
            return self._submit_reduce(receipt, position, generation)

        raise NotImplementedError("partial close arrives in plan 1 task 6")

    def _handover(self, receipt: LiquidationReceipt, generation: int) -> LiquidationReceipt:
        stop_price, target_price = receipt.stop_price, receipt.target_price
        if self._protection is not None:
            info = self._protection.handover(
                account_id=receipt.account_id, conid=int(receipt.conid),
                close_root_id=receipt.cause_command_id, now=self._now(),
            )
            stop_price = stop_price if stop_price is not None else info.stop_price
            target_price = target_price if target_price is not None else info.target_price
        return self._set(receipt, "CANCELLING", generation_id=generation, phase="cancel",
                         stop_price=stop_price, target_price=target_price,
                         detail="protection handed over to the close")
```

Why the order matters: `_handover` runs before `_cancel_conid_orders` so the saga is already `CLOSE_OWNED` when the broker reports the stop as cancelled. In `_advance_conid` the "newer generation" guard is skipped for `REQUESTED` because a brand-new root has no generation yet. After `_submit_reduce` the position check on the next newer generation decides `CLOSED`; if the position is still there with the reduce child confirmed terminal (fill reflected late), `_submit_reduce` runs again — it is reduce-only against the live broker quantity, so it cannot flip exposure.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: conid-scoped full close with protection hand-over

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Partial close, re-protect with a linked stop/target, recovery by ref, escalation

After the cancel phase the service sends `reduce_partial`, waits for the remaining broker quantity, then places the exit-only OCA pair for the remainder (`REPROTECTING → VERIFYING → DONE`). Recovery reads the broker by ref before placing. Any re-protect failure or a missed deadline in that phase escalates to a full close and trips the breaker.

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `reprotect_ref(root_id, leg)` from Task 2; `_Dispatch.reduce_partial/place_exit_oca/find_orders` fakes from Task 5.
- Produces: `LiquidationService.upgrade_to_zero(root_id) -> LiquidationReceipt` (Task 8 calls it when the registry reports `UPGRADED`). Partial rounding helper `partial_close_quantity(position_quantity: float, requested: float) -> Optional[float]` (module level): whole shares, `None` means "treat as a full close", `ValueError` for a request that rounds to zero.
- Quantity rules (review focus 2): `q = floor(requested)`; `q < 1` → `ValueError("PARTIAL_QUANTITY_INVALID")`; `q >= |position|` or `|position| - q < 1` → full close (`None`).
- Stop side rule: long → `stop_price < market_price`; short → `stop_price > market_price`. Otherwise escalate with detail `STOP_NOT_PROTECTIVE`. A missing stop price escalates with `STOP_PRICE_MISSING`.
- `DONE` means a generation newer than the placement shows a working row for `reprotect_ref(root, "stop")` (and `"target"` when `target_price` is set) with `total_quantity == expected_remaining`. `CLOSED` from the re-protect phase means position zero with any residual leg cancelled.

- [ ] **Step 1: Write the failing tests**

```python
from trader.trading.liquidation_service import partial_close_quantity


def _working_row(group, quantity=6.0, status="Submitted"):
    return SimpleNamespace(status=status, order_group_id=group, total_quantity=quantity)


def _position_with_price(quantity=10.0, market_price=100.0, conid=1):
    base = _position(quantity).__dict__
    base.update({"conid": conid, "market_price": market_price})
    return BrokerPositionRow(**base)


def test_partial_quantity_edge_cases():
    """Review focus 2."""
    assert partial_close_quantity(10.0, 4.7) == 4.0
    assert partial_close_quantity(-10.0, 4.0) == 4.0
    assert partial_close_quantity(10.0, 10.0) is None
    assert partial_close_quantity(10.0, 12.0) is None
    assert partial_close_quantity(10.0, 9.5) is None          # less than one share would remain
    with pytest.raises(ValueError):
        partial_close_quantity(10.0, 0.4)


def test_start_turns_oversized_partial_into_full_close():
    service, dispatch, _b, _ = _scoped([_snapshot(1, [_position()], [])])
    receipt = service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=10.0)
    assert receipt.goal_quantity is None
    assert dispatch.calls[0][0] == "reduce"


def test_partial_close_reduces_partially_then_reprotects_remaining_quantity():
    pos = _position_with_price(10.0, 100.0)
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, breaker, protection = _scoped([
        _snapshot(1, [pos], [_stop_order(quantity=10)]),
        _snapshot(2, [pos], []),
        _snapshot(3, [remaining], []),
        _snapshot(4, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6)]),
    ], protection=_Protection(stop_price=95.0, target_price=None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-cancel-stop-1"] = [_row("Cancelled", "c")]
    receipt = service.rescan()
    assert dispatch.calls[-1] == ("reduce_partial", 1, "SELL", 4.0, "p-1-liquidation-reduce-1")
    assert (receipt.state, receipt.phase, receipt.expected_remaining) == ("VERIFYING", "reduce", 6.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()
    assert receipt.state == "VERIFYING" and receipt.phase == "reprotect"
    assert dispatch.calls[-1] == ("place_exit_oca", 1, 6.0, 95.0, None, "p-1", ("stop",))
    assert receipt.children[-1].command_id == "p-1-reprotect-stop"
    dispatch.orders["p-1-reprotect-stop"] = [_working_row("p-1-reprotect-stop", 6.0)]
    receipt = service.rescan()
    assert receipt.state == "DONE"
    assert protection.calls[-1] == ("release_after_partial", "p-1", 6.0, False)
    assert breaker.calls == []


def test_partial_close_with_target_places_both_legs_and_waits_for_both():
    pos = _position_with_price(10.0, 100.0)
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, protection = _scoped([
        _snapshot(1, [pos], []),
        _snapshot(2, [remaining], []),
        _snapshot(3, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6)]),
        _snapshot(4, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6),
                                   _stop_order("rt", group="p-1-reprotect-target", quantity=6)]),
    ], protection=_Protection(stop_price=95.0, target_price=120.0))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()
    assert dispatch.calls[-1] == ("place_exit_oca", 1, 6.0, 95.0, 120.0, "p-1", ("stop", "target"))
    dispatch.orders["p-1-reprotect-stop"] = [_working_row("p-1-reprotect-stop", 6.0)]
    receipt = service.rescan()                       # generation 3: only the stop is visible
    assert receipt.state == "VERIFYING"
    dispatch.orders["p-1-reprotect-target"] = [_working_row("p-1-reprotect-target", 6.0)]
    receipt = service.rescan()                       # generation 4: both legs working
    assert receipt.state == "DONE"
    assert protection.calls[-1] == ("release_after_partial", "p-1", 6.0, True)


def test_recovery_places_only_missing_reprotect_leg():
    """Review focus 5: after a restart the stop is already working; place only the target."""
    remaining = _position_with_price(6.0, 100.0)
    broker = _Broker([
        _snapshot(5, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6)]),
        _snapshot(6, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6),
                                   _stop_order("rt", group="p-1-reprotect-target", quantity=6)]),
    ])
    dispatch, breaker, protection = _Dispatch(), _Breaker(), _Protection(95.0, 120.0)
    dispatch.orders["p-1-reprotect-stop"] = [_working_row("p-1-reprotect-stop", 6.0)]
    service = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: NOW, protection=protection)
    # Recovered receipt: reduce confirmed, re-protect phase, nothing recorded as placed.
    recovered = LiquidationReceipt(ACCOUNT, "p-1", "REPROTECTING", DEADLINE, generation_id=4, scope="conid",
                                   conid=1, goal_quantity=4.0, phase="reprotect", expected_remaining=6.0,
                                   stop_price=95.0, target_price=120.0)
    service._runs["p-1"] = recovered
    receipt = service.rescan()
    assert dispatch.calls == [("place_exit_oca", 1, 6.0, 95.0, 120.0, "p-1", ("target",))]
    dispatch.orders["p-1-reprotect-target"] = [_working_row("p-1-reprotect-target", 6.0)]
    assert service.rescan().state == "DONE"


def test_reprotect_closed_by_exit_cancels_residual_leg_and_ends_closed():
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, protection = _scoped([
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
        _snapshot(3, [], [_stop_order("rt", group="p-1-reprotect-target", quantity=6)]),   # stop filled; target residual
        _snapshot(4, [], []),
    ], protection=_Protection(95.0, 120.0))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    service.rescan()                                             # places both legs at generation 2
    dispatch.orders["p-1-reprotect-stop"] = [_row("Filled", "s")]
    dispatch.orders["p-1-reprotect-target"] = [_working_row("p-1-reprotect-target", 6.0)]
    receipt = service.rescan()                                   # generation 3
    assert dispatch.calls[-1][0] == "cancel" and dispatch.calls[-1][1] == "rt"
    assert receipt.state == "VERIFYING"
    dispatch.orders["p-1-reprotect-target"] = [_row("Cancelled", "t")]
    dispatch.orders[dispatch.calls[-1][2]] = [_row("Cancelled", "c")]
    receipt = service.rescan()                                   # generation 4
    assert receipt.state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_close_of_short_reprotects_above_price():
    """Review focus 1."""
    short = _position_with_price(-10.0, 100.0)
    remaining = _position_with_price(-6.0, 100.0)
    service, dispatch, _b, _ = _scoped([
        _snapshot(1, [short], []), _snapshot(2, [remaining], []),
    ], protection=_Protection(stop_price=105.0, target_price=None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    assert dispatch.calls[0] == ("reduce_partial", 1, "BUY", 4.0, "p-1-liquidation-reduce-1")
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()
    assert dispatch.calls[-1] == ("place_exit_oca", 1, 6.0, 105.0, None, "p-1", ("stop",))
    assert receipt.state == "VERIFYING"


def test_non_protective_stop_escalates_to_full_close_and_trips_breaker():
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, breaker, _ = _scoped([
        _snapshot(1, [_position_with_price(10.0)], []), _snapshot(2, [remaining], []),
    ], protection=_Protection(stop_price=101.0, target_price=None))     # above market on a long
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()
    assert receipt.escalated is True and receipt.goal_quantity is None
    assert any("STOP_NOT_PROTECTIVE" in detail for _root, detail in breaker.calls)
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-liquidation-reduce-1")


def test_place_exit_oca_failure_escalates_once_then_failed_safe_at_deadline():
    remaining = _position_with_price(6.0, 100.0)
    clock = {"now": NOW}
    broker = _Broker([_snapshot(1, [_position_with_price(10.0)], []), _snapshot(2, [remaining], [])])
    dispatch, breaker, protection = _Dispatch(), _Breaker(), _Protection(95.0, None)
    dispatch.fail_place_exit_oca = RuntimeError("IB rejected")
    service = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: clock["now"],
                                 protection=protection, deadline_seconds=60.0)
    service.start(ACCOUNT, "p-1", NOW + dt.timedelta(minutes=5), scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()
    assert receipt.escalated is True
    assert receipt.deadline == NOW + dt.timedelta(seconds=60)
    assert dispatch.calls[-1][0] == "reduce"
    clock["now"] = NOW + dt.timedelta(seconds=61)
    assert service.rescan().state == "FAILED_SAFE"


def test_reprotect_deadline_escalates_instead_of_failing_safe():
    remaining = _position_with_price(6.0, 100.0)
    clock = {"now": NOW}
    broker = _Broker([_snapshot(1, [_position_with_price(10.0)], []), _snapshot(2, [remaining], []), _snapshot(3, [remaining], [])])
    dispatch, breaker, protection = _Dispatch(), _Breaker(), _Protection(95.0, None)
    service = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: clock["now"],
                                 protection=protection, deadline_seconds=60.0)
    service.start(ACCOUNT, "p-1", NOW + dt.timedelta(seconds=30), scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    service.rescan()                                   # places the stop, phase reprotect
    clock["now"] = NOW + dt.timedelta(seconds=31)      # deadline passes while re-protecting
    receipt = service.rescan()
    assert receipt.state != "FAILED_SAFE"
    assert receipt.escalated is True and receipt.goal_quantity is None
    assert breaker.calls


def test_upgrade_to_zero_during_reprotect_cancels_replacement_exits_and_closes():
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, protection = _scoped([
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
        _snapshot(3, [remaining], [_stop_order("rs", group="p-1-reprotect-stop", quantity=6)]),
        _snapshot(4, [remaining], []),
        _snapshot(5, [], []),
    ], protection=_Protection(95.0, None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    service.rescan()                                             # stop placed at generation 2
    dispatch.orders["p-1-reprotect-stop"] = [_working_row("p-1-reprotect-stop", 6.0)]
    receipt = service.upgrade_to_zero("p-1")
    assert receipt.goal_quantity is None
    receipt = service.rescan()                                   # generation 3: cancel the replacement stop
    assert dispatch.calls[-1][0] == "cancel" and dispatch.calls[-1][1] == "rs"
    dispatch.orders["p-1-reprotect-stop"] = [_row("Cancelled", "s")]
    dispatch.orders[dispatch.calls[-1][2]] = [_row("Cancelled", "c")]
    receipt = service.rescan()                                   # generation 4: reduce remainder
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-liquidation-reduce-1")
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r2")]
    receipt = service.rescan()                                   # generation 5
    assert receipt.state == "CLOSED"
    assert ("release_after_partial", "p-1", 6.0, False) not in protection.calls
    assert protection.calls[-1] == ("close_after_full", "p-1")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30 -k "partial or reprotect or escalat or upgrade or recovery"`
Expected: FAIL (`ImportError: partial_close_quantity`, `NotImplementedError: partial close arrives in plan 1 task 6`).

- [ ] **Step 3: Implement**

Module-level helper (near `_children_from_json`):

```python
import math
from trader.trading.order_correlation import reprotect_ref


def partial_close_quantity(position_quantity: float, requested: float) -> Optional[float]:
    """Whole-share partial size, or None when the request amounts to a full close."""
    held = abs(float(position_quantity))
    shares = math.floor(float(requested))
    if shares < 1:
        raise ValueError("PARTIAL_QUANTITY_INVALID")
    if shares >= held or held - shares < 1:
        return None
    return float(shares)
```

In `start`, after the validation block and before the `current = self._runs.get(...)` lookup, resolve the partial quantity against the live position:

```python
        if scope == "conid" and quantity is not None:
            snapshot = self._broker.capture(account_id)
            position = self._position_for(snapshot, int(conid))
            if position is None:
                raise ValueError("NO_POSITION")
            quantity = partial_close_quantity(float(position.quantity), float(quantity))
```

Replace the `raise NotImplementedError("partial close ...")` in `_advance_conid` with the partial branches, and add the helpers:

```python
        if receipt.phase == "cancel":                       # partial goal: cancel first, then partial reduce
            if working:
                return self._cancel_conid_orders(receipt, working, generation)
            if position is None:
                return self._finish_closed(receipt, generation, "position closed before the partial reduce")
            return self._submit_partial_reduce(receipt, position, generation)

        if receipt.phase == "reduce":                       # partial goal: wait for the remaining quantity
            if position is None:
                return self._finish_closed(receipt, generation, "position closed during the partial reduce")
            if abs(float(position.quantity)) != float(receipt.expected_remaining):
                return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting partial reduce fill")
            return self._reprotect(receipt, position, working, generation)

        if receipt.phase == "reprotect":
            return self._verify_reprotect(receipt, position, working, generation)

        raise RuntimeError(f"unknown close phase {receipt.phase!r}")

    def _submit_partial_reduce(self, receipt, position, generation):
        broker_quantity = float(position.quantity)
        side = "SELL" if broker_quantity > 0 else "BUY"
        q = float(receipt.goal_quantity)
        child = self.child_command_id(receipt.cause_command_id, "reduce", str(receipt.conid))
        current = self._set(receipt, "REDUCING", generation_id=generation, phase="reduce",
                            expected_remaining=abs(broker_quantity) - q,
                            detail=f"submitting partial reduce of {q:g}")
        try:
            self._dispatch.reduce_partial(position, side, q, child)
            current = self._with_child(current, child, "reduce", generation)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"partial reduction outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="partial reduce submitted; awaiting remaining quantity")

    def _reprotect_legs(self, receipt) -> tuple[str, ...]:
        return ("stop", "target") if receipt.target_price is not None else ("stop",)

    def _leg_rows(self, receipt, working, leg: str) -> list:
        ref = reprotect_ref(receipt.cause_command_id, leg)
        return [order for order in working if getattr(order, "order_group_id", None) == ref]

    def _stop_is_protective(self, receipt, position) -> bool:
        price = getattr(position, "market_price", None)
        if receipt.stop_price is None or price is None:
            return False
        if float(position.quantity) > 0:
            return float(receipt.stop_price) < float(price)
        return float(receipt.stop_price) > float(price)

    def _reprotect(self, receipt, position, working, generation):
        if receipt.stop_price is None:
            return self._escalate(receipt, generation, "STOP_PRICE_MISSING: no stop price for re-protect")
        if not self._stop_is_protective(receipt, position):
            return self._escalate(receipt, generation, "STOP_NOT_PROTECTIVE: stop is not on the protective side of the market price")
        remaining = abs(float(position.quantity))
        missing = tuple(leg for leg in self._reprotect_legs(receipt) if not self._leg_rows(receipt, working, leg))
        current = self._set(receipt, "REPROTECTING", generation_id=generation, phase="reprotect",
                            expected_remaining=remaining, detail=f"placing re-protect legs {missing}")
        if not missing:
            return self._set(current, "VERIFYING", generation_id=generation, detail="re-protect legs already working")
        try:
            self._dispatch.place_exit_oca(
                position, quantity=remaining, stop_price=float(receipt.stop_price),
                target_price=receipt.target_price, command_id=receipt.cause_command_id, legs=missing,
            )
            for leg in missing:
                current = self._with_child(current, reprotect_ref(receipt.cause_command_id, leg), "reprotect", generation)
        except Exception as exc:
            return self._escalate(current, generation, f"REPROTECT_FAILED: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="re-protect submitted; awaiting working legs")

    def _verify_reprotect(self, receipt, position, working, generation):
        if position is None:
            residual = [order for leg in ("stop", "target") for order in self._leg_rows(receipt, working, leg)]
            if residual:
                return self._cancel_conid_orders(
                    replace(receipt, phase="reprotect"), tuple(residual), generation)
            return self._finish_closed(receipt, generation, "position closed by an exit; no residual legs")
        remaining = abs(float(position.quantity))
        for leg in self._reprotect_legs(receipt):
            rows = self._leg_rows(receipt, working, leg)
            if not rows:
                return self._reprotect(receipt, position, working, generation)   # recovery: place only the missing leg
            if any(float(getattr(row, "total_quantity", remaining)) != remaining for row in rows):
                return self._escalate(receipt, generation, "REPROTECT_QUANTITY_MISMATCH: working leg size differs from the position")
        done = self._set(receipt, "DONE", generation_id=generation, expected_remaining=remaining,
                         detail="re-protect legs working for the remaining quantity")
        if self._protection is not None:
            self._protection.release_after_partial(
                close_root_id=receipt.cause_command_id, remaining_quantity=remaining,
                has_target=receipt.target_price is not None, now=self._now(),
            )
        if self._registry is not None:
            self._registry.release(receipt.cause_command_id, self._now())
        return done

    def _escalate(self, receipt, generation, reason: str):
        """Give up on the partial goal: trip the breaker and continue as a full close.

        generation_id is left as it was so the caller may advance the escalated
        receipt once more on the same snapshot without tripping the
        newer-generation guard."""
        if self._breaker is not None:
            self._breaker.trip_liquidation(receipt.cause_command_id, reason)
        return self._set(
            receipt, "CANCELLING", phase="cancel", goal_quantity=None, escalated=True,
            deadline=self._now() + dt.timedelta(seconds=self._deadline_seconds),
            detail=f"escalated to full close: {reason}",
        )

    def upgrade_to_zero(self, root_id: str) -> LiquidationReceipt:
        receipt = self._runs[root_id]
        if receipt.scope != "conid":
            raise ValueError("only a conid-scoped close can be upgraded")
        if receipt.state in _RESCAN_TERMINAL:
            return receipt
        if receipt.goal_quantity is None:
            return receipt
        return self._set(receipt, "CANCELLING", phase="cancel", goal_quantity=None,
                         detail="goal upgraded to zero exposure; replacement exits will be cancelled")
```

Two adjustments to earlier code so the branches compose:

1. In `_advance` the deadline check becomes:

```python
        if self._now() >= receipt.deadline:
            if receipt.scope == "conid" and receipt.phase == "reprotect" and not receipt.escalated:
                escalated = self._escalate(receipt, receipt.generation_id or 0, "REPROTECT_DEADLINE: re-protect missed its deadline")
                return self._advance(escalated)   # fresh snapshot; the new deadline is in the future
            return self._set(receipt, "FAILED_SAFE", detail="liquidation deadline elapsed without broker-confirmed flat state")
```

2. The goal-zero branch from Task 5 (`if receipt.phase in ("cancel", "reduce") and receipt.goal_quantity is None:`) stays first; the new `phase == "cancel"` / `"reduce"` branches below it therefore only run for a partial goal. After `upgrade_to_zero` sets `goal_quantity=None` and `phase="cancel"`, the zero branch cancels any working replacement legs for the conid and then reduces the remainder — exactly the sequence `test_upgrade_to_zero_during_reprotect_cancels_replacement_exits_and_closes` pins.

Note on escalation: `_escalate` never captures a snapshot. `_advance_conid` runs the escalated receipt once more on the snapshot it already holds, and `escalated=True` makes a second deadline miss `FAILED_SAFE`, so there is no loop.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: partial close with linked re-protect and escalation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Account flatten takes over scoped closes in the right order

Spec 5.1, "An account flatten takes over every scoped owner": claim and supersede in one transaction → hand every open saga over → locate every child the superseded closes submitted → cancel every identified working order (including replacement protection) → wait until **every** ref is confirmed on a newer generation → reduce. A deadline with any ref still unknown is `FAILED_SAFE`, never a second order.

**Files:**
- Modify: `trader/trading/liquidation_service.py`
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `ExitOwnerRegistry.claim_account` (Task 3) via the service's optional `registry`; `ProtectionOwnershipPort.handover_account` (Task 5).
- Produces: `LiquidationService.supersede(root_id, by_root_id) -> LiquidationReceipt` (marks a scoped root `SUPERSEDED`, no breaker, returns it so the flatten can inherit its children); account-scope `start` inherits those children.

- [ ] **Step 1: Write the failing tests**

```python
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration


def _service_with_registry(tmp_path, snapshots, *, protection=None, now=NOW):
    db = DuckDBConnection.get_instance(str(tmp_path / "owners.duckdb"))
    apply_exit_owner_migration(SchemaMigrator(db))
    registry = ExitOwnerRegistry(db)
    dispatch, breaker = _Dispatch(), _Breaker()
    protection = protection or _Protection()
    clock = {"now": now}
    service = LiquidationService(_Broker(snapshots), dispatch, breaker=breaker, now=lambda: clock["now"],
                                 registry=registry, protection=protection)
    return service, dispatch, breaker, protection, registry, clock


def test_flatten_cancels_identified_replacement_exits_and_reaches_flat_without_waiting_for_them(tmp_path):
    """Spec test: kill during REPROTECTING with working replacement exits."""
    remaining = _position_with_price(6.0, 100.0)
    reprotect_stop = _stop_order("rs", group="p-1-reprotect-stop", quantity=6)
    service, dispatch, breaker, protection, registry, _clock = _service_with_registry(tmp_path, [
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
        _snapshot(3, [remaining], [reprotect_stop]),      # partial close is REPROTECTING here
        _snapshot(4, [remaining], []),                     # cancel confirmed
        _snapshot(5, [], []),                              # reduce confirmed
    ], protection=_Protection(95.0, None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    service.rescan()                                       # places the replacement stop (generation 2)
    dispatch.orders["p-1-reprotect-stop"] = [_working_row("p-1-reprotect-stop", 6.0)]

    receipt = service.start(ACCOUNT, "flat-1", DEADLINE)   # account flatten at generation 3
    assert service.receipt_for("p-1").state == "SUPERSEDED"
    assert registry.account_owner(ACCOUNT).root_id == "flat-1"
    assert ("handover_account", "flat-1") in protection.calls
    cancels = [c for c in dispatch.calls if c[0] == "cancel"]
    assert cancels[-1][1] == "rs"                          # the replacement stop was cancelled, not waited on
    assert receipt.state == "VERIFYING"
    assert any(child.command_id == "p-1-reprotect-stop" for child in receipt.children)   # inherited

    dispatch.orders["p-1-reprotect-stop"] = [_row("Cancelled", "s")]
    dispatch.orders[cancels[-1][2]] = [_row("Cancelled", "c")]
    receipt = service.rescan()                             # generation 4 → reduce
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "flat-1-liquidation-reduce-1")
    dispatch.orders["flat-1-liquidation-reduce-1"] = [_row("Filled", "f")]
    assert service.rescan().state == "FLAT"                # generation 5
    assert service.receipt_for("p-1").state == "SUPERSEDED"


def test_flatten_never_reduces_while_an_inherited_child_is_unknown(tmp_path):
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, breaker, _p, _r, clock = _service_with_registry(tmp_path, [
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),            # partial reduce submitted at generation 1 is still working
        _snapshot(3, [remaining], []),
    ])
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)      # reduce_partial at gen 1
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Submitted", "r")]              # visible, not terminal
    receipt = service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(minutes=2))          # flatten at gen 2
    assert receipt.state == "VERIFYING"
    assert "awaiting child confirmation" in receipt.detail
    assert [c[0] for c in dispatch.calls] == ["reduce_partial"]          # nothing working to cancel, no reduce yet
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()                                            # gen 3: child confirmed → reduce
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "flat-1-liquidation-reduce-1")


def test_flatten_deadline_with_unknown_child_is_failed_safe_not_a_second_order(tmp_path):
    service, dispatch, breaker, _p, _r, clock = _service_with_registry(tmp_path, [
        _snapshot(1, [_position()], []),
        _snapshot(1, [_position()], []),
    ])
    service.start(ACCOUNT, "flat-1", NOW + dt.timedelta(seconds=30))     # reduce submitted at gen 1
    assert [c[0] for c in dispatch.calls] == ["reduce"]
    clock["now"] = NOW + dt.timedelta(seconds=31)
    receipt = service.rescan()
    assert receipt.state == "FAILED_SAFE"
    assert [c[0] for c in dispatch.calls] == ["reduce"]
    assert breaker.calls


def test_superseded_close_stops_at_once_and_never_reprotects(tmp_path):
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, protection, _r, _c = _service_with_registry(tmp_path, [
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
    ], protection=_Protection(95.0, None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    service.start(ACCOUNT, "flat-1", DEADLINE)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    service.rescan()
    assert not any(c[0] == "place_exit_oca" for c in dispatch.calls)
    assert not any(c[0] == "release_after_partial" for c in protection.calls)
    assert service.receipt_for("p-1").state == "SUPERSEDED"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30 -k "flatten or superseded"`
Expected: FAIL — `SUPERSEDED` never set, `handover_account` never called, replacement stop not cancelled before the position reduce.

- [ ] **Step 3: Implement**

Add to `LiquidationService`:

```python
    def supersede(self, root_id: str, by_root_id: str) -> LiquidationReceipt:
        receipt = self._runs[root_id]
        if receipt.state in _RESCAN_TERMINAL:
            return receipt
        return self._set(receipt, "SUPERSEDED", detail=f"taken over by account flatten {by_root_id}")

    def _claim_account_owner(self, receipt: LiquidationReceipt) -> LiquidationReceipt:
        """First pass of an account flatten: own the account, supersede scoped closes, inherit their children."""
        inherited: list[ChildRef] = []
        if self._registry is not None:
            claim = self._registry.claim_account(account_id=receipt.account_id,
                                                 root_id=receipt.cause_command_id, now=self._now())
            for root in claim.superseded:
                superseded = self.supersede(root, receipt.cause_command_id) if root in self._runs else None
                if superseded is not None:
                    inherited.extend(superseded.children)
        if self._protection is not None:
            self._protection.handover_account(account_id=receipt.account_id,
                                              close_root_id=receipt.cause_command_id, now=self._now())
        children = receipt.children + tuple(c for c in inherited if c.command_id not in {x.command_id for x in receipt.children})
        return self._set(receipt, "REQUESTED", phase="cancel", children=children,
                         detail="account owner claimed; scoped closes superseded")
```

Change `_advance_account` so its first pass claims, and so it waits for every child before reducing:

```python
    def _advance_account(self, receipt: LiquidationReceipt, snapshot, generation: int) -> LiquidationReceipt:
        if receipt.phase == "":
            receipt = self._claim_account_owner(receipt)

        confirmed, why = self._children_confirmed(receipt, generation)

        # A zero snapshot is valid only after the generation that observed or
        # submitted the action, and only once every child is accounted for.
        if (not snapshot.positions and not snapshot.working_orders and receipt.generation_id is not None
                and generation > receipt.generation_id and confirmed):
            return self._set(receipt, "FLAT", generation_id=generation, detail="fresh broker snapshot confirms no positions or working orders")

        # Never retry a submitted reduction absent new broker truth.
        if receipt.state in {"REDUCING", "VERIFYING", "OUTCOME_UNKNOWN"} and receipt.generation_id is not None and generation <= receipt.generation_id:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting newer broker generation")

        working = tuple(snapshot.working_orders)
        if working:
            # Cancel every positively identified working order, replacement protection included.
            current = self._set(receipt, "CANCELLING_ENTRIES", generation_id=generation, detail="cancelling broker-reported working orders")
            try:
                for order in working:
                    child = self.child_command_id(receipt.cause_command_id, "cancel", str(order.order_entity_id))
                    self._dispatch.cancel(order, child)
                    current = self._with_child(current, child, "cancel", generation)
            except Exception as exc:
                return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"cancel outcome unknown: {exc}")
            return self._set(current, "VERIFYING", generation_id=generation, detail="awaiting broker confirmation that working orders are gone")

        if not confirmed:
            # An unknown child forbids a new reduce; it never forbade the cancels above.
            return self._set(receipt, "VERIFYING", generation_id=generation, detail=f"awaiting child confirmation: {why}")

        positions = tuple(position for position in snapshot.positions if float(position.quantity) != 0.0)
        if not positions:
            return self._set(receipt, "VERIFYING", generation_id=generation, detail="awaiting fresh broker flat confirmation")
        current = self._set(receipt, "REDUCING", generation_id=generation, detail="submitting reduce-only liquidation orders")
        try:
            for position in positions:
                quantity = abs(float(position.quantity))
                side = "SELL" if position.quantity > 0 else "BUY"
                child = self.child_command_id(receipt.cause_command_id, "reduce", str(position.conid))
                self._dispatch.reduce(position, side, quantity, child)
                current = self._with_child(current, child, "reduce", generation)
        except Exception as exc:
            return self._set(current, "OUTCOME_UNKNOWN", generation_id=generation, detail=f"reduction outcome unknown: {exc}")
        return self._set(current, "VERIFYING", generation_id=generation, detail="reduction submitted; awaiting broker-confirmed flat state")
```

Parity note: with no registry and no children, `confirmed` is always `True` and this is the old flow; the eight original tests must still pass. `_set` for `SUPERSEDED` does not trip the breaker because `_trips_breaker` only fires for conid scope on `FAILED_SAFE`.

Also make `_advance_conid` return the receipt unchanged when `receipt.state == "SUPERSEDED"` (it is in `_RESCAN_TERMINAL`, so `_advance` already returns early — add a test assertion only, no code).

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: account flatten supersedes scoped closes and waits for every child

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Scoped `start` claims through the registry — join, upgrade, refuse

A scoped request never creates a competing root. The registry decides: `CLAIMED` starts a new root; `JOINED` returns the existing root's receipt; `UPGRADED` calls `upgrade_to_zero` on the existing root; `JOINED_FLATTEN` returns the account flatten's receipt; a partial against any owner raises `ExitInProgress`. The account owner is checked first and a `SUPERSEDED` owner is never upgraded (both are registry rules from Task 3; this task wires them in and pins them at the service level).

**Files:**
- Modify: `trader/trading/liquidation_service.py` (`start`)
- Test: `tests/test_liquidation_service.py`

**Interfaces:**
- Consumes: `ExitOwnerRegistry.claim_scoped`, `ExitInProgress` (Task 3); `upgrade_to_zero` (Task 6).
- Produces: `start(...)` for `scope="conid"` returns the receipt of the root the caller must poll (`receipt.cause_command_id` may differ from the `cause_command_id` passed in). `ExitInProgress` propagates to the caller.

- [ ] **Step 1: Write the failing tests**

```python
from trader.trading.exit_owner import ExitInProgress


def test_second_full_close_joins_and_returns_the_owners_receipt(tmp_path):
    service, dispatch, _b, _p, _r, _c = _service_with_registry(tmp_path, [_snapshot(1, [_position()], [])])
    first = service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    joined = service.start(ACCOUNT, "ai-close-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "exit-1"
    assert service.receipt_for("ai-close-1") is None
    assert [c[0] for c in dispatch.calls] == ["reduce"]           # one root, one order


def test_time_exit_during_partial_close_upgrades_goal_and_ends_closed(tmp_path):
    """Spec test: a time exit arriving during a partial close ends with zero position, not DONE."""
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, protection, _r, _c = _service_with_registry(tmp_path, [
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
        _snapshot(3, [], []),
    ], protection=_Protection(95.0, None))
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    upgraded = service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)   # full close joins the partial
    assert upgraded.cause_command_id == "p-1"
    assert upgraded.goal_quantity is None
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    receipt = service.rescan()                                   # generation 2: remainder → reduce, never re-protect
    assert dispatch.calls[-1] == ("reduce", 1, "SELL", 6.0, "p-1-liquidation-reduce-1")
    assert not any(c[0] == "place_exit_oca" for c in dispatch.calls)
    dispatch.orders["p-1-liquidation-reduce-1"] = [_row("Filled", "r2")]
    receipt = service.rescan()                                   # generation 3
    assert receipt.state == "CLOSED"
    assert protection.calls[-1] == ("close_after_full", "p-1")


def test_partial_against_existing_owner_raises_exit_in_progress(tmp_path):
    service, _d, _b, _p, _r, _c = _service_with_registry(tmp_path, [_snapshot(1, [_position()], [])])
    service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    with pytest.raises(ExitInProgress):
        service.start(ACCOUNT, "p-2", DEADLINE, scope="conid", conid=1, quantity=3.0)


def test_full_close_during_active_flatten_joins_flatten_and_leaves_superseded_owner_alone(tmp_path):
    remaining = _position_with_price(6.0, 100.0)
    service, dispatch, _b, _p, registry, _c = _service_with_registry(tmp_path, [
        _snapshot(1, [_position_with_price(10.0)], []),
        _snapshot(2, [remaining], []),
    ])
    service.start(ACCOUNT, "p-1", DEADLINE, scope="conid", conid=1, quantity=4.0)
    service.start(ACCOUNT, "flat-1", DEADLINE)
    joined = service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    assert joined.cause_command_id == "flat-1"
    assert service.receipt_for("p-1").state == "SUPERSEDED"
    assert service.receipt_for("p-1").goal_quantity == 4.0           # not upgraded, not revived
    assert registry.get("p-1").state == "SUPERSEDED"


def test_claimed_scoped_root_is_released_when_closed(tmp_path):
    service, dispatch, _b, _p, registry, _c = _service_with_registry(tmp_path, [
        _snapshot(1, [_position()], []), _snapshot(2, [], []),
    ])
    service.start(ACCOUNT, "exit-1", DEADLINE, scope="conid", conid=1)
    dispatch.orders["exit-1-liquidation-reduce-1"] = [_row("Filled", "r")]
    assert service.rescan().state == "CLOSED"
    assert registry.get("exit-1").state == "RELEASED"
    assert registry.owner_for(ACCOUNT, 1) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_liquidation_service.py -q --timeout=30 -k "joins or upgrades_goal or exit_in_progress or leaves_superseded or released"`
Expected: FAIL — two roots are created and two reduces sent; `ExitInProgress` not raised.

- [ ] **Step 3: Implement**

In `start`, after the partial-quantity resolution and before `current = self._runs.get(cause_command_id)`:

```python
        if scope == "conid" and self._registry is not None:
            claim = self._registry.claim_scoped(
                account_id=account_id, conid=int(conid), root_id=cause_command_id,
                goal_quantity=quantity, now=self._now(),
            )
            if claim.outcome == "JOINED_FLATTEN" or claim.outcome == "JOINED":
                owner = self._runs.get(claim.root_id)
                if owner is None:
                    raise RuntimeError(f"exit owner {claim.root_id} has no liquidation run")
                return owner
            if claim.outcome == "UPGRADED":
                return self.upgrade_to_zero(claim.root_id)
            # CLAIMED: fall through and create the root under cause_command_id.
```

`ExitInProgress` from `claim_scoped` propagates unchanged. Why `JOINED` returns the owner's current receipt without advancing it: the caller polls that root via `receipt_for`, and `rescan` advances it on the next promoted generation; advancing here would send an order on a snapshot the owner has already acted on.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_liquidation_service.py tests/test_exit_owner.py -q --timeout=30`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/liquidation_service.py tests/test_liquidation_service.py
git commit -m "feat: scoped closes claim through the exit owner registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Protective saga — `CLOSE_OWNED`, hand-over, release, re-protect groups

While a close owns protection, a cancel of this saga's legs is expected and is not `MISSING_PROTECTION`. Any protection loss **outside** a hand-over still is an incident. After a partial close, the saga goes back to `PROTECTED` for the remaining quantity and watches the re-protect groups. After a full close it is `CLOSED`.

**Files:**
- Modify: `trader/automation/protective_order_saga.py`
- Test: `tests/automation/test_protective_order_saga.py`

**Interfaces:**
- Produces (the saga implements `ProtectionOwnershipPort` from Task 5):

```python
PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 34
def apply_protective_order_saga_migration(migrator) -> bool    # now applies 30 and 34
SAGA_STATES |= {"CLOSE_OWNED"}
SagaState: + close_root_id: Optional[str] = None, + reprotect_group_ids: tuple[str, ...] = ()
ProtectiveOrderSagaStore.load_by_group(order_group_id)          # also resolves re-protect groups
ProtectiveOrderSagaStore.load_open_by_conid(account_id, conid) -> list[SagaState]
ProtectiveOrderSagaStore.load_open_by_account(account_id) -> list[SagaState]
ProtectiveOrderSagaStore.load_by_close_root(close_root_id) -> list[SagaState]
ProtectiveOrderSaga.handover(*, account_id, conid, close_root_id, now) -> HandoverInfo
ProtectiveOrderSaga.handover_account(*, account_id, close_root_id, now) -> None
ProtectiveOrderSaga.release_after_partial(*, close_root_id, remaining_quantity, has_target, now) -> None
ProtectiveOrderSaga.close_after_full(*, close_root_id, now) -> None
```

- Migration 34: `ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS account_id VARCHAR`, `... conid INTEGER`, a backfill `UPDATE automated_order_sagas SET account_id = json_extract_string(payload, '$.account_id'), conid = CAST(json_extract(payload, '$.conid') AS INTEGER) WHERE account_id IS NULL`, and `CREATE TABLE IF NOT EXISTS automated_order_saga_groups (order_group_id VARCHAR PRIMARY KEY, command_id VARCHAR NOT NULL)`.
- `HandoverInfo` prices come from `plan_json`: the `stop` leg's `stop_price` and the `take_profit` leg's `limit_price` (strings → float), `None` when absent.
- Open saga = state not in `_TERMINAL_SAGA` and not `CLOSE_OWNED`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/automation/test_protective_order_saga.py`:

```python
def _protected(tmp_path, **kw):
    saga, intent, state, breaker, liquidation, dispatch = _started(tmp_path, **kw)
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2), ("take_profit", 3)):
        saga.on_broker_event(_event(og, leg=leg, status="Submitted", order_id=oid))
    state = saga.on_broker_event(_event(og, leg="entry", status="Filled", filled=10.0, total=10.0))
    assert state.state == "PROTECTED"
    return saga, intent, state, breaker, liquidation


def test_migration_34_adds_ownership_columns_and_groups_table(tmp_path):
    saga, *_rest, db = _build_saga(tmp_path)
    cols = {r[0] for r in db.execute("DESCRIBE automated_order_sagas", fetch="all")}
    assert {"account_id", "conid"} <= cols
    assert db.execute("SELECT count(*) FROM automated_order_saga_groups", fetch="one")[0] == 0


def test_handover_moves_open_saga_to_close_owned_and_returns_prices(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    info = saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", now=NOW)
    owned = saga.resume(intent.command_id)
    assert owned.state == "CLOSE_OWNED"
    assert owned.close_root_id == "close-1"
    assert info.stop_price == float(intent.stop_policy.stop_price)
    assert info.target_price == float(intent.target_policy.target_price)


def test_expected_stop_cancel_under_close_owned_is_not_an_incident(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", now=NOW)
    og = state.order_group_id
    state = saga.on_broker_event(_event(og, leg="stop", status="Cancelled", order_id=2))
    state = saga.on_broker_event(_event(og, leg="take_profit", status="Cancelled", order_id=3))
    assert state.state == "CLOSE_OWNED"
    assert state.stop_rejected is False
    assert breaker.signals == []
    assert liquidation.starts == []


def test_unexpected_stop_cancel_without_handover_still_liquidates(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    og = state.order_group_id
    state = saga.on_broker_event(_event(og, leg="stop", status="Cancelled", order_id=2))
    assert state.state == "SAFETY_FAILED"
    assert any(s.kind == "PROTECTIVE_ORDER_FAILURE" for s in breaker.signals)
    assert len(liquidation.starts) == 1


def test_handover_account_owns_every_open_saga_on_the_account(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover_account(account_id=ACCOUNT, close_root_id="flat-1", now=NOW)
    owned = saga.resume(intent.command_id)
    assert (owned.state, owned.close_root_id) == ("CLOSE_OWNED", "flat-1")
    state = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert breaker.signals == [] and liquidation.starts == []


def test_release_after_partial_returns_to_protected_for_remaining_and_watches_reprotect_groups(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", now=NOW)
    saga.release_after_partial(close_root_id="close-1", remaining_quantity=6.0, has_target=True, now=NOW)
    released = saga.resume(intent.command_id)
    assert released.state == "PROTECTED"
    assert released.protection_quantity == Decimal("6")
    assert released.close_root_id is None
    assert released.reprotect_group_ids == ("close-1-reprotect-stop", "close-1-reprotect-target")
    # A fill on the re-protect stop reaches this saga and closes it.
    state = saga.on_broker_event(_event("close-1-reprotect-stop", leg="stop", status="Filled", filled=6.0, total=6.0, order_id=9))
    assert state.command_id == intent.command_id
    assert state.state in ("EXITING", "CLOSED")
    assert breaker.signals == []


def test_close_after_full_closes_saga_without_error(tmp_path):
    saga, intent, state, breaker, liquidation = _protected(tmp_path)
    saga.handover(account_id=ACCOUNT, conid=CONID, close_root_id="close-1", now=NOW)
    saga.close_after_full(close_root_id="close-1", now=NOW)
    closed = saga.resume(intent.command_id)
    assert (closed.state, closed.error_code) == ("CLOSED", None)
    # Late broker events for a closed saga are ignored.
    state = saga.on_broker_event(_event(state.order_group_id, leg="stop", status="Cancelled", order_id=2))
    assert state.state == "CLOSED" and breaker.signals == []


def test_handover_with_no_open_saga_returns_empty_prices(tmp_path):
    saga, *_rest = _build_saga(tmp_path)
    info = saga.handover(account_id=ACCOUNT, conid=999, close_root_id="close-x", now=NOW)
    assert (info.stop_price, info.target_price) == (None, None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/automation/test_protective_order_saga.py -q --timeout=30 -k "migration_34 or handover or close_owned or release_after or close_after"`
Expected: FAIL — `AttributeError: 'ProtectiveOrderSaga' object has no attribute 'handover'`; `DESCRIBE` lacks `account_id`.

- [ ] **Step 3: Implement**

Constants and migration:

```python
PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION = 34

SAGA_STATES = frozenset({
    "VALIDATED", "SUBMITTING", "ENTRY_WORKING", "PARTIALLY_FILLED", "PROTECTED",
    "EXITING", "CLOSED", "OUTCOME_UNKNOWN", "SAFETY_FAILED", "CLOSE_OWNED",
})
_OPEN_EXCLUDED = _TERMINAL_SAGA | {"CLOSE_OWNED"}


def apply_protective_order_saga_migration(migrator: SchemaMigrator) -> bool:
    """Journal migrations 30 (saga rows) and 34 (ownership columns, re-protect groups)."""
    applied = migrator.apply(PROTECTIVE_ORDER_SAGA_MIGRATION_VERSION, PROTECTIVE_ORDER_SAGA_MIGRATION_NAME, (
        ...existing three statements unchanged...
    ))
    migrator.apply(PROTECTIVE_SAGA_OWNERSHIP_MIGRATION_VERSION, "sp1_saga_ownership", (
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS account_id VARCHAR",
        "ALTER TABLE automated_order_sagas ADD COLUMN IF NOT EXISTS conid INTEGER",
        """UPDATE automated_order_sagas
           SET account_id = json_extract_string(payload, '$.account_id'),
               conid = CAST(json_extract(payload, '$.conid') AS INTEGER)
           WHERE account_id IS NULL""",
        """CREATE TABLE IF NOT EXISTS automated_order_saga_groups (
            order_group_id VARCHAR PRIMARY KEY, command_id VARCHAR NOT NULL
        )""",
    ))
    return applied
```

`SagaState`: add the two fields, serialise them in `to_payload` (`"close_root_id"`, `"reprotect_group_ids": list(...)`) and read them in `from_payload` (`payload.get("close_root_id")`, `tuple(payload.get("reprotect_group_ids") or ())`).

Store:

```python
    def load_by_group(self, order_group_id: str) -> Optional[SagaState]:
        row = self._db.execute(
            "SELECT payload FROM automated_order_sagas WHERE order_group_id = ?", [order_group_id], fetch="one")
        if row is None:
            row = self._db.execute(
                "SELECT s.payload FROM automated_order_saga_groups g "
                "JOIN automated_order_sagas s ON s.command_id = g.command_id WHERE g.order_group_id = ?",
                [order_group_id], fetch="one")
        if row is None:
            return None
        return SagaState.from_payload(json.loads(row[0]))

    def _load_many(self, where: str, params: list) -> list[SagaState]:
        rows = self._db.execute(f"SELECT payload FROM automated_order_sagas WHERE {where}", params, fetch="all")
        return [SagaState.from_payload(json.loads(r[0])) for r in rows]

    def load_open_by_conid(self, account_id: str, conid: int) -> list[SagaState]:
        markers = ", ".join("?" for _ in _OPEN_EXCLUDED)
        return self._load_many(f"account_id = ? AND conid = ? AND state NOT IN ({markers})",
                               [account_id, int(conid), *sorted(_OPEN_EXCLUDED)])

    def load_open_by_account(self, account_id: str) -> list[SagaState]:
        markers = ", ".join("?" for _ in _OPEN_EXCLUDED)
        return self._load_many(f"account_id = ? AND state NOT IN ({markers})", [account_id, *sorted(_OPEN_EXCLUDED)])

    def load_by_close_root(self, close_root_id: str) -> list[SagaState]:
        return self._load_many("state = 'CLOSE_OWNED' AND json_extract_string(payload, '$.close_root_id') = ?",
                               [close_root_id])

    def save_in_tx(self, conn, state: SagaState, now: dt.datetime) -> None:
        payload = json.dumps(state.to_payload(), sort_keys=True, default=str)
        conn.execute("DELETE FROM automated_order_sagas WHERE command_id = ?", [state.command_id])
        conn.execute(
            "INSERT INTO automated_order_sagas "
            "(command_id, order_group_id, state, payload, updated_at, account_id, conid) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [state.command_id, state.order_group_id, state.state, payload, now, state.account_id, int(state.conid)],
        )
        for group in state.reprotect_group_ids:
            conn.execute("INSERT OR REPLACE INTO automated_order_saga_groups VALUES (?, ?)", [group, state.command_id])
```

Saga methods (ownership port):

```python
    # -- close ownership (ProtectionOwnershipPort) ---------------------------

    @staticmethod
    def _prices_from_plan(state: SagaState) -> "HandoverInfo":
        from trader.trading.liquidation_service import HandoverInfo
        legs = (state.plan_json or {}).get("legs", [])
        stop = next((l for l in legs if l.get("role") == "stop"), None)
        target = next((l for l in legs if l.get("role") == "take_profit"), None)
        return HandoverInfo(
            stop_price=None if stop is None or stop.get("stop_price") is None else float(stop["stop_price"]),
            target_price=None if target is None or target.get("limit_price") is None else float(target["limit_price"]),
        )

    def _own(self, states: list[SagaState], close_root_id: str, now: dt.datetime) -> None:
        for state in states:
            owned = replace(state, state="CLOSE_OWNED", close_root_id=close_root_id, revision=state.revision + 1)
            self._persist(owned, now, from_state=state.state)

    def handover(self, *, account_id: str, conid: int, close_root_id: str, now: dt.datetime):
        from trader.trading.liquidation_service import HandoverInfo
        states = self._store.load_open_by_conid(account_id, conid)
        self._own(states, close_root_id, _as_utc(now))
        return self._prices_from_plan(states[0]) if states else HandoverInfo(None, None)

    def handover_account(self, *, account_id: str, close_root_id: str, now: dt.datetime) -> None:
        self._own(self._store.load_open_by_account(account_id), close_root_id, _as_utc(now))

    def release_after_partial(self, *, close_root_id: str, remaining_quantity: float,
                              has_target: bool, now: dt.datetime) -> None:
        from trader.trading.order_correlation import reprotect_ref
        groups = (reprotect_ref(close_root_id, "stop"),) + ((reprotect_ref(close_root_id, "target"),) if has_target else ())
        for state in self._store.load_by_close_root(close_root_id):
            remaining = _dec(remaining_quantity)
            released = replace(
                state, state="PROTECTED", close_root_id=None, reprotect_group_ids=state.reprotect_group_ids + groups,
                filled_quantity=remaining, protection_quantity=remaining, protection_working=True,
                stop_working=True, target_working=has_target, stop_rejected=False, target_rejected=False,
                stop_filled=False, target_filled=False, revision=state.revision + 1,
            )
            self._persist(released, _as_utc(now), from_state="CLOSE_OWNED")

    def close_after_full(self, *, close_root_id: str, now: dt.datetime) -> None:
        for state in self._store.load_by_close_root(close_root_id):
            closed = replace(state, state="CLOSED", error_code=None, close_root_id=None,
                             stop_working=False, target_working=False, revision=state.revision + 1)
            self._persist(closed, _as_utc(now), from_state="CLOSE_OWNED")
```

Event handling while owned — in `on_broker_event`, after the `_TERMINAL_SAGA` early return:

```python
        if state.state == "CLOSE_OWNED":
            # Expected cancels and exit fills while a close owns protection: bookkeeping only.
            owned = self._apply_owned_event(state, event)
            owned = replace(owned, seen_event_ids=state.seen_event_ids + (event.event_id,), revision=state.revision + 1)
            self._persist(owned, now, from_state=state.state, event_id=event.event_id)
            return owned
```

and the helper:

```python
    def _apply_owned_event(self, state: SagaState, event: BrokerOrderEvent) -> SagaState:
        status = event.status
        working = status in _WORKING_STATUSES
        if event.leg == "entry":
            return replace(state, entry_working=working, entry_cancelled=status in _CANCELLED_STATUSES or state.entry_cancelled)
        if event.leg == "stop":
            return replace(state, stop_working=working, stop_filled=status in _FILLED_STATUSES or state.stop_filled)
        if event.leg == "take_profit":
            return replace(state, target_working=working, target_filled=status in _FILLED_STATUSES or state.target_filled)
        return state
```

`_persist` must accept `"CLOSE_OWNED"` (it checks `SAGA_STATES`, which now includes it). Nothing else in `_apply_event` changes: the unexpected-cancel path (`stop_rejected=True` → `SAFETY_FAILED`) is reached only when the saga is **not** owned, which is what `test_unexpected_stop_cancel_without_handover_still_liquidates` pins.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/automation/test_protective_order_saga.py -q --timeout=30`
Expected: all PASS, including `test_migration_30_creates_automated_order_sagas_table` (migration 30 still applies first).

- [ ] **Step 5: Commit**

```bash
git add trader/automation/protective_order_saga.py tests/automation/test_protective_order_saga.py
git commit -m "feat: protective saga hands protection over to a close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Runtime primitives — `reduce_partial`, `place_exit_oca`, `find_orders`

Two new reduce-only order primitives on the one IB boundary, plus the `_LiquidationDispatch` adapter methods the service calls. `place_exit_oca` places a stop and an optional target for an existing position as one OCA group, both `transmit=True`, no entry parent, `ocaType=2` (reduce the sibling on a partial fill, with block).

**Files:**
- Modify: `trader/trading/trading_runtime.py` (`Trader.place_exit_oca`, `Trader._place_single_order`, `TradingRuntimeOrderDispatch.reduce_partial`, `.place_exit_oca`, `._run_on_trader_loop`)
- Modify: `trader/trading/command_stack.py:206-216` (`_LiquidationDispatch`)
- Test: `tests/test_order_dispatch_ports.py`, `tests/test_command_stack_liquidation_dispatch.py` (create)

**Interfaces:**
- Produces on `Trader`:

```python
async def _place_single_order(self, contract: Contract, order: Order) -> Optional[Trade]
    # the body of the nested _place_and_wait in place_expressive_order, lifted to a method; the nested
    # function becomes `return await self._place_single_order(c, o)`
async def place_exit_oca(self, contract: Contract, action: str, quantity: float, *, stop_price: float,
                         target_price: Optional[float], oca_group: str, stop_ref: str, target_ref: str,
                         legs: tuple[str, ...]) -> SuccessFail
```

- Produces on `TradingRuntimeOrderDispatch`:

```python
def _run_on_trader_loop(self, coro) -> Any            # run_coroutine_threadsafe(coro, self._trader._main_loop).result(timeout)
def reduce_partial(self, position, side: str, quantity: float, order_ref: str)
def place_exit_oca(self, position, *, quantity: float, stop_price: float, target_price: Optional[float],
                   command_id: str, legs: tuple[str, ...])
```

- Produces on `_LiquidationDispatch` (command_stack): `reduce_partial`, `place_exit_oca`, `find_orders(account_id, command_id)` → `self._dispatch.find_by_order_ref(account_id, encode_order_ref(command_id))`. Order refs: stop `encode_order_ref(reprotect_ref(command_id, "stop"))`, target `encode_order_ref(reprotect_ref(command_id, "target"))`, OCA group `f"{command_id}-reprotect"`.
- Rules in `reduce_partial`: `side` must be the reducing side; `0 < quantity < |broker_quantity|`; `quantity` whole; otherwise `ValueError`. MARKET DAY, `exit_type NONE`, like `reduce_position`.
- Rules in `Trader.place_exit_oca`: `action` must be the reducing side for the position the caller passes (the dispatch derives it from `position.quantity`); `quantity > 0`; a stop on the wrong side of `stop_price` vs `target_price` (`target <= stop` for a SELL exit, `target >= stop` for a BUY exit) is refused with `SuccessFail.fail`. Legs are placed stop first, then target; if the target fails after the stop was accepted, the stop is **kept** (protection stays) and the result is `SuccessFail.fail(error='Exit OCA: target rejected; stop kept')` — the close service then escalates (Task 6), and the stop is one of the "identified working orders" the full close cancels.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_order_dispatch_ports.py`:

```python
import asyncio

from trader.trading.order_correlation import reprotect_ref


def _position(quantity=10.0):
    return SimpleNamespace(conid=265598, symbol="AAPL", sec_type="STK", exchange="SMART",
                           currency="USD", quantity=quantity, market_price=100.0)


class _RecordingTrader:
    def __init__(self):
        self.calls = []
        self._main_loop = object()          # never used: _run_on_trader_loop is patched to run inline

    async def place_expressive_order(self, contract, action, quantity, spec, algo_name="proposal"):
        self.calls.append(("expressive", contract.conId, action, quantity, spec, algo_name))
        return SuccessFail.success([])

    async def place_exit_oca(self, contract, action, quantity, **kw):
        self.calls.append(("exit_oca", contract.conId, action, quantity, kw))
        return SuccessFail.success([])


def _inline_dispatch():
    from trader.common.helpers import SuccessFail  # noqa: F401  (import path used by trading_runtime)
    trader = _RecordingTrader()
    dispatch = TradingRuntimeOrderDispatch(trader)
    dispatch._run_on_trader_loop = lambda coro: asyncio.run(coro)
    return dispatch, trader


def test_reduce_partial_sends_market_day_reduce_for_a_strict_subset():
    dispatch, trader = _inline_dispatch()
    dispatch.reduce_partial(_position(10.0), "SELL", 4.0, "mmr:p-1-liquidation-reduce-265598")
    kind, conid, action, qty, spec, ref = trader.calls[0]
    assert (kind, conid, action, qty) == ("expressive", 265598, "SELL", 4.0)
    assert spec == {"order_type": "MARKET", "exit_type": "NONE", "tif": "DAY", "outside_rth": False}
    assert ref == "mmr:p-1-liquidation-reduce-265598"


@pytest.mark.parametrize("quantity,side", [(10.0, "SELL"), (0.0, "SELL"), (11.0, "SELL"), (4.5, "SELL"), (4.0, "BUY")])
def test_reduce_partial_refuses_full_zero_oversized_fractional_or_wrong_side(quantity, side):
    dispatch, trader = _inline_dispatch()
    with pytest.raises(ValueError):
        dispatch.reduce_partial(_position(10.0), side, quantity, "mmr:x")
    assert trader.calls == []


def test_reduce_partial_on_a_short_buys():
    dispatch, trader = _inline_dispatch()
    dispatch.reduce_partial(_position(-10.0), "BUY", 3.0, "mmr:x")
    assert trader.calls[0][2:4] == ("BUY", 3.0)


def test_place_exit_oca_derives_side_refs_and_group_from_the_position_and_root():
    dispatch, trader = _inline_dispatch()
    dispatch.place_exit_oca(_position(6.0), quantity=6.0, stop_price=95.0, target_price=120.0,
                            command_id="p-1", legs=("stop", "target"))
    kind, conid, action, qty, kw = trader.calls[0]
    assert (kind, conid, action, qty) == ("exit_oca", 265598, "SELL", 6.0)
    assert kw["stop_price"] == 95.0 and kw["target_price"] == 120.0
    assert kw["oca_group"] == "p-1-reprotect"
    assert kw["stop_ref"] == encode_order_ref(reprotect_ref("p-1", "stop"))
    assert kw["target_ref"] == encode_order_ref(reprotect_ref("p-1", "target"))
    assert kw["legs"] == ("stop", "target")


def test_place_exit_oca_raises_when_the_broker_rejects():
    dispatch, trader = _inline_dispatch()
    async def failing(*a, **k):
        return SuccessFail.fail(error="Exit OCA: stop rejected")
    trader.place_exit_oca = failing
    with pytest.raises(RuntimeError):
        dispatch.place_exit_oca(_position(6.0), quantity=6.0, stop_price=95.0, target_price=None,
                                command_id="p-1", legs=("stop",))
```

Check the `SuccessFail` import path at the top of `trading_runtime.py` (`grep -n "SuccessFail" trader/trading/trading_runtime.py | head -3`) and import it from there in the test.

New file `tests/test_trader_place_exit_oca.py` for the `Trader` method, driven through a fake executioner:

```python
import asyncio
from types import SimpleNamespace

import pytest
from reactivex import Observable

from trader.trading.trading_runtime import Trader


class _FakeExecutioner:
    """Acks every order immediately with a Trade-like object; records them in order."""
    def __init__(self, reject_targets=False):
        self.placed = []
        self.reject_targets = reject_targets

    async def subscribe_place_order_direct(self, contract, order):
        self.placed.append(order)
        rejected = self.reject_targets and order.orderType == "LMT"
        def subscribe(observer):
            if rejected:
                observer.on_error(RuntimeError("rejected"))
            else:
                observer.on_next(SimpleNamespace(order=order))
            observer.on_completed()
        return Observable(subscribe)


def _trader(executioner):
    trader = Trader.__new__(Trader)
    trader.executioner = executioner
    trader.ib_account = "DU123"
    trader.client = SimpleNamespace(ib=SimpleNamespace(cancelOrder=lambda o: None))
    return trader


def _contract():
    from ib_async import Contract
    return Contract(conId=265598, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")


def test_place_exit_oca_places_stop_then_target_in_one_group_with_reduce_oca_type():
    ex = _FakeExecutioner()
    result = asyncio.run(_trader(ex).place_exit_oca(
        _contract(), "SELL", 6.0, stop_price=95.0, target_price=120.0, oca_group="p-1-reprotect",
        stop_ref="mmr:p-1-reprotect-stop", target_ref="mmr:p-1-reprotect-target", legs=("stop", "target")))
    assert result.is_success()
    stop, target = ex.placed
    assert (stop.orderType, stop.action, stop.totalQuantity, stop.auxPrice) == ("STP", "SELL", 6.0, 95.0)
    assert (target.orderType, target.action, target.totalQuantity, target.lmtPrice) == ("LMT", "SELL", 6.0, 120.0)
    for order in (stop, target):
        assert order.ocaGroup == "p-1-reprotect" and order.ocaType == 2
        assert order.transmit is True and order.parentId == 0 and order.account == "DU123"
    assert (stop.orderRef, target.orderRef) == ("mmr:p-1-reprotect-stop", "mmr:p-1-reprotect-target")


def test_place_exit_oca_places_only_requested_legs():
    ex = _FakeExecutioner()
    asyncio.run(_trader(ex).place_exit_oca(
        _contract(), "SELL", 6.0, stop_price=95.0, target_price=120.0, oca_group="g",
        stop_ref="mmr:s", target_ref="mmr:t", legs=("target",)))
    assert [o.orderType for o in ex.placed] == ["LMT"]


def test_place_exit_oca_keeps_stop_when_target_is_rejected():
    ex = _FakeExecutioner(reject_targets=True)
    result = asyncio.run(_trader(ex).place_exit_oca(
        _contract(), "SELL", 6.0, stop_price=95.0, target_price=120.0, oca_group="g",
        stop_ref="mmr:s", target_ref="mmr:t", legs=("stop", "target")))
    assert not result.is_success()
    assert "stop kept" in str(result.error)
    assert [o.orderType for o in ex.placed] == ["STP", "LMT"]


@pytest.mark.parametrize("action,stop,target", [("SELL", 95.0, 90.0), ("BUY", 105.0, 110.0)])
def test_place_exit_oca_refuses_target_on_the_wrong_side_of_the_stop(action, stop, target):
    ex = _FakeExecutioner()
    result = asyncio.run(_trader(ex).place_exit_oca(
        _contract(), action, 6.0, stop_price=stop, target_price=target, oca_group="g",
        stop_ref="mmr:s", target_ref="mmr:t", legs=("stop", "target")))
    assert not result.is_success() and ex.placed == []
```

`Trader.__new__` skips the singleton `__init__` (which needs IB config); only the attributes `place_exit_oca` reads are set. If `Trader` uses a metaclass that refuses this, build it the way `tests/test_trading_runtime.py` builds its instance — copy that helper.

New file `tests/test_command_stack_liquidation_dispatch.py`:

```python
from types import SimpleNamespace

from trader.trading.command_stack import _LiquidationDispatch


class _Inner:
    def __init__(self):
        self.calls = []
    def cancel(self, entity, ref): self.calls.append(("cancel", entity, ref))
    def reduce_position(self, position, side, qty, ref): self.calls.append(("reduce", position.conid, side, qty, ref))
    def reduce_partial(self, position, side, qty, ref): self.calls.append(("reduce_partial", position.conid, side, qty, ref))
    def place_exit_oca(self, position, **kw): self.calls.append(("place_exit_oca", position.conid, kw))
    def find_by_order_ref(self, account_id, ref): self.calls.append(("find", account_id, ref)); return ["row"]


def test_liquidation_dispatch_encodes_child_ids_as_order_refs():
    inner = _Inner()
    adapter = _LiquidationDispatch(inner)
    pos = SimpleNamespace(conid=1)
    adapter.cancel(SimpleNamespace(order_entity_id="e-1"), "root-liquidation-cancel-e-1")
    adapter.reduce(pos, "SELL", 10.0, "root-liquidation-reduce-1")
    adapter.reduce_partial(pos, "SELL", 4.0, "root-liquidation-reduce-1")
    adapter.place_exit_oca(pos, quantity=6.0, stop_price=95.0, target_price=None, command_id="root", legs=("stop",))
    assert adapter.find_orders("DU123", "root-reprotect-stop") == ["row"]
    assert inner.calls == [
        ("cancel", "e-1", "mmr:root-liquidation-cancel-e-1"),
        ("reduce", 1, "SELL", 10.0, "mmr:root-liquidation-reduce-1"),
        ("reduce_partial", 1, "SELL", 4.0, "mmr:root-liquidation-reduce-1"),
        ("place_exit_oca", 1, {"quantity": 6.0, "stop_price": 95.0, "target_price": None, "command_id": "root", "legs": ("stop",)}),
        ("find", "DU123", "mmr:root-reprotect-stop"),
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_order_dispatch_ports.py tests/test_trader_place_exit_oca.py tests/test_command_stack_liquidation_dispatch.py -q --timeout=30`
Expected: FAIL — `AttributeError` for `reduce_partial`, `place_exit_oca`, `_run_on_trader_loop`, `find_orders`.

- [ ] **Step 3: Implement**

In `Trader` (trading_runtime.py), lift the nested helper and add the OCA method next to `place_standalone_order`:

```python
    async def _place_single_order(self, contract: Contract, order: Order) -> Optional[Trade]:
        """Place one order and await the IB ack. None when the observer errored."""
        event = asyncio.Event()
        result: Dict[str, Optional[Trade]] = {'trade': None}

        def _on_next(trade: Trade):
            result['trade'] = trade
            event.set()

        obs = await self.executioner.subscribe_place_order_direct(contract, order)
        obs.subscribe(Observer(on_next=_on_next, on_error=lambda e: event.set(), on_completed=lambda: None))
        await event.wait()
        return result['trade']

    async def place_exit_oca(
        self, contract: Contract, action: str, quantity: float, *, stop_price: float,
        target_price: Optional[float], oca_group: str, stop_ref: str, target_ref: str,
        legs: tuple[str, ...],
    ) -> SuccessFail:
        """Exit-only linked stop (+ optional target) for an existing position.

        No entry parent. Both legs transmit at once in one OCA group with
        ocaType=2, so a partial fill of one leg reduces the other to the
        remaining position. A rejected target keeps the accepted stop: protection
        stays and the caller escalates.
        """
        if action not in ('BUY', 'SELL') or quantity <= 0:
            return SuccessFail.fail(error='Exit OCA: action must be BUY/SELL and quantity positive')
        if 'target' in legs and target_price is not None:
            wrong_side = target_price <= stop_price if action == 'SELL' else target_price >= stop_price
            if wrong_side:
                return SuccessFail.fail(error='Exit OCA: target is on the wrong side of the stop')
        trades: List[Trade] = []
        if 'stop' in legs:
            stop = StopOrder(action=action, totalQuantity=quantity, stopPrice=stop_price, transmit=True,
                             account=self.ib_account, orderRef=stop_ref, tif='DAY', outsideRth=False)
            stop.ocaGroup, stop.ocaType = oca_group, 2
            stop_trade = await self._place_single_order(contract, stop)
            if stop_trade is None:
                return SuccessFail.fail(error='Exit OCA: stop rejected')
            trades.append(stop_trade)
        if 'target' in legs and target_price is not None:
            target = LimitOrder(action=action, totalQuantity=quantity, lmtPrice=target_price, transmit=True,
                                account=self.ib_account, orderRef=target_ref, tif='DAY', outsideRth=False)
            target.ocaGroup, target.ocaType = oca_group, 2
            target_trade = await self._place_single_order(contract, target)
            if target_trade is None:
                return SuccessFail.fail(error='Exit OCA: target rejected; stop kept')
            trades.append(target_trade)
        return SuccessFail.success(trades)
```

Inside `place_expressive_order`, replace the body of the nested `_place_and_wait` with `return await self._place_single_order(c, o)`. Check `SuccessFail.success` is the constructor the file already uses (`grep -n "SuccessFail\." trader/trading/trading_runtime.py | head`); match it.

In `TradingRuntimeOrderDispatch`:

```python
    def _run_on_trader_loop(self, coro):
        loop = getattr(self._trader, '_main_loop', None)
        if loop is None:
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
    def _reducing_side(position) -> str:
        broker_quantity = float(position.quantity)
        if broker_quantity == 0:
            raise ValueError('no position to reduce')
        return 'SELL' if broker_quantity > 0 else 'BUY'

    def reduce_partial(self, position, side: str, quantity: float, order_ref: str):
        """Reduce-only MARKET order for a strict whole-share subset of the position."""
        held = abs(float(position.quantity))
        if side != self._reducing_side(position):
            raise ValueError('partial reduce must be on the reducing side')
        if not float(quantity).is_integer() or not 0 < float(quantity) < held:
            raise ValueError('partial reduce needs a whole quantity strictly between 0 and the position')
        result = self._run_on_trader_loop(self._trader.place_expressive_order(
            self._contract_for(position), side, float(quantity),
            {'order_type': 'MARKET', 'exit_type': 'NONE', 'tif': 'DAY', 'outside_rth': False},
            algo_name=order_ref,
        ))
        return self._unwrap(result, 'partial reduce dispatch failed')

    def place_exit_oca(self, position, *, quantity: float, stop_price: float, target_price,
                       command_id: str, legs: tuple[str, ...]):
        from trader.trading.order_correlation import encode_order_ref, reprotect_ref
        side = self._reducing_side(position)
        result = self._run_on_trader_loop(self._trader.place_exit_oca(
            self._contract_for(position), side, float(quantity),
            stop_price=float(stop_price), target_price=None if target_price is None else float(target_price),
            oca_group=f'{command_id}-reprotect',
            stop_ref=encode_order_ref(reprotect_ref(command_id, 'stop')),
            target_ref=encode_order_ref(reprotect_ref(command_id, 'target')),
            legs=tuple(legs),
        ))
        return self._unwrap(result, 'exit OCA dispatch failed')

    @staticmethod
    def _unwrap(result, default_error: str):
        if result.is_success():
            return result.obj or []
        if result.exception is not None:
            raise result.exception
        raise RuntimeError(str(result.error or default_error))
```

Refactor `reduce_position` to use `_run_on_trader_loop`, `_contract_for` and `_unwrap` (same behaviour; its exact-size rule stays). In `command_stack._LiquidationDispatch` add:

```python
    def reduce_partial(self, position, side: str, quantity: float, command_id: str) -> None:
        self._dispatch.reduce_partial(position, side, quantity, encode_order_ref(command_id))

    def place_exit_oca(self, position, *, quantity, stop_price, target_price, command_id, legs) -> None:
        self._dispatch.place_exit_oca(position, quantity=quantity, stop_price=stop_price,
                                      target_price=target_price, command_id=command_id, legs=legs)

    def find_orders(self, account_id: str, command_id: str) -> list:
        return self._dispatch.find_by_order_ref(account_id, encode_order_ref(command_id))
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_order_dispatch_ports.py tests/test_trader_place_exit_oca.py tests/test_command_stack_liquidation_dispatch.py tests/test_trading_runtime.py -q --timeout=30`
Expected: all PASS (the bracket-rollback tests in `test_trading_runtime.py` still pass after the `_place_and_wait` lift).

- [ ] **Step 5: Commit**

```bash
git add trader/trading/trading_runtime.py trader/trading/command_stack.py tests/test_order_dispatch_ports.py tests/test_trader_place_exit_oca.py tests/test_command_stack_liquidation_dispatch.py
git commit -m "feat: reduce-only partial reduce and exit-only OCA primitives

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Time exits use the scoped close; the session controller polls its exact flatten root

Removes the Task 1 `xfail`. `SessionTimeExitAdapter` asks `LiquidationService` for a conid-scoped full close instead of calling `reduce`. `SessionController._poll_flat` reads the flatten's own receipt (`receipt_for(flatten_command_id)`) and never accepts another root's `FLAT`.

**Files:**
- Modify: `trader/automation/session_controller.py` (`LiquidationPort`, `SessionTimeExitAdapter`, `_poll_flat`)
- Test: `tests/automation/test_session_controller.py`

**Interfaces:**
- Produces: `SessionTimeExitAdapter(liquidation, *, account_id: str, now: Callable[[], dt.datetime], deadline_seconds: float = 300.0)`; `request_exit(*, command_id, conid, quantity, side)` calls `liquidation.start(account_id, command_id, now() + deadline, scope="conid", conid=conid)` and swallows `ExitInProgress` (a close is already running for that conid; the time exit joined it or was refused as a partial — neither is an error for a full close, and `start` only raises `ExitInProgress` for partials). `quantity` and `side` are accepted for the port contract and not used: the size comes from the broker.
- `LiquidationPort` gains `def receipt_for(self, root_id: str) -> Any: ...`.

- [ ] **Step 1: Remove the xfail and add the polling tests**

In `tests/automation/test_session_controller.py` delete the `@pytest.mark.xfail(...)` line from `test_time_exit_requests_scoped_close_and_never_reduces_directly` (Task 1). Extend `FakeLiquidation` and add tests:

```python
class FakeLiquidation:
    def __init__(self):
        self.starts: list[tuple] = []
        self._receipts: dict[str, Any] = {}
        self.rescans = 0
        self.other_flat: Any = None       # a FLAT receipt for a different root, returned by rescan()

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        self.starts.append((account_id, cause_command_id, deadline))
        self._receipts[cause_command_id] = SimpleNamespace(
            account_id=account_id, cause_command_id=cause_command_id, state="REQUESTED",
            deadline=deadline, generation_id=None, detail="started", **kwargs)
        return self._receipts[cause_command_id]

    def rescan(self):
        self.rescans += 1
        if self.other_flat is not None:
            return self.other_flat
        return next(iter(self._receipts.values()), None)

    def receipt_for(self, root_id):
        return self._receipts.get(root_id)

    def mark_flat(self, generation_id: int = 2, root_id: str | None = None):
        root = root_id or next(iter(self._receipts))
        r = self._receipts[root]
        self._receipts[root] = SimpleNamespace(
            account_id=r.account_id, cause_command_id=root, state="FLAT",
            deadline=r.deadline, generation_id=generation_id, detail="flat")


def test_poll_flat_ignores_another_roots_flat_receipt(tmp_path):
    controller, broker, cancel, liquidation, breaker, time_exit, journal, db, clock = _build_controller(tmp_path)
    clock[0] = _utc(15, 46)
    controller.recover(clock[0])
    state = controller.run_due(clock[0])
    assert state.state == "FLATTENING"
    liquidation.other_flat = SimpleNamespace(cause_command_id="someone-else", state="FLAT", generation_id=9)
    state = controller.run_due(_utc(15, 47))
    assert state.state in ("FLATTENING", "VERIFYING_FLAT")      # not FLAT
    liquidation.mark_flat(generation_id=3)
    state = controller.run_due(_utc(15, 48))
    assert state.state == "FLAT" and state.flat_generation == 3


def test_time_exit_adapter_tolerates_exit_in_progress():
    from trader.automation.session_controller import SessionTimeExitAdapter
    from trader.trading.exit_owner import ExitInProgress

    class _Refusing:
        def start(self, *a, **k):
            raise ExitInProgress("other-root")

    adapter = SessionTimeExitAdapter(_Refusing(), account_id=ACCOUNT, now=lambda: _utc(15, 0))
    adapter.request_exit(command_id="exit-1", conid=CONID, quantity=Decimal("10"), side="BUY")   # no raise
```

Existing tests that call `FakeLiquidation.mark_flat()` without arguments keep working (the default picks the only root).

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/automation/test_session_controller.py -q --timeout=30`
Expected: `test_time_exit_requests_scoped_close_and_never_reduces_directly` FAILS (`TypeError` on the constructor), `test_poll_flat_ignores_another_roots_flat_receipt` FAILS (the controller accepts `someone-else`'s FLAT).

- [ ] **Step 3: Implement**

```python
class LiquidationPort(Protocol):
    def start(self, account_id: str, cause_command_id: str, deadline: dt.datetime, **kwargs: Any) -> Any: ...
    def rescan(self) -> Any: ...
    def receipt_for(self, root_id: str) -> Any: ...


class SessionTimeExitAdapter:
    """Time exit = conid-scoped safe close. The close cancels the protective
    stop first, sizes from the broker, and owns the position until it is flat."""

    def __init__(self, liquidation: Any, *, account_id: str, now: Callable[[], dt.datetime],
                 deadline_seconds: float = 300.0):
        self._liquidation = liquidation
        self._account_id = account_id
        self._now = now
        self._deadline_seconds = deadline_seconds

    def request_exit(self, *, command_id: str, conid: int, quantity: Decimal, side: str) -> None:
        from trader.trading.exit_owner import ExitInProgress
        deadline = self._now() + dt.timedelta(seconds=self._deadline_seconds)
        try:
            self._liquidation.start(self._account_id, command_id, deadline, scope="conid", conid=int(conid))
        except ExitInProgress:
            # Another close already owns this position; a full close has nothing to add.
            return
```

In `_poll_flat` replace the receipt lookup:

```python
        receipt = None
        try:
            self._liquidation.rescan()
            receipt_for = getattr(self._liquidation, "receipt_for", None)
            receipt = (receipt_for(state.flatten_command_id) if receipt_for is not None and state.flatten_command_id
                       else None)
        except Exception:
            receipt = None
```

The rest of `_poll_flat` is unchanged. `SessionCancelAdapter` is untouched.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/automation/test_session_controller.py -q --timeout=30`
Expected: all PASS, no xfail left.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/session_controller.py tests/automation/test_session_controller.py
git commit -m "fix: time exits close through the scoped liquidation and poll their own root

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: One-strategy SELL intents become a proven-reduction close

A SELL intent on the old path is an exit of the held long (long-only model). It must never go through `build_bracket_plan` (which adds a reverse BUY stop). Before treating it as a reduction the trader checks, on a fresh fenced snapshot: a position exists on that conid; SELL reduces it; the requested quantity (if any) is at most the position. Anything else is refused `NOT_A_REDUCTION`. The close is then a scoped `LiquidationService` root keyed by the command id; the command sits in `OUTCOME_UNKNOWN` (`CLOSE_PENDING`) until the service proves `CLOSED`/`DONE` and resolves it.

**Files:**
- Modify: `trader/automation/automated_intent_command.py`
- Test: `tests/automation/test_automated_command_boundary.py`

**Interfaces:**
- Consumes: `LiquidationService.start(... scope="conid", conid, quantity)` and `ExitInProgress` (Tasks 6, 8); `BrokerRiskSnapshot.reducible_quantity` (`trader/data/broker_state.py:279`); the service's `_resolve_root_command` already resolves `OUTCOME_UNKNOWN → RESOLVED` on `CLOSED`/`DONE` (Task 4).
- Produces: `AutomatedIntentCommandService(..., liquidation: Optional[Any] = None, broker: Optional[Any] = None, close_deadline_seconds: float = 300.0)`. With `liquidation` and `broker` set, every SELL intent takes the close path after the artifact and claim checks; BUY intents are unchanged. Refusal codes: `NOT_A_REDUCTION`, `EXIT_IN_PROGRESS`, `BROKER_SNAPSHOT_UNAVAILABLE`. Receipt for an accepted close: `OUTCOME_UNKNOWN`, `error_code="CLOSE_PENDING"`, outcome `{"close_root_id": <root the caller polls>, "liquidation_state": ...}`.

- [ ] **Step 1: Write the failing tests**

Read how `tests/automation/test_automated_command_boundary.py` builds the service at line ~246 (`ledger`, `audit`, `journal`, `controls`, `dispatch`, `verifier`, `clock`, `schedule`) and reuse that builder. Add a fake liquidation and broker:

```python
class _FakeCloseLiquidation:
    def __init__(self, raise_in_progress=False):
        self.starts = []
        self.raise_in_progress = raise_in_progress

    def start(self, account_id, cause_command_id, deadline, **kwargs):
        from trader.trading.exit_owner import ExitInProgress
        if self.raise_in_progress:
            raise ExitInProgress("other-root")
        self.starts.append((account_id, cause_command_id, deadline, kwargs))
        return SimpleNamespace(cause_command_id=cause_command_id, state="VERIFYING",
                               generation_id=1, detail="close submitted")


class _FakeBrokerSnapshot:
    def __init__(self, quantity):
        self.quantity = quantity

    def capture(self, account_id):
        row = SimpleNamespace(conid=CONID, quantity=self.quantity)
        snap = SimpleNamespace(account_id=account_id, generation_id=1, positions=(row,) if self.quantity else ())
        snap.reducible_quantity = lambda conid: sum(r.quantity for r in snap.positions if r.conid == conid)
        return snap


def _sell_body(quantity=None):
    body = _intent_body(side="SELL")            # the existing helper that builds a valid intent body
    body["requested_quantity"] = None if quantity is None else str(quantity)
    return body


def test_sell_intent_becomes_scoped_close_and_never_brackets(tmp_path):
    liquidation = _FakeCloseLiquidation()
    service, coordinator, dispatch = _build_service(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    receipt = coordinator.execute(_command("sell-1", _sell_body()))
    assert (receipt.state, receipt.error_code) == ("OUTCOME_UNKNOWN", "CLOSE_PENDING")
    assert receipt.outcome["close_root_id"] == "sell-1"
    account, root, _deadline, kwargs = liquidation.starts[0]
    assert (root, kwargs["scope"], kwargs["conid"], kwargs["quantity"]) == ("sell-1", "conid", CONID, None)
    assert dispatch.submits == []                      # no bracket, no direct dispatch


def test_sell_intent_with_smaller_quantity_is_a_partial_close(tmp_path):
    liquidation = _FakeCloseLiquidation()
    service, coordinator, _d = _build_service(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    coordinator.execute(_command("sell-2", _sell_body(4)))
    assert liquidation.starts[0][3]["quantity"] == 4.0


def test_sell_intent_equal_to_position_is_a_full_close(tmp_path):
    liquidation = _FakeCloseLiquidation()
    service, coordinator, _d = _build_service(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(10.0))
    coordinator.execute(_command("sell-3", _sell_body(10)))
    assert liquidation.starts[0][3]["quantity"] is None


@pytest.mark.parametrize("held,requested", [(0.0, None), (10.0, 11), (-5.0, None)])
def test_sell_that_is_not_a_reduction_is_refused_and_treated_as_entry_attempt(tmp_path, held, requested):
    liquidation = _FakeCloseLiquidation()
    service, coordinator, dispatch = _build_service(tmp_path, liquidation=liquidation, broker=_FakeBrokerSnapshot(held))
    receipt = coordinator.execute(_command("sell-4", _sell_body(requested)))
    assert (receipt.state, receipt.error_code) == ("REJECTED", "NOT_A_REDUCTION")
    assert liquidation.starts == [] and dispatch.submits == []


def test_sell_intent_refused_while_another_close_owns_the_conid(tmp_path):
    service, coordinator, _d = _build_service(tmp_path, liquidation=_FakeCloseLiquidation(raise_in_progress=True),
                                              broker=_FakeBrokerSnapshot(10.0))
    receipt = coordinator.execute(_command("sell-5", _sell_body(3)))
    assert (receipt.state, receipt.error_code) == ("REJECTED", "EXIT_IN_PROGRESS")


def test_buy_intent_path_is_unchanged_with_close_configured(tmp_path):
    service, coordinator, dispatch = _build_service(tmp_path, liquidation=_FakeCloseLiquidation(), broker=_FakeBrokerSnapshot(0.0))
    receipt = coordinator.execute(_command("buy-1", _intent_body(side="BUY")))
    assert receipt.state != "REJECTED" or receipt.error_code != "NOT_A_REDUCTION"
    assert len(dispatch.submits) == 1
```

`_build_service(tmp_path, **overrides)` wraps the existing construction at line ~246 and forwards `liquidation=`/`broker=`; `_command(id, body)` and `_intent_body(side=...)` are the file's existing helpers for a `CommandRequest` with `action="execute_automated_intent"` and a valid intent body (reuse them; if the names differ, use the file's). The dispatch fake must expose the list of submits (`dispatch.submits`); if the existing fake records under another name, assert on that one.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/automation/test_automated_command_boundary.py -q --timeout=30 -k "sell_intent or buy_intent_path"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'liquidation'`.

- [ ] **Step 3: Implement**

Constructor: add `liquidation: Optional[Any] = None, broker: Optional[Any] = None, close_deadline_seconds: float = 300.0` and store them as `self._liquidation`, `self._broker`, `self._close_deadline_seconds`.

In `execute`, right after the claim block succeeds (`self._journal.mutate_batch_work(...)` and before the `# Task 5 path` comment):

```python
        if intent.side == "SELL" and self._liquidation is not None and self._broker is not None:
            return self._execute_close(cmd, intent)
```

New method:

```python
    def _execute_close(self, cmd, intent) -> CommandReceipt:
        """A SELL on the long-only path is an exit. Prove it reduces, then hand it to the scoped close."""
        from trader.trading.exit_owner import ExitInProgress

        try:
            snapshot = self._broker.capture(self._account_id)
        except Exception as ex:
            self._transition(cmd, "SUBMITTING", "REJECTED", error_code="BROKER_SNAPSHOT_UNAVAILABLE")
            return self._receipt(cmd.command_id, "REJECTED", "BROKER_SNAPSHOT_UNAVAILABLE", False,
                                 outcome={"detail": str(ex)})

        held = float(snapshot.reducible_quantity(intent.conid))
        requested = None if intent.requested_quantity is None else float(intent.requested_quantity)
        if held <= 0 or (requested is not None and requested > held):
            self._transition(cmd, "SUBMITTING", "REJECTED", error_code="NOT_A_REDUCTION")
            return self._receipt(cmd.command_id, "REJECTED", "NOT_A_REDUCTION", False,
                                 outcome={"held": held, "requested": requested})

        quantity = None if requested is None or requested >= held else requested
        deadline = self._now_utc() + dt.timedelta(seconds=self._close_deadline_seconds)
        try:
            receipt = self._liquidation.start(
                self._account_id, cmd.command_id, deadline, scope="conid", conid=intent.conid, quantity=quantity,
            )
        except ExitInProgress:
            self._transition(cmd, "SUBMITTING", "REJECTED", error_code="EXIT_IN_PROGRESS")
            return self._receipt(cmd.command_id, "REJECTED", "EXIT_IN_PROGRESS", False)
        except Exception as ex:
            self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="DISPATCH_AMBIGUOUS")
            if self._schedule_reconcile is not None:
                self._schedule_reconcile(cmd.command_id)
            return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "DISPATCH_AMBIGUOUS", False,
                                 outcome={"detail": str(ex)})

        outcome = {"close_root_id": receipt.cause_command_id, "liquidation_state": receipt.state,
                   "generation_id": receipt.generation_id, "detail": receipt.detail}
        self._transition(cmd, "SUBMITTING", "OUTCOME_UNKNOWN", error_code="CLOSE_PENDING", outcome=outcome)
        return self._receipt(cmd.command_id, "OUTCOME_UNKNOWN", "CLOSE_PENDING", False, outcome=outcome)
```

Check `self._transition`'s signature accepts `outcome=`; if it does not, extend it the same way `_receipt` takes `outcome`. When the close root is a **joined** root (`receipt.cause_command_id != cmd.command_id`), this command stays `OUTCOME_UNKNOWN` until the Task-9 reconciler sees no order for it; record `close_root_id` so an operator can follow it. The `requested >= held → None` rule mirrors `partial_close_quantity`: a request for the whole position is a full close.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/automation/test_automated_command_boundary.py tests/automation/test_execution_intent.py -q --timeout=30`
Expected: all PASS. Existing BUY-path tests are unaffected because they construct the service without `liquidation`/`broker`.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/automated_intent_command.py tests/automation/test_automated_command_boundary.py
git commit -m "feat: sell intents close through the scoped liquidation after a reduction proof

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Wire it into `build_command_stack` and prove the spec scenarios end to end

Composition: the registry is built and migrated, the liquidation service gets `registry=` and (after the saga exists) `attach_protection(saga)`, the time-exit adapter gets the liquidation service, and the intent service gets `liquidation=`/`broker=`. Then an integration test with the **real** `ExitOwnerRegistry`, the **real** `ProtectiveOrderSaga` and the fake dispatch runs the two scenarios the spec names.

**Files:**
- Modify: `trader/trading/command_stack.py` (migrations block ~`:599`, `LiquidationService(` ~`:808`, saga ~`:872`, session controller ~`:894`, `_build_automated_intent_service` `:479` and its two call sites `:968`/`:1039`, `CommandStack` dataclass ~`:398`, `trader.liquidation_service = ...` ~`:1090`)
- Test: `tests/test_safe_close_integration.py` (create), `tests/automation/test_session_controller.py` (structural check)

**Interfaces:**
- Produces: `CommandStack.exit_owner_registry: Any = None`; `trader.exit_owner_registry`.
- Consumes: everything from Tasks 3–12.

- [ ] **Step 1: Write the failing tests**

Structural (append to `tests/automation/test_session_controller.py`):

```python
def test_command_stack_exposes_exit_owner_registry():
    from trader.trading.command_stack import CommandStack
    assert "exit_owner_registry" in CommandStack.__dataclass_fields__


def test_command_stack_wires_time_exit_through_liquidation_source_level():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "trader" / "trading" / "command_stack.py").read_text()
    assert "SessionTimeExitAdapter(liquidation_service" in src
    assert "SessionTimeExitAdapter(_LiquidationDispatch" not in src
    assert "liquidation_service.attach_protection(protective_order_saga)" in src
```

Integration (`tests/test_safe_close_integration.py`):

```python
"""Spec 5.1 scenarios with the real registry and the real protective saga.

Only the broker and the order dispatch are faked. Each broker generation is a
hand-built snapshot, exactly as tests/test_liquidation_service.py does it.
"""
import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from trader.data.broker_state import BrokerOrderRow, BrokerPositionRow, BrokerRiskSnapshot
from trader.data.command_ledger import CommandLedger, apply_command_ledger_migration
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.automation.protective_order_saga import (
    BrokerOrderEvent, ProtectiveOrderSaga, apply_protective_order_saga_migration,
)
from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
from trader.trading.liquidation_service import LiquidationService, apply_liquidation_migration

from tests.automation.test_protective_order_saga import (
    CONID, FakeBracketDispatch, FakeBreaker, FakeDispatchGuard, FakeSessionRisk,
    FakeCommandRequest, make_approval, make_intent,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT = "DU123"
OTHER = CONID + 1


def _position(conid, quantity, price=100.0):
    return BrokerPositionRow(
        account_id=ACCOUNT, conid=conid, symbol="SYM", sec_type="STK", exchange="SMART", currency="USD",
        quantity=quantity, average_cost=90.0, market_price=price, market_value=quantity * price,
        unrealized_pnl=0.0, realized_pnl=0.0, daily_pnl=0.0, deleted=False, revision=1, source_timestamp=NOW)


def _order(entity, conid, group, leg="stop", quantity=10.0, order_type="STP"):
    return BrokerOrderRow(
        order_entity_id=entity, account_id=ACCOUNT, conid=conid, symbol="SYM", order_group_id=group, leg=leg,
        is_external=False, action="SELL", order_type=order_type, total_quantity=quantity, filled_quantity=0,
        avg_fill_price=None, limit_price=None, stop_price=95.0, tif="DAY", status="Submitted", deleted=False,
        revision=1, source_timestamp=NOW)


def _snapshot(generation, positions=(), working=()):
    return BrokerRiskSnapshot(generation_id=generation, source_cursor=generation, promoted_at=NOW,
                              account_id=ACCOUNT, account_mode="paper", net_liquidation=100_000.0,
                              daily_pnl=0.0, positions=tuple(positions), working_orders=tuple(working))


class _Broker:
    def __init__(self): self.snapshots = []
    def push(self, snap): self.snapshots.append(snap)
    def capture(self, account_id):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


class _Dispatch:
    def __init__(self):
        self.calls, self.orders = [], {}
    def cancel(self, order, cid): self.calls.append(("cancel", order.order_entity_id, cid))
    def reduce(self, p, side, q, cid): self.calls.append(("reduce", p.conid, side, q, cid))
    def reduce_partial(self, p, side, q, cid): self.calls.append(("reduce_partial", p.conid, side, q, cid))
    def place_exit_oca(self, p, **kw): self.calls.append(("place_exit_oca", p.conid, kw))
    def find_orders(self, account_id, cid): return list(self.orders.get(cid, []))


class _Breaker:
    def __init__(self): self.calls = []
    def trip_liquidation(self, root, detail): self.calls.append((root, detail))


def _row(status): return SimpleNamespace(status=status, order_group_id="x")


def _stack(tmp_path: Path):
    db = DuckDBConnection.get_instance(str(tmp_path / "stack.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    apply_protective_order_saga_migration(migrator)
    apply_liquidation_migration(migrator)
    apply_exit_owner_migration(migrator)
    ledger = CommandLedger(journal)
    broker, dispatch, breaker = _Broker(), _Dispatch(), _Breaker()
    saga_breaker = FakeBreaker()
    liquidation = LiquidationService(broker, dispatch, breaker=breaker, now=lambda: NOW,
                                     registry=ExitOwnerRegistry(db))
    saga = ProtectiveOrderSaga(journal=journal, ledger=ledger, dispatch=FakeBracketDispatch(),
                               dispatch_guard=FakeDispatchGuard(), session_risk=FakeSessionRisk(),
                               breaker=saga_breaker, liquidation=liquidation, account_id=ACCOUNT,
                               account_mode="paper", now=lambda: NOW, db=db)
    liquidation.attach_protection(saga)
    return liquidation, saga, broker, dispatch, breaker, saga_breaker


def _protect(saga, conid, command_id):
    intent = make_intent(conid=conid, command_id=command_id, intent_id=f"i-{command_id}")
    state = saga.start(intent=intent, approval=make_approval(), request=FakeCommandRequest(intent.command_id),
                       artifact=SimpleNamespace(artifact_id=intent.artifact_id, allowlist=(str(conid),),
                                                max_gross_allocation=0.06, parameters={}),
                       session_state=SimpleNamespace(high_water_mark=100_000.0, expected_account_id=ACCOUNT, liquidity=None),
                       allocation=SimpleNamespace(max_gross_fraction=0.06))
    og = state.order_group_id
    for leg, oid in (("entry", 1), ("stop", 2)):
        saga.on_broker_event(BrokerOrderEvent(og, leg, "Submitted", 0.0, 10.0, oid, f"{og}:{leg}:sub", NOW))
    saga.on_broker_event(BrokerOrderEvent(og, "entry", "Filled", 10.0, 10.0, 1, f"{og}:entry:fill", NOW))
    assert saga.resume(command_id).state == "PROTECTED"
    return og


def test_partial_close_of_one_protected_position_leaves_the_other_untouched_and_breaker_clear(tmp_path):
    liquidation, saga, broker, dispatch, breaker, saga_breaker = _stack(tmp_path)
    og_a = _protect(saga, CONID, "entry-a")
    og_b = _protect(saga, OTHER, "entry-b")
    stop_a, stop_b = _order("sa", CONID, og_a), _order("sb", OTHER, og_b)
    a10, a6, b10 = _position(CONID, 10.0), _position(CONID, 6.0), _position(OTHER, 10.0)
    broker.push(_snapshot(1, [a10, b10], [stop_a, stop_b]))

    liquidation.start(ACCOUNT, "p-a", NOW + dt.timedelta(minutes=5), scope="conid", conid=CONID, quantity=4.0)
    assert saga.resume("entry-a").state == "CLOSE_OWNED"
    assert saga.resume("entry-b").state == "PROTECTED"
    assert [c for c in dispatch.calls if c[0] == "cancel"] == [("cancel", "sa", "p-a-liquidation-cancel-sa")]

    # Broker reports the expected cancel of A's stop: not an incident.
    saga.on_broker_event(BrokerOrderEvent(og_a, "stop", "Cancelled", 0.0, 10.0, 2, f"{og_a}:stop:cx", NOW))
    assert saga_breaker.signals == []

    dispatch.orders["p-a-liquidation-cancel-sa"] = [_row("Cancelled")]
    broker.push(_snapshot(2, [a10, b10], [stop_b]))
    liquidation.rescan()
    assert dispatch.calls[-1][:4] == ("reduce_partial", CONID, "SELL", 4.0)

    dispatch.orders["p-a-liquidation-reduce-%d" % CONID] = [_row("Filled")]
    broker.push(_snapshot(3, [a6, b10], [stop_b]))
    liquidation.rescan()
    assert dispatch.calls[-1][0] == "place_exit_oca" and dispatch.calls[-1][2]["quantity"] == 6.0

    dispatch.orders["p-a-reprotect-stop"] = [SimpleNamespace(status="Submitted", order_group_id="p-a-reprotect-stop", total_quantity=6.0)]
    broker.push(_snapshot(4, [a6, b10], [stop_b, _order("ra", CONID, "p-a-reprotect-stop", quantity=6.0)]))
    receipt = liquidation.rescan()
    assert receipt.state == "DONE"
    released = saga.resume("entry-a")
    assert released.state == "PROTECTED" and released.protection_quantity == Decimal("6")
    assert saga.resume("entry-b").state == "PROTECTED"
    assert all(c[1] != "sb" for c in dispatch.calls if c[0] == "cancel")      # B's stop never touched
    assert breaker.calls == [] and saga_breaker.signals == []


def test_kill_during_reprotect_supersedes_close_cancels_replacement_stop_and_reaches_flat(tmp_path):
    liquidation, saga, broker, dispatch, breaker, saga_breaker = _stack(tmp_path)
    og_a = _protect(saga, CONID, "entry-a")
    a10, a6 = _position(CONID, 10.0), _position(CONID, 6.0)
    broker.push(_snapshot(1, [a10], []))
    liquidation.start(ACCOUNT, "p-a", NOW + dt.timedelta(minutes=5), scope="conid", conid=CONID, quantity=4.0)
    dispatch.orders["p-a-liquidation-reduce-%d" % CONID] = [_row("Filled")]
    broker.push(_snapshot(2, [a6], []))
    liquidation.rescan()                                   # replacement stop placed
    replacement = _order("ra", CONID, "p-a-reprotect-stop", quantity=6.0)
    dispatch.orders["p-a-reprotect-stop"] = [SimpleNamespace(status="Submitted", order_group_id="p-a-reprotect-stop", total_quantity=6.0)]

    broker.push(_snapshot(3, [a6], [replacement]))
    flatten = liquidation.start(ACCOUNT, "kill-1", NOW + dt.timedelta(minutes=5))      # account flatten
    assert liquidation.receipt_for("p-a").state == "SUPERSEDED"
    assert saga.resume("entry-a").state == "CLOSE_OWNED" and saga.resume("entry-a").close_root_id == "kill-1"
    assert ("cancel", "ra", "kill-1-liquidation-cancel-ra") in dispatch.calls
    assert flatten.state == "VERIFYING"

    dispatch.orders["p-a-reprotect-stop"] = [_row("Cancelled")]
    dispatch.orders["kill-1-liquidation-cancel-ra"] = [_row("Cancelled")]
    broker.push(_snapshot(4, [a6], []))
    liquidation.rescan()
    assert dispatch.calls[-1] == ("reduce", CONID, "SELL", 6.0, "kill-1-liquidation-reduce-%d" % CONID)
    dispatch.orders["kill-1-liquidation-reduce-%d" % CONID] = [_row("Filled")]
    broker.push(_snapshot(5, [], []))
    assert liquidation.rescan().state == "FLAT"
    assert not any(c[0] == "place_exit_oca" and c[2].get("legs") == ("stop",) and c is dispatch.calls[-1] for c in dispatch.calls)
    assert saga_breaker.signals == []                      # the saga never saw an unexpected cancel
```

If `make_intent` does not accept `conid=`/`command_id=`/`intent_id=` overrides, extend its `**overrides` in `tests/automation/test_protective_order_saga.py` (it already forwards `**overrides` into `_intent_fields`). Child ids for the reduce use the conid as key (`child_command_id(root, "reduce", str(conid))`), hence `"p-a-liquidation-reduce-%d" % CONID`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_safe_close_integration.py tests/automation/test_session_controller.py -q --timeout=30 -k "integration or exit_owner_registry or source_level or partial_close_of_one or kill_during"`
Expected: structural tests FAIL (field and source markers missing). The integration tests should PASS already if Tasks 3–9 are complete — if one fails, that is a real integration bug: fix it in the owning module before wiring.

- [ ] **Step 3: Wire `build_command_stack`**

In the migrations block (after `apply_liquidation_migration(migrator)`):

```python
    from trader.trading.exit_owner import ExitOwnerRegistry, apply_exit_owner_migration
    apply_exit_owner_migration(migrator)
    exit_owner_registry = ExitOwnerRegistry(trader.journal_db)
```

`LiquidationService(` call: add `registry=exit_owner_registry,`.

After `protective_order_saga = ProtectiveOrderSaga(...)`:

```python
    liquidation_service.attach_protection(protective_order_saga)
```

Session controller: replace `time_exit=SessionTimeExitAdapter(_LiquidationDispatch(dispatch)),` with

```python
        time_exit=SessionTimeExitAdapter(liquidation_service, account_id=trader.ib_account, now=now),
```

`_build_automated_intent_service`: add a keyword parameter `liquidation: Any = None` and pass `liquidation=liquidation, broker=broker` into `AutomatedIntentCommandService(...)`; at both call sites (`:968` and `:1039`) pass `liquidation=liquidation_service`.

`CommandStack`: add `exit_owner_registry: Any = None`; pass `exit_owner_registry=exit_owner_registry` in `return CommandStack(...)`; next to `trader.liquidation_service = liquidation_service` add `trader.exit_owner_registry = exit_owner_registry`.

- [ ] **Step 4: Run the whole suite**

Run: `pytest tests/ -q --timeout=30 --ignore=tests/test_ibrx_async.py`
Expected: all PASS. Pay attention to `tests/test_production_rpc_security.py` and `tests/test_web_dashboard.py`, which build the command stack with fakes: if a fake `trader` lacks `journal_db` for the registry, extend the fake the same way `LiquidationRunStore(trader.journal_db)` already requires.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_stack.py tests/test_safe_close_integration.py tests/automation/test_session_controller.py
git commit -m "feat: wire exit ownership, scoped closes and time exits into the command stack

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Out of scope for this plan (owned by later plans)

- The old-path regression "after a loss breach a new entry is refused but a safe close is allowed" needs `session_risk` not to run for reductions — that lives in plan 3 (`ai_paper` path and `PAPER_LIMITS`). This plan already routes SELL intents away from `session_risk`, so the test will be written there against this code.
- The `/flatten` command (`liquidate_account`, `production_api.py:1876`) already calls `LiquidationService.liquidate` → `start(scope="account")`, so it claims the account owner through Task 7 with no change here. The kill line (plan 4) uses the same entry.
- Wiring a live caller for `SessionController.on_bar` (SP2).
- The real IB paper session that proves `ocaType=2` behaviour (plan 6, acceptance harness).

## Self-review notes (done while writing)

- Spec coverage, section 5.1: problem list (Tasks 1, 2), scope and goal (Tasks 4–6), protection ownership (Task 9), breaker signals (Task 4 `_trips_breaker`), one execution owner incl. goal upgrade, supersede, account-owner-first and exact-root polling (Tasks 3, 7, 8, 11), flatten order and unknown-child rule (Task 7), exit-only OCA with recovery and `DONE`/`CLOSED` meaning (Tasks 6, 10), users of the safe close (Tasks 11, 12), SELL-must-prove-reduction (Task 12). Section 5.5 step 2 binding (kill flatten is an account owner): Task 7 via `start(scope="account")`.
- Spec test list "Safe close": every bullet maps to a named test in Tasks 1, 5, 6, 7, 8, 9, 13.
- Names used across tasks: `ChildRef`, `HandoverInfo`, `ProtectionOwnershipPort`, `partial_close_quantity`, `reprotect_ref`, `ExitOwnerRegistry.claim_scoped/claim_account/release`, `LiquidationService.start/receipt_for/rescan/upgrade_to_zero/supersede/attach_protection`, `_LiquidationDispatch.reduce_partial/place_exit_oca/find_orders`, `TradingRuntimeOrderDispatch.reduce_partial/place_exit_oca/_run_on_trader_loop`, `Trader.place_exit_oca/_place_single_order`, `SessionTimeExitAdapter(liquidation, *, account_id, now, deadline_seconds)`, `AutomatedIntentCommandService(liquidation=, broker=)`. Checked for consistency task by task.
