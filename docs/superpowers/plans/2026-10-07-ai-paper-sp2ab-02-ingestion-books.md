# AI Paper SP2 — Plan 2: Trader: cost and simulation ingestion, separate baseline books, simulator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the `ai` service hand the trader its model costs and its hypothetical baseline decisions through two idempotent, signed commands. The trader then computes each baseline outcome itself from 1-minute bars at session end. The scoreboard shows one separate book per baseline id and cohort, never summed, and every cost with a status label.

**Architecture:** All code lives in the trader's `trader/scoreboard/` package and its journal DuckDB file. Two direct typed-RPC command handlers (`record_ai_cost`, `record_simulated_decision`, `ai_supervisor` only) validate the experiment and decision links, then write sealed rows through one new store method that compares a body digest inside the write transaction (exact duplicate → `DUPLICATE`, different body → refusal). Corrections are new sealed rows, never edits. A new `SessionSimulator` step on the existing 30 s scoreboard tick reads 1-minute bars (local history DuckDB first, then the trader's Alpaca history provider) and writes one sealed outcome per simulated decision. The report groups decisions and outcomes into books at read time. CLI, Telegram text and the dashboard script render the new shape.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, pandas (bars), pytest, Node (the existing dashboard script test). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md` (binding sections 6.3, 6.8, 7; also 3, 12 "Baseline books"). Index: `docs/superpowers/plans/2026-10-07-ai-paper-sp2ab-00-index.md`. SP1 spec section 5.2 (scoreboard) is amended as the spec says.

## Global Constraints

- **Base:** master after SP1 Plans 3–6. Code read at `/private/tmp/sp1-impl6`.
- **Journal migrations:** this plan uses **95** (`simulated_decisions`) and **96** (`simulated_outcomes`), appended to `MIGRATIONS` in `trader/scoreboard/schema.py`. 97–99 stay unused. `ai_costs` is edited in place inside migration 63; `simulated_books` is deleted from migration 63. No ALTER, no backfill, no old-row compatibility (owner: there is no legacy data).
- **Principals:** `record_ai_cost` and `record_simulated_decision` are `("command", …)` entries for `frozenset({"ai_supervisor"})` only. `cli`, `dashboard`, `ai_research` get `PERMISSION_DENIED`. Reads of the report stay `cli`, `dashboard`, `ai_supervisor`.
- **No controller epoch** on these two commands. They record facts; a late or stale controller's facts are still facts. Idempotency rests on stable ids and the body digest, not on leadership.
- **No command ledger.** Direct handlers, like `record_state_acknowledged`. A refusal is a normal reply body (`status: "REFUSED"`), never a raised error (a raised error becomes a scrubbed `INTERNAL_ERROR`).
- **Wire models:** `ConfigDict(extra="forbid", strict=True)`. Ids are regex-checked. Times are ISO-8601 text with an offset; naive times are refused. `True` is never an int.
- **Sealed rows:** `ai_costs`, `simulated_decisions`, `simulated_outcomes` are in `SEALED_TABLES`. They are insert-only and covered by `verify_seals`. Server-time columns (`recorded_at`, `computed_at`) are never part of `body_digest`.
- **DuckDB access:** only through `ScoreboardStore` / `DuckDBConnection.transaction`. Inside a transaction callback never call `db.execute` or `db.transaction` (non-reentrant lock). Warm `store.columns(table)` before opening the transaction.
- **Bars:** the price-history DuckDB is read only. Nothing in this plan writes, migrates or backfills it.
- **Never print secrets.** Source errors are stored as a class name and a short code, never as a URL, header or key.
- **Fail loudly:** missing bars, bad bars, unresolvable conids and missing keys produce an `INCOMPLETE` outcome with a reason. Nothing is filled in.
- Test-first. Per task run only the listed tests. Full suite once, in Task 8: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`.
- Commit subjects `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order, deploy or push is authorized by this plan.

## Rulings (spec silent, or the code forces a choice)

1. **Extend or add tables.** `ai_costs` is rewritten in place (its old shape had no status, role, attempt or correction link). `simulated_books` is **deleted**: its one-row-per-session sum is exactly what spec 6.8 forbids. Two new sealed tables hold the facts: `simulated_decisions` (what the `ai` service sent) and `simulated_outcomes` (what the trader computed). A *book* is the group of rows with one `(experiment_id, baseline_id, cohort)`; the report builds it at read time, so there is no stored total that can go stale. Both new tables carry `baseline_id` and `cohort` and an index on `(experiment_id, baseline_id, cohort)`. *If wrong:* a stored per-book summary would be needed for very large histories; the grouping query is cheap at paper volume.
2. **Idempotency.** `record_id` is the stable key, chosen by the `ai` service. The trader hashes the client body (`body_digest`, times normalized to UTC) and compares it inside the write transaction. Same id and same digest → `DUPLICATE` (nothing written, no new seal). Same id and a different digest → refused `CONFLICTING_DUPLICATE`. Two more conflicts: a second *original* cost for the same `(experiment_id, attempt_id)`, and a second simulated decision for the same `(experiment_id, baseline_id, opportunity_id)` under a different record id. *If wrong:* Plan 6 must put several records under one opportunity; then the opportunity id must be extended (see Cross-plan additions).
3. **Cost status.** `confirmed` (provider usage seen), `estimated` (computed from tokens and the configured price), `unknown` (outcome not known; `cost_usd` must be null). `unknown` ⇔ null cost, enforced in the wire model and by a table CHECK. A null cost is never 0.
4. **Corrections.** A correction is a new `record_id` with `corrects_record_id` set. It must point at an *original* (not a correction) of the same experiment, repeat its identity (`role`, `provider`, `model`, `attempt_id`, `called_at`), and may not lower the status rank (`unknown` < `estimated` < `confirmed`). Equal rank with a new number is allowed. The trader assigns `correction_seq` (1, 2, …) inside the transaction; the report uses the highest sequence per original, and the call is counted once. All rows stay. *If wrong:* a provider invoice that lowers a confirmed number later is refused and needs an operator-visible incident; accepted cost of never silently rewriting a confirmed number.
5. **Link validation.** `experiment_id` must exist. A simulated `decided_at` must lie inside `[started_at, stopped_at]`. A cost `called_at` must be at or after `started_at` but may be **after** `stopped_at`: a model call in flight at the stop still costs money, and spec 7 says costs are never lost. `decision_id` (costs) and `linked_decision_id` (simulated) are optional. When given, the trader must know the decision (`ai_paper_decisions`), for the same account, received at or after the experiment start. A simulated link must also be an `ENTER` on the same conid. An unknown decision is refused `DECISION_LINK_UNKNOWN` with `retryable: true`. A Jev `SKIP` never creates a trader decision, so the `ai` service sends `decision_id` only for decisions it really submitted; other costs use `served_kind` / `served_id`.
6. **Side.** `BUY` only. Spec 13 puts short styles out of scope. A `SELL` is refused by the wire model. *If wrong:* a short style later needs mirrored bracket rules and a new baseline version.
7. **Quantity** is a whole number of shares (≥ 1). **Prices** are positive finite floats with `stop < reference < target`.
8. **Entry window.** For baselines that trade, `decided_at` must be inside an XNYS session and strictly before that session's flatten start (`SessionSchedule.flatten_start_utc`, 15:45 ET, or 12:45 ET on an early close). `no_trade.v1` only needs to lie inside the experiment window.
9. **Allowed baselines.** `follow_signal.v1` / `strategy_signal`, `fixed_rule.v1` / `self_found`, `no_trade.v1` / `self_found`, `matched_entry_bracket_exit.v1` / `model_close`. Other ids or pairings are refused. A new baseline version is a one-line change in `BASELINES` (`trader/scoreboard/ingest.py`).
10. **Bracket semantics (long only).** Entry fills at the reference price at `decided_at`. Only bars that start **after the minute containing `decided_at`** are scanned (no look-ahead). For each bar in order: if `low <= stop`, exit at `min(stop, bar.open)` (a gap through the stop fills at the open) with kind `STOP`; else if `high >= target`, exit at `target` with kind `TARGET`. **If one bar touches both, the stop wins** (conservative). Bars starting at or after the flatten start are not scanned; if nothing hit, exit at the **open of the first bar that starts in `[flatten_start, flatten_start + 10 min)`**, kind `FLATTEN`. The exit time of a `STOP`/`TARGET` is the bar's start. *If wrong:* results shift by about one bar of price; the rule is the same for every baseline, so books stay comparable.
11. **What counts as missing data.** Alpaca emits no bar for a minute with no trades, so a missing minute alone is not a gap. A record is `INCOMPLETE` when: a used bar is invalid (`BAD_BAR`: non-finite, non-positive, `high < low`, open or close outside the range) or repeated (`DUPLICATE_BAR`); two bars (or the entry and the first bar, or the last bar and the flatten bar) are more than **30 minutes** apart (`BAR_GAP`); there is no flatten bar (`NO_FLATTEN_BAR`); no source returned bars (`NO_BARS`); or no source is configured (`NO_BAR_SOURCE`). *If wrong:* a genuinely quiet stock shows as incomplete instead of complete. That is the safe side.
12. **P&L is gross.** No commission and no slippage is modeled. The book carries `pnl_basis: "gross, no commissions or slippage"` and the dashboard and CLI print it. Baselines therefore look slightly better than the real book. *If wrong:* a fee model can be added later as a new outcome field; old outcomes keep their basis label.
13. **When the simulator runs.** A session's bars are ready at **20:16 ET** (the Alpaca provider's own completed-session rule, `SESSION_COMPLETE_ET`). From then each pending record is tried on every tick (at most once per 5 minutes per record). The first source that gives a `COMPLETE` result wins (local DuckDB, then Alpaca). If no source completes, the record keeps being retried until **2 hours** after ready time; then a sealed `INCOMPLETE` outcome with the combined reasons is written. An outcome is final: sealed rows are never edited, so an operator who backfills bars later must accept that this record stays incomplete. *If wrong:* a longer data outage burns records to `INCOMPLETE`; raise `GRACE`.
14. **`no_trade.v1`.** The decision row and a `COMPLETE` outcome (`pnl_usd = 0.0`, `trades = 0`, `bar_source = "none"`) are written in one transaction at ingestion. It never waits for bars.
15. **Book status.** `COMPLETE` (every record has a complete outcome), `INCOMPLETE` (at least one incomplete outcome), `PENDING` (no incomplete outcome, but at least one record has no outcome yet). `pnl_usd` is the sum only for a `COMPLETE` book, else `null`. `known_pnl_usd` is the sum over the complete records, labelled partial in every display. A bad book never hides another book.
16. **Costs in the report.** Keep the SP1 keys (`ai_cost_usd`, `ai_calls`, `ai_costs_status`, `pnl_minus_ai_cost_usd`) with their SP1 meaning: `ai_cost_usd` is `null` while any call is `unknown`. Add `ai_cost` with `status` (`NONE`, `CONFIRMED`, `ESTIMATED`, `INCOMPLETE`), `confirmed_usd`, `estimated_usd`, `unknown_calls`, `corrections`. `pnl_minus_ai_cost_usd` counts estimated cost as cost and is `null` while any call is unknown.
17. **Alpaca keys on the trader.** `Trader.__init__` gets `alpaca_api_key_id` and `alpaca_api_secret_key` (default `''`). `Container.resolve` fills them from the configuration or the `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` env that `docker-compose.yml` already passes. Blank keys → no Alpaca source, one WARNING at start, records end `INCOMPLETE` (`NO_BAR_SOURCE`) if the local DuckDB has no bars. The conid is mapped to a ticker only through `trader.universe_accessor.resolve_symbol(conid, first_only=True)`, and only for `STK` in `USD`. Anything else is `UNSUPPORTED_INSTRUMENT`.

## Cross-plan additions

Plan 5 (outbox) and Plan 6 (baselines, acceptance) use these exact names.

**RPC `record_ai_cost`** (command, `ai_supervisor`). Request (strict, no extra keys):
`record_id: str` (`^[A-Za-z0-9_-]{8,96}$`), `experiment_id: str` (`^exp-[0-9a-f]{20}$`), `role: "orchestrator"|"jev"|"research"`, `provider: str`, `model: str` (`^[A-Za-z0-9_./:@+-]{1,128}$`), `attempt_id: str` (same shape as `record_id`), `input_tokens: Optional[int]`, `output_tokens: Optional[int]` (≥ 0), `cost_usd: Optional[float]` (≥ 0, finite), `cost_status: "confirmed"|"estimated"|"unknown"` (`unknown` ⇔ `cost_usd` is null), `called_at: str` (ISO-8601 with offset), `served_kind: "decision"|"cycle"|"signal"|"research"`, `served_id: str` (`^[A-Za-z0-9_.:-]{1,128}$`), `decision_id: Optional[str]` (`^[A-Za-z0-9_-]{8,64}$`), `corrects_record_id: Optional[str]`.

**RPC `record_simulated_decision`** (command, `ai_supervisor`). Request (strict):
`record_id: str`, `experiment_id: str`, `baseline_id: str`, `cohort: str`, `opportunity_id: str` (`^[A-Za-z0-9_.:-]{1,128}$`), `conid: Optional[int]` (> 0), `side: Optional["BUY"]`, `quantity: Optional[int]` (≥ 1), `reference_price`, `stop_price`, `target_price: Optional[float]` (> 0), `decided_at: str`, `linked_decision_id: Optional[str]`. For every baseline except `no_trade.v1` all of `conid`, `side`, `quantity`, `reference_price`, `stop_price`, `target_price` are required. For `no_trade.v1`, `side`, `quantity` and the three prices must be null; `conid` is optional.

**Response of both** (always a dict, never an RPC error for a business refusal):
`{"status": "INSERTED" | "DUPLICATE" | "REFUSED", "record_id": str, "code": Optional[str], "detail": Optional[str], "retryable": bool}`. `INSERTED` and `DUPLICATE` are both success: the outbox marks the item delivered. A `REFUSED` with `retryable: true` is retried; with `false` it is dead-lettered and shown.

**Refusal codes.** Not retryable: `EXPERIMENT_UNKNOWN`, `CALL_OUTSIDE_EXPERIMENT` (before the start only), `DECIDED_OUTSIDE_EXPERIMENT`, `UNKNOWN_BASELINE`, `COHORT_NOT_ALLOWED`, `DECIDED_OUTSIDE_ENTRY_WINDOW`, `DECISION_LINK_WRONG_ACCOUNT`, `DECISION_LINK_OUTSIDE_EXPERIMENT`, `DECISION_LINK_CONID_MISMATCH`, `DECISION_LINK_NOT_ENTER`, `CORRECTION_OF_CORRECTION`, `CORRECTION_IDENTITY_MISMATCH`, `CORRECTION_DOWNGRADE`, `CONFLICTING_DUPLICATE`. Retryable: `DECISION_LINK_UNKNOWN`, `CORRECTION_TARGET_UNKNOWN`.

**Plan 6 constraints.** At most one simulated decision per `(experiment_id, baseline_id, opportunity_id)`. `matched_entry_bracket_exit.v1` records the **entry**: `decided_at` is the entry time, `reference_price` the entry fill price, `quantity` the entry quantity, `linked_decision_id` the real `ENTER` decision. `follow_signal.v1`'s `opportunity_id` should come from the signal's `source_event_id`. A `decision_id` on a cost is allowed only for a decision the trader has accepted.

**Report shape** (`get_scoreboard`; Plan 6 acceptance reads it): `benchmarks.books: list[{baseline_id, cohort, label: "simulated", pnl_basis, status: "COMPLETE"|"INCOMPLETE"|"PENDING", records, complete, incomplete, pending, trades, pnl_usd, known_pnl_usd, incomplete_reasons: {reason: count}}]`, sorted by `(baseline_id, cohort)`; `benchmarks.ai_cost: {status, calls, confirmed_usd, estimated_usd, unknown_calls, corrections, total_usd}`. The SP1 key `benchmarks.simulated` is **removed**.

**Trader attributes.** `Trader.alpaca_api_key_id: str`, `Trader.alpaca_api_secret_key: str` (Ruling 17). Plan 3's discovery read must reuse these names, not add a second pair.

## Review Focus

1. **A retry that differs only in server time or time spelling** (`Z` vs `+00:00`) must be a `DUPLICATE`, never a conflict. → Task 2 `test_redelivery_with_a_different_time_spelling_is_a_duplicate`, Task 1 `test_ingest_duplicate_ignores_server_time_columns`.
2. **One bar touches both stop and target.** The stop must win, and a gap through the stop fills at the open. → Task 4 `test_same_bar_stop_and_target_is_a_stop`, `test_gap_through_the_stop_fills_at_the_open`.
3. **Missing flatten bar, a 30-minute hole, an early-close day.** Each gives an `INCOMPLETE` record and an `INCOMPLETE` book; the other books stay complete. → Task 4 `test_missing_flatten_bar_is_incomplete`, `test_a_thirty_minute_hole_is_incomplete`; Task 5 `test_early_close_uses_the_early_flatten_start`; Task 8 `test_incomplete_book_does_not_hide_complete_books`.
4. **A cost correction after a confirmed cost.** The total changes once; a downgrade and a correction of a correction are refused; the call count does not grow. → Task 2 `test_correction_replaces_the_cost_once`, `test_correction_cannot_lower_the_status`; Task 6 `test_correction_is_counted_once_and_status_labelled`.
5. **Principal rights through the real signed server.** `cli`, `dashboard` and `ai_research` are denied both commands; `ai_supervisor` can call them; a conflicting duplicate comes back as `REFUSED`, not as an internal error. → Task 3 `test_only_ai_supervisor_may_ingest`, `test_a_conflict_is_a_refused_reply_not_an_rpc_error`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/scoreboard/schema.py`, `trader/scoreboard/store.py`, `trader/data/schema_migrations.py` (docstring) | tables, idempotent sealed ingest | 1 |
| `trader/scoreboard/ingest_models.py`, `trader/scoreboard/ingest.py`, `trader/scoreboard/ports.py` | wire models, validation, `AiIngest`, decision facts port | 2 |
| `trader/messaging/ai_ingest_surface.py`, `trader/messaging/principals.py`, `trader/messaging/production_api.py`, `trader/scoreboard/wiring.py`, `trader/trading/command_stack.py` | RPC registration, ACL, wiring | 3 |
| `trader/scoreboard/simulator.py` | pure bracket simulation | 4 |
| `trader/scoreboard/bar_sources.py`, `trader/scoreboard/session_simulator.py`, `trader/trading/trading_runtime.py`, `trader/scoreboard/wiring.py` | bar sources, runner, tick step, Alpaca keys | 5 |
| `trader/scoreboard/books.py`, `trader/scoreboard/report.py`, `trader/scoreboard/service.py`, `trader/scoreboard/summary_text.py` | books, costs, readback | 6 |
| `trader/mmr_cli.py`, `web/static/command_center_scoreboard.js` and `.test.js`, docs | CLI and dashboard display | 7 |
| tests, full suite | acceptance and suite | 8 |

---

### Task 1: Tables and the idempotent sealed ingest

**Files:**
- Modify: `trader/scoreboard/schema.py` (migration 63 edited; 95, 96 added)
- Modify: `trader/scoreboard/store.py` (`SEALED_TABLES`; new `ingest_sealed_many`; refactor of `insert_sealed_many`)
- Modify: `trader/data/schema_migrations.py` (docstring: "SP2 Plan 2 uses 95 (simulated decisions) and 96 (simulated outcomes)")
- Delete: `trader/scoreboard/inputs.py`, `tests/scoreboard/test_inputs.py` (replaced by Task 2)
- Modify tests: `tests/scoreboard/test_store.py` (the raw `ai_costs` insert), `tests/scoreboard/test_wiring.py` (table list)
- Test: `tests/scoreboard/test_ingest_store.py` (new)

**Interfaces:**
- Consumes: `ScoreboardStore`, `SchemaMigrator`.
- Produces:
  - `class IngestRefused(Exception)`: `__init__(self, code: str, detail: str, *, retryable: bool = False)`; attributes `.code`, `.detail`, `.retryable`.
  - `ScoreboardStore.ingest_sealed_many(items: Sequence[tuple[str, Mapping[str, Any]]], *, extend: Optional[Callable[[Any], Mapping[str, Any]]] = None) -> str` returning `"INSERTED"` or `"DUPLICATE"`. The first item is the primary row; its table has a `body_digest` column. `extend(conn)` runs inside the transaction, may raise `IngestRefused` or `ScoreboardConflict`, and returns extra columns merged into the primary row.
  - Tables `ai_costs` (new shape), `simulated_decisions`, `simulated_outcomes`; `simulated_books` is gone.

Table definitions. In `_BOOKS_COSTS` (rename the tuple `_COSTS`; migration 63 name `scoreboard_costs`) replace both statements with:

```python
_COSTS = (
    """CREATE TABLE IF NOT EXISTS ai_costs (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        role VARCHAR NOT NULL CHECK (role IN ('orchestrator','jev','research')),
        provider VARCHAR NOT NULL, model VARCHAR NOT NULL, attempt_id VARCHAR NOT NULL,
        input_tokens BIGINT, output_tokens BIGINT, cost_usd DOUBLE,
        cost_status VARCHAR NOT NULL CHECK (cost_status IN ('confirmed','estimated','unknown')),
        called_at TIMESTAMPTZ NOT NULL, served_kind VARCHAR NOT NULL, served_id VARCHAR NOT NULL,
        decision_id VARCHAR, corrects_record_id VARCHAR, correction_seq INTEGER NOT NULL,
        body_digest VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
        CHECK ((cost_status = 'unknown') = (cost_usd IS NULL)))""",
)

_SIMULATED = (
    """CREATE TABLE IF NOT EXISTS simulated_decisions (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        baseline_id VARCHAR NOT NULL, cohort VARCHAR NOT NULL, opportunity_id VARCHAR NOT NULL,
        conid BIGINT, side VARCHAR CHECK (side IS NULL OR side = 'BUY'), quantity BIGINT,
        reference_price DOUBLE, stop_price DOUBLE, target_price DOUBLE,
        decided_at TIMESTAMPTZ NOT NULL, session_date DATE NOT NULL, linked_decision_id VARCHAR,
        body_digest VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_simulated_decisions_book "
    "ON simulated_decisions(experiment_id, baseline_id, cohort)",
)

_OUTCOMES = (
    """CREATE TABLE IF NOT EXISTS simulated_outcomes (
        record_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
        baseline_id VARCHAR NOT NULL, cohort VARCHAR NOT NULL, session_date DATE NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('COMPLETE','INCOMPLETE')), reason VARCHAR,
        exit_kind VARCHAR NOT NULL CHECK (exit_kind IN ('STOP','TARGET','FLATTEN','NONE')),
        exit_at TIMESTAMPTZ, exit_price DOUBLE, pnl_usd DOUBLE, trades INTEGER,
        bar_source VARCHAR NOT NULL, bars_digest VARCHAR, computed_at TIMESTAMPTZ NOT NULL,
        CHECK ((status = 'COMPLETE') = (pnl_usd IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS idx_simulated_outcomes_book "
    "ON simulated_outcomes(experiment_id, baseline_id, cohort)",
)

MIGRATIONS = (
    (60, "scoreboard_core", _CORE),
    (61, "scoreboard_round_trips", _ROUND_TRIPS),
    (62, "scoreboard_benchmark", _BENCHMARK),
    (63, "scoreboard_costs", _COSTS),
    (64, "scoreboard_outbox", _OUTBOX),
    (95, "sp2_simulated_decisions", _SIMULATED),
    (96, "sp2_simulated_outcomes", _OUTCOMES),
)
```

Update the module docstring ("Journal migrations 60-64 …; SP2 Plan 2 adds 95-96") and `apply_scoreboard_migrations`'s docstring ("True when any migration of this module was newly applied").

In `store.py`: `SEALED_TABLES` loses `"simulated_books"` and gains `"simulated_decisions": ("record_id",)`, `"simulated_outcomes": ("record_id",)`; `"ai_costs": ("record_id",)`.

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/test_ingest_store.py`)

```python
import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID, NOW
from trader.scoreboard.store import IngestRefused, ScoreboardConflict


def cost_row(**changes):
    row = {"record_id": "cost-0000001", "experiment_id": EXP_ID, "role": "jev", "provider": "openrouter",
           "model": "m1", "attempt_id": "att-0000001", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5,
           "cost_status": "confirmed", "called_at": NOW, "served_kind": "cycle", "served_id": "cycle-1",
           "decision_id": None, "corrects_record_id": None, "correction_seq": 0, "body_digest": "a" * 64,
           "recorded_at": NOW}
    row.update(changes)
    return row


def test_migrations_95_and_96_create_the_tables_and_drop_simulated_books(db, store):
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"simulated_decisions", "simulated_outcomes", "ai_costs"} <= tables
    assert "simulated_books" not in tables
    assert {95, 96} <= {r[0] for r in db.execute("SELECT version FROM schema_migrations", fetch="all")}


def test_ingest_inserts_once_and_seals_the_row(store):
    assert store.ingest_sealed_many([("ai_costs", cost_row())]) == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_usd"] == 0.5 and store.verify_seals() == []


def test_ingest_duplicate_ignores_server_time_columns(store):
    store.ingest_sealed_many([("ai_costs", cost_row())])
    later = NOW + dt.timedelta(hours=3)
    assert store.ingest_sealed_many([("ai_costs", cost_row(recorded_at=later))]) == "DUPLICATE"
    assert len(store.fetch("ai_costs", {})) == 1 and store.seal_count() == 1


def test_ingest_with_a_different_digest_is_a_conflict(store):
    store.ingest_sealed_many([("ai_costs", cost_row())])
    with pytest.raises(ScoreboardConflict):
        store.ingest_sealed_many([("ai_costs", cost_row(body_digest="b" * 64, cost_usd=9.0))])
    assert store.fetch("ai_costs", {})[0]["cost_usd"] == 0.5


def test_extend_runs_inside_the_transaction_and_its_refusal_writes_nothing(store):
    def refuse(conn):
        raise IngestRefused("NOPE", "no", retryable=True)
    with pytest.raises(IngestRefused) as exc:
        store.ingest_sealed_many([("ai_costs", cost_row())], extend=refuse)
    assert exc.value.retryable is True and store.fetch("ai_costs", {}) == [] and store.seal_count() == 0


def test_extend_columns_are_merged_into_the_primary_row(store):
    store.ingest_sealed_many([("ai_costs", cost_row(correction_seq=0))], extend=lambda conn: {"correction_seq": 4})
    assert store.fetch("ai_costs", {})[0]["correction_seq"] == 4


def test_a_status_without_a_cost_is_refused_by_the_table(store):
    with pytest.raises(Exception, match="(?i)constraint"):
        store.ingest_sealed_many([("ai_costs", cost_row(cost_status="unknown"))])
    with pytest.raises(Exception, match="(?i)constraint"):
        store.ingest_sealed_many([("ai_costs", cost_row(record_id="cost-0000002", cost_usd=None))])


def test_primary_and_extra_rows_are_written_together_or_not_at_all(store):
    outcome = {"record_id": "sim-0000001", "experiment_id": EXP_ID, "baseline_id": "no_trade.v1",
               "cohort": "self_found", "session_date": dt.date(2026, 10, 6), "status": "COMPLETE", "reason": None,
               "exit_kind": "NONE", "exit_at": None, "exit_price": None, "pnl_usd": 0.0, "trades": 0,
               "bar_source": "none", "bars_digest": None, "computed_at": NOW}
    def conflict(conn):
        raise ScoreboardConflict("x")
    with pytest.raises(ScoreboardConflict):
        store.ingest_sealed_many([("ai_costs", cost_row()), ("simulated_outcomes", outcome)], extend=conflict)
    assert store.fetch("simulated_outcomes", {}) == [] and store.seal_count() == 0
```

- [ ] **Step 2: Run, expect failure**
`.venv/bin/python -m pytest tests/scoreboard/test_ingest_store.py -q --timeout=30` → fails (`ImportError: IngestRefused`, missing tables).

- [ ] **Step 3: Implement.** Apply the schema edits above. Delete `inputs.py` and `test_inputs.py`. In `store.py` add `IngestRefused` next to `ScoreboardConflict`, and split `insert_sealed_many` into two helpers that it and the new method share:

```python
class IngestRefused(Exception):
    """An ingestion command was understood but refused; ``code`` is the stable reason."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.retryable = code, detail, retryable
```

```python
    def _prepare_items(self, items):
        prepared = []
        for table, row in items:
            if table not in SEALED_TABLES:
                raise ValueError(f"{table!r} is not a sealed scoreboard table")
            values = self.prepare(table, row)
            prepared.append((table, values, row_key(values, SEALED_TABLES[table]), row_digest(values)))
        return prepared

    @staticmethod
    def _insert_prepared_in_tx(conn, prepared, sealed_at) -> None:
        """The former body of ``insert_sealed_many``'s transaction, unchanged."""
        last = conn.execute(
            "SELECT seal_id, chain FROM scoreboard_seals ORDER BY seal_id DESC LIMIT 1").fetchone()
        seal_id, prev = (0, GENESIS) if last is None else (int(last[0]), last[1])
        for table, values, key, digest in prepared:
            key_columns = SEALED_TABLES[table]
            where = " AND ".join(f"{column} = ?" for column in key_columns)
            if conn.execute(f"SELECT 1 FROM {table} WHERE {where}",
                            [values[c] for c in key_columns]).fetchone() is not None:
                raise ScoreboardConflict(f"{table} row {key} exists already")
            names = list(values)
            conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                         [values[name] for name in names])
            seal_id, chain = seal_id + 1, chain_digest(prev, table, key, digest)
            conn.execute(
                "INSERT INTO scoreboard_seals (seal_id, table_name, row_key, row_digest, prev_chain, chain, "
                "sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", [seal_id, table, key, digest, prev, chain, sealed_at])
            prev = chain

    def insert_sealed_many(self, items):
        prepared = self._prepare_items(items)
        sealed_at = self._now()
        self._db.transaction(lambda conn: self._insert_prepared_in_tx(conn, prepared, sealed_at))
```

```python
    def ingest_sealed_many(self, items, *, extend=None) -> str:
        """Insert-or-recognise: the first item is the primary row and carries ``body_digest``.

        Inside one transaction: a row with the same key and the same digest is a DUPLICATE (nothing is
        written); the same key with another digest raises ``ScoreboardConflict``. Otherwise ``extend`` may
        check other rows and add columns, then every row and its seal are written together.
        """
        table, row = items[0]
        if table not in SEALED_TABLES:
            raise ValueError(f"{table!r} is not a sealed scoreboard table")
        key_columns = SEALED_TABLES[table]
        for name, _row in items:
            self.columns(name)      # warm the cache: inside the transaction the database lock is held
        sealed_at = self._now()
        where = " AND ".join(f"{column} = ?" for column in key_columns)

        def tx(conn):
            found = conn.execute(f"SELECT body_digest FROM {table} WHERE {where}",
                                 [row[c] for c in key_columns]).fetchone()
            if found is not None:
                if found[0] == row["body_digest"]:
                    return "DUPLICATE"
                raise ScoreboardConflict(f"{table} row {row_key(row, key_columns)} exists with a different body")
            extra = {} if extend is None else dict(extend(conn))
            prepared = self._prepare_items([(table, {**row, **extra}), *items[1:]])
            self._insert_prepared_in_tx(conn, prepared, sealed_at)
            return "INSERTED"
        return self._db.transaction(tx)
```

Fix the two existing tests: in `test_store.py` replace the raw insert with `db.execute("INSERT INTO ai_costs (record_id, experiment_id, role, provider, model, attempt_id, cost_usd, cost_status, called_at, served_kind, served_id, correction_seq, body_digest, recorded_at) VALUES ('c1', 'e', 'jev', 'p', 'm', 'a1', 0.1, 'confirmed', now(), 'cycle', 'j1', 0, 'x', now())")`. In `test_wiring.py` replace `"simulated_books"` with `"simulated_decisions", "simulated_outcomes"` in the table list and `{60, 61, 62, 63, 64}` with `{60, 61, 62, 63, 64, 95, 96}`. In `test_verify.py` and `test_surface.py` the references are rewritten in Tasks 2 and 3.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard/test_ingest_store.py tests/scoreboard/test_store.py tests/scoreboard/test_wiring.py -q --timeout=30` → pass. (`test_verify.py` and `test_report.py` are fixed in later tasks; do not run them yet.)
- [ ] **Step 5: Commit** `feat: add sealed ingest tables and an idempotent sealed insert` (trailer).

---

### Task 2: Wire models and the ingestion service

**Files:**
- Create: `trader/scoreboard/ingest_models.py`, `trader/scoreboard/ingest.py`
- Modify: `trader/scoreboard/ports.py` (decision facts port)
- Test: `tests/scoreboard/ingest_world.py` (helpers), `tests/scoreboard/test_ingest.py`
- Modify test: `tests/scoreboard/test_verify.py`

**Interfaces:**
- Consumes: `ScoreboardStore.ingest_sealed_many`, `IngestRefused`, `ScoreboardConflict`, an experiment reader (`get(experiment_id)` → object with `experiment_id, account_id, started_at, stopped_at`), `XNYSCalendarPolicy.resolve`.
- Produces:
  - `RecordAiCostRequest`, `RecordSimulatedDecisionRequest` (pydantic, fields in Cross-plan additions); `parse_utc(text: str) -> datetime`; `body_digest(payload: Mapping[str, Any]) -> str`.
  - `BASELINES: Mapping[str, str]`; `STATUS_RANK: Mapping[str, int]`.
  - `class AiIngest(store, experiments, decisions, calendar, now)` with `record_cost(req) -> dict` and `record_simulated(req) -> dict` (response shape in Cross-plan additions).
  - In `ports.py`: `@dataclass(frozen=True) DecisionFact(decision_id: str, account_id: str, conid: Optional[int], action: Optional[str], received_at: dt.datetime)`; `class DecisionFacts(Protocol): def get(self, decision_id: str) -> Optional[DecisionFact]`; `NullDecisionFacts` (always `None`); `DecisionStoreFacts(decision_store)` adapting `AiPaperDecisionStore.row(decision_id)` (returns `None` when the row is missing or has no `decision_id`).

`ingest_models.py` holds the two strict models (`ConfigDict(extra="forbid", strict=True)`, fields and regexes exactly as in Cross-plan additions), `parse_utc`, and `body_digest`. Every id field gets a `field_validator` that applies its regex; both `called_at` / `decided_at` validators call `parse_utc` (naive → `ValueError`). The load-bearing parts, in full:

```python
def parse_utc(text: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("a time needs an offset, for example 2026-10-06T14:30:00+00:00")
    return moment.astimezone(dt.timezone.utc)


def body_digest(payload: Mapping[str, Any]) -> str:
    """Over client fields only, with times normalized to UTC, so a retry spelled differently still matches."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def normalized(self, *time_fields: str) -> dict:
        payload = self.model_dump(mode="json")
        for name in time_fields:
            payload[name] = parse_utc(payload[name]).isoformat()
        return payload
```

```python
# RecordAiCostRequest
_Count = Annotated[int, Field(ge=0, le=10_000_000_000)]
_Usd = Annotated[float, Field(ge=0, allow_inf_nan=False)]

@model_validator(mode="after")
def _status_matches_cost(self):
    if (self.cost_status == "unknown") != (self.cost_usd is None):
        raise ValueError("cost_status 'unknown' needs a null cost_usd, and any other status needs a number")
    if self.corrects_record_id == self.record_id:
        raise ValueError("a record cannot correct itself")
    return self

def digest(self) -> str:
    return body_digest(self.normalized("called_at"))
```

```python
# RecordSimulatedDecisionRequest
_Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]    # quantity: Annotated[int, Field(ge=1, le=10_000_000)]

@model_validator(mode="after")
def _shape_by_baseline(self):
    trade = (self.conid, self.side, self.quantity, self.reference_price, self.stop_price, self.target_price)
    if self.baseline_id == "no_trade.v1":
        if any(v is not None for v in trade[1:]):
            raise ValueError("a no_trade record carries no side, quantity or prices")
        return self
    if any(v is None for v in trade):
        raise ValueError("a trading baseline needs conid, side, quantity and all three prices")
    if not self.stop_price < self.reference_price < self.target_price:
        raise ValueError("stop_price < reference_price < target_price is required for a BUY")
    return self

def digest(self) -> str:
    return body_digest(self.normalized("decided_at"))
```

All optional fields default to `None`; `side` is `Optional[Literal["BUY"]]`; `role`, `cost_status` and `served_kind` are `Literal`s.

`ingest.py` (full code):

```python
"""Idempotent ingestion of AI costs and simulated baseline decisions (spec 6.3, 6.8; SP2 Plan 2)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest, parse_utc
from trader.scoreboard.ports import session_date_et
from trader.scoreboard.store import IngestRefused, ScoreboardConflict, ScoreboardStore

logger = logging.getLogger(__name__)

BASELINES = {
    "follow_signal.v1": "strategy_signal",
    "fixed_rule.v1": "self_found",
    "no_trade.v1": "self_found",
    "matched_entry_bracket_exit.v1": "model_close",
}
STATUS_RANK = {"unknown": 0, "estimated": 1, "confirmed": 2}
NO_TRADE = "no_trade.v1"
BAR_SOURCE_NONE = "none"


def _reply(status: str, record_id: str, code: Optional[str] = None, detail: Optional[str] = None,
           retryable: bool = False) -> dict:
    return {"status": status, "record_id": record_id, "code": code, "detail": detail, "retryable": retryable}


class AiIngest:
    def __init__(self, *, store: ScoreboardStore, experiments: Any, decisions: Any, calendar: Any,
                 now: Callable[[], dt.datetime]):
        self._store = store
        self._experiments = experiments
        self._decisions = decisions
        self._calendar = calendar
        self._now = now

    # -- the two commands ----------------------------------------------------

    def record_cost(self, req: RecordAiCostRequest) -> dict:
        return self._guarded(req.record_id, lambda: self._cost(req))

    def record_simulated(self, req: RecordSimulatedDecisionRequest) -> dict:
        return self._guarded(req.record_id, lambda: self._simulated(req))

    @staticmethod
    def _guarded(record_id: str, work: Callable[[], str]) -> dict:
        try:
            return _reply(work(), record_id)
        except IngestRefused as refusal:
            return _reply("REFUSED", record_id, refusal.code, refusal.detail, refusal.retryable)
        except ScoreboardConflict as conflict:
            return _reply("REFUSED", record_id, "CONFLICTING_DUPLICATE", str(conflict))

    # -- links -----------------------------------------------------------------

    def _experiment(self, experiment_id: str) -> Any:
        experiment = self._experiments.get(experiment_id)
        if experiment is None:
            raise IngestRefused("EXPERIMENT_UNKNOWN", f"no experiment {experiment_id}")
        return experiment

    @staticmethod
    def _inside(experiment: Any, moment: dt.datetime, code: str, *, upper_bound: bool = True) -> None:
        stopped_at = getattr(experiment, "stopped_at", None)
        too_late = upper_bound and stopped_at is not None and moment > stopped_at
        if moment < experiment.started_at or too_late:
            raise IngestRefused(code, f"{moment.isoformat()} is outside experiment {experiment.experiment_id}")

    def _decision_link(self, decision_id: str, experiment: Any, *, conid: Optional[int], must_enter: bool) -> None:
        fact = self._decisions.get(decision_id)
        if fact is None:
            raise IngestRefused("DECISION_LINK_UNKNOWN", f"the trader has no decision {decision_id}", retryable=True)
        if fact.account_id != experiment.account_id:
            raise IngestRefused("DECISION_LINK_WRONG_ACCOUNT", f"decision {decision_id} is of another account")
        if fact.received_at < experiment.started_at:
            raise IngestRefused("DECISION_LINK_OUTSIDE_EXPERIMENT", f"decision {decision_id} predates the experiment")
        if must_enter and fact.action != "ENTER":
            raise IngestRefused("DECISION_LINK_NOT_ENTER", f"decision {decision_id} is a {fact.action}")
        if conid is not None and fact.conid != conid:
            raise IngestRefused("DECISION_LINK_CONID_MISMATCH", f"decision {decision_id} is on conid {fact.conid}")

    # -- costs -------------------------------------------------------------------

    def _cost(self, req: RecordAiCostRequest) -> str:
        experiment = self._experiment(req.experiment_id)
        called_at = parse_utc(req.called_at)
        self._inside(experiment, called_at, "CALL_OUTSIDE_EXPERIMENT", upper_bound=False)
        if req.decision_id is not None:
            self._decision_link(req.decision_id, experiment, conid=None, must_enter=False)
        row = {**req.model_dump(), "called_at": called_at, "correction_seq": 0, "body_digest": req.digest(),
               "recorded_at": self._now()}
        return self._store.ingest_sealed_many([("ai_costs", row)], extend=lambda conn: self._cost_checks(conn, req))

    @staticmethod
    def _cost_checks(conn, req: RecordAiCostRequest) -> dict:
        if req.corrects_record_id is None:
            clash = conn.execute(
                "SELECT record_id FROM ai_costs WHERE experiment_id = ? AND attempt_id = ? "
                "AND corrects_record_id IS NULL", [req.experiment_id, req.attempt_id]).fetchone()
            if clash is not None:
                raise ScoreboardConflict(f"attempt {req.attempt_id} already has the original cost {clash[0]}")
            return {"correction_seq": 0}
        target = req.corrects_record_id
        original = conn.execute(
            "SELECT experiment_id, role, provider, model, attempt_id, called_at, corrects_record_id "
            "FROM ai_costs WHERE record_id = ?", [target]).fetchone()
        if original is None:
            raise IngestRefused("CORRECTION_TARGET_UNKNOWN", f"no cost record {target}", retryable=True)
        if original[6] is not None:
            raise IngestRefused("CORRECTION_OF_CORRECTION", f"{target} is itself a correction")
        same = (req.experiment_id, req.role, req.provider, req.model, req.attempt_id, parse_utc(req.called_at))
        if tuple(original[:6]) != same:
            raise IngestRefused("CORRECTION_IDENTITY_MISMATCH", f"{target} is a different call")
        status, seq = conn.execute(
            "SELECT cost_status, correction_seq FROM ai_costs WHERE record_id = ? OR corrects_record_id = ? "
            "ORDER BY correction_seq DESC LIMIT 1", [target, target]).fetchone()
        if STATUS_RANK[req.cost_status] < STATUS_RANK[status]:
            raise IngestRefused("CORRECTION_DOWNGRADE", f"{target} is {status}; {req.cost_status} is weaker")
        return {"correction_seq": int(seq) + 1}

    # -- simulated decisions -------------------------------------------------------

    def _simulated(self, req: RecordSimulatedDecisionRequest) -> str:
        experiment = self._experiment(req.experiment_id)
        cohort = BASELINES.get(req.baseline_id)
        if cohort is None:
            raise IngestRefused("UNKNOWN_BASELINE", f"{req.baseline_id} is not a known baseline id")
        if req.cohort != cohort:
            raise IngestRefused("COHORT_NOT_ALLOWED", f"{req.baseline_id} belongs to cohort {cohort}")
        decided_at = parse_utc(req.decided_at)
        self._inside(experiment, decided_at, "DECIDED_OUTSIDE_EXPERIMENT")
        if req.baseline_id != NO_TRADE:
            schedule = self._calendar.resolve(decided_at)
            if schedule is None or not schedule.open_utc <= decided_at < schedule.flatten_start_utc:
                raise IngestRefused("DECIDED_OUTSIDE_ENTRY_WINDOW", f"{decided_at.isoformat()} is not before the flatten start")
        if req.linked_decision_id is not None:
            self._decision_link(req.linked_decision_id, experiment, conid=req.conid, must_enter=True)
        now = self._now()
        session_date = session_date_et(decided_at)
        decision = {**req.model_dump(), "decided_at": decided_at, "session_date": session_date,
                    "body_digest": req.digest(), "recorded_at": now}
        items = [("simulated_decisions", decision)]
        if req.baseline_id == NO_TRADE:
            items.append(("simulated_outcomes", {
                "record_id": req.record_id, "experiment_id": req.experiment_id, "baseline_id": req.baseline_id,
                "cohort": req.cohort, "session_date": session_date, "status": "COMPLETE", "reason": None,
                "exit_kind": "NONE", "exit_at": None, "exit_price": None, "pnl_usd": 0.0, "trades": 0,
                "bar_source": BAR_SOURCE_NONE, "bars_digest": None, "computed_at": now}))
        return self._store.ingest_sealed_many(items, extend=lambda conn: self._opportunity_check(conn, req))

    @staticmethod
    def _opportunity_check(conn, req: RecordSimulatedDecisionRequest) -> dict:
        clash = conn.execute(
            "SELECT record_id FROM simulated_decisions WHERE experiment_id = ? AND baseline_id = ? "
            "AND opportunity_id = ?", [req.experiment_id, req.baseline_id, req.opportunity_id]).fetchone()
        if clash is not None:
            raise ScoreboardConflict(f"opportunity {req.opportunity_id} is already recorded as {clash[0]}")
        return {}
```

`ingest_models` is a leaf; `ingest.py` imports `session_date_et` from ports (exists).

Test helpers `tests/scoreboard/ingest_world.py`:

```python
import datetime as dt
from types import SimpleNamespace

from tests.scoreboard.common import ACCOUNT, EXP_ID, NOW
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.ingest import AiIngest
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest
from trader.scoreboard.ports import DecisionFact

UTC = dt.timezone.utc
STARTED = dt.datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
DECIDED = "2026-10-06T14:30:00+00:00"          # 10:30 ET, Tuesday; flatten start is 19:45 UTC


class FakeExperiments:
    def __init__(self, **changes):
        self.record = SimpleNamespace(experiment_id=EXP_ID, account_id=ACCOUNT, started_at=STARTED,
                                      stopped_at=None, state="ARMED", **changes)

    def get(self, experiment_id):
        return self.record if experiment_id == self.record.experiment_id else None


class FakeDecisions:
    def __init__(self, *facts):
        self.facts = {f.decision_id: f for f in facts}

    def get(self, decision_id):
        return self.facts.get(decision_id)


def enter_fact(decision_id="dec-00000001", conid=265598, **changes):
    values = dict(decision_id=decision_id, account_id=ACCOUNT, conid=conid, action="ENTER",
                  received_at=STARTED + dt.timedelta(days=1))
    values.update(changes)
    return DecisionFact(**values)


def make_ingest(store, *, experiments=None, decisions=None):
    return AiIngest(store=store, experiments=experiments or FakeExperiments(), decisions=decisions or FakeDecisions(),
                    calendar=XNYSCalendarPolicy(), now=lambda: NOW)


def cost_body(**changes):
    body = dict(record_id="cost-0000001", experiment_id=EXP_ID, role="jev", provider="openrouter", model="m1",
                attempt_id="att-0000001", input_tokens=100, output_tokens=20, cost_usd=0.5,
                cost_status="confirmed", called_at=DECIDED, served_kind="cycle", served_id="cycle-1")
    body.update(changes)
    return body


def sim_body(**changes):
    body = dict(record_id="sim-0000001", experiment_id=EXP_ID, baseline_id="follow_signal.v1",
                cohort="strategy_signal", opportunity_id="sig-1", conid=265598, side="BUY", quantity=10,
                reference_price=100.0, stop_price=98.0, target_price=104.0, decided_at=DECIDED)
    body.update(changes)
    return body


def no_trade_body(**changes):
    return sim_body(**{**dict(record_id="sim-0000009", baseline_id="no_trade.v1", cohort="self_found",
                              opportunity_id="opp-9", side=None, quantity=None, reference_price=None,
                              stop_price=None, target_price=None), **changes})


def cost(ingest, **changes):
    return ingest.record_cost(RecordAiCostRequest.model_validate(cost_body(**changes)))


def sim(ingest, body=None, **changes):
    return ingest.record_simulated(RecordSimulatedDecisionRequest.model_validate({**(body or sim_body()), **changes}))
```

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/test_ingest.py`; every test uses the `store` fixture)

```python
import datetime as dt

import pydantic
import pytest

from tests.scoreboard.common import EXP_ID
from tests.scoreboard.ingest_world import (DECIDED, STARTED, FakeDecisions, FakeExperiments, cost, cost_body,
                                           enter_fact, make_ingest, no_trade_body, sim, sim_body)
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest


@pytest.fixture
def ingest(store):
    return make_ingest(store, decisions=FakeDecisions(enter_fact()))


def codes(reply):
    return (reply["status"], reply["code"])


def test_a_cost_is_inserted_and_sealed(ingest, store):
    assert cost(ingest)["status"] == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_status"] == "confirmed" and store.verify_seals() == []


def test_redelivery_with_a_different_time_spelling_is_a_duplicate(ingest, store):
    assert cost(ingest)["status"] == "INSERTED"
    assert cost(ingest, called_at="2026-10-06T10:30:00-04:00")["status"] == "DUPLICATE"
    assert len(store.fetch("ai_costs", {})) == 1 and store.seal_count() == 1


def test_a_changed_body_under_the_same_id_is_refused(ingest):
    cost(ingest)
    assert codes(cost(ingest, cost_usd=9.0)) == ("REFUSED", "CONFLICTING_DUPLICATE")


def test_a_second_original_for_one_attempt_is_a_conflict(ingest):
    cost(ingest)
    assert codes(cost(ingest, record_id="cost-0000002")) == ("REFUSED", "CONFLICTING_DUPLICATE")


@pytest.mark.parametrize("changes", [
    dict(cost_status="unknown"), dict(cost_status="confirmed", cost_usd=None), dict(cost_usd=-1.0),
    dict(cost_usd=float("nan")), dict(called_at="2026-10-06T14:30:00"), dict(role="oracle"),
    dict(record_id="short"), dict(experiment_id="exp-1"), dict(extra_field=1)])
def test_bad_cost_bodies_never_reach_the_store(changes):
    with pytest.raises(pydantic.ValidationError):
        RecordAiCostRequest.model_validate(cost_body(**changes))


def test_unknown_cost_is_stored_null_not_zero(ingest, store):
    assert cost(ingest, cost_status="unknown", cost_usd=None)["status"] == "INSERTED"
    assert store.fetch("ai_costs", {})[0]["cost_usd"] is None


def test_unknown_experiment_and_calls_before_the_start_are_refused(store):
    ingest = make_ingest(store)
    assert codes(cost(ingest, experiment_id="exp-ffffffffffffffffffff")) == ("REFUSED", "EXPERIMENT_UNKNOWN")
    assert codes(cost(ingest, called_at="2026-10-05T12:00:00+00:00")) == ("REFUSED", "CALL_OUTSIDE_EXPERIMENT")


def test_a_cost_after_the_stop_is_still_recorded_but_a_simulated_decision_is_not(store):
    stopped = FakeExperiments()
    stopped.record.stopped_at = STARTED + dt.timedelta(hours=1)
    ingest = make_ingest(store, experiments=stopped)
    assert cost(ingest)["status"] == "INSERTED"                       # money spent is never dropped
    assert codes(sim(ingest)) == ("REFUSED", "DECIDED_OUTSIDE_EXPERIMENT")


def test_a_decision_link_is_validated_and_an_unknown_one_is_retryable(ingest, store):
    unknown = cost(ingest, decision_id="dec-99999999")
    assert codes(unknown) == ("REFUSED", "DECISION_LINK_UNKNOWN") and unknown["retryable"] is True
    assert cost(ingest, decision_id="dec-00000001")["status"] == "INSERTED"
    other = make_ingest(store, decisions=FakeDecisions(enter_fact(account_id="DU999")))
    assert codes(cost(other, record_id="cost-0000003", attempt_id="att-0000003", decision_id="dec-00000001")) == (
        "REFUSED", "DECISION_LINK_WRONG_ACCOUNT")


def test_correction_replaces_the_cost_once(ingest, store):
    cost(ingest, cost_status="estimated", cost_usd=0.4)
    fix = cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001", cost_status="confirmed",
               cost_usd=0.55)
    assert fix["status"] == "INSERTED"
    rows = {r["record_id"]: r for r in store.fetch("ai_costs", {})}
    assert rows["cost-0000002"]["correction_seq"] == 1 and rows["cost-0000001"]["cost_usd"] == 0.4
    again = cost(ingest, record_id="cost-0000003", corrects_record_id="cost-0000001", cost_status="confirmed",
                 cost_usd=0.56)
    assert again["status"] == "INSERTED" and store.fetch("ai_costs", {"record_id": "cost-0000003"})[0][
        "correction_seq"] == 2


def test_correction_cannot_lower_the_status(ingest):
    cost(ingest)
    assert codes(cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001",
                      cost_status="estimated", cost_usd=0.1)) == ("REFUSED", "CORRECTION_DOWNGRADE")


def test_correction_rules(ingest):
    assert cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001")["retryable"] is True
    cost(ingest)
    cost(ingest, record_id="cost-0000002", corrects_record_id="cost-0000001", cost_usd=0.6)
    assert codes(cost(ingest, record_id="cost-0000004", corrects_record_id="cost-0000002")) == (
        "REFUSED", "CORRECTION_OF_CORRECTION")
    assert codes(cost(ingest, record_id="cost-0000005", corrects_record_id="cost-0000001", model="other")) == (
        "REFUSED", "CORRECTION_IDENTITY_MISMATCH")


def test_a_simulated_decision_is_inserted_with_its_session_date(ingest, store):
    assert sim(ingest)["status"] == "INSERTED"
    row = store.fetch("simulated_decisions", {})[0]
    assert row["session_date"] == dt.date(2026, 10, 6) and row["baseline_id"] == "follow_signal.v1"
    assert store.fetch("simulated_outcomes", {}) == []


def test_simulated_redelivery_is_a_duplicate_and_a_changed_body_is_a_conflict(ingest):
    sim(ingest)
    assert sim(ingest)["status"] == "DUPLICATE"
    assert codes(sim(ingest, quantity=11)) == ("REFUSED", "CONFLICTING_DUPLICATE")


def test_one_decision_per_opportunity_and_baseline(ingest):
    sim(ingest)
    assert codes(sim(ingest, record_id="sim-0000002")) == ("REFUSED", "CONFLICTING_DUPLICATE")
    assert sim(ingest, record_id="sim-0000003", baseline_id="fixed_rule.v1", cohort="self_found")["status"] == "INSERTED"


@pytest.mark.parametrize("changes", [
    dict(side="SELL"), dict(quantity=0), dict(stop_price=101.0), dict(target_price=99.0), dict(conid=None),
    dict(reference_price=float("inf")), dict(decided_at="2026-10-06T14:30:00")])
def test_bad_simulated_bodies_never_reach_the_store(changes):
    with pytest.raises(pydantic.ValidationError):
        RecordSimulatedDecisionRequest.model_validate({**sim_body(), **changes})


def test_no_trade_with_prices_is_refused_by_the_model():
    with pytest.raises(pydantic.ValidationError):
        RecordSimulatedDecisionRequest.model_validate({**no_trade_body(), "quantity": 5})


def test_baseline_cohort_and_entry_window_rules(ingest):
    assert codes(sim(ingest, baseline_id="follow_signal.v9")) == ("REFUSED", "UNKNOWN_BASELINE")
    assert codes(sim(ingest, cohort="self_found")) == ("REFUSED", "COHORT_NOT_ALLOWED")
    assert codes(sim(ingest, decided_at="2026-10-06T19:45:00+00:00")) == ("REFUSED", "DECIDED_OUTSIDE_ENTRY_WINDOW")
    assert codes(sim(ingest, decided_at="2026-10-10T14:30:00+00:00")) == ("REFUSED", "DECIDED_OUTSIDE_ENTRY_WINDOW")


def test_linked_decision_must_be_an_enter_on_the_same_conid(store):
    ingest = make_ingest(store, decisions=FakeDecisions(enter_fact(), enter_fact("dec-00000002", action="CLOSE")))
    assert sim(ingest, linked_decision_id="dec-00000001")["status"] == "INSERTED"
    assert codes(sim(ingest, record_id="sim-0000002", opportunity_id="sig-2",
                     linked_decision_id="dec-00000002")) == ("REFUSED", "DECISION_LINK_NOT_ENTER")
    assert codes(sim(ingest, record_id="sim-0000003", opportunity_id="sig-3", conid=4815747,
                     linked_decision_id="dec-00000001")) == ("REFUSED", "DECISION_LINK_CONID_MISMATCH")


def test_no_trade_is_complete_with_zero_pnl_at_once(ingest, store):
    assert sim(ingest, no_trade_body())["status"] == "INSERTED"
    outcome = store.fetch("simulated_outcomes", {})[0]
    assert (outcome["status"], outcome["pnl_usd"], outcome["trades"], outcome["bar_source"]) == (
        "COMPLETE", 0.0, 0, "none")
    assert sim(ingest, no_trade_body())["status"] == "DUPLICATE" and store.seal_count() == 2
```

In `tests/scoreboard/test_verify.py` drop the `trader.scoreboard.inputs` import, add `from tests.scoreboard.ingest_world import cost, make_ingest, sim`, and replace the parametrization and the test body with:

```python
@pytest.mark.parametrize("table,sql", [
    ("equity_adjustments", "UPDATE equity_adjustments SET amount_usd = amount_usd + 1"),
    ("benchmark_prices", "UPDATE benchmark_prices SET close = close + 1"),
    ("ai_costs", "UPDATE ai_costs SET cost_usd = 9"),
    ("simulated_decisions", "UPDATE simulated_decisions SET quantity = 99"),
    ("simulated_outcomes", "UPDATE simulated_outcomes SET pnl_usd = 9"),
])
def test_edited_adjustment_benchmark_ai_cost_and_simulated_rows_are_detected(service, world, db, table, sql):
    from tests.scoreboard.ingest_world import no_trade_body
    set_commission(db, ACCOUNT, "e2", 1.30)
    service.refresh()
    ingest = make_ingest(world.store)
    cost(ingest)
    sim(ingest, no_trade_body())
    assert service.verify()["ok"] is True
    db.execute(sql)
    result = service.verify()
    assert result["ok"] is False and any(m["check"] == "ROW_EDITED" and m["table"] == table
                                         for m in result["mismatches"])
```

(`simulated_outcomes` exists because a `no_trade.v1` record writes its outcome at once.)

Add to `ports.py`:

```python
@dataclass(frozen=True)
class DecisionFact:
    decision_id: str
    account_id: str
    conid: Optional[int]
    action: Optional[str]
    received_at: dt.datetime


class DecisionFacts(Protocol):
    def get(self, decision_id: str) -> Optional[DecisionFact]: ...


class NullDecisionFacts:
    """No ai_paper stack on this trader: every decision link is unknown."""

    def get(self, decision_id: str) -> None:
        return None


class DecisionStoreFacts:
    """Adapter over ``AiPaperDecisionStore.row``; a row without a decision id is not a decision."""

    def __init__(self, decision_store: Any):
        self._store = decision_store

    def get(self, decision_id: str) -> Optional[DecisionFact]:
        row = self._store.row(decision_id)
        if row is None or row.decision_id is None:
            return None
        return DecisionFact(row.decision_id, row.account_id, row.conid, row.action, row.received_at)
```

and in `tests/scoreboard/test_ports.py`:

```python
def test_decision_store_facts_maps_a_row_and_hides_a_missing_one():
    from types import SimpleNamespace
    from trader.scoreboard.ports import DecisionStoreFacts
    row = SimpleNamespace(decision_id="dec-00000001", account_id="DU1", conid=265598, action="ENTER",
                          received_at=NOW)
    facts = DecisionStoreFacts(SimpleNamespace(row=lambda decision_id: row if decision_id == "dec-00000001"
                                               else SimpleNamespace(decision_id=None) if decision_id == "bare"
                                               else None))
    assert facts.get("dec-00000001").conid == 265598 and facts.get("dec-00000001").action == "ENTER"
    assert facts.get("dec-99999999") is None and facts.get("bare") is None
```
(import `NOW` from `tests.scoreboard.common`.)

- [ ] **Step 2: Run, expect failure** `.venv/bin/python -m pytest tests/scoreboard/test_ingest.py -q --timeout=30` (import errors).
- [ ] **Step 3: Implement** the files above.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard/test_ingest.py tests/scoreboard/test_ports.py tests/scoreboard/test_verify.py tests/scoreboard/test_ingest_store.py -q --timeout=30` → pass.
- [ ] **Step 5: Commit** `feat: ingest ai costs and simulated decisions idempotently`.

---

### Task 3: RPC surface, ACL and wiring

**Files:**
- Create: `trader/messaging/ai_ingest_surface.py`
- Modify: `trader/messaging/principals.py`, `trader/messaging/production_api.py`, `trader/scoreboard/wiring.py`, `trader/trading/command_stack.py`, `trader/messaging/scoreboard_surface.py` (docstring only), `tests/scoreboard/test_surface.py`
- Test: `tests/scoreboard/test_ingest_surface.py`

**Interfaces:**
- Consumes: `AiIngest`, `TypedRpcRegistry.register`.
- Produces: `register_ai_ingest_surface(registry, ingest) -> None` (registers both commands on role `"command"`; `None` registers nothing); `ScoreboardServices.ingest: AiIngest`; `trader.ai_ingest`.

```python
"""Cost and simulation ingestion over typed RPC (SP2 Plan 2). ai_supervisor only; facts, not edits."""
from __future__ import annotations

from typing import Any

from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest


def register_ai_ingest_surface(registry: Any, ingest: Any) -> None:
    """``ingest`` is the trader's ``AiIngest``; None (no scoreboard) registers nothing."""
    if ingest is None:
        return
    registry.register("command", "record_ai_cost", RecordAiCostRequest, dict, ingest.record_cost,
                      execution="thread")
    registry.register("command", "record_simulated_decision", RecordSimulatedDecisionRequest, dict,
                      ingest.record_simulated, execution="thread")
```

`principals.py`: replace the comment `# SP1 scoreboard (Plan 5): reads only. verify is for humans; no principal has a scoreboard write.` with `# SP1 scoreboard (Plan 5): reads. verify is for humans. SP2 Plan 2 adds the two ingestion commands below (ai_supervisor only).` and add

```python
    ("command", "record_ai_cost"): frozenset({"ai_supervisor"}),
    ("command", "record_simulated_decision"): frozenset({"ai_supervisor"}),
```
`scoreboard_surface.py` docstring: "Scoreboard reads over typed RPC (SP1 Plan 5 Task 7). Reads only; the ingestion commands are in ai_ingest_surface."

`production_api.py`, right after `register_scoreboard_surface(registry, getattr(trader, 'scoreboard_service', None))`:

```python
    from trader.messaging.ai_ingest_surface import register_ai_ingest_surface
    register_ai_ingest_surface(registry, getattr(trader, 'ai_ingest', None))
```
`wiring.py` (`build_scoreboard`): after `service` is built add
```python
    ingest = AiIngest(store=store, experiments=reader, decisions=(NullDecisionFacts() if decision_store is None
                      else DecisionStoreFacts(decision_store)), calendar=calendar, now=now)
```
and add `ingest: Any = None` to `ScoreboardServices` (set it in the constructor call). `command_stack.py`, next to `trader.scoreboard_service = scoreboard.service`: `trader.ai_ingest = scoreboard.ingest`.

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/test_ingest_surface.py`, same `ServedStack` shape as `test_surface.py` but the registry holds role `command`)

```python
import pytest

from tests.rpc_identity_fixtures import ServedStack, make_identities
from tests.scoreboard.ingest_world import FakeDecisions, cost_body, enter_fact, make_ingest, sim_body
from trader.messaging.ai_ingest_surface import register_ai_ingest_surface
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError

METHODS = {"record_ai_cost": cost_body, "record_simulated_decision": sim_body}


@pytest.fixture
def served(store):
    registry = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_ai_ingest_surface(registry, make_ingest(store, decisions=FakeDecisions(enter_fact())))
    stack = ServedStack({("trader", "command"): registry}, make_identities())
    yield stack
    stack.close()


def test_only_ai_supervisor_may_ingest(served):
    for method, body in METHODS.items():
        assert served.client("ai_supervisor", role="command").call(method, body(), dict)["status"] == "INSERTED"
        for principal in ("cli", "dashboard", "ai_research"):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role="command").call(method, body(), dict)
            assert exc.value.code == "PERMISSION_DENIED"


def test_a_conflict_is_a_refused_reply_not_an_rpc_error(served):
    client = served.client("ai_supervisor", role="command")
    client.call("record_ai_cost", cost_body(), dict)
    reply = client.call("record_ai_cost", cost_body(cost_usd=9.0), dict)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "CONFLICTING_DUPLICATE", False)


def test_a_malformed_body_is_a_validation_error(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("ai_supervisor", role="command").call("record_ai_cost", cost_body(cost_status="unknown"), dict)
    assert exc.value.code == "VALIDATION_ERROR"


def test_ingestion_rights_are_exact_and_the_ai_has_no_other_scoreboard_write():
    assert TRADER_ACL[("command", "record_ai_cost")] == {"ai_supervisor"}
    assert TRADER_ACL[("command", "record_simulated_decision")] == {"ai_supervisor"}
    for principal in ("ai_supervisor", "ai_research"):
        writes = {k[1] for k, allowed in TRADER_ACL.items() if principal in allowed and k[0] == "command"
                  and any(word in k[1] for word in ("scoreboard", "ai_cost", "simulated", "benchmark", "equity"))}
        assert writes == ({"record_ai_cost", "record_simulated_decision"} if principal == "ai_supervisor" else set())


def test_the_full_production_registry_registers_both_commands():
    from tests.rpc_identity_fixtures import build_full_production_registry
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert {("command", "record_ai_cost"), ("command", "record_simulated_decision")} <= registered
```

`ServedStack.client(caller, server="trader", role="query", ...)` takes the socket role as the keyword `role`. Replace `test_ai_principals_have_no_write_path_to_scoreboard_tables` in `test_surface.py` with the same test for `ai_research` only (`for principal in ("ai_research",)`); the exact `ai_supervisor` set is asserted in `test_ingestion_rights_are_exact_and_the_ai_has_no_other_scoreboard_write`.

`build_full_production_registry` builds the production registry from a `MagicMock` trader, so `getattr(trader, 'ai_ingest', None)` is a mock and both commands register without any fixture change (the SP1 test for `get_scoreboard` relies on the same effect).

- [ ] **Step 2: Run, expect failure** `.venv/bin/python -m pytest tests/scoreboard/test_ingest_surface.py -q --timeout=30`.
- [ ] **Step 3: Implement** the edits above.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard/test_ingest_surface.py tests/scoreboard/test_surface.py tests/scoreboard/test_wiring.py -q --timeout=30` → pass.
- [ ] **Step 5: Commit** `feat: add ai_supervisor-only cost and simulated decision commands`.

---

### Task 4: The bracket simulator (pure)

**Files:**
- Create: `trader/scoreboard/simulator.py`
- Test: `tests/scoreboard/test_simulator.py`

**Interfaces:**
- Produces: `Bar(NamedTuple: start: datetime (UTC, start of minute), open, high, low, close: float)`; `SimInput(frozen dataclass: conid: int, quantity: int, reference_price: float, stop_price: float, target_price: float, decided_at: datetime, flatten_start_utc: datetime)`; `SimResult(frozen dataclass: status: str, reason: Optional[str], exit_kind: str, exit_at: Optional[datetime], exit_price: Optional[float], pnl_usd: Optional[float], trades: Optional[int], bars_digest: Optional[str])`; `simulate_long_bracket(trade: SimInput, bars: Sequence[Bar]) -> SimResult`; constants `MAX_BAR_GAP = 30 min`, `FLATTEN_BAR_WINDOW = 10 min`.

```python
"""Bracket outcome of one long position from 1-minute bars (SP2 Plan 2, ruling 10 and 11).

Pure: no I/O, no clock. Entry at the reference price; only bars after the minute of the decision are scanned.
One bar that touches both stop and target is a stop. Missing data is INCOMPLETE, never filled in.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

MINUTE = dt.timedelta(minutes=1)
MAX_BAR_GAP = dt.timedelta(minutes=30)
FLATTEN_BAR_WINDOW = dt.timedelta(minutes=10)


class Bar(NamedTuple):
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SimInput:
    conid: int
    quantity: int
    reference_price: float
    stop_price: float
    target_price: float
    decided_at: dt.datetime
    flatten_start_utc: dt.datetime


@dataclass(frozen=True)
class SimResult:
    status: str
    reason: Optional[str]
    exit_kind: str
    exit_at: Optional[dt.datetime]
    exit_price: Optional[float]
    pnl_usd: Optional[float]
    trades: Optional[int]
    bars_digest: Optional[str]


def _incomplete(reason: str) -> SimResult:
    return SimResult("INCOMPLETE", reason, "NONE", None, None, None, None, None)


def _floor_minute(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)


def _is_valid(bar: Bar) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close)
    return (all(isinstance(p, (int, float)) and math.isfinite(p) and p > 0 for p in prices)
            and bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close)
            and bar.start.tzinfo is not None)


def _digest(bars: Sequence[Bar]) -> str:
    rows = [[b.start.astimezone(dt.timezone.utc).isoformat(), repr(b.open), repr(b.high), repr(b.low),
             repr(b.close)] for b in bars]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _hit(trade: SimInput, bar: Bar) -> tuple[Optional[str], Optional[float]]:
    if bar.low <= trade.stop_price:
        return "STOP", min(trade.stop_price, bar.open)
    if bar.high >= trade.target_price:
        return "TARGET", trade.target_price
    return None, None


def _complete(trade: SimInput, kind: str, at: dt.datetime, price: float, used: Sequence[Bar]) -> SimResult:
    pnl = (price - trade.reference_price) * trade.quantity
    return SimResult("COMPLETE", None, kind, at, price, pnl, 1, _digest(used))


def simulate_long_bracket(trade: SimInput, bars: Sequence[Bar]) -> SimResult:
    first_start = _floor_minute(trade.decided_at) + MINUTE
    flatten = trade.flatten_start_utc
    used = sorted((b for b in bars if first_start <= b.start < flatten + FLATTEN_BAR_WINDOW),
                  key=lambda b: b.start)
    if not used:
        return _incomplete("NO_BARS")
    if len({b.start for b in used}) != len(used):
        return _incomplete("DUPLICATE_BAR")
    if not all(_is_valid(b) for b in used):
        return _incomplete("BAD_BAR")
    cursor = first_start
    for bar in (b for b in used if b.start < flatten):
        if bar.start - cursor > MAX_BAR_GAP:
            return _incomplete("BAR_GAP")
        kind, price = _hit(trade, bar)
        if kind is not None:
            return _complete(trade, kind, bar.start, price, [b for b in used if b.start <= bar.start])
        cursor = bar.start + MINUTE
    exit_bar = next((b for b in used if b.start >= flatten), None)
    if exit_bar is None:
        return _incomplete("NO_FLATTEN_BAR")
    if exit_bar.start - cursor > MAX_BAR_GAP:
        return _incomplete("BAR_GAP")
    return _complete(trade, "FLATTEN", exit_bar.start, exit_bar.open, [b for b in used if b.start <= exit_bar.start])
```

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/test_simulator.py`)

```python
import datetime as dt

from trader.scoreboard.simulator import Bar, SimInput, simulate_long_bracket

UTC = dt.timezone.utc
DECIDED = dt.datetime(2026, 10, 6, 14, 30, 20, tzinfo=UTC)       # inside the 14:30 minute
FLATTEN = dt.datetime(2026, 10, 6, 19, 45, tzinfo=UTC)
TRADE = SimInput(conid=265598, quantity=10, reference_price=100.0, stop_price=98.0, target_price=104.0,
                 decided_at=DECIDED, flatten_start_utc=FLATTEN)


def bar(hhmm, o=100.0, h=100.5, l=99.5, c=100.0, day=6):
    hour, minute = divmod(hhmm, 100)
    return Bar(dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC), o, h, l, c)


def quiet_day(first=1431, last=1944, step=15):
    """One quiet bar every ``step`` minutes: a calm stock that trades often enough."""
    out, now = [], dt.datetime(2026, 10, 6, first // 100, first % 100, tzinfo=UTC)
    while now.hour * 100 + now.minute <= last:
        out.append(bar(now.hour * 100 + now.minute))
        now += dt.timedelta(minutes=step)
    return out


def with_flatten(bars, open_=101.0):
    return [*bars, bar(1945, o=open_, h=open_ + 0.2, l=open_ - 0.2, c=open_)]


def test_no_hit_exits_at_the_open_of_the_flatten_bar():
    result = simulate_long_bracket(TRADE, with_flatten(quiet_day(), 101.0))
    assert (result.status, result.exit_kind, result.exit_price, result.pnl_usd, result.trades) == (
        "COMPLETE", "FLATTEN", 101.0, 10.0, 1)
    assert result.exit_at == FLATTEN and len(result.bars_digest) == 64


def test_the_stop_exits_at_the_stop():
    bars = with_flatten([bar(1431), bar(1500, o=99.0, l=97.5), bar(1600)])
    result = simulate_long_bracket(TRADE, bars)
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("STOP", 98.0, -20.0)


def test_gap_through_the_stop_fills_at_the_open():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, o=96.5, h=97.0, l=96.0, c=96.8)]))
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("STOP", 96.5, -35.0)


def test_the_target_exits_at_the_target():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, h=104.7)]))
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("TARGET", 104.0, 40.0)


def test_same_bar_stop_and_target_is_a_stop():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, h=105.0, l=97.0)]))
    assert result.exit_kind == "STOP" and result.exit_price == 98.0


def test_the_minute_of_the_decision_is_not_scanned():
    bars = with_flatten([bar(1430, l=90.0), *quiet_day()])
    assert simulate_long_bracket(TRADE, bars).exit_kind == "FLATTEN"


def test_bars_at_or_after_the_flatten_start_are_not_scanned_for_a_stop():
    bars = [*quiet_day(), bar(1945, o=101.0, h=101.0, l=90.0, c=100.0)]
    result = simulate_long_bracket(TRADE, bars)
    assert result.exit_kind == "FLATTEN" and result.exit_price == 101.0


def test_missing_flatten_bar_is_incomplete():
    result = simulate_long_bracket(TRADE, quiet_day())
    assert (result.status, result.reason, result.pnl_usd) == ("INCOMPLETE", "NO_FLATTEN_BAR", None)
    late = simulate_long_bracket(TRADE, [*quiet_day(), bar(1956)])
    assert late.reason == "NO_FLATTEN_BAR"


def test_a_thirty_minute_hole_is_incomplete():
    bars = with_flatten([bar(1431), bar(1445), bar(1530), bar(1600)])
    assert simulate_long_bracket(TRADE, bars).reason == "BAR_GAP"


def test_a_hole_before_the_flatten_bar_is_incomplete():
    steps = [1431, 1500, 1530, 1600, 1630, 1700, 1730, 1800, 1830, 1900]
    assert simulate_long_bracket(TRADE, [bar(t) for t in [*steps, 1930, 1945]]).status == "COMPLETE"
    assert simulate_long_bracket(TRADE, [bar(t) for t in [*steps, 1945]]).reason == "BAR_GAP"


def test_no_bars_is_incomplete():
    assert simulate_long_bracket(TRADE, []).reason == "NO_BARS"
    assert simulate_long_bracket(TRADE, [bar(1000)]).reason == "NO_BARS"


def test_bad_and_repeated_bars_are_incomplete():
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431, h=99.0, l=99.5)])).reason == "BAD_BAR"
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431, o=float("nan"))])).reason == "BAD_BAR"
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1431)])).reason == "DUPLICATE_BAR"


def test_the_result_does_not_depend_on_input_order():
    bars = with_flatten(quiet_day())
    assert simulate_long_bracket(TRADE, list(reversed(bars))) == simulate_long_bracket(TRADE, bars)
```

- [ ] **Step 2: Run, expect failure** (`ModuleNotFoundError`). **Step 3: Implement** `simulator.py`. **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard/test_simulator.py -q --timeout=30` → pass. **Step 5: Commit** `feat: add the pure long bracket simulator`.

---

### Task 5: Bar sources, the session runner and the Alpaca keys

**Files:**
- Create: `trader/scoreboard/bar_sources.py`, `trader/scoreboard/session_simulator.py`
- Modify: `trader/scoreboard/wiring.py`, `trader/trading/trading_runtime.py`, `tests/scoreboard/test_wiring.py`, `tests/test_trading_runtime.py`
- Test: `tests/scoreboard/test_bar_sources.py`, `tests/scoreboard/test_session_simulator.py`

**Interfaces:**
- Consumes: `simulate_long_bracket`, `ScoreboardStore`, `XNYSCalendarPolicy.resolve`, `TickStorage.get_tickdata(BarSize.Mins1).read(conid, date_range=DateRange(...))`, `ProviderRegistry.from_config(...).get(Capability.HISTORY, "alpaca")`.
- Produces:
  - `class BarSourceError(RuntimeError)`; `BarSource` protocol: `name: str`; `bars(conid: int, start: datetime, end: datetime) -> list[Bar]` (empty list = the source has no bars; raises `BarSourceError(code)` on failure).
  - `LocalHistoryBars(history_db_path: str)` (`name = "history_duckdb"`), `AlpacaBars(provider, security_for: Callable[[int], Any])` (`name = "alpaca"`), `frame_to_bars(frame) -> list[Bar]`, `default_bar_sources(trader) -> list[BarSource]`.
  - `class SessionSimulator(store, calendar, sources, now, grace=2h, retry_after=5min)` with `run_due() -> int` (outcomes written this call) and `data_ready_at(session_date) -> datetime`.
  - `ScoreboardServices.simulator`; `tick()` gets a `"simulate"` step.
  - `Trader.alpaca_api_key_id`, `Trader.alpaca_api_secret_key`.

`bar_sources.py`:

```python
"""1-minute bar sources for the baseline simulator: local history first, then the trader's Alpaca provider."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional, Protocol

import pandas as pd

from trader.scoreboard.simulator import Bar

logger = logging.getLogger(__name__)


class BarSourceError(RuntimeError):
    """A source failed. The message is a short code, never a URL, header or key."""


class BarSource(Protocol):
    name: str

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]: ...


def frame_to_bars(frame: Optional[pd.DataFrame]) -> list[Bar]:
    if frame is None or len(frame) == 0:
        return []
    index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
    return [Bar(stamp.to_pydatetime(), float(o), float(h), float(l), float(c))
            for stamp, o, h, l, c in zip(index, frame["open"], frame["high"], frame["low"], frame["close"])]


class LocalHistoryBars:
    name = "history_duckdb"

    def __init__(self, history_db_path: str):
        self._path = history_db_path

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        if not self._path:
            raise BarSourceError("NO_HISTORY_DB")
        from trader.data.data_access import TickStorage
        from trader.data.store import DateRange
        from trader.objects import BarSize
        try:
            tickdata = TickStorage(self._path).get_tickdata(BarSize.Mins1)
            raw = tickdata.read(conid, date_range=DateRange(start=start, end=end))
        except Exception as exc:
            logger.warning("local 1-minute history unreadable for conid %s: %s", conid, type(exc).__name__)
            raise BarSourceError(f"HISTORY_READ_{type(exc).__name__}") from exc
        return [b for b in frame_to_bars(raw) if start <= b.start < end]


class AlpacaBars:
    name = "alpaca"

    def __init__(self, provider: Any, security_for: Callable[[int], Any]):
        self._provider = provider
        self._security_for = security_for

    def bars(self, conid: int, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        from trader.objects import BarSize
        security = self._security_for(conid)
        if security is None:
            raise BarSourceError("SYMBOL_UNRESOLVED")
        if security.secType != "STK" or security.currency != "USD":
            raise BarSourceError("UNSUPPORTED_INSTRUMENT")
        try:
            frame = self._provider.get_history(security.symbol, BarSize.Mins1, start, end)
        except Exception as exc:
            logger.warning("alpaca 1-minute history failed for conid %s: %s", conid, type(exc).__name__)
            raise BarSourceError(f"ALPACA_{type(exc).__name__}") from exc
        return [b for b in frame_to_bars(frame) if start <= b.start < end]


def default_bar_sources(trader: Any) -> list[BarSource]:
    sources: list[BarSource] = [LocalHistoryBars(getattr(trader, "history_duckdb_path", "") or "")]
    keys = {"alpaca_api_key_id": getattr(trader, "alpaca_api_key_id", "") or "",
            "alpaca_api_secret_key": getattr(trader, "alpaca_api_secret_key", "") or ""}
    from trader.data_providers import Capability, ProviderError, ProviderRegistry
    try:
        provider = ProviderRegistry.from_config(keys).get(Capability.HISTORY, "alpaca")
    except ProviderError as exc:
        logger.warning("no Alpaca history for baseline simulation (%s); local bars only", type(exc).__name__)
        return sources

    def security_for(conid: int):
        rows = trader.universe_accessor.resolve_symbol(conid, first_only=True)
        return rows[0] if rows else None
    sources.append(AlpacaBars(provider, security_for))
    return sources
```

Note: `get_history` takes whole days and the Alpaca provider returns every bar of those days; `bars()` filters to `[start, end)`. The `start` / `end` the runner passes are `floor_minute(decided_at) + 1 min` and `flatten_start + 10 min`.

`session_simulator.py`:

```python
"""Writes one sealed outcome per simulated decision once the session's bars are ready (SP2 Plan 2, ruling 13)."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from trader.scoreboard.bar_sources import BarSource, BarSourceError
from trader.scoreboard.simulator import FLATTEN_BAR_WINDOW, MINUTE, SimInput, SimResult, simulate_long_bracket
from trader.scoreboard.store import ScoreboardConflict, ScoreboardStore

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DATA_READY_ET = dt.time(20, 16)          # the Alpaca provider's own completed-session rule
GRACE = dt.timedelta(hours=2)
RETRY_AFTER = dt.timedelta(minutes=5)
PENDING_SQL = (
    "SELECT d.record_id, d.experiment_id, d.baseline_id, d.cohort, d.session_date, d.conid, d.quantity, "
    "d.reference_price, d.stop_price, d.target_price, d.decided_at FROM simulated_decisions d "
    "LEFT JOIN simulated_outcomes o ON o.record_id = d.record_id WHERE o.record_id IS NULL "
    "ORDER BY d.session_date, d.decided_at, d.record_id")


class SessionSimulator:
    def __init__(self, *, store: ScoreboardStore, calendar: Any, sources: Sequence[BarSource],
                 now: Callable[[], dt.datetime], grace: dt.timedelta = GRACE,
                 retry_after: dt.timedelta = RETRY_AFTER):
        self._store, self._calendar, self._sources, self._now = store, calendar, list(sources), now
        self._grace, self._retry_after = grace, retry_after
        self._last_try: dict[str, dt.datetime] = {}

    @staticmethod
    def data_ready_at(session_date: dt.date) -> dt.datetime:
        return dt.datetime.combine(session_date, DATA_READY_ET, tzinfo=ET).astimezone(dt.timezone.utc)

    def run_due(self) -> int:
        now = self._now()
        written = 0
        cache: dict[tuple[str, int, dt.date], list] = {}
        for record in self._store.db.execute(PENDING_SQL, fetch="all"):
            row = dict(zip(("record_id", "experiment_id", "baseline_id", "cohort", "session_date", "conid",
                            "quantity", "reference_price", "stop_price", "target_price", "decided_at"), record))
            ready = self.data_ready_at(row["session_date"])
            if now < ready:
                continue
            tried = self._last_try.get(row["record_id"])
            if tried is not None and now - tried < self._retry_after:
                continue
            self._last_try[row["record_id"]] = now
            result, source = self._simulate(row, cache)
            if result.status == "INCOMPLETE" and now < ready + self._grace:
                continue                       # data may still arrive; the next attempt is allowed
            written += self._write(row, result, source, now)
        return written

    def _simulate(self, row: dict, cache: dict) -> tuple[SimResult, str]:
        schedule = self._calendar.resolve(dt.datetime.combine(row["session_date"], dt.time(12), tzinfo=ET))
        if schedule is None:
            return SimResult("INCOMPLETE", "NOT_A_SESSION", "NONE", None, None, None, None, None), "none"
        trade = SimInput(row["conid"], int(row["quantity"]), row["reference_price"], row["stop_price"],
                         row["target_price"], row["decided_at"], schedule.flatten_start_utc)
        start = row["decided_at"].astimezone(dt.timezone.utc).replace(second=0, microsecond=0) + MINUTE
        end = schedule.flatten_start_utc + FLATTEN_BAR_WINDOW
        if not self._sources:
            return SimResult("INCOMPLETE", "NO_BAR_SOURCE", "NONE", None, None, None, None, None), "none"
        reasons: list[str] = []
        for source in self._sources:
            key = (source.name, row["conid"], row["session_date"])
            try:
                if key not in cache:
                    cache[key] = source.bars(row["conid"], start, end)
                bars = cache[key]
            except BarSourceError as exc:
                reasons.append(f"{source.name}:{exc}")
                continue
            result = simulate_long_bracket(trade, bars) if bars else SimResult(
                "INCOMPLETE", "NO_BARS", "NONE", None, None, None, None, None)
            if result.status == "COMPLETE":
                return result, source.name
            reasons.append(f"{source.name}:{result.reason}")
        return SimResult("INCOMPLETE", "; ".join(reasons), "NONE", None, None, None, None, None), "none"

    def _write(self, row: dict, result: SimResult, source: str, now: dt.datetime) -> int:
        outcome = {
            "record_id": row["record_id"], "experiment_id": row["experiment_id"], "baseline_id": row["baseline_id"],
            "cohort": row["cohort"], "session_date": row["session_date"], "status": result.status,
            "reason": result.reason, "exit_kind": result.exit_kind, "exit_at": result.exit_at,
            "exit_price": result.exit_price, "pnl_usd": result.pnl_usd, "trades": result.trades,
            "bar_source": source, "bars_digest": result.bars_digest, "computed_at": now}
        try:
            self._store.insert_sealed("simulated_outcomes", outcome)
        except ScoreboardConflict:
            return 0                           # another tick wrote it first; sealed rows are final
        self._last_try.pop(row["record_id"], None)
        if result.status == "INCOMPLETE":
            logger.warning("simulated decision %s is INCOMPLETE: %s", row["record_id"], result.reason)
        return 1
```

`wiring.py`: `build_scoreboard(..., bar_sources: Optional[Sequence[Any]] = None)`; builds `SessionSimulator(store=store, calendar=calendar, sources=default_bar_sources(trader) if bar_sources is None else bar_sources, now=now)`; `ScoreboardServices` gets `simulator: Any = None` and `tick()` calls `self._step("simulation", self.simulator.run_due)` when set. (`command_stack._build_scoreboard` needs no change: the default builds the sources from the trader.)

`trading_runtime.py` `Trader.__init__`: add parameters `alpaca_api_key_id: str = ''`, `alpaca_api_secret_key: str = ''` after `automation_strategy_name` and set `self.alpaca_api_key_id = alpaca_api_key_id or ''` (same for the secret). Never log them.

- [ ] **Step 1: Write the failing tests**

`tests/scoreboard/test_session_simulator.py`. `FakeSource(name, by_conid, error=None)` returns bars from a dict and counts calls.

```python
import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID
from tests.scoreboard.ingest_world import make_ingest, sim, sim_body
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.bar_sources import BarSourceError
from trader.scoreboard.session_simulator import GRACE, SessionSimulator
from trader.scoreboard.simulator import Bar

UTC = dt.timezone.utc
SESSION = dt.date(2026, 10, 6)


def minute_bars(day, hhmm_list, open_=100.0):
    out = []
    for hhmm in hhmm_list:
        hour, minute = divmod(hhmm, 100)
        out.append(Bar(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC), open_, open_ + 0.3,
                       open_ - 0.3, open_))
    return out


QUIET = [1431, 1500, 1530, 1600, 1630, 1700, 1730, 1800, 1830, 1900, 1930, 1945]


class FakeSource:
    def __init__(self, name, bars=(), error=None):
        self.name, self.bars_, self.error, self.calls = name, list(bars), error, 0

    def bars(self, conid, start, end):
        self.calls += 1
        if self.error:
            raise BarSourceError(self.error)
        return [b for b in self.bars_ if start <= b.start < end]


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def ready_clock(extra=dt.timedelta(minutes=1)):
    return Clock(SessionSimulator.data_ready_at(SESSION) + extra)


def simulator(store, sources, clock):
    return SessionSimulator(store=store, calendar=XNYSCalendarPolicy(), sources=sources, now=clock)


@pytest.fixture
def ingest(store):
    return make_ingest(store)


def outcome(store):
    return store.fetch("simulated_outcomes", {})[0]


def test_nothing_runs_before_the_bars_are_ready(store, ingest):
    sim(ingest)
    clock = Clock(SessionSimulator.data_ready_at(SESSION) - dt.timedelta(minutes=1))
    source = FakeSource("alpaca", minute_bars(SESSION, QUIET))
    assert simulator(store, [source], clock).run_due() == 0 and source.calls == 0


def test_a_complete_outcome_is_sealed_with_its_source(store, ingest):
    sim(ingest)
    local = FakeSource("history_duckdb")
    alpaca = FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))
    assert simulator(store, [local, alpaca], ready_clock()).run_due() == 1
    row = outcome(store)
    assert (row["status"], row["exit_kind"], row["bar_source"], row["pnl_usd"]) == ("COMPLETE", "FLATTEN", "alpaca", 10.0)
    assert store.verify_seals() == []


def test_the_first_complete_source_wins_and_the_second_is_not_called(store, ingest):
    sim(ingest)
    local, alpaca = FakeSource("history_duckdb", minute_bars(SESSION, QUIET)), FakeSource("alpaca")
    simulator(store, [local, alpaca], ready_clock()).run_due()
    assert outcome(store)["bar_source"] == "history_duckdb" and alpaca.calls == 0


def test_missing_bars_wait_for_the_grace_period_then_end_incomplete(store, ingest):
    sim(ingest)
    clock = ready_clock()
    runner = SessionSimulator(store=store, calendar=XNYSCalendarPolicy(), sources=[FakeSource("alpaca")],
                              now=clock, retry_after=dt.timedelta(0))
    assert runner.run_due() == 0 and store.fetch("simulated_outcomes", {}) == []
    clock.at += GRACE
    assert runner.run_due() == 1
    row = outcome(store)
    assert row["status"] == "INCOMPLETE" and row["pnl_usd"] is None and "alpaca:NO_BARS" in row["reason"]


def test_a_source_error_is_a_reason_and_never_a_secret(store, ingest):
    sim(ingest)
    clock = ready_clock(GRACE)
    runner = simulator(store, [FakeSource("history_duckdb", error="NO_HISTORY_DB"),
                               FakeSource("alpaca", error="ALPACA_ProviderError")], clock)
    runner.run_due()
    assert outcome(store)["reason"] == "history_duckdb:NO_HISTORY_DB; alpaca:ALPACA_ProviderError"


def test_no_source_at_all_is_a_stated_reason(store, ingest):
    sim(ingest)
    simulator(store, [], ready_clock(GRACE)).run_due()
    assert outcome(store)["reason"] == "NO_BAR_SOURCE"


def test_a_second_run_writes_nothing_more(store, ingest):
    sim(ingest)
    runner = simulator(store, [FakeSource("alpaca", minute_bars(SESSION, QUIET))], ready_clock())
    assert runner.run_due() == 1 and runner.run_due() == 0 and len(store.fetch("simulated_outcomes", {})) == 1


def test_the_stop_is_used_when_bars_show_it(store, ingest):
    sim(ingest)
    bars = minute_bars(SESSION, QUIET)
    bars[2] = Bar(bars[2].start, 99.0, 99.2, 97.0, 97.5)
    simulator(store, [FakeSource("alpaca", bars)], ready_clock()).run_due()
    assert (outcome(store)["exit_kind"], outcome(store)["exit_price"]) == ("STOP", 98.0)


def test_early_close_uses_the_early_flatten_start(store):
    day = dt.date(2026, 11, 27)                      # the day after Thanksgiving: close 13:00 ET, flatten 12:45 ET
    ingest = make_ingest(store)
    sim(ingest, record_id="sim-0000005", opportunity_id="sig-5", decided_at="2026-11-27T15:00:00+00:00")
    early = [1501, 1530, 1600, 1630, 1700, 1730, 1745]              # UTC; 17:45 UTC is 12:45 ET
    clock = Clock(SessionSimulator.data_ready_at(day) + dt.timedelta(minutes=1))
    simulator(store, [FakeSource("alpaca", minute_bars(day, early, 102.0))], clock).run_due()
    row = outcome(store)
    assert (row["status"], row["exit_kind"], row["exit_price"]) == ("COMPLETE", "FLATTEN", 102.0)
    assert row["exit_at"] == dt.datetime(2026, 11, 27, 17, 45, tzinfo=UTC)
```

`tests/scoreboard/test_bar_sources.py` (no network; fakes only):

```python
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from trader.scoreboard.bar_sources import AlpacaBars, BarSourceError, default_bar_sources, frame_to_bars

UTC = dt.timezone.utc


def frame(tz):
    index = pd.DatetimeIndex(["2026-10-06 14:31", "2026-10-06 14:32"], tz=tz)
    return pd.DataFrame({"open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5], "close": [1.2, 2.2]}, index=index)


def test_frames_become_utc_bars_whatever_their_timezone():
    assert [b.start for b in frame_to_bars(frame("UTC"))] == [dt.datetime(2026, 10, 6, 14, 31, tzinfo=UTC),
                                                                dt.datetime(2026, 10, 6, 14, 32, tzinfo=UTC)]
    assert frame_to_bars(frame("US/Eastern"))[0].start == dt.datetime(2026, 10, 6, 18, 31, tzinfo=UTC)
    assert frame_to_bars(pd.DataFrame()) == [] and frame_to_bars(None) == []


class FakeProvider:
    def __init__(self, frame=None, error=None):
        self.frame, self.error, self.asked = frame, error, []

    def get_history(self, ticker, bar_size, start, end, timezone="US/Eastern"):
        self.asked.append(ticker)
        if self.error:
            raise self.error
        return self.frame


def security(**changes):
    values = dict(symbol="AAPL", secType="STK", currency="USD")
    values.update(changes)
    return SimpleNamespace(**values)


START, END = dt.datetime(2026, 10, 6, 14, 31, tzinfo=UTC), dt.datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def test_alpaca_bars_are_asked_by_the_resolved_ticker_and_filtered_to_the_window():
    provider = FakeProvider(frame("UTC"))
    bars = AlpacaBars(provider, lambda conid: security()).bars(265598, START, END)
    assert provider.asked == ["AAPL"] and len(bars) == 2


@pytest.mark.parametrize("found,code", [(None, "SYMBOL_UNRESOLVED"), (security(secType="OPT"), "UNSUPPORTED_INSTRUMENT"),
                                        (security(currency="CAD"), "UNSUPPORTED_INSTRUMENT")])
def test_an_inexact_instrument_is_refused_not_guessed(found, code):
    with pytest.raises(BarSourceError, match=code):
        AlpacaBars(FakeProvider(frame("UTC")), lambda conid: found).bars(1, START, END)


def test_a_provider_failure_keeps_only_the_error_class_name():
    provider = FakeProvider(error=RuntimeError("https://x/?key=SECRET"))
    with pytest.raises(BarSourceError) as exc:
        AlpacaBars(provider, lambda conid: security()).bars(1, START, END)
    assert str(exc.value) == "ALPACA_RuntimeError" and "SECRET" not in str(exc.value)


def test_blank_alpaca_keys_leave_only_the_local_source(caplog):
    trader = SimpleNamespace(history_duckdb_path="/x/h.duckdb", alpaca_api_key_id="", alpaca_api_secret_key="")
    assert [s.name for s in default_bar_sources(trader)] == ["history_duckdb"]
    assert "SECRET" not in caplog.text


def test_set_alpaca_keys_add_the_alpaca_source():
    trader = SimpleNamespace(history_duckdb_path="/x/h.duckdb", alpaca_api_key_id="id", alpaca_api_secret_key="s",
                             universe_accessor=SimpleNamespace(resolve_symbol=lambda conid, first_only: []))
    assert [s.name for s in default_bar_sources(trader)] == ["history_duckdb", "alpaca"]
```

Add to `tests/scoreboard/test_wiring.py`:

```python
def test_the_tick_runs_the_simulator_and_survives_its_failure(prod_stack, caplog):
    _trader_, stack = prod_stack
    calls = []

    def boom():
        calls.append("simulate")
        raise RuntimeError("bars down")
    stack.scoreboard.simulator.run_due = boom
    stack.scoreboard.tick()                       # must not raise
    assert calls == ["simulate"] and "scoreboard simulation failed" in caplog.text
```

and to `tests/test_trading_runtime.py`:

```python
_ALPACA_KWARGS = dict(
    ib_server_address='127.0.0.1', ib_server_port=7497, trading_runtime_ib_client_id=5, ib_account='DU12345',
    duckdb_path='/tmp/mmr.duckdb', universe_library='Universes', zmq_pubsub_server_address='tcp://127.0.0.1',
    zmq_pubsub_server_port=42002, zmq_rpc_server_address='tcp://127.0.0.1', zmq_rpc_server_port=42001,
    zmq_strategy_rpc_server_address='tcp://127.0.0.1', zmq_strategy_rpc_server_port=42005,
    zmq_messagebus_server_address='tcp://127.0.0.1', zmq_messagebus_server_port=42006)


def test_trader_keeps_the_alpaca_keys_for_baseline_simulation():
    blank = Trader(**_ALPACA_KWARGS)
    assert (blank.alpaca_api_key_id, blank.alpaca_api_secret_key) == ('', '')
    trader = Trader(**_ALPACA_KWARGS, alpaca_api_key_id='id', alpaca_api_secret_key='s')
    assert (trader.alpaca_api_key_id, trader.alpaca_api_secret_key) == ('id', 's')
```

- [ ] **Step 2: Run, expect failure** `.venv/bin/python -m pytest tests/scoreboard/test_session_simulator.py tests/scoreboard/test_bar_sources.py -q --timeout=30`.
- [ ] **Step 3: Implement** the files and edits above.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard/test_session_simulator.py tests/scoreboard/test_bar_sources.py tests/scoreboard/test_simulator.py tests/scoreboard/test_wiring.py tests/test_trading_runtime.py -q --timeout=30` → pass.
- [ ] **Step 5: Commit** `feat: simulate baseline outcomes from 1-minute bars at session end`.

---

### Task 6: Separate books and cost labels in the report and readback

**Files:**
- Create: `trader/scoreboard/books.py`
- Modify: `trader/scoreboard/report.py`, `trader/scoreboard/service.py`, `trader/scoreboard/summary_text.py`
- Test: `tests/scoreboard/test_books.py`; modify `tests/scoreboard/test_report.py`, `tests/scoreboard/test_summary_text.py`

**Interfaces:**
- Produces:
  - `build_books(decisions: Sequence[Mapping], outcomes: Sequence[Mapping]) -> list[dict]` (shape in Cross-plan additions).
  - `summarize_costs(rows: Sequence[Mapping]) -> dict` (the `ai_cost` shape).
  - `ReportInputs` loses `simulated`, gains `sim_decisions: Sequence[Mapping]` and `sim_outcomes: Sequence[Mapping]`; `ai_costs` now holds all `ai_costs` rows of the experiment (originals and corrections).
  - `PNL_BASIS = "gross, no commissions or slippage"`.

`books.py`:

```python
"""Separate baseline books and cost totals, built at read time from sealed rows (SP2 Plan 2, rulings 1, 15, 16).

A book is the group of simulated decisions with one (baseline_id, cohort). Books are never summed together.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

PNL_BASIS = "gross, no commissions or slippage"
LABEL = "simulated"


def build_books(decisions: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]]) -> list[dict]:
    outcome_of = {o["record_id"]: o for o in outcomes}
    grouped: dict[tuple[str, str], list[Mapping]] = defaultdict(list)
    for decision in decisions:
        grouped[(decision["baseline_id"], decision["cohort"])].append(decision)
    books = []
    for (baseline_id, cohort), members in sorted(grouped.items()):
        found = [outcome_of.get(m["record_id"]) for m in members]
        complete = [o for o in found if o is not None and o["status"] == "COMPLETE"]
        incomplete = [o for o in found if o is not None and o["status"] == "INCOMPLETE"]
        pending = len(found) - len(complete) - len(incomplete)
        status = "INCOMPLETE" if incomplete else ("PENDING" if pending else "COMPLETE")
        known = float(sum(o["pnl_usd"] for o in complete))
        books.append({
            "baseline_id": baseline_id, "cohort": cohort, "label": LABEL, "pnl_basis": PNL_BASIS,
            "status": status, "records": len(members), "complete": len(complete), "incomplete": len(incomplete),
            "pending": pending, "trades": sum(o["trades"] or 0 for o in complete),
            "pnl_usd": known if status == "COMPLETE" else None,
            "known_pnl_usd": known if complete else None,
            "incomplete_reasons": dict(Counter(o["reason"] for o in incomplete)),
        })
    return books


def _effective_costs(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping], int]:
    originals = [r for r in rows if r["corrects_record_id"] is None]
    latest: dict[str, Mapping] = {}
    for row in rows:
        target = row["corrects_record_id"]
        if target is not None and (target not in latest or row["correction_seq"] > latest[target]["correction_seq"]):
            latest[target] = row
    return [latest.get(o["record_id"], o) for o in originals], len(rows) - len(originals)


def summarize_costs(rows: Sequence[Mapping[str, Any]]) -> dict:
    effective, corrections = _effective_costs(rows)
    confirmed = float(sum(r["cost_usd"] for r in effective if r["cost_status"] == "confirmed"))
    estimated = float(sum(r["cost_usd"] for r in effective if r["cost_status"] == "estimated"))
    unknown = sum(1 for r in effective if r["cost_status"] == "unknown")
    if not effective:
        status = "NONE"
    elif unknown:
        status = "INCOMPLETE"
    else:
        status = "ESTIMATED" if estimated or any(r["cost_status"] == "estimated" for r in effective) else "CONFIRMED"
    return {"status": status, "calls": len(effective), "confirmed_usd": confirmed, "estimated_usd": estimated,
            "unknown_calls": unknown, "corrections": corrections,
            "total_usd": None if status in ("NONE", "INCOMPLETE") else confirmed + estimated}
```

`report.py` `_benchmarks`: delete the `simulated` block and the old cost block; replace with

```python
    cost = summarize_costs(inputs.ai_costs)
    unavailable = cost["status"] == "NONE"
    pnl = account["pnl_usd"]
    return {"spy": spy, "vs_spy_pp": vs_spy, "books": build_books(inputs.sim_decisions, inputs.sim_outcomes),
            "ai_cost": cost, "ai_cost_usd": cost["total_usd"], "ai_calls": None if unavailable else cost["calls"],
            "ai_costs_status": "UNAVAILABLE" if unavailable else "AVAILABLE",
            "pnl_minus_ai_cost_usd": None if pnl is None or cost["total_usd"] is None else pnl - cost["total_usd"]}
```
(import `build_books`, `summarize_costs` from `trader.scoreboard.books`; drop `_sum_or_none` if now unused, check with grep). `ReportInputs`: replace `simulated` with `sim_decisions`, `sim_outcomes`. `service.py` `report()` / `_inputs`: pass `self.store.fetch("ai_costs", {"experiment_id": exp_id})`, `self.store.fetch("simulated_decisions", {"experiment_id": exp_id})`, `self.store.fetch("simulated_outcomes", {"experiment_id": exp_id})`; for `experiment is None` pass empty lists. `summary_text.py`: replace the `ai_cost` line source with

```python
    cost = bench["ai_cost"]
    ai_cost = ("unavailable" if cost["status"] == "NONE" else
               f"{_money(cost['total_usd'])} ({cost['status'].lower()}; {cost['unknown_calls']} unknown call(s))")
    books_line = (f"Baselines: {len(bench['books'])} books, "
                  f"{sum(1 for b in bench['books'] if b['status'] != 'COMPLETE')} not complete")
```
and add `books_line` after the AI cost line in `lines`.

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/test_books.py`)

```python
import datetime as dt

from trader.scoreboard.books import build_books, summarize_costs


def decision(record_id, baseline="follow_signal.v1", cohort="strategy_signal"):
    return {"record_id": record_id, "baseline_id": baseline, "cohort": cohort}


def outcome(record_id, status="COMPLETE", pnl=1.0, reason=None, trades=1):
    return {"record_id": record_id, "status": status, "pnl_usd": None if status == "INCOMPLETE" else pnl,
            "reason": reason, "trades": trades}


def test_books_are_separate_and_never_summed():
    books = build_books(
        [decision("a"), decision("b"), decision("c", "fixed_rule.v1", "self_found"),
         decision("d", "no_trade.v1", "self_found")],
        [outcome("a", pnl=10.0), outcome("b", pnl=5.0), outcome("c", pnl=-3.0), outcome("d", pnl=0.0, trades=0)])
    by_id = {(b["baseline_id"], b["cohort"]): b for b in books}
    assert [b["baseline_id"] for b in books] == ["fixed_rule.v1", "follow_signal.v1", "no_trade.v1"]
    assert by_id[("follow_signal.v1", "strategy_signal")]["pnl_usd"] == 15.0
    assert by_id[("fixed_rule.v1", "self_found")]["pnl_usd"] == -3.0
    assert by_id[("no_trade.v1", "self_found")]["pnl_usd"] == 0.0
    assert all(b["status"] == "COMPLETE" and b["label"] == "simulated" for b in books)


def test_the_same_baseline_in_two_cohorts_is_two_books():
    books = build_books([decision("a"), decision("b", cohort="self_found")], [outcome("a"), outcome("b")])
    assert len(books) == 2


def test_incomplete_book_does_not_hide_complete_books():
    books = build_books([decision("a"), decision("b"), decision("c", "fixed_rule.v1", "self_found")],
                        [outcome("a", pnl=10.0), outcome("b", "INCOMPLETE", reason="alpaca:NO_BARS"),
                         outcome("c", pnl=2.0)])
    bad, good = books[1], books[0]
    assert (good["baseline_id"], good["status"], good["pnl_usd"]) == ("fixed_rule.v1", "COMPLETE", 2.0)
    assert (bad["status"], bad["pnl_usd"], bad["known_pnl_usd"], bad["complete"], bad["incomplete"]) == (
        "INCOMPLETE", None, 10.0, 1, 1)
    assert bad["incomplete_reasons"] == {"alpaca:NO_BARS": 1}


def test_a_record_without_an_outcome_is_pending_not_zero():
    book = build_books([decision("a"), decision("b")], [outcome("a", pnl=4.0)])[0]
    assert (book["status"], book["pending"], book["pnl_usd"], book["known_pnl_usd"]) == ("PENDING", 1, None, 4.0)
    only = build_books([decision("a")], [])[0]
    assert (only["status"], only["known_pnl_usd"]) == ("PENDING", None)


def cost(record_id, status="confirmed", usd=1.0, corrects=None, seq=0):
    return {"record_id": record_id, "cost_status": status, "cost_usd": None if status == "unknown" else usd,
            "corrects_record_id": corrects, "correction_seq": seq}


def test_no_cost_rows_is_none_not_zero():
    summary = summarize_costs([])
    assert (summary["status"], summary["total_usd"], summary["calls"]) == ("NONE", None, 0)


def test_cost_statuses_are_labelled_and_unknown_hides_the_total():
    assert summarize_costs([cost("a"), cost("b", usd=2.0)])["status"] == "CONFIRMED"
    est = summarize_costs([cost("a"), cost("b", "estimated", 0.5)])
    assert (est["status"], est["confirmed_usd"], est["estimated_usd"], est["total_usd"]) == ("ESTIMATED", 1.0, 0.5, 1.5)
    unknown = summarize_costs([cost("a"), cost("b", "unknown")])
    assert (unknown["status"], unknown["unknown_calls"], unknown["total_usd"]) == ("INCOMPLETE", 1, None)


def test_correction_is_counted_once_and_status_labelled():
    rows = [cost("a", "estimated", 0.4), cost("b", "confirmed", 0.55, corrects="a", seq=1),
            cost("c", "confirmed", 0.56, corrects="a", seq=2)]
    summary = summarize_costs(rows)
    assert (summary["calls"], summary["corrections"], summary["confirmed_usd"], summary["status"]) == (
        1, 2, 0.56, "CONFIRMED")
    assert summary["total_usd"] == 0.56
```

Update `tests/scoreboard/test_report.py`: `empty_inputs` takes `sim_decisions=[], sim_outcomes=[]` instead of `simulated=[]`. `_cost` becomes `{"record_id": "c", "cost_usd": cost, "cost_status": "unknown" if cost is None else "confirmed", "corrects_record_id": None, "correction_seq": 0}` (give each call its own `record_id`: use a counter). `test_report_with_no_sessions_is_all_unknown` replaces its `simulated` assertion with `assert r["benchmarks"]["books"] == [] and r["benchmarks"]["ai_cost"]["status"] == "NONE"`. `test_unknown_ai_cost_makes_pnl_minus_cost_unknown`, `test_no_ai_cost_rows_is_unavailable_not_zero` and `test_pnl_minus_ai_cost_uses_account_pnl_in_usd` keep their expectations. Replace `test_simulated_rows_are_shown_with_the_simulated_label` with:

```python
def test_books_are_listed_separately_with_the_simulated_label():
    decisions = [{"record_id": "a", "baseline_id": "follow_signal.v1", "cohort": "strategy_signal"},
                 {"record_id": "b", "baseline_id": "no_trade.v1", "cohort": "self_found"}]
    outcomes = [{"record_id": "a", "status": "COMPLETE", "pnl_usd": 3.0, "reason": None, "trades": 1},
                {"record_id": "b", "status": "COMPLETE", "pnl_usd": 0.0, "reason": None, "trades": 0}]
    b = build_report(full_inputs(sim_decisions=decisions, sim_outcomes=outcomes))["benchmarks"]
    assert "simulated" not in b
    assert [(x["baseline_id"], x["label"], x["pnl_usd"]) for x in b["books"]] == [
        ("follow_signal.v1", "simulated", 3.0), ("no_trade.v1", "simulated", 0.0)]


def test_estimated_cost_is_labelled_and_counted_in_pnl_minus_cost():
    estimated = {**_cost(1.5), "record_id": "e", "cost_status": "estimated"}
    b = build_report(full_inputs(ai_costs=[_cost(0.5), estimated]))["benchmarks"]
    assert b["ai_cost"]["status"] == "ESTIMATED" and b["ai_cost_usd"] == pytest.approx(2.0)
    assert b["pnl_minus_ai_cost_usd"] == pytest.approx(498.0)
```

`tests/scoreboard/test_summary_text.py`: the `report()` builder's `benchmarks` dict gets `"ai_cost": {"status": "NONE", "total_usd": None, "unknown_calls": 0}` and `"books": []`; add

```python
def test_summary_labels_estimated_cost_and_counts_incomplete_books():
    data = report()
    data["benchmarks"]["ai_costs_status"] = "AVAILABLE"
    data["benchmarks"]["ai_cost"] = {"status": "ESTIMATED", "total_usd": 2.0, "unknown_calls": 0}
    data["benchmarks"]["books"] = [{"status": "COMPLETE"}, {"status": "INCOMPLETE"}]
    text = format_daily_summary(data, D)
    assert "$2.00 (estimated; 0 unknown call(s))" in text and "Baselines: 2 books, 1 not complete" in text
```

- [ ] **Step 2: Run, expect failure** `.venv/bin/python -m pytest tests/scoreboard/test_books.py tests/scoreboard/test_report.py tests/scoreboard/test_summary_text.py -q --timeout=30`.
- [ ] **Step 3: Implement.** **Step 4: Run** the same command plus `tests/scoreboard/test_verify.py tests/scoreboard/test_surface.py` → pass.
- [ ] **Step 5: Commit** `feat: report one simulated book per baseline and cohort and label ai costs`.

---

### Task 7: CLI, dashboard script and docs

**Files:**
- Modify: `trader/mmr_cli.py` (`_scoreboard_lines`), `web/static/command_center_scoreboard.js`, `web/static/command_center_scoreboard.test.js`, `tests/scoreboard/test_cli.py`, `docs/ARCHITECTURE.md`, `docs/CLI_REFERENCE.md`
- Test: `tests/scoreboard/test_cli.py`, `tests/test_command_center_scoreboard_js.py` (existing wrapper), `tests/test_web_scoreboard_routes.py` (check only)

**Interfaces:** consumes the report shape of Task 6. `routes_scoreboard.py` only forwards the report, so it needs no change; confirm by running its test.

CLI: in `_scoreboard_lines` replace the `simulated = bench['simulated']` block and the `AI cost` line with:

```python
    books = bench['books']
    lines.append('simulated baselines (one book each, never summed; ' + (books[0]['pnl_basis'] if books else 'none yet') + ')')
    for book in books:
        detail = (f"P&L {_dash(book['pnl_usd'], '${:,.2f}')}" if book['status'] == 'COMPLETE' else
                  f"P&L - (known so far {_dash(book['known_pnl_usd'], '${:,.2f}')} from {book['complete']} of "
                  f"{book['records']} records)")
        reasons = ''.join(f"; {reason} x{count}" for reason, count in book['incomplete_reasons'].items())
        lines.append(f"  {book['baseline_id']} / {book['cohort']} (simulated): {book['status']}, "
                     f"{book['records']} records, {detail}{reasons}")
    cost = bench['ai_cost']
    ai_cost = ('unavailable' if cost['status'] == 'NONE' else
               f"{_dash(cost['total_usd'], '${:,.2f}')} {cost['status'].lower()} "
               f"(confirmed {_dash(cost['confirmed_usd'], '${:,.2f}')}, estimated "
               f"{_dash(cost['estimated_usd'], '${:,.2f}')}, unknown calls {cost['unknown_calls']}, "
               f"{cost['calls']} calls)")
    lines.append(f"AI cost: {ai_cost}   P&L minus AI cost: {_dash(bench['pnl_minus_ai_cost_usd'], '${:,.2f}')}")
```

Dashboard script (`command_center_scoreboard.js`): in `benchmarks(report)` remove the `simulated` and `aiCost` locals and the `Baseline` / `AI` rows. Add a `books(report)` section, called from `renderScoreboard` right after `benchmarks(report)`: one `<tr data-book-status="…">` per book with the columns Baseline, Cohort, Label (`<span class="sb-label">simulated</span>`), Status (`<span class="sb-state" data-book-state="COMPLETE|INCOMPLETE|PENDING">`), Records, P&L, Why incomplete. A `COMPLETE` book shows `formatMoney(pnl_usd)`. Any other book shows the dash plus `<span class="sb-label">partial: $X from N of M</span>` built from `known_pnl_usd`, `complete`, `records`. The reasons column joins `reason xCount` pairs. The section prints `pnl_basis` once above the table, and `No simulated baseline yet.` when the list is empty. Every value goes through `escapeHtml`. Replace the AI row with `AI cost: ` + `aiCostText(b.ai_cost)`: `unavailable` when `status === 'NONE'`, else `formatMoney(total_usd)` (a dash for null), `<span class="sb-label">` + lower-case status + `</span>`, then `(confirmed $a, estimated $b, unknown calls n, N calls)`.

JS tests (`command_center_scoreboard.test.js`): `EMPTY_REPORT.benchmarks` becomes `{spy, vs_spy_pp: null, books: [], ai_cost: {status: 'NONE', calls: 0, confirmed_usd: 0, estimated_usd: 0, unknown_calls: 0, corrections: 0, total_usd: null}, ai_cost_usd: null, ai_calls: null, ai_costs_status: 'UNAVAILABLE', pnl_minus_ai_cost_usd: null}`. Replace the old `simulated` tests with these, using a `BOOK(extra)` factory (`baseline_id: 'follow_signal.v1', cohort: 'strategy_signal', label: 'simulated', pnl_basis: 'gross, no commissions or slippage', status: 'COMPLETE', records: 2, complete: 2, incomplete: 0, pending: 0, trades: 2, pnl_usd: 7, known_pnl_usd: 7, incomplete_reasons: {}`):
- two books (`$7.00` and `-$3.00`): the html matches both baseline ids, both amounts and `gross, no commissions or slippage`, and does **not** match `$4.00` (nothing is summed);
- one `COMPLETE` book and one `INCOMPLETE` (`pnl_usd: null`, `known_pnl_usd: 10`, `complete: 1`, reasons `{'alpaca:NO_BARS': 1}`): the html has both `data-book-state="INCOMPLETE"` and `data-book-state="COMPLETE"`, matches `alpaca:NO_BARS x1` and `partial: \$10\.00 from 1 of 2`;
- `ai_cost` with status `INCOMPLETE`, `total_usd: null`, `unknown_calls: 1`: the html matches `AI cost[^<]*—[^<]*<span class="sb-label">incomplete</span>` and `unknown calls 1`; `EMPTY_REPORT` matches `AI cost[^<]*unavailable` and never `AI cost[^<]*\$0\.00`;
- a book with `baseline_id: '<img src=x>'` renders without the raw tag.

CLI tests (`test_cli.py`): the `REPORT` fixture's `benchmarks` gets `books` and `ai_cost` as above; keep `"AI cost: unavailable"` and add `test_table_output_shows_each_book_with_its_status` (two books, one `INCOMPLETE`: output contains both `follow_signal.v1 / strategy_signal (simulated): INCOMPLETE` and `fixed_rule.v1 / self_found (simulated): COMPLETE`, contains `known so far`, and does not contain `None` or `nan`).

Docs: in `docs/ARCHITECTURE.md`, under the existing scoreboard section (find it with `grep -n -i scoreboard docs/ARCHITECTURE.md`), add a short paragraph: the two ingestion commands, `ai_supervisor` only; sealed tables `ai_costs`, `simulated_decisions`, `simulated_outcomes`; the simulator rules in one sentence each (long only, stop wins, flatten bar open, 30-minute hole, grace); books never summed. In `docs/CLI_REFERENCE.md`, in the `scoreboard` entry, describe the new `simulated baselines` lines and the cost status labels.

- [ ] **Step 1: Write the failing tests** (above). **Step 2: Run** `.venv/bin/python -m pytest tests/scoreboard/test_cli.py tests/test_command_center_scoreboard_js.py -q --timeout=60` and `node web/static/command_center_scoreboard.test.js` → fail. **Step 3: Implement.** **Step 4: Run** the same two commands plus `.venv/bin/python -m pytest tests/test_web_scoreboard_routes.py -q --timeout=30` → pass. **Step 5: Commit** `feat: show separate simulated books and cost status labels`.

---

### Task 8: Acceptance tests and the full suite

**Files:**
- Test: `tests/scoreboard/test_baseline_books_acceptance.py`
- Modify (only if a stale reference remains): any file that `grep -rn "simulated_books\|record_simulated_row\|benchmarks\]\[.simulated\|inputs import" trader tests web` still lists.

**Interfaces:** consumes everything above through the real `ScoreboardService.report`, `SessionSimulator` and `AiIngest`, on the `world` fixture's store.

- [ ] **Step 1: Write the acceptance tests**

```python
import datetime as dt

import pytest

from tests.scoreboard.common import EXP_ID
from tests.scoreboard.ingest_world import make_ingest, no_trade_body, sim, sim_body
from tests.scoreboard.test_session_simulator import FakeSource, QUIET, SESSION, minute_bars
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.scoreboard.session_simulator import GRACE, SessionSimulator
from trader.scoreboard.simulator import Bar


@pytest.fixture
def world_ingest(world):
    return make_ingest(world.store)


class ByConid:
    name = "alpaca"

    def __init__(self, by_conid):
        self.by_conid = by_conid

    def bars(self, conid, start, end):
        return [b for b in self.by_conid.get(conid, []) if start <= b.start < end]


def books_of(scoreboard):
    scoreboard.refresh()
    return {(b["baseline_id"], b["cohort"]): b for b in scoreboard.report(EXP_ID)["benchmarks"]["books"]}


def run(world, sources, extra):
    clock = world.clock
    clock[0] = SessionSimulator.data_ready_at(SESSION) + extra
    SessionSimulator(store=world.store, calendar=XNYSCalendarPolicy(), sources=sources, now=lambda: clock[0]).run_due()


def seed_three_books(ingest):
    sim(ingest, sim_body())                                                     # follow_signal, conid 265598
    sim(ingest, sim_body(record_id="sim-0000002", baseline_id="fixed_rule.v1", cohort="self_found",
                         opportunity_id="cycle-1", conid=4815747))
    sim(ingest, no_trade_body())


def test_three_baselines_report_as_three_separate_books(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    run(world, [FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))], dt.timedelta(minutes=1))
    books = books_of(scoreboard)
    assert set(books) == {("follow_signal.v1", "strategy_signal"), ("fixed_rule.v1", "self_found"),
                          ("no_trade.v1", "self_found")}
    assert books[("follow_signal.v1", "strategy_signal")]["pnl_usd"] == 10.0
    assert books[("fixed_rule.v1", "self_found")]["pnl_usd"] == 10.0
    assert books[("no_trade.v1", "self_found")]["pnl_usd"] == 0.0 and books[("no_trade.v1", "self_found")]["trades"] == 0
    assert world.store.verify_seals() == []


def test_incomplete_book_does_not_hide_complete_books(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    source = ByConid({265598: minute_bars(SESSION, QUIET, 101.0)})        # no bars for conid 4815747
    run(world, [source], GRACE)
    books = books_of(scoreboard)
    bad = books[("fixed_rule.v1", "self_found")]
    assert (bad["status"], bad["pnl_usd"], bad["incomplete"]) == ("INCOMPLETE", None, 1)
    assert books[("follow_signal.v1", "strategy_signal")]["status"] == "COMPLETE"
    assert books[("no_trade.v1", "self_found")]["status"] == "COMPLETE"


def test_nothing_in_the_report_adds_books_together(scoreboard, world, world_ingest):
    seed_three_books(world_ingest)
    run(world, [FakeSource("alpaca", minute_bars(SESSION, QUIET, 101.0))], dt.timedelta(minutes=1))
    scoreboard.refresh()
    benchmarks = scoreboard.report(EXP_ID)["benchmarks"]
    assert "simulated" not in benchmarks and len(benchmarks["books"]) == 3
    assert all(set(b) >= {"baseline_id", "cohort", "status", "pnl_usd", "label"} for b in benchmarks["books"])


def test_costs_in_the_report_carry_status_and_a_correction_changes_the_total_once(scoreboard, world_ingest):
    from tests.scoreboard.ingest_world import cost
    cost(world_ingest, cost_status="estimated", cost_usd=0.4)
    cost(world_ingest, record_id="cost-0000002", attempt_id="att-0000002", cost_status="unknown", cost_usd=None)
    scoreboard.refresh()
    bench = scoreboard.report(EXP_ID)["benchmarks"]
    assert (bench["ai_cost"]["status"], bench["ai_cost_usd"], bench["pnl_minus_ai_cost_usd"]) == ("INCOMPLETE", None, None)
    cost(world_ingest, record_id="cost-0000003", attempt_id="att-0000002", corrects_record_id="cost-0000002",
         cost_status="confirmed", cost_usd=0.6)
    cost(world_ingest, record_id="cost-0000004", corrects_record_id="cost-0000001", cost_status="confirmed", cost_usd=0.45)
    bench = scoreboard.report(EXP_ID)["benchmarks"]
    assert (bench["ai_cost"]["status"], bench["ai_cost"]["calls"], bench["ai_cost"]["corrections"]) == ("CONFIRMED", 2, 2)
    assert bench["ai_cost"]["total_usd"] == pytest.approx(1.05)


def test_the_report_reads_through_the_signed_rpc(scoreboard, world_ingest):
    from tests.rpc_identity_fixtures import ServedStack, make_identities
    from tests.scoreboard.ingest_world import cost_body
    from trader.messaging.ai_ingest_surface import register_ai_ingest_surface
    from trader.messaging.principals import TRADER_ACL
    from trader.messaging.scoreboard_surface import register_scoreboard_surface
    from trader.messaging.typed_rpc import TypedRpcRegistry
    command = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_ai_ingest_surface(command, world_ingest)
    query = TypedRpcRegistry(acl=TRADER_ACL, default_execution="thread")
    register_scoreboard_surface(query, scoreboard)
    stack = ServedStack({("trader", "command"): command, ("trader", "query"): query}, make_identities())
    try:
        ai = stack.client("ai_supervisor", role="command")
        assert ai.call("record_ai_cost", cost_body(), dict)["status"] == "INSERTED"
        assert ai.call("record_simulated_decision", no_trade_body(), dict)["status"] == "INSERTED"
        report = stack.client("dashboard").call("get_scoreboard", {"experiment_id": EXP_ID}, dict)
    finally:
        stack.close()
    bench = report["benchmarks"]
    assert [b["baseline_id"] for b in bench["books"]] == ["no_trade.v1"] and bench["books"][0]["pnl_usd"] == 0.0
    assert (bench["ai_cost"]["calls"], bench["ai_cost"]["status"]) == (1, "CONFIRMED")
```

The cost test in this file needs `cost(...)` calls whose corrected records repeat the original identity; `cost(..., record_id="cost-0000003", attempt_id="att-0000002", corrects_record_id="cost-0000002", ...)` repeats `role`, `provider`, `model`, `called_at` from `cost_body` defaults, as required. `cost_body`'s `attempt_id` default is `att-0000001`; the second call overrides it, and the correction of the first call keeps the default.

- [ ] **Step 2: Run, expect pass or fix** `.venv/bin/python -m pytest tests/scoreboard/test_baseline_books_acceptance.py -q --timeout=60`. Failures here point at a bug in Tasks 2–6; fix there, not here.
- [ ] **Step 3: Stale references.** `grep -rn "simulated_books\|record_simulated_row\|trader.scoreboard.inputs" trader tests web docs/*.md` must return nothing (docs under `docs/superpowers` are plans, leave them).
- [ ] **Step 4: Full suite** `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py` → green. Also run `node web/static/command_center_scoreboard.test.js`.
- [ ] **Step 5: Commit** `test: add baseline book acceptance tests`.

---

## Self-review

- Spec 6.3, 6.8, 7 are covered by Tasks 1–3 (ingestion), 4–5 (simulator), 6–7 (books and cost labels in report, readback, CLI, dashboard). The owner update (no legacy data, in-place edits, plain CREATEs) is applied in Task 1.
- Names are the same in every task and in Cross-plan additions: `record_id`, `body_digest`, `correction_seq`, `AiIngest`, `IngestRefused`, `ingest_sealed_many`, `SessionSimulator`.
- Each Review Focus line names a test that exists in Tasks 1–8.
