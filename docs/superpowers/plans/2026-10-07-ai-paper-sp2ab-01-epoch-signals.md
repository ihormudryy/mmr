# AI Paper SP2 — Plan 1: Trader: controller epoch, signal record, operator initial policy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the trader the three things the `ai` controller (Plans 5 and 6) needs before it can act: a trader-granted controller epoch with a lease that fences every AI command and controller read, a durable strategy signal record with a monotonic cursor and gap reporting, and an operator (`cli`) way to publish the initial AI risk policy.

**Architecture:** A new trader module `trader/automation/controller_epoch.py` keeps one row per granted epoch in the trader journal (migration 90) and grants or renews it inside the journal's serialized write transaction. The typed RPC envelope gets `controller_epoch`, covered by the signature and handed to handlers on `RpcCaller`. `submit_ai_paper_decision` checks the epoch twice: a read-only check in the RPC handler (no ledger row, no replay receipt for a stale holder) and an atomic check inside the `VALIDATED -> SUBMITTING` claim transaction (the last transaction before any broker side effect). A new module `trader/data/strategy_signal_record.py` is written by the strategy service into `duckdb_path` (the file it already writes `trading_events` to) and read by the trader for `read_ai_signals`. `publish_ai_risk_policy` gains the `cli` principal and an `mmr ai-policy publish` command.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DomainJournal.mutate_batch_work`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, PyYAML `safe_load`, pytest. No new dependencies. No model calls, no IB calls.

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`. Binding sections: 5.1 (leadership), 6.1 (durable signal record), 6.2 and 6.2b (controller epoch, envelope field), 6.7 (initial policy). Index: `docs/superpowers/plans/2026-10-07-ai-paper-sp2ab-00-index.md` (shared names, migration slots 90–94, test commands). Code base cited by file and function name (base: master after SP1 Plans 3–6).

## Global Constraints

- **Migration:** trader journal version **90** (`sp2_ai_controller_epochs`). Plan 1 holds 90–94 and uses only 90. No legacy data: the one SP1 table this plan changes (`ai_paper_decisions`, migration 56) gets its column by editing its `CREATE` in place. No `ALTER`, no backfill.
- **Signal record tables** live in `duckdb_path` (`mmr.duckdb`), created with `CREATE TABLE IF NOT EXISTS` in `EventStore` style. They take no journal migration number.
- **Epoch codes:** `CONTROLLER_EPOCH_MISSING`, `CONTROLLER_EPOCH_STALE`, `CONTROLLER_EPOCH_HELD`, `CONTROLLER_EPOCH_UNKNOWN`.
- **Lease:** `lease_seconds` is a strict int in `10..600`. Plan 5 uses 60 s and renews every 20 s (index).
- **`holder_id`:** `^[a-z0-9][a-z0-9_.-]{0,63}$`.
- **Envelope:** `TypedRpcRequest.controller_epoch: Optional[int] = None`; a JSON integer `1..2**53` or null; always a key in `rpc_signing_bytes`; only `ai_supervisor` may send a non-null value.
- **Signal read:** `limit` strict int `1..500`; `after_cursor` strict int `>= 0`.
- **Signal retention default:** 7 days, key `strategy_signal_record_retention_days` (strict int `1..365`).
- **Allow-list rows:** `("command", "grant_ai_controller_epoch"): {"ai_supervisor"}`, `("query", "read_ai_signals"): {"ai_supervisor"}`, `("query", "get_ai_paper_decision"): {"ai_supervisor"}`, `("command", "publish_ai_risk_policy"): {"ai_supervisor", "cli"}`.
- **Strict input:** wire models use `ConfigDict(extra="forbid", strict=True)`. `True` is never an int. Domain code re-checks types with `type(x) is int`.
- **DuckDB:** only `DuckDBConnection.execute` / `execute_atomic` / `transaction` and `DomainJournal.mutate_batch_work`. No long-lived connection to `duckdb_path`.
- **YAML:** `yaml.safe_load` only.
- **Times:** DuckDB 1.4 returns `TIMESTAMPTZ` as an aware `datetime` in the process's local zone (checked in the project venv); both new modules convert with `astimezone(timezone.utc)` and refuse naive input.
- **Fail loudly:** every refusal has its own code; nothing returns an empty result on error.
- **No secrets** in logs, receipts or test output.
- Per task: targeted pytest only (`.venv/bin/python -m pytest <files> -q --timeout=60`). Full suite once, in Task 9: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`.
- Commit subjects `feat: ...` / `test: ...` / `docs: ...`, lowercase, imperative. Every commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order or deploy is authorized by this plan.

## Rulings

1. **Two epoch checks on `submit_ai_paper_decision`.** (a) The RPC handler calls `ControllerEpochs.require_current(caller.controller_epoch)` before `coordinator.execute`. A missing or stale epoch is an `RpcProblem` (`TypedRpcRemoteError` on the client), so no ledger row is written and a stale holder's resend of an existing id never sees the coordinator's replay receipt. (b) `AiPaperDecisionService._claim` checks the epoch inside the `VALIDATED -> SUBMITTING` transaction (`CommandSteps.claim`), which is the last transaction before the saga or the close touches the broker. Both `ENTER` and `CLOSE` / `PARTIAL_CLOSE` pass through it. Why not at the coordinator's `RECEIVED` insert: admission takes a broker capture and evidence work between `RECEIVED` and the claim; fencing at the claim also catches a takeover during that window. Cost if wrong: none for safety; a takeover during admission turns that one decision into a `REJECTED` row.
2. **"Current" means equal to the newest granted epoch.** An expired lease alone does not refuse; only a successor's grant makes an epoch stale. Why: fencing comes from the epoch number; refusing on expiry would only add false refusals when no successor exists. Cost if wrong: a paused holder whose lease expired can still submit until someone else takes over; there is still only one valid epoch at a time.
3. **Grant rules.** Live lease (`lease_expires_at > now`): same `holder_id` **and** `current_epoch` equal to it → renew, same epoch, new expiry; anything else (another holder, or the same holder sending `null` or another number) → `CONTROLLER_EPOCH_HELD`. No live lease (none yet, or expired) → new epoch = newest + 1 (first epoch is 1). A `current_epoch` larger than the newest granted epoch → `CONTROLLER_EPOCH_UNKNOWN` (the caller holds an epoch this trader never granted: a reset journal or a bug). Cost if wrong: a restarted controller with a fresh `holder_id` waits up to one lease (60 s) before it can act.
4. **Epoch refusal shapes.** Before admission: `RpcProblem` with the code (no ledger row). During admission (takeover between the handler check and the claim): `REJECTED` receipt with `CONTROLLER_EPOCH_STALE`, `retryable=false`, written on the ledger and the decision row. A successor that later resends that id gets the recorded `REJECTED` replay: the decision is lost, never duplicated.
5. **Controller reads need the epoch too** (spec 5.1 "reads and reconciles ... may not omit it"): `read_ai_signals` and the new `get_ai_paper_decision` refuse a missing or stale epoch with the same codes. `get_command` keeps its SP1 rights (`ACCOUNT_READERS`, which includes `ai_supervisor`) because the SP1 acceptance harness and humans use it; Plan 5 must use `get_ai_paper_decision` for its own reconciliation.
6. **Only `ai_supervisor` may carry an epoch.** `ServiceIdentity.verify_request` raises `AuthenticationError("controller_epoch is only accepted from ai_supervisor")` for any other principal, before the nonce claim (same pattern as `on_behalf_of`). `grant_ai_controller_epoch` ignores the envelope epoch; the body's `current_epoch` is what it reads.
7. **Signing context bump.** `RPC_REQUEST_CONTEXT` moves from `v2` to `v3`, because the signed bytes gain a key. All services come from one image, so they upgrade together.
8. **Grant is not a coordinator command.** A renewal every 20 s through `TradingCommandCoordinator.execute` would add a ledger and an audit row each time. The handler calls `ControllerEpochs.grant` directly, like the existing direct read handlers; the `ai_controller_epochs` table keeps one row per epoch (`granted_at`, `renewed_at`) as the audit trail.
9. **Where the signal record lives.** The trader journal (`journal_duckdb_path`) is held open by `DomainJournal` for the trader's whole life (`DomainJournal.__init__` keeps `_shared_conn`), so the strategy service cannot write it. The strategy already writes `duckdb_path` through short-lived connections (`EventStore`), and the trader opens the same file the same way. So the strategy writes the record into `duckdb_path` and the trader serves it.
10. **Cursor and gap.** Cursors come from a counter row (`strategy_signal_record_state.last_cursor`) updated in the same transaction as the insert, so a rolled-back write never leaves a hole (a DuckDB sequence does not roll back). Retention deletes a prefix of cursors and raises `retention_watermark` in the same transaction. `gap = after_cursor < retention_watermark`. `oldest_retained_cursor = MIN(cursor)` or `retention_watermark + 1` when the table is empty. An `after_cursor` above `last_cursor` is refused `SIGNAL_CURSOR_AHEAD` (the file was reset; a consumer must never wait silently for cursors it already passed).
11. **Signal identity.** `source_event_id = "sig-" + sha256(strategy_name, conid, action, signal_time ISO)[:32]`, where `signal_time` is the completed bar time (`frame.index[-1]`, naive read as UTC, the same rule the intent emitter uses). A second signal for the same strategy, conid, action and bar is the same signal: no new row. `strategy_name` is the runtime's `strategy.name`, not the strategy-authored `signal.source_name`.
12. **What is recorded.** Every strategy's `BUY` and `SELL` signal, whatever its `auto_execute` mode. `NEUTRAL` is not recorded. A non-finite `probability` is stored as `null`. The record is written before the `trading_events` row and the MessageBus publish; a failed write raises, as a failed `EventStore.append` does today.
13. **Retention.** Default 7 days, flat config key `strategy_signal_record_retention_days`, pruned on each append (cheap: signals arrive at most once per bar per strategy). Cost if wrong: an `ai` outage longer than 7 days produces a coverage gap, which is reported, never hidden.
14. **Registration.** `grant_ai_controller_epoch`, `read_ai_signals` and `get_ai_paper_decision` are registered by `register_ai_paper_authority`, like the other ai_paper methods: only when `ai_paper.enabled` built the services. Migration 90 runs on every command stack, like 54–56.
15. **Operator policy.** `AiPaperActions._publish` accepts `{ai_supervisor, cli}`. `dashboard` gets nothing new. `mmr ai-policy publish FILE --reason TEXT [--command-id ID]` reads `{limits: {...}}` with `yaml.safe_load`, checks it locally with `RiskLimits.from_json`, and uses `cli-pol-<uuid hex>` as the command id unless `--command-id` is given (to retry the same publish after a timeout). SP2a/b never publishes on startup or restart; that is Plan 5's test ("a restart neither republishes nor loosens policy").
16. **SP1 acceptance harness is a controller too.** `RpcAcceptancePort` grants its own epoch (`holder_id = "acceptance-<12 hex>"`, lease 60 s) before each `submit_ai_paper_decision` and waits (5 s steps, at most one lease) on `CONTROLLER_EPOCH_HELD`. The SP1 acceptance run therefore needs the `ai` service stopped; this is added to the runbook line in `docs/OPERATIONAL_STATE.md`.
17. **Decision rows carry the epoch.** `ai_paper_decisions.controller_epoch BIGINT` (the migration-56 `CREATE` edited in place) records the envelope epoch of the command, for audit and for `get_ai_paper_decision`.

## Cross-plan additions

Interfaces other plans must use that the index does not name:

- `TypedRpcClient.call(method, body, response_model, timeout=None, *, on_behalf_of=None, controller_epoch: Optional[int] = None)`. `RpcCaller(principal: str, on_behalf_of: Optional[str], controller_epoch: Optional[int] = None)`. `ServiceIdentity.sign_request(..., controller_epoch: Optional[int] = None)`.
- `trader.messaging.principals.CONTROLLER_PRINCIPAL = "ai_supervisor"`.
- Epoch refusal codes as `TypedRpcRemoteError.code`: `CONTROLLER_EPOCH_MISSING`, `CONTROLLER_EPOCH_STALE` (submit, `read_ai_signals`, `get_ai_paper_decision`); `CONTROLLER_EPOCH_HELD`, `CONTROLLER_EPOCH_UNKNOWN` (grant). A takeover during admission returns a `REJECTED` receipt with `error_code="CONTROLLER_EPOCH_STALE"`, `retryable=false` (Ruling 4).
- Grant body bounds: `holder_id` `^[a-z0-9][a-z0-9_.-]{0,63}$`; `current_epoch` key required (null or int `>= 1`); `lease_seconds` int `10..600`. Response: `{"epoch": int, "lease_expires_at": str}` (ISO-8601 with offset, trader clock).
- New query `get_ai_paper_decision` (`ai_supervisor` only, epoch required): body `{"decision_id": str}` (`^[A-Za-z0-9_-]{8,64}$`) → `{"decision_id": str, "command_id": str, "found": bool, "receipt": Optional[{"command_id", "correlation_id", "state", "outcome", "error_code", "retryable"}], "decision_state": Optional[str], "decision_error_code": Optional[str], "close_root_id": Optional[str], "controller_epoch": Optional[int]}`. `found=false` means the trader has no ledger row for that id.
- `read_ai_signals` refuses an `after_cursor` above the newest cursor with `SIGNAL_CURSOR_AHEAD`. In the response, `probability` is `Optional[float]` (null when the strategy gave a non-finite value); `signal_time` and `recorded_at` are ISO-8601 strings with offset; `source_event_id` matches `^sig-[0-9a-f]{32}$`.
- `mmr ai-policy publish FILE --reason TEXT [--command-id ID]` and `mmr ai-policy show` (operator setup, Plan 5/6 acceptance and runbook).

## Review Focus

1. **A stale holder resends a command id the trader already accepted.** Expect `CONTROLLER_EPOCH_STALE` as an RPC problem, never the replay receipt; the successor's resend of the same id and body replays the original receipt and no second order exists. → Task 4 `test_stale_holder_resend_is_refused_and_successor_replays`.
2. **A takeover lands between the handler check and the claim.** Expect a `REJECTED` / `CONTROLLER_EPOCH_STALE` receipt, `retryable=false`, and no dispatch. → Task 4 `test_takeover_during_admission_is_refused_at_the_claim`.
3. **The epoch is changed or removed after signing, or sent by another principal.** Expect `AUTHENTICATION_ERROR` before any handler runs. → Task 2 `test_altered_epoch_breaks_the_signature`, `test_epoch_from_another_principal_is_refused`.
4. **Retention removes signals past the consumer's cursor, a write rolls back, or the file is reset.** Expect `gap=true` only when rows were really pruned, no false gap after a rollback, and `SIGNAL_CURSOR_AHEAD` after a reset. → Task 6 `test_gap_only_after_real_pruning`, `test_rolled_back_append_leaves_no_hole`, `test_cursor_ahead_of_the_record_is_refused`.
5. **Two controllers ask for an epoch at the same time, or one restarts without its epoch.** Expect exactly one epoch, the loser `CONTROLLER_EPOCH_HELD`, and a restarted holder held until the lease ends. → Task 1 `test_concurrent_grants_give_one_epoch`, `test_same_holder_without_its_epoch_is_held_until_expiry`.

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/automation/controller_epoch.py` (new), `trader/data/schema_migrations.py` (docstring) | migration 90, `ControllerEpochs` grant and checks | 1 |
| `trader/messaging/typed_rpc.py`, `trader/messaging/principals.py`, `tests/rpc_identity_fixtures.py` | envelope field, signing, `RpcCaller`, client | 2 |
| `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/command_stack.py` | grant RPC, ACL row, composition | 3 |
| `trader/trading/command_coordinator.py`, `trader/automation/ai_paper_decision.py`, `trader/messaging/production_api.py`, `trader/trading/command_stack.py`, `trader/acceptance/ports.py`, `tests/automation/ai_paper_world.py`, `tests/sp1_fixtures.py`, `tests/test_ai_paper_rpc.py` | epoch at admission and its ripple | 4 |
| `trader/messaging/production_api.py`, `trader/messaging/principals.py` | `get_ai_paper_decision` | 5 |
| `trader/data/strategy_signal_record.py` (new), `trader/strategy/strategy_runtime.py`, `trader/config.py`, `config_defaults/trader.yaml`, `tests/test_signal_proposer.py` | signal record, writer, retention config | 6 |
| `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/command_stack.py`, `tests/test_command_stack.py`, `tests/sp1_fixtures.py` | `read_ai_signals` | 7 |
| `trader/automation/ai_paper_actions.py`, `trader/messaging/principals.py`, `trader/automation/ai_policy_file.py` (new), `trader/sdk.py`, `trader/mmr_cli.py`, `tests/test_rpc_acl.py`, `docs/CLI_REFERENCE.md` | operator initial policy | 8 |
| `AGENTS.md`, `docs/OPERATIONAL_STATE.md` | docs, full suite | 9 |

---

### Task 1: Controller epoch table and grant service

**Files:**
- Create: `trader/automation/controller_epoch.py`
- Modify: `trader/data/schema_migrations.py` (module docstring: append "SP2 Plan 1 owns 90–94 and uses **90** (`ai_controller_epochs`).")
- Test: `tests/automation/test_controller_epoch.py`

**Interfaces:**
- Consumes: `SchemaMigrator.apply(version, name, statements)`, `DomainJournal.mutate_batch_work(conn, work)`, `DomainJournal.connect()`.
- Produces:
  - `CONTROLLER_EPOCH_MIGRATION_VERSION = 90`; `apply_controller_epoch_migration(migrator) -> bool`.
  - Codes `EPOCH_MISSING`, `EPOCH_STALE`, `EPOCH_HELD`, `EPOCH_UNKNOWN`; `MIN_LEASE_SECONDS = 10`, `MAX_LEASE_SECONDS = 600`, `MAX_CONTROLLER_EPOCH = 2**53`, `HOLDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")`.
  - `class EpochRefused(Exception)` with `.code`, `.message`.
  - `@dataclass(frozen=True) class EpochGrant: epoch: int; holder_id: str; lease_expires_at: datetime; renewed: bool`.
  - `class ControllerEpochs(*, journal, now: Callable[[], datetime])`: `grant(*, holder_id: str, current_epoch: Optional[int], lease_seconds: int) -> EpochGrant`; `require_current(epoch: Optional[int]) -> None`; `require_current_in_tx(conn, epoch: Optional[int]) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_controller_epoch.py
"""SP2 Plan 1 Task 1: the trader-granted controller epoch and its lease (spec 5.1, 6.2)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from trader.automation.controller_epoch import (
    EPOCH_HELD, EPOCH_MISSING, EPOCH_STALE, EPOCH_UNKNOWN, ControllerEpochs, EpochRefused,
    apply_controller_epoch_migration,
)
from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.timezone.utc)


class Clock:
    """A settable trader clock; Plan 1's other test files import it."""

    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def epochs(tmp_path, clock):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    assert apply_controller_epoch_migration(migrator) is True
    assert apply_controller_epoch_migration(migrator) is False
    return ControllerEpochs(journal=journal, now=clock)


def refused(fn, **kwargs):
    with pytest.raises(EpochRefused) as exc:
        fn(**kwargs)
    return exc.value.code


def test_first_grant_is_epoch_one_with_a_lease(epochs):
    grant = epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (1, False)
    assert grant.lease_expires_at == NOW + dt.timedelta(seconds=60)


def test_renew_keeps_the_epoch_and_moves_the_lease(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(20)
    grant = epochs.grant(holder_id="ctl-a", current_epoch=1, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (1, True)
    assert grant.lease_expires_at == NOW + dt.timedelta(seconds=80)


def test_another_holder_is_held_while_the_lease_lives(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(59)
    assert refused(epochs.grant, holder_id="ctl-b", current_epoch=None, lease_seconds=60) == EPOCH_HELD


def test_takeover_after_expiry_gets_the_next_epoch(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(60)
    assert epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60).epoch == 2
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=1, lease_seconds=60) == EPOCH_HELD


def test_same_holder_without_its_epoch_is_held_until_expiry(epochs, clock):     # Review Focus 5, Ruling 3
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=None, lease_seconds=60) == EPOCH_HELD
    clock.advance(60)
    assert epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60).epoch == 2


def test_an_expired_holder_renewing_gets_a_new_epoch(epochs, clock):
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    clock.advance(61)
    grant = epochs.grant(holder_id="ctl-a", current_epoch=1, lease_seconds=60)
    assert (grant.epoch, grant.renewed) == (2, False)


def test_an_epoch_never_granted_is_unknown(epochs):
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=1, lease_seconds=60) == EPOCH_UNKNOWN
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    assert refused(epochs.grant, holder_id="ctl-a", current_epoch=7, lease_seconds=60) == EPOCH_UNKNOWN


def test_require_current(epochs, clock):
    assert refused(epochs.require_current, epoch=None) == EPOCH_MISSING
    assert refused(epochs.require_current, epoch=1) == EPOCH_STALE         # nothing granted yet
    epochs.grant(holder_id="ctl-a", current_epoch=None, lease_seconds=60)
    epochs.require_current(1)
    clock.advance(600)
    epochs.require_current(1)                                              # Ruling 2: expiry alone is not stale
    epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60)
    assert refused(epochs.require_current, epoch=1) == EPOCH_STALE
    epochs.require_current(2)


@pytest.mark.parametrize("holder,current,lease", [
    ("Ctl-A", None, 60), ("ctl:a", None, 60), ("ctl-a", None, 9), ("ctl-a", None, 601),
    ("ctl-a", None, True), ("ctl-a", True, 60), ("ctl-a", 0, 60)])
def test_bad_grant_input_is_a_value_error(epochs, holder, current, lease):
    with pytest.raises(ValueError):
        epochs.grant(holder_id=holder, current_epoch=current, lease_seconds=lease)


def test_concurrent_grants_give_one_epoch(epochs):                            # Review Focus 5
    barrier, results = threading.Barrier(2), {}

    def run(holder):
        barrier.wait()
        try:
            results[holder] = epochs.grant(holder_id=holder, current_epoch=None, lease_seconds=60).epoch
        except EpochRefused as ex:
            results[holder] = ex.code
    threads = [threading.Thread(target=run, args=(h,)) for h in ("ctl-a", "ctl-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(map(str, results.values())) == ["1", EPOCH_HELD]
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/automation/test_controller_epoch.py -q --timeout=60`
Expected: collection error, `ModuleNotFoundError: No module named 'trader.automation.controller_epoch'`.

- [ ] **Step 3: Implement**

```python
# trader/automation/controller_epoch.py
"""The trader-granted controller epoch and its lease (SP2 spec 5.1, amendment 6.2).

One row per granted epoch; the newest row is the current epoch. A grant runs
inside ``DomainJournal.mutate_batch_work``, the same serialized write
transaction the ai_paper decision claim uses, so a grant and a claim never
interleave. "Current" means equal to the newest epoch: only a successor's
grant makes an epoch stale, an expired lease alone does not (Plan 1 Ruling 2).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

CONTROLLER_EPOCH_MIGRATION_VERSION = 90
EPOCH_MISSING = "CONTROLLER_EPOCH_MISSING"
EPOCH_STALE = "CONTROLLER_EPOCH_STALE"
EPOCH_HELD = "CONTROLLER_EPOCH_HELD"
EPOCH_UNKNOWN = "CONTROLLER_EPOCH_UNKNOWN"
MIN_LEASE_SECONDS = 10
MAX_LEASE_SECONDS = 600
MAX_CONTROLLER_EPOCH = 2**53
HOLDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def apply_controller_epoch_migration(migrator: Any) -> bool:
    return migrator.apply(CONTROLLER_EPOCH_MIGRATION_VERSION, "sp2_ai_controller_epochs", (
        """CREATE TABLE IF NOT EXISTS ai_controller_epochs (
            epoch BIGINT PRIMARY KEY, holder_id VARCHAR NOT NULL, granted_at TIMESTAMPTZ NOT NULL,
            renewed_at TIMESTAMPTZ NOT NULL, lease_expires_at TIMESTAMPTZ NOT NULL)""",
    ))


class EpochRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EpochGrant:
    epoch: int
    holder_id: str
    lease_expires_at: dt.datetime
    renewed: bool


@dataclass(frozen=True)
class _Latest:
    epoch: int
    holder_id: str
    lease_expires_at: dt.datetime


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("controller epoch times must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def is_epoch_number(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_CONTROLLER_EPOCH


def _check_grant_input(holder_id: object, current_epoch: object, lease_seconds: object) -> None:
    if not isinstance(holder_id, str) or not HOLDER_ID.fullmatch(holder_id):
        raise ValueError("holder_id must match ^[a-z0-9][a-z0-9_.-]{0,63}$")
    if current_epoch is not None and not is_epoch_number(current_epoch):
        raise ValueError("current_epoch must be null or an integer >= 1")
    if type(lease_seconds) is not int or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(f"lease_seconds must be an integer in {MIN_LEASE_SECONDS}..{MAX_LEASE_SECONDS}")


def _latest_in_tx(conn: Any) -> Optional[_Latest]:
    row = conn.execute(
        "SELECT epoch, holder_id, lease_expires_at FROM ai_controller_epochs ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _Latest(int(row[0]), row[1], _as_utc(row[2]))


class ControllerEpochs:
    def __init__(self, *, journal: Any, now: Callable[[], dt.datetime]):
        self._journal = journal
        self._now = now

    def grant(self, *, holder_id: str, current_epoch: Optional[int], lease_seconds: int) -> EpochGrant:
        _check_grant_input(holder_id, current_epoch, lease_seconds)
        now = _as_utc(self._now())
        return self._journal.mutate_batch_work(
            self._journal.connect(),
            lambda conn, _append: self._grant_in_tx(conn, holder_id, current_epoch, lease_seconds, now))

    def _grant_in_tx(self, conn: Any, holder_id: str, current_epoch: Optional[int], lease_seconds: int,
                     now: dt.datetime) -> EpochGrant:
        latest = _latest_in_tx(conn)
        if current_epoch is not None and (latest is None or current_epoch > latest.epoch):
            raise EpochRefused(EPOCH_UNKNOWN, f"epoch {current_epoch} was never granted by this trader")
        expires = now + dt.timedelta(seconds=lease_seconds)
        if latest is not None and latest.lease_expires_at > now:
            if latest.holder_id != holder_id or current_epoch != latest.epoch:
                raise EpochRefused(EPOCH_HELD, f"epoch {latest.epoch} is leased until "
                                               f"{latest.lease_expires_at.isoformat()}")
            conn.execute("UPDATE ai_controller_epochs SET renewed_at = ?, lease_expires_at = ? WHERE epoch = ?",
                         [now, expires, latest.epoch])
            return EpochGrant(latest.epoch, holder_id, expires, renewed=True)
        epoch = 1 if latest is None else latest.epoch + 1
        conn.execute("INSERT INTO ai_controller_epochs VALUES (?, ?, ?, ?, ?)",
                     [epoch, holder_id, now, now, expires])
        return EpochGrant(epoch, holder_id, expires, renewed=False)

    def require_current(self, epoch: Optional[int]) -> None:
        """Read-only check outside a transaction (RPC handlers, Plan 1 Ruling 1a)."""
        self.require_current_in_tx(self._journal.connect(), epoch)

    def require_current_in_tx(self, conn: Any, epoch: Optional[int]) -> None:
        if epoch is None:
            raise EpochRefused(EPOCH_MISSING, "a controller epoch is required")
        latest = _latest_in_tx(conn)
        if latest is None or epoch != latest.epoch:
            current = None if latest is None else latest.epoch
            raise EpochRefused(EPOCH_STALE, f"epoch {epoch} is not the current epoch {current}")
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/bin/python -m pytest tests/automation/test_controller_epoch.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/automation/controller_epoch.py trader/data/schema_migrations.py tests/automation/test_controller_epoch.py
git commit -m "feat: add the trader-granted ai controller epoch and lease

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Signed `controller_epoch` in the typed RPC envelope

**Files:**
- Modify: `trader/messaging/typed_rpc.py` (`TypedRpcRequest`, `RPC_REQUEST_CONTEXT`, `rpc_signing_bytes`, `RpcCaller`, `ServiceIdentity.sign_request`, `ServiceIdentity.verify_request`, `TypedRpcClient.call`, module docstring note 7)
- Modify: `trader/messaging/principals.py` (add `CONTROLLER_PRINCIPAL = "ai_supervisor"`)
- Modify: `tests/rpc_identity_fixtures.py` (`ServedStack.signed(..., controller_epoch=None)`)
- Test: `tests/test_typed_rpc_controller_epoch.py`

**Interfaces:**
- Consumes: `is_epoch_number`, `MAX_CONTROLLER_EPOCH` (Task 1).
- Produces: `TypedRpcRequest.controller_epoch: Optional[int] = None`; `RpcCaller(principal, on_behalf_of, controller_epoch=None)`; `sign_request(..., controller_epoch=None)`; `TypedRpcClient.call(..., controller_epoch=None)`; `CONTROLLER_PRINCIPAL`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_typed_rpc_controller_epoch.py
"""SP2 Plan 1 Task 2: the controller epoch rides in the signed envelope (spec 5.1, 6.2b)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, make_identities
from trader.messaging.typed_rpc import (
    RPC_REQUEST_CONTEXT, AuthenticationError, RpcCaller, TypedRpcRegistry, canonical_json, decode_request,
    rpc_signing_bytes,
)

NOW = 1_800_000_000.0


def signed(ids, caller="ai_supervisor", epoch=None):
    return ids[caller].sign_request(server="trader", role="command", method="m", request_id=str(uuid.uuid4()),
                                    nonce=uuid.uuid4().hex, body={}, controller_epoch=epoch)


def test_the_key_is_always_signed():
    ids = make_identities(now=lambda: NOW)
    assert RPC_REQUEST_CONTEXT == b"mmr.typed-rpc.request.v3\x00"
    for epoch in (None, 3):
        covered = json.loads(rpc_signing_bytes(signed(ids, epoch=epoch))[len(RPC_REQUEST_CONTEXT):])
        assert "controller_epoch" in covered and covered["controller_epoch"] == epoch


def test_verify_hands_the_epoch_to_the_caller():
    ids = make_identities(now=lambda: NOW)
    caller = ids["trader"].verify_request(signed(ids, epoch=3), role="command")
    assert caller == RpcCaller("ai_supervisor", None, 3)
    assert RpcCaller("cli", None) == RpcCaller("cli", None, None)         # SP1 call sites unchanged


@pytest.mark.parametrize("altered", [4, None, 2])
def test_altered_epoch_breaks_the_signature(altered):                    # Review Focus 3
    ids = make_identities(now=lambda: NOW)
    request = signed(ids, epoch=3).model_copy(update={"controller_epoch": altered})
    with pytest.raises(AuthenticationError, match="signature mismatch"):
        ids["trader"].verify_request(request, role="command")


def test_adding_an_epoch_after_signing_breaks_the_signature():
    ids = make_identities(now=lambda: NOW)
    request = signed(ids).model_copy(update={"controller_epoch": 5})
    with pytest.raises(AuthenticationError, match="signature mismatch"):
        ids["trader"].verify_request(request, role="command")


@pytest.mark.parametrize("caller", ["cli", "dashboard", "ai_research", "strategy"])
def test_epoch_from_another_principal_is_refused(caller):               # Ruling 6
    ids = make_identities(now=lambda: NOW)
    with pytest.raises(AuthenticationError, match="controller_epoch is only accepted from ai_supervisor"):
        ids["trader"].verify_request(signed(ids, caller=caller, epoch=1), role="command")


@pytest.mark.parametrize("bad", [True, "3", 3.0, 0, -1, 2**53 + 1])
def test_epoch_on_the_wire_is_strict(bad):
    ids = make_identities(now=lambda: NOW)
    wire = signed(ids, epoch=3).model_dump(mode="json")
    wire["controller_epoch"] = bad
    with pytest.raises(AuthenticationError, match="malformed typed RPC request"):
        decode_request(canonical_json(wire))


def test_a_request_without_the_key_decodes_as_none():
    ids = make_identities(now=lambda: NOW)
    wire = signed(ids).model_dump(mode="json")
    del wire["controller_epoch"]
    assert decode_request(canonical_json(wire)).controller_epoch is None


def test_the_client_sends_the_epoch_and_the_handler_reads_it():
    ids = make_identities(now=time.time)
    registry = TypedRpcRegistry(acl=ALLOW_ALL)
    registry.register("command", "echo_epoch", dict, dict,
                      lambda body, caller: {"epoch": caller.controller_epoch}, with_caller=True)
    served = ServedStack({("trader", "command"): registry}, ids)
    try:
        client = served.client("ai_supervisor", "trader", "command")
        assert client.call("echo_epoch", {}, dict, controller_epoch=7) == {"epoch": 7}
        assert client.call("echo_epoch", {}, dict) == {"epoch": None}
        tampered = served.signed("ai_supervisor", role="command", method="echo_epoch", controller_epoch=7)
        tampered = tampered.model_copy(update={"controller_epoch": 8})
        assert served.raw_code(tampered) == "AUTHENTICATION_ERROR"
    finally:
        served.close()
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_typed_rpc_controller_epoch.py -q --timeout=60`
Expected: failures, `TypeError: ... got an unexpected keyword argument 'controller_epoch'`.

- [ ] **Step 3: Implement**

In `trader/messaging/principals.py`, after `RESERVED_PRINCIPALS`:

```python
# The one principal that may carry a controller epoch in the envelope (SP2 spec 5.1).
CONTROLLER_PRINCIPAL = "ai_supervisor"
```

In `trader/messaging/typed_rpc.py`:

```python
from trader.automation.controller_epoch import MAX_CONTROLLER_EPOCH, is_epoch_number
from trader.messaging.principals import CONTROLLER_PRINCIPAL, SERVER_ACCEPTS, SERVER_PRINCIPALS, is_valid_principal_name

RPC_REQUEST_CONTEXT = b"mmr.typed-rpc.request.v3\x00"   # v3: the signed bytes carry controller_epoch


class TypedRpcRequest(BaseModel):
    ...                                   # existing fields unchanged
    signature: str
    # SP2 spec 5.1: the ai controller's trader-granted epoch. Optional so every
    # other principal's request keeps its shape; always signed (null when absent).
    controller_epoch: Optional[int] = None

    @field_validator("controller_epoch", mode="before")
    @classmethod
    def _epoch_is_a_json_integer(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        if not is_epoch_number(value):
            raise ValueError(f"controller_epoch must be null or a JSON integer in 1..{MAX_CONTROLLER_EPOCH}")
        return value


def rpc_signing_bytes(request: TypedRpcRequest) -> bytes:
    """Bytes a request signature covers: caller, destination, payload and the controller epoch."""
    return RPC_REQUEST_CONTEXT + canonical_json({
        "principal": request.principal, "on_behalf_of": request.on_behalf_of,
        "server": request.server, "role": request.role, "method": request.method,
        "request_id": request.request_id, "timestamp": request.timestamp,
        "nonce": request.nonce, "body": request.body, "controller_epoch": request.controller_epoch,
    })


@dataclass(frozen=True)
class RpcCaller:
    """The authenticated caller of one request. ``on_behalf_of`` is log-only.

    ``controller_epoch`` is the signed envelope epoch (only ai_supervisor sends one).
    """

    principal: str
    on_behalf_of: Optional[str]
    controller_epoch: Optional[int] = None
```

`ServiceIdentity.sign_request` gains `controller_epoch: Optional[int] = None` and passes it into the unsigned `TypedRpcRequest(...)`. In `verify_request`, after the `on_behalf_of` check and before `self._nonce_cache.claim(...)`:

```python
        if request.controller_epoch is not None and request.principal != CONTROLLER_PRINCIPAL:
            raise AuthenticationError("controller_epoch is only accepted from ai_supervisor")
        self._nonce_cache.claim(request.nonce)
        return RpcCaller(request.principal, request.on_behalf_of, request.controller_epoch)
```

`TypedRpcClient.call(..., *, on_behalf_of=None, controller_epoch: Optional[int] = None)` passes `controller_epoch=controller_epoch` to `sign_request`. Docstring note 7 becomes: "All envelope models use `extra="forbid"`. Required fields have no defaults, so a missing or unexpected field raises. The one optional field is `controller_epoch` (default null, always signed)."

Import cycle check: `trader/automation/controller_epoch.py` imports nothing from `trader.messaging`, so the new import is safe.

In `tests/rpc_identity_fixtures.py`, `ServedStack.signed(...)` gets `controller_epoch=None` and passes it to `signer.sign_request(...)`.

- [ ] **Step 4: Run the new and the existing identity tests**

Also add `("controller_epoch", 1)` to the parametrize list of `tests/test_typed_rpc_identity.py::test_tampered_request_is_rejected` (the request there is signed by `cli` with no epoch, so the changed field must fail the signature or the principal check).

Run: `.venv/bin/python -m pytest tests/test_typed_rpc_controller_epoch.py tests/test_typed_rpc_identity.py tests/test_rpc_acl.py tests/test_typed_rpc.py -q --timeout=60`
Expected: all pass. If an existing test asserts that dropping any envelope field is malformed or pins the exact field set, exclude `controller_epoch` from that test; never make the field required (the index fixes `= None`).

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/typed_rpc.py trader/messaging/principals.py tests/rpc_identity_fixtures.py tests/test_typed_rpc_controller_epoch.py
git commit -m "feat: sign a controller epoch in the typed rpc envelope

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `grant_ai_controller_epoch` over typed RPC

**Files:**
- Modify: `trader/messaging/production_api.py` (wire model, handler, registration in `register_ai_paper_authority`)
- Modify: `trader/messaging/principals.py` (`TRADER_ACL` row)
- Modify: `trader/trading/command_stack.py` (`AiPaperServices.epochs`; migration 90 next to 54–56; build in `_build_ai_paper_services`)
- Modify: `tests/test_ai_paper_rpc.py` (`_served(..., now=lambda: NOW)` parameter)
- Test: `tests/test_ai_controller_rpc.py`

**Interfaces:**
- Consumes: `ControllerEpochs`, `EpochRefused`, `MIN_LEASE_SECONDS`, `MAX_LEASE_SECONDS`, `HOLDER_ID` (Task 1); `RpcCaller.controller_epoch` (Task 2); `_DispatchProblem` (`typed_rpc`).
- Produces: `GrantAiControllerEpochRequest`; `AiPaperServices.epochs: ControllerEpochs`; the `grant_ai_controller_epoch` command; `_require_controller_epoch(epochs, caller) -> None` (used by Tasks 4, 5, 7).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ai_controller_rpc.py
"""SP2 Plan 1: controller epoch, reconcile read and signals over signed typed RPC."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.test_controller_epoch import Clock
from tests.test_ai_paper_rpc import _served, command, query
from trader.automation.ai_paper_config import AiPaperConfig
from trader.messaging.typed_rpc import TypedRpcRemoteError


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def served(tmp_path, monkeypatch, clock):
    stack = _served(tmp_path, monkeypatch, AiPaperConfig(enabled=True), now=clock)
    yield stack
    stack.close()


def grant(served, holder="ctl-a", current=None, lease=60, principal="ai_supervisor"):
    return command(served, principal).call(
        "grant_ai_controller_epoch", {"holder_id": holder, "current_epoch": current, "lease_seconds": lease}, dict)


def code_of(fn, *args, **kwargs):
    with pytest.raises(TypedRpcRemoteError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_grant_renew_and_takeover(served, clock):
    first = grant(served)
    assert first["epoch"] == 1
    assert dt.datetime.fromisoformat(first["lease_expires_at"]) == NOW + dt.timedelta(seconds=60)
    clock.advance(20)
    assert grant(served, current=1)["epoch"] == 1
    assert code_of(grant, served, holder="ctl-b") == "CONTROLLER_EPOCH_HELD"
    clock.advance(61)
    assert grant(served, holder="ctl-b")["epoch"] == 2
    assert code_of(grant, served, current=9) == "CONTROLLER_EPOCH_UNKNOWN"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research", "strategy"])
def test_only_the_supervisor_may_grant(served, principal):
    assert code_of(grant, served, principal=principal) == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [
    {"holder_id": "ctl-a", "lease_seconds": 60},                                   # current_epoch key required
    {"holder_id": "ctl-a", "current_epoch": True, "lease_seconds": 60},
    {"holder_id": "ctl-a", "current_epoch": None, "lease_seconds": 60, "extra": 1}])
def test_grant_wire_is_strict(served, body):
    assert code_of(command(served, "ai_supervisor").call, "grant_ai_controller_epoch", body, dict) == \
        "VALIDATION_ERROR"
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py -q --timeout=60`
Expected: `TypeError: _served() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Implement**

`tests/test_ai_paper_rpc.py`: `def _served(tmp_path, monkeypatch, config, now=lambda: NOW)` and `build_command_stack(..., now=now)`.

`trader/messaging/principals.py`, in the "SP1 ai_paper" block of `TRADER_ACL`:

```python
    # SP2 Plan 1: the controller epoch (spec 6.2). Explicit sets per method.
    ("command", "grant_ai_controller_epoch"): frozenset({"ai_supervisor"}),
```

`trader/trading/command_stack.py`:

```python
@dataclass(frozen=True)
class AiPaperServices:
    ...                    # existing fields
    entry_filter: Any
    epochs: Any            # ControllerEpochs (SP2 Plan 1)
```

In the migration block after `apply_ai_paper_decision_migration(migrator)       # 56`:

```python
    from trader.automation.controller_epoch import apply_controller_epoch_migration

    apply_controller_epoch_migration(migrator)        # 90 (SP2 Plan 1)
```

In `_build_ai_paper_services`, before `AiPaperDecisionService(...)`: `epochs = ControllerEpochs(journal=journal, now=now)` (import from `trader.automation.controller_epoch`), and `AiPaperServices(..., epochs=epochs)`.

`trader/messaging/production_api.py`, next to the other ai_paper wire models:

```python
class GrantAiControllerEpochRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    holder_id: str
    current_epoch: Optional[Annotated[int, Field(ge=1, le=MAX_CONTROLLER_EPOCH)]]
    lease_seconds: Annotated[int, Field(ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)]

    @field_validator("holder_id")
    @classmethod
    def _holder_id_shape(cls, value: str) -> str:
        if not HOLDER_ID.fullmatch(value):
            raise ValueError("holder_id must match ^[a-z0-9][a-z0-9_.-]{0,63}$")
        return value
```

(`from trader.automation.controller_epoch import HOLDER_ID, MAX_CONTROLLER_EPOCH, MAX_LEASE_SECONDS, MIN_LEASE_SECONDS` at module top.) Handlers:

```python
def _require_controller_epoch(epochs, caller: RpcCaller) -> None:
    """Spec 5.1: checked before the coordinator, so a refusal writes no ledger row (Ruling 1a)."""
    from trader.automation.controller_epoch import EpochRefused
    try:
        epochs.require_current(caller.controller_epoch)
    except EpochRefused as ex:
        raise _DispatchProblem(ex.code, ex.message) from None


def _grant_ai_controller_epoch_handler(epochs):
    from trader.automation.controller_epoch import EpochRefused

    def _handler(parsed: GrantAiControllerEpochRequest, caller: RpcCaller) -> Dict[str, Any]:
        if caller.principal != CONTROLLER_PRINCIPAL:      # the allow-list already refuses; defense in depth
            raise _DispatchProblem("PERMISSION_DENIED", "only ai_supervisor holds a controller epoch")
        try:
            grant = epochs.grant(holder_id=parsed.holder_id, current_epoch=parsed.current_epoch,
                                 lease_seconds=parsed.lease_seconds)
        except EpochRefused as ex:
            raise _DispatchProblem(ex.code, ex.message) from None
        return {"epoch": grant.epoch, "lease_expires_at": grant.lease_expires_at.isoformat()}
    return _handler
```

In `register_ai_paper_authority`, after the `submit_ai_paper_decision` registration:

```python
    registry.register(
        "command", "grant_ai_controller_epoch", GrantAiControllerEpochRequest, dict,
        _grant_ai_controller_epoch_handler(ai_paper.epochs), with_caller=True,
    )
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_command_stack.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/production_api.py trader/messaging/principals.py trader/trading/command_stack.py tests/test_ai_paper_rpc.py tests/test_ai_controller_rpc.py
git commit -m "feat: grant the ai controller epoch over typed rpc

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Epoch checked at admission of `submit_ai_paper_decision`

**Files:**
- Modify: `trader/trading/command_coordinator.py` (`CommandRequest.controller_epoch`)
- Modify: `trader/automation/ai_paper_decision.py` (migration-56 `CREATE`, `_ROW_COLUMNS`, `DecisionRow`, `AiPaperDecisionService.__init__`, `_parse`, `_claim`)
- Modify: `trader/messaging/production_api.py` (`_submit_ai_paper_decision_rpc_handler`)
- Modify: `trader/trading/command_stack.py` (pass `epochs` to `AiPaperDecisionService`)
- Modify: `trader/acceptance/ports.py` (`RpcAcceptancePort`)
- Modify (ripple): `tests/automation/ai_paper_world.py`, `tests/sp1_fixtures.py`, `tests/test_ai_paper_rpc.py` (`test_end_to_end_enter_through_the_stack`)
- Test: `tests/automation/test_ai_paper_epoch.py`, `tests/test_ai_controller_rpc.py`, `tests/sp1_acceptance/test_controller_epoch_port.py`

**Interfaces:**
- Consumes: `ControllerEpochs.require_current_in_tx`, `EpochRefused`, `EPOCH_MISSING` (Task 1); `RpcCaller.controller_epoch` (Task 2); `_require_controller_epoch`, `AiPaperServices.epochs` (Task 3); `CommandSteps.claim(cmd, require_unpaused=..., extra=...)`.
- Produces: `CommandRequest.controller_epoch: Optional[int] = None` (not in `canonical_request_hash`); `AiPaperDecisionService(..., epochs: ControllerEpochs, ...)`; `DecisionRow.controller_epoch`; `RpcAcceptancePort` epoch handling.

- [ ] **Step 1: Write the failing tests**

```python
# tests/automation/test_ai_paper_epoch.py
"""SP2 Plan 1 Task 4: the epoch is checked atomically with the decision claim (spec 5.1)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.ai_paper_world import World
from trader.trading.command_coordinator import canonical_request_hash


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)                       # the World's experiment is ARMED by default


def takeover(world):
    world.clock.advance(seconds=61)
    return world.epochs.grant(holder_id="ctl-b", current_epoch=None, lease_seconds=60).epoch


def test_the_epoch_is_not_part_of_the_command_identity(world):
    request = world.request(world.body())
    assert canonical_request_hash(request) == canonical_request_hash(replace(request, controller_epoch=9))


def test_a_current_epoch_is_admitted_and_recorded(world):
    receipt = world.submit()
    assert receipt.state == "SUBMITTED", receipt
    assert world.decisions.row("dec-00000001").controller_epoch == world.epoch


def test_a_missing_epoch_is_refused(world):
    receipt = world.coordinator.execute(replace(world.request(world.body()), controller_epoch=None))
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_MISSING", False)
    assert world.dispatch.plans == []


def test_a_stale_epoch_is_refused_at_the_claim(world):
    takeover(world)
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_STALE", False)
    assert world.dispatch.plans == []


def test_takeover_during_admission_is_refused_at_the_claim(world, monkeypatch):     # Review Focus 2
    real = world.evidence.prepare_entry

    def prepare_then_take_over(**kwargs):
        prepared = real(**kwargs)
        takeover(world)              # after _validate, before _claim
        return prepared
    monkeypatch.setattr(world.evidence, "prepare_entry", prepare_then_take_over)
    receipt = world.submit()
    assert (receipt.state, receipt.error_code, receipt.retryable) == ("REJECTED", "CONTROLLER_EPOCH_STALE", False)
    assert world.dispatch.plans == []
    assert world.ledger.get(receipt.command_id).state == "REJECTED"
    assert world.decisions.row("dec-00000001").error_code == "CONTROLLER_EPOCH_STALE"


def test_an_admitted_command_survives_a_later_takeover(world):
    assert world.submit().state == "SUBMITTED"
    takeover(world)
    assert world.coordinator.get_command("aip-dec-00000001").state == "SUBMITTED"
    assert len(world.dispatch.plans) == 1


def test_a_reduction_needs_the_current_epoch_too(world):
    world.held(CONID, 300.0)
    takeover(world)
    close = world.body(action="CLOSE", side="SELL", deployment_digest=None, policy_revision=None,
                       stop_price=None, quantity=None)
    assert world.submit(close).error_code == "CONTROLLER_EPOCH_STALE"
```

Append to `tests/test_ai_controller_rpc.py`:

```python
from tests.test_ai_paper_rpc import enter_body, publish, register


def armed(served):
    publish(served)
    digest = register(served)
    started = command(served, "cli").call("start_experiment", {"command_id": "start-1", "reason": "go"}, dict)
    assert started["outcome"]["state"] == "ARMED", started
    served.stack.experiments.monitor.recover()
    return digest


def submit(served, body, epoch):
    return command(served, "ai_supervisor").call("submit_ai_paper_decision", body, dict, controller_epoch=epoch)


def test_missing_epoch_is_refused_without_a_ledger_row(served):
    body = enter_body(armed(served))
    assert code_of(submit, served, body, None) == "CONTROLLER_EPOCH_MISSING"
    assert served.coordinator.get_command("aip-dec-00000001") is None


def test_stale_holder_resend_is_refused_and_successor_replays(served, clock):       # Review Focus 1
    body = enter_body(armed(served))
    first = submit(served, body, grant(served)["epoch"])
    assert first["state"] == "SUBMITTED", first
    clock.advance(61)
    successor = grant(served, holder="ctl-b")["epoch"]
    assert code_of(submit, served, body, 1) == "CONTROLLER_EPOCH_STALE"
    replay = submit(served, body, successor)                                         # replay, not a new command
    assert (replay["state"], replay["command_id"]) == ("SUBMITTED", first["command_id"])
    assert len(served.orders.plans) == 1


def test_epoch_from_cli_on_submit_is_an_authentication_error(served):
    assert code_of(command(served, "cli").call, "submit_ai_paper_decision", enter_body(), dict,
                   controller_epoch=1) == "AUTHENTICATION_ERROR"
```

```python
# tests/sp1_acceptance/test_controller_epoch_port.py
"""SP2 Plan 1 Task 4: the SP1 acceptance port holds its own controller epoch (Ruling 16)."""
from __future__ import annotations

from trader.acceptance.ports import RpcAcceptancePort
from trader.messaging.typed_rpc import TypedRpcRemoteError


class FakeClient:
    def __init__(self, held_times=0):
        self.calls, self.held_times = [], held_times

    def call(self, method, body, kind, **options):
        self.calls.append((method, body, options))
        if method == "grant_ai_controller_epoch":
            if self.held_times:
                self.held_times -= 1
                raise TypedRpcRemoteError("CONTROLLER_EPOCH_HELD", "held")
            return {"epoch": 4, "lease_expires_at": "2026-10-07T14:01:00+00:00"}
        return {"state": "SUBMITTED"}


def test_submit_carries_a_granted_epoch_and_waits_while_held():
    command, sleeps = FakeClient(held_times=2), []
    port = RpcAcceptancePort(FakeClient(), command, FakeClient(), sleep=sleeps.append)
    assert port.supervisor("submit_ai_paper_decision", {"decision_id": "d"}) == {"state": "SUBMITTED"}
    grants = [c for c in command.calls if c[0] == "grant_ai_controller_epoch"]
    assert len(grants) == 3 and sleeps == [5.0, 5.0]
    assert grants[0][1]["holder_id"].startswith("acceptance-") and grants[0][1]["lease_seconds"] == 60
    assert command.calls[-1] == ("submit_ai_paper_decision", {"decision_id": "d"}, {"controller_epoch": 4})
    port.supervisor("submit_ai_paper_decision", {"decision_id": "e"})
    assert [c for c in command.calls if c[0] == "grant_ai_controller_epoch"][-1][1]["current_epoch"] == 4


def test_other_supervisor_calls_carry_no_epoch():
    command = FakeClient()
    port = RpcAcceptancePort(FakeClient(), command, FakeClient())
    port.supervisor("pause_experiment", {"x": 1})
    assert command.calls == [("pause_experiment", {"x": 1}, {})]
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/automation/test_ai_paper_epoch.py tests/test_ai_controller_rpc.py tests/sp1_acceptance/test_controller_epoch_port.py -q --timeout=60`
Expected: `AttributeError: 'World' object has no attribute 'epochs'`; the RPC tests fail with `SUBMITTED` instead of the epoch codes; the port test fails on the missing grant.

- [ ] **Step 3: Implement**

`trader/trading/command_coordinator.py`, `CommandRequest`, after `principal`:

```python
    # SP2 spec 5.1: the signed envelope epoch of an ai_supervisor command (set by
    # the RPC handler from RpcCaller, never from the body). Not command identity:
    # canonical_request_hash leaves it out, so a successor's resend replays.
    controller_epoch: Optional[int] = None
```

`trader/automation/ai_paper_decision.py`:

- Migration-56 `CREATE TABLE ai_paper_decisions`: add `controller_epoch BIGINT` after `principal VARCHAR` (edit in place; no legacy data).
- `_ROW_COLUMNS`: add `"controller_epoch"` after `"principal"`. `DecisionRow`: add `controller_epoch: Optional[int] = None`. `DecisionRow.received`: pass `controller_epoch=cmd.controller_epoch`.
- Imports: `from trader.automation.controller_epoch import EPOCH_MISSING, EpochRefused`.
- `AiPaperDecisionService.__init__` gets a required keyword `epochs: Any` and stores `self._epochs = epochs`.
- `_parse`, right after the command id and target check:

```python
        if cmd.controller_epoch is None:
            raise _Refusal(EPOCH_MISSING)
```

- `_claim` becomes:

```python
    def _claim(self, cmd, admission, *, require_unpaused: bool) -> None:
        """VALIDATED -> SUBMITTING. The epoch is read in the same transaction (spec 5.1, Ruling 1b)."""
        write_row = self._row_writer(admission, "SUBMITTING")

        def fenced_claim_writes(conn) -> None:
            self._epochs.require_current_in_tx(conn, cmd.controller_epoch)
            write_row(conn)
        try:
            self._steps.claim(cmd, require_unpaused=require_unpaused, extra=fenced_claim_writes)
        except EpochRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None          # not retryable: a successor holds it
        except Exception as ex:
            raise _Refusal(str(getattr(ex, "code", None) or "TRADING_PAUSED"), retryable=True) from None
        admission.state = "SUBMITTING"
```

A raise inside `extra` rolls back the whole `mutate_batch_work` transaction, including the `VALIDATED -> SUBMITTING` transition; `_reject` then moves `VALIDATED -> REJECTED` as for every other claim refusal.

`trader/trading/command_stack.py`, `_build_ai_paper_services`: `AiPaperDecisionService(..., epochs=epochs, ...)`.

`trader/messaging/production_api.py`:

```python
def _submit_ai_paper_decision_rpc_handler(coordinator: TradingCommandCoordinator, account_id: Optional[str],
                                          epochs):
    from trader.automation.ai_paper_decision import AI_PAPER_ACTION, command_id_for

    def _handler(parsed: SubmitAiPaperDecisionRequest, caller: RpcCaller) -> Dict[str, Any]:
        _require_controller_epoch(epochs, caller)
        request = CommandRequest(
            command_id=command_id_for(parsed.decision_id), action=AI_PAPER_ACTION, account_id=account_id,
            target_type="conid", target_id=str(parsed.conid), expected_version=None,
            body=parsed.model_dump(mode="json"), source=caller.principal, principal=caller.principal,
            controller_epoch=caller.controller_epoch,
        )
        return _receipt_to_dict(coordinator.execute(request))
    return _handler
```

and in `register_ai_paper_authority`: `_submit_ai_paper_decision_rpc_handler(coordinator, account_id, ai_paper.epochs)`.

`trader/acceptance/ports.py`:

```python
ACCEPTANCE_LEASE_SECONDS = 60
HELD_RETRY_SECONDS = 5.0


class RpcAcceptancePort:
    def __init__(self, research_client, supervisor_command, supervisor_query, operator_client=None, *,
                 now=_utc_now, sleep=time.sleep):
        ...                                                       # existing assignments
        self._holder_id = f"acceptance-{uuid.uuid4().hex[:12]}"
        self._epoch: Optional[int] = None

    def supervisor(self, method: str, body: dict) -> dict:
        client = self._supervisor_command if method in SUPERVISOR_COMMANDS else self._supervisor_query
        if method == "submit_ai_paper_decision":
            return self._call(client, method, body, controller_epoch=self._controller_epoch())
        return self._call(client, method, body)

    def _controller_epoch(self) -> int:
        """SP2 spec 5.1: the harness is a controller; grant or renew before each decision (Ruling 16)."""
        waited = 0.0
        while True:
            try:
                grant = self._call(self._supervisor_command, "grant_ai_controller_epoch", {
                    "holder_id": self._holder_id, "current_epoch": self._epoch,
                    "lease_seconds": ACCEPTANCE_LEASE_SECONDS})
            except RemoteRefusal as refusal:
                if refusal.code != "CONTROLLER_EPOCH_HELD" or waited >= ACCEPTANCE_LEASE_SECONDS:
                    raise
                self._sleep(HELD_RETRY_SECONDS)
                waited += HELD_RETRY_SECONDS
                continue
            self._epoch = grant["epoch"]
            return self._epoch

    @staticmethod
    def _call(client: Any, method: str, body: dict, **options: Any) -> dict:
        from trader.messaging.typed_rpc import TypedRpcRemoteError
        try:
            return client.call(method, body, dict, **options)
        except TypedRpcRemoteError as exc:
            raise RemoteRefusal(exc.code, str(exc)) from exc
```

(`import uuid` at the top.)

Ripple in tests:

- `tests/automation/ai_paper_world.py`: add `apply_controller_epoch_migration` to the migration tuple; after the journal setup, `self.epochs = ControllerEpochs(journal=self.journal, now=self.clock)` and `self.epoch = self.epochs.grant(holder_id="world", current_epoch=None, lease_seconds=60).epoch`; pass `epochs=self.epochs` to `AiPaperDecisionService`; `request(...)` builds `CommandRequest(..., controller_epoch=self.epoch)`.
- `tests/sp1_fixtures.py`, `ServedStack`: `self._epoch = None` in `__init__`, and

```python
    def call(self, principal, method, body):
        self.calls.append((principal, method))
        epoch = self.controller_epoch() if (principal, method) == ("ai_supervisor", "submit_ai_paper_decision") \
            else None
        return self.client(principal, _role_of(method)).call(method, body, dict, controller_epoch=epoch)

    def controller_epoch(self) -> int:
        """Grant or renew this fixture's controller epoch (SP2 Plan 1); not recorded in ``calls``."""
        grant = self.client("ai_supervisor", "command").call("grant_ai_controller_epoch", {
            "holder_id": "sp1-fixture", "current_epoch": self._epoch, "lease_seconds": 600}, dict)
        self._epoch = grant["epoch"]
        return self._epoch
```

- `tests/test_ai_paper_rpc.py::test_end_to_end_enter_through_the_stack`: grant first (`epoch = command(served, "ai_supervisor").call("grant_ai_controller_epoch", {"holder_id": "ctl-a", "current_epoch": None, "lease_seconds": 60}, dict)["epoch"]`) and pass `controller_epoch=epoch` to the submit call. `test_every_decision_is_refused_without_an_experiment` passes the epoch the same way (it asserts `NO_EXPERIMENT`, which comes after the epoch check).

- [ ] **Step 4: Run the new tests and every suite that submits decisions**

Run: `.venv/bin/python -m pytest tests/automation/test_ai_paper_epoch.py tests/test_ai_controller_rpc.py tests/sp1_acceptance/ tests/automation/test_ai_paper_entry.py tests/automation/test_ai_paper_reductions.py tests/automation/test_ai_dispatch_gate.py tests/automation/test_ai_paper_experiment_integration.py tests/test_ai_paper_rpc.py tests/test_command_coordinator.py tests/test_command_stack.py tests/test_command_stack_experiments.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/trading/command_coordinator.py trader/automation/ai_paper_decision.py trader/messaging/production_api.py trader/trading/command_stack.py trader/acceptance/ports.py tests/automation/ai_paper_world.py tests/sp1_fixtures.py tests/test_ai_paper_rpc.py tests/automation/test_ai_paper_epoch.py tests/test_ai_controller_rpc.py tests/sp1_acceptance/test_controller_epoch_port.py
git commit -m "feat: fence ai paper decisions with the controller epoch at the claim

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `get_ai_paper_decision` reconcile read

**Files:**
- Modify: `trader/messaging/production_api.py` (wire model, handler, registration)
- Modify: `trader/messaging/principals.py` (`TRADER_ACL` row)
- Test: `tests/test_ai_controller_rpc.py`

**Interfaces:**
- Consumes: `_require_controller_epoch` (Task 3); `TradingCommandCoordinator.get_command`; `AiPaperDecisionStore.row`; `command_id_for`; `_receipt_to_dict`.
- Produces: query `get_ai_paper_decision` (shape in Cross-plan additions).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ai_controller_rpc.py`)

```python
def read_decision(served, decision_id, epoch, principal="ai_supervisor"):
    return query(served, principal).call("get_ai_paper_decision", {"decision_id": decision_id}, dict,
                                         controller_epoch=epoch)


def test_reconcile_read_returns_receipt_and_decision_row(served):
    body = enter_body(armed(served))
    epoch = grant(served)["epoch"]
    first = submit(served, body, epoch)
    view = read_decision(served, "dec-00000001", epoch)
    assert view["found"] is True and view["receipt"]["state"] == first["state"] == "SUBMITTED"
    assert view["receipt"]["command_id"] == first["command_id"]
    assert (view["command_id"], view["decision_state"], view["controller_epoch"]) == \
        ("aip-dec-00000001", "SUBMITTED", epoch)
    unknown = read_decision(served, "dec-99999999", epoch)
    assert (unknown["found"], unknown["receipt"], unknown["decision_state"]) == (False, None, None)


def test_reconcile_read_needs_the_current_epoch(served, clock):
    epoch = grant(served)["epoch"]
    assert code_of(read_decision, served, "dec-00000001", None) == "CONTROLLER_EPOCH_MISSING"
    clock.advance(61)
    grant(served, holder="ctl-b")
    assert code_of(read_decision, served, "dec-00000001", epoch) == "CONTROLLER_EPOCH_STALE"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research"])
def test_reconcile_read_is_supervisor_only(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("get_ai_paper_decision", {"decision_id": "dec-00000001"}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [{"decision_id": "dec:0001"}, {"decision_id": 7}, {}, {"decision_id": "d" * 8,
                                                                                          "x": 1}])
def test_reconcile_read_wire_is_strict(served, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("get_ai_paper_decision", body, dict, controller_epoch=1)
    assert exc.value.code == "VALIDATION_ERROR"
```

- [ ] **Step 2: Run and see them fail**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py -q --timeout=60 -k reconcile`
Expected: `METHOD_NOT_ALLOWED` for every call.

- [ ] **Step 3: Implement**

ACL row: `("query", "get_ai_paper_decision"): frozenset({"ai_supervisor"}),`.

```python
class GetAiPaperDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision_id: str

    @field_validator("decision_id")
    @classmethod
    def _decision_id_shape(cls, value: str) -> str:
        if not _AI_DECISION_ID.match(value):
            raise ValueError("decision_id must match ^[A-Za-z0-9_-]{8,64}$")
        return value


def _get_ai_paper_decision_handler(coordinator: TradingCommandCoordinator, ai_paper):
    from trader.automation.ai_paper_decision import command_id_for

    def _handler(parsed: GetAiPaperDecisionRequest, caller: RpcCaller) -> Dict[str, Any]:
        _require_controller_epoch(ai_paper.epochs, caller)
        command_id = command_id_for(parsed.decision_id)
        receipt = coordinator.get_command(command_id)
        row = ai_paper.decision_store.row(parsed.decision_id)
        return {
            "decision_id": parsed.decision_id, "command_id": command_id, "found": receipt is not None,
            "receipt": None if receipt is None else _receipt_to_dict(receipt),
            "decision_state": None if row is None else row.state,
            "decision_error_code": None if row is None else row.error_code,
            "close_root_id": None if row is None else row.close_root_id,
            "controller_epoch": None if row is None else row.controller_epoch,
        }
    return _handler
```

Register in `register_ai_paper_authority`:
`registry.register("query", "get_ai_paper_decision", GetAiPaperDecisionRequest, dict, _get_ai_paper_decision_handler(coordinator, ai_paper), with_caller=True)`.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py tests/test_rpc_acl.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/production_api.py trader/messaging/principals.py tests/test_ai_controller_rpc.py
git commit -m "feat: add the epoch-fenced get_ai_paper_decision reconcile read

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Durable strategy signal record, written by the strategy service

**Files:**
- Create: `trader/data/strategy_signal_record.py`
- Modify: `trader/strategy/strategy_runtime.py` (`__init__` param, setup next to `EventStore`, `_dispatch_signal`, new `_record_signal`)
- Modify: `trader/config.py` (`StrategyRuntimeConfig.signal_record_retention_days = 7`; flat key `'strategy_signal_record_retention_days': ('strategy', 'signal_record_retention_days')`)
- Modify: `config_defaults/trader.yaml` (after `strategies_directory`: `strategy_signal_record_retention_days: 7  # durable signal record for the ai controller (SP2)`)
- Modify: `tests/test_signal_proposer.py` (`_make_runtime` sets `rt.signal_record`)
- Test: `tests/test_strategy_signal_record.py`

**Interfaces:**
- Consumes: `DuckDBConnection.transaction`, `DuckDBConnection.get_instance`; `Action` (`trader/objects.py`).
- Produces:
  - `DEFAULT_RETENTION_DAYS = 7`, `MAX_READ_LIMIT = 500`, `SOURCE_EVENT_ID = re.compile(r"^sig-[0-9a-f]{32}$")`.
  - `class SignalCursorAhead(ValueError)` with `code = "SIGNAL_CURSOR_AHEAD"`.
  - `@dataclass(frozen=True) class SignalEntry: source_event_id, strategy_name, conid: int, action: str, probability: Optional[float], signal_time: datetime`; `SignalEntry.create(*, strategy_name, conid, action, probability, signal_time) -> SignalEntry`.
  - `@dataclass(frozen=True) class RecordedSignal: cursor: int; entry: SignalEntry; recorded_at: datetime`; `to_json() -> dict` (index shape).
  - `@dataclass(frozen=True) class SignalPage: signals: tuple[RecordedSignal, ...]; next_cursor: int; oldest_retained_cursor: int; gap: bool`.
  - `class StrategySignalRecord(db: DuckDBConnection, *, retention_days: int = DEFAULT_RETENTION_DAYS, now=...)`: `append(entry) -> int`; `append_in_tx(conn, entry) -> int`; `read(after_cursor: int, limit: int) -> SignalPage`.
  - `completed_bar_time(frame) -> datetime` (aware UTC).
  - `StrategyRuntime.signal_record: StrategySignalRecord`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_strategy_signal_record.py
"""SP2 Plan 1 Task 6: the durable strategy signal record (spec amendment 6.1)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from tests.automation.test_controller_epoch import Clock
from tests.test_signal_proposer import _frame, _make_runtime
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import (
    SOURCE_EVENT_ID, SignalCursorAhead, SignalEntry, StrategySignalRecord,
)
from trader.objects import Action
from trader.trading.strategy import Signal

T0 = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def record(tmp_path, clock):
    return StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "mmr.duckdb")),
                                retention_days=7, now=clock)


def entry(minute=0, action="BUY", conid=265598, probability=0.7, strategy="orb"):
    return SignalEntry.create(strategy_name=strategy, conid=conid, action=action, probability=probability,
                              signal_time=T0 + dt.timedelta(minutes=minute))


def test_cursors_are_monotonic_and_reads_page(record):
    assert [record.append(entry(m)) for m in range(5)] == [1, 2, 3, 4, 5]
    page = record.read(after_cursor=1, limit=2)
    assert [s.cursor for s in page.signals] == [2, 3]
    assert (page.next_cursor, page.oldest_retained_cursor, page.gap) == (3, 1, False)
    view = page.signals[0].to_json()
    assert set(view) == {"cursor", "source_event_id", "strategy_name", "conid", "action", "probability",
                         "signal_time", "recorded_at"}
    assert SOURCE_EVENT_ID.fullmatch(view["source_event_id"]) and view["conid"] == 265598
    assert view["signal_time"] == (T0 + dt.timedelta(minutes=1)).isoformat()


def test_a_redelivered_signal_is_the_same_row(record):
    assert record.append(entry(0)) == 1
    assert record.append(entry(0)) == 1
    assert record.append(entry(0, action="SELL")) == 2
    assert len(record.read(0, 500).signals) == 2


def test_gap_only_after_real_pruning(record, clock):                        # Review Focus 4
    for m in range(3):
        record.append(entry(m))
    clock.now = T0 + dt.timedelta(days=8)
    record.append(entry(10_000))                                            # prunes cursors 1..3
    page = record.read(after_cursor=1, limit=10)
    assert page.gap is True and [s.cursor for s in page.signals] == [4]
    assert page.oldest_retained_cursor == 4
    assert record.read(after_cursor=3, limit=10).gap is False


def test_rolled_back_append_leaves_no_hole(record, tmp_path):              # Review Focus 4
    record.append(entry(0))
    db = DuckDBConnection.get_instance(str(tmp_path / "mmr.duckdb"))

    def append_then_fail(conn):
        record.append_in_tx(conn, entry(1))
        raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        db.transaction(append_then_fail)
    assert record.append(entry(2)) == 2
    assert record.read(after_cursor=0, limit=10).gap is False


def test_empty_record_reports_the_next_cursor(record):
    page = record.read(after_cursor=0, limit=10)
    assert (page.signals, page.next_cursor, page.oldest_retained_cursor, page.gap) == ((), 0, 1, False)


def test_cursor_ahead_of_the_record_is_refused(record):                     # Review Focus 4
    record.append(entry(0))
    with pytest.raises(SignalCursorAhead):
        record.read(after_cursor=2, limit=10)


@pytest.mark.parametrize("after,limit", [(-1, 10), (0, 0), (0, 501), (True, 10), (0, True), (0.0, 10)])
def test_read_arguments_are_strict(record, after, limit):
    with pytest.raises(ValueError):
        record.read(after_cursor=after, limit=limit)


@pytest.mark.parametrize("changes", [{"action": "NEUTRAL"}, {"conid": 0}, {"conid": True}, {"conid": "265598"},
                                     {"strategy": ""}])
def test_entries_are_strict(changes):
    kwargs = {"minute": 0, **changes}
    with pytest.raises(ValueError):
        entry(**kwargs)


def test_non_finite_probability_is_stored_as_null(record):
    record.append(entry(0, probability=math.nan))
    assert record.read(0, 1).signals[0].to_json()["probability"] is None


@pytest.mark.parametrize("days", [0, 366, True, 7.0])
def test_retention_days_are_checked(tmp_path, days):
    with pytest.raises(ValueError):
        StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "x.duckdb")), retention_days=days)


def test_dispatch_records_buy_and_sell_but_not_neutral(tmp_path, installed_strategy, clock):
    rt = _make_runtime(tmp_path)
    rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(str(tmp_path / "s.duckdb")), now=clock)
    frame = _frame(last_time="2026-10-07 14:30")
    for action in (Action.BUY, Action.NEUTRAL, Action.SELL, Action.BUY):
        rt._dispatch_signal(installed_strategy, Signal(source_name="x", action=action, probability=0.6, risk=0.1),
                            conId=4391, frame=frame)
    signals = [s.to_json() for s in rt.signal_record.read(0, 10).signals]
    assert [(s["action"], s["conid"], s["strategy_name"]) for s in signals] == [
        ("BUY", 4391, installed_strategy.name), ("SELL", 4391, installed_strategy.name)]
    assert signals[0]["signal_time"] == "2026-10-07T14:30:00+00:00"
    assert len(rt.event_store.events) == 4                                   # trading_events unchanged
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_strategy_signal_record.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.data.strategy_signal_record'`.

- [ ] **Step 3: Implement**

```python
# trader/data/strategy_signal_record.py
"""Durable strategy signal record with a monotonic cursor (SP2 spec amendment 6.1).

The strategy service writes one row per BUY/SELL signal into ``duckdb_path``
(the file it already writes ``trading_events`` to); the trader reads it for
``read_ai_signals``. Cursors come from a counter row updated in the same
transaction as the insert, so a rolled-back write leaves no hole. Retention
deletes a prefix of cursors and raises ``retention_watermark`` in the same
transaction; ``gap`` is computed from that watermark only (Plan 1 Ruling 10).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import numbers
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 365
MAX_READ_LIMIT = 500
SIGNAL_ACTIONS = ("BUY", "SELL")
SOURCE_EVENT_ID = re.compile(r"^sig-[0-9a-f]{32}$")

_CREATE = (
    """CREATE TABLE IF NOT EXISTS strategy_signal_record (
        cursor BIGINT PRIMARY KEY, source_event_id VARCHAR NOT NULL UNIQUE, strategy_name VARCHAR NOT NULL,
        conid BIGINT NOT NULL, action VARCHAR NOT NULL, probability DOUBLE,
        signal_time TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS strategy_signal_record_state (
        last_cursor BIGINT NOT NULL, retention_watermark BIGINT NOT NULL)""",
    """INSERT INTO strategy_signal_record_state SELECT 0, 0
        WHERE NOT EXISTS (SELECT 1 FROM strategy_signal_record_state)""",
)
_COLUMNS = "cursor, source_event_id, strategy_name, conid, action, probability, signal_time, recorded_at"


class SignalCursorAhead(ValueError):
    code = "SIGNAL_CURSOR_AHEAD"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError("signal times must be timezone-aware datetimes")
    return value.astimezone(dt.timezone.utc)


def _is_count(value: object, low: int, high: Optional[int] = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def completed_bar_time(frame: Any) -> dt.datetime:
    """The completed bar's time; a naive index is read as UTC (same rule as the intent emitter)."""
    last_bar = frame.index[-1]
    bar_time = last_bar.to_pydatetime() if hasattr(last_bar, "to_pydatetime") else last_bar
    if not isinstance(bar_time, dt.datetime):
        raise ValueError(f"signal frame index must hold datetimes, got {type(bar_time).__name__}")
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=dt.timezone.utc)
    return bar_time.astimezone(dt.timezone.utc)


def source_event_id_for(strategy_name: str, conid: int, action: str, signal_time: dt.datetime) -> str:
    material = "\x00".join((strategy_name, str(conid), action, signal_time.isoformat()))
    return "sig-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class SignalEntry:
    source_event_id: str
    strategy_name: str
    conid: int
    action: str
    probability: Optional[float]
    signal_time: dt.datetime

    @classmethod
    def create(cls, *, strategy_name: str, conid: object, action: str, probability: object,
               signal_time: dt.datetime) -> "SignalEntry":
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("strategy_name must be a non-empty string")
        if isinstance(conid, bool) or not isinstance(conid, numbers.Integral) or conid <= 0:
            raise ValueError("conid must be an integer > 0")
        if action not in SIGNAL_ACTIONS:
            raise ValueError(f"action must be one of {SIGNAL_ACTIONS}")
        exact_conid = int(conid)                     # numpy integers are exact; strings were refused above
        when = _as_utc(signal_time)
        finite = (isinstance(probability, (int, float)) and not isinstance(probability, bool)
                  and math.isfinite(probability))
        return cls(source_event_id_for(strategy_name, exact_conid, action, when), strategy_name, exact_conid,
                   action, float(probability) if finite else None, when)


@dataclass(frozen=True)
class RecordedSignal:
    cursor: int
    entry: SignalEntry
    recorded_at: dt.datetime

    def to_json(self) -> dict:
        return {"cursor": self.cursor, "source_event_id": self.entry.source_event_id,
                "strategy_name": self.entry.strategy_name, "conid": self.entry.conid,
                "action": self.entry.action, "probability": self.entry.probability,
                "signal_time": self.entry.signal_time.isoformat(), "recorded_at": self.recorded_at.isoformat()}


@dataclass(frozen=True)
class SignalPage:
    signals: tuple[RecordedSignal, ...]
    next_cursor: int
    oldest_retained_cursor: int
    gap: bool


class StrategySignalRecord:
    def __init__(self, db: Any, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                 now: Callable[[], dt.datetime] = _utc_now):
        if not _is_count(retention_days, 1, MAX_RETENTION_DAYS):
            raise ValueError(f"strategy_signal_record_retention_days must be an integer in 1..{MAX_RETENTION_DAYS}")
        self._db = db
        self._retention = dt.timedelta(days=retention_days)
        self._now = now
        self._db.transaction(self._create_in_tx)

    @staticmethod
    def _create_in_tx(conn: Any) -> None:
        for statement in _CREATE:
            conn.execute(statement)

    def append(self, entry: SignalEntry) -> int:
        return self._db.transaction(lambda conn: self.append_in_tx(conn, entry))

    def append_in_tx(self, conn: Any, entry: SignalEntry) -> int:
        existing = conn.execute("SELECT cursor FROM strategy_signal_record WHERE source_event_id = ?",
                                [entry.source_event_id]).fetchone()
        if existing is not None:
            return int(existing[0])
        last_cursor, watermark = conn.execute(
            "SELECT last_cursor, retention_watermark FROM strategy_signal_record_state").fetchone()
        now = _as_utc(self._now())
        cursor = int(last_cursor) + 1
        conn.execute(f"INSERT INTO strategy_signal_record ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     [cursor, entry.source_event_id, entry.strategy_name, entry.conid, entry.action,
                      entry.probability, entry.signal_time, now])
        watermark = self._prune_in_tx(conn, int(watermark), now)
        conn.execute("UPDATE strategy_signal_record_state SET last_cursor = ?, retention_watermark = ?",
                     [cursor, watermark])
        return cursor

    def _prune_in_tx(self, conn: Any, watermark: int, now: dt.datetime) -> int:
        """Delete a prefix of cursors older than the retention; return the new watermark."""
        (newest_expired,) = conn.execute("SELECT MAX(cursor) FROM strategy_signal_record WHERE recorded_at < ?",
                                         [now - self._retention]).fetchone()
        if newest_expired is None or newest_expired <= watermark:
            return watermark
        conn.execute("DELETE FROM strategy_signal_record WHERE cursor <= ?", [newest_expired])
        return int(newest_expired)

    def read(self, after_cursor: int, limit: int) -> SignalPage:
        if not _is_count(after_cursor, 0):
            raise ValueError("after_cursor must be an integer >= 0")
        if not _is_count(limit, 1, MAX_READ_LIMIT):
            raise ValueError(f"limit must be an integer in 1..{MAX_READ_LIMIT}")
        return self._db.transaction(lambda conn: self._read_in_tx(conn, after_cursor, limit))

    def _read_in_tx(self, conn: Any, after_cursor: int, limit: int) -> SignalPage:
        last_cursor, watermark = (int(v) for v in conn.execute(
            "SELECT last_cursor, retention_watermark FROM strategy_signal_record_state").fetchone())
        if after_cursor > last_cursor:
            raise SignalCursorAhead(f"after_cursor {after_cursor} is beyond the newest cursor {last_cursor}; "
                                    "the signal record was reset")
        rows = conn.execute(f"SELECT {_COLUMNS} FROM strategy_signal_record WHERE cursor > ? "
                            "ORDER BY cursor LIMIT ?", [after_cursor, limit]).fetchall()
        (oldest,) = conn.execute("SELECT MIN(cursor) FROM strategy_signal_record").fetchone()
        signals = tuple(
            RecordedSignal(int(row[0]), SignalEntry(row[1], row[2], int(row[3]), row[4], row[5], _as_utc(row[6])),
                           _as_utc(row[7]))
            for row in rows)
        next_cursor = signals[-1].cursor if signals else max(after_cursor, watermark)
        return SignalPage(signals, next_cursor, int(oldest) if oldest is not None else watermark + 1,
                          after_cursor < watermark)
```

`trader/strategy/strategy_runtime.py`:

- `__init__` gets `strategy_signal_record_retention_days: int = DEFAULT_RETENTION_DAYS` and stores it.
- In the setup block, right after `self.event_store = EventStore(self.duckdb_path)`:

```python
            self.signal_record = StrategySignalRecord(
                self.event_store.db, retention_days=self.strategy_signal_record_retention_days)
```

- In `_dispatch_signal`, replace the "Persist signal to event store" block's first line with `self._record_signal(strategy, signal, conId, frame)` followed by the unchanged `TradingEvent` / `self.event_store.append(event)` lines, and add:

```python
    def _record_signal(self, strategy: Strategy, signal, conId: int, frame: pd.DataFrame) -> None:
        """Spec 6.1: BUY/SELL go to the durable record before any publish; a failed write raises."""
        if signal.action not in (Action.BUY, Action.SELL):
            return
        self.signal_record.append(SignalEntry.create(
            strategy_name=strategy.name, conid=conId, action=str(signal.action),
            probability=signal.probability, signal_time=completed_bar_time(frame)))
```

`tests/test_signal_proposer.py::_make_runtime`: add `rt.signal_record = _RecordingSignalRecord()` with

```python
class _RecordingSignalRecord:
    def __init__(self):
        self.entries = []

    def append(self, entry):
        self.entries.append(entry)
        return len(self.entries)
```

`trader/config.py`: the field and flat-key entry from the Files list. `Container.resolve` passes the flat value to the `StrategyRuntime` parameter of the same name; `StrategySignalRecord` refuses a bad value at startup.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_strategy_signal_record.py tests/test_signal_proposer.py tests/test_event_store.py tests/test_config.py -q --timeout=60`
Expected: all pass. (If `tests/test_config.py` pins the flat key set, add the new key to its expected set in this step.)

- [ ] **Step 5: Commit**

```bash
git add trader/data/strategy_signal_record.py trader/strategy/strategy_runtime.py trader/config.py config_defaults/trader.yaml tests/test_signal_proposer.py tests/test_strategy_signal_record.py
git commit -m "feat: record strategy signals durably with a monotonic cursor

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `read_ai_signals` over typed RPC

**Files:**
- Modify: `trader/messaging/production_api.py` (wire model, handler, registration)
- Modify: `trader/messaging/principals.py` (`TRADER_ACL` row)
- Modify: `trader/trading/command_stack.py` (`AiPaperServices.signals`; build in `_build_ai_paper_services`)
- Modify: `tests/test_command_stack.py` (`_trader` gets `duckdb_path=str(tmp_path / "mmr.duckdb")`)
- Modify: `tests/sp1_fixtures.py` (`Composed`: `trader.duckdb_path = str(tmp_path / "mmr.duckdb")`)
- Test: `tests/test_ai_controller_rpc.py`

**Interfaces:**
- Consumes: `StrategySignalRecord.read`, `SignalCursorAhead`, `RecordedSignal.to_json` (Task 6); `_require_controller_epoch` (Task 3).
- Produces: `AiPaperServices.signals: StrategySignalRecord`; query `read_ai_signals` (index shape).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ai_controller_rpc.py`)

```python
from trader.data.duckdb_store import DuckDBConnection
from trader.data.strategy_signal_record import SignalEntry, StrategySignalRecord


def strategy_writes(served, minutes, *, now=NOW):
    """The strategy service's side: its own record object on the same file."""
    record = StrategySignalRecord(DuckDBConnection.get_instance(served.stack.ai_paper.signals_path),
                                  now=lambda: now)
    for minute in minutes:
        record.append(SignalEntry.create(strategy_name="orb", conid=265598, action="BUY", probability=0.6,
                                         signal_time=NOW + dt.timedelta(minutes=minute)))


def read_signals(served, after, limit, epoch, principal="ai_supervisor"):
    return query(served, principal).call("read_ai_signals", {"after_cursor": after, "limit": limit}, dict,
                                         controller_epoch=epoch)


def test_signals_page_through_rpc(served):
    epoch = grant(served)["epoch"]
    strategy_writes(served, range(3))
    page = read_signals(served, 0, 2, epoch)
    assert [s["cursor"] for s in page["signals"]] == [1, 2]
    assert (page["next_cursor"], page["oldest_retained_cursor"], page["gap"]) == (2, 1, False)
    assert page["signals"][0]["action"] == "BUY" and page["signals"][0]["conid"] == 265598


def test_signal_gap_and_reset_are_reported(served):
    epoch = grant(served)["epoch"]
    strategy_writes(served, range(2))
    strategy_writes(served, [9_999], now=NOW + dt.timedelta(days=8))
    assert read_signals(served, 0, 10, epoch)["gap"] is True
    assert code_of(read_signals, served, 50, 10, epoch) == "SIGNAL_CURSOR_AHEAD"


def test_signal_read_needs_the_current_epoch(served, clock):
    epoch = grant(served)["epoch"]
    assert code_of(read_signals, served, 0, 10, None) == "CONTROLLER_EPOCH_MISSING"
    clock.advance(61)
    grant(served, holder="ctl-b")
    assert code_of(read_signals, served, 0, 10, epoch) == "CONTROLLER_EPOCH_STALE"


@pytest.mark.parametrize("principal", ["cli", "dashboard", "ai_research", "strategy"])
def test_signal_read_is_supervisor_only(served, principal):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, principal).call("read_ai_signals", {"after_cursor": 0, "limit": 1}, dict)
    assert exc.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("body", [{"after_cursor": 0, "limit": 501}, {"after_cursor": True, "limit": 1},
                                  {"after_cursor": 0, "limit": 1, "x": 1}])
def test_signal_read_wire_is_strict(served, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served, "ai_supervisor").call("read_ai_signals", body, dict, controller_epoch=1)
    assert exc.value.code == "VALIDATION_ERROR"
```

- [ ] **Step 2: Run and see them fail**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py -q --timeout=60 -k signal`
Expected: `AttributeError: 'AiPaperServices' object has no attribute 'signals_path'`.

- [ ] **Step 3: Implement**

ACL row: `("query", "read_ai_signals"): frozenset({"ai_supervisor"}),`.

`trader/trading/command_stack.py`: `AiPaperServices` gains `signals: Any` (`StrategySignalRecord`) and `signals_path: str`. In `_build_ai_paper_services`:

```python
    signals_path = getattr(trader, "duckdb_path", None)
    if not signals_path:
        raise CommandStackConfigurationError(
            "MISSING_DUCKDB_PATH", "ai_paper needs duckdb_path to serve the strategy signal record")
    signals = StrategySignalRecord(DuckDBConnection.get_instance(signals_path))
```

(passed as `signals=signals, signals_path=signals_path`).

`trader/messaging/production_api.py`:

```python
class ReadAiSignalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    after_cursor: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=500)]


def _read_ai_signals_handler(ai_paper):
    from trader.data.strategy_signal_record import SignalCursorAhead

    def _handler(parsed: ReadAiSignalsRequest, caller: RpcCaller) -> Dict[str, Any]:
        _require_controller_epoch(ai_paper.epochs, caller)
        try:
            page = ai_paper.signals.read(parsed.after_cursor, parsed.limit)
        except SignalCursorAhead as ex:
            raise _DispatchProblem(ex.code, str(ex)) from None
        return {"signals": [signal.to_json() for signal in page.signals], "next_cursor": page.next_cursor,
                "oldest_retained_cursor": page.oldest_retained_cursor, "gap": page.gap}
    return _handler
```

Register in `register_ai_paper_authority` on the query socket with `with_caller=True` (the production registry's default `thread` execution keeps DuckDB lock back-off off the event loop).

Fixtures: `tests/test_command_stack.py::_trader` adds `duckdb_path=str(tmp_path / "mmr.duckdb")`; `tests/sp1_fixtures.py::Composed.__init__` adds `trader.duckdb_path = str(tmp_path / "mmr.duckdb")` next to `trader.ib_account`.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_ai_controller_rpc.py tests/test_command_stack.py tests/test_command_stack_experiments.py tests/scoreboard/test_wiring.py tests/test_rpc_acl.py tests/sp1_acceptance/ -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/production_api.py trader/messaging/principals.py trader/trading/command_stack.py tests/test_command_stack.py tests/sp1_fixtures.py tests/test_ai_controller_rpc.py
git commit -m "feat: serve the strategy signal record to the ai supervisor

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Operator publishes the initial risk policy

**Files:**
- Modify: `trader/messaging/principals.py` (`publish_ai_risk_policy` row)
- Modify: `trader/automation/ai_paper_actions.py` (`POLICY_PUBLISHERS`, `_publish`, module docstring)
- Create: `trader/automation/ai_policy_file.py`
- Modify: `trader/sdk.py` (`ai_policy_publish`, `ai_policy_view`)
- Modify: `trader/mmr_cli.py` (`ai-policy` parser, `_handle_ai_policy`, dispatch next to `experiment`)
- Modify: `tests/test_rpc_acl.py` (`AI_PAPER_FAMILY`, new rights test)
- Modify: `docs/CLI_REFERENCE.md` (two lines after the `experiment` block)
- Test: `tests/test_ai_policy_operator.py`

**Interfaces:**
- Consumes: `RiskLimits.from_json`, `RiskLimitsError` (`trader/automation/risk_limits.py`); `PublishAiRiskPolicyRequest`; `MMR._typed_command`, `MMR._typed_query`.
- Produces: `POLICY_PUBLISHERS = frozenset({"ai_supervisor", "cli"})`; `PolicyFileError(ValueError)`; `load_policy_file(path) -> dict` (the `limits` mapping); `MMR.ai_policy_publish(limits: dict, reason: str, command_id: Optional[str] = None) -> SuccessFail`; `MMR.ai_policy_view() -> dict`; CLI `ai-policy publish FILE --reason TEXT [--command-id ID]`, `ai-policy show`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ai_policy_operator.py
"""SP2 Plan 1 Task 8: the operator publishes the initial AI risk policy (spec amendment 6.7)."""
from __future__ import annotations

import json

import pytest

from tests.test_ai_paper_rpc import command, query, served  # noqa: F401 (fixture)
from trader import mmr_cli
from trader.automation.ai_policy_file import PolicyFileError, load_policy_file
from trader.automation.risk_limits import PAPER_LIMITS
from trader.messaging.principals import TRADER_ACL
from trader.mmr_cli import build_parser

SP2_PLAN1_ROWS = {
    ("command", "grant_ai_controller_epoch"): {"ai_supervisor"},
    ("query", "read_ai_signals"): {"ai_supervisor"},
    ("query", "get_ai_paper_decision"): {"ai_supervisor"},
    ("command", "publish_ai_risk_policy"): {"ai_supervisor", "cli"},
}


def rights(principal):
    return {key for key, allowed in TRADER_ACL.items() if principal in allowed}


def test_plan1_rows_are_exact_and_research_gets_none():
    assert {key: set(TRADER_ACL[key]) for key in SP2_PLAN1_ROWS} == SP2_PLAN1_ROWS
    assert not set(SP2_PLAN1_ROWS) & rights("ai_research")
    assert not set(SP2_PLAN1_ROWS) & rights("dashboard")


def test_ai_research_rights_are_exactly_the_sp1_set():
    from trader.messaging.principals import _TRADER_MARKET_READS
    expected = {("query", m) for m in _TRADER_MARKET_READS} | {
        ("query", "resolve_instrument"), ("query", "discover_instrument"),
        ("command", "register_ai_deployment"), ("query", "get_ai_deployment")}
    assert rights("ai_research") == expected


def test_cli_gains_only_the_publish():
    new_for_cli = {key for key in SP2_PLAN1_ROWS if "cli" in SP2_PLAN1_ROWS[key]}
    assert new_for_cli == {("command", "publish_ai_risk_policy")}
    assert ("command", "submit_ai_paper_decision") not in rights("cli")


def test_operator_publishes_through_signed_rpc(served):
    out = command(served, "cli").call(
        "publish_ai_risk_policy", {"command_id": "cli-pol-1", "limits": PAPER_LIMITS.to_json(), "reason": "init"},
        dict)
    assert (out["state"], out["outcome"]["revision"]) == ("RESOLVED", 1)
    view = query(served, "cli").call("get_ai_risk_policy", {}, dict)
    assert view["latest_published_revision"] == 1


def write(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    return path


def test_policy_file_loads_exact_limits(tmp_path):
    lines = "\n".join(f"  {k}: {v}" for k, v in PAPER_LIMITS.to_json().items())
    assert load_policy_file(write(tmp_path, f"limits:\n{lines}\n")) == PAPER_LIMITS.to_json()


@pytest.mark.parametrize("text", ["", "- 1\n", "limits: 3\n", "limits: {max_positions: 3}\n",
                                  "limits: {}\nextra: 1\n", "!!python/object:os.system {}\n"])
def test_bad_policy_files_fail_loudly(tmp_path, text):
    with pytest.raises(PolicyFileError):
        load_policy_file(write(tmp_path, text))


class FakeSdk:
    def __init__(self):
        self.calls = []

    def ai_policy_publish(self, limits, reason, command_id=None):
        from trader.common.reactivex import SuccessFail
        self.calls.append((limits, reason, command_id))
        return SuccessFail.success(obj={"revision": 1, "applied_now": [], "queued": []})

    def ai_policy_view(self):
        return {"latest_published_revision": 1}


def test_cli_publish_reads_the_file_and_sends_the_reason(tmp_path, capsys, monkeypatch):
    lines = "\n".join(f"  {k}: {v}" for k, v in PAPER_LIMITS.to_json().items())
    path = write(tmp_path, f"limits:\n{lines}\n")
    sdk = FakeSdk()
    monkeypatch.setattr(mmr_cli, "_json_mode", True)
    mmr_cli._handle_ai_policy(sdk, build_parser().parse_args(
        ["ai-policy", "publish", str(path), "--reason", "initial", "--command-id", "cli-pol-7"]))
    assert sdk.calls == [(PAPER_LIMITS.to_json(), "initial", "cli-pol-7")]
    assert json.loads(capsys.readouterr().out)["data"]["revision"] == 1


def test_sdk_publish_uses_a_colon_free_cli_command_id(monkeypatch):
    from types import SimpleNamespace
    from trader.domain.commands import CommandReceipt
    from trader.sdk import MMR

    calls = []
    client = SimpleNamespace(call=lambda method, body, kind: calls.append((method, body)) or CommandReceipt(
        body["command_id"], body["command_id"], "RESOLVED", {"revision": 2}, None, False))
    mmr = MMR.__new__(MMR)
    monkeypatch.setattr(MMR, "_typed_command", property(lambda self: client))
    assert mmr.ai_policy_publish(PAPER_LIMITS.to_json(), "init").is_success()
    method, body = calls[0]
    assert method == "publish_ai_risk_policy" and body["command_id"].startswith("cli-pol-")
    assert ":" not in body["command_id"] and body["reason"] == "init"
```

Add to `tests/test_ai_paper_rpc.py::test_wrong_principal_is_refused_by_the_service_too` a fourth case: `request("publish_ai_risk_policy", {...}, ("ai_policy", ACCOUNT), "dashboard")` → `PRINCIPAL_FORBIDDEN` (the service refuses principals outside `POLICY_PUBLISHERS` even when the allow-list is bypassed).

- [ ] **Step 2: Run and see them fail**

Run: `.venv/bin/python -m pytest tests/test_ai_policy_operator.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.automation.ai_policy_file'`.

- [ ] **Step 3: Implement**

`trader/messaging/principals.py`: `("command", "publish_ai_risk_policy"): frozenset({"ai_supervisor", "cli"}),` with the comment `# SP2 Plan 1 (spec 6.7): the operator publishes the initial policy; SP2a/b code never calls it as ai_supervisor.`

`trader/automation/ai_paper_actions.py`:

```python
# SP2 spec 6.7: the operator publishes the initial policy; ai_supervisor keeps the right for SP2d.
POLICY_PUBLISHERS = frozenset({AI_SUPERVISOR, "cli"})
...
        if cmd.principal not in POLICY_PUBLISHERS:
            raise _Refused("PRINCIPAL_FORBIDDEN", "only ai_supervisor or the cli operator publishes risk policies")
```

(Module docstring: "``publish_ai_risk_policy`` (ai_supervisor, cli)".)

```python
# trader/automation/ai_policy_file.py
"""The operator's AI risk policy file for ``mmr ai-policy publish`` (SP2 spec 6.7).

Format: one mapping with exactly the key ``limits``, whose value has exactly the
``RiskLimits`` fields. Loaded with ``yaml.safe_load``; checked here so a typo
fails before anything is sent. The trader checks it again.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from trader.automation.risk_limits import RiskLimits, RiskLimitsError


class PolicyFileError(ValueError):
    pass


def load_policy_file(path: str | Path) -> dict:
    try:
        document = yaml.safe_load(Path(path).expanduser().read_text())
    except (OSError, yaml.YAMLError) as ex:
        raise PolicyFileError(f"cannot read policy file {path}: {ex}") from None
    if not isinstance(document, dict) or set(document) != {"limits"}:
        raise PolicyFileError("policy file must be a mapping with exactly the key 'limits'")
    try:
        return RiskLimits.from_json(document["limits"]).to_json()
    except RiskLimitsError as ex:
        raise PolicyFileError(str(ex)) from None
```

`trader/sdk.py`, next to `experiment_start` (same shape as `_experiment_command`):

- `ai_policy_publish(self, limits: dict, reason: str, command_id: Optional[str] = None) -> SuccessFail`: `command_id = command_id or f'cli-pol-{uuid.uuid4().hex}'`; body `{'command_id', 'limits', 'reason'}`; `self._typed_command.call('publish_ai_risk_policy', body, CommandReceipt)`. `RESOLVED` → `SuccessFail.success(obj=receipt.outcome)`; a `REJECTED` receipt → `SuccessFail.fail` naming `error_code` and `outcome['message']`; `TypedRpcRemoteError` → fail with `code: message`; `TimeoutError` / `ConnectionError` → fail with the text `Retry with --command-id {command_id} to replay the same publish.` (an identical retry replays from the ledger instead of publishing a second revision).
- `ai_policy_view(self) -> dict`: `self._typed_query.call('get_ai_risk_policy', {}, dict)`.

`trader/mmr_cli.py`, after the `experiment` parser: `ai-policy` with subcommands `show` and `publish FILE --reason TEXT (required) [--command-id ID]` (dest `ai_policy_action`, epilog with the three examples in `docs/CLI_REFERENCE.md` below). Dispatch `elif cmd == 'ai-policy': _handle_ai_policy(mmr, args)` next to `experiment`. The handler:

```python
def _handle_ai_policy(mmr: MMR, args: argparse.Namespace):
    from trader.automation.ai_policy_file import PolicyFileError, load_policy_file

    if (getattr(args, 'ai_policy_action', None) or 'show') == 'show':
        print_json_result(mmr.ai_policy_view(), title='AI risk policy (paper)')
        return
    try:
        limits = load_policy_file(args.file)
    except PolicyFileError as ex:
        print_status(f'ai-policy publish: {ex}', success=False)
        sys.exit(1)
    result = mmr.ai_policy_publish(limits, args.reason, command_id=args.command_id)
    if not result.is_success():
        print_status(f"ai-policy publish failed: {result.error or result.exception}", success=False)
        sys.exit(1)
    print_json_result(result.obj or {}, title='AI risk policy published')
```

`tests/test_rpc_acl.py`: `AI_PAPER_FAMILY[("command", "publish_ai_risk_policy")] = {"ai_supervisor", "cli"}`; in `test_ai_paper_family_rights_are_exact` nothing else changes (`ai_research` still has only register and the deployment read).

`docs/CLI_REFERENCE.md`, after the `experiment stop` line:

```
ai-policy show                               # PAPER AI risk policy: published, effective, queued limits
ai-policy publish policy.yaml --reason "x"   # operator only; file = {limits: {...}}; --command-id to retry
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_ai_policy_operator.py tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_mmr_cli_experiment.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/principals.py trader/automation/ai_paper_actions.py trader/automation/ai_policy_file.py trader/sdk.py trader/mmr_cli.py tests/test_rpc_acl.py tests/test_ai_paper_rpc.py tests/test_ai_policy_operator.py docs/CLI_REFERENCE.md
git commit -m "feat: let the operator publish the initial ai risk policy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Docs and full suite

**Files:**
- Modify: `AGENTS.md` (Architecture paragraph on typed RPC: one sentence)
- Modify: `docs/OPERATIONAL_STATE.md` (SP1 acceptance runbook line)

**Interfaces:** none new.

- [ ] **Step 1: Docs**

`AGENTS.md`, after "Command authority comes from the verified principal, never the body.":

> The `ai_supervisor` principal also signs a trader-granted `controller_epoch` in the envelope (`grant_ai_controller_epoch`); the trader refuses its decisions and controller reads with a missing or stale epoch.

`docs/OPERATIONAL_STATE.md`, in the SP1 acceptance runbook entry:

> The acceptance harness holds its own controller epoch (lease 60 s). Stop the `ai` service before an acceptance run, or the harness waits on `CONTROLLER_EPOCH_HELD` and then fails.

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`
Expected: all pass. A failure in a test that builds an `ai_paper`-enabled stack without `duckdb_path`, or submits a decision without an epoch, is a missed ripple of Task 4 or Task 7: fix the fixture in the same way (grant an epoch / set `duckdb_path`), never by relaxing the check.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md docs/OPERATIONAL_STATE.md
git commit -m "docs: describe the controller epoch and the acceptance lease

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review

- **Spec coverage.** 5.1: grant (Tasks 1, 3), envelope and signature (Task 2), required on admission and checked in the claim transaction (Task 4), controller reads carry it (Tasks 5, 7), successor reconciles by id and body (Task 4 `test_stale_holder_resend_is_refused_and_successor_replays`), accepted work continues through takeover (Task 4 `test_an_admitted_command_survives_a_later_takeover`). 6.1: durable record, monotonic cursor, retention, `ai_supervisor`-only read, gap (Tasks 6, 7). 6.2 / 6.2b: Tasks 1–4. 6.7: Task 8; "never publishes on startup" is Plan 5's test (Ruling 15). Spec 12 "Signed epoch": missing epoch and altered field (Tasks 2, 4). "Method restrictions through signed typed RPC, including cross-principal calls": Tasks 3, 5, 7, 8.
- **Index names.** `grant_ai_controller_epoch`, `read_ai_signals`, `publish_ai_risk_policy`, `submit_ai_paper_decision`, `TypedRpcRequest.controller_epoch: Optional[int] = None`, the four epoch codes, grant and signal shapes, migration 90: as in the index.
- **Review Focus → tests:** 1 → Task 4; 2 → Task 4; 3 → Task 2; 4 → Task 6; 5 → Task 1.
