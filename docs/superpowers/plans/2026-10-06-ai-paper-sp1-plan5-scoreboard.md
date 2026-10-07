# AI Paper SP1 — Plan 5: Scoreboard, `/cc` Tab and Telegram Daily Summary — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the tasks in number order; each task ends with the full suite green.

**Goal:** Give the paper bot an honest referee: durable per-session equity history, a round-trip projection rebuilt from broker fills, SPY and cost benchmarks, a tamper-evident `verify`, `mmr --json scoreboard`, a Scoreboard tab on `/cc`, and a send-only Telegram daily summary behind a config gate.

**Architecture:** A new package `trader/scoreboard/` lives inside `trader_service` and writes only to the trader-owned journal DuckDB file (`trader.journal_db`). Pure functions (round-trip walker, metrics, report builder, summary text) sit under thin stores and ports. Everything outside the dashboard reads through two new typed queries (`get_scoreboard`, `verify_scoreboard`); the dashboard and the CLI never open the journal. Inputs that other plans own (experiments, attribution links, kill end-state) come in through small ports defined here.

**Tech Stack:** Python 3.12, DuckDB (`DuckDBConnection`, `SchemaMigrator`), `exchange_calendars` (via `XNYSCalendarPolicy`), `httpx` (already a dependency), FastAPI + Jinja + plain JS (node tests), pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, section 5.2 "Scoreboard", the scoreboard bullets of section 6, the Telegram and kill-alert lines of sections 2, 5.5 and 9, and delivery step 5 of section 7. The spec is `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md` (lands with PR #46; not on master. Immutable source: PR #46 commit `2a021c204798907736b7d3380901c0d7f40e7ac6`. Read it with `git fetch origin 2a021c204798907736b7d3380901c0d7f40e7ac6 && git show 2a021c204798907736b7d3380901c0d7f40e7ac6:docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, or `gh pr checkout 46`.) Delivery order is Plans 1, 2, 3, 4, 5: Plans 1–4 are on master before Task 1 starts (see "Interfaces assumed").

## Global Constraints

- Written only by trader_service (spec 5.2): no AI container and no dashboard process mounts or opens the journal DB. The dashboard and CLI call `get_scoreboard` / `verify_scoreboard` over typed RPC.
- Reporting currency is USD (spec 5.2). Net liquidation is stored in the base currency **and** USD, with the FX rate, its source and its time. "A row with no FX evidence for a non-USD base is not written as USD. It is an incident."
- Append-only inputs: `equity_daily` is never edited; a late commission becomes an `equity_adjustments` row; a data refresh never changes a stored `benchmark_prices` row; a correction is a new benchmark version.
- "Every view says 'paper'. Nothing on it is proof of live edge." (spec 5.2). The CLI, the JSON, the tab and the Telegram text all carry the label.
- Unknown is unknown: a number that cannot be computed is `null` in JSON and `—` in the tab (`-` in Telegram and CLI tables), never `0`. Counts (`sessions`, `trades`) may be `0`; AI calls and simulated rows are unavailable until SP2 fills them (ruling 16).
- **Owner answers of 2026-10-06** are binding and carried by rulings 5, 6, 14, 15, 16 and 17.
- Telegram is send-only, through a durable outbox with stable event ids. The bot token comes from a secret file, only the configured chat id receives anything, and a Telegram outage only delays a message (spec 5.2). No hard-coded id or token anywhere; no token in any log, error or test output.
- Session dates are XNYS session dates, i.e. the America/New_York calendar date (`trader/automation/calendar_policy.py:12`). A UTC date is never used to bucket a fill.
- New journal migrations use version range **60–69** (existing: 1–11, 20–25, 30–34, 40–45, 50–53; `trader/data/schema_migrations.py:1-24`). Plan 1 uses 35–38, Plan 3 54–56, Plan 4 70–79.
- Test-first. Single file: `.venv/bin/python -m pytest <path> -q --timeout=30`. Full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects: `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- This plan authorizes no container restart, no broker order, no deploy and no push. Task 10 edits compose; the owner runs it.
- Line numbers cite master `8116f6a5` (this branch adds only docs on top). The function name beside each number is the anchor.

## Interfaces assumed from Plans 3–4 (and Plan 1)

Plan 5 reads data that other plans produce. Everything below is defined in `trader/scoreboard/ports.py` (Task 1) so Plan 5 is testable alone with fakes. Task 10 wires the real implementations named in the Owner column.

| # | Port | What Plan 5 needs | Taken from spec | Owner |
|---|---|---|---|---|
| A1 | `ExperimentReader.latest() -> ExperimentRecord \| None`, `.get(experiment_id)`; Plan 4's `ExperimentStore` satisfies it structurally (its record is a superset with the same names) | The `experiments` table: `experiment_id`, `account_id`, `started_at`, `start_net_liquidation` (base ccy), `base_currency`, `start_usd_per_base`, `state` in `ARMED/PAUSED/KILLED/STOPPED`, `killed_at` (nullable) | 5.2 "experiments"; 5.5 "records the start net liquidation" | Plan 4 (creates the table; Plan 5 does **not**) |
| A2 | `AttributionLookup.links_for_order_ref(ref) -> AttributionLinks \| None`. Plan 3 R24 gives `AiPaperDecisionStore.links_for_order_ref(ref) -> tuple[DecisionLink, ...]` on `trader.ai_paper_attribution`; Task 10's `DecisionStoreAttribution` adapter returns the single `ENTER` link as `AttributionLinks` (fields as `str`, `digest` = the link's deployment digest), else `None` | `decision_id`, `decider`, `strategy_version` (deployment record), `policy_revision`, `style`, and a content `digest` of those records | 5.2 "broker fill → order ref → command → decision → deployment record and policy revision" | Plan 3 |
| A3 | `SessionEnd` notice: `record_session_end(end)`, `end` with `account_id, session_date, state, ended_at` and `state` in `FLAT/KILLED/FAILED_SAFE` (Plan 4 `SessionEndPort`, `KillSessionEnd`) | Kill path tells Plan 5 the day ended `KILLED`. `FLAT` and `FAILED_SAFE` come from `SessionController` here (Task 3) | 5.2 "equity_daily … FLAT, KILLED or FAILED_SAFE" | Plan 4 (kill); Plan 1 (`FAILED_SAFE` naming) |
| A4 | `TelegramOutbox.enqueue(event_id, kind, text) -> bool` used by Plan 4 for "kill started" (Plan 4 `KillAlertPort`; Plan 4 writes the text and the event id `kill_started:{experiment_id}:{kill_seq}`) | Plan 5 defines it (Task 9) | 5.5 kill step 3 | Plan 4 calls it |
| A5 | `trader.ai_paper_config.raw_section` — Plan 3's typed `AiPaperConfig` keeps a read-only copy of the `ai_paper:` YAML section (R22) | Plan 5 parses only `raw_section.get("telegram")` (own loader, Task 9) | 5.5 "Configuration" | Plan 3 |
| A6 | Plan 2 `TRADER_ACL` keys `("query","get_scoreboard")`, `("query","verify_scoreboard")` | Added by Task 7 in `trader/messaging/principals.py` | 5.3 | Plan 2 base |

Fallbacks if an owner plan lands differently: A1 without `start_usd_per_base` makes the start USD value `null` for a non-USD base (metrics show unknown, never a guess). A2 absent (`None` lookup) leaves attribution columns `null` and the group `unattributed`.

## Rulings (spec silent or incomplete)

Each is binding for this plan. Owner-visible ones are repeated under "Open questions".

1. **`experiments` belongs to Plan 4.** Spec 5.2 lists it among the scoreboard tables, but 5.5 makes arming write it. Plan 5 only reads it (A1). Why: one writer per table.
2. **Start value of a session is derived, not captured.** `start_nlv` = the end value of the latest earlier row of the same experiment with a known end value (`start_source='prev_end'`), else the experiment's recorded start (`'experiment_start'`), else `null` (`'unknown'`). The row stores `missing_sessions_before` (XNYS sessions skipped). Why: it is deterministic from stored rows, so a restart mid-day has nothing to re-capture and cannot capture the wrong value.
3. **Session bucketing.** A fill belongs to the ET date of its `fill_time`. A fill on a date that is not an XNYS session is projected into round trips but belongs to no `equity_daily` row; the report lists it under `warnings` (`FILL_OUTSIDE_SESSION`). Why: spec says sessions; dropping or guessing a session would hide money.
4. **Session end mapping.** `SessionController` state `FLAT` → `FLAT`, `INCIDENT` → `FAILED_SAFE`. If the experiment is `KILLED` with `killed_at` on that ET date, the row is `KILLED` whatever the controller says (the kill flatten ends in `FLAT` too). Why: spec 5.2 names the three end states; a kill is the cause the owner wants to see.
5. **A row and a summary for every session end (owner answer).** A row is written for `FLAT`, `KILLED` and `FAILED_SAFE`, and the Telegram summary goes out for each, its first line `PAPER — <END STATE>` in capitals so the end state is the first thing read. An `UNKNOWN` row (ruling 6) sends no summary; its incident is on the scoreboard.
6. **Failures to capture produce a row with unknowns, plus an incident, never a silent gap and never an invented value (owner answer).** Broker snapshot unavailable → end value `null`, `SNAPSHOT_UNAVAILABLE`. FX missing for a non-USD base → USD columns `null`, `FX_EVIDENCE_MISSING`. At startup, `recover` looks at every XNYS session from the experiment start to yesterday without an `equity_daily` row: a durable `SessionController` terminal row gives its mapped end state (`FLAT` / `FAILED_SAFE`, ruling 4) with `null` NLV, `null` realized P&L and `null` commissions (incident `SESSION_ROW_RECOVERED_WITHOUT_NLV`); a session with no terminal controller row gets `session_end_state = 'UNKNOWN'`, every money column `null` (incident `SESSION_MISSING`). Equity, P&L and `FLAT` are never inferred. Fill counts and `fills_digest` are still stored (they are facts) and the round trips keep the fills.
7. **Round trips.** Walked per conid in `(fill_time, exec_id)` order with an average-cost position. A trip opens when the position leaves zero and closes when it returns to zero; partial exits stay in one trip; a fill that crosses zero is split into a closing piece and an opening piece (commission pro rata). `net_pnl` is `null` when any piece lacks a USD commission. Attribution comes from the first opening fill's order ref only (exits by flatten or time exit are not decisions). A new walker is written; `trader/automation/attribution.py:233` (`rebuild_attribution_from_evidence`) is keyed by one `trade_id` and its evidence rows, so it cannot walk a conid's fills.
8. **Realized P&L and commissions are USD by assumption.** SP1 trades XNYS stocks only (spec 2, styles). A fill whose `commission_currency` is not `USD` has an unknown fee. A position currency other than `USD` makes the day's peak gross `null` (`NON_USD_POSITION`).
9. **Peak gross exposure** is the maximum of Σ|market value| over promoted snapshots seen during the session, persisted after each observation (`equity_session_peak`). If any observation had a missing market value or a non-USD position, the stored peak is `null`, not a lower bound.
10. **Late commissions.** The row stores the commission per exec id at write time (`commission_json`). When a later refresh finds a different value, one `equity_adjustments` row (kind `COMMISSION`, delta in USD) is added per exec id. A fill that appears after the row was written is an incident `LATE_FILL_AFTER_SESSION_ROW`, not an automatic adjustment.
11. **Integrity model.** `scoreboard_seals` is an append-only hash chain over every row of the scoreboard-owned input tables (`equity_daily`, `equity_adjustments`, `benchmark_versions`, `benchmark_prices`, `ai_costs`, `simulated_books`). `verify` checks the chain and each live row against its seal, recomputes `round_trips` and the fill-derived `equity_daily` columns from `broker_fills`, and checks the attribution digest against Plan 3's records. Edits to `broker_fills` are caught by the immutable `fills_digest` and the commission reconciliation of ruling 10. Why: the spec's test "detects an edited row in any input table" needs something recorded at write time to compare with.
12. **`verify` runs in trader_service** (`verify_scoreboard`, humans only). The journal lives in the named volume `mmr_db_data`; the host CLI cannot open it. AI principals get `get_scoreboard` only (spec 5.3: "scoreboard read").
13. **Reads refresh derived tables.** `get_scoreboard` first runs `refresh()` (rebuild `round_trips`, write due commission adjustments). The same refresh runs on a 30 s loop. Why: the projection is a table by spec, and a read must not be older than the broker state.
14. **Benchmark.** SPY (conid 756733, `BENCHMARK_CONID` in `trader/research/market_context.py:24`) price return, labelled **"SPY price only; dividends excluded"** in the JSON (`spy.label`), the CLI, the tab and Telegram (owner answer). Base = the last **completed** SPY close strictly before the experiment's start date: the previous XNYS session's close, also across a weekend or a holiday (`XNYSCalendarPolicy.previous_session(start_date)`), never the start date's own close (owner answer); each session is compared at its own close. A missing SPY bar is `null` for that date; it is never forward-filled. A refresh that sees a different close than the stored one leaves the stored row and records `BENCHMARK_SOURCE_DIFFERS`; `correct_benchmark(...)` creates a new version.
15. **Metrics.** Daily Sharpe = mean / sample stdev (ddof 1) of per-session USD returns, not annualised, plus an annualised value (× √252); both `null` below 2 sessions or at zero stdev; a `SMALL_SAMPLE` warning below 60 sessions (spec). The drawdown metric is named `eod_drawdown_pct` (never `max_drawdown`, never "kill") and labelled **"drawdown from end-of-day equity; intraday lows may be missed"** everywhere it is shown (owner answer); it uses end-of-day USD NLV including the start point. It is a scoreboard statistic, separate in name and display from the operational kill-line drawdown (Plan 4), which the tab shows only as a link to the experiment status. Profit factor and win rate use closed trips with known net P&L; no losing trip → profit factor `null`. Splits by strategy, decider and style cover trip metrics only (the account-level metrics cannot be split).
16. **AI cost and simulated books are unavailable until SP2 fills them (owner answer).** Plan 5 creates `ai_costs` and `simulated_books`; SP2 writes them. While the experiment has no `ai_costs` row, the report shows `ai_cost_usd: null`, `ai_calls: null`, `ai_costs_status: "UNAVAILABLE"` and "P&L minus AI cost" `null`; never `0`. The same for `simulated_books` (`simulated: {"label": "simulated", "status": "UNAVAILABLE"}`). Once rows exist, any call with unknown cost makes the total and "P&L minus AI cost" `null`.
17. **Telegram.** Gate is `ai_paper.telegram.enabled`. Enabled with a missing or malformed chat id, a missing/unsafe/empty token file, a non-bool `enabled` or an unknown key **fails config load** (startup stops). Disabled → no HTTP and **no outbox rows** (so enabling later does not flood old messages). The sender runs in `trader_service` (only it owns the tables). Delivery is at-least-once: a crash between send and mark-sent can repeat one message; every text ends with its event id. **Owner answer:** an invalid bot token file, chat id or token-file config with `enabled: true` fails startup; the bot identity and chat id come from the owner outside chat; no secret value reaches a log, error or test output. Delivery is **at-least-once**, never described as exactly-once in code, docs or text; every message ends with its stable event id, so a repeat is recognisable.
18. **Plain text only** for Telegram (no `parse_mode`), cut at 4000 characters with a visible notice, so no escaping bug can drop a message.
19. **Per-trip read (added with the Plan 6 review).** `get_scoreboard` is aggregate. Plan 6 must assert which conid and how many shares each closed trip had, without host DB access, so Task 7 also registers `("query","get_experiment_trips")` with `GetExperimentTripsRequest(experiment_id: str, extra=forbid)` returning `{"experiment_id", "generation": <int>, "trips": [{"round_trip_id", "conid", "symbol", "direction", "opened_at", "closed_at", "opened_quantity", "closed_quantity", "exec_ids": [...], "net_pnl_usd": float | None, "decision_id": str | None, "strategy_ref": str | None, "state": "CLOSED" | "OPEN"}]}` from the `round_trips` table after `refresh()`, ordered by `opened_at`. ACL: `{"cli", "dashboard", "ai_supervisor"}` (read-only, like `get_scoreboard`); `ai_research` and `strategy` get `PERMISSION_DENIED`. SDK `MMR.experiment_trips(experiment_id)`. Plan 6 consumes it for the A and B trip assertions.

## Review Focus

Inputs and failure modes the spec implies but its test list does not name. Each has a named test.

1. **A fill at 00:30 UTC on Friday** (= 20:30 ET Thursday). It belongs to Thursday's session, also across the DST change. → Task 3 `test_fill_after_utc_midnight_belongs_to_the_previous_et_session`, Task 2 `test_session_date_uses_et_across_dst`.
2. **A single fill that flips a long into a short.** Expect two pieces, one closed trip and one open trip, commission split by quantity. → Task 2 `test_fill_crossing_zero_closes_one_trip_and_opens_the_next`.
3. **An experiment with no finished session** (just armed, or mid-day). Expect all metrics `null`, counts `0`, no `ZeroDivisionError`, and a tab that renders. → Task 5 `test_report_with_no_sessions_is_all_unknown`, Task 8 `renders an empty report`.
4. **The bot token inside an exception** (`httpx` puts the URL, which contains the token, into error text). Expect redaction in logs, `last_error` and test output. → Task 9 `test_transport_error_text_never_contains_the_token`.
5. **A restart between "row written" and "summary enqueued", and between "sent" and "marked sent".** Expect exactly one outbox row per event id and at most one repeat. → Task 9 `test_enqueue_is_idempotent_per_event_id`, Task 10 `test_restart_after_row_written_still_enqueues_the_summary`.
6. **A missing SPY bar for one session.** Expect that session's benchmark `null`, no forward-fill, and `vs_spy` unknown when the last session is the one missing. → Task 4 `test_missing_spy_bar_is_unknown_not_forward_filled`, Task 5.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/scoreboard/__init__.py` (new) | Package marker | 1 |
| `trader/scoreboard/ports.py` (new) | `ExperimentRecord`, `ExperimentReader`, `AttributionLinks`, `AttributionLookup`, `FxEvidence`, `FxEvidencePort`, `SessionEnd` | 1 |
| `trader/scoreboard/schema.py` (new) | Migrations 60–64, `apply_scoreboard_migrations` | 1 |
| `trader/scoreboard/seal.py` (new) | Canonical row digest and hash chain | 1 |
| `trader/scoreboard/store.py` (new) | `ScoreboardStore`: sealed inserts, projection replace, incidents, `verify_seals` | 1 |
| `trader/scoreboard/round_trips.py` (new) | `FillFact`, `project_round_trips`, `load_fill_facts` | 2 |
| `trader/scoreboard/session_ledger.py` (new) | `SessionLedger`: session-end rows, peak gross, adjustments, startup recovery | 3 |
| `trader/automation/session_controller.py` | `on_terminal` listener, called on `FLAT` / `INCIDENT` (`_persist` :736) | 3 |
| `trader/scoreboard/fx.py` (new) | `IbFxEvidence` over `get_account_cash_by_currency` | 3 |
| `trader/scoreboard/benchmark.py` (new) | SPY reader, `BenchmarkBook`, versions | 4 |
| `trader/scoreboard/inputs.py` (new) | `record_ai_cost`, `record_simulated_row` (SP2 fills them) | 4 |
| `trader/scoreboard/metrics.py` (new) | Pure metrics | 5 |
| `trader/scoreboard/report.py` (new) | `build_report` (pure) | 5 |
| `trader/scoreboard/service.py` (new) | `ScoreboardService`: `refresh`, `report`, `verify` | 5, 6 |
| `trader/messaging/scoreboard_surface.py` (new), `trader/messaging/principals.py`, `trader/messaging/production_api.py:2171` | Typed queries and ACL | 7 |
| `trader/sdk.py`, `trader/mmr_cli.py` | `mmr --json scoreboard`, `mmr scoreboard verify` | 7 |
| `web/command_center/routes_scoreboard.py` (new), `web/app.py:1733`, `web/templates/_scoreboard_tab.html` (new), `web/templates/command_center.html:1801,2158`, `web/static/command_center_scoreboard.js` (new) + test, `web/static/dash_admin.js:148` | `/cc` tab | 8 |
| `trader/scoreboard/telegram_config.py`, `telegram_outbox.py`, `telegram_sender.py`, `summary_text.py` (new) | Gate, outbox, sender, text | 9 |
| `trader/trader_service.py:320-370`, `trader/trading/command_stack.py:1085`, `docker-compose.yml`, `AGENTS.md`, `docs/OPERATIONAL_STATE.md` | Wiring, secret mount, docs | 10 |
| `tests/scoreboard/` (new) | All tests of this plan | 1–10 |

---

### Task 1: Ports, tables, seals and the sealed insert

**Files:**
- Create: `trader/scoreboard/__init__.py`, `ports.py`, `schema.py`, `seal.py`, `store.py`; `tests/scoreboard/__init__.py`, `tests/scoreboard/conftest.py`, `tests/scoreboard/test_store.py`
- Test: `tests/scoreboard/test_store.py`

**Interfaces:**
- Produces:
  - `ports.py` dataclasses and protocols exactly as in the table above. `ExperimentRecord(experiment_id: str, account_id: str, started_at: datetime, start_net_liquidation: float | None, base_currency: str | None, start_usd_per_base: float | None, state: str, killed_at: datetime | None)`. `AttributionLinks(decision_id, decider, strategy_version, policy_revision, style, digest)` (all `str`). `FxEvidence(base_currency: str, usd_per_base: float | None, source: str, as_of: datetime)`. `SessionEnd(account_id: str, session_date: date, state: str, ended_at: datetime)`. `ET = ZoneInfo("America/New_York")`, `def session_date_et(moment: datetime) -> date` (raises `ValueError` on a naive datetime).
  - `schema.apply_scoreboard_migrations(migrator) -> bool`.
  - `seal.row_digest(row: Mapping[str, Any]) -> str`; `seal.chain_digest(prev: str, table: str, key: str, digest: str) -> str`.
  - `ScoreboardStore(db, *, now)` with `insert_sealed(table, row) -> None` (raises `ScoreboardConflict` on a duplicate key; `ValueError` on an unknown table or column), `fetch(table, where: Mapping) -> list[dict]`, `replace_round_trips(experiment_id, rows: Sequence[Mapping]) -> None`, `record_incident(kind, key, detail) -> bool` (idempotent on `(kind, key)`), `incidents() -> list[dict]`, `verify_seals() -> list[dict]` (mismatch dicts; empty = intact).
- `SEALED_TABLES: dict[str, tuple[str, ...]]` maps table to key columns: `equity_daily` `(experiment_id, session_date)`, `equity_adjustments` `(adjustment_id,)`, `benchmark_versions` `(version,)`, `benchmark_prices` `(version, bar_date)`, `ai_costs` `(call_id,)`, `simulated_books` `(book_id,)`.

**Tables** (migration 60 `scoreboard_core`, 61 `scoreboard_round_trips`, 62 `scoreboard_benchmark`, 63 `scoreboard_books_costs`, 64 `scoreboard_outbox`; all `CREATE TABLE IF NOT EXISTS`):

```sql
-- 60
equity_daily (
  experiment_id VARCHAR NOT NULL, session_date DATE NOT NULL, account_id VARCHAR NOT NULL,
  session_end_state VARCHAR NOT NULL CHECK (session_end_state IN ('FLAT','KILLED','FAILED_SAFE','UNKNOWN')),
  base_currency VARCHAR, start_nlv_base DOUBLE, end_nlv_base DOUBLE,
  fx_usd_per_base DOUBLE, fx_source VARCHAR, fx_as_of TIMESTAMPTZ,
  start_nlv_usd DOUBLE, end_nlv_usd DOUBLE,
  start_source VARCHAR NOT NULL CHECK (start_source IN ('prev_end','experiment_start','unknown')),
  missing_sessions_before INTEGER,
  realized_pnl_usd DOUBLE, commissions_usd DOUBLE, commission_json VARCHAR NOT NULL,
  peak_gross_exposure_usd DOUBLE, trade_count INTEGER NOT NULL, fill_count INTEGER NOT NULL,
  open_positions INTEGER, fills_digest VARCHAR NOT NULL,
  ended_at TIMESTAMPTZ NOT NULL, written_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (experiment_id, session_date))
equity_adjustments (adjustment_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
  session_date DATE NOT NULL, kind VARCHAR NOT NULL CHECK (kind IN ('COMMISSION')),
  exec_id VARCHAR NOT NULL, amount_usd DOUBLE NOT NULL, recorded_at TIMESTAMPTZ NOT NULL)
equity_session_peak (experiment_id VARCHAR NOT NULL, session_date DATE NOT NULL,
  peak_gross_usd DOUBLE, incomplete BOOLEAN NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (experiment_id, session_date))
scoreboard_seals (seal_id BIGINT PRIMARY KEY DEFAULT nextval('scoreboard_seal_seq'),
  table_name VARCHAR NOT NULL, row_key VARCHAR NOT NULL, row_digest VARCHAR NOT NULL,
  prev_chain VARCHAR NOT NULL, chain VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL)
scoreboard_incidents (kind VARCHAR NOT NULL, key VARCHAR NOT NULL, detail VARCHAR NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (kind, key))
-- 61
round_trips (round_trip_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
  account_id VARCHAR NOT NULL, conid BIGINT NOT NULL, symbol VARCHAR,
  direction VARCHAR NOT NULL, status VARCHAR NOT NULL CHECK (status IN ('OPEN','CLOSED')),
  opened_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ, opened_session DATE NOT NULL,
  closed_session DATE, entry_qty DOUBLE NOT NULL, exit_qty DOUBLE NOT NULL,
  entry_avg DOUBLE, exit_avg DOUBLE, gross_pnl_usd DOUBLE NOT NULL, fees_usd DOUBLE,
  net_pnl_usd DOUBLE, fees_complete BOOLEAN NOT NULL, notional_traded_usd DOUBLE NOT NULL,
  strategy_version VARCHAR, decider VARCHAR, policy_revision VARCHAR, style VARCHAR,
  decision_id VARCHAR, links_digest VARCHAR, exec_ids VARCHAR NOT NULL, fills_digest VARCHAR NOT NULL)
-- 62
benchmark_versions (version INTEGER PRIMARY KEY, reason VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL)
benchmark_prices (version INTEGER NOT NULL, bar_date DATE NOT NULL, symbol VARCHAR NOT NULL,
  conid BIGINT NOT NULL, close DOUBLE NOT NULL, provider VARCHAR NOT NULL,
  bar_size VARCHAR NOT NULL, fetched_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (version, bar_date))
-- 63
simulated_books (book_id VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
  session_date DATE NOT NULL, baseline VARCHAR NOT NULL,
  label VARCHAR NOT NULL CHECK (label = 'simulated'), pnl_usd DOUBLE, trades INTEGER,
  created_at TIMESTAMPTZ NOT NULL)
ai_costs (call_id VARCHAR PRIMARY KEY, experiment_id VARCHAR, provider VARCHAR NOT NULL,
  model VARCHAR NOT NULL, input_tokens BIGINT, output_tokens BIGINT, cost_usd DOUBLE,
  called_at TIMESTAMPTZ NOT NULL, served_kind VARCHAR NOT NULL, served_id VARCHAR NOT NULL)
-- 64
telegram_outbox (event_id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL, text VARCHAR NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, status VARCHAR NOT NULL CHECK (status IN ('PENDING','SENT')),
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL,
  last_error VARCHAR, sent_at TIMESTAMPTZ, telegram_message_id BIGINT)
```

(`CREATE SEQUENCE IF NOT EXISTS scoreboard_seal_seq START 1` is the first statement of migration 60. `equity_session_peak`, `round_trips` and `telegram_outbox` are mutable working tables and are not sealed: the first two are derived or incremental, the outbox is delivery state.)

- [ ] **Step 1: Write the failing tests** (`tests/scoreboard/conftest.py` provides `db`, `migrator`, `store`, `NOW`)

```python
# tests/scoreboard/conftest.py
import datetime as dt, pytest
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.store import ScoreboardStore

NOW = dt.datetime(2026, 10, 6, 21, 0, tzinfo=dt.timezone.utc)

@pytest.fixture
def db(tmp_path):
    return DuckDBConnection(str(tmp_path / "journal.duckdb"))

@pytest.fixture
def migrator(db):
    return SchemaMigrator(db)

@pytest.fixture
def store(db, migrator):
    apply_scoreboard_migrations(migrator)
    return ScoreboardStore(db, now=lambda: NOW)
```

```python
# tests/scoreboard/test_store.py
def test_migrations_are_idempotent_and_in_range(migrator):
    assert apply_scoreboard_migrations(migrator) is True
    assert apply_scoreboard_migrations(migrator) is False
    assert {v for v in migrator.applied_versions() if v >= 60} == {60, 61, 62, 63, 64}

def test_sealed_insert_roundtrips_nulls_as_null(store):
    store.insert_sealed("equity_daily", EQUITY_ROW | {"end_nlv_usd": None})
    assert store.fetch("equity_daily", {"experiment_id": "e1"})[0]["end_nlv_usd"] is None

def test_duplicate_key_raises_conflict_and_leaves_one_seal(store):
    store.insert_sealed("equity_daily", EQUITY_ROW)
    with pytest.raises(ScoreboardConflict):
        store.insert_sealed("equity_daily", EQUITY_ROW | {"end_nlv_usd": 1.0})
    assert len(_seals(store)) == 1

def test_unknown_table_or_column_is_refused(store):
    with pytest.raises(ValueError):
        store.insert_sealed("round_trips", {})
    with pytest.raises(ValueError):
        store.insert_sealed("equity_daily", EQUITY_ROW | {"nope": 1})

def test_digest_survives_a_database_roundtrip(store):
    store.insert_sealed("equity_daily", EQUITY_ROW)
    stored = store.fetch("equity_daily", {"experiment_id": "e1"})[0]
    assert row_digest(stored) == row_digest(EQUITY_ROW)       # tz-aware timestamps, DOUBLE, DATE

def test_intact_chain_verifies_clean(store):
    for day in (6, 7, 8):
        store.insert_sealed("equity_daily", EQUITY_ROW | {"session_date": dt.date(2026, 10, day)})
    assert store.verify_seals() == []

@pytest.mark.parametrize("sql,check", [
    ("UPDATE equity_daily SET end_nlv_usd = 1 WHERE session_date = DATE '2026-10-07'", "ROW_EDITED"),
    ("DELETE FROM equity_daily WHERE session_date = DATE '2026-10-07'", "ROW_MISSING"),
    ("DELETE FROM scoreboard_seals WHERE seal_id = 2", "CHAIN_BROKEN"),
    ("UPDATE scoreboard_seals SET row_digest = 'x' WHERE seal_id = 2", "CHAIN_BROKEN"),
])
def test_tampering_is_detected(store, db, sql, check):
    for day in (6, 7, 8):
        store.insert_sealed("equity_daily", EQUITY_ROW | {"session_date": dt.date(2026, 10, day)})
    db.execute(sql)
    assert check in {m["check"] for m in store.verify_seals()}

def test_row_without_a_seal_is_detected(store, db):
    db.execute("INSERT INTO ai_costs VALUES ('c1', NULL, 'p', 'm', 1, 1, 0.1, now(), 'job', 'j1')")
    assert "ROW_UNSEALED" in {m["check"] for m in store.verify_seals()}

def test_incident_is_idempotent_per_kind_and_key(store):
    assert store.record_incident("FX_EVIDENCE_MISSING", "e1:2026-10-06", "no rate") is True
    assert store.record_incident("FX_EVIDENCE_MISSING", "e1:2026-10-06", "again") is False
    assert len(store.incidents()) == 1

def test_session_date_et_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        session_date_et(dt.datetime(2026, 10, 6, 12, 0))
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: trader.scoreboard`).
- [ ] **Step 3: Implement.**
  - `seal.row_digest`: canonical JSON, keys sorted, `None` → `null`, `float` → `repr(float)`, `datetime` → `astimezone(UTC).isoformat()`, `date` → `isoformat()`, `int`/`str` as is, anything else `TypeError`. Return `sha256(...).hexdigest()`. `chain_digest(prev, table, key, digest) = sha256(f"{prev}|{table}|{key}|{digest}")`. Genesis `prev` is `"0" * 64`.
  - `store.insert_sealed(table, row)`: validate table in `SEALED_TABLES` and columns against `information_schema.columns` (read once per table); one `db.transaction`: check the key does not exist (else `ScoreboardConflict`), insert, read the last seal's `chain` (genesis if none), insert the seal with `row_key = "|".join(str(row[k]) for k in key_cols)`.
  - `verify_seals()`: walk seals by `seal_id`, recompute each chain link (`CHAIN_BROKEN`, naming the first bad `seal_id`; a gap in `seal_id` numbering is also `CHAIN_BROKEN`); for each seal fetch the live row by key: absent → `ROW_MISSING`; digest differs → `ROW_EDITED`; then every live row of every sealed table with no seal → `ROW_UNSEALED`. Mismatch dict: `{"check", "table", "key", ...}`.
  - `session_date_et` in `ports.py`.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add scoreboard tables and sealed insert`.

---

### Task 2: Round-trip projection from fills

**Files:**
- Create: `trader/scoreboard/round_trips.py`, `tests/scoreboard/test_round_trips.py`
- Modify: `trader/scoreboard/store.py` (`replace_round_trips` already declared; implement here)

**Interfaces:**
- Consumes: Task 1 `AttributionLookup`, `AttributionLinks`, `session_date_et`, `ScoreboardStore`.
- Produces:
  - `FillFact(exec_id: str, conid: int, side: str, quantity: Decimal, price: Decimal, commission: Decimal | None, fill_time: datetime, order_ref: str | None = None, symbol: str | None = None)`; `commission` is `None` when unknown or not USD.
  - `Piece(exec_id, conid, session_date: date, realized: Decimal, fee: Decimal | None, closes_trip: bool)`.
  - `RoundTrip` (frozen dataclass mirroring the `round_trips` columns; money as `float`, `exec_ids: tuple[str, ...]`) with `.as_row(experiment_id, account_id) -> dict`.
  - `Projection(trips: tuple[RoundTrip, ...], pieces: tuple[Piece, ...])`.
  - `project_round_trips(fills: Sequence[FillFact], *, links_for: Callable[[str], AttributionLinks | None]) -> Projection`. Raises `ProjectionError` for a non-positive quantity or price, a side other than `BUY`/`SELL`, or a naive `fill_time`.
  - `load_fill_facts(conn, account_id: str, since: datetime) -> list[FillFact]`: joins `broker_fills` to `broker_order_aliases` (`alias_type = 'order_ref'`, `MIN(alias_value)` per `order_entity_id`) and `broker_orders.symbol`; `commission` is `Decimal(str(value))` only when `commission_currency == 'USD'` and the value is not null (`trader/data/broker_state.py:87-104`).
  - `round_trip_id = sha256(f"{account_id}|{conid}|{first_exec_id}")[:32]`.

- [ ] **Step 1: Write the failing tests** (`fill(...)` helper builds `FillFact` with `Decimal` and UTC times)

```python
def test_long_round_trip_net_of_fees():
    p = project_round_trips([fill("e1","BUY",10,100,"1.00",T0), fill("e2","SELL",10,101,"1.00",T1)],
                            links_for=lambda ref: None)
    t, = p.trips
    assert (t.direction, t.status, t.gross_pnl_usd, t.fees_usd, t.net_pnl_usd) == ("LONG","CLOSED",10.0,2.0,8.0)
    assert t.fees_complete is True

def test_partial_exits_stay_in_one_trip():
    p = project_round_trips([fill("e1","BUY",10,100,"0",T0), fill("e2","SELL",4,102,"0",T1),
                             fill("e3","SELL",6,101,"0",T2)], links_for=lambda r: None)
    assert len(p.trips) == 1 and p.trips[0].gross_pnl_usd == 4*2 + 6*1
    assert p.trips[0].exec_ids == ("e1","e2","e3")

def test_short_round_trip_profits_when_price_falls():
    p = project_round_trips([fill("e1","SELL",10,100,"0",T0), fill("e2","BUY",10,98,"0",T1)], links_for=lambda r: None)
    assert (p.trips[0].direction, p.trips[0].net_pnl_usd) == ("SHORT", 20.0)

def test_fill_crossing_zero_closes_one_trip_and_opens_the_next():
    p = project_round_trips([fill("e1","BUY",10,100,"0",T0), fill("e2","SELL",15,101,"3.00",T1)], links_for=lambda r: None)
    closed, opened = sorted(p.trips, key=lambda t: t.opened_at)
    assert (closed.status, closed.fees_usd) == ("CLOSED", 2.0)          # 3.00 * 10/15
    assert (opened.status, opened.direction, opened.entry_qty, opened.fees_usd) == ("OPEN","SHORT",5.0,1.0)

def test_open_trip_has_unknown_net_not_zero():
    t, = project_round_trips([fill("e1","BUY",10,100,"1.00",T0)], links_for=lambda r: None).trips
    assert t.status == "OPEN" and t.closed_at is None and t.net_pnl_usd is None

def test_unknown_commission_makes_net_unknown_and_flags_it():
    t, = project_round_trips([fill("e1","BUY",10,100,None,T0), fill("e2","SELL",10,101,"1.00",T1)], links_for=lambda r: None).trips
    assert (t.net_pnl_usd, t.fees_usd, t.fees_complete, t.gross_pnl_usd) == (None, None, False, 10.0)

def test_conids_are_independent_and_tie_broken_by_exec_id():
    p = project_round_trips([fill("b","SELL",5,10,"0",T1,conid=2), fill("a","BUY",5,9,"0",T1,conid=2),
                             fill("x","BUY",1,5,"0",T0,conid=1)], links_for=lambda r: None)
    assert {t.conid for t in p.trips} == {1, 2}
    assert [t for t in p.trips if t.conid == 2][0].direction == "LONG"      # exec "a" sorts before "b"

def test_attribution_comes_from_the_first_opening_fill_only():
    links = AttributionLinks("d1","jev","sv-1","rev-3","intraday_long","dig")
    seen = []
    p = project_round_trips([fill("e1","BUY",10,100,"0",T0,ref="r-entry"), fill("e2","SELL",10,101,"0",T1,ref="r-flatten")],
                            links_for=lambda ref: seen.append(ref) or links)
    assert seen == ["r-entry"] and p.trips[0].decider == "jev" and p.trips[0].links_digest == "dig"

def test_unattributed_trip_keeps_null_links():
    t, = project_round_trips([fill("e1","BUY",1,1,"0",T0)], links_for=lambda r: None).trips
    assert (t.decider, t.strategy_version, t.decision_id, t.links_digest) == (None,)*4

def test_session_date_uses_et_across_dst():
    # DST ends Sun 2026-11-01: 2026-11-02 00:30 UTC is 19:30 EST on Nov 1 (UTC-5), still the Nov 1 session
    p = project_round_trips([fill("e1","BUY",1,1,"0",dt.datetime(2026,11,2,0,30,tzinfo=UTC)),
                             fill("e2","SELL",1,1,"0",dt.datetime(2026,11,2,15,0,tzinfo=UTC))], links_for=lambda r: None)
    assert p.pieces[0].session_date == dt.date(2026,11,1) and p.pieces[1].session_date == dt.date(2026,11,2)

@pytest.mark.parametrize("bad", [dict(quantity=0), dict(price=-1), dict(side="HOLD"), dict(naive=True)])
def test_bad_fills_fail_loudly(bad):
    with pytest.raises(ProjectionError):
        project_round_trips([fill("e1","BUY",1,1,"0",T0, **bad)], links_for=lambda r: None)

def test_empty_input_is_empty_projection():
    assert project_round_trips([], links_for=lambda r: None) == Projection((), ())

def test_load_fill_facts_reads_ref_symbol_and_usd_only_commission(db, migrator):   # uses BrokerStateStore.upsert_fill_in_tx
    ...  # one USD fill with an order_ref alias and an order symbol -> ref/symbol set; one EUR commission -> commission None;
         # a fill before `since` is excluded
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Core of the walker (the rest is bookkeeping around it):

```python
def project_round_trips(fills, *, links_for):
    by_conid: dict[int, list[FillFact]] = defaultdict(list)
    for fill in fills:
        _validate(fill)
        by_conid[fill.conid].append(fill)
    trips, pieces = [], []
    for conid in sorted(by_conid):
        position = average = ZERO
        trip: _TripBuilder | None = None
        for fill in sorted(by_conid[conid], key=lambda f: (f.fill_time, f.exec_id)):
            left = fill.quantity if fill.side == "BUY" else -fill.quantity
            while left != 0:
                opening = position == 0 or (position > 0) == (left > 0)
                size = abs(left) if opening else min(abs(left), abs(position))
                fee = None if fill.commission is None else fill.commission * size / fill.quantity
                if opening:
                    trip = trip or _TripBuilder(fill, "LONG" if left > 0 else "SHORT")
                    average = (abs(position) * average + size * fill.price) / (abs(position) + size)
                    position += size if left > 0 else -size
                    realized = ZERO
                    trip.add_entry(fill, size, fee)
                else:
                    direction = 1 if position > 0 else -1
                    realized = (fill.price - average) * size * direction
                    position -= direction * size
                    trip.add_exit(fill, size, realized, fee)
                left += -size if left > 0 else size
                closes = not opening and position == 0
                pieces.append(Piece(fill.exec_id, conid, session_date_et(fill.fill_time), realized, fee, closes))
                if closes:
                    trips.append(trip.build("CLOSED", links_for)); trip, average = None, ZERO
        if trip is not None:
            trips.append(trip.build("OPEN", links_for))
    return Projection(tuple(trips), tuple(pieces))
```

  `_TripBuilder.build` sets `fees_complete` false when any piece fee is `None`, `fees_usd`/`net_pnl_usd` `None` then (and `net_pnl_usd` `None` for `OPEN`), `notional_traded_usd = Σ size × price`, `links = links_for(first.order_ref) if first.order_ref else None`, `fills_digest = sha256` of the sorted `(exec_id, side, quantity, price, fill_time)` tuples (commission excluded, ruling 10). `ScoreboardStore.replace_round_trips` deletes by `experiment_id` and inserts in one transaction.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: project round trips from broker fills`.

---

### Task 3: Session ledger, `equity_daily`, adjustments and recovery

**Files:**
- Create: `trader/scoreboard/session_ledger.py`, `trader/scoreboard/fx.py`, `tests/scoreboard/test_session_ledger.py`, `tests/scoreboard/test_fx.py`
- Modify: `trader/automation/session_controller.py` (constructor :310, `_persist` :736), `tests/automation/test_session_controller.py`

**Interfaces:**
- Consumes: Task 1 ports and store; Task 2 `load_fill_facts`, `project_round_trips`; `BrokerSnapshotPort.capture(account_id) -> BrokerRiskSnapshot` (`trader/data/broker_state.py:260`; raises `BrokerRiskSnapshotError` when no promoted generation); `XNYSCalendarPolicy` (`calendar_policy.py:50`).
- Produces:
  - `SessionController(..., on_terminal: Callable[[SessionControllerState], None] | None = None)`. `_persist` calls it after the durable save when `state.state in _TERMINAL` (`FLAT`, `INCIDENT`), each call wrapped so an exception is logged with `logging.exception` and never stops the controller.
  - `SessionLedger(store, db, experiments, broker, fx, calendar, links, now, on_row_written=None)`:
    - `on_controller_terminal(state: SessionControllerState) -> None` (maps `FLAT`/`INCIDENT`, ruling 4, then `record_session_end`).
    - `record_session_end(end: SessionEnd) -> dict | None` — the written (or existing) `equity_daily` row; `None` when no experiment covers the date. Idempotent.
    - `observe_snapshot(snapshot) -> None` — peak gross (ruling 9).
    - `reconcile_commissions() -> int` — writes `equity_adjustments`, returns how many (ruling 10).
    - `recover(now) -> list[str]` — rows written for past terminal sessions found without one (ruling 6); returns the session dates.
  - `fx.IbFxEvidence(get_cash: Callable[[], dict], now)` implementing `FxEvidencePort`: base `USD` → `FxEvidence("USD", 1.0, "base_is_usd", now)`; else `usd_per_base = 1 / currencies["USD"]["exchange_rate"]` (IB reports base units per 1 unit of the currency, `trader_service_api.py:321-345`), source `ib_account_values`; no rate → `usd_per_base=None`. A missing `base_currency` raises `FxEvidenceError`.

- [ ] **Step 1: Write the failing tests** (fakes: `FakeExperiments`, `FakeBroker` returning a `BrokerRiskSnapshot`, `FakeFx`; fills inserted with `BrokerStateStore.upsert_fill_in_tx`)

```python
def test_flat_session_writes_one_row_with_usd_values(ledger, seed_round_trip):
    seed_round_trip(date=D, buy=("e1", 10, 100, "1.00"), sell=("e2", 10, 101, "1.00"))
    row = ledger.record_session_end(SessionEnd(ACCOUNT, D, "FLAT", END))
    assert (row["session_end_state"], row["realized_pnl_usd"], row["commissions_usd"],
            row["trade_count"], row["fill_count"], row["open_positions"]) == ("FLAT", 10.0, 2.0, 1, 2, 0)
    assert (row["start_source"], row["start_nlv_usd"]) == ("experiment_start", 100_000.0)

def test_second_call_is_a_no_op_and_never_edits(ledger):
    first = ledger.record_session_end(END_FLAT)
    ledger.broker.net_liquidation = 1.0
    assert ledger.record_session_end(END_FLAT) == first

def test_next_session_starts_from_previous_end_value(ledger):
    ledger.record_session_end(END_FLAT); ledger.broker.net_liquidation = 100_500.0
    row = ledger.record_session_end(SessionEnd(ACCOUNT, D_NEXT, "FLAT", END2))
    assert (row["start_source"], row["start_nlv_usd"], row["missing_sessions_before"]) == ("prev_end", 100_250.0, 0)

def test_skipped_sessions_are_counted(ledger):          # D = Fri, next row on the Wed after (Mon, Tue skipped)
    ...
    assert row["missing_sessions_before"] == 2

def test_no_experiment_for_the_date_writes_nothing(ledger):
    ledger.experiments.record = None
    assert ledger.record_session_end(END_FLAT) is None and ledger.store.fetch("equity_daily", {}) == []

def test_kill_on_that_date_overrides_flat(ledger):
    ledger.experiments.record = replace(EXP, state="KILLED", killed_at=KILL_AT_SAME_ET_DATE)
    assert ledger.record_session_end(END_FLAT)["session_end_state"] == "KILLED"

def test_incident_state_maps_to_failed_safe_with_open_positions_counted(ledger):
    ledger.broker.positions = [position(5)]
    state = controller_state("INCIDENT")
    ledger.on_controller_terminal(state)
    row = ledger.store.fetch("equity_daily", {})[0]
    assert (row["session_end_state"], row["open_positions"]) == ("FAILED_SAFE", 1)

def test_fill_after_utc_midnight_belongs_to_the_previous_et_session(ledger, seed_fill):
    seed_fill("e1", "BUY", 1, 100, "0", dt.datetime(2026, 10, 9, 0, 30, tzinfo=UTC))   # Thu 20:30 ET
    assert ledger.record_session_end(SessionEnd(ACCOUNT, dt.date(2026,10,8), "FLAT", END))["fill_count"] == 1

def test_non_usd_base_with_fx_writes_both_values_and_the_rate(ledger):
    ledger.fx.evidence = FxEvidence("EUR", 1.10, "ib_account_values", END); ledger.broker.net_liquidation = 90_000.0
    row = ledger.record_session_end(END_FLAT)
    assert (row["end_nlv_base"], row["end_nlv_usd"], row["fx_usd_per_base"], row["fx_source"]) == (90_000.0, 99_000.0, 1.10, "ib_account_values")

def test_non_usd_base_without_fx_never_writes_usd_and_records_an_incident(ledger):
    ledger.fx.evidence = FxEvidence("EUR", None, "ib_account_values", END)
    row = ledger.record_session_end(END_FLAT)
    assert row["end_nlv_usd"] is None and row["end_nlv_base"] is not None
    assert [i["kind"] for i in ledger.store.incidents()] == ["FX_EVIDENCE_MISSING"]

def test_snapshot_unavailable_gives_unknown_end_value_and_an_incident(ledger):
    ledger.broker.raises = BrokerRiskSnapshotError("NO_PROMOTED_GENERATION", "none")
    row = ledger.record_session_end(END_FLAT)
    assert row["end_nlv_base"] is None and row["open_positions"] is None
    assert "SNAPSHOT_UNAVAILABLE" in {i["kind"] for i in ledger.store.incidents()}

def test_peak_gross_is_the_maximum_observed_and_survives_a_restart(ledger):
    ledger.observe_snapshot(snap(gross=1_000)); ledger.observe_snapshot(snap(gross=3_000)); ledger.observe_snapshot(snap(gross=2_000))
    restarted = rebuild_ledger(ledger)                       # new SessionLedger on the same db
    restarted.observe_snapshot(snap(gross=2_500))
    assert restarted.record_session_end(END_FLAT)["peak_gross_exposure_usd"] == 3_000.0

def test_peak_gross_is_unknown_if_any_observation_lacked_a_market_value(ledger):
    ledger.observe_snapshot(snap(gross=1_000)); ledger.observe_snapshot(snap(market_value=None))
    assert ledger.record_session_end(END_FLAT)["peak_gross_exposure_usd"] is None

def test_session_without_any_snapshot_observation_has_unknown_peak_not_zero(ledger):
    assert ledger.record_session_end(END_FLAT)["peak_gross_exposure_usd"] is None

def test_late_commission_becomes_an_adjustment_and_the_row_is_untouched(ledger, seed_round_trip, set_commission):
    seed_round_trip(date=D, buy=("e1", 10, 100, None), sell=("e2", 10, 101, "1.00"))
    row = ledger.record_session_end(END_FLAT); assert row["commissions_usd"] is None
    set_commission("e1", "1.00")
    assert ledger.reconcile_commissions() == 1 and ledger.reconcile_commissions() == 0
    assert ledger.store.fetch("equity_daily", {})[0] == row
    assert ledger.store.fetch("equity_adjustments", {})[0]["amount_usd"] == 1.0

def test_revised_commission_adds_only_the_delta(ledger, ...):  # 1.00 -> 1.30 gives +0.30

def test_fill_arriving_after_the_row_is_an_incident_not_an_adjustment(ledger, seed_fill):
    ledger.record_session_end(END_FLAT); seed_fill("late", "SELL", 1, 100, "0", IN_SESSION)
    ledger.reconcile_commissions()
    assert "LATE_FILL_AFTER_SESSION_ROW" in {i["kind"] for i in ledger.store.incidents()}
    assert ledger.store.fetch("equity_adjustments", {}) == []

def test_recover_after_restart_between_flat_and_row_writes_it_once(ledger_with_flat_controller_row):
    assert ledger.recover(NOW) == [D.isoformat()] and ledger.recover(NOW) == []

def test_a_session_without_any_terminal_record_is_unknown_never_flat(ledger_with_a_silent_past_session):   # ruling 6
    ledger.recover(NOW)
    row = ledger.store.fetch("equity_daily", {})[0]
    assert row["session_end_state"] == "UNKNOWN"
    assert (row["end_nlv_base"], row["realized_pnl_usd"], row["commissions_usd"]) == (None, None, None)
    assert "SESSION_MISSING" in {i["kind"] for i in ledger.store.incidents()}

def test_recover_of_a_past_session_writes_an_unknown_nlv_row_and_an_incident(ledger_with_old_terminal_row):
    ledger.recover(NOW)
    row = ledger.store.fetch("equity_daily", {})[0]
    assert row["end_nlv_base"] is None and row["fill_count"] >= 0
    assert "SESSION_ROW_RECOVERED_WITHOUT_NLV" in {i["kind"] for i in ledger.store.incidents()}

def test_recover_ignores_sessions_before_the_experiment(ledger_with_pre_experiment_row):
    assert ledger.recover(NOW) == []

def test_on_row_written_failure_does_not_lose_the_row(ledger):      # callback raises -> row exists, error logged
    ...
```
  `tests/scoreboard/test_fx.py`: base USD → rate 1.0 `base_is_usd`; base `CAD` with `USD` exchange rate 1.36 → `usd_per_base ≈ 0.7353`; base EUR but no USD line → `usd_per_base is None`; no `base_currency` → `FxEvidenceError`.
  `tests/automation/test_session_controller.py` (use `_build_controller`, :183):

```python
def test_flat_calls_the_terminal_listener_once_with_the_state(tmp_path):
    seen = []; c = _build_controller(tmp_path, on_terminal=seen.append)
    ...drive to FLAT as in test_broker_confirmed_flat_records_generation...
    assert [s.state for s in seen] == ["FLAT"]
def test_missed_flat_calls_the_listener_with_incident(tmp_path): ...
def test_a_raising_listener_never_stops_the_controller(tmp_path): ...   # state still FLAT, error logged
def test_listener_is_not_called_for_non_terminal_states(tmp_path): ...
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - `record_session_end`: resolve the experiment with `experiments.latest()`; return `None` unless `experiment.account_id == end.account_id` and `end.session_date >= session_date_et(experiment.started_at)`. If a row exists return it. Capture the snapshot and FX in separate `try` blocks (each failure records its incident, ruling 6). Start value per ruling 2; `missing_sessions_before` = number of XNYS sessions strictly between the previous row's date (or the start date) and this date, via the calendar object (`XNYSCalendarPolicy._calendar.sessions_in_range`; add small public `sessions_between(start, end)` and `previous_session(day)` (the last XNYS session strictly before `day`; ruling 14) to `XNYSCalendarPolicy`, test both on a holiday week). Project all fills since `started_at` (Task 2), take the pieces whose `session_date` equals the date: `realized_pnl_usd = Σ realized`, `commission_json = {exec_id: fee_or_null}` (per **fill**, summed over its pieces), `commissions_usd = Σ` or `None` if any is `None`, `trade_count` = trips whose `closed_session` is the date, `fill_count` = distinct exec ids that day, `fills_digest` over the immutable fill fields. Insert with `store.insert_sealed`, then call `on_row_written(experiment_id, session_date)` inside its own `try`.
  - `observe_snapshot`: only for an `ARMED`/`PAUSED`/`KILLED` experiment and when today's ET date is an XNYS session; upsert `equity_session_peak` (`incomplete` is sticky).
  - `reconcile_commissions`: for each written row of the experiment, recompute per-exec current commissions from `load_fill_facts`, compare with `commission_json` plus prior adjustments, insert one adjustment per difference with `adjustment_id = sha256(f"{experiment_id}|{date}|{exec_id}|{n}")` where `n` counts the exec's earlier adjustments; unknown exec ids that are in-session and not in `commission_json` → incident.
  - `recover(now)`: for every XNYS session from the experiment start date to today without an `equity_daily` row, read `automation_session_state` (`session_controller.py:51`). Today with `state IN ('FLAT','INCIDENT')`: call `record_session_end` (the broker snapshot is still the closing one). An earlier date with a terminal row: the mapped end state, `null` NLV, P&L and commissions (`SESSION_ROW_RECOVERED_WITHOUT_NLV`). An earlier date with no terminal row: `UNKNOWN`, every money column `null` (`SESSION_MISSING`). Ruling 6.
  - `IbFxEvidence` as specified; the production `get_cash` is `TraderServiceApi.get_account_cash_by_currency`.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: write equity_daily at session end and reconcile late commissions`.

---

### Task 4: Benchmark prices, AI costs and simulated books

**Files:**
- Create: `trader/scoreboard/benchmark.py`, `trader/scoreboard/inputs.py`, `tests/scoreboard/test_benchmark.py`, `tests/scoreboard/test_inputs.py`

**Interfaces:**
- Consumes: Task 1 `ScoreboardStore`; history reads as in `trader/research/evaluation_data.py:62-100` (`TickStorage(history_db).get_tickdata(BarSize.parse_str('1 day')).read(BENCHMARK_CONID, DateRange(...))`, `normalize_historical`).
- Produces:
  - `read_spy_closes(history_db_path: str, start: date, end: date) -> dict[date, float]` — daily closes by UTC bar date; raises `BenchmarkSourceError` (message names `mmr data download SPY --bar-size "1 day"`) when the history has no SPY bars at all; a duplicate bar for one date raises too. Missing single dates are simply absent.
  - `BenchmarkBook(store, source: Callable[[date, date], Mapping[date, float]], calendar, now)`:
    - `refresh(start: date, end: date) -> int` — inserts missing `(current_version, bar_date)` rows for XNYS sessions in range (provider `"history_duckdb"`, `bar_size "1 day"`); a stored row whose source close now differs is left alone and records incident `BENCHMARK_SOURCE_DIFFERS`; version 1 is created on first use.
    - `current_version() -> int | None`; `closes(version=None) -> dict[date, float]`.
    - `correct_benchmark(bar_date, close, reason) -> int` — new version = copy of the current rows with that date replaced; returns the new version.
  - `inputs.record_ai_cost(store, *, call_id, provider, model, input_tokens, output_tokens, cost_usd, called_at, served_kind, served_id, experiment_id=None)`; `inputs.record_simulated_row(store, *, book_id, experiment_id, session_date, baseline, pnl_usd, trades)` (always `label='simulated'`). Both sealed. SP2 calls them; SP1 only creates and displays (spec 5.2). Neither is exposed over RPC here (open question 5).

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_stores_one_row_per_session_and_skips_weekends(book):            # source has Fri, Sat(junk), Mon
    assert book.refresh(dt.date(2026,10,9), dt.date(2026,10,12)) == 2
    assert set(book.closes()) == {dt.date(2026,10,9), dt.date(2026,10,12)}

def test_refresh_is_idempotent(book): ...
def test_missing_spy_bar_is_unknown_not_forward_filled(book):                    # no bar for Tue 10-13
    book.refresh(dt.date(2026,10,12), dt.date(2026,10,14))
    assert dt.date(2026,10,13) not in book.closes()
def test_data_refresh_never_changes_a_stored_price(book, source):
    book.refresh(D, D); source.closes[D] = 999.0; book.refresh(D, D)
    assert book.closes()[D] == 500.0 and "BENCHMARK_SOURCE_DIFFERS" in {i["kind"] for i in book.store.incidents()}
def test_correction_is_a_new_version_and_old_rows_stay(book):
    book.refresh(D, D2); v = book.correct_benchmark(D, 501.0, "bad tick")
    assert v == 2 and book.closes(version=1)[D] == 500.0 and book.closes()[D] == 501.0 and book.closes()[D2] == 510.0
def test_correction_of_a_date_that_was_never_stored_is_refused(book): ...
def test_read_spy_closes_without_any_bars_names_the_download_command(tmp_path): ...    # BenchmarkSourceError, "mmr data download SPY"
def test_read_spy_closes_refuses_two_bars_for_one_date(tmp_path): ...
def test_ai_cost_row_is_sealed_and_unknown_cost_stays_null(store):
    record_ai_cost(store, call_id="c1", ..., cost_usd=None, ...)
    assert store.fetch("ai_costs", {})[0]["cost_usd"] is None and store.verify_seals() == []
def test_duplicate_call_id_is_a_conflict(store): ...
def test_simulated_row_is_always_labelled_simulated(store):
    record_simulated_row(store, book_id="b1", ...); assert store.fetch("simulated_books", {})[0]["label"] == "simulated"
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** as specified. `refresh` takes the XNYS session dates in `[start, end]` from the calendar; it never calls the source for dates already stored with a matching close.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add immutable spy benchmark, ai cost and simulated book inputs`.

---

### Task 5: Metrics and the report

**Files:**
- Create: `trader/scoreboard/metrics.py`, `trader/scoreboard/report.py`, `trader/scoreboard/service.py`, `tests/scoreboard/test_metrics.py`, `tests/scoreboard/test_report.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `metrics.session_returns(rows) -> list[float | None]` (`end_nlv_usd / start_nlv_usd - 1`, `None` when either is `None` or start ≤ 0).
  - `metrics.daily_sharpe(returns) -> tuple[float | None, float | None, str | None]` → `(daily, annualised, warning)`; warning `"SMALL_SAMPLE"` below 60 returns.
  - `metrics.eod_drawdown_pct(values: Sequence[float]) -> float | None` (`None` below 2 points); `EOD_DRAWDOWN_LABEL = "drawdown from end-of-day equity; intraday lows may be missed"`; `SPY_LABEL = "SPY price only; dividends excluded"`.
  - `metrics.trip_metrics(trips, *, start_nlv_usd) -> dict` with keys `closed, open, unresolved_fee_trips, net_pnl_usd, net_pnl_complete, fees_usd, fees_complete, win_rate, profit_factor, turnover`.
  - `metrics.group_trip_metrics(trips, key: str, *, start_nlv_usd) -> dict[str, dict]` (key one of `strategy_version`, `decider`, `style`; `None` → `"unattributed"`).
  - `report.build_report(inputs: ReportInputs) -> dict` (pure). `ReportInputs(experiment, rows, adjustments, trips, spy_closes, spy_version, spy_provider, ai_costs, simulated, incidents, warnings, outbox)`.
  - `ScoreboardService(store, db, experiments, ledger, book, links, outbox=None, now=...)` with `refresh() -> None` (rebuild `round_trips` for the latest experiment, `ledger.reconcile_commissions()`), `report(experiment_id: str | None = None) -> dict`.

Report shape (JSON-safe; `None` = unknown; every dict has `"label": "PAPER"` at the top and `"disclaimer": "Paper trading. Nothing here is proof of live edge."`):

```
{"label","disclaimer","experiment": {id, state, started_at, base_currency} | None,
 "account": {"sessions": int, "start_nlv_usd", "end_nlv_usd", "return_pct", "eod_drawdown_pct", "eod_drawdown_label",
             "sharpe_daily", "sharpe_annualised", "sharpe_warning", "unknown_nlv_sessions": int},
 "benchmarks": {"spy": {"return_pct", "base_date", "last_date", "version", "label": "SPY price only; dividends excluded"},
                "vs_spy_pp", "simulated": {"label":"simulated","status": "AVAILABLE" | "UNAVAILABLE","rows": int | None,"pnl_usd"},
                "ai_cost_usd", "ai_calls": int | None, "ai_costs_status": "AVAILABLE" | "UNAVAILABLE", "pnl_minus_ai_cost_usd"},
 "trips": {...trip_metrics}, "splits": {"strategy_version": {...}, "decider": {...}, "style": {...}},
 "sessions": [{date, end_state, start_nlv_usd, end_nlv_usd, return_pct, realized_pnl_usd, commissions_usd,
               peak_gross_exposure_usd, trade_count, open_positions, start_source, missing_sessions_before}],
 "warnings": [...], "incidents": [...], "outbox": {"enabled", "pending", "last_sent_at"} | None}
```
Percentages are percent numbers (`0.25` = 0.25 %). `return_pct` of the account = last known `end_nlv_usd` over the experiment start value. `vs_spy_pp` = account return minus SPY return, `None` if either is `None`. SPY return uses ruling 14.

- [ ] **Step 1: Write the failing tests**

```python
def test_daily_sharpe_known_values():
    d, a, w = daily_sharpe([0.01, -0.01, 0.02, 0.0])
    assert d == pytest.approx(0.3873, abs=1e-4) and a == pytest.approx(d * 252 ** 0.5) and w == "SMALL_SAMPLE"
@pytest.mark.parametrize("returns", [[], [0.01], [0.01, 0.01]])
def test_sharpe_is_unknown_without_variance_or_sample(returns):
    assert daily_sharpe(returns)[:2] == (None, None)
def test_sharpe_warning_clears_at_sixty_sessions(): ...
def test_returns_skip_unknown_values_instead_of_zeroing_them():
    assert session_returns([row(100, 101), row(None, 105), row(100, None)]) == [0.01, None, None]
def test_eod_drawdown_includes_the_start_point(): assert eod_drawdown_pct([100, 110, 99, 105]) == pytest.approx(10.0)
def test_eod_drawdown_needs_two_points(): assert eod_drawdown_pct([100]) is None
def test_trip_metrics_win_rate_profit_factor_and_unresolved_fees(): ...   # 2 wins (+10,+5), 1 loss (-5), 1 fees-unknown
    # win_rate 2/3, profit_factor 3.0, unresolved_fee_trips 1, net_pnl_complete False
def test_profit_factor_is_unknown_without_losses_and_with_no_trips(): ...
def test_turnover_is_unknown_without_a_start_value_but_zero_for_no_trades(): ...
def test_groups_put_missing_attribution_under_unattributed(): ...

def test_report_with_no_sessions_is_all_unknown():
    r = build_report(empty_inputs(experiment=EXP))
    assert r["account"]["sessions"] == 0 and r["account"]["return_pct"] is None
    assert r["account"]["sharpe_daily"] is None and r["account"]["eod_drawdown_pct"] is None
    assert r["trips"]["closed"] == 0 and r["trips"]["win_rate"] is None and r["trips"]["profit_factor"] is None
    assert (r["benchmarks"]["ai_cost_usd"], r["benchmarks"]["ai_calls"], r["benchmarks"]["ai_costs_status"]) == (None, None, "UNAVAILABLE")
    assert r["benchmarks"]["simulated"]["status"] == "UNAVAILABLE" and r["label"] == "PAPER"
def test_report_without_an_experiment_is_explicit(): assert build_report(empty_inputs(experiment=None))["experiment"] is None
def test_report_json_has_no_nan_or_infinity(): json.dumps(build_report(full_inputs()), allow_nan=False)
def test_two_sessions_give_return_drawdown_and_vs_spy(): ...
def test_missing_spy_bar_for_the_last_session_makes_spy_return_and_vs_spy_unknown(): ...
@pytest.mark.parametrize("start,base", [(dt.date(2026, 10, 7), dt.date(2026, 10, 6)),      # Wednesday -> Tuesday
    (dt.date(2026, 10, 12), dt.date(2026, 10, 9)),                                          # Monday -> Friday (weekend)
    (dt.date(2026, 11, 27), dt.date(2026, 11, 25))])                                        # Friday after Thanksgiving -> Wednesday
def test_spy_base_is_the_last_completed_close_before_the_start_date(start, base):           # ruling 14, owner answer
    assert build_report(inputs_with_spy(start=start))["benchmarks"]["spy"]["base_date"] == base.isoformat()
def test_labels_for_spy_and_eod_drawdown_are_present():
    r = build_report(full_inputs())
    assert r["benchmarks"]["spy"]["label"] == "SPY price only; dividends excluded"
    assert r["account"]["eod_drawdown_label"] == "drawdown from end-of-day equity; intraday lows may be missed"
    assert "kill" not in json.dumps(r["account"]).lower()
def test_session_with_unknown_nlv_is_counted_and_excluded_from_drawdown(): ...
def test_row_commissions_include_adjustments_and_stay_unknown_until_complete(): ...
def test_unknown_ai_cost_makes_pnl_minus_cost_unknown(): ...
def test_no_ai_cost_rows_is_unavailable_not_zero(): ...                   # ruling 16: null, UNAVAILABLE
def test_pnl_minus_ai_cost_uses_account_pnl_in_usd(): ...
def test_fill_outside_session_is_listed_as_a_warning(): ...
def test_simulated_rows_are_shown_with_the_simulated_label(): ...
def test_service_report_rebuilds_round_trips_first(service, seed_round_trip): ...   # report() after a new fill shows it
def test_report_for_a_named_experiment_ignores_other_experiments(): ...
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** (pure functions first; `ScoreboardService.report` loads rows with `store.fetch`, calls `build_report`). Money in the report is rounded only at the edge (`round(x, 6)`), never inside the metrics.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add scoreboard metrics and report`.

---

### Task 6: `verify`

**Files:**
- Modify: `trader/scoreboard/service.py`
- Test: `tests/scoreboard/test_verify.py`

**Interfaces:**
- Produces: `ScoreboardService.verify(experiment_id: str | None = None) -> dict` → `{"ok": bool, "checked": {"seals": n, "round_trips": n, "sessions": n}, "mismatches": [ {"check", "table", "key", "stored", "recomputed"} ], "incidents": [...]}`. `ok` is `False` for any mismatch; open incidents are listed but do not flip `ok` (they are already loud).
- Checks (each is one `check` code): `CHAIN_BROKEN`, `ROW_EDITED`, `ROW_MISSING`, `ROW_UNSEALED` (Task 1); `ROUND_TRIP_MISMATCH` (stored vs recomputed from `broker_fills`, ignoring nothing but `projected` bookkeeping); `ROUND_TRIP_MISSING` / `ROUND_TRIP_EXTRA`; `SESSION_FILLS_CHANGED` (recomputed `fills_digest` of the session, or `fill_count`/`trade_count`/`realized_pnl_usd` differ from the row); `COMMISSION_MISMATCH` (current per-exec commissions ≠ `commission_json` + adjustments); `ATTRIBUTION_CHANGED` (stored `links_digest` ≠ `links_for_order_ref(...).digest` now, or a link vanished).

- [ ] **Step 1: Write the failing tests**

```python
def test_clean_books_verify_ok(service_with_two_sessions): assert service.verify()["ok"] is True
def test_edited_equity_row_is_detected(service, db):
    db.execute("UPDATE equity_daily SET end_nlv_usd = end_nlv_usd + 1")
    assert "ROW_EDITED" in checks(service.verify())
def test_edited_adjustment_benchmark_ai_cost_and_simulated_rows_are_detected(service, db, table): ...   # parametrized over 4 tables
def test_edited_round_trip_row_is_detected(service, db):
    db.execute("UPDATE round_trips SET net_pnl_usd = 999"); assert "ROUND_TRIP_MISMATCH" in checks(service.verify())
def test_edited_fill_price_is_detected_through_the_fills_digest(service, db):
    db.execute("UPDATE broker_fills SET price = price + 1 WHERE exec_id = 'e1'")
    assert "SESSION_FILLS_CHANGED" in checks(service.verify())
def test_edited_commission_without_an_adjustment_is_detected(service, db): ...   # COMMISSION_MISMATCH
def test_late_commission_with_its_adjustment_verifies_ok(service, set_commission):
    set_commission("e1", "1.30"); service.refresh(); assert service.verify()["ok"] is True
def test_changed_decision_record_is_detected(service, links): links.digest = "other"; assert "ATTRIBUTION_CHANGED" in checks(service.verify())
def test_verify_with_no_experiment_checks_only_the_seals(service_empty): assert service_empty.verify()["ok"] is True
def test_incidents_are_listed_but_do_not_fail_verify(service, store): store.record_incident("FX_EVIDENCE_MISSING","k","d"); r = service.verify(); assert r["ok"] and r["incidents"]
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `verify` calls `store.verify_seals()`, then recomputes with Tasks 2–3 pure code over a fresh `load_fill_facts`, never over stored derived rows, and never writes.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add scoreboard verify`.

---

### Task 7: Typed queries, ACL, SDK and `mmr` commands

**Files:**
- Create: `trader/messaging/scoreboard_surface.py`, `tests/scoreboard/test_surface.py`, `tests/scoreboard/test_cli.py`
- Modify: `trader/messaging/principals.py` (`TRADER_ACL`, from Plan 2 Task 4), `trader/messaging/production_api.py:2171` (next to `register_cli_surface`), `trader/sdk.py`, `trader/mmr_cli.py:1466` (parser), `:2588` (dispatch), `:11997` (`_LOCAL_ONLY_COMMANDS`)

**Interfaces:**
- Produces:
  - `get_experiment_trips` (ruling 19), registered by the same function.
  - `scoreboard_surface.register_scoreboard_surface(registry, service) -> None`. Registers `('query','get_scoreboard')` with `GetScoreboardRequest(experiment_id: str | None = None, extra=forbid)` returning `service.report(...)` after `service.refresh()`, and `('query','verify_scoreboard')` with the same body returning `service.verify(...)`. Both run on the thread execution lane (DuckDB work). A `service` of `None` registers nothing (no experiment stack on this trader).
  - ACL (Plan 2 tables, explicit sets per method as Plan 3 R23 requires): `("query","get_scoreboard"): frozenset({"cli", "dashboard", "ai_supervisor"})`, `("query","verify_scoreboard"): frozenset({"cli", "dashboard"})`. `ai_research`, `strategy` and `trader` have neither (spec 5.3 table).
  - SDK: `MMR.scoreboard(experiment_id: str | None = None) -> dict`, `MMR.verify_scoreboard(experiment_id=None) -> dict`.
  - CLI: `mmr --json scoreboard [--experiment ID]` → `{"data": report, "title": "Scoreboard (paper)"}`; table mode prints the summary lines with `-` for unknown and the PAPER label first. `mmr scoreboard verify` prints mismatches and exits `1` when `ok` is false, `0` otherwise. `scoreboard` joins `_LOCAL_ONLY_COMMANDS` (typed RPC only, no legacy 42001) and is **not** in the IB-upstream-gated set (`mmr_cli.py:2068`): it reads the journal, not IB.

- [ ] **Step 1: Write the failing tests**

```python
def test_get_scoreboard_registered_with_acl(registry_with_acl, service):        # Plan 2 TypedRpcRegistry(acl=TRADER_ACL)
    register_scoreboard_surface(registry_with_acl, service)                    # raises if an entry were missing
def test_every_principal_outcome_for_get_scoreboard(served):                   # real server fixture from Plan 2 tests
    for p in ("cli", "dashboard", "ai_supervisor"): assert served.client(p).call("get_scoreboard", {}, dict)["label"] == "PAPER"
    for p in ("ai_research", "strategy"): assert code(served.client(p), "get_scoreboard") == "PERMISSION_DENIED"
def test_verify_scoreboard_is_human_only(served): ...                           # ai_supervisor -> PERMISSION_DENIED
def test_get_experiment_trips_returns_identity_and_quantity_per_trip(served_with_fills):   # R19
    trips = served_with_fills.client("ai_supervisor").call("get_experiment_trips", {"experiment_id": "exp1"}, dict)["trips"]
    assert [(t["conid"], t["closed_quantity"], t["state"]) for t in trips] == [(265598, 3, "CLOSED")]
def test_get_experiment_trips_acl_and_unknown_experiment(served): ...           # ai_research, strategy -> PERMISSION_DENIED; unknown id -> EXPERIMENT_NOT_FOUND
def test_unknown_field_in_the_body_is_a_validation_error(served): ...
def test_no_scoreboard_service_registers_nothing(registry): register_scoreboard_surface(registry, None); assert not registry.registrations()
def test_get_scoreboard_refreshes_before_reading(service_spy): assert service_spy.calls == ["refresh", "report"]
def test_ai_principals_have_no_write_path_to_scoreboard_tables(): ...           # table test: no ACL entry for record_ai_cost etc. (SP2 adds them)
# test_cli.py
def test_json_output_wraps_data_and_title(capsys, fake_mmr): ...
def test_table_output_prints_paper_label_first_and_dash_for_unknown(capsys, fake_mmr): ...
def test_verify_exits_1_on_mismatch_and_0_when_clean(fake_mmr): ...
def test_scoreboard_does_not_probe_ib_upstream(): assert "scoreboard" not in IB_UPSTREAM_COMMANDS and "scoreboard" in _LOCAL_ONLY_COMMANDS
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** In `production_api.py` after `register_cli_surface(registry, api)`: `register_scoreboard_surface(registry, getattr(api.trader, "scoreboard_service", None))`. Add both ACL entries to `TRADER_ACL` in the same commit (Plan 2's `test_every_production_trader_method_has_an_entry` runs over the full registry).
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: expose scoreboard over typed rpc and mmr`.

---

### Task 8: `/cc` Scoreboard tab

**Files:**
- Create: `web/command_center/routes_scoreboard.py`, `web/templates/_scoreboard_tab.html`, `web/static/command_center_scoreboard.js`, `web/static/command_center_scoreboard.test.js`, `tests/test_command_center_scoreboard_js.py`, `tests/test_web_scoreboard_routes.py`
- Modify: `web/app.py:1733` (include router next to `create_research_router`), `web/templates/command_center.html:1801-1809` (tab button), `:2158` (pane + include), `:2334` (script tag), `web/static/dash_admin.js:148` (`normalizeDashTab`), `web/static/dash_admin.test.js`

**Interfaces:**
- Produces: `routes_scoreboard.create_scoreboard_router(cc) -> APIRouter` with `GET /api/scoreboard?experiment_id=` (session required via `cc.require_session`, as in `routes_research.py:44-46`). It calls `cc._query_client.call("get_scoreboard", body, dict, timeout=8)` through `asyncio.to_thread` (pattern at `routes_read.py:138-146`). Errors are loud, never an empty 200: no query client → `503 {"error":{"code":"TRADER_UNAVAILABLE",...}}`; `TypedRpcRemoteError` `PERMISSION_DENIED` → `403`; other remote error or timeout → `502` with the remote `code`; an unknown query parameter → `422` (as `routes_research.reject_unknown`). The route never touches a DuckDB file.
- JS: `command_center_scoreboard.js` exports (CommonJS for the test, `window.CCScoreboard` in the browser) `formatMoney(v)`, `formatPct(v)`, `formatNumber(v, digits)`, `escapeHtml(s)`, `renderScoreboard(report) -> string`. Unknown (`null`/`undefined`/`NaN`) renders `—`; `0` renders `0.00`. The rendered HTML always starts with the `PAPER` banner and the disclaimer. Sections: Account (return, end-of-day drawdown with its label, Sharpe with the `SMALL_SAMPLE` warning), Benchmarks (SPY price return with its label "SPY price only; dividends excluded", vs SPY, simulated baseline labelled `simulated`, AI cost, P&L minus AI cost), Trades (closed/open, win rate, profit factor, fees, turnover, unresolved-fee count), Splits (three tables), Sessions table (end state badge: `FLAT`, `KILLED`, `FAILED_SAFE`, `UNKNOWN`), Warnings and Incidents, Telegram line (`disabled` / `N pending, last sent <time>`). All report strings go through `escapeHtml`.

- [ ] **Step 1: Write the failing tests**

```js
// web/static/command_center_scoreboard.test.js (node, same style as command_center_research.test.js)
test('unknown renders as a dash and zero renders as zero', () => {
  assert.equal(formatMoney(null), '—'); assert.equal(formatMoney(NaN), '—'); assert.equal(formatMoney(0), '$0.00');
  assert.equal(formatPct(undefined), '—'); assert.equal(formatPct(0), '0.00%'); });
test('negative money keeps its sign', () => assert.equal(formatMoney(-12.5), '-$12.50'));
test('an empty report renders without throwing and still says paper', () => {
  const html = renderScoreboard(EMPTY_REPORT); assert.match(html, /PAPER/); assert.match(html, /proof of live edge/); assert.doesNotMatch(html, /NaN|undefined|null/); });
test('a null report renders a no-experiment notice', () => assert.match(renderScoreboard(null), /No experiment/));
test('small-sample warning is shown', () => assert.match(renderScoreboard(R_WITH_WARNING), /fewer than 60 sessions/));
test('simulated baseline is labelled simulated', () => assert.match(renderScoreboard(R_WITH_SIM), /simulated/));
test('unavailable ai cost and simulated book say unavailable, not zero', () => { const h = renderScoreboard(EMPTY_REPORT); assert.match(h, /AI cost[^<]*unavailable/i); assert.doesNotMatch(h, /AI cost[^<]*\$0\.00/); });
test('labels for SPY and drawdown are shown', () => { const h = renderScoreboard(FULL_REPORT); assert.match(h, /dividends excluded/); assert.match(h, /intraday lows may be missed/); });
test('killed and failed-safe sessions are visibly different from flat', () => {
  const html = renderScoreboard(R_WITH_THREE_STATES); for (const s of ['FLAT','KILLED','FAILED_SAFE']) assert.match(html, new RegExp(`data-end-state="${s}"`)); });
test('report strings are escaped', () => assert.doesNotMatch(renderScoreboard(R_WITH_SCRIPT_IN_SYMBOL), /<script>/));
test('unknown group keys render as unattributed', ...);
test('telegram line shows disabled, pending and last sent', ...);
// dash_admin.test.js: normalizeDashTab('scoreboard') === 'scoreboard' and the hash #scoreboard activates dash-scoreboard
```
```python
# tests/test_web_scoreboard_routes.py (FastAPI TestClient with a fake cc, as tests/test_web_dashboard.py does)
def test_requires_a_session(): ...                                  # 401 without a session cookie
def test_returns_the_trader_report_as_is(): ...
def test_trader_down_is_503_not_an_empty_report(): ...
def test_permission_denied_is_403(): ...   def test_timeout_is_502_with_a_code(): ...
def test_unknown_query_parameter_is_422(): ...
def test_page_contains_the_scoreboard_tab_button_and_pane(client): assert 'data-dash-tab="scoreboard"' in html and 'id="dash-scoreboard"' in html
```
`tests/test_command_center_scoreboard_js.py` is the node wrapper (copy of `tests/test_command_center_research_js.py`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The tab fetches `/api/scoreboard` when first activated and every 60 s while active; a fetch error shows the router's `error.code` and message in the tab, with the last good report dimmed (never blank).
- [ ] **Step 4: Run, expect PASS** (also run `node web/static/command_center_scoreboard.test.js` and `node web/static/dash_admin.test.js`), then the full suite. If a headless check is wanted, use the `verify` skill recipe for `/cc`.
- [ ] **Step 5: Commit** — `feat: add scoreboard tab to the command center`.

---

### Task 9: Telegram config gate, outbox, sender and summary text

**Files:**
- Create: `trader/scoreboard/telegram_config.py`, `telegram_outbox.py`, `telegram_sender.py`, `summary_text.py`; `tests/scoreboard/test_telegram_config.py`, `test_telegram_outbox.py`, `test_telegram_sender.py`, `test_summary_text.py`

**Interfaces:**
- Produces:
  - `telegram_config.TelegramConfig(chat_id: str, token: str = field(repr=False))`; `load_telegram_config(section: Mapping | None) -> TelegramConfig | None`; `TelegramConfigError(Exception)` (messages never contain token text).
  - `telegram_outbox.TelegramOutbox(db, now)`: `enqueue(event_id: str, kind: str, text: str) -> bool` (`False` if the id exists), `due(limit=10) -> list[OutboxRow]`, `mark_sent(event_id, message_id)`, `mark_failed(event_id, error: str)` (attempts + 1; `next_attempt_at = now + min(30 * 2**attempts, 3600) s`), `counts() -> {"pending": int, "last_sent_at": datetime | None}`.
  - `telegram_sender.TelegramSender(outbox, config, *, post, redact_extra=())` with `drain() -> int` (messages sent). `post(url: str, payload: dict) -> PostResult(status: int, message_id: int | None, retry_after: float | None)`; `http_post(...)` is the `httpx` implementation (10 s timeout, no redirects). URL is `https://api.telegram.org/bot<token>/sendMessage`; payload `{"chat_id": config.chat_id, "text": text}` only (no `parse_mode`).
  - `summary_text.format_daily_summary(report: dict, session_date: date) -> str` and `DailySummaryProducer(service, outbox, experiments)` with `on_session_row(experiment_id, session_date) -> bool` that enqueues `daily_summary:{experiment_id}:{session_date}` (kind `daily_summary`). No kill-alert text here: Plan 4 K18 owns `kill_alert_text` and the event id `kill_started:{experiment_id}:{kill_seq}`.

- [ ] **Step 1: Write the failing tests**

```python
# config gate
def test_missing_or_disabled_section_means_off(): assert load_telegram_config(None) is None and load_telegram_config({"enabled": False}) is None
def test_disabled_section_ignores_unset_fields(): assert load_telegram_config({"enabled": False, "chat_id": None, "token_secret_file": ""}) is None
@pytest.mark.parametrize("section", [
  {"enabled": True}, {"enabled": True, "chat_id": None, "token_secret_file": "x"},
  {"enabled": True, "chat_id": 0, "token_secret_file": "x"}, {"enabled": True, "chat_id": True, "token_secret_file": "x"},
  {"enabled": True, "chat_id": "abc", "token_secret_file": "x"}, {"enabled": True, "chat_id": 5, "token_secret_file": ""},
  {"enabled": "yes", "chat_id": 5, "token_secret_file": "x"}, {"enabled": True, "chat_id": 5, "token_secret_file": "x", "chatid": 1}])
def test_enabled_but_incomplete_or_malformed_fails_loudly(section): with pytest.raises(TelegramConfigError): load_telegram_config(section)
def test_token_file_must_exist(tmp_path), must_not_be_a_symlink, must_be_mode_0600 (0644 refused), must_not_be_empty, must_look_like_a_bot_token
def test_valid_config_reads_the_token_and_strips_whitespace(tmp_path): ...
def test_config_repr_and_errors_never_contain_the_token(tmp_path): ...
@pytest.mark.parametrize("chat", [123456789, "-1001234567890", "@my_channel"]) def test_accepts_numeric_and_channel_ids(chat, tmp_path): ...

# outbox
def test_enqueue_is_idempotent_per_event_id(outbox): assert outbox.enqueue("e1","k","t") is True and outbox.enqueue("e1","k","other") is False and len(outbox.due()) == 1
def test_failed_send_backs_off_and_stays_pending(outbox, clock): ...      # not due until next_attempt_at; 30 s, 60 s, ... capped 3600 s
def test_sent_row_is_never_due_again(outbox): ...
def test_counts_report_pending_and_last_sent(outbox): ...

# sender (fake post)
def test_sends_once_per_event_id_and_marks_sent(sender, post): sender.drain(); sender.drain(); assert len(post.calls) == 1
def test_chat_id_is_always_the_configured_one(sender, post): assert post.calls[0].payload["chat_id"] == CONFIG.chat_id and set(post.calls[0].payload) == {"chat_id","text"}
def test_outage_delays_then_delivers_without_duplicates(sender, post, clock): post.fail_next(2); ...   # 3 drains over time -> exactly 1 success
def test_http_429_honours_retry_after(sender, post, clock): ...
def test_http_4xx_keeps_the_row_pending_and_logs_an_error(sender, post, caplog): ...   # bad token / chat not found: loud, not dropped
def test_transport_error_text_never_contains_the_token(sender, post, caplog, db):
    post.raise_next(httpx.ConnectError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage"))
    sender.drain()
    assert TOKEN not in caplog.text and TOKEN not in last_error(db)
def test_one_failing_row_does_not_block_the_next(sender, post): ...
def test_rows_marked_pending_after_a_crash_are_resent_once_with_their_event_id_in_the_text(...): ...
def test_disabled_gate_means_no_sender_and_no_http(): ...                  # build_telegram(None, ...) -> (None, None)
def test_http_post_uses_no_redirects_and_a_timeout(monkeypatch): ...

# text
def test_summary_has_paper_label_end_state_and_unknowns_as_dash(): ...
def test_summary_first_line_is_the_end_state_for_all_three(): ...          # "PAPER — FLAT" / "PAPER — KILLED" / "PAPER — FAILED_SAFE"
def test_unknown_row_sends_no_summary(): ...
def test_summary_carries_the_spy_label_and_never_says_exactly_once(): ...
def test_summary_is_plain_text_and_ends_with_the_event_id(): ...
def test_summary_over_4000_chars_is_cut_with_a_notice(): ...
def test_producer_enqueues_one_message_per_session_row(): ...              # twice -> one outbox row
def test_producer_for_a_session_without_a_row_enqueues_nothing(): ...
def test_outbox_accepts_a_plan4_kill_alert(outbox): ...                   # enqueue("kill_started:E:1", "kill_started", t) is True; same id again is False
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `_redact(text) = text.replace(token, "<token>")`, applied to every log line, exception text and `last_error` before they leave the sender; `http_post` never logs the URL. The token file check: `os.lstat` regular file (not symlink), `st_mode & 0o077 == 0`, content `.strip()` matching `^\d+:[A-Za-z0-9_-]{20,}$`. `build_telegram(section, db, now)` returns `(None, None)` when off, else `(outbox, sender)`.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add telegram config gate, outbox and daily summary sender`.

---

### Task 10: Production wiring, compose secret mount and docs

**Files:**
- Modify: `trader/trading/command_stack.py` (migrations at :887-893, attributes at :1085-1094), `trader/trader_service.py` (loops, after `_maybe_start_session_recovery` :344-369), `docker-compose.yml` (trader service volumes), `AGENTS.md`, `docs/OPERATIONAL_STATE.md`
- Create: `tests/scoreboard/test_wiring.py`, `tests/test_compose_telegram_secret_mount.py`

**Interfaces:**
- Consumes: everything above; Plan 4 `stack.experiments.store` (A1) and `stack.experiments.monitor.attach_notices(alerts=outbox, session_end=ledger)` (A3, A4; `alerts=None` when Telegram is off), Plan 3 `trader.ai_paper_attribution` through a new `ports.DecisionStoreAttribution` adapter (A2), `trader.ai_paper_config.raw_section` (A5). When `stack.experiments` or `stack.ai_paper` is `None`, the wiring uses `NullExperimentReader` (always `latest() -> None`) and `NullAttributionLookup`, so the loops idle and nothing is written.
- Produces:
  - `command_stack`: `apply_scoreboard_migrations(migrator)`; builds `ScoreboardStore`, `SessionLedger` (passed to `SessionController(on_terminal=ledger.on_controller_terminal)`), `BenchmarkBook`, `ScoreboardService`, and `trader.scoreboard_service`, `trader.session_ledger`, `trader.telegram_outbox`. `load_telegram_config(...)` runs here, so an enabled-but-bad Telegram section stops the stack build (ruling 17).
  - `trader_service`: `_scoreboard_loop(trader, interval=30.0)` on `_watched_ticks` (same helper as the session loop) running `ledger.observe_snapshot(broker.capture(account))`, `service.refresh()`, `book.refresh(...)` (benchmark failures are logged, never raised), and, when Telegram is on, `sender.drain()`, all in `asyncio.to_thread`. `ledger.recover(now)` runs once before the session loop starts, next to `_maybe_start_session_recovery`.
  - Compose: the trader service mounts `${HOME}/.config/mmr/secrets` read-only at the same container path; no other service mounts it. A directory mount is used on purpose: a missing single-file bind mount makes Docker create a directory in its place (same trap as Plan 2 Review Focus 4).

- [ ] **Step 1: Write the failing tests**

```python
def test_build_stack_exposes_the_scoreboard_service_and_listener(stack): assert trader.scoreboard_service is not None and stack.session_controller._on_terminal is not None
def test_flat_session_through_the_real_controller_writes_a_row_and_one_summary(prod_stack, telegram_on):   # production composition, fakes only for broker/IB/http
    drive_controller_to_flat(prod_stack)
    assert len(rows("equity_daily")) == 1 and [r.event_id for r in outbox_rows()] == [f"daily_summary:{EXP}:{D}"]
def test_restart_after_row_written_still_enqueues_the_summary(prod_stack, telegram_on):   # kill the process between row and enqueue
    ...; rebuilt = rebuild_stack(prod_stack); rebuilt.ledger.recover(NOW) / loop tick; assert one outbox row, one equity row
def test_telegram_off_writes_rows_but_no_outbox_rows_and_no_sender(prod_stack): ...
def test_telegram_enabled_without_a_chat_id_stops_stack_build(prod_stack_config): with pytest.raises(TelegramConfigError): build(...)
def test_benchmark_failure_does_not_stop_the_loop_and_is_logged(...): ...
def test_loop_idles_without_an_experiment(...): ...                  # NullExperimentReader: no rows, no HTTP
def test_kill_notices_are_attached_to_the_plan4_monitor(prod_stack, telegram_on): ...   # a kill enqueues kill_started:{EXP}:1 and writes a KILLED row
def test_decision_store_attribution_returns_the_single_enter_link(): ...   # close links and no link -> None
def test_scoreboard_tables_live_in_the_journal_db_not_the_research_db(prod_stack): ...
def test_a_sender_failure_never_blocks_the_session_controller_tick(...): ...
# compose
def test_only_the_trader_mounts_the_secrets_dir_read_only(): ...     # parses docker-compose.yml like tests/test_compose_rpc_keys.py (Plan 2 Task 6)
def test_no_other_service_mounts_the_secrets_dir_or_the_journal(): ...
```
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Docs: `AGENTS.md` gets the `mmr --json scoreboard` / `scoreboard verify` lines in "CLI Commands", a "Scoreboard" paragraph in "Architecture" (tables, seals, USD rule, unknown rule, Telegram gate keys), and the `scoreboard` row in the "No service needed"/"Requires trader typed RPC" lists (it is the latter). `docs/OPERATIONAL_STATE.md` gets: where the token file lives (`~/.config/mmr/secrets/telegram.token`, mode `0600`, owner creates it), that `enabled: true` without it stops the trader at start, that nothing is sent while `enabled: false`, and that delivery is at-least-once (a repeat carries the same event id). No token or chat id appears in any file.
- [ ] **Step 4: Run, expect PASS**, then the full suite and `node web/static/command_center_scoreboard.test.js`.
- [ ] **Step 5: Commit** — `feat: wire scoreboard, telegram summary and secret mount into the trader`.

---

## Self-review against the spec

| Spec 5.2 / 6 / 5.5 requirement | Task |
|---|---|
| `experiments` table | Plan 4 (read via A1) |
| `equity_daily`: one row per session end in `FLAT`/`KILLED`/`FAILED_SAFE`, start/end NLV, realized P&L, commissions, peak gross, trade count, open positions, `session_end_state` | 1, 3 |
| `round_trips` projection from `broker_fills`, partial exits in one trip, attribution by stored links | 1, 2 |
| `simulated_books` slot, labelled `simulated`, SP2 fills | 1, 4, 8 |
| `benchmark_prices`: exact SPY closes, provider, bar date, fetch time, version; refresh never changes a row | 1, 4 |
| `ai_costs` table, SP2 fills | 1, 4 |
| `equity_adjustments`: late commission, old row never edited | 1, 3 |
| Currency: base + USD, FX rate/source/time, no FX = incident, never written as USD | 3 |
| Benchmarks: SPY from start, simulated baseline, AI cost, "P&L minus AI cost" | 4, 5 |
| Metrics: return vs SPY, max drawdown, daily Sharpe (small-sample warning below 60), profit factor, win rate, turnover, fees; split by strategy/decider/style | 5 |
| AI principals read-only scoreboard methods | 7 |
| `mmr scoreboard verify` rebuilds every number from stored inputs; mismatch is an incident | 1, 6, 7 |
| No AI service mounts the trader database | 7 (typed RPC only), 10 (compose test) |
| `mmr --json scoreboard` | 7 |
| Scoreboard tab on `/cc` | 8 |
| Send-only Telegram summary, durable outbox, stable event ids, token from secret file, only configured chat, outage only delays | 9, 10 |
| Every view says "paper" | 5, 7, 8, 9 |
| Kill alert goes out through the outbox (producer and text are Plan 4) | 9 (`enqueue`), 10 (`attach_notices`) |
| Section 6 scoreboard tests: projection rebuild matches, partial exits one trip, `verify` detects an edited row in any input table, late commission = adjustment, non-USD base without FX = incident, refresh does not change a stored benchmark, outbox sends once per event id and retries after an outage | 2, 6, 3, 3, 4, 9 |
| Section 6: `equity_daily` row after `KILLED` and after `FAILED_SAFE` | 3 (the listener and the A3 notice; the kill-side call is Plan 4's test) |
| Telegram identity/chat id are owner-supplied outside chat; never hard-coded | 9 (config only), open question 3 |

Placeholder scan: test lines written as `...` inside a test body name the exact scenario in their comment or title; the implementer writes the body from the title and the fixtures of the same file (these cover input classes already pinned by an adjacent fully written test). No "TBD" or "later" remains.

## Open questions for the owner

All nine questions were answered by the owner on 2026-10-06 (rulings 5, 6, 14, 15, 16, 17; the migration range is settled). None open.
